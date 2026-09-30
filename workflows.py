"""Read-only desktop readiness and explicit, licensed model acquisition."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

import asset
import state

SCHEMA = "kilix.workflows/v1"
MAX_JSON = 16_384
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class WorkflowError(RuntimeError):
    pass


def _read_json(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65_536:
            raise WorkflowError("model metadata must be a regular file of at most 64 KiB")
        data = os.read(fd, 65_537)
        if len(data) > 65_536:
            raise WorkflowError("model metadata exceeded 64 KiB")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise WorkflowError("model metadata must be an object")
        return value
    finally:
        os.close(fd)


def _verify(path: Path, digest: str, size: int | None = None) -> None:
    if not isinstance(digest, str) or not _SHA.fullmatch(digest):
        raise WorkflowError("invalid SHA-256 metadata")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WorkflowError(f"not a regular file: {path}")
        expected = info.st_size if size is None else size
        if info.st_size != expected:
            raise WorkflowError(f"file size does not match: {path}")
        hashed, total = hashlib.sha256(), 0
        while chunk := os.read(fd, 1 << 20):
            total += len(chunk)
            if total > expected:
                raise WorkflowError(f"file grew while being verified: {path}")
            hashed.update(chunk)
        if total != expected or hashed.hexdigest() != digest:
            raise WorkflowError(f"SHA-256 does not match: {path}")
    finally:
        os.close(fd)


def _readonly_store(lic):
    # ReceiptStore's ordinary constructor mkdirs/chmods. Retain the authority's
    # canonical root, bounded reads, parsing and coverage rules without writes.
    class ReadOnlyStore(lic.ReceiptStore):
        def __init__(self):
            self.root = Path(lic.receipt_store_root())

        def write(self, *_args, **_kwargs):
            raise WorkflowError("status cannot write licence receipts")

    return ReadOnlyStore()


def _content_root(root: str | None) -> Path:
    path = Path(asset.content_root(root))
    # Match Content's root refusal without its mkdir side effect.
    if os.path.lexists(path) and (path.is_symlink() or not path.is_dir()):
        raise WorkflowError(f"Content root must be a real directory: {path}")
    return path


def _model_status() -> tuple[list[dict], bool]:
    models, runtime = [], False
    selection = state.home() / "model.json"
    if os.path.lexists(selection):
        try:
            config = _read_json(selection)
            if config.get("schema") == "kilix-needle.selection/v2" and isinstance(config.get("jobs"), dict):
                jobs = config["jobs"]
            elif "schema" not in config and "weights" in config:
                jobs = {"panes": config}
            else:
                raise WorkflowError("unknown model selection format")
            # Apps and files use grammar/baseline paths, not tuned selections.
            for job in ("panes", "agents"):
                if job not in jobs:
                    continue
                library = os.environ.get("KILIX_NEEDLE_LIBRARY")
                runtime = runtime or not bool(library)
                row = {"kind": "selection", "job": job, "state": "ready", "error": None}
                try:
                    item = jobs[job]
                    if not isinstance(item, dict) or not isinstance(item.get("weights"), str):
                        raise WorkflowError("invalid selected model metadata")
                    path = Path(item["weights"])
                    if not path.is_absolute():
                        raise WorkflowError("selected weights path must be absolute")
                    _verify(path, item.get("sha256"))
                    if library:
                        _verify(Path(library), asset.PINNED_LIB_SHA256, asset.PINNED_LIB_BYTES)
                except (OSError, ValueError, WorkflowError) as error:
                    row.update(state="invalid", error=str(error))
                models.append(row)
        except (OSError, ValueError, WorkflowError) as error:
            models.append({"kind": "selection", "job": "unknown", "state": "invalid", "error": str(error)})
    override = os.environ.get("KILIX_NEEDLE_SYSTEM_PROFILE")
    profile = Path(override).expanduser() if override else state.home() / "system-normalizer" / "profile.json"
    legacy = False
    if not override and not os.path.lexists(profile.parent):
        # Observe legacy state without invoking state.place's relocation.
        profile = state._legacy("system-normalizer") / "profile.json"
        legacy = True
    if override or os.path.lexists(profile):
        row = {"kind": "system-profile", "job": "system", "state": "ready", "error": None}
        try:
            if not profile.is_absolute():
                raise WorkflowError("system profile path must be absolute")
            if legacy:
                info = profile.parent.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                    raise WorkflowError("legacy system profile directory cannot be adopted")
            config = _read_json(profile)
            if config.get("schema") != "kilix-needle-system-normalizer/v1" or config.get("role") != "proposal-only":
                raise WorkflowError("invalid system profile schema or role")
            for kind in ("weights", "library"):
                item = config.get(kind)
                if not isinstance(item, dict) or not isinstance(item.get("sha256"), str) or not _SHA.fullmatch(item["sha256"]) or type(item.get("size")) is not int or item["size"] <= 0:
                    raise WorkflowError(f"invalid system profile {kind} metadata")
                expected = profile.parent / "objects" / item["sha256"] / ("weights.cact" if kind == "weights" else "libneedle.so")
                if item.get("path") != str(expected):
                    raise WorkflowError(f"invalid system profile {kind} path")
                if kind == "library" and (item["sha256"] != asset.PINNED_LIB_SHA256 or item["size"] != asset.PINNED_LIB_BYTES):
                    raise WorkflowError("system profile library does not match the upstream pin")
                _verify(expected, item["sha256"], item["size"])
        except (OSError, ValueError, WorkflowError) as error:
            row.update(state="invalid", error=str(error))
        models.append(row)
    return models, runtime


def _inspect_asset(spec, root: Path, first_use, records, store) -> dict:
    destination = root / "assets" / spec.asset_id
    row = {"id": spec.asset_id, "version": spec.version, "path": str(destination), "state": "ready", "error": None}
    agreement = first_use.needs_agreement(spec, records=records, store=store)
    if not os.path.lexists(destination):
        row.update(state="agreement-required" if agreement else "missing",
                   error="current asset licence requires typed acceptance" if agreement else "asset is not installed")
        return row
    try:
        if destination.is_symlink() or destination.parent.is_symlink() or not destination.is_dir():
            raise WorkflowError("asset selection must be a real directory")
        expected = {item.path for item in spec.files}
        for item in spec.files:
            path = destination / item.path
            if any(parent.is_symlink() for parent in path.parents if parent != destination and destination in parent.parents):
                raise WorkflowError("asset member directory is a symlink")
            _verify(path, item.sha256, item.bytes)
        def unreadable(error):
            raise error

        for base, directories, files in os.walk(destination, onerror=unreadable):
            if any((Path(base) / name).is_symlink() for name in directories):
                raise WorkflowError("asset contains a symlink directory")
            for name in files:
                if str((Path(base) / name).relative_to(destination)) not in expected:
                    raise WorkflowError("asset contains an unexpected file")
    except (OSError, WorkflowError) as error:
        row.update(state="corrupt", error=str(error))
    if row["state"] == "ready" and agreement:
        row.update(state="agreement-required", error="current asset licence requires typed acceptance")
    return row


def status(root: str | None = None) -> dict:
    """No engine, acquisition, receipt writes, directory creation or migration."""
    models, runtime = _model_status()
    required = [asset.ASSET_ID] + ([asset.RUNTIME_ID] if runtime else [])
    assets, errors = [], []
    try:
        content, first_use, lic = asset._content()
        catalog = content.verified_packaged_catalog()
        records, store = lic.load_determined_records(), _readonly_store(lic)
        content_root = _content_root(root)
        for asset_id in required:
            try:
                assets.append(_inspect_asset(catalog.require_asset(asset_id), content_root, first_use, records, store))
            except Exception as error:
                assets.append({"id": asset_id, "version": None, "path": None, "state": "unavailable", "error": str(error)})
    except Exception as error:
        errors.append(str(error))
    if os.environ.get("KILIX_NEEDLE_ENGINE"):
        errors.append("KILIX_NEEDLE_ENGINE overrides are unsupported by desktop workflow setup; unset the override before enabling workflows")
    errors.extend(f"{row['id']}: {row['error']}" for row in assets if row["state"] != "ready")
    errors.extend(f"{row['kind']} {row['job']}: {row['error']}" for row in models if row["state"] != "ready")
    ready = len(assets) == len(required) and not errors
    result = {"schema": SCHEMA, "ready": ready, "status": "ready" if ready else "not-ready",
              "detail": "Required workflow model assets and selected models are verified." if ready else "Workflow model setup needs attention.",
              "required_assets": required, "assets": assets, "models": models, "errors": errors}
    if len(json.dumps(result, ensure_ascii=True).encode()) > MAX_JSON:
        result.update(ready=False, status="not-ready", detail="Workflow status exceeds its output limit.", assets=[], models=[], errors=["status output exceeded 16 KiB"])
    return result


def setup(root: str | None = None, *, stdin=None, stdout=None) -> dict:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if not (stdin.isatty() and stdout.isatty()):
        raise WorkflowError("run `kilix-needle workflows setup` at a terminal for typed licence acceptance")
    # One session owns licence presentation and every required asset. Content's
    # separate asset lock still guards staging/selection. Never wait for another
    # session or infer that a stale lock file means an active process.
    directory = Path(os.path.realpath(_content_root(root)))
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / ".needle-workflows-setup.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkflowError("workflow setup lock is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WorkflowError("workflow setup is already running for this Content root; finish or close that terminal before retrying") from error
        return _setup_locked(str(directory), stdin=stdin, stdout=stdout)
    finally:
        os.close(fd)


def _setup_locked(root: str, *, stdin, stdout) -> dict:
    before = status(root)
    invalid = [row["error"] for row in before["models"] if row["state"] != "ready"]
    invalid.extend(row["error"] for row in before["assets"] if row["state"] in ("corrupt", "unavailable"))
    if invalid or not before["assets"] or os.environ.get("KILIX_NEEDLE_ENGINE"):
        raise WorkflowError("; ".join(invalid or before["errors"]) + "; existing custom selections and corrupt assets were left unchanged")
    for row in before["assets"]:
        if row["state"] == "ready":
            continue
        if row["state"] == "agreement-required":
            asset.install(root, stdin=stdin, stdout=stdout, asset_id=row["id"])
        else:
            # Reuse current exact acceptance, with the same Content lock and
            # verified staging as first use. No extra consent or custom source.
            content, _first_use, lic = asset._content()
            spec = content.verified_packaged_catalog().require_asset(row["id"])
            store, records = lic.ReceiptStore.shared(), lic.load_determined_records()
            with tempfile.TemporaryDirectory(prefix="kilix-needle-workflows-") as scratch:
                notices = lic.load_determined_texts(Path(scratch) / "texts")
                content.Installer(asset.content_root(root)).ensure_upstream_asset(
                    spec, store=store, records=records, notices=notices,
                    report=lambda message: print(message, file=stdout))
    after = status(root)
    if not after["ready"]:
        raise WorkflowError("; ".join(after["errors"]) or after["detail"])
    print(after["detail"], file=stdout)
    return after


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kilix-needle workflows")
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("status", help="verify readiness without writes or downloads")
    probe.add_argument("--json", action="store_true")
    probe.add_argument("--root", help="absolute Content root (normally supplied by Kilix)")
    acquire = commands.add_parser("setup", help="interactively acquire missing licensed model assets")
    acquire.add_argument("--root", help="absolute Content root (normally supplied by Kilix)")
    args = parser.parse_args(argv)
    if args.command == "status":
        result = status(args.root)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True) if args.json else result["detail"] + ("\n" + "\n".join(result["errors"]) if result["errors"] else ""))
        return 0 if result["ready"] else 1
    try:
        setup(args.root)
        return 0
    except Exception as error:
        print(f"Workflow setup failed: {str(error)[:4096]}", file=sys.stderr)
        return 1
