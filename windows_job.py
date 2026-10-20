"""Text-to-action contract for Windows desktop control through the Kilix SDK.

Needle plans; the Kilix Windows desktop SDK (`kilix desktop` in WSL, or
`kilix windows --host HOST` from Linux) resolves identities and performs the
work. The model never emits a shell command or a Windows command line:
programs come from a fixed alias table or an executable named in the request,
and command interpreters are refused. Validation happens before any desktop
connection. Window and control names are data; typing never presses Enter.
"""
from __future__ import annotations

import copy
import re
import unicodedata

VERSION = "kilix.windows-actions/v1"
MAX_ACTIONS = 8
MAX_REQUEST_BYTES = 2048
MAX_TEXT_BYTES = 4096
MAX_PAUSE_MS = 10000
MAX_TOTAL_PAUSE_MS = 20000
DEFAULT_WAIT_MS = 10000
MAX_WAIT_MS = 30000

# alias: (launch program, window app, window title hint). Windows owns the
# Calculator frame through ApplicationFrameHost, so its window needs a title.
APPS = {
    "notepad": ("notepad.exe", "notepad.exe", None),
    "wordpad": ("write.exe", "wordpad.exe", None),
    "paint": ("mspaint.exe", "mspaint.exe", None),
    "mspaint": ("mspaint.exe", "mspaint.exe", None),
    "calculator": ("calc.exe", "ApplicationFrameHost.exe", "Calculator"),
    "calc": ("calc.exe", "ApplicationFrameHost.exe", "Calculator"),
    "explorer": ("explorer.exe", "explorer.exe", None),
    "file explorer": ("explorer.exe", "explorer.exe", None),
    "character map": ("charmap.exe", "charmap.exe", None),
    "charmap": ("charmap.exe", "charmap.exe", None),
    "snipping tool": ("SnippingTool.exe", "SnippingTool.exe", None),
    "magnifier": ("magnify.exe", "Magnify.exe", None),
}
# Launch starts a desktop application, not a command. These run their
# arguments as code or change the system, so Needle never starts them.
INTERPRETERS = frozenset("""
cmd powershell pwsh powershell_ise wsl wslhost bash sh wscript cscript mshta rundll32 regsvr32
msiexec reg regedit regedt32 schtasks at sc net net1 runas python pythonw py node conhost
wt certutil bitsadmin forfiles wmic msbuild installutil cmstp curl ftp tftp taskkill shutdown
format diskpart bcdedit vssadmin icacls takeown cipher sdelete psexec mmc control
hh pcalua msdt syncappvpublishingserver presentationhost ieexec infdefaultinstall odbcconf regasm
regsvcs msxsl appvlp atbroker mavinject scriptrunner dotnet java javaw perl ruby php deno bun
ssh scp sftp winrs pypy rscript r julia lua luajit tclsh wish irb jshell groovy kotlin scala zsh fish
busybox nu xonsh csi fsi csc vbc jsc ipython ipy jupyter pip uv uvx npm npx pnpm yarn conda winget
choco scoop git git-bash
""".split())
# Windows whose input runs whatever is typed: consoles, terminals (Kilix
# included), Explorer's address bar, Start/search and Task Manager's Run.
SHELL_APPS = frozenset("""
cmd.exe powershell.exe pwsh.exe powershell_ise.exe conhost.exe openconsole.exe windowsterminal.exe
wt.exe wsl.exe wslhost.exe bash.exe mintty.exe kitty.exe kilix.exe explorer.exe searchapp.exe
searchhost.exe searchui.exe startmenuexperiencehost.exe shellexperiencehost.exe taskmgr.exe
regedit.exe mmc.exe
""".split())
SHELL_TITLES = frozenset(["run"])
# Explorer opens a path with its associated program, so it runs a document as
# readily as cmd would; it may only open its own window.
NO_DOCUMENT = frozenset(["explorer"])
CONTROL_TYPES = {
    "button": "Button", "field": "Edit", "box": "Edit", "text box": "Edit", "textbox": "Edit",
    "edit box": "Edit", "input": "Edit", "menu item": "MenuItem", "menu": "MenuItem",
    "tab": "TabItem", "checkbox": "CheckBox", "check box": "CheckBox", "radio button": "RadioButton",
    "link": "Hyperlink", "hyperlink": "Hyperlink", "list item": "ListItem", "item": "ListItem",
    "combo box": "ComboBox", "dropdown": "ComboBox", "drop-down": "ComboBox",
    "document": "Document", "text area": "Document", "slider": "Slider", "tree item": "TreeItem",
}
UIA_TYPES = frozenset(CONTROL_TYPES.values()) | frozenset("""
Calendar CheckBox ComboBox Custom DataGrid DataItem Document Edit Group Header HeaderItem Hyperlink
Image List ListItem Menu MenuBar MenuItem Pane ProgressBar RadioButton ScrollBar SemanticZoom
Separator Slider Spinner SplitButton StatusBar Tab TabItem Table Text Thumb TitleBar ToolBar
ToolTip Tree TreeItem Window AppBar
""".split())
# The SDK's key names. Needle's chords use the same spelling, joined by '+'.
SDK_KEYS = frozenset(["CTRL", "SHIFT", "ALT", "WIN", "ENTER", "TAB", "ESC", "SPACE", "BACKSPACE",
                      "DELETE", "HOME", "END", "PAGEUP", "PAGEDOWN", "LEFT", "RIGHT", "UP", "DOWN",
                      *("F" + str(i) for i in range(1, 13)), *"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"])
MODIFIERS = ("CTRL", "ALT", "SHIFT", "WIN")


def _enum(*values):
    return {"type": "string", "enum": list(values)}


def _tool(name, description, properties, required):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}


_TEXT = {"type": "string"}
_WINDOW = {"app": {"type": "string", "description": "executable, e.g. notepad.exe"},
           "title": {"type": "string", "description": "part of the window title"}}
_CONTROL = {"name": _TEXT, "control_type": {"type": "string", "description": "UIA type, e.g. Button or Edit"},
            "automation_id": _TEXT}
_POINT = {"x": {"type": "integer", "minimum": 0, "maximum": 16383},
          "y": {"type": "integer", "minimum": 0, "maximum": 16383}}
TOOLS = [
    _tool("launch", "Start a Windows application by name or executable; never a shell. document is one file path to open.", {
        "program": _TEXT, "document": _TEXT}, ["program"]),
    _tool("window", "List or find windows, or focus, minimize, maximize, restore, close, move or resize one window. "
          "Omit app and title to mean the window used earlier in the request.", {
              "action": _enum("list", "find", "focus", "minimize", "maximize", "restore", "close", "move", "resize"),
              **_WINDOW, "x": {"type": "integer", "minimum": -32768, "maximum": 32767},
              "y": {"type": "integer", "minimum": -32768, "maximum": 32767},
              "width": {"type": "integer", "minimum": 1, "maximum": 16384},
              "height": {"type": "integer", "minimum": 1, "maximum": 16384}}, ["action"]),
    _tool("control", "Inspect a window's UI Automation controls, find controls, invoke (click) one, or set its text value.", {
        "action": _enum("inspect", "find", "invoke", "set_value"), **_WINDOW, **_CONTROL, "text": _TEXT}, ["action"]),
    _tool("keyboard", "Type exact literal text without Enter, or press one key or chord (ctrl+s, enter), in a window.", {
        "action": _enum("type", "press"), "text": _TEXT, "keys": _TEXT, **_WINDOW}, ["action"]),
    _tool("pointer", "Click at x/y pixels relative to a window's top-left corner.", {
        "action": _enum("click", "double_click"), **_POINT, "button": _enum("left", "right", "middle"), **_WINDOW},
        ["action", "x", "y"]),
    _tool("scroll", "Scroll a window up or down by wheel steps, at its center or at window-relative x/y.", {
        "direction": _enum("up", "down"), "steps": {"type": "integer", "minimum": 1, "maximum": 10}, **_POINT, **_WINDOW},
        ["direction"]),
    _tool("wait", "Wait for a window or control to be present, visible, enabled or closed, or pause for ms.", {
        "for": _enum("window", "control", "time"), "state": _enum("present", "visible", "enabled", "closed"),
        "ms": {"type": "integer", "minimum": 1, "maximum": MAX_WAIT_MS}, **_WINDOW, **_CONTROL}, ["for"]),
]
_SCHEMAS = {t["name"]: t["parameters"] for t in TOOLS}
READ_ONLY = frozenset([("window", "list"), ("window", "find"), ("control", "inspect"), ("control", "find")])


class WindowsActionError(ValueError):
    pass


def kind(call):
    args = call["arguments"]
    return call["name"] + ("." + args["action"] if "action" in args else "." + args["for"] if "for" in args else "")


def read_only(actions):
    return all((c["name"], c["arguments"].get("action")) in READ_ONLY for c in actions)


def _single_line(value, label, limit=512):
    if not isinstance(value, str) or not value or len(value.encode()) > limit:
        raise WindowsActionError(f"{label} must be 1-{limit} bytes of text")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in value):
        raise WindowsActionError(f"{label} must be one line without control characters")
    return value


def program_name(value):
    """Return the launch program for an alias or executable, refusing interpreters."""
    _single_line(value, "program", 1024)
    alias = APPS.get(re.sub(r"\s+", " ", value.strip().lower()))
    if alias:
        return alias[0]
    if value.strip() != value or '"' in value or not re.search(r"\.exe$", value, re.I):
        raise WindowsActionError("name an application alias or an executable ending in .exe")
    if not (re.fullmatch(r"[\w.-]+", value) or re.fullmatch(r"[A-Za-z]:\\[^/:*?<>|\"%^&]+", value)):
        raise WindowsActionError("program must be an executable name or an absolute Windows path")
    base = re.split(r"[\\/]", value)[-1][:-4].lower()
    if "~" in base:
        raise WindowsActionError("short (8.3) program names are not launched; use the full executable name")
    # python3.12.exe and pwsh7.exe are still python and pwsh.
    if base in INTERPRETERS or re.sub(r"[\d.]+$", "", base) in INTERPRETERS:
        raise WindowsActionError("command interpreters and system tools are not launched; open them yourself")
    return value


def window_app(value):
    """Map an alias to the executable that owns its window; check executables."""
    _single_line(value, "app", 1024)
    alias = APPS.get(re.sub(r"\s+", " ", value.strip().lower()))
    if alias:
        return alias[1]
    if not re.search(r"\.exe$", value, re.I) or not (
            re.fullmatch(r"[^\\/:*?<>|\"]+", value) or re.fullmatch(r"[A-Za-z]:\\[^/:*?<>|\"]+", value)):
        raise WindowsActionError("app must be an executable name ending in .exe or an absolute path")
    return value


_KEY_ALIASES = {"control": "CTRL", "ctl": "CTRL", "ctrl": "CTRL", "alt": "ALT", "shift": "SHIFT",
                "win": "WIN", "windows": "WIN", "super": "WIN", "enter": "ENTER", "return": "ENTER",
                "tab": "TAB", "escape": "ESC", "esc": "ESC", "space": "SPACE", "spacebar": "SPACE",
                "backspace": "BACKSPACE", "delete": "DELETE", "del": "DELETE", "home": "HOME", "end": "END",
                "pageup": "PAGEUP", "pagedown": "PAGEDOWN", "pgup": "PAGEUP", "pgdn": "PAGEDOWN",
                "left": "LEFT", "right": "RIGHT", "up": "UP", "down": "DOWN"}


class LauncherChordError(WindowsActionError):
    pass


def key_chord(value):
    """Canonical SDK chord such as CTRL+SHIFT+S, from ctrl+shift+s or 'control s'."""
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise WindowsActionError("keys must be one bounded chord")
    value = re.sub(r"\bpage\s+(up|down)\b", lambda m: "page" + m[1], value.strip(), flags=re.I)
    parts = [p for p in re.split(r"\s*(?:\+|-|\band\b|\s)\s*", value) if p]
    out = []
    for part in parts:
        low = part.lower()
        key = _KEY_ALIASES.get(low)
        if key is None and re.fullmatch(r"[a-z0-9]", low):
            key = low.upper()
        if key is None and re.fullmatch(r"f(?:[1-9]|1[0-2])", low):
            key = low.upper()
        if key is None or key not in SDK_KEYS:
            raise WindowsActionError("unsupported key; use a key name or modifier chord")
        out.append(key)
    if (not out or len(out) > 5 or len(out) != len(set(out))
            or any(k not in MODIFIERS for k in out[:-1]) or out[-1] in MODIFIERS):
        raise WindowsActionError("press one key, optionally with distinct modifiers")
    chord = "+".join(sorted(out[:-1], key=MODIFIERS.index) + out[-1:])
    # Windows-key chords and Ctrl+Esc reach Start, Run and search, where typing launches programs.
    if "WIN" in out or chord in ("CTRL+ESC", "CTRL+SHIFT+ESC"):
        raise LauncherChordError("Windows-key and Start menu chords are not sent; they open system launchers")
    return chord


def _check_window(args, name):
    for field in ("app", "title"):
        if field in args:
            _single_line(args[field], field, 1024 if field == "app" else 200)
    if "app" in args:
        alias = APPS.get(re.sub(r"\s+", " ", args["app"].strip().lower()))
        args["app"] = window_app(args["app"])
        if alias and alias[2] and "title" not in args:
            # Calculator's window belongs to ApplicationFrameHost, like every packaged app's.
            args["title"] = alias[2]


def _alias_title(args, clause, literals):
    """The title an app alias implies, when that alias is written in the clause."""
    return any(owner.lower() == args.get("app", "").lower() and title == args.get("title")
               and _mentions(alias, clause, literals) for alias, (_program, owner, title) in APPS.items() if title)


def normalize(calls):
    """Check structure, types and bounds; canonicalize keys, apps and steps."""
    if not isinstance(calls, list) or not 1 <= len(calls) <= MAX_ACTIONS:
        raise WindowsActionError(f"expected a list of 1-{MAX_ACTIONS} actions")
    result = []
    for call in calls:
        if not isinstance(call, dict) or set(call) != {"name", "arguments"} or call.get("name") not in _SCHEMAS:
            raise WindowsActionError("unknown Windows tool or malformed call")
        name, args = call["name"], copy.deepcopy(call["arguments"])
        schema = _SCHEMAS[name]
        if not isinstance(args, dict) or set(args) - schema["properties"].keys() or set(schema["required"]) - args.keys():
            raise WindowsActionError(f"missing or unknown {name} arguments")
        for key, value in args.items():
            spec = schema["properties"][key]
            expected = int if spec["type"] == "integer" else str
            if type(value) is not expected or "enum" in spec and value not in spec["enum"]:
                raise WindowsActionError(f"invalid {name}.{key}")
            if expected is int and not spec["minimum"] <= value <= spec["maximum"]:
                raise WindowsActionError(f"out-of-range {name}.{key}")
        action = args.get("action")
        if name == "launch":
            args["program"] = program_name(args["program"])
            if "document" in args:
                _single_line(args["document"], "document", 1024)
                if args["document"][0] in "-/":
                    raise WindowsActionError("document must be a file path, not an option")
                if re.split(r"[\\/]", args["program"])[-1][:-4].lower() in NO_DOCUMENT:
                    raise WindowsActionError("Explorer opens a path with its associated program; name the application to open it in")
        elif name == "window":
            _check_window(args, name)
            needs = {"move": {"x", "y"}, "resize": {"width", "height"}}.get(action, set())
            if set(args) & {"x", "y", "width", "height"} != needs:
                raise WindowsActionError("coordinates belong only to move; width/height only to resize")
            if action == "list" and set(args) != {"action"}:
                raise WindowsActionError("window list takes no target; use find to filter")
            if action == "find" and not set(args) & {"app", "title"}:
                raise WindowsActionError("window find needs an app or title")
        elif name == "control":
            _check_window(args, name)
            selector = set(args) & {"name", "control_type", "automation_id"}
            for field in selector:
                _single_line(args[field], field, 256)
            if "control_type" in args and args["control_type"] not in UIA_TYPES:
                raise WindowsActionError("unknown UI Automation control type")
            if action == "inspect" and (selector or "text" in args):
                raise WindowsActionError("inspect reads the whole window; it takes no selector")
            if action != "inspect" and not selector:
                raise WindowsActionError("name a control by name, control_type or automation_id")
            if (action == "set_value") != ("text" in args):
                raise WindowsActionError("text belongs only to set_value")
            if "text" in args:
                _single_line(args["text"], "text", MAX_TEXT_BYTES)
        elif name == "keyboard":
            _check_window(args, name)
            field = "text" if action == "type" else "keys"
            if set(args) - {"app", "title"} != {"action", field}:
                raise WindowsActionError("provide exactly text for typing or keys for pressing")
            if field == "keys":
                args["keys"] = key_chord(args["keys"])
            else:
                _single_line(args["text"], "text", MAX_TEXT_BYTES)
        elif name == "pointer":
            _check_window(args, name)
            args.setdefault("button", "left")
        elif name == "scroll":
            _check_window(args, name)
            if ("x" in args) != ("y" in args):
                raise WindowsActionError("supply both x and y, or neither for the window center")
            args.setdefault("steps", 3)
        elif name == "wait":
            what = args["for"]
            _check_window(args, name)
            selector = set(args) & {"name", "control_type", "automation_id"}
            for field in selector:
                _single_line(args[field], field, 256)
            if "control_type" in args and args["control_type"] not in UIA_TYPES:
                raise WindowsActionError("unknown UI Automation control type")
            if what == "time":
                if set(args) != {"for", "ms"} or args["ms"] > MAX_PAUSE_MS:
                    raise WindowsActionError(f"a pause takes only ms, at most {MAX_PAUSE_MS}")
            else:
                if "state" not in args:
                    raise WindowsActionError("waits need a state: present, visible, enabled or closed")
                if (what == "control") != bool(selector):
                    raise WindowsActionError("control waits need a selector; window waits take none")
                args.setdefault("ms", DEFAULT_WAIT_MS)
                if args["ms"] < 100:
                    raise WindowsActionError("condition waits need at least 100 ms")
        result.append({"name": name, "arguments": args})
    if sum(c["arguments"]["ms"] for c in result if c["name"] == "wait" and c["arguments"]["for"] == "time") > MAX_TOTAL_PAUSE_MS:
        raise WindowsActionError("total pauses must not exceed twenty seconds")
    return result


# Request clauses ------------------------------------------------------------

# Double quotes are raw so Windows paths keep their backslashes; there are no
# escapes. Text containing a double quote uses single or curly quotes.
_QUOTE = re.compile(r'"[^"]*"|“[^”]*”|(?<!\w)\x27[^\x27]*\x27(?!\w)')
_NUMBERS = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty".split(),
                    [*range(21), 30]))
_COURTESY = re.compile(r"^(?:(?:please|kindly|now|can you|could you|would you|will you|i want you to|i would like you to|i'd like you to|i need you to)\s+)+", re.I)
VERBS = (r"(?:open|launch|start|run|list|show|inspect|find|locate|look|focus|activate|switch|bring|minimi[sz]e|maximi[sz]e|"
         r"restore|close|move|resize|type|enter|write|insert|press|hit|tap|click|double[ -]click|right[ -]click|"
         r"invoke|set|fill|clear|scroll|wait|pause)")
_START = re.compile(r"^" + VERBS + r"\b", re.I)
_PROHIBITED = re.compile(r"\b(?:don't|do not|not|never|avoid|unless|if|whenever|repeat|loop|forever|every|instead|ignore|pretend|"
                         r"example|said|says|quoted|suggested|install|uninstall|download|execute|sudo|shutdown|reboot|"
                         r"format|delete all|password)\b", re.I)


def _literal(match):
    return match.group()[1:-1]


def request_clauses(request):
    """Split one direct request into ordered clauses with its quoted literals.

    Literals become LITERALn tokens, so their content never shapes a clause.
    Conditions, negation and repetition refuse the whole request.
    """
    if not isinstance(request, str) or not request.strip() or len(request.encode()) > MAX_REQUEST_BYTES:
        raise WindowsActionError(f"provide a request of 1-{MAX_REQUEST_BYTES} UTF-8 bytes")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in request):
        raise WindowsActionError("requests must be a single line without control characters")
    literals = []

    def hide(match):
        literals.append(_literal(match))
        return f" LITERAL{len(literals) - 1} "
    masked = _QUOTE.sub(hide, request)
    if '"' in masked or "“" in masked or "”" in masked:
        raise WindowsActionError("unclosed quoted text")
    pieces = re.split(r"\s*(?:;|,?\s+(?:and\s+)?then\s+|,\s+(?=" + VERBS + r"\b)|\s+and\s+(?=" + VERBS + r"\b))\s*",
                      masked.strip(), flags=re.I)
    if not 1 <= len(pieces) <= MAX_ACTIONS:
        raise WindowsActionError(f"use at most {MAX_ACTIONS} explicit actions in order")
    clauses = []
    for piece in pieces:
        piece = re.sub(r"\s+", " ", _COURTESY.sub("", piece.strip()).strip(" .?!"))
        if not _START.search(piece):
            raise WindowsActionError("start each step with an action such as open, focus, type, press, click or wait")
        if _PROHIBITED.search(piece):
            raise WindowsActionError("use direct desktop instructions; conditions, repetition and quoted instructions are not actions")
        piece = re.sub(r"\b(" + "|".join(_NUMBERS) + r")\b", lambda m: str(_NUMBERS[m[0].lower()]), piece, flags=re.I)
        clauses.append((piece, literals))
    return clauses


def clause_literals(clause, literals):
    return [literals[int(m[1])] for m in re.finditer(r"LITERAL(\d+)", clause)]


def clause_numbers(clause):
    plain = re.sub(r"LITERAL\d+", " ", clause)
    plain = re.sub(r"(?<=\d)[x×](?=\d)", " ", plain)
    return [int(n) for n in re.findall(r"(?<![\w.-])-?\d+(?![\w.-])", plain)]


_INTENTS = {
    "launch": r"\b(?:open|launch|start|run)\b",
    "window.list": r"\b(?:list|show)\b.*\bwindows\b",
    "window.find": r"\b(?:find|locate|look)\b",
    "window.focus": r"\b(?:focus|activate|switch|bring)\b",
    "window.minimize": r"\bminimi[sz]e\b",
    "window.maximize": r"\bmaximi[sz]e\b",
    "window.restore": r"\brestore\b",
    "window.close": r"\bclose\b",
    "window.move": r"\bmove\b",
    "window.resize": r"\bresize\b",
    "control.inspect": r"\b(?:list|show|inspect)\b",
    "control.find": r"\b(?:find|locate|look)\b",
    "control.invoke": r"\b(?:click|press|invoke|hit|tap)\b",
    "control.set_value": r"\b(?:set|fill|type|enter|write|insert|clear)\b",
    "keyboard.type": r"\b(?:type|enter|write|insert)\b",
    "keyboard.press": r"\b(?:press|hit|tap)\b",
    "pointer.click": r"\b(?:click|right[ -]click)\b",
    "pointer.double_click": r"\bdouble[ -]click\b",
    "scroll": r"\bscroll\b",
    "wait.window": r"\b(?:wait)\b",
    "wait.control": r"\b(?:wait)\b",
    "wait.time": r"\b(?:wait|pause)\b",
}


def _mentions(value, clause, literals):
    """A string argument is grounded when quoted exactly or written in the clause."""
    if value in literals:
        return True
    plain = re.sub(r"LITERAL\d+", " ", clause)
    return bool(re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", plain, re.I))


def _app_mentioned(app, clause, literals, *, launch=False):
    for alias, (program, owner, _title) in APPS.items():
        if (program if launch else owner).lower() == app.lower() and _mentions(alias, clause, literals):
            return True
    return _mentions(app, clause, literals)


def validate(request, calls):
    """Ground a proposal in the request: order, intent, literals and numbers.

    Every clause yields one action. String arguments must be quoted or written
    in that clause, numbers must match its ordered numbers, and every quoted
    literal must be used. A bounded interface, not a general language proof.
    """
    clauses = request_clauses(request)
    actions = normalize(calls)
    if len(actions) != len(clauses):
        raise WindowsActionError("the proposal must cover every requested clause exactly once")
    for (clause, literals), call in zip(clauses, actions):
        quoted = clause_literals(clause, literals)
        name, args = call["name"], call["arguments"]
        if not re.search(_INTENTS[kind(call)], clause, re.I):
            raise WindowsActionError(f"proposed {kind(call)} does not match its request clause")
        used = []
        for field in ("title", "name", "automation_id", "text", "document"):
            if field in args:
                if field == "title" and _alias_title(args, clause, quoted):
                    continue
                if not _mentions(args[field], clause, quoted):
                    raise WindowsActionError(f"{name}.{field} was not requested")
                used.append(args[field])
        if "control_type" in args:
            nouns = [noun for noun, uia in CONTROL_TYPES.items() if uia == args["control_type"]]
            if not any(_mentions(noun, clause, quoted) for noun in nouns) and not _mentions(args["control_type"], clause, quoted):
                raise WindowsActionError("control type was not requested")
        if name == "launch":
            if not _app_mentioned(args["program"], clause, quoted, launch=True):
                raise WindowsActionError("program was not requested")
            if args["program"] in quoted:
                used.append(args["program"])
        elif "app" in args:
            if not _app_mentioned(args["app"], clause, quoted):
                raise WindowsActionError("window app was not requested")
            # ApplicationFrameHost.exe grounded by "calculator" must keep Calculator's title.
            implied = {title for alias, (_program, owner, title) in APPS.items()
                       if title and owner.lower() == args["app"].lower() and _mentions(alias, clause, quoted)}
            if implied and not _mentions(args["app"], clause, quoted) and args.get("title") not in implied:
                raise WindowsActionError("the window title implied by the app was dropped")
            if args["app"] in quoted:
                used.append(args["app"])
        if "text" in args and args["text"] not in quoted:
            raise WindowsActionError("quote the exact text to type or set; it is typed without Enter")
        if name == "keyboard" and args["action"] == "press":
            tail = re.sub(r"^.*?\b(?:press|hit|tap)\s+", "", clause, flags=re.I)
            tail = re.sub(r"\s+\b(?:in|into|on)\b.*$", "", tail, flags=re.I)
            tail = re.sub(r"\b(?:the|key|keys|chord|shortcut|together)\b", "", tail, flags=re.I).strip()
            if re.fullmatch(r"LITERAL(\d+)", tail):
                tail = literals[int(tail[7:])]
            if key_chord(tail) != args["keys"]:
                raise WindowsActionError("proposed key chord differs from the request")
        if set(quoted) - set(used) - ({args["keys"]} if "keys" in args else set()) and not (
                name == "keyboard" and args["action"] == "press"):
            raise WindowsActionError("quoted text in the request is not used by its action")
        numbers = clause_numbers(clause)
        expected = [args[f] for f in ("x", "y", "width", "height", "steps") if f in args]
        if name == "wait":
            if args["for"] == "time" or re.search(r"\b\d+\s*(?:ms|milliseconds?|s|secs?|seconds?)\b", clause, re.I):
                duration = re.search(r"\b(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|secs?|seconds?)\b", re.sub(r"LITERAL\d+", " ", clause), re.I)
                if not duration or round(float(duration[1]) * (1 if duration[2].lower().startswith("m") else 1000)) != args["ms"]:
                    raise WindowsActionError("wait duration differs from the request")
                expected = [int(float(duration[1]))] if float(duration[1]).is_integer() else []
                numbers = [n for n in numbers if n == expected[0]] if expected else []
        if name == "scroll" and "steps" in args and args["steps"] == 3 and 3 not in numbers:
            expected = [args[f] for f in ("x", "y") if f in args]
        if name == "keyboard" and args["action"] == "press":
            numbers = expected = []  # Digits in a chord are keys; the chord check above covers them.
        if numbers != expected:
            raise WindowsActionError("numbers do not match the ordered requested values")
        if name == "pointer":
            button = "right" if re.search(r"\bright[ -]click\b|\bright\b", clause, re.I) else \
                "middle" if re.search(r"\bmiddle\b", clause, re.I) else "left"
            if args["button"] != button:
                raise WindowsActionError("mouse button differs from the request")
        if name == "scroll" and not re.search(r"\b" + args["direction"] + r"\b", clause, re.I):
            raise WindowsActionError("scroll direction differs from the request")
    return actions
