# Workflows setup and model readiness

Reference for [kilix-needle](../README.md).

`kilix-needle setup` itself is described in the README's [Setup](../README.md#setup) section.

The desktop's **Enable Kilix Workflows** integration shares Ask's model
selection, release pins and Content installer:

```sh
kilix-needle workflows status --json   # read-only; exit 0 ready, 1 needs attention
kilix-needle workflows setup          # set up the current choice; saves nothing
kilix-needle workflows setup --model needle3  # install and select the alternate
kilix-needle workflows setup --model grammar  # select grammar; no download
kilix-needle workflows select --model lfm2    # select an already installed model
kilix-needle workflows status --legacy --json # older Needle2 jobs, separately
```

Only an explicit `--model` is saved, and only after that model is ready.
Setup without it prepares the model chosen by `KILIX_ASK_COMMANDS_BACKEND`,
the saved preference or the default, and leaves the saved preference unchanged.
An unreadable or invalid saved preference is refused and left as it is; the
error names the explicit model option and the `workflows select` repair.

Kilix supplies `KILIX_CONTENT_ROOT`; a standalone call may pass `--root` with
an absolute Content root. Status emits at most 16 KiB of JSON with schema
`kilix.workflows/v1`, `ready`, `status` (`ready` or `not-ready`), `detail`,
`scope`, `backend`, `grammar_ready`, `required_assets`, `assets`, `models`, and `errors`. Each asset reports `id`,
`version`, `path`, `state` (`ready`, `missing`, `corrupt`, `agreement-required`,
or `unavailable`), and `error`. Model rows report `kind`, `job`, `state`, and
`error`. Readiness verifies the current catalog, shared licence receipts and
all installed member bytes; it starts no engine and writes nothing.

LFM2 is the default Ask backend. An explicit model option overrides
`KILIX_ASK_COMMANDS_BACKEND`, which overrides the saved Workflows preference.
Setup saves that preference only after its assets pass verification; `select`
checks and saves without installing. Grammar needs no model, licence or Content
root. Model readiness covers the selected backend's platform, release pins,
files and licence; it does not run inference. It is independent of older tuned
job selections. The Model Setup wizard offers LFM2, Needle3 and grammar together;
when several models are accepted, the first accepted choice becomes the default.

With `--legacy`, the base `needle2` asset is required. A selected panes/agents tuned model also
needs `needle2-runtime` unless an explicitly configured pinned library is
verified. Configured system profiles verify their own managed weights and
library. Training assets are not required. Grammar and structured actions
remain usable without models. Readiness describes verified files and licence
coverage; it does not measure inference or restore model context.

Setup acquires only missing current assets through Content's typed licence
flow. An exact current receipt is reused on retry, and a nonblocking lock per
Content root prevents overlapping setup sessions. Setup verifies readiness
again before succeeding. It never trains, promotes selections, downloads
custom weights, or replaces a corrupt selected asset: remove/repair that asset
explicitly before retrying. The separate `--legacy` setup requires explicit
repair of invalid custom selections/profiles and does not support
`KILIX_NEEDLE_ENGINE` overrides. There is no unattended acceptance flag.
