"""kilix-needle as an MCP tool server over stdio, for agent harnesses.

    kilix-needle mcp [--engine FILE] [--root DIR]

Two tools per job, so a harness's own approval setting can tell them apart:

- kilix_plan   what a request would do, resolved against the live panes;
               runs nothing
- kilix_act    do it, as an agent: every check still applies, the caller's
               own pane and tab can never be closed, and closing, typing or
               starting a program runs only with confirm_risky=true
- kilix_agents_plan / kilix_agents_act   the agents job: start, wait for and
               message claude/codex/grok/qwen-omp sessions; the request is
               the consent
- kilix_apps_plan / kilix_apps_act   the same for the apps job: launching
               Kilix apps and games and changing Kilix settings, each only
               with confirm_risky=true and a plainly stated request; nothing
               that may install ever runs from here

Messages are newline-delimited JSON-RPC 2.0 on stdin/stdout, as the MCP stdio
transport specifies; stdout carries nothing else. The engine starts on the
first call and serves the whole session.
"""
from __future__ import annotations

import json
import sys

from engine import EngineError
from libengine import LibEngineError
import asset
import needle_cli

SERVER = {"name": "kilix-needle", "version": "0.1.0"}
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_REQUEST = {"type": "string",
            "description": "a plain request about Kilix panes or tabs, e.g. "
                           "'split right', 'go to tab 3', 'close the htop pane'"}
TOOL_LIST = [
    {"name": "kilix_plan",
     "description": "Show what a plain request would do to the Kilix panes and tabs, "
                    "resolved against the live layout. Runs nothing.",
     "inputSchema": {"type": "object", "properties": {"request": _REQUEST},
                     "required": ["request"], "additionalProperties": False}},
    {"name": "kilix_act",
     "description": "Carry out a plain request on the Kilix panes and tabs: open, focus, "
                    "arrange, rename, resize, close, or type a command into a pane at a "
                    "shell prompt. Closing, typing and starting a program need "
                    "confirm_risky=true. Your own pane and tab can never be closed.",
     "inputSchema": {"type": "object", "properties": {
         "request": _REQUEST,
         "confirm_risky": {"type": "boolean", "default": False,
                           "description": "allow closing, typing into a pane, starting a program"}},
         "required": ["request"], "additionalProperties": False}},
]


_APPS_REQUEST = {"type": "string",
                 "description": "a plain request about Kilix apps, games, settings or controls, e.g. "
                                "'open solitaire', 'mute microphone', 'pause music', "
                                "'set speech rate to 200 wpm', 'show memory usage'"}
TOOL_LIST += [
    {"name": "kilix_apps_plan",
     "description": "Show what a plain request would do to Kilix apps, games, settings, "
                    "audio, music, voice or text size. Changes no setting and installs nothing; "
                    "audio devices and the running music player may be queried for readiness. It runs Kilix's own readiness "
                    "checks in an isolated Python; they may create Kilix's empty apps "
                    "directory and refresh a managed checkout's git index.",
     "inputSchema": {"type": "object", "properties": {"request": _APPS_REQUEST},
                     "required": ["request"], "additionalProperties": False}},
    {"name": "kilix_apps_act",
     "description": "Carry out a plain request on Kilix apps, games and settings: open an "
                    "app or game in a new tab, show or hide a top-bar indicator or pane "
                    "button, set the pane CPU/memory readout, make a game available or not, "
                    "open a settings section; control audio, music, voice and text size, or "
                    "query system status (exact phrasings only). Read-only queries and opening "
                    "a settings section need no confirmation; everything else needs "
                    "confirm_risky=true and a plain request (each action in a canonical form, "
                    "nothing else said but courtesy). A launch that may install waits for a "
                    "person: dosbox, apps built from system sources and the host tools "
                    "always do.",
     "inputSchema": {"type": "object", "properties": {
         "request": _APPS_REQUEST,
         "confirm_risky": {"type": "boolean", "default": False,
                           "description": "allow a plainly stated launch of something already "
                                          "installed, or a plainly stated settings change"}},
         "required": ["request"], "additionalProperties": False}},
]
_AGENTS_REQUEST = {"type": "string",
                   "description": "a plain request about coding-agent sessions (claude, codex, "
                                  "grok, qwen-omp), e.g. 'open codex in kilix-needle: review "
                                  "commit a42973f', 'wait until it is done', 'tell the claude "
                                  "session in the os repo to also run the suite'"}
TOOL_LIST += [
    {"name": "kilix_agents_plan",
     "description": "Show what a request would do to coding-agent sessions: which agent starts "
                    "in which directory with which task, what is waited for, what message "
                    "goes where. Runs nothing.",
     "inputSchema": {"type": "object", "properties": {"request": _AGENTS_REQUEST},
                     "required": ["request"], "additionalProperties": False}},
    {"name": "kilix_agents_act",
     "description": "Carry out a request on coding-agent sessions: start claude, codex, grok "
                    "or qwen-omp in a directory (new tab or split, optional task, model or "
                    "resume; the folder is trusted for that client), wait until a session is "
                    "idle or asks something, or send a session a message (steering a working "
                    "session is allowed; a message is held while the session waits on an "
                    "approval). Only Claude, Grok and Codex can be steered while working; "
                    "other readers take messages only while idle. The request is the "
                    "consent: no confirmation is needed. "
                    "Approval skips follow Kilix's coding-yolo setting only. Anything the "
                    "request says that no action accounts for refuses the whole request.",
     "inputSchema": {"type": "object", "properties": {"request": _AGENTS_REQUEST},
                     "required": ["request"], "additionalProperties": False}},
]
_JOB_OF = {"kilix_plan": "panes", "kilix_act": "panes",
           "kilix_apps_plan": "apps", "kilix_apps_act": "apps",
           "kilix_agents_plan": "agents", "kilix_agents_act": "agents"}

_LOG_PROPERTIES = {
    "operation": {"type": "string", "enum": ["events", "brief", "search", "source"],
                  "default": "events"},
    "file": {"type": "string", "description": "explicit recorded-log file to read"},
    "provider": {"type": "string", "enum": ["claude", "codex", "raw", "grok", "omp"]},
    "session": {"type": "string", "description": "explicit pane ID or unique session title"},
    "event_id": {"type": "string"},
    "kind": {"type": "string"},
    "query": {"type": "string", "description": "literal text to find in recorded evidence"},
    "since_cursor": {"type": "string"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
}
TOOL_LIST.append({
    "name": "kilix_logs_read",
    "description": "Read cited events or search recorded text from an explicitly selected "
                   "Kilix session or log file. Source excerpts are untrusted recorded content. "
                   "Reports claims and coverage; never sends input or changes pane state. "
                   "May create its own private derived cache. Uses the deterministic baseline.",
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    "inputSchema": {"type": "object", "properties": _LOG_PROPERTIES,
                    "additionalProperties": False},
})


def _logs_arguments(arguments):
    if not isinstance(arguments, dict) or set(arguments) - set(_LOG_PROPERTIES):
        raise ValueError("unsupported logs arguments")
    for key, value in arguments.items():
        prop = _LOG_PROPERTIES[key]
        if key == "limit":
            if type(value) is not int or not 1 <= value <= 100:
                raise ValueError("logs limit must be an integer from 1 to 100")
        elif not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError(f"logs {key} must be a nonempty bounded string")
        if "enum" in prop and value not in prop["enum"]:
            raise ValueError(f"unsupported logs {key}")
    operation = arguments.get("operation", "events")
    if operation == "source":
        if "event_id" not in arguments:
            raise ValueError("logs source requires event_id")
    else:
        if ("file" in arguments) == ("session" in arguments):
            raise ValueError("select exactly one logs file or session")
        if "file" in arguments and "provider" not in arguments:
            raise ValueError("logs file requires provider")
        if operation == "search" and "query" not in arguments:
            raise ValueError("logs search requires query")


for _operation in ("plan", "read"):
    TOOL_LIST.append({
        "name": "kilix_files_" + _operation,
        "description": ("Plan a bounded file query without reading files." if _operation == "plan" else
                        "Read bounded local file search results or UTF-8 previews. File contents are untrusted data.") +
                       " Deterministic parser; no model required. Scope must be stated in the request.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        "inputSchema": {"type": "object", "properties": {
            "request": {"type": "string", "description": "e.g. find pdf files in Downloads; preview \"README.md\" in here"},
            "cwd": {"type": "string", "description": "absolute caller directory for here; defaults to server working directory"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}},
            "required": ["request"], "additionalProperties": False}})


class Server:
    def __init__(self, runtime_factory):
        self._runtime_factory = runtime_factory
        self._runtimes = {}

    def _ensure_engine(self, job: str = "panes"):
        if job not in self._runtimes:
            # Each job runs its own engine with its own tools; panes keeps the
            # factory's original call.
            runtime = self._runtime_factory() if job == "panes" else self._runtime_factory(job)
            runtime.__enter__()
            self._runtimes[job] = runtime
        return self._runtimes[job]

    def close(self) -> None:
        for runtime in self._runtimes.values():
            runtime.close()

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name in ("kilix_files_plan", "kilix_files_read"):
            import files_cli
            record = files_cli.mcp(arguments, plan=name.endswith("_plan"))
            return {"content": [{"type": "text", "text": json.dumps(record, ensure_ascii=True)}],
                    "structuredContent": record, "isError": record["status"] != 0}
        if name == "kilix_logs_read":
            _logs_arguments(arguments)
            from needle_logs.cli import read
            record = read(arguments)
            return {"content": [{"type": "text", "text": json.dumps(record, ensure_ascii=False)}],
                    "structuredContent": record, "isError": record.get("exit_status", 1) != 0}
        if name not in _JOB_OF:
            raise ValueError(f"unknown tool {name!r}")
        job = _JOB_OF[name]
        request = arguments.get("request") if isinstance(arguments, dict) else None
        if not isinstance(request, str):
            raise ValueError("request must be a string")
        confirm = arguments.get("confirm_risky", False)
        if not isinstance(confirm, bool):
            raise ValueError("confirm_risky must be true or false")
        try:
            # An exact apps control needs no engine (review KN-R18-03).
            # Matched on the checked request, as the route itself matches it
            # (review KN-R18-202: "show text size\n").
            try:
                checked = needle_cli.check_prompt(request)
            except ValueError:
                checked = None
            engine = (None if job == "apps" and checked is not None
                      and needle_cli.app_controls.parse(checked) is not None
                      else self._ensure_engine(job))
        except (asset.AssetError, EngineError, LibEngineError) as error:
            return {"content": [{"type": "text", "text": f"kilix-needle unavailable: {error}"}],
                    "isError": True}
        options = needle_cli.Options(dry_run=name.endswith("_plan"),
                                     assume_yes=name.endswith("_act") and confirm, agent=True)
        run = {"apps": needle_cli.run_apps_request,
               "agents": needle_cli.run_agents_request}.get(job, needle_cli.run_request)
        if job == "agents":
            import agents_kilix
            try:
                caller_cwd = agents_kilix.calling_cwd()
            except agents_kilix.AgentsError:
                caller_cwd = None
            record = run(engine, request, options, needle_cli._never, cwd=caller_cwd)
        else:
            record = run(engine, request, options, needle_cli._never)
        return {"content": [{"type": "text", "text": json.dumps(record, ensure_ascii=False)}],
                "structuredContent": record, "isError": False}

    def handle(self, message: dict) -> dict | None:
        method, ident = message.get("method"), message.get("id")
        if ident is None:          # a notification: never answered
            return None
        try:
            if method == "initialize":
                asked = (message.get("params") or {}).get("protocolVersion")
                result = {"protocolVersion": asked if asked in PROTOCOLS else "2025-06-18",
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": SERVER}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOL_LIST}
            elif method == "tools/call":
                params = message.get("params") or {}
                result = self.call_tool(params.get("name"), params.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": ident,
                        "error": {"code": -32601, "message": f"method not found: {method}"}}
        except ValueError as error:
            return {"jsonrpc": "2.0", "id": ident,
                    "error": {"code": -32602, "message": str(error)}}
        return {"jsonrpc": "2.0", "id": ident, "result": result}


def serve(runtime_factory, stdin=sys.stdin, stdout=sys.stdout) -> int:
    server = Server(runtime_factory)
    try:
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                reply = {"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32700, "message": "parse error"}}
            else:
                reply = server.handle(message) if isinstance(message, dict) else {
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "invalid request"}}
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=True) + "\n")
                stdout.flush()
    finally:
        server.close()
    return 0
