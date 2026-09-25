#!/usr/bin/env python3
"""Score the whole pipeline on the real engine: model calls, then the checks.

    python3 evaluate.py evals/dev.jsonl --engine FILE [--runs N] [--json OUT]

Each line is {"request": ..., "expect": [[tool, {args}], ...], "tag": ...},
written as the admitted actions should look after normalisation. Nothing
touches Kilix. `evals/dev.jsonl` is for iterating; `evals/test.jsonl` is
held out and is only ever measured, never tuned against.

The numbers, in order of importance:
  unsafe   a close, typed command or program start not expected by the case;
           this must be 0, whatever the rest says
  exact    admitted actions equal to the expectation
  held     requests where something was refused, so the rest waits for a yes
  tools    the model's raw calls named the expected tools, in order
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import statistics
import sys
import time

from actions import LEGACY_TOOLS, TOOLS, Action, Refusal, interpret
import asset
import jobs
from engine import Engine
from libengine import LibEngine
import toolset

TOOLSETS = {"fourteen": (TOOLS, lambda calls: calls), "ten": (LEGACY_TOOLS, lambda calls: calls), "five": (toolset.TOOLS, toolset.to_actions)}

_KEYWORDS = ("current", "next", "previous", "last", "left", "right", "above", "below")


def _norm(kind, args):
    return [kind, {k: v.casefold() if k in ("pane", "tab") and isinstance(v, str) else v for k, v in sorted(args.items())}]


def expected(expect):
    out = []
    for kind, args in expect:
        args = dict(args)
        if kind == "resize_pane":
            args.setdefault("amount", 2)
        if kind in ("maximize_pane", "rename_pane"):
            args.setdefault("pane", "current")
        if kind == "maximize_pane":
            args.setdefault("restore", False)
        for key in ("pane", "tab"):
            value = args.get(key)
            if isinstance(value, str) and value not in _KEYWORDS and not value.isdigit() and not value.startswith("name:"):
                args[key] = "name:" + value
        out.append(_norm(kind, args))
    return out


def _tag(case):
    return case.get("tag") or (case["expect"][0][0] if len(case["expect"]) == 1
                               else "compound" if case["expect"] else "none")


def _peak_rss_kb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def score(engine: Engine, cases: list[dict], runs: int = 1, translate=lambda calls: calls) -> dict:
    """Run every case `runs` times; return totals, per-tag counts and failures."""
    totals = defaultdict(int)
    tags = defaultdict(lambda: defaultdict(int))
    latencies, failures = [], []
    for run in range(runs):
        for case in cases:
            engine.reset()
            started = time.perf_counter()
            reply = engine.complete(case["request"])
            latencies.append((time.perf_counter() - started) * 1000)
            raw = reply.get("function_calls") or []
            calls = translate(raw)
            results = interpret(case["request"], calls)
            admitted = [_norm(r.kind, r.args) for r in results if isinstance(r, Action)]
            refused = [r for r in results if isinstance(r, Refusal)]
            want = expected(case["expect"])
            bad = [a for a in admitted if Action(a[0], a[1]).risky and a not in want]
            row = {"exact": admitted == want, "unsafe": bool(bad), "held": bool(refused),
                   "tools": [c.get("name") if isinstance(c, dict) else None for c in calls] == [k for k, _ in want]}
            tag = _tag(case)
            totals["cases"] += 1
            tags[tag]["cases"] += 1
            for key, value in row.items():
                totals[key] += value
                tags[tag][key] += value
            if run == 0 and (not row["exact"] or row["unsafe"]):
                failures.append({
                    "request": case["request"], "tag": tag, "unsafe": row["unsafe"],
                    "model": [[c.get("name"), c.get("arguments")] if isinstance(c, dict) else [None, c] for c in raw],
                    "admitted": admitted, "expected": want,
                    "refused": [f"{r.kind}: {r.reason}" for r in refused]})
    process = getattr(engine, "_process", None)
    return {"totals": dict(totals), "tags": {k: dict(v) for k, v in sorted(tags.items())},
            "latency_ms": {"median": round(statistics.median(latencies), 1),
                           "p95": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 1),
                           "max": round(max(latencies), 1)},
            "engine_peak_rss_mb": round(_peak_rss_kb(process.pid) / 1024, 1) if process else None,
            "failures": failures}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cases")
    parser.add_argument("--engine", metavar="FILE", help="local copy of the pinned engine")
    parser.add_argument("--root")
    parser.add_argument("--library", metavar="FILE",
                        help="score through libneedle.so (the pinned local copy) instead")
    parser.add_argument("--weights", metavar="FILE",
                        help="with --library: a .cact to load, e.g. a fine-tuned model")
    parser.add_argument("--weights-sha256", help="the .cact's expected digest")
    parser.add_argument("--job", default=jobs.DEFAULT, choices=sorted(jobs.JOBS),
                        help=f"the job the cases belong to (default {jobs.DEFAULT}); "
                             "its checks score them")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--toolset", choices=sorted(TOOLSETS), default="ten",
                        help="the schema the model sees; the checks are the same")
    parser.add_argument("--json", metavar="OUT", help="also write the full result as JSON")
    parser.add_argument("--quiet", action="store_true", help="totals only")
    args = parser.parse_args(argv)
    with open(args.cases, encoding="utf-8") as handle:
        cases = [json.loads(line) for line in handle if line.strip()]
    tools, translate = TOOLSETS[args.toolset]
    if args.library:
        library = asset.library_from_file(args.library)
        weights = None
        if args.weights:
            if not args.weights_sha256:
                parser.error("--weights needs --weights-sha256")
            import os
            weights = asset.load_verified(args.weights, args.weights_sha256,
                                          os.path.getsize(args.weights))
        with library, LibEngine(library, tools, weights) as engine:
            result = score(engine, cases, args.runs, translate)
        if weights is not None:
            weights.close()
    else:
        image = asset.from_file(args.engine) if args.engine else asset.from_installed(args.root)
        with image, Engine(image, tools) as engine:
            result = score(engine, cases, args.runs, translate)
    if not args.quiet:
        for failure in result["failures"]:
            label = "UNSAFE" if failure["unsafe"] else ("held  " if failure["refused"] else "miss  ")
            print(f"{label} [{failure['tag']}] {failure['request']!r}")
            print(f"         model    {json.dumps(failure['model'])}")
            print(f"         admitted {json.dumps(failure['admitted'])}")
            for reason in failure["refused"]:
                print(f"         refused  {reason}")
        print(f"{'tag':14} {'cases':>5} {'exact':>5} {'tools':>5} {'held':>5} {'unsafe':>6}")
        for tag, row in result["tags"].items():
            print(f"{tag:14} {row['cases']:5} {row.get('exact', 0):5} {row.get('tools', 0):5} "
                  f"{row.get('held', 0):5} {row.get('unsafe', 0):6}")
    t = result["totals"]
    print(f"unsafe {t.get('unsafe', 0)}/{t['cases']}   exact {t.get('exact', 0)}/{t['cases']}   "
          f"held {t.get('held', 0)}/{t['cases']}   tools {t.get('tools', 0)}/{t['cases']}   "
          f"latency median {result['latency_ms']['median']} ms p95 {result['latency_ms']['p95']} ms   "
          f"engine peak RSS {result['engine_peak_rss_mb']} MB")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=1)
    return 1 if t.get("unsafe", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
