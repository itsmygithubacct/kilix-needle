# Ask: a session named by its pane

Coding agents in Kilix often address each other by pane number: "Tell pane
143: ...", "Send this message to the Codex session in Kilix pane 9: ...".
Ask reads these as a wait or a message whose session is named by its pane,
and runs them only after Kilix shows that the pane holds a coding-agent
session that can take the message. No model is involved in the reading, as
for the rest of the [phrasing](ask-phrasing.md).

## What is read

The target is the word `pane`, a space and the pane number, as `kilix panes
list` shows it, optionally with that pane's broker id (the 16 hex characters
of `KITTY_PTY_BROKER_SESSION`, lowercase). An agent or a session noun may come
first:

    Tell pane 812: the fixture rebuild is done, rerun step 4.
    Send this message to the Codex session in Kilix pane 77: review PLAN-B.md
    Send a message to the existing Codex agent in pane 39: ...
    Message the session in pane 7: ...
    tell claude in pane 2: ...
    Tell the grok session in pane 119: ...
    Tell existing Codex pane 544 broker 0c9e1f2a3b4d5e6f: ...
    tell pane 32 on broker 0c9e1f2a3b4d5e6f: ...
    Send this message to the existing agent in pane 32, broker 0c9e1f2a3b4d5e6f: ...
    ... in pane 32 with broker session 0c9e1f2a3b4d5e6f. Message: ...
    message pane 59 'hello'
    tell the codex session in pane 9 "run the tests"
    wait for the claude session in pane 23 to finish
    wait for pane 31 to finish, then tell it "next batch"

After a colon the message is the rest of the request, as for `tell claude
here: ...`; without one it must be quoted. A message is at most 1024 bytes and
a single line. An unquoted message after a pane is held to the payload rule of
[ask-phrasing.md](ask-phrasing.md#unquoted-payloads):

- a later clause that may be an instruction for Ask refuses the request
  (`tell pane 3: fix it, then message pane 4`, `..., then wait for it`);
- one addressed to the sender stays in the message (`tell pane 3: fix it,
  and tell me when done`);
- a client command or `exit` is never sent.

`agents message pane 59 'hello'` (the job named first, no colon) and `wait
for ... in pane N to finish what it's doing` are read too.

These are not targets, and such a request has no reading: a number not said
as `pane N` (`pane503`, `session 518`, `tab 12`, `window 3`), a pane not named
by number (`the pane on the left`, `my other pane`), two panes for one
message, a broker id that is not lowercase hex of 16 to 64 characters, a
sender claim (`from pane 4, tell pane 9 ...`), and a pane together with a
directory for the same session (`tell codex in pane 5 in ~/x: ...`). A
request that names a session by its pane and also waits for or messages that
agent without a pane (`tell claude in pane 2: "x"; then wait for claude`) has
no reading either, since the second `claude` may be the one in pane 2. An
agent with a pane may be Claude, Codex or Grok; Grok is read only in the
colon and `"..."`-after-the-target shapes above.

## What is checked before anything is typed

When Ask plans the request it lists the panes (`kilix panes list --json`)
and refuses, with the reason, unless:

- the pane exists and is not the pane the request comes from;
- its broker is the one the request names, when it names one;
- it holds a live coding-agent session Kilix reads (a `coding_session` whose
  provider the agents job messages): a shell, an editor, a tmux client or
  any other program is refused, and nothing is sent into tmux;
- that session is the agent the request names, when it names one;
- the session reports an absolute working directory and a readable state:
  `idle`, or `working` for Claude and Grok (others take a message only when
  idle). A session waiting on an approval or a menu takes nothing, since a
  submitted line could answer it.

The plan binds the pane's number and the broker it observed. When you
confirm, the plan is checked again (a replaced pane, another agent or another
directory there means "the planned target or caller changed; review a new plan
and confirm again"), and just before sending the pane is checked once more.
The message is sent with `kilix agent-control send PANE --expect-broker
BROKER --submit`, which refuses a different broker, the caller's own pane and
a leading `/` or `!`.

## The tool schema is unchanged

`pane` and `broker` are grammar-only arguments of `wait_for_agent` and
`message_agent` (`commands_contract.GRAMMAR_ONLY`). `commands-tools.json`,
which the command models are prompted with and whose hash their bundles pin,
does not change, and a command model's proposal is admitted only against it:
a proposal that carries a pane is refused, so LFM2 and Needle3 are admitted
exactly as before.

## Not read here

Resuming a session, a Grok session by directory, and a model outside
Ask's four are read by the agents job, which Ask uses when the command
grammar reads nothing. A reasoning effort, a session id, a tab, or a wait
with a time limit or for an approval are not read by Ask's command grammar.
