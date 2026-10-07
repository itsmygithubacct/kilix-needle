"""Structured Kilix actions: exact targets, no model or language parsing.

    kilix-needle action --dry-run --request-json - < action.json
    kilix-needle action --yes --request-json - < action.json
    kilix-needle action --request-json - < status.json

The JSON schema is kilix.actions/v1. A mutation needs --yes or interactive
consent; operation.status is read-only and needs no consent. Reuse an operation
ID to query its receipt; submission never proves agent acknowledgment or task
completion. timeout is in seconds (1–60, default 15); omit it for routine calls.
With --request-json -, pipe or redirect JSON in the same invocation.
See docs/actions.md for complete canonical requests.
"""
from __future__ import annotations

import argparse
import json
import sys

import action_backend

IDENTITY_SCHEMA = {"type": "object", "properties": {
    "pane_id": {"type": "integer", "minimum": 1},
    "broker": {"type": "string", "pattern": "^[0-9a-f]{16,64}$"}},
    "required": ["pane_id", "broker"], "additionalProperties": False}

_STR = {"type": "string"}
PARAM_PROPERTIES = {
    "argv": {"type": "array", "items": _STR, "minItems": 1, "maxItems": 64},
    "agent": {"type": "string", "enum": ["codex", "claude", "kimi", "grok", "qwen-omp"]},
    "cwd": {"type": "string", "description": "existing absolute directory"},
    "title": {"type": "string", "maxLength": 200},
    "placement": {"type": "string", "enum": ["split", "new-tab"]},
    "direction": {"type": "string", "enum": ["right", "left", "up", "down"]},
    "bias": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 100},
    "model": _STR, "prompt": _STR, "resume": _STR,
    "reasoning_effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh"],
        "description": "Codex agent.launch only; preserve the requested effort. Model support is checked by the client."},
    "coding_yolo": {"type": "boolean"}, "trust_folder": {"const": False,
        "description": "set trust explicitly through agent-control before this launch"},
    "agent_arg": {"type": "array", "items": _STR},
    "text": {"type": "string", "maxLength": 900, "description": "literal single-line message"},
    "mode": {"type": "string", "enum": ["steer", "defer"]},
}


def request_schema(*, status=False) -> dict:
    operations = ["operation.status"] if status else list(action_backend.OPERATIONS[:-1])
    result = {"type": "object", "properties": {
        "schema": {"const": action_backend.SCHEMA},
        "operation_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"},
        "operation": {"type": "string", "enum": operations},
        "source": IDENTITY_SCHEMA, "target": IDENTITY_SCHEMA,
        "params": {"type": "object", "properties": {} if status else PARAM_PROPERTIES,
                   "additionalProperties": False},
        "timeout": {"type": "number", "minimum": 1, "maximum": 60, "default": 15,
                    "description": "Seconds, not milliseconds. Omit to use 15 seconds."},
        "dry_run": {"type": "boolean", "default": False}},
        "required": ["schema", "operation_id", "operation", "source", "target", "params"],
        "additionalProperties": False}
    if not status:
        result["allOf"] = [
            {"if": {"properties": {"operation": {"const": operation}}},
             "then": {"properties": {"params": {"required": required}}}}
            for operation, required in (
                ("pane.open", ["argv", "cwd", "title", "placement"]),
                ("agent.launch", ["agent", "cwd", "title", "placement"]),
                ("agent.deliver", ["text"]))]
    return result


def run(request, *, dry_run=False, assume_yes=False, agent=False, confirm=None, backend=None) -> dict:
    try:
        if any(type(value) is not bool for value in (dry_run, assume_yes, agent)):
            raise ValueError("action options must be booleans")
        request = action_backend.validate(request)
        if dry_run:
            request["dry_run"] = True
            request = action_backend.validate(request)
    except (ValueError, TypeError) as exc:
        return action_backend.error(request, str(exc))
    if request["operation"] != "operation.status" and not request["dry_run"] and not assume_yes:
        target = request["target"]
        question = f"{request['operation']} on pane {target['pane_id']} broker {target['broker']}? [y/N] "
        if agent or confirm is None or not confirm(question):
            return action_backend.error(request, "needs --yes or confirm_risky=true; no action performed")
    try:
        return action_backend.checked((backend or action_backend.dispatch)(request), request)
    except (ValueError, OSError, TypeError) as exc:
        return action_backend.error(request, f"invalid action result: {exc}",
                                    uncertain=not request["dry_run"] and request["operation"] != "operation.status")


def mcp(arguments, *, plan=False, status=False) -> dict:
    allowed = {"request"} | (set() if plan or status else {"confirm_risky"})
    if (not isinstance(arguments, dict) or set(arguments) - allowed
            or not isinstance(arguments.get("request"), dict)
            or type(arguments.get("confirm_risky", False)) is not bool):
        raise ValueError("action tools require a structured request and optional act confirm_risky")
    operation = arguments["request"].get("operation")
    if status != (operation == "operation.status"):
        raise ValueError("use kilix_action_status for operation.status; plan/act accept mutations")
    return run(arguments["request"], dry_run=plan,
               assume_yes=arguments.get("confirm_risky", False), agent=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kilix-needle action", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--request-json", required=True, metavar="JSON|-", help="literal JSON object, or - to read stdin")
    parser.add_argument("--dry-run", action="store_true", help="validate and resolve; mutate nothing")
    parser.add_argument("--yes", action="store_true", help="explicitly authorize this structured mutation")
    parser.add_argument("--agent", action="store_true", help="never prompt")
    parser.add_argument("--json", action="store_true", help="JSON is always emitted; accepted for CLI consistency")
    args = parser.parse_args(argv)

    def confirm(question):
        # JSON stdin must never also be used for interactive confirmation.
        if args.request_json == "-" or not sys.stdin.isatty():
            return False
        print(question, end="", file=sys.stderr, flush=True)
        return sys.stdin.readline(16).strip().casefold() in ("y", "yes")

    request = None
    try:
        raw = sys.stdin.read(action_backend.MAX_REQUEST + 1) if args.request_json == "-" else args.request_json
        if len(raw.encode()) > action_backend.MAX_REQUEST:
            raise ValueError("action request exceeds 16384 bytes")
        if not raw.strip():
            raise ValueError("empty request; pipe JSON or redirect a file into --request-json - in the same invocation")
        request = json.loads(raw, object_pairs_hook=action_backend.unique_object)
        receipt = run(request, dry_run=args.dry_run, assume_yes=args.yes, agent=args.agent, confirm=confirm)
    except (ValueError, UnicodeError) as exc:
        receipt = action_backend.error(request, f"invalid request JSON: {exc}")
    except KeyboardInterrupt:
        receipt = action_backend.error(request, "interrupted; mutation and completion unknown; query operation.status", uncertain=True)
    print(json.dumps(receipt, ensure_ascii=True, separators=(",", ":")))
    return action_backend.exit_status(receipt)
