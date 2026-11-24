# Ask suggestions

Experimental and off by default. When Ask plans nothing for a request, a
decision model on this computer may offer a wording the command grammar
reads:

```sh
export KILIX_ASK_SUGGEST=d3:2b
kilix-needle ask --plan "Run Codex in dusk-ferry in a split below"
# the whole request has no supported reading
#   hint: did you mean: start codex in "dusk-ferry" in a split below
```

The setting names a decision model on the local Ollama server
(`OLLAMA_HOST`, loopback only; Ollama 0.40 or newer serves the `d3` family).
Nothing is installed or downloaded for it. If the server or the model is
missing, Ask answers as it does without the setting.

## What a suggestion is

A suggestion is a sentence. It is never run, and it changes neither the
status nor the exit code of the request it answers. Sending it is a new
request, which the grammar reads, plans and asks consent for like any other.
The JSON record carries it as `suggestion` (`request`, `calls`, `model`), and
the hint line shows it.

Only a request that planned nothing gets one. A request that a job planned,
ran or refused does not, and neither does one sent with `--job`, a job label
or `--agent`. The MCP tools do not offer suggestions.

## How it is made

A decision model answers multiple-choice questions and writes nothing, so it
is only ever given choices the five command tools allow:

1. The request's own marks find what must be copied, never reworded: quoted
   text, paths, a name after a word of place (`in dusk-ferry`), a model name.
2. An agent or application can be chosen only if the request names it, and a
   split direction only if the request says it. A request that names neither
   an agent nor an application is refused before any question, and so is one
   that uses a word the tools cannot express (`if`, `unless`, `or`, `not`,
   `later` and the like).
3. The model chooses the kind of request, the number of operations when
   clauses are joined, each operation (or "something these cannot do", which
   refuses the whole request) and each argument from the spans found in
   step 1.
4. A plan that leaves out a path, a quoted text or a direction the request
   names is refused. So is one that repeats an operation.
5. The model is asked whether the plan does exactly what the request asks.
6. The plan is put into the grammar's plain wording. The suggestion exists
   only if every answer was sure enough and the grammar reads that wording
   back as exactly that plan.

A request takes about five questions and 1,000 prompt tokens. At the cost
measured for `d3:2b` on a 6 GB laptop GPU that comes to about 0.4 seconds.

## Measured, 2026-11-24, `d3:2b`

Each set has 100 requests, 72 supported and 28 that must not act, and was
written by an author who saw only the tool schema and the scorer. A wrong
suggestion is a wording that, if accepted, would do something other than
what the request expected.

| Set | Grammar reads | Suggestions | Right | Wrong |
|---|---|---|---|---|
| Blind, first version | 19 of 72 | 38 | 20 | 18 |
| Blind, this version before one fix | 14 of 72 | 47 | 46 | 1 |
| All four sets, this version | 100 of 288 | 151 | 151 | 0 |

The first version dropped directories written without a leading slash and
did not read single or curly quotes; steps 1, 2 and 4 above answer that. The
one wrong suggestion of the second blind run was for "Start claude in
birch-tally provided nobody else has a session open in that directory":
`provided` was not yet among the words of step 2, and the model itself rated
the request a plain command (0.85) and the plan correct (0.92). The last row
is not a blind result: the sets had been seen when those rules were written.

On the second blind set, accepting every suggestion would have answered 60
of the 72 supported requests instead of 14.

## Limits

- The model's own checks do not reliably catch a condition or an added
  unsupported action. The word list and the refusals of step 4 do most of
  that work, and a phrasing they have not met can get through. That is why a
  suggestion is offered and never run.
- A plan the grammar's wording cannot carry gets no suggestion: a model name
  the grammar does not list, or a PDF given by a relative path.
- Requests for the other jobs (panes, files, system) are not suggested.
