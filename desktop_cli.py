"""Natural-language X11 desktop actions, previewed by default.

  kilix-needle desktop --list
  kilix-needle desktop --model /path/to/bundle 'focus window "Notes"'
  kilix-needle desktop --model /path/to/bundle --yes 'type "hello" then press Return'

--model selects an explicit experimental bundle. Without it, the installed
Needle 2 base is used. --parse-only proposes/validates without an X11 connection.
All mutation requires --yes. Typing never implicitly submits text.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import asset
import desktop_job as job
import desktop_codec as codec_module
from desktop_x11 import X11
from engine import reply_calls
from libengine import LibEngine, LibEngineError


def schema_sha(codec=codec_module.DEFAULT):
    return hashlib.sha256(json.dumps(codec_module.tools(codec), sort_keys=True).encode()).hexdigest()


def open_runtime(model=None, root=None, *, codec=None):
    from needle_cli import Runtime
    library, weights = None, None
    selected_codec = codec or codec_module.DEFAULT
    try:
        library = asset.installed_library(root)
        if model:
            bundle = Path(model).expanduser().resolve()
            manifest_path = bundle / "model.json"
            if manifest_path.stat().st_size > 65536:
                raise job.DesktopError("desktop model manifest exceeded its size limit")
            spec = json.loads(manifest_path.read_text())
            selected_codec = spec.get("codec", codec_module.DIRECT)
            if codec is not None and codec != selected_codec:
                raise job.DesktopError("selected model uses a different output format")
            if spec.get("schema") != job.VERSION or spec.get("tools_sha256") != schema_sha(selected_codec):
                raise job.DesktopError("desktop model schema differs from this checkout")
            if "runtime_sha256" in spec and spec["runtime_sha256"] != library.sha256:
                raise job.DesktopError("desktop model was evaluated with a different native runtime")
            # File name is code-owned. Metadata cannot choose another path.
            if not isinstance(spec.get("sha256"), str) or not __import__("re").fullmatch(r"[0-9a-f]{64}", spec["sha256"]) or type(spec.get("bytes")) is not int or not 1 <= spec["bytes"] <= 64 * 1024 * 1024:
                raise job.DesktopError("invalid desktop model artifact identity")
            weights = asset.load_verified(bundle / "model.cact", spec["sha256"], spec["bytes"])
        images = [i for i in (library, weights) if i is not None]
        return Runtime(LibEngine(library, codec_module.tools(selected_codec), weights), images,
                       translate=lambda calls: codec_module.decode(calls, selected_codec),
                       label="desktop experimental " + weights.sha256[:12] if weights else "desktop stock Needle 2")
    except BaseException:
        for image in (library, weights):
            if image is not None:
                image.close()
        raise


def run(request, *, engine=None, calls=None, execute=False, parse_only=False, backend=None, display=None):
    record = dict(schema=job.VERSION, status=1, outcome="refused", actions=[], results=[])
    started = time.monotonic()
    try:
        if type(execute) is not bool or type(parse_only) is not bool or execute and parse_only:
            raise job.DesktopError("execution and parse-only are mutually exclusive")
        # Rejection before model access keeps quoted instructions and conditions
        # out of the action path. Model-only evaluation measures them separately.
        if calls is None:
            job.request_clauses(request)
            if engine is None:
                raise job.DesktopError("a model runtime is required for natural-language planning")
            engine.reset()
            reply = engine.complete(request)
            raw, error = reply_calls(reply)
            if error:
                raise job.DesktopError("desktop model failed; no actions admitted")
            raw = getattr(engine, "translate", lambda calls: calls)(raw)
            actions = job.validate(request, raw)
            record["model"] = getattr(engine, "label", "desktop model")
        else:
            if request is not None:
                raise job.DesktopError("supply natural language or structured actions, not both")
            actions = job.normalize(calls)
            if not actions:
                raise job.DesktopError("provide at least one action")
            record["model"] = "structured actions"
        record["actions"] = actions
        if parse_only:
            record.update(status=0, outcome="proposed", note="arguments checked; desktop targets not resolved")
            return record
        backend = backend or X11(display=display)
        snapshot = backend.snapshot()
        plan = backend.plan(actions, snapshot)
        record["plan"] = plan
        record["display"] = snapshot["display"]
        if not execute:
            record.update(status=0, outcome="planned", note="preview; pass --yes to execute")
            if all(c["name"] == "window" and c["arguments"]["action"] == "list" for c in actions):
                record.update(outcome="observed", observation=snapshot, note="window snapshot")
            return record
        record.update(backend.execute(plan))
        return record
    except (job.DesktopError, asset.AssetError, LibEngineError, OSError, ValueError, KeyError) as error:
        record["note"] = str(error)
        return record
    finally:
        record["elapsed_ms"] = round((time.monotonic() - started) * 1000)


def mcp(arguments, *, engine=None, plan=False, read=False):
    allowed = {"display"} if read else {"request", "display"} if plan else {"request", "display", "confirm_risky"}
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise ValueError("unknown desktop arguments")
    if "display" in arguments and not isinstance(arguments["display"], str):
        raise ValueError("display must be a string")
    consent = arguments.get("confirm_risky", False)
    if type(consent) is not bool:
        raise ValueError("confirm_risky must be a boolean")
    if read:
        return run(None, calls=[{"name": "window", "arguments": {"action": "list"}}], display=arguments.get("display"))
    if not isinstance(arguments.get("request"), str):
        raise ValueError("request must be a string")
    if not plan and not consent:
        return dict(schema=job.VERSION, status=1, outcome="blocked", note="desktop act requires confirm_risky=true", actions=[], results=[])
    return run(arguments["request"], engine=engine, execute=not plan, display=arguments.get("display"))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kilix-needle desktop", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request", nargs="*")
    parser.add_argument("--model", help="explicit model bundle directory (model.json and model.cact)")
    parser.add_argument("--root", help="installed Kilix content root")
    parser.add_argument("--display", help="X11 display; native Wayland is unsupported")
    parser.add_argument("--list", action="store_true", help="read the window snapshot without a model")
    parser.add_argument("--actions-json", metavar="FILE", help="structured action list, or - for bounded stdin")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--yes", action="store_true", help="execute the requested actions")
    modes.add_argument("--dry-run", action="store_true", help="preview against the current desktop (default)")
    modes.add_argument("--parse-only", action="store_true", help="plan without connecting to a desktop")
    parser.add_argument("--json", action="store_true", help="print a compact JSON receipt")
    args = parser.parse_args(argv)
    request = " ".join(args.request) if args.request else None
    if sum((bool(request), args.list, bool(args.actions_json))) != 1:
        parser.error("choose one request, --list, or --actions-json")
    try:
        options = dict(execute=args.yes, parse_only=args.parse_only, display=args.display)
        if args.list:
            record = run(None, calls=[{"name": "window", "arguments": {"action": "list"}}], **options)
        elif args.actions_json:
            if args.actions_json == "-":
                data = sys.stdin.buffer.read(16385)
            else:
                with open(args.actions_json, "rb") as source:
                    data = source.read(16385)
            if len(data) > 16384:
                raise job.DesktopError("structured action request exceeds 16 KiB")
            record = run(None, calls=json.loads(data), **options)
        else:
            from needle_cli import LazyRuntime
            with LazyRuntime(lambda: open_runtime(args.model, args.root)) as engine:
                record = run(request, engine=engine, **options)
    except (job.DesktopError, asset.AssetError, OSError, ValueError) as error:
        record = dict(schema=job.VERSION, status=1, outcome="refused", note=str(error))
    print(json.dumps(record, ensure_ascii=True, separators=(",", ":") if args.json else None, indent=None if args.json else 2))
    return record["status"]


if __name__ == "__main__":
    raise SystemExit(main())
