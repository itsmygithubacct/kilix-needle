# Workflows setup and model readiness

Reference for [kilix-needle](../README.md).

`kilix-needle setup` itself is described in the README's [Setup](../README.md#setup) section.

The desktop's **Enable Kilix Workflows** integration shares Ask's model
selection, release pins and Content installer:

```sh
kilix-needle workflows status --json   # read-only; exit 0 ready, 1 needs attention
kilix-needle workflows choice         # "BACKEND SOURCE": the Ask backend and why
kilix-needle workflows setup          # set up the current choice; saves nothing
kilix-needle workflows setup --model lfm2     # show LFM2's terms, download, select
kilix-needle workflows setup --model needle3  # install and select the alternate
kilix-needle workflows setup --model grammar  # select grammar; no download
kilix-needle workflows select --model lfm2    # select an already installed model
kilix-needle workflows status --legacy --json # older Needle2 jobs, separately
kilix-needle workflows suggest                # how Ask suggests a wording; reads only
kilix-needle workflows suggest --set d3:2b    # off, model, or a decision model in Ollama
```

Only an explicit `--model` is saved, and only after that model is ready.
Setup without it prepares the model chosen by `KILIX_ASK_COMMANDS_BACKEND` or
the saved preference, and leaves the saved preference unchanged. With neither,
the choice is LFM2 on a machine that has it installed and the grammar on one
that does not, so setup without `--model` never downloads LFM2 unasked.
`workflows choice` prints that backend and its source (`option`,
`environment`, `saved`, `installed` or `not-installed`) on one line. It writes
nothing and hashes no file.
An unreadable or invalid saved preference is refused and left as it is; the
error names the explicit model option and the `workflows select` repair.
`workflows suggest` prints, and with `--set` saves, where the wording comes
from that Ask offers for a request it cannot read; see
[Ask suggestions](ask-suggestions.md). What the grammar reads without any
model is in [Ask phrasing](ask-phrasing.md). It downloads and installs nothing.

Kilix supplies `KILIX_CONTENT_ROOT`; a standalone call may pass `--root` with
an absolute Content root. Status emits at most 16 KiB of JSON with schema
`kilix.workflows/v1`, `ready`, `status` (`ready` or `not-ready`), `detail`,
`scope`, `backend`, `grammar_ready`, `required_assets`, `assets`, `models`, and `errors`. An Ask status also has
`selection`, the source `workflows choice` prints, and `offer`, which is `lfm2`
when the grammar is standing in for the model that is not installed and null
otherwise. Each asset reports `id`,
`version`, `path`, `state` (`ready`, `missing`, `corrupt`, `agreement-required`,
or `unavailable`), and `error`. Model rows report `kind`, `job`, `state`, and
`error`. Readiness verifies the current catalog, shared licence receipts and
all installed member bytes; it starts no engine and writes nothing.

LFM2 is the default Ask backend where it is installed, and the grammar is
the default where it is not. An explicit model option overrides
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
