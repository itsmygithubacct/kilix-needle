"""Explicit, proposal-only system normalizer model profile.

This profile is independent of gated execution-model selection. Its weights and
library are sealed and checked before use; an invalid profile is an error.
"""
from __future__ import annotations

from contextlib import ExitStack
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import asset
from libengine import LibEngine, LibEngineError
import system_job

SCHEMA = "kilix-needle-system-normalizer/v1"
ROLE = "proposal-only"


class SystemModelError(RuntimeError):
    """The configured system normalizer is missing or invalid."""


def profile_path() -> Path:
    override = os.environ.get("KILIX_NEEDLE_SYSTEM_PROFILE")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise SystemModelError("KILIX_NEEDLE_SYSTEM_PROFILE must be absolute")
        return path
    data = os.environ.get("XDG_DATA_HOME")
    if data:
        root = Path(data)
        if not root.is_absolute():
            raise SystemModelError("XDG_DATA_HOME must be absolute")
    else:
        root = Path.home() / ".local/share"
    return root / "kilix-needle/system-normalizer/profile.json"


def _digest_ok(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _managed_path(kind: str, digest: str) -> Path:
    return profile_path().parent / "objects" / digest / ("weights.cact" if kind == "weights" else "libneedle.so")


def _write_image(image: asset.EngineImage, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            size = os.fstat(image.fd).st_size
            for offset in range(0, size, 1 << 20):
                out.write(os.pread(image.fd, min(1 << 20, size-offset), offset))
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _entry(kind: str, image: asset.EngineImage) -> dict:
    return {"path": str(_managed_path(kind, image.sha256)), "sha256": image.sha256,
            "size": os.fstat(image.fd).st_size}


def configure(weights: str | os.PathLike, library: str | os.PathLike,
              expected_sha256: str) -> dict:
    """Validate a candidate, then atomically make it the system default.

    `expected_sha256` is the source manifest's digest for the weights. The
    library must match the upstream pin. A failed worker load preserves the
    previous profile.
    """
    if not _digest_ok(expected_sha256):
        raise SystemModelError("expected weights SHA-256 must be 64 lowercase hex digits")
    try:
        with ExitStack() as stack:
            weight_size = os.stat(weights, follow_symlinks=False).st_size
            weight_image = stack.enter_context(asset.load_verified(weights, expected_sha256, weight_size))
            library_image = stack.enter_context(asset.library_from_file(os.fspath(library)))
            with LibEngine(library_image, system_job.TOOLS, weight_image):
                pass  # needle_load and needle_init must both accept this candidate
            entries = {"weights": _entry("weights", weight_image),
                       "library": _entry("library", library_image)}
            for kind, image in (("weights", weight_image), ("library", library_image)):
                _write_image(image, Path(entries[kind]["path"]))
            # Verify the copies before publishing the config.
            for kind, item in entries.items():
                with asset.load_verified(item["path"], item["sha256"], item["size"]):
                    pass
    except (OSError, asset.AssetError) as error:
        raise SystemModelError(str(error)) from error

    config = {"schema": SCHEMA, "role": ROLE, **entries}
    path = profile_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(config, out, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return config


def _read_profile() -> dict:
    path = profile_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SystemModelError(f"system normalizer profile is missing: {path}") from error
    except (OSError, ValueError) as error:
        raise SystemModelError(f"cannot read system normalizer profile: {error}") from error
    if not isinstance(config, dict) or config.get("schema") != SCHEMA or config.get("role") != ROLE:
        raise SystemModelError("invalid system normalizer profile schema or role")
    for kind in ("weights", "library"):
        item = config.get(kind)
        if not isinstance(item, dict) or not _digest_ok(item.get("sha256")) or type(item.get("size")) is not int or item["size"] <= 0:
            raise SystemModelError(f"invalid system normalizer {kind} metadata")
        if item.get("path") != str(_managed_path(kind, item["sha256"])):
            raise SystemModelError(f"invalid system normalizer {kind} path")
    if (config["library"]["sha256"] != asset.PINNED_LIB_SHA256
            or config["library"]["size"] != asset.PINNED_LIB_BYTES):
        raise SystemModelError("system normalizer library does not match the upstream pin")
    return config


def status() -> dict:
    """Report profile metadata and verified file availability without inference."""
    path = None
    try:
        path = profile_path()
        config = _read_profile()
        verified = {}
        for kind in ("weights", "library"):
            item = config[kind]
            with asset.load_verified(item["path"], item["sha256"], item["size"]):
                verified[kind] = True
        return {"configured": True, "available": True, "profile": str(path),
                "schema": SCHEMA, "role": ROLE, "weights": config["weights"],
                "library": config["library"], "verified": verified}
    except (SystemModelError, asset.AssetError, OSError) as error:
        path_text = str(path) if path is not None else os.environ.get("KILIX_NEEDLE_SYSTEM_PROFILE", "")
        return {"configured": bool(path is not None and path.exists()),
                "available": False, "profile": path_text, "error": str(error)}


class Runtime:
    def __init__(self, engine: LibEngine, images: ExitStack, weights_sha256: str):
        self.engine = engine
        self.images = images
        self.label = f"system-normalizer tuned {weights_sha256[:12]}"

    def __enter__(self) -> "Runtime":
        try:
            self.engine.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def complete(self, text: str) -> dict:
        return self.engine.complete(text)

    def reset(self) -> None:
        self.engine.reset()

    def close(self) -> None:
        self.engine.close()
        self.images.close()


def open_runtime(args=None) -> Runtime:
    """Open only the configured system profile; start inference on entry."""
    if getattr(args, "engine", None) or os.environ.get("KILIX_NEEDLE_ENGINE"):
        raise SystemModelError("--engine and KILIX_NEEDLE_ENGINE cannot override the system normalizer profile")
    config = _read_profile()
    images = ExitStack()
    try:
        library = images.enter_context(asset.library_from_file(config["library"]["path"]))
        weights = config["weights"]
        weight_image = images.enter_context(asset.load_verified(weights["path"], weights["sha256"], weights["size"]))
        return Runtime(LibEngine(library, system_job.TOOLS, weight_image), images,
                       weights["sha256"])
    except BaseException:
        images.close()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the planning-only system normalizer profile")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show configured metadata and verified availability")
    setup = commands.add_parser("configure", help="validate and atomically set the profile")
    setup.add_argument("--weights", required=True, metavar="FILE")
    setup.add_argument("--library", required=True, metavar="FILE")
    setup.add_argument("--sha256", required=True, metavar="HASH")
    args = parser.parse_args(argv)
    try:
        result = (status() if args.command == "status" else
                  configure(args.weights, args.library, args.sha256))
    except (SystemModelError, LibEngineError, asset.AssetError, OSError) as error:
        print(f"system normalizer: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
