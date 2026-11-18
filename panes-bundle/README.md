# The panes bundle

`bundle.bin` holds what `panes_bundle.py` reads: two members, the same 51
small nets trained from seeds 0 and 1 (667,725 weights each), and the tables
the reader goes by, in one file of 4.2 MB (file format v6; what v6 changed
is below, "The reader now"). `manifest.json` is its provenance. It
records:

- the file's sha256 and size;
- the trainer's settings and its PyTorch version;
- where the rows came from: the generator's counts, a digest of the
  generator's library as it was staged and the commit of its checkout, the
  agent-style rows' seed and counts, each spent eval set with its sha256 and
  row count, and the eval files whose requests were kept out;
- for each member, the check that the file reads as the trained nets did;
- what the whole reader, with the checks, answers on its own training rows,
  with the second reader as the file switches it on and with it on for every
  kind;
- the kinds the second reader answers (`second_reader.kinds`, below);
- the sha256 of `panes_bundle.py`, `panes_bundle_train.py` and
  `panes_bundle_agent.py`, and of the code that decides the rows and which
  of them are kept (`actions.py`, `tuning.py`, `toolset.py`,
  `job_schemas.py`, `job_rows.py`, `pane_reads.py`, `job_routes.py` and the
  jobs' grammars and checks it asks), as they were when the file was
  written.

`panes_bundle.py` refuses a file whose digest does not match the manifest,
a file whose header points outside it or lacks a table, a file with fewer
than two members or with two members that are the same nets, a file whose
second reader's runs are not of the length this reader holds to, and a
manifest that switches the second reader on for a kind the file has no tool
for.

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
each tool's readings (below). A request decodes only the table rows it
touches.

## When it answers

Only what it was trained on, said with other names. It proposes nothing, and
the model reads the request as before, unless all of these hold:

1. **Every clause is a training clause of its tool, read as that clause was
   labelled.** The clause is taken as the nets read it, its **frame**: each
   copied text becomes one mark that names its argument (`<name>`, or
   `<tab#>` where the text was read as a number), and every other word stays
   as it is. The frame, with every value the nets read from a closed list (a
   side, a layout, a direction), is a **reading**. It must be one of the
   4,084 readings of the training clauses of that tool: the same words in
   the same order, and the same values. For the kinds the manifest names,
   the second reader may answer a clause that is not one of them (below,
   "The second reader"). A reading also records where its
   clause stood. One that stood first or alone in training says all it means
   by itself and may stand anywhere. One that only ever followed another
   clause ("go to it") leans on that clause, and is read only after a clause
   of the same tool.

   So the nets say where the clauses are, which tool each one is and which
   words are copied, and the table says whether that is something the bundle
   was shown. "rename this tab to api" is answered because "rename this tab
   to `<name>`" is a training clause; "rename the logs tab to api", "not a
   tab" and "rearrange the panes" are not training clauses of any tool that
   the nets take them for.
2. Every pair of neighbouring words in each clause stood side by side in a
   training row. A word it never saw must be part of a copied text (a title,
   a command, a program). New word pairs inside or beside copied text are let
   through only for `rename_tab`, `rename_pane`, `go_to_pane` and
   `go_to_tab`.
3. A tool fits each clause, its required arguments are there, and its
   numbers are ones the tool takes. A copied "3rd" is the number 3. A number
   in digits other than 0 to 9 ("３") is not read: the nets take it for a
   number and the reader cannot. A word spelt like one of the frame's marks
   ("<name>") is not read either. A pane's number is its id ("close pane
   412"). A tab's number is its place ("go to tab 4"), but after "tab id" it
   is the tab's id (`id:4`), never its place. For the nets every run of
   digits is one form, so "pane 412" is read as "pane 7" is; the number
   itself is copied.
4. Two clauses are joined only by a word that joined two in training (",",
   ";", "and", "then"), and there are no more clauses than a training row
   had (two).
5. A copied text is no longer than the longest text copied for that argument
   in training: three words for a name, five for a program, seven for a
   command, one for everything else.
6. A copied name, title or command is made of words new to the bundle or of
   words that were copied for an argument of that name in training (801
   words for a name, 309 for a command, 331 for a program, 42 for a pane,
   25 for a tab, 249 for what a list names beside it). A word the bundle
   knows only as wording is wording: "rename this tab please" does not name
   it "please". And no copy ends in the mark that ended the sentence
   ("api!").

   **A word new to the bundle is a name only where it stands alone.** A
   name, or a title a pane or tab is found by, of more than one word is
   made of words that were copied as such in training ("build logs"). Beside
   another word, a new word may as well be wording the bundle does not
   know: "rename this tab to api thx" is not answered, where the reader
   before this one named the tab "api thx". The same holds for what a list
   names beside it. One word is one token, whatever marks it holds
   ("api-server", "v1.2"). A command and a program are not held to this:
   they are made of new words, and they wait for a yes that shows them.
7. The pane or tab that `close_pane`, `close_tab` or `run_in_pane` acts on is
   named with trained words or a number. A word it has never seen may be a
   title, but it may as well be a misspelt "right" or a "3rd".
8. Every word of a clause that names a value is in that clause's call. These
   are the 2,545 words that named a value in every training row that held
   them ("lower", a program's name, a digit, a word of an agent's command)
   and the 27 words that alone named a side, a place, a layout or a
   direction in some row ("left", "top", "grid", "wider"): 2,556 words in
   all. Such a word is in the call when it is the value, or part of a phrase
   that named a value of the call: "above" in "one above another" belongs to
   the layout in rows. A number is in the call as that number or as a tab's
   id (`id:4` for the 4 of "tab id 4"). The other clause's call does not
   count.
9. **No clause goes by what the clause before it changes.** The route
   resolves every call of a request against the desktop as it was when the
   request was made, and it does so on purpose: "close tab 2 and close tab
   3" means the tabs as the person saw them (`needle_cli.py`, "One snapshot
   per request"). That suits "tab 3" and "this tab". It does not suit "it"
   or "there" after a move: after "go to the logs tab", "rename it to api"
   means the logs tab, and the route would rename the tab the request came
   from. A call does not say which of the two its clause meant. And every
   call is sent from the pane the request was made in. So the reader works
   out what each call changes for the calls after it, and what each call is
   resolved by (`panes_bundle.changes`, `goes_by`), and stands aside
   wherever the two readings could differ:

   | A call changes | when it |
   | --- | --- |
   | which pane is "this pane", "it", "here" | goes to a pane or a tab, opens or closes a pane or a tab, or names a pane |
   | which tab is "this tab" | goes to a tab, opens or closes one, goes to or maximizes a pane named by a title or a number (it may be in another tab), closes the caller's own pane, or names a tab |
   | which pane is to the left, right, above or below, or the next | opens or closes a pane, maximizes one, arranges or swaps panes |
   | which tab is tab 2, the next or the last | opens, closes or moves a tab; closes a pane that is not named by its place beside the caller's (it may be the last of its tab, and the tab goes with it) |
   | whether a tab shows one pane at full size | maximizes a pane, restores the layout, or arranges the panes |
   | where the focus is | goes to a pane or a tab, opens one, or maximizes another pane than the caller's |
   | whether the caller's own pane is still there | closes a pane that is not named by its place beside the caller's, or a tab other than the next or the previous one |
   | what a pane or tab is called | gives a name, starts a program or types a command: each word of it, and each word of the title it was found by |

   | A call is resolved by | when it |
   | --- | --- |
   | the caller's pane | names no pane, or "this" one, or one by its side, or the next or previous one; opens a pane; swaps panes |
   | the caller's tab | names no tab, or "this" one, or the next, previous or last; arranges panes; moves a tab; names a pane by a title (looked for in the caller's tab first) |
   | the sides | names a pane by its side, or the next or previous one; swaps panes |
   | the order of the tabs | names a tab by its number, or the next, previous or last; moves a tab |
   | whether the tab shows one pane at full size | maximizes a pane or restores the layout (the route reads it from the desktop as it was) |
   | the focus | moves a tab or swaps panes: both are made through the caller's pane and leave the focus on it |
   | the caller's own pane | always: every call is sent from it |
   | a title | names a pane or a tab by it: each word of it |

   A request whose second call is resolved by something its first call
   changes is not answered: "go to tab 2 and make it wider", "split right
   and make it wider", "close tab 1 and go to tab 2", "rename this tab to
   api and go to the api tab", "maximize this pane and restore the layout",
   "go to the left pane and move this tab left", "close this pane and go to
   tab 1". "rename this tab to api and split right", "split right and go to
   tab 2" and "close the left pane and go to tab 2" are.
10. A rename in the second clause that does not say whether it renames a tab
    or a pane follows a clause about the same kind of thing. Read alone,
    "rename it to api" is a tab's rename, and every training row with such a
    clause has it after a clause about a tab. After a clause about a pane it
    is the pane.
11. Both members give the same calls.
12. **The checks (`actions.interpret`) admit every call as the bundle read
    it** (`needle_cli.checks_agree`). The checks read the whole request and
    add what they find in it: for "call this tab 2" they make the rename a
    rename of tab 2, and for "make this pane 3 rows taller" a resize of pane
    3. The bundle read a name and an amount there, no tab and no pane. A
    proposal is used only when what the checks admit is, for each call, the
    same tool with every argument the bundle read and its value (a title
    spelt as the checks spell one, a pane's number as its id), and nothing
    added but what the checks add to any such call: the caller's own pane
    for a maximize or a pane's rename, "do not restore", and a step of 2 for
    a resize. Where there are two clauses, the checks must also admit for
    each call, shown its clause alone, what they admit shown the whole
    request.

13. **A list stands alone.** A request for a list of the panes, the tabs or
    both (`panes_bundle.LIST`, a kind of the bundle's own) is answered only
    when it is the request's one clause, and the route answers it with the
    read-only list the exact list reader gives (`needle_cli`). What the
    request names beside the list ("list pane 412 and its title": `about`)
    is held to conditions 5 and 6 and is not used: "describe pane 301" shows
    the whole list, pane 301 among the rest.
14. **No resize's verb and direction pull opposite ways.** "Grow this pane
    lower" asks for a taller pane in a person's words and a shorter one in
    the words the bundle was trained on ("lower" is shorter there), and
    "shrink this pane broader" says both. Where a resize's direction grows
    the pane and a word of its clause shrinks it (shrink, reduce, decrease,
    compress, squeeze, shorten, lessen, contract, trim), or the other way
    round (grow, expand, enlarge, stretch, extend, lengthen, increase,
    widen, broaden, heighten), it is not answered. The tuner's own rows hold
    four such, all of one template ("stretch this pane lower", labelled
    shorter); this file does not answer them.

### The second reader

For a kind the manifest switches on (`second_reader.kinds`: a tool, or
`compound` for two clauses), a clause that is none of its tool's readings
(condition 1) may still be answered, if every other condition holds and:

- every run of three neighbours of its frame stood in a training frame of
  the same tool, the clause's start and end counted as neighbours: in one
  that stood first or alone, or in one that followed a clause of the tool
  that comes before it here;
- the values it takes from closed lists were had together by a training
  clause of the tool;
- a side, a place, a layout or a direction it gives is named by a word of
  the clause;
- no call it would answer closes, types or starts a program. Those are
  answered only for whole training clauses.

In a request of two clauses (`compound`) it answers a clause only where it
is switched on for that clause's tool too: beside another clause, a clause
in new wording is no more answered than alone. (As first shipped it was
not so held, and the fourth review found it answering resizes, maximizes,
swaps and renames of panes beside a second clause, kinds its standard had
kept off; below, "What the fourth review found".)

Its answers carry the label "(second reader)" in the history. As this file
(v6) is shipped it is switched on for go_to_pane, list and open_pane: the
kinds it answered on experiment 11's development requests, each exact at
least as often as the route (below, "The reader now"). As v5 was shipped
it was on for arrange_panes, go_to_pane, go_to_tab, move_tab and
rename_tab, and for `compound`, each by the standard of experiment 10
(below, "The reader of format v5"). The
kinds are the manifest's word: the file's digest is of `bundle.bin`, and
whoever can write the manifest can switch every kind on. A close, a typed
command or a started program is never the second reader's to answer,
whatever the manifest says.

None of these is a confidence threshold. They are lookups of wordings,
words and word pairs, agreement, and the checks.

Nothing in front of the model stops a request. With the bundle switched on,
each of these leaves one line on stderr and the model reads the request: no
bundle directory or no manifest; a manifest that is a pipe, a device or a
directory, is larger than 1 MiB or cannot be parsed; a file that does not
match its manifest; a manifest that names a file outside its own directory,
a pipe or a device, or a size no bundle has (what kind of file each of the
two is and how large is asked of the open file before a byte is read, and
no more than 64 MiB is ever read); a header that lacks a table or points
outside the file; members that are the same nets; a reader that cannot be
imported; any error while a request is read. A file whose manifest matches
is taken at its word: one whose weights are nonsense declines every request
and says nothing.

## Where the rows come from

All training text is ours. None of it comes from a third-party dataset.

- **Generated rows** (5,669). The tuner's own rows for the tuned model
  (`tuning.build_data`): kilix-ml's `kilix_panes` pack with
  `corpus-supplement/`, seed 0, 12 for each template. 6,442 were generated
  and 773 dropped because the checks do not admit them as labelled. A request
  equal to a request of any file under `evals/` is never generated (147
  dropped).
- **Agent-style rows** (4,196, `--agent 0`): requests the way coding agents
  write them, from the templates of `panes_bundle_agent.py` (seed 0, 30 for
  each template): panes and tabs named by the ids a list gave them ("close
  pane 412", "rename tab id 4 to api"), a full stop or "only" at the end,
  ";" between two requests, longer names, commands and programs, and lists
  asked for in words the exact list reader does not take ("list pane 412
  and its title"). The templates were written by hand after the request
  history of one machine was read for its wording only; no line of it is a
  template or a row, and `--exclude-history` drops a row equal to one of its
  lines and counts it. Of 4,382 generated, 77 repeat another row, 8 are
  equal to a line of that history, 64 copy a word the bundle knows only as
  wording (such a row would teach the hold on such words, condition 6, an
  exception), and 37 are not admitted by the checks as labelled.
- **Spent eval sets** (1,649 cases, `--spent`): `evals/dev.jsonl`,
  `evals/test.jsonl` and `evals/heldout-v2.jsonl` to `heldout-v14.jsonl`, as
  the router is trained on them. Of 2,126 cases, 270 were dropped because the
  checks do not admit their labels as written and 207 because they repeat a
  request.
- **A current gate set gives no row.** `panes_bundle_train.py` refuses a
  job's current held-out set (`jobs.py`; `evals/heldout-v15.jsonl` today) as
  a source of rows, with `--spent` and with `--rows`, under any path that
  leads to it, and a copy of it under another name by its digest. The
  generator does open every file under `evals/`, that one among them, as the
  tuner does: to keep a request equal to an eval request out of the rows it
  makes.
- 15 of the 11,514 rows are not used: the boundary between two of their
  actions cannot be told from what the row records.

A bundle trained with `--spent` must not be scored on those sets. The
manifest lists them under `rows.spent`.

`jobs.py` calls the job's held-out set the newest one "nobody has
consulted". This work consulted `heldout-v15.jsonl`: it is scored below, and
those scores shaped the rules. It gives the bundle no row and gates nothing
here; whether it can still gate anything else is the owner's to say.

## Retraining and checking

```sh
uv run --with torch==2.6.0 panes_bundle_train.py --spent --agent 0 --exclude-history \
    --out panes-bundle --seeds 0 1 --name v5 \
    --free-text rename_tab rename_pane go_to_pane go_to_tab --second-kinds arrange_panes compound go_to_pane go_to_tab move_tab rename_tab
python3 panes_bundle.py "new right pane with htop"   # what the bundle reads, or why it reads nothing
python3 evaluate.py evals/dev.jsonl --route bundle     # the route as shipped; dev is among the rows, so this is no score
```

`--exclude-history` reads, of this machine's request history
(`history.directory()`), only the request of each panes line that reached a
tuned model, and only to keep an agent-style row equal to one of them out
(`panes_bundle_train.history_requests`). The manifest records how many lines
were compared and how many rows were dropped, and nothing of a line. With
no history the trainer stops.

Training needs PyTorch on a CPU (about nine minutes for both members on
one thread of a laptop, with the second pair of seeds training beside it). Reading needs only the standard library. The trainer writes
nothing unless all of this holds:

- There are at least two different seeds. The bundle answers when its
  members agree, and one member agrees with itself about anything.
- Each net, rounded to half floats, scores a sample of its examples the same
  with PyTorch and with the reader: 200 of them spread evenly over all it
  has (28,834 examples a member here), and no example may come out
  differently.
- The whole reader with the checks, as a request meets them, answers every
  training row it answers as that row is labelled. With the second reader
  off, this file proposes calls for 9,370 of its 11,514 rows. 15 proposals
  are dropped by condition 12: in 7 the checks add the pane that a resize
  names, in 2 the tab of a rename, and in 6 they admit for a clause alone
  something else than for the whole request. The other 9,355 are admitted
  exactly as labelled. With the second reader on for the six kinds it is
  shipped with, or for every kind, it answers 6 more, each as labelled. Of
  the rows it does not answer, 1,114 are labelled with no call (no tool fits
  them), 396 are declined by condition 9 (training rows with two actions
  whose second is resolved by something the first changes), 297 by
  condition 6 (a name or title of several words that holds a word new to
  the bundle), 209 because they arrange the panes "vertical" or
  "horizontal" (below, "What it still gets wrong") and 4 by condition 14.

Run again on the same machine, with the same PyTorch, it wrote this file
byte for byte.

## How it was gated

By kilix-needle's rule: on blind held-out sets, each read once. Blind sets
were written for this change from the tools' specification alone, labelled a
second time by another session, de-duplicated against every training row
and every eval request, and split into halves before anything was scored
(v16, 179 cases, sha256 `8c001317…`; v17, 171 cases, sha256 `f17e9908…`; and
a second pair, v18 and v19). None is in `evals/` yet.

**The reader in this directory was gated once, and failed one line.** Its
gate was declared
before it was trained: once on the agent-style half A1 and once on v19, for
both pairs of members (seeds 0 and 1, and seeds 2 and 3), with the tuned
model qat-9 behind the route. G1: no answer of the bundle unsafe, and the
route no less safe than without it. G2: the route exact at least as often as
without it, and no kind of request losing more than 2 cases. G3: the bundle
answers at least 25% of what reaches the model on A1 and 10% on v19. G4
(seeds 0 and 1, timed on v16 on an eight-core machine): a request it
answers takes at most half the median whole call without it, and one it
declines at most 50 ms more. G5: at most 2 of the first reader's answers not
exact on each set, none where the route without the bundle is exact, and
the second reader exact at least as often as the route without it on what
it answers.

It was read on 2026-11-18, for this file (`464f4ba8…`) and the second pair:

| | A1 (184), seeds 0 and 1 | seeds 2 and 3 | v19 (153), seeds 0 and 1 | seeds 2 and 3 |
| --- | ---: | ---: | ---: | ---: |
| exact, without the bundle | 88 | 88 | 100 | 100 |
| exact, with it | 95 | 95 | 105 | 104 |
| unsafe, with it | 0 | 0 | 0 | 0 |
| of what reaches the model, it answers | 17 of 172 (9.9%) | 18 of 172 (10.5%) | 24 of 145 (16.6%) | 23 of 145 (15.9%) |
| of its answers, exact | 17 | 18 | 24 | 23 |

G1, G2, G4 and G5 pass for both pairs on both sets. G3 fails on A1, where
the line was 25%, so `needle_cli.BUNDLE_BY_DEFAULT` stays off. Of what it
declines on A1, most (106 of 172) holds a pair of neighbouring words that no
training row had: A1 was written by a session that saw only a description
of the tools, and its wording is further from the agent rows than was
expected. Time (G4, seeds 0 and 1, on v16): a request it answers took 237 ms
against 3,804 ms without it; one it declines, 38.6 ms more by the median of
the paired differences (49.6 ms by the difference of medians).

The gates so far were of the first reader (format v2, file `124c491a…`),
which held a request only to known words and word pairs:

- **v16** failed the safety gate on one case. A request to close a tab held
  a word the bundle had never seen, the bundle copied it as the tab's title,
  and the checks admitted that reading. Condition 7 above is the fix. The
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

That gate passed, and the reader it passed was wrong in ways the gate could
not see. `evaluate.py --route bundle` compares calls with labels. It has no
desktop, it counts only closing, typing and starting a program as unsafe,
and no blind set held a request of the kinds below. A pass says how often
the route is exact on requests like those of the set, and nothing about what
the bundle does with a sentence nobody labelled.

## What the reviews found

kilix-needle's rule asks for an independent review beside the gate. Three
were made on 2026-11-17, each by a fresh session of the same assistant that
was given the code and a brief and none of the results. Each ran its
findings through the whole route on the tests' fake desktop; the third also
on a fake desktop of its own that changes when a call runs.

**The first** (of the gated reader) found requests made of known words and
pairs that the bundle answered and misread. None is answered now:

| Request | What the gated reader did |
| --- | --- |
| "rename the logs tab to api" | renamed the caller's own tab |
| "make the left pane wider" | widened the caller's own pane |
| "back to the logs tab" | went to the previous tab |
| "switch to the htop pane and all the others" | went to the htop pane, then stacked the panes |
| "go to tab 2 or rename this tab to api" | did both |
| "close tab 2 or go to tab 1" | asked about the close, and went to tab 1 either way |
| "split right, close this pane, split below" | opened one pane below |
| "go to the tab named two" | went to tab 2 |

It also found that `KILIX_NEEDLE_BUNDLE=off` (or any value but 0) left the
bundle on; now only `1`, `on`, `true` or `yes` switch it on. And that a file
cut short after its header raised an error out of the request.

The answer to it (format v3) held each clause to pairs of neighbouring words
of its own tool's training clauses, with the joining words, the length of a
copy and the words of place. **The second review** found that this did not
hold either: eleven kinds of request that the bundle answered and the route
ran with no question, doing something else than was asked. Two came from the
fix itself. None of its requests is answered now:

| Kind | A request of that kind | What the reviewed reader did | What stops it now |
| --- | --- | --- | --- |
| A number becomes the target | "call this tab 2" | renamed tab 2 to "2": the checks bound the number, and one admitted call was used as it came back | 12 |
| | "make this pane 3 rows taller" | resized pane 3 | 1, and 12 |
| "it" after a move or an open | "go to the logs tab and rename it to api" | went there, then renamed the tab the request came from | 9 |
| | "split right and make it wider" | opened a pane, then widened the caller's | 9 |
| A word of place answered for by the other clause | "make the left pane wider and go to the left pane" | widened the caller's pane | 1, and 8 |
| A layout nobody named | "rearrange the panes", "layout" | stacked the panes | 1 |
| A place nobody named | "bring my cursor back in", "go to it" | went to another pane | 1 |
| Fragments and statements | "i need one", "right please", "tab named work" | opened a pane or a tab | 1 |
| A negation passed over | "not a tab", "not a new tab, thanks" | opened a tab | 1 |
| A name that is not one | "rename this tab please", "rename this tab to api!" | named the tab "please" and "api!" | 1, 6 |
| A tab or a pane by the spelling of the new name | "rename this one api", "rename this one left" | renamed the tab for one and the pane for the other | 1 |
| A resize that names another pane by its title | "widen the log pane" | widened the caller's pane | 1 |
| Two requests of one kind joined by a comma | (5 of 2,994 joined answers) | read as one | 1 |

The cause of most of them is one thing: pairs of neighbours can be walked
into sentences nobody wrote. The reviewer walked 84,000 requests out of the
bundle's own pair tables, and the reviewed reader answered 14,956 of them.
Condition 1 replaced the pairs. The second kind has another cause, the way
the route resolves two calls, and the labels: rows such as "go to the left
pane and run make" are labelled "go left, then run in the current pane".
Condition 9 answers it. The first kind is in the checks, which the model's
calls share (below).

It also found faults that could stop a request, and gaps:

- The manifest's `file` was followed as a path, a named pipe hung the
  request, and the whole file was read before its size was looked at. Now
  the name must be a plain name in the manifest's directory and the file a
  regular file of the stated size, at most 64 MiB.
- With the switch on and the reader not importable, the request ended in an
  error. Now everything in front of the model is guarded.
- A header without the table of value words loaded and condition 8 was
  silently off; a one-member file loaded; the trainer took one seed. Each is
  refused now.
- A missing manifest took the bundle out without a word. It says so now.
  `python3 panes_bundle.py "…"` ended in a traceback on a bad file; it says
  why and returns 1.
- The trainer wrote a file that answered a training row wrongly as long as
  the answer was not risky; checked the first 200 examples of a net, not a
  spread; knew a gate set by its path, so a copy would have been read; and
  recorded neither the generator's library nor `actions.py`. All four are
  changed (above).
- The tests never ran the checked-in bundle through the whole route, and one
  pinned the first kind above as intended. `tests/test_panes_bundle.py` now
  runs every request of both reviews against the checked-in file and through
  the route on the fake desktop.

Found sound by both: the switch; that the bundle's calls go through the same
checks, resolution, confirmation and own-pane guard as the model's (2,490
paired runs in six modes); that no close, typed command or program start ran
without its question.

The answer to the second review (format v4, `bd860c4`) held each clause to
whole training clauses. **The third review** found that this did not hold
either. Its verdict: with the switch on, the bundle still answered requests
with calls that do something other than was asked. What it found, and what
stops it now:

| Kind | A request of that kind | What the reviewed reader did | What stops it now |
| --- | --- | --- | --- |
| A word it never saw, beside a name, taken into the name | "rename this tab to api thx", "rename this tab to uh api", "title this tab api pronto" | named the tab "api thx", "uh api", "api pronto" (264 of 265 such answers, 33 words in all 8 wordings) | 6: a new word is a name only alone |
| | "rename the tab asap", "title this pane asap" | named it "asap" | **nothing: still answered** (below) |
| A closed pane may be the last of its tab | "close the notes pane and shift this tab to position 1" | closed it, and the tab, then moved this tab once more, to the end | 9: a close of a pane not named by its side changes the order of the tabs |
| | "close the notes pane and go to tab 2" | went to the tab that had been tab 2 | 9 |
| Whether a tab shows one pane at full size | "maximize the current pane and restore layout" | maximized it and said "done" for a restore it did not do | 9: maximizing, restoring and arranging change it; a maximize and a restore go by it |
| | "panes grid and full-screen this pane please" (tab already maximized) | arranged a grid and did not maximize | 9 |
| A move of a tab or a swap puts the focus back on the caller's pane | "go to the left pane and move this tab to the left" | went left, then back | 9: the focus changes, and these go by it |
| The caller's own pane or tab closed first | "close this pane and go to tab 1", "close tab 3 and new tab" | sent the second call from a pane that was gone | 9: every call goes by the caller's pane, and a close that may be the caller's changes it |
| A pane renamed or typed into, named again by its old title | "call the build pane api and close the build pane" (found by a second run of the reviewer's sweep) | closed the pane that had been called build | 9: the title it was found by changes |
| "vertical" and "horizontal" | "arrange the panes side by side", "panes in rows" | answered as labelled, and kitty shows those layouts the other way round | a new hold: the bundle does not answer these two layouts |

It also found:

- `manifest.json` itself was read with no look at what kind of file it is:
  a named pipe in its place held the request up. Now the manifest is read
  like the file, a regular file of at most 1 MiB, asked of the open file
  before a byte is read.
- Two members that are the same nets loaded and answered, and condition 11
  was then void; a header with nets of no size loaded. Both are refused.
- Four statements of these documents were not true of the code. They are
  put right here and in `README.md`.
- The tests could not see the first four kinds, and two of them pinned the
  rules behind the first two. Both are changed; the requests of this review
  are run against the checked-in file and through the route, as those of the
  first two are. The tests' fake desktop still does not change when a call
  runs; the reviewer's own, which does, is with the experiment's records,
  and its sweep was run again on this reader (below).

Found sound by it: 31 of the 32 requests the two earlier reviews found are
declined (the other is read, and dropped by the checks); 40 negations,
conditions and questions near what it knows, none answered; 22 names with
marks or in other scripts copied character for character; 52 damaged
directories, all but the manifest as a pipe and the same member twice
standing aside at once; the switch (22 values); the bundle's calls held as
the model's are (126 paired runs in six modes); the trainer's counts and
digests.

## The reader now (format v6, this file)

Experiment 11 of the neuronte-lm research made a bundle for every
kilix-needle job (`../job-bundles/README.md`), on rules written down before
they were measured. For panes, format v6:

- **reads another pane's screen**: `read_pane`, a kind of its own, with the
  pane's id and, if the request gives one, a number of lines. Like the
  exact reader's reads, it is checked against the desktop
  (`pane_reads.check`). Its rows are made up in `job_rows.py`. The four
  agent-style templates whose wording a read now answers are left out
  (`panes_bundle_agent.READ_TEMPLATES`);
- **has more list wordings**: the list rows of `job_rows.py`, each of the
  list's closed values in every wording;
- **reads with three changes**:
  - a training clause of a read, read as it was labelled, is held to its
    label: "the last 20 lines of pane 42" names no last tab. Every other
    panes clause keeps the rule that a word naming a value must be in its
    call;
  - a mark just inside a closing quote is the quoted text's own
    ("'panic!'" copies panic!);
  - "71's" and "#63" are read as numbers;
- **has the second reader on for go_to_pane, list and open_pane**. On
  experiment 11's development requests, with the second reader on for every
  kind, it answered in those three kinds only, each exact at least as often
  as the route and never unsafe. v5's other kinds (arrange_panes,
  go_to_tab, move_tab, rename_tab and compound) are off: it answered none
  of them there, and the experiment's rule switches a kind on only on that
  evidence. Switched on by hand (`Bundle.second_kinds`), they are read much
  as v5 read them, and the tests hold what the reviews of v5 found with
  them on (`WhatV5Reads`, `WhatTheFourthReviewFound`). One request v5's
  second reader answered, "switch to fat", v6 declines with them on too.

This file was trained on 16,614 rows: the tuner's generated rows, the spent
eval sets, the agent-style rows and 5,280 rows of `job_rows.py`. It answers
14,400 of them, all as labelled, and declines the rest. Its twin from seeds
2 and 3, which the experiment's gate also reads, answers 14,402, all as
labelled. How it did on the experiment's held-out requests is in the
research records, not here.

## The reader of format v5

On 2026-11-18 the owner asked for the bundle to answer more. Format v5
widens what it answers in five ways, each held to rules written down before
it was measured (the experiment's records, `runs/exp10/DECLARATION.md`):

- **Agent-style rows** (above, "Where the rows come from"). Of the 161 panes
  requests in this machine's history that reached a tuned model, most were
  sent by coding agents, in wording no template of the tuner has.
- **A list kind** (condition 13). 50 of the 125 of those requests that the
  route still sends to the model ask for a list in words the exact list
  reader does not take, and the model mostly answered them with nothing, a
  new pane or typing.
- **Tab ids and number forms** (condition 3), and **";"** between two
  clauses (condition 4), as the tool's own examples write them.
- **The second reader** (above), switched on kind by kind.

**How a kind was switched on.** For each kind, on the development requests
the second reader answers and the first does not:

- (a) no answer may be unsafe;
- (b) it must be exact at least as often as the tuned model qat-9, both on
  every labelled request (the spent sets dev, test and v15 to v18; the
  generated requests that are not training rows; the reviews' requests;
  labelled probes) and on the reviews' requests and labelled probes alone;
- (c) where a word is put into a request the first reader answers, or two
  are joined by a word that never joined two, it may pass the word over no
  more often than qat-9 does.

The reviews' requests are the 92 of the three reviews, and the second
review's walked requests that the second reader answers: 182 of those, of
the kinds that passed and whose wording may be shown, were labelled by a
labeller that saw neither the second reader's answer nor the model's. The
probes are made from 600 generated and agent-style rows the first reader
answers, 40 of each tool: 326,605 with one of 75 words put in at every
place (negations, "or", "all", "again", "only", and the commonest words of
copied texts that the bundle also knows as wording), 4,000 pairs joined by a
word that joined two in training or by one that never did, and 1,500
requests of three clauses.

Held, as first built, to pairs of neighbours of its tool's frames, the second
reader gave the second review's misreadings back: it answered 11 of the
reviews' 92 requests, all wrongly ("not a new tab, thanks" opened a tab),
and passed over an inserted word with a risky call 163 times. So, before the
standard was applied, it was held to runs of three and to no risky call:

| Kind | Labelled requests it answers | Exact | qat-9 exact on them | Unsafe | Probes with a word put in that it answers | It passes the word over | qat-9 passes it over | On |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| arrange_panes | 87 | 81 | 79 | 0 | 38 | 38 | 38 | yes |
| compound | 56 | 56 | 49 | 0 | 0 | 0 | 0 | yes |
| go_to_pane | 44 | 42 | 37 | 0 | 10 | 10 | 10 | yes |
| go_to_tab | 89 | 85 | 84 | 0 | 9 | 9 | 9 | yes |
| list | 0 | 0 | 0 | 0 | 2 | 2 | 0 | no |
| maximize_pane | 5 | 5 | 3 | 0 | 22 | 22 | 15 | no |
| move_tab | 150 | 150 | 102 | 0 | 3 | 3 | 3 | yes |
| open_pane | 337 | 337 | 119 | 0 | 6 | 6 | 1 | no |
| open_tab | 53 | 53 | 23 | 0 | 10 | 10 | 5 | no |
| rename_pane | 185 | 185 | 127 | 0 | 15 | 15 | 13 | no |
| rename_tab | 56 | 53 | 53 | 0 | 12 | 12 | 12 | yes |
| resize_pane | 257 | 257 | 199 | 0 | 4 | 4 | 3 | no |
| swap_panes | 39 | 39 | 35 | 0 | 20 | 20 | 19 | no |

No joined request or request of three clauses was answered by the second
reader, and no probe put a word into one of two clauses, so `compound` met
(c) with no probe at all (the fourth review; below). Every kind met (a) and
(b). (c) kept seven off: on the probes of
those kinds the model answered otherwise than the row more often than the
second reader did, which for the list is always, since the model has no
list to give. The prediction, made before, was that at most three kinds
would go on.

**What it answers.** Measured on eval sets the bundle is not trained on,
with the tuned model qat-9. These sets shaped the rules, so they gate
nothing:

| Set | Cases | Exact without the bundle | With format v4 | With this file | With the second reader on for every kind | This file answers | Of them exact |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| v15 (the job's own held-out set) | 182 | 90 | 93 | 93 | 93 | 8 of the 180 that reach the model (4.4%) | 8 |
| v16 | 179 | 96 | 102 | 102 | 107 | 18 of 170 (10.6%) | 18 |
| v17 | 171 | 106 | 107 | 107 | 110 | 15 of 166 (9.0%) | 15 |
| v18 | 162 | 92 | 95 | 98 | 99 | 21 of 156 (13.5%) | 21 |

The second reader gives 2, 3, 3 and 4 of those answers. No answer of the
bundle is unsafe: the one unsafe case on v16 is an exact reader's and the
one on v18 the model's, with the bundle and without it. The second pair of
members (seeds 2 and 3) answers 8, 18, 17 and 21, all exact, and the route
is exact on 93, 102, 108 and 98 cases. With the second reader on for every
kind the route would be exact on 5 more cases of v16, 3 of v17 and 1 of
v18. That is what (c) cost here, and the change for two clauses after the
fourth review (below), which declined 2 answers each of v16 and v18, all
exact; what they guard against, a word put into a request, is not in these
sets. As first shipped, before that change, this file answered 20 of v16
and 23 of v18, and the route was exact on 104 and 99.

Against format v4, each through its own reader and the checks: on the
24,676 generated requests of experiment 9's pool this file answers 400 that
format v4 declined (137 by the first reader, 263 by the second) and declines
96 it answered (37 where the members no longer agree, 32 in wording no
longer a training clause's or of a kind the second reader is off for, 21
for an untrained pair of words, 4 by condition 14, 2 for three
neighbours). Every answer it gives on that pool is as labelled (11,111 of
the first reader, 262 of the second; training rows are among the pool). On
v15 to v18 it answers 19 that format v4 declined and declines 1 that it
answered (on v16, a copied text that holds a word the bundle knows only as
wording).

Of the 161 panes requests in this machine's history that reached a tuned
model, 142 would reach it today (the exact readers now take 18; one is
refused). This file answers 55 of them, 10 with a list; format v4 answers
18. The second reader as shipped answers none of them; on for every kind,
it would answer 5 more (4 new panes, 1 list). The lines are counted, never
labelled, so whether the 55 are right is not measured. They are 25 new
tabs, 8 closes of a pane, 6 new panes, 2 commands typed into a pane, 2
renames of a tab, 1 of a pane and 1 switch to another tab. A close and a
command still wait for a yes that shows them.

**A fault found after the standard.** A tab's number after "tab id" was read
as the tab's id, but condition 8 took the id as standing for the number only
where that number had been a tab's id in training: "rename tab id 64 to x"
was read and "rename tab id 4 to api" was declined, the agent module's own
example among them. `stands_for` now takes `id:N` as the number N. The
standard was applied again to the fixed reader and gave the same six kinds
(addendum 3), and the fix changes nothing on the training rows.

### What the fourth review found

A fresh session of the same assistant, given the code of format v5 as first
shipped and a brief, and not the working records behind it, reviewed it on
2026-11-18 (the dates here are UTC). Its verdict: the change did not hold.
The requests below are its own.

| Finding | Which reader | What was done |
| --- | --- | --- |
| 1. "rename this tab to api and grow this pane lower" resized the pane shorter, with no question | second, two clauses | declined (condition 14, and the change below) |
| 2. Beside a second clause the second reader answered resizes, maximizes, swaps and renames of panes, kinds its standard had kept off. (c) was never applied to two clauses: no probe had two | second, two clauses | declined: a clause is answered beside another only for a kind that is on (the change below) |
| 3. Clauses that make no sense, or contradict themselves, acted: "label the pane to the left pane to the left api and go to tab 2", "full-screen this pane to the normal pane layout and go to tab 2" | second, two clauses | declined by the same change |
| 4. Questions and fragments read as requests: "tab 2?", "arrange the panes tall?", "back to the build pane?", "i'd like the panes stacked" | second | left: each is labelled the request it reads (below), and qat-9 reads each the same |
| 5. A list about one pane shows the whole list ("describe pane 301") | first | left, and said in condition 13: it changes nothing, and the model has no list to give |
| 6. The list shows a tab by its id ("tab 20: work"), and "tab 20" is read as a place | both | left: the tool's convention, which the model shares (below, "What it still gets wrong") |
| 7. The second reader's kinds are the manifest's, under no digest | | said (above, "The second reader") |
| 8. The records' dates are a day after the session's | | they are UTC |

**Compared with qat-9, as the declaration asks.** The 31 requests the report
quotes were labelled by a fresh labeller that saw neither the bundle's
answer nor the model's, and qat-9's replies to them were taken on the Core i7-6820HQ under
the lock, both before the comparison was run (`runs/exp10/review/` of the
experiment's records). On those the reviewed reader answered:

| Reader and kind | Answered | Exact | qat-9 exact on them |
| --- | ---: | ---: | ---: |
| second, arrange_panes | 2 | 2 | 2 |
| second, go_to_pane | 2 | 2 | 2 |
| second, go_to_tab | 2 | 2 | 2 |
| second, compound | 7 | 3 | 1 |
| first, list | 4 | 4 | 0 |
| first, rename_tab | 2 | 2 | 2 |
| first, resize_pane | 1 | 0 | 0 |

No answer was unsafe. So by the declared rules no kind is switched off: the
second reader is nowhere worse than qat-9 here, and its four two-clause
misreadings (the three resizes of finding 1, "move this tab left; shrink
this pane broader" read wider among them, and the full-screen of finding 3)
are qat-9's too, which the rules report and leave.
The first reader's misreading, "stretch this pane lower" read as shorter
(labelled taller, and the tuner's own row), is declined: condition 14.

**The change for two clauses goes beyond the rules, and declines only.** The
seven kinds were kept off because the second reader passed over a word put
into a request more often than the model, and nothing in its holds depends
on where a clause stands; `compound` had gone on with no probe of its own.
Now a clause in new wording is answered beside another only for a kind that
is on. It costs answers that were right: of the review's 31, two that the
second reader read as labelled and qat-9 does not ("maximize left pane again
and go to tab 2", "label the pane to the left pane to the left api and go to
tab 2"); of the generated pool, 34, each as labelled, all a new pane beside
a layout; and 2 each of v16 and v18.

That the reader after the review only declines more was checked
(`exp8/compare_readers.py`, the reviewed reader against this one on the same
file): on the generated pool, the training rows, dev, test, v15 to v18, the
reviews' requests and walks, the second review's 14,956 walks, the fourth's
31 and the probes, it answers nothing the reviewed reader did not and
nothing with other calls. It declines 38 of the pool's answers (34 by the
change for two clauses, 4 by condition 14), 4 training rows, 2 each of v16
and v18, and 7 of the review's 31.

**The reader as it now is has been reviewed once, changed after the review
only to decline more, and gated once** (above, "How it was gated"). It
failed the reach line on A1, so the bundle stays off by default.

## The reader of format v4

The nets are the same: the same rows, seeds and settings give the same
weights (the part of the file that holds them is byte for byte what the
gated file held), and the trainer, run again with each reader, wrote this
file byte for byte. What changed is what a request is held to. Conditions
1, 6, 9 and 12 are new since the gated reader and 8 is clause by clause;
since the third review, condition 6 holds a name of several words, the
account of condition 9 knows the order after a close, the layout, the
focus, the caller's own pane and a renamed pane's old title, the two crossed
layouts are not answered, and the manifest is read as carefully as the file.

Every rule added since the third review only declines more. On 40,988
requests (the generated pool, the four eval sets the bundle is not trained
on, the second reviewer's walks, dev, test and the third reviewer's filler
words) the reader as it now is answers no request the reviewed one did not,
and none with other calls (`exp8/compare_readers.py` in the experiment's
records).

What a tighter hold on wording lets through of the 14,956 walked requests
the reader of the second review answered, and what it keeps of the bundle's
right answers on eval sets it is not trained on:

| Hold on a clause | Walked requests still answered | Answers on v15, v16, v17, v18 |
| --- | ---: | --- |
| pairs of neighbours of its tool's frames (the reader of the second review) | 14,956 | 16, 48, 52, 41, all exact |
| three neighbours | 1,297 | 12, 38, 40, 34 |
| four neighbours | 639 | 8, 30, 31, 25 |
| the whole frame is a training clause's | 414 | 8, 23, 20, 20 |
| the whole reading, and conditions 6, 8, 9, 12 (the reader of the third review) | 361 | 6, 17, 14, 16 |
| this reader | 346 | 6, 16, 11, 15 |

The 346 are whole training clauses with other names in them ("close this
tab", "stack everything", "label this pane 2").

**The price is reach.** Measured on eval sets the bundle is not trained on,
with the tuned model qat-9. These sets shaped the rules, so they gate
nothing:

| Set | Cases | Exact without the bundle | With the gated reader | With the reader of the second review | Of the third | With this reader | This reader answers | Of them exact |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| v15 (the job's own held-out set) | 182 | 90 | 95 | 93 | 93 | 93 | 6 of the 180 that reach the model (3.3%) | 6 |
| v16 | 179 | 96 | 116 | 115 | 102 | 102 | 16 of 170 (9.4%) | 16 |
| v17 | 171 | 106 | 121 | 121 | 109 | 107 | 11 of 166 (6.6%) | 11 |
| v18 | 162 | 92 | 103 | 102 | 95 | 95 | 15 of 156 (9.6%) | 15 |

No answer of the bundle was unsafe, and no kind of request lost a case. What
the third review cost on these sets is 5 answers, each exact: 3 names of
two words with a word the bundle never saw (one on each of v16, v17, v18),
the one crossed layout (v17) and one two-clause request (v17). The second
pair of members (seeds 2 and 3, the same tables) answers 6, 16, 11 and 13,
all exact, and the route is exact on 93, 102, 107 and 94 cases.

What each condition turns away that no other does, and whether the answer it
turns away would have been as labelled (`exp8/hold_cost.py` in the
experiment's records):

| Condition | Generated requests (24,676): as labelled / not | v15 to v18 (694 cases): as labelled / not |
| --- | --- | --- |
| 1, the whole reading | 4,020 / 17 | 104 / 0 |
| 9, what the clause before changes | 2,181 / 0 | 15 / 0 |
| the crossed layouts | 250 / 0 | 1 / 0 |
| 8, a word that names a value, clause by clause | 3 / 0 | 0 / 0 |
| 6, a copied name made of wording | 0 / 0 | 2 / 0 |
| 6, a name of several words with a word never seen | 0 / 0 | 3 / 0 |
| 5, 10, and the closing mark of 6 | 0 / 0 | 0 / 0 |

Condition 9 and the crossed layouts count as a loss here only because the
labels call those answers right: they are rows labelled as the route would
run them, and layouts labelled the other way round from kitty. On 24,676
generated requests (5,309 of them training rows; four generator seeds and
318 templates written after training), after the exact readers, the bundle
answers 11,096, every one as labelled. The reader of the second review
answered 17,522 and the gated reader 18,345, 94 of them not as labelled.

Probes made from generated rows the bundle answers:

- One word put into a row at every place (420 rows, 83 words: negations,
  "or", "all", "other", titles, words of place and layout; 252,569
  requests): 11 still got the row's own answer, where the row with the word
  in it is another training clause ("the" six times, "again" four times,
  "a" once). The reader of the second review passed a word over 863 times
  and the gated reader 12,386 times.
- Two rows joined by "and", "then" or a comma, 40 for every two tools
  (7,840 requests), through the reader and the checks: 1,657 answers,
  every one what the checks admit for each row alone. The reader proposed
  1,716. In 56 the checks refuse a call, and in 3 they admit something else
  than for each row alone (a rename gets the other clause's tab, a resize
  the other's amount); condition 12 drops all 59. The route of the second
  review answered 3,096, 5 of them two requests of one kind read as one,
  and the gated route 3,634, 329 of them otherwise than for each row alone.
- Two rows joined by a word that never joined two in training (2,000), or
  three rows joined (1,500): none answered. Joined by a trained word
  (2,000): 544 answered, each with the two rows' labels in order.
- A word of place put into a row, and the row joined to a clause whose own
  call holds that place, as the second reviewer did (400 rows, 22,967
  sentences the reader declines alone, 91,868 joined requests): none
  answered. The reader of the second review answered 826 of the reviewer's
  1,972.
- The third reviewer's sweep: every pair of 254 clauses made from generator
  readings, joined by "and" (64,516 requests), each answer run on three
  fake desktops that change when a call runs, against the same calls run
  one by one. 84 of the runs differ, all of two kinds: a pane or tab named
  twice by different words (the second call fails either way), or a pane
  renamed by its side and then named by the title it had when the request
  was made, which is what those words say. The reader of the third review
  had 587 such runs, and 789 more that differed in where the focus ended.

**Time**, measured again with this reader the way it was for the gate (Core i7-6820HQ,
eight cores, nothing else measured on it, under the benchmark lock; three
whole calls each way in a new process): a request of v16 the bundle answers
took 231 ms (median, 16 requests) with it and 3,830 ms without; a request
it declines took 32 ms more by the median of the paired differences (40
requests). Inside a running process the file loads in 9 ms, an answer takes
1.8 ms and a decline 0.5 ms (medians).

More generated wording does not buy the reach back. With the readings of
all 24,676 generated requests added to the table (1,043 more readings; the
nets untouched, every other condition in place, measured on the reader of
the third review) the bundle answers no more on v15, v16 and v18 and one
more on v17. Where the rule on word pairs is lifted as well it answers 3, 1
and 1 more on v16, v17 and v18, all as labelled. What it does not answer on
these sets is wording no template of ours has.

**Format v4's reader was neither reviewed nor gated.** The third review was
of the reader before its last change (`bd860c4`). The blind half v19 was
kept for a reader a review had left unchanged; it was read at last to gate
format v5 (above, "How it was gated").

## What it still gets wrong, and what is not its to fix

- **The second reader answers fragments, and names it cannot place.** Of
  the 182 walked requests of its six kinds that were labelled, it answers
  15 otherwise than labelled, and every one of them is a request the
  labeller judged to ask for nothing plainly, or to name something that may
  be a tab or a pane: "a grid", "stacked layout", "the panes stacked?",
  "over to the right", "could you switch to work", "name this one
  frontend". The tuned model answers 14 of the same 15 otherwise than
  labelled too. That is the line the owner set for it: no worse than the
  model, and never unsafe.
- **Since v5 it does not split some requests at "and".** "close the next
  tab and open a new tab" goes to the model. The agent rows' "open a new
  tab named X and run Y" is one clause, and the separator nets learnt from
  it that "and" between a name and a verb need not join two clauses; the
  whole is then wording no tool was trained on. v5 did not split "rename
  this tab to api and split right" either; v6 does again. "rename this tab
  to logs and split right" and "rename this tab to api then split right"
  are still read (`tests/test_panes_bundle.py`, `NO_LONGER_READ`).
- **A command still takes in new words that have a digit or a mark.**
  A command or a program is made of words new to the bundle, and waits for
  a yes that shows it. The fifth review found it taking in a plain word
  after the command: "run make tonight in pane 42" read the command "make
  tonight". Since then, a command of several words that holds a plain word
  no training row had as wording or in a command or a program is declined
  (`NEW_IN_COMMAND`). A word with
  a digit or a mark ("-n", "15", "nvidia-smi", a path) may still be new, so
  "run make -j8 in pane 42" still reads "-j8" as part of the command, as it
  should, and "run make 2x in pane 42" reads "make 2x" too. A quoted
  command is not read at all ("an untrained word pair"): no training row
  quotes one.
- **A full stop after a path.** The tokenizer leaves a "." on a word with a
  "/" in it, so a path keeps its own dots; "cat /etc/hosts." would copy
  "/etc/hosts." (the fifth review). A copy that ends in such a word is
  declined as one that ends in the mark of the sentence (`CLOSED`).
- **Only a rename names a tab by its id.** The agent rows name a tab by its
  id only in renames. "go to tab id 4" and "close tab id 7" are no
  training clause and go to the model, and so does "rename tab 4 to api",
  where 4 would be a place. The model reads "go to tab id 4" as the tab in
  the fourth place (the fourth review's requests).
- **The list shows a tab by its id, and "tab N" is a place.** The list the
  route gives (the exact list reader's, and the bundle's list kind with it)
  writes "tab 20: work", 20 being the tab's id. "Tab 20" in a request is the
  tab in the twentieth place, as the tool defines it, for the bundle and the
  model alike: "change the title of tab 20 to api" is refused when there are
  fewer tabs. Once a tab has been closed, ids and places come apart, and an
  agent that copies "tab 4" from a list may act on another tab with no
  question. That is the list's wording and the tool's convention to change,
  not the bundle's (the fourth review).
- **It repeats its labels.** A training clause is answered as it was
  labelled, with other names. Where a label is itself not what a person
  means, the bundle says the same for that literal wording: a resize that
  names another pane by a title, in the one wording a training row has,
  resizes the caller's own pane. No resize label in any eval set holds a
  pane.
- **A call that cannot be resolved ends the request.** "go to the bottom
  pane" is read as the pane below, and where there is none the request
  fails and the model is not asked. That is the route's way with any call,
  the model's too.
- **In the route, for any proposer's calls** (not changed here; the owner's
  to decide):
  - The checks bind a number in the request to a rename's tab or a resize's
    pane ("call this tab 2", "make this pane 3 rows taller"). The bundle
    stands aside for such a request (12). A model's call is still bound
    that way.
  - Every call is resolved against the desktop as it was when the request
    was made. That is deliberate, and right for "close tab 2 and close tab
    3"; it is not what "it" means after a move. The bundle stands aside
    (9). A model that answers "go to the logs tab and rename it to api"
    with a switch and a rename renames the caller's tab, and the tuned model
    is trained on rows labelled that way ("go to the left pane and run
    make": go left, then run in the current pane).
  - When a risky call is answered "no", the other calls of the request
    still run: "close the left pane and arrange the rest in a grid"
    arranges the panes whatever the answer.
  - "vertical" and "horizontal" are the other way round from kitty. The
    tool's description says "vertical is side by side; horizontal is one
    above another" (`actions.py`), and the rows and the eval cases are
    labelled by it; kitty's own source lays out its Vertical layout along
    the height, one pane below another, and its Horizontal layout along the
    width (`kitty/layout/vertical.py`). Kilix's configuration enables
    neither, so such a call ends in "not enabled here". The bundle does not
    answer either layout. A model's call is sent as labelled.
  - A tab's new name is sent without `--` (`kilix.py`, `set-tab-title`), so
    a name that begins with a dash is read by kitty as an option.
- **A word it never saw, standing alone where a name stands, is the
  name.** "rename the tab asap" names the tab "asap", and "call this pane
  thx" names the pane "thx". The reader cannot tell such a word from a
  name it has not seen ("rename the tab q3"), and a list of words that are
  not names is always one word short. Beside another word such a word is not
  taken (6). Of the reviewer's 67 words in 8 wordings (536 requests) the
  route uses the bundle's reading for 67, and in 66 the word is the name:
  33 words, each in the two wordings where the name follows the noun with
  no word between ("rename the tab W", "title this pane W"). The reader of
  the third review did that for 264 requests. The tuned model is no better
  here: of those 66 requests qat-9 takes the word into the name on 52 and
  does nothing on 14, and of all 536 it takes the word into the name on 273,
  among them 185 of the 402 that do give a name ("rename this tab to api
  thx" names the tab "api thx"; the bundle declines those now). A rule
  against the 66 would send them to a reader that mostly does the same, and
  it is not added. That was decided before the model's replies were read
  (the experiment's records, `runs/exp8/REVIEW.md`).
- **It knows only the wording of its training rows.** Of the requests that
  reach the model it answers about one in ten on the sets written for this
  change and one in thirty on the job's own held-out set.
- **A file whose manifest matches is taken at its word.** One whose
  weights are nonsense (all NaN) loads, declines every request and says
  nothing.

## What is not in this directory

The blind sets v16 to v19 and the agent-style sets A1 and A2 are kept with
the experiment's records, outside this repository, with their scores, the
reviewers' reports and scripts, and the probes. v16, v17 and v18 are spent
for the bundle: v16 shaped condition 7, v17 gated the first reader, and v18
was a check set while the answers to the reviews were written. A1 (184
cases) and v19 (153) are for the gate of this file, and nothing has read
them; A2 (177) is kept for a later one. A1 and A2 were written and
labelled by two fresh assistant sessions (subagents that had seen no code,
no row and no history) from a specification of the tool alone, in the style
coding agents send requests; 53 requests equal to a training row and 6
equal to one of an earlier blind set were dropped before the set was split.
