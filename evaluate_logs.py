"""Evidence and semantic event scoring for logs extraction.

JSON input: {cases:[{records:[...], gold:[event...], result:{events,errors,complete}}]}.
Every case contributes gold to recall, including timed-out or rejected cases.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from needle_logs.extract import KINDS, _class


def _span(event):
    if not isinstance(event, dict):
        return None
    evidence = event.get("evidence")
    if not isinstance(evidence, list) or len(evidence) != 1:
        return None
    e = evidence[0]
    if not isinstance(e, dict):
        return None
    return e.get("record_id"), e.get("start"), e.get("end")


def _valid(event, records):
    span = _span(event)
    if span is None:
        return False
    rid, start, end = span
    if type(rid) is not str or type(start) is not int or type(end) is not int:
        return False
    r = records.get(rid)
    if r is None or not isinstance(r.get("text"), str) or not (0 <= start < end <= len(r["text"])):
        return False
    if event["evidence"][0].get("quote") != r["text"][start:end]:
        return False
    for key in ("source_id", "session_id", "generation", "sequence", "timestamp"):
        if event.get(key) != r.get(key):
            return False
    if event.get("evidence_class") != _class(r):
        return False
    return True


def _overlap(a, b):
    """Same-record intersection over union; broad quotations lose credit."""
    if a is None or b is None or a[0] != b[0]:
        return 0.0
    start = max(a[1], b[1])
    end = min(a[2], b[2])
    intersection = max(0, end - start)
    union = max(a[2], b[2]) - min(a[1], b[1])
    return intersection / union if union else 0.0


def evaluate(cases: list[dict]) -> dict:
    if not isinstance(cases, list):
        raise ValueError("evaluation cases must be a list")
    by_kind = {kind: Counter() for kind in sorted(KINDS)}
    totals = Counter()
    failures = Counter()
    critical = []
    for ci, case in enumerate(cases):
        if not isinstance(case, dict) or not isinstance(case.get("records"), list):
            raise ValueError(f"invalid evaluation fixture in case {ci}")
        rows = case["records"]
        if any(not isinstance(r, dict) or type(r.get("record_id")) is not str
               or not isinstance(r.get("text"), str) for r in rows):
            raise ValueError(f"invalid canonical record in case {ci}")
        records = {r["record_id"]: r for r in rows}
        if len(records) != len(rows):
            raise ValueError(f"duplicate canonical record in case {ci}")
        gold = case.get("gold")
        if not isinstance(gold, list):
            raise ValueError(f"gold must be a list in case {ci}")
        for gi, event in enumerate(gold):
            if (not isinstance(event, dict) or type(event.get("kind")) is not str
                    or event["kind"] not in by_kind or not _valid(event, records)):
                raise ValueError(f"invalid gold event {gi} in case {ci}; scoring refused")
        result = case.get("result") or {}
        if not isinstance(result, dict):
            result = {}
            failures["invalid_result"] += 1
        predicted = result.get("events", [])
        if not isinstance(predicted, list):
            predicted = []
            failures["invalid_result"] += 1
        if not result.get("complete", False):
            failures["incomplete_cases"] += 1
        if result.get("errors"):
            failures["errored_cases"] += 1
        valid = [_valid(p, records) for p in predicted]
        totals["predictions"] += len(predicted)
        totals["valid_evidence"] += sum(valid)
        gold_used = set()
        for pi, pred in enumerate(predicted):
            kind = pred.get("kind") if isinstance(pred, dict) else None
            if not isinstance(kind, str) or kind not in by_kind:
                failures["unknown_kind"] += 1
                failures["false_positives"] += 1
                continue
            by_kind[kind]["predicted"] += 1
            if valid[pi]:
                match = next((gi for gi, g in enumerate(gold) if gi not in gold_used
                              and g.get("kind") == kind and _overlap(_span(g), _span(pred)) >= 0.5), None)
                if match is not None:
                    gold_used.add(match)
                    by_kind[kind]["tp"] += 1
                    if _span(gold[match]) == _span(pred):
                        totals["exact_spans"] += 1
                    continue
            span = _span(pred)
            cited = records.get(span[0]) if span is not None and type(span[0]) is str else None
            cross_source = cited is None or any(pred.get(key) != cited.get(key)
                                                  for key in ("source_id", "session_id", "generation"))
            failures["false_positives"] += 1
            if kind in ("decision", "completion", "test_result", "blocker") or cross_source:
                critical.append({"case": ci, "prediction": pi, "kind": kind,
                                 "reason": "cross_source_attribution" if cross_source else "unmatched_or_invalid_evidence"})
        for g in gold:
            by_kind[g["kind"]]["gold"] += 1
    scores = {}
    for kind, c in by_kind.items():
        p = c["tp"] / c["predicted"] if c["predicted"] else 0.0
        r = c["tp"] / c["gold"] if c["gold"] else 0.0
        scores[kind] = {"tp": c["tp"], "predicted": c["predicted"], "gold": c["gold"],
                        "precision": p, "recall": r, "f1": 2*p*r/(p+r) if p+r else 0.0}
    supported = [s for s in scores.values() if s["gold"]]
    tp = sum(s["tp"] for s in scores.values())
    pred = totals["predictions"]
    gold_count = sum(s["gold"] for s in scores.values())
    p = tp/pred if pred else 0.0
    r = tp/gold_count if gold_count else 0.0
    return {"schema": "kilix.logs.eval/v1", "cases": len(cases),
            "evidence": {"valid": totals["valid_evidence"], "total": totals["predictions"],
                         "validity": totals["valid_evidence"]/totals["predictions"] if totals["predictions"] else 1.0},
            "micro": {"tp": tp, "predicted": pred, "gold": gold_count,
                      "precision": p, "recall": r, "f1": 2*p*r/(p+r) if p+r else 0.0,
                      "precision_defined": pred > 0, "recall_defined": gold_count > 0},
            "macro_f1": sum(s["f1"] for s in supported)/len(supported) if supported else 0.0,
            "exact_span_matches": totals["exact_spans"],
            "per_kind": scores, "failures": dict(failures), "critical_misattributions": critical}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", help="JSON file with a cases array")
    args = parser.parse_args()
    with open(args.cases, encoding="utf-8") as source:
        data = json.load(source)
    print(json.dumps(evaluate(data["cases"]), indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
