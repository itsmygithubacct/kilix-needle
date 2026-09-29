"""Canonical pane commands, read without a model.

An agent's exact command ("Close the pane titled bench-target in this tab.",
"run 'make' in pane 70", "go to tab 2") needs no inference: the model once
proposed close_tab for the first, twice (token-cost finding 13, route benchmark
2026-09-29). These forms are turned into calls directly. The calls still pass
every check (actions.interpret), resolution against the live tree and the
same confirmation as a model's; if the checks refuse any of them, this route
steps aside and the model reads the request as before.
"""
from __future__ import annotations

import re

from actions import Action, interpret, normalize

_HEAD = re.compile(r"^(?:(?:please|pls|kindly|ok|okay|just|can you|could you|would you)[\s,]+)+", re.I)
_TAIL = re.compile(r"(?:[\s,]+(?:please|pls|thanks|thank you|now))*[\s.!]*$", re.I)
_NAME = r"[\w.+@:-]+"
_HERE = r"(?:\s+in\s+(?:this|the current)\s+tab)?"
# A pane: "the pane titled X", "the X pane", "pane 70" / "pane:70", "the left pane".
_PANE = (rf"(?:(?:the\s+)?pane\s+(?:titled|named|called|labell?ed)\s+(?P<titled>{_NAME})"
         rf"|(?:the\s+)?pane[:\s]\s*(?P<id>[0-9]{{1,6}})"
         rf"|(?:the\s+)?pane\s+(?!(?:titled|named|called|labell?ed)\b)(?P<bare>{_NAME})"
         rf"|(?:the\s+)?(?P<name>{_NAME})\s+pane){_HERE}")
_QUOTED = r"(?:'(?P<sq>[^'\n]+)'|\"(?P<dq>[^\"\n]+)\"|`(?P<bq>[^`\n]+)`)"
_FORMS = (
    ("close_pane", re.compile(rf"(?:close|kill)\s+{_PANE}", re.I)),
    ("go_to_pane", re.compile(rf"(?:go\s+to|focus|switch\s+to)\s+{_PANE}", re.I)),
    ("run_in_pane", re.compile(rf"(?:run|type)\s+(?:the\s+command\s+)?{_QUOTED}\s+(?:in|into)\s+{_PANE}"
                               rf"(?:,?\s+(?:and\s+)?(?:press|hit)\s+(?:enter|return))?", re.I)),
    ("close_tab", re.compile(r"close\s+tab\s+(?P<tab>[0-9]{1,2})", re.I)),
    ("go_to_tab", re.compile(r"(?:go\s+to|switch\s+to)\s+tab\s+(?P<tab>[0-9]{1,2})", re.I)),
)


def calls(request: str) -> list | None:
    """The one call an exact pane command states, or None."""
    text = _TAIL.sub("", _HEAD.sub("", normalize(request).strip()))
    for kind, form in _FORMS:
        m = form.fullmatch(text)
        if not m:
            continue
        found = m.groupdict()
        if kind in ("close_tab", "go_to_tab"):
            return [{"name": kind, "arguments": {"tab": found["tab"]}}]
        pane = (found.get("titled") or found.get("id") or found.get("bare")
                or found.get("name"))
        args = {"pane": pane}
        if kind == "run_in_pane":
            args["command"] = found.get("sq") or found.get("dq") or found.get("bq")
        return [{"name": kind, "arguments": args}]
    return None


def admitted(request: str) -> list | None:
    """Those calls, only when every one of them passes the checks."""
    found = calls(request)
    if not found:
        return None
    results = interpret(request, found)
    if not results or not all(isinstance(r, Action) for r in results):
        return None
    return found
