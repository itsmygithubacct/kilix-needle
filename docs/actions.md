# Structured actions

`kilix-needle action --request-json -` accepts the same `kilix.actions/v1`
request as `kilix action --request-json -`. It uses the shared Kilix controller
directly, with no model, model assets, or natural language parsing. The existing
pane and agent grammars remain available for plain requests.

Every request requires an operation ID and exact source/target identities.
Obtain pane IDs and broker identities from the Kilix pane snapshot. `source`
must match the inherited caller. The controller rechecks both identities before
each side effect and refuses missing, stale, or ambiguous identities.

Pass the JSON in the same invocation, substituting the inspected identities and
an existing absolute directory:

```sh
kilix-needle action --yes --request-json - <<'JSON'
{"schema":"kilix.actions/v1","operation_id":"build-001","operation":"pane.open","source":{"pane_id":1,"broker":"aaaaaaaaaaaaaaaa"},"target":{"pane_id":2,"broker":"bbbbbbbbbbbbbbbb"},"params":{"argv":["/bin/sh"],"cwd":"/abs/project","title":"build","placement":"split","direction":"right","bias":50}}
JSON
```

Or save the JSON as `action.json` and redirect it. A preview is optional; an
authorized action performs the same checks directly:

```sh
kilix-needle action --dry-run --request-json - < action.json
kilix-needle action --yes --request-json - < action.json
```

`argv` is a literal argument array. To create a coding-agent pane, use
`operation: "agent.launch"` with these params:

```json
{"agent":"codex","cwd":"/abs/project","title":"review","placement":"new-tab","model":"gpt-6.1-sol","prompt":"Review the diff"}
```

Launch supports `codex`, `claude`, `kimi`, `grok`, and `qwen-omp`. Optional
`resume`, `coding_yolo`, and allowlisted `agent_arg` follow the shared controller's
checks. `trust_folder: true` is refused: configure folder trust explicitly with
the existing `agent-control` setup before launching. `direction` and `bias` apply
only to `placement: "split"`.

To deliver a literal single-line message to a supported Codex composer, use a
new operation ID, `operation: "agent.deliver"`, and:

```json
{"text":"Run the tests","mode":"steer"}
```

`mode: "defer"` queues the message when supported. Text including its operation
ID marker must fit the controller's 900-byte wire limit. Unknown fields are
refused. Requests are limited to 16 KiB. `timeout` is optional and measured in
**seconds**, from 1 to 60, default 15. Omit it for routine calls or use
`"timeout": 15`; millisecond values such as `15000` are invalid. Empty stdin is
refused: `--request-json -` needs a pipe, a quoted here-document, or file
redirection in the same invocation.
CLI always prints one compact JSON receipt. Mutations require `--yes` or
interactive consent; stdin JSON requires `--yes` because stdin cannot also carry
consent. `--agent` never prompts.

To inspect `build-001`, keep its exact source/target, change `operation` to
`operation.status`, and set `params` to `{}`. This read-only request needs no
`--yes`; omit `dry_run`. Status reads durable state and never resubmits. Reusing
an operation ID with the identical mutation returns its receipt with
`duplicate: true`; conflicting reuse is refused. After uncertainty, inspect the
existing ID before deciding what to do next.
Skip routine status preflight for a fresh operation and another lookup after a
verified receipt. Keep that receipt and operation ID for recovery.

MCP exposes `kilix_action_plan`, `kilix_action_act`, and `kilix_action_status`.
Each accepts `{"request": REQUEST_OBJECT}`. Act additionally requires
`"confirm_risky": true` to authorize mutation; plan/act accept mutation
operations, and status accepts `operation.status` only. Both MCP result forms
contain the same compact receipt as CLI.

For an agent using only structured actions, start the server with:

```sh
kilix-needle mcp --tools actions
```

This mode lists and accepts only the three `kilix_action_*` tools. Requests for
other tools fail before dispatch or model loading. `--tools all` is the default
and retains every existing tool. Each server keeps its own selection; consent,
identity checks and receipt behavior are identical in both modes. Configure the
client's server arguments as `["mcp", "--tools", "actions"]` to opt in. The
selection is a tool surface, not an operating-system sandbox.

`planned` verifies identities without mutation. `created` verifies a new pane
and includes its exact identity in `evidence.pane`. `submitted` and `deferred`
verify screen delivery. None establishes agent startup, acknowledgment, or task
completion; those verification flags remain false. `pending` means a recorded
operation has not reached mutation intent; `uncertain` means mutation may have
begun or its outcome could not be verified. `blocked` and `not_found` also carry
no completion claim. These four statuses return a nonzero CLI status, and MCP
marks them as errors. The structured route has contract tests and isolated
integration checks. Token and latency savings depend on the client workflow;
measure them for the selected tool menu.

Discovery uses `KILIX_ACTION_MODULE_ROOT` when explicitly set to an absolute
Kilix config directory. Otherwise it uses `KILIX_NEEDLE_KILIX` (a source directory
or executable), then `kilix` on PATH and resolves its symlink to `config`.
Only `agent_actions.py` is imported in an isolated Python worker; the Kilix
launcher and installation setup are never executed. Receipts and worker output
are bounded to 16 KiB, with stdout reserved for the receipt.
