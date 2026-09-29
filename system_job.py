"""Read-only OS queries: five tools, with independently checked arguments.

The grammar consumes the whole request. Model calls must match its complete
reading before any collector runs. Observations are data, never new requests.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from actions import Refusal


def _enum(*values):
    return {"type": "string", "enum": list(values)}


TOOLS = [
    {"name": "resources", "description": "Read memory, CPU load or filesystem capacity",
     "parameters": {"type": "object", "properties": {
         "kind": _enum("memory", "cpu", "disk", "all"),
         "path": {"type": "string", "description": "absolute filesystem path; default /"}},
         "required": ["kind"], "additionalProperties": False}},
    {"name": "processes", "description": "Read processes ranked by resident memory or sampled CPU",
     "parameters": {"type": "object", "properties": {
         "sort": _enum("memory", "cpu"),
         "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
         "required": ["sort"], "additionalProperties": False}},
    {"name": "services", "description": "Read failed/all loaded services or one exact service's status",
     "parameters": {"type": "object", "properties": {
         "state": _enum("failed", "all"), "unit": {"type": "string"},
         "scope": _enum("system", "user")}, "required": [], "additionalProperties": False}},
    {"name": "journal", "description": "Read recent journal records, with service, boot, time and priority filters",
     "parameters": {"type": "object", "properties": {
         "unit": {"type": "string"},
         "identifier": {"type": "string", "description": "syslog identifier (a program's log tag)"},
         "boot": _enum("current", "previous", "any"),
         "since": {"type": "string", "description": "today, yesterday, N minutes/hours/days ago, or YYYY-MM-DD"},
         "priority": _enum("all", "err", "warning"), "scope": _enum("system", "user"),
         "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
         "required": [], "additionalProperties": False}},
    {"name": "packages", "description": "Read installed package status or ownership of a command/absolute file path",
     "parameters": {"type": "object", "properties": {
         "operation": _enum("status", "owner"), "target": {"type": "string"}},
         "required": ["operation", "target"], "additionalProperties": False}},
]
DEFAULTS = {
    "resources": {"path": "/"}, "processes": {"limit": 10},
    "services": {"state": "all", "scope": "system"},
    "journal": {"boot": "current", "priority": "all", "scope": "system", "limit": 50},
    "packages": {},
}
UNIT = r"[A-Za-z0-9_][A-Za-z0-9_.@:-]{0,127}"
PACKAGE = r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?"
PATH = r"/(?:[A-Za-z0-9_+.,@%=-]+/)*[A-Za-z0-9_+.,@%=-]*"
COMMAND = r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}"
SINCE = (r"(?:today|yesterday|[1-9][0-9]{0,2} (?:minutes?|hours?|days?) ago"
         r"|[0-9]{4}-[0-9]{2}-[0-9]{2}(?: [0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?(?: [uU][tT][cC])?)")


@dataclass(frozen=True)
class Action:
    kind: str
    args: dict


def normalize(kind, args):
    """Strict schema validation, shared by interpretation and collector entry."""
    spec = next((t["parameters"] for t in TOOLS if t["name"] == kind), None)
    if spec is None or not isinstance(args, dict) or set(args) - set(spec["properties"]):
        raise ValueError("unknown system tool or argument")
    if any(key not in args for key in spec["required"]):
        raise ValueError("missing required system argument")
    result = {**DEFAULTS[kind], **args}
    for key, value in result.items():
        prop = spec["properties"][key]
        if prop["type"] == "integer":
            if type(value) is not int or not prop["minimum"] <= value <= prop["maximum"]:
                raise ValueError("system query limit is out of range")
        elif not isinstance(value, str) or not value or len(value) > 512:
            raise ValueError("system argument must be a bounded string")
        if "enum" in prop and value not in prop["enum"]:
            raise ValueError("unsupported system argument")
    if "unit" in result:
        if (not re.fullmatch(UNIT, result["unit"]) or result["unit"].endswith(".")
                or result["unit"].lower() in ("the", "this", "that", "it")):
            raise ValueError("name one exact service")
        if not result["unit"].endswith(".service"):
            result["unit"] += ".service"
    if "identifier" in result:
        if (not re.fullmatch(UNIT, result["identifier"]) or result["identifier"].endswith(".")
                or result["identifier"].lower() in ("the", "this", "that", "it")):
            raise ValueError("name one exact program tag")
        if "unit" in result:
            raise ValueError("choose a service or a program tag, not both")
    if "path" in result and not re.fullmatch(PATH, result["path"]):
        raise ValueError("use an absolute filesystem path without wildcards or spaces")
    if kind == "resources" and result["kind"] not in ("disk", "all") and result["path"] != "/":
        raise ValueError("only disk queries take a path")
    if kind == "services" and "unit" in result and result["state"] != "all":
        raise ValueError("choose a service status or a failed-service list")
    if "since" in result:
        if not re.fullmatch(SINCE, result["since"]):
            raise ValueError("unsupported journal time")
        stamp = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?: ([0-9]{2}:[0-9]{2}(?::[0-9]{2})?))?(?: utc)?",
                             result["since"], re.I)
        if stamp:
            # A real date and time only ("2026-13-40" and "25:61" are refused).
            from datetime import date, time
            date.fromisoformat(stamp[1])
            if stamp[2]:
                time.fromisoformat(stamp[2])
            result["since"] = result["since"][:10] + result["since"][10:].replace("utc", "UTC")
    if kind == "packages":
        pattern = PACKAGE if result["operation"] == "status" else f"(?:{PATH}|{COMMAND})"
        if not re.fullmatch(pattern, result["target"]) or result["target"].lower() in ("the", "this", "that", "it"):
            raise ValueError("name one exact package, command or absolute file path")
    return result


def action(tool, **args):
    return Action(tool, normalize(tool, args))


def ambiguous_owner(request):
    """A definite generic object needs a name, not a guessed command target.

    `owns file` and `owns the file command` still name the real file utility.
    `owns the file` does not identify which file the caller means.
    """
    return isinstance(request, str) and bool(re.search(
        r"\b(?:which|what) package (?:provides|owns) "
        r"(?:the|this|that|my) (?:file|command|program|executable|path)"
        r"\s*[?!.]*$", request.strip(), re.I))


def _clause(text):
    if ambiguous_owner(text):
        return None

    def match(pattern):
        return re.fullmatch(pattern, text, re.I)

    if match(r"(?:show |check )?(?:system resources|resource usage|system load)"):
        return [action("resources", kind="all")]
    if match(r"why is (?:my |the )?(?:machine|computer|system) (?:so )?slow"):
        return [action("resources", kind="all"), action("processes", sort="cpu")]
    if match(r"(?:show |check )?(?:memory|ram|swap)(?: usage| status)?|how much (?:memory|ram) is (?:free|available)"):
        return [action("resources", kind="memory")]
    if match(r"(?:show |check )?(?:cpu load|load averages?)"):
        return [action("resources", kind="cpu")]
    m = match(rf"(?:show |check )?(?:disk space|disk usage|free space)(?: (?:on|for) (?P<path>{PATH}|the root disk))?")
    if not m:
        m = match(rf"how much (?:disk )?space is (?:left|free)(?: on (?P<path>{PATH}|the root disk))?")
    if m:
        return [action("resources", kind="disk", path=m["path"] if m["path"] and m["path"] != "the root disk" else "/")]
    m = match(r"(?:what(?:'s| is) (?:using|consuming) (?:all )?(?:my |the )?(?:most )?(?P<metric>memory|ram|cpu)|"
              r"(?:show |list )?(?:the )?(?:top (?P<limit>[0-9]+) )?processes (?:by|using the most) (?P<sort>memory|ram|cpu))")
    if m:
        sort = (m["metric"] or m["sort"]).lower()
        return [action("processes", sort="memory" if sort == "ram" else sort, limit=int(m["limit"] or 10))]
    m = match(r"(?:show |list )?(?:(?P<scope>user|system) )?(?P<state>failed|all) services")
    if m:
        return [action("services", state=m["state"].lower(), scope=(m["scope"] or "system").lower())]
    m = match(rf"(?:show |check )?(?:the )?(?:(?P<scope>user|system) )?(?:status of (?:the )?)?(?P<unit>{UNIT}) service(?: status)?")
    if m:
        return [action("services", unit=m["unit"], scope=(m["scope"] or "system").lower())]
    if match(r"what failed during (?:this|the current) boot"):
        return [action("services", state="failed"), action("journal", priority="err")]
    m = match(rf"(?:show |read |list )?(?:the )?(?:(?P<scope>user|system) )?"
              rf"(?P<kind>journal|logs|errors|warnings)(?: from (?:the )?(?P<unit>{UNIT}) service"
              rf"| from (?:the )?program (?P<ident>{UNIT}))?"
              rf"(?: (?:from|during) (?P<boot>this|the current|the previous|all) boots?)?"
              rf"(?: since (?P<since>{SINCE}))?(?: (?:limit|last) (?P<limit>[0-9]+)(?: entries)?)?")
    if m:
        args = {"priority": {"errors": "err", "warnings": "warning"}.get(m["kind"].lower(), "all"),
                "scope": (m["scope"] or "system").lower(), "limit": int(m["limit"] or 50)}
        if m["unit"]:
            args["unit"] = m["unit"]
        if m["ident"]:
            args["identifier"] = m["ident"]
        args["boot"] = {"this": "current", "the current": "current", "the previous": "previous", "all": "any"}.get(
            (m["boot"] or "").lower(), "any" if m["since"] else "current")
        if m["since"]:
            args["since"] = m["since"].lower()
        return [action("journal", **args)]
    m = match(rf"(?:which|what) package (?:provides|owns) (?:the )?(?P<target>{PATH}|{COMMAND})(?: command)?")
    if m:
        return [action("packages", operation="owner", target=m["target"])]
    m = match(rf"is (?P<target>{PACKAGE}) installed|(?:show |check )?(?:package )?(?P<package>{PACKAGE}) package status")
    if m:
        return [action("packages", operation="status", target=m["target"] or m["package"])]
    return None


def parse(request):
    """The queries the request states, or None. An agent's wording of one query is
    read through its grammar sentence (system_agent.canonical)."""
    found = _parse(request)
    if found is None and isinstance(request, str) and len(request) <= 2048 \
            and not any(ord(c) < 32 or ord(c) == 127 for c in request):
        import system_agent
        sentence = system_agent.canonical(request)
        if sentence is not None:
            found = _parse(sentence)
    return found


def _parse(request):
    if not isinstance(request, str) or not request or len(request) > 2048:
        return None
    # No control characters, multi-line text or executable syntax. Values keep case.
    if any(ord(c) < 32 or ord(c) == 127 for c in request):
        return None
    text = " ".join(request.strip().split()).replace("’", "'")
    text = re.sub(r"^(?:please |can you |could you )", "", text, flags=re.I)
    text = re.sub(r"(?:,? please|,? thanks)$", "", text, flags=re.I)
    text = text.rstrip("?!")
    try:
        single = _clause(text)
        if single:
            return single
        parts = re.split(r" and (?:then )?", text, flags=re.I)
        if 2 <= len(parts) <= 3:
            groups = [_clause(part) for part in parts]
            if all(groups) and sum(len(group) for group in groups) <= 3:
                return [item for group in groups for item in group]
    except (ValueError, OverflowError):
        pass
    return None


def interpret(request, calls):
    wanted = parse(request)
    if wanted is None:
        return [Refusal("system", "unsupported or incomplete read-only query; name a resource, process metric, service, journal filter or package")]
    if not isinstance(calls, list) or len(calls) != len(wanted):
        return [Refusal("system", "model did not return every requested query exactly once")]
    observed = []
    try:
        for call in calls:
            if not isinstance(call, dict):
                raise ValueError("malformed system call")
            observed.append(Action(call.get("name"), normalize(call.get("name"), call.get("arguments"))))
    except (ValueError, TypeError) as error:
        return [Refusal("system", str(error))]
    if observed != wanted:
        return [Refusal("system", "model query, target or filters differ from the complete request")]
    return wanted


class Baseline:
    """Explicit engine-free grammar baseline; never presented as model inference."""
    label = "system grammar baseline"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def reset(self):
        pass

    def complete(self, request):
        return {"function_calls": [{"name": a.kind, "arguments": a.args} for a in parse(request) or []]}
