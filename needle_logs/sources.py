"""Bounded snapshots of explicit regular-file log sources."""
from __future__ import annotations

import hashlib
from collections import deque
import importlib.util
from pathlib import Path
import json
import os
import select
import stat
import subprocess
import sys
import time
import tempfile
from .normalize import stable_id
from .schema import DEFAULT_MAX_BYTES, RECORD_SCHEMA
from .terminal import plain_lines

HEADER_BYTES = 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024


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


_REPLAY_MODULE = "_needle_pinned_transcript_clean"


def _replayer():
    """Load the pinned screen replay by path, or None when it is not checked out.

    A raw pane transcript is the PTY output of whatever ran in the pane. A
    full-screen program redraws in place, so its text only exists once the
    screen is replayed; kilix-transcript-clean does that and returns the lines
    in reading order.
    """
    loaded = sys.modules.get(_REPLAY_MODULE)
    if loaded is not None:
        return loaded.clean_bytes
    package = (Path(__file__).resolve().parents[1] / "third_party" / "kilix-transcript-clean"
               / "src" / "kilix_transcript_clean")
    if not (package / "__init__.py").is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location(
            _REPLAY_MODULE, package / "__init__.py", submodule_search_locations=[str(package)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[_REPLAY_MODULE] = module
        spec.loader.exec_module(module)
        return module.clean_bytes
    except (ImportError, OSError, AttributeError, SyntaxError):
        for name in [key for key in sys.modules if key.split(".")[0] == _REPLAY_MODULE]:
            del sys.modules[name]
        return None


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


def _read_prefix(fd: int, size: int, deadline: float, *, start: int = 0) -> bytes:
    parts = []
    offset = 0
    while offset < size:
        if time.monotonic() > deadline:
            raise SourceError("source_timeout", "source read exceeded deadline")
        try:
            part = os.pread(fd, min(65536, size - offset), start + offset)
        except OSError as exc:
            raise SourceError("source_read_failed", str(exc)) from exc
        if not part:
            raise SourceError("source_changed", "file truncated during snapshot")
        parts.append(part)
        offset += len(part)
    return b"".join(parts)


def _recent_lines(data: bytes, start: int, max_bytes: int) -> tuple[bytes, int]:
    # The extra byte before the window tells us whether it starts on a row
    # boundary. Never turn a fragment into an invalid JSON record or citation.
    if start or len(data) > max_bytes:
        cut = data.find(b"\n") + 1
        if not cut:
            cut = len(data)
        return data[cut:], start + cut
    return data, start


def _identity(provider: str, row: dict) -> str:
    value = None
    if provider == "claude":
        value = row.get("sessionId")
    elif provider == "codex" and row.get("type") == "session_meta":
        payload = row.get("payload")
        value = payload.get("id") if isinstance(payload, dict) else None
    elif provider == "grok":
        params = row.get("params")
        value = params.get("sessionId") if isinstance(params, dict) else None
    elif provider == "omp" and row.get("type") == "session":
        value = row.get("id")
    return value if isinstance(value, str) and value else ""


def _header_identity(provider: str, data: bytes) -> str:
    identities = set()
    for line in data.split(b"\n")[:-1]:
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(row, dict):
            value = _identity(provider, row)
            if value:
                identities.add(value)
    if len(identities) > 1:
        raise SourceError("session_conflict", "conflicting session identities in source header")
    return next(iter(identities), "")


def _decompress(compressed: bytes, max_bytes: int, deadline: float, *, recent: bool = False
                ) -> tuple[bytes, bool, int, int, bytes]:
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
    parts = deque()
    header = bytearray()
    retained = 0
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
            chunk = os.read(proc.stdout.fileno(), 65536 if recent else min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            parts.append(chunk)
            total += len(chunk)
            if recent:
                header.extend(chunk[:max(0, HEADER_BYTES - len(header))])
                retained += len(chunk)
                while parts and retained - len(parts[0]) >= max_bytes + 1:
                    retained -= len(parts.popleft())
                if total > MAX_EXPANDED_BYTES:
                    raise SourceError("source_too_large", "archive expansion exceeds the recent-read bound")
                continue
            if total > max_bytes:
                limited = True
                break
        if limited:
            return b"".join(parts)[:max_bytes], True, 0, max_bytes, b""
        if proc.wait(timeout=max(0.01, deadline - time.monotonic())):
            raise SourceError("invalid_archive", "zstd decompression failed")
        data = b"".join(parts)
        if recent:
            data = data[-(max_bytes + 1):]
            data, start = _recent_lines(data, total - len(data), max_bytes)
            return data, start > 0, start, total, bytes(header)
        return data, False, 0, total, b""
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
    """Read a fixed window and export records with replayable byte/pointer origins.

    Providers: claude, codex, grok (updates.jsonl), omp (also qwen), raw. Archives
    may end in .zst. Errors in a valid file are returned in the result; invalid
    source arguments raise SourceError.
    """
    if not isinstance(provider, str) or provider not in {"claude", "codex", "grok", "omp", "qwen", "raw"}:
        raise SourceError("unsupported_provider", f"unsupported provider: {provider}")
    provider = "omp" if provider == "qwen" else provider
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
        recent = provider != "raw"
        start = max(0, snapshot - max_bytes - 1) if recent and not archive else 0
        read_size = snapshot if archive else min(snapshot, max_bytes + int(recent))
        if start:
            compressed = _read_prefix(fd, read_size, deadline, start=start)
            verify = _read_prefix(fd, read_size, deadline, start=start)
        else:
            compressed = _read_prefix(fd, read_size, deadline)
            verify = _read_prefix(fd, read_size, deadline)
        if compressed != verify:
            raise SourceError("source_changed", "source window changed during read")
        header = b""
        if recent and not archive and snapshot > max_bytes:
            header_size = min(snapshot, HEADER_BYTES)
            header = _read_prefix(fd, header_size, deadline)
            if header != _read_prefix(fd, header_size, deadline):
                raise SourceError("source_changed", "source header changed during read")
        after = os.fstat(fd)
        try:
            named = (os.stat(Path(binding["relative_path"]).name, dir_fd=parent_fd,
                             follow_symlinks=False) if binding is not None else os.stat(path))
        except OSError as exc:
            raise SourceError("source_changed", "source path disappeared") from exc
        if (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or after.st_size < snapshot or (after.st_size == snapshot and (after.st_mtime_ns != before.st_mtime_ns or after.st_ctime_ns != before.st_ctime_ns)):
            raise SourceError("source_changed", "source replaced or truncated during read")
        if archive:
            data, limited, start, total, header = _decompress(compressed, max_bytes, deadline, recent=recent)
        elif recent:
            data, start = _recent_lines(compressed, start, max_bytes)
            limited, total = start > 0, snapshot
        else:
            data, limited, total = compressed, snapshot > max_bytes, snapshot
        identity = _header_identity(provider, header) if start else ""
        digest = hashlib.sha256(compressed).hexdigest()
        source_id = stable_id("src-", os.path.abspath(path), provider, before.st_dev, before.st_ino)
        # A full-snapshot generation changes on both rewrites and appends.
        generation = stable_id("gen-", before.st_dev, before.st_ino, digest, snapshot,
                               start, hashlib.sha256(header).hexdigest(),
                               before.st_mtime_ns, before.st_ctime_ns)
        source = {"source_id": source_id, "session_id": identity, "generation": generation,
                  "path": os.path.abspath(path), "provider": provider, "digest": digest,
                  "size": snapshot, "device": before.st_dev, "inode": before.st_ino,
                  "mtime_ns": before.st_mtime_ns, "compressed_digest": digest if archive else None,
                  "snapshot_format": "zst" if archive else "plain"}
        records = []
        errors = []
        coverage = {"complete": True, "processed_bytes": 0,
                    "snapshot_bytes": total, "window_start": start, "window_end": start + len(data),
                    "window": "recent" if recent else "prefix",
                    "gaps": [], "excluded": []}
        if limited:
            coverage["complete"] = False
            coverage["gaps"].append({"code": "byte_limit", "byte_start": 0 if recent else len(data),
                                      "byte_end": start if recent else (None if archive else snapshot),
                                      "message": "older history omitted from recent window" if recent else "source byte limit reached"})
            errors.append({"code": "byte_limit", "message": "source byte limit reached"})
        def add_record(item, start, end, sequence, origin):
            # Replayed lines all span the whole snapshot, so their position in
            # the replay is what tells them apart.
            replay = (origin["replay"]["line"],) if "replay" in origin else ()
            records.append({"schema": RECORD_SCHEMA, "source_id": source_id,
                "session_id": source["session_id"], "generation": generation,
                "record_id": stable_id("rec-", generation, start, origin.get("json_pointer", ""), *replay),
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
            clean = (_replayer() if any(gap["code"] == "terminal_controls" for gap in gaps)
                     else None)
            if clean is not None:
                # Terminal controls mean cursor redraws: replay the screen and
                # export what it showed. A screen line cannot be traced to one
                # byte range, so each record spans the snapshot and carries
                # its replay position instead.
                transcript = clean(data)
                lines, gaps = [], []
                if time.monotonic() > deadline:
                    gaps = [{"code": "source_timeout", "byte_start": 0, "byte_end": len(data),
                             "message": "normalization exceeded deadline"}]
                else:
                    source["replay"] = {
                        "tool": "kilix-transcript-clean",
                        "rotated": bool(transcript.source.get("rotated")),
                        "size_recorded": bool(transcript.source.get("size_recorded")),
                        "programs": [section.provider for section in transcript.sections]}
                    sequence = 0
                    for section in transcript.sections:
                        for text in section.lines:
                            if not text.strip():
                                continue
                            add_record({"timestamp": None, "timestamp_basis": "unknown",
                                        "role": "unknown", "channel": "message", "turn_id": None,
                                        "tool_call_id": None, "text": text}, 0, len(data), sequence,
                                       {"replay": {"line": sequence, "program": section.provider}})
                            sequence += 1
                    coverage["processed_bytes"] = len(data)
            for seq, (line_start, end, value) in enumerate(lines):
                if time.monotonic() > deadline:
                    coverage["gaps"].append({"code": "source_timeout", "byte_start": line_start,
                                             "byte_end": len(data), "message": "normalization exceeded deadline"})
                    errors.append({"code": "source_timeout", "message": "normalization exceeded deadline"})
                    coverage["complete"] = False
                    break
                add_record({"timestamp": None, "timestamp_basis": "unknown", "role": "unknown",
                            "channel": "message", "turn_id": None, "tool_call_id": None,
                            "text": value}, line_start, end, seq, {"spans": [{"byte_start": line_start, "byte_end": end - 1}]})
                coverage["processed_bytes"] = end
            coverage["gaps"].extend(gaps)
            if gaps:
                coverage["complete"] = False
                errors.extend({"code": gap["code"], "message": gap["message"]} for gap in gaps)
        else:
            adapt_record = _readers()
            offset = start
            sequence = 0
            for line in data.splitlines(keepends=True):
                end = offset + len(line)
                if time.monotonic() > deadline:
                    coverage["gaps"].append({"code": "source_timeout", "byte_start": offset,
                                             "byte_end": start + len(data), "message": "normalization exceeded deadline"})
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
                                             "byte_end": start + len(data), "message": "normalization exceeded deadline"})
                    errors.append({"code": "source_timeout", "message": "normalization exceeded deadline"})
                    coverage["complete"] = False
                    break
                identity = _identity(provider, row)
                if identity and source["session_id"] and identity != source["session_id"]:
                    coverage["gaps"].append({"code": "session_conflict", "byte_start": offset,
                                             "byte_end": start + len(data), "message": "conflicting session identity; remaining rows omitted"})
                    errors.append({"code": "session_conflict", "message": "conflicting session identity; remaining rows omitted"})
                    coverage["complete"] = False
                    break
                if identity and not source["session_id"]:
                    if records:
                        prior_spans = sorted({(record["origin"]["byte_start"], record["origin"]["byte_end"])
                                              for record in records})
                        for prior_start, stop in prior_spans:
                            coverage["gaps"].append({"code": "identity_unknown", "byte_start": prior_start,
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
                                             "byte_end": start + len(data), "message": "normalization exceeded deadline"})
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
                coverage["processed_bytes"] = end - start
                offset = end
        if archive:
            source["decompressed_digest"] = hashlib.sha256(data).hexdigest()
            source["decompressed_size"] = total
            source["decompressed_window_start"] = start
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
    """Read one snapshot, retrying one changed-generation race."""
    for attempt in range(2):
        try:
            return _read_source_once(path, provider, max_bytes=max_bytes, binding=binding)
        except SourceError as exc:
            if exc.code != "source_changed" or attempt:
                raise
    raise AssertionError("unreachable")
