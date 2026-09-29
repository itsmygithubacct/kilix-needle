# System job development cases

`dev.jsonl` and `test.jsonl` are authored implementation fixtures. Both have
been inspected during development. Neither is a blind evaluation, and no
model promotion may use them as one. Expected actions state the intended
query independently of an engine response; defaults normalize through the
system schema. Evaluation never runs the collectors.

These sets cover the five query families, compound requests, changed scopes,
missing targets, invalid filters, and requests for mutations. They do not
establish broad language coverage or diagnosis quality. A grammar baseline
is available through `evaluate.py --job system --baseline`.

Before training or selection, collect representative natural requests with
reviewed desired queries, include unknown/missing-target and permission cases,
and measure how many desired queries the checks can admit. Split by phrasing
family and target identity. Keep training, development and independently
authored final evaluation separate, with hash and semantic-overlap audits.

## Held-out gate

`heldout-v1.jsonl` (150 cases, sha256 4ee943f10dc4…) was written blind from
`SPEC.md` alone, audited against `dev.jsonl` and `test.jsonl` for overlap, and
gated once on 2026-09-29:

| Arm | Command | Exact | Unsafe |
|---|---|---|---|
| grammar baseline | `evaluate.py --job system --baseline` | 43/150 | 0 |
| grammar + normalizer proposals | `evaluate.py --job system --normalizer` | 55/150 | 0 |

The normalizer's 13 exact proposals came with 5 that differ from the request
(`misled`) and 18 runtime or protocol errors. Proposals are never collected, so
`unsafe` counts only grammar reads. v1 is now consulted. The gate is `heldout-v2.jsonl` (150 cases, sha256 c852c4e534e4…),
written blind the same way and audited against dev, test and v1. Nobody has
consulted it.
