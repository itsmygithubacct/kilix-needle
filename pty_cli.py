"""Deterministic requests about persistent pane sessions, through `kilix pty --json`.

    kilix-needle pty 'list sessions'
    kilix-needle pty 'show session 3fa9c2d41b7e6a05'
    kilix-needle pty 'which session is pane 12'
    kilix-needle pty 'show the last 50 lines of session 3fa9c2d41b7e6a05'
    kilix-needle pty 'show archived journal 3fa9c2d41b7e6a05'
    kilix-needle pty --dry-run 'end session 3fa9c2d41b7e6a05'
    kilix-needle pty --yes 'end session 3fa9c2d41b7e6a05'

One operation per request; anything else refuses whole, with `hint`. Ending a
session takes its exact full ID and a yes (`--yes`, or MCP's confirm_risky),
never the caller's own session, and runs only when the caller's own session is
known. No model is loaded.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pty_backend
import pty_job

OWN = "KITTY_PTY_BROKER_SESSION"
CONSENT = "needs --yes or confirm_risky=true; no session was ended"


def _caller(env) -> str | None:
    """The caller's own broker session, or None when it cannot be identified."""
    value = env.get(OWN)
    return value if pty_job.valid_id(value) else None


def _refuse(record: dict, status: int, reason: str, note: str, hint: str) -> dict:
    record.update(status=status, refused=reason, note=note, hint=hint)
    return record


def _failed(record: dict, reply) -> dict:
    note = reply.error
    if reply.kind in ("unavailable", "too_old"):
        note += ". kilix pty needs Kilix 0.2.2-rc6 or newer: run `kilix pty help` to check"
    record.update(status=1 if reply.kind in ("timeout", "failed") else 2, note=note)
    return record


def _hint(record: dict, action: dict | None) -> None:
    """Every refusal and recovery record names one request the grammar accepts.

    Explanations (install Kilix, identity, consent, re-list before retrying) are the
    note's; the hint is only ever a request that `pty_job.parse` reads.
    """
    if record.get("status") == 0 or "hint" in record:
        return
    ident = (action or {}).get("id")
    gone = isinstance(record.get("result"), dict) and record["result"].get("result") == "not_found"
    record["hint"] = (pty_job.show_request(ident) if ident and not gone and action["operation"] == "kill"
                      else pty_job.HINTS["list"])


def _answer(record: dict, action: dict, reply, step: str = "run") -> dict | None:
    """Fold one reply into the record; return the checked document or None."""
    if reply.kind != "ok":
        _failed(record, reply)
        if step == "kill" and reply.kind in ("timeout", "failed"):
            # The request may have reached the broker: an unanswered kill is not "nothing happened".
            record.update(completion="unknown", hint=pty_job.show_request(action["id"]))
            record["note"] += ". The request may have reached the broker: re-list before retrying"
        return None
    try:
        return pty_backend.checked(action, reply.document, reply.returncode, step)
    except ValueError as exc:
        record.update(status=1, note=f"kilix pty returned an unexpected document: {exc}")
        if action["operation"] == "kill" and step == "kill":
            record.update(completion="unknown", hint=pty_job.show_request(action["id"]))
            record["note"] += ". The request may have reached the broker: re-list before retrying"
        return None


def _summary(session: dict) -> dict:
    keys = ("id", "started_millis", "command", "cwd", "cwd_now", "attached", "child_pid", "reachable")
    return {key: session[key] for key in keys if key in session}


def _kill(record, action, *, dry_run, assume_yes, agent, confirm, call, env) -> dict:
    ident = action["id"]
    caller = _caller(env)
    if caller is None:
        return _refuse(record, 3, "caller_unidentified",
                       f"the caller's own session is unknown ({OWN} is not set): "
                       "nothing is ended when the caller cannot be identified. A person outside a "
                       "Kilix pane ends a session with `kilix pty kill ID --yes`", pty_job.HINTS["list"])
    if caller == ident:
        return _refuse(record, 3, "own_session",
                       "that is this pane's own session; ending it would end this program. "
                       "Run it from another pane", pty_job.HINTS["list"])
    if not dry_run and not assume_yes and (agent or confirm is None):
        record.update(status=1, note=CONSENT, hint=f"end session {pty_job.id_literal(ident)}")
        return record
    # The lookup in this same call supplies the session's identity and start time.
    status_argv = pty_backend.argv_for(action, "status")
    document = _answer(record, action, call(status_argv, wall=None), "status")
    if document is None:
        record["note"] += " (the lookup failed; nothing was sent)"
        return record
    if document.get("result") == "not_found":
        record.update(result=document, status=4)
        return record
    session = document["session"]
    started = session.get("started_millis")
    if not pty_backend._integer(started):
        record.update(status=1, note="the status read has no started_millis to bind the kill to; "
                                     "nothing was sent")
        return record
    action = {**action, "expect": started}
    kill_argv = pty_backend.argv_for(action, "kill")
    record["resolved"] = _summary(session)
    if dry_run:
        record.update(status=0, plan={"would": "end", "argv": kill_argv,
                                      "needs": "--yes or confirm_risky=true"})
        return record
    if not assume_yes:
        question = (f"end session {ident} (started {started}, "
                    f"{'attached' if session.get('attached') else 'detached'}, "
                    f"{json.dumps(str(session.get('command', ''))[:120], ensure_ascii=True)})? [y/N] ")
        if not confirm(question):
            return _refuse(record, 3, "declined", "not ended: declined", pty_job.show_request(ident))
    reply = call(kill_argv, wall=None)
    document = _answer(record, action, reply, "kill")
    if document is not None:
        record.update(result=document, status=reply.returncode)
    return record


def run(request, **kwargs) -> dict:
    held = {}
    record = _run(request, held, **kwargs)
    _hint(record, held.get("action"))
    return record


def _run(request, held, *, dry_run=False, assume_yes=False, agent=False, confirm=None,
         timeout_seconds=None, reads_only=False, backend=None, environ=None) -> dict:
    record = {"request": request, "job": "pty", "status": 2}
    env = os.environ if environ is None else environ
    try:
        if any(type(value) is not bool for value in (dry_run, assume_yes, agent, reads_only)):
            raise ValueError("pty dry_run, assume_yes and agent must be booleans")
    except ValueError as exc:
        record.update(note=str(exc), hint=pty_job.HINTS["list"])
        return record
    try:
        action = pty_job.structured(request) if isinstance(request, dict) else pty_job.parse(request)
        if timeout_seconds is not None:
            if "timeout_seconds" in action:
                raise pty_job.Refused("timeout_seconds is set twice", pty_job.HINTS["list"])
            action["timeout_seconds"] = pty_job._number(timeout_seconds, "timeout_seconds",
                                                        pty_job.MIN_TIMEOUT, pty_job.MAX_TIMEOUT,
                                                        "in seconds (not milliseconds)")
    except pty_job.Refused as exc:
        record.update(note=str(exc), hint=exc.hint)
        return record
    except ValueError as exc:
        record.update(note=str(exc), hint=pty_job.HINTS["list"])
        return record
    held["action"] = action
    operation = action["operation"]
    record["operation"] = operation
    if reads_only and operation == "kill":
        record.update(note="this tool only reads; ending a session is kilix_pty_act with "
                           "confirm_risky=true", hint=pty_job.HINTS["kill"])
        return record
    if dry_run:
        record["dry_run"] = True
    call = backend or pty_backend.call
    if operation == "kill":
        return _kill(record, action, dry_run=dry_run, assume_yes=assume_yes, agent=agent,
                     confirm=confirm, call=call, env=env)
    argv = pty_backend.argv_for(action)
    if dry_run:
        record.update(status=0, plan={"would": "run", "argv": argv})
        return record
    reply = call(argv, wall=None)
    document = _answer(record, action, reply)
    if document is not None:
        record.update(result=document, status=reply.returncode)
    return record


def render(record: dict) -> str:
    if "plan" in record:
        text = json.dumps({key: record[key] for key in ("operation", "resolved", "plan") if key in record},
                          ensure_ascii=True, indent=2)
    elif "result" in record:
        # JSON keeps observed text as data and escapes terminal control sequences.
        text = json.dumps(record["result"], ensure_ascii=True, indent=2)
    else:
        text = record.get("note", "")
    if record.get("hint") and record.get("status"):
        text += f"\nhint: {record['hint']}"
    return text


def mcp(arguments, *, plan=False, read=False) -> dict:
    allowed = {"request"} | (set() if plan or read else {"confirm_risky"})
    if (not isinstance(arguments, dict) or set(arguments) - allowed
            or not isinstance(arguments.get("request"), (str, dict))
            or type(arguments.get("confirm_risky", False)) is not bool):
        raise ValueError("pty tools require request (a string or an object) and, for act, "
                         "an optional boolean confirm_risky")
    return run(arguments["request"], dry_run=plan, assume_yes=arguments.get("confirm_risky", False),
               agent=True, reads_only=read)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kilix-needle pty", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve and show what would run; end nothing")
    parser.add_argument("--yes", action="store_true", help="allow a plain 'end session ID' request")
    parser.add_argument("--agent", action="store_true", help="never prompt for confirmation")
    parser.add_argument("--json", action="store_true", help="print one JSON record")
    parser.add_argument("--timeout-seconds", type=float, metavar="S",
                        help=f"bound each broker call, in seconds, {pty_job.MIN_TIMEOUT:g}-"
                             f"{pty_job.MAX_TIMEOUT:g}")
    parser.add_argument("--request-json", help="a structured request object, or - for JSON on stdin")
    parser.add_argument("request", nargs="*", help=pty_job.USAGE)
    args = parser.parse_args(argv)

    def confirm(question):
        print(question, end="", file=sys.stderr, flush=True)
        return sys.stdin.readline(16).strip().casefold() in ("y", "yes")

    try:
        request = " ".join(args.request)
        if args.request_json is not None:
            if args.request:
                parser.error("choose request text or --request-json, not both")
            try:
                raw = sys.stdin.read(4097) if args.request_json == "-" else args.request_json
                if len(raw) > 4096:
                    raise ValueError("structured pty request exceeds 4096 characters")
                request = json.loads(raw)
            except (ValueError, TypeError, UnicodeError) as error:
                record = {"job": "pty", "status": 2, "note": f"--request-json: {error}",
                          "hint": pty_job.STRUCTURED_HINTS["list"]}
                print(json.dumps(record) if args.json else render(record))
                return 2
        record = run(request, dry_run=args.dry_run, assume_yes=args.yes, agent=args.agent,
                     confirm=confirm if sys.stdin.isatty() else None,
                     timeout_seconds=args.timeout_seconds)
        print(json.dumps(record, ensure_ascii=True) if args.json else render(record))
        return record["status"]
    except KeyboardInterrupt:
        return 130
