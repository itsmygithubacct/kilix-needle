"""The system job's whole route, scored: grammar reads plus the normalizer's proposals.

Grammar reads run; model proposals are shown but never collected. Both count
toward `exact`. `unsafe` is a read that would run and was not asked for;
`misled` is a proposal that differs from the request. No collector runs here.
"""
from collections import Counter, defaultdict
import math
import statistics
import time

import system_job
import system_normalize


def score(runtime, cases):
    if not cases:
        raise ValueError("system evaluation needs cases")
    totals = Counter(); tags = defaultdict(Counter); failures = []; latencies = []

    def classify(text):
        runtime.reset()
        return runtime.complete(text)

    for case in cases:
        start = time.perf_counter()
        plan = system_normalize.plan(case["request"], classify)
        want = [[k, system_job.normalize(k, a)] for k, a in case["expect"]]
        got = plan["actions"] if plan["decision"] == "read" else []
        ran = got if plan["path"] == "grammar" else []
        proposed = got if plan["path"] == "model" else []
        row = {"cases": 1, "exact": int(got == want), "grammar_exact": int(bool(ran) and ran == want),
               "proposal_exact": int(bool(proposed) and proposed == want),
               "unsafe": int(any(a not in want for a in ran)),
               "misled": int(bool(proposed) and proposed != want),
               "held": int(not got), "errors": int(plan["runtime_error"] or plan["protocol_error"]),
               "actionable": int(bool(want))}
        tag = case.get("tag", "untagged"); totals.update(row); tags[tag].update(row)
        latencies.append((time.perf_counter() - start) * 1000)
        if not row["exact"]:
            failures.append({"request": case["request"], "tag": tag, "path": plan["path"],
                             "actions": got, "expected": want, "reasons": plan["reasons"],
                             "unsafe": row["unsafe"], "misled": row["misled"]})
    return {"complete": totals["cases"] == len(cases), "totals": dict(totals),
            "tags": {k: dict(v) for k, v in tags.items()}, "failures": failures,
            "latency_ms": {"median": statistics.median(latencies),
                           "p95": sorted(latencies)[math.ceil(.95 * len(latencies)) - 1],
                           "max": max(latencies)},
            "scope": "system route: grammar reads and unverified model proposals; no collector reads"}


def score_bundle(bundle, cases):
    """Grammar plus bundle reads, no model: what would be collected, scored.

    ``bundle_differs`` counts bundle reads that are not exactly the label (the
    bundle gate's safety bar); ``unsafe`` counts collected reads (grammar or
    bundle) the label does not ask for. ``grammar_exact`` is the same route
    without the bundle, for the gain."""
    if not cases:
        raise ValueError("system evaluation needs cases")
    totals = Counter(); tags = defaultdict(Counter); failures = []
    for case in cases:
        want = [[k, system_job.normalize(k, a)] for k, a in case["expect"]]
        plan = system_normalize.plan(case["request"], None, bundle=bundle)
        got = plan["actions"] if plan["decision"] == "read" else []
        alone = system_normalize.plan(case["request"], None)
        alone_got = alone["actions"] if alone["decision"] == "read" else []
        read = plan["path"] == "bundle"
        row = {"cases": 1, "exact": int(got == want), "grammar_exact": int(alone_got == want),
               "bundle_reads": int(read), "bundle_exact": int(read and got == want),
               "bundle_differs": int(read and got != want),
               "unsafe": int(any(a not in want for a in got)), "held": int(not got)}
        tag = case.get("tag", "untagged"); totals.update(row); tags[tag].update(row)
        if row["bundle_differs"] or row["unsafe"]:
            failures.append({"request": case["request"], "tag": tag, "path": plan["path"],
                             "actions": got, "expected": want})
    return {"complete": totals["cases"] == len(cases), "totals": dict(totals),
            "tags": {k: dict(v) for k, v in tags.items()}, "failures": failures,
            "scope": "system route without a model: grammar reads and admitted bundle reads"}
