"""Route system requests through grammar collection or planning-only inference."""
from __future__ import annotations

import system_collect
import system_job
import system_normalize


def dispatch(request, classify, options, *, baseline=False, collector=None,
             model_label=None):
    """Return the established system record, with an explicit collection marker.

    ``classify`` is lazy: it is called only for an unfamiliar, safe request.
    Model proposals are never passed to a collector, regardless of options.
    """
    if baseline:
        plan = None
    else:
        plan = system_normalize.plan(request, classify)

    if baseline or plan["path"] == "grammar":
        record = system_collect.run_request(system_job.Baseline(), request, options,
                                            collector=collector)
        record["query_source"] = "system grammar baseline"
        record["collection_performed"] = any(
            item.get("outcome") in ("done", "failed") for item in record["items"])
        return record

    source = model_label() if callable(model_label) else model_label
    record = {"request": request, "status": 0,
              "query_source": source or ("tuned normalizer unavailable" if plan["runtime_error"]
                                           else "request checks"),
              "decision": plan["decision"],
              "runtime_error": plan["runtime_error"],
              "protocol_error": plan["protocol_error"],
              "validation_error": plan["validation_error"],
              "collection_performed": False, "note": "No observations collected.",
              "items": []}
    if plan["path"] == "model" and plan["trust"] == "model_proposal":
        for kind, args in plan["actions"]:
            record["items"].append({"kind": kind, "args": args, "outcome": "proposed",
                                    "trust": "model_proposal"})
        record["note"] = "Unverified model proposal; use a supported grammar request to collect."
    else:
        record["status"] = 1
        record["note"] = "; ".join(plan["reasons"]) or "No supported read-only query."
        record["items"] = [{"kind": "system", "outcome": "refused",
                            "reason": record["note"]}]
    return record
