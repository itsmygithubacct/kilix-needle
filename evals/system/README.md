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

Register a fresh final gate only after its data and decision rules are frozen.
`heldout-pending.jsonl` is a reserved name, not an existing gate. There is no
system tuning pack or qualified model yet, so automatic training and model
selection refuse before creating a run. Existing job selections are unchanged.
