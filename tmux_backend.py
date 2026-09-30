"""Bounded transport to the shared tmux controller; selects no socket itself."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


SCHEMA = "kilix.tmux/v1"
ERRORS = {"EUSAGE": 2, "ENOENT": 3, "EEXIST": 4, "ETIMEDOUT": 5,
          "ENOSERVER": 6, "ETMUX": 7, "EAMBIGUOUS": 8}


def error(code: str, message: str) -> dict:
    return {"schema": SCHEMA, "ok": False, "code": code, "error": message, "exit": ERRORS[code]}


def cli_args(request: dict) -> list[str]:
    args = ["--socket", request["socket"], "--json"]
    if request["dry_run"]:
        args.append("--dry-run")
    operation = request["operation"]
    args.append(operation)
    if operation == "new":
        args.append(request["name"])
        if "cwd" in request:
            args.extend(["--cwd", request["cwd"]])
    elif operation != "list":
        args.append(request["target"])
        if operation == "read" and "lines" in request:
            args.extend(["--lines", str(request["lines"])])
        elif operation in ("send", "type"):
            args.extend(["--", request["text"]])
        elif operation == "key":
            args.extend(request["keys"])
        elif operation == "rename":
            args.append(request["new_name"])
    return args


def checked(result, request: dict) -> dict:
    if not isinstance(result, dict) or result.get("schema") != SCHEMA or type(result.get("ok")) is not bool:
        raise ValueError("invalid controller response envelope")
    if not result["ok"]:
        if (result.get("code") not in ERRORS or type(result.get("exit")) is not int
                or result["exit"] != ERRORS[result["code"]] or not isinstance(result.get("error"), str)):
            raise ValueError("invalid controller error")
        return result
    data = result.get("data")
    if (not isinstance(data, dict) or data.get("operation") != request["operation"]
            or type(data.get("dry_run")) is not bool or data["dry_run"] != request["dry_run"]):
        raise ValueError("controller response does not match the requested operation/mode")
    if request["dry_run"] and (data.get("submitted") is True or data.get("completion") == "done"):
        raise ValueError("controller reported submission for a plan")
    operation = request["operation"]
    if operation == "list":
        if not isinstance(data.get("sessions"), list):
            raise ValueError("controller list is missing its session snapshot")
    elif request["dry_run"]:
        if not isinstance(data.get("request"), dict):
            raise ValueError("controller plan is missing its normalized request")
    elif operation in ("new", "rename"):
        session = data.get("session")
        name = request["name"] if operation == "new" else request["new_name"]
        if (not isinstance(session, dict) or session.get("name") != name
                or not isinstance(session.get("id"), str) or not re.fullmatch(r"\$[0-9]+", session["id"])
                or not isinstance(session.get("panes"), list)):
            raise ValueError("controller is missing the requested stable session result")
    elif operation == "close":
        if not isinstance(data.get("closed"), str) or not re.fullmatch(r"\$[0-9]+", data["closed"]):
            raise ValueError("controller close is missing its stable session ID")
    if not request["dry_run"] and operation in ("read", "send", "type", "key"):
        if not isinstance(data.get("target"), str) or not re.fullmatch(r"%[0-9]+", data["target"]):
            raise ValueError("controller I/O is missing its stable pane ID")
        if operation == "read" and (not isinstance(data.get("text"), str)
                                    or type(data.get("lines")) is not int or not 1 <= data["lines"] <= 2000):
            raise ValueError("controller read is missing bounded text/line metadata")
        if operation in ("send", "type") and (type(data.get("sent")) is not int
                                              or data["sent"] != len(request["text"])):
            raise ValueError("controller sent count differs from the requested literal text")
        if operation == "key" and data.get("keys") != request["keys"]:
            raise ValueError("controller sent different keys from the request")
    if not request["dry_run"] and request["operation"] in ("send", "type", "key"):
        submitted = (request["operation"] == "type" or
                     request["operation"] == "key" and "Enter" in request["keys"])
        if type(data.get("submitted")) is not bool or data["submitted"] != submitted:
            raise ValueError("controller response has incorrect submission semantics")
        if request["operation"] in ("type", "key") and data.get("completion") != "unknown":
            raise ValueError("controller response incorrectly claims command completion")
    return result


def dispatch(request: dict) -> dict:
    """Use an explicitly selected CLI/root, then installed CLI/bundled discovery.

    CLI selection takes precedence when both implementation selectors are set.
    No selection variable supplies a socket; it always comes from the request.
    """
    cli = os.environ.get("KILIX_TMUX_CLI")
    root = os.environ.get("KILIX_TMUX_MODULE_ROOT")
    if cli is None and root is None:
        cli = shutil.which("kilix-tmux")
    if cli is not None:
        executable = shutil.which(cli) if cli else None
        if executable is None:
            return error("ETMUX", "KILIX_TMUX_CLI must select an existing controller executable")
        command = [executable, *cli_args(request)]
        stdin = None
    else:
        command = [sys.executable, "-I", "-B", str(Path(__file__).resolve())]
        stdin = json.dumps(request, ensure_ascii=True)
    try:
        process = subprocess.run(command, input=stdin, text=True, capture_output=True, timeout=30)
        result = checked(json.loads(process.stdout), request)
        expected = 0 if result["ok"] else result["exit"]
        if process.returncode != expected:
            raise ValueError("controller process status disagrees with its response")
        return result
    except subprocess.TimeoutExpired:
        return error("ETIMEDOUT", "tmux controller exceeded the 30-second wall limit; completion unknown")
    except (OSError, ValueError) as exc:
        return error("ETMUX", f"tmux controller unavailable or invalid: {exc}")


def _worker() -> int:
    try:
        request = json.load(sys.stdin)
        root = os.environ.get("KILIX_TMUX_MODULE_ROOT")
        if root is None:
            # Resolve the same source selection used by other Needle jobs.
            # An installed command symlink points back to Kilix's source tree.
            # Reading its bundled package never executes Kilix or its setup.
            executable = shutil.which(os.environ.get("KILIX_NEEDLE_KILIX", "kilix"))
            if not executable:
                raise ValueError("Kilix source unavailable; select KILIX_TMUX_MODULE_ROOT or KILIX_TMUX_CLI")
            root = str(Path(executable).resolve().parent / "config")
        if not root or not Path(root).is_absolute() or not Path(root).is_dir():
            raise ValueError("tmux module root must select an absolute import directory")
        if not (Path(root) / "kilix_tmux" / "__init__.py").is_file():
            raise ValueError("selected Kilix config/module root does not contain kilix_tmux")
        sys.path.insert(0, root)
        from kilix_tmux import dispatch as controller_dispatch
        result = checked(controller_dispatch(request), request)
    except Exception as exc:
        result = error("ETMUX", f"tmux controller could not load or dispatch: {type(exc).__name__}: {exc}")
    print(json.dumps(result, ensure_ascii=True))
    return 0 if isinstance(result, dict) and result.get("ok") is True else result.get("exit", 7)


if __name__ == "__main__":
    raise SystemExit(_worker())
