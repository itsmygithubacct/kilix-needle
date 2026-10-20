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
  2026-10-19 by agent sessions that never saw a router held-out set:
  - system, tmux, logs, pty, desktop, agents, files and none rows;
  - 160 cross-job confusables;
  - round 2: 200 apps-controls rows (audio, music, voice, text size and their
    status), 60 short apps launches, 80 agents message and wait rows, and 80
    `none` questions about tools (`apps-controls`, `apps-launch`,
    `agents-message-wait`, `none-about-tools`);
  - `window-words`: 40 panes rows that call a pane a window, by where it is
    ("close the window below", "split this window"), and 12 desktop rows that
    name an application's window with the same words. Written on 2026-10-19 by
    the router's author (who had seen the spent round-1 sets, not round 2's),
    because "dismiss the window below", a spent panes request, routed to the
    desktop job. Review round 2 found two rows asking for what no panes tool
    does; an audit of all 48 against the panes tools relabelled 8 to `none`
    (clear, scroll, move a pane to a new tab, close the others, even out the
    sizes, undo a split).
- **Relabels** (`evals/router/relabels.jsonl`): spent job-eval rows tagged
  `none` that a current job answers. A job eval's `none` tag means "not this
  job", which is not always "no job":
  - 2 the system job answers (memory and disk questions);
  - 3 the apps job's text-size controls answer ("increase the font size",
    "make the font bigger").

  Two agent-written rows were corrected in place for the same reason: "mute the
  speakers" (`none.jsonl`) and "increase the system volume" (`confusables.jsonl`)
  are apps controls.
- **Spent router held-out sets** (rule `spent`): round 1's fresh held-out text
  set (337 single-job rows; its ambiguous rows are left out) and its 63 voice
  transcripts (Piper speech recognised by the dictation engine). They were
  scored once, on 2026-10-19, and the router failed that gate; they are now
  training rows. In cross-validation each stays whole, so it measures transfer to
  a writer the other rows never saw.
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
- it uses its own L-BFGS, run to a gradient max-norm of 1e-6, or until no
  decrease of the loss is measurable in double precision;
- it rounds weights to 4 decimals.

BLAS rounding can differ between CPUs, so `--check` (and
`tests/test_router.py`) compare every weight within 1e-4 and the top label on
`evals/router/probe.jsonl`, rather than the digest.

## Threshold

**The rule** is the lowest probability in 0.50–0.95 (in steps of 0.01) at
which the accepted cross-validation rows are at least 98.5% correct.

The rule was frozen on 2026-10-19, before any router held-out row was read, with
a 99.5% target. That router failed its first held-out gate: about 20% of fresh
requests fell below the threshold and were asked to clarify. Round 2 moved the
target to 98.5%, on cross-validation only, before any round-2 held-out row was
read. On the spent round-1 rows, held out whole, a threshold of 0.55 left 8.1%
unsure for 2.7% wrong, where 0.70 left 20.0% unsure for 1.4% wrong. After
review round 2 relabelled 8 rows and the trainer's stop rule changed, the same
rule selected 0.57.

**The folds** are grouped 5-fold:
- each job eval file and each spent held-out set stays whole, so it measures
  transfer to new phrasing;
- each router file is split into five chunks by a text hash.

**The result** is in `selection.json`:

| Measure | Value |
|---|---:|
| Cross-validation accuracy | 96.5% (macro recall 96.4%) |
| Selected threshold | 0.57 |
| Rows above the threshold | 93.7% |
| Accuracy above the threshold | 98.6% |
