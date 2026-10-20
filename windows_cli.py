"""Natural-language control of Windows applications through the Kilix SDK.

  kilix-needle windows --list
  kilix-needle windows 'open notepad then type "hello" then press ctrl+s'
  kilix-needle windows --yes 'click the "Save" button in notepad'
  kilix-needle windows --host thinktop-windows --yes 'close notepad'

Inside WSL the requests go through `kilix desktop`; from Linux, --host names
an SSH alias for a Windows machine running Kilix. Preview is the default and
resolves windows without input; --yes executes; --parse-only connects to
nothing. The grammar answers first; --parser needle or hybrid lets the stock
Needle 2 model propose plans it cannot read (unqualified, always validated).
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import windows_job as job
import windows_grammar
import desktop_windows


def open_runtime(root=None):
    import asset
    from libengine import LibEngine
    from needle_cli import Runtime
    library = asset.installed_library(root)
    try:
        return Runtime(LibEngine(library, job.TOOLS), [library], label="windows stock Needle 2 (unqualified)")
    except BaseException:
        library.close()
        raise


def _say(progress, message):
    if progress:
        print(f"kilix-needle windows: {message}", file=sys.stderr, flush=True)


def _step_text(index, total, item):
    args = item["arguments"]
    window = item.get("window", {}).get("window") or item.get("window", {}).get("selector") or {}
    where = f' in "{window.get("title") or window.get("app")}"' if window else ""
    what = item["name"] + (" " + args["action"] if "action" in args else " " + args.get("for", ""))
    return f"step {index + 1}/{total}: {what.strip()}{where}"


def run(request, *, engine=None, calls=None, execute=False, parse_only=False, backend=None,
        host=None, windows_root=None, parser_mode="grammar", progress=False):
    record = dict(schema=job.VERSION, status=1, outcome="refused", actions=[], results=[])
    started = time.monotonic()
    try:
        if type(execute) is not bool or type(parse_only) is not bool or execute and parse_only:
            raise job.WindowsActionError("execution and parse-only are mutually exclusive")
        if parser_mode not in ("grammar", "needle", "hybrid"):
            raise job.WindowsActionError("parser must be grammar, needle, or hybrid")
        if calls is None:
            job.request_clauses(request)
            record["parser_mode"] = parser_mode
            reasons = []
            actions = windows_grammar.parse(request, reasons) if parser_mode != "needle" else None
            if actions is not None:
                record.update(parser="grammar", model=None)
            else:
                if parser_mode == "grammar":
                    raise job.WindowsActionError(reasons[0] if reasons else
                                                 "request does not match the Windows grammar; see kilix-needle windows --help")
                if engine is None:
                    raise job.WindowsActionError("a model runtime is required for natural-language planning")
                from engine import reply_calls
                _say(progress, "loading the Needle model and planning")
                record["parser"] = "needle"
                engine.reset()
                raw, error = reply_calls(engine.complete(request))
                if error:
                    raise job.WindowsActionError("the model failed; no actions admitted")
                actions = job.validate(request, raw)
                record["model"] = getattr(engine, "label", "windows model")
        else:
            if request is not None:
                raise job.WindowsActionError("supply natural language or structured actions, not both")
            actions = job.normalize(calls)
            record.update(parser="structured", model=None)
        record["actions"] = actions
        if parse_only:
            record.update(status=0, outcome="proposed", note="arguments checked; windows not resolved")
            return record
        backend = backend or desktop_windows.Windows(host=host, windows_root=windows_root)
        _say(progress, f"reading the Windows desktop ({backend.where})")
        snapshot = backend.snapshot()
        plan = backend.plan(actions, snapshot)
        record["plan"] = desktop_windows.public_plan(plan)
        record["where"] = snapshot["where"]
        if job.read_only(actions) and len(actions) == 1 and actions[0]["arguments"].get("action") == "list":
            record.update(status=0, outcome="observed", observation=[desktop_windows.describe(r) for r in snapshot["windows"]
                                                                     if (r.get("title") or "").strip()],
                          note="window snapshot")
            return record
        if not execute and not job.read_only(actions):
            record.update(status=0, outcome="planned", note="preview; pass --yes to execute")
            return record
        record.update(backend.execute(plan, progress=(lambda i, n, item: _say(progress, _step_text(i, n, item)))
                                      if progress else None))
        return record
    except (job.WindowsActionError, OSError, ValueError, KeyError) as error:
        record["note"] = str(error)
        if getattr(error, "code", None):
            record["code"] = error.code
        return record
    finally:
        record["elapsed_ms"] = round((time.monotonic() - started) * 1000)


def mcp(arguments, *, engine=None, plan=False, read=False, parser_mode="grammar", host=None, windows_root=None):
    allowed = set() if read else {"request", "actions", "parser"} if plan else {"request", "actions", "parser", "confirm_risky"}
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise ValueError("unknown windows arguments")
    consent = arguments.get("confirm_risky", False)
    if type(consent) is not bool:
        raise ValueError("confirm_risky must be a boolean")
    options = dict(host=host, windows_root=windows_root)
    if read:
        return run(None, calls=[{"name": "window", "arguments": {"action": "list"}}], **options)
    if ("request" in arguments) == ("actions" in arguments):
        raise ValueError("supply exactly one of request or actions")
    if "request" in arguments and not isinstance(arguments["request"], str):
        raise ValueError("request must be a string")
    if "actions" in arguments and "parser" in arguments:
        raise ValueError("parser applies only to natural-language requests")
    if not plan and not consent:
        return dict(schema=job.VERSION, status=1, outcome="blocked", note="windows act requires confirm_risky=true",
                    actions=[], results=[])
    return run(arguments.get("request"), calls=arguments.get("actions"), engine=engine, execute=not plan,
               parser_mode=arguments.get("parser", parser_mode), **options)


def read_actions(path):
    data = sys.stdin.buffer.read(65537) if path == "-" else open(path, "rb").read(65537)
    if len(data) > 65536:
        raise job.WindowsActionError("structured actions exceed 64 KiB")

    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise job.WindowsActionError("duplicate field in structured actions")
            out[key] = value
        return out
    return json.loads(data, object_pairs_hook=unique)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kilix-needle windows", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("request", nargs="*")
    parser.add_argument("--host", help="SSH alias of a Windows machine running Kilix (required outside WSL)")
    parser.add_argument("--windows-root", help="portable Windows package directory on that host")
    parser.add_argument("--root", help="installed Kilix content root (for the model)")
    parser.add_argument("--parser", choices=("grammar", "needle", "hybrid"), default="grammar",
                        help="grammar (default, no model); hybrid: grammar, then stock Needle 2; needle: model only")
    parser.add_argument("--list", action="store_true", help="list titled windows; no model")
    parser.add_argument("--actions-json", metavar="FILE", help="structured action list, or - for stdin")
    parser.add_argument("--schema", action="store_true", help="print the action schema; connects to nothing")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--yes", action="store_true", help="execute the requested actions")
    modes.add_argument("--dry-run", action="store_true", help="preview against the current desktop (default)")
    modes.add_argument("--parse-only", action="store_true", help="plan without connecting to Windows")
    parser.add_argument("--json", action="store_true", help="print a compact JSON receipt")
    parser.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    args = parser.parse_args(argv)
    request = " ".join(args.request) if args.request else None
    if args.schema:
        print(json.dumps(dict(schema=job.VERSION, tools=job.TOOLS, max_actions=job.MAX_ACTIONS,
                              apps={k: v[0] for k, v in job.APPS.items()}), indent=2))
        return 0
    if sum((bool(request), args.list, bool(args.actions_json))) != 1:
        parser.error("choose one request, --list, or --actions-json")
    progress = not args.quiet and sys.stderr.isatty()
    try:
        options = dict(execute=args.yes, parse_only=args.parse_only, host=args.host,
                       windows_root=args.windows_root, parser_mode=args.parser, progress=progress)
        if args.list:
            record = run(None, calls=[{"name": "window", "arguments": {"action": "list"}}], **options)
        elif args.actions_json:
            record = run(None, calls=read_actions(args.actions_json), **options)
        else:
            from needle_cli import LazyRuntime
            with LazyRuntime(lambda: open_runtime(args.root)) as engine:
                record = run(request, engine=engine, **options)
    except (job.WindowsActionError, OSError, ValueError) as error:
        record = dict(schema=job.VERSION, status=1, outcome="refused", note=str(error))
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":") if args.json else None,
                     indent=None if args.json else 2))
    return record["status"]


if __name__ == "__main__":
    raise SystemExit(main())
