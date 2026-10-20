# Desktop actions experiment

`kilix-needle desktop` converts a direct natural-language request into five
structured tool types: `window`, `pointer`, `keyboard`, `scroll`, and `wait`.
Needle 2 proposes a JSON plan. The contract checks its fields, literal text,
numbers, targets, intent and clause order. An xdotool backend resolves exact
windows and submits the corresponding X11 operations using fixed argument
arrays. There is no shell-command generation.

## Use

All interfaces use the same versioned action contract, target resolution and X11
executor. The typed interface and grammar need no model assets or licence setup.
Install the typed command with `kilix-needle setup --only desktop-command`.

```sh
kilix-desktop window list
kilix-desktop --yes window focus --target 'title:Notes'
kilix-desktop --yes window move --target active --x 80 --y 120
kilix-desktop --yes window resize --width 800 --height 600
kilix-desktop --yes pointer click --x 300 --y 240 --button left
kilix-desktop --yes keyboard type --text 'literal text'
kilix-desktop --yes keyboard press Return
kilix-desktop --yes scroll down --steps 3
kilix-desktop --yes wait 100
kilix-desktop --yes batch actions.json
kilix-desktop schema
kilix desktop-action --help
```

`kilix desktop-action` delegates to `kilix-desktop` on PATH. An explicit
`KILIX_DESKTOP_CLI` executable or `KILIX_DESKTOP_MODULE_ROOT` source directory
selects an alternative installation without automatic fallback. Help and schema
are read-only. Every typed subcommand has `--help`. Keyboard type also accepts
`--text-file FILE` (`-` for stdin); it preserves whitespace and rejects control
characters, including trailing newlines.

Natural-language parsing has three explicit modes:

| Mode | Behavior |
| --- | --- |
| `--parser grammar` | Deterministic bounded language; no model is loaded. |
| `--parser needle` | Every eligible request uses the selected Needle model. |
| `--parser hybrid` (default) | Grammar first, then lazy Needle fallback on a grammar miss. |

Receipts contain `parser_mode` and the actual `parser` (`grammar`, `needle`, or
`structured`). Fallback happens during planning only. Failed validation, target
resolution, or execution never triggers another executor or a retry.

The grammar supports every action family, explicit quoted titles, active/current
windows, decimal window IDs, numeric coordinates/sizes, exact quoted text, key
chords, scroll steps and short pauses. Examples:

```sh
kilix-needle desktop --parser grammar --parse-only 'resize the active window to 800x600'
kilix-needle desktop --parser grammar --yes 'focus window "Notes" then type "hello" then press Return'
kilix-needle desktop --parser grammar --yes 'move the pointer to 120, 250 then double click'
kilix-needle desktop --parser hybrid --model /abs/desktop-bundle --yes 'scroll down five steps'
kilix-needle desktop --parser needle --model /abs/desktop-bundle --parse-only 'wait 0.5 seconds'
```

MCP remains opt-in: `kilix-needle mcp --tools desktop --desktop-parser hybrid
--desktop-model /abs/desktop-bundle`. The read tool needs no model. Plan/act
accept exactly one of `request` (natural language) or `actions` (the JSON action
list). A request may select `parser`; structured actions never load a model.
Act requires `confirm_risky: true`. For example, structured arguments are
`{"actions":[{"name":"keyboard","arguments":{"action":"type","text":"hello"}}],"confirm_risky":true}`.
Use `kilix-desktop schema` for all action fields and bounds.

This first backend requires an X11 display, an EWMH window manager, `xprop`, and
`xdotool`. Graceful close and maximize/restore require a recent xdotool providing
`windowquit` and `windowstate`. The backend checks for those commands and never
substitutes destructive `windowclose` or `windowkill`. An absolute
`KILIX_NEEDLE_XDOTOOL` path selects a separately built executable.

```sh
kilix-needle desktop --list
kilix-needle desktop --model /abs/desktop-bundle 'focus window "Notes"'
kilix-needle desktop --model /abs/desktop-bundle --yes 'focus window "Notes" then type "hello" then press Return'
kilix-needle desktop --model /abs/desktop-bundle --yes 'move the pointer to 400, 300 then double-click'
kilix-needle desktop --model /abs/desktop-bundle --yes 'scroll down 3 steps'
kilix-needle desktop --model /abs/desktop-bundle --parse-only 'resize the active window to 800 by 600'
```

The default is a preview against a live window snapshot. `--parse-only` avoids
an X11 connection and returns a checked proposal with unresolved targets.
`--yes` authorizes execution. `--list` reads windows without loading a model.
Without `--model`, natural-language proposals use the installed Needle 2 base.
A bundle consists of `model.json` and `model.cact`; its schema digest, file size
and SHA-256 must match. Newly packaged bundles also pin the native runtime's
hash, so a runtime change requires explicit re-evaluation. Invalid selected weights fail; there is no silent
fallback to stock. The installed, licence-covered Needle 2 runtime is reused.

Names mean exact complete window titles; duplicate titles are ambiguous. Use
`window id 12345` from the current listing to disambiguate. `active` binds to
the snapshot's active window, updated by preceding explicit focus operations.
The executor rechecks identity and keyboard focus before input. A click that
focuses another window can make a later keyboard action fail; explicitly focus
the intended window in the sequence or issue a new request after observing it.

Typing copies one quoted, single-line literal of up to 512 UTF-8 bytes and does
not press Enter. Characters are paced; non-ASCII text gets extra time for X11
key mapping updates. Key presses accept one allowlisted key or modifier chord. Pointer
coordinates are absolute screen pixels. Drag starts at the current pointer and
releases its button even when movement fails. Scroll amounts are wheel notches,
with a default of three and a maximum of twenty. Pauses accept milliseconds or
seconds, up to three seconds per pause. Requests have at most six actions and
six seconds of total pauses. Unknown clauses are refused as a whole.

Explicit structured actions use the same resolver and executor without a model:

```sh
kilix-needle desktop --yes --actions-json - <<'JSON'
[{"name":"pointer","arguments":{"action":"move","x":400,"y":300}}]
JSON
```

MCP is opt-in:

```sh
kilix-needle mcp --tools desktop --desktop-model /abs/desktop-bundle
```

This menu has `kilix_desktop_read`, `kilix_desktop_plan`, and
`kilix_desktop_act`. Act requires `confirm_risky: true`. The ordinary `all`,
`actions`, and `pty` menus retain their own capabilities. MCP calls can specify
`display`; CLI uses `--display`, otherwise `DISPLAY`. An implicit Wayland
session is refused. An explicitly selected X11 display can run under a Wayland
host, but this does not grant control of native Wayland applications.

## Receipts and limits

A `submitted` receipt records successful event/command submission. It does not
establish that text was saved, a button performed its function, a window closed,
or a task completed. Geometry observations and verified focus are reported when
available. Graceful close may leave a save prompt. Sequences stop at their first
failure and record preceding effects; they are not transactions and are not
retried automatically. `uncertain` requires observation before another attempt.

X11 cannot make a focus check and a following injected event atomic. Window IDs
also lack generation counters. PID/class and, when PID is missing, title checks
reduce stale-target errors but cannot eliminate races with another desktop user
or a changing application. Application-specific shortcut semantics still apply.

This version does not interpret screenshots, find visual controls, infer
coordinates, navigate applications autonomously, or translate commands for
native Wayland. It does not accept loops, conditions, arbitrary scripts, program
launches, background keyboard injection, or unbounded event streams.

## Training and evaluation

`experiments/desktop-actions/prepare.py` builds a reproducible synthetic corpus
with directly constructed labels. Development uses separate phrasing templates
and parameter pools, not a random split of copies of the same templates.
Training has no access to the development file. The corpus and implementation
share an author; these measurements are development evidence, not independent
release qualification. Existing job models and their evaluation sets are not
inputs to this experiment.

The `json-plan/v1` model interface has one tool with a required `plan` string.
That string holds the full JSON action list. Strict decoding rejects malformed
JSON, duplicate fields, extra outer calls and invalid plan lengths before action
validation. This avoids the native decoder's optional-argument and enum
selection branches. Legacy `direct/v1` bundles retain their original interface
for comparisons. `encode_plans.py` changes serialization while preserving every
query, canonical reference and ordering; it records their common digest.

`train.py` pins Needle 2 source commit
`571fcd68f48b6649c91f353eb91d0a0c9b9135ce`, checkpoint and tokenizer hashes,
and the schema and corpus digests. It applies quantization-aware LoRA for four
epochs by default (`--epochs` selects a recorded override; rank 16, alpha 32,
learning rate 0.0002, batch 16). Training renders the
reasoning and tool calls in Needle's native format and rejects truncated rows.
Prompt and answer are tokenized together: independently encoding the answer
adds a SentencePiece space token that the native answer grammar does not use.
Preflight asserts that the prompt tokens are unchanged and that the answer
begins directly with the `<think>` marker.
The plan recipe uses a 1,024-token dynamic KV window and a 640-token training
length after checking that all rows fit. The untuned comparison uses the same
checkpoint, output interface, quantization and context window.
Export uses the ordinary merged weights and then deployment quantization once.
The internal 5% training split monitors loss; it is not the development set.

`expand.py` adds varied titles, numbers, literal text, key chords, negative
instructions and common desktop sequences. It preserves the original
development file and checks exact query separation. This augmentation follows
development findings; negative framing vocabulary is shared between splits.
It must not be described as independent or blind qualification.

`evaluate.py` scores stock and tuned exports through the same deployed CPU
library. Raw full-call accuracy, actionable accuracy, negative-case accuracy,
admitted wrong actions, invalid replies and latency are separate measures.
The evaluator never connects to a desktop. Dataset hashes, per-request model
outputs, references and refusals are retained.

`xvfb_smoke.py` creates an authenticated private Xvfb/Openbox desktop and a
recording application. It checks observed typing, keys, pointer buttons,
scrolling, drag release, window operations, focus changes and graceful-close
delivery. `--model` additionally exercises natural-language execution using a
tuned bundle. The fixture exits and Xvfb is torn down afterwards.

```sh
python3 -B -m unittest discover -s tests -p test_desktop.py
python3 -B experiments/desktop-actions/prepare.py --out /abs/run/data-v1
python3 -B experiments/desktop-actions/expand.py --base /abs/run/data-v1 --out /abs/run/expanded
python3 -B experiments/desktop-actions/encode_plans.py --data /abs/run/expanded --out /abs/run/plans
python3 -B experiments/desktop-actions/train.py --src /abs/pinned-needle2-src --data /abs/run/plans/train.jsonl --out /abs/run/output --epochs 3 --max-len 640 --kv-window 1024
python3 -B experiments/desktop-actions/package.py --run /abs/run/output --out /abs/desktop-bundle
python3 -B experiments/desktop-actions/evaluate.py --data /abs/run/plans/dev.jsonl --model /abs/desktop-bundle --out /abs/run/tuned
python3 -B experiments/desktop-actions/xvfb_smoke.py --xdotool /abs/xdotool --out /abs/run/xvfb --model /abs/desktop-bundle
```
