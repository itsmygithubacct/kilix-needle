# Read pane logs: details

Reference for [kilix-needle](../README.md).

The commands, selectors and providers are in the README's [Read pane logs](../README.md#read-pane-logs) section.

Events cite exact Unicode spans in canonical records, which retain byte and
JSON-pointer origins. The baseline extracts user requests/questions, assistant
answer excerpts, and narrow test/error lines in structured tool output. An
assistant's statement stays an assistant claim. Raw recording lines produce
`transcript` events labelled `unattributed_text`; displayed prompt markers and
test summaries do not establish a speaker or a verified result. These lines
appear under `other` in a brief and retain citations into the recorded snapshot.
`brief` shows the newest bounded
events with citations; it does not reconcile conflicting statements. Search is
lexical and also searches records without event labels. Source lookup retrieves
the cached evidence snapshot, which may differ from a subsequently edited log.

JSON reports coverage gaps and source generations. Exit codes are 0 for a
complete read, 1 for partial coverage or read failure, and 2 for invalid input
or an invalid cursor. Limits bound snapshots and responses. A response too large
for the envelope is rejected explicitly; narrow the query. Events can be paged
with `--since-cursor`, but appends or rewrites invalidate the old snapshot's
cursor. This first implementation rebuilds each snapshot rather than indexing
appends incrementally.

The private local SQLite cache contains excerpts of the source logs. CLI
`--cache-path` can place it in a chosen private directory. The MCP tool
`kilix_logs_read` exposes `events`, `brief`, `search`, and `source` with the same
evidence rules; it has no action-engine or confirmation path. Only the CLI
exposes cache clearing.

System-journal errors use `kilix_system_read`, for example
`errors from the program my-worker in the last 15 minutes`. A logs search
without a source, an unmatched session selector, or a file that cannot be
opened returns this routing hint. The hint reads no journal entries; use
the program tag and time window from the original request in the next call.

Raw recording support is deliberately limited: terminal control sequences,
redraws, binary data, and unfinished lines produce explicit partial coverage.
It does not reconstruct a terminal screen or infer speakers from rendered
labels. Grok/OMP structured adapters and full terminal replay are pending.
Semantic extraction and fine-tuning are experiments; the usable CLI currently
supports only `--mode baseline`. No logs model has qualified for selection.
