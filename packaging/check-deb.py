#!/usr/bin/env python3
"""check-deb.py DEB : kilix-needle ships the runtime and not the command model."""

import subprocess
import sys
import tempfile
from pathlib import Path


def fail(message: str) -> None:
    print(f"check-deb: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: check-deb.py DEB")
    deb = Path(sys.argv[1])
    changelog = Path(__file__).resolve().parents[1] / "debian/changelog"
    version = subprocess.check_output(
        ["dpkg-parsechangelog", "-l", str(changelog), "-S", "Version"], text=True).strip()
    fields = subprocess.run(
        ["dpkg-deb", "-f", str(deb), "Package", "Version", "Architecture", "Depends"],
        check=True, capture_output=True, text=True).stdout
    for needle in (
        "Package: kilix-needle\n",
        f"Version: {version}\n",
        "Architecture: all\n",
    ):
        if needle not in fields:
            fail(f"unexpected fields:\n{fields}")
    for name in ("kilix-content", "kilix-ml", "kilix-transcript-clean", "kilix-tui-utils"):
        if name not in fields:
            fail(f"missing dependency {name}:\n{fields}")
    with tempfile.TemporaryDirectory(prefix="kilix-needle-deb.") as raw:
        root = Path(raw)
        subprocess.run(["dpkg-deb", "-x", str(deb), str(root)], check=True)
        share = root / "usr/share/kilix-needle"
        launcher = root / "usr/bin/kilix-needle"
        if not (share / "needle_cli.py").is_file() or not launcher.is_symlink():
            fail("command missing")
        if not (share / "router-grammar-only").is_file():
            fail("missing grammar-only router selection")
        forbidden_parts = {"third_party", "router-model", "tests", "experiments", "evals"}
        for path in root.rglob("*"):
            if forbidden_parts.intersection(path.parts):
                fail(f"excluded tree packaged: {path}")
            if path.name == "router.json" or path.suffix in {".onnx", ".gguf", ".safetensors"}:
                fail(f"weight packaged: {path}")
            if path.is_file() and not path.is_symlink() and path.stat().st_size > 1_000_000:
                fail(f"file over 1MB: {path} ({path.stat().st_size})")
        ran = subprocess.run(
            [str(launcher), "--help"], check=False, capture_output=True, text=True)
        if ran.returncode != 0 or "usage: kilix-needle" not in ran.stdout:
            fail(f"help failed ({ran.returncode}):\n{ran.stdout}\n{ran.stderr}")
    print("check-deb: passed")


if __name__ == "__main__":
    main()
