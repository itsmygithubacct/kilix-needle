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
    r"^\s*read[- ]only\s*[:,-]\s*|[,;]?\s*\(\s*read[- ]only\s*\)|,?\s+read[- ]only\s*[.!]?$",
    # A parenthesis that carries a value ("(last 15 min)", "(priority err)") is read
    # as words; any other one ("(/)", "(cron or crond)") is dropped.
    (r"\s*\(((?:[^()]*?\b(?:[0-9]+|err|error|errors|warning|warnings|priority|level|min|mins|minutes?|"
     r"hours?|days?|boot)\b[^()]*))\)", r" \1 "),
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
    r"identify|state|print|reply|say|confirm|note|summari[sz]e|describe|quote|show\s+me)\b(?!\s+(?:its|the)\s+(?:installed\s+)?version\b).*$", re.I)


def _strip(text: str) -> str:
    text = " ".join(text.split())
    text = re.sub(r"^(?:please\s+|can\s+you\s+|could\s+you\s+|now\s+)+", "", text, flags=re.I)
    for pattern in _FRAMING:
        pattern, to = pattern if isinstance(pattern, tuple) else (pattern, " ")
        text = re.sub(pattern, to, text, flags=re.I)
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
     rf"inventory|record|entry)\s+(?:for|of)\s+(?:the\s+)?(?:package\s+)?(?P<t>{_P}){_VERSION_TAIL}",
     lambda m: f"is {m['t']} installed"),
    (rf"check\s+(?:the\s+)?installed\s+(?:version\s+of\s+(?:the\s+)?(?:package\s+)?|package\s+)(?P<t>{_P})"
     rf"(?:\s+package)?",
     lambda m: f"is {m['t']} installed"),
    (rf"(?:list|show|find)\s+(?:the\s+)?installed\s+packages?\s+(?:matching|named|called)\s+(?P<t>{_P})",
     lambda m: f"is {m['t']} installed"),
    (rf"(?:check|about)\s+(?:the\s+)?package\s+(?P<t>{_P})\s*:\s*is\s+it\s+installed{_VERSION_TAIL}",
     lambda m: f"is {m['t']} installed"),
    (rf"(?:check|about)\s+(?:the\s+)?command\s+(?P<c>{COMMAND})\s*:\s*(?:(?:report|say|tell\s+me)\s+)?"
     rf"(?:whether|if)\s+it\s+is\s+(?:installed|available){_VERSION_TAIL}",
     lambda m: f"which package owns the {m['c']} command"),
    (rf"(?:check|about)\s+(?:the\s+)?command\s+(?P<c>{COMMAND})",
     lambda m: f"which package owns the {m['c']} command"),
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
    # a count: "show the top 1 process by memory", "top 5 processes by memory usage"
    (rf"(?:(?:show|list|get|find)\s+)?(?:the\s+)?top\s+(?P<n>{_NUM})\s+process(?:es)?\s+"
     rf"(?:by|using\s+the\s+most|sorted\s+by|ranked\s+by)\s+(?P<m>memory|ram|cpu)(?:\s+usage)?",
     lambda m: f"show top {_n(m['n'])} processes by {_metric(m['m'])}" if 1 <= _n(m['n']) <= 50 else None),
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
# Agents also echo the tool's own argument names ("priority err", "scope user",
# "limit 50", "current boot"); each part is read wherever it comes, and any word
# not accounted for leaves the request to the grammar and the normalizer.
_JOURNAL_HEAD = re.compile(
    r"(?:(?:find|show|list|search|query|get|read|check)\s+(?:for\s+)?)?(?:the\s+|my\s+)?"
    r"(?:(?P<scope>user|system)\s+)?"
    r"(?:(?:most\s+)?recent\s+|latest\s+|last\s+)?(?:system\s+)?(?:(?:journal|log|journalctl|syslog)\s+)?"
    r"(?P<kind>error\s+(?:log\s+)?(?:entries|messages|logs?)\b|errors?\s+(?:entries|logs?)\b|errors?\b|warnings?\b"
    r"|(?:log\s+)?entries\b|logs?\b|journal\b)"
    r"(?:\s+(?:logged|entries|messages))?", re.I)
_V = rf"(?P<v>{UNIT})"
_JOURNAL_PARTS = [
    ("unit", re.compile(rf",?\s+(?:(?:from|for|of)\s+(?:the\s+)?)?(?:service|unit)\s+{_V}", re.I)),
    ("unit", re.compile(rf",?\s+(?:from|for|of)\s+(?:the\s+)?{_V}\s+service", re.I)),
    ("unit", re.compile(rf",?\s+(?:from|for|of)\s+(?:the\s+)?(?P<v>{UNIT}\.service)(?=[\s,.]|$)", re.I)),
    ("ident", re.compile(rf",?\s+(?:(?:logged\s+)?(?:by|from|for)\s+(?:the\s+)?)?(?:program|process|application|app)"
                         rf"(?:\s+(?:tag|tagged|identifier))?\s+{_V}", re.I)),
    ("ident", re.compile(rf",?\s+(?:(?:by|from|for|with)\s+(?:the\s+)?)?(?:tag|tagged|identifier|syslog\s+identifier)"
                         rf"\s+{_V}", re.I)),
    ("ident", re.compile(rf",?\s+(?:filter(?:ed)?\s+by\s+(?:program\s+)?tag)\s+{_V}", re.I)),
    ("time", re.compile(rf",?\s+(?:(?:in|within|from|during|over|for)\s+the\s+(?:last|past)|since)\s+"
                        rf"(?P<v>{_NUM})\s+(?P<u>minutes?|hours?|days?)(?:\s+ago)?", re.I)),
    ("time", re.compile(r",?\s+(?:(?:in|within|from|during|over)\s+the\s+(?:last|past)\s+)(?P<u>minute|hour|day)",
                        re.I)),
    ("stamp", re.compile(r",?\s+(?:since|after|from)\s+(?P<v>[0-9]{4}-[0-9]{2}-[0-9]{2}"
                         r"(?:[ T][0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?(?:\s*(?:UTC|Z))?)", re.I)),
    ("boot", re.compile(r",?\s+(?:(?:from|in|during|for|of)\s+)?(?:the\s+)?(?P<v>any|all|every|current|this|previous|last)"
                        r"\s+boots?", re.I)),
    ("prio", re.compile(r",?\s+(?:with\s+)?(?:severity|priority)\s+(?P<v>errors?|err|warnings?|all)", re.I)),
    ("limit", re.compile(r",?\s+(?:limit|last)\s+(?P<v>[0-9]{1,3})(?:\s+entries)?", re.I)),
    ("scope", re.compile(r",?\s+scope\s+(?P<v>user|system)", re.I)),
    ("where", re.compile(r"\s+(?:in|from)\s+the\s+(?:system\s+)?(?:journal|logs?)", re.I)),
    # a bare "for X": in a log request a name without "service" is a program tag
    ("ident", re.compile(rf"\s+for\s+{_V}(?=[\s,.]|$)", re.I)),
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
    if re.search(r"\b(?:newest|latest|last|most\s+recent)\s+(?:(?:journal|log)\s+)?(?:error|warning|entry|message|line)\b(?!s)",
                 text, re.I):
        return None             # one newest entry: the part reader keeps its count
    head = _JOURNAL_HEAD.match(text)
    if not head:
        return None
    kind = head["kind"].lower()
    found: dict = {}
    rest = text[head.end():]
    while rest.strip(" ,"):
        for name, pattern in _JOURNAL_PARTS:
            m = pattern.match(rest)
            if m and name not in found:
                found[name] = m
                rest = rest[m.end():]
                break
        else:
            return None
    prio = found.get("prio")
    level = prio["v"].lower() if prio else None
    if level in ("warning", "warnings") or (not level and kind.startswith("warning")):
        words = "warnings"
    elif level in ("err", "error", "errors") or (not level and kind.startswith("error")):
        words = "errors"
    else:
        words = "journal"
    scope = (found["scope"]["v"] if "scope" in found else head["scope"] or "").lower()
    out = ("user " if scope == "user" else "") + words
    if "ident" in found and "unit" in found:
        return None
    if "ident" in found:
        out += f" from the program {found['ident']['v']}"
    if "unit" in found:
        out += f" from the {_unit(found['unit']['v'])} service"
    if "boot" in found:
        b = found["boot"]["v"].lower()
        out += {"any": " from all boots", "all": " from all boots", "every": " from all boots",
                "current": " from this boot", "this": " from this boot",
                "previous": " from the previous boot", "last": " from the previous boot"}[b]
    if "stamp" in found and "time" in found:
        return None
    if "stamp" in found:
        v = re.sub(r"(?<=[0-9])T(?=[0-9])", " ", found["stamp"]["v"])
        v = re.sub(r"\s*(?:utc|z)$", " UTC", v, flags=re.I)
        out += f" since {v}"
    if "time" in found:
        t = found["time"]
        count = _n(t["v"]) if t.groupdict().get("v") else 1
        unit = t["u"].lower().rstrip("s")
        out += f" since {count} {unit}{'s' if count != 1 else ''} ago"
    if "limit" in found:
        out += f" last {int(found['limit']['v'])}"
    return out


# ---- slot readers: the words a question needs, every other word accounted for ----

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._:@/-]*|'s\b|[?!.,;:\"']")
_REFUSE = re.compile(r"(?<![\w-])(?:not|never|no|don'?t|doesn'?t|isn'?t|without|if\s+not|unless|until|after|"
                     r"tomorrow|later|install|remove|uninstall|purge|upgrade|downgrade|delete|kill|"
                     r"restart|stop|start|enable|disable|reinstall|apt(?:-get)?\s+(?:install|remove|purge))(?![\w-])", re.I)
_PKG_FILLER = frozenset("""
what which whats what's is are the a an of and at if so then tell me whether its it has have i do does
been being installed version versions present available exist exists status get check find out
show report query currently current system machine this on here debian package packages dpkg dpkg-query
-w -l -s policy apt please right now which's installation state info information details record entry
there there's any exact version? yes to see whether confirm determine read look up lookup see level
w l s for from via that that's database db manager in give return string number
""".split())
_PKG_CUES = frozenset("installed installation version versions present available exist exists status policy "
                      "dpkg dpkg-query info information details".split())
_PKG_GENERIC = frozenset("""it that this them one something anything package packages all kernel os system
software linux debian python everything service services unit units process processes memory disk cpu log logs
journal help version status""".split())


def _package(text: str) -> str | None:
    """"do I have jq installed?", "installed version of jq", "dpkg status of jq" -> "is jq installed"."""
    if _REFUSE.search(text) or re.search(r"[*?\[\]]\S|\S[*\[\]]|(?<!\S)--(?!no-pager)\w", text):
        return None             # a pattern or a flag is never one package's name
    words = [w.rstrip(":") for w in _WORD.findall(text.lower()) if w not in "?!.,;:\"'" and w != "'s"]
    if not any(w in _PKG_CUES for w in words):
        return None
    left = [w for w in words if w not in _PKG_FILLER]
    if len(left) != 1 or left[0] in _PKG_GENERIC or not re.fullmatch(PACKAGE, left[0]):
        return None
    return f"is {left[0]} installed"


_J_FILLER = frozenset("""
show list get find search look for check what which did does has have had any the a an of in on from by with
within over during journal journals system systemd journalctl syslog log logs logged logging entries entry
messages message lines level recent recently latest newest most last past previous me my all please there is
are were been since ago and to that this machine right now here up display print read give fetch return only
current boot query dump tail see new occurred happened written wrote emitted produced reported output
program process app application tag tagged identifier syslog_identifier named called its it's
pull grab bring collect retrieve report records record under whose i need want wrote write writes
""".split())
_J_NOUNS = frozenset("""services service processes process packages package memory disk cpu users user
failures boots boot today yesterday who lines
few several some couple handful many more most all any other others those these their our your my
new old bunch lot lots dozen whole entire every each both same such own""".split())
_J_UNITS = {"m": "minute", "min": "minute", "mins": "minute", "minute": "minute", "minutes": "minute",
            "h": "hour", "hr": "hour", "hrs": "hour", "hour": "hour", "hours": "hour",
            "d": "day", "day": "day", "days": "day"}


def _journal_slots(text: str) -> str | None:
    """A journal question read by its parts: level, program tag or unit, time window."""
    if _REFUSE.search(text) or re.search(r"\b(?:delete|vacuum|rotate|flush|clear)\b", text, re.I):
        return None
    if re.search(r"[*?\[\]]", text.replace("?", "", text.endswith("?"))) or \
            re.search(r"\blogged\s+(?:in|on|into)\b(?!\s+(?:the|during|over|within|since)\b)|\bwho\b", text, re.I):
        return None             # a pattern, or who is logged in, is not a journal read
    t = " " + text.lower().replace("\u2019", "'") + " "
    if re.search(r"\s--(?!no-pager|since|identifier|unit|priority|lines|boot|user|output)\w", t):
        return None
    # "that" as a pointer ("that boot", "from that", "at that time"), not as a relative pronoun
    if re.search(r"\b(?:then|earlier|those|them|again|previously|aforementioned|same)\b"
                 r"|\bthat\s+(?:one|time|boot|error|errors|entry|entries|message|unit|service|program|tag|window|period)\b"
                 r"|\b(?:from|since|at|before|after|of)\s+that\b|\bthat\s*[.?!]?\s*$", t):
        return None             # "the errors from then": what "then" means is not in the request
    found: dict = {}

    def take(pattern, key, group=1):
        nonlocal t
        m = re.search(pattern, t, re.I)
        if not m:
            return None
        if key in found:
            return False
        value = m.group(group) if group is not None else m.group(0)
        if key in ("ident", "unit"):
            said = re.search(rf"(?<![\w.@:-]){re.escape(value)}(?![\w.@:-])", text, re.I)
            value = said.group(0) if said else value
        found[key] = value
        t = t[:m.start()] + " " + t[m.end():]
        return True

    # journalctl-style flags agents sometimes send
    take(r"\s(?:-t|--identifier[= ])\s*['\"]?([a-z0-9_][a-z0-9_.@:-]*)['\"]?", "ident")
    take(r"\s(?:syslog_identifier|syslog\s+identifier)\s*[=:]\s*['\"]?([a-z0-9_][a-z0-9_.@:-]*)['\"]?", "ident")
    take(r"\s_?systemd_unit\s*[=:]\s*['\"]?([a-z0-9_][a-z0-9_.@:-]*)['\"]?", "unit")
    # a level said as a phrase: "at error priority", "error-priority", "priority err", "severity: warning"
    level_phrase = (r"\s(?:(?:at|with|of)\s+)?(?:(?:the|an?)\s+)?(?:(err|error|warning|warn)[- ](?:priority|level|severity)"
                    r"|(?:priority|level|severity)\s*[=:]?\s*(err|error|3|warning|warn|4))(?:\s+or\s+(?:higher|above|worse))?\b")
    m = re.search(level_phrase, t)
    levels = set()
    while m:
        levels.add({"3": "err", "error": "err", "4": "warning", "warn": "warning"}.get(m[1] or m[2], m[1] or m[2]))
        t = t[:m.start()] + " " + t[m.end():]
        m = re.search(level_phrase, t)
    if len(levels) > 1:
        return None             # two levels: which one is meant is not clear
    if levels:
        found["prio"] = levels.pop()
    if re.search(r"\berror-level\b", t) and found.get("prio", "err") == "err":
        found["prio"] = "err"; t = re.sub(r"\berror-level\b", " ", t)
    t = re.sub(r",?\s+(?:(?:with\s+)?(?:the\s+)?(?:newest|latest|most\s+recent|oldest)\s+(?:ones?\s+)?first"
               r"|in\s+(?:reverse\s+)?(?:chronological|time)\s+order)\b", " ", t)
    take(r"\s(?:-u|--unit[= ])\s*['\"]?([a-z0-9_][a-z0-9_.@:-]*)['\"]?", "unit")
    take(r"\s(?:-p|--priority[= ])\s*['\"]?(err|error|3|warning|warn|4)['\"]?", "prio")
    take(r"\s--since[= ]\s*['\"]([^'\"]+)['\"]", "since_raw")
    take(r"\s(?:-n|--lines[= ])\s*([0-9]{1,3})", "limit")
    if re.search(r"\s(?:-b|--boot)\s+-1\b", t):
        found["boot"] = "previous"; t = re.sub(r"\s(?:-b|--boot)\s+-1\b", " ", t)
    if re.search(r"\s(?:-b|--boot)(?=\s)", t):
        found.setdefault("boot", "current"); t = re.sub(r"\s(?:-b|--boot)(?=\s)", " ", t)
    if re.search(r"\s--user(?=\s)", t):
        found["scope"] = "user"; t = re.sub(r"\s--user(?=\s)", " ", t)
    t = re.sub(r"\s--no-pager|\s-o\s+\S+|\s--output[= ]\S+", " ", t)
    t = re.sub(r":(?=\s)", " ", t)                     # "journal: errors, tag X"
    t = re.sub(r"\b(?:a\s+)?quarter(?:\s+of\s+an)?\s+hour\b", "15 minutes", t)
    t = re.sub(r"\b(?:a\s+)?half(?:\s+an)?\s+hour\b", "30 minutes", t)
    # time: "in the past 20 min", "10m ago", "the last hour", "since 10 minutes ago"
    m = re.search(rf"\s(?:(?:in|within|over|from|during|for|since)\s+)?(?:the\s+)?(?:last|past|previous)?\s*"
                  rf"(?P<n>{_NUM}|an|a|one)\s*(?P<u>minutes?|mins?|m|hours?|hrs?|h|days?|d)\b(?:\s+ago)?", t)
    if m:
        n = m["n"]
        found["time"] = (1 if n in ("an", "a") else _n(n), _J_UNITS[m["u"]])
        t = t[:m.start()] + " " + t[m.end():]
    else:
        m = re.search(r"\s(?:(?:in|within|over|from|during|for)\s+)?(?:the\s+)?(?:last|past|previous)\s+"
                      r"(minute|hour|day)\b", t)
        if m:
            found["time"] = (1, m[1]); t = t[:m.start()] + " " + t[m.end():]
    take(r"\s(?:since|after|from)\s+([0-9]{4}-[0-9]{2}-[0-9]{2}(?:[ t][0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?"
         r"(?:\s*(?:utc|z))?)", "stamp")
    take(r"\s(?:since|from|for)\s+(?:the\s+)?(today|yesterday)\b", "day")
    # boots: "during the last boot", "this boot", "all boots"
    m = re.search(r"\s(?:(?:during|in|from|for|of|since)\s+)?(?:the\s+)?(last|previous|this|current|any|all|every)"
                  r"\s+boots?\b", t)
    if m:
        found["boot"] = {"last": "previous", "previous": "previous", "this": "current",
                         "current": "current"}.get(m[1], "any")
        t = t[:m.start()] + " " + t[m.end():]
    if re.search(r"\b(?:errors?|err|error-level|failures?|critical)\b", t):
        found.setdefault("prio", "err")
    elif re.search(r"\bwarn(?:ings?)?\b", t):
        found.setdefault("prio", "warning")
    # a count of lines: "last 100 log lines", "the 10 most recent error messages"
    m = re.search(rf"\s(?:(?:the\s+)?(?:last|latest|newest|most\s+recent)\s+)?({_NUM}|[0-9]{{1,4}})\s+"
                  rf"(?:(?:most\s+recent|latest|newest)\s+)?(?:(?:journal|log|error|warning)\s+)?"
                  rf"(?:lines|entries|messages|records|logs|errors|warnings)\b", t)
    if m and not re.fullmatch(r"(?:minutes?|hours?|days?)", m[1]):
        count = _n(m[1]) if not m[1].isdigit() else int(m[1])
        if "limit" in found or not 1 <= count <= 100:
            return None
        found["limit"] = str(count)
        t = t[:m.start()] + " " + t[m.end():] + " lines "
    elif re.search(r"\b(?:the\s+)?(?:newest|latest|last|most\s+recent)\s+(?:(?:journal|log)\s+)?"
                   r"(?:error|warning|entry|message|log\s+line|line)\b(?!s)", t) and "limit" not in found:
        found["limit"] = "1"
    # level
    if re.search(r"\b(?:errors?|err|error-level|failures?|critical)\b", t):
        found.setdefault("prio", "err")
    elif re.search(r"\bwarn(?:ings?)?\b", t):
        found.setdefault("prio", "warning")
    t = re.sub(r"\b(?:errors?|err|error-level|failures?|critical|warnings?|warn)\b", " ", t)
    if re.search(r"\buser\s+(?:journal|logs?|session)\b|\bmy\s+user\b", t):
        found["scope"] = "user"
    t = re.sub(r"\buser\b", " ", t)
    # unit or program tag
    for pat, key in ((rf"\b({UNIT}\.service)\b", "unit"),
                     (rf"\b(?:service|unit)\s+({UNIT})", "unit"),
                     (rf"\b({UNIT})\s+service\b", "unit")):
        if key not in found:
            take(pat, key)
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9_.@:+-]*|'s", t) if w != "'s"]
    left = [w for w in words if w not in _J_FILLER]
    # One name left with no "program" or "tag" word is a service (the tool's own
    # reading); an empty read of it names the program-tag sentence.
    named = bool(left) and re.search(
        rf"\b(?:(?:for|from|of|by)\s+(?:the\s+)?|(?:program|tag|tagged|identifier|unit|service)\s+(?:is\s+|[=:]\s*)?)"
        rf"{re.escape(left[0])}\b"
        rf"|\b{re.escape(left[0])}\s+(?:has\s+|have\s+)?(?:logged|wrote|written|emitted|produced|reported)\b|\b{re.escape(left[0])}(?:'s)?\s+"
        rf"(?:(?:user|system|journal|recent|latest)\s+)?(?:logs?|errors?|warnings?|journal|entries|messages|log\s+lines)\b",
        text, re.I)
    if "ident" not in found and "unit" not in found and len(left) == 1 and re.fullmatch(UNIT, left[0]) \
            and not left[0].isdigit() and left[0] not in _J_NOUNS and named:
        tagged = re.search(rf"\b(?:program|process|app|application|tag|tagged|identifier)\b", text, re.I)
        # Unit names and tags are case-sensitive: take the name as it was written.
        said = re.search(rf"(?<![\w.@:-]){re.escape(left[0])}(?![\w.@:-])", text, re.I)
        found["ident" if tagged else "unit"] = said.group(0) if said else left[0]
        left = []
    if left or ("ident" in found and "unit" in found):
        return None
    if not re.search(r"\b(?:journal|journalctl|syslog|logs?|entries|messages|lines)\b|errors?|warnings?",
                     text, re.I):
        return None
    words_out = {"err": "errors", "error": "errors", "3": "errors",
                 "warning": "warnings", "warn": "warnings", "4": "warnings"}.get(found.get("prio"), "journal")
    out = ("user " if found.get("scope") == "user" else "") + words_out
    if "ident" in found:
        out += f" from the program {found['ident']}"
    if "unit" in found:
        out += f" from the {_unit(found['unit'])} service"
    if found.get("boot") == "previous":
        out += " from the previous boot"
    since = None
    if "time" in found:
        count, unit = found["time"]
        since = f"{count} {unit}{'s' if count != 1 else ''} ago"
    if "stamp" in found:
        if since:
            return None
        v = re.sub(r"(?<=[0-9])t(?=[0-9])", " ", found["stamp"])
        since = re.sub(r"\s*(?:utc|z)$", " UTC", v)
    if "since_raw" in found:
        if since:
            return None
        raw = found["since_raw"].strip().lower()
        m = re.fullmatch(rf"({_NUM})\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d)(?:\s+ago)?", raw)
        if m:
            count = _n(m[1]); unit = _J_UNITS[m[2]]
            since = f"{count} {unit}{'s' if count != 1 else ''} ago"
        elif raw in ("today", "yesterday") or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
            since = raw
        else:
            return None
    if "day" in found:
        if since:
            return None
        since = found["day"]
    if since:
        out += f" since {since}"
    if found.get("boot") == "current" and not since:
        out += " from this boot"
    if found.get("boot") == "any" and not since:
        out += " from all boots"
    if "limit" in found:
        out += f" last {int(found['limit'])}"
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
    return _journal(text) or _journal_slots(text) or _package(text)


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
