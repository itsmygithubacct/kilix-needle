# The job router's model

`router.json` holds the weights of the router in `router.py`, a multinomial
logistic regression over word 1–2-grams and within-word character 3–5-grams,
weighted by log-TF × IDF. `manifest.json` is its provenance. It records:

- the labels and trainer settings;
- the threshold and the rule that chose it;
- every training source: its path, its rule, its sha256 and its row count;
- the relabels file;
- the row count for each label;
- the artifact's sha256.

`router.py` refuses an artifact whose digest does not match the manifest.

## Where the rows come from

All training text is ours. None of it comes from a third-party dataset.

- **Job eval sets** (rule `eval`), written by our agents for each job's own evals:
  - panes: `evals/dev.jsonl`, `evals/test.jsonl` and `evals/heldout-v2.jsonl` to `heldout-v14.jsonl`;
  - the apps, agents, files and system `dev`, `test` and spent held-out sets.

  An eval row with calls belongs to its job. A row tagged `none` belongs to no
  job. Bait and refusal rows are skipped.
- **Current gate sets are never read.** That covers every job's `heldout` in
  `jobs.py`. `router_train.py` refuses such a path before opening it, and they are
  listed under `excluded_gate_sets`.
- **Router files** (rule `labelled`, under `evals/router/`), written on
  2026-10-19 by an agent session that never saw the router's held-out set:
  - system, tmux, logs, pty, desktop, agents, files and none rows;
  - 160 cross-job confusables;
  - 2 relabels: spent panes `none` rows that the system job now answers.
- **`evals/router/desktop-v3.jsonl`.** 300 queries sampled deterministically
  (the first 300 by sha256 of the text) from the desktop-action job's own
  generated training queries (`data-v3/train.jsonl`, 2026-10-19).

## Retraining and checking

```sh
python3 router_train.py            # rewrite router.json and the manifest's digests
python3 router_train.py --check    # retrain in memory; compare with the checked-in files
python3 router_train.py --select   # grouped 5-fold cross-validation -> selection.json
```

Training needs NumPy; inference does not. The trainer is deterministic:
- it runs single-threaded;
- it uses a sorted vocabulary (features seen in at least 2 rows);
- it uses its own L-BFGS, run to a gradient max-norm of 1e-6;
- it rounds weights to 4 decimals.

BLAS rounding can differ between CPUs, so `--check` (and
`tests/test_router.py`) compare every weight within 1e-4 and the top label on
`evals/router/probe.jsonl`, rather than the digest.

## Threshold

**The rule** was frozen on 2026-10-19, before any router held-out row was read.
The threshold is the lowest probability in 0.50–0.95 (in steps of 0.01) at which
the accepted cross-validation rows are at least 99.5% correct.

**The folds** are grouped 5-fold:
- each job eval file stays whole, so it measures transfer to new phrasing;
- each router file is split into five chunks by a text hash.

**The result** is in `selection.json`:

| Measure | Value |
|---|---:|
| Cross-validation accuracy | 97.5% (macro recall 97.2%) |
| Selected threshold | 0.70 |
| Rows above the threshold | 89.5% |
| Accuracy above the threshold | 99.5% |
