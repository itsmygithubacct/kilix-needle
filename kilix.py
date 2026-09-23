"""Resolve admitted actions against the live Kilix tree and run them.

Everything goes through `kilix @` over the instance socket, as argv, never a
shell. A reference is resolved against one `kilix @ ls` snapshot to exactly
one pane or tab by id; anything ambiguous or missing is refused with the
candidates named, not guessed.

Typing into a pane follows the path Kilix agent panes need: a pane behind the
PTY broker is matched by its `KITTY_PTY_BROKER_SESSION` (an `id:` match does
not reach its input), the text goes through `--stdin` so no escape is
interpreted, and Enter is a separate send. It is refused unless shell
integration reports the pane at a prompt, so text never lands in an editor
or a running program.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shlex
import subprocess

from actions import Action

KILIX = os.environ.get("KILIX_NEEDLE_KILIX", "kilix")
MAX_TYPED = 1024
_BROKER = re.compile(r"[0-9a-f]{16,64}\Z")
_LOCATION = {"right": "vsplit", "left": "vsplit-before",
             "below": "hsplit", "above": "hsplit-before"}
_NEIGHBOR = {"left": "left", "right": "right", "above": "top", "below": "bottom"}
_RESIZE = {"wider": ("horizontal", 1), "narrower": ("horizontal", -1),
           "taller": ("vertical", 1), "shorter": ("vertical", -1)}


class KilixError(RuntimeError):
    """Kilix could not be read, or an action could not be resolved or run."""


@dataclass(frozen=True)
class Step:
    """One resolved action: what it will do, in words, and the argv that does it."""
    action: Action
    summary: str
    commands: tuple[tuple[tuple[str, ...], bytes | None], ...]
    closes: frozenset = frozenset()   # ids of every pane this step would close


def _ancestors() -> list[int]:
    """This process and its parents, nearest first, from /proc."""
    chain, pid = [], os.getpid()
    while pid > 1 and len(chain) < 64:
        chain.append(pid)
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as stat:
                # the command name is parenthesised and may contain spaces
                pid = int(stat.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return chain


def _comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", encoding="ascii", errors="replace") as comm:
            return comm.read().strip()
    except OSError:
        return ""


def _target() -> list[str]:
    """How to reach this Kilix instance.

    A pane has KITTY_LISTEN_ON. An agent harness may start kilix-needle as a
    tool with a stripped environment (Codex passes only listed variables), so
    the instance is then found as the nearest `kitty` ancestor, whose socket
    Kilix names `unix:@kilix-<pid>`. Anything else is refused, never guessed.
    """
    if os.environ.get("KITTY_LISTEN_ON"):
        return []
    for pid in _ancestors()[1:]:
        if _comm(pid) == "kitty":
            return ["--to", f"unix:@kilix-{pid}"]
    raise KilixError("not running inside Kilix: no KITTY_LISTEN_ON and no Kilix ancestor")


def _run(argv: list[str], data: bytes | None = None) -> str:
    try:
        done = subprocess.run([KILIX, "@", *_target(), *argv], input=data, capture_output=True,
                              timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise KilixError(f"kilix @ {argv[0]} failed: {error}") from error
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip() or f"status {done.returncode}"
        raise KilixError(f"kilix @ {argv[0]}: {detail}")
    return done.stdout.decode("utf-8", "replace")


def snapshot(*, under_overlay: bool = False) -> "Tree":
    try:
        data = json.loads(_run(["ls"]))
    except ValueError as error:
        raise KilixError("kilix @ ls returned malformed JSON") from error
    return Tree(data, under_overlay=under_overlay)


def _program(window: dict) -> str:
    processes = window.get("foreground_processes") or []
    for process in processes:
        cmdline = process.get("cmdline") or []
        if cmdline:
            return os.path.basename(str(cmdline[0]))
    return ""


def _describe_pane(window: dict) -> str:
    program = _program(window)
    title = str(window.get("title") or "")
    label = title if not program or program in title else f"{title} ({program})"
    return f"pane {window.get('id')} '{label}'"


class Tree:
    """One `kilix @ ls` snapshot, focused on the OS window being used."""

    def __init__(self, data, *, under_overlay: bool = False):
        if not isinstance(data, list) or not data:
            raise KilixError("kilix reported no windows")
        # "This pane" is the pane kilix-needle was started from, not whichever
        # tab the user happens to be looking at: run from a background tab, the
        # focused pane is someone else's session. Without KITTY_WINDOW_ID (a
        # harness may strip it) the pane is the window whose process is one of
        # ours; that match is exact, so an unrelated window is never chosen.
        own = os.environ.get("KITTY_WINDOW_ID", "")
        ancestry = set(_ancestors()) if not own else set()
        os_window = own_tab = own_pane = None
        for candidate in data:
            for tab in candidate.get("tabs") or []:
                for window in tab.get("windows") or []:
                    if (own and str(window.get("id")) == own) or \
                            (not own and window.get("pid") in ancestry):
                        os_window, own_tab, own_pane = candidate, tab, window
        self.caller = own_pane
        if own_pane is not None and under_overlay:
            # Opened from a hotkey as an overlay, the overlay is the active
            # window; the pane the user means is the one it covers.
            history = [wid for wid in own_tab.get("active_window_history") or []
                       if wid != own_pane.get("id")]
            under = next((w for w in own_tab.get("windows") or []
                          if history and w.get("id") == history[-1]), None)
            if under is None:
                raise KilixError("cannot tell which pane the overlay was opened over")
            own_pane = under
        if os_window is None:
            os_window = next((w for w in data if w.get("is_focused")), None) \
                or next((w for w in data if w.get("is_active")), data[0])
        self.tabs = list(os_window.get("tabs") or [])
        if not self.tabs:
            raise KilixError("the Kilix window has no tabs")
        self.active_tab = own_tab or next((t for t in self.tabs if t.get("is_active")),
                                          self.tabs[0])
        windows = self.active_tab.get("windows") or []
        if not windows:
            raise KilixError("the active tab has no panes")
        self.active_pane = own_pane or next((w for w in windows if w.get("is_active")),
                                            windows[0])

    def _all_panes(self):
        for tab in self.tabs:
            for window in tab.get("windows") or []:
                yield tab, window

    def pane(self, ref: str) -> dict:
        if ref == "current":
            return self.active_pane
        if ref in ("next", "previous"):
            windows = self.active_tab.get("windows") or []
            if len(windows) < 2:
                raise KilixError("there is no other pane in this tab")
            index = windows.index(self.active_pane)
            return windows[(index + (1 if ref == "next" else -1)) % len(windows)]
        if ref in _NEIGHBOR:
            # Kitty reports neighbours as window *group* ids (measured: window 31's
            # right neighbour 122 is the group holding window 114). A group's
            # visible window is its last one; an overlay sits on top of the rest.
            ids = (self.active_pane.get("neighbors") or {}).get(_NEIGHBOR[ref]) or []
            if len(ids) != 1:
                raise KilixError(f"there is {'no' if not ids else 'more than one'} pane {ref} "
                                 "of the current one")
            group = next((g for g in self.active_tab.get("groups") or [] if g.get("id") == ids[0]),
                         None)
            members = (group or {}).get("windows") or []
            for window in self.active_tab.get("windows") or []:
                if members and window.get("id") == members[-1]:
                    return window
            raise KilixError(f"the pane {ref} of the current one is not listed")
        name = ref[5:].casefold()

        # Review KN-04: a substring of a title, preferred in the current tab,
        # chose "pleb@host: ~/src/catalog (vim)" for "the log pane" while `log`
        # ran in another tab. An exact program or title wins anywhere (the
        # current tab first); otherwise a whole word of a title, only if unique.
        def exact(window):
            return name in (_program(window).casefold(),
                            str(window.get("title") or "").strip().casefold())

        local = [w for w in self.active_tab.get("windows") or [] if exact(w)]
        found = local or [w for _tab, w in self._all_panes() if exact(w)]
        if not found:
            found = [w for _tab, w in self._all_panes()
                     if _whole_word(name, str(w.get("title") or ""))]
        if len(found) == 1:
            return found[0]
        if not found:
            raise KilixError(f"no pane is called or running {name!r}")
        raise KilixError(f"{name!r} matches several panes: "
                         + ", ".join(_describe_pane(w) for w in found))

    def tab(self, ref: str) -> dict:
        index = self.tabs.index(self.active_tab)
        if ref == "current":
            return self.active_tab
        if ref == "next":
            return self.tabs[(index + 1) % len(self.tabs)]
        if ref == "previous":
            return self.tabs[(index - 1) % len(self.tabs)]
        if ref == "last":
            return self.tabs[-1]
        if ref.isdigit():
            number = int(ref)
            if not 1 <= number <= len(self.tabs):
                raise KilixError(f"there is no tab {number}; there are {len(self.tabs)}")
            return self.tabs[number - 1]
        name = ref[5:].casefold() if ref.startswith("name:") else ref.casefold()
        found = [t for t in self.tabs if str(t.get("title") or "").strip().casefold() == name] \
            or [t for t in self.tabs if _whole_word(name, str(t.get("title") or ""))]
        if len(found) == 1:
            return found[0]
        if not found:
            raise KilixError(f"no tab is called {name!r}")
        raise KilixError(f"{name!r} matches several tabs: "
                         + ", ".join(f"{self.tabs.index(t) + 1} '{t.get('title')}'"
                                     for t in found))

    def describe_tab(self, tab: dict) -> str:
        count = len(tab.get("windows") or [])
        return (f"tab {self.tabs.index(tab) + 1} '{tab.get('title')}' "
                f"({count} pane{'s' if count != 1 else ''})")


def _whole_word(name: str, title: str) -> bool:
    """`name` as whole words of `title`; path characters join a word, so
    "log" is not in "~/src/catalog" and "host" is not in "pleb@host:"."""
    edge = r"[\w./~@:-]"
    return re.search(rf"(?<!{edge}){re.escape(name)}(?!{edge})", title, re.I) is not None


def _program_argv(program: str) -> list[str]:
    try:
        argv = shlex.split(program)
    except ValueError as error:
        raise KilixError(f"cannot split the program {program!r}: {error}") from error
    if not argv:
        raise KilixError("the program is empty")
    return argv


def resolve(action: Action, tree: Tree) -> Step:
    """Bind an admitted action to concrete ids and the argv that performs it."""
    kind, args = action.kind, action.args
    if kind == "open_pane":
        # Measured live: without a tab match, launch opens the pane in whichever
        # tab is active, and --cwd=current takes that tab's directory. Both are
        # bound to the anchor pane explicitly.
        anchor = tree.active_pane
        argv = ["launch", "--type=window", f"--match=window_id:{anchor['id']}",
                f"--source-window=id:{anchor['id']}", "--cwd=current",
                f"--next-to=id:{anchor['id']}"]
        side = args.get("side")
        if side:
            argv.append(f"--location={_LOCATION[side]}")
        if args.get("name"):
            argv.append(f"--title={args['name']}")
        words = f"open a pane{' ' + side if side else ''} of {_describe_pane(anchor)}"
        if args.get("program"):
            argv += ["--", *_program_argv(args["program"])]
            words += f" running {args['program']!r}"
        return Step(action, words, ((tuple(argv), None),))
    if kind == "open_tab":
        argv = ["launch", "--type=tab", f"--source-window=id:{tree.active_pane['id']}",
                "--cwd=current"]
        if args.get("name"):
            argv.append(f"--tab-title={args['name']}")
        words = "open a new tab" + (f" called {args['name']!r}" if args.get("name") else "")
        if args.get("program"):
            argv += ["--", *_program_argv(args["program"])]
            words += f" running {args['program']!r}"
        return Step(action, words, ((tuple(argv), None),))
    if kind in ("close_pane", "go_to_pane"):
        window = tree.pane(args["pane"])
        verb = "close" if kind == "close_pane" else "go to"
        command = "close-window" if kind == "close_pane" else "focus-window"
        return Step(action, f"{verb} {_describe_pane(window)}",
                    (((command, f"--match=id:{window['id']}"), None),),
                    frozenset({window["id"]}) if kind == "close_pane" else frozenset())
    if kind in ("close_tab", "go_to_tab"):
        tab = tree.tab(args["tab"])
        verb = "close" if kind == "close_tab" else "go to"
        command = "close-tab" if kind == "close_tab" else "focus-tab"
        return Step(action, f"{verb} {tree.describe_tab(tab)}",
                    (((command, f"--match=id:{tab['id']}"), None),),
                    frozenset(w["id"] for w in tab.get("windows") or [])
                    if kind == "close_tab" else frozenset())
    if kind == "arrange_panes":
        tab = tree.active_tab
        enabled = tab.get("enabled_layouts") or []
        if args["layout"] not in enabled:
            raise KilixError(f"the {args['layout']} layout is not enabled here; "
                             f"enabled: {', '.join(enabled) or 'none'}")
        return Step(action, f"arrange {tree.describe_tab(tab)} as {args['layout']}",
                    ((("goto-layout", f"--match=id:{tab['id']}", args["layout"]), None),))
    if kind == "rename_tab":
        tab = tree.active_tab
        return Step(action, f"rename {tree.describe_tab(tab)} to {args['name']!r}",
                    ((("set-tab-title", f"--match=id:{tab['id']}", args["name"]), None),))
    if kind == "rename_pane":
        window = tree.pane(args["pane"])
        return Step(action, f"rename {_describe_pane(window)} to {args['name']!r}",
                    ((("set-window-title", f"--match=id:{window['id']}", "--", args["name"]), None),))
    if kind == "maximize_pane":
        window = tree.pane(args["pane"])
        tab = next(t for t, w in tree._all_panes() if w["id"] == window["id"])
        stacked = tab.get("layout") == "stack"
        if args["restore"]:
            commands = ((("last-used-layout", f"--match=id:{tab['id']}"), None),) if stacked else ()
            return Step(action, f"restore the layout of {tree.describe_tab(tab)}", commands)
        if "stack" not in (tab.get("enabled_layouts") or []):
            raise KilixError("the stack layout is not enabled in the target tab")
        commands = ((("focus-window", f"--match=id:{window['id']}"), None),)
        if not stacked:
            commands += ((("goto-layout", f"--match=id:{tab['id']}", "stack"), None),)
        return Step(action, f"maximize {_describe_pane(window)}", commands)
    if kind == "swap_panes":
        anchor = tree.active_pane
        other = tree.pane(args["side"])  # refuses missing or ambiguous neighbours
        # Tab.move_window operates on the tab's active group, not the matched
        # window. Focus the anchor explicitly before invoking it.
        return Step(action, f"swap {_describe_pane(anchor)} with {_describe_pane(other)}",
                    ((("focus-window", f"--match=id:{anchor['id']}"), None),
                     (("action", f"--match=id:{anchor['id']}",
                       f"move_window {_NEIGHBOR[args['side']]}"), None)))
    if kind == "move_tab":
        tab, anchor = tree.active_tab, tree.active_pane
        index = tree.tabs.index(tab)
        destination = (args["position"] - 1 if "position" in args else
                       index + (-1 if args["direction"] == "left" else 1))
        if not 0 <= destination < len(tree.tabs):
            raise KilixError("the requested tab position is outside this window")
        distance = destination - index
        commands = ()
        if distance:
            # Boss.move_tab_forward uses the active OS window. An action match
            # alone does not select either the tab or its OS window.
            commands = ((("focus-window", f"--match=id:{anchor['id']}"), None),)
            verb = "move_tab_forward" if distance > 0 else "move_tab_backward"
            commands += ((("action", f"--match=id:{anchor['id']}", verb), None),) * abs(distance)
        return Step(action, f"move {tree.describe_tab(tab)} to position {destination + 1}", commands)
    if kind == "resize_pane":
        window = tree.active_pane
        axis, sign = _RESIZE[args["direction"]]
        return Step(action, f"make {_describe_pane(window)} {args['direction']} "
                            f"by {args['amount']}",
                    ((("resize-window", f"--match=id:{window['id']}", f"--axis={axis}",
                       f"--increment={sign * args['amount']}"), None),))
    if kind == "run_in_pane":
        window = tree.pane(args["pane"])
        if window.get("at_prompt") is not True:
            raise KilixError(f"{_describe_pane(window)} is not at a shell prompt; "
                             "nothing will be typed into it")
        text = args["command"].encode("utf-8")
        if len(text) > MAX_TYPED:
            raise KilixError("the command is too long to type")
        broker = str((window.get("env") or {}).get("KITTY_PTY_BROKER_SESSION") or "")
        match = f"--match=env:KITTY_PTY_BROKER_SESSION={broker}" if _BROKER.fullmatch(broker) \
            else f"--match=id:{window['id']}"
        return Step(action, f"type {args['command']!r} into {_describe_pane(window)} and press Enter",
                    ((("send-text", match, "--stdin"), text),
                     (("send-text", match, "--stdin"), b"\r")))
    raise KilixError(f"no kilix command for {kind}")


def perform(step: Step) -> None:
    for argv, data in step.commands:
        _run(list(argv), data)
