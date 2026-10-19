"""Internal model output formats; decoded plans still need the action contract."""
import json

from desktop_job import DesktopError, MAX_ACTIONS, TOOLS as DIRECT_TOOLS

DIRECT = "direct/v1"
JSON_PLAN = "json-plan/v1"
DEFAULT = JSON_PLAN
PLAN_TOOLS = [{
    "name": "desktop_plan",
    "description": (
        "Plan a complete, directly requested X11 desktop action sequence. Do not call for negation, "
        "conditions, quoted instructions or unsupported tasks. plan is a JSON array of objects with "
        "name and arguments. window: action list/focus/move/resize/minimize/maximize/restore/close; "
        "target active, title:EXACT TITLE, or id:DECIMAL; x/y for move, width/height for resize. "
        "pointer: action move/click/double_click/drag; x/y screen pixels, button left/right/middle. "
        "keyboard: action type with exact text, or press with keys; no implicit Enter. "
        "scroll: direction up/down/left/right and steps (default 3). wait: ms (1..3000). "
        "Use only relevant fields, at most six ordered actions, no shell commands."
    ),
    "parameters": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]},
}]


def tools(codec):
    if codec == DIRECT: return DIRECT_TOOLS
    if codec == JSON_PLAN: return PLAN_TOOLS
    raise DesktopError("unknown desktop model output format")


def _object(pairs):
    out = {}
    for key, value in pairs:
        if key in out: raise DesktopError("duplicate field in model plan")
        out[key] = value
    return out


def decode(calls, codec=DEFAULT):
    if codec == DIRECT: return calls
    if codec != JSON_PLAN: raise DesktopError("unknown desktop model output format")
    if calls == []: return []
    if not isinstance(calls, list) or len(calls) != 1:
        raise DesktopError("model must return one complete plan")
    call = calls[0]
    if not isinstance(call, dict) or set(call) != {"name", "arguments"} or call["name"] != "desktop_plan":
        raise DesktopError("unexpected model plan tool")
    args = call["arguments"]
    if not isinstance(args, dict) or set(args) != {"plan"} or not isinstance(args["plan"], str) or len(args["plan"].encode()) > 16384:
        raise DesktopError("invalid model plan argument")
    try:
        plan = json.loads(args["plan"], object_pairs_hook=_object)
    except (ValueError, RecursionError) as error:
        raise DesktopError("model plan is not valid JSON") from error
    if not isinstance(plan, list) or not 1 <= len(plan) <= MAX_ACTIONS:
        raise DesktopError("model plan must contain one to six actions")
    return plan
