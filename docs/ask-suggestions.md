# Ask suggestions

Off by default. When Ask plans nothing for a request, it can offer a wording
the command grammar reads:

```sh
kilix-needle workflows suggest --set d3:2b
kilix-needle ask --plan "Run Codex in dusk-ferry in a split below"
# the whole request has no supported reading
#   hint: did you mean: start codex in "dusk-ferry" in a split below
```

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
`model`), and the hint line shows it. `source` is `walk` (the decision model
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

A decision model answers multiple-choice questions and writes nothing, so it
is given only choices these rules allow: the kind of request, the number of
operations when clauses are joined, each operation (or "something these
cannot do", which refuses the whole request) and each argument from the
spans of step 1. Last it is asked whether the plan does exactly what the
request asks. On its own, its plan is offered only when every answer was
sure enough. A request takes about five questions and 950 prompt tokens: at
the cost measured for `d3:2b` on a 6 GB laptop GPU, about 0.4 seconds.

## Measured, 2026-11-24

Each set has 100 requests, 72 supported and 28 that must not act, written by
an author who saw only the tool schema and the scorer, with labels checked by
a second reader. "Answered" counts a request the grammar planned correctly,
one correctly left alone, or one whose suggestion is the expected plan. A
wrong suggestion is a wording that, if accepted, would do something other
than what the request expected. The models are `d3:2b` and the release LFM2
and Needle3 command models.

Sixth set, blind: the code was frozen before the set was written and it was
run once.

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

Time: the command models take 1.0 to 1.9 seconds per request on a CPU once
loaded, and 8 to 11 seconds in a fresh `kilix-needle ask` process, which
loads the model first. `model` adds nothing to that, because the proposal
has already been made. A decision model adds its questions.

## Limits

- No setting made zero wrong suggestions on both blind sets. That is why a
  suggestion is offered and never run, and why the setting is off unless
  chosen.
- An unsupported second action joined with `and` can get through a decision
  model alone, as above. Agreement with a command model caught it.
- `it` for an agent's session in a directory named earlier is not followed
  by the rules.
- A plan the grammar's wording cannot carry gets no suggestion: a model name
  the grammar does not list, or a PDF given by a relative path.
- Requests for the other jobs (panes, files, system) are not suggested.
