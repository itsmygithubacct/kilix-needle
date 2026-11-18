"""Plain-language requests for kilix_logs_read, read by a grammar.

kilix_logs_read takes structured arguments (operation, session or file,
provider, query, event_id, limit). Agents called it 44 times in a week of
their sessions (2026-11-11 to 2026-11-18), always with those arguments. This
reads the same calls said in words ("show the brief of pane 12", "search
pane 12's log for "timeout"", "show the source of event e41"), for the
grammar arm of experiment 11's bench; the bundle proposes the same calls.
Either way a call is admitted only by the tool's own argument checks
(mcp_server._logs_arguments), unchanged.

The forms were fixed before any request of the bench's suite was written.
"""
from __future__ import annotations

import re

MAX_REQUEST = 1024
PROVIDERS = ("claude", "codex", "grok", "omp", "qwen")

_HEAD = re.compile(r"^(?:(?:please|pls|kindly|ok|okay|just|can you|could you|would you)[\s,]+)+", re.I)
_TAIL = re.compile(r"(?:[\s,]+(?:please|pls|thanks|thank you|now))*[\s.!?]*$", re.I)
_QUOTED = r"""(?:"(?P<{g}d>[^"\n]{{1,200}})"|'(?P<{g}s>[^'\n]{{1,200}})')"""
_PANE = r"pane\s+(?:id\s+)?(?P<{g}>[0-9]{{1,9}})"
_SID = r"(?P<sid>[0-9a-f][0-9a-f-]{7,63})"
_TITLE = _QUOTED.format(g="t")
# A session: by its pane, by its id, or by its quoted title.
_SESSION = (rf"(?:(?:the\s+)?(?:(?:{'|'.join(PROVIDERS)})\s+)?session\s+(?:in|of|behind)\s+{_PANE.format(g='pa')}"
            rf"|{_PANE.format(g='pb')}(?:'s\s+session)?"
            rf"|(?:the\s+)?session\s+(?:id\s+)?{_SID}"
            rf"|(?:the\s+)?session\s+(?:titled|named|called)\s+{_TITLE})")
_QUERY = rf"(?:{_QUOTED.format(g='q')}|(?P<qw>[\w.:/-]{{1,64}}))"
_LOG = r"(?:log|logs|events|transcript|session\s+log|history)"
_PATH = r"(?P<path>/[\w./+@-]{1,4095})"
_EVENT = r"(?P<event>[A-Za-z0-9][A-Za-z0-9:._-]{0,127})"

_FORMS = (
    ("brief", re.compile(rf"(?:(?:give|show|get)(?:\s+me)?\s+)?(?:a\s+|the\s+)?brief\s+(?:of|for|on)\s+{_SESSION}", re.I)),
    ("brief", re.compile(rf"(?:summari[sz]e|recap)\s+{_SESSION}", re.I)),
    ("events", re.compile(rf"(?:show|read|list|get)(?:\s+me)?\s+(?:the\s+)?(?:last\s+(?P<limit>[0-9]{{1,3}})\s+)?"
                          rf"{_LOG}\s+(?:of|for|from|in)\s+{_SESSION}", re.I)),
    ("events", re.compile(rf"(?:show|read|get)(?:\s+me)?\s+{_SESSION}'s\s+{_LOG}", re.I)),
    ("search", re.compile(rf"search\s+(?:the\s+)?{_LOG}\s+(?:of|for|in|from)\s+{_SESSION}\s+for\s+{_QUERY}", re.I)),
    ("search", re.compile(rf"search\s+{_SESSION}(?:'s\s+{_LOG})?\s+for\s+{_QUERY}", re.I)),
    ("search", re.compile(rf"find\s+{_QUERY}\s+in\s+(?:the\s+{_LOG}\s+of\s+)?{_SESSION}", re.I)),
    ("source", re.compile(rf"(?:show|get|read)(?:\s+me)?\s+(?:the\s+)?source\s+(?:of|for)\s+event\s+{_EVENT}", re.I)),
    ("file", re.compile(rf"(?:read|show)(?:\s+me)?\s+(?:the\s+)?(?P<provider>raw|{'|'.join(PROVIDERS)})\s+"
                        rf"(?:log|session)\s+file\s+{_PATH}", re.I)),
    ("file", re.compile(rf"(?:read|show)(?:\s+me)?\s+(?:the\s+)?log\s+file\s+{_PATH}", re.I)),
    ("file_search", re.compile(rf"search\s+(?:the\s+)?(?:raw\s+)?log\s+file\s+{_PATH}\s+for\s+{_QUERY}", re.I)),
)


def _session(match) -> str:
    found = match.groupdict()
    pane = found.get("pa") or found.get("pb")
    if pane:
        return str(int(pane))
    if found.get("sid"):
        return found["sid"]
    return found.get("td") or found.get("ts")


def _query(match) -> str:
    found = match.groupdict()
    return found.get("qd") or found.get("qs") or found.get("qw")


def parse(request: str) -> dict | None:
    """The grammar's reading of a request as kilix_logs_read's arguments, or None."""
    if not isinstance(request, str) or not request.strip() or len(request) > MAX_REQUEST:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in request):
        return None
    text = _HEAD.sub("", request.strip())
    for candidate in dict.fromkeys((text, _TAIL.sub("", text))):
        for kind, form in _FORMS:
            match = form.fullmatch(candidate)
            if not match:
                continue
            if kind == "brief":
                args = {"operation": "brief", "session": _session(match)}
            elif kind == "events":
                args = {"operation": "events", "session": _session(match)}
                if match.groupdict().get("limit"):
                    args["limit"] = int(match["limit"])
            elif kind == "search":
                args = {"operation": "search", "session": _session(match), "query": _query(match)}
            elif kind == "source":
                args = {"operation": "source", "event_id": match["event"]}
            elif kind == "file":
                provider = (match.groupdict().get("provider") or "raw").lower()
                args = {"operation": "events", "file": match["path"], "provider": provider}
            else:
                args = {"operation": "search", "file": match["path"], "provider": "raw", "query": _query(match)}
            return args if check(args) is None else None
    return None


def check(args) -> str | None:
    """Why these are not arguments kilix_logs_read takes, or None: the tool's
    own checks, unchanged."""
    import mcp_server
    try:
        mcp_server._logs_arguments(args)
    except ValueError as error:
        return str(error)
    return None
