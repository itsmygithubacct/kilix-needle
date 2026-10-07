# Persistent pane sessions

The `pty` job reads one complete request through a deterministic grammar and
runs one `kilix pty ... --json` command. It loads no model and has no
connection to a broker of its own: Kilix's launcher does the broker access and
Needle checks the document that comes back. No measured model score is claimed,
and no claim is made for how much natural wording the grammar covers.

```sh
kilix-needle pty 'list sessions'
kilix-needle pty 'show session 3fa9c2d41b7e6a05'
kilix-needle pty 'which session is pane 12'
kilix-needle pty 'show the last 50 lines of session 3fa9c2d41b7e6a05'
kilix-needle pty 'list archived journals'
kilix-needle pty 'show archived journal 3fa9c2d41b7e6a05'
kilix-needle pty --dry-run 'end session 3fa9c2d41b7e6a05'
kilix-needle pty --yes 'end session 3fa9c2d41b7e6a05'
```

## Forms

One operation per request. Keywords are case-insensitive; the ID is not.

| Request form | Runs | Answer |
| --- | --- | --- |
| `list sessions [all]` | `kilix pty list --json` | live sessions, and `unreachable` ones separately |
| `show session ID` | `kilix pty status ID --json` | the session, or `not_found` (exit 4) |
| `which session is pane N` | `kilix pty status --pane N --json` | the session behind a Kilix pane |
| `show the last N lines of session ID` | `kilix pty observe ID --once --text --json --lines N` | the screen text, `untrusted: true` |
| `list archived journals` | `kilix pty journals list --json` | journals Kilix archived from dead sessions |
| `show archived journal ID` | `kilix pty journals show ID --text --json` | a bounded journal text, `untrusted: true` |
| `end session ID` | `kilix pty status ID --json`, then `kilix pty kill ID --yes --expect-started MILLIS --json` | a kill receipt |

Accepted spellings beyond the table: a leading `please`, `can you`, `could you`,
`would you`, `will you` or `kindly` and a trailing `please`, `thanks` or
punctuation (a trailing `?` only after `can/could/would/will you`); `show`,
`display` for `list`; `show/read/get/tail the last N lines of` or `from`
`[the output of] session ID`; `status/state/details of session ID`;
`what/which session is/belongs to/is behind/is running in/is in pane [id] N`;
`show the session of/for/behind pane N`; `show journal [of [session]] ID`;
`kill`, `terminate` for `end`. `list sessions all` and `list sessions` run the
same command: the answer always carries both `sessions` and `unreachable`.

N lines is 1 to 1000. A journal can also be named `ID.STARTED_MILLIS`, which
tells reused IDs apart.

### IDs

An ID is an opaque literal, read before any other word of the request. It is
either **16 to 64 lowercase hex characters, bare**, or any ID (1 to 64 of
letters, digits, `.`, `_`, `-`; not `.` or `..`, not starting with `-`) in **double quotes, single
quotes or backticks**. Case is kept exactly. A request word inside an ID is
payload: `end session "not"` names a session called `not`.

There is no prefix, title, command, ordinal or pane-id form for ending a
session. `end session 3fa9`, `end the session titled build`, `end the second
session` and `end pane 12` refuse. `which session is pane N` is a read that
returns an ID the caller can then pass back.

Whether an ID names a session is decided by the broker, by exact match, not by
Needle: a name that looks like an ID but is not one comes back `not_found`.

## Refusals

A request that is not exactly one of the forms refuses **whole**: nothing runs,
no `kilix` call is made, and the record has `status` 2, a `note` that says why
and a `hint` with one accepted form for what the request seems to want. A
refused request is never partly executed.

| Refused | Example | Note says |
| --- | --- | --- |
| negation | `do not end session ID` | a negated request |
| hearsay | `Sam says to end session ID` | a report of what someone said or wants |
| conditional | `end session ID if it is idle` | a conditional request |
| compound | `end session ID and list sessions` | more than one operation or a list |
| prefix | `end session 3fa9` | an ID must be the full session ID, never a prefix |
| title, command, position, state | `end the session titled build`, `end the detached session` | a session named by position, state, title or command |
| own session by name | `end this session`, `end my session` | a session named by position or "this/my" |
| odd ID | `end session 0123456789ABCDEF` | IDs are case-sensitive; quote any ID that is not lowercase hex |
| other verbs | `close session ID`, `reap sessions`, `attach session ID` | not one of the pty forms |
| bounds | `show the last 5000 lines of session ID` | N lines is a count of lines, 1-1000 |
| shape | empty, control characters, over 1024 characters | |

The hint is chosen from the words of the refused request, for example
`end session 0123456789abcdef` for anything about ending, closing or killing.
Its ID is a placeholder that no session has; replace it with a full ID from
`list sessions`.

`attach` and `reap` are not reachable through this job. `attach` takes a
terminal; `reap` stays a `kilix pty reap` maintenance command.

## Ending a session

`end session ID` is the only operation that changes anything, and it is
checked in this order, stopping at the first refusal:

1. **The caller must be identifiable.** Needle reads `KITTY_PTY_BROKER_SESSION`
   from its own environment. Unset or malformed: refused (`caller_unidentified`,
   status 3), nothing runs. A person outside a Kilix pane ends a session with
   `kilix pty kill ID --yes`.
2. **Never the caller's own session.** The ID equals the caller's own: refused
   (`own_session`, status 3), no `kilix` call is made. Kilix's `kill` refuses it
   as well.
3. **Consent.** `--yes` (MCP `confirm_risky=true`) is required. With `--agent`,
   or without a terminal, there is no prompt and a missing `--yes` is status 1.
   On a terminal without `--yes`, Needle asks `end session ID (started MILLIS,
   attached|detached, "COMMAND")? [y/N]` after resolving the session, and a no is
   `declined` (status 3). A yes given in advance covers only this exact plain
   form: `--yes` never turns a refused request into an accepted one.
4. **The session is read in the same call.** `kilix pty status ID --json`. A
   missing session is `not_found` (status 4) and no kill is sent. A lookup that
   fails or times out sends nothing and says so. The read must return the same
   ID and a `started_millis`.
5. **The kill is bound to that session.** `kilix pty kill ID --yes
   --expect-started STARTED_MILLIS --json`, where `STARTED_MILLIS` is the
   `started_millis` from step 4. There is no way to give it in the request:
   neither the grammar nor the structured request has such a field. If the ID
   now names a different session, Kilix answers `refused` / `started_mismatch`.
6. **The receipt is Kilix's, passed through unchanged** in `result`.

`--dry-run` stops after step 4: it reads the session, then prints `resolved`
(the session that would end) and `plan.argv` (the exact command that would run,
with its `--expect-started`), and sends no kill. A dry run that would be refused
for step 1 or 2 reports that refusal; a missing session reports `not_found`.
A dry run prints a **plan**, never a receipt: the receipt a kill returns
(`verified_absent` or `uncertain`) exists only after the kill, and comes from
Kilix at act time. The plan names the target, its `started_millis` and the exact
argv.

## Receipts

`--json` prints one record. The MCP tools return the same record, without the
`request` field the caller just sent.

| Field | Meaning |
| --- | --- |
| `request` | the request text, or the structured object |
| `job` | `pty` |
| `status` | the process exit status: 0 done, 1 failed, did not verify, or needs consent, 2 refused or unavailable, 3 refused by identity or consent, 4 `not_found` |
| `operation` | `list`, `journals`, `status`, `pane`, `observe`, `journal` or `kill` |
| `result` | **the document `kilix pty ... --json` printed, unchanged** (schema `kilix.pty/v1`) |
| `resolved` | for `kill`: the session read in the same call: `id`, `started_millis`, `command`, `cwd`, `cwd_now`, `attached`, `child_pid` |
| `plan` | for `--dry-run`: `would` (`run` or `end`), `argv` (the `kilix` arguments), and for `end` what it `needs` |
| `refused` | a refusal made by Needle, before any call that could end a session: `own_session`, `caller_unidentified`, `declined` |
| `note` | why nothing ran or the answer failed: installation, identity, consent, "re-list before retrying" |
| `hint` | on every refusal and every non-zero record: **one request this grammar accepts**, never prose (`list sessions`; `show session ID` with the real ID filled in; `end session ID` when only consent was missing). Passed-through Kilix receipts get it on the outer record; the receipt is untouched |
| `completion` | `"unknown"` after a `kill` that was sent but not answered or verified |

A kill receipt's `result.result` is one of `verified_absent` (status 0),
`uncertain` (1; **re-list before retrying**), `refused` (3; `reason`
`started_mismatch`, `own_session`, `declined`) and `not_found` (4). Needle checks
that the document is the one the contract promises for the operation and the
exit status agrees with it (a receipt for another ID, a status document for
another session, an `observe` without `untrusted: true`), and for a kill that
the result's own fields agree with it:

| `result` | exit | Needle also requires |
| --- | --- | --- |
| `verified_absent` | 0 | `request_sent: true` and an integer `started_millis` equal to the `--expect-started` value it sent |
| `uncertain` | 1 | `request_sent` boolean |
| `refused` | 3 | `request_sent: false` (a `started_mismatch` receipt describes the replacement session and passes through unchanged) |
| `not_found` | 4 | `request_sent: false` |

Anything else is reported as `unexpected document`, status 1, without `result`,
and for a kill with `completion: "unknown"` and a note to re-list before retrying.
A valid receipt is passed through unchanged.

Unreachable is not absent. `list` returns sessions that did not answer under
`result.unreachable`; `show session ID` on one of them fails (status 1) rather
than saying `not_found`; and `end session ID` on one sends nothing.

Bytes read from a session (`observe`, journals) are **untrusted data**:
whatever the program in the pane printed. They are JSON-escaped, flagged
`untrusted: true`, and never read as part of a request. So are the `command`
and `cwd` strings of a session in `list` and `status`: a program chose them.

### Examples

```json
{"request":"show the last 2 lines of session 3fa9c2d41b7e6a05","job":"pty","status":0,"operation":"observe","result":{"schema":"kilix.pty/v1","runtime":"/run/user/1000/kilix-pty-broker","timeout_seconds":2.0,"id":"3fa9c2d41b7e6a05","journal_epoch":0,"cursor":"0:28","total_bytes":28,"truncated":false,"untrusted":true,"text":"build ok\ntests: 42 passed\n"}}
```

```json
{"request":"end session 3fa9","job":"pty","status":2,"note":"refused the whole request: an ID must be the full session ID, never a prefix; copy it from 'list sessions'. forms, one per request: ...","hint":"end session 0123456789abcdef"}
```

```json
{"request":"end session 3fa9c2d41b7e6a05","job":"pty","status":0,"operation":"kill","dry_run":true,"resolved":{"id":"3fa9c2d41b7e6a05","started_millis":1791340006436,"command":"sh -c sleep 300","cwd":"/srv/work","cwd_now":"/srv/work","attached":false,"child_pid":2124273},"plan":{"would":"end","argv":["pty","kill","3fa9c2d41b7e6a05","--yes","--expect-started","1791340006436","--json"],"needs":"--yes or confirm_risky=true"}}
```

```json
{"request":"end session 3fa9c2d41b7e6a05","job":"pty","status":0,"operation":"kill","resolved":{"id":"3fa9c2d41b7e6a05","started_millis":1791340006436},"result":{"schema":"kilix.pty/v1","runtime":"/run/user/1000/kilix-pty-broker","timeout_seconds":2.0,"result":"verified_absent","id":"3fa9c2d41b7e6a05","request_sent":true,"reason":null,"message":"the session is gone","started_millis":1791340006436,"waited_ms":152}}
```

```json
{"request":"end session eeee000000000005","job":"pty","status":3,"operation":"kill","refused":"own_session","note":"that is this pane's own session; ending it would end this program. Run it from another pane"}
```

## Locating Kilix

The job runs `kilix` as argv (never a shell): `KILIX_NEEDLE_KILIX` if set, as
for the other jobs, else `kilix` on `PATH`. The call is bounded by a 30-second
wall limit (90 with `--timeout-seconds`); the launcher bounds each broker call
itself (`KILIX_PTY_GUARD`, 10 seconds). A call that outlives its wall limit is
killed, and for `kill` reported as `completion: "unknown"`. The caller's
environment reaches the launcher unchanged, so the runtime is the one the
caller's panes use (`KITTY_PTY_BROKER_RUNTIME`, `XDG_RUNTIME_DIR`).

This job needs a Kilix with `kilix pty list|status|observe|kill|journals`
(0.2.2-rc6 or newer). When it is missing, or too old (an older Kilix answers any
`kilix pty` argument with `usage: kilix pty [--install-only]`), the request is
refused with status 2, a `note` that says which (install Kilix 0.2.2-rc6 or newer;
`kilix pty help` shows the verbs) and a `hint` that is an accepted request. For `end session` this happens at the lookup, so nothing was
sent.

## Options

| Option | Effect |
| --- | --- |
| `--json` | one JSON record |
| `--yes` | consent for a plain `end session ID` request |
| `--dry-run` | resolve and show; end nothing; a read prints its `argv` and runs nothing |
| `--agent` | never prompt for consent |
| `--timeout-seconds S` | bound each broker call, in seconds, 0.1 to 60 (not milliseconds) |
| `--request-json JSON\|-` | a structured request (below) instead of request text |

## Structured requests

For callers that would rather not build a sentence. The object has the same
operations and runs the same checks; unknown fields and wrong types refuse whole
with the unit and range in the note and an example in the hint.

```json
{"operation":"observe","id":"3fa9c2d41b7e6a05","max_lines":50}
```

| Field | Type, unit, range | Operations |
| --- | --- | --- |
| `operation` | `list`, `journals`, `status`, `pane`, `observe`, `journal`, `kill` | all |
| `id` | the full session ID, 1-64 of `A-Za-z0-9._-`, not starting with `-` | `status`, `observe`, `journal`, `kill` |
| `pane_id` | integer, a Kilix pane number | `pane` |
| `max_lines` | integer, lines, 1-1000 | `observe`, `journal` |
| `max_bytes` | integer, bytes, 1-65536 (not with `max_lines`) | `observe`, `journal` |
| `timeout_seconds` | number, seconds, 0.1-60 | all |

With neither `max_lines` nor `max_bytes`, Kilix applies its own bound (200 lines
and 64 KiB, the newest).

## MCP

`kilix-needle mcp --tools pty` serves exactly three tools and no model:

| Tool | Does |
| --- | --- |
| `kilix_pty_read` | the six read forms; refuses `end session` and points to `kilix_pty_act` |
| `kilix_pty_plan` | any form, without effect: a read prints its `argv`; `end session` resolves the session and prints the plan |
| `kilix_pty_act` | any form; `end session ID` needs `confirm_risky=true`; reads need no confirmation |

Each takes `request`, a string in the forms above or a structured object; `act`
also takes the boolean `confirm_risky` (default false). Extra fields are
rejected. `kilix_pty_read` and `kilix_pty_plan` carry the MCP read-only hint;
`kilix_pty_act` is marked destructive. The tools return the record above as
text and as structured content, and `isError` is true when `status` is not 0.

**Opt-in.** The three tools are in no other menu: the default `--tools all` and
`--tools actions` neither list nor accept them. Register a second server for
them, for example:

```sh
claude mcp add --scope user kilix-needle-pty -- kilix-needle mcp --tools pty
```

A direct `act` with an exact ID is one call: `plan` first only adds a call.
Schema sizes (tool name, description and input schema, compact JSON):
`kilix_pty_read` 941 bytes, `kilix_pty_plan` 814, `kilix_pty_act` 943.

**Caller identity.** The server's environment must carry the caller's
`KITTY_PTY_BROKER_SESSION`. A server started by Claude Code inside a Kilix pane
inherits it. Codex forwards only the variables named in `env_vars`; the entry
`kilix-needle setup --only codex` writes names `KITTY_PTY_BROKER_SESSION`,
`KITTY_PTY_BROKER_RUNTIME` and `KITTY_PTY_BROKER_EXECUTABLE` (re-run it after
updating; a hand-written Codex entry for the pty server must list them too).
Without the session, `kilix_pty_act` refuses `end session` (`caller_unidentified`)
and runs nothing; reads still work through `kilix_pty_act` without it (decided:
only `end session` fails closed).

## Limits

- One operation per request, one `kilix` call per read, two per kill.
- `observe` and `journal` return a bounded tail, never a stream; the broker
  replays at most the newest 1 MiB of a live session.
- No attach, no reap, no typing into a session, no pane-id kill.
- The grammar covers the wording in this document. Words outside it refuse; use
  the hint, or the structured request.
