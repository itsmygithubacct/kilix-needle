"""Resolve an explicit live-pane selector to its recorded source.

This module invokes only the pane list command. It never sends input, waits
for a turn, installs a provider, or changes a pane's state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time


class ResolveError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def snapshot() -> dict:
    """Read one size- and time-bounded Kilix pane snapshot."""
    command = [os.environ.get("KILIX_NEEDLE_KILIX", "kilix"), "panes", "list", "--json"]
    buffers = {"out": bytearray(), "err": bytearray()}
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        raise ResolveError("snapshot_unavailable", "cannot start the Kilix pane reader") from error
    deadline = time.monotonic() + 10
    try:
        with selectors.DefaultSelector() as ready:
            for name, pipe in (("out", process.stdout), ("err", process.stderr)):
                os.set_blocking(pipe.fileno(), False)
                ready.register(pipe, selectors.EVENT_READ, name)
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ResolveError("snapshot_timeout", "Kilix pane snapshot timed out")
                for key, _ in ready.select(min(remaining, 0.2)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        ready.unregister(key.fileobj)
                        continue
                    buffers[key.data].extend(chunk)
                    limit = 8 * 1024 * 1024 if key.data == "out" else 65536
                    if len(buffers[key.data]) > limit:
                        raise ResolveError("snapshot_limit", "Kilix pane snapshot exceeded its limit")
            try:
                code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise ResolveError("snapshot_timeout", "Kilix pane reader did not finish") from error
        if code:
            raise ResolveError("snapshot_unavailable", "Kilix pane reader failed")
        try:
            result = json.loads(buffers["out"])
        except (ValueError, UnicodeError) as error:
            raise ResolveError("snapshot_invalid", "invalid Kilix pane snapshot") from error
        if (not isinstance(result, dict) or result.get("schema") != "kilix.panes/v1"
                or not isinstance(result.get("panes"), list)):
            raise ResolveError("snapshot_invalid", "unsupported Kilix pane snapshot")
        return result
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        process.stdout.close()
        process.stderr.close()


def _session(pane: dict) -> dict:
    item = pane.get("coding_session")
    return item if isinstance(item, dict) else {}


def _broker(pane: dict) -> str:
    value = pane.get("broker")
    return str(value.get("session_id") or "") if isinstance(value, dict) else ""


def _pick(selector: str, tree: dict) -> dict:
    if (not isinstance(selector, str) or not selector.strip()
            or len(selector) > 256 or any(ord(c) < 32 for c in selector)):
        raise ResolveError("invalid_selector", "give a pane ID, session ID or unique title")
    value = selector.strip()
    panes = [p for p in tree["panes"] if isinstance(p, dict)
             and type(p.get("pane_id")) is int and p["pane_id"] > 0]
    if value.isascii() and value.isdecimal():
        matches = [p for p in panes if p["pane_id"] == int(value)]
    else:
        folded = value.casefold()
        matches = [p for p in panes if any(folded == x.casefold() for x in
                   (str(p.get("title") or ""), str(_session(p).get("session_id") or ""),
                    _broker(p)) if x and x != "unknown")]
        if not matches:
            matches = [p for p in panes if folded in str(p.get("title") or "").casefold()
                       or (len(folded) >= 4 and any(x.casefold().startswith(folded) for x in
                           (str(_session(p).get("session_id") or ""), _broker(p))
                           if x and x != "unknown"))]
    if not matches:
        raise ResolveError("not_found", "no pane matches that selector")
    if len(matches) != 1:
        ids = ", ".join(str(p["pane_id"]) for p in matches)
        raise ResolveError("ambiguous", f"selector matches multiple panes: {ids}")
    return matches[0]


def _inside(path: Path, roots: list[Path]) -> bool:
    real = path.resolve()
    return any(real.is_relative_to(root.expanduser().resolve()) for root in roots)


def _bound(path: Path, root: Path) -> dict:
    """Capture the identities that the reader must open again by descriptor."""
    path = path.absolute()
    root = root.absolute()
    try:
        relative = path.relative_to(root)
        root_stat = root.stat()
        source_stat = path.stat()
    except (OSError, ValueError) as exc:
        raise ResolveError("source_changed", "recording binding changed") from exc
    return {"path": str(path), "root": str(root), "relative_path": str(relative),
            "root_device": root_stat.st_dev, "root_inode": root_stat.st_ino,
            "source_device": source_stat.st_dev, "source_inode": source_stat.st_ino}


def _structured_path(provider: str, raw_path: str) -> tuple[Path, Path] | None:
    home = Path.home()
    roots = {
        "claude": [Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "projects"],
        "codex": [Path(os.environ.get("CODEX_HOME") or home / ".codex") / x
                  for x in ("sessions", "archived_sessions")],
    }
    # Additional providers can be admitted after their path/source adapter is
    # qualified. Until then the explicit raw recording remains available.
    if provider not in roots or not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute() or not _inside(path, roots[provider]):
        raise ResolveError("source_outside_root", "recorded session path is outside its provider root")
    if not path.is_file():
        return None
    for root in roots[provider]:
        if path.absolute().is_relative_to(root.expanduser().absolute()):
            return path, root.expanduser()
    raise ResolveError("source_outside_root", "recorded path traverses outside its provider root")


def _claude_session_path(session_id: str) -> str:
    """Find an exact known session when live inventory omits its file path.

    Claude stores one UUID-named JSONL beneath each immediate project folder.
    Scan folder names only, never transcripts or nested subagent directories.
    Ambiguity and an incomplete search are errors, not guesses.
    """
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", session_id):
        return ""
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    found = []
    deadline = time.monotonic() + 2
    try:
        with os.scandir(root) as folders:
            for count, folder in enumerate(folders):
                if count >= 4096 or time.monotonic() > deadline:
                    raise ResolveError("source_lookup_limit", "Claude session lookup exceeded its bound")
                if not folder.is_dir(follow_symlinks=False):
                    continue
                candidate = Path(folder.path) / (session_id.lower() + ".jsonl")
                if candidate.is_file():
                    found.append(candidate)
                    if len(found) > 1:
                        raise ResolveError("ambiguous_source", "multiple transcripts claim this Claude session ID")
    except FileNotFoundError:
        return ""
    except OSError as error:
        raise ResolveError("source_lookup_failed", "cannot inspect Claude session folders") from error
    if time.monotonic() > deadline:
        raise ResolveError("source_lookup_limit", "Claude session lookup exceeded its bound")
    return str(found[0]) if found else ""


def resolve_source(selector: str, *, tree: dict | None = None) -> dict:
    pane = _pick(selector, snapshot() if tree is None else tree)
    coding = _session(pane)
    provider = str(coding.get("provider") or "")
    session_id = str(coding.get("session_id") or "")
    raw_path = str(coding.get("path") or "")
    if provider == "claude" and not raw_path:
        raw_path = _claude_session_path(session_id)
    structured = _structured_path(provider, raw_path)
    broker = _broker(pane)
    binding = {"pane_id": pane["pane_id"], "expected_broker_id": broker,
               "expected_session_id": session_id if session_id != "unknown" else None}
    if structured is not None:
        path, root = structured
        return binding | _bound(path, root) | {"provider": provider, "source_kind": "structured"}
    if not re.fullmatch(r"[0-9a-f]{16,64}", broker):
        raise ResolveError("source_unavailable", "pane has no verified recording identity")
    base = Path(os.environ.get("GPU_TERMINAL_HOME") or Path.home() / ".local/gpu_terminal")
    state = Path(os.environ.get("KILIX_STATE_DIRECTORY") or base / "kilix/state")
    root = Path(os.environ.get("KILIX_TRANSCRIPT_DIR") or state / "transcripts")
    for candidate in (root / f"{broker}.log", root / "recent" / f"{broker}.log.zst",
                      root / "archive" / f"{broker}.log.zst"):
        if candidate.is_file():
            if not _inside(candidate, [root]):
                raise ResolveError("source_outside_root", "pane recording escapes its transcript root")
            return binding | _bound(candidate, root) | {"provider": "raw", "source_kind": "raw"}
    raise ResolveError("source_unavailable", "pane has no available structured or raw recording")


def check_binding(binding: dict) -> None:
    """After a read, ensure the pane still denotes the inspected recording."""
    current = resolve_source(str(binding["pane_id"]))
    keys = ("path", "provider", "expected_broker_id", "expected_session_id",
            "root", "root_device", "root_inode", "relative_path", "source_device", "source_inode")
    if any(current.get(key) != binding.get(key) for key in keys):
        raise ResolveError("source_changed", "pane source changed during the read; retry")
