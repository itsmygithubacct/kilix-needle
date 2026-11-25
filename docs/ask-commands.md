# Ask commands and models

Reference for [kilix-needle](../README.md).

## Shared command backends

Ask uses LFM2 Kilix Commands by default, with a common command grammar and
validation path for starting coding
sessions, waiting for or messaging a named existing session, and opening the
calculator, notepad, file manager or PDF viewer:

```sh
kilix-needle install --commands
kilix-needle ask --plan "open calculator then open notepad"
kilix-needle ask --commands-backend grammar --plan "open calculator then open notepad"
kilix-needle ask --commands-backend grammar --yes --operation-id open-tools "open calculator then open notepad"
```

`grammar` loads no generative model. `needle3`, `lfm2` and `lfm3` retain that grammar as
the fast path and validate every model proposal against the same complete
request. All four modes keep the other Ask jobs' deterministic parsing; their
older generative fallbacks are disabled. The routing classifier remains in use.
Set `KILIX_ASK_COMMANDS_BACKEND` to select a mode for the `kilix ask` launcher.
Without an override or a saved selection, Ask uses LFM2 when this release's
bundle is installed with its receipt, and the grammar when it is not. An empty
setting means the same. Ask never downloads the model on its own: `kilix ask`
at a terminal asks whether to install it, and the Kilix model wizard's Kilix
Ask page offers it too. Declining at `kilix ask` saves the grammar as the
selection. Declining on the wizard page is recorded by the wizard, and the
next `kilix ask` at a terminal saves the grammar without asking. Either way
the question is not repeated; an unanswered or failed offer is made again on a
later run. A request the stand-in grammar cannot read says that
the model is not installed. The grammar fast path works without loading the
model. Model requests need the installed bundle; `install --commands` and
`workflows setup --model lfm2` show its full terms before installation. LFM2's terms, including its commercial-use limitation, are a
notice by the owner's 2026-11-07 determination: the install is confirmed, and
no agreement is typed or recorded. The selected CPU runtime requires x86-64
Linux.

The pinned LFM2 delivery is `lfm2-kilix-commands-20261115-v10`, with weight
revision `lfm2-kilix-v5e1-wse-20261106.9`. Its model file and runtime build are
those of `lfm2-kilix-commands-20261106-v9`, byte for byte; v10 ships each
versioned runtime library once, under the name the other runtime files load,
instead of three times. It retains the fine-tuned V5 lineage
and changes output rows 568 and 8110 of `20261106.8`; all internal tensors, the
prompt and the CPU runtime remain unchanged. The repair uses regularized
output-row optimization on frozen activations with analytic gradients. The
manifest records the parent and GGUF checksum; `notices/NOTICE.txt` describes
the modifications. The vendored kilix-content supplies the same catalog asset
and licence record that the Plebian-OS image installs.

New installations use a manifest-specific directory, preserving earlier model
bytes and receipts for catalog rollback. `.21` remains available as the rollback
release. A previous receipt does not silently authorize a new manifest.
Plebian-OS 0.2.3 does not carry the archive on its installation media and
does not download it during an upgrade. A download happens only when the user
asks for the model, and that installation shows the model's own licence terms.
Installing a delivery reuses the files of an earlier delivery already on the
machine when they verify against the new manifest, so moving from
`lfm2-kilix-commands-20261106-v9` needs no download. The earlier copy is kept.
`tools/build_delivery.py` is the program that built the v10 archive from the
extracted v9 delivery; it is deterministic and checks every kept file against
its source.

A request that plans nothing can be answered with a wording the command
grammar reads ("did you mean: start codex in a split below"). By default
the decision model `d3:2b` builds it where the local Ollama server lists that
model, and nothing is offered elsewhere; `kilix-needle workflows suggest --set
off` turns this off, `--set model` takes the wording from the selected command
model's own proposal, and `--set d3:2b` (or another decision model) uses that
model on the local Ollama server (with a command model selected as well, only
when both make the same plan). Every wording is held to the
request's own words and must be read back by the grammar as the same plan.
It is offered, never run. On a blind set of 100 requests the grammar alone
answered 49; with suggestions 87 (LFM2's proposal, 2 wrong of 40), 94
(`d3:2b`, 1 wrong of 47) and 87 (both agreeing, 0 wrong of 38). See
[Ask suggestions](ask-suggestions.md) for how it works and what was
measured.

## LFM2 benchmark: upstream, V5, .21 and .8

The fresh 160-request comparison uses the same prompt and runtime for every
model. Requests were written before selecting the repaired weights; they are
internal tests, not an independent human evaluation. No proposed actions were
executed. CPU results:

| Measure | Upstream Q8_0 | Kilix V5 | Kilix .21 | Kilix .8 |
| --- | ---: | ---: | ---: | ---: |
| Exact model responses | 68/160 | 132/160 | 134/160 | 139/160 |
| Correct result after application validation | 92/160 | 140/160 | 138/160 | 143/160 |

`.8` gains five correct responses over `.21` on both CPU and GPU without losing
a previously correct response in this set. It retains three false abstentions;
no incorrect action was admitted. Development and literal-copy probes also show
no observed loss against `.21`. On 32 operational requests the two models tie
at 27 exact model responses and 29 correct validated results. That operational
set includes one development overlap and one disclosed label correction; it is
not an independent holdout. These measurements concern Kilix command requests,
not general reasoning ability. Later application changes need their own checks.

The [earlier stage-by-stage benchmark](lfm2-output-format-benchmark.md) and
[original .21 report](lfm2-v21-benchmark.md) retain their historical prompts,
application revisions and results; they do not describe the current default.

Install the Needle 3 alternate with `kilix-needle install --commands needle3`,
then use `kilix ask --model needle3 "open calculator"`. LFM2 remains the default
on a machine that has it installed.
For subsequent requests, set `export KILIX_ASK_COMMANDS_BACKEND=needle3` in your
shell (or its startup file to keep the preference). `--model lfm2` selects LFM2
for one request without changing that preference; `--model grammar` uses no model.
Needle 3 uses the pinned V9 epoch-1 model and cached CPU runtime on x86-64-v3
Linux. Missing assets name the installer; Ask never downloads them or substitutes
another model. Both installers show the applicable licence and notices before
asking for agreement. Existing licence receipts retain their original bindings.

LFM3-1.7B is an optional model for Plebian-OS 0.2.3 rc1. Install it with
`kilix-needle install --commands lfm3`, then select it for one request with
`kilix ask --model lfm3 "open calculator"`, or save it with
`kilix-needle workflows select --model lfm3`. It uses unchanged upstream Q8_0
weights and a pinned LFM3-compatible CPU runtime. It has not been fine-tuned
for Kilix. The full model terms and runtime notices accompany the bundle under
`notices/`; LFM2 remains the default where it is installed. Adding the model does not establish
benchmark superiority or qualify an OS release.

Needle 3 prepares its fixed tool prefix once when an owned runtime starts. The
worker must acknowledge that phase within 30 seconds; preparation then has a
separate, absolute 90-second limit. Requests retain their 30-second limit.
An unresponsive or cancelled runtime is closed and reaped. File and licence
readiness alone does not prove that initialization will finish on a given CPU.
Python callers can inspect `startup_phase` and `startup_stages` on the runtime;
`compact_model.open_runtime(..., cancel_event=event)` also accepts a caller-owned
`threading.Event` to cancel initialization or requests. Preparation reports a
stage, without claiming token-level progress. Its cache belongs to that runtime;
no prepared state is shared between bundles or saved to disk.
Each request's deadline covers sending it as well as receiving its reply.
Protocol requests are limited to 1 MiB; a full or unread pipe remains cancellable.

An explicit verified manifest with `--commands-candidate` overrides the installed
Needle 3 selection. The `kilix ask` launcher forwards this option too.
LFM2 and LFM3 use their pinned release selections and Content bundles; an explicit local
bundle can be supplied with `--commands-bundle`. To evaluate another local
candidate, pass its artifact manifest with `--commands-manifest` together with
`--commands-bundle`. That manifest selects the model and native runtime files;
their recorded sizes and hashes are verified before loading. Missing or
mismatched assets
fail without substituting a different model. `--commands-model-only` bypasses
the grammar fast path for evaluation; the same validation and host checks
still apply. A model proposal alone cannot authorize an action.

Double-quoted task and message arguments use JSON escaping and retain their
internal spacing and case. For example, `start codex with task "keep A  B"`
sends `keep A  B`. A complete quoted argument after a colon uses the same
escaping: `tell codex: "keep A  B"` sends `keep A  B`. Other colon text remains
literal, including embedded quotes and polite words. Quote a payload before
adding another action, as in `tell codex: "ready"; then open calculator`.
An apparent action after an unquoted payload is ambiguous and is refused.
Extra instructions outside a quoted argument must have a complete supported
reading; conditional, deferred and unsupported actions are refused.

A final period or exclamation mark after an unquoted path is sentence
punctuation. Quote paths with literal final punctuation: `start codex in
"/projects/sample."`. Internal dots and explicit `.` or `..` path components
remain unchanged. `kick off a Codex session` is also an explicit launch form.

Agent launches in this shared command contract use bounded wording. `kick off`
is a launch synonym with the same named-agent and explicit-field rules as
`start` and `launch`; complete courtesy suffixes are supported outside literal arguments. For launches
without a task, leading `please`, a request such as `could you ...?`, and final
`please`, `now`, or `for me` are supported.
For example, `Please kick off the Codex session here.` and `Could you launch
Codex here?` are complete requests. `Please kick off the Codex session here now.` is also complete.
Unconsumed trailing `right now` or `tomorrow` remains unsupported. Unknown suffixes, conditions, extra actions and ambiguous
shared fields are never discarded to admit a shorter request.

Application opening has its own wording: `Open calculator now.` and `I need
the file manager opened now.` are supported. The same complete courtesy suffixes apply to agent launches. The separate
panes and agents jobs do not widen this shared command contract. Literal arguments remain data: `kick off Codex
with task "Report now."` keeps the task exactly, and `message Codex: "now"`
sends `now`. A bare `now` immediately after the closed quoted argument is an unsupported tail,
not part of that payload. A valid action beside a refused clause does not admit
a partial batch.

Unquoted paths, placement words and the four-action limit are described in the README's [Paths and placement](../README.md#paths-and-placement-in-ask-commands) section.
