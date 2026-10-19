"""Freeze guard evidence: mutate constructed labels and replay saved predictions.

No inference, desktop connection, or training. Replay retains the original raw
predictions and records the validator hash used for the final admission score.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import desktop_job as job


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def mutants(calls):
    yield "extra-action", copy.deepcopy(calls + calls[:1])
    if len(calls) > 1:
        yield "missing-action", copy.deepcopy(calls[:-1])
        if calls[::-1] != calls: yield "reordered", copy.deepcopy(calls[::-1])
    for i, call in enumerate(calls):
        m = copy.deepcopy(calls); m[i]["arguments"]["command"] = "not an action argument"
        yield "unknown-field", m
        for key, value in call["arguments"].items():
            m = copy.deepcopy(calls)
            if type(value) is int:
                m[i]["arguments"][key] = value + 1
            elif key == "text":
                m[i]["arguments"][key] = value + "!"
            elif key == "target":
                m[i]["arguments"][key] = "title:UNREQUESTED WINDOW"
            elif key == "keys":
                m[i]["arguments"][key] = "F24" if value != "F24" else "F23"
            elif key == "direction":
                m[i]["arguments"][key] = "up" if value != "up" else "down"
            elif key == "button":
                m[i]["arguments"][key] = "right" if value != "right" else "left"
            elif key == "action":
                options = {"window": "close", "pointer": "drag", "keyboard": "press"}
                m[i]["arguments"][key] = options[call["name"]]
                if m == calls: continue
            else:
                continue
            yield "changed-" + key, m


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", type=Path, nargs="*")
    p.add_argument("--predictions", type=Path, nargs="*")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    report = dict(validator_sha256=sha(ROOT / "desktop_job.py"), schema=job.VERSION,
                  positive_labels=0, mutants_refused=0, mutant_failures=[], replays=[])
    for path in args.corpus or []:
        for line in path.read_text().splitlines():
            row = json.loads(line)
            row["answers"] = row.get("canonical_answers", row["answers"])
            if not row["answers"]: continue
            wanted = job.normalize(row["answers"])
            assert job.validate(row["query"], row["answers"]) == wanted
            report["positive_labels"] += 1
            for kind, wrong in mutants(row["answers"]):
                try:
                    admitted = job.validate(row["query"], wrong)
                except job.DesktopError:
                    report["mutants_refused"] += 1
                    continue
                if admitted != wanted:
                    report["mutant_failures"].append(dict(query=row["query"], mutation=kind, proposed=wrong, admitted=admitted))
    for path in args.predictions or []:
        counts = dict(rows=0, raw_exact=0, positive=0, positive_raw_exact=0, negative=0,
                      negative_raw_exact=0, admitted_correct=0, admitted_wrong=0, refused=0)
        wrong = []
        for line in path.read_text().splitlines():
            r = json.loads(line); expected = r["expected"]
            counts["rows"] += 1; counts["raw_exact"] += int(r["raw_exact"])
            tag = "positive" if expected else "negative"
            counts[tag] += 1; counts[tag+"_raw_exact"] += int(r["raw_exact"])
            try:
                if r["failure"]: raise job.DesktopError("invalid model reply")
                admitted = job.validate(r["query"], r["normalized"])
            except job.DesktopError:
                counts["refused"] += 1
                admitted = []
            if admitted and admitted == expected: counts["admitted_correct"] += 1
            elif admitted:
                counts["admitted_wrong"] += 1
                wrong.append(dict(query=r["query"], expected=expected, admitted=admitted))
        report["replays"].append(dict(path=str(path), predictions_sha256=sha(path), totals=counts, wrong=wrong))
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k:v for k,v in report.items() if k != "replays"}, indent=2))
    if report["mutant_failures"] or any(r["totals"]["admitted_wrong"] for r in report["replays"]):
        raise SystemExit(1)


if __name__ == "__main__": main()
