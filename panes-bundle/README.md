# The panes bundle

`bundle.bin` holds what `panes_bundle.py` reads: two members, the same 42
small nets trained from seeds 0 and 1 (598,263 weights each), and the tables
of words the reader goes by, in one file of 3.2 MB. `manifest.json` is its
provenance. It records:

- the file's sha256 and size;
- the trainer's settings and its PyTorch version;
- where the rows came from: the generator's counts, each spent eval set with
  its sha256 and row count, and the eval files whose requests were kept out;
- for each member, the check that the file reads as the trained nets did;
- what the whole reader, with the checks, answers on its own training rows;
- the sha256 of `panes_bundle.py` and `panes_bundle_train.py` as they were
  when the file was written.

`panes_bundle.py` refuses a file whose digest does not match the manifest,
and a file whose header points outside it.

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
feature numbers, each member's weights as little-endian half floats, and
each tool's frames (below). A request decodes only the table rows it
touches.

## When it answers

Only on ground it was trained on. It proposes nothing, and the model reads
the request as before, unless all of these hold:

1. Every pair of neighbouring words in each clause stood side by side in a
   training row. A word it never saw must be part of a copied text (a title,
   a command, a program). New word pairs inside or beside copied text are let
   through only for `rename_tab`, `rename_pane`, `go_to_pane` and
   `go_to_tab`.
2. A tool fits each clause, its required arguments are there, and its
   numbers are ones the tool takes. A copied "3rd" is the number 3. A number
   in digits other than 0 to 9 ("３") is not read: the nets take it for a
   number and the reader cannot.
3. Each clause is wording of its own tool. The clause is taken as its answer
   reads it, its **frame**: each copied text becomes one mark that names its
   argument (`<name>`, or `<tab#>` where the text was read as a number),
   wherever that text stands in the clause, and every other word stays as it
   is. Every pair of neighbours in the frame must have stood side by side in
   a training clause of the same tool (4,011 pairs for the 14 tools).
   "rename the logs tab to api" is made of pairs the bundle knows from other
   tools, but no rename it was trained on passes over a tab's name; "go to
   the tab named two" has a title where only a number stood.
4. Two clauses are joined only by a word that joined two in training (",",
   "and", "then"), and there are no more clauses than a training row had
   (two).
5. A copied text is no longer than the longest text copied for that argument
   in training: two words for the name a rename gives, four for a command,
   three for the program of a new pane, one for everything else. A name of
   nine words is the rest of the sentence.
6. The pane or tab that `close_pane`, `close_tab` or `run_in_pane` acts on is
   named with trained words or a number. A word it has never seen may be a
   title, but it may as well be a misspelt "right" or a "3rd".
7. Both members give the same calls.
8. Every word of the request that names a value is in the answer. These are
   the 188 words that named a value in every training row that held them
   ("lower", a program's name, a digit) and the 27 words that alone named a
   side, a place, a layout or a direction in some row ("left", "top",
   "grid", "wider"): 199 words in all. Such a word is in the answer when it
   is the value, or part of a phrase that named a value of the answer:
   "above" in "one above another" belongs to the layout in rows. "take me to
   the top pane to the left" is not answered: "top" is not in an answer that
   goes left.
9. The checks (`actions.interpret`) admit every call. If they refuse one, the
   whole proposal is dropped. Where there are two clauses, the checks must
   also admit for each call, shown its clause alone, what they admit shown
   the whole request (`needle_cli.checks_agree`). The checks read the whole
   request and bind what they find in it: in "rename this tab to api and go
   to tab 2" they give the rename tab 2. The bundle read that "tab 2" for the
   other call, so it stands aside.

None of these is a confidence threshold. They are lookups of words and word
pairs, agreement, and the checks.

Nothing that goes wrong in the bundle stops a request. A file that cannot be
loaded, or an error while a request is read, takes the bundle out for that
process with one line on stderr, and the model reads the request.

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
- **A current gate set gives no row.** `panes_bundle_train.py` refuses a
  job's current held-out set (`jobs.py`; `evals/heldout-v15.jsonl` today) as
  a source of rows, with `--spent` and with `--rows`, under any path that
  leads to it. The generator does open every file under `evals/`, that one
  among them, as the tuner does: to keep a request equal to an eval request
  out of the rows it makes.
- 15 of the 7,318 rows are not used: the boundary between two of their
  actions cannot be told from what the row records.

A bundle trained with `--spent` must not be scored on those sets. The
manifest lists them under `rows.spent`.

## Retraining and checking

```sh
uv run --with torch==2.6.0 panes_bundle_train.py --spent --out panes-bundle \
    --seeds 0 1 --name v2 --free-text rename_tab rename_pane go_to_pane go_to_tab
python3 panes_bundle.py "put a pane underneath with htop"   # what the bundle reads, or why it reads nothing
python3 evaluate.py evals/dev.jsonl --route bundle     # the route as shipped; dev is among the rows, so this is no score
```

Training needs PyTorch on a CPU (about twelve minutes for both members, one
thread). Reading needs only the standard library. Before it writes a file
the trainer makes two checks, and writes nothing if either fails:

- It rounds each net to half floats, scores 200 examples of every net with
  PyTorch and again with the reader, and no example may come out
  differently.
- It runs the whole reader and the checks on every training row, as a
  request meets them, and no row may be answered with a risky action the row
  does not hold. For 6,411 of its 7,318 rows this file proposes calls. 15
  proposals are dropped by condition 9 (in 10 of them the checks add the
  tab's number to a rename that follows a switch). Of the 6,396 answers,
  6,391 are admitted exactly as labelled; in the other 5 the checks add the
  pane that a resize names.

Run again on the same machine, with the same PyTorch, it wrote this file
byte for byte.

## How it was gated

By kilix-needle's rule: on blind held-out sets, each read once. Blind sets
were written for this change from the tools' specification alone, labelled a
second time by another session, de-duplicated against every training row
and every eval request, and split into halves before anything was scored
(v16, 179 cases, sha256 `8c001317…`; v17, 171 cases, sha256 `f17e9908…`; and
a second pair, v18 and v19). None is in `evals/` yet.

The first gates were of the reader as it then was (format v2, file
`124c491a…`):

- **v16** failed the safety gate on one case. A request to close a tab held
  a word the bundle had never seen, the bundle copied it as the tab's title,
  and the checks admitted that reading. Condition 6 above is the fix. The
  route was exact on 116 of the 179 cases against 96 without the bundle.
- **Generated requests** showed a second kind of mistake: an argument left
  out although the request names it ("new lower pane with node" read as a
  pane with node and no side). The first half of condition 8 is the fix.
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
  (32 ms more by the median of the paired differences).

## What the review found

kilix-needle's rule asks for an independent review beside the gate. One was
made after the gate on v17 (2026-11-17), by a fresh session of the same
assistant that was given the code and not the results. It found requests
the gated reader answered and misread. Each was confirmed by running the
whole route on the tests' fake desktop:

| Request | What the gated reader did | What this reader does |
| --- | --- | --- |
| "rename the logs tab to api" | renamed the caller's own tab | declines: no rename it was trained on passes over a tab's name (3) |
| "make the left pane wider" | widened the caller's own pane | declines: "left" is not in the answer (8) |
| "back to the logs tab" | went to the previous tab | declines (3) |
| "switch to the htop pane and all the others" | went to the htop pane, then stacked the panes | declines (3) |
| "go to tab 2 or rename this tab to api" | did both | declines: "or" never joined two clauses (4) |
| "close tab 2 or go to tab 1" | asked about the close, and went to tab 1 either way | declines (4) |
| "split right, close this pane, split below" | opened one pane below | declines (3) |
| "go to the tab named two" | went to tab 2 | declines: a title where only a number stood (3) |

No blind set held such a request, and the scorer calls only closing, typing
and starting a program unsafe, so neither gate could see them. Renaming,
resizing, focusing and arranging run at once with no question.

The same review found that `KILIX_NEEDLE_BUNDLE=off` (or any value but 0)
left the bundle on; now only `1`, `on`, `true` or `yes` switch it on. It
found that a file cut short after its header raised an error out of the
request; now nothing wrong with the file stops a request. And it noted, in
the checks that the model's calls share, that "rename this tab to api and go
to tab 1" renames tab 1. The bundle read such a request rightly and the
checks then gave the rename the other clause's tab. The second half of
condition 9 keeps the bundle out of it; the checks themselves are unchanged,
so the model's proposal for such a request is still bound that way.

## The fix (format v3, this file)

The nets are the same: the same rows, seeds and settings give the same
weights. What changed is what a request is held to: conditions 3, 4 and 5,
the second half of 8 and of 9, and in 2 the digits and "3rd".

Every one of them only declines more. On 25,496 requests (24,676 generated
ones and six eval sets) the gated reader proposed calls for 19,735 and this
reader for 18,880, none of which the gated reader had declined. One answer
differs: a "3rd" that was copied as a tab's title is now the number 3.

Measured on eval sets the bundle is not trained on, with the tuned model
qat-9. These sets shaped the rules, so they gate nothing:

| Set | Cases | Exact without the bundle | With the gated reader | With this reader | This reader answers | Of them exact |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| v15 (the job's own held-out set) | 182 | 90 | 95 | 93 | 16 of the 180 that reach the model (9%) | 16 |
| v16 | 179 | 96 | 116 | 115 | 48 of 170 (28%) | 48 |
| v17 | 171 | 106 | 121 | 121 | 53 of 166 (32%) | 52 |
| v18 | 162 | 92 | 103 | 102 | 42 of 156 (27%) | 41 |

No answer of the bundle was unsafe, and no kind of request lost a case. On
v15, which other hands wrote, it answers far less than on the sets written
for this change.

On 24,676 generated requests (5,309 of them training rows; four generator
seeds and 318 templates written after training), after the exact readers,
the bundle answers 17,522, every one as labelled. The gated reader answered
18,345, 94 of them not as labelled. On the requests from the new templates
this reader gives 3,579 answers; the gated reader gave 4,288 with 19 wrong.

Probes made from generated rows the bundle answers:

- One word put into a row at every place (420 rows, 83 words: negations,
  "or", "all", "other", titles, words of place and layout; 248,087
  requests): 863 still got the row's own answer. What is passed over is
  mostly a small word ("the", "pane", "in", "a", "to"). A word of place or
  layout is passed over only where it repeats what the row already says
  ("add a right pane right"). No "not", "don't", "never", "no", "without",
  "except", "or", "nor", "but", "instead", "unless", "if", "when", "only",
  "also", "both", "each", "every", "twice", "cancel", "undo" or "stop" is
  passed over.
- Two rows joined by "and", "then" or a comma, 40 for every two tools
  (7,840 requests), through the reader and the checks: 3,096 answers, 3,091
  of them what the checks admit for each row alone. The other 5 are two
  requests of the same kind joined by a comma and read as one. The gated
  route, probed the same way, answered 3,634 of its 7,840, 329 of them
  otherwise than for each row alone.
- Two rows joined by a word that never joined two in training (2,000), or
  three rows joined (1,500): none answered.

**The fixed reader has not been gated yet.** The blind half v19 is kept for
it and nothing has read it. Until it has passed,
`needle_cli.BUNDLE_BY_DEFAULT` is off.

## What it still gets wrong

- In a request to make a pane full size and name it, it renames the tab
  (one case each on v17 and v18). In its training rows a rename that follows
  another action is of the tab 47 times and of a pane once. More rows of
  that kind would be the remedy.
- A resize that names another pane by a title, in the one wording a training
  row has ("narrow the log pane by 2"), resizes the caller's own pane, as
  that row is labelled. No resize label in any eval set holds a pane. Where
  a side names the other pane ("make the left pane wider") this reader now
  declines, although 11 eval cases label that as a resize of the caller's
  pane.
- A word can still be passed over where it stands in trained wording:
  "maximize the pane up to the right" goes to the pane on the right, and
  "squeeze this pane by 3 rows" narrows it by 3.
- Two requests of the same kind joined by a comma can be read as one:
  "split below and run btop, carve a pane below and call it db" opens one
  pane that runs btop and is called db (5 of 3,096 joined answers).
- It knows only the wording of its training rows. Of the requests that
  reach the model, it answers about three in ten on the sets written for
  this change and one in ten on the job's own held-out set.

## What is not in this directory

The blind sets v16 to v19 are kept with the experiment's records, outside
this repository, with their scores. v16, v17 and v18 are spent for the
bundle: v16 shaped condition 6, v17 gated the first reader, and v18 was a
check set while the fix was written.
