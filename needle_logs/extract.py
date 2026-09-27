"""One bounded semantic tool; all evidence and metadata come from records."""
from __future__ import annotations

import hashlib
import json

KINDS = frozenset(("request", "decision", "change", "test_result", "error",
                   "blocker", "question", "completion", "answer"))
MAX_EVENTS = 4
TOOLS = [{"name": "extract_events", "description": "Label up to four events in numbered source-text candidates. Use only listed candidate IDs.",
          "parameters": {"type": "object", "properties": {"events": {"type": "array", "maxItems": MAX_EVENTS,
              "items": {"type": "object", "properties": {
                  "kind": {"type": "string", "enum": sorted(KINDS)},
                  "candidate": {"type": "string"}},
                  "required": ["kind", "candidate"]}}},
              "required": ["events"]}}]


def candidates(chunk: dict) -> list[dict]:
    """Stable chunk-local IDs with exact full-record source offsets."""
    import re
    found = []
    for part in chunk["extractable"]:
        text = part["text"]
        boundaries = [0] + [m.end() for m in re.finditer(r"(?<=[.!?])\s+|\n+", text)] + [len(text)]
        for a, b in zip(boundaries, boundaries[1:]):
            while a < b and text[a].isspace(): a += 1
            while b > a and text[b-1].isspace(): b -= 1
            if a < b:
                found.append({"candidate": f"c{len(found)+1}", "record": part["record_id"],
                              "start": part["start"]+a, "end": part["start"]+b,
                              "text": text[a:b]})
    return found


def prompt(chunk: dict) -> str:
    rows = [{"candidate": c["candidate"], "text": c["text"]} for c in candidates(chunk)]
    return ("Label stated events in recorded text. Use candidate IDs only. "
            "Call extract_events once.\n" + json.dumps(rows, ensure_ascii=False))


def _class(r):
    if r.get("role") == "user" and r.get("quality") == "structured":
        return "user_statement"
    if r.get("role") == "assistant":
        return "assistant_claim"
    if r.get("role") == "tool" and r.get("quality") == "structured":
        return "structured_fact"
    return "unattributed_text"


def validate(records: list[dict], proposals, *, chunk: dict | None = None) -> dict:
    """Reject malformed/foreign spans and construct immutable cited events."""
    errors, events = [], []
    by_id = {r["record_id"]: r for r in records}
    if len(by_id) != len(records):
        return {"events": [], "errors": [{"code": "duplicate_record_id"}]}
    allowed = None
    if chunk is not None:
        allowed = {c["candidate"]: c for c in candidates(chunk)}
    if not isinstance(proposals, list) or len(proposals) > MAX_EVENTS:
        return {"events": [], "errors": [{"code": "invalid_proposals"}]}
    seen = set()
    for n, p in enumerate(proposals):
        code = None
        if not isinstance(p, dict) or set(p) != {"kind", "candidate"}:
            code = "invalid_schema"
        elif not isinstance(p["kind"], str) or p["kind"] not in KINDS or not isinstance(p["candidate"], str):
            code = "invalid_value"
        elif allowed is None or p["candidate"] not in allowed:
            code = "foreign_candidate"
        else:
            target = allowed[p["candidate"]]
            r = by_id[target["record"]]
            start, end = target["start"], target["end"]
            if not (0 <= start < end <= len(r["text"])):
                code = "invalid_span"
            elif r.get("role") == "system":
                code = "excluded_record"
            elif chunk is not None and any(r.get(k) != chunk.get(k) for k in ("source_id", "session_id", "generation")):
                code = "foreign_source"
            elif (p["kind"], r["record_id"], start, end) in seen:
                code = "duplicate_span"
            else:
                seen.add((p["kind"], r["record_id"], start, end))
                identity = [r.get("source_id"), r.get("generation"), r["record_id"], start, end, p["kind"]]
                event_id = "evt-" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]
                events.append({"schema": "kilix.logs.event/v1", "event_id": event_id,
                               "kind": p["kind"], "evidence_class": _class(r),
                               "evidence": [{"record_id": r["record_id"], "start": start,
                                             "end": end, "quote": r["text"][start:end]}],
                               "source_id": r.get("source_id"), "session_id": r.get("session_id"),
                               "generation": r.get("generation"), "sequence": r.get("sequence"),
                               "timestamp": r.get("timestamp"), "extractor": "needle_logs/v1"})
        if code:
            errors.append({"code": code, "proposal_index": n})
    return {"events": events, "errors": errors}


def extract_chunk(records: list[dict], chunk: dict, model) -> dict:
    """Injected model has reset() and complete(text); errors retain coverage gaps."""
    try:
        model.reset()
        reply = model.complete(prompt(chunk))
    except Exception as error:
        return {"events": [], "errors": [{"code": "inference_failed", "message": str(error)}],
                "complete": False, "raw": None}
    calls = reply.get("function_calls") if isinstance(reply, dict) else None
    if (not isinstance(reply, dict) or reply.get("type") != "call" or reply.get("success") is False
            or not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict)
            or calls[0].get("name") != "extract_events"):

        return {"events": [], "errors": [{"code": "invalid_model_reply"}], "complete": False, "raw": reply}
    args = calls[0].get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = None
    if not isinstance(args, dict) or set(args) != {"events"}:
        return {"events": [], "errors": [{"code": "invalid_model_arguments"}], "complete": False, "raw": reply}
    result = validate(records, args["events"], chunk=chunk)
    saturated = isinstance(args["events"], list) and len(args["events"]) == MAX_EVENTS
    result.update({"complete": not result["errors"] and not saturated,
                   "saturated": saturated, "raw": reply})
    return result


def extract_all(records: list[dict], model, *, max_chars: int = 800,
                max_chunks: int = 128, max_depth: int = 2) -> dict:
    """Run bounded chunks, retry saturated output by splitting, retain gaps."""
    from .chunks import chunk_records
    view = chunk_records(records, max_chars=max_chars, max_chunks=max_chunks)
    events, errors, raw = [], [], []
    def visit(chunk, depth):
        prior = len(events)
        result = extract_chunk(records, chunk, model)
        raw.append(result.get("raw"))
        events.extend(result["events"])
        errors.extend(result["errors"])
        if result.get("saturated") and depth < max_depth:
            part = chunk["extractable"][0]
            middle = (part["start"] + part["end"]) // 2
            if middle > part["start"]:
                del events[prior:]
                children = []
                for a, b in ((part["start"], middle), (middle, part["end"])):
                    child = {**chunk, "extractable": [{**part, "start": a, "end": b,
                                                        "text": part["text"][a-part["start"]:b-part["start"]]}]}
                    children.append(child)
                for child in children: visit(child, depth + 1)
                return
        if not result["complete"]:
            errors.append({"code": "coverage_gap", "chunk_id": chunk["chunk_id"],
                           "span": [p["record_id"], p["start"], p["end"]] if (p := chunk["extractable"][0]) else None})
    for chunk in view["chunks"]: visit(chunk, 0)
    unique = {event["event_id"]: event for event in events}
    return {"events": list(unique.values()), "errors": errors,
            "complete": not errors and view["next_cursor"] is None,
            "next_cursor": view["next_cursor"], "raw": raw,
            "coverage": view["coverage"]}


def main():
    import argparse
    import sys
    import asset
    from libengine import LibEngine
    parser = argparse.ArgumentParser(description="Extract events from a JSON array of synthetic/canonical records")
    parser.add_argument("records", help="JSON file containing a records array")
    parser.add_argument("--library", required=True, help="local pinned libneedle.so")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    with open(args.records, encoding="utf-8") as source:
        data = json.load(source)
    with asset.library_from_file(args.library) as image, LibEngine(image, TOOLS, timeout=args.timeout) as model:
        result = extract_all(data["records"], model)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()

if __name__ == "__main__":
    main()
