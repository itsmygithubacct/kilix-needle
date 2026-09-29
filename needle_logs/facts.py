"""Conservative facts from canonical records; labels describe recorded evidence."""
from __future__ import annotations

import hashlib
import json
import re

VERSION = "baseline-1"
_TEST = re.compile(r"(?im)^(?:=+[ \t]*)?(?:(\d+) passed|FAILED[ \t]+\S+|(?:Tests?:?[ \t]*)?([0-9]+) failed|Ran \d+ tests? in [\d.]+s|OK(?: \(skipped=\d+\))?|FAILURES!.*)(?:[ \t].*)?$")
_EXIT = re.compile(r"(?im)^(?:exit (?:code|status)|process exited with code)\s*[:= ]\s*(-?\d+)\s*$")
_ERROR = re.compile(r"(?im)^(?:error:|fatal:|traceback \(most recent call last\):|exception:).*$")


def _event(record: dict, kind: str, start: int, end: int, evidence_class: str, extractor: str) -> dict:
    quote = record["text"][start:end]
    identity = [record["source_id"], record["generation"], record["record_id"], kind, start, end, extractor]
    event_id = "evt-" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:24]
    return {
        "schema": "kilix.logs.event/v1", "event_id": event_id, "kind": kind,
        "evidence_class": evidence_class,
        "evidence": [{"record_id": record["record_id"], "start": start, "end": end, "quote": quote}],
        "source_id": record["source_id"], "session_id": record["session_id"],
        "generation": record["generation"], "sequence": record["sequence"],
        "timestamp": record.get("timestamp"), "extractor": extractor,
    }


def extract(records: list[dict]) -> list[dict]:
    """Extract narrow, cited events. No prose success or resolution inference."""
    events = []
    for record in records:
        text = record.get("text", "")
        if not isinstance(text, str) or not text or record.get("role") == "system":
            continue
        role, channel = record.get("role"), record.get("channel")
        quality = record.get("quality")
        if role == "user" and channel == "message":
            kind = "question" if text.rstrip().endswith("?") else "request"
            events.append(_event(record, kind, 0, len(text), "user_statement" if quality == "structured" else "unattributed_text", VERSION))
        elif role == "tool" and channel == "tool_result" and quality == "structured":
            for match in _TEST.finditer(text):
                events.append(_event(record, "test_result", match.start(), match.end(), "structured_fact", VERSION + ":test-output"))
            if _TEST.search(text):
                for match in _EXIT.finditer(text):
                    # Only attach exit status to an explicit test output record.
                    events.append(_event(record, "test_result", match.start(), match.end(), "structured_fact", VERSION + ":exit-status"))
            for match in _ERROR.finditer(text):
                events.append(_event(record, "error", match.start(), match.end(), "structured_fact", VERSION + ":error-line"))
        elif role == "assistant" and channel == "message":
            events.append(_event(record, "answer", 0, len(text),
                                 "assistant_claim" if quality == "structured" else "unattributed_text", VERSION))
    return events
