"""Read-only baseline logs CLI and JSON-returning dispatch."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import facts, query
from .index import Index

SCHEMA = "kilix.logs.read/v1"
ARGUMENTS = {"operation", "file", "provider", "session", "cache_path", "event_id", "session_id",
             "since_cursor", "mode", "limit", "kind", "query"}


def _error(code: str, message: str, status: int) -> dict:
    return {"schema": SCHEMA, "status": "error", "exit_status": status,
            "events": [], "errors": [{"code": code, "message": message[:1024]}]}


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
        if "limit" in arguments and (type(arguments["limit"]) is not int or not 1 <= arguments["limit"] <= query.MAX_LIMIT):
            return _error("invalid_input", "invalid limit", 2)
        if operation in ("events", "brief", "search"):
            query._validate(arguments.get("limit", 20), arguments.get("kind"), arguments.get("query") if operation == "search" else None)
            if operation == "search" and arguments.get("query") is None:
                return _error("invalid_input", "search requires query", 2)
        if operation in ("events", "brief", "search"):
            session = arguments.get("session")
            if bool(path) == bool(session):
                return _error("invalid_input", "select exactly one file or session", 2)
            expected_session = None
            if session:
                from .resolve import resolve_source, check_binding
                resolved = resolve_source(session)
                path, provider = resolved["path"], resolved["provider"]
                expected_session = resolved.get("expected_session_id") if resolved.get("source_kind") == "structured" else None
            if not path or not provider:
                return _error("invalid_input", "file and provider are required", 2)
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
                # This path is intentionally explicit; coordinator adds session resolution.
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
                events = facts.extract(source_result["records"])
                config = facts.VERSION + ":record-v1:" + str(source_result["source"].get("provider", provider))
                index.put(source_result, events, config=config)
                source_id = source_result["source"]["source_id"]
                limit = arguments.get("limit", 20)
                if operation == "brief":
                    result = query.brief(index, source_id, limit=limit)
                else:
                    term = arguments.get("query") if operation == "search" else None
                    if operation == "search" and term is None:
                        return _error("invalid_input", "search requires query", 2)
                    if operation == "search":
                        result = query.search(index, source_id, term, limit=limit)
                    else:
                        result = query.list_events(index, source_id, limit=limit, kind=arguments.get("kind"),
                                                   since_cursor=arguments.get("since_cursor"))
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
        return _error(code, str(exc), 1 if code in ("source_changed", "source_unavailable", "snapshot_unavailable", "snapshot_timeout", "snapshot_limit") else 2)
    except (OSError, ImportError, RuntimeError) as exc:
        return _error(getattr(exc, "code", "read_error"), str(exc), 1)
    except Exception as exc:
        return _error(getattr(exc, "code", "read_error"), str(exc), 1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kilix-needle logs")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("events", "brief", "search"):
        command = sub.add_parser(name)
        target = command.add_mutually_exclusive_group(required=True)
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
