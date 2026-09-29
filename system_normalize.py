"""Planning-only, grammar-first normalizer for read-only system questions.

Model output is a proposal, never authorization to run a collector.  The checks
below bound the proposal and ground literal arguments; they cannot prove that a
model understood every nuance of an arbitrary sentence.
"""
from __future__ import annotations

import re

import system_job as job


_WRITE = re.compile(
    r"\b(?:restart|reboot|shutdown|power\s*off|stop|start|enable|disable|"
    r"install|uninstall|remove|delete|erase|kill|terminate|modify|change|"
    r"set|configure|update|upgrade|write|clear|truncate|create|touch|"
    r"mount|unmount|umount|fix|repair|patch|edit|launch|execute|"
    r"run\s+(?:a\s+|the\s+)?command|turn\s+(?:on|off))\b", re.I)
_NEGATIVE = re.compile(r"\b(?:do\s+not|don't|dont|never|without|no)\s+", re.I)
_REFERENT = re.compile(r"\b(?:same thing|same one|that one|the other|it|its|this|that|them|those|then)\b", re.I)
_UNSUPPORTED = re.compile(
    r"\b(?:temperature|thermal|fan\s*speed|wifi|wi-fi|network|bandwidth|"
    r"battery|gpu|remote|another host|container|docker|podman|kernel config|"
    r"file contents?|directory contents?|inodes?|directory\s+sizes?|"
    r"cache\s+sizes?|caches?)\b", re.I)
_READ = re.compile(
    r"\b(?:show|check|read|list|look|inspect|status|usage|space|load|"
    r"memory|ram|swap|cpu|disk|resources?|process|service|journal|logs?|errors?|"
    r"warnings?|package|installed|owns?|provides?|failed|what|which|why|how)\b", re.I)
_NUMBER = re.compile(r"\b\d+\b")
_PATH = re.compile(job.PATH)
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
          "eleven": 11, "twelve": 12, "twenty": 20, "fifty": 50}
_LIMIT = re.compile(r"\b(?:first|top|last|limit|up\s+to)\s+"
                    r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
                    r"eleven|twelve|twenty|fifty)\b(?!\s+(?:minutes?|hours?|days?|boots?))", re.I)


def _result(path, decision, actions=(), raw_reply=None, reasons=(),
            runtime_error=False, protocol_error=False, validation_error=False,
            trust=None):
    return {"path": path, "decision": decision, "actions": list(actions),
            "raw_reply": raw_reply, "reasons": list(reasons),
            "runtime_error": runtime_error, "protocol_error": protocol_error,
            "validation_error": validation_error,
            "trust": trust}


def _active_text(request):
    """Remove *only* explicit negative write clauses for write preflight.

    A negated write followed by a read is safe to propose.  We deliberately do
    not strip negative read clauses, since those can reverse the request.
    """
    spans = []
    for match in _WRITE.finditer(request):
        prefix = request[max(0, match.start()-32):match.start()]
        negative = list(_NEGATIVE.finditer(prefix))
        if negative and not re.search(r"[.!?;]", prefix[negative[-1].end():]):
            spans.append((match.start(), match.end()))
    text = request
    for start, end in reversed(spans):
        text = text[:start] + " " * (end-start) + text[end:]
    return text


def _bounds(request):
    if (not isinstance(request, str) or not request.strip() or len(request) > 2048
            or any((ord(ch) < 32 and ch not in "\n\t") or ord(ch) == 127 for ch in request)):
        return "decline", "malformed or oversized request"
    return None


def _preflight(request):
    if job.ambiguous_owner(request):
        return "clarify", "name the command or absolute file path for package ownership"
    if re.search(r"`|\$\(|;|&&|\|", request):
        return "decline", "command syntax is not supported"
    active = _active_text(request)
    if _WRITE.search(active):
        return "decline", "request includes a state change"
    if _UNSUPPORTED.search(request):
        return "decline", "observation is outside the five read tools"
    if re.search(r"\b(?:but\s+not|except|excluding)\b", request, re.I):
        return "decline", "negative exclusion is not supported by the read tools"
    if re.search(r"\b(?:do not|don't|dont|never)\s+(?:show|check|read|list|query|inspect)\b", request, re.I):
        # A later, explicit read may override an earlier prohibition.
        if not re.search(r"\b(?:but|instead|just)\b.{0,100}\b(?:show|check|read|list)\b", request, re.I):
            return "decline", "read is explicitly prohibited"
    # Bare deictic requests lack a stable target in this standalone interface.
    if re.search(r"\b(?:the other|same|that|this)\s+(?:service|package|command|path)\b", request, re.I):
        return "clarify", "named target depends on unresolved prior context"
    if re.search(r"\b(?:that|this)\s+(?:drive|disk|filesystem)\b", request, re.I):
        return "clarify", "filesystem target depends on unresolved prior context"
    if re.search(r"\b(?:don'?t|do\s+not)\s+(?:remember|know)\s+(?:its|the)\s+name\b", request, re.I):
        return "clarify", "exact service or package name is missing"
    if re.search(r"\b(?:its\s+logs|errors?\s+from\s+then)\b", request, re.I) and not re.search(
            r"\b(?:previous|current|this)\s+boot\b|\bprevious\s+one\b", request, re.I):
        return "clarify", "log referent is unresolved"
    if _REFERENT.search(request) and not re.search(
            r"\b(?:memory|ram|swap|cpu|disk|space|process|service|journal|log|"
            r"package|installed|load|error|warning)\b", request, re.I):
        return "clarify", "request depends on unresolved prior context"
    if not _READ.search(request):
        return "clarify", "no supported observation is specified"
    return None


def _literal_present(request, value):
    # Exact case matters for paths and systemd units; package syntax is lower case.
    return bool(re.search(r"(?<![A-Za-z0-9_./+-])" + re.escape(value)
                          + r"(?![A-Za-z0-9_./+-])", request))


def _clause_for(request, kind, args):
    clauses = re.split(r"\s+and(?:\s+then)?\s+|[.!?]\s+", request, flags=re.I)
    if len(clauses) == 1:
        return request
    literal = (args.get("target") if kind == "packages" else
               args.get("unit") if kind in ("services", "journal") else
               args.get("path") if kind == "resources" else None)
    if literal and literal != "/":
        bare = literal[:-8] if literal.endswith(".service") else literal
        matched = [clause for clause in clauses if _literal_present(clause, literal)
                   or _literal_present(clause, bare)]
        if len(matched) == 1:
            return matched[0]
    if kind == "resources":
        cue = {"memory": r"\b(?:memory|ram|swap)\b", "cpu": r"\b(?:cpu|load)\b",
               "disk": r"\b(?:disk|space|filesystem|storage)\b",
               "all": r"\b(?:resource|overall|system)\b"}[args["kind"]]
    else:
        cue = {"processes": r"\b(?:process|rank|top)\b",
               "services": r"\bservice\b", "journal": r"\b(?:journal|logs?|errors?|warnings?)\b",
               "packages": r"\bpackage\b"}[kind]
    matched = [clause for clause in clauses if re.search(cue, clause, re.I)]
    return matched[-1] if len(matched) == 1 else request


def _numeric(token):
    return int(token) if token.isdigit() else _WORDS[token.lower()]


def _ground(request, kind, args):
    local = _clause_for(request, kind, args)
    low = local.lower()
    correction = re.search(r"\b(?:actually|rather|instead)\b", request, re.I)
    literal_request = request[correction.end():] if correction else request
    explicit_paths = [m.group() for m in _PATH.finditer(literal_request)
                      if m.group() != "/"]
    process_intent = bool(re.search(
        r"\b(?:process(?:es)?|rank(?:ed|ing)?|hogs?|consumers?|chewing|"
        r"eating|taking\s+up|using\s+(?:the\s+)?most)\b", low))
    if kind == "resources" and args["kind"] in ("disk", "all") and explicit_paths:
        if args["path"] not in explicit_paths:
            return "disk path conflicts with request"
    if "path" in args and args["path"] != "/":
        if not _literal_present(literal_request, args["path"]):
            return "filesystem path is not an exact request literal"
    if "unit" in args:
        unit = args["unit"]
        bare = unit[:-8] if unit.endswith(".service") else unit
        if not (_literal_present(literal_request, unit) or _literal_present(literal_request, bare)):
            return "service is not an exact request literal"
    if kind == "packages":
        if not _literal_present(literal_request, args["target"]):
            return "package or owner target is not an exact request literal"
        owner_cue = bool(re.search(r"\b(?:owns?|provides?|belongs?\s+to|came\s+from)\b", low))
        status_cue = bool(re.search(r"\b(?:installed|installation|package\s+status|"
                                    r"already\s+here|present)\b", low))
        if not (owner_cue or status_cue or re.search(r"\bpackage\b", low)):
            return "package query was not requested"
        if owner_cue and args["operation"] != "owner":
            return "package operation conflicts with request"
        if status_cue and not owner_cue and args["operation"] != "status":
            return "package operation conflicts with request"
    if kind == "processes":
        if not process_intent:
            return "process ranking was not requested"
        cpu = bool(re.search(r"\bcpu\b", low))
        memory = bool(re.search(r"\b(?:memory|ram|rss)\b", low))
        if cpu != memory and args["sort"] != ("cpu" if cpu else "memory"):
            return "process ranking conflicts with request"
    if kind == "resources":
        if process_intent:
            return "resource totals cannot answer process ranking"
        cues = {"memory": r"\b(?:memory|ram|swap)\b", "cpu": r"\b(?:cpu|load)\b",
                "disk": r"\b(?:disk|space|filesystem|storage)\b"}
        present = {key for key, pattern in cues.items() if re.search(pattern, low)}
        if args["kind"] == "all" and not (
                re.search(r"\b(?:overall|system|all)\s+resources?\b|\bresources?\s+(?:overall|usage|figures?)\b", low)
                or len(present) == 3):
            return "overall resource read was not requested"
        if args["kind"] != "all" and args["kind"] not in present:
            return "resource kind was not requested"
        if len(present) == 1 and args["kind"] != next(iter(present)):
            return "resource kind conflicts with request"
        if args["kind"] == "all" and len(present) == 1:
            return "broad resource read was not requested"
    if kind == "services":
        if re.search(r"\b(?:yesterday|ago|since|previous\s+boot|last\s+boot)\b", low):
            return "historical service state is unavailable"
        if "unit" not in args and not re.search(r"\b(?:list|all|failed|services)\b", low):
            return "named service status requires its unit"
        if "unit" not in args and re.search(r"\b(?:running|status|active)\b", low) and not re.search(
                r"\b(?:list|all|failed)\b", low):
            return "named service status requires its unit"
        if re.search(r"\bfailed\b", low) and args["state"] != "failed" and "unit" not in args:
            return "service state conflicts with request"
    if kind == "journal":
        if not re.search(r"\b(?:journal|logs?|errors?|warnings?)\b", low):
            return "journal read was not requested"
        if re.search(r"\b(?:errors?|err)\b", low) and args["priority"] != "err":
            return "journal priority conflicts with request"
        if re.search(r"\bwarn(?:ing)?s?\b", low) and args["priority"] != "warning":
            return "journal priority conflicts with request"
        if args["priority"] == "err" and not re.search(r"\b(?:errors?|err|failed)\b", low):
            return "error priority was not requested"
        if args["priority"] == "warning" and not re.search(r"\bwarn(?:ing)?s?\b", low):
            return "warning priority was not requested"
        if "since" in args and not _literal_present(request, args["since"]):
            return "journal time is not an exact request literal"
        explicit_since = re.search(r"\bsince\s+(today|yesterday|\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?"
                                   r"(?: utc)?|"
                                   r"\d+\s+(?:minutes?|hours?|days?)\s+ago)\b", local, re.I)
        if explicit_since and args.get("since", "").lower() != explicit_since.group(1).lower():
            return "journal time conflicts with request"
    if "scope" in args:
        if re.search(r"\buser\b", low) and args["scope"] != "user":
            return "scope conflicts with request"
        if args["scope"] == "user" and not re.search(r"\buser\b", low):
            return "user scope was not requested"
    if "boot" in args:
        boot_text = low if re.search(r"\b(?:previous|prior|last|this|current|all|any)\s+boot\b|"
                                     r"\bprevious\s+one\b", low) else request.lower()
        previous = bool(re.search(r"\b(?:previous|prior|last)\s+boot\b|\bprevious\s+one\b", boot_text))
        current = bool(re.search(r"\b(?:this|current)\s+boot\b", boot_text))
        any_boot = bool(re.search(r"\b(?:all|any)\s+boots\b", boot_text))
        wanted = "previous" if previous else "current" if current else "any" if any_boot else None
        if wanted and args["boot"] != wanted:
            return "boot filter conflicts with request"
        if kind == "journal" and "since" in args and not wanted and args["boot"] != "any":
            return "journal time requires any boot by default"
        if kind == "journal" and re.search(r"\bsince\b", low) and not wanted and args["boot"] != "any":
            return "journal time requires any boot by default"
        if not wanted and args["boot"] not in ("current", "any" if "since" in args else "current"):
            return "boot filter was not requested"
    if "limit" in args:
        limit_mentions = [_numeric(m.group(1)) for m in _LIMIT.finditer(local)]
        limit_mentions += [_numeric(m.group(1)) for m in re.finditer(
            r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve|twenty|fifty)\s+(?:biggest|largest|top)\b", local, re.I)]
        if re.search(r"\b(?:newest|latest|most\s+recent|biggest)\s+"
                     r"(?:one|entry|record|process|error|log|journal)\b", low):
            limit_mentions.append(1)
        if limit_mentions and args["limit"] != limit_mentions[-1]:
            return "limit conflicts with request"
        default = job.DEFAULTS[kind]["limit"]
        if args["limit"] != default and args["limit"] not in limit_mentions:
            return "limit is not a request literal"
    return None


def plan(request, classify):
    """Return a validated plan. ``classify(request)`` returns a library reply.

    ``actions`` are ``[kind, normalized_args]`` pairs.  Callers must decide
    separately whether collection is authorized; model plans carry explicit
    ``model_proposal`` trust.
    """
    preflight = _bounds(request)
    if preflight:
        return _result("refused", preflight[0], reasons=(preflight[1],))
    parsed = job.parse(request)
    if parsed:
        return _result("grammar", "read", [[a.kind, a.args] for a in parsed],
                       reasons=("complete grammar match",), trust="grammar")
    preflight = _preflight(request)
    if preflight:
        return _result("refused", preflight[0], reasons=(preflight[1],))
    try:
        reply = classify(request)
    except Exception as exc:
        return _result("refused", "clarify", reasons=(f"classifier failed: {type(exc).__name__}: {exc}",),
                       runtime_error=True)
    if not isinstance(reply, dict) or reply.get("error"):
        return _result("refused", "clarify", raw_reply=reply,
                       reasons=("classifier returned an error",), runtime_error=True)
    calls = reply.get("function_calls")
    if not isinstance(calls, list):
        return _result("refused", "clarify", raw_reply=reply,
                       reasons=("missing function_calls list",), protocol_error=True)
    if not calls:
        return _result("refused", "clarify", raw_reply=reply,
                       reasons=("classifier did not propose a read",))
    if len(calls) > 3:
        return _result("refused", "decline", raw_reply=reply,
                       reasons=("more than three calls",), protocol_error=True)
    actions, seen = [], set()
    try:
        for call in calls:
            if not isinstance(call, dict) or set(call) != {"name", "arguments"}:
                raise ValueError("malformed call object")
            kind = call["name"]
            args = job.normalize(kind, call["arguments"])
            signature = (kind, tuple(sorted(args.items())))
            if signature in seen:
                raise ValueError("duplicate call")
            seen.add(signature)
            actions.append([kind, args])
    except (ValueError, TypeError) as exc:
        return _result("refused", "clarify", raw_reply=reply, reasons=(str(exc),),
                       protocol_error=True)
    for kind, args in actions:
        reason = _ground(request, kind, args)
        if reason:
            return _result("refused", "clarify", raw_reply=reply,
                           reasons=(reason,), validation_error=True)
    return _result("model", "read", actions, raw_reply=reply,
                   reasons=("bounded, request-grounded model proposal; semantic completeness unproven",),
                   trust="model_proposal")
