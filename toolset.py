"""The tool set Needle sees, kept apart from the actions kilix-needle admits.

Needle 2 ranks declared tools with a retrieval head and, above five, renders
only the top five per turn: an unselected tool cannot be called at all, and
LoRA does not train that head. The ten actions in `actions.TOOLS` therefore
reach the model as five tools, and each call is translated back into those
actions *before* `actions.interpret`, so every check there applies unchanged.

Five tools also shortens every prompt: the ten schemas were 843 of 891 prompt
tokens, which set both latency and the cost of each fine-tuning step.
"""
from __future__ import annotations

from actions import DIRECTIONS, LAYOUTS, SIDES

_KIND = {"type": "string", "enum": ["pane", "tab"], "description": "a pane or a tab"}
_WHICH = {"type": "string",
          "description": "which one: this, left, right, above, below, next, previous, "
                         "a number, or its name or program"}

TOOLS = [
    {"name": "open",
     "description": "Open a new pane (split) or a new tab, optionally running a program",
     "parameters": {"type": "object", "properties": {
         "kind": _KIND,
         "side": {"type": "string", "enum": list(SIDES),
                  "description": "for a pane: which side of the current one"},
         "program": {"type": "string",
                     "description": "program to start in it, only if the user names one"},
         "name": {"type": "string", "description": "its name, only if the user gives one"}},
         "required": ["kind"]}},
    {"name": "close",
     "description": "Close, kill or quit a pane or a tab. Only when the user says close, "
                    "kill, quit or exit about a pane or tab",
     "parameters": {"type": "object", "properties": {"kind": _KIND, "which": _WHICH},
                    "required": ["kind", "which"]}},
    {"name": "go_to",
     "description": "Go to, switch to or focus another pane or tab without changing it",
     "parameters": {"type": "object", "properties": {"kind": _KIND, "which": _WHICH},
                    "required": ["kind", "which"]}},
    {"name": "adjust",
     "description": "Change the current tab or a pane: layout, tab name, pane size; maximize "
                    "or restore a pane, rename a pane, swap with a neighbour, move the tab",
     "parameters": {"type": "object", "properties": {
         "layout": {"type": "string", "enum": list(LAYOUTS),
                    "description": "grid; stack shows one pane at a time; vertical is side "
                                   "by side; horizontal is one above another"},
         "tab_name": {"type": "string", "description": "a new name for the tab"},
         "resize": {"type": "string", "enum": list(DIRECTIONS)},
         "amount": {"type": "integer", "minimum": 1, "maximum": 50,
                    "description": "cells to resize by, only if the user gives a number"},
         "maximize": {"type": "string", "enum": ["maximize", "restore"],
                      "description": "show one pane full-size, or restore the layout"},
         "pane_name": {"type": "string", "description": "a new name for a pane"},
         "pane": {"type": "string",
                  "description": "which pane to maximize or rename, if not this one"},
         "swap_with": {"type": "string", "enum": ["left", "right", "above", "below"],
                       "description": "swap this pane with its neighbour on that side"},
         "move_tab": {"type": "string", "enum": ["left", "right"],
                      "description": "move this tab left or right in the tab bar"},
         "tab_position": {"type": "integer", "minimum": 1, "maximum": 9,
                          "description": "move this tab to that position, only if the user "
                                         "gives one"}},
         "required": []}},
    {"name": "run_in_pane",
     "description": "Type a command into an existing pane and press enter",
     "parameters": {"type": "object", "properties": {
         "pane": {"type": "string", "description": "this, left, right, above, below, "
                                                   "or the pane's name or program"},
         "command": {"type": "string", "description": "the exact command text"}},
         "required": ["pane", "command"]}},
]


# What adjust can change; an adjust call with none of them reaches the checks as is.
ADJUSTS = frozenset({"layout", "tab_name", "resize", "maximize", "pane_name", "swap_with",
                     "move_tab", "tab_position"})


def to_actions(calls) -> list:
    """Translate five-tool calls into calls on the ten internal actions.

    Anything unrecognised passes through unchanged, so `actions.interpret`
    refuses it by name rather than this layer dropping it silently.
    """
    out = []
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict) or not isinstance(call.get("arguments"), dict):
            out.append(call)
            continue
        name, args = call.get("name"), call["arguments"]
        kind = args.get("kind")
        if name == "open" and kind in ("pane", "tab"):
            # Every argument passes through: a side on a tab is the model's error,
            # and dropping it silently would hide that from the checks (measured:
            # "pop open a split above me" -> kind tab, side above).
            out.append({"name": f"open_{kind}",
                        "arguments": {k: v for k, v in args.items() if k != "kind"}})
        elif name in ("close", "go_to") and kind in ("pane", "tab"):
            out.append({"name": f"{name}_{kind}", "arguments": {kind: args.get("which", "")}})
        elif name == "adjust":
            if "layout" in args:
                out.append({"name": "arrange_panes", "arguments": {"layout": args["layout"]}})
            if "tab_name" in args:
                out.append({"name": "rename_tab", "arguments": {"name": args["tab_name"]}})
            if "resize" in args:
                resize = {"direction": args["resize"]}
                if "amount" in args:
                    resize["amount"] = args["amount"]
                out.append({"name": "resize_pane", "arguments": resize})
            target = {"pane": args["pane"]} if "pane" in args else {}
            if "maximize" in args:
                maximize = dict(target)
                if args["maximize"] == "restore":
                    maximize["restore"] = True
                elif args["maximize"] != "maximize":
                    maximize["maximize"] = args["maximize"]     # for the checks to refuse
                out.append({"name": "maximize_pane", "arguments": maximize})
            if "pane_name" in args:
                out.append({"name": "rename_pane", "arguments": {**target, "name": args["pane_name"]}})
            if "swap_with" in args:
                out.append({"name": "swap_panes", "arguments": {"side": args["swap_with"]}})
            if "move_tab" in args or "tab_position" in args:
                move = {}
                if "move_tab" in args:
                    move["direction"] = args["move_tab"]
                if "tab_position" in args:
                    move["position"] = args["tab_position"]
                out.append({"name": "move_tab", "arguments": move})
            if not ADJUSTS & set(args):
                out.append({"name": "adjust", "arguments": args})
        elif name == "run_in_pane":
            out.append(call)
        else:
            out.append(call)
    return out


def from_actions(actions) -> list[dict]:
    """The five-tool calls that express internal actions: `to_actions`'s inverse.

    Used to write training answers. `to_actions(from_actions(a)) == a` for
    every internal action (checked in tests).
    """
    calls = []
    for kind, args in actions:
        if kind in ("open_pane", "open_tab"):
            calls.append({"name": "open", "arguments": {"kind": kind[5:], **args}})
        elif kind in ("close_pane", "close_tab", "go_to_pane", "go_to_tab"):
            verb, unit = kind.rsplit("_", 1)
            calls.append({"name": verb, "arguments": {"kind": unit, "which": str(args[unit])}})
        elif kind == "arrange_panes":
            calls.append({"name": "adjust", "arguments": {"layout": args["layout"]}})
        elif kind == "rename_tab":
            calls.append({"name": "adjust", "arguments": {"tab_name": args["name"]}})
        elif kind == "resize_pane":
            resize = {"resize": args["direction"]}
            if "amount" in args:
                resize["amount"] = args["amount"]
            calls.append({"name": "adjust", "arguments": resize})
        elif kind == "run_in_pane":
            calls.append({"name": "run_in_pane", "arguments": dict(args)})
        elif kind == "maximize_pane":
            maximize = {"maximize": "restore" if args.get("restore") else "maximize"}
            if args.get("pane", "current") != "current":
                maximize["pane"] = args["pane"]
            calls.append({"name": "adjust", "arguments": maximize})
        elif kind == "rename_pane":
            rename = {"pane_name": args["name"]}
            if args.get("pane", "current") != "current":
                rename["pane"] = args["pane"]
            calls.append({"name": "adjust", "arguments": rename})
        elif kind == "swap_panes":
            calls.append({"name": "adjust", "arguments": {"swap_with": args["side"]}})
        elif kind == "move_tab":
            move = {}
            if "direction" in args:
                move["move_tab"] = args["direction"]
            if "position" in args:
                move["tab_position"] = args["position"]
            calls.append({"name": "adjust", "arguments": move})
        else:
            raise ValueError(f"no five-tool form for {kind}")
    return calls
