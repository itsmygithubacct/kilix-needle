"""Versioned, model-independent Kilix domain contract and JSONL bridge.

`contract` exports the schemas. `bridge` validates by default without contacting
Kilix; --mode plan resolves and --mode execute performs admitted calls. Every
input carries the contract digest saved with the corpus/model, preventing an
old model from silently running against changed checks.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unicodedata

from actions import TOOLS, Action, Refusal, interpret

VERSION = "kilix-panes-tools/v2"
MAX_REQUEST_BYTES = 4096
MAX_CALLS = 32
MAX_LINE = 65536


def check_request(request: str) -> str:
    if not isinstance(request, str) or not request.strip():
        raise ValueError("request must be a nonempty string")
    if len(request.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds 4096 bytes")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in request):
        raise ValueError("request contains control characters")
    return request.strip()


def contract() -> dict:
    tools = deepcopy(TOOLS)
    for tool in tools:
        tool["parameters"]["additionalProperties"] = False
        for key, spec in tool["parameters"]["properties"].items():
            if spec.get("type") == "string" and "enum" not in spec:
                spec["x-source"] = "input" if key in ("program", "name", "command") else "input-or-canonical"
    root = Path(__file__).resolve().parent
    record = {
        "version": VERSION,
        "tools": tools,
        "defaults": {"maximize_pane": {"pane": "current", "restore": False},
                     "rename_pane": {"pane": "current"}, "resize_pane": {"amount": 2}},
        "constraints": {"move_tab": "exactly one of direction and position",
                        "target": "current is the caller; named targets normalize to name:<value>",
                        "commands": "exact case-sensitive request span; quotes are wrappers",
                        "pronouns": "close it is refused; explicit targets required"},
        "confirmation": {"always": ["close_pane", "close_tab", "run_in_pane"],
                         "with_program": ["open_pane", "open_tab"],
                         "partial_refusal": "requires human confirmation even with --yes"},
        "limits": {"request_bytes": MAX_REQUEST_BYTES, "calls": MAX_CALLS},
        "implementation_sha256": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("actions.py", "kilix.py", "needle_cli.py", "domain_bridge.py")},
    }
    record["sha256"] = hashlib.sha256(json.dumps(record, sort_keys=True,
                                                 separators=(",", ":")).encode()).hexdigest()
    return record


def validate(request: str, calls: list) -> dict:
    request = check_request(request)
    if not isinstance(calls, list) or len(calls) > MAX_CALLS:
        raise ValueError("function_calls must be a list of at most 32 calls")
    results = interpret(request, calls)
    refusals = [{"kind": r.kind, "reason": r.reason} for r in results if isinstance(r, Refusal)]
    return {"request": request, "status": int(bool(refusals)),
            "actions": [[r.kind, r.args] for r in results if isinstance(r, Action)],
            "refusals": refusals}


def process(envelope: dict, *, mode: str = "validate", assume_yes: bool = False,
            agent: bool = False, under_overlay: bool = False) -> dict:
    if not isinstance(envelope, dict):
        raise ValueError("input must be an object")
    if envelope.get("contract_sha256") != contract()["sha256"]:
        raise ValueError("contract_sha256 does not match; revalidate the corpus/model before use")
    if "function_calls" not in envelope:
        raise ValueError("function_calls is required (use [] for a refusal)")
    checked = validate(envelope.get("request"), envelope["function_calls"])
    if mode == "validate":
        return checked
    if mode not in ("plan", "execute"):
        raise ValueError("unknown bridge mode")
    # Lazy import: corpus validation has no dependency on an engine, asset or desktop.
    from needle_cli import Options, _never, run_calls
    return run_calls(checked["request"], envelope["function_calls"],
                     Options(dry_run=mode == "plan", assume_yes=assume_yes,
                             agent=agent, under_overlay=under_overlay), _never)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("contract", "bridge"))
    parser.add_argument("--mode", choices=("validate", "plan", "execute"), default="validate")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--agent", action="store_true")
    parser.add_argument("--under-overlay", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "contract":
        print(json.dumps(contract(), indent=2, ensure_ascii=False))
        return 0
    status = 0
    while True:
        line = sys.stdin.readline(MAX_LINE + 1)
        if not line:
            break
        if len(line) > MAX_LINE:
            print(json.dumps({"status": 1, "error": "input line exceeds 65536 characters"}))
            return 1
        try:
            result = process(json.loads(line), mode=args.mode, assume_yes=args.yes,
                             agent=args.agent, under_overlay=args.under_overlay)
        except (ValueError, TypeError) as error:
            result = {"status": 1, "error": str(error)}
        print(json.dumps(result, ensure_ascii=False), flush=True)
        status |= result["status"]
    return status


if __name__ == "__main__":
    sys.exit(main())
