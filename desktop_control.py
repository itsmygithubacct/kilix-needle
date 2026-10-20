"""Typed X11 desktop actions using the shared desktop contract and executor."""
from __future__ import annotations

import argparse
import json
import sys

import desktop_cli
import desktop_job as job


DESCRIPTION = """Window, pointer, keyboard, scroll and wait actions on an X11 desktop.
Preview is the default. --yes executes; --parse-only checks arguments without X11.
Targets: active, title:EXACT TITLE, id:DECIMAL ID. Ambiguous titles are refused.
Typing preserves literal text and never presses Enter; use keyboard press Return
separately. JSON batches contain 1–6 {name, arguments} actions, in order.
"""


def parser():
    common = argparse.ArgumentParser(add_help=False, allow_abbrev=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--display", help="explicit X11 display")
    common.add_argument("--json", action="store_true", help="compact JSON receipt")
    modes = common.add_mutually_exclusive_group()
    modes.add_argument("--yes", action="store_true", help="execute actions")
    modes.add_argument("--dry-run", action="store_true", help="preview against the desktop (default)")
    modes.add_argument("--parse-only", action="store_true", help="validate without connecting to X11")
    root = argparse.ArgumentParser(prog="kilix-desktop", description=DESCRIPTION,
                                   formatter_class=argparse.RawDescriptionHelpFormatter,
                                   parents=[common], allow_abbrev=False)
    commands = root.add_subparsers(dest="name", required=True)
    for name, actions in (("window", ("list", "focus", "move", "resize", "minimize", "maximize", "restore", "close")),
                          ("pointer", ("move", "click", "double-click", "drag")),
                          ("keyboard", ("type", "press"))):
        group = commands.add_parser(name, parents=[common], allow_abbrev=False)
        verbs = group.add_subparsers(dest="action", required=True)
        for action in actions:
            verb = verbs.add_parser(action, parents=[common], allow_abbrev=False)
            if name == "window" and action != "list":
                verb.add_argument("--target", default="active", help="active, title:EXACT TITLE, or id:DECIMAL ID")
            if action in ("move", "drag") or name == "pointer":
                for field in ("x", "y"):
                    verb.add_argument("--" + field, type=int, required=action in ("move", "drag"))
            if action == "resize":
                for field in ("width", "height"):
                    verb.add_argument("--" + field, type=int, required=True)
            if name == "pointer" and action != "move":
                verb.add_argument("--button", choices=("left", "middle", "right"), default="left")
            if name == "keyboard" and action == "type":
                literal = verb.add_mutually_exclusive_group(required=True)
                literal.add_argument("--text", help="exact single-line literal, without Enter")
                literal.add_argument("--text-file", metavar="FILE", help="UTF-8 file, or - for stdin; no whitespace is trimmed")
            if name == "keyboard" and action == "press":
                verb.add_argument("keys", help="one key or chord, e.g. Return, ctrl+l")
    scroll = commands.add_parser("scroll", parents=[common], allow_abbrev=False)
    scroll.add_argument("direction", choices=("up", "down", "left", "right"))
    scroll.add_argument("--steps", type=int, default=3)
    wait = commands.add_parser("wait", parents=[common], allow_abbrev=False)
    wait.add_argument("ms", type=int, help="1–3000 milliseconds; batch total at most 6000")
    batch = commands.add_parser("batch", parents=[common], allow_abbrev=False)
    batch.add_argument("file", help="JSON action list file, or - for bounded stdin")
    commands.add_parser("schema", parents=[common], allow_abbrev=False, help="print the versioned action schema; no desktop connection")
    return root


def main(argv=None):
    args = vars(parser().parse_args(argv))
    compact = args.pop("json", False)
    execute, parse_only, dry_run = (args.pop(key, False) for key in ("yes", "parse_only", "dry_run"))
    display = args.pop("display", None)
    name = args.pop("name")
    try:
        if sum((execute, parse_only, dry_run)) > 1:
            raise job.DesktopError("choose one execution mode")
        if name == "schema":
            if execute:
                raise job.DesktopError("schema is read-only")
            record = dict(schema=job.VERSION, status=0, tools=job.TOOLS, max_actions=job.MAX_ACTIONS)
        else:
            if name == "batch":
                calls = desktop_cli.read_actions(args["file"])
            else:
                args = {key: value for key, value in args.items() if value is not None}
                if "text_file" in args:
                    args["text"] = desktop_cli.read_bounded(args.pop("text_file"), 512).decode("utf-8")
                if "action" in args:
                    args["action"] = args["action"].replace("-", "_")
                calls = [{"name": name, "arguments": args}]
            record = desktop_cli.run(None, calls=calls, execute=execute, parse_only=parse_only, display=display)
    except (job.DesktopError, OSError, ValueError) as error:
        record = dict(schema=job.VERSION, status=1, outcome="refused", note=str(error), actions=[], results=[])
    print(json.dumps(record, ensure_ascii=True, separators=(",", ":") if compact else None, indent=None if compact else 2))
    return record["status"]


if __name__ == "__main__":
    raise SystemExit(main())
