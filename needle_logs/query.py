"""Bounded lexical queries and cited, extractive briefing.

Every response fits MAX_JSON_TEXT bytes of UTF-8 JSON. An event too large for
any response by itself is returned abridged rather than refused: each long
evidence item keeps its exact record span but omits its quote, which is never
shortened in place, and carries an exact excerpt instead. A source whose
records do not fit is returned in exact chunks with a continuation cursor.
Offsets count Unicode code points, as Python string indices do.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import Counter

from .index import Index, SCHEMA_VERSION, consistent_read

MAX_LIMIT = 100
MAX_QUERY = 256
MAX_RENDER = 12000
MAX_JSON_TEXT = 32768
# The longest excerpt an abridged evidence item carries, in characters.
EXCERPT_CHARS = 2000
KINDS = {"request", "decision", "change", "test_result", "error", "blocker", "question", "completion", "answer", "transcript"}


class QueryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def clean(text: str) -> str:
    """Display controls visibly without changing stored evidence."""
    return "".join(c if (c.isprintable() or c == "\n") and c != "\x1b" else f"\\u{ord(c):04x}" for c in text)


def _render_slice(text: str) -> str:
    # Reserve one byte for print's trailing newline; marker is part of the cap.
    raw = text.encode("utf-8")
    maximum = MAX_RENDER - 1
    if len(raw) <= maximum:
        return text
    marker = b" [truncated]"
    return raw[:maximum - len(marker)].decode("utf-8", errors="ignore") + marker.decode()


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


def bounded(result: dict, *, reserve: int = 0) -> dict:
    # Long transcripts can have hundreds of unsupported rows. Keep useful
    # evidence available while explicitly accounting for omitted diagnostics.
    result = dict(result)
    coverage = result.get("coverage")
    if isinstance(coverage, dict):
        coverage = dict(coverage)
        gaps = coverage.get("gaps")
        if isinstance(gaps, list) and len(gaps) > 32:
            coverage.update(gaps=gaps[:20], gaps_total=len(gaps), gaps_omitted=len(gaps) - 20,
                            gap_counts=dict(Counter(str(g.get("code", "unknown")) for g in gaps)))
        result["coverage"] = coverage
    errors = result.get("errors")
    if isinstance(errors, list) and len(errors) > 32:
        result.update(errors=errors[:20], errors_total=len(errors), errors_omitted=len(errors) - 20,
                      error_counts=dict(Counter(str(e.get("code", "unknown")) for e in errors)))
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_JSON_TEXT - reserve:
        raise QueryError("result_too_large", "response envelope exceeds size bound; narrow the query")
    return result


def _fits(result: dict, reserve: int = 0) -> dict | None:
    """The bounded result, or None when it is too large; other errors raise."""
    try:
        return bounded(result, reserve=reserve)
    except QueryError as exc:
        if exc.code != "result_too_large":
            raise
        return None


def _abridge(event: dict, cap: int = EXCERPT_CHARS, keep: int | None = None) -> dict:
    """The event with every quote longer than ``keep`` (default ``cap``) characters omitted.

    An omitted quote keeps its record_id, start and end, the exact cited span,
    and gains quote_omitted plus an exact excerpt of its first ``cap``
    characters, possibly none: record text[start:excerpt_end]. A quote that is
    present is therefore always the complete cited text. The source operation
    returns the rest.
    """
    keep = cap if keep is None else keep
    evidence = []
    for item in event["evidence"]:
        if len(item["quote"]) <= keep:
            evidence.append(item)
            continue
        excerpt = item["quote"][:cap]
        evidence.append({key: value for key, value in item.items() if key != "quote"} |
                        {"quote_omitted": True, "excerpt": excerpt, "excerpt_end": item["start"] + len(excerpt)})
    return dict(event, evidence=evidence)


def _abridged(event: dict) -> bool:
    return any(item.get("quote_omitted") for item in event["evidence"])


def _too_long(events: list[dict]) -> bool:
    # Every character costs at least one byte of JSON: a cheap, exact refusal
    # that spares serializing a multi-megabyte quote only to measure it.
    return sum(len(item.get("quote", "")) for event in events for item in event["evidence"]) > MAX_JSON_TEXT


def _shrink(event: dict, fits) -> dict | None:
    """The event whole if ``fits`` it, else abridged with the longest excerpt that fits.

    None when even bare references to its evidence do not fit.
    """
    if not _too_long([event]) and fits(event):
        return event
    candidate = _abridge(event)
    if fits(candidate):
        return candidate
    low, high, best = 0, EXCERPT_CHARS - 1, None
    while low <= high:
        middle = (low + high) // 2
        candidate = _abridge(event, middle)
        if fits(candidate):
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    return best


def _fit_events(build, events: list[dict], reserve: int = 0, *, newest: bool = False) -> dict:
    """The bounded result holding the most of ``events`` that fits.

    ``build(chosen)`` returns the unbounded result for a list of events: the
    first of ``events`` or, with ``newest``, the last. When all of them fit,
    the result is unchanged. Otherwise each event too large for a response by
    itself is abridged, never dropped, so every page holds at least one event
    and a cursor always moves past a large record. Pages are then trimmed to
    fit as before.
    """
    whole = None if _too_long(events) else _fits(build(events), reserve)
    if whole is not None:
        return whole
    if not events:
        raise QueryError("result_too_large", "response envelope exceeds size bound; narrow the query")

    def alone(event: dict) -> bool:
        return _fits(build([event]), reserve) is not None

    prepared = []
    for event in (reversed(events) if newest else events):
        shown = _shrink(event, alone)
        if shown is None:
            break       # Later events wait for a page that starts at this one.
        prepared.append(shown)
    if not prepared:
        # Not even references to this event's evidence fit: an empty page
        # could not advance, so refuse rather than stall the cursor.
        raise QueryError("result_too_large", "event metadata exceeds size bound; narrow the query")
    if newest:
        prepared.reverse()
    pick = (lambda n: prepared[len(prepared) - n:]) if newest else (lambda n: prepared[:n])
    return _fitting(lambda n: build(pick(n)), len(prepared), reserve)


def _fitting(build, count: int, reserve: int = 0) -> dict:
    """The bounded result holding the most of ``count`` events that fits.

    ``build(n)`` returns the unbounded result for the first ``n`` events. A
    long transcript's events can be larger than one response; returning fewer
    of them, marked by has_more, beats refusing the whole read. Diagnostics too
    large for any response are still refused. ``reserve`` is room the caller
    still needs for its own fields.
    """
    try:
        return bounded(build(count), reserve=reserve)
    except QueryError as exc:
        if exc.code != "result_too_large" or count <= 1:
            raise
    low, high, best = 1, count - 1, None
    while low <= high:
        middle = (low + high) // 2
        try:
            best = bounded(build(middle), reserve=reserve)
            low = middle + 1
        except QueryError as exc:
            if exc.code != "result_too_large":
                raise
            high = middle - 1
    if best is None:
        raise QueryError("result_too_large", "response envelope exceeds size bound; narrow the query")
    return best


def _envelope(index: Index, source_id: str) -> dict:
    snap = index.snapshot(source_id)
    if not snap:
        raise QueryError("not_found", "source is not indexed")
    return {"events": [], "next_cursor": None, "has_more": False,
            "source": {k: snap[k] for k in ("source_id", "session_id", "generation", "digest", "path", "provider", "size")},
            "coverage": snap["coverage"], "pipeline": {"config": snap["config"], "schema_version": SCHEMA_VERSION},
            "snapshot": {"checkpoint": snap["checkpoint"]}}


@consistent_read
def list_events(index: Index, source_id: str, *, limit: int = 20, kind: str | None = None,
                query: str | None = None, since_cursor: str | None = None, reserve: int = 0) -> dict:
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
        if cursor.get("op") is not None:
            raise QueryError("cursor_invalid", "cursor belongs to another operation")
        if any(cursor.get(k) != value for k, value in binding.items()):
            raise QueryError("cursor_invalid", "source snapshot, configuration, or filter changed")
        if type(cursor.get("sequence")) is not int or not isinstance(cursor.get("event_id"), str):
            raise QueryError("cursor_invalid", "invalid cursor position")
        after = cursor["sequence"], cursor["event_id"]
    found = index.events(source_id, kind=kind, query=query, after=after, limit=limit + 1)
    page = found[:limit]
    def build(events: list[dict]) -> dict:
        # A page cut short to fit still pages exactly: the cursor names the
        # last event returned, abridged or not, and has_more says the rest remain.
        count = len(events)
        next_cursor = None
        if events:
            last = events[-1]
            next_cursor = _token(binding | {"sequence": last["sequence"], "event_id": last["event_id"]})
        result = {"events": events, "next_cursor": next_cursor, "has_more": len(found) > count,
                  "source": {k: snap[k] for k in ("source_id", "session_id", "generation", "digest", "path", "provider", "size")},
                  "coverage": snap["coverage"], "pipeline": {"config": snap["config"], "schema_version": SCHEMA_VERSION}, "snapshot": {"checkpoint": snap["checkpoint"]}}
        if count < len(page):
            result["events_trimmed_to_fit"] = len(page) - count
        abridged = sum(map(_abridged, events))
        if abridged:
            result["events_abridged"] = abridged
        return result
    return _fit_events(build, page, reserve)


def _source_snapshot(event: dict, records: list[dict], snap: dict) -> str:
    """What a source cursor is valid for: this event's records in this snapshot."""
    binding = {k: snap[k] for k in ("source_id", "session_id", "generation", "digest", "config")}
    binding.update(schema_version=SCHEMA_VERSION, event_id=event["event_id"],
                   records=[[record["record_id"], len(record["text"])] for record in records])
    raw = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _source_position(token: str, event: dict, records: list[dict], snapshot: str) -> tuple[int, int]:
    cursor = _decode(token)
    if cursor.get("op") != "source" or cursor.get("event_id") != event["event_id"]:
        raise QueryError("cursor_invalid", "cursor belongs to another event or operation")
    if cursor.get("snapshot") != snapshot:
        raise QueryError("cursor_invalid", "source snapshot or configuration changed")
    ordinal, offset = cursor.get("evidence"), cursor.get("offset")
    if (type(ordinal) is not int or type(offset) is not int or not 0 <= ordinal < len(records)
            or not 0 <= offset < len(records[ordinal]["text"])):
        raise QueryError("cursor_invalid", "invalid cursor position")
    return ordinal, offset


def _source_chunks(event: dict, records: list[dict], envelope: dict, position: tuple[int, int],
                   snapshot: str, accept) -> dict:
    """The longest exact run of record text from ``position`` that ``accept`` takes.

    The records' texts are read in evidence order as one stream. Each response
    holds at least one character, so following next_cursor always finishes.
    """
    texts = [record["text"] for record in records]
    first, offset = position
    remaining = len(texts[first]) - offset + sum(map(len, texts[first + 1:]))

    def build(shown: dict, take: int) -> dict:
        entries, ordinal, start, left = [], first, offset, take
        while left > 0:
            text = texts[ordinal]
            end = min(len(text), start + left)
            entries.append({key: value for key, value in records[ordinal].items() if key != "text"} |
                           {"evidence_index": ordinal, "chunk_start": start, "chunk_end": end,
                            "total_chars": len(text), "chunk": text[start:end]})
            left -= end - start
            ordinal, start = (ordinal + 1, 0) if end == len(text) else (ordinal, end)
        more = ordinal < len(texts)
        cursor = _token({"op": "source", "event_id": event["event_id"], "evidence": ordinal,
                         "offset": start, "snapshot": snapshot}) if more else None
        return {"event": shown, "records": entries, "next_cursor": cursor, "has_more": more, **envelope}

    def most(shown: dict) -> dict | None:
        # Each character costs at least one byte, so no more than the bound fits.
        if remaining <= MAX_JSON_TEXT:
            whole = accept(build(shown, remaining))
            if whole is not None:
                return whole
        low, high, best = 1, min(remaining - 1, MAX_JSON_TEXT), None
        while low <= high:
            middle = (low + high) // 2
            candidate = accept(build(shown, middle))
            if candidate is not None:
                best, low = candidate, middle + 1
            else:
                high = middle - 1
        return best

    # The chunks carry the text, so a long quote is only a reference here: an
    # excerpt would repeat it and crowd out the text each response can carry.
    result = most(_abridge(event, 0, EXCERPT_CHARS)) or most(_abridge(event, 0))
    if result is None:
        raise QueryError("result_too_large", "source metadata exceeds size bound")
    return result


@consistent_read
def source_event(index: Index, event_id: str, *, since_cursor: str | None = None,
                 reserve: int = 0, fits=None) -> dict:
    """The event and every record it cites.

    When they fit one response the result is unchanged: the event and whole
    records. Otherwise, or when continuing from ``since_cursor``, each quote
    longer than EXCERPT_CHARS becomes a reference (quote_omitted, an empty
    excerpt) and the records arrive as exact chunks: record metadata with
    evidence_index, chunk_start, chunk_end (characters), total_chars and chunk,
    plus has_more and next_cursor. ``fits`` is an extra test for each
    candidate response, such as the human renderer's bound.
    """
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
        records.append(record)
    envelope = {"source": {k: snap[k] for k in ("source_id", "session_id", "generation", "path", "provider")},
                "coverage": snap["coverage"]}

    def accept(result: dict) -> dict | None:
        shown = _fits(result, reserve)
        return shown if shown is not None and (fits is None or fits(shown)) else None

    snapshot = _source_snapshot(event, records, snap)
    if since_cursor is None:
        long = _too_long([event]) or sum(len(record["text"]) for record in records) > MAX_JSON_TEXT
        whole = None if long else accept({"event": event, "records": records, **envelope})
        if whole is not None:
            return whole
        position = (0, 0)
    else:
        position = _source_position(since_cursor, event, records, snapshot)
    return _source_chunks(event, records, envelope, position, snapshot, accept)


@consistent_read
def search(index: Index, source_id: str, term: str, *, limit: int = 20) -> dict:
    _validate(limit, None, term)
    result = _envelope(index, source_id)
    events = index.events(source_id, query=term, limit=limit + 1)
    # Search returns references, not shortened objects masquerading as full events.
    # Full evidence remains available through event/source lookup.
    result["event_matches"] = [{k: event[k] for k in
                                ("event_id", "kind", "sequence", "evidence_class")}
                               for event in events[:limit]]
    result["has_more"] = len(events) > limit
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


@consistent_read
def brief(index: Index, source_id: str, *, limit: int = 20, reserve: int = 0) -> dict:
    _validate(limit, None, None)
    envelope = _envelope(index, source_id)
    recent, omitted = index.recent_events(source_id, limit)
    def build(events: list[dict]) -> dict:
        # The brief keeps the newest events; any dropped to fit the response
        # join the older ones counted in omitted_events. A large one is
        # abridged instead, so it cannot crowd every older event out.
        count = len(events)
        result = dict(envelope)
        result["events"] = events
        result["omitted_events"] = omitted + len(recent) - count
        result["has_more"] = result["omitted_events"] > 0
        result["next_cursor"] = None
        if count < len(recent):
            result["events_trimmed_to_fit"] = len(recent) - count
        abridged = sum(map(_abridged, events))
        if abridged:
            result["events_abridged"] = abridged
        result["sections"] = _sections(result["events"])
        return result
    return _fit_events(build, recent, reserve, newest=True)


def _evidence_text(item: dict) -> tuple[str, bool]:
    """An evidence item's quote, or its excerpt when the quote was omitted."""
    return (item["quote"], True) if "quote" in item else (item["excerpt"], False)


def _excerpt(text: str, complete: bool, maximum: int = 800) -> tuple[str, bool]:
    # Each character cleans to at least one, so the first maximum + 1 decide.
    shown = clean(text[:maximum + 1])
    return shown[:maximum], len(shown) > maximum or not complete


def _sections(events: list[dict]) -> dict:
    sections = {key: [] for key in ("requests", "decisions", "results", "reported_issues", "other")}
    for event in events:
        kind = event["kind"]
        section = ("requests" if kind in ("request", "question") else
                   "decisions" if kind == "decision" else
                   "results" if kind in ("change", "test_result", "completion", "answer") else
                   "reported_issues" if kind in ("blocker", "error") else "other")
        evidence = event["evidence"][0]
        label = ("Tool output" if event["evidence_class"] == "structured_fact" else
                 "User said" if event["evidence_class"] == "user_statement" else
                 "Assistant reported" if event["evidence_class"] == "assistant_claim" else "Unattributed text")
        excerpt, truncated = _excerpt(*_evidence_text(evidence))
        sections[section].append({"event_id": event["event_id"], "label": label, "kind": kind,
                                  "record_id": evidence["record_id"], "excerpt": excerpt,
                                  "excerpt_truncated": truncated})
    return sections


def _source_text(result: dict) -> str:
    if "has_more" not in result:        # whole records
        return clean("\n".join(record["text"] for record in result["records"]))
    lines = [record["chunk"] for record in result["records"]]
    if result.get("next_cursor"):
        lines.append("Continue with --since-cursor " + result["next_cursor"])
    return clean("\n".join(lines))


def source_renders(result: dict) -> bool:
    """Whether the human source rendering shows all of ``result``, uncut."""
    return len((_source_text(result) + "\n").encode("utf-8")) <= MAX_RENDER


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
        return _render_slice(clean("\n".join(lines)))
    if operation == "source":
        return _render_slice(_source_text(result))
    lines = []
    for event in result.get("events", []):
        ref = event["evidence"][0]
        excerpt, truncated = _excerpt(*_evidence_text(ref))
        lines.append(f"{event['kind']} [{event['event_id']} / {ref['record_id']}]: {excerpt}{' [truncated]' if truncated else ''}")
    if operation == "search":
        for match in result.get("event_matches", []):
            lines.append(f"{match['kind']} [{match['event_id']}] ({match['evidence_class']})")
        for match in result.get("matches", []):
            lines.append(f"Record ({match['role']}/{match['channel']}) [{match['record_id']}:{match['start']}]: {clean(match['quote'])}")
    if not lines:
        lines.append("No matching events in indexed view.")
    return _render_slice(clean("\n".join(lines)))
