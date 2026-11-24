# Ask suggestions

Off by default. When Ask plans nothing for a request, it can offer a wording
the command grammar reads:

```sh
kilix-needle workflows suggest --set d3:2b
kilix-needle ask --plan "Start Claude and Codex, both in ~/work/fig-lantern"
# the whole request has no supported reading
#   hint: did you mean: start claude in "~/work/fig-lantern"; then start codex in "~/work/fig-lantern"
```

The grammar itself reads most of the wordings a suggestion used to be needed
for ("Run Codex in dusk-ferry in a split below" is now planned directly); see
[Ask phrasing](ask-phrasing.md). A suggestion is now for what is left.

## The setting

`kilix-needle workflows suggest` prints the setting; `--set` saves it.
`KILIX_ASK_SUGGEST` overrides the saved setting for one command.

| Setting | A wording comes from |
|---|---|
| `off` | nothing (the default) |
| `model` | the selected command model's own proposal for the request (LFM2, LFM3 or Needle3). With the grammar selected there is none. |
| a decision model, such as `d3:2b` | that model's choices. With a command model selected as well, a wording is offered only when both make the same plan. |

A decision model is one on the local Ollama server (`OLLAMA_HOST`, loopback
only; Ollama 0.40 or newer serves the `d3` family). Nothing is installed or
downloaded for it, and `workflows suggest` says whether the server lists it.
If the server or the model is missing, nothing is offered.

## What a suggestion is

A suggestion is a sentence. It is never run, and it changes neither the
status nor the exit code of the request it answers. Sending it is a new
request, which the grammar reads, plans and asks consent for like any other.
The JSON record carries it as `suggestion` (`request`, `calls`, `source`,
`model`), and the hint line shows it. `source` is `built` (the decision model
alone), `proposal` (the command model alone) or `agreed` (both).

Only a request that planned nothing gets one. A request that a job planned,
ran or refused does not, and neither does one sent with `--job`, a job label
or `--agent`. The MCP tools do not offer suggestions.

## Why the command model's proposal is not simply run

Ask admits a command only when the grammar itself reads the request. A
selected command model therefore never changes what Ask does: on 600
requests the answers with LFM2 or Needle3 selected were the same as with the
grammar alone, request for request. Without that admission the models
answer more and act wrongly far too often (LFM2: 89 wrong actions on those
600 requests, Needle3: 131). A suggestion puts the proposal to use without
running it.

## How a plan is held to the request

Every plan, whichever model made it, must pass the same rules, which read
only the request's own words:

1. The request's marks find what must be copied, never reworded: quoted
   text, paths, a name after a word of place (`in dusk-ferry`), a model name.
   An argument that is not one of those, word for word, refuses the plan.
2. An agent or application must be named by the request, and a split
   direction must be said by it. A request that names neither an agent nor an
   application gets no suggestion, and no model is asked.
3. A request that uses a word the tools cannot express gets none either:
   a condition (`if`, `unless`, `once`, `provided`), a time (`later`,
   `every`, `Monday`, `tonight`), a choice or a negation (`or`, `not`), a
   question (`how`, `what`), or an ending (`quit`, `stop`, `close`). So does a
   request that stops short (`start codex in`).
4. Nothing the request names may be left out: a path, a quoted text, a
   direction, an agent, an application, or the operation its verb asks for
   (`wait`, `tell`, `start`). A directory named twice, or named and then
   called `there` or `both`, is used twice. Nothing is used more often than
   the request states it, and no operation is repeated.
5. The plan is put into the grammar's plain wording. The suggestion exists
   only if the grammar reads that wording back as exactly that plan.

## How a decision model builds the sentence

A decision model answers multiple-choice questions and writes nothing, so it
is never asked for words. Before any question, every plan the rules above
allow is listed, using only the spans of step 1, and each is kept only if the
grammar reads its plain wording back as that plan. Of those, only the plans
with the fewest operations that still use everything the request names are
kept, and operations that each name a different agent or application keep
the order the request names them in. The wordings of what is left form a tree
of pieces: a fixed phrase (`start codex`, ` in a split below`,
`; then wait for claude`) or a copied span (` in "dusk-ferry"`,
`: "run the tests"`). Every complete path through the tree is a sentence the
grammar reads. A request with no such plan gets no question at all.

More rules shape the list, all from the request's own words:

- Some requests get no plan at all: a question asked with `is`, `do` or
  `has` first, or one that ends in a question mark after its text; a
  hypothetical, a report of what was done, or a time other than now (`at
  6pm`, `in an hour`, `after lunch`) or a condition (`whenever`, `in case`,
  `errors out`); an action or application no tool has (`then delete the
  build folder`, `as root`, `take a screenshot`, `set the theme`, the
  calendar, a clause that opens with a verb none of the tools has), another
  agent (`aider`, `the reviewer`), or many sessions at once; a split without
  its direction, or a pane without its number (`the pane on the left`); a
  sender (`from pane 4, tell pane 9 ...`); `it` or `the codex` after two
  such sessions have been named; a labelled field (`Model: x.`) or step
  (`Step 2: wait for it`), a task with no text, or a request that stops
  after `:` or `second,`.
- A detail no plan's wording can say means no plan unless the plan's own
  sentence says it: a tab, window or session by number or id, `resume`, an effort
  level, a title, a flag (`--yolo`), another host, a file left in the
  request's own words (`draft.txt`).
- A model the request names, as one (`model o3`), after `with`, or bare
  (`opus model`), must be in the plan, so a model the grammar cannot carry
  means no suggestion rather than a plan without it.
- Each operation verb asks for an operation of its kind (`start`, `wait`,
  `tell`), and so do `it` after a join (`then wait for it`), `for both`, and
  `here` next to a directory (`start codex here and in ~/x` is two). The
  plan's calls must come in the order the request states them: `start codex,
  wait for it, then tell it "x"` is never `start; then tell; then wait`.
- A plan uses every directory and text the request copies, and names one
  agent's directory alike in all its waits and messages.
- Where some plans fit the request's words more closely, only those are
  kept: `it` and a directory said in front (`In ~/x, start codex and
  claude`) are the same session and the same directory; a text is a task
  when the request says task, a message when it says tell; and each call
  takes the directory, text and direction said in its own clause, a
  directory said elsewhere only when `the same folder`, `there` or `both`
  carries it there (`claude and codex in ./x` puts both in ./x).
- A split is offered only when the request says split, pane, side or a
  direction (`a new tab` says none), and `the same directory` names the
  directory again.
- A session named by its Kilix pane (`nudge pane 41 with "rebase now"`)
  is waited for or messaged with the pane and broker copied from
  `targets()`, never with a directory. The agent goes beside the pane only
  when it is named next to it (`the codex in pane 9`), never because it was
  named for another call, and an agent waited for or messaged by its pane is
  never also addressed without one (`tell claude in pane 2 ..., then wait for
  claude` may mean another claude). A pane goes only with the calls of its
  own clause, or of a later one that says `it`.
- A message or wait for a session this plan started in a directory names
  that directory (`start claude in ./x ..., tell claude "..."`), unless the
  clause says `here`.
- A wait needs words that ask for one (`wait`, `hold on`, `let it finish`,
  `done`): `make pane 311 wider` gets no plan. `wait for them` and `tell
  both` are one operation on every agent the plan has, and `claude and codex`
  go with one verb, so `open claude and codex and wait` never starts one and
  waits for the other.

These rules narrow the tree, often to a single sentence, so fewer questions
are asked; where a rule leaves no plan, nothing is asked and nothing is
suggested.

The model is then asked:

1. what kind of request this is (a plain command, a question, a condition, ...);
2. at each point where the tree branches, which continuation keeps the
   command true to the request, each shown as the whole sentence so far plus
   the piece, with "complete" where a plan ends;
3. which of the complete sentences does exactly what the request asks, as
   wholes, up to 24 in one question;
4. if 2 and 3 chose the same sentence, whether that plan does exactly what the
   request asks.

Its plan is offered only when 2 and 3 agree and every answer was sure
enough. Where the tree has a single sentence, only 1 and 4 are asked. A
request takes about 1.4 questions and 460 prompt tokens on average: at the
cost measured for `d3:2b` on a 6 GB laptop GPU, about 0.16 seconds.

The other plan source, the walk (`suggest(..., plan=walk)`), is described
next.

## How the walk plans

The walk asks for the plan one operation at a time instead of building the
grammar's sentence. It works in this order.

1. **Words the tools cannot express refuse before any question**
   (`walk_refuses()`). These rules belong to the walk only. They read the
   request's own words, with quoted texts and paths left out, and come on top
   of the rules above:
   - a session addressed by tab, window, number, id or title, or a pane not
     given as `pane N` (`tell claude in tab 4`, `the pane on the left`, `in
     #2207`, `codex session 0a1b...`); for a pane, see below;
   - a reasoning effort, or a level after a model (`model gpt-6 high`);
   - resuming or restarting a session;
   - a command-line flag (`--yolo`);
   - another agent or program (`grok`, `the supervisor`, `the other claude`);
   - an SSH host;
   - `as root`, `yourself`, or memory or a GPU;
   - many copies (`five claude sessions`);
   - a time other than now (`at 6pm`, `in an hour`, `after lunch`);
   - a negated operation (`don't start codex`; `don't do anything else, just
     wait` is not one);
   - a question (`is the codex in ~/x done?`; `could you ...?` is a request);
   - a hypothetical, an example, a quote or past events (`imagine`, `the
     tutorial says`, `yesterday I started`);
   - a model the grammar cannot carry (`model claude-opus-9`, `on haiku`);
   - `it` after both agents are named;
   - a split without a side;
   - a request that ends on `with the task`;
   - a place the copier cannot copy (`in ~/`, `the folder pine-cone`);
   - `Directory: /x` given as a field;
   - text after a colon that nothing before the colon calls a message or a
     task;
   - a clause whose first word is a verb none of the five tools does (`push
     the branch`, `email the team`, `work out 18%`, `run git push`), also
     after `but`; a file named for an application (`open notes.txt in the
     notepad`).
2. **The kind of request**, as for the built sentence.
3. **Each operation in turn.** The first question offers the operations the
   named agents, applications and panes allow, and `other` (something none of
   them can do). Later questions see the operations already planned and may
   also answer `nothing more`. That answer is offered only once the plan
   leaves nothing the request names unused. There are at most as many
   operations as the request has clauses (joined by `and`, `then`, `but`,
   commas, semicolons, colons, dashes, sentences or list numbers), one more
   for a `both` that points back at two agents, and four in all. A request
   that begins with a launch verb and an agent (`open codex in ...`) begins
   with that launch. `other` refuses the whole request; the same call again,
   once the plan is whole, ends it.
4. **Each argument the words leave open.** An argument the words already fix
   is taken without a question:
   - the one agent named, for a wait or a message;
   - the applications, in the order the request names them;
   - for the last operation the clauses lead one to expect: the only path, the
     one direction not yet used, the only model, the only text left;
   - the message, when one text is left.
   Other choices are asked, among the request's own spans only, with `none`
   where leaving the argument out is a fair answer. A text the words call a
   task (`with the task '...'`, `task: ...`) is never a message, and one they
   call a message (`message it "..."`, `tell it: ...`) is never a task.
   `right here` and `right now` name no side.
5. **The rules.** A repeated operation, anything named left out, a named
   model left out, a pane named twice and used once, or a `both` without a
   second operation on each agent ends the walk. So does any reason
   `refusal()` gives, before the final question is asked.
6. **The plan against its near misses.** The plan is ranked against the
   variants of it that the rules also allow (one operation fewer, an argument
   dropped, another named path, side or pane, the other agent) and `none of
   these`. It must win with odds of at least `WALK_CHECK` (0.5). A plan with
   no such variant gets the yes/no question instead, and needs `yes` with at
   least `WALK_VERIFY` (0.7).

`walk_signals()` reads a walk's (gate, least sure choice, final ranking), and
`suggest(..., plan=walk)` holds them to (`GATE`, `STEP`, `WALK_CHECK`).

### A session named by its pane

The grammar reads the plain pane shapes itself
([ask-pane-targets.md](ask-pane-targets.md)). For a pane request it does not
read (an unusual verb or order, a payload introduced oddly, a wait and a
message mixed), the walk plans as follows:

- **The session question.** A wait or a message asks "which session" among
  each pane the request names (`targets()`) and each agent it names without
  one. With one choice, it is taken without a question.
- **Copied, never written.** The pane and its broker are copied from
  `targets()`.
- **The agent beside a pane.** The agent is the one named right before the
  pane (`the Codex agent in pane 9`). Otherwise it is left out.
- **No directory.** A pane call is never given a directory, so a request that
  names one beside the pane has nothing to put it in and gets no plan.
- **No plan at all** for:
  - a sender (`from pane 4, tell pane 9 ...`);
  - a broker `targets()` cannot copy (not lowercase hex);
  - one pane with two brokers;
  - `the same pane` when two are named;
  - an agent named both with a pane and without one;
  - a Grok session, since `refusal()` holds the agent to Claude or Codex.

  Two calls of the same kind on two panes are refused by `refusal()` as
  repeated, so they get no plan either.

### The walk on requests the grammar does not read (not blind)

On the merged grammar (pane targets and unquoted payloads read), with
`d3:2b`. Questions and tokens are averages per request that reaches a plan
source. The rows for "before" are the walk as it was merged; the built
sentence is the merged one, for reference.

| Set (unread, reaching a plan source, supported) | Plan source | Right | Wrong | Questions | Tokens |
|---|---|---|---|---|---|
| hard pane requests and neighbours (108, 90, 68) | walk before | 6 | 1 | 1.72 | 410 |
| | built sentence | 6 | 2 | 0.26 | 61 |
| | walk now | 40 | 0 | 3.39 | 793 |
| hard requests, set 1 (139, 107, 92) | walk before | 62 | 0 | 3.96 | 858 |
| | built sentence | 58 | 3 | 1.59 | 345 |
| | walk now | 63 | 0 | 3.85 | 833 |
| hard requests, set 2 (140, 113, 93) | walk before | 46 | 4 | 3.46 | 737 |
| | built sentence | 51 | 3 | 1.56 | 344 |
| | walk now | 52 | 0 | 3.40 | 734 |
| authored residue (577, 253, 69) | walk before | 40 | 0 | 1.91 | 391 |
| | built sentence | 41 | 0 | 0.37 | 81 |
| | walk now | 40 | 0 | 1.64 | 335 |
| real requests (42 reaching a plan source, 2 supported) | walk before | 1 | 0 | 0.43 | 93 |
| | built sentence | 1 | 0 | 0.05 | 12 |
| | walk now | 2 | 0 | 0.43 | 98 |

These sets shaped the rules above, so the numbers are not blind, except
two:

- **Hard set 2, "walk before".** It was run once on the walk as merged, before
  any change; its four wrong suggestions led to the task-or-message rule, the
  `right here` rule and the colon rule's `mid-task`.
- **A fresh set of 80 pane and other hard requests** (63 unread, 54 reaching
  a plan source, 37 supported), written and labelled by two readers who agreed
  on every label. It was run once with the code frozen:
  - the walk: 18 right, 0 wrong, 44 of 63 answered, 2.8 questions;
  - the built sentence: 6 right, 0 wrong, 32 answered.

## Measured, 2026-11-24

### The grammar and the built sentence together, blind

Two sets of 100 requests (72 supported, 28 that must not act), each written
by an author who saw only the tool schema and the scorer, with labels checked
by a second reader, after the code was frozen; each was run once. "Before" is
the grammar as it was, alone and with `d3:2b`'s walk; "now" is the grammar
with frames, alone and with `d3:2b` building the sentence. For the seventh
set the two halves were still separate and their recorded runs were put
together; the eighth ran this code as it is.

| Set | Before: grammar | with `d3:2b` | Now: grammar | with `d3:2b` | Suggestions | Wrong | Questions per request |
|---|---|---|---|---|---|---|---|
| seventh | 48 | 91 | 83 | 92 | 9 | 0 | 0.57 |
| eighth | 48 | 86 | 81 | 93 | 12 | 0 | 0.51 |

No wrong action and no wrong suggestion on either set, and all 56
must-not-act requests left alone. The walk asked 3.9 and 3.6 questions per
request on the same sets. On the eighth set the present code answers 7
requests the earlier one did not and loses none (exact McNemar, p = 0.016).
All 7 supported requests it still misses there name a model the contract does
not admit or give a PDF by a relative path.

The sections below were measured before the grammar grew and describe the
suggestions alone.

### The built sentence against the walk

Not blind: the six sets below shaped the rules of both, and the walk's
numbers on them are with today's rules. The seventh, `fresh1`, was written
for this comparison by a fresh author (and its labels checked by a second
reader) after the built sentence's rules were fixed, and was run once; it is
development data, not a qualification. Per 100 requests, the decision model
alone with the grammar selected:

| Set | Walk: answered | wrong | questions | Built: answered | wrong | questions |
|---|---|---|---|---|---|---|
| v1-v6 pooled (600) | 542 | 1 | 3.44 | 547 | 1 | 1.42 |
| fresh1 (100) | 88 | 0 | 2.67 | 92 | 0 | 1.22 |

The one wrong suggestion in each is the same request ("Open the calculator
and work out 18% of 2,350", below). Prompt tokens per request: 665 for the
walk and 460 built on v1-v6, 508 and 254 on fresh1. With a command model
selected, agreement answered 495 (LFM2) and 483 (Needle3) of the 600 with the
built plan, against 494 and 480 with the walk, with the same wrong
suggestions (0 and 1).

### The built sentence on what the grammar leaves unread

Not blind. Every set below was written by an author who saw only the tool
schema, the scorer and a brief, and each was cut to the requests the grammar
does not read. The rules above were written while looking at the
development sets, so their numbers are a ceiling. The unseen set was written
by a fresh author, its labels checked by a second reader, and it was run
once after the code was frozen; no rule was changed after it. Suggestions
from the decision model alone (`d3:2b`), the grammar selected:

| Set | Requests (supported) | Before: right | wrong | questions | Now: right | wrong | questions |
|---|---|---|---|---|---|---|---|
| development, pooled | 1,063 (242) | 108 | 93 | 1.39 | 141 | 0 | 0.31 |
| unseen | 197 (117) | 75 | 15 | 2.62 | 90 | 1 | 1.23 |
| unseen, the walk | 197 (117) | 71 | 13 | 5.72 | | | |

"Before" is the built sentence as it was. On the unseen set it now leaves
all 80 must-not-act requests alone (before, 9 got a suggestion), answers 16
requests it did not and loses 1. Prompt tokens per request fell from 852 to
283. The one wrong suggestion starts two agents in a directory and then
tells each one something: the messages leave the directory out, and the
labels name it.

### The walk, blind

Each set has 100 requests, 72 supported and 28 that must not act, written by
an author who saw only the tool schema and the scorer, with labels checked by
a second reader. "Answered" counts a request the grammar planned correctly,
one correctly left alone, or one whose suggestion is the expected plan. A
wrong suggestion is a wording that, if accepted, would do something other
than what the request expected. The models are `d3:2b` and the release LFM2
and Needle3 command models.

These are the walk's results, before the built sentence replaced it. Sixth
set, blind: the code was frozen before the set was written and it was run
once.

| Setting | Selected model | Answered | Suggestions | Right | Wrong |
|---|---|---|---|---|---|
| `off` | any | 49 | 0 | | |
| `model` | LFM2 | 87 | 40 | 38 | 2 |
| `model` | Needle3 | 85 | 37 | 36 | 1 |
| `d3:2b` | grammar | 94 | 47 | 46 | 1 |
| `d3:2b` | LFM2 | 87 | 38 | 38 | 0 |
| `d3:2b` | Needle3 | 85 | 36 | 36 | 0 |

The grammar made no wrong action. Each suggesting setting answers more than
`off` (exact McNemar, p < 1e-10). `d3:2b` alone answers more than either
LFM2 setting (8 requests to 1, p = 0.04 before correction for the several
comparisons made).

The wrong suggestions came from three requests:

- `d3:2b` alone, for "Open the calculator and work out 18% of 2,350":
  `open the calculator`. The second half is not something Ask can do, so the
  whole request should have been left alone.
- LFM2, for "Wait for codex in ~/ml/pecan-semaphore and then tell it "write
  up the results"": the message without the directory. `it` is the session in
  that directory.
- LFM2 and Needle3, for "Tell Claude in ~/work/harpoon-daisy "pause after the
  next commit", then wait for it to finish": the wait without the directory.

The fifth set was blind for an earlier version of the rules, which made more
wrong suggestions (`d3:2b` alone: 2 of 43; LFM2's proposal: 3 of 35; both
agreeing: 2 of 31). Steps 3 and 4 above were extended after it, and it also
showed a fault of the grammar itself: a message wholly inside single quotes
was sent with the quote marks. That is fixed.

On the five earlier sets, which the rules have seen, the present version
makes 191 suggestions with `d3:2b` alone and 143 where LFM2 agrees, none of
them wrong, and 153 from LFM2's proposal alone, none wrong. Those are not
blind results.

Time: the built sentence's list of plans takes a few milliseconds to make,
and up to about 3 seconds for a request of four operations with long quoted
texts. The command models take 1.0 to 1.9 seconds per request on a CPU once
loaded, and 8 to 11 seconds in a fresh `kilix-needle ask` process, which
loads the model first. `model` adds nothing to that, because the proposal
has already been made. A decision model adds its questions.

## Limits

- No setting made zero wrong suggestions on both the fifth and the sixth
  blind set. The two later sets had none, but with the grammar reading more
  they hold only 21 suggestions between them, too few to change the rule: a
  suggestion is offered and never run, and the setting is off unless chosen.
- An unsupported second action joined with `and` can get through a decision
  model alone, as above. Agreement with a command model caught it.
- A message for an agent started earlier in the same request leaves its
  directory out when the request does not repeat it (`start claude in ~/x
  ..., and tell claude "y"`), and that can be the wrong plan.
- The rules read words, not grammar: a word they list (`close`, `memory`,
  `terminal`) rules a request out wherever it stands outside quotes, and a
  wording they do not list gets through to the model.
- A plan with the same operation twice on one agent, such as two messages
  for it or two waits, is refused, so a request that asks for that gets no
  suggestion.
- A request of more than four operations gets no suggestion.
- A plan the grammar's wording cannot carry gets no suggestion: a model name
  the grammar does not list, or a PDF given by a relative path.
- Requests for the other jobs (panes, files, system) are not suggested.
