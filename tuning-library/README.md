# Kilix panes domain

`contract.json` is the kilix-panes-tools/v2 contract exported by kilix-needle.
`tools.json` is its 14-tool list for prompt rendering and constrained decoding.
The contract fingerprint binds the schemas, defaults, checks and execution
implementation. Record it in each training manifest and model export. A runtime
must refuse a mismatch until the corpus/model has been revalidated.

`validate.py` sends a whole batch through one kilix-needle process without
loading Needle 2, PyTorch, NumPy, content assets or a desktop. It accepts JSONL
with `request` and `calls` fields. `validate_many(rows)` exposes the same batch
interface in Python. Set `KILIX_NEEDLE_HOME` when the application is not in the
standard workspace location. Refused proposals return `status=1`; no proposal
is silently removed from the validation report. Compare normalized `actions`
with the intended labels as well as checking the status.

`generate.py`, `corpus/`, `manifest.toml` and `requirements-train.*` were moved
in from kilix-needle-tuning. `MIGRATION.json` records the original commit and
byte digests. The old pinned submodule remains available as a compatibility
fallback for standalone Needle installations. Workspace tuning reads this pack;
`KILIX_ML_HOME` can select an explicit source root.

The pack now includes blind-written templates for all 14 actions and a blind
off-domain list. Five new-action templates had a side-slot grammar error;
their corrected forms and the contract pinned to kilix-needle `59beb18` feed
the immutable kilix-ml `14-v6` training corpus. Its swap validation split
holds out two whole phrasings with all four directions. `MIGRATION.json` still records
the original imported ten-action files; it does not describe later blind
additions. Regression tests in kilix-needle are development material, not a
held-out model evaluation set. Keep the independent evaluator away from this
pack and the training data.

The Needle compatibility recipe still renders five tools and skips/counts
examples requiring new actions it cannot express. The 7.2M model uses all 14
tools directly, not that recipe or its LoRA settings.

For inference, feed raw `function_calls` and the original request to
`kilix-needle bridge --mode plan` or `--mode execute`. The JSONL envelope must
include `contract_sha256`. Execution uses the same checks, target resolution,
confirmation and agent self-protection as Needle's existing path. `--yes` may
confirm risky calls but never overrides a partial refusal. JSONL mode cannot
ask for interactive confirmation. Use `--agent` when called by an agent.
