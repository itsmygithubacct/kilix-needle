# The panes bundle

`bundle.bin` holds what `panes_bundle.py` reads: two members, the same 42
small nets trained from seeds 0 and 1 (598,263 weights each), and the tables
the reader goes by, in one file of 3.3 MB. `manifest.json` is its
provenance. It records:

- the file's sha256 and size;
- the trainer's settings and its PyTorch version;
- where the rows came from: the generator's counts, a digest of the
  generator's library as it was staged and the commit of its checkout, each
  spent eval set with its sha256 and row count, and the eval files whose
  requests were kept out;
- for each member, the check that the file reads as the trained nets did;
- what the whole reader, with the checks, answers on its own training rows;
- the sha256 of `panes_bundle.py` and `panes_bundle_train.py`, and of
  `actions.py`, `tuning.py` and `toolset.py`, which decide the rows, as they
  were when the file was written.

`panes_bundle.py` refuses a file whose digest does not match the manifest,
a file whose header points outside it or lacks a table, and a file with
fewer than two members or with two members that are the same nets.

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
   3,237 readings of the training clauses of that tool: the same words in
   the same order, and the same values. A reading also records where its
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
   ("<name>") is not read either.
4. Two clauses are joined only by a word that joined two in training (",",
   "and", "then"), and there are no more clauses than a training row had
   (two).
5. A copied text is no longer than the longest text copied for that argument
   in training: two words for the name a rename gives, four for a command,
   three for the program of a new pane, one for everything else.
6. A copied name, title or command is made of words new to the bundle or of
   words that were copied for an argument of that name in training (77 words
   for a name, 70 for a command, 42 for a pane, 35 for a program, 25 for a
   tab). A word the bundle knows only as wording is wording: "rename this
   tab please" does not name it "please". And no copy ends in the mark that
   ended the sentence ("api!").

   **A word new to the bundle is a name only where it stands alone.** A
   name, or a title a pane or tab is found by, of more than one word is
   made of words that were copied as such in training ("build logs"). Beside
   another word, a new word may as well be wording the bundle does not
   know: "rename this tab to api thx" is not answered, where the reader
   before this one named the tab "api thx". One word is one token, whatever
   marks it holds ("api-server", "v1.2"). A command and a program are not
   held to this: they are made of new words, and they wait for a yes that
   shows them.
7. The pane or tab that `close_pane`, `close_tab` or `run_in_pane` acts on is
   named with trained words or a number. A word it has never seen may be a
   title, but it may as well be a misspelt "right" or a "3rd".
8. Every word of a clause that names a value is in that clause's call. These
   are the 188 words that named a value in every training row that held them
   ("lower", a program's name, a digit) and the 27 words that alone named a
   side, a place, a layout or a direction in some row ("left", "top",
   "grid", "wider"): 199 words in all. Such a word is in the call when it is
   the value, or part of a phrase that named a value of the call: "above" in
   "one above another" belongs to the layout in rows. The other clause's
   call does not count.
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
- 15 of the 7,318 rows are not used: the boundary between two of their
  actions cannot be told from what the row records.

A bundle trained with `--spent` must not be scored on those sets. The
manifest lists them under `rows.spent`.

`jobs.py` calls the job's held-out set the newest one "nobody has
consulted". This work consulted `heldout-v15.jsonl`: it is scored below, and
those scores shaped the rules. It gives the bundle no row and gates nothing
here; whether it can still gate anything else is the owner's to say.

## Retraining and checking

```sh
uv run --with torch==2.6.0 panes_bundle_train.py --spent --out panes-bundle \
    --seeds 0 1 --name v4 --free-text rename_tab rename_pane go_to_pane go_to_tab
python3 panes_bundle.py "new right pane with htop"   # what the bundle reads, or why it reads nothing
python3 evaluate.py evals/dev.jsonl --route bundle     # the route as shipped; dev is among the rows, so this is no score
```

Training needs PyTorch on a CPU (about twelve minutes for both members, one
thread). Reading needs only the standard library. The trainer writes
nothing unless all of this holds:

- There are at least two different seeds. The bundle answers when its
  members agree, and one member agrees with itself about anything.
- Each net, rounded to half floats, scores a sample of its examples the same
  with PyTorch and with the reader: 200 of them spread evenly over all it
  has (21,399 examples a member here), and no example may come out
  differently.
- The whole reader with the checks, as a request meets them, answers every
  training row it answers as that row is labelled. For 5,841 of its 7,318
  rows this file proposes calls. 10 proposals are dropped by condition 12:
  in 5 the checks add the pane that a resize names, and in 5 they admit for
  a clause alone something else than for the whole request. The other 5,831
  are admitted exactly as labelled. Of the rows it does not answer, 342 are
  declined by condition 9 (training rows with two actions whose second is
  resolved by something the first changes) and 209 because they arrange the
  panes "vertical" or "horizontal" (below, "What it still gets wrong").

Run again on the same machine, with the same PyTorch, it wrote this file
byte for byte.

## How it was gated

By kilix-needle's rule: on blind held-out sets, each read once. Blind sets
were written for this change from the tools' specification alone, labelled a
second time by another session, de-duplicated against every training row
and every eval request, and split into halves before anything was scored
(v16, 179 cases, sha256 `8c001317…`; v17, 171 cases, sha256 `f17e9908…`; and
a second pair, v18 and v19). None is in `evals/` yet.

**The reader in this directory has not been gated.** The gates so far were
of the first reader (format v2, file `124c491a…`), which held a request only
to known words and word pairs:

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

## The reader now (format v4, this file)

The nets are the same: the same rows, seeds and settings give the same
weights (the part of the file that holds them is byte for byte what the
gated file held). What changed is what a request is held to: conditions 1,
6, 9 and 12 are new, 8 is clause by clause, and the file is loaded more
carefully.

What a tighter hold on wording lets through of the 14,956 walked requests
the reviewed reader answered, and what it keeps of the bundle's right
answers on eval sets it is not trained on:

| Hold on a clause | Walked requests still answered | Answers on v15, v16, v17, v18 |
| --- | ---: | --- |
| pairs of neighbours of its tool's frames (the reviewed reader) | 14,956 | 16, 48, 52, 41, all exact |
| three neighbours | 1,297 | 12, 38, 40, 34 |
| four neighbours | 639 | 8, 30, 31, 25 |
| the whole frame is a training clause's | 414 | 8, 23, 20, 20 |
| this reader: the whole reading, and conditions 6, 8, 9, 12 | 361 | 6, 17, 14, 16 |

The 361 are whole training clauses with other names in them ("close
this tab", "stack everything", "label this pane 2").

**The price is reach.** Measured on eval sets the bundle is not trained on,
with the tuned model qat-9. These sets shaped the rules, so they gate
nothing:

| Set | Cases | Exact without the bundle | With the gated reader | With the reviewed reader | With this reader | This reader answers | Of them exact |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| v15 (the job's own held-out set) | 182 | 90 | 95 | 93 | 93 | 6 of the 180 that reach the model (3%) | 6 |
| v16 | 179 | 96 | 116 | 115 | 102 | 17 of 170 (10%) | 17 |
| v17 | 171 | 106 | 121 | 121 | 109 | 14 of 166 (8%) | 14 |
| v18 | 162 | 92 | 103 | 102 | 95 | 16 of 156 (10%) | 16 |

No answer of the bundle was unsafe, and no kind of request lost a case. The
reviewed reader answered 16, 48, 52 and 41 of those requests, and the gated
reader 58 of the 166 on v17. The second pair of members (seeds 2 and 3, the
same tables) answers 6, 16, 14 and 14, all exact, and the route is exact on
93, 102, 109 and 94 cases.

What each condition turns away that no other does, and whether the answer it
turns away would have been as labelled (`exp8/hold_cost.py` in the
experiment's records):

| Condition | Generated requests (24,676): as labelled / not | v15 to v18 (694 cases): as labelled / not |
| --- | --- | --- |
| 1, the whole reading | 4,141 / 17 | 108 / 0 |
| 9, what the clause before changes | 1,741 / 0 | 14 / 0 |
| 8, a word that names a value, clause by clause | 5 / 0 | 0 / 0 |
| 6, a copied name made of wording | 0 / 0 | 2 / 0 |
| 5, 10, and the closing mark of 6 | 0 / 0 | 0 / 0 |

Condition 9 counts as a loss here only because the labels call those
answers right: they are the rows labelled as the route would run them. On
24,676 generated requests (5,309 of them training rows; four generator
seeds and 318 templates written after training), after the exact readers,
the bundle answers 11,926, every one as labelled. The reviewed reader
answered 17,522 and the gated reader 18,345, 94 of them not as labelled.

Probes made from generated rows the bundle answers:

- One word put into a row at every place (420 rows, 83 words: negations,
  "or", "all", "other", titles, words of place and layout; 248,087
  requests): 8 still got the row's own answer, where the row with the word
  in it is another training clause ("the" six times, "layout" and "again"
  once each). The reviewed reader passed a word over 863 times and the
  gated reader 12,386 times.
- Two rows joined by "and", "then" or a comma, 40 for every two tools
  (7,840 requests), through the reader and the checks: 1,777 answers,
  every one what the checks admit for each row alone. The reader proposed
  1,843. In 57 the checks refuse a call, and in 9 they admit something else
  than for each row alone (a rename gets the other clause's tab, a resize
  the other's amount); condition 12 drops all 66. The reviewed route
  answered 3,096, 5 of them two requests of one kind read as one, and the
  gated route 3,634, 329 of them otherwise than for each row alone.
- Two rows joined by a word that never joined two in training (2,000), or
  three rows joined (1,500): none answered. Joined by a trained word
  (2,000): 546 answered, each with the two rows' labels in order.
- A word of place put into a row, and the row joined to a clause whose own
  call holds that place, as the second reviewer did (400 rows, 23,027
  sentences the reader declines alone, 92,108 joined requests): none
  answered. The reviewed reader answered 826 of the reviewer's 1,972.

More generated wording does not buy the reach back. With the readings of
all 24,676 generated requests added to the table (1,043 more readings; the
nets untouched, every other condition in place) the bundle answers no more
on v15, v16 and v18 and one more on v17. Where the rule on word pairs is
lifted as well it answers 3, 1 and 1 more on v16, v17 and v18, all as
labelled. What it does not answer on these sets is wording no template of
ours has.

**The reader as it now is has been neither reviewed nor gated.** The third
review was of the reader before its last change (`bd860c4`). The blind half
v19 is kept and nothing has read it: it is to gate a reader that a review
has left unchanged, and none has yet. `needle_cli.BUNDLE_BY_DEFAULT` is off.

## What it still gets wrong, and what is not its to fix

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
  no word between ("rename the tab W", "title this pane W"). The reviewed
  reader did that for 264 requests.
- **It knows only the wording of its training rows.** Of the requests that
  reach the model it answers about one in ten on the sets written for this
  change and one in thirty on the job's own held-out set.
- **A file whose manifest matches is taken at its word.** One whose
  weights are nonsense (all NaN) loads, declines every request and says
  nothing.

## What is not in this directory

The blind sets v16 to v19 are kept with the experiment's records, outside
this repository, with their scores, the two reviewers' reports and scripts,
and the probes. v16, v17 and v18 are spent for the bundle: v16 shaped
condition 7, v17 gated the first reader, and v18 was a check set while the
answers to the reviews were written.
