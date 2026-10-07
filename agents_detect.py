"""Session-state evidence the agents job adds to what Kilix's pane listing says.

Rule: a false hold is acceptable; a false `idle` or `working`, or bytes reaching
anything but the identified agent's empty composer, is not. Positive evidence
must be exact and structurally located; anything else holds.

Kilix's reader names a coding session from the foreground process of a pane and
a state from the agent's own records. Three real sessions fell outside it:
a Codex pane whose rollout is not held open (state `agent`), a Claude pane idle
at `❯` with a background monitor, and a Claude session inside tmux (the pane's
foreground process is `tmux`, so no coding session is listed).

**Screens.** State is decided only from the bottom UI region, located by its
layout, never from transcript lines:

- Claude: the composer lies between the last two full-width rules, the footer
  below the last one, nothing below the footer. `idle` needs one composer line
  that is exactly an empty prompt and a footer without `esc to interrupt`;
  `working` needs the same empty composer and `esc to interrupt` in the footer.
- Codex: the composer line `› Ask Codex to do anything` (an empty composer shows
  exactly that), a blank line above it, footer lines only below it. The status
  block directly above holds `• Working (… esc to interrupt)` while it works.
- A screen holding nothing but that region (a pane cut down to its UI) reads the
  same way.
- A numbered choice anywhere on the visible screen, approval wording (`Would you
  like to`, `Do you want to`, `esc to cancel`) in the last lines, matched across
  wrapped lines, the working marker outside its place, an unknown layout, a draft
  or a truncated region: no state, and the message is held. (Approval wording
  higher up is transcript, which may quote it; a real idle pane did.)

**tmux.** A tmux-hosted session is found by asking tmux which session the pane's
own tmux client is attached to, then reading the process trees of that session's
panes. The agent must own the foreground process group of the pane's terminal
(`tpgid` from `/proc`), and its identity is kept as provider, pids, start times,
full argv, working directory, socket, session `$N`, window `@N`, pane `%N` and
client pid. A message is delivered by tmux itself to that exact pane `%N`, with
the checks and the send in one tmux command so the server applies them
atomically; nothing goes through the Kilix pane or the tmux client, which can be
in a command prompt, a prefix table or showing another pane.
"""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

PROC_ROOT = "/proc"
MAX_PROCESSES = 20000
MAX_DEPTH = 8
AGENT_NAMES = {"codex": "codex", "claude": "claude", "grok": "grok", "omp": "omp",
               "kimi": "kimi", "kimi-code": "kimi"}
SENT, HELD = "NEEDLE-SENT", "NEEDLE-HELD"
# What Kilix may call a session whose state it cannot read: a named state is never supplemented.
GENERIC_DIRECT = frozenset({"agent", "unknown"})
GENERIC_HOSTED = frozenset({"agent", "unknown", "running"})

# --------------------------------------------------------------------------- screens

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_RULE = re.compile(r"^[─━═]{10,}$")
_FOOTER = re.compile(r"shortcuts|for agents|permissions|shift\+tab|plan mode|accept edits|auto-accept|"
                     r"monitor|context|esc to|ctrl\+|·|%|bypass", re.I)
_APPROVAL = re.compile(r"would you like to|do you want to|press enter to confirm|esc to cancel|"
                       r"enter to select|\(y/n\)|\[y/n\]", re.I)
_CHOICE = re.compile(r"^\s*(?:[│|]\s*)?(?:[❯›>▶➤]\s*)?\d{1,2}[.)]\s+\S")
_INTERRUPT = re.compile(r"esc to interrupt", re.I)
_CODEX_WORKING = re.compile(r"•\s*working\b", re.I)
_SPINNER = re.compile(r"^\s*\S\s+\S+…\s*\(\d")
_EMPTY_CODEX = "› Ask Codex to do anything"
RULE_DISTANCE = 10          # most lines a composer may span between its two rules
FOOTER_LINES = 3
STATUS_BLOCK = 12
WAITING_TAIL = 15


def _lines(text: str) -> list[str]:
    cleaned = _ANSI.sub("", str(text)).replace(" ", " ")
    lines = [line.rstrip() for line in cleaned.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _flat(lines: list[str]) -> str:
    return " ".join(" ".join(line.split()) for line in lines)


def _footer_ok(footer: list[str]) -> bool:
    return len(footer) <= FOOTER_LINES and all(_FOOTER.search(line) for line in footer)


def screen_state(provider: str, text: str) -> str | None:
    """`idle`, `working` or `waiting` only when the bottom UI region says so exactly; else None."""
    lines = _lines(text)
    if not lines or provider not in ("claude", "codex"):
        return None
    flat = _flat(lines)
    nonempty = [line for line in lines if line.strip()]
    tail = _flat(nonempty[-WAITING_TAIL:])
    # A numbered choice anywhere on the screen is a menu. Approval wording counts where a dialog
    # sits (the last lines, joined across wrapping); higher up it is the transcript, which may quote
    # it (a real idle pane did), and the layout checks below decide whether a composer is there.
    if _APPROVAL.search(tail) or any(_CHOICE.match(line) for line in lines):
        return "waiting" if _APPROVAL.search(tail) else None
    return _claude(lines, flat) if provider == "claude" else _codex(lines, flat)


def _claude(lines: list[str], flat: str) -> str | None:
    rules = [index for index, line in enumerate(lines) if _RULE.match(line.strip())]
    if len(rules) >= 2:
        top, bottom = rules[-2], rules[-1]
        if len(lines[top].strip()) != len(lines[bottom].strip()) or not 1 <= bottom - top - 1 <= RULE_DISTANCE:
            return None
        composer = [line for line in lines[top + 1:bottom] if line.strip()]
        footer = [line for line in lines[bottom + 1:] if line.strip()]
        above = [line for line in lines[:top] if line.strip()]
    else:
        # The whole screen is the region (a pane cut down to its UI): composer, then footer.
        everything = [line for line in lines if line.strip()]
        composer, footer, above = everything[:1], everything[1:], []
    if len(composer) != 1 or not _footer_ok(footer):
        return None
    prompt = composer[0].strip()
    if not prompt.startswith("❯") or prompt[1:].strip():
        return None                                   # a draft, or not the prompt at all
    if _INTERRUPT.search(_flat(footer)):
        return "working"
    if _INTERRUPT.search(flat) or any(_SPINNER.match(line) for line in above[-6:]):
        return None                                   # a marker where it does not belong, or a spinner
    return "idle"


def _codex(lines: list[str], flat: str) -> str | None:
    composer = max((index for index, line in enumerate(lines) if line.lstrip().startswith("›")), default=None)
    if composer is None:
        return None
    footer = [line for line in lines[composer + 1:] if line.strip()]
    if lines[composer].strip() != _EMPTY_CODEX or not _footer_ok(footer):
        return None                                   # a draft, a continuation line, or an unknown footer
    if composer > 0 and lines[composer - 1].strip():
        return None                                   # something sits on the composer: a modal, not the UI
    block = []
    for line in reversed(lines[:composer]):
        if not line.strip():
            if block:
                break
            continue
        block.append(line)
        if len(block) > STATUS_BLOCK:
            break
    status = _flat(list(reversed(block)))
    marked = bool(_INTERRUPT.search(flat) or _CODEX_WORKING.search(flat))
    if marked:
        return "working" if (_INTERRUPT.search(status) or _CODEX_WORKING.search(status)) \
            and len(block) <= STATUS_BLOCK else None
    return "idle"


# --------------------------------------------------------------------------- processes

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


@dataclass(frozen=True)
class Stat:
    parent: int
    pgrp: int
    tpgid: int
    start_ticks: int


def proc_stat(pid: int, proc_root: str = PROC_ROOT) -> Stat | None:
    """parent, process group, the terminal's foreground group and the start time of a process."""
    fields = _proc_text(proc_root, pid, "stat").rsplit(")", 1)
    if len(fields) != 2:
        return None
    rest = fields[1].split()
    try:
        return Stat(parent=int(rest[1]), pgrp=int(rest[2]), tpgid=int(rest[5]), start_ticks=int(rest[19]))
    except (IndexError, ValueError):
        return None


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
        stat = proc_stat(int(name), proc_root)
        if stat is not None:
            found.setdefault(stat.parent, []).append(int(name))
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


# --------------------------------------------------------------------------- tmux

_SESSION = re.compile(r"\$[0-9]+")
_WINDOW = re.compile(r"@[0-9]+")
_PANE = re.compile(r"%[0-9]+")


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
    """The coding agents in the foreground of the tmux session this pane's tmux client shows.

    `tmux(args) -> str` runs tmux with those arguments. The session is the one tmux
    itself reports for the client process in the pane's foreground (`$N`, never a
    name). One entry per (tmux pane, provider), only for agent processes that own
    the foreground process group of the pane's terminal and agree on one working
    directory. Every identity field is validated; an incomplete record is dropped,
    which holds the message.
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
        if not _SESSION.fullmatch(session):
            continue
        try:
            panes = _rows(tmux([*socket, "list-panes", "-s", "-t", session, "-F",
                                "#{pane_id}\t#{pane_pid}\t#{pane_active}\t#{window_active}\t#{pane_in_mode}"
                                "\t#{window_id}\t#{pane_dead}"]), 7)
        except Exception:
            continue
        for pane_id, pane_pid, active, window_active, in_mode, window_id, dead in panes:
            if (not _PANE.fullmatch(pane_id) or not _WINDOW.fullmatch(window_id) or not pane_pid.isdigit()
                    or dead != "0" or active not in ("0", "1") or window_active not in ("0", "1")):
                continue
            root = proc_stat(int(pane_pid), proc_root)
            if root is None or root.tpgid <= 0:
                continue
            by_provider: dict[str, list[tuple]] = {}
            for pid in process_tree(int(pane_pid), proc_root):
                stat = proc_stat(pid, proc_root)
                argv = process_argv(pid, proc_root)
                provider = process_agent(argv)
                # Only the terminal's foreground group can read what is typed there.
                if provider and stat is not None and stat.pgrp == root.tpgid and stat.tpgid == root.tpgid:
                    by_provider.setdefault(provider, []).append(
                        (pid, stat.start_ticks, tuple(argv), process_cwd(pid, proc_root)))
            for provider, members in by_provider.items():
                cwds = {member[3] for member in members}
                if len(cwds) != 1 or not next(iter(cwds)):
                    continue
                hosted.append({
                    "provider": provider, "pid": members[0][0], "members": tuple(members),
                    "cwd": next(iter(cwds)), "pgrp": root.tpgid,
                    "tmux_socket": socket, "session_id": session, "window_id": window_id,
                    "tmux_pane": pane_id, "pane_pid": int(pane_pid), "client_pid": client_pid,
                    "active": active == "1" and window_active == "1", "in_mode": in_mode != "0"})
    return hosted


_IDENTITY = ("provider", "members", "cwd", "pgrp", "tmux_socket", "session_id", "window_id", "tmux_pane",
             "pane_pid", "client_pid")


def same_hosted(first: dict, second: dict) -> bool:
    return all(first.get(key) == second.get(key) for key in _IDENTITY)


def hosted_screen(hosted: dict, tmux) -> str:
    """The text of exactly that tmux pane."""
    try:
        return tmux([*hosted["tmux_socket"], "capture-pane", "-p", "-t", hosted["tmux_pane"]])
    except Exception:
        return ""


def guard(hosted: dict) -> str:
    """The tmux format that is true only while the pane is the identified one and takes input."""
    return ("#{&&:#{&&:#{==:#{pane_id},%s},#{==:#{pane_pid},%d}},"
            "#{&&:#{&&:#{==:#{pane_in_mode},0},#{==:#{pane_dead},0}},"
            "#{&&:#{==:#{pane_input_off},0},#{&&:#{==:#{window_id},%s},#{==:#{session_id},%s}}}}}"
            % (hosted["tmux_pane"], hosted["pane_pid"], hosted["window_id"], hosted["session_id"]))


def _conditional(hosted: dict, tmux, then: str, otherwise: str | None = None) -> str:
    """One tmux command: run `then` against the exact pane only while the guard holds."""
    held = f"{otherwise} ; display-message -p {HELD}" if otherwise else f"display-message -p {HELD}"
    return tmux([*hosted["tmux_socket"], "if-shell", "-F", "-t", hosted["tmux_pane"], guard(hosted),
                 f"{then} ; display-message -p {SENT}", held])


def tmux_type(hosted: dict, text: str, tmux) -> bool:
    """Insert `text` into exactly that pane, atomically with the guard. True if it was sent.

    The text travels as an argument of `set-buffer` (no quoting, no tmux parsing) and is
    pasted by the same tmux command that checks the pane, then removed.
    """
    pane = hosted["tmux_pane"]
    buffer = "needle-" + secrets.token_hex(8)
    tmux([*hosted["tmux_socket"], "set-buffer", "-b", buffer, "--", text])
    try:
        answer = _conditional(hosted, tmux, f"paste-buffer -d -b {buffer} -t {pane}",
                              f"delete-buffer -b {buffer}")
    finally:
        try:
            tmux([*hosted["tmux_socket"], "delete-buffer", "-b", buffer])
        except Exception:
            pass                                      # already pasted or deleted
    return answer.strip() == SENT


def tmux_enter(hosted: dict, tmux) -> bool:
    """Press Enter in exactly that pane under the same guard. True if it was sent."""
    answer = _conditional(hosted, tmux, f"send-keys -t {hosted['tmux_pane']} Enter")
    return answer.strip() == SENT


def same_directory(cwd: str, directory: Path) -> bool:
    try:
        return bool(cwd) and Path(cwd).resolve() == directory
    except OSError:
        return False
