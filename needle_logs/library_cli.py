"""CLI and Python dispatch for the local, persistent log library."""
from __future__ import annotations

from collections import Counter
import json
import os
import sys
import time

from .index import Index
from .library import Library, SCHEMA
from . import query


def read(arguments: dict) -> dict:
    operation = arguments.get("operation")
    errors = []
    try:
        if operation not in ("list", "index", "search", "show"):
            raise ValueError("unsupported library operation")
        if operation == "search" and not arguments.get("query"):
            raise ValueError("search requires query")
        if arguments.get("mode", "baseline") != "baseline":
            raise ValueError("only baseline mode is available")
        limit, offset = arguments.get("limit", 20), arguments.get("offset", 0)
        query._validate(limit, None, arguments.get("query"))
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if arguments.get("rank", "bm25") not in ("bm25", "recent") or arguments.get("match", "all") not in ("all", "any"):
            raise ValueError("unsupported search rank or match mode")
        with Index(arguments.get("cache_path"), source_path=arguments.get("file")) as index:
            library = Library(index)
            provider = arguments.get("provider")
            provider = "omp" if provider == "qwen" else provider
            if provider is not None and provider not in library.inventory.PROVIDERS:
                raise ValueError("unsupported provider")
            if operation == "show":
                start, chars = arguments.get("start", 0), arguments.get("chars", 8000)
                if type(start) is not int or start < 0 or type(chars) is not int or not 1 <= chars <= 8000:
                    raise ValueError("start must be nonnegative and chars must be 1..8000")
                result = library.record(arguments["record_id"], start=start, chars=chars)
                # UTF-8 expansion can exceed the envelope despite the character
                # bound. Shorten the text and retain an exact continuation.
                while len(json.dumps(result, ensure_ascii=False).encode()) > 28000:
                    if not result["text"]:
                        raise query.QueryError("result_too_large", "record metadata exceeds response limit")
                    result["text"] = result["text"][:max(0, len(result["text"]) // 2)]
                    result["end"] = result["start"] + len(result["text"])
                    result["next_start"] = result["end"]
                return query.bounded({"schema": SCHEMA, "status": "ok", "exit_status": 0,
                                      "operation": operation, "errors": [], **result})
            path, session = arguments.get("file"), arguments.get("session")
            if path and session:
                raise ValueError("select at most one file or session")
            binding = None
            if session:
                from .resolve import resolve_source
                binding = library.binding(session, provider) or resolve_source(session)
                path, provider = binding["path"], binding["provider"]
            if path:
                if not provider:
                    raise ValueError("file requires provider")
                if provider == "grok":
                    from .resolve import grok_updates
                    path = grok_updates(path)
                if arguments.get("cached"):
                    stored = index.db.execute("SELECT metadata FROM log_inventory WHERE path=? AND provider=?",
                                              (os.path.abspath(os.path.expanduser(path)), provider)).fetchone()
                    if stored is None:
                        raise query.QueryError("not_found", "selected file is not indexed")
                    item = json.loads(stored[0])
                else:
                    item = library.inventory.describe(path, provider)
                items = [item]
                if not arguments.get("cached"):
                    with index.db:
                        library._remember(item)
            elif not arguments.get("cached"):
                discovered = library.discover(provider)
                items = discovered["sessions"]
                errors.extend(discovered["errors"])
            else:
                items = []
            if operation == "list":
                result = library.list(provider=provider, term=arguments.get("query"), limit=limit, offset=offset)
            else:
                counts = Counter()
                source_id = None
                previous = time.monotonic()
                for number, item in enumerate([] if arguments.get("cached") else items):
                    try:
                        updated = library.refresh(item, binding=binding, rebuild=bool(arguments.get("rebuild")))
                        counts[updated["index_action"]] += 1
                        counts["added_records"] += updated["added_records"]
                        if not updated["coverage"]["complete"]:
                            counts["partial_sources"] += 1
                        if path:
                            source_id = updated["source"]["source_id"]
                    except Exception as exc:
                        # A failed file must not hide other providers. Individual
                        # source transactions retain their last successful data.
                        counts["failed"] += 1
                        if len(errors) < 20:
                            errors.append({"code": getattr(exc, "code", "index_failed"),
                                           "path": item["path"], "message": str(exc)[:512]})
                    if arguments.get("progress") and time.monotonic() - previous > 2:
                        print(f"Indexed {number + 1}/{len(items)} sources; {counts['cached']} unchanged", file=sys.stderr)
                        previous = time.monotonic()
                summary = {"discovered": len(items), **dict(counts)}
                if operation == "index":
                    result = {"index": summary}
                else:
                    if not arguments.get("query"):
                        raise ValueError("search requires query")
                    if path and source_id is None:
                        if errors:
                            raise ValueError("selected source could not be indexed: " + errors[0]["message"])
                        row = index.db.execute("SELECT p.source_id FROM log_inventory i JOIN log_progress p USING(inventory_id) "
                                               "WHERE i.path=? AND i.provider=?", (items[0]["path"], provider)).fetchone()
                        source_id = row[0] if row else None
                        if source_id is None:
                            raise ValueError("selected source is not indexed")
                    with index.read_transaction():
                        result = library.search(arguments["query"], provider=provider, source_id=source_id,
                            limit=limit, offset=offset, rank=arguments.get("rank", "bm25"), match=arguments.get("match", "all"))
                    result["index"] = summary
            partial = bool(errors or result.get("index", {}).get("failed") or result.get("index", {}).get("partial_sources")
                           or result.get("unindexed_sources") or result.get("partial_sources"))
            envelope = {"schema": SCHEMA, "status": "partial" if partial else "ok", "exit_status": int(partial),
                        "operation": operation, "errors": errors[:20], "errors_total": len(errors),
                        "freshness": "cached" if arguments.get("cached") else "refreshed", **result}
            # Bound responses, not the indexed history. The next offset always
            # continues after the rows actually returned.
            key = "sessions" if operation == "list" else "matches"
            while len(json.dumps(envelope, ensure_ascii=False).encode()) > query.MAX_JSON_TEXT:
                rows = envelope.get(key)
                if not rows:
                    raise query.QueryError("result_too_large", "response metadata exceeds size bound")
                if len(rows) == 1:
                    raise query.QueryError("result_too_large", "one result exceeds size bound; narrow the query")
                rows.pop()
                envelope["next_offset"] = offset + len(rows)
            return query.bounded(envelope)
    except Exception as exc:
        code = getattr(exc, "code", "invalid_input" if isinstance(exc, (ValueError, TypeError)) else "read_error")
        return {"schema": SCHEMA, "status": "error", "exit_status": 2 if code == "invalid_input" else 1,
                "operation": operation, "errors": [{"code": code, "message": str(exc)[:1024]}]}


def render(result: dict) -> str:
    if result["status"] == "error":
        return query.clean("logs: " + result["errors"][0]["message"])
    operation = result["operation"]
    if operation == "show":
        return query.clean(result["text"]) + (f"\nContinue with --start {result['next_start']}" if result["next_start"] is not None else "")
    if operation == "index":
        return "Index: " + ", ".join(f"{key}={value}" for key, value in result["index"].items())
    rows = ([f"{len(result['sessions'])} of {result['total']} sessions · newest first"] if operation == "list" else [])
    if result["status"] == "partial":
        rows.append(f"Partial coverage: {result.get('unindexed_sources', 0)} unindexed sources, "
                    f"{result.get('partial_sources', 0)} sources with gaps; use --json for details.")
    for item in result.get("sessions", result.get("matches", [])):
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["updated"]))
        identity = item.get("record_id") or item["inventory_id"]
        score = f"  score={item['score']:.3g}" if operation == "search" else ""
        rows.append(f"{stamp}  {item['provider']:6}  {item['title']}  [{identity}]{score}")
        if operation == "search":
            rows.append("  " + item["snippet"])
        elif item.get("cwd"):
            rows.append("  " + item["cwd"])
    if result.get("next_offset") is not None:
        rows.append(f"Continue with --offset {result['next_offset']}")
    return query.clean("\n".join(rows) or "No matching logs.")
