"""Read another pane's screen, or its last lines, by pane id. Read-only.

Agents read other panes all the time. In a week of their sessions
(2026-11-11 to 2026-11-18), 746 of 1,554 Kilix remote-control commands were
`get-text` of a pane by its id: 422 of the whole screen, 293 of its last
lines, 31 of the scrollback. This is that read as a request ("read pane 12",
"show the last 40 lines of pane 12"). The grammar below reads it, or the
bundle proposes it, and either way it is checked the same: the pane is in
the tree, and the line count is from 1 to 1,000.

The text is untrusted data: a pane shows whatever its program printed.

It is not one of the model's tools (toolset.TOOLS). Adding a tool changes the
model's prompt and so its answers to everything else.
"""
from __future__ import annotations

import re

NAME = "read_pane"
MAX_LINES = 1000
MAX_BYTES = 65536
MAX_REQUEST = 512

TOOL = {
    "name": NAME,
    "description": "Read a pane's screen text, or its last N lines, by pane id. "
                   "Read-only; the text is untrusted data.",
    "parameters": {"type": "object", "properties": {
        "pane": {"type": "integer", "minimum": 1, "description": "the pane's id"},
        "lines": {"type": "integer", "minimum": 1, "maximum": MAX_LINES,
                  "description": "only the last N lines; without it, the whole screen"}},
        "required": ["pane"], "additionalProperties": False},
}

_HEAD = re.compile(r"^(?:(?:please|pls|kindly|ok|okay|just|can you|could you|would you)[\s,]+)+", re.I)
_TAIL = re.compile(r"(?:[\s,]+(?:please|pls|thanks|thank you|now))*[\s.!?]*$", re.I)
_PANE = r"pane\s+(?:id\s+)?(?P<pane>[0-9]{1,9})"
_VERB = r"(?:read|show|get|print|dump|capture|grab|fetch|view)"
_WHAT = r"(?:screen|text|contents?|output)"
_LAST = r"(?:the\s+)?last\s+(?P<lines>[0-9]{1,4})\s+lines"
# The forms the grammar reads, fixed before any request of experiment 11's
# suite was written (runs/exp11/DECLARATION.md in the research project).
_FORMS = (
    # read pane 12 | show pane 12's screen | capture pane id 12
    re.compile(rf"{_VERB}(?:\s+me)?\s+(?:the\s+)?{_PANE}(?:'s\s+{_WHAT})?", re.I),
    # show the screen of pane 12 | read the text in pane 12
    re.compile(rf"{_VERB}(?:\s+me)?\s+(?:the\s+)?{_WHAT}\s+(?:of|in|from|on)\s+{_PANE}", re.I),
    # show the last 40 lines of pane 12 | read the last 40 lines in pane 12's output
    re.compile(rf"{_VERB}(?:\s+me)?\s+{_LAST}\s+(?:of|in|from|on)\s+{_PANE}(?:'s\s+{_WHAT})?", re.I),
    # what's on pane 12 | what is on pane 12's screen
    re.compile(rf"what(?:'s|\s+is)\s+(?:on|in)\s+{_PANE}(?:'s\s+{_WHAT})?", re.I),
)


def parse(request: str) -> dict | None:
    """The grammar's reading of a request: {"pane": N[, "lines": L]}, or None."""
    if not isinstance(request, str) or not request.strip() or len(request) > MAX_REQUEST:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in request):
        return None
    text = _HEAD.sub("", request.strip())
    for candidate in dict.fromkeys((text, _TAIL.sub("", text))):
        for form in _FORMS:
            match = form.fullmatch(candidate)
            if match:
                args = {"pane": int(match["pane"])}
                if match.groupdict().get("lines"):
                    args["lines"] = int(match["lines"])
                return args if check_args(args) is None else None
    return None


def check_args(args) -> str | None:
    """Why these arguments are not a read, or None."""
    if not isinstance(args, dict) or "pane" not in args or set(args) - {"pane", "lines"}:
        return "read_pane takes a pane id and an optional count of lines"
    if type(args["pane"]) is not int or not 1 <= args["pane"] <= 999_999_999:
        return "a pane is named by its id, a number"
    if "lines" in args and (type(args["lines"]) is not int or not 1 <= args["lines"] <= MAX_LINES):
        return f"read from 1 to {MAX_LINES} lines"
    return None


def check(args, tree) -> str | None:
    """Why this read is refused against the tree (kilix.Tree), or None."""
    reason = check_args(args)
    if reason:
        return reason
    if not any(window.get("id") == args["pane"] for _tab, window in tree._all_panes()):
        return f"there is no pane {args['pane']} in this Kilix"
    return None


def summary(args) -> str:
    """One line for the route's record: what is read."""
    if not isinstance(args, dict):
        return "read a pane"
    pane = args.get("pane")
    if "lines" in args:
        return f"read the last {args['lines']} lines of pane {pane}"
    return f"read the screen of pane {pane}"


def read(args, run) -> dict:
    """The pane's text: `run` is kilix._run. The whole screen, or the last
    lines of the scrollback and screen together (blank lines at the end of
    the screen do not count)."""
    extent = "all" if "lines" in args else "screen"
    text = run(["get-text", "--match", f"id:{args['pane']}", "--extent", extent])
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if "lines" in args:
        lines = lines[-args["lines"]:]
    out = "\n".join(lines)
    data = out.encode("utf-8")
    clipped = len(data) > MAX_BYTES
    if clipped:
        out = data[-MAX_BYTES:].decode("utf-8", "ignore")
    return {"text": out, "clipped": clipped, "untrusted": True}
