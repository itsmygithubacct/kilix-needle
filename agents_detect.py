"""Session-state evidence the agents job adds to what Kilix's pane listing says.

Kilix's reader names a coding session from the foreground process of a pane and
a state from that agent's own records. Three real sessions fell outside it:

- a Codex pane whose rollout is not held open: the state is `agent`, not
  `idle`/`working`/`waiting`, though the screen says `• Working (...)`, shows an
  empty composer `› Ask Codex to do anything`, or asks `Would you like to run the
  following command?`;
- a Claude pane idle at `❯` with a background monitor ("1 monitor") whose record
  says something this reader does not map;
- a Claude session that runs inside tmux, where the pane's foreground process is
  `tmux`, so no coding session is listed at all.

Screen text is read only as evidence about a pane Kilix already names as that
agent (or tmux names for one exact tmux pane); it never names a session by
itself. A tmux-hosted session is found by asking tmux which session the pane's
own tmux *client process* is attached to, then reading the process tree of that
session's panes: exact IDs only, the agent's own working directory, one match
per directory. Nothing here sends input.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

PROC_ROOT = "/proc"
SCREEN_TAIL = 14
MAX_PROCESSES = 20000
MAX_DEPTH = 8
AGENT_NAMES = {"codex": "codex", "claude": "claude", "grok": "grok", "omp": "omp",
               "kimi": "kimi", "kimi-code": "kimi"}

_CODEX_WORKING = re.compile(r"^\s*(?:[•◦●○·]\s*)?Working\s*\(")
_CODEX_IDLE = re.compile(r"^\s*›\s*Ask Codex to do anything\s*$")
_CODEX_WAITING = re.compile(r"^\s*Would you like to .+\?\s*$")
_CLAUDE_WORKING = re.compile(r"esc to interrupt", re.I)
_CLAUDE_IDLE = re.compile(r"^\s*(?:[│|]\s*)?[❯>]\s*(?:[│|]\s*)?$")
_CLAUDE_MENU_ITEM = re.compile(r"^\s*(?:[│|]\s*)?(?:[❯>]\s*)?(\d)\.\s+\S")
_CLAUDE_QUESTION = re.compile(r"^\s*(?:[│|]\s*)?Do you want to .+\?")


def _tail(text: str) -> list[str]:
    return [line.rstrip() for line in str(text).splitlines() if line.strip()][-SCREEN_TAIL:]


def screen_state(provider: str, text: str) -> str | None:
    """`idle`, `working` or `waiting` if the screen's last lines say so plainly, else None.

    Anything unclear is None: a half-typed composer, an unknown modal, an empty
    screen. The caller then keeps Kilix's own answer and holds the message.
    """
    lines = _tail(text)
    if provider == "codex":
        if any(_CODEX_WAITING.match(line) for line in lines):
            return "waiting"
        if any(_CODEX_WORKING.match(line) for line in lines):
            return "working"
        if any(_CODEX_IDLE.match(line) for line in lines):
            return "idle"
        return None
    if provider == "claude":
        numbers = {m.group(1) for line in lines if (m := _CLAUDE_MENU_ITEM.match(line))}
        if any(_CLAUDE_QUESTION.match(line) for line in lines) or {"1", "2"} <= numbers:
            return "waiting"
        if any(_CLAUDE_WORKING.search(line) for line in lines):
            return "working"
        if any(_CLAUDE_IDLE.match(line) for line in lines):
            return "idle"
        return None
    return None


def process_agent(argv) -> str:
    """The same rule Kilix uses: the first two arguments' base names."""
    names = {(os.path.basename(str(value)) or str(value)).casefold() for value in list(argv)[:2]}
    for name, provider in AGENT_NAMES.items():
        if name in names:
            return provider
    return ""


def _proc_text(proc_root: str, pid: int, name: str) -> str:
    try:
        with open(f"{proc_root}/{pid}/{name}", "rb") as handle:
            return handle.read(65536).decode("utf-8", "replace")
    except OSError:
        return ""


def _children(proc_root: str) -> dict[int, list[int]]:
    found: dict[int, list[int]] = {}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return found
    for count, name in enumerate(names):
        if count >= MAX_PROCESSES:
            break
        if not name.isdigit():
            continue
        stat = _proc_text(proc_root, int(name), "stat")
        try:
            parent = int(stat.rsplit(")", 1)[1].split()[1])
        except (IndexError, ValueError):
            continue
        found.setdefault(parent, []).append(int(name))
    return found


def process_tree(root: int, proc_root: str = PROC_ROOT) -> list[int]:
    """`root` and its descendants, breadth first, bounded."""
    children = _children(proc_root)
    order, level = [root], [root]
    for _ in range(MAX_DEPTH):
        level = [child for pid in level for child in sorted(children.get(pid, ()))]
        if not level:
            break
        order.extend(level)
    return order


def process_argv(pid: int, proc_root: str = PROC_ROOT) -> list[str]:
    return [part for part in _proc_text(proc_root, pid, "cmdline").split("\0") if part]


def process_cwd(pid: int, proc_root: str = PROC_ROOT) -> str:
    try:
        return os.readlink(f"{proc_root}/{pid}/cwd")
    except OSError:
        return ""


def tmux_socket_args(argv) -> list[str]:
    """`-S PATH` or `-L NAME` from a tmux client's own command line; else the default socket."""
    argv = list(argv)
    for index, value in enumerate(argv[1:], 1):
        if value in ("-S", "-L") and index + 1 < len(argv):
            return [value, argv[index + 1]]
        if value[:2] in ("-S", "-L") and len(value) > 2:
            return [value[:2], value[2:]]
        if not value.startswith("-"):
            break
    return []


def tmux_clients(pane: dict) -> list[tuple[int, list[str]]]:
    """(pid, socket arguments) of each tmux client in a pane's foreground."""
    process = pane.get("process") if isinstance(pane.get("process"), dict) else {}
    found = []
    for item in process.get("foreground") or []:
        if not isinstance(item, dict) or type(item.get("pid")) is not int:
            continue
        argv = item.get("argv") if isinstance(item.get("argv"), list) else []
        if argv and os.path.basename(str(argv[0])) == "tmux":
            found.append((item["pid"], tmux_socket_args(argv)))
    return found


def _rows(text: str, width: int) -> list[list[str]]:
    return [row for row in (line.split("\t") for line in text.splitlines()) if len(row) == width]


def tmux_hosted(pane: dict, tmux, *, proc_root: str | None = None) -> list[dict]:
    """The coding agents running in the tmux session this pane's tmux client shows.

    `tmux(args) -> str` runs tmux with those arguments. The session is the one
    tmux itself reports for the client process that is in the pane's foreground,
    not one matched by a name; a client tmux does not list names no session.
    One entry per (tmux pane, provider), and only when the agent processes of that
    tmux pane agree on one working directory.
    """
    proc_root = proc_root or PROC_ROOT
    hosted = []
    for client_pid, socket in tmux_clients(pane):
        try:
            clients = _rows(tmux([*socket, "list-clients", "-F", "#{client_pid}\t#{session_id}"]), 2)
        except Exception:
            continue
        sessions = {row[1] for row in clients if row[0] == str(client_pid)}
        if len(sessions) != 1:
            continue
        session = sessions.pop()
        try:
            panes = _rows(tmux([*socket, "list-panes", "-s", "-t", session, "-F",
                                "#{pane_id}\t#{pane_pid}\t#{pane_active}\t#{window_active}\t#{pane_in_mode}"]), 5)
        except Exception:
            continue
        for pane_id, pane_pid, active, window_active, in_mode in panes:
            if not pane_pid.isdigit() or not re.fullmatch(r"%[0-9]+", pane_id):
                continue
            by_provider: dict[str, list[tuple[int, str]]] = {}
            for pid in process_tree(int(pane_pid), proc_root):
                provider = process_agent(process_argv(pid, proc_root))
                if provider:
                    by_provider.setdefault(provider, []).append((pid, process_cwd(pid, proc_root)))
            for provider, members in by_provider.items():
                cwds = {cwd for _pid, cwd in members}
                if len(cwds) != 1 or not next(iter(cwds)):
                    continue
                hosted.append({
                    "provider": provider, "pid": members[0][0], "cwd": next(iter(cwds)),
                    "tmux_socket": socket, "session_id": session, "tmux_pane": pane_id,
                    "client_pid": client_pid, "active": active == "1" and window_active == "1",
                    "in_mode": in_mode != "0"})
    return hosted


def same_hosted(first: dict, second: dict) -> bool:
    keys = ("provider", "pid", "cwd", "tmux_socket", "session_id", "tmux_pane", "client_pid")
    return all(first.get(key) == second.get(key) for key in keys)


def hosted_screen(hosted: dict, tmux) -> str:
    """The text of exactly that tmux pane."""
    try:
        return tmux([*hosted["tmux_socket"], "capture-pane", "-p", "-t", hosted["tmux_pane"]])
    except Exception:
        return ""


def same_directory(cwd: str, directory: Path) -> bool:
    try:
        return bool(cwd) and Path(cwd).resolve() == directory
    except OSError:
        return False
