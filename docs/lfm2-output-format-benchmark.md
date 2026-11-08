# LFM2 benchmark: format, command correctness and admission

Analysis and prompt trials completed on 2026-11-04. The selected `.21` model
keeps its original validated prompt: every tested clarification lost at least
one previously correct development answer. Model weights, tools, parser,
sampling settings and application admission are unchanged in the final source.

The tables marked **validated prompt** separate the existing complete runs of
unchanged upstream LFM2-1.2B-Tool Q8_0, fine-tuned Kilix V5, and weight revision
`.21` into three measurement stages. Their original correctness scores are
unchanged. Valid returned replies establish syntax compliance; recorded parser
exceptions identify format failures. This is a reanalysis of retained evidence,
not a claim that the original inference was rerun.

The first experimental LFM2-only clarification appended:

> Return only the native tool-call block, without explanatory text or Markdown.
> When returning no tool calls, output exactly `<|tool_call_start|>[]<|tool_call_end|>`.

That experiment was freshly run on all three models, then rejected. Three
shorter wordings were also tested on `.21` development cases and rejected.
LFM3's prompt was never changed. The [earlier report](lfm2-v21-benchmark.md)
retains its original comparison and timing results. Separate stage metrics,
model identities, evidence hashes and every prompt trial are in
[the benchmark record](lfm2-output-format-benchmark.json).

## What each measurement means

1. **Format compliance:** the unchanged native parser accepts the complete
   response. A second row counts responses that also satisfy the tool schema.
   Valid syntax can contain an unknown tool or an invalid argument.
2. **Command correctness:** the ordered calls and literal arguments match the
   frozen reference. Conditional rows use only schema-valid responses; their
   denominators differ between models. The all-request rows count every
   format, schema and runtime failure as incorrect and prevent low coverage
   from disappearing behind a high conditional score.
3. **Application acceptance:** the unchanged whole-request validator accepts a
   correct supported proposal. Accepted proposals are never executed in this
   study. Correct refusals count toward the all-request outcome total, not
   toward the supported-command acceptance rate.

The validator can reject a wrong proposal on an unsupported request, yielding
a correct no-action outcome. That does not make the model's proposal correct.

Prose-only refusals still fail LFM2 format validation. Their meaning is not
independently graded here. A zero native-abstention score therefore does not
establish that a model never refuses in prose. Runtime failures also receive
no abstention credit. These measures describe Kilix compatibility and exact
command behavior; they are not a general model-intelligence ranking.

## Validated prompt: CPU, 208 confirmation cases

The set contains 96 supported and 112 unsupported requests. It was frozen
before `.21` weight selection but has already been evaluated in earlier work.

| Measure | Upstream Q8_0 | Kilix V5, pre-edit | Kilix .21 |
| --- | ---: | ---: | ---: |
| Native syntax accepted / all requests | 142/208 (68.3%) | 208/208 (100.0%) | 208/208 (100.0%) |
| Tool schema accepted / all requests | 107/208 (51.4%) | 200/208 (96.2%) | 207/208 (99.5%) |
| Format errors | 66 | 0 | 0 |
| Runtime errors | 0 | 0 | 0 |
| Schema errors after valid syntax | 35 | 8 | 1 |
| Exact commands or abstention / schema-valid responses | 39/107 (36.4%) | 182/200 (91.0%) | 193/207 (93.2%) |
| Exact supported commands / schema-valid supported responses | 39/73 (53.4%) | 78/96 (81.2%) | 82/96 (85.4%) |
| Correct abstentions / schema-valid unsupported responses | 0/34 (0.0%) | 104/104 (100.0%) | 111/111 (100.0%) |
| Exact model responses / all requests | 39/208 (18.8%) | 182/208 (87.5%) | 193/208 (92.8%) |
| Exact supported commands / all supported requests | 39/96 (40.6%) | 78/96 (81.2%) | 82/96 (85.4%) |
| Correct native abstentions / all unsupported requests | 0/112 (0.0%) | 104/112 (92.9%) | 111/112 (99.1%) |
| Well-formed false abstentions on supported requests | 0 | 4 | 0 |
| Correct supported proposals admitted / all supported requests | 11/96 (11.5%) | 39/96 (40.6%) | 42/96 (43.8%) |
| Correct supported proposals retained / correct supported proposals | 11/39 (28.2%) | 39/78 (50.0%) | 42/82 (51.2%) |
| Correct supported proposals rejected | 28 | 39 | 40 |
| Incorrect actions admitted | 0 | 0 | 0 |
| Correct outcomes after admission / all requests | 59/208 (28.4%) | 151/208 (72.6%) | 154/208 (74.0%) |

Under this validated prompt, `.21` gains 11 correct CPU responses over V5
with no paired losses. Against upstream, it still loses four correct cases
that repeat one literal-message pattern: `.21` drops the final period from
`Check the résumé notes; preserve  double spaces.` V5 also misses those cases.
The [original paired comparison](lfm2-v21-benchmark.md) records these limitations.

## Rejected explicit-marker trial: CPU effect

These are fresh runs under the longer instruction quoted above. They are
experimental results, not the final default behavior.

| Model | Original prompt, exact / 208 | Explicit prompt, exact / 208 | Newly correct | Previously correct lost |
| --- | ---: | ---: | ---: | ---: |
| Upstream Q8_0 | 39 | 45 | 6 | 0 |
| Kilix V5 | 182 | 180 | 2 | 4 |
| Kilix .21 | 193 | 190 | 2 | 5 |

Paired counts use identical requests and expected answers. Changing the prompt can
change command choices as well as formatting, even with identical model bytes.

Kilix V5 lost these previously correct cases: `sequence-confirm-004`, `sequence-confirm-019`, `sequence-confirm-036`, `sequence-confirm-052`.

Kilix .21 lost these previously correct cases: `sequence-confirm-021`, `sequence-confirm-052`, `sequence-confirm-097`, `sequence-confirm-111`, `sequence-confirm-208`.

| Measure | Upstream Q8_0 | Kilix V5, pre-edit | Kilix .21 |
| --- | ---: | ---: | ---: |
| Native syntax accepted / all requests | 141/208 (67.8%) | 208/208 (100.0%) | 206/208 (99.0%) |
| Tool schema accepted / all requests | 114/208 (54.8%) | 200/208 (96.2%) | 205/208 (98.6%) |
| Exact commands or abstention / schema-valid responses | 45/114 (39.5%) | 180/200 (90.0%) | 190/205 (92.7%) |
| Exact model responses / all requests | 45/208 (21.6%) | 180/208 (86.5%) | 190/208 (91.3%) |
| Correct supported proposals admitted / all supported requests | 15/96 (15.6%) | 36/96 (37.5%) | 41/96 (42.7%) |
| Incorrect actions admitted | 0 | 0 | 0 |

## Why the prompt clarification was not promoted

The preservation rule required every previously correct `.21` raw and admitted
answer to remain correct, with no increase in errors or false abstentions. The
original GPU development result is 1,128/1,175. All four trials changed only the
LFM2 instruction and used the same 1,175 cases and model bytes.

| Wording variant | Exact / 1,175 | Raw gains | Raw losses | Admitted gains | Admitted losses |
| --- | ---: | ---: | ---: | ---: | ---: |
| explicit-markers | 1020 | 5 | 113 | 5 | 36 |
| minimal-empty | 1125 | 2 | 5 | 2 | 3 |
| empty-tool-list | 1128 | 1 | 1 | 1 | 0 |
| empty-list-of-tools | 1125 | 0 | 3 | 0 | 1 |

* **explicit-markers**: Append: Return only the native tool-call block, without explanatory text or Markdown. When returning no tool calls, output exactly <|tool_call_start|>[]<|tool_call_end|>.
* **minimal-empty**: Replace return no tool calls. with return an empty native tool-call list ([]).
* **empty-tool-list**: Replace return no tool calls. with return an empty tool-call list.
* **empty-list-of-tools**: Replace return no tool calls. with return an empty list of tool calls.

One shorter wording tied the total, but exchanged a previously correct answer
for a different gain. It did not pass the preservation rule. These results show
prompt sensitivity; they do not isolate special delimiter tokens as the cause.
The original prompt was restored, and no new weight version was created.

## Validated prompt: GPU, the same 208 confirmation cases

| Measure | Upstream Q8_0 | Kilix V5, pre-edit | Kilix .21 |
| --- | ---: | ---: | ---: |
| Native syntax accepted / all requests | 142/208 (68.3%) | 208/208 (100.0%) | 208/208 (100.0%) |
| Tool schema accepted / all requests | 109/208 (52.4%) | 200/208 (96.2%) | 207/208 (99.5%) |
| Format errors | 66 | 0 | 0 |
| Runtime errors | 0 | 0 | 0 |
| Schema errors after valid syntax | 33 | 8 | 1 |
| Exact commands or abstention / schema-valid responses | 41/109 (37.6%) | 181/200 (90.5%) | 193/207 (93.2%) |
| Exact supported commands / schema-valid supported responses | 41/76 (53.9%) | 77/96 (80.2%) | 82/96 (85.4%) |
| Correct abstentions / schema-valid unsupported responses | 0/33 (0.0%) | 104/104 (100.0%) | 111/111 (100.0%) |
| Exact model responses / all requests | 41/208 (19.7%) | 181/208 (87.0%) | 193/208 (92.8%) |
| Exact supported commands / all supported requests | 41/96 (42.7%) | 77/96 (80.2%) | 82/96 (85.4%) |
| Correct native abstentions / all unsupported requests | 0/112 (0.0%) | 104/112 (92.9%) | 111/112 (99.1%) |
| Well-formed false abstentions on supported requests | 0 | 5 | 0 |
| Correct supported proposals admitted / all supported requests | 12/96 (12.5%) | 39/96 (40.6%) | 42/96 (43.8%) |
| Correct supported proposals retained / correct supported proposals | 12/41 (29.3%) | 39/77 (50.6%) | 42/82 (51.2%) |
| Correct supported proposals rejected | 29 | 38 | 40 |
| Incorrect actions admitted | 0 | 0 | 0 |
| Correct outcomes after admission / all requests | 60/208 (28.8%) | 151/208 (72.6%) | 154/208 (74.0%) |

## Validated prompt: GPU, 1,175 development cases

These 635 supported and 540 unsupported cases informed the weight-edit study.
They are development evidence.

| Measure | Upstream Q8_0 | Kilix V5, pre-edit | Kilix .21 |
| --- | ---: | ---: | ---: |
| Native syntax accepted / all requests | 1025/1175 (87.2%) | 1171/1175 (99.7%) | 1171/1175 (99.7%) |
| Tool schema accepted / all requests | 776/1175 (66.0%) | 1156/1175 (98.4%) | 1171/1175 (99.7%) |
| Format errors | 150 | 4 | 4 |
| Runtime errors | 0 | 0 | 0 |
| Schema errors after valid syntax | 249 | 15 | 0 |
| Exact commands or abstention / schema-valid responses | 301/776 (38.8%) | 943/1156 (81.6%) | 1128/1171 (96.3%) |
| Exact supported commands / schema-valid supported responses | 301/474 (63.5%) | 563/633 (88.9%) | 589/632 (93.2%) |
| Correct abstentions / schema-valid unsupported responses | 0/302 (0.0%) | 380/523 (72.7%) | 539/539 (100.0%) |
| Exact model responses / all requests | 301/1175 (25.6%) | 943/1175 (80.3%) | 1128/1175 (96.0%) |
| Exact supported commands / all supported requests | 301/635 (47.4%) | 563/635 (88.7%) | 589/635 (92.8%) |
| Correct native abstentions / all unsupported requests | 0/540 (0.0%) | 380/540 (70.4%) | 539/540 (99.8%) |
| Well-formed false abstentions on supported requests | 0 | 31 | 0 |
| Correct supported proposals admitted / all supported requests | 260/635 (40.9%) | 497/635 (78.3%) | 522/635 (82.2%) |
| Correct supported proposals retained / correct supported proposals | 260/301 (86.4%) | 497/563 (88.3%) | 522/589 (88.6%) |
| Correct supported proposals rejected | 41 | 66 | 67 |
| Incorrect actions admitted | 0 | 0 | 0 |
| Correct outcomes after admission / all requests | 665/1175 (56.6%) | 1035/1175 (88.1%) | 1061/1175 (90.3%) |

## Experimental paired model comparison under the rejected explicit prompt

| Device / suite | Baseline | Layer | .21 gains | .21 losses |
| --- | --- | --- | ---: | ---: |
| CPU / sequence-confirmation | Upstream Q8_0 | raw | 149 | 4 |
| CPU / sequence-confirmation | Upstream Q8_0 | admitted | 95 | 6 |
| CPU / sequence-confirmation | Kilix V5 | raw | 13 | 3 |
| CPU / sequence-confirmation | Kilix V5 | admitted | 6 | 3 |
| GPU / sequence-confirmation | Upstream Q8_0 | raw | 150 | 4 |
| GPU / sequence-confirmation | Upstream Q8_0 | admitted | 95 | 6 |
| GPU / sequence-confirmation | Kilix V5 | raw | 15 | 3 |
| GPU / sequence-confirmation | Kilix V5 | admitted | 7 | 3 |
| GPU / development | Upstream Q8_0 | raw | 720 | 14 |
| GPU / development | Upstream Q8_0 | admitted | 373 | 37 |
| GPU / development | Kilix V5 | raw | 97 | 36 |
| GPU / development | Kilix V5 | admitted | 25 | 30 |

## Reproduction and limits

* Validated original inference source: Needle `a7b6791e369adea9228acc168e75941f9a00343e`.
* Explicit-marker experiment source: `2ee4fcb6b0dbffa4c430e8f28c876687781d8866`; original prompt restored at `5d9f753973aa8d78fb51e4e89c581481e0d6c970`. Each scenario uses the same frozen source for every model.
* CPU runtime: `620d2225cc436ab0b001e3200e590e3b286ef9ab`; GPU Vulkan runtime: `8642541b255bfb5dc39cd351a178be09013a55d6`.
* Hardware: Intel Core i7-10700F and NVIDIA GeForce RTX 3070.
* Greedy generation, seed 17, context 8,192, maximum 512 output tokens;
  microbatch 16 on CPU and 256 on GPU. No generation grammar is injected.
* Exact model bytes and tokenizer metadata match the preceding comparison.
  The `.21` checksum and published model archive are unchanged.
* All nine explicit-marker trial results were regenerated. The three shorter
  wording trials each ran `.21` GPU development. Raw outputs, including format
  failures, are retained locally with frozen scripts and cases.
* New CPU and GPU accuracy queues may overlap. Their timings are not reported
  as performance measurements. Earlier timing results apply to the validated prompt.
* CPU and GPU repetitions reuse cases and are not independent samples.
  Synthetic cases were authored within this project; this is not a blind human
  evaluation. No live application task completion is claimed.
