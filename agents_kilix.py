"""Carry out admitted agents-job actions through Kilix's own tools.

- A launch is `kilix agent-control new-tab|split`, anchored to the caller's
  own pane (its broker identity checked), with the directory resolved here.
  It records the client's trust for exactly that directory
  (`--trust-folder`), and adds an approval skip only when Kilix's
  coding-yolo setting is on (`--coding-yolo`; the request never decides).
- A wait is `kilix panes wait PANE --for idle|waiting`. Prompted and resumed
  launches first observe `working`. A message sent while working must cross
  idle then working before a following wait, because panes/v1 has no turn id.
- A message is `kilix agent-control send PANE --expect-broker B --submit`,
  after an optional wait for idle. It is held only while the session is
  `waiting` on an approval or a menu, where a submitted line could answer
  it. Steering while working is allowed only for readers that distinguish
  `waiting` from events seen in real transcripts (Claude, Grok); others (Codex
  until its approval events are seen with approvals on, qwen-omp, Kimi) take
  messages only idle.

Where Kilix's own reader leaves a state unknown (`agent`) for a Claude or Codex
pane, or lists no coding session because the agent runs inside tmux, the screen
text and tmux's own process tree are read as extra evidence (`agents_detect`).
They never widen identity: a tmux-hosted session is the one agent process in one
exact tmux pane of the session the pane's own tmux client shows, in exactly the
requested directory, and a message is sent only while tmux shows that pane.

Directories resolve to exactly one existing directory: an explicit path, the
caller's directory ("here"), a name or alias in ~/.config/kilix-needle/dirs.json,
or a repository name found under ~/gpu_terminal (never ~/research, never a
generic name). Scanned names shorter than three characters and names that are
not unique across every scanned depth refuse. Sessions resolve to exactly one
live agent pane in that directory. Ambiguity refuses; nothing is created.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess
import time

import agents
import agents_detect as detect
import kilix

HERE = ("here", "this repo", "this directory", "the current folder", "this folder",
        "the current directory", "this project")
# Only the source tree is scanned for a bare name: ~/research holds review
# seats, reference clones and test checkouts whose names are generic ("base",
# "checkout"), so it is reached by path or dirs.json (review R14 round 4, 54).
ROOTS = ("gpu_terminal",)
# Names too generic to pick a checkout by, whatever the scan finds.
_GENERIC_NAMES = frozenset(("base", "checkout", "repo", "repository", "src", "source", "main",
                            "master", "test", "tests", "work", "code", "project", "app", "lib",
                            "tmp", "temp", "old", "new", "copy", "clone", "review", "seat",
                            "reference", "refs", "agents", "docs"))
PROVIDER_AGENT = {"claude": "claude", "codex": "codex", "grok": "grok", "omp": "qwen-omp",
                  "kimi": "kimi"}
_SCAN_SKIP = frozenset(("node_modules", "venv", "virtualenv", "env", "scratch",
                        "scratch-workers", "worktree", "worktrees"))
_DIR_SCAN_CACHE: dict[Path, tuple[float, tuple[tuple[str, Path, int], ...]]] = {}
_DIR_SCAN_TTL = 60.0
# Codex's approval events are read (008c8d6) but not yet seen live with
# approvals on, and codex has other modals the reader doesn't cover; until a
# live check, codex takes messages only when idle (R14 round 4, 55).
_WORKING_STEER_AGENTS = frozenset(("claude", "grok"))


POLL_SECONDS = 1.0


class AgentsError(RuntimeError):
    pass


def _run(argv: list[str], timeout: float = 30) -> str:
    try:
        done = subprocess.run([kilix.KILIX, *argv], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AgentsError(f"kilix {argv[0]} failed: {error}") from error
    if done.returncode != 0:
        raise AgentsError(done.stderr.strip() or f"kilix {argv[0]} exited {done.returncode}")
    return done.stdout


def _tmux(args: list[str]) -> str:
    """tmux with exactly these arguments; a failure is an error the caller treats as no answer."""
    try:
        done = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AgentsError(f"tmux {args[0] if args else ''} failed: {error}") from error
    if done.returncode != 0:
        raise AgentsError(done.stderr.strip() or f"tmux exited {done.returncode}")
    return done.stdout


def _screen(pane_id) -> str:
    """The visible text of a pane, as evidence only; unreadable is empty."""
    try:
        return kilix._run(["get-text", "--match", f"id:{pane_id}", "--extent", "screen"])
    except kilix.KilixError:
        return ""


def _skip_scan_dir(name: str) -> bool:
    low = name.casefold()
    return (name.startswith(".") or low in _SCAN_SKIP or low.startswith(("scratch-", "worktree-"))
            or low.endswith(("-scratch", "-worktree", "-worktrees")))


def _git_directories(home: Path) -> tuple[tuple[str, Path, int], ...]:
    """Briefly cached, bounded index of checkout roots below the source roots."""
    key = home.resolve()
    now = time.monotonic()
    cached = _DIR_SCAN_CACHE.get(key)
    if cached is not None and now - cached[0] < _DIR_SCAN_TTL:
        return cached[1]
    found = []
    for root in ROOTS:
        base = key / root
        if not base.is_dir() or base.is_symlink():
            continue
        pending = [(base, 0)]
        while pending:
            candidate, depth = pending.pop()
            # A directory-valued .git marks a top-level checkout. Do not walk
            # into it: nested .git files are submodules/worktrees, not another
            # checkout for name resolution.
            if (candidate / ".git").is_dir():
                found.append((candidate.name, candidate.resolve(), depth))
                continue
            if depth >= 3:
                continue
            try:
                children = candidate.iterdir()
                directories = [child for child in children
                               if child.is_dir() and not child.is_symlink()
                               and not _skip_scan_dir(child.name)]
            except OSError:
                continue
            pending.extend((child, depth + 1) for child in directories)
    answer = tuple(found)
    _DIR_SCAN_CACHE[key] = (now, answer)
    return answer


def _directory_map(home: Path, names: tuple[str, ...]) -> Path | None:
    """Resolve one exact configured name or alias."""
    map_file = home / ".config" / "kilix-needle" / "dirs.json"
    if not map_file.exists():
        return None
    try:
        mapped = json.loads(map_file.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AgentsError(f"cannot read directory map {map_file}: {error}") from error
    if not isinstance(mapped, dict):
        raise AgentsError(f"directory map {map_file} must be a JSON object")
    for name in names:
        if name not in mapped:
            continue
        value = mapped[name]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise AgentsError(f"directory map entry {name!r} must be an absolute path")
        path = Path(value).resolve()
        if not path.is_dir():
            raise AgentsError(f"directory map entry {name!r} is not an existing directory")
        return path
    return None


def resolve_dir(said: str, *, cwd: str | None = None, home: Path | None = None) -> Path:
    """Exactly one existing directory for the words the request used."""
    home = home or Path.home()
    raw = said.strip()
    if raw.startswith(("~/", "/")):
        path = ((home / raw[2:]) if raw.startswith("~/") else Path(raw)).resolve()
        if not path.is_dir():
            raise AgentsError(f"{said} is not an existing directory")
        return path
    folded = agents._fold(said)
    if folded in HERE:
        if not cwd:
            raise AgentsError("the calling pane's directory is unknown; 'here' cannot be resolved")
        path = Path(cwd).resolve()
        if not path.is_dir():
            raise AgentsError(f"the calling pane's directory is not available: {cwd}")
        return path
    name = raw
    if name.casefold().startswith("the "):
        name = name[4:]
    for suffix in agents._DIR_SUFFIX:          # the words the checks take after a name
        if name.casefold().endswith(" " + suffix):
            name = name[:-len(suffix) - 1]
            break

    configured = _directory_map(home, tuple(dict.fromkeys((raw, name))))
    if configured is not None:
        return configured

    found = [] if len(name) < 3 or name.casefold() in _GENERIC_NAMES else sorted({path for repo_name, path, _depth
                                             in _git_directories(home)
                                             if repo_name == name})
    if len(found) != 1:
        candidates = "none" if not found else ", ".join(map(str, found))
        raise AgentsError(
            f"{said}: {'no' if not found else len(found)} matching directories; "
            f"candidates: {candidates}; add the intended name or alias to "
            "~/.config/kilix-needle/dirs.json")
    return found[0]


def _listing(argv: list[str]) -> dict:
    try:
        listing = json.loads(_run(argv))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise AgentsError(f"kilix {argv[0]} returned invalid JSON: {error}") from error
    if not isinstance(listing, dict):
        raise AgentsError(f"kilix {argv[0]} returned an invalid listing")
    return listing


def caller_info() -> tuple[int, str, str | None]:
    """This process's own pane, broker identity and pane cwd."""
    listing = _listing(["agent-control", "list"])
    pane = listing.get("caller_pane")
    for item in listing.get("panes", []):
        if isinstance(item, dict) and item.get("pane_id") == pane and item.get("broker"):
            cwd = item.get("cwd") if isinstance(item.get("cwd"), str) else None
            return pane, str(item["broker"]), cwd
    raise AgentsError("the caller's own pane is unknown; run from a Kilix pane")


def caller() -> tuple[int, str]:
    pane, broker, _cwd = caller_info()
    return pane, broker


def calling_cwd() -> str | None:
    return caller_info()[2]


def _session_identity(pane: dict) -> dict | None:
    """What Kilix says a coding session is: provider, directory, session and live pids."""
    coding = pane.get("coding_session")
    if not isinstance(coding, dict):
        return None
    cwd = coding.get("cwd") or pane.get("cwd") or ""
    try:
        cwd = str(Path(cwd).resolve()) if cwd else ""
    except OSError:
        cwd = ""
    pids = coding.get("live_pids")
    return {"provider": PROVIDER_AGENT.get(str(coding.get("provider") or ""), ""), "cwd": cwd,
            "session_id": coding.get("session_id"),
            "pids": tuple(pids) if isinstance(pids, list) else ()}


def _same_identity(known: dict, now: dict) -> bool:
    """The session found is the one sent to: provider and directory, and whatever Kilix named
    (its session id, its live pids) must not have changed."""
    if known["provider"] != now["provider"] or known["cwd"] != now["cwd"]:
        return False
    if known["session_id"] not in (None, "", "unknown") and known["session_id"] != now["session_id"]:
        return False
    return not known["pids"] or known["pids"] == now["pids"]


def find_session(agent: str, directory: Path, *, caller_pane: int | None = None) -> dict:
    """Exactly one live pane of that agent in that directory.

    A pane Kilix lists as that agent counts. So does a pane with no coding session,
    a generic state and a tmux client whose session holds that agent, in the
    foreground of its terminal, in that directory; it carries `_hosted`, the exact
    tmux pane and processes found. A listed pane carries `_identity`.
    """
    if caller_pane is None:
        caller_pane = caller_info()[0]
    snapshot = _listing(["panes", "list", "--json"])
    matches = []
    for pane in snapshot.get("panes", []):
        if not isinstance(pane, dict) or pane.get("pane_id") == caller_pane:
            continue
        coding = pane.get("coding_session")
        if not isinstance(coding, dict):
            if coding is None and pane.get("activity") in detect.GENERIC_HOSTED:
                for item in detect.tmux_hosted(pane, _tmux):
                    if (PROVIDER_AGENT.get(item["provider"]) == agent
                            and detect.same_directory(item["cwd"], directory)):
                        matches.append({**pane, "_hosted": item})
            continue
        provider = PROVIDER_AGENT.get(str(coding.get("provider") or ""))
        cwd = coding.get("cwd") or pane.get("cwd") or ""
        if provider == agent and cwd and Path(cwd).resolve() == directory:
            matches.append({**pane, "_identity": _session_identity(pane)})
    if len(matches) != 1:
        raise AgentsError(f"{'no' if not matches else len(matches)} live {agent} sessions in "
                          f"{directory}")
    return matches[0]


def _state(pane: dict, agent: str, hosted: dict | None) -> tuple[str, dict | None]:
    """The session's state, and for a tmux-hosted one the tmux pane as it is now.

    A state Kilix names (`idle`, `working`, `waiting`) is used as is; the screen
    supplements only a generic one (`GENERIC_DIRECT`: `agent`, `unknown`; for a
    tmux-hosted pane `GENERIC_HOSTED`, which adds `running`, what Kilix says of a
    pane whose foreground program is tmux). For a tmux-hosted session the screen
    is always read, from its own tmux pane, because the message goes into its
    composer: a named `idle`/`working` that the screen does not confirm holds, and
    a named `waiting` holds.
    """
    activity = pane.get("activity")
    if hosted is not None:
        now = [item for item in detect.tmux_hosted(pane, _tmux) if detect.same_hosted(item, hosted)]
        if len(now) != 1:
            raise AgentsError("the tmux-hosted session changed; the message is held")
        seen = detect.screen_state(hosted["provider"], detect.hosted_screen(now[0], _tmux))
        if activity in ("idle", "working"):
            return (activity if seen == activity else "agent"), now[0]
        # (`waiting`, `shell`, ... are not generic: they are returned as named, and they hold)
        return (seen or "agent" if activity in detect.GENERIC_HOSTED else activity), now[0]
    if activity in detect.GENERIC_DIRECT and agent in ("claude", "codex"):
        return detect.screen_state(agent, _screen(pane.get("pane_id"))) or activity, None
    return activity, None


def _deliver_hosted(pane: dict, hosted: dict, text: str) -> None:
    """Type `text` and Enter into exactly the identified tmux pane, through tmux itself.

    Nothing goes through the Kilix pane or the tmux client. The identity is read again
    immediately before; the pane's mode, pid, window and session are then checked by
    tmux in the same command that types, so a change in between types nothing.
    """
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise AgentsError("a message with line breaks or control characters cannot be typed into "
                          "a tmux-hosted session; the message is held")
    again = [item for item in detect.tmux_hosted(pane, _tmux) if detect.same_hosted(item, hosted)]
    if len(again) != 1:
        raise AgentsError("the tmux-hosted session changed; the message is held")
    if not detect.tmux_type(again[0], text, _tmux):
        raise AgentsError("tmux held the message: that pane is not in a state to take input; "
                          "nothing was typed")
    time.sleep(0.15)       # the text first, then Enter as its own write, as agent-control does
    if not detect.tmux_enter(again[0], _tmux):
        raise AgentsError("the text was typed into the tmux pane but tmux held Enter; "
                          "the line is pending there")


def _broker_id(pane: dict) -> str:
    broker = pane.get("broker")
    if isinstance(broker, dict):
        broker = broker.get("session_id")
    if not isinstance(broker, str) or not broker:
        raise AgentsError(f"pane {pane.get('pane_id')} has no broker identity")
    return broker


@dataclass
class Step:
    action: agents.Action
    summary: str
    argv: list = field(default_factory=list)


def launch_argv(action: agents.Action, directory: Path, pane: int, broker: str) -> list[str]:
    args = action.args
    place = args.get("place", "tab")
    title = f"{args['agent']} · {directory.name}"
    argv = ["agent-control", "new-tab" if place == "tab" else "split", str(pane),
            "--expect-broker", broker, "--agent", args["agent"], "--cwd", str(directory),
            "--title", title, "--trust-folder", "--coding-yolo"]
    if place != "tab":
        argv += ["--direction", place]
    for key in ("model", "resume"):
        if args.get(key):
            argv += [f"--{key}", args[key]]
    if args.get("prompt"):
        argv += [f"--prompt={args['prompt']}"]
    return argv


def summary(entry: dict) -> str:
    args = entry.get("args", {})
    if entry["kind"] == "agent":
        words = f"start {args.get('agent')} in {args.get('dir')}"
        if args.get("resume"):
            words = f"resume {args.get('agent')} session {args['resume']} in {args.get('dir')}"
        if args.get("place", "tab") != "tab":
            words += f", split {args['place']}"
        if args.get("prompt"):
            words += f", with the task \"{args['prompt']}\""
        return words
    if entry["kind"] == "wait":
        return f"wait until {args.get('session')} is {args.get('for')}"
    return (f"{'when it is idle, ' if args.get('wait') else ''}tell {args.get('session')}: "
            f"\"{args.get('text')}\"")


def perform(actions: list, *, cwd: str | None = None, dry_run: bool = False) -> list[dict]:
    """Run admitted actions in order; stop at the first that fails."""
    results = []
    launched: dict[str, dict] = {}
    last = None
    caller_cache = None
    # A pane may need a turn edge before a later wait can be trusted. New
    # prompted/resumed panes must first become working. A message queued while
    # already working must cross idle and then working, since panes/v1 exposes
    # no monotonic turn counter.
    transitions: dict[object, str] = {}
    # What each target pane is, for waits that Kilix cannot track (unknown state, tmux-hosted).
    tracked: dict[object, tuple[str, dict | None]] = {}

    def wait_state(pane_id, state: str, timeout, run_timeout) -> None:
        """`kilix panes wait`, or polling where Kilix cannot see the state."""
        agent, hosted = tracked.get(pane_id, ("", None))
        argv = ["panes", "wait", str(pane_id), "--for", state, "--json", "--timeout", str(timeout)]
        if hosted is None:
            if agent not in ("claude", "codex"):
                _run(argv, timeout=run_timeout)
                return
            snapshot = _listing(["panes", "list", "--json"])
            pane = next((p for p in snapshot.get("panes", [])
                         if isinstance(p, dict) and p.get("pane_id") == pane_id), None)
            if pane is None or pane.get("activity") != "agent":
                _run(argv, timeout=run_timeout)
                return
        deadline = time.monotonic() + float(timeout)
        while True:
            snapshot = _listing(["panes", "list", "--json"])
            pane = next((p for p in snapshot.get("panes", [])
                         if isinstance(p, dict) and p.get("pane_id") == pane_id), None)
            if pane is None:
                raise AgentsError(f"pane {pane_id} is gone")
            if _state(pane, agent, hosted)[0] == state:
                return
            if time.monotonic() >= deadline:
                raise AgentsError(f"timed out after {timeout}s waiting for pane {pane_id} to be {state}")
            time.sleep(POLL_SECONDS)

    def get_caller():
        nonlocal caller_cache
        if caller_cache is None:
            caller_cache = caller_info()
        return caller_cache

    def observe_transition(pane_id, timeout: float) -> None:
        transition = transitions.pop(pane_id, None)
        if transition == "idle-working":
            wait_state(pane_id, "idle", timeout, timeout + 30)
        if transition in ("working", "idle-working"):
            wait_state(pane_id, "working", 30, 60)

    for action in actions:
        entry = {"kind": action.kind, "args": dict(action.args)}
        results.append(entry)
        try:
            if action.kind == "agent":
                prompt = action.args.get("prompt")
                if prompt is not None and (not isinstance(prompt, str)
                                           or len(prompt.encode("utf-8")) > 1024):
                    raise AgentsError("a launch prompt is at most 1024 bytes")
                directory = resolve_dir(action.args["dir"], cwd=cwd)
                pane, broker, _caller_cwd = get_caller()
                argv = launch_argv(action, directory, pane, broker)
                if dry_run:
                    entry.update(outcome="would", argv=argv)
                    last = {"pane_id": f"<new pane {len(launched) + 1}>",
                            "agent": action.args["agent"], "broker": "<new pane broker>",
                            "dir": str(directory)}
                    launched[f"{action.args['agent']}@{action.args['dir']}"] = last
                    continue
                result = _listing_result(argv, timeout=60)
                pane_result = result.get("pane")
                if not isinstance(pane_result, dict) or pane_result.get("pane_id") is None:
                    raise AgentsError("kilix agent-control returned no launched pane")
                last = {"pane_id": pane_result["pane_id"], "agent": action.args["agent"],
                        "dir": str(directory)}
                tracked[last["pane_id"]] = (action.args["agent"], None)
                launched[f"{action.args['agent']}@{action.args['dir']}"] = last
                if action.args.get("resume") or prompt:
                    transitions[last["pane_id"]] = "working"
                entry.update(outcome="done", pane=last["pane_id"],
                             folder_trust=result.get("folder_trust"))
                continue
            target = action.args["session"]
            if target == "it":
                if last is None:
                    raise AgentsError("'it' names no session launched by this request")
                target_info = last
                pane_id = last["pane_id"]
                target_agent = last["agent"]
                target_dir = last.get("dir")
            else:
                agent, _, said = target.partition("@")
                caller_pane = get_caller()[0]
                found_dir = resolve_dir(said, cwd=cwd)
                target_info = find_session(agent, found_dir, caller_pane=caller_pane)
                target_dir = str(found_dir)
                pane_id = target_info["pane_id"]
                target_agent = agent
                tracked[pane_id] = (agent, target_info.get("_hosted"))
            if action.kind == "wait":
                wait_timeout = action.args.get("timeout") or 3600
                argv = ["panes", "wait", str(pane_id), "--for", action.args["for"], "--json",
                        "--timeout", str(wait_timeout)]
                if dry_run:
                    entry.update(outcome="would", argv=argv)
                    continue
                observe_transition(pane_id, wait_timeout)
                wait_state(pane_id, action.args["for"], wait_timeout, wait_timeout + 30)
                entry.update(outcome="done", pane=pane_id)
                continue
            # tell
            waited = bool(action.args.get("wait"))
            wait_timeout = action.args.get("timeout") or 3600
            if waited and not dry_run:
                observe_transition(pane_id, wait_timeout)
                wait_state(pane_id, "idle", wait_timeout, wait_timeout + 30)
            if dry_run and isinstance(pane_id, str) and pane_id.startswith("<new pane"):
                pane = target_info
                activity = "idle" if waited else "working"
            elif dry_run and waited:
                pane = target_info
                activity = "idle"
            else:
                snapshot = _listing(["panes", "list", "--json"])
                pane = next((p for p in snapshot.get("panes", [])
                             if isinstance(p, dict) and p.get("pane_id") == pane_id), None)
                if pane is None:
                    raise AgentsError(f"pane {pane_id} is gone")
                coding = pane.get("coding_session")
                hosted_target = target_info.get("_hosted") if isinstance(target_info, dict) else None
                if hosted_target is not None:
                    # Found through tmux, so Kilix lists no coding session. Re-read the same
                    # tmux pane, processes and screen; delivery goes to that pane only.
                    if coding is not None:
                        raise AgentsError("the target changed (Kilix now lists a coding session); "
                                          "the message is held")
                    activity, hosted_now = _state(pane, target_agent, hosted_target)
                else:
                    if not isinstance(coding, dict):
                        raise AgentsError("the target is not a live coding-agent pane")
                    observed_agent = PROVIDER_AGENT.get(str(coding.get("provider") or ""), "")
                    if not observed_agent and target != "it":
                        raise AgentsError("the target is not a live coding-agent pane")
                    if observed_agent and observed_agent != target_agent:
                        raise AgentsError("the target coding agent changed; the message is held")
                    # The same session in the same directory as when it was found.
                    fresh = _session_identity(pane)
                    if target_dir and fresh["cwd"] != str(Path(target_dir).resolve()):
                        raise AgentsError("the session's directory changed; the message is held")
                    known = target_info.get("_identity") if isinstance(target_info, dict) else None
                    if known and not _same_identity(known, fresh):
                        raise AgentsError("the session changed since it was found; the message is held")
                    activity, hosted_now = _state(pane, target_agent, None)
            if activity == "waiting":
                raise AgentsError("the session is waiting for an approval or a menu; "
                                  "the message is held")
            if activity not in ("idle", "working"):
                raise AgentsError("the session is not idle or working; the message is held")
            if target_agent not in _WORKING_STEER_AGENTS and activity != "idle":
                # Only readers which can distinguish approval/menu waits may
                # steer a working turn. OMP and Kimi expose no safe waiting
                # state, so they take messages only while idle.
                raise AgentsError(f"a {target_agent} session takes a message only when idle; "
                                  "ask to wait until it is done first")
            hosted_target = target_info.get("_hosted") if isinstance(target_info, dict) else None
            if hosted_target is not None:
                argv = ["tmux", *hosted_target["tmux_socket"], "if-shell", "-F", "-t", hosted_target["tmux_pane"],
                        "<guard>", "paste-buffer + Enter into exactly that pane"]
                if dry_run:
                    entry.update(outcome="would", argv=argv)
                    continue
                _deliver_hosted(pane, hosted_target, action.args["text"])
            else:
                broker = _broker_id(pane)
                argv = ["agent-control", "send", str(pane_id), "--expect-broker", broker,
                        "--text", action.args["text"], "--submit"]
                if dry_run:
                    entry.update(outcome="would", argv=argv)
                    continue
                _run(argv)
            if activity == "idle":
                try:
                    wait_state(pane_id, "working", 30, 60)
                except AgentsError as error:
                    raise AgentsError(f"delivery not confirmed: {error}") from error
                delivery = "delivered"
            else:
                transitions[pane_id] = "idle-working"
                delivery = "queued"
            entry.update(outcome="done", delivery=delivery, pane=pane_id)
        except (AgentsError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            reason = str(error)
            sent = [e for e in results[:-1] if e["kind"] == "tell" and e.get("outcome") == "done"]
            if sent:
                # A failed wait after a message doesn't undo it: say so, so
                # nobody sends it again (review R14 round 5, KN-R14-64).
                reason += "; the earlier message was sent" if len(sent) == 1 else \
                    f"; the {len(sent)} earlier messages were sent"
            entry.update(outcome="failed", reason=reason)
            break
    return results


def _listing_result(argv: list[str], *, timeout: float) -> dict:
    try:
        result = json.loads(_run(argv, timeout=timeout))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise AgentsError(f"kilix {argv[0]} returned invalid JSON: {error}") from error
    if not isinstance(result, dict):
        raise AgentsError(f"kilix {argv[0]} returned an invalid result")
    return result
