# Read pane logs: details

Reference for [kilix-needle](../README.md).

The commands are in the README's [Read pane logs](../README.md#read-pane-logs) section.

`list` refreshes only the inventory. `index` streams the complete history into
a private SQLite cache; there is no 32 MiB file window. A first index can take
time for a large collection. Each file commits atomically, so an interrupted
index can be restarted: unchanged files reuse their cached records. Appends
verify the previously indexed bytes and parse only the new rows. Rewrites,
replacement, truncation, and parser upgrades rebuild the affected source.
`index --file PATH --provider CLIENT` or `index --session ID` updates one source;
`--rebuild` forces it to be parsed again.

CLI search refreshes discovery and indexing before querying. `--cached` queries
the existing index immediately without reading source files. Searches use
SQLite FTS5 with BM25 ranking: higher returned scores rank first. Query words
are literal tokens, not FTS operators. All words must match by default;
`--match any` accepts any word, and `--rank recent` sorts matching sessions by
recency. `--provider` narrows the client; `--file` plus `--provider`, or
`--session`, narrows a search to one recording. Unicode tokenization ignores
Latin diacritics. Match snippets are excerpts; `show` retrieves the canonical
record with exact character pagination and byte/JSON-pointer origins.

Inventory and search responses use `kilix.logs.library/v1`, with `sessions` or
`matches`, `offset`, and `next_offset`. Continue with `--offset`; `--limit` is
1–100. Pages may be shortened to fit the response envelope. Concurrent index
updates can change search ordering between pages. JSON reports indexing actions,
unindexed sources and errors instead of treating a failed source as no matches.
`show` returns `next_start` for a long record. These response limits do not limit
the history stored or searched. Python callers can use
`needle_logs.library_cli.read({...})` with the same operation names and arguments.

Pane numbers and titles belong to one Kilix instance. The reader first asks
the caller's pane inventory. When a shared Codex app-server cannot identify
the calling pane, the reader can use a single local Kilix listener and its
live window inventory. If several instances are listening, or the inherited
route conflicts with the listener, select the intended window explicitly.
Run `printenv KITTY_PID` in a fresh shell in that window. For example, if it
prints `1234`, use `kilix-needle logs brief --instance 1234 --session 18`.
The equivalent `kilix_logs_read` arguments are
`{"operation":"brief","provider":"grok","session":"18","instance":1234}`.
An explicit instance scopes the selector to its live panes, including after
a broker reattachment. A supplied `provider` must match the selected recording.
The Linux reader checks the socket peer's PID and user
and rechecks the instance and recording identity before returning pane logs.

`events`, `brief` and `source` retain the `kilix.logs.read/v1` evidence API.
Events cite exact Unicode spans in records. Tool output retains its tool role,
an assistant's statement stays an assistant claim, and reasoning, binary
payloads and metadata are excluded categorically. `brief` returns recent cited
events from the full index; `events --since-cursor` pages earlier history too.
Responses stay within 32 KiB of UTF-8 JSON. An oversized event keeps its ID and
exact evidence spans; long quotes are replaced by `quote_omitted: true`, an
exact `excerpt`, and `excerpt_end`. A present `quote` always covers the full
cited span. Event pagination advances past these references normally.

`source` returns complete cached records when they fit. Otherwise each record
contains `chunk`, `chunk_start`, `chunk_end`, `total_chars` and `evidence_index`;
offsets count Unicode code points, not UTF-8 bytes. Append chunks for each
evidence index and continue with `--since-cursor` set to `next_cursor` until
`has_more` is false. The human-readable source view also prints its continuation
cursor. Source pages may omit excerpts to leave room for the source text.
Normal-sized event and source responses retain their existing fields.

Appends, rewrites or parser changes invalidate cursors after the index refreshes.
`source` returns cached evidence, which can differ from a subsequently edited
log. The MCP `kilix_logs_read` tool
exposes these per-source operations. Larger files, compressed sources and
previously indexed recordings use the persistent index there as well. The
low-level `needle_logs.sources.read_source` remains a bounded preview helper.

Raw pane recordings with terminal controls use the pinned
`kilix-transcript-clean` screen replayer. Replayed lines span the recorded
snapshot and carry `origin.replay`; they do not have exact single-line byte
origins. Their role stays unknown: rendered labels never establish a speaker.
The Session Logs TUI (`kilix-session-log`, also in `kilix-session-center`) lists
all three transcript tiers by recency, uses readable names, and filters by name,
directory, command, ID or tier. Its preview retains a bounded tail in memory.

The cache contains excerpts of private logs and requires private ownership and
permissions. `--cache-path` selects another private location on a disk with room
for the index and temporary archive expansion. Cache clearing removes inventory
and search data too. Exit codes are 0 for a complete operation, 1 for partial
coverage or read failure, and 2 for invalid input or an invalid cursor.
System-journal errors remain available through `kilix_system_read`; the log
library does not collect the system journal.
