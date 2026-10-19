"""Text-to-action contract for an experimental desktop Needle 2 adapter.

The model never emits a shell command. Validation happens before any desktop
connection. Titles/text are data, coordinates are screen pixels, and typing
does not press Enter. The X11 executor resolves identities separately.
"""
from __future__ import annotations

import copy
import json
import re
import unicodedata

VERSION = "kilix.desktop/v1"
MAX_ACTIONS = 6
MAX_REQUEST_BYTES = 2048


def _enum(*values):
    return {"type": "string", "enum": list(values)}


def _tool(name, description, properties, required):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}


_PIXEL = {"type": "integer", "minimum": 0, "maximum": 32767}
TOOLS = [
    _tool("window", "List desktop windows or change one window. target is active, title:EXACT TITLE, or id:DECIMAL ID.", {
        "action": _enum("list", "focus", "move", "resize", "minimize", "maximize", "restore", "close"),
        "target": {"type": "string"}, "x": _PIXEL, "y": _PIXEL,
        "width": {"type": "integer", "minimum": 1, "maximum": 32767},
        "height": {"type": "integer", "minimum": 1, "maximum": 32767}}, ["action"]),
    _tool("pointer", "Move the mouse, click, double-click, or drag to screen pixels. Omit x/y to click at the current pointer. Drag uses the current pointer as its start.", {
        "action": _enum("move", "click", "double_click", "drag"),
        "x": _PIXEL, "y": _PIXEL, "button": _enum("left", "middle", "right")}, ["action"]),
    _tool("keyboard", "Type exact literal text into the focused window without Enter, or press one key/chord (e.g. ctrl+l, Return).", {
        "action": _enum("type", "press"), "text": {"type": "string"},
        "keys": {"type": "string"}}, ["action"]),
    _tool("scroll", "Scroll at the pointer in one direction. steps is wheel notches, default 3.", {
        "direction": _enum("up", "down", "left", "right"),
        "steps": {"type": "integer", "minimum": 1, "maximum": 20}}, ["direction"]),
    _tool("wait", "Pause a short sequence for an explicit duration in milliseconds, at most 3000.", {
        "ms": {"type": "integer", "minimum": 1, "maximum": 3000}}, ["ms"]),
]

_SCHEMAS = {t["name"]: t["parameters"] for t in TOOLS}
_QUOTE = re.compile(r'"(?:[^"\\]|\\.)*"|“[^”]*”|(?<!\w)\x27[^\x27]*\x27(?!\w)')
_NUMBERS = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(), range(21)))
_COURTESY = re.compile(r"^(?:(?:please|kindly|can you|could you|would you|will you|i want you to|i would like you to|i'd like you to|i need you to)\s+)+", re.I)
_VERBS = r"(?:list|show|focus|activate|switch|bring|move|position|place|put|resize|size|set|minimize|iconify|maximize|restore|unmaximize|close|dismiss|(?:left[ -]|right[ -]|middle[ -])?click|double[ -]click|drag|type|enter|write|insert|press|hit|tap|hold|scroll|wheel|wait|pause)"
_START = re.compile(r"^" + _VERBS + r"\b", re.I)
_PROHIBITED = re.compile(r"\b(?:don't|not|never|avoid|unless|if|when|while|until|after|before|later|tomorrow|repeat|loop|instead|ignore|pretend|example|said|says|quoted|suggested|install|download|execute|launch|sudo|shutdown|reboot)\b", re.I)
_INTENTS = {
    "window.list": r"\b(?:list|show)\b.*\bwindows\b",
    "window.focus": r"\b(?:focus|activate|switch|bring)\b",
    "window.move": r"\b(?:move|position|place|put)\b",
    "window.resize": r"\b(?:resize|size|set|make)\b",
    "window.minimize": r"\b(?:minimize|iconify)\b",
    "window.maximize": r"\bmaximize\b",
    "window.restore": r"\b(?:restore|unmaximize)\b",
    "window.close": r"\b(?:close|dismiss)\b",
    "pointer.move": r"\b(?:move|position|place|put)\b.*\b(?:mouse|pointer|cursor)\b",
    "pointer.click": r"\bclick\b",
    "pointer.double_click": r"\bdouble[ -]click\b",
    "pointer.drag": r"\bdrag\b",
    "keyboard.type": r"\b(?:type|enter|write|insert)\b",
    "keyboard.press": r"\b(?:press|hit|tap|hold)\b",
    "scroll": r"\b(?:scroll|wheel)\b",
    "wait": r"\b(?:wait|pause)\b",
}


class DesktopError(ValueError):
    pass


def _only_words(text, allowed):
    """Reject unaccounted request content, including unsupported extra clauses."""
    text = re.sub(r"(?<=\d)[x×](?=\d)", " ", text)
    rest = re.sub(r"\b(?:" + "|".join(allowed.split()) + r")\b", " ", text, flags=re.I)
    if re.sub(r"[\s\d,().?!:×-]", "", rest):
        raise DesktopError("request contains unsupported or unaccounted desktop instructions")


def _literal(match):
    value = match.group()
    if value.startswith('"'):
        try:
            return json.loads(value)
        except ValueError as e:
            raise DesktopError("quoted text must use valid JSON escapes") from e
    return value[1:-1]


def request_clauses(request):
    if not isinstance(request, str) or not request.strip() or len(request.encode()) > MAX_REQUEST_BYTES:
        raise DesktopError("provide a request of 1–2048 UTF-8 bytes")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in request):
        raise DesktopError("requests must be a single line without control characters")
    literals = []
    def hide(match):
        literals.append(_literal(match))
        return f" LITERAL{len(literals)-1} "
    masked = _QUOTE.sub(hide, request)
    if '"' in masked or '“' in masked or '”' in masked:
        raise DesktopError("unclosed quoted text")
    # Bind order before looking at a model result. Never drop an unknown clause.
    pieces = re.split(r"\s*(?:;|,?\s+(?:and\s+)?then\s+|\s+and\s+(?=" + _VERBS + r"\b))\s*", masked, flags=re.I)
    if not 1 <= len(pieces) <= MAX_ACTIONS:
        raise DesktopError("use at most six explicit actions in order")
    clauses = []
    for piece in pieces:
        piece = _COURTESY.sub("", piece.strip()).strip(" .?!")
        if not _START.search(piece) or _PROHIBITED.search(piece):
            raise DesktopError("use direct desktop instructions; conditions, repetition, and quoted instructions are not actions")
        piece = re.sub(r"\b(" + "|".join(_NUMBERS) + r")\b", lambda m: str(_NUMBERS[m[0].lower()]), piece, flags=re.I)
        clauses.append((piece, literals))
    return clauses


_KEY_ALIASES = {"control": "ctrl", "ctl": "ctrl", "escape": "Escape", "esc": "Escape",
                "enter": "Return", "return": "Return", "space": "space", "spacebar": "space",
                "tab": "Tab", "backspace": "BackSpace", "delete": "Delete", "del": "Delete",
                "up": "Up", "down": "Down", "left": "Left", "right": "Right",
                "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
                "prior": "Prior", "next": "Next", "super": "super", "win": "super",
                "windows": "super", "shift": "shift", "alt": "alt", "ctrl": "ctrl"}


def key_chord(value):
    if not isinstance(value, str) or len(value) > 80:
        raise DesktopError("keys must be one bounded chord")
    value = re.sub(r"\bpage\s+(up|down)\b", lambda m: "page" + m[1], value.strip(), flags=re.I)
    parts = re.split(r"\s*(?:\+|-|\band\b|\s)\s*", value)
    out = []
    for part in parts:
        if not part:
            continue
        low = part.lower()
        key = _KEY_ALIASES.get(low)
        if key is None and re.fullmatch(r"[a-z0-9]", low):
            key = low
        if key is None and re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", low):
            key = low.upper()
        if key is None:
            raise DesktopError("unsupported key; use a key name or modifier chord")
        out.append(key)
    if not out or len(out) > 5 or len(out) != len(set(out)) or any(k not in ("ctrl", "alt", "shift", "super") for k in out[:-1]) or out[-1] in ("ctrl", "alt", "shift", "super"):
        raise DesktopError("press one key, optionally with distinct modifiers")
    order = {"ctrl": 0, "alt": 1, "shift": 2, "super": 3}
    return "+".join(sorted(out[:-1], key=order.get) + out[-1:])


def normalize(calls):
    if not isinstance(calls, list) or len(calls) > MAX_ACTIONS:
        raise DesktopError("expected a list of at most six actions")
    result = []
    for call in calls:
        if not isinstance(call, dict) or set(call) != {"name", "arguments"} or call.get("name") not in _SCHEMAS:
            raise DesktopError("unknown desktop tool or malformed call")
        name, args = call["name"], copy.deepcopy(call["arguments"])
        schema = _SCHEMAS[name]
        if not isinstance(args, dict) or set(args) - schema["properties"].keys() or set(schema["required"]) - args.keys():
            raise DesktopError("missing or unknown action arguments")
        for key, value in args.items():
            spec = schema["properties"][key]
            expected = int if spec["type"] == "integer" else str
            if type(value) is not expected or "enum" in spec and value not in spec["enum"]:
                raise DesktopError(f"invalid {name}.{key}")
            if expected is int and not spec["minimum"] <= value <= spec["maximum"]:
                raise DesktopError(f"out-of-range {name}.{key}")
            if expected is str and (len(value.encode()) > 512 or any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in value)):
                raise DesktopError("strings must be bounded single-line text")
        if name == "window":
            action = args["action"]
            required = {"action"} if action == "list" else {"action", "target"}
            required |= {"x", "y"} if action == "move" else {"width", "height"} if action == "resize" else set()
            if action != "list":
                args.setdefault("target", "active")
                target = args["target"]
                if target != "active" and not re.fullmatch(r"id:[1-9][0-9]{0,9}|title:.{1,200}", target):
                    raise DesktopError("target must be active, title:EXACT TITLE, or id:DECIMAL ID")
            if set(args) != required:
                raise DesktopError("arguments do not belong to this window action")
        elif name == "pointer":
            action = args["action"]
            if ("x" in args) != ("y" in args) or action in ("move", "drag") and "x" not in args:
                raise DesktopError("supply both x and y coordinates")
            if action == "move" and "button" in args:
                raise DesktopError("moving the pointer does not press a button")
            if action != "move":
                args.setdefault("button", "left")
        elif name == "keyboard":
            field = "text" if args["action"] == "type" else "keys"
            if set(args) != {"action", field} or not args[field]:
                raise DesktopError("provide exactly text for typing or keys for pressing")
            if field == "keys":
                args[field] = key_chord(args[field])
        elif name == "scroll":
            args.setdefault("steps", 3)
        result.append({"name": name, "arguments": args})
    if sum(c["arguments"]["ms"] for c in result if c["name"] == "wait") > 6000:
        raise DesktopError("total pauses must not exceed six seconds")
    return result


def validate(request, calls):
    """Validate the entire proposal, literal arguments, intent and clause order.

    This is a bounded command interface, not a general language-proof system.
    Preview is the default; execution requires explicit caller consent.
    """
    clauses = request_clauses(request)
    actions = normalize(calls)
    if not actions or len(actions) != len(clauses):
        raise DesktopError("the proposal must cover every requested clause exactly once")
    for (clause, literals), call in zip(clauses, actions):
        name, args = call["name"], call["arguments"]
        kind = name + "." + args["action"] if "action" in args else name
        if not re.search(_INTENTS[kind], clause, re.I):
            raise DesktopError("proposed action does not match its request clause")
        quoted = [literals[int(m[1])] for m in re.finditer(r"LITERAL(\d+)", clause)]
        plain = re.sub(r"LITERAL\d+", "", clause).lower()
        if quoted and (name not in ("window", "keyboard") or name == "window" and args["action"] == "list"):
            raise DesktopError("quoted text is only a window title, literal typing payload, or key chord")
        if name == "window":
            target = args.get("target")
            if args["action"] != "list":
                if re.search(r"\b(?:mouse|pointer|cursor)\b", plain):
                    raise DesktopError("a pointer request is not a window action")
                if target == "active":
                    if quoted or re.search(r"\b(?:window\s+)?id\s+\d+", plain) or not re.search(r"\b(?:active|current|this|foreground)\b", plain):
                        raise DesktopError("name the active window or an exact target")
                elif target.startswith("id:"):
                    if not re.search(r"\b(?:window\s+)?id\s+" + re.escape(target[3:]) + r"\b", plain):
                        raise DesktopError("window ID was not requested")
                elif target[6:] not in quoted and not re.search(r"(?<!\w)" + re.escape(target[6:]) + r"(?!\w)", clause, re.I):
                    raise DesktopError("window title was not requested")
                if target.startswith("title:"):
                    if quoted:
                        if quoted != [target[6:]]:
                            raise DesktopError("name exactly one window title")
                    else:
                        if target[6:].lower() in ("active", "current", "this", "window", "foreground"):
                            raise DesktopError("quote a title that is also a window reference")
                        plain = re.sub(re.escape(target[6:]), "", plain, count=1, flags=re.I)
                elif quoted:
                    raise DesktopError("extra quoted text is not part of this window action")
            vocabulary = {
                "list": "list show all desktop windows me my the",
                "focus": "focus activate switch bring to the front forward over",
                "move": "move position place put to at coordinates x y pixels",
                "resize": "resize size set width height to and by of pixels x",
                "minimize": "minimize iconify", "maximize": "maximize",
                "restore": "restore unmaximize to normal size from maximized", "close": "close dismiss",
            }[args["action"]]
            _only_words(plain, vocabulary + " the window active current this foreground my id now")
            if args["action"] not in ("move", "resize") and re.search(r"\d", re.sub(r"\bid\s+\d+", "", plain)):
                raise DesktopError("unaccounted number in the window request")
        if name in ("window", "pointer") and any(k in args for k in ("x", "width")):
            pair = (args["x"], args["y"]) if "x" in args else (args["width"], args["height"])
            # One ordered numeric pair; other quantities cannot silently migrate.
            numeric = re.sub(r"\b(?:window\s+)?id\s+\d+", "", plain)
            numeric = re.sub(r"(?<=\d)[x×](?=\d)", " ", numeric)
            numbers = [int(n) for n in re.findall(r"(?<![\w.-])\d+(?![\w.-])", numeric)]
            if numbers != list(pair):
                raise DesktopError("coordinates or size do not match the ordered requested values")
        if name == "pointer":
            if re.search(r"\bwindow\b", plain):
                raise DesktopError("window positions are not pointer coordinates")
            if "x" not in args and re.search(r"\d", plain):
                raise DesktopError("pointer coordinates were omitted")
            if args["action"] == "click" and re.search(r"\bdouble\b", plain):
                raise DesktopError("a double click needs two clicks")
            if args["action"] != "move":
                button = "right" if re.search(r"\bright\b", plain) else "middle" if re.search(r"\bmiddle\b", plain) else "left"
                if args["button"] != button:
                    raise DesktopError("mouse button differs from the request")
            vocabulary = {"move": "move position place put", "click": "click", "double_click": "double click", "drag": "drag"}[args["action"]]
            _only_words(plain, vocabulary + " the my a mouse pointer cursor button left right middle to at over with here current coordinates position x y pixels")
        elif name == "keyboard":
            if args["action"] == "type":
                if quoted != [args["text"]]:
                    raise DesktopError("quote the exact text to type; it is copied without Enter")
                if not re.fullmatch(r"(?:type|enter|write|insert)\s+(?:(?:the\s+)?(?:literal\s+)?text\s+)?LITERAL\d+\s*", clause, re.I):
                    raise DesktopError("type exact quoted text into the focused window; focus another window in a separate clause")
            else:
                tail = re.sub(r"^.*?\b(?:press|hit|tap|hold)\s+", "", clause, flags=re.I)
                tail = re.sub(r"\b(?:the|key|keys|chord|shortcut|together)\b", "", tail, flags=re.I).strip()
                if quoted:
                    if len(quoted) != 1 or re.sub(r"LITERAL\d+", "", tail).strip():
                        raise DesktopError("press one key or chord")
                    tail = quoted[0]
                if key_chord(tail) != args["keys"]:
                    raise DesktopError("proposed key chord differs from the request")
        elif name == "scroll":
            directions = re.findall(r"\b(up|down|left|right)\b", plain)
            numbers = re.findall(r"\b\d+\b", plain)
            if directions != [args["direction"]] or (numbers and numbers != [str(args["steps"])]) or (not numbers and args["steps"] != 3):
                raise DesktopError("scroll direction or amount differs from the request")
            _only_words(plain, "scroll wheel up down left right the by steps step notches notch through")
        elif name == "wait":
            duration = re.fullmatch(r"(?:wait|pause)(?: for)? (\d+(?:\.\d+)?)\s*(milliseconds?|ms|seconds?|s)", plain.strip())
            if not duration or float(duration[1]) * (1 if duration[2].startswith("m") else 1000) != args["ms"]:
                raise DesktopError("pause duration differs from the request")
    return actions
