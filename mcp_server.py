"""kilix-needle as an MCP tool server over stdio, for agent harnesses.

    kilix-needle mcp [--engine FILE] [--root DIR]

Two tools per job, so a harness's own approval setting can tell them apart:

- kilix_plan   what a request would do, resolved against the live panes;
               runs nothing
- kilix_act    do it, as an agent: every check still applies, the caller's
               own pane and tab can never be closed, and closing, typing or
               starting a program runs only with confirm_risky=true
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
                 "description": "a plain request about Kilix apps, games or settings, e.g. "
                                "'open solitaire', 'hide the clock', 'disable doom'"}
TOOL_LIST += [
    {"name": "kilix_apps_plan",
     "description": "Show what a plain request would do to Kilix apps, games and settings. "
                    "Changes nothing; it runs Kilix's own readiness checks in an isolated "
                    "Python, which at most creates Kilix's empty apps directory.",
     "inputSchema": {"type": "object", "properties": {"request": _APPS_REQUEST},
                     "required": ["request"], "additionalProperties": False}},
    {"name": "kilix_apps_act",
     "description": "Carry out a plain request on Kilix apps, games and settings: open an "
                    "app or game in a new tab, show or hide a top-bar indicator or pane "
                    "button, set the pane CPU/memory readout, make a game available or not, "
                    "open a settings section. Everything but opening a settings section needs "
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
_JOB_OF = {"kilix_plan": "panes", "kilix_act": "panes",
           "kilix_apps_plan": "apps", "kilix_apps_act": "apps"}


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
            engine = self._ensure_engine(job)
        except (asset.AssetError, EngineError, LibEngineError) as error:
            return {"content": [{"type": "text", "text": f"kilix-needle unavailable: {error}"}],
                    "isError": True}
        options = needle_cli.Options(dry_run=name.endswith("_plan"),
                                     assume_yes=name.endswith("_act") and confirm, agent=True)
        run = needle_cli.run_apps_request if job == "apps" else needle_cli.run_request
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
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()
    finally:
        server.close()
    return 0
