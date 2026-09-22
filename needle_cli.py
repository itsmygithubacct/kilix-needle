"""kilix-needle: drive Kilix panes and tabs from plain requests with Needle 2.

    kilix-needle split right and run htop
    kilix-needle                     # a prompt loop; one request per line
    kilix-needle --dry-run close tab 2

Each request is one Needle turn. Its calls pass the checks in `actions`,
resolve against a fresh `kilix @ ls`, and are printed before anything runs.
Opening, focusing, arranging, renaming and resizing run straight away.
Closing, typing into a pane and starting a program wait for `y`, and so does
everything in a request any part of which was refused. There is no flag that
answers for you; without a terminal, those actions are not run.
"""
from __future__ import annotations

import argparse
import os
import sys

from actions import TOOLS, Action, Refusal, interpret
import asset
from engine import Engine, EngineError, check_prompt
import kilix


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    sys.stdout.write(question)
    sys.stdout.flush()
    answer = sys.stdin.readline(16)
    return answer.strip().casefold() in ("y", "yes")


def handle(engine: Engine, request: str, *, dry_run: bool, out=sys.stdout) -> int:
    """Run one request. 0 = done or nothing to do, 1 = refused or failed."""
    try:
        request = check_prompt(request)
    except ValueError as error:
        print(f"kilix-needle: {error}", file=out)
        return 1
    engine.reset()
    reply = engine.complete(request)
    results = interpret(request, reply.get("function_calls") or [])
    if not results:
        print("Needle found no pane or tab action in that request.", file=out)
        return 0

    refused = [item for item in results if isinstance(item, Refusal)]
    for item in refused:
        print(f"  refused {item.kind}: {item.reason}", file=out)
    actions = [item for item in results if isinstance(item, Action)]
    if not actions:
        return 1
    hold = bool(refused)
    if hold:
        print("  part of the request was refused, so nothing runs without a yes", file=out)

    status = 0
    for action in actions:
        try:
            step = kilix.resolve(action, kilix.snapshot())
        except kilix.KilixError as error:
            print(f"  cannot {action.kind.replace('_', ' ')}: {error}", file=out)
            status = 1
            continue
        if dry_run:
            print(f"  would {step.summary}", file=out)
            continue
        if (hold or action.risky) and not _confirm(f"  {step.summary}? [y/N] "):
            print("  skipped" if sys.stdin.isatty() else
                  f"  not run without a terminal to confirm: {step.summary}", file=out)
            status = 1
            continue
        try:
            kilix.perform(step)
        except kilix.KilixError as error:
            print(f"  failed: {error}", file=out)
            return 1
        print(f"  done: {step.summary}", file=out)
    return status


def _image(args):
    engine_file = args.engine or os.environ.get("KILIX_NEEDLE_ENGINE")
    if engine_file:
        return asset.from_file(engine_file)
    return asset.from_installed(args.root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kilix-needle", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request", nargs="*", help="what to do; omit for a prompt loop")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and run nothing")
    parser.add_argument("--engine", metavar="FILE",
                        help="a local copy of the pinned engine instead of the installed asset")
    parser.add_argument("--root", help="the Kilix content root, if not inherited from Kilix")
    args = parser.parse_args(argv)
    try:
        image = _image(args)
    except asset.AssetError as error:
        print(f"kilix-needle: {error}", file=sys.stderr)
        return 2
    try:
        with image, Engine(image, TOOLS) as engine:
            if args.request:
                return handle(engine, " ".join(args.request), dry_run=args.dry_run)
            if not sys.stdin.isatty():
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
                    status = handle(engine, line, dry_run=args.dry_run)
    except EngineError as error:
        print(f"kilix-needle: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nkilix-needle: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
