"""Validate model calls using the pinned kilix-needle contract, without a desktop.

Input JSONL rows use {"request": ..., "calls": [{"name": ..., "arguments": ...}]}.
A single bridge process validates the batch; importing this module loads no model.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

PACK = Path(__file__).resolve().parent


def validate_many(rows: list[dict], *, needle_root: Path | None = None) -> list[dict]:
    contract = json.loads((PACK / "contract.json").read_text(encoding="utf-8"))
    needle_root = needle_root or Path(os.environ.get("KILIX_NEEDLE_HOME") or
                                     PACK.parents[3] / "kilix-apps" / "kilix-needle")
    envelopes = [{"contract_sha256": contract["sha256"], "request": row["request"],
                  "function_calls": row["calls"]} for row in rows]
    done = subprocess.run([sys.executable, "-B", str(needle_root / "domain_bridge.py"), "bridge"],
                          input="".join(json.dumps(e, ensure_ascii=False) + "\n" for e in envelopes),
                          text=True, capture_output=True, check=False)
    results = [json.loads(line) for line in done.stdout.splitlines()]
    if done.returncode not in (0, 1) or len(results) != len(rows):
        raise RuntimeError(f"kilix-needle validation failed: {done.stderr.strip()}")
    if any("error" in row for row in results):
        raise ValueError(next(row["error"] for row in results if "error" in row))
    return results


def main() -> int:
    rows = [json.loads(line) for line in sys.stdin if line.strip()]
    results = validate_many(rows)
    for result in results:
        print(json.dumps(result, ensure_ascii=False))
    return int(any(result["status"] for result in results))


if __name__ == "__main__":
    sys.exit(main())
