"""Whole-request grammar for persistent pane sessions (`kilix pty`); no inference.

One request is one operation. A request that is not exactly one of the forms
below refuses whole, with a hint that names one accepted form. IDs are opaque
literals: a quoted ID or a bare lowercase-hex ID, read before any other word.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata


MAX_REQUEST = 1024
MAX_LINES = 1000
MAX_BYTES = 65536
MIN_TIMEOUT, MAX_TIMEOUT = 0.1, 60.0
ID_CHARS = re.compile(r"[A-Za-z0-9._-]{1,64}", re.ASCII)
EXAMPLE_ID = "0123456789abcdef"
OPERATIONS = ("list", "journals", "status", "pane", "observe", "journal", "kill")
READS = frozenset(OPERATIONS) - {"kill"}
USAGE = ("forms, one per request: list sessions [all] | show session ID | "
         "which session is pane N | show the last N lines of session ID | "
         "list archived journals | show archived journal ID | end session ID. "
         "ID: the full ID, 16-64 lowercase hex, or quoted. N: 1-1000.")
HINTS = {
    "kill": f"end session {EXAMPLE_ID}",
    "observe": f"show the last 50 lines of session {EXAMPLE_ID}",
    "journal": f"show archived journal {EXAMPLE_ID}",
    "journals": "list archived journals",
    "pane": "which session is pane 12",
    "status": f"show session {EXAMPLE_ID}",
    "list": "list sessions",
}
STRUCTURED_HINTS = {
    "list": '{"operation":"list"}',
    "journals": '{"operation":"journals"}',
    "status": f'{{"operation":"status","id":"{EXAMPLE_ID}"}}',
    "pane": '{"operation":"pane","pane_id":12}',
    "observe": f'{{"operation":"observe","id":"{EXAMPLE_ID}","max_lines":50}}',
    "journal": f'{{"operation":"journal","id":"{EXAMPLE_ID}","max_lines":50}}',
    "kill": f'{{"operation":"kill","id":"{EXAMPLE_ID}"}}',
}


class Refused(ValueError):
    """The whole request is refused; `hint` is one accepted form."""

    def __init__(self, note: str, hint: str):
        super().__init__(note)
        self.hint = hint


# An ID is read first and as one opaque token: a quoted ID of the ID characters,
# or 16-64 lowercase hex. `(?-i:...)` keeps IDs case-sensitive under re.I.
_ID = (r"""(?-i:"[A-Za-z0-9._-]{1,64}"|'[A-Za-z0-9._-]{1,64}'|`[A-Za-z0-9._-]{1,64}`"""
       r"""|[0-9a-f]{16,64})""")
# A journal may also be named `ID.STARTED_MILLIS`, which tells reused IDs apart.
_JOURNAL_ID = (r"""(?-i:"[A-Za-z0-9._-]{1,64}"|'[A-Za-z0-9._-]{1,64}'|`[A-Za-z0-9._-]{1,64}`"""
               r"""|[0-9a-f]{16,64}(?:\.[0-9]{1,20})?)""")
_LINES = r"(?P<lines>[0-9]{1,5})"
_PANE = r"(?P<pane>[0-9]{1,9})"
_HEAD = re.compile(r"^((?:(?:please|can you|could you|would you|will you|kindly)\s+)+)", re.I | re.A)
_TAIL = re.compile(r"(?:,?\s+(?:please|thanks|thank you))?[.!?]*$", re.I | re.A)
_FORMS = (
    ("list", re.compile(r"(?:list|show|display)\s+(?:(?:all|the|my)\s+)?"
                        r"(?:(?:pty|persistent|broker)\s+)?sessions(?:\s+all)?", re.I | re.A)),
    ("journals", re.compile(r"(?:list|show)\s+(?:(?:all|the)\s+)?(?:archived|saved)\s+journals",
                            re.I | re.A)),
    ("status", re.compile(rf"(?:(?:show|get|describe|inspect)\s+)?(?:the\s+)?"
                          rf"(?:(?:status|state|details)\s+of\s+)?session\s+(?P<id>{_ID})",
                          re.I | re.A)),
    ("pane", re.compile(rf"(?:which|what)\s+session\s+(?:is|belongs\s+to|is\s+behind|"
                        rf"is\s+running\s+in|is\s+in)\s+(?:the\s+)?pane\s+(?:id\s+)?{_PANE}",
                        re.I | re.A)),
    ("pane", re.compile(rf"(?:show|get)\s+(?:the\s+)?session\s+(?:of|for|behind)\s+"
                        rf"(?:the\s+)?pane\s+(?:id\s+)?{_PANE}", re.I | re.A)),
    ("observe", re.compile(rf"(?:show|read|get|tail)\s+(?:the\s+)?last\s+{_LINES}\s+lines\s+"
                           rf"(?:of|from)\s+(?:the\s+)?(?:output\s+of\s+)?session\s+(?P<id>{_ID})",
                           re.I | re.A)),
    ("journal", re.compile(rf"(?:show|read|get)\s+(?:the\s+)?(?:archived\s+)?journal\s+"
                           rf"(?:of\s+(?:session\s+)?)?(?P<id>{_JOURNAL_ID})", re.I | re.A)),
    ("kill", re.compile(rf"(?:end|kill|terminate)\s+(?:the\s+)?session\s+(?P<id>{_ID})",
                        re.I | re.A)),
)

# Words that make a refused request worth a more specific note. They are read
# only after the IDs have been taken out, and never admit anything.
_NEGATION = re.compile(r"\b(?:not|never|no|don'?t|do\s+not|dont|cancel|without|isn'?t|won'?t)\b", re.I)
_HEARSAY = re.compile(r"\b(?:said|says|say|told|tells?|asked|asks?|wants?|wanted|thinks?|heard|"
                      r"claims?|reported|suggest(?:s|ed)?|wrote|note|ticket|quote[sd]?)\b", re.I)
_CONDITIONAL = re.compile(r"\b(?:if|when|whenever|unless|once|after|before|in\s+case|should|until)\b", re.I)
_COMPOUND = re.compile(r"\b(?:and|then|also|plus|but|or|both|each|every|all\s+but)\b|[;&|,]", re.I)
_OWN = re.compile(r"\b(?:this|my|own|current|mine|here)\b", re.I)
_ORDINAL = re.compile(r"\b(?:first|second|third|last|oldest|newest|latest|other|next|previous|"
                      r"stuck|wedged|detached|attached|dead|idle|biggest|that\s+one|the\s+one)\b", re.I)
_TITLE = re.compile(r"\b(?:named|called|titled|title|name|command|running|prefix|starting\s+with|"
                    r"beginning\s+with)\b", re.I)
_KILL_WORDS = re.compile(r"\b(?:end|kill|terminate|stop|close|delete|remove|reap|destroy|quit)\b", re.I)
_QUOTED = re.compile(r"""\"[^\"]*\"|'[^']*'|`[^`]*`""")
_SHORT_HEX = re.compile(r"(?<![A-Za-z0-9._-])[0-9a-f]{4,15}(?![A-Za-z0-9._-])")
_LONG_ID = re.compile(r"(?<![A-Za-z0-9._-])[A-Za-z0-9._-]{65,}(?![A-Za-z0-9._-])")
_BARE_ID = re.compile(r"(?<![A-Za-z0-9._-])[0-9a-f]{16,64}(?![A-Za-z0-9._-])")
_ODD_ID = re.compile(r"(?<![A-Za-z0-9._-])(?=[A-Za-z0-9._-]*[0-9._-])[A-Za-z0-9._-]{16,64}(?![A-Za-z0-9._-])")


def id_literal(ident: str) -> str:
    """The spelling of an ID the grammar reads back: bare lowercase hex, else quoted."""
    return ident if re.fullmatch(r"[0-9a-f]{16,64}", ident) else f'"{ident}"'


def show_request(ident: str) -> str:
    return f"show session {id_literal(ident)}"


def valid_id(ident) -> bool:
    """An ID Needle will pass to `kilix pty` as one argument. A leading '-' would read as a flag."""
    return (isinstance(ident, str) and ID_CHARS.fullmatch(ident) is not None
            and ident not in (".", "..") and not ident.startswith("-"))


def controls(text: str) -> bool:
    return any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in text)


def hint_for(request: str) -> str:
    """One accepted form, chosen by what the refused request seems to want."""
    text = request.casefold()
    if _KILL_WORDS.search(text):
        return HINTS["kill"]
    if re.search(r"\b(?:lines?|tail|output|last|read)\b", text):
        return HINTS["observe"]
    if "journals" in text:
        return HINTS["journals"]
    if "journal" in text:
        return HINTS["journal"]
    if re.search(r"\bpane\b", text):
        return HINTS["pane"]
    if re.search(r"\b(?:status|state|details|show|describe|inspect|get)\b.*\bsession\b", text):
        return HINTS["status"]
    return HINTS["list"]


def _reason(request: str) -> str:
    """Why a request outside the grammar was refused, for the note only."""
    words = _QUOTED.sub(" ID ", request)
    words = _BARE_ID.sub(" ID ", words)
    if _LONG_ID.search(words):
        return "an ID is at most 64 characters"
    if _SHORT_HEX.search(words) and re.search(r"\bsession\b|\bjournal\b", words, re.I):
        return "an ID must be the full session ID, never a prefix; copy it from 'list sessions'"
    if _ODD_ID.search(words):
        return "IDs are case-sensitive; a bare ID is lowercase hex, quote any other ID"
    for pattern, reason in (
            (_NEGATION, "a negated request"), (_HEARSAY, "a report of what someone said or wants"),
            (_CONDITIONAL, "a conditional request"), (_COMPOUND, "more than one operation or a list")):
        if pattern.search(words):
            return reason
    if _OWN.search(words):
        return "a session named by position or 'this/my', not by its full ID"
    if _ORDINAL.search(words) or _TITLE.search(words):
        return "a session named by position, state, title or command, not by its full ID"
    return "not one of the pty forms"


def parse(request: str) -> dict:
    """Return one operation and its literal arguments, or refuse the whole request."""
    if not isinstance(request, str) or not request.strip():
        raise Refused("provide one pty request", HINTS["list"])
    if len(request) > MAX_REQUEST:
        raise Refused(f"a pty request is at most {MAX_REQUEST} characters", HINTS["list"])
    hint = hint_for(request)
    if controls(request):
        raise Refused("control and format characters are not supported in a pty request", hint)
    stripped = request.strip()
    head = _HEAD.match(stripped)
    asked = bool(head and re.search(r"\b(?:can|could|would|will)\b", head[1], re.I))
    body = stripped[head.end():] if head else stripped
    found = None
    # Prefer the complete literal reading: a quoted ID ending in a dot keeps it.
    for text in dict.fromkeys((body, _TAIL.sub("", body))):
        if text != body and "?" in body[len(text):] and not asked:
            continue
        for operation, form in _FORMS:
            match = form.fullmatch(text)
            if match:
                found = operation, match
                break
        if found:
            break
    if not found:
        raise Refused(f"refused the whole request: {_reason(request)}. " + USAGE, hint)
    operation, match = found
    args = {key: value for key, value in match.groupdict().items() if value is not None}
    if "id" in args:
        ident = args["id"]
        if ident[:1] in "\"'`":
            ident = ident[1:-1]
        args["id"] = ident
        if not valid_id(ident):
            raise Refused("an ID is 1-64 of letters, digits, '.', '_' and '-', not starting with '-' "
                          "and not '.' or '..'", hint)
    if "lines" in args:
        lines = int(args.pop("lines"))
        if not 1 <= lines <= MAX_LINES:
            raise Refused(f"N lines is a count of lines, 1-{MAX_LINES}", hint)
        args["max_lines"] = lines
    if "pane" in args:
        args["pane_id"] = int(args.pop("pane"))
    return {"operation": operation, **args}


def _number(value, name, low, high, unit, whole=False):
    ok = (type(value) is int) if whole else (type(value) in (int, float) and math.isfinite(value))
    if not ok or not low <= value <= high:
        raise ValueError(f"{name} is {unit}, {'an integer ' if whole else ''}from {low:g} to {high:g}")
    return value


def structured(request) -> dict:
    """The same operations as an object: unknown fields and wrong types refuse."""
    operation = request.get("operation") if isinstance(request, dict) else None
    hint = STRUCTURED_HINTS.get(operation, STRUCTURED_HINTS["list"]) if isinstance(operation, str) \
        else STRUCTURED_HINTS["list"]
    try:
        if not isinstance(request, dict):
            raise ValueError("a structured pty request is an object")
        if operation not in OPERATIONS:
            raise ValueError("operation is one of " + ", ".join(OPERATIONS))
        needs = {"status": {"id"}, "observe": {"id"}, "journal": {"id"}, "kill": {"id"},
                 "pane": {"pane_id"}, "list": set(), "journals": set()}[operation]
        bounds = {"max_lines", "max_bytes"} if operation in ("observe", "journal") else set()
        extra = {"timeout_seconds"}
        unknown = set(request) - {"operation"} - needs - bounds - extra
        if unknown:
            raise ValueError(f"{operation} does not take {', '.join(sorted(unknown))}")
        missing = needs - set(request)
        if missing:
            raise ValueError(f"{operation} requires {', '.join(sorted(missing))}")
        if {"max_lines", "max_bytes"} <= set(request):
            raise ValueError("give max_lines or max_bytes, not both")
        action = {"operation": operation}
        if "id" in request:
            ident = request["id"]
            if not valid_id(ident):
                raise ValueError("id is the full session ID: 1-64 of letters, digits, '.', '_', '-', "
                                 "not starting with '-'")
            action["id"] = ident
        if "pane_id" in request:
            action["pane_id"] = _number(request["pane_id"], "pane_id", 0, 999999999,
                                        "a Kilix pane number", whole=True)
        if "max_lines" in request:
            action["max_lines"] = _number(request["max_lines"], "max_lines", 1, MAX_LINES,
                                          "a count of lines", whole=True)
        if "max_bytes" in request:
            action["max_bytes"] = _number(request["max_bytes"], "max_bytes", 1, MAX_BYTES,
                                          "a count of bytes", whole=True)
        if "timeout_seconds" in request:
            action["timeout_seconds"] = _number(request["timeout_seconds"], "timeout_seconds",
                                                MIN_TIMEOUT, MAX_TIMEOUT, "in seconds (not milliseconds)")
        return action
    except ValueError as exc:
        raise Refused(f"refused the whole request: {exc}. e.g. " + hint, hint) from None


def timeout_arg(value: float) -> str:
    """The decimal-seconds spelling `kilix pty --timeout` accepts."""
    return f"{value:.3f}".rstrip("0").rstrip(".")


def dumps(request) -> str:
    return json.dumps(request, ensure_ascii=True, sort_keys=True)
