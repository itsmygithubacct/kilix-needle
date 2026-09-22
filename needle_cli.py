"""kilix-needle: drive Kilix panes and tabs from plain requests with Needle 2.

    kilix-needle split right and run htop
    kilix-needle                     # a prompt loop; one request per line
    kilix-needle --dry-run close tab 2

Each request is one Needle turn. Its calls pass the checks in `actions`,
resolve against a fresh `kilix @ ls`, and are printed before anything runs.
Opening, focusing, arranging, renaming and resizing run straight away.
Closing, typing into a pane and starting a program wait for `y`. `--yes`
answers that question for you, for scripts and agent harnesses. It cannot
override a refusal: if any part of a request was refused, the rest waits for a
typed `y` even with `--yes`, and without a terminal nothing runs.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import sys
from typing import Callable

from actions import TOOLS, Action, Refusal, interpret
import asset
from engine import Engine, EngineError, check_prompt
import kilix


@dataclass(frozen=True)
class Options:
    dry_run: bool = False
    assume_yes: bool = False
    # An agent harness is calling: it may never close its own pane or tab, and
    # nothing is ever read from stdin (for MCP, stdin is the protocol stream).
    agent: bool = False
    under_overlay: bool = False


def _terminal_confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    sys.stdout.write(question)
    sys.stdout.flush()
    answer = sys.stdin.readline(16)
    return answer.strip().casefold() in ("y", "yes")


def _never(_question: str) -> bool:
    return False


def run_request(engine: Engine, request: str, options: Options,
                confirm: Callable[[str], bool] = _terminal_confirm) -> dict:
    """One request, as a record: {"request", "status", "note", "items": [...]}.

    status 0 = done or nothing to do, 1 = something was refused, skipped or failed.
    Each item has "outcome": refused | unresolved | would | skipped | done | failed.
    """
    record = {"request": request, "status": 0, "note": "", "items": []}
    items = record["items"]
    try:
        request = check_prompt(request)
    except ValueError as error:
        record.update(status=1, note=str(error))
        return record
    engine.reset()
    reply = engine.complete(request)
    results = interpret(request, reply.get("function_calls") or [])
    if not results:
        record["note"] = "Needle found no pane or tab action in that request."
        return record

    refused = [item for item in results if isinstance(item, Refusal)]
    for item in refused:
        items.append({"kind": item.kind, "outcome": "refused", "reason": item.reason})
    actions = [item for item in results if isinstance(item, Action)]
    hold = bool(refused)
    if refused:
        record["status"] = 1
    if hold and actions:
        record["note"] = "part of the request was refused, so nothing runs without a yes"

    for action in actions:
        entry = {"kind": action.kind, "args": dict(action.args)}
        items.append(entry)
        try:
            tree = kilix.snapshot(under_overlay=options.under_overlay)
            step = kilix.resolve(action, tree)
        except kilix.KilixError as error:
            entry.update(outcome="unresolved", reason=str(error))
            record["status"] = 1
            continue
        entry["summary"] = step.summary
        if options.agent and tree.caller is not None and tree.caller.get("id") in step.closes:
            entry.update(outcome="refused",
                         reason="an agent may not close its own pane or the tab it is in")
            record["status"] = 1
            continue
        if options.dry_run:
            entry["outcome"] = "would"
            continue
        # --yes answers for a risky action; a partly refused request still needs a
        # person, because what survived may be the wrong half of a misreading.
        needs_yes = hold or action.risky
        if needs_yes and not (options.assume_yes and not hold) \
                and not confirm(f"  {step.summary}? [y/N] "):
            entry["outcome"] = "skipped"
            entry["reason"] = ("declined" if confirm is _terminal_confirm and sys.stdin.isatty()
                               else "needs a yes and there is no one to ask")
            record["status"] = 1
            continue
        try:
            kilix.perform(step)
        except kilix.KilixError as error:
            entry.update(outcome="failed", reason=str(error))
            record["status"] = 1
            return record
        entry["outcome"] = "done"
    return record


def render(record: dict) -> str:
    """The human form of a request record."""
    lines = []
    items = record["items"]
    if not items and record["note"]:
        prefix = "kilix-needle: " if record["status"] else ""
        return prefix + record["note"]
    for item in items:
        if item["outcome"] == "refused" and "summary" not in item:
            lines.append(f"  refused {item['kind']}: {item['reason']}")
    if record["note"]:
        lines.append(f"  {record['note']}")
    for item in items:
        outcome = item["outcome"]
        if outcome == "refused" and "summary" not in item:
            continue
        if outcome == "unresolved":
            lines.append(f"  cannot {item['kind'].replace('_', ' ')}: {item['reason']}")
        elif outcome == "refused":
            lines.append(f"  refused: {item['summary']}: {item['reason']}")
        elif outcome == "would":
            lines.append(f"  would {item['summary']}")
        elif outcome == "skipped":
            lines.append("  skipped" if item["reason"] == "declined" else
                         f"  not run without a terminal to confirm: {item['summary']}")
        elif outcome == "failed":
            lines.append(f"  failed: {item['reason']}")
        else:
            lines.append(f"  done: {item['summary']}")
    return "\n".join(lines)


def handle(engine: Engine, request: str, *, dry_run: bool = False, assume_yes: bool = False,
           out=sys.stdout, as_json: bool = False, agent: bool = False,
           under_overlay: bool = False) -> int:
    """Run one request and print it. 0 = done or nothing to do, 1 = otherwise."""
    options = Options(dry_run=dry_run, assume_yes=assume_yes, agent=agent,
                      under_overlay=under_overlay)
    record = run_request(engine, request, options, _never if agent else _terminal_confirm)
    print(json.dumps(record, ensure_ascii=False) if as_json else render(record), file=out)
    return record["status"]


def _image(args):
    engine_file = args.engine or os.environ.get("KILIX_NEEDLE_ENGINE")
    if engine_file:
        return asset.from_file(engine_file)
    return asset.from_installed(args.root)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["mcp"]:
        import mcp_server
        parser = argparse.ArgumentParser(prog="kilix-needle mcp",
                                         description=mcp_server.__doc__,
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("--engine", metavar="FILE")
        parser.add_argument("--root")
        args = parser.parse_args(argv[1:])
        return mcp_server.serve(lambda: _image(args))

    parser = argparse.ArgumentParser(prog="kilix-needle", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request", nargs="*", help="what to do; omit for a prompt loop")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and run nothing")
    parser.add_argument("--yes", action="store_true",
                        help="run closing, typing and program starts without asking; "
                             "never overrides a refusal")
    parser.add_argument("--json", action="store_true", help="print one JSON record per request")
    parser.add_argument("--agent", action="store_true",
                        help="called by an agent: never prompts, and never closes its own "
                             "pane or tab")
    parser.add_argument("--under-overlay", action="store_true",
                        help="opened as an overlay (a hotkey): 'this pane' is the one beneath")
    parser.add_argument("--engine", metavar="FILE",
                        help="a local copy of the pinned engine instead of the installed asset")
    parser.add_argument("--root", help="the Kilix content root, if not inherited from Kilix")
    args = parser.parse_args(argv)
    modes = dict(dry_run=args.dry_run, assume_yes=args.yes, as_json=args.json,
                 agent=args.agent, under_overlay=args.under_overlay)
    try:
        image = _image(args)
    except asset.AssetError as error:
        print(f"kilix-needle: {error}", file=sys.stderr)
        return 2
    try:
        with image, Engine(image, TOOLS) as engine:
            if args.request:
                return handle(engine, " ".join(args.request), **modes)
            if args.agent or not sys.stdin.isatty():
                print("kilix-needle: give a request, or run it in a terminal", file=sys.stderr)
                return 2
            status = 0
            while True:
                try:
                    line = input("needle> ")
                except EOFError:
                    print()
                    return status
                if line.strip() in ("quit", "exit", ":q"):
                    return status
                if line.strip():
                    status = handle(engine, line, **modes)
    except EngineError as error:
        print(f"kilix-needle: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nkilix-needle: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
