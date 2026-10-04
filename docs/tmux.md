# Tmux session control

The `tmux` job reads one complete request through a deterministic grammar.
It loads no model and delegates to the shared `kilix_tmux` controller. Each
CLI request requires `--socket` with an absolute private socket path. Each
MCP call requires the same path in `socket`. Neither implementation discovery
nor the ambient `TMUX` environment supplies a socket.

```sh
kilix-needle tmux --socket /tmp/private.sock 'list sessions'
kilix-needle tmux --socket /tmp/private.sock 'new session build in /tmp/project'
kilix-needle tmux --socket /tmp/private.sock 'read build last 80 lines'
kilix-needle tmux --socket /tmp/private.sock --yes 'send "make test;" to build'
kilix-needle tmux --socket /tmp/private.sock --yes 'press Enter in build'
kilix-needle tmux --socket /tmp/private.sock --yes 'type "/status" in build'
kilix-needle tmux --socket /tmp/private.sock 'rename session build to review'
kilix-needle tmux --socket /tmp/private.sock --dry-run 'close session review'
kilix-needle tmux --socket /tmp/private.sock --yes 'close session review'
```

`new` starts tmux's configured shell. An optional directory must be absolute
and exist. A new session can start a server at the exact requested socket.
An absent server returns an empty session list; other operations report
absence. Session names contain ASCII letters, digits, underscores and
hyphens, with a letter, digit or underscore first and at most 128 characters.

| Request form | Meaning |
| --- | --- |
| `list sessions` | List exact session IDs, names and pane IDs |
| `new session NAME [in "/absolute/directory"]` | Start a named session |
| `read TARGET [last N lines]` | Capture bounded pane text |
| `send "TEXT" to TARGET` | Insert literal text without Enter |
| `type "TEXT" in TARGET` | Insert literal text, then send a separate Enter |
| `press Enter C-c in TARGET` | Send an ordered list of allowed keys |
| `rename session TARGET to NAME` | Rename an exact session |
| `close session TARGET` | Close an exact session |

Targets use exact case-sensitive session names, stable session IDs such as
`$0`, stable pane IDs such as `%3`, or numeric `NAME:WINDOW.PANE` addresses.
Rename and close accept only session names or session IDs. Input and read
refuse a session containing multiple panes; choose a pane explicitly.
Prefixes, patterns, missing targets and extra conditions or actions refuse.

Text requires matching double quotes, single quotes or backticks. Their
contents are preserved, including spaces, tabs, Unicode, quotes of another
style and trailing semicolons. Choose a delimiter absent from the text.
The grammar performs no expansion, unescaping or normalization. If the
target is a shell, that shell interprets submitted text. Empty text, missing
literals and control characters other than tab refuse. Input is bounded to
65,536 characters. Reads default to 80 lines and accept 1–2,000 lines.
Keys accept 1–16 names from this list:

```text
Enter Tab Escape BSpace Space Up Down Left Right Home End PageUp PageDown
Delete C-c C-d C-u C-a C-e C-l
```

For text containing every quote style, pass a structured literal request.
The CLI accepts `--request-json JSON`, or `--request-json -` to read JSON from
stdin. MCP accepts the same object in `request`:

```json
{"operation":"send","target":"%3","text":"literal ' \" ` $HOME ;"}
```

Use `send` to leave the text pending, `type` to add Enter. JSON decoding is
the only decoding step; the text has the same size and control-character
limits as the grammar. Confirmation, exact target resolution and dry-run
behavior remain the same. Do not shell-interpolate literal text into a
request. To submit an already pending line, use `press Enter in %3` without
retyping the line.

If the user specifically requests native tmux, `send-keys -l` still subjects
arguments to tmux's command parser and can consume a trailing semicolon.
Load the literal bytes from stdin using `load-buffer -b UNIQUE -`, then
`paste-buffer -d -r -b UNIQUE -t PANE` on the same explicit `-S SOCKET`.
Choose a fresh buffer name and remove only that buffer if the paste fails.
Do not append a newline or Enter unless submission was requested.
For a JSON-encoded payload, decode the original encoded string once with a
JSON parser. Its escape backslashes are not literal input bytes. Before
pasting, compare `save-buffer -b UNIQUE -` with the decoded payload; if the
bytes differ, remove only the fresh buffer and report the mismatch.

Send, type, key and close require interactive confirmation or `--yes`.
`--agent` never prompts. Refusal cannot be overridden with confirmation.
`--dry-run` validates and resolves the request without mutation; a read plan
does not capture text. JSON output (`--json`) contains the controller's
`kilix.tmux/v1` envelope in `result`. Displayed pane text is JSON escaped.
`submitted` reports input submission only. Type and key report
`completion: "unknown"`; verify command effects separately. A partial Enter
failure retains the controller's error details about already-inserted text.

MCP tools `kilix_tmux_plan` and `kilix_tmux_act` take `request` and `socket`.
The act tool also accepts boolean `confirm_risky`, default false, for input
and close. Plan performs no mutations. Both return the same controller
envelope and status as the CLI. Reconnect existing MCP sessions after
updating the checkout to discover the new tools.

## Controller discovery

`KILIX_TMUX_CLI` selects an executable and takes precedence over
`KILIX_TMUX_MODULE_ROOT`, which selects an absolute directory containing the
`kilix_tmux` package. An invalid explicit selection fails without fallback.
With neither selector, Needle first looks for `kilix-tmux` on `PATH`, then
resolves the `kilix` command to its source tree and imports
`config/kilix_tmux` in an isolated Python worker. `KILIX_NEEDLE_KILIX` selects
an alternative Kilix command, as it does for other Needle jobs. Symlinks to
the installed command are resolved. This discovery reads the package without
executing Kilix or running installation or configuration setup.

A Kilix version bundling the controller, or an explicit implementation
selector, is required. Discovery chooses implementation only; every request
still supplies its socket. The transport uses argv and structured JSON,
preserves controller error codes, and enforces a 30-second wall limit.

This job has no tuned model or model-selection entry. Product tests cover
the contract and private server effects; no measured model score is claimed.
