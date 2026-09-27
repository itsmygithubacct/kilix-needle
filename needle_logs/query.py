"""Bounded lexical queries and cited, extractive briefing."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re

from .index import Index, SCHEMA_VERSION

MAX_LIMIT = 100
MAX_QUERY = 256
MAX_RENDER = 12000
MAX_JSON_TEXT = 32768
KINDS = {"request", "decision", "change", "test_result", "error", "blocker", "question", "completion", "answer"}


class QueryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def clean(text: str) -> str:
    """Display controls visibly without changing stored evidence."""
    return "".join(c if (c.isprintable() or c == "\n") and c != "\x1b" else f"\\u{ord(c):04x}" for c in text)


def _slice(text: str, maximum: int) -> str:
    return text if len(text) <= maximum else text[:maximum] + " [truncated]"


def _token(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    checksum = hashlib.sha256(raw).hexdigest()[:16]
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + checksum


def _decode(token: str) -> dict:
    if not isinstance(token, str) or not 1 <= len(token) <= 4096:
        raise QueryError("cursor_invalid", "invalid cursor")
    try:
        encoded, checksum = token.split(".", 1)
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if len(raw) > 2048 or hashlib.sha256(raw).hexdigest()[:16] != checksum:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise QueryError("cursor_invalid", "invalid cursor") from exc


def _validate(limit: int, kind: str | None, query: str | None) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise QueryError("invalid_input", f"limit must be 1..{MAX_LIMIT}")
    if kind is not None and kind not in KINDS:
        raise QueryError("invalid_input", "unknown event kind")
    if query is not None and (not isinstance(query, str) or not 1 <= len(query) <= MAX_QUERY):
        raise QueryError("invalid_input", f"query must be 1..{MAX_QUERY} characters")


def bounded(result: dict) -> dict:
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_JSON_TEXT:
        raise QueryError("result_too_large", "response envelope exceeds size bound; narrow the query")
    return result


def list_events(index: Index, source_id: str, *, limit: int = 20, kind: str | None = None,
                query: str | None = None, since_cursor: str | None = None) -> dict:
    _validate(limit, kind, query)
    snap = index.snapshot(source_id)
    if not snap:
        raise QueryError("not_found", "source is not indexed")
    binding = {k: snap[k] for k in ("source_id", "session_id", "generation", "digest", "config")}
    binding["schema_version"] = SCHEMA_VERSION
    binding.update({"kind": kind, "query": query})
    after = None
    if since_cursor is not None:
        cursor = _decode(since_cursor)
        if any(cursor.get(k) != value for k, value in binding.items()):
            raise QueryError("cursor_invalid", "source snapshot, configuration, or filter changed")
        if type(cursor.get("sequence")) is not int or not isinstance(cursor.get("event_id"), str):
            raise QueryError("cursor_invalid", "invalid cursor position")
        after = cursor["sequence"], cursor["event_id"]
    found = index.events(source_id, kind=kind, query=query, after=after, limit=limit + 1)
    more = len(found) > limit
    events = found[:limit]
    next_cursor = None
    if events:
        last = events[-1]
        next_cursor = _token(binding | {"sequence": last["sequence"], "event_id": last["event_id"]})
    return bounded({"events": events, "next_cursor": next_cursor, "has_more": more,
            "source": {k: snap[k] for k in ("source_id", "session_id", "generation", "digest", "path", "provider", "size")},
            "coverage": snap["coverage"], "pipeline": {"config": snap["config"], "schema_version": SCHEMA_VERSION}, "snapshot": {"checkpoint": snap["checkpoint"]}})


def source_event(index: Index, event_id: str) -> dict:
    if not isinstance(event_id, str) or len(event_id) > 128:
        raise QueryError("invalid_input", "invalid event ID")
    pair = index.event(event_id)
    if not pair:
        raise QueryError("not_found", "event not found")
    event, snap = pair
    records = []
    for evidence in event["evidence"]:
        record = index.record(event["source_id"], evidence["record_id"])
        if not record or record["text"][evidence["start"]:evidence["end"]] != evidence["quote"]:
            raise QueryError("index_corrupt", "event evidence no longer resolves")
        if len(record["text"]) > MAX_JSON_TEXT:
            raise QueryError("result_too_large", "record exceeds source response bound")
        records.append(record)
    return bounded({"event": event, "records": records, "source": {k: snap[k] for k in ("source_id", "session_id", "generation", "path", "provider")}, "coverage": snap["coverage"]})


def search(index: Index, source_id: str, term: str, *, limit: int = 20) -> dict:
    _validate(limit, None, term)
    result = list_events(index, source_id, limit=limit, query=term)
    records = index.search_records(source_id, term, limit + 1)
    result["has_more_records"] = len(records) > limit
    matches = []
    for record in records[:limit]:
        text = record["text"]
        position = text.lower().find(term.lower())
        if position < 0:
            continue
        start = max(0, position - 160)
        end = min(len(text), position + len(term) + 160)
        matches.append({"record_id": record["record_id"], "sequence": record["sequence"],
                        "role": record["role"], "channel": record["channel"],
                        "start": start, "end": end, "quote": text[start:end]})
    result["matches"] = matches
    return bounded(result)


def brief(index: Index, source_id: str, *, limit: int = 20) -> dict:
    _validate(limit, None, None)
    result = list_events(index, source_id, limit=1)
    result["events"], omitted = index.recent_events(source_id, limit)
    result["omitted_events"] = omitted
    result["has_more"] = omitted > 0
    result["next_cursor"] = None
    sections = {key: [] for key in ("requests", "decisions", "results", "reported_issues", "other")}
    for event in result["events"]:
        kind = event["kind"]
        section = ("requests" if kind in ("request", "question") else
                   "decisions" if kind == "decision" else
                   "results" if kind in ("change", "test_result", "completion", "answer") else
                   "reported_issues" if kind in ("blocker", "error") else "other")
        evidence = event["evidence"][0]
        label = ("Tool output" if event["evidence_class"] == "structured_fact" else
                 "User said" if event["evidence_class"] == "user_statement" else
                 "Assistant reported" if event["evidence_class"] == "assistant_claim" else "Unattributed text")
        excerpt = clean(evidence["quote"])
        sections[section].append({"event_id": event["event_id"], "label": label, "kind": kind,
                                  "record_id": evidence["record_id"], "excerpt": excerpt[:800],
                                  "excerpt_truncated": len(excerpt) > 800})
    result["sections"] = sections
    return bounded(result)


def render(result: dict, operation: str) -> str:
    if operation == "brief":
        lines = []
        for key, items in result["sections"].items():
            lines.append(key.replace("_", " ").title() + ":")
            lines.extend(f"  {item['label']} [{item['event_id']} / {item['record_id']}]: {item['excerpt']}{' [truncated]' if item['excerpt_truncated'] else ''}" for item in items)
            if not items:
                lines.append("  (none in indexed view)")
        lines.append("Coverage: " + ("complete" if result["coverage"].get("complete", False) else "partial; see JSON coverage for gaps"))
        if result["omitted_events"]:
            lines.append(f"Omitted {result['omitted_events']} older indexed event(s); increase limit or search for earlier evidence.")
        return _slice(clean("\n".join(lines)), MAX_RENDER)
    if operation == "source":
        return _slice(clean("\n".join(record["text"] for record in result["records"])), MAX_RENDER)
    lines = []
    for event in result.get("events", []):
        ref = event["evidence"][0]
        lines.append(f"{event['kind']} [{event['event_id']} / {ref['record_id']}]: {_slice(clean(ref['quote']), 800)}")
    if operation == "search":
        for match in result.get("matches", []):
            lines.append(f"Record ({match['role']}/{match['channel']}) [{match['record_id']}:{match['start']}]: {clean(match['quote'])}")
    if not lines:
        lines.append("No matching events in indexed view.")
    return _slice(clean("\n".join(lines)), MAX_RENDER)
