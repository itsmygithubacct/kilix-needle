"""Bounded immutable-prefix reads of explicit regular-file log sources."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import json
import os
import select
import stat
import subprocess
import time
import tempfile
from .normalize import stable_id
from .schema import DEFAULT_MAX_BYTES, RECORD_SCHEMA
from .terminal import plain_lines


class SourceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _readers():
    """Load the pinned adapter by path, independent of installed packages."""
    adapter = Path(__file__).resolve().parents[1] / "third_party" / "kilix-tui-utils" / "src" / "kilix_rollout" / "records.py"
    if not adapter.is_file():
        raise SourceError("adapter_unavailable", "pinned kilix_rollout.records is unavailable")
    try:
        spec = importlib.util.spec_from_file_location("_needle_pinned_rollout_records", adapter)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.adapt_record
    except (ImportError, OSError, AttributeError) as exc:
        raise SourceError("adapter_unavailable", "pinned kilix_rollout.records is unavailable") from exc


def _open_bound(path: str, binding: dict) -> tuple[int, int]:
    """Open every discovered component beneath the resolved root without links."""
    if not isinstance(binding, dict):
        raise SourceError("source_binding_invalid", "discovered source has no root binding")
    root = binding.get("root")
    relative = binding.get("relative_path")
    if not isinstance(root, str) or not isinstance(relative, str) or not relative:
        raise SourceError("source_binding_invalid", "discovered source has no root binding")
    parts = Path(relative).parts
    if (os.path.isabs(relative) or any(part in ("", ".", "..") for part in parts)
            or os.path.abspath(os.path.join(root, relative)) != os.path.abspath(path)):
        raise SourceError("source_binding_invalid", "discovered source path differs from binding")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent = os.open(root, flags | os.O_DIRECTORY)
        st = os.fstat(parent)
        if (st.st_dev, st.st_ino) != (binding.get("root_device"), binding.get("root_inode")):
            raise SourceError("source_changed", "discovered root changed")
        for part in parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], flags | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (binding.get("source_device"), binding.get("source_inode")):
            os.close(fd)
            raise SourceError("source_changed", "discovered source changed")
        return fd, parent
    except (OSError, SourceError) as exc:
        if "parent" in locals():
            os.close(parent)
        if isinstance(exc, SourceError):
            raise
        raise SourceError("source_changed", "discovered source path changed") from exc


def _read_prefix(fd: int, size: int, deadline: float) -> bytes:
    parts = []
    offset = 0
    while offset < size:
        if time.monotonic() > deadline:
            raise SourceError("source_timeout", "source read exceeded deadline")
        try:
            part = os.pread(fd, min(65536, size - offset), offset)
        except OSError as exc:
            raise SourceError("source_read_failed", str(exc)) from exc
        if not part:
            raise SourceError("source_changed", "file truncated during snapshot")
        parts.append(part)
        offset += len(part)
    return b"".join(parts)


def _decompress(compressed: bytes, max_bytes: int, deadline: float) -> tuple[bytes, bool]:
    if not (compressed.startswith(b"\x28\xb5\x2f\xfd") or
            (len(compressed) >= 4 and 0x184D2A50 <= int.from_bytes(compressed[:4], "little") <= 0x184D2A5F)):
        raise SourceError("invalid_archive", "source is not a zstd archive")
    snapshot_file = tempfile.TemporaryFile()
    snapshot_file.write(compressed)
    snapshot_file.flush()
    snapshot_file.seek(0)
    try:
        proc = subprocess.Popen(["zstd", "-dc"], stdin=snapshot_file, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        snapshot_file.close()
        raise SourceError("unsupported_archive", f"zstd unavailable: {exc}") from exc
    parts = []
    total = 0
    limited = False
    try:
        assert proc.stdout is not None
        while True:
            wait = deadline - time.monotonic()
            if wait <= 0:
                raise SourceError("source_timeout", "archive decompression exceeded deadline")
            if not select.select([proc.stdout], [], [], wait)[0]:
                raise SourceError("source_timeout", "archive decompression exceeded deadline")
            chunk = os.read(proc.stdout.fileno(), min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            parts.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                limited = True
                break
        if limited:
            return b"".join(parts)[:max_bytes], True
        if proc.wait(timeout=max(0.01, deadline - time.monotonic())):
            raise SourceError("invalid_archive", "zstd decompression failed")
        return b"".join(parts), False
    except subprocess.TimeoutExpired as exc:
        raise SourceError("source_timeout", "archive decompression exceeded deadline") from exc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()
        snapshot_file.close()


def _read_source_once(path: str, provider: str, *, max_bytes: int = DEFAULT_MAX_BYTES,
                      binding: dict | None = None) -> dict:
    """Read a fixed prefix and export records with replayable byte/pointer origins.

    Providers: claude, codex, raw. Archives may end in .zst. Errors in a valid
    file are returned in the result; invalid source arguments raise SourceError.
    """
    if not isinstance(provider, str) or provider not in {"claude", "codex", "raw"}:
        raise SourceError("unsupported_provider", f"unsupported provider: {provider}")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise SourceError("invalid_limit", "max_bytes must be a positive integer")
    try:
        path = os.fspath(path)
    except TypeError as exc:
        raise SourceError("invalid_source", "source path must be a filesystem path") from exc
    if not isinstance(path, str):
        raise SourceError("invalid_source", "source path must be text")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    parent_fd = None
    try:
        if binding is not None:
            fd, parent_fd = _open_bound(path, binding)
        else:
            fd = os.open(path, flags)
    except OSError as exc:
        raise SourceError("source_open_failed", str(exc)) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise SourceError("not_regular_file", "source is not a regular file")
        snapshot = before.st_size
        archive = path.endswith(".zst")
        if archive and snapshot > max_bytes * 2:
            raise SourceError("source_too_large", "compressed archive exceeds input bound")
        deadline = time.monotonic() + 30
        read_size = snapshot if archive else min(snapshot, max_bytes)
        compressed = _read_prefix(fd, read_size, deadline)
        # A second pass detects in-place mutation of the captured prefix.
        if compressed != _read_prefix(fd, read_size, deadline):
            raise SourceError("source_changed", "source prefix changed during read")
        after = os.fstat(fd)
        try:
            named = (os.stat(Path(binding["relative_path"]).name, dir_fd=parent_fd,
                             follow_symlinks=False) if binding is not None else os.stat(path))
        except OSError as exc:
            raise SourceError("source_changed", "source path disappeared") from exc
        if (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or after.st_size < snapshot or (after.st_size == snapshot and (after.st_mtime_ns != before.st_mtime_ns or after.st_ctime_ns != before.st_ctime_ns)):
            raise SourceError("source_changed", "source replaced or truncated during read")
        data, limited = _decompress(compressed, max_bytes, deadline) if archive else (compressed, snapshot > max_bytes)
        digest = hashlib.sha256(compressed).hexdigest()
        source_id = stable_id("src-", os.path.abspath(path), provider, before.st_dev, before.st_ino)
        # A full-snapshot generation changes on both rewrites and appends.
        generation = stable_id("gen-", before.st_dev, before.st_ino, digest, snapshot)
        source = {"source_id": source_id, "session_id": "", "generation": generation,
                  "path": os.path.abspath(path), "provider": provider, "digest": digest,
                  "size": snapshot, "device": before.st_dev, "inode": before.st_ino,
                  "mtime_ns": before.st_mtime_ns, "compressed_digest": digest if archive else None,
                  "snapshot_format": "zst" if archive else "plain"}
        records = []
        errors = []
        coverage = {"complete": True, "processed_bytes": 0,
                    "snapshot_bytes": len(data) if archive else snapshot,
                    "gaps": [], "excluded": []}
        if limited:
            coverage["complete"] = False
            coverage["gaps"].append({"code": "byte_limit", "byte_start": len(data),
                                      "byte_end": None if archive else snapshot,
                                      "message": "source byte limit reached"})
            errors.append({"code": "byte_limit", "message": "source byte limit reached"})
        def add_record(item, start, end, sequence, origin):
            records.append({"schema": RECORD_SCHEMA, "source_id": source_id,
                "session_id": source["session_id"], "generation": generation,
                "record_id": stable_id("rec-", generation, start, origin.get("json_pointer", "")),
                "sequence": sequence, "timestamp": item["timestamp"],
                "timestamp_basis": item["timestamp_basis"], "role": item["role"],
                "channel": item["channel"], "turn_id": item["turn_id"],
                "tool_call_id": item["tool_call_id"], "text": item["text"],
                "origin": {"byte_start": start, "byte_end": end, **origin},
                "quality": "approximate" if provider == "raw" else "structured"})
        if provider == "raw":
            if time.monotonic() > deadline:
                raise SourceError("source_timeout", "normalization exceeded deadline")
            lines, gaps = plain_lines(data)
            if time.monotonic() > deadline:
                lines = []
                gaps = [{"code": "source_timeout", "byte_start": 0, "byte_end": len(data),
                         "message": "normalization exceeded deadline"}]
            for seq, (start, end, value) in enumerate(lines):
                if time.monotonic() > deadline:
                    coverage["gaps"].append({"code": "source_timeout", "byte_start": start,
                                             "byte_end": len(data), "message": "normalization exceeded deadline"})
                    errors.append({"code": "source_timeout", "message": "normalization exceeded deadline"})
                    coverage["complete"] = False
                    break
                add_record({"timestamp": None, "timestamp_basis": "unknown", "role": "unknown",
                            "channel": "message", "turn_id": None, "tool_call_id": None,
                            "text": value}, start, end, seq, {"spans": [{"byte_start": start, "byte_end": end - 1}]})
                coverage["processed_bytes"] = end
            coverage["gaps"].extend(gaps)
            if gaps:
                coverage["complete"] = False
                errors.extend({"code": gap["code"], "message": gap["message"]} for gap in gaps)
        else:
            adapt_record = _readers()
            offset = 0
            sequence = 0
            for line in data.splitlines(keepends=True):
                end = offset + len(line)
                if time.monotonic() > deadline:
                    coverage["gaps"].append({"code": "source_timeout", "byte_start": offset,
                                             "byte_end": len(data), "message": "normalization exceeded deadline"})
                    errors.append({"code": "source_timeout", "message": "normalization exceeded deadline"})
                    coverage["complete"] = False
                    break
                if not line.endswith(b"\n"):
                    coverage["gaps"].append({"code": "partial_eof", "byte_start": offset, "byte_end": end,
                                             "message": "unfinished JSONL row"})
                    errors.append({"code": "partial_eof", "message": "unfinished JSONL row"})
                    coverage["complete"] = False
                    break
                try:
                    row = json.loads(line.decode("utf-8"))
                    if not isinstance(row, dict):
                        raise ValueError("JSONL row is not an object")
                except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                    coverage["gaps"].append({"code": "invalid_json", "byte_start": offset,
                                             "byte_end": end, "message": str(exc)})
                    errors.append({"code": "invalid_json", "message": str(exc)})
                    coverage["complete"] = False
                    offset = end
                    continue
                if time.monotonic() > deadline:
                    coverage["gaps"].append({"code": "source_timeout", "byte_start": offset,
                                             "byte_end": len(data), "message": "normalization exceeded deadline"})
                    errors.append({"code": "source_timeout", "message": "normalization exceeded deadline"})
                    coverage["complete"] = False
                    break
                identity = None
                if provider == "claude" and isinstance(row.get("sessionId"), str) and row["sessionId"]:
                    identity = row["sessionId"]
                if provider == "codex" and row.get("type") == "session_meta" and isinstance(row.get("payload"), dict):
                    value = row["payload"].get("id")
                    if isinstance(value, str) and value:
                        identity = value
                if identity and source["session_id"] and identity != source["session_id"]:
                    coverage["gaps"].append({"code": "session_conflict", "byte_start": offset,
                                             "byte_end": len(data), "message": "conflicting session identity; remaining rows omitted"})
                    errors.append({"code": "session_conflict", "message": "conflicting session identity; remaining rows omitted"})
                    coverage["complete"] = False
                    break
                if identity and not source["session_id"]:
                    if records:
                        prior_spans = sorted({(record["origin"]["byte_start"], record["origin"]["byte_end"])
                                              for record in records})
                        for start, stop in prior_spans:
                            coverage["gaps"].append({"code": "identity_unknown", "byte_start": start,
                                                     "byte_end": stop,
                                                     "message": "row preceded validated session identity"})
                        errors.append({"code": "identity_unknown",
                                       "message": "earlier unidentified rows omitted"})
                        coverage["complete"] = False
                        records.clear()
                        sequence = 0
                    source["session_id"] = identity
                try:
                    adapted = adapt_record(provider, row)
                except (TypeError, ValueError, KeyError, RecursionError) as exc:
                    adapted = {"records": [], "excluded": [], "errors": [
                        {"code": "unsupported_record", "message": f"adapter rejected malformed row: {type(exc).__name__}"}]}
                if time.monotonic() > deadline:
                    coverage["gaps"].append({"code": "source_timeout", "byte_start": offset,
                                             "byte_end": len(data), "message": "normalization exceeded deadline"})
                    errors.append({"code": "source_timeout", "message": "normalization exceeded deadline"})
                    coverage["complete"] = False
                    break
                for item in adapted["records"]:
                    add_record(item, offset, end, sequence, {"json_pointer": item["json_pointer"]})
                    sequence += 1
                for category in adapted["excluded"]:
                    if category not in coverage["excluded"]:
                        coverage["excluded"].append(category)
                for err in adapted["errors"]:
                    errors.append(err)
                    coverage["gaps"].append({"code": err["code"], "byte_start": offset,
                                             "byte_end": end, "message": err["message"]})
                    coverage["complete"] = False
                coverage["processed_bytes"] = end
                offset = end
        if archive:
            source["decompressed_digest"] = hashlib.sha256(data).hexdigest()
            source["decompressed_size"] = len(data)
        # Session identity is part of the source key; a formerly unidentified
        # snapshot cannot collide with a later validated session in the index.
        source_id = stable_id("src-", os.path.abspath(path), provider,
                              before.st_dev, before.st_ino, source["session_id"])
        source["source_id"] = source_id
        for record in records:
            record["source_id"] = source_id
        return {"source": source, "records": records, "coverage": coverage, "errors": errors}
    finally:
        os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)


def read_source(path: str, provider: str, *, max_bytes: int = DEFAULT_MAX_BYTES,
                binding: dict | None = None) -> dict:
    """Read one immutable prefix, retrying one changed-generation race."""
    for attempt in range(2):
        try:
            return _read_source_once(path, provider, max_bytes=max_bytes, binding=binding)
        except SourceError as exc:
            if exc.code != "source_changed" or attempt:
                raise
    raise AssertionError("unreachable")
