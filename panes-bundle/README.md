# The panes bundle

`bundle.bin` holds the nets that `panes_bundle.py` reads: two members, the
same 42 small nets trained from seeds 0 and 1 (598,263 weights each), in one
file of 3.2 MB. `manifest.json` is its provenance. It records:

- the file's sha256 and size;
- the trainer's settings and its PyTorch version;
- where the rows came from: the generator's counts, each spent eval set with
  its sha256 and row count, and the eval files whose requests were kept out;
- for each member, the check that the file reads as the trained nets did;
- the sha256 of `panes_bundle.py` and `panes_bundle_train.py` as they were
  when the file was written.

`panes_bundle.py` refuses a file whose digest does not match the manifest.

## What a member is

One net for each state of the answer's grammar:

| Net | Asked | It says |
| --- | --- | --- |
| `seg` | for every word | whether it separates two clauses |
| `tool` | for every clause | which pane tool, or none |
| `arg TOOL KEY` | for a clause of that tool | absent, one value of the argument's closed list, or "copy" |
| `span TOOL KEY` | for every word of the clause | whether it is the first, and whether it is the last, word of the copied text |

A net adds up the 8-number rows of its table for the features it is shown,
divides by the square root of their count, and passes the sum through 32
rectified units to its outputs. The state picks the net. No score routes
anything, and what is systematic is code: splitting words, reading a number
word as a number, copying text character for character.

The file stores every feature name once, the known word pairs, each net's
feature numbers, and each member's weights as little-endian half floats. A
request decodes only the table rows it touches.

## When it answers

Only on ground it was trained on. It proposes nothing, and the model reads
the request as before, unless all of these hold:

1. Every pair of neighbouring words in each clause stood side by side in a
   training row. A word it never saw must be part of a copied text (a title,
   a command, a program). New word pairs inside or beside copied text are let
   through only for `rename_tab`, `rename_pane`, `go_to_pane` and
   `go_to_tab`.
2. A tool fits each clause, its required arguments are there, and its
   numbers are ones the tool takes.
3. The pane or tab that `close_pane`, `close_tab` or `run_in_pane` acts on is
   named with trained words or a number. A word it has never seen may be a
   title, but it may as well be a misspelt "right" or a "3rd".
4. Both members give the same calls.
5. Every word of the request that always named a value in training (187
   words: "lower", a program's name, a digit) is in the answer.
6. The checks (`actions.interpret`) admit every call. If they refuse one, the
   whole proposal is dropped.

None of these is a confidence threshold. They are lookups of words and word
pairs, agreement, and the checks.

## Where the rows come from

All training text is ours. None of it comes from a third-party dataset.

- **Generated rows** (5,669). The tuner's own rows for the tuned model
  (`tuning.build_data`): kilix-ml's `kilix_panes` pack with
  `corpus-supplement/`, seed 0, 12 for each template. 6,442 were generated
  and 773 dropped because the checks do not admit them as labelled. A request
  equal to a request of any file under `evals/` is never generated (147
  dropped).
- **Spent eval sets** (1,649 cases, `--spent`): `evals/dev.jsonl`,
  `evals/test.jsonl` and `evals/heldout-v2.jsonl` to `heldout-v14.jsonl`, as
  the router is trained on them. Of 2,126 cases, 270 were dropped because the
  checks do not admit their labels as written and 207 because they repeat a
  request.
- **A current gate set is never read.** `panes_bundle_train.py` refuses a
  job's current held-out set (`jobs.py`; `evals/heldout-v15.jsonl` today)
  before opening it.
- 15 of the 7,318 rows are not used: the boundary between two of their
  actions cannot be told from what the row records.

A bundle trained with `--spent` must not be scored on those sets. The
manifest lists them under `rows.spent`.

## Retraining and checking

```sh
uv run --with torch==2.6.0 panes_bundle_train.py --spent --out panes-bundle \
    --seeds 0 1 --name v1 --free-text rename_tab rename_pane go_to_pane go_to_tab
python3 panes_bundle.py "split right and run htop"     # what the bundle reads, and why not
python3 evaluate.py evals/dev.jsonl --route bundle     # the route as shipped, scored
```

Training needs PyTorch on a CPU (about twelve minutes for both members, one
thread). Reading needs only the standard library. The trainer rounds each
net to half floats, scores 200 examples of every net with PyTorch and again
with the reader, and refuses to write a file in which any example comes out
differently. Run again on the same machine it wrote this file byte for byte.

## How it was gated

By kilix-needle's rule: on blind held-out sets, each read once. One blind set
of 420 cases was written for this change from the tools' specification
alone, labelled a second time by another session, de-duplicated against
every training row and every eval request, and split into two halves before
anything was scored (v16, 179 cases, sha256 `8c001317…`; v17, 171 cases,
sha256 `f17e9908…`). Neither half is in `evals/` yet.

- **v16** failed the safety gate on one case. A request to close a tab held
  a word the bundle had never seen, the bundle copied it as the tab's title,
  and the checks admitted that reading. Rule 3 above is the fix. The route
  was exact on 116 of the 179 cases against 96 without the bundle.
- **Generated requests** showed a second kind of mistake: an argument left
  out although the request names it ("new lower pane with node" read as a
  pane with node and no side). Of 10,427 generated requests the bundle
  answered (4,378 of them its own training rows), 78 were answered wrongly.
  Rule 5 is the fix: it declines 44 of the 78 and none of the 10,349 right
  answers.
- **v17** gated the bundle with both rules, for two pairs of seeds:

  | | Exact, of 171 | Unsafe |
  | --- | ---: | ---: |
  | exact routes, then the tuned model (qat-9) | 106 | 0 |
  | exact routes, the bundle (seeds 0 and 1), then the model | 121 | 0 |
  | the same with seeds 2 and 3 | 119 | 0 |

  The bundle answered 58 of the 166 requests that reach the model today
  (35%). 56 of its answers were exact; on the other 2 the model was not
  exact either. No kind of request lost a case.
- **Time**, on an eight-core machine with nothing else measured on it, 40
  requests of each kind, three whole calls each way in a new process: a
  request the bundle answers took 227 ms (median) with it and 3,811 ms
  without; a request it declines took 3,855 ms with it and 3,894 ms without
  (32 ms more by the median of the paired differences). Inside a running
  process the file loads in 8.5 ms, an answer takes 1.6 ms and a decline
  0.45 ms.

## What it still gets wrong

- On v17, two answers, neither unsafe: after a request to switch to a tab
  and rename it, the rename repeats the tab's number, which the label leaves
  out (the same tab, so the same effect); and in a request to make a pane
  full size and name it, it renamed the tab.
- On generated requests it was not trained on (four generator seeds, 48 for
  each template, and 318 templates written after it was trained): 90 wrong
  answers in the 13,651 it gave, none unsafe.
- It knows only the wording of its training rows. Of the requests that
  reached the model on the blind set, two in three still do.

## What is not in this directory

The blind sets v16 and v17 (and a second pair, v18 and v19, written the same
way) are kept with the experiment's records, outside this repository, with
their scores. They are spent for the bundle: v16 shaped rule 3 and v17 gated
the result.
