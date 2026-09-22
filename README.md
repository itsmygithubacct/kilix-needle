# kilix-needle

Drive Kilix panes and tabs from plain requests, using
[Needle 2](https://huggingface.co/Cactus-Compute/needle2), a 45M-parameter
tool-calling model that runs on the CPU in about 25 MB of RAM and answers a
request in well under a second.

```sh
kilix-needle split right
kilix-needle close tab 2 and go to tab 1
kilix-needle make it a grid
kilix-needle                  # a prompt loop, one request per line
kilix-needle --dry-run close this pane
kilix-needle --yes close tab 2  # no question; for scripts and agents
```

Requires Python 3.10+ on Linux x86-64, run inside Kilix. No Python
dependencies. Status: a local prototype. It is not published and not part of
a release.

## What it can do

| Request | Action | Runs |
| --- | --- | --- |
| split right, open htop in a pane below | open a pane, optionally running a program | straight away; **asks** if it starts a program |
| new tab, open a tab running btop | open a tab | straight away; **asks** if it starts a program |
| go to the left pane, switch to the htop pane | focus a pane | straight away |
| next tab, go to tab 3, go to the logs tab | focus a tab | straight away |
| make it a grid, stack the panes | change the tab's layout (only layouts enabled in that tab) | straight away |
| rename this tab to build | set the tab title | straight away |
| make this pane wider by 10 | resize the pane | straight away |
| close this pane, close tab 2 | close a pane or tab | **asks** |
| run make test in the left pane | type a command and press Enter | **asks** |

"This pane" and "this tab" mean the pane kilix-needle was started from, not
whichever tab is on screen. Side references use Kilix's own neighbour map;
names match a pane's title or foreground program, preferring the current tab.
An ambiguous or missing reference is refused with the candidates listed.

## Why it asks, and what it refuses

Needle's confidence score does not gate anything. On the pinned engine,
"close tab 2 and go to tab 1" produced **two** close calls at confidence
0.99, and "go to the left pane" produced a close at 0.41. So every call
passes checks that do not depend on the model before it becomes an action:

- A close needs a closing verb (close, kill, quit, exit, shut) in the same
  clause as its target, with the pane or tab as the verb's object. "Exit vim in
  the left pane" does not close the pane.
- A tab close needs a clause that says *tab*. A pane action is refused from a
  clause that names only a tab. Tabs are titled after their active pane, so
  "close the htop pane" misread as `close_tab(htop)` could otherwise match a
  real tab.
- Typing a command needs a run/type verb and a target named outside the command
  text. It is refused unless shell integration reports the pane at a prompt,
  so text never lands in an editor or a running program.
- Every free-text value (a program, a name, a command) must appear in the
  request. Values the model invents are refused, not run.

Closing, typing and starting a program wait for `y`. `--yes` answers that
for scripts and agent harnesses. It never overrides a refusal: if any part of
a request is refused, everything else in it waits for a typed `y`, even with
`--yes`, and without a terminal nothing runs. The caller gets exit status 1
and should rephrase.

Everything runs as `kilix @ ...` argv over the instance socket, never through a
shell. Programs are split with `shlex` and executed directly.

## The engine

The engine is the upstream `needle` binary, installed as the `needle2`
asset of the Kilix content catalog:

```sh
kilix models install needle2     # licence screen, typed agreement, download
```

kilix-needle never downloads and never accepts a licence itself. At start it:

1. verifies the packaged catalog against its pinned digest;
2. checks that a licence receipt covers `needle2`;
3. reads the installed engine once into a sealed memfd, checks its size and
   SHA-256 against the catalog manifest, and executes it from that
   descriptor. What runs is what was checked, even if the file is replaced
   afterwards.

The engine serves on 127.0.0.1 at a port chosen for it. Every request first
checks, through `/proc`, that the listening socket belongs to the engine
process, so a local process that took the port first is never trusted. The
engine gets a minimal environment. `NEEDLE_DEBUG` would make it write logits
to `/tmp`.

For development, `--engine FILE` (or `KILIX_NEEDLE_ENGINE`) admits a local
copy instead. It is held to the same pinned digest, so it can only be the
upstream bytes: `Cactus-Compute/needle2` at
`32e9e3a93b205f786929697446ae669cf0a84579`, `linux-x86_64/needle`.

Use the standalone binary, not the `cactus-needle` Python package. The package
sends usage telemetry by default. The binary links only libc and libm, contains
no URLs, and imports no outbound-connection calls. Its only socket use is the
`--serve` listener.

Upstream server quirks the client works around (measured):

- The request parser reads `{"input":"..."}` only in compact form; with a
  space after the colon the request arrives empty and gets no calls.
- `\uXXXX` escapes are not decoded, so requests are sent as raw UTF-8.
- A backslash splits the text, so requests containing one are refused.

## Measured quality

`evaluate.py` scores the whole pipeline (engine calls, then the checks) on
the real engine. On the 40 requests in `evals/commands.jsonl`:

```
unsafe 0/40   exact 26/40   held 4/40   model picked the right tools 28/40
```

The engine is deterministic: three runs produced byte-identical output per request.
Weaknesses of the model on this tool set:

- It never produced `rename_tab`.
- It often drops the program from "open X in a pane".
- It confuses closely worded actions ("make the left pane bigger" → open a pane).

The checks are there so that these mistakes cost a retry, not a closed pane.

```sh
make test                                          # no engine, no Kilix
python3 evaluate.py evals/commands.jsonl --engine FILE
```

The tests never reach the live desktop: `KITTY_LISTEN_ON` is removed, and a
recording fake stands in for `kilix`.

## Files

| File | Role |
| --- | --- |
| `actions.py` | the tool set Needle sees, and the checks between its calls and anything that runs |
| `kilix.py` | resolution against `kilix @ ls`, and the argv that performs each action |
| `engine.py` | the private engine process and its loopback client |
| `asset.py` | admitting the installed engine, or a pinned local copy |
| `needle_cli.py` | the command line and prompt loop |
| `evaluate.py` | end-to-end scoring on the real engine |

Needle 2 is published by Cactus Compute under the Apache License 2.0. The
licence is shown and accepted through `kilix models install`.
