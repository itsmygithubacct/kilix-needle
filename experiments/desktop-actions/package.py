"""Package a verified training export for explicit desktop-model loading."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import desktop_cli
import desktop_job
import asset
import desktop_codec


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True, help="directory containing run-manifest.json and tuned.cact")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    report = json.loads((args.run / "run-manifest.json").read_text())
    codec = report.get("codec", desktop_codec.DIRECT)
    source = args.run / "tuned.cact"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if report["status"] != "complete" or report["export_sha256"] != digest or report["schema_sha256"] != desktop_cli.schema_sha(codec):
        raise ValueError("training report, export or tool schema failed verification")
    library = asset.installed_library()
    try:
        runtime_sha256 = library.sha256
    finally:
        library.close()
    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError("bundle directory must be empty")
    args.out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, args.out / "model.cact")
    manifest = dict(schema=desktop_job.VERSION, tools_sha256=desktop_cli.schema_sha(codec), sha256=digest, codec=codec,
                    runtime_sha256=runtime_sha256,
                    bytes=source.stat().st_size, base="Needle 2", source_commit=report["source_commit"],
                    checkpoint_sha256=report["checkpoint_sha256"], data_sha256=report["data_sha256"],
                    training_manifest_sha256=hashlib.sha256((args.run / "run-manifest.json").read_bytes()).hexdigest(),
                    answer_encoding=report.get("answer_encoding", "upstream separate prompt/target chunks"),
                    kv_window=report.get("kv_window", 256),
                    recipe=report["recipe"], seed=report["seed"], qualification="experimental; explicit selection only",
                    independent_qualification=False)
    (args.out / "model.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__": main()
