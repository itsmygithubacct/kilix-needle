"""Pinned Needle 2 QAT LoRA training; never reads the development set."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

CHECKPOINT = "4b0a972d163ffc7678fb3c36bace508114872e9d2ce9e10f225825752d3795bc"
RECIPE = dict(epochs=4, batch_size=16, lr=2e-4, lora_rank=16,
              lora_alpha=32.0, max_len=1536, val_split=0.05)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1048576), b""):
            h.update(b)
    return h.hexdigest()


def runtime_encode(tokenizer, example, max_len):
    """Keep the answer boundary identical to native constrained decoding.

    Upstream encodes prompt and target separately. SentencePiece adds a dummy
    leading space to the second chunk, which the native answer grammar masks
    out. Encode the continuous sequence instead, and assert prefix stability.
    """
    from needle.model.finetune import render_example
    from needle.model.tokenizer import BOS_ID, EOS_ID, PAD_ID, THINK_START_ID
    prompt, target = render_example(example)
    prefix = tokenizer.encode(prompt)
    full = tokenizer.encode(prompt + target)
    if full[:len(prefix)] != prefix or full[len(prefix)] != THINK_START_ID:
        raise ValueError("tokenizer changed the native prompt/answer boundary")
    ids = [BOS_ID] + full + [EOS_ID]
    mask = [0.0] * (len(prefix) + 1) + [1.0] * (len(full) - len(prefix) + 1)
    if len(ids) > max_len:
        raise ValueError("training sequence would be truncated")
    pad = max_len - len(ids)
    return ids + [PAD_ID] * pad, mask + [0.0] * pad


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--src", type=Path, required=True)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seed", type=int, default=19)
    p.add_argument("--epochs", type=int, choices=range(1, 21), default=RECIPE["epochs"])
    p.add_argument("--kv-window", type=int, choices=[256, 512, 1024], default=256)
    p.add_argument("--max-len", type=int, choices=[512, 640, 768, 1024, 1536], default=RECIPE["max_len"])
    p.add_argument("--check-only", action="store_true")
    args = p.parse_args()
    recipe = {**RECIPE, "epochs": args.epochs, "max_len": args.max_len}
    src, data, out = args.src.resolve(), args.data.resolve(), args.out.resolve()
    pins = json.loads(Path(__file__).with_name("source-pin.json").read_text())
    for rel, expected in pins["files"].items():
        if sha(src / rel) != expected:
            raise ValueError("source/tokenizer mismatch: " + rel)
    checkpoint = src / "checkpoints/needle2.pkl"
    if sha(checkpoint) != CHECKPOINT:
        raise ValueError("base checkpoint hash mismatch")
    manifest = json.loads(data.with_name("manifest.json").read_text())
    if sha(data) != manifest["files"]["train.jsonl"]["sha256"]:
        raise ValueError("training data hash mismatch")
    os.environ.update(HF_HUB_OFFLINE="1", NEEDLE_TELEMETRY="0", DO_NOT_TRACK="1",
                      XLA_PYTHON_CLIENT_PREALLOCATE="false")
    sys.path.insert(0, str(src))
    import numpy as np
    from needle.model import finetune
    from needle.model.tokenizer import SANTokenizer
    tokenizer = SANTokenizer(str(src / "needle/model/tokenizer.model"))
    lengths = []
    for line in data.read_text().splitlines():
        row = json.loads(line)
        prompt, target = finetune.render_example(row)
        # Exercise exactly the encoding later consumed by the training loop.
        runtime_encode(tokenizer, row, recipe["max_len"])
        lengths.append(len(tokenizer.encode(prompt + target)) + 2)
    if len(lengths) != manifest["rows"]["train"] or max(lengths) > recipe["max_len"]:
        raise ValueError(f"row/length mismatch: {len(lengths)} rows, max {max(lengths)} tokens; no truncation allowed")
    report = dict(status="preflight-pass", recipe=recipe, seed=args.seed, source_commit=pins["source_commit"],
                  source_files=pins["files"], checkpoint_sha256=CHECKPOINT, data_sha256=sha(data),
                  schema_sha256=manifest["schema_sha256"], wrapper_sha256=sha(__file__),
                  codec=manifest.get("codec", "direct/v1"), kv_window=args.kv_window,
                  rows=len(lengths), max_tokens=max(lengths), min_tokens=min(lengths),
                  truncated_rows=0, development_access=False, independent_qualification=False,
                  answer_encoding="continuous prompt+target; no second SentencePiece dummy prefix")
    if args.check_only:
        print(json.dumps(report, indent=2))
        return
    if out.exists() and any(out.iterdir()):
        raise ValueError("output directory must be empty")
    out.mkdir(parents=True, exist_ok=True)
    from needle.model.quantize import cq_ste_mixed_params, parse_bits_map
    from needle.model.run import load_checkpoint
    from needle.model import architecture
    import jax
    if jax.default_backend() != "gpu":
        raise ValueError("training requires the selected GPU backend")
    params, config = load_checkpoint(str(checkpoint))
    del params
    bits_map, default_bits = parse_bits_map(config.weight_bits)
    report.update(status="running", devices=[str(d) for d in jax.devices()], weight_bits=config.weight_bits,
                  started_unix=time.time())
    report_path = out / "run-manifest.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    plain_merge = finetune.merge_lora
    plain_init = finetune.init_lora
    plain_encode = finetune._encode
    plain_window = architecture.effective_kv_window
    finetune._encode = runtime_encode
    finetune.merge_lora = lambda params, lora, scale: cq_ste_mixed_params(plain_merge(params, lora, scale), bits_map, default_bits)
    finetune.init_lora = lambda params, paths, rank, key: plain_init(params, paths, rank, jax.random.PRNGKey(args.seed))
    np.random.seed(args.seed)
    start = time.monotonic()
    try:
        finetune.finetune_local(argparse.Namespace(**recipe, jsonl_path=str(data), checkpoint=str(checkpoint),
                              generate=0, model=None, workers=1, checkpoint_dir=str(out), out=str(out / "lora.pkl")))
        # Export quantizes once, from the ordinary merged float weights.
        finetune.merge_lora = plain_merge
        # The static tool prefix is retained separately. A full plan string
        # needs a longer dynamic cache than independent short tool arguments.
        architecture.effective_kv_window = lambda config: args.kv_window
        finetune.build_main(argparse.Namespace(checkpoint=str(checkpoint), lora=str(out / "lora.pkl"),
                                              out=str(out / "tuned.cact"), bits=None, upload=False))
        report.update(status="complete", export_sha256=sha(out / "tuned.cact"), adapter_sha256=sha(out / "lora.pkl"))
    except BaseException as e:
        report.update(status="failed", error=f"{type(e).__name__}: {e}")
        raise
    finally:
        finetune.merge_lora = plain_merge
        finetune.init_lora = plain_init
        finetune._encode = plain_encode
        architecture.effective_kv_window = plain_window
        report["wall_seconds"] = time.monotonic() - start
        report_path.write_text(json.dumps(report, indent=2) + "\n")
    print("TRAINING_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
