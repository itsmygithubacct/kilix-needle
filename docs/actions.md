# Structured actions

`kilix-needle action --request-json -` accepts the same `kilix.actions/v1`
request as `kilix action --request-json -`. It uses the shared Kilix controller
directly, with no model, model assets, or natural language parsing. The existing
pane and agent grammars remain available for plain requests.

Every request requires an operation ID and exact source/target identities.
Obtain pane IDs and broker identities from the Kilix pane snapshot. `source`
must match the inherited caller. The controller rechecks both identities before
each side effect and refuses missing, stale, or ambiguous identities.

Save this as `action.json`, substituting the inspected identities and an existing
absolute directory:

```json
{"schema":"kilix.actions/v1","operation_id":"build-001","operation":"pane.open","source":{"pane_id":1,"broker":"aaaaaaaaaaaaaaaa"},"target":{"pane_id":2,"broker":"bbbbbbbbbbbbbbbb"},"params":{"argv":["/bin/sh"],"cwd":"/abs/project","title":"build","placement":"split","direction":"right","bias":50}}
```

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
refused. Requests are limited to 16 KiB; `timeout` is optional, 1–60 seconds.
CLI always prints one compact JSON receipt. Mutations require `--yes` or
interactive consent; stdin JSON requires `--yes` because stdin cannot also carry
consent. `--agent` never prompts.

To inspect `build-001`, keep its exact source/target, change `operation` to
`operation.status`, and set `params` to `{}`. This read-only request needs no
`--yes`; omit `dry_run`. Status reads durable state and never resubmits. Reusing
an operation ID with the identical mutation returns its receipt with
`duplicate: true`; conflicting reuse is refused. After uncertainty, inspect the
existing ID before deciding what to do next.

MCP exposes `kilix_action_plan`, `kilix_action_act`, and `kilix_action_status`.
Each accepts `{"request": REQUEST_OBJECT}`. Act additionally requires
`"confirm_risky": true` to authorize mutation; plan/act accept mutation
operations, and status accepts `operation.status` only. Both MCP result forms
contain the same compact receipt as CLI.

`planned` verifies identities without mutation. `created` verifies a new pane
and includes its exact identity in `evidence.pane`. `submitted` and `deferred`
verify screen delivery. None establishes agent startup, acknowledgment, or task
completion; those verification flags remain false. `pending` means a recorded
operation has not reached mutation intent; `uncertain` means mutation may have
begun or its outcome could not be verified. `blocked` and `not_found` also carry
no completion claim. These four statuses return a nonzero CLI status, and MCP
marks them as errors. The structured route has contract tests and isolated
integration checks; no token savings or paid-model benchmark is claimed.

Discovery uses `KILIX_ACTION_MODULE_ROOT` when explicitly set to an absolute
Kilix config directory. Otherwise it uses `KILIX_NEEDLE_KILIX` (a source directory
or executable), then `kilix` on PATH and resolves its symlink to `config`.
Only `agent_actions.py` is imported in an isolated Python worker; the Kilix
launcher and installation setup are never executed. Receipts and worker output
are bounded to 16 KiB, with stdout reserved for the receipt.
