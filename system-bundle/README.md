# The system bundle

Small nets that read familiar read-only system requests the grammar does not
read (`system_bundle.py`), trained by `system_bundle_train.py` from
`training.jsonl`. **Off by default**: set `KILIX_NEEDLE_SYSTEM_BUNDLE=1` to use
it. When on, the system route is grammar, then the bundle, then the model's
proposal. A bundle read is collected only when the checks that bound every
proposal admit it (`system_normalize`: bounds, preflight, schema, grounding);
otherwise the request goes on to the model as before.

| File | What it is |
|---|---|
| `training.jsonl` | the rows: request, expected calls, tag, source |
| `bundle.json` | the nets, ASCII JSON with half-float weights as hex text |
| `manifest.json` | sha256 of `bundle.json` and of `training.jsonl`, seed, members |

Rebuild, which is deterministic (`tests/test_system_bundle.py` retrains it and
compares the digest):

```sh
python3 system_bundle_train.py
```

Score it without a model:

```sh
python3 evaluate.py CASES.jsonl --job system --bundle system-bundle
```

## Where the rows come from

All rows are spent: none is from the held-out gate set
(`evals/system/heldout-v2.jsonl`). A builder script checked every row against
that set and printed counts only. No exact match was found, and rows sharing
most of their 4-word runs with a gate request were left out (29 rows: 10 exact, 19 near).

| Source | What it is |
|---|---|
| `dev`, `test` | the system job's authored fixtures |
| `heldout-v1` | the first blind system set, gated once on 2026-09-29 and spent since |
| `natural`, `normalizer` | natural-language request sets written for the normalizer studies |
| `qat-eval`, `router-eval`, `qat-probes`, `qat-intents` | template-generated requests from the quantisation and router studies |
| `generated-a` … `generated-d` | natural requests written from `evals/system/SPEC.md` alone by four sessions (terse terminal users; polite users and agents; people describing a problem; admins asking for several reads at once) that saw no code and no other set. Each set was labelled again by a separate session that saw only the SPEC and the bare requests; only rows where both labels agree are kept. Writers and labellers are the same model family, so agreement shows consistency with the SPEC, not independent human judgement; a sample was checked by hand |

A row is trained on only where code reproduces its label exactly: the clause
split grounds every call, and the time, path, number and copied name that code
reads are the label's. Rows that do not align are counted and left out.
