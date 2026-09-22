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
from libengine import LibEngine, LibEngineError
import toolset
import tuning


class Runtime:
    """The engine in use, what its calls are translated through, and its images."""

    def __init__(self, engine, images, translate=lambda calls: calls, label="base"):
        self.engine, self.images, self.translate, self.label = engine, images, translate, label

    def __enter__(self) -> "Runtime":
        self.engine.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        self.engine.close()
        for image in self.images:
            image.close()

    def reset(self) -> None:
        self.engine.reset()

    def complete(self, text: str) -> dict:
        return self.engine.complete(text)


def open_runtime(args, *, may_install: bool = False) -> Runtime:
    """The tuned model if one passed its gates and is selected, else the base engine."""
    explicit = getattr(args, "engine", None) or os.environ.get("KILIX_NEEDLE_ENGINE")
    choice = None if explicit else tuning.selected()
    if choice is not None:
        library = weights = None
        try:
            dev_library = os.environ.get("KILIX_NEEDLE_LIBRARY")
            library = asset.library_from_file(dev_library) if dev_library \
                else asset.installed_library(getattr(args, "root", None))
            weights = asset.load_verified(choice["weights"], choice["sha256"],
                                          os.path.getsize(choice["weights"]))
            return Runtime(LibEngine(library, toolset.TOOLS, weights), [library, weights],
                           toolset.to_actions, label=f"tuned {choice.get('run', '')}".strip())
        except (asset.AssetError, OSError, KeyError) as error:
            for image in (library, weights):
                if image is not None:
                    image.close()
            print(f"kilix-needle: the selected tuned model is unavailable ({error}); "
                  "using the base model", file=sys.stderr)
    image = _image(args, may_install=may_install)
    return Runtime(Engine(image, TOOLS), [image])


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
    translate = getattr(engine, "translate", lambda calls: calls)
    results = interpret(request, translate(reply.get("function_calls") or []))
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


def _offer_tuning(args) -> None:
    """After the first install, at a terminal only: offer to fine-tune in the background."""
    missing = asset.missing_for_tuning(getattr(args, "root", None))
    print("\nkilix-needle can fine-tune Needle 2 for Kilix on this machine. It runs in the\n"
          "background at the lowest priority, takes several hours, peaks near 9 GB of\n"
          "memory, and is used only if it beats the base model on every benchmark gate.")
    if missing:
        print(f"It needs {', '.join(missing)} first: run `kilix-needle install --tuning`,\n"
              "then `kilix-needle tune --background`.\n")
        return
    answer = input("Start fine-tuning now? [Y/n] ").strip().casefold()
    if answer in ("", "y", "yes"):
        tuning.main(["--background"])
    else:
        print("Not started. Run `kilix-needle tune --background` at any time.\n")


def _image(args, *, may_install: bool = False):
    """The engine to run: --engine, then KILIX_NEEDLE_ENGINE, then the installed asset.

    On first use at a terminal, a missing asset leads into `install`: the
    licence screen and typed agreement, then the download.
    """
    engine_file = getattr(args, "engine", None) or os.environ.get("KILIX_NEEDLE_ENGINE")
    if engine_file:
        return asset.from_file(engine_file)
    try:
        return asset.from_installed(args.root)
    except asset.AssetError as error:
        missing = "is not installed" in str(error) or "has not been accepted" in str(error)
        if not (may_install and missing and sys.stdin.isatty() and sys.stdout.isatty()):
            raise
        print(f"kilix-needle: {error}\nFirst use: installing the Needle 2 engine.\n")
        asset.install(args.root)
        _offer_tuning(args)
        return asset.from_installed(args.root)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["setup"]:
        import setup_surfaces
        parser = argparse.ArgumentParser(prog="kilix-needle setup",
                                         description=setup_surfaces.__doc__,
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--undo", action="store_true")
        parser.add_argument("--only", default=",".join(setup_surfaces.SURFACES))
        args = parser.parse_args(argv[1:])
        only = [name.strip() for name in args.only.split(",") if name.strip()]
        unknown = [name for name in only if name not in setup_surfaces.SURFACES]
        if unknown:
            parser.error(f"unknown surface: {', '.join(unknown)}")
        status, lines = setup_surfaces.setup(only, undo=args.undo, dry_run=args.dry_run)
        print("\n".join(lines))
        return status
    if argv[:1] == ["tune"]:
        return tuning.main(argv[1:])
    if argv[:1] == ["install"]:
        parser = argparse.ArgumentParser(prog="kilix-needle install",
                                         description="Accept the Needle 2 licence and install "
                                                     "the engine (the needle2 content asset).")
        parser.add_argument("--from", dest="supplied", metavar="DIR",
                            help="read the files from DIR instead of downloading them")
        parser.add_argument("--tuning", action="store_true",
                            help="also install what fine-tuning needs: the base checkpoint "
                                 "and tokenizer (needle2-train) and libneedle.so "
                                 "(needle2-runtime), each with its own licence screen")
        parser.add_argument("--root")
        args = parser.parse_args(argv[1:])
        wanted = [asset.ASSET_ID] + (list(asset.TUNING_ASSETS) if args.tuning else [])
        for asset_id in wanted:
            try:
                where = asset.install(args.root, supplied=args.supplied, asset_id=asset_id)
            except asset.AssetError as error:
                print(f"kilix-needle: {error}", file=sys.stderr)
                return 1
            print(f"installed: {where}")
        return 0
    if argv[:1] == ["mcp"]:
        import mcp_server
        parser = argparse.ArgumentParser(prog="kilix-needle mcp",
                                         description=mcp_server.__doc__,
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("--engine", metavar="FILE")
        parser.add_argument("--root")
        args = parser.parse_args(argv[1:])
        return mcp_server.serve(lambda: open_runtime(args))

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
        runtime = open_runtime(args, may_install=not args.agent)
    except asset.AssetError as error:
        print(f"kilix-needle: {error}", file=sys.stderr)
        return 2
    try:
        with runtime as engine:
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
    except (EngineError, LibEngineError) as error:
        print(f"kilix-needle: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nkilix-needle: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
