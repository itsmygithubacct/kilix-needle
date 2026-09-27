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
ROOTS = ("gpu_terminal", "research", "src", "projects", "code", "repos", "work", "scratch-workers")
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
        return Path(cwd or os.getcwd()).resolve()
    if folded.startswith("~/") or folded.startswith("/"):
        path = Path(os.path.expanduser(said.strip())).resolve()
        if not path.is_dir():
            raise AgentsError(f"{said} is not an existing directory")
        return path
    name = folded.removeprefix("the ").removesuffix(" repo").removesuffix(" repository")
    name = name.replace(" ", "-")
    found = set()
    for root in ROOTS:
        base = home / root
        if not base.is_dir():
            continue
        if base.name == name:
            found.add(base.resolve())
        for depth in ("*", "*/*", "*/*/*"):
            for candidate in base.glob(depth):
                if candidate.name == name and candidate.is_dir() and not candidate.is_symlink():
                    found.add(candidate.resolve())
    if len(found) != 1:
        raise AgentsError(f"{said}: {'no' if not found else len(found)} matching directories"
                          f"{'' if not found else ' (' + ', '.join(map(str, sorted(found))) + ')'}")
    return found.pop()


def caller() -> tuple[int, str]:
    """This process's own pane and its broker identity."""
    listing = json.loads(_run(["agent-control", "list"]))
    pane = listing.get("caller_pane")
    for item in listing.get("panes", []):
        if item.get("pane_id") == pane and item.get("broker"):
            return pane, item["broker"]
    raise AgentsError("the caller's own pane is unknown; run from a Kilix pane")


def find_session(agent: str, directory: Path) -> dict:
    """Exactly one live pane of that agent in that directory."""
    snapshot = json.loads(_run(["panes", "list", "--json"]))
    matches = []
    for pane in snapshot.get("panes", []):
        coding = pane.get("coding_session") or {}
        provider = PROVIDER_AGENT.get(str(coding.get("provider") or ""))
        cwd = coding.get("cwd") or pane.get("cwd") or ""
        if provider == agent and cwd and Path(cwd).resolve() == directory:
            matches.append(pane)
    if len(matches) != 1:
        raise AgentsError(f"{'no' if not matches else len(matches)} live {agent} sessions in "
                          f"{directory}")
    return matches[0]


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
    launched: dict[str, int] = {}
    last = None
    for action in actions:
        entry = {"kind": action.kind, "args": dict(action.args)}
        results.append(entry)
        try:
            if action.kind == "agent":
                directory = resolve_dir(action.args["dir"], cwd=cwd)
                pane, broker = caller()
                argv = launch_argv(action, directory, pane, broker)
                if dry_run:
                    entry.update(outcome="would", argv=argv)
                    last = None
                    continue
                result = json.loads(_run(argv, timeout=60))
                last = result["pane"]["pane_id"]
                launched[f"{action.args['agent']}@{action.args['dir']}"] = last
                entry.update(outcome="done", pane=last,
                             folder_trust=result.get("folder_trust"))
                continue
            target = action.args["session"]
            if target == "it":
                if last is None:
                    raise AgentsError("'it' names no session launched by this request")
                pane_id = last
            else:
                agent, _, said = target.partition("@")
                pane_id = find_session(agent, resolve_dir(said, cwd=cwd))["pane_id"]
            if action.kind == "wait":
                argv = ["panes", "wait", str(pane_id), "--for", action.args["for"], "--json"]
                if action.args.get("timeout"):
                    argv += ["--timeout", str(action.args["timeout"])]
                if dry_run:
                    entry.update(outcome="would", argv=argv)
                    continue
                _run(argv, timeout=(action.args.get("timeout") or 3600) + 30)
                entry.update(outcome="done", pane=pane_id)
                continue
            # tell
            if action.args.get("wait") and not dry_run:
                _run(["panes", "wait", str(pane_id), "--for", "idle", "--json"], timeout=3630)
            snapshot = json.loads(_run(["panes", "list", "--json"]))
            pane = next((p for p in snapshot.get("panes", []) if p.get("pane_id") == pane_id),
                        None)
            if pane is None:
                raise AgentsError(f"pane {pane_id} is gone")
            if pane.get("activity") == "waiting":
                raise AgentsError("the session is waiting on an approval or a menu; the "
                                  "message is held so it can't answer that")
            broker = pane.get("broker") or ""
            argv = ["agent-control", "send", str(pane_id), "--expect-broker", broker,
                    "--text", action.args["text"], "--submit"]
            if dry_run:
                entry.update(outcome="would", argv=argv)
                continue
            _run(argv)
            entry.update(outcome="done", delivery="not confirmed", pane=pane_id)
        except (AgentsError, KeyError, ValueError) as error:
            entry.update(outcome="failed", reason=str(error))
            break
    return results
