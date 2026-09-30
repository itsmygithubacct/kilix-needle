"""Bounded, model-free transport to Kilix's shared structured action controller."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time

SCHEMA = "kilix.actions/v1"
OPERATIONS = ("pane.open", "agent.launch", "agent.deliver", "operation.status")
STATUSES = ("planned", "created", "submitted", "deferred", "blocked", "uncertain", "pending", "not_found")
VERIFICATION = ("identity_verified", "pane_created_verified", "delivery_verified",
                "agent_startup_verified", "acknowledgment_verified", "completion_verified")
MAX_REQUEST = 16384
MAX_RECEIPT = 16384


def validate(request: dict) -> dict:
    """Check the transport envelope before consent; controller checks all semantics."""
    required = {"schema", "operation_id", "operation", "source", "target", "params"}
    if not isinstance(request, dict) or not required <= request.keys() or request.keys() - required - {"timeout", "dry_run"}:
        raise ValueError("action requires schema, operation_id, operation, source, target, params; unknown fields refused")
    if request["schema"] != SCHEMA or request["operation"] not in OPERATIONS:
        raise ValueError("use schema kilix.actions/v1 and a supported operation")
    if not isinstance(request["operation_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", request["operation_id"]):
        raise ValueError("operation_id must be 1–64 ASCII identifier characters")
    for name in ("source", "target"):
        identity = request[name]
        if (not isinstance(identity, dict) or set(identity) != {"pane_id", "broker"}
                or type(identity["pane_id"]) is not int or identity["pane_id"] <= 0
                or not isinstance(identity["broker"], str)
                or not re.fullmatch(r"[0-9a-f]{16,64}", identity["broker"])):
            raise ValueError(f"{name} requires exact positive pane_id and lowercase hex broker (16–64 characters)")
    if type(request.get("dry_run", False)) is not bool:
        raise ValueError("dry_run must be boolean")
    timeout = request.get("timeout", 15)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 60:
        raise ValueError("timeout must be a number from 1 to 60 seconds")
    params = request["params"]
    fields = {
        "pane.open": ({"argv", "cwd", "title", "placement"}, {"direction", "bias"}),
        "agent.launch": ({"agent", "cwd", "title", "placement"},
                         {"direction", "bias", "model", "prompt", "resume", "coding_yolo", "trust_folder", "agent_arg"}),
        "agent.deliver": ({"text"}, {"mode"}),
        "operation.status": (set(), set()),
    }
    needed, optional = fields[request["operation"]]
    if not isinstance(params, dict) or not needed <= params.keys() or params.keys() - needed - optional:
        raise ValueError(f"{request['operation']} params have missing or unknown fields")
    if request["operation"] == "operation.status" and request.get("dry_run", False):
        raise ValueError("operation.status is already read-only; omit dry_run")
    if request["operation"] == "agent.launch" and params.get("trust_folder", False) is not False:
        raise ValueError("trust_folder must be false; set folder trust explicitly through agent-control before launch")
    if len(json.dumps(request, ensure_ascii=True).encode()) > MAX_REQUEST:
        raise ValueError("action request exceeds 16384 bytes")
    return dict(request, dry_run=request.get("dry_run", False))


def error(request, message: str, *, uncertain=False) -> dict:
    request = request if isinstance(request, dict) else {}
    def safe_identity(value):
        if (isinstance(value, dict) and set(value) == {"pane_id", "broker"}
                and type(value["pane_id"]) is int and value["pane_id"] > 0
                and isinstance(value["broker"], str) and re.fullmatch(r"[0-9a-f]{16,64}", value["broker"])):
            return dict(value)
        return None
    operation_id = request.get("operation_id")
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", operation_id):
        operation_id = None
    return {"schema": SCHEMA, "operation_id": operation_id,
            "operation": request.get("operation") if request.get("operation") in OPERATIONS else None,
            "status": "uncertain" if uncertain else "blocked",
            "source": safe_identity(request.get("source")), "target": safe_identity(request.get("target")),
            "duplicate": False, "dry_run": request.get("dry_run") is True,
            **{name: False for name in VERIFICATION}, "error": message[:240]}


def checked(receipt, request: dict) -> dict:
    required = {"schema", "operation_id", "operation", "status", "source", "target", "duplicate", "dry_run", *VERIFICATION}
    if (not isinstance(receipt, dict) or not required <= receipt.keys()
            or receipt.keys() - required - {"evidence", "error"}
            or receipt["schema"] != SCHEMA or receipt["status"] not in STATUSES):
        raise ValueError("invalid action receipt envelope")
    if receipt["operation_id"] != request["operation_id"] or any(receipt[key] != request[key] for key in ("source", "target")):
        raise ValueError("action receipt does not match operation ID and exact identities")
    for key in ("source", "target"):
        identity = receipt[key]
        if (not isinstance(identity, dict) or set(identity) != {"pane_id", "broker"}
                or type(identity["pane_id"]) is not int or identity["pane_id"] <= 0
                or not isinstance(identity["broker"], str) or not re.fullmatch(r"[0-9a-f]{16,64}", identity["broker"])):
            raise ValueError("action receipt has invalid identity types")
    expected_operation = request["operation"]
    if receipt["operation"] not in OPERATIONS or expected_operation != "operation.status" and receipt["operation"] != expected_operation:
        raise ValueError("action receipt does not match operation")
    if any(type(receipt[name]) is not bool for name in (*VERIFICATION, "duplicate", "dry_run")):
        raise ValueError("action receipt verification fields must be booleans")
    if expected_operation != "operation.status" and request["dry_run"] and (receipt["dry_run"] is not True or receipt["status"] not in ("planned", "blocked", "not_found")):
        raise ValueError("controller reported mutation for a plan")
    if expected_operation != "operation.status" and receipt["dry_run"] != request["dry_run"]:
        raise ValueError("action receipt mode differs from request")
    if any(receipt[name] for name in ("agent_startup_verified", "acknowledgment_verified", "completion_verified")):
        raise ValueError("controller incorrectly claims agent startup, acknowledgment or completion")
    if receipt["status"] == "created" and not (receipt["identity_verified"] and receipt["pane_created_verified"]):
        raise ValueError("created action lacks identity and pane verification")
    if receipt["status"] == "planned" and not receipt["identity_verified"]:
        raise ValueError("planned action lacks identity verification")
    if receipt["status"] in ("submitted", "deferred") and not (receipt["identity_verified"] and receipt["delivery_verified"]):
        raise ValueError("delivery action lacks identity and delivery verification")
    if receipt["status"] == "created":
        if receipt["operation"] not in ("pane.open", "agent.launch"):
            raise ValueError("created receipt has an incompatible operation")
        pane = receipt.get("evidence", {}).get("pane") if isinstance(receipt.get("evidence"), dict) else None
        if (not isinstance(pane, dict) or set(pane) != {"pane_id", "broker", "tab_id", "os_window_id"}
                or any(type(pane[name]) is not int or pane[name] <= 0 for name in ("pane_id", "tab_id", "os_window_id"))
                or not isinstance(pane["broker"], str) or not re.fullmatch(r"[0-9a-f]{16,64}", pane["broker"])
                or pane["pane_id"] in (request["source"]["pane_id"], request["target"]["pane_id"])):
            raise ValueError("created receipt lacks an exact new pane identity")
    if receipt["status"] in ("submitted", "deferred") and receipt["operation"] != "agent.deliver":
        raise ValueError("delivery receipt has an incompatible operation")
    if "error" in receipt and (not isinstance(receipt["error"], str) or len(receipt["error"]) > 240):
        raise ValueError("invalid bounded action error")
    if "evidence" in receipt and not isinstance(receipt["evidence"], dict):
        raise ValueError("invalid action evidence")
    if len(json.dumps(receipt, ensure_ascii=True).encode()) > MAX_RECEIPT:
        raise ValueError("action receipt exceeds 16384 bytes")
    return receipt


def module_root() -> Path:
    """Use explicit config selection or tmux's selected/PATH Kilix discovery."""
    selected = os.environ.get("KILIX_ACTION_MODULE_ROOT")
    if selected is None:
        kilix_selection = os.environ.get("KILIX_NEEDLE_KILIX", "kilix")
        if Path(kilix_selection).is_absolute() and Path(kilix_selection).is_dir():
            root = Path(kilix_selection) / "config"
        else:
            executable = shutil.which(kilix_selection)
            if not executable:
                raise ValueError("Kilix source unavailable; select KILIX_NEEDLE_KILIX or KILIX_ACTION_MODULE_ROOT")
            root = Path(executable).resolve().parent / "config"
    else:
        root = Path(selected)
        if not selected or not root.is_absolute():
            raise ValueError("KILIX_ACTION_MODULE_ROOT must select an absolute config directory")
    if not (root / "agent_actions.py").is_file():
        raise ValueError("selected Kilix config does not contain agent_actions.py")
    return root


def dispatch(request: dict) -> dict:
    request = validate(request)
    try:
        output, returncode = _process(request)
        result = checked(json.loads(output, object_pairs_hook=unique_object), request)
        if returncode != exit_status(result):
            raise ValueError("controller process status disagrees with receipt")
        return result
    except subprocess.TimeoutExpired:
        return error(request, "action controller timed out; mutation and completion are unknown; query operation.status before any retry", uncertain=True)
    except (OSError, ValueError) as exc:
        return error(request, f"action controller unavailable or invalid: {exc}", uncertain=not request["dry_run"] and request["operation"] != "operation.status")


def exit_status(receipt: dict) -> int:
    return 0 if receipt["status"] in ("planned", "created", "submitted", "deferred") else 1


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object field")
        result[key] = value
    return result


def _process(request):
    """Read at most one bounded receipt; stderr never enters the result."""
    timeout = request.get("timeout", 15) + 5
    command = [sys.executable, "-I", "-B", str(Path(__file__).resolve())]
    with tempfile.TemporaryFile() as payload:
        payload.write(json.dumps(request, ensure_ascii=True).encode())
        payload.seek(0)
        with subprocess.Popen(command, stdin=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            try:
                deadline, output = time.monotonic() + timeout, bytearray()
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise subprocess.TimeoutExpired(command, timeout)
                        chunk = os.read(process.stdout.fileno(), min(4096, MAX_RECEIPT + 1 - len(output)))
                        if not chunk:
                            break
                        output.extend(chunk)
                        if len(output) > MAX_RECEIPT:
                            raise ValueError("action controller output exceeds 16384 bytes")
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
                return bytes(output), process.returncode
            except BaseException:
                process.kill()
                process.wait()
                raise


def _worker() -> int:
    request = None
    entered = False
    try:
        request = validate(json.loads(sys.stdin.read(MAX_REQUEST + 1), object_pairs_hook=unique_object))
        sys.path.insert(0, str(module_root()))
        from agent_actions import dispatch as controller_dispatch
        entered = True
        result = checked(controller_dispatch(request), request)
    except Exception as exc:
        result = error(request, f"action controller could not load or dispatch: {type(exc).__name__}: {exc}",
                       uncertain=entered and isinstance(request, dict) and not request["dry_run"] and request["operation"] != "operation.status")
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return exit_status(result)


if __name__ == "__main__":
    raise SystemExit(_worker())
