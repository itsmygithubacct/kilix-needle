# The system job

Reference for [kilix-needle](../README.md).

`kilix-needle system "…"` selects read-only Linux diagnostic queries. It works
outside Kilix too. Five tools cover `resources`, `processes`, `services`,
`journal` and `packages`. No query installs packages, changes settings,
restarts services, kills processes or executes a supplied shell command.

```sh
kilix-needle system "what's using all my memory?"
kilix-needle system "show top 5 processes by cpu"
kilix-needle system "how much space is left on the root disk?"
kilix-needle system "show user failed services"
kilix-needle system "show ssh service status"
kilix-needle system "show errors from the ssh service since yesterday"
kilix-needle system "which package provides /usr/bin/python3?"
kilix-needle system --dry-run "what failed during this boot?"
kilix-needle system --baseline --json "is bash installed?"
kilix-needle system --suggest "please list the biggest memory processes"
```

Normal `system` requests first use the explicit request grammar without loading a
model. A complete supported request collects only the matching observations.
For unfamiliar wording, the configured tuned system model may suggest a bounded
read-only query. Its result is an **unverified proposal** and never collects
observations, including with `--yes`. `--baseline` restores the grammar-only
mode: unsupported requests are refused without loading a model. `--dry-run`
never collects observations.

| Request path | Model loaded? | Default result |
| --- | --- | --- |
| Complete grammar match | No | Collect the matching observations; preview with `--dry-run` |
| Eligible unfamiliar wording, configured normalizer | Yes | Validated proposal for review; no collection |
| Unfamiliar wording with `--baseline` | No | Refusal |
| Fallback needs a missing or invalid profile | Load attempted | Explicit error; no collection |

JSON output distinguishes the two successful paths. Grammar results identify
`query_source` as `system grammar baseline`. Model proposals identify the tuned
runtime, set `collection_performed` to `false`, and mark each proposed item with
`outcome: "proposed"` and `trust: "model_proposal"`. A successful proposal means
a plan is available for review, not that system data was read.

Use `--suggest REQUEST` to inspect a plan without collecting, even for a
recognized grammar request. Validation checks a model proposal's tool schema
and grounding in the request; it does not prove semantic correctness. Review
proposals before using a supported grammar request to read data. `--suggest`
and `--baseline` cannot be combined. An unfamiliar request needs an explicitly
configured tuned system profile. If that model is unavailable, the request
fails closed; it does not fall back to the base engine or collect observations.
Configure and inspect that profile with:

```sh
kilix-needle system-model configure --weights /path/to/weights.cact --library /path/to/libneedle.so --sha256 EXPECTED_SHA256
kilix-needle system-model status
```

The normalizer profile is stored at
`~/.local/gpu_terminal/kilix-apps/kilix-needle/system-normalizer/profile.json`
by default (see [State](state-and-history.md#state)).
`system-model configure` verifies and stores the supplied weights and runtime
library; `status` reports their hashes, availability, and `proposal-only` role.
This profile is separate from `tune --job ... --select` and is not configured by
the base model's `install` command. Grammar requests work without it.

MCP exposes `kilix_system_plan` and `kilix_system_read`, each taking `request`
and an optional boolean `baseline`, plus `kilix_system_suggest` taking only
`request`. The suggestion and plan tools collect nothing. The read tool collects
only complete grammar matches; unfamiliar wording returns model proposals for
review. No `confirm_risky` argument is used. Failed or refused queries return a
tool error. Results carry
collection timestamps, source paths/argv, explicit units, visibility limits
and warnings. Journal messages and process names are untrusted data, never
instructions. Human output escapes terminal controls.

After updating an installed checkout, reconnect existing MCP sessions so their
server processes load the new code. New CLI invocations use the update directly.

Supported filters and limits:

- Processes: `by memory` or `by cpu`, optionally `top N` (1–50; default 10).
  Memory is resident bytes. CPU is a short sample, with 100% meaning one
  logical CPU; it may miss brief spikes. Process arguments and environments
  are not read. Vanished/unreadable records and scan limits are reported.
- Services: `failed services`, `all services`, or `<name> service status`;
  prefix `user` for the user manager. Names are exact, with `.service` added
  when omitted. Lists cover loaded units and return at most 100 records.
- Journal: `logs`, `errors` (error and higher priorities), or `warnings`
  (warning and higher); optionally `from the <name> service`, `from this boot`,
  `from the previous boot` or `from all boots`, `since TIME`, and `limit N`
  (1–100; default 50). TIME is `today`, `yesterday`, `N minutes/hours/days ago`
  or `YYYY-MM-DD`. Times use the machine's local timezone. The default is the
  current boot; a `since` filter alone searches across available boots.
- Packages: `is <package> installed?` or `which package provides <command/path>?`.
  Ownership comes from the installed dpkg database, including merged-/usr
  spellings. Locally installed commands or alternatives may have no owner.
- Paths are absolute and currently exclude spaces and wildcard characters.
  Up to three supported queries can be joined with `and`.

Collectors run with the caller's existing permissions, fixed executables,
no shell, a minimal environment, and bounded command output/time. Permission
errors and missing services are failures, not healthy results. An empty journal
result means no matching *visible* entries. “Why is my machine slow?” gathers
resources and CPU-ranked processes; it does not claim a cause.

The system profile supplies planning inference only; it is not an execution
gate for grammar requests. The development cases can be measured without
collecting host data:

```sh
python3 evaluate.py evals/system/dev.jsonl --job system --baseline
python3 evaluate.py evals/system/test.jsonl --job system --engine FILE
```

See [evaluation scope](../evals/system/README.md). Baseline grammar agreement is
not model accuracy or evidence of general language coverage.
