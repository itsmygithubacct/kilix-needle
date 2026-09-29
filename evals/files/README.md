# Files job evaluation sets

- `dev.jsonl`: authored implementation fixtures, inspected during development.
- `test.jsonl`: 60 cases written from `SPEC.md`, inspected; a development set.
- `heldout-v1.jsonl`: 150 cases written blind from `SPEC.md` alone, audited against
  `dev.jsonl` and `test.jsonl` for overlap, and gated once on 2026-09-29. The
  grammar baseline scored 41/150 exact with 1 unsafe query: "markdown files" searched
  `*.markdown`. Type words now map to extensions and categories are refused. v1 is
  consulted; the next gate needs a fresh blind set (`heldout-v2.jsonl`).

`evaluate.py --job files --baseline` scores the grammar. No collector runs.
