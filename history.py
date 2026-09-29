"""A local history of the requests kilix-needle's engines answer, for improving
its grammar and models.

One JSON line per panes, apps or agents request answered through the CLI or
MCP (needle_cli._recorded). Lines go to requests.jsonl in GPU_TERMINAL_HOME's
kilix-apps/kilix-needle/history (default ~/.local/gpu_terminal). Each line holds:
- the request as given, and the checked form the engine saw when it differs;
- the job, and whether a person or an agent sent it;
- what proposed the calls (the grammar, or which model);
- the raw calls, and what the checks admitted or refused and why;
- each action's outcome, and how long it took.

The external-inference bridge (domain_bridge) runs calls it was handed, not
requests, and records nothing.

The history never leaves the machine. Requests can hold private text, such as
a message to a coding session, so:
- **Files.** Every directory and file is opened relative to a directory
  descriptor, without following links. Each must be owned by this user and be a
  plain directory or a single-link regular file. Each is set to 0700 or 0600
  through the descriptor, archives included.
- **Size.** Each entry is bounded (MAX_ENTRY, with long text marked as
  truncated), and the files rotate at MAX_BYTES, keeping KEEP of them.
- **Items.** Only an item's kind, arguments, outcome and the checks' reason are
  kept: no display summaries (they name resolved panes and titles), no text of a
  runtime failure, no command lines, and no pane or broker identities.
- **Switching it off.** Set KILIX_NEEDLE_HISTORY=0 to record nothing.

Recording never changes what a request does or returns:
- If another writer holds the lock for longer than LOCK_WAIT, the entry is
  dropped.
- A write that cannot complete is rolled back, so the file stays whole lines.
- Any failure is reported at most once on stderr, and never raises.
"""
from __future__ import annotations

import datetime
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import time

SCHEMA = 2
MAX_BYTES = 8 * 1024 * 1024
KEEP = 8                        # requests.jsonl and requests.1.jsonl ... requests.7.jsonl
MAX_ENTRY = 64 * 1024           # one serialised entry, whatever it holds
MAX_TEXT = 4000                 # the request, and each text field of a call or an item
LOCK_WAIT = 0.25                # seconds; a busier history drops the entry
ITEM_FIELDS = ("kind", "args", "outcome", "reason")
REASONED = frozenset({"refused", "skipped"})     # reasons from the checks and confirmation
_OFF = {"0", "off", "no", "false"}
_PARTS = ("kilix-apps", "kilix-needle", "history")
_warned = False


class Unsafe(Exception):
    """A path the history will not use."""


def root() -> Path:
    return Path(os.environ.get("GPU_TERMINAL_HOME") or Path.home() / ".local" / "gpu_terminal")


def directory() -> Path:
    return root().joinpath(*_PARTS)


def enabled() -> bool:
    return os.environ.get("KILIX_NEEDLE_HISTORY", "").strip().casefold() not in _OFF


def _warn(message: str) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    try:
        print(f"kilix-needle: {message}", file=sys.stderr)
    except Exception:           # noqa: BLE001 - even stderr may be gone
        pass


def _mine(info: os.stat_result, kind) -> bool:
    return kind(info.st_mode) and info.st_uid == os.getuid()


def _open_dir(parent: int | None, name: str, *, create: bool) -> int:
    """A directory by name, never through a link, owned by this user."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, flags, dir_fd=parent)
    try:
        if not _mine(os.fstat(fd), stat.S_ISDIR):
            raise Unsafe(f"{name} is not a directory of this user's")
        return fd
    except BaseException:
        os.close(fd)            # every failure closes what it opened (review KN-R17-201)
        raise


def _history_dir() -> int:
    """The history directory's descriptor. The configured root is trusted as
    given (it is this user's own setting); below it nothing is followed."""
    base = root()
    base.mkdir(parents=True, exist_ok=True)
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in _PARTS:
            child = _open_dir(fd, name, create=True)
            os.close(fd)
            fd = child
        os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_file(folder: int, name: str, flags: int) -> int:
    """A regular file of this user's with one link, opened without following
    links or blocking on anything that is not a file, and set to 0600."""
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600,
                 dir_fd=folder)
    try:
        info = os.fstat(fd)
        if not _mine(info, stat.S_ISREG) or info.st_nlink != 1:
            raise Unsafe(f"{name} is not a regular file of this user's")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _exists(folder: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=folder, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _rotate(folder: int) -> None:
    for n in range(KEEP - 1, 0, -1):
        newer = "requests.jsonl" if n == 1 else f"requests.{n - 1}.jsonl"
        if _exists(folder, newer):
            os.close(_open_file(folder, newer, os.O_RDONLY))    # validated and 0600
            os.rename(newer, f"requests.{n}.jsonl", src_dir_fd=folder, dst_dir_fd=folder)


def _lock(folder: int) -> int | None:
    fd = _open_file(folder, ".lock", os.O_WRONLY | os.O_CREAT)
    try:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return None
                time.sleep(0.01)
    except BaseException:
        os.close(fd)
        raise


def _append(folder: int, line: bytes) -> None:
    fd = _open_file(folder, "requests.jsonl", os.O_RDWR | os.O_APPEND | os.O_CREAT)
    try:
        _heal(fd)
        start = os.fstat(fd).st_size
        written = 0
        try:
            while written < len(line):
                n = os.write(fd, line[written:])
                if n <= 0:
                    raise OSError(errno.EIO, "the history write made no progress")
                written += n
        except BaseException:
            os.ftruncate(fd, start)         # whole lines or nothing
            raise
    finally:
        os.close(fd)


def _read_at(fd: int, size: int, offset: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = os.pread(fd, size - len(data), offset + len(data))
        if not chunk:
            raise OSError(errno.EIO, "the history ended early while it was read")
        data += chunk
    return data


def _heal(fd: int) -> None:
    """End the file at a whole line before anything is added. A write whose
    rollback failed, or a process that died mid-write, leaves a partial last
    line. It is cut back to just after the last newline, or to nothing when the
    whole file is that line. When no newline lies within 2 * MAX_ENTRY of the end
    (a file this writer could not have made), the history is not touched and
    the entry is dropped (reviews KN-R17-203, KN-R17-302)."""
    size = os.fstat(fd).st_size
    if size == 0 or _read_at(fd, 1, size - 1) == b"\n":
        return
    window = min(size, 2 * MAX_ENTRY)
    cut = _read_at(fd, window, size - window).rfind(b"\n")
    if cut >= 0:
        os.ftruncate(fd, size - window + cut + 1)
    elif window == size:
        os.ftruncate(fd, 0)
    else:
        raise Unsafe("the history ends in an overlong partial line; nothing was added")


def _repair_archives(folder: int) -> None:
    """Every retained archive is checked and set to 0600 on each write, not
    only when it rotates (review KN-R17-204)."""
    for n in range(1, KEEP):
        name = f"requests.{n}.jsonl"
        if _exists(folder, name):
            os.close(_open_file(folder, name, os.O_RDONLY))


def _text(value, limit: int = MAX_TEXT):
    """Long strings, wherever they are, cut to a marked length."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"…[{len(value)} chars]"
    if isinstance(value, dict):
        return {str(k)[:200]: _text(v, limit) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [_text(v, limit) for v in list(value)[:50]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _text(str(value), limit)


def entry(job: str, given, checked, engine, calls, options, result: dict, seconds: float) -> dict:
    # The admitted action (kind, args) and why it did not run are the data. A
    # display summary names resolved panes and titles, and so can an unresolved
    # or failed action's text, so only the reasons the checks and confirmation
    # give are kept (reviews KN-R17-202, KN-R17-301).
    items = [{k: item[k] for k in ITEM_FIELDS if k in item
              and not (k == "reason" and item.get("outcome") not in REASONED)}
             for item in result.get("items", []) if isinstance(item, dict)]
    text = given if isinstance(given, str) else str(given)
    out = {
        "schema": SCHEMA,
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
        "job": job,
        "caller": "agent" if getattr(options, "agent", False) else "person",
        "engine": str(getattr(engine, "label", None) or type(engine).__name__)[:100],
        "request": _text(text),
        "request_chars": len(text),
        "calls": _text(calls),
        "dry_run": bool(getattr(options, "dry_run", False)),
        "assume_yes": bool(getattr(options, "assume_yes", False)),
        "status": result.get("status"),
        "note": _text(result.get("note", "")),
        "items": _text(items),
        "ms": round(seconds * 1000, 1),
    }
    if isinstance(checked, str) and checked != text:
        out["checked"] = _text(checked)
    return out


def _line(data: dict) -> bytes:
    # ASCII JSON: any text, even a lone surrogate, serialises (review KN-R17-07).
    line = json.dumps(data, ensure_ascii=True, default=str)
    if len(line) + 1 > MAX_ENTRY:
        for field in ("items", "calls", "note"):
            data[field] = {"omitted": True, "chars": len(json.dumps(data[field], default=str))}
        data["request"] = _text(data["request"], 1000)
        line = json.dumps(data, ensure_ascii=True, default=str)
    return (line + "\n").encode("ascii")


def record(job: str, given, checked, engine, calls, options, result: dict,
           seconds: float) -> None:
    """Append one request's entry. Never raises; a busy or unsafe history drops it."""
    if not enabled():
        return
    folder = None
    try:
        line = _line(entry(job, given, checked, engine, calls, options, result, seconds))
        folder = _history_dir()
        lock = _lock(folder)
        if lock is None:
            _warn("the request history was busy; an entry was not recorded")
            return
        try:
            # Heal before rotating, so no partial line is carried into an
            # archive (review KN-R17-303).
            if _exists(folder, "requests.jsonl"):
                active = _open_file(folder, "requests.jsonl", os.O_RDWR)
                try:
                    _heal(active)
                    size = os.fstat(active).st_size
                finally:
                    os.close(active)
                if size + len(line) > MAX_BYTES:
                    _rotate(folder)
            _repair_archives(folder)
            _append(folder, line)
        finally:
            os.close(lock)
    except Exception as error:      # noqa: BLE001 - recording must never break a request
        _warn(f"the request history was not written ({error})")
    finally:
        if folder is not None:
            os.close(folder)
