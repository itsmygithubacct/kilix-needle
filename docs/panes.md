# The panes job

Reference for [kilix-needle](../README.md).

## What it can do

| Request | Action | Runs |
| --- | --- | --- |
| split right, open htop in a pane below | open a pane, optionally running a program | straight away; **asks** if it starts a program |
| new tab, open a tab running btop | open a tab | straight away; **asks** if it starts a program |
| go to the left pane, next pane | focus a pane | straight away |
| next tab, go to tab 3, go to the logs tab | focus a tab | straight away |
| make it a grid, stack the panes | change the tab's layout (only layouts enabled in that tab) | straight away |
| rename this tab to build | set the tab title | straight away |
| make this pane wider by 10 | resize the pane | straight away |
| rename tab id 10 to build | rename that stable tab ID | verified receipt |
| make pane 200 wider by 5 cells | resize that stable pane ID | verified receipt |
| close this pane, close tab 2 | close a pane or tab | **asks** |
| run make test in the left pane | type a command and press Enter | **asks** |

"This pane" and "this tab" mean the pane kilix-needle was started from, not
whichever tab is on screen. If a harness strips `KITTY_WINDOW_ID`, the pane
is found through the process ancestry. With `--under-overlay` (the hotkey),
it means the pane the overlay covers. Side references use Kilix's neighbour
map, which reports window groups. Names match a pane's title or foreground
program exactly, without regard to case, or else a whole word of exactly one
title. A pane in the current tab is chosen over one elsewhere with the same
name ("build" here, "Build" in another tab), but a yes given in advance does
not cover that choice. For pane navigation, an explicit "in this tab" or
"in the current tab" restricts the lookup to that tab before matching names or
IDs. A target found only elsewhere is unresolved; outside duplicates do not
affect a scoped lookup. This applies to "jump to", "take me to", "switch over
to", "go to", "focus" and "switch to". Unscoped pane IDs and names retain their
cross-tab lookup, and "go to tab 2" still selects a tab by position. Pane qualifiers
such as "in tab 2" are not supported and must not be treated as unscoped.
Tab titles have no such preference: two tabs with the
same name are refused. The pane and tab kilix-needle was started from are never matched by a
whole word: while it runs, Kilix titles them with the request itself. An
ambiguous or missing reference is refused with the candidates named. "Next"
and "previous" tab or pane wrap round for a go-to, never for a close or a
typed command. A tab titled "next", "previous" or "last", or a pane titled or
running "next", "previous" or a side ("left", "above"), makes that word
ambiguous, and it is refused.

## Why it asks, and what it refuses

Needle's confidence score gates nothing. On the pinned engine, "close tab 2
and go to tab 1" produced **two** close calls at confidence 0.99. So every
call passes checks that do not depend on the model. Each check below was
added because a measured model output got past the one before:

- A close needs a closing verb (close, kill, quit, exit, shut) in the same
  clause as its target.
- The pane or tab must be bound to a pane or tab word between the verb and
  the first contrastive or subordinating word ("but", "so", "instead of",
  "next to", "(" …). So "exit vim in the left pane" and "kill top in the left
  pane" close nothing, and "close tab 1 but keep tab 2" never closes tab 2.
  These are rules about wording: they narrow what the model may do, but they
  do not prove what a sentence means. The safeguard that does not depend on
  wording is the question below.
- A tab close needs a clause that says *tab*. A pane action is never taken
  from a clause that names only a tab.
- A typed command must be the whole text after the run verb ("make test", not
  "test"). Its pane must be named outside the command. It is typed only into a
  pane at a shell prompt.
- Every program, name and command must appear in the request. A target made
  only of filler words is no reference.

Closing, typing and starting a program wait for `y`, and the question names
the pane or tab it resolved to. `--yes` (and MCP's `confirm_risky`) answers in
advance, for scripts and agents, but only for a **plain instruction**. What
that means, exactly:

- Each clause is a canonical wording of one admitted action, built from that
  action's own values: "close tab 2", "close the second tab", "close the build
  pane", "close the pane on the left", "run make in the build pane", "split
  right and run htop", "go to tab 1". A safe action beside a risky one is held
  to its canonical wording too; a safe action with none makes the request not
  plain.
- Politeness only at the edges: "please", "pls", "kindly", "ok", "okay",
  "alright", "hey", "just", "go ahead and", "can/could/would/will you" before;
  "please", "pls", "thanks", "thank you", "now", "right now", "for me" after.
  A request that ends in a question mark (anywhere in its closing punctuation)
  is plain only after "can/could/would/will you".
- After any clause that moves focus (a go-to, or opening a pane or tab), no
  risky action is plain.
- A command or program is plain unquoted only as one word ("make", "htop"),
  or when every later word is plainly a shell argument: a flag, a path (`/`,
  `./`, `~/`, or letters and a slash), a file name with an extension,
  `VAR=value`, a plain integer or an operator ("ls -la ~/src", "make -C
  ./src", "tail -n 50 app.log"). Times, dates and plain words are not
  arguments ("17:00", "9/25", "make test"). A typed command with more is plain
  only in quotes (`run "make test" in the build pane`); a program for a new
  pane or tab cannot be quoted, so a multi-word one waits for a person. English
  after a program may be a condition or a delay that would be typed along with
  it, and two reviews found every list of such words short.
- "top" and "bottom" are never plain: Kilix resolves "above" and "below" to the
  adjacent pane, not the outermost one.
- Tab numbers are ASCII digits or number words.

Beyond the wording, a yes given in advance never covers a target found only
by a whole word of a title, a pane chosen over another tab's pane of the same
name, nor a close of the pane or tab the request was made from.

Anything else waits for a person, who sees the target. Every action in one
request is resolved against the one desktop the request was made on and
performed by id, so "close tab 2 and close tab 3" closes the tabs that were
2 and 3. Just before a command is typed, its pane is read again: if an earlier
command in the request started a program there, nothing is typed. With the
usual emacs-style line editing, anything half-typed on a one-line prompt is
first cut to the shell's kill ring (Ctrl-E, Ctrl-U; Ctrl-Y brings it back), so
it is never run with the command appended; see Known issues for where that
does not hold. Once any
action is refused, cannot be resolved or fails, nothing later in the request
runs on a yes given in advance. Nine reviews shaped this; their records are
in the release's research notes.

A pre-answered yes never overrides a refusal: if any part of a request is
refused, everything else in it waits for a typed `y`, and without a terminal
nothing runs. With `--agent` (and in the MCP server), the caller's own pane and
tab are never closed, and when the caller's pane cannot be identified, nothing
risky runs at all.

Everything runs as `kilix @ ...` argv over the instance socket, never through a
shell. Programs are split with `shlex` and executed directly.
