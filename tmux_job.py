"""Whole-request grammar for the kilix.tmux.request/v1 contract; no inference."""
from __future__ import annotations

import re
import unicodedata


MAX_TEXT = 65536
KEYS = ("Enter", "Tab", "Escape", "BSpace", "Space", "Up", "Down", "Left", "Right",
        "Home", "End", "PageUp", "PageDown", "Delete", "C-c", "C-d", "C-u", "C-a", "C-e", "C-l")
RISKY = frozenset({"send", "type", "key", "close"})
OPERATIONS = ("list", "new", "read", "send", "type", "key", "rename", "close")
USAGE = ('tmux requests: list sessions | new session NAME [in "/absolute/directory"] | '
         'read TARGET [last N lines] | send "TEXT" to TARGET | type "TEXT" in TARGET | '
         'press Enter in TARGET | rename session TARGET to NAME | close session TARGET; '
         'provide --socket /absolute/private/socket; send adds no Enter, type submits. '
         'Quotes are literal delimiters, not shell escapes: use single quotes around TEXT '
         'when it contains double quotes (and vice versa), or use --request-json '
         'with operation, target and text for exact literal input.')

_NAME = r"[A-Za-z0-9_][A-Za-z0-9_-]{0,127}"
_SESSION = rf"(?:{_NAME}|\$[0-9]+)"
_TARGET = rf"(?:{_NAME}:[0-9]+\.[0-9]+|{_SESSION}|%[0-9]+)"
_LITERAL = r'''(?:"[^"\n]*"|'[^'\n]*'|`[^`\n]*`)'''
_PATH = rf"(?:{_LITERAL}|/[\w./+@-]+)"
_TARGET_WORDS = r"(?:(?:the\s+)?(?:tmux\s+)?(?:session|pane|target)\s+)?"
_UNIT = r"(?:tmux\s+)?session"
_HEAD = re.compile(r"^(?:(?:please|can you|could you|would you|kindly)\s+)+", re.I)
_TAIL = re.compile(r"(?:,?\s+(?:please|thanks|thank you))?[.!]*$", re.I)
_FORMS = (
    ("list", re.compile(r"(?:list|show)\s+(?:(?:all|the)\s+)?(?:tmux\s+)?sessions", re.I)),
    ("new", re.compile(rf"(?:new|(?:create|open|start)(?:\s+a)?(?:\s+new)?)\s+{_UNIT}"
                       rf"\s+(?:(?:named|called)\s+)?(?P<name>{_NAME})"
                       rf"(?:\s+in\s+(?:directory\s+)?(?P<cwd>{_PATH}))?", re.I)),
    ("read", re.compile(rf"(?:read|capture|show)\s+{_TARGET_WORDS}(?P<target>{_TARGET})"
                        r"(?:\s+(?:last\s+)?(?P<lines>[0-9]+)\s+lines)?", re.I)),
    ("read", re.compile(rf"(?:read|capture|show)\s+(?:the\s+)?(?:last\s+)?(?P<lines>[0-9]+)\s+lines"
                        rf"\s+(?:from|of|in)\s+{_TARGET_WORDS}(?P<target>{_TARGET})", re.I)),
    ("send", re.compile(rf"send\s+(?P<text>{_LITERAL})\s+(?:to|into)\s+{_TARGET_WORDS}"
                        rf"(?P<target>{_TARGET})(?:\s+without\s+(?:pressing\s+)?Enter)?", re.I)),
    ("type", re.compile(rf"type(?:\s+and\s+press\s+Enter)?\s+(?P<text>{_LITERAL})\s+"
                        rf"(?:in|into|to)\s+{_TARGET_WORDS}(?P<target>{_TARGET})"
                        r"(?:\s+and\s+press\s+Enter)?", re.I)),
    ("key", re.compile(rf"(?:press|send\s+keys?)\s+(?P<keys>[\w-]+(?:\s+[\w-]+)*)\s+"
                       rf"(?:in|to|into)\s+{_TARGET_WORDS}(?P<target>{_TARGET})", re.I)),
    ("rename", re.compile(rf"rename\s+(?:the\s+)?{_UNIT}\s+(?P<target>{_SESSION})\s+to\s+"
                          rf"(?P<new_name>{_NAME})", re.I)),
    ("close", re.compile(rf"(?:close|kill)\s+(?:the\s+)?{_UNIT}\s+(?P<target>{_SESSION})", re.I)),
)


def literal_request(request: dict) -> dict:
    """Structured literal input bypasses natural-language quotation delimiters."""
    if (not isinstance(request, dict) or set(request) != {"operation", "target", "text"}
            or request.get("operation") not in ("send", "type")
            or not isinstance(request.get("target"), str)
            or len(request["target"]) > 160
            or not re.fullmatch(_TARGET, request["target"])
            or not isinstance(request.get("text"), str)
            or not 1 <= len(request["text"]) <= MAX_TEXT or controls(request["text"])):
        raise ValueError("literal request needs operation send|type, exact target, and nonempty text without controls except tab")
    return dict(request)


def controls(text: str) -> bool:
    return any(unicodedata.category(c) in ("Cc", "Cs", "Zl", "Zp") and c != "\t" for c in text)


def parse(request: str) -> dict:
    """Return one operation and its literal arguments, or refuse the whole request.

    Quotes delimit text; their contents are not shell-expanded, normalized or
    unescaped. Use another quote style when a literal contains the delimiter.
    The socket is a separate, mandatory CLI/MCP field.
    """
    if not isinstance(request, str) or not request.strip() or len(request) > MAX_TEXT + 1024:
        raise ValueError("provide one bounded tmux request. " + USAGE)
    if controls(request):
        raise ValueError("control characters other than tab are not supported")
    original = _HEAD.sub("", request.strip())
    found = None
    # Prefer the complete literal reading: a session named "thanks" and a
    # bare path ending in a dot must not lose their last characters as filler.
    for text in dict.fromkeys((original, _TAIL.sub("", original))):
        for operation, form in _FORMS:
            match = form.fullmatch(text)
            if match:
                found = operation, match
                break
        if found:
            break
    if not found:
        raise ValueError("unsupported or compound tmux request. " + USAGE)
    operation, match = found
    args = {k: v for k, v in match.groupdict().items() if v is not None}
    for key in ("name", "new_name"):
        if key in args and not re.fullmatch(_NAME, args[key]):
            raise ValueError("session names must use ASCII letters, digits, underscore or hyphen")
    if "target" in args and not re.fullmatch(_SESSION if operation in ("rename", "close") else _TARGET,
                                             args["target"]):
        raise ValueError("use an exact supported session name, stable ID or numeric pane target")
    if "text" in args:
        args["text"] = args["text"][1:-1]
        if not args["text"] or len(args["text"]) > MAX_TEXT:
            raise ValueError("quote nonempty text of at most 65536 characters")
    if "cwd" in args:
        path = args["cwd"]
        args["cwd"] = path[1:-1] if path[:1] in ("'", '"', "`") else path
        if not args["cwd"].startswith("/") or len(args["cwd"]) > 4096:
            raise ValueError("new-session directory must be an absolute bounded path")
    if "lines" in args:
        if len(args["lines"]) > 4 or not 1 <= int(args["lines"]) <= 2000:
            raise ValueError("read lines must be from 1 to 2000")
        args["lines"] = int(args["lines"])
    if "keys" in args:
        names = {key.casefold(): key for key in KEYS}
        keys = args["keys"].split()
        if not 1 <= len(keys) <= 16 or any(k.casefold() not in names for k in keys):
            raise ValueError("use 1–16 allowed named keys: " + ", ".join(KEYS))
        args["keys"] = [names[k.casefold()] for k in keys]
    if "target" in args and len(args["target"]) > 160:
        raise ValueError("target is too long")
    return {"operation": operation, **args}


def socket_path(value: str) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or len(value) > 4096
            or any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in value)):
        raise ValueError("provide an explicit absolute private socket path")
    return value
