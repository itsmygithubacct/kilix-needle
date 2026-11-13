"""Read-only baseline logs CLI and JSON-returning dispatch."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import facts, query
from .index import Index
from .hints import journal_hint

SCHEMA = "kilix.logs.read/v1"
ARGUMENTS = {"operation", "file", "provider", "session", "cache_path", "event_id", "session_id",
             "since_cursor", "mode", "limit", "kind", "query", "indexed"}


def _error(code: str, message: str, status: int) -> dict:
    return {"schema": SCHEMA, "status": "error", "exit_status": status,
            "events": [], "errors": [{"code": code, "message": message[:1024]}]}


def _source_error(code, message, status, arguments):
    result = _error(code, message, status)
    operation = arguments.get("operation", "events")
    if operation in ("events", "brief", "search") and (
            (code == "not_found" and arguments.get("session")) or
            (code == "source_open_failed" and arguments.get("file"))):
        result["hint"] = journal_hint(arguments.get("session") or arguments.get("file"))
    return result


def read(arguments: dict) -> dict:
    """Return a JSON-compatible result; import source readers only when needed."""
    if not isinstance(arguments, dict):
        return _error("invalid_input", "arguments must be an object", 2)
    unknown = set(arguments) - ARGUMENTS
    if unknown:
        return _error("invalid_input", "unknown argument key", 2)
    operation = arguments.get("operation", "events")
    if operation not in ("events", "brief", "search", "source", "cache_status", "cache_clear"):
        return _error("invalid_input", "unknown operation", 2)
    applicable = {"events": {"file", "provider", "session", "cache_path", "mode", "limit", "kind", "since_cursor"},
                  "brief": {"file", "provider", "session", "cache_path", "mode", "limit"},
                  "search": {"file", "provider", "session", "cache_path", "mode", "limit", "query"},
                  "source": {"event_id", "cache_path"}, "cache_status": {"cache_path"},
                  "cache_clear": {"cache_path", "session_id"}}
    if operation in ("events", "brief", "search"):
        applicable[operation].add("indexed")
    if set(arguments) - applicable[operation] - {"operation"}:
        return _error("invalid_input", "argument does not apply to operation", 2)
    path = arguments.get("file")
    provider = arguments.get("provider")
    try:
        for key, maximum in (("file", 4096), ("provider", 32), ("session", 256),
                             ("cache_path", 4096), ("event_id", 128), ("session_id", 256),
                             ("since_cursor", 4096), ("query", query.MAX_QUERY),
                             ("kind", 64), ("mode", 32)):
            value = arguments.get(key)
            if value is not None and (not isinstance(value, str) or not value or len(value) > maximum):
                return _error("invalid_input", f"invalid {key}", 2)
        if arguments.get("mode", "baseline") != "baseline":
            return _error("mode_unavailable", "only baseline mode is implemented", 2)
        provider = "omp" if provider == "qwen" else provider
        if "indexed" in arguments and type(arguments["indexed"]) is not bool:
            return _error("invalid_input", "indexed must be boolean", 2)
        if "limit" in arguments and (type(arguments["limit"]) is not int or not 1 <= arguments["limit"] <= query.MAX_LIMIT):
            return _error("invalid_input", "invalid limit", 2)
        if operation in ("events", "brief", "search"):
            query._validate(arguments.get("limit", 20), arguments.get("kind"), arguments.get("query") if operation == "search" else None)
            if operation == "search" and arguments.get("query") is None:
                return _error("invalid_input", "search requires query", 2)
        if operation in ("events", "brief", "search"):
            session = arguments.get("session")
            if bool(path) == bool(session):
                result = _error("invalid_input", "select exactly one file or session", 2)
                if not path and not session:
                    result["hint"] = journal_hint(arguments.get("query"))
                return result
            expected_session = None
            if session:
                from .resolve import resolve_source, check_binding
                if session.startswith("log-"):
                    from .library import Library
                    with Index(arguments.get("cache_path")) as catalog:
                        resolved = Library(catalog).binding(session)
                    if resolved is None:
                        return _error("not_found", "inventory ID is not known; run logs list", 1)
                else:
                    resolved = resolve_source(session)
                path, provider = resolved["path"], resolved["provider"]
                expected_session = resolved.get("expected_session_id") if resolved.get("source_kind") == "structured" else None
            if not path or not provider:
                return _error("invalid_input", "file and provider are required", 2)
            if provider == "grok" and not session:
                from .resolve import grok_updates
                path = grok_updates(path)
            from .index import default_path
            cache_path = Path(arguments.get("cache_path") or default_path())
            if Path(path).resolve() == cache_path.resolve():
                return _error("invalid_input", "source is the cache database", 2)
            if Path(path).exists() and cache_path.exists() and Path(path).stat().st_dev == cache_path.stat().st_dev and Path(path).stat().st_ino == cache_path.stat().st_ino:
                return _error("invalid_input", "source and cache are the same file", 2)
        with Index(arguments.get("cache_path"), source_path=path if operation in ("events", "brief", "search") else None) as index:
            if operation == "cache_status":
                return query.bounded({"schema": SCHEMA, "status": "ok", "exit_status": 0, "sources": index.status(), "errors": []})
            if operation == "cache_clear":
                count = index.clear(arguments.get("session_id"))
                return {"schema": SCHEMA, "status": "ok", "exit_status": 0, "cleared": count, "errors": []}
            if operation == "source":
                event_id = arguments.get("event_id")
                if not event_id:
                    return _error("invalid_input", "source requires event_id", 2)
                result = query.source_event(index, event_id)
            else:
                query._validate(arguments.get("limit", 20), arguments.get("kind"),
                                arguments.get("query") if operation == "search" else None)
                if operation == "search" and arguments.get("query") is None:
                    return _error("invalid_input", "search requires query", 2)
                # CLI reads use the persistent full-history index. Older API
                # clients also use it for large logs or an already indexed file.
                indexed = False
                if provider in {"claude", "codex", "grok", "omp", "raw"}:
                    from .schema import DEFAULT_MAX_BYTES
                    from .library import Library, has_index
                    try:
                        source_size = Path(path).stat().st_size
                    except OSError as exc:
                        return _source_error("source_open_failed", str(exc), 1, arguments)
                    indexed = (arguments.get("indexed", False) or path.endswith(".zst") or source_size > DEFAULT_MAX_BYTES
                               or has_index(index, path, provider))
                if indexed:
                    library = Library(index)
                    item = library.inventory.describe(path, provider)
                    source_result = library.refresh(item, binding=resolved if session and "root" in resolved else None)
                else:
                    from .sources import read_source
                    source_result = (read_source(path, provider, binding=resolved)
                                     if session and "root" in resolved else read_source(path, provider))
                if (source_result["source"]["provider"] != provider or
                        (source_result["source"]["path"] != str(Path(path).absolute()) if session
                         else Path(source_result["source"]["path"]).resolve() != Path(path).resolve())):
                    return _error("source_mismatch", "reader returned another source", 1)
                if expected_session and source_result["source"]["session_id"] != expected_session:
                    return _error("source_mismatch", "resolved session does not match source", 1)
                if session:
                    check_binding(resolved)
                if not indexed:
                    events = facts.extract(source_result["records"])
                    config = facts.VERSION + ":record-v1:" + str(source_result["source"].get("provider", provider))
                    index.put(source_result, events, config=config)
                source_id = source_result["source"]["source_id"]
                limit = arguments.get("limit", 20)
                # Room for the fields added below, so a trimmed page still fits.
                reserve = len(json.dumps(query.bounded({
                    "schema": SCHEMA, "status": "partial", "exit_status": 1, "operation": operation,
                    "errors": source_result.get("errors", [])}), ensure_ascii=False).encode("utf-8"))
                if operation == "brief":
                    result = query.brief(index, source_id, limit=limit, reserve=reserve)
                else:
                    term = arguments.get("query") if operation == "search" else None
                    if operation == "search" and term is None:
                        return _error("invalid_input", "search requires query", 2)
                    if operation == "search":
                        result = query.search(index, source_id, term, limit=limit)
                    else:
                        result = query.list_events(index, source_id, limit=limit, kind=arguments.get("kind"),
                                                   since_cursor=arguments.get("since_cursor"), reserve=reserve)
                result["errors"] = source_result.get("errors", [])
            coverage = result.get("coverage", {})
            partial = bool(result.get("errors")) or not coverage.get("complete", False)
            return query.bounded({"schema": SCHEMA, "status": "partial" if partial else "ok",
                    "exit_status": 1 if partial else 0, **result,
                    "errors": result.get("errors", []), "operation": operation})
    except query.QueryError as exc:
        return _error(exc.code, str(exc), 2 if exc.code in ("invalid_input", "cursor_invalid") else 1)
    except (ValueError, TypeError, KeyError) as exc:
        code = getattr(exc, "code", "invalid_input")
        return _source_error(code, str(exc), 1 if code in (
            "source_changed", "source_unavailable", "snapshot_unavailable",
            "snapshot_timeout", "snapshot_limit", "caller_context_unverified",
            "pane_context_unavailable") else 2, arguments)
    except (OSError, ImportError, RuntimeError) as exc:
        return _source_error(getattr(exc, "code", "read_error"), str(exc), 1, arguments)
    except Exception as exc:
        return _source_error(getattr(exc, "code", "read_error"), str(exc), 1, arguments)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kilix-needle logs")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("events", "brief", "search"):
        command = sub.add_parser(name)
        target = command.add_mutually_exclusive_group(required=name != "search")
        target.add_argument("--file")
        target.add_argument("--session")
        command.add_argument("--provider")
        command.add_argument("--mode", default="baseline")
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--json", action="store_true")
        command.add_argument("--cache-path")
        if name == "events":
            command.add_argument("--kind")
            command.add_argument("--since-cursor")
        if name == "search":
            command.add_argument("--query", required=True)
            command.add_argument("--rank", choices=("bm25", "recent"), default="bm25")
            command.add_argument("--match", choices=("all", "any"), default="all")
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--cached", action="store_true", help="search the index without reading log files")
    for name in ("list", "index"):
        command = sub.add_parser(name, help="discover sessions" if name == "list" else "update the full-history index")
        command.add_argument("--provider")
        command.add_argument("--cache-path")
        command.add_argument("--json", action="store_true")
        if name == "list":
            command.add_argument("--query")
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--cached", action="store_true")
        else:
            target = command.add_mutually_exclusive_group()
            target.add_argument("--file")
            target.add_argument("--session")
            command.add_argument("--rebuild", action="store_true")
    show = sub.add_parser("show", help="read an indexed record, with character pagination")
    show.add_argument("record_id")
    show.add_argument("--start", type=int, default=0)
    show.add_argument("--chars", type=int, default=8000)
    show.add_argument("--cache-path")
    show.add_argument("--json", action="store_true")
    source = sub.add_parser("source")
    source.add_argument("--event", dest="event_id", required=True)
    source.add_argument("--cache-path")
    source.add_argument("--json", action="store_true")
    cache = sub.add_parser("cache")
    cache_sub = cache.add_subparsers(dest="cache_operation", required=True)
    for name in ("status", "clear"):
        command = cache_sub.add_parser(name)
        command.add_argument("--cache-path")
        command.add_argument("--json", action="store_true")
        if name == "clear":
            command.add_argument("--session", dest="session_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = vars(_parser().parse_args(argv))
    operation = args.pop("operation")
    if operation == "cache":
        operation = "cache_" + args.pop("cache_operation")
    as_json = args.pop("json", False)
    if operation in ("list", "index", "search", "show"):
        from . import library_cli
        result = library_cli.read({"operation": operation, **args, "progress": sys.stderr.isatty()})
        print(json.dumps(result, ensure_ascii=False) if as_json else library_cli.render(result))
        return result["exit_status"]
    if operation in ("events", "brief"):
        args["indexed"] = True
    result = read({"operation": operation, **args})
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
    elif result["status"] == "error":
        print(f"logs: {query.clean(str(result['errors'][0]['code']))}: {query.clean(str(result['errors'][0]['message']))}", file=sys.stderr)
    elif operation == "cache_status":
        for source in result["sources"]:
            print(f"{query.clean(source['source_id'])} {query.clean(source['session_id'])} {query.clean(source['path'])}")
    elif operation == "cache_clear":
        print(f"Cleared {result['cleared']} source(s).")
    else:
        print(query.render(result, operation))
        if result["status"] == "partial":
            print("logs: partial coverage; inspect --json for gaps and errors", file=sys.stderr)
    return result["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
