"""Carry out admitted agents-job actions through Kilix's own tools.

- A launch is `kilix agent-control new-tab|split`, anchored to the caller's
  own pane (its broker identity checked), with the directory resolved here.
  It records the client's trust for exactly that directory
  (`--trust-folder`), and adds an approval skip only when Kilix's
  coding-yolo setting is on (`--coding-yolo`; the request never decides).
- A wait is `kilix panes wait PANE --for idle|waiting`.
- A message is `kilix agent-control send PANE --expect-broker B --submit`,
  after an optional wait for idle. It is held only while the session is
  `waiting` on an approval or a menu, where a submitted line could answer
  it (the builder's rule, which the owner can overrule; steering a working
  session is allowed).

Directories resolve to exactly one existing directory: an explicit path, the
caller's directory ("here"), or a repository name found under the usual
source roots. Sessions resolve to exactly one live agent pane in that
directory. Ambiguity refuses; nothing is created.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess

import agents
import kilix

HERE = ("here", "this repo", "this directory", "the current folder", "this folder",
        "the current directory", "this project")
ROOTS = ("gpu_terminal", "research")
PROVIDER_AGENT = {"claude": "claude", "codex": "codex", "grok": "grok", "omp": "qwen-omp",
                  "kimi": "kimi"}


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


def resolve_dir(said: str, *, cwd: str | None = None, home: Path | None = None) -> Path:
    """Exactly one existing directory for the words the request used."""
    home = home or Path.home()
    folded = agents._fold(said)
    if folded in HERE:
        if not cwd:
            raise AgentsError("the calling pane's directory is unknown; 'here' cannot be resolved")
        path = Path(cwd).resolve()
        if not path.is_dir():
            raise AgentsError(f"the calling pane's directory is not available: {cwd}")
        return path
    if said.strip().startswith(("~/", "/")):
        path = Path(os.path.expanduser(said.strip())).resolve()
        if not path.is_dir():
            raise AgentsError(f"{said} is not an existing directory")
        return path

    name = said.strip()
    if name.casefold().startswith("the "):
        name = name[4:]
    for suffix in (" repository", " repo"):
        if name.casefold().endswith(suffix):
            name = name[:-len(suffix)]
            break

    map_file = home / ".config" / "kilix-needle" / "dirs.json"
    if map_file.exists():
        try:
            mapped = json.loads(map_file.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise AgentsError(f"cannot read directory map {map_file}: {error}") from error
        if not isinstance(mapped, dict):
            raise AgentsError(f"directory map {map_file} must be a JSON object")
        if name in mapped:
            value = mapped[name]
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise AgentsError(f"directory map entry {name!r} must be an absolute path")
            path = Path(value).resolve()
            if not path.is_dir():
                raise AgentsError(f"directory map entry {name!r} is not an existing directory")
            return path

    found = set()
    for root in ROOTS:
        base = home / root
        if not base.is_dir() or base.is_symlink():
            continue
        for current, directories, _files in os.walk(base, followlinks=False):
            directories[:] = [item for item in directories
                              if not item.startswith(".") and not (Path(current) / item).is_symlink()]
            candidate = Path(current)
            if candidate.name == name and (candidate / ".git").exists():
                found.add(candidate.resolve())
            directories[:] = [item for item in directories if item != ".git"]
    if len(found) != 1:
        raise AgentsError(f"{said}: {'no' if not found else len(found)} matching directories"
                          f"{'' if not found else ' (' + ', '.join(map(str, sorted(found))) + ')'}")
    return found.pop()


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


def find_session(agent: str, directory: Path, *, caller_pane: int | None = None) -> dict:
    """Exactly one live pane of that agent in that directory."""
    if caller_pane is None:
        caller_pane = caller_info()[0]
    snapshot = _listing(["panes", "list", "--json"])
    matches = []
    for pane in snapshot.get("panes", []):
        if not isinstance(pane, dict) or pane.get("pane_id") == caller_pane:
            continue
        coding = pane.get("coding_session") or {}
        provider = PROVIDER_AGENT.get(str(coding.get("provider") or ""))
        cwd = coding.get("cwd") or pane.get("cwd") or ""
        if provider == agent and cwd and Path(cwd).resolve() == directory:
            matches.append(pane)
    if len(matches) != 1:
        raise AgentsError(f"{'no' if not matches else len(matches)} live {agent} sessions in "
                          f"{directory}")
    return matches[0]


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

    def get_caller():
        nonlocal caller_cache
        if caller_cache is None:
            caller_cache = caller_info()
        return caller_cache

    for action in actions:
        entry = {"kind": action.kind, "args": dict(action.args)}
        results.append(entry)
        try:
            if action.kind == "agent":
                directory = resolve_dir(action.args["dir"], cwd=cwd)
                pane, broker, _caller_cwd = get_caller()
                argv = launch_argv(action, directory, pane, broker)
                if dry_run:
                    entry.update(outcome="would", argv=argv)
                    last = {"pane_id": f"<new pane {len(launched) + 1}>",
                            "agent": action.args["agent"], "broker": "<new pane broker>"}
                    launched[f"{action.args['agent']}@{action.args['dir']}"] = last
                    continue
                result = _listing_result(argv, timeout=60)
                pane_result = result.get("pane")
                if not isinstance(pane_result, dict) or pane_result.get("pane_id") is None:
                    raise AgentsError("kilix agent-control returned no launched pane")
                last = {"pane_id": pane_result["pane_id"], "agent": action.args["agent"]}
                launched[f"{action.args['agent']}@{action.args['dir']}"] = last
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
            else:
                agent, _, said = target.partition("@")
                caller_pane = get_caller()[0]
                target_info = find_session(agent, resolve_dir(said, cwd=cwd),
                                           caller_pane=caller_pane)
                pane_id = target_info["pane_id"]
                target_agent = agent
            if action.kind == "wait":
                wait_timeout = action.args.get("timeout") or 3600
                argv = ["panes", "wait", str(pane_id), "--for", action.args["for"], "--json",
                        "--timeout", str(wait_timeout)]
                if dry_run:
                    entry.update(outcome="would", argv=argv)
                    continue
                _run(argv, timeout=wait_timeout + 30)
                entry.update(outcome="done", pane=pane_id)
                continue
            # tell
            waited = bool(action.args.get("wait"))
            wait_timeout = action.args.get("timeout") or 3600
            if waited and not dry_run:
                _run(["panes", "wait", str(pane_id), "--for", "idle", "--json", "--timeout",
                      str(wait_timeout)], timeout=wait_timeout + 30)
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
                activity = pane.get("activity")
                coding = pane.get("coding_session") or {}
                observed_agent = PROVIDER_AGENT.get(str(coding.get("provider") or ""), "")
                if not observed_agent and target != "it":
                    raise AgentsError("the target is not a live coding-agent pane")
                if observed_agent and observed_agent != target_agent:
                    raise AgentsError("the target coding agent changed; the message is held")
            if activity not in ("idle", "working"):
                raise AgentsError("the session is not idle or working; the message is held")
            if target_agent == "qwen-omp" and activity != "idle":
                # omp's files can't show an approval prompt (review R13, KX-R13-16):
                # only an idle omp session takes a message.
                raise AgentsError("a qwen-omp session takes a message only when idle; ask to "
                                  "wait until it is done first")
            broker = _broker_id(pane)
            argv = ["agent-control", "send", str(pane_id), "--expect-broker", broker,
                    "--text", action.args["text"], "--submit"]
            if dry_run:
                entry.update(outcome="would", argv=argv)
                continue
            _run(argv)
            entry.update(outcome="done", delivery="not confirmed", pane=pane_id)
        except (AgentsError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            entry.update(outcome="failed", reason=str(error))
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
