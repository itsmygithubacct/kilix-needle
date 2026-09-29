# Files job evaluation sets

- `dev.jsonl`: authored implementation fixtures, inspected during development.
- `test.jsonl`: 60 cases written from `SPEC.md`, inspected; a development set.
- `heldout-v1.jsonl`: 150 cases written blind from `SPEC.md` alone, audited against
  `dev.jsonl` and `test.jsonl` for overlap, and gated once on 2026-09-29. The
  grammar baseline scored 41/150 exact with 1 unsafe query: "markdown files" searched
  `*.markdown`. Type words now map to extensions and categories are refused. v1 is
  consulted.
- `heldout-v2.jsonl` (sha256 e2faba47c60f…): the gate. Written blind the same way, audited
  against dev, test and v1. Nobody has consulted it.

`evaluate.py --job files --baseline` scores the grammar. No collector runs.
