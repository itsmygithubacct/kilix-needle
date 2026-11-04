# LFM2 command benchmark: upstream, V5 and weight revision .21

Measured on 2026-11-04 for `lfm2-kilix-commands-20261103-v21`. This compares
three Q8_0 models in the Kilix command role:

* **Upstream:** the unchanged official [Liquid AI LFM2-1.2B-Tool Q8_0 file](https://huggingface.co/LiquidAI/LFM2-1.2B-Tool-GGUF/blob/e7ec4149433c75d4bb53a0742ee1ebde7fcbd183/LFM2-1.2B-Tool-Q8_0.gguf).
  “Vanilla” here means no Kilix fine-tuning or direct weight editing; Liquid AI's
  Tool model is already specialized by its publisher.
* **Kilix V5:** the prior production model, already fine-tuned for Kilix,
  before our direct weight edits.
* **Kilix .21:** the selected version of those edits, based on V5, with no
  additional gradient training during weight repair.

The complete pinned metrics, checksums, per-case gain/loss IDs and timing
measurements are in [the benchmark record](lfm2-v21-benchmark.json).

## CPU confirmation: 208 cases

These 96 supported and 112 unsupported requests were frozen before `.21`
selection. All models receive the same Kilix prompt and tools, with the shipped
CPU runtime. The upstream run was added after selection; neither model weights
nor the evaluation prompt were changed for this comparison.

Raw correctness requires Kilix's native tool-call format, including a valid
empty call list for abstention. A prose-only refusal counts as an output-format
error here, even if its meaning is appropriate. Empty optional arguments and
unsupported argument values can also fail the Kilix schema. This measures
deployment compatibility as well as command accuracy, not general tool-use
ability under a separately adapted interface.

| Measure | Upstream Q8_0 | Kilix V5, pre-edit | Kilix .21 |
| --- | ---: | ---: | ---: |
| Correct raw responses in Kilix format | 39/208 (18.8%) | 182/208 (87.5%) | 193/208 (92.8%) |
| Correct supported proposals | 39/96 (40.6%) | 78/96 (81.2%) | 82/96 (85.4%) |
| Correct raw abstentions on unsupported requests | 0/112 (0.0%) | 104/112 (92.9%) | 111/112 (99.1%) |
| Raw false abstentions on supported requests | 0 | 4 | 0 |
| Raw runtime/schema errors | 101 | 8 | 1 |
| Correct outcomes after application admission | 59/208 (28.4%) | 151/208 (72.6%) | 154/208 (74.0%) |
| Correct supported proposals admitted | 11/96 (11.5%) | 39/96 (40.6%) | 42/96 (43.8%) |
| Incorrect actions admitted | 0 | 0 | 0 |

“Raw” scores the model's parsed proposal before application admission.
“Admitted” scores the calls left after whole-request validation, including
correct abstention on unsupported requests. Admission does not execute the
calls. Correct raw proposals can still be rejected by the application; its
accepted-command count is therefore shown separately. Error counters are
diagnostic and can overlap incorrect-response counts. False abstention counts
only well-formed empty tool calls; prose and schema failures appear as errors.

## GPU confirmation: the same 208 cases

| Measure | Upstream Q8_0 | Kilix V5, pre-edit | Kilix .21 |
| --- | ---: | ---: | ---: |
| Correct raw responses in Kilix format | 41/208 (19.7%) | 181/208 (87.0%) | 193/208 (92.8%) |
| Correct supported proposals | 41/96 (42.7%) | 77/96 (80.2%) | 82/96 (85.4%) |
| Correct raw abstentions on unsupported requests | 0/112 (0.0%) | 104/112 (92.9%) | 111/112 (99.1%) |
| Raw false abstentions on supported requests | 0 | 5 | 0 |
| Raw runtime/schema errors | 99 | 8 | 1 |
| Correct outcomes after application admission | 60/208 (28.8%) | 151/208 (72.6%) | 154/208 (74.0%) |
| Correct supported proposals admitted | 12/96 (12.5%) | 39/96 (40.6%) | 42/96 (43.8%) |
| Incorrect actions admitted | 0 | 0 | 0 |

## GPU development: 1,175 cases

These 635 supported and 540 unsupported requests were used during weight
editing and candidate selection. Treat them as development results; the
confirmation comparison above is the stronger check of generalization.

| Measure | Upstream Q8_0 | Kilix V5, pre-edit | Kilix .21 |
| --- | ---: | ---: | ---: |
| Correct raw responses in Kilix format | 301/1175 (25.6%) | 943/1175 (80.3%) | 1128/1175 (96.0%) |
| Correct supported proposals | 301/635 (47.4%) | 563/635 (88.7%) | 589/635 (92.8%) |
| Correct raw abstentions on unsupported requests | 0/540 (0.0%) | 380/540 (70.4%) | 539/540 (99.8%) |
| Raw false abstentions on supported requests | 0 | 31 | 0 |
| Raw runtime/schema errors | 399 | 19 | 4 |
| Correct outcomes after application admission | 665/1175 (56.6%) | 1035/1175 (88.1%) | 1061/1175 (90.3%) |
| Correct supported proposals admitted | 260/635 (40.9%) | 497/635 (78.3%) | 522/635 (82.2%) |
| Incorrect actions admitted | 0 | 0 | 0 |

## Paired changes for .21

A gain is a case `.21` gets correct that the baseline misses. A loss is a
previously correct baseline case that `.21` misses. All paired comparisons
verify identical queries, expected answers, scoring code and runtime settings.

| Suite / device | Baseline | Raw gains / losses | Admitted gains / losses |
| --- | --- | ---: | ---: |
| Confirmation / CPU | Upstream | 158 / 4 | 99 / 4 |
| Confirmation / CPU | V5 | 11 / 0 | 3 / 0 |
| Confirmation / GPU | Upstream | 156 / 4 | 98 / 4 |
| Confirmation / GPU | V5 | 12 / 0 | 3 / 0 |
| Development / GPU | Upstream | 840 / 13 | 408 / 12 |
| Development / GPU | V5 | 185 / 0 | 26 / 0 |

The four confirmation losses against upstream repeat one literal-message
pattern with four directories: `.21` removes a final period from text that
must be forwarded verbatim. V5 also misses these cases. Application admission
refuses the altered text. Thus `.21` improves the aggregate score but does not
preserve every upstream-correct answer; these losses remain in the totals.

## CPU latency and memory

Each model ran the same fixed 24-case timing set twice, with a fresh server per
run. Model order was upstream, V5, .21, then .21, V5, upstream. These timing
runs started after both accuracy jobs exited.

| Measure | Upstream Q8_0 | Kilix V5, pre-edit | Kilix .21 |
| --- | ---: | ---: | ---: |
| Median seconds, run 1 / run 2 | 1.530 / 1.517 | 0.872 / 0.863 | 0.376 / 0.378 |
| p95 seconds, run 1 / run 2 | 4.722 / 4.724 | 2.133 / 2.133 | 2.085 / 2.096 |
| Peak RSS GiB, run 1 / run 2 | 1.306 / 1.305 | 1.306 / 1.305 | 1.438 / 1.438 |
| GGUF bytes | 1,246,252,768 | 1,246,252,640 | 1,388,859,040 |

Latency includes different output lengths: a correct empty response can be
shorter than an incorrect tool call. These numbers are request latency on this
small workload, not a general tokens-per-second speedup. Peak RSS measures the
server process, not the entire desktop. The larger `.21` GGUF includes the
explicit output projection introduced earlier in the edit lineage.

Measured background CPU consumption stayed below 0.052 of one core during
these runs, with no busy external process detected. This sampling does not
capture processes born or exited within the measurement interval.

## Reproduction and limits

* CPU: Intel Core i7-10700F, 16 logical CPUs. GPU: NVIDIA GeForce RTX 3070,
  using Vulkan. Linux x86-64 throughout.
* CPU llama.cpp revision: `620d2225cc436ab0b001e3200e590e3b286ef9ab`.
  GPU revision: `8642541b255bfb5dc39cd351a178be09013a55d6`.
* Temperature 0, seed 17, context 8,192, maximum 512 output tokens, batch 256.
  Microbatch 16 on CPU and 256 on GPU. No constrained output grammar.
* Prompt, tools, parser and application admission are frozen at Needle commit
  `a7b6791e369adea9228acc168e75941f9a00343e`. Each model uses identical settings
  within a device comparison. The final rc1 admission source reproduced the
  stored V5 and `.21` decisions; later source changes add delivery support.
* GGUF chat-template, token vocabulary, token types, merges and BOS/EOS
  metadata digests match across all three models; the JSON records these
  checks in addition to the shared prompt and harness hashes.
* The V5 and `.21` accuracy results reuse their verified frozen runs; the
  upstream model runs the identical suites through byte-identical harness code.
  All timing results above are new matched runs.
* The full 1,175-case upstream development comparison was run on GPU. The CPU
  three-way comparison uses the 208-case confirmation set; no upstream CPU
  development score is inferred from GPU results.
* Cases are synthetic and editor-authored, not an independent human or general
  model benchmark. CPU/GPU repetitions are not independent samples. Development
  cases informed the edits. The upstream comparison is post-selection.
* No model-proposed application commands were executed. Accuracy-job timings
  are excluded because those jobs could overlap; timing jobs ran serially.
* Source, suite and raw-result hashes are recorded in the JSON. Raw research
  artifacts stay outside the publication repository; the JSON contains
  aggregates and paired case IDs without host paths or process metadata.

## Weight identities

| Model | GGUF SHA-256 |
| --- | --- |
| Upstream LFM2-1.2B-Tool Q8_0 | `bf035d5876eb8ef0b1a87cf0cf30d9717e4cc628d483c5b49b342603d5f2ee67` |
| Kilix V5, before direct weight edits | `96f1eabf0a2b99d0f96f9868ece3fdb284718d1db6e17c9122c6a507419adf65` |
| Kilix weight revision .21 | `c6b065d617b95745bd207c4f9c9b605a8af922f5ebb5d7ff1d4c686d9d1c8f0e` |
