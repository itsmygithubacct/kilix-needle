# Ask phrasing

Ask acts only on what the command grammar (`commands_contract.py`) reads. No
model is involved: the same request always gets the same reading, and a
request the grammar does not read is not run (at most it gets a
[suggestion](ask-suggestions.md)).

The grammar has two kinds of reader. The fixed shapes take each operation in
a few set word orders ("start codex in /srv/x to the right", "tell claude to
...", "wait for codex in /srv/x"). Frames, added in November 2026, read the
many other ways people say the same five operations. A wait or a message
may also name its session by Kilix pane: see [ask-pane-targets.md](ask-pane-targets.md).

## What a frame reads

A frame first finds the parts of the request that are copied, never
reworded, and puts a placeholder where each one was:

- quoted text (double, single or typographic quotes);
- a path (`/srv/x`, `~/x`, `./x`, `../x`), or a quoted name after a word of
  place (`in "plum harbor"`), or a bare name after one (`in pewter-anchor`,
  `in the jalapeño-grid directory`, `directory ./x`), and `here`, `in this
  folder`, `the current directory`;
- a split direction or a tab, said in any of the usual ways (`in a split on
  the right`, `right-hand split`, `split underneath`, `an up split`, `a pane
  above`, `put it in a split below`, `new tab`);
- a model (`with model sonnet`, `set the model to opus`);
- the agent (`claude`, `claude code`, `codex`) or the application.

The words that are left must then be one of a closed set of shapes for one
operation:

| Operation | For example |
|---|---|
| start an agent | kick off, fire up, spin up, bring up, begin, put, give me, get ... going, I'd like ... started, would you mind starting, a new Claude session |
| a task | `with the task "..."`, `and give it the task '...'`, `task: "..."` |
| wait | wait till/until/on, pause/block/hold on/hang on/sit tight/stand by until; finishes (up), completes, wraps up, is done, has finished its turn, X's current turn ends; "the one working in ...", "the codex over in ..." |
| message | say/relay/forward/pass `"..."` to X, ping X with `"..."`, let X know: `"..."`, shoot X a message saying `"..."`, write to X: `"..."`, have this sent to X: `"..."`, to X in DIR, send `"..."` |
| open an application | pop open, pull up, fire up, get ... up, "I need the calculator, open it", a list ("the notepad and the calculator") |

Leading and trailing courtesy (hey, just, please, thanks, for me, go ahead
and, could you, would you mind) is allowed around a shape.

Between steps:

- "it" after a wait or a message is that session: `wait for codex in ~/x and
  then tell it "..."`. "there" is the directory of the step before: `wait for
  claude in x-y, then start codex there`.
- Steps may be listed as `first ..., second ..., third ...`, `1) ... 2) ...`
  or `Three things: ...`, in order, and the count must match.
- `open the notepad, then the calculator` shares the verb.

## What a frame refuses

Any word that is not in the shape refuses the whole request: a condition, a
time, a negation, a question that is not a polite request, a second agent, an
unsupported action anywhere in the request. On top of that, every reading a
frame takes part in must also pass the rules suggestions are held to
(`commands_suggest.refusal`), on the whole request.

Also refused:

- a message or task that is not quoted (`let claude know: the build is
  done`). The fixed shapes still read their own unquoted forms (`tell claude
  to ...`, `tell claude: ...`);
- a backslash inside quotes, since an escape is not guessed at;
- one plain word after "in" (`in parallel`); a hyphenated word that says how
  or when, not where (`in read-only`, `in dry-run`, `in mid-december`); a
  name that is a model or an application;
- a path that, with the words after it, names an existing spaced path; the
  fixed shapes decide those;
- `can I ...`, which is as often a question about what is possible;
- a wait or message for an agent that an earlier step has in another
  directory (`start claude here and claude in ../x, then tell the claude ...`);
- "it" after a launch, and "it" or "there" with nothing single to point at.

## What is still not read

- a model other than the four the contract admits (sonnet, opus, gpt-6,
  gpt-6.1-sol): "with model o3" has a reading, which admission refuses;
- a PDF given by a relative path, which the runner cannot open;
- "both in DIR" and "the same directory" across launches, and "there is a
  Claude session; send it ...";
- "bring the calculator to the foreground", "nothing else", and other
  one-off phrasings.

## Measured, 2026-11-24

On the six labelled sets the grammar was developed from (600 requests, 432
supported, 168 must not act; not blind): 138 of 432 supported requests read
before, 383 after, no wrong action, all 168 must-not-act requests refused.
With the d3 walk's suggestions after the grammar, 225 requests that used to
be suggested are now acted on directly; the requests answered either way
went from 542 to 562 of 600.

On a fresh set written for this work and looked at while growing it (100
requests, 72 supported): 30 read before, 64 after, no wrong action, 28 of 28
must-not-act refused. Every miss left is a model name outside the contract or
a relative PDF path. A red-team set of 200 must-not-act requests in these
phrasings is refused whole (one leak, an ambiguous "the claude", was fenced).

On a second fresh set, not looked at and run once with the code frozen (100
requests, 72 supported): 23 read before, 61 after, no wrong action, 28 of 28
must-not-act refused; with the d3 walk after the grammar, 33 requests moved
from suggested to acted on, and 90 answered became 95. Of the 11 supported
requests it misses, 4 name a model outside the contract and 7 use wordings
no frame has yet: a request with no verb ("Codex please, left split, in
x-y"), "would you kindly", "sit and wait until", "reply to", "show me ... in
the PDF viewer", and "that same Claude".

On two further sets, each written after the code was frozen and run once
(100 requests, 72 supported, 28 must not act): 20 read before and 55 after on
the first, 20 and 53 on the second, no wrong action, and all 56 must-not-act
requests refused. Of the 19 supported requests still unread on the second, a
[suggestion](ask-suggestions.md) covers 12, and the other 7 name a model
outside the contract or give a PDF by a relative path.
