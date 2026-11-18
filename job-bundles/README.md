# The job bundles

A bundle of small nets for each kilix-needle job other than panes: files,
system, agents, apps, tmux, pty and logs. Each directory holds a
`bundle.bin` that `panes_bundle.py` reads and its `manifest.json`, in file
format v6. The panes job keeps its own bundle in `../panes-bundle/`, which in
v6 also reads a request to read another pane's screen (`read_pane`).

**Nothing in kilix-needle's routes asks these bundles.** The MCP server and
the command lines are as they were; `KILIX_NEEDLE_BUNDLE` switches only the
panes bundle. The job bundles are measured by the needle-calls bench of
kilix-bench, through `job_routes.py` (below). They were made for experiment
11 of the neuronte-lm research.

## What a file is

Format v6 is v5 with one more field: the head and the manifest name the job
the bundle reads for (`Bundle.job`). A v5 file is read as a panes bundle. The
reader is the same for every job: two members (the same nets trained from
seeds 0 and 1) must propose the same calls, and a first reader answers only a
whole training clause of a tool with other copied texts in it. A second
reader, off unless the manifest switches it on for a kind, may answer a
clause made of runs of three neighbours that stood in training clauses of
that tool (`panes-bundle/README.md` describes both).

Each job's tools are made for reading (`job_schemas.py`): a call the bundle
proposes is written as the job's own call by `job_schemas.to_job`. For
example, `slow_machine` is the resources and the processes by cpu, and a
journal's `since` with `since_n` is "N hours ago". The tools are:

| job | tools |
| --- | --- |
| files | `preview_file`, `search_text`, `find_files`, `list_files` |
| system | `resources`, `processes`, `services`, `journal`, `packages`, `slow_machine`, `boot_failures` |
| agents | `agent`, `wait`, `tell` |
| apps | `launch`, `settings` |
| tmux | `tmux_list`, `tmux_new`, `tmux_read`, `tmux_rename` |
| pty | `pty_list`, `pty_journals`, `pty_status`, `pty_pane`, `pty_observe`, `pty_journal`, `pty_foreground` |
| logs | `logs_events`, `logs_brief`, `logs_search`, `logs_source` |

Two rules of the reader are held differently outside panes:

- **A training clause read as it was labelled is held to its label.** The
  panes reader declines a clause that holds a word naming a value of a
  closed list ("left", "grid") when the call does not hold that value. The
  other jobs' closed lists hold everyday words ("status", "modified", the
  "up" of "spin up"), so a clause that is a whole training clause of its
  tool, read with the values it was labelled with, is answered as labelled.
  The second reader's clauses are still held to the rule.
- **Copied text may hold word pairs never seen in training** for every tool
  with an argument of copied text (the manifest's `free_text`), when copied
  text accounts for each such pair.

And one holds for every job, panes included: a mark just inside a closing
quote is the quoted text's own (`'panic!'` copies `panic!`).

## Where the rows come from

`job_rows.py` makes every row. Its names, paths, sessions and texts are made
up there; none is taken from an eval set, a suite or a log. A template is
wording with slots: `{slot}` takes a value from a pool, `[a|b]` one wording,
`(x)` x or nothing. Each template gives:

- every wording it has, with every value of each closed slot and both quote
  marks around a quoted copy (the frame the first reader matches keeps those
  words), or 4,000 of them drawn at random when there are more;
- copied values and numbers drawn at random, each wording at least once and
  more often when the template has fewer wordings than `--job-per-template`;
- each row also without its final mark, with a full stop, and, for a
  question, with a question mark.

A row is kept only if its job takes it as labelled (`job_rows.admitted`):

- files, system, agents, apps: the job's grammar reads the request, and
  reads it as the row is labelled. On these jobs an answer of the bundle is
  admitted only when the grammar reads the request the same way (below), so
  a row the grammar does not read would teach an answer that is never used;
- tmux, pty: the call reads back through the job's grammar sentence
  (`tmux_job.reads_back`, `pty_job.reads_back`);
- logs: the logs tool's argument checks take it (`logs_words.check`);
- a read of a pane: `pane_reads.check_args` takes its arguments.

The panes bundle adds the read rows to experiment 10's rows (the tuner's
generated rows, the spent eval sets and the agent-style rows). The four
agent-style templates whose wording a read now answers ("show pane N",
"read the output of pane N", "show the screen text of pane N", "show the
last lines of pane N") are left out (`panes_bundle_agent.READ_TEMPLATES`).

Training is as for panes (`panes_bundle_train.py --job JOB --job-rows SEED`):
nothing is written unless the file, read as a request meets it and with the
job's checks, answers every training row it answers as that row is labelled,
with the second reader off and with it on for every kind.

## How an answer is checked

`job_routes.py` runs a request through a job's route in one of three arms,
with every stage of today's route that only refuses kept in each:

- **A** is today's route: the exact readers or the job's grammar, then the
  job's tuned model where one is loaded (panes, agents).
- **B** is the job's bundle, then the model. Where a job has no model, what
  the bundle does not answer is refused.
- **C** is the grammar, then the bundle, then the model.

A bundle's answer goes through the job's own checks, as the grammar's does:

| job | the check |
| --- | --- |
| files | `files_job.interpret`, and it must equal the grammar's reading |
| system | `system_job.interpret`, and it must equal the grammar's reading |
| agents | `agents.interpret` |
| apps | `apps.interpret` |
| tmux, pty | the call reads back through the job's grammar sentence |
| logs | `logs_words.check` |
| panes | `needle_cli.checks_agree` and `actions.interpret`; a list is the exact list reader's; a read is checked against the desktop (`pane_reads.check`) |

So on files and system the bundle answers at most what the grammar reads,
and as the grammar reads it.

## What is not in this directory

The bench's suite, its desktop and its scorer are in kilix-bench
(`needle_calls.py`). How the bundles were gated, and what they got wrong, is
in the neuronte-lm research records, not here.
