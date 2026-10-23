"""Validate selector receipt relationships; never perform census/matching or IPC."""
import json

import pty_job


def require(condition, message):
    if not condition:
        raise ValueError(message)


def opaque_id(value):
    # The protocol allows leading hyphens; the legacy prose grammar does not.
    return isinstance(value, str) and bool(pty_job.ID_CHARS.fullmatch(value)) and value not in (".", "..")


def captured_caller(environment):
    value = environment.get("KITTY_PTY_BROKER_SESSION")
    return {"source": "environment", "authenticated": False,
            "status": "valid" if opaque_id(value) else "missing" if not value else "malformed",
            "id": value if opaque_id(value) else None}


def caller(value):
    require(isinstance(value, dict) and value.get("source") == "environment" and value.get("authenticated") is False
            and value.get("status") in ("valid", "missing", "malformed"), "invalid unauthenticated caller echo")
    require(opaque_id(value.get("id")) if value["status"] == "valid" else value.get("id") is None,
            "caller status contradicts ID")


def selected_id(selector, ident):
    require(opaque_id(ident), "invalid opaque session ID")
    require(("id" not in selector or ident == selector["id"])
            and ("id_prefix" not in selector or ident.startswith(selector["id_prefix"])), "ID contradicts selector")


def binding(value, selector):
    require(isinstance(value, dict), "invalid resolved incarnation")
    selected_id(selector, value.get("id"))
    require(type(value.get("started_millis")) is int and 0 < value["started_millis"] < 2**64
            and type(value.get("attached")) is bool, "invalid resolved fields")
    require("attached" not in selector or value["attached"] is selector["attached"], "resolved attachment contradicts selector")
    for field in ("broker_pid", "child_pid", "start_ticks", "child_start_ticks"):
        if field in value:
            require(value[field] is None or (type(value[field]) is int and value[field] >= 0), "invalid process identity")
    if "boot_id" in value:
        require(value["boot_id"] is None or (isinstance(value["boot_id"], str) and 0 < len(value["boot_id"]) < 64
                and "\0" not in value["boot_id"]), "invalid boot identity")


def census(value, selector):
    require(isinstance(value, dict) and type(value.get("complete")) is bool, "invalid census")
    for field in ("census_count", "matches", "possible"):
        require(type(value.get(field)) is int and 0 <= value[field] <= 1024, "invalid census counts")
    rows, total = value.get("candidates"), value["matches"] + value["possible"]
    require(total <= value["census_count"] and isinstance(rows, list) and len(rows) == min(total, 32)
            and type(value.get("candidates_truncated")) is bool
            and value["candidates_truncated"] is (total > len(rows)), "invalid candidate display bounds")
    ids, possible = set(), 0
    for row in rows:
        require(isinstance(row, dict) and type(row.get("reachable")) is bool and type(row.get("possible")) is bool,
                "invalid candidate row")
        selected_id(selector, row.get("id"))
        require(row["id"] not in ids, "duplicate candidate ID")
        ids.add(row["id"]); possible += row["possible"]
    require(possible <= value["possible"] and len(rows) - possible <= value["matches"], "candidate rows contradict counts")


def unique(value, selected):
    require(value is not None and selected is not None and value["complete"] and value["matches"] == 1
            and value["possible"] == 0 and len(value["candidates"]) == 1, "success lacks conservative uniqueness")
    row = value["candidates"][0]
    require(row["id"] == selected["id"] and row["reachable"] and not row["possible"], "selection contradicts candidate evidence")


def checked(action, document, returncode, step, valid_selector, expected_caller=None):
    verb = "resolve" if step == "resolve" else action["operation"]
    require(isinstance(document, dict) and document.get("schema") == "kilix.pty.selector/v1"
            and {"reason", "request_sent", "receiver_refused", "completion", "resolved", "census"} <= document.keys()
            and document.get("operation") == verb and document.get("selector") == action["selector"]
            and isinstance(document.get("runtime"), str) and type(returncode) is int
            and len(json.dumps(document, ensure_ascii=True).encode()) <= 65536, "selector receipt does not bind request")
    valid_selector(document["selector"])
    caller(document.get("caller"))
    if expected_caller is not None:
        require(document["caller"] == expected_caller, "receipt differs from captured caller")
    sent, rejected = document.get("request_sent"), document.get("receiver_refused")
    require((sent is None or type(sent) is bool) and type(rejected) is bool, "invalid send flags")
    selected, observed = document.get("resolved"), document.get("census")
    if selected is not None:
        binding(selected, action["selector"])
    if observed is not None:
        census(observed, action["selector"])
    result, completion, reason = document.get("result"), document.get("completion"), document.get("reason")
    require(isinstance(result, str) and (reason is None or isinstance(reason, str)), "invalid outcome")
    if verb == "resolve":
        require(result in ("unique", "ambiguous", "unresolved", "not_found", "error") and sent is False and not rejected
                and returncode == (1 if result == "error" else 0) and completion == "read", "resolve must be a read")
        if result != "error":
            require(observed is not None and observed["complete"], "preview lacks complete census")
            total = observed["matches"] + observed["possible"]
            require((result == "unique" or selected is None) and (result != "ambiguous" or total > 1)
                    and (result != "unresolved" or total == 1) and (result != "not_found" or total == 0),
                    "preview contradicts census")
    else:
        codes = {"verified_absent": 0, "uncertain": 1, "refused": 3, "ambiguous": 3}
        require(document.get("operation_id") == action["operation_id"] and opaque_id(document.get("operation_id"))
                and type(document.get("duplicate")) is bool and codes.get(result) == returncode, "invalid end outcome/key")
        if result == "verified_absent":
            require(sent is True and not rejected and completion == "verified_absent" and reason is None,
                    "verified absence lacks accepted send")
            require(document["caller"]["status"] == "valid" and selected is not None
                    and document["caller"]["id"] != selected["id"], "success lacks valid non-own caller")
        elif result == "uncertain":
            require(sent is not False and not rejected and completion == "unknown", "uncertain history marked unsent")
        elif rejected:
            require(result == "refused" and sent is True and completion == "receiver_refused"
                    and reason in ("started_mismatch", "attachment_mismatch", "cannot_bind") and selected is not None,
                    "invalid receiver refusal")
            require(reason != "attachment_mismatch" or "attached" in action["selector"], "unsupplied attachment rejection")
            unique(observed, selected)
        else:
            require(sent is False and completion == "not_sent", "unsent refusal has contradictory completion")
        if result == "ambiguous":
            require(observed is not None and observed["matches"] + observed["possible"] > 1, "ambiguity lacks alternatives")
    if result in ("unique", "verified_absent"):
        unique(observed, selected)
    return document
