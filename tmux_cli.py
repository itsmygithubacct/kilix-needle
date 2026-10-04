"""Deterministic tmux session requests on an explicit private socket.

    kilix-needle tmux --socket /abs/socket 'list sessions'
    kilix-needle tmux --socket /abs/socket 'new session build in /abs/project'
    kilix-needle tmux --socket /abs/socket --yes 'type "make test" in build'
    kilix-needle tmux --socket /abs/socket 'read build last 80 lines'

The shared kilix-tmux controller resolves exact names/IDs and refuses ambiguous
session I/O. New sessions start tmux's configured shell. No model is loaded.
"""
from __future__ import annotations

import argparse
import json
import sys

import tmux_backend
import tmux_job


def run(request: str | dict, *, socket: str, dry_run=False, assume_yes=False, agent=False, confirm=None,
        backend=None) -> dict:
    record = {"request": request, "job": "tmux", "status": 2}
    try:
        if any(type(value) is not bool for value in (dry_run, assume_yes, agent)):
            raise ValueError("tmux dry_run, assume_yes and agent must be booleans")
        socket = tmux_job.socket_path(socket)
        action = tmux_job.literal_request(request) if isinstance(request, dict) else tmux_job.parse(request)
    except ValueError as exc:
        record.update(note=str(exc), hint=tmux_job.USAGE)
        return record
    payload = {"schema": "kilix.tmux.request/v1", "socket": socket, "dry_run": dry_run, **action}
    if not dry_run and action["operation"] in tmux_job.RISKY and not assume_yes:
        if agent or confirm is None or not confirm(f"{action['operation']} on {action['target']} at {socket}? [y/N] "):
            record.update(status=1, note="needs --yes or confirm_risky=true; no tmux operation performed")
            return record
    result = (backend or tmux_backend.dispatch)(payload)
    try:
        result = tmux_backend.checked(result, payload)
    except ValueError as exc:
        result = tmux_backend.error("ETMUX", str(exc))
    record.update(result=result, status=0 if result["ok"] else result["exit"])
    return record


def render(record: dict) -> str:
    if "result" not in record:
        return record["note"]
    result = record["result"]
    if not result["ok"]:
        return result["code"] + ": " + json.dumps(result["error"], ensure_ascii=True)
    # JSON retains text as data and escapes terminal control sequences.
    return json.dumps(result["data"], ensure_ascii=True, indent=2)


def mcp(arguments, *, plan=False) -> dict:
    allowed = {"request", "socket"} | (set() if plan else {"confirm_risky"})
    if (not isinstance(arguments, dict) or set(arguments) - allowed
            or not isinstance(arguments.get("request"), (str, dict))
            or not isinstance(arguments.get("socket"), str)
            or type(arguments.get("confirm_risky", False)) is not bool):
        raise ValueError("tmux tools require request, explicit socket and optional act confirm_risky")
    return run(arguments["request"], socket=arguments["socket"], dry_run=plan,
               assume_yes=arguments.get("confirm_risky", False), agent=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kilix-needle tmux", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--socket", required=True, help="explicit absolute private tmux socket")
    parser.add_argument("--dry-run", action="store_true", help="validate and resolve; mutate nothing")
    parser.add_argument("--yes", action="store_true", help="allow a plain send/type/key/close request")
    parser.add_argument("--agent", action="store_true", help="never prompt for confirmation")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("request", nargs="*", help=tmux_job.USAGE)
    parser.add_argument("--request-json", help="literal send/type object, or - for JSON on stdin")
    args = parser.parse_args(argv)

    def confirm(question):
        if not sys.stdin.isatty():
            return False
        print(question, end="", file=sys.stderr, flush=True)
        return sys.stdin.readline(16).strip().casefold() in ("y", "yes")

    try:
        request = " ".join(args.request)
        if args.request_json is not None:
            if args.request:
                parser.error("choose request text or --request-json, not both")
            try:
                limit = 12 * tmux_job.MAX_TEXT + 1024  # includes escaped Unicode pairs
                raw = sys.stdin.read(limit + 1) if args.request_json == "-" else args.request_json
                if len(raw) > limit:
                    raise ValueError("literal JSON exceeds size limit")
                request = tmux_job.literal_request(json.loads(raw))
            except (ValueError, TypeError, UnicodeError) as error:
                record = {"job": "tmux", "status": 2, "note": str(error)}
                print(json.dumps(record) if args.json else render(record))
                return 2
        record = run(request, socket=args.socket, dry_run=args.dry_run,
                     assume_yes=args.yes, agent=args.agent, confirm=confirm)
        print(json.dumps(record, ensure_ascii=True) if args.json else render(record))
        return record["status"]
    except KeyboardInterrupt:
        return 130
