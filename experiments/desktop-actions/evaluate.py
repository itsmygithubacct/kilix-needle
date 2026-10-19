"""Score raw model calls and admitted plans separately; no desktop connection."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import desktop_cli
import desktop_job
import desktop_codec
from engine import reply_calls
from libengine import LibEngineError


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--model", type=Path)
    p.add_argument("--codec", choices=[desktop_codec.DIRECT, desktop_codec.JSON_PLAN])
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        raise SystemExit("evaluation output directory must be empty")
    args.out.mkdir(parents=True, exist_ok=True)
    data = [json.loads(line) for line in args.data.read_text().splitlines() if line.strip()]
    bundle = None if args.model is None else json.loads((args.model / "model.json").read_text())
    codec = args.codec or (bundle.get("codec", desktop_codec.DIRECT) if bundle else desktop_codec.DEFAULT)
    totals, families, latencies = Counter(), defaultdict(Counter), []
    meta = dict(data_sha256=hashlib.sha256(args.data.read_bytes()).hexdigest(),
                model="stock" if bundle is None else bundle, codec=codec,
                tools_sha256=desktop_cli.schema_sha(codec), independent_qualification=False,
                definition="raw exact is the full normalized call list before guards; admitted wrong counts any admitted nonempty list differing from the entire reference")
    (args.out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    with desktop_cli.open_runtime(args.model, codec=codec) as engine, (args.out / "predictions.jsonl").open("w") as stream:
        for index, row in enumerate(data):
            expected = desktop_job.normalize(row.get("canonical_answers", row["answers"]))
            start = time.monotonic()
            restart = False
            try:
                engine.reset()
                reply = engine.complete(row["query"])
            except LibEngineError as error:
                # A broken worker is an incorrect runtime result, including on
                # negative examples. Never turn it into a credited empty call.
                reply = {"type": "error", "error": str(error)}
                restart = True
            elapsed = (time.monotonic() - start) * 1000
            if restart:
                # Keep the verified images, replace only their child process.
                engine.engine.close()
                engine.engine.start()
            latencies.append(elapsed)
            calls, failure = reply_calls(reply)
            if not failure:
                try:
                    calls = engine.translate(calls)
                except desktop_job.DesktopError as error:
                    failure = str(error)
            normalized, admitted, refused = None, [], None
            if not failure:
                try:
                    normalized = desktop_job.normalize(calls)
                except desktop_job.DesktopError as e:
                    failure = str(e)
                try:
                    admitted = desktop_job.validate(row["query"], calls)
                except desktop_job.DesktopError as e:
                    refused = str(e)
            exact = failure is None and normalized == expected
            wrong = bool(admitted) and admitted != expected
            family = row.get("family", "unlabelled").split(":")[0]
            metrics = dict(total=1, raw_exact=int(exact), positive=int(bool(expected)),
                           positive_raw_exact=int(bool(expected) and exact), negative=int(not expected),
                           negative_raw_exact=int(not expected and exact), admitted_correct=int(bool(expected) and admitted == expected),
                           admitted_wrong=int(wrong), invalid_or_runtime_error=int(failure is not None))
            totals.update(metrics); families[family].update(metrics)
            stream.write(json.dumps(dict(index=index, query=row["query"], family=family, expected=expected, reply=reply,
                                         normalized=normalized, admitted=admitted, raw_exact=exact, admitted_wrong=wrong,
                                         failure=failure, refusal=refused, elapsed_ms=round(elapsed, 2)), ensure_ascii=False) + "\n")
            stream.flush()
            if (index + 1) % 25 == 0:
                print(f"{index+1}/{len(data)} raw_exact={totals['raw_exact']} admitted_wrong={totals['admitted_wrong']}", flush=True)
    report = dict(metadata=meta, totals=dict(totals), families={k: dict(v) for k, v in families.items()},
                  latency_ms=dict(median=statistics.median(latencies), p95=sorted(latencies)[min(len(latencies)-1, int(len(latencies)*.95))], total=sum(latencies)))
    (args.out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"totals": report["totals"], "latency_ms": report["latency_ms"]}, indent=2))


if __name__ == "__main__":
    main()
