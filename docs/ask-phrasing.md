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

- a message or task that is not quoted and has no marker (`let claude know
  the build is done`). With a marker it is read as in
  [Unquoted payloads](#unquoted-payloads). The fixed shapes still read their
  own unquoted forms (`tell claude to ...`, `tell claude: ...`);
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

## Unquoted payloads

Agents that use Ask send long requests: a head that names the session or the
launch, a colon, and then a message or task that is not quoted and has
sentences of its own.

    Tell the Codex agent in /srv/oast-galliard/run-4: fix the flaky test; if it
    still fails, skip it and say why.

    Start codex in /srv/oast-galliard/v2.1 using model gpt-6 in a new tab. Task:
    Read NOTES.md; reply with OK.

Where neither the fixed shapes nor a frame reads such a request, it is read
under this rule:

- The payload starts after the first colon that is followed by a space and
  is outside quotes. The head before it may quote only with double quotes.
- The head must be one frame shape with a marker in front of the colon. For a
  launch: `Task:`, `Its task:`, `Instructions:`, `with the task:`, `give it
  the following task:`, `with these instructions:`, or a bare colon after the
  launch. For a message: `tell`, `message`, `ping`, `notify`, `instruct` or
  `write to` X, `send X this message:`, `send the following to X:`, `relay
  this to X:`, `let X know:`, or `then tell it:` after a wait. The head is
  read like any frame. A condition, a time, a negation, a second session, a
  pane, a broker or a model outside the four refuses it, and so does any word
  outside the shape. A directory may end its own sentence before the marker
  (`in /srv/x. Task:`).
- The payload runs to the end of the request and is copied byte for byte. It
  is data: its "if", "then", "do not" and "run" are the receiving session's
  (`tell claude in /srv/x: start codex if the tests pass` is one message to
  Claude). The rules suggestions are held to do not read it either.
- It is refused when it may be a second instruction for Ask. That is when one
  of its later clauses (after a comma, a semicolon, a full stop, "and",
  "then" and the like) has a verb Ask has and names something Ask acts on (an
  agent, an application, a session, a tab, a split, a pane): `...: fix it,
  then tell codex in /srv/y: rebase`, `...: fix it. Then open the
  calculator`. It is also refused when a later clause waits on "it", or when
  a later clause starts with an action word the fixed shapes stop at (open,
  start, tell, wait, delete, close and the like). "run", "do not", "if" and
  "unless" at the start of a later clause stay data unless the clause names
  something Ask acts on.
- It is refused when it is no message at all. That is a client command
  (starting with `/`, `!`, `#`, `@` or `-`), a word that ends the session
  (`exit`, `quit`, `bye`), more than 1,024 bytes, or empty. A launch task
  with a question mark outside quotes is also refused, since it may be a
  question for Ask (`start codex in ../x: would that pick the parent
  folder?`).
- `agents:` or `apps:` before a request names the job. The rest must read as
  that job's actions only.

Some of these rules apply to every reading, including the fixed shapes'.
They follow the payload rule of the agents job (`agents.py`):

- the client-command and exit-word rules;
- for a payload that is not wholly quoted, the later-clause rule (a verb Ask
  has and something Ask acts on, or a wait on "it") and the question-mark
  rule for launches;
- "nothing else" outside the payloads must end the request.

Before 0.2.4 the fixed shapes sent `tell codex here: /compact` as a message
and read `tell codex in /srv/x: fix it, and pop open the calculator` as one
message.

## What is still not read

- a model other than the four the contract admits (sonnet, opus, gpt-6,
  gpt-6.1-sol): "with model o3" has a reading, which admission refuses;
- a PDF given by a relative path, which the runner cannot open;
- "both in DIR" and "the same directory" across launches, and "there is a
  Claude session; send it ...";
- "bring the calculator to the foreground" and other one-off phrasings;
- an unquoted payload without a colon (`message codex only. Fix the test`),
  or after "to" or "and" in a frame (`tell codex in /srv/x to fix it` is the
  fixed shapes' and reads; `ask codex in /srv/x, when it is free, to ...`
  does not);
- the same wait twice in one request, `both in new tabs`, `~/` alone as a
  directory, and `do not give it a task`;
- what the five tools cannot say: a pane or tab number, a broker or session
  id, another agent, resuming a session, a reasoning effort, or a model
  outside the four.

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

## Measured, unquoted payloads, 2026-11-24

On the 1,200 requests of the earlier sets, every reading is the same as
before: the payload rule only reads what nothing read before, and the fences
it adds to every reading change none of them.

On a set written for this work in the style agents really send (long heads,
unquoted payloads of several sentences; 100 requests, 66 supported) and
looked at while growing it: 31 read before, 63 after, no wrong action, all
34 must-not-act requests refused.

On a second set like it, not looked at and run once with the code frozen:
28 read before, 55 after, no wrong action, all 34 must-not-act refused. Of
the 11 supported requests it misses, 4 launch with words no frame has ("no
initial task", "with the working directory set to", "; the default model is
fine and it should open in a new tab", "in a split below, in DIR, using
sonnet. Task:"), 2 use "relay to X:" and "reply to X:", 2 are waits said in
new ways ("wait on X until it finishes the turn it is on"), 1 starts two
agents with "there", and 1 has a later sentence in its message that starts
like an instruction, which the fixed shapes' rule refuses.

A red-team set of 150 must-not-act requests in this style found 7 wrong
actions, all of them in the fixed shapes before this change: 6 client
commands sent as messages and 1 question sent as a launch task. All 150 are
now refused.
