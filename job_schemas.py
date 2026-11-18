"""What the bundle reads for each job, and how its reading becomes the job's call.

The bundle of small nets (panes_bundle.py reads it; panes_bundle_train.py
trains it) answers with calls of a schema: each tool's arguments, each one a
value of a closed list (enum), a flag, a number (int) or text copied from the
request. For the panes job that schema is the actions' own. For every other
job it is the schema below, made for reading: one tool for each operation the
suite of experiment 11 asks of the job, tool names that no other job uses,
and values the job's call builds from (a "last N days" is a closed value and a
number). `to_job` turns a reading into the job's own call, the way the job's
grammar writes it, and the job's own checks then decide, unchanged:

- files: the call must equal the files grammar's reading (files_job.interpret);
- system: the queries must equal the system grammar's (system_job.interpret);
- agents: the calls must match the agents grammar's readings (agents.interpret);
- apps: the apps reading (apps.interpret);
- tmux, pty: the grammar's own sentence for the call reads back as the call
  (tmux_job.reads_back, pty_job.reads_back);
- logs: the logs tool's argument checks (logs_words.check).

Nothing here admits anything. A reading that cannot be written as a call of
the job is None, and the bundle has no answer.
"""
from __future__ import annotations

import posixpath

JOBS = ("panes", "files", "system", "agents", "apps", "tmux", "pty", "logs")

AGENTS = ("claude", "codex", "grok", "qwen-omp")
# A journal time as the grammar keeps it: the unit word as the request says it.
SINCE_UNITS = ("today", "yesterday", "minute", "minutes", "hour", "hours", "day", "days", "date")


def _arg(key, kind, low=None, high=None, values=None):
    return {"key": key, "kind": kind, "low": low, "high": high, "values": list(values or ())}


# job -> {tool: {"args": [...], "required": [...]}}
SCHEMAS = {
    "files": {
        "preview_file": {"args": [_arg("path", "text"), _arg("scope", "text")], "required": ["path"]},
        "search_text": {"args": [_arg("text", "text"), _arg("scope", "text")], "required": ["text", "scope"]},
        "find_files": {"args": [_arg("scope", "text"), _arg("name", "text"), _arg("extension", "text"),
                                _arg("modified", "enum", values=("today", "yesterday", "days")),
                                _arg("days", "int", 1, 99), _arg("prefix", "flag")],
                       "required": ["scope"]},
        "list_files": {"args": [_arg("scope", "text"), _arg("order", "enum", values=("size", "modified"))],
                       "required": ["scope", "order"]},
    },
    "system": {
        "resources": {"args": [_arg("kind", "enum", values=("memory", "cpu", "disk", "all")),
                               _arg("path", "text")], "required": ["kind"]},
        "processes": {"args": [_arg("sort", "enum", values=("memory", "cpu")), _arg("limit", "int", 1, 50)],
                      "required": ["sort"]},
        "services": {"args": [_arg("state", "enum", values=("failed", "all")), _arg("unit", "text"),
                              _arg("scope", "enum", values=("user",))], "required": []},
        "journal": {"args": [_arg("unit", "text"), _arg("identifier", "text"),
                             _arg("boot", "enum", values=("current", "previous", "any")),
                             _arg("since", "enum", values=SINCE_UNITS), _arg("since_n", "int", 1, 999),
                             _arg("since_date", "text"),
                             _arg("priority", "enum", values=("err", "warning")),
                             _arg("scope", "enum", values=("user",)), _arg("limit", "int", 1, 100)],
                    "required": []},
        "packages": {"args": [_arg("operation", "enum", values=("status", "owner")), _arg("target", "text")],
                     "required": ["operation", "target"]},
        # Two queries the grammar reads from one phrase.
        "slow_machine": {"args": [], "required": []},
        "boot_failures": {"args": [], "required": []},
    },
    "agents": {
        "agent": {"args": [_arg("agent", "enum", values=AGENTS), _arg("dir", "text"), _arg("prompt", "text"),
                           _arg("resume", "text"), _arg("model", "text"),
                           _arg("place", "enum", values=("tab", "right", "left", "down", "up"))],
                  "required": ["agent", "dir"]},
        "wait": {"args": [_arg("s_agent", "enum", values=AGENTS), _arg("s_dir", "text"),
                          _arg("for", "enum", values=("idle", "waiting")),
                          _arg("timeout", "int", 1, 86400), _arg("minutes", "flag")],
                 "required": ["s_agent", "s_dir", "for"]},
        "tell": {"args": [_arg("s_agent", "enum", values=AGENTS), _arg("s_dir", "text"), _arg("text", "text"),
                          _arg("wait", "flag")],
                 "required": ["s_agent", "s_dir", "text"]},
    },
    "apps": {
        "launch": {"args": [_arg("app", "enum")], "required": ["app"]},      # values: apps.LAUNCHABLE
        "settings": {"args": [_arg("section", "enum", values=("top-bar", "pane-buttons", "voice", "games",
                                                               "tools"))], "required": ["section"]},
    },
    "tmux": {
        "tmux_list": {"args": [], "required": []},
        "tmux_new": {"args": [_arg("name", "text"), _arg("cwd", "text")], "required": ["name"]},
        "tmux_read": {"args": [_arg("target", "text"), _arg("lines", "int", 1, 2000)], "required": ["target"]},
        "tmux_rename": {"args": [_arg("target", "text"), _arg("new_name", "text")],
                        "required": ["target", "new_name"]},
    },
    "pty": {
        "pty_list": {"args": [], "required": []},
        "pty_journals": {"args": [], "required": []},
        "pty_status": {"args": [_arg("id", "text")], "required": ["id"]},
        "pty_pane": {"args": [_arg("pane_id", "int", 1, 999_999_999)], "required": ["pane_id"]},
        "pty_observe": {"args": [_arg("id", "text"), _arg("max_lines", "int", 1, 1000)],
                        "required": ["id", "max_lines"]},
        "pty_journal": {"args": [_arg("id", "text")], "required": ["id"]},
        "pty_foreground": {"args": [_arg("id", "text")], "required": ["id"]},
    },
    "logs": {
        "logs_events": {"args": [_arg("pane", "int", 1, 999_999_999), _arg("session", "text"), _arg("file", "text"),
                                 _arg("provider", "enum", values=("raw", "claude", "codex", "grok", "omp")),
                                 _arg("limit", "int", 1, 100)], "required": []},
        "logs_brief": {"args": [_arg("pane", "int", 1, 999_999_999), _arg("session", "text")], "required": []},
        "logs_search": {"args": [_arg("pane", "int", 1, 999_999_999), _arg("session", "text"), _arg("file", "text"),
                                 _arg("provider", "enum", values=("raw", "claude", "codex", "grok", "omp")),
                                 _arg("query", "text")], "required": ["query"]},
        "logs_source": {"args": [_arg("event", "text")], "required": ["event"]},
    },
}

# The bundle's calls that close, type, start a program or an agent, message
# an agent or launch an app: never answered by the second reader.
RISKY = frozenset({"agent", "tell", "launch"})


def schema(job: str) -> dict:
    """The trainer's form of a job's schema: {tool: {"args": [[key, kind, low, high]], "required": [...]}}."""
    out = {}
    for tool, spec in SCHEMAS[job].items():
        out[tool] = {"args": [[a["key"], a["kind"], a["low"], a["high"]] for a in spec["args"]],
                     "required": list(spec["required"])}
    return out


def closed_values(job: str) -> set:
    """Every value of a closed list in a job's schema."""
    values = set()
    for spec in SCHEMAS[job].values():
        for a in spec["args"]:
            values.update(a["values"])
    if job == "apps":
        import apps
        values.update(apps.LAUNCHABLE)
    return values


# ---- a reading as the job's call -------------------------------------------

def _files_scope(text):
    import files_job
    for candidate in (text, f'"{text}"' if '"' not in text else None):
        if candidate is None:
            continue
        try:
            return files_job._scope(candidate)
        except ValueError:
            continue
    return None


def _files(tool, a):
    import files_job
    if tool == "preview_file":
        path = a["path"]
        if "scope" in a:
            scope = _files_scope(a["scope"])
            return None if scope is None else {"name": tool, "arguments": {"scope": scope, "path": path}}
        if path.startswith("/") and not path.endswith("/"):
            head, tail = posixpath.split(path)
            return {"name": tool, "arguments": {"scope": head or "/", "path": tail}}
        return None
    scope = _files_scope(a["scope"]) if "scope" in a else None
    if scope is None:
        return None
    if tool == "search_text":
        return {"name": tool, "arguments": {"scope": scope, "text": a["text"]}}
    if tool == "list_files":
        return {"name": tool, "arguments": {"scope": scope, "order": a["order"]}}
    if tool == "find_files":
        extension = a.get("extension", "").lower().lstrip(".")
        extension = files_job._TYPE_WORDS.get(extension, extension)
        modified = a.get("modified", "any")
        if modified == "days":
            if "days" not in a:
                return None
            modified = f"last {a['days']} days"
        args = {"scope": scope, "name": a.get("name", ""), "extension": extension, "modified": modified}
        if a.get("prefix"):
            args["name_match"] = "prefix"
        return {"name": tool, "arguments": args}
    return None


def _since(a):
    unit = a.get("since")
    if unit is None:
        return None
    if unit in ("today", "yesterday"):
        return unit
    if unit == "date":
        return a.get("since_date")
    n = a.get("since_n")
    if n is None:
        return None
    return f"{n} {unit} ago"


def _system(tool, a):
    if tool == "slow_machine":
        return [{"name": "resources", "arguments": {"kind": "all"}},
                {"name": "processes", "arguments": {"sort": "cpu"}}]
    if tool == "boot_failures":
        return [{"name": "services", "arguments": {"state": "failed"}},
                {"name": "journal", "arguments": {"priority": "err"}}]
    if tool == "journal":
        args = {k: a[k] for k in ("unit", "identifier", "priority", "scope", "limit") if k in a}
        if "since" in a:
            since = _since(a)
            if since is None:
                return None
            args["since"] = since
            args["boot"] = a.get("boot", "any")
        elif "boot" in a:
            args["boot"] = a["boot"]
        return [{"name": tool, "arguments": args}]
    keep = {"resources": ("kind", "path"), "processes": ("sort", "limit"),
            "services": ("state", "unit", "scope"), "packages": ("operation", "target")}[tool]
    return [{"name": tool, "arguments": {k: a[k] for k in keep if k in a}}]


def _session(a):
    return f"{a['s_agent']}@{a['s_dir']}" if "s_agent" in a and "s_dir" in a else None


def _agents(tool, a):
    if tool == "agent":
        return {"name": tool, "arguments": {k: a[k] for k in ("agent", "dir", "prompt", "resume", "model", "place")
                                            if k in a}}
    session = _session(a)
    if session is None:
        return None
    if tool == "wait":
        args = {"session": session, "for": a["for"]}
        if "timeout" in a:
            args["timeout"] = a["timeout"] * (60 if a.get("minutes") else 1)
        return {"name": tool, "arguments": args}
    args = {"session": session, "text": a["text"]}
    if a.get("wait"):
        args["wait"] = True
    return {"name": tool, "arguments": args}


def _tmux(tool, a):
    op = tool[len("tmux_"):]
    return {"operation": op, **a}


def _pty(tool, a):
    return {"operation": tool[len("pty_"):], **a}


def _logs(tool, a):
    op = tool[len("logs_"):]
    if op == "source":
        return {"operation": "source", "event_id": a["event"]}
    args = {"operation": op}
    if "pane" in a and "session" in a:
        return None
    if "pane" in a:
        args["session"] = str(a["pane"])
    elif "session" in a:
        args["session"] = a["session"]
    if "file" in a:
        args["file"] = a["file"]
        args["provider"] = a.get("provider", "raw")
    elif "provider" in a:
        return None
    for key in ("query", "limit"):
        if key in a:
            args[key] = a[key]
    return args


def to_job(job: str, calls: list) -> list | None:
    """The bundle's calls as the job's own: a list of {"name", "arguments"} for
    files, system, agents and apps; a list of one operation dict for tmux, pty
    and logs. None when a reading cannot be written as the job's call."""
    if job == "panes":
        return calls
    out = []
    for call in calls:
        tool, args = call["name"], dict(call["arguments"])
        if tool not in SCHEMAS[job]:
            return None
        try:
            if job == "files":
                got = _files(tool, args)
            elif job == "system":
                got = _system(tool, args)
            elif job == "agents":
                got = _agents(tool, args)
            elif job == "apps":
                got = {"name": tool, "arguments": args}
            elif job == "tmux":
                got = _tmux(tool, args)
            elif job == "pty":
                got = _pty(tool, args)
            else:
                got = _logs(tool, args)
        except (KeyError, TypeError, ValueError):
            return None
        if got is None:
            return None
        out.extend(got if isinstance(got, list) else [got])
    return out
