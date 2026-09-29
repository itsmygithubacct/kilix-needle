"""How agents word system questions, rewritten to the grammar's own sentences.

In the first system benchmark (gpt-6-luna, 2026-09-29) agents almost never used
the grammar's sentences: of 147 system calls, 22 collected. They wrap one query
in framing ("on this machine", "right now", "using a read-only system query",
"exact", a trailing "Return the name and version.") and use a few shapes per
family: "is the jq package installed, and which version is it?", "which Debian
package provides the file /usr/bin/python3?", "is the cron service running?",
"which process is using the most memory right now?", "how much free space is
left on the root filesystem?", "find errors logged by program X in the last ten
minutes".

`canonical` strips the framing and rewrites one of those shapes to the sentence
the grammar reads; the grammar then parses it as always, so what is collected
is still exactly what the grammar reads. A shape not listed here is left to
the grammar and the planning-only normalizer. `sentence` goes the other way:
the grammar sentence for a query, offered as the accepted form when only the
normalizer understood a request.
"""
from __future__ import annotations

import re

from system_job import PACKAGE, PATH, UNIT, COMMAND

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty-five": 45,
          "sixty": 60}
_NUM = r"(?:[1-9][0-9]{0,2}|" + "|".join(sorted(_WORDS, key=len, reverse=True)) + ")"

# Framing around a query, never part of it.
_FRAMING = [
    r"\s*\((?:[^()]*)\)",                                    # "(/)", "(cron or crond)"
    r",?\s+using\s+(?:a\s+)?read-only\s+(?:system\s+)?(?:query|queries|tools?)",
    r"\s+on\s+this\s+(?:machine|system|computer|laptop|host)",
    r"\s+(?:right\s+now|currently|at\s+the\s+moment)",
    r"(?<=\s)(?:the\s+)?exact\s+(?=(?:package|service|systemd|unit|named|program)\b)",
    r"(?<=\s)exact\s+",
    r"[.;?!]\s*check\s+only\b.*$",
    r",?\s+including\s+(?!(?:its|the)\s+(?:installed\s+)?version\b).*$",
    r";\s*identify\b.*$",
]
# Trailing clauses that say how to answer, not what to read.
_ANSWER_TAIL = re.compile(
    r"(?:[.;:!?,]|,?\s+and)\s*(?:then\s+)?(?:return|report|answer|include|give|tell\s+me|list|"
    r"identify|state|print|reply|say|confirm|note)\b(?!\s+(?:its|the)\s+(?:installed\s+)?version\b).*$", re.I)


def _strip(text: str) -> str:
    text = " ".join(text.split())
    text = re.sub(r"^(?:please\s+|can\s+you\s+|could\s+you\s+|now\s+)+", "", text, flags=re.I)
    for pattern in _FRAMING:
        text = re.sub(pattern, " ", text, flags=re.I)
    text = " ".join(text.split())
    text = _ANSWER_TAIL.sub("", text).strip()
    return text.rstrip(" .?!").strip()


def _n(word: str) -> int:
    return _WORDS[word.lower()] if word.lower() in _WORDS else int(word)


_P = PACKAGE
_VERSION_TAIL = (r"(?:\s*,?\s*(?:and\s+)?(?:(?:what|which)\s+version(?:\s+(?:is\s+it|it\s+is|is\s+installed))?"
                 r"|(?:report|find|show|get|including|with)\s+(?:its\s+)?(?:installed\s+)?version"
                 r"|its\s+(?:installed\s+)?version))?")
_FAMILIES = [
    # packages: status (the answer carries the installed version)
    (rf"(?:is|check\s+(?:whether|if)|determine\s+(?:whether|if)|see\s+(?:whether|if))\s+(?:the\s+)?"
     rf"(?:package\s+)?(?P<t>{_P})(?:\s+package)?\s+(?:is\s+)?installed{_VERSION_TAIL}",
     lambda m: f"is {m['t']} installed"),
    (rf"(?:what|which)\s+version\s+of\s+(?:the\s+)?(?:package\s+)?(?P<t>{_P})(?:\s+package)?\s+is\s+installed",
     lambda m: f"is {m['t']} installed"),
    # "status of X" alone may be a service: the word "package" must say which.
    (rf"(?:(?:check|get|show|read|what\s+is)\s+)?(?:the\s+)?(?:installed\s+)?package\s+status"
     rf"(?:\s+and\s+version)?\s+(?:of|for)\s+(?:the\s+)?(?:package\s+)?(?P<t>{_P})(?:\s+package)?",
     lambda m: f"is {m['t']} installed"),
    (rf"(?:(?:check|get|show|read|what\s+is)\s+)?(?:the\s+)?(?:installed\s+)?status"
     rf"(?:\s+and\s+version)?\s+(?:of|for)\s+(?:the\s+)?(?:package\s+(?P<t>{_P})|(?P<t2>{_P})\s+package)",
     lambda m: f"is {m['t'] or m['t2']} installed"),
    (rf"(?:check\s+)?package\s+(?:status\s+)?(?P<t>{_P})(?:\s+status)?",
     lambda m: f"is {m['t']} installed"),
    (rf"(?:show|read|get|check)\s+(?:the\s+)?installed\s+(?:package\s+)?(?:details|information|info|"
     rf"inventory)\s+(?:for|of)\s+(?:the\s+)?(?:package\s+)?(?P<t>{_P}){_VERSION_TAIL}",
     lambda m: f"is {m['t']} installed"),
    (rf"check\s+(?:the\s+)?installed\s+(?:version\s+of\s+(?:the\s+)?(?:package\s+)?|package\s+)(?P<t>{_P})"
     rf"(?:\s+package)?",
     lambda m: f"is {m['t']} installed"),
    # a command, not a package: "is the jq command installed" is answered by the package
    # that provides it (for its version, "is PKG installed" names the package)
    (rf"(?:is|check\s+(?:whether|if)|determine\s+(?:whether|if))\s+(?:the\s+)?(?P<c>{COMMAND})\s+command\s+"
     rf"(?:is\s+)?(?:installed|available){_VERSION_TAIL}",
     lambda m: f"which package owns the {m['c']} command"),
    (rf"check\s+(?:the\s+)?installed\s+version\s+of\s+(?:the\s+)?(?P<c>{COMMAND})\s+command",
     lambda m: f"which package owns the {m['c']} command"),
    # packages: which package owns a file or command
    (rf"(?:find\s+)?(?:what|which)\s+(?:debian\s+|installed\s+)?package\s+(?:provides|owns|contains|installed)\s+"
     rf"(?:the\s+(?:file\s+|command\s+)?(?P<p>{PATH})|(?:the\s+)?(?P<p2>{COMMAND})\s+command"
     rf"|(?P<p3>{PATH}))",
     lambda m: f"which package owns {m['p'] or m['p2'] or m['p3']}"),
    (rf"(?:show\s+)?(?:the\s+)?(?:installed\s+)?package\s+(?:owner(?:\s+lookup)?|owning|that\s+owns)\s+"
     rf"(?:(?:for|of)\s+)?(?:the\s+)?(?:file\s+)?(?P<p>{PATH})",
     lambda m: f"which package owns {m['p']}"),
    (rf"(?:packages?\s+)?(?:the\s+)?owner\s+(?:of\s+)?(?:the\s+)?(?:file\s+)?(?P<p>{PATH})",
     lambda m: f"which package owns {m['p']}"),
    # services: one named service's status
    (rf"is\s+(?:the\s+)?(?:(?:systemd|system)\s+)?(?:service\s+)?(?P<u>{UNIT})(?:\s+service)?\s+"
     rf"(?:running|active|up|enabled|started)",
     lambda m: f"show {_unit(m['u'])} service status"),
    (rf"(?:(?:check|read|show|get|query)\s+)?(?:the\s+)?status\s+(?:of|for)\s+(?:the\s+)?"
     rf"(?:(?:systemd|system)\s+)?(?:service|unit)?\s*(?:named\s+)?(?P<u>{UNIT})(?:\s+(?:service|unit))?",
     lambda m: f"show {_unit(m['u'])} service status"),
    (rf"(?:check|determine|see)\s+(?:whether|if)\s+(?:the\s+)?(?:(?:systemd|system)\s+)?(?:service\s+)?"
     rf"(?:named\s+)?(?P<u>{UNIT})(?:\s+service)?\s+is\s+(?:active|running)(?:\s+or\s+(?:active|running))?",
     lambda m: f"show {_unit(m['u'])} service status"),
    (rf"(?:show|list)\s+(?:the\s+)?(?:system\s+)?services?\s+(?P<u>{UNIT}\.service)",
     lambda m: f"show {_unit(m['u'])} service status"),
    # processes: ranked by memory or CPU
    (r"(?:which|what|identify\s+the|find\s+the)\s+process(?:es)?\s+(?:is\s+|are\s+)?(?:using|consuming)\s+"
     r"the\s+most\s+(?P<m>memory|ram|cpu)",
     lambda m: f"show processes by {_metric(m['m'])}"),
    (r"(?:show|list|get)\s+(?:the\s+)?(?:current\s+|running\s+)?processes\s+(?:sorted|ranked|ordered)\s+by\s+"
     r"(?P<m>memory|ram|cpu)(?:\s+usage)?(?:\s*,?\s*(?:highest|most)\s+first)?",
     lambda m: f"show processes by {_metric(m['m'])}"),
    (r"(?:top\s+)?processes\s+(?:by\s+)?(?P<m>memory|ram|cpu)\s+usage",
     lambda m: f"show processes by {_metric(m['m'])}"),
    # disk: capacity of the root filesystem or a path
    (r"how\s+much\s+(?:free\s+)?(?:disk\s+)?space\s+is\s+(?:available|left|free)\s+on\s+(?:the\s+)?root"
     r"(?:\s+(?:filesystem|file\s+system|partition|disk|mount))?",
     lambda m: "show disk space on /"),
    (rf"how\s+much\s+(?:free\s+)?(?:disk\s+)?space\s+is\s+(?:available|left|free)\s+on\s+(?P<p>{PATH})",
     lambda m: f"show disk space on {m['p']}"),
    (rf"(?:(?:show|get|read|query|check|list)\s+)?(?:the\s+)?(?P<d>(?:(?:disk|filesystem|free|available|used|"
     rf"capacity|space|usage|resources|and)(?:\s+|/))*(?:disk|space|usage|capacity|resources))"
     rf"\s+(?:for|on|of|at)\s+(?:the\s+)?"
     rf"(?:(?:root\s+)?(?:filesystem|mount|file\s+system)(?:\s+(?:path|point))?\s+(?:mounted\s+at\s+)?)?(?P<p>{PATH})",
     lambda m: f"show disk space on {m['p']}" if re.search(r"disk|space|capacity|filesystem", m['d'], re.I)
     else None),
    (r"(?:show|get|read|check)\s+(?:the\s+)?disk\s+(?:space|usage|resources|capacity)",
     lambda m: "show disk space on /"),
]
_UNIT_SUFFIX = re.compile(r"\.service$", re.I)


def _unit(unit: str) -> str:
    return _UNIT_SUFFIX.sub("", unit)


def _metric(metric: str) -> str:
    return "memory" if metric.lower() in ("memory", "ram") else "cpu"


# journal: errors or warnings, optionally from one program tag or service, in a time window.
_JOURNAL_HEAD = re.compile(
    r"(?:(?:find|show|list|search|query|get|read|check)\s+(?:for\s+)?)?(?:the\s+)?"
    r"(?:(?:most\s+)?recent\s+|latest\s+|last\s+)?(?:system\s+)?(?:(?:journal|log|journalctl|syslog)\s+)?"
    r"(?P<kind>error\s+(?:log\s+)?(?:entries|messages)|(?:journal\s+)?errors?|(?:journal\s+)?warnings?|"
    r"(?:log\s+)?entries|logs?|journal)"
    r"(?:\s+(?:logged|entries|messages))?", re.I)
_JOURNAL_PARTS = [
    ("ident", re.compile(rf"\s+(?:(?:logged\s+)?(?:by|from|for)\s+(?:the\s+)?)?(?:program|process|tag|"
                         rf"identifier|application|app)(?:\s+tagged)?\s+(?P<v>{UNIT})", re.I)),
    ("ident", re.compile(rf"\s+(?:(?:by|from)\s+)?(?:the\s+)?program\s+tagged\s+(?P<v>{UNIT})", re.I)),
    ("ident", re.compile(rf",?\s+filter(?:ed)?\s+by\s+(?:program\s+)?tag\s+(?P<v>{UNIT})", re.I)),
    ("unit", re.compile(rf"\s+(?:from|for|of)\s+(?:the\s+)?(?:service|unit)\s+(?P<v>{UNIT})", re.I)),
    ("unit", re.compile(rf"\s+(?:from|for|of)\s+(?:the\s+)?(?P<v>{UNIT})\s+service", re.I)),
    ("time", re.compile(rf",?\s+(?:(?:in|within|from|during|over|for)\s+the\s+(?:last|past)|since)\s+"
                        rf"(?P<v>{_NUM})\s+(?P<u>minutes?|hours?|days?)(?:\s+ago)?", re.I)),
    ("time", re.compile(r"\s+(?:(?:in|within|from|during|over)\s+the\s+(?:last|past)\s+)(?P<u>minute|hour|day)",
                        re.I)),
    ("stamp", re.compile(r",?\s+(?:since|after|from)\s+(?P<v>[0-9]{4}-[0-9]{2}-[0-9]{2}"
                         r"(?:[ T][0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?(?:\s*(?:UTC|Z))?)", re.I)),
    ("where", re.compile(r"\s+(?:in|from)\s+the\s+(?:system\s+)?(?:journal|logs?)", re.I)),
    # a bare "for X": in a log request a name without "service" is a program tag
    ("ident", re.compile(rf"\s+for\s+(?P<v>{UNIT})(?=\s|,|$)", re.I)),
    ("sev", re.compile(r"\s+with\s+(?:severity|priority)\s+(?P<v>error|err|warning)", re.I)),
]


def _journal(text: str) -> str | None:
    """"find errors logged by program X in the last ten minutes" -> the grammar's sentence."""
    svc = re.fullmatch(rf"(?:read|show|get|list)\s+(?:the\s+)?journal\s+(?:logs?|entries)\s+(?:for|from)\s+(?:the\s+)?"
                       rf"(?:service|unit)\s+(?P<u>{UNIT}),?\s+(?P<k>errors|warnings)(?P<rest>.*)", text, re.I)
    if svc:
        text = f"{svc['k']} from the service {svc['u']}{svc['rest']}"
    lead = re.match(r"(?:search|query|check)\s+(?:the\s+)?(?:system\s+)?(?:journal|logs?)\s+for\s+", text, re.I)
    if lead:
        text = text[lead.end():]
    head = _JOURNAL_HEAD.match(text)
    if not head:
        return None
    kind = head["kind"].lower().replace("journal ", "")
    found: dict = {}
    rest = text[head.end():]
    while rest:
        for name, pattern in _JOURNAL_PARTS:
            m = pattern.match(rest)
            if m and name not in found:
                found[name] = m
                rest = rest[m.end():]
                break
        else:
            return None
    sev = found.get("sev")
    if kind.startswith("warning") or (sev and sev["v"].lower() == "warning"):
        words = "warnings"
    elif kind.startswith("error") or sev:
        words = "errors"
    else:
        words = "journal"
    out = words
    if "ident" in found:
        out += f" from the program {found['ident']['v']}"
    if "unit" in found:
        if "ident" in found:
            return None
        out += f" from the {_unit(found['unit']['v'])} service"
    if "stamp" in found:
        if "time" in found:
            return None
        v = re.sub(r"(?<=[0-9])T(?=[0-9])", " ", found["stamp"]["v"])
        v = re.sub(r"\s*(?:utc|z)$", " UTC", v, flags=re.I)
        out += f" since {v}"
    if "time" in found:
        t = found["time"]
        count = _n(t["v"]) if t.groupdict().get("v") else 1
        unit = t["u"].lower().rstrip("s")
        out += f" since {count} {unit}{'s' if count != 1 else ''} ago"
    return out


def canonical(request: str) -> str | None:
    """The grammar's sentence for an agent's wording of one query, or None."""
    if not isinstance(request, str):
        return None
    text = _strip(request)
    if not text:
        return None
    import system_job
    if system_job.ambiguous_owner(text) or system_job.ambiguous_owner(text + "?"):
        return None             # "which package owns the file?": which file is unsaid
    for pattern, build in _FAMILIES:
        m = re.fullmatch(pattern, text, re.I)
        if m and (built := build(m)) is not None:
            return built
    return _journal(text)


# ---- the other way: the grammar sentence for a query ----------------------------

def _one(kind: str, args: dict) -> str | None:
    if kind == "resources":
        k = args.get("kind")
        if k == "memory":
            return "show memory"
        if k == "cpu":
            return "show cpu load"
        if k == "all":
            return "show system resources"
        if k == "disk":
            return f"show disk space on {args.get('path', '/')}"
    if kind == "processes":
        return f"show top {args.get('limit', 10)} processes by {args.get('sort')}"
    if kind == "services":
        scope = "user " if args.get("scope") == "user" else ""
        if "unit" in args:
            return f"show {scope}{_unit(args['unit'])} service status"
        return f"show {scope}{args.get('state', 'all')} services"
    if kind == "packages":
        if args.get("operation") == "status":
            return f"is {args.get('target')} installed"
        return f"which package owns {args.get('target')}"
    if kind == "journal":
        words = {"err": "errors", "warning": "warnings"}.get(args.get("priority"), "journal")
        out = ("user " if args.get("scope") == "user" else "") + words
        if "unit" in args:
            out += f" from the {_unit(args['unit'])} service"
        if "identifier" in args:
            out += f" from the program {args['identifier']}"
        boot = args.get("boot", "current")
        if "since" not in args and boot == "previous":
            out += " from the previous boot"
        elif "since" not in args and boot == "any":
            out += " from all boots"
        if "since" in args:
            out += f" since {args['since']}"
        if args.get("limit", 50) != 50:
            out += f" last {args['limit']}"
        return out
    return None


def sentence(actions: list) -> str | None:
    """The grammar sentence that reads exactly these queries, or None.

    Checked by parsing it back: a sentence is offered only when the grammar
    reads it as these same queries."""
    import system_job
    parts = []
    for kind, args in actions:
        part = _one(kind, args)
        if part is None:
            return None
        parts.append(part)
    text = " and ".join(parts)
    back = system_job.parse(text)
    if back is None or [[a.kind, a.args] for a in back] != [[k, system_job.normalize(k, a)] for k, a in actions]:
        return None
    return text


FORMS = ("show memory | show cpu load | show disk space on / | show top 5 processes by memory "
         "| show ssh service status | show failed services | errors since 1 hour ago "
         "| errors from the ssh service since 1 hour ago | errors from the program TAG since 10 minutes ago "
         "| is jq installed | which package owns /usr/bin/python3")
