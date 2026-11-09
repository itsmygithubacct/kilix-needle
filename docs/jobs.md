# Jobs

Reference for [kilix-needle](../README.md).

This table predates routing: a request with no job named is now routed (see
[routing](routing.md)), and `kilix-needle panes REQUEST` names the panes job.

Requests are grouped into these jobs:

| Job | CLI | Scope |
| --- | --- | --- |
| `panes` (default) | `kilix-needle REQUEST` | Kilix panes and tabs |
| `apps` | `kilix-needle apps REQUEST` | Apps, games and settings; exact audio, music, voice, text-size and status controls |
| `agents` | `kilix-needle agents REQUEST` | Launch, resume, wait for and message coding-agent sessions |
| `files` | `kilix-needle files REQUEST` | Read-only file search, listings and previews in a named scope |
| `system` | `kilix-needle system REQUEST` | Read resources, processes, services, journal entries and installed package information |
| logs | `kilix-needle logs …` | Read and search local pane logs, with citations |
| `tmux` | `kilix-needle tmux --socket PATH REQUEST` | Exact tmux session and pane control on an explicit socket |
| `pty` | `kilix-needle pty REQUEST` | Persistent pane sessions: list, show, read the tail, archived journals, end one by its exact ID |
| `desktop` (experimental) | `kilix-needle desktop REQUEST` | X11 windows, pointer, keyboard, scrolling and short sequences; preview by default, explicit model bundle and execution consent. See [desktop actions](desktop.md). |
| `windows` (experimental) | `kilix-needle windows REQUEST` | Windows applications through the Kilix desktop SDK: launch, windows, UI Automation controls, keyboard, pointer, scrolling and waits; preview by default, `--host` from Linux. See [Windows actions](windows.md). |

## How each job understands a request

Every job has a **proposer** and **checks**. The proposer suggests calls. The checks
read the whole request themselves and admit a call only if the request says it.
The checks decide; a proposer that invents a call gets it refused.

| Job | Proposer in use | Why |
| --- | --- | --- |
| `panes` | the tuned Needle 2 model qat-6, over five tools that expand to 14 actions | Commands, titles and sizes are free-form, so a model has to propose them |
| `apps` | no model: `apps.propose` offers every call the checks can read, and the exact controls come first | Every argument is from a closed list, so the checks can read all of it and no model can beat them (held-out v4: grammar 111/146, tuned model 93) |
| `agents` | the tuned model agents-qat-1; the checks' clause reading must equal the calls | The request is the consent to launch, so the reading must be exact |
| `files` | the grammar; a trained research model was not promoted | Scopes and filters must be exact; the model refused too rarely (4/40) |
| `system` | the grammar first, then a tuned normalizer that only *proposes* for unfamiliar wording | The grammar is exact but narrow; model proposals never collect on their own |
| logs | a baseline reader, no model | It reads, it doesn't act |
| `tmux` | a whole-request grammar, no model | One explicit operation with exact targets and literal input |
| `pty` | a whole-request grammar, no model | One operation, IDs read as opaque literals, the only mutation is an exact-ID kill with consent |

A default changes only by measurement on a **blind held-out set**. Such a set is
written from a specification by a session that sees no code, no training data and
no other set, and each set gates once. A change also needs an independent review.

Model-backed jobs have separate eval sets and model selections (`jobs.py`). Selecting a tuned
execution model for one job never changes another job's model, and a model gated
for one job can't be selected for another. The `system` job's separate
`system-model` profile does not select or promote an execution model; automatic
collection from model proposals is not enabled.

For jobs using an execution model, model selection compares stock and tuned
models, from any Needle generation. The criteria, in order:
1. no unsafe outcomes;
2. accuracy on the job's newest unconsulted held-out set;
3. speed and memory.

```sh
kilix-needle tune --status                    # every job's model in use
kilix-needle tune --job panes --select RUN    # a gated run, for that job only
kilix-needle tune --job panes --deselect      # that job back to its base model
kilix-needle tune --job agents --select RUN   # a gated coding-session model
```

A selection saved before jobs existed is read as the `panes` selection.
