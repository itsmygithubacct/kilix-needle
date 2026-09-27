"""Bounded, source-offset-preserving model views of canonical records."""
from __future__ import annotations

import hashlib
import json

MAX_CHARS = 1600
MAX_CHUNKS = 128


def _parts(text: str, limit: int):
    """Partition at paragraph/line boundaries, then hard split long lines."""
    start = 0
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            cut = text.rfind("\n", start + 1, end + 1)
            if cut > start:
                end = cut + 1
        yield start, end
        start = end
    if not text:
        yield 0, 0


def _cursor(records, index, offset, limit):
    if index >= len(records):
        return None
    first = records[0]
    binding = [first.get("session_id"), first.get("source_id"), first.get("generation"), limit]
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()[:16]
    return {"schema": "kilix.logs.cursor/v1", "binding": digest,
            "record_id": records[index]["record_id"], "offset": offset}


def chunk_records(records: list[dict], *, max_chars: int = MAX_CHARS,
                  max_chunks: int = MAX_CHUNKS, cursor: dict | None = None) -> dict:
    """Return owning chunks plus a bound continuation. Each character has one owner.

    Character limits are a conservative transport bound, not a tokenizer claim.
    Excluded source records are never handed to the model.
    """
    if (type(max_chars) is not int or type(max_chunks) is not int
            or not 1 <= max_chars <= 8192 or not 1 <= max_chunks <= 1024):
        raise ValueError("invalid chunk limits")
    ids = [r["record_id"] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate record IDs")
    if records and len({(r["source_id"], r["session_id"], r["generation"]) for r in records}) != 1:
        raise ValueError("mixed source generations")
    start_index, start_offset = 0, 0
    if cursor is not None:
        if cursor.get("schema") != "kilix.logs.cursor/v1" or cursor.get("record_id") not in ids:
            raise ValueError("cursor_invalid")
        start_index = ids.index(cursor["record_id"])
        start_offset = cursor.get("offset")
        expected = _cursor(records, start_index, start_offset, max_chars)
        if type(start_offset) is not int or start_offset < 0 or start_offset > len(records[start_index]["text"]) or cursor != expected:
            raise ValueError("cursor_invalid")
    chunks, excluded = [], []
    next_cursor = None
    for i in range(start_index, len(records)):
        r = records[i]
        if r.get("role") in ("system", "developer") or r.get("channel") not in ("message", "tool_request", "tool_result", "lifecycle"):
            excluded.append({"record_id": r["record_id"], "reason": "excluded_role_or_channel"})
            continue
        text = r["text"]
        for a, b in _parts(text, max_chars):
            if i == start_index and b <= start_offset:
                continue
            a = max(a, start_offset) if i == start_index else a
            if len(chunks) == max_chunks:
                next_cursor = _cursor(records, i, a, max_chars)
                break
            part = {"record_id": r["record_id"], "start": a, "end": b,
                    "text": text[a:b], "role": r.get("role"), "channel": r.get("channel"),
                    "quality": r.get("quality"), "sequence": r.get("sequence")}
            chunks.append({"chunk_id": f"chunk-{i}-{a}", "extractable": [part],
                       "context": [], "source_id": records[0]["source_id"] if records else None,
                       "session_id": records[0]["session_id"] if records else None,
                       "generation": records[0]["generation"] if records else None})
        if next_cursor is not None:
            break
    return {"chunks": chunks, "next_cursor": next_cursor,
            "coverage": {"complete": next_cursor is None, "owned_fragments": len(chunks),
                         "remaining_fragments": 0 if next_cursor is None else None,
                         "excluded": excluded}}
