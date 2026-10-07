"""Bounded transport to `kilix pty ... --json`; no second implementation of broker access.

Needle runs `kilix pty` as argv and checks that what came back is the document
the contract promises for the operation it asked. It never talks to a broker,
opens a socket or reads a runtime directory itself.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shutil
import signal
import subprocess

import pty_job


SCHEMA = "kilix.pty/v1"
RESULTS = {"verified_absent": 0, "uncertain": 1, "refused": 3, "not_found": 4}
WALL_SECONDS = 30            # one kilix call; the launcher's own guard is 10 s
WALL_SECONDS_TIMEOUT = 90    # `--timeout` makes the launcher wait up to 75 s
MAX_OUTPUT = 4 * 1024 * 1024
# What a Kilix without the new subcommands prints for any `pty` argument.
_OLD_USAGE = "pty [--install-only]"


@dataclass
class Reply:
    """What one `kilix pty` call returned. `kind` is ok or the way it did not."""
    kind: str
    returncode: int | None = None
    document: dict | None = None
    error: str = ""


def kilix_executable() -> str | None:
    return shutil.which(os.environ.get("KILIX_NEEDLE_KILIX", "kilix") or "kilix")


def argv_for(action: dict, step: str = "run") -> list[str]:
    """The `kilix pty` arguments (after `kilix`) for one step of an operation.

    `step` is `run` for reads, `status` for a kill's lookup, and `kill` for its
    last call, which takes the lookup's `started_millis` as `action["expect"]`.
    """
    head = ["pty"]
    if "timeout_seconds" in action:
        head += ["--timeout", pty_job.timeout_arg(action["timeout_seconds"])]
    operation = action["operation"]
    bounds = []
    if "max_lines" in action:
        bounds = ["--lines", str(action["max_lines"])]
    elif "max_bytes" in action:
        bounds = ["--bytes", str(action["max_bytes"])]
    if operation == "list":
        return head + ["list", "--json"]
    if operation == "journals":
        return head + ["journals", "list", "--json"]
    if operation == "pane":
        return head + ["status", "--pane", str(action["pane_id"]), "--json"]
    if operation == "status" or (operation == "kill" and step == "status"):
        return head + ["status", action["id"], "--json"]
    if operation == "observe":
        return head + ["observe", action["id"], "--once", "--text", "--json"] + bounds
    if operation == "journal":
        return head + ["journals", "show", action["id"], "--text", "--json"] + bounds
    if operation == "kill":
        return head + ["kill", action["id"], "--yes", "--expect-started",
                       str(action["expect"]), "--json"]
    raise ValueError(f"no kilix pty form for {operation}")


def _line(text: str) -> str:
    """A bounded, single-line, printable excerpt of what kilix wrote to stderr."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    return json.dumps(first[:300], ensure_ascii=True)[1:-1]


def call(argv: list[str], *, wall: float | None = None) -> Reply:
    """Run `kilix ARGV` and return its JSON document, or why there is none."""
    executable = kilix_executable()
    if executable is None:
        return Reply("unavailable", error="kilix was not found; install Kilix or set KILIX_NEEDLE_KILIX")
    wall = wall or (WALL_SECONDS_TIMEOUT if "--timeout" in argv else WALL_SECONDS)
    try:
        process = subprocess.Popen([executable, *argv], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
    except OSError as exc:
        return Reply("unavailable", error=f"kilix could not be started: {exc.strerror or exc}")
    try:
        out, err = process.communicate(timeout=wall)
    except subprocess.TimeoutExpired:
        # The group is the one process this call started; nothing else is signalled.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.communicate()
        return Reply("timeout", error=f"kilix pty did not return within {wall:g} seconds")
    stderr = err.decode("utf-8", "replace")
    document = None
    if 0 < len(out) <= MAX_OUTPUT:
        try:
            parsed = json.loads(out.decode("utf-8"))
        except (ValueError, UnicodeError):
            parsed = None
        document = parsed if isinstance(parsed, dict) else None
    if document is None:
        if _OLD_USAGE in stderr:
            return Reply("too_old", process.returncode,
                         error="this kilix has no `kilix pty list|status|observe|kill` (it only opens "
                               "the session manager); update Kilix to a version with `kilix pty --json`")
        if len(out) > MAX_OUTPUT:
            return Reply("failed", process.returncode, error="kilix pty wrote more than 4 MiB")
        return Reply("failed", process.returncode,
                     error=_line(stderr) or f"kilix pty exited {process.returncode} with no JSON document")
    return Reply("ok", process.returncode, document)


def _integer(value) -> bool:
    return type(value) is int


def checked(action: dict, document: dict, returncode: int, step: str = "run") -> dict:
    """The document must be the contract's for this operation, or ValueError."""
    if document.get("schema") != SCHEMA:
        raise ValueError(f"unexpected schema {str(document.get('schema'))[:40]!r}, not {SCHEMA}")
    if not isinstance(document.get("runtime"), str):
        raise ValueError("no runtime in the document")
    operation = action["operation"]
    ident = action.get("id")
    if operation == "kill" and step == "kill":
        result = document.get("result")
        if (result not in RESULTS or RESULTS[result] != returncode or document.get("id") != ident
                or type(document.get("request_sent")) is not bool):
            raise ValueError("kill receipt does not match the request or its exit status")
        sent = document["request_sent"]
        if result == "verified_absent":
            # Success is only for the incarnation read just before the kill was sent.
            if (sent is not True or not _integer(document.get("started_millis"))
                    or document["started_millis"] != action.get("expect")):
                raise ValueError("verified_absent receipt must have request_sent true and the "
                                 "started_millis that --expect-started carried")
        elif result in ("refused", "not_found") and sent is not False:
            raise ValueError(f"{result} receipt must have request_sent false")
        return document
    if operation in ("status", "pane", "kill"):      # kill's step is the status lookup
        if document.get("result") == "not_found":
            if returncode != 4 or not isinstance(document.get("id"), str) or (
                    ident is not None and document["id"] != ident):
                raise ValueError("not_found document does not match the request or its exit status")
            return document
        session = document.get("session")
        if (returncode != 0 or not isinstance(session, dict) or not isinstance(session.get("id"), str)
                or not pty_job.ID_CHARS.fullmatch(session["id"])
                or (ident is not None and session["id"] != ident)):
            raise ValueError("status document does not match the request or its exit status")
        return document
    if returncode != 0:
        raise ValueError("exit status is not 0 for a read")
    if operation == "list":
        if not isinstance(document.get("sessions"), list) or not isinstance(document.get("unreachable"), list):
            raise ValueError("list document lacks sessions and unreachable")
    elif operation == "journals":
        if not isinstance(document.get("journals"), list):
            raise ValueError("journals document lacks journals")
    elif operation in ("observe", "journal"):
        returned = document.get("id")
        if (not isinstance(returned, str)
                or not (ident == returned or (operation == "journal" and ident.startswith(returned + ".")))
                or document.get("untrusted") is not True or not isinstance(document.get("text"), str)
                or type(document.get("truncated")) is not bool or not _integer(document.get("total_bytes"))):
            raise ValueError("snapshot document does not match the request or lacks its untrusted marker")
    return document
