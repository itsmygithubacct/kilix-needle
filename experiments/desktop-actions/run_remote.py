"""Train on one explicitly selected Vast rental; recover verified artifacts.

Uses the existing project launcher and an already running cost guard. The
caller owns provisioning and final teardown so failed transfers can recover.
Only synthetic training data, source, and licensed model assets are uploaded.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--src", type=Path, required=True)
    p.add_argument("--instance", required=True)
    p.add_argument("--remote-dir", default="/workspace/desktop")
    p.add_argument("--reuse-environment", action="store_true")
    p.add_argument("--epochs", type=int, choices=range(1, 21), default=4)
    p.add_argument("--kv-window", type=int, choices=[256, 512, 1024], default=256)
    p.add_argument("--max-len", type=int, choices=[512, 640, 768, 1024, 1536], default=1536)
    args = p.parse_args()
    if not re.fullmatch(r"/workspace/[A-Za-z0-9_-]+", args.remote_dir):
        raise ValueError("remote directory must be one direct /workspace child")
    root, code = args.root.resolve(), Path(__file__).resolve().parent
    launcher = root / "launcher/vast"
    log = root / "remote-driver.log"
    def command(*words, timeout=1800, capture=False):
        argv = [sys.executable, "-B", str(launcher), "--instance", args.instance, *map(str, words)]
        print("STEP", words[0], time.strftime("%H:%M:%S"), flush=True)
        if capture:
            return subprocess.run(argv, check=True, text=True, capture_output=True, timeout=timeout).stdout
        with log.open("a") as stream:
            return subprocess.run(argv, check=True, stdout=stream, stderr=stream, timeout=timeout)
    command("wait", "--timeout", "600", timeout=650)
    command("run", "--", "mkdir", "-p", args.remote_dir + "/data", "/workspace/needle-src")
    if not args.reuse_environment:
        command("sync-up", str(args.src.resolve()) + "/", "/workspace/needle-src/", "--exclude", ".git", "--exclude", "__pycache__")
    for name in ("train.py", "source-pin.json"):
        command("upload", code / name, args.remote_dir + "/" + name)
    # Development stays local: training has no path to it.
    for name in ("train.jsonl", "manifest.json"):
        command("upload", root / "data-v1" / name, args.remote_dir + "/data/" + name)
    command("upload", root / "requirements-train-cuda.lock", args.remote_dir + "/requirements.lock")
    setup = r'''set -eu
curl -LsSf https://astral.sh/uv/0.12.3/install.sh -o /workspace/install-uv.sh
sh /workspace/install-uv.sh >/workspace/uv-install.log 2>&1
/root/.local/bin/uv venv -q -p 3.12 /workspace/env
/root/.local/bin/uv pip install -q --require-hashes --python /workspace/env/bin/python -r /workspace/desktop/requirements.lock
/workspace/env/bin/python -c 'import jax; print(jax.devices()); assert jax.default_backend()=="gpu"'
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
/workspace/env/bin/python -m pip freeze > /workspace/desktop/environment.txt || /root/.local/bin/uv pip freeze --python /workspace/env/bin/python > /workspace/desktop/environment.txt
'''
    if args.reuse_environment:
        setup = """set -eu
/workspace/env/bin/python -c 'import jax; print(jax.devices()); assert jax.default_backend()=="gpu"'
/root/.local/bin/uv pip freeze --python /workspace/env/bin/python > /workspace/desktop/environment.txt
"""
    command("run", "--", "bash", "-lc", setup.replace("/workspace/desktop", args.remote_dir), timeout=2400)
    run = r'''set -eu
cd /workspace/desktop
export JAX_PLATFORMS=cuda XLA_PYTHON_CLIENT_PREALLOCATE=false HF_HUB_OFFLINE=1 NEEDLE_TELEMETRY=0 DO_NOT_TRACK=1
/workspace/env/bin/python -u train.py --src /workspace/needle-src --data data/train.jsonl --out output --check-only > preflight.json
/workspace/env/bin/python -u train.py --src /workspace/needle-src --data data/train.jsonl --out output > train.log 2>&1
'''
    failure = None
    try:
        run = run.replace("/workspace/desktop", args.remote_dir).replace("--out output", f"--out output --epochs {args.epochs} --kv-window {args.kv_window} --max-len {args.max_len}")
        command("run", "--", "bash", "-lc", run, timeout=7200)
    except Exception as exc:
        failure = repr(exc)
        print("TRAINING COMMAND FAILED; collecting evidence", failure, flush=True)
    names = ["train.log", "preflight.json", "environment.txt", "output/run-manifest.json", "output/lora.pkl", "output/tuned.cact"]
    remote = args.remote_dir + "/"
    # Bound the hash script to this exact artifact list; no private directories.
    script = "import pathlib,hashlib,json; names=" + repr(names) + "; print(json.dumps({n:hashlib.sha256(pathlib.Path(n).read_bytes()).hexdigest() for n in names if pathlib.Path(n).is_file()}))"
    hashes = json.loads(command("run", "--", "bash", "-lc", "cd " + args.remote_dir + " && /usr/bin/python3 -c " + __import__("shlex").quote(script), capture=True, timeout=120))
    dest = root / "qat-1"; dest.mkdir(exist_ok=True)
    (dest / "remote-hashes.json").write_text(json.dumps(dict(instance=args.instance, hashes=hashes, failure=failure), indent=2) + "\n")
    for name, digest in hashes.items():
        path = dest / name; path.parent.mkdir(exist_ok=True)
        for attempt in range(3):
            try:
                command("download", remote + name, path, timeout=300)
            except Exception:
                if not path.exists() or sha(path) != digest:
                    if attempt == 2:
                        raise
                    continue
            if sha(path) == digest:
                print("VERIFIED", name, digest, flush=True)
                break
        else:
            raise ValueError("unverified download: " + name)
    if failure or "output/tuned.cact" not in hashes:
        raise RuntimeError("training did not complete; recovered available evidence")
    print("RESULTS_RECOVERED", flush=True)


if __name__ == "__main__":
    main()
