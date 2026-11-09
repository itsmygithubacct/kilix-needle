# State and request history

Reference for [kilix-needle](../README.md).

## State

Everything kilix-needle keeps lives in one directory,
`$GPU_TERMINAL_HOME/kilix-apps/kilix-needle` (by default
`~/.local/gpu_terminal/kilix-apps/kilix-needle`):

| Path | What |
|---|---|
| `model.json` | each job's selected tuned model |
| `tuning/` | tuning runs |
| `history/` | request history |
| `system-normalizer/` | the system normalizer profile and its verified objects |
| `logs/index.sqlite3` | the logs reader's index (a rebuildable cache) |

Older versions kept the normalizer profile and the logs index in
`~/.local/share/kilix-needle`. The first use moves each one into the state
directory; nothing is copied or left behind. `~/.config/kilix-needle/dirs.json`
is configuration you edit, not state, and stays where it is.

## Request history

Each panes, apps or agents request answered by kilix-needle's own engines,
and each default system request through the CLI or MCP, is added to a local
history. Explicit system suggestion previews are not recorded:

```
~/.local/gpu_terminal/kilix-apps/kilix-needle/history/requests.jsonl
```

`GPU_TERMINAL_HOME` moves it. The external-inference bridge runs calls it is
handed, not requests, and records nothing.

Each line is one request. It holds:
- the request exactly as given, plus the checked form when it differs;
- the job, and whether a person or an agent sent it;
- what proposed the calls (`grammar`, or the model in use) and the raw calls;
- what the checks admitted or refused and why, and each outcome;
- dry run and advance yes, and how long it took.

This is the material for improving the grammar and models: the misses, the
refusals and what people actually ask. Anything taken from it for training
or for a grammar change must still be kept apart from the eval sets.

The history never leaves the machine. Requests can hold private text, such as a
message to a coding session. So:
- **Files.** Every directory and file below `GPU_TERMINAL_HOME` is opened without
  following links, and must belong to you. The directory is set to `0700` and
  its files, archives included, to `0600`.
- **Items.** Only an item's kind, arguments and outcome are kept, with the
  reason only when the checks refused it or confirmation held it.
  - Nothing that names resolved panes or their titles: no display summaries, and
    no reason for an unresolved or failed action.
  - No runtime error text, no command lines, and no pane or broker identities.
  - No system observations: system entries keep query plans and outcomes only.
  - The note is kilix-needle's own wording, or the checks' reading of the request.
- **Size.** Each entry is at most 64 KB, with long text cut and marked. The file
  rotates at 8 MB, and eight files are kept.
- **Turning it off.** Set `KILIX_NEEDLE_HISTORY=0` to record nothing.

Recording never changes what a request does or returns:
- An entry is dropped if another writer holds the history for more than a
  quarter of a second, or if a path is unsafe.
- A write that can't complete is rolled back. A partial last line, from a
  failed rollback or a crash, is cut away before the next write or rotation, so
  the files stay whole lines.
- If no line boundary lies near the end, for example in a damaged file, nothing
  is added, and a warning is given.
- A failure is reported at most once on stderr.
