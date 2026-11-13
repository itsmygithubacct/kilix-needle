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


SNAPSHOT_ERRORS = {
    "caller_context_unverified": (
        "The Kilix log reader cannot verify the calling pane, so it cannot look "
        "up panes by number or title. Give the full session ID instead (a Codex "
        "thread ID or Claude session ID), read an explicit recording with file "
        "and provider, or run codex --no-daemon from the intended Kilix pane."),
    "pane_context_unavailable": (
        "The Kilix log reader has no usable authenticated pane connection. "
        "Give the full session ID instead (a Codex thread ID or Claude session "
        "ID), run it from the intended live Kilix pane, or read an explicit "
        "recording with file and provider."),
}
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


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
            # Accept only the host's fixed diagnostic contract. Never echo
            # child stderr or arbitrary JSON messages (keys/paths may occur
            # there), and never accept pane data from a failed command.
            try:
                failure = json.loads(buffers["out"])
            except (ValueError, UnicodeError):
                failure = None
            if (isinstance(failure, dict)
                    and failure.get("schema") == "kilix.panes.error/v1"
                    and isinstance(failure.get("code"), str)
                    and failure["code"] in SNAPSHOT_ERRORS):
                raise ResolveError(failure["code"], SNAPSHOT_ERRORS[failure["code"]])
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
        "grok": [_grok_home() / "sessions"],
        "omp": [_omp_home() / "sessions"],
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
    if not _UUID.fullmatch(session_id):
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


def _grok_home() -> Path:
    return Path(os.environ.get("GROK_HOME") or Path.home() / ".grok")


def grok_updates(path: str) -> str:
    """The updates.jsonl of the Grok session a path names.

    A Grok session is a folder; its full conversation is updates.jsonl.
    The folder itself, or its events.jsonl or chat_history.jsonl, names the
    same session, so they read as its updates.jsonl. Anything else is kept.
    """
    candidate = Path(path).expanduser()
    folder = candidate if candidate.is_dir() else (
        candidate.parent if candidate.name in ("events.jsonl", "chat_history.jsonl") else None)
    if folder is not None and (folder / "updates.jsonl").is_file():
        return str(folder / "updates.jsonl")
    return path


def _grok_session_path(session_id: str) -> str:
    """Find a Grok session's updates.jsonl by session ID, from folder names only.

    Grok writes sessions/<encoded working directory>/<session ID>/. Only
    folder names are listed. Ambiguity and an incomplete search are errors.
    """
    if not _UUID.fullmatch(session_id):
        return ""
    root = _grok_home() / "sessions"
    found = []
    deadline = time.monotonic() + 2
    try:
        with os.scandir(root) as folders:
            for count, folder in enumerate(folders):
                if count >= 4096 or time.monotonic() > deadline:
                    raise ResolveError("source_lookup_limit", "Grok session lookup exceeded its bound")
                if not folder.is_dir(follow_symlinks=False):
                    continue
                candidate = Path(folder.path) / session_id.lower() / "updates.jsonl"
                if candidate.is_file():
                    found.append(candidate)
                    if len(found) > 1:
                        raise ResolveError("ambiguous_source", "multiple folders claim this Grok session ID")
    except FileNotFoundError:
        return ""
    except OSError as error:
        raise ResolveError("source_lookup_failed", "cannot inspect Grok session folders") from error
    if time.monotonic() > deadline:
        raise ResolveError("source_lookup_limit", "Grok session lookup exceeded its bound")
    return str(found[0]) if found else ""


def _codex_session_path(session_id: str) -> str:
    """Find an exact Codex rollout by thread ID, from file names only.

    Codex writes sessions/YYYY/MM/DD/rollout-<time>-<thread ID>.jsonl and
    moves archived ones to archived_sessions/ under the same name. Directory
    and file names are listed; no transcript is opened to search. Ambiguity
    and an incomplete search are errors, not guesses.
    """
    if not _UUID.fullmatch(session_id):
        return ""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    suffix = "-" + session_id.lower() + ".jsonl"
    found: list[str] = []
    visited = 0
    deadline = time.monotonic() + 2

    def scan(directory: Path, depth: int) -> None:
        nonlocal visited
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 65536 or time.monotonic() > deadline:
                        raise ResolveError("source_lookup_limit", "Codex session lookup exceeded its bound")
                    if depth:
                        if entry.is_dir(follow_symlinks=False):
                            scan(Path(entry.path), depth - 1)
                    elif (entry.name.startswith("rollout-") and entry.name.endswith(suffix)
                          and entry.is_file(follow_symlinks=False)):
                        found.append(entry.path)
                        if len(found) > 1:
                            raise ResolveError("ambiguous_source", "multiple rollouts claim this Codex thread ID")
        except FileNotFoundError:
            return
        except OSError as error:
            raise ResolveError("source_lookup_failed", "cannot inspect Codex session folders") from error

    scan(home / "sessions", 3)
    scan(home / "archived_sessions", 0)
    return found[0] if found else ""


def _omp_home() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".omp/agent").expanduser()


def _omp_session_path(session_id: str) -> str:
    """Find an OMP/Qwen recording by its UUID suffix, without reading logs."""
    if not _UUID.fullmatch(session_id):
        return ""
    suffix = "_" + session_id.lower() + ".jsonl"
    found = []
    deadline = time.monotonic() + 2
    visited = 0
    try:
        with os.scandir(_omp_home() / "sessions") as folders:
            for count, folder in enumerate(folders):
                if count >= 4096 or time.monotonic() > deadline:
                    raise ResolveError("source_lookup_limit", "OMP session lookup exceeded its bound")
                if not folder.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(folder.path) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > 65536 or time.monotonic() > deadline:
                            raise ResolveError("source_lookup_limit", "OMP session lookup exceeded its bound")
                        if entry.name.endswith(suffix) and entry.is_file(follow_symlinks=False):
                            found.append(entry.path)
                            if len(found) > 1:
                                raise ResolveError("ambiguous_source", "multiple recordings claim this OMP session ID")
    except FileNotFoundError:
        return ""
    except OSError as error:
        raise ResolveError("source_lookup_failed", "cannot inspect OMP session folders") from error
    return found[0] if found else ""


def _session_source(selector: str) -> dict | None:
    """Resolve a full session ID to its recording without any pane listing.

    A Claude, Grok or OMP session ID or a Codex thread ID names exactly one
    structured recording, so no pane, route or credential is involved. This
    is the path that works behind a shared Codex app-server, where the
    caller's pane cannot be verified. The reader still checks the session
    identity inside the file. None means the ID names no recording here.
    """
    value = selector.strip() if isinstance(selector, str) else ""
    if not _UUID.fullmatch(value):
        return None
    value = value.lower()
    matches = [(provider, path) for provider, path in
               (("claude", _claude_session_path(value)), ("codex", _codex_session_path(value)),
                ("grok", _grok_session_path(value)), ("omp", _omp_session_path(value))) if path]
    if not matches:
        return None
    if len(matches) > 1:
        raise ResolveError("ambiguous_source", "recordings of more than one client claim this session ID")
    provider, raw_path = matches[0]
    structured = _structured_path(provider, raw_path)
    if structured is None:
        return None
    path, root = structured
    return ({"pane_id": None, "expected_broker_id": "", "expected_session_id": value}
            | _bound(path, root) | {"provider": provider, "source_kind": "structured",
                                   "resolution": "session_id"})


def resolve_source(selector: str, *, tree: dict | None = None) -> dict:
    # A full session ID is resolved from the provider's own folders first, so
    # it needs no pane listing and no verified caller.
    direct = _session_source(selector)
    if direct is not None:
        return direct
    pane = _pick(selector, snapshot() if tree is None else tree)
    coding = _session(pane)
    provider = str(coding.get("provider") or "")
    provider = "omp" if provider == "qwen" else provider
    session_id = str(coding.get("session_id") or "")
    raw_path = str(coding.get("path") or "")
    if provider == "claude" and not raw_path:
        raw_path = _claude_session_path(session_id)
    if provider == "grok":
        # Kilix lists a Grok pane by session ID, without a file path.
        raw_path = grok_updates(raw_path) if raw_path else _grok_session_path(session_id)
    if provider == "omp" and not raw_path:
        raw_path = _omp_session_path(session_id)
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
    """After a read, ensure the selector still denotes the inspected recording."""
    if binding.get("resolution") == "inventory":
        from .sources import _open_bound
        fd, parent = _open_bound(binding["path"], binding)
        os.close(fd)
        os.close(parent)
        return
    if binding.get("resolution") == "session_id":
        current = _session_source(str(binding["expected_session_id"])) or {}
    else:
        current = resolve_source(str(binding["pane_id"]))
    keys = ("path", "provider", "expected_broker_id", "expected_session_id",
            "root", "root_device", "root_inode", "relative_path", "source_device", "source_inode")
    if any(current.get(key) != binding.get(key) for key in keys):
        raise ResolveError("source_changed", "pane source changed during the read; retry")
