"""A local history of the requests kilix-needle is asked, for improving its
grammar and models.

One JSON line per request, in GPU_TERMINAL_HOME's kilix-apps/kilix-needle/history
(default ~/.local/gpu_terminal/kilix-apps/kilix-needle/history/requests.jsonl). Each line holds:
- the request as it was given;
- the job, and whether a person or an agent sent it;
- what proposed the calls (the grammar, or which model);
- the raw calls, and what the checks admitted or refused and why;
- each action's outcome, and how long it took.

The history never leaves the machine. Requests can hold private text, such as
a message to a coding session, so the directory is 0700 and the files 0600.
The files rotate at MAX_BYTES and only KEEP of them are kept. Set
KILIX_NEEDLE_HISTORY=0 to record nothing.

Recording never changes what a request does: a history that cannot be written
is reported once on stderr and otherwise ignored.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
from pathlib import Path
import sys

SCHEMA = 1
MAX_BYTES = 8 * 1024 * 1024
KEEP = 8                    # requests.jsonl and requests.1.jsonl ... requests.7.jsonl
_OFF = {"0", "off", "no", "false"}
_warned = False


def directory() -> Path:
    home = os.environ.get("GPU_TERMINAL_HOME") or str(Path.home() / ".local" / "gpu_terminal")
    return Path(home) / "kilix-apps" / "kilix-needle" / "history"


def enabled() -> bool:
    return os.environ.get("KILIX_NEEDLE_HISTORY", "").strip().casefold() not in _OFF


def _rotate(path: Path) -> None:
    for n in range(KEEP - 1, 0, -1):
        older = path.with_name(f"requests.{n}.jsonl")
        newer = path if n == 1 else path.with_name(f"requests.{n - 1}.jsonl")
        if newer.exists():
            os.replace(newer, older)


def record(job: str, request, engine, calls, options, result: dict, seconds: float) -> None:
    """Append one request's entry. Never raises."""
    global _warned
    if not enabled():
        return
    try:
        entry = {
            "schema": SCHEMA,
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
            "job": job,
            "caller": "agent" if getattr(options, "agent", False) else "person",
            "engine": getattr(engine, "label", None) or type(engine).__name__,
            "request": request,
            "calls": calls,
            "dry_run": bool(getattr(options, "dry_run", False)),
            "assume_yes": bool(getattr(options, "assume_yes", False)),
            "status": result.get("status"),
            "note": result.get("note", ""),
            "items": result.get("items", []),
            "ms": round(seconds * 1000, 1),
        }
        line = (json.dumps(entry, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        folder = directory()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder, 0o700)
        path = folder / "requests.jsonl"
        lock = os.open(folder / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)     # several MCP servers may write at once
            if path.exists() and path.stat().st_size + len(line) > MAX_BYTES:
                _rotate(path)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        finally:
            os.close(lock)
    except Exception as error:      # noqa: BLE001 - recording must never break a request
        if not _warned:
            _warned = True
            print(f"kilix-needle: the request history was not written ({error})", file=sys.stderr)
