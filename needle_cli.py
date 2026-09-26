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

from actions import LEGACY_TOOLS, Action, Refusal, interpret, plain
import asset
import jobs
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


def open_runtime(args, *, may_install: bool = False, job: str = jobs.DEFAULT) -> Runtime:
    """The tuned model if one passed its gates and is selected, else the base engine."""
    explicit = getattr(args, "engine", None) or os.environ.get("KILIX_NEEDLE_ENGINE")
    if job == "apps":
        import apps
        tuned_tools, tuned_translate, base_tools = apps.TOOLS, (lambda calls: calls), apps.TOOLS
    else:
        tuned_tools, tuned_translate, base_tools = toolset.TOOLS, toolset.to_actions, LEGACY_TOOLS
    choice = None if explicit else tuning.selected(job)
    if choice is not None:
        library = weights = None
        try:
            dev_library = os.environ.get("KILIX_NEEDLE_LIBRARY")
            library = asset.library_from_file(dev_library) if dev_library \
                else asset.installed_library(getattr(args, "root", None))
            weights = asset.load_verified(choice["weights"], choice["sha256"],
                                          os.path.getsize(choice["weights"]))
            return Runtime(LibEngine(library, tuned_tools, weights), [library, weights],
                           tuned_translate, label=f"tuned {choice.get('run', '')}".strip())
        except (asset.AssetError, OSError, KeyError) as error:
            for image in (library, weights):
                if image is not None:
                    image.close()
            print(f"kilix-needle: the selected tuned model is unavailable ({error}); "
                  "using the base model", file=sys.stderr)
    image = _image(args, may_install=may_install)
    return Runtime(Engine(image, base_tools), [image])


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
    try:
        request = check_prompt(request)
    except ValueError as error:
        return {"request": request, "status": 1, "note": str(error), "items": []}
    engine.reset()
    reply = engine.complete(request)
    translate = getattr(engine, "translate", lambda calls: calls)
    return run_calls(request, translate(reply.get("function_calls") or []), options, confirm)


def run_calls(request: str, calls: list, options: Options,
              confirm: Callable[[str], bool] = _terminal_confirm) -> dict:
    """Shared execution path for Needle 2 and external kilix-ml inference.

    Calls are untrusted model proposals. They always pass interpretation,
    resolution, confirmation and the agent's own-pane protection here.
    """
    record = {"request": request, "status": 0, "note": "", "items": []}
    items = record["items"]
    try:
        from domain_bridge import check_request, validate
        request = check_request(request)
        validate(request, calls)
    except ValueError as error:
        record.update(status=1, note=str(error))
        return record
    if options.agent:
        confirm = _never
    results = interpret(request, calls)
    if not results:
        record["note"] = "There is no pane or tab action in that request."
        return record

    refused = [item for item in results if isinstance(item, Refusal)]
    for item in refused:
        items.append({"kind": item.kind, "outcome": "refused", "reason": item.reason})
    actions = [item for item in results if isinstance(item, Action)]
    hold = bool(refused)
    unplain = plain(request, actions)
    if refused:
        record["status"] = 1
    if hold and actions:
        record["note"] = "part of the request was refused, so nothing runs without a yes"

    # One snapshot per request: every action is resolved against the desktop
    # the person was looking at, and performed by id. Review KN-R4-01: with a
    # snapshot per action, "close tab 2 and close tab 3" closed tab 2 and then
    # the tab that had been tab 4, because Kilix renumbers at once.
    try:
        tree = kilix.snapshot(under_overlay=options.under_overlay) if actions else None
    except kilix.KilixError as error:
        tree, snapshot_error = None, str(error)
    # Once any action is refused, unresolved or fails, nothing later runs on a
    # yes given in advance (review KN-R4-04: an unresolved go-to was skipped
    # and the close after it ran).
    broken = False
    for action in actions:
        entry = {"kind": action.kind, "args": dict(action.args)}
        items.append(entry)
        if tree is None:
            entry.update(outcome="unresolved", reason=snapshot_error)
            record["status"] = 1
            broken = True
            continue
        # Without a known caller the own-pane guard below cannot work, and
        # "current" would mean the user's focused pane (review KN-03: an agent
        # with no KITTY_WINDOW_ID closed the user's vim pane in another tab).
        # Relative panes too (review KN-R2-04: "run rm -rf build in the right
        # pane" typed into the user's pane): every reference resolves against
        # a pane that is not the agent's, so nothing risky runs at all.
        if options.agent and tree.caller is None and (
                action.risky or "current" in action.args.values()):
            entry.update(outcome="refused",
                         reason="cannot tell which pane is the agent's own, so nothing "
                                "risky and no 'this pane' from an agent here")
            record["status"] = 1
            broken = True
            continue
        try:
            step = kilix.resolve(action, tree)
        except kilix.KilixError as error:
            entry.update(outcome="unresolved", reason=str(error))
            record["status"] = 1
            broken = True
            continue
        entry["summary"] = step.summary
        if options.agent and tree.caller is not None and tree.caller.get("id") in step.closes:
            entry.update(outcome="refused",
                         reason="an agent may not close its own pane or the tab it is in")
            record["status"] = 1
            broken = True
            continue
        if options.dry_run:
            entry["outcome"] = "would"
            continue
        # --yes answers for a risky action; a partly refused request still needs a
        # person, because what survived may be the wrong half of a misreading.
        needs_yes = hold or action.risky
        # A yes given in advance (--yes, MCP confirm_risky) covers only a plain
        # instruction; anything else waits for a person who sees the target
        # (review R2: word lists of what to refuse were always one word short).
        # Never on a yes given in advance: a target found only by a whole word
        # of a title, or a close of the requester's own pane or tab (review
        # KN-R5-01; an agent is refused that outright, above).
        own = tree.caller is not None and tree.caller.get("id") in step.closes
        waived = (options.assume_yes and not hold and not broken and unplain is None
                  and not step.fuzzy and not step.elsewhere and not own)
        if needs_yes and not waived \
                and not confirm(f"  {step.summary}? [y/N] "):
            entry["outcome"] = "skipped"
            entry["reason"] = ("declined" if confirm is _terminal_confirm and sys.stdin.isatty()
                               else "needs a person's yes: an earlier action in the request "
                                    "did not go as asked" if broken and options.assume_yes
                               else "needs a person's yes: the target was found only by a word "
                                    "of its title" if step.fuzzy and options.assume_yes
                               else "needs a person's yes: another tab has a pane by that "
                                    "name too" if step.elsewhere and options.assume_yes
                               else "needs a person's yes: it closes the pane or tab this "
                                    "request was made from" if own and options.assume_yes
                               else "needs a yes and there is no one to ask" if unplain is None
                               or not options.assume_yes
                               else f"needs a person's yes: the request is not a plain "
                                    f"instruction ({unplain})")
            record["status"] = 1
            continue
        if step.types_into is not None:
            try:
                ready = kilix.still_at_prompt(step.types_into, under_overlay=options.under_overlay)
            except kilix.KilixError:
                ready = False
            if not ready:
                entry.update(outcome="unresolved",
                             reason="that pane is no longer at a shell prompt; nothing typed")
                record["status"] = 1
                broken = True
                continue
        try:
            kilix.perform(step)
        except kilix.KilixError as error:
            entry.update(outcome="failed", reason=str(error))
            record["status"] = 1
            return record
        # A later numbered tab still means the tab it was when the request was
        # made: its step was resolved above to an id, never to a position.
        entry["outcome"] = "done"
    return record


def run_apps_request(engine, request: str, options: Options,
                     confirm: Callable[[str], bool] = _terminal_confirm) -> dict:
    """One apps-job request, as the same record as run_request."""
    try:
        request = check_prompt(request)
    except ValueError as error:
        return {"request": request, "status": 1, "note": str(error), "items": []}
    engine.reset()
    reply = engine.complete(request)
    translate = getattr(engine, "translate", lambda calls: calls)
    return run_apps_calls(request, translate(reply.get("function_calls") or []), options, confirm)


def run_apps_calls(request: str, calls: list, options: Options,
                   confirm: Callable[[str], bool] = _terminal_confirm) -> dict:
    """Interpret, confirm and perform apps-job calls.

    Every launch and settings change needs a yes (review R12: "should I hide
    the clock?" once hid it with no one asked). A yes given in advance (--yes,
    MCP confirm_risky) gives it only for an action the request states plainly,
    and for a launch only of something already installed: a launch that may
    install always waits for a person. Opening the settings screen changes
    nothing and needs no yes. Once anything in the request is refused or
    fails, nothing later runs without a person's yes.
    """
    import apps
    import apps_kilix
    record = {"request": request, "status": 0, "note": "", "items": []}
    items = record["items"]
    if options.agent:
        confirm = _never
    results = apps.interpret(request, calls)
    if not results:
        record["note"] = "There is no app or settings action in that request."
        return record
    refused = [r for r in results if isinstance(r, Refusal)]
    for item in refused:
        items.append({"kind": item.kind, "outcome": "refused", "reason": item.reason})
    actions = [r for r in results if isinstance(r, apps.Action)]
    hold = bool(refused)
    unplain = apps.plain(request, actions)
    if refused:
        record["status"] = 1
    if hold and actions:
        record["note"] = "part of the request was refused, so nothing runs without a yes"
    broken = False
    for action in actions:
        entry = {"kind": action.kind, "args": dict(action.args)}
        items.append(entry)
        step = apps_kilix.resolve(action)
        entry["summary"] = step.summary
        if options.dry_run:
            entry["outcome"] = "would"
            continue
        needs_yes = hold or broken or action.kind in apps.SIDE_EFFECT_KINDS
        waived = (options.assume_yes and not hold and not broken and unplain is None
                  and step.ready)
        # A request that isn't plain is shown whole, so the person sees what
        # else it says ("hide the clock later") and not only the action.
        question = (f"  {step.summary}? [y/N] " if unplain is None
                    else f"  {request!r} asks to {step.summary}? [y/N] ")
        if needs_yes and not waived and not confirm(question):
            entry["outcome"] = "skipped"
            entry["reason"] = ("declined" if confirm is _terminal_confirm and sys.stdin.isatty()
                               else "needs a person's yes: it may install first" if not step.ready
                               else "needs a person's yes: an earlier action in the request "
                                    "did not go as asked" if (hold or broken) and options.assume_yes
                               else f"needs a person's yes: the request is not a plain "
                                    f"instruction ({unplain})" if unplain and options.assume_yes
                               else "needs a yes and there is no one to ask")
            record["status"] = 1
            continue
        try:
            # Installing only on a person's own yes: a yes given in advance
            # never reaches an install (waived needs step.ready).
            apps_kilix.perform(step, install=not step.ready)
        except kilix.KilixError as error:
            entry.update(outcome="failed", reason=str(error))
            record["status"] = 1
            broken = True
            continue
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
                         f"  not run, {item['reason']}: {item['summary']}"
                         if item["reason"].startswith("needs a person's yes") else
                         f"  not run without a terminal to confirm: {item['summary']}")
        elif outcome == "failed":
            lines.append(f"  failed: {item['reason']}")
        else:
            lines.append(f"  done: {item['summary']}")
    return "\n".join(lines)


def handle(engine: Engine, request: str, *, dry_run: bool = False, assume_yes: bool = False,
           out=sys.stdout, as_json: bool = False, agent: bool = False,
           under_overlay: bool = False, job: str = jobs.DEFAULT) -> int:
    """Run one request and print it. 0 = done or nothing to do, 1 = otherwise."""
    options = Options(dry_run=dry_run, assume_yes=assume_yes, agent=agent,
                      under_overlay=under_overlay)
    run = run_apps_request if job == "apps" else run_request
    record = run(engine, request, options, _never if agent else _terminal_confirm)
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
    if argv[:1] in (["contract"], ["bridge"]):
        import domain_bridge
        return domain_bridge.main(argv)
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
        parser.add_argument("--runtime", action="store_true",
                            help="also install libneedle.so (needle2-runtime), which runs a "
                                 "tuned model, with its own licence screen")
        parser.add_argument("--root")
        args = parser.parse_args(argv[1:])
        wanted = [asset.ASSET_ID] + (list(asset.TUNING_ASSETS) if args.tuning
                                     else [asset.RUNTIME_ID] if args.runtime else [])
        for asset_id in wanted:
            # An asset already accepted and installed is not asked about again:
            # `install --runtime` after `install` re-showed the needle2 screen,
            # and declining it stopped before the runtime (measured).
            try:
                _spec, where = asset._installed_spec(asset_id, args.root)
            except asset.AssetError:
                pass
            else:
                print(f"already installed: {where}")
                continue
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
        return mcp_server.serve(lambda job=jobs.DEFAULT: open_runtime(args, job=job))

    job = jobs.DEFAULT
    if argv[:1] == ["apps"]:
        # Launch Kilix apps and games and change Kilix settings (the apps job).
        job, argv = "apps", argv[1:]
    parser = argparse.ArgumentParser(prog="kilix-needle apps" if job == "apps" else "kilix-needle",
                                     description=__doc__,
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
                 agent=args.agent, under_overlay=args.under_overlay, job=job)
    try:
        runtime = open_runtime(args, may_install=not args.agent, job=job)
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
