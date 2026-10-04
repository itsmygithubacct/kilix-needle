# Files: scoped local search

`kilix-needle files` finds filenames, searches literal UTF-8 text, lists large or
recent files, and previews an explicitly named text file. It reads local files;
it does not launch applications, execute file contents, move, delete, index,
cache, upload or modify them. It works outside Kilix without an installed model.

```sh
kilix-needle files 'find pdf files in Downloads modified yesterday'
kilix-needle files 'find files named "needle" in research'
kilix-needle files 'find text "PipeWire" in projects'
kilix-needle files --limit 10 'show largest files in Downloads'
kilix-needle files 'show recent files in this project'
kilix-needle files 'preview "README.md" in here'
kilix-needle files --dry-run --json 'preview "notes/day one.txt" in "/tmp/work files"'
```

With no request, an interactive terminal gets a `files>` prompt. `exit`, `quit`,
`:q`, or EOF ends it. A noninteractive caller must supply a request. `--json`
returns one structured record. Exit 0 means a complete query or valid dry-run;
1 means refused, partial, or failed; 2 means command/setup failure; 130 means
interrupted. A normal result limit is reported separately from scan completeness.

## Supported language

The entire request must match a supported form. Quoted values are literal;
there is no shell expansion, regular expression, or semantic/vector search.

| Form | Meaning |
|---|---|
| `find files named "NAME" in SCOPE` | Case-insensitive filename substring |
| `find files starting with "PREFIX" in SCOPE` | Case-insensitive filename prefix |
| `find EXT files in SCOPE` | Case-insensitive final extension, e.g. `pdf`, `py` |
| Any filename form above followed by `modified today`, `modified yesterday`, or `modified last N days` | Modification-time filter; N is 1–99 |
| `find files in SCOPE modified today` | Time filter without a name/type restriction |
| `find text "TEXT" in SCOPE` | Case-sensitive literal content search |
| `show largest files in SCOPE` | Largest first |
| `show recent files in SCOPE` | Most recently modified first |
| `preview "RELATIVE/PATH" in SCOPE` | Bounded UTF-8 text preview |

`search for files named`, `show EXT files`, `search "TEXT"`, `list largest`,
`show large`, and `list newest` are also supported. Ambiguous recollections,
compound commands, negations, deletion requests and unsupported filters are
refused as a whole, rather than silently dropping part of the request.

`whose name starts with` and a bare trailing-star form such as `ledger-*`
select a prefix. A quoted name in the `named` form stays a literal substring,
including any asterisk. Prefix queries do not match text in the middle of a name.

A trailing `(visible regular files only)` or `(skip hidden entries and symlinks)`
is accepted because these restrictions already apply. They can also be combined
as `(visible regular files only, skip hidden entries and symlinks)`; `hidden`
and `hidden files` are accepted in place of `hidden entries`. Other filters and
additional instructions must still match the supported language.

Every request states its scope: `here`/`this project`/`the current directory`,
`Downloads`, `Documents`, `research`, `projects`, or an explicit absolute path,
`~/path`, or `./path`. Aliases use the user's home; `projects` means
`~/gpu_terminal`. `here` uses the CLI's working directory, not a guessed pane.
Use quotes inside the request for paths containing spaces. A missing alias
folder is reported as an error; there is no fallback to searching the home.

Dates use the collector's local timezone. Yesterday is the previous local
calendar day; last N days is a rolling N × 24-hour interval ending at collection time (exclusive).
Calendar-day boundaries account for local daylight-saving transitions. Modification time
is **not** download or creation time. PDFs and office files can be found by
filename/extension; their internal text is not extracted. Name and text search
results are newest first. One excerpt (the first occurrence) is returned per
matching text file, with a 1-based line number. This is direct evidence, not a
model-written summary of what a file means.

## Coverage and bounds

The Linux collector opens each scope/path component with `O_NOFOLLOW`, anchors
reads to directory descriptors, and reads regular files only. It does not follow
symlinks, including symlinked scopes. Preview paths cannot be absolute, hidden,
or contain parent/dot components. Hard links remain regular directory entries;
this is not a filesystem sandbox against a malicious filesystem owner.

Search skips hidden entries and `node_modules`, `__pycache__`, `venv`, and `env`
directories. These exclusions are reported. Preview can name an otherwise
excluded nonhidden directory explicitly. Search is capped at 10,000 visited
entries, depth 12 below the root, three seconds inside the collector, and five
seconds for the worker process. The worker is killed on timeout. A blocked
kernel I/O operation may still depend on the operating system returning control.

Content search examines files up to 1 MiB, reading at most 16 MiB total per
request. Larger or growing files and exhausted budgets are reported as incomplete
coverage. Binary and non-UTF-8 files are excluded from text searches. Preview
reads at most 16 KiB plus one byte to determine truncation. Results default to 20,
with `--limit` from 1 to 100; excerpts are bounded. No results never implies an
exhaustive absence when coverage is partial. Ranking is within scanned coverage.

Structured output includes the resolved scope, observation timestamp, display
paths, lossless `path_bytes_hex` identifiers, sizes, modification times, coverage counters, skips, errors, limits and
truncation. Paths can change after observation. Permission errors remain errors;
there is no privilege escalation. File contents and names are untrusted data.
Non-UTF-8 filename bytes use backslash escapes in display paths; decode
`path_bytes_hex` to recover the actual filename bytes. Human output escapes terminal controls; JSON escapes controls and Unicode for
transport. Reading may update filesystem access times according to mount policy.

## MCP

- `kilix_files_plan`: parse and validate without searching or reading target files.
- `kilix_files_read`: execute one bounded read-only query.

Both take `request`, optional `limit`, and optional absolute `cwd`. MCP `here`
uses supplied `cwd`, or the MCP server's directory when omitted. Callers should
supply their working directory when it differs. Results use `structuredContent`;
`isError` is true for refused, partial or failed queries. These tools use the
baseline and never load a model or inherit another job's selected weights.

## Model and evaluation status

The default is a deterministic grammar baseline (`--baseline` is explicit).
There is **no trained or qualified files model**, training pack, held-out gate,
or automatic tuning/promotion for this job. An explicit `files --engine FILE`
allows experimental Needle proposals with the four-tool files schema. Every
proposal must exactly match the independently parsed query: the model cannot
change scope, target, filters or add another read. Model errors produce no query.
This guard also limits language coverage to the baseline grammar; this version
does not claim a model expands that coverage.

```sh
python3 evaluate.py evals/files/dev.jsonl --job files --baseline
python3 evaluate.py evals/files/dev.jsonl --job files --engine /path/to/engine
```

The 24 development cases are authored implementation regressions, not a blind
benchmark. Evaluation scores queries only and never runs collectors. Runtime
errors cannot earn no-action credit. Raw exactness, admitted exactness,
actionable positives, errors and unexpected admitted reads are distinct metrics.
Collector correctness is tested independently with temporary filesystem fixtures.
A future training experiment needs independent realistic requests, argument
annotation, disjoint development/final sets, and a measurable improvement over
this baseline before a model can be qualified. No GPU rental is needed to use
this implementation.
