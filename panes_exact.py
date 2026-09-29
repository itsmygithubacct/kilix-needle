"""Canonical pane commands, read without a model.

An agent's exact command ("Close the pane titled bench-target in this tab.",
"run 'make' in pane 70", "go to tab 2") needs no inference: the model once
proposed close_tab for the first, twice (token-cost finding 13, route benchmark
2026-09-29), and in the gpt-6-luna benchmark every failed pane task was a
wording like these that reached the model. These forms are turned into calls
directly. The calls still pass every check (actions.interpret), resolution
against the live tree and the same confirmation as a model's; if the checks
refuse any of them, this route steps aside and the model reads the request as
before.
"""
from __future__ import annotations

import re

from actions import _SHELL_ARGUMENT, Action, interpret, normalize

_HEAD = re.compile(r"^(?:(?:please|pls|kindly|ok|okay|just|can you|could you|would you)[\s,]+)+", re.I)
_TAIL = re.compile(r"(?:[\s,]+(?:please|pls|thanks|thank you|now))*[\s.!]*$", re.I)
_WORD = r"[\w.+@:-]+"
# A pane's name, bare or in quotes/backticks: "the pane titled "bench-target"".
_NAME = rf"(?:\"(?P<{{g}}q>[^\"\n]+)\"|'(?P<{{g}}s>[^'\n]+)'|`(?P<{{g}}b>[^`\n]+)`|(?P<{{g}}w>{_WORD}))"
_HERE = r"(?:\s+in\s+(?:this|the current)\s+tab)?(?:,?\s+not\s+the\s+tab(?:\s+itself)?)?"


def _pane(prefix: str = "") -> str:
    """A pane: "the pane titled X", "the X pane", "pane 70" / "pane:70", "pane X"."""
    titled = _NAME.format(g=f"{prefix}t")
    bare = _NAME.format(g=f"{prefix}n")
    return (rf"(?:(?:the\s+)?pane\s+(?:titled|named|called|labell?ed)\s+{titled}"
            rf"|(?:the\s+)?pane[:\s]\s*(?P<{prefix}id>[0-9]{{1,6}})"
            rf"|(?:the\s+)?pane\s+(?!(?:titled|named|called|labell?ed)\b){bare}"
            rf"|(?:the\s+)?(?P<{prefix}name>{_WORD})\s+pane){_HERE}")


_QUOTED = r"(?:'(?P<sq>[^'\n]+)'|\"(?P<dq>[^\"\n]+)\"|`(?P<bq>[^`\n]+)`)"
# An unquoted command: a program, then only plain shell arguments ("touch /x/y").
_UNQUOTED = r"(?P<uq>[A-Za-z_][\w.+-]*(?:\s+\S+)*?)"
_TYPE = r"(?:type|run|enter|execute)(?:\s+and\s+(?:run|press\s+enter))?(?:\s+(?:the\s+command|exactly))?:?"
_ENTER = (r"(?:\s+at\s+(?:the|its)\s+(?:shell\s+)?prompt)?"
          r"(?:,?\s+(?:and\s+)?(?:then\s+)?(?:press|hit)\s+(?:enter|return))?")
_SIDE = r"(?P<side>right|left|below|above|down|up)"
_SHELLS = {"bash", "sh", "zsh", "fish", "dash", "shell", "a shell", "the shell", "a new shell",
           "a terminal", "terminal"}
_FORMS = (
    ("close_pane", re.compile(rf"(?:close|kill)\s+{_pane()}", re.I)),
    ("go_to_pane", re.compile(rf"(?:go\s+to|focus|switch\s+to)\s+{_pane()}", re.I)),
    ("run_in_pane", re.compile(rf"{_TYPE}\s+(?:{_QUOTED}|{_UNQUOTED})\s+(?:in|into)\s+{_pane()}{_ENTER}",
                               re.I)),
    ("run_in_pane", re.compile(rf"in\s+{_pane()},?\s+{_TYPE}\s+(?:{_QUOTED}|{_UNQUOTED}){_ENTER}", re.I)),
    ("run_in_pane", re.compile(rf"(?:type\s+and\s+press\s+enter|type\s+and\s+run):?\s+(?:{_QUOTED}|{_UNQUOTED})"
                               rf"\s+(?:in|into)\s+{_pane()}", re.I)),
    ("open_pane", re.compile(
        rf"(?:split(?:\s+(?:the\s+)?(?:pane|window))?|open\s+(?:a\s+)?(?:new\s+)?(?:shell\s+)?pane)"
        rf"(?:\s+directly)?\s+(?:to\s+the\s+|on\s+the\s+)?{_SIDE}"
        rf"(?:\s+of\s+(?:this|the\s+current|my|the)\s+pane(?:\s+you\s+are\s+running\s+in)?)?"
        rf"(?:(?:,?\s+and\s+(?:run|start)|[.;]\s+it\s+should\s+run|,?\s+running|\s+with)\s+"
        rf"(?P<prog>'[^'\n]+'|`[^`\n]+`|[\w.+-]+(?:\s+[\w.+-]+)?)(?:\s+in\s+(?:it|the\s+new\s+pane))?)?", re.I)),
    ("close_tab", re.compile(r"close\s+tab\s+(?P<tab>[0-9]{1,2})", re.I)),
    ("go_to_tab", re.compile(r"(?:go\s+to|switch\s+to)\s+tab\s+(?P<tab>[0-9]{1,2})", re.I)),
)
_SIDES = {"down": "below", "up": "above"}


def _first(found: dict, *keys):
    return next((found[k] for k in keys if found.get(k)), None)


def _quote(text: str) -> str:
    for q in ("'", "`", '"'):
        if q not in text:
            return f"{q}{text}{q}"
    return text


def _pane_words(found: dict, pane: str) -> str:
    """How the canonical sentence names the pane the request named."""
    if found.get("id"):
        return f"pane {pane}"
    if found.get("name"):
        return f"the {pane} pane"
    return f"the pane titled {_quote(pane) if ' ' in pane else pane}"


def read(request: str) -> tuple[list, str] | None:
    """The one call an exact pane command states, and the canonical sentence it
    was read as, or None. The fullmatch allows only fixed filler around the
    values, so the canonical sentence says exactly what the request says."""
    text = _TAIL.sub("", _HEAD.sub("", normalize(request).strip()))
    for kind, form in _FORMS:
        m = form.fullmatch(text)
        if not m:
            continue
        found = m.groupdict()
        if kind in ("close_tab", "go_to_tab"):
            verb = "close" if kind == "close_tab" else "go to"
            return [{"name": kind, "arguments": {"tab": found["tab"]}}], f"{verb} tab {found['tab']}"
        if kind == "open_pane":
            args = {"side": _SIDES.get(found["side"].lower(), found["side"].lower())}
            program = (found.get("prog") or "").strip("'`").strip()
            if program and program.lower() not in _SHELLS:
                args["program"] = program
            shown = program if re.fullmatch(r"[\w.+-]+", program) else _quote(program)
            canonical = f"split {args['side']}" + (f" and run {shown}" if "program" in args else "")
            return [{"name": kind, "arguments": args}], canonical
        pane = _first(found, "tq", "ts", "tb", "tw", "id", "nq", "ns", "nb", "nw", "name")
        args = {"pane": pane}
        if kind == "run_in_pane":
            command = _first(found, "sq", "dq", "bq")
            if command is None:
                command = found.get("uq") or ""
                words = command.split()
                # Unquoted, only a program and plain shell arguments: English after
                # a program may be a condition or a delay that would be typed too.
                if not words or not all(_SHELL_ARGUMENT.fullmatch(w) for w in words[1:]):
                    return None
            args["command"] = command
            return [{"name": kind, "arguments": args}], f"run {_quote(command)} in {_pane_words(found, pane)}"
        verb = "close" if kind == "close_pane" else "go to"
        return [{"name": kind, "arguments": args}], f"{verb} {_pane_words(found, pane)}"
    return None


def calls(request: str) -> list | None:
    got = read(request)
    return got[0] if got else None


def admitted(request: str) -> tuple[list, str] | None:
    """Those calls and the canonical sentence, only when the checks admit every
    call for that sentence."""
    got = read(request)
    if not got:
        return None
    found, canonical = got
    results = interpret(canonical, found)
    if not results or not all(isinstance(r, Action) for r in results):
        return None
    return found, canonical
