# Source files

Reference for [kilix-needle](../README.md).

## Files

| File | Role |
| --- | --- |
| `actions.py` | the fourteen actions, and the checks between the model's calls and anything that runs |
| `toolset.py` | the five-tool schema the tuned model sees, translated onto those actions |
| `kilix.py` | resolution against `kilix @ ls`, and the argv that performs each action |
| `agents.py` | the coding-session tool schema, request grammar and checks |
| `agents_kilix.py` | directory/session resolution and coding-session execution through Kilix |
| `system_job.py` | read-only OS tool schemas and complete-request checks |
| `system_collect.py` | bounded Linux collectors, observation records and safe rendering |
| `system_dispatch.py` | grammar-first routing, proposal-only fallback and system request history |
| `system_normalize.py` | model query normalization, schema validation and request grounding |
| `system_model.py` | verified weights/runtime profile for the tuned system normalizer |
| `jobs.py` | per-job evaluation sets and model-selection boundaries |
| `router.py` | the job router: grammar claims and the checked-in linear classifier |
| `router_train.py` | the router's deterministic trainer, check and threshold selection |
| `router-model/` | the router's weights and training-data manifest (provenance) |
| `ask.py` | `kilix-needle ask`, the routed default and `kilix_ask_plan` / `kilix_ask_act` |
| `engine.py` | the `needle` binary as a private loopback server |
| `libengine.py` | `libneedle.so` in a worker, for tuned weights |
| `asset.py` | admitting installed assets or pinned local copies; the first-use install |
| `tuning.py` | `kilix-needle tune` |
| `panes_exact.py` | canonical pane commands read without a model, then checked like any call |
| `state.py` | the one state directory, and moving older data-home state into it |
| `needle_cli.py` | the command line, prompt loop and runtime selection |
| `mcp_server.py` | `kilix-needle mcp` |
| `setup_surfaces.py` | `kilix-needle setup` |
| `evaluate.py` | end-to-end scoring |
