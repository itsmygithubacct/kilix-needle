"""The tool set Needle sees, and what its calls are allowed to become.

Needle's confidence does not gate anything here. Measured on the pinned
engine, "close tab 2 and go to tab 1" produced two close_tab calls at
confidence 0.99 and "go to the left pane" produced close_pane at 0.41. So a
call becomes an action only through checks that do not depend on the model:

- a close needs a closing verb in the request, and typing a command needs a
  run/type verb;
- every free-text value must appear in the request (a program, name or
  command the user never said is refused, not guessed);
- the values are normalised to a small vocabulary before resolution.

Whether an admitted action then runs straight away or waits for a yes is
`Action.risky`, decided by kind and never by confidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata

SIDES = ("left", "right", "above", "below")
LAYOUTS = ("tall", "fat", "grid", "horizontal", "vertical", "splits", "stack")
DIRECTIONS = ("wider", "narrower", "taller", "shorter")


def _target(what: str, choices: str) -> dict:
    return {"type": "string", "description": f"{choices}, or the name or program of a {what}"}


TOOLS = [
    {"name": "open_pane",
     "description": "Split the window: open a new pane next to the current one. "
                    "Use for split, new pane, open X in a pane",
     "parameters": {"type": "object", "properties": {
         "side": {"type": "string", "enum": list(SIDES),
                  "description": "which side of the current pane"},
         "program": {"type": "string",
                     "description": "program to start in it, only if the user names one"},
         "name": {"type": "string",
                  "description": "what to call the pane, only if the user names it"}},
         "required": []}},
    {"name": "open_tab",
     "description": "Open a new tab. Use for new tab, open X in a tab",
     "parameters": {"type": "object", "properties": {
         "program": {"type": "string",
                     "description": "program to start in it, only if the user names one"},
         "name": {"type": "string",
                  "description": "what to call the tab, only if the user names it"}},
         "required": []}},
    {"name": "close_pane",
     "description": "Close, kill or quit a pane. Only when the user says close, kill, quit or exit",
     "parameters": {"type": "object", "properties": {
         "pane": _target("pane", "current, left, right, above, below")},
         "required": ["pane"]}},
    {"name": "close_tab",
     "description": "Close, kill or quit a tab. Only when the user says close, kill, quit or exit",
     "parameters": {"type": "object", "properties": {
         "tab": _target("tab", "current, a tab number")},
         "required": ["tab"]}},
    {"name": "go_to_pane",
     "description": "Go to, switch to, move to or focus another pane without changing it",
     "parameters": {"type": "object", "properties": {
         "pane": _target("pane", "left, right, above, below")},
         "required": ["pane"]}},
    {"name": "go_to_tab",
     "description": "Go to or switch to another tab without changing it",
     "parameters": {"type": "object", "properties": {
         "tab": _target("tab", "next, previous, a tab number")},
         "required": ["tab"]}},
    {"name": "arrange_panes",
     "description": "Rearrange the panes in this tab",
     "parameters": {"type": "object", "properties": {
         "layout": {"type": "string", "enum": list(LAYOUTS),
                    "description": "grid; stack shows one pane at a time; vertical is "
                                   "side by side; horizontal is one above another"}},
         "required": ["layout"]}},
    {"name": "rename_tab",
     "description": "Rename or call the current tab something",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string", "description": "the new name"}},
         "required": ["name"]}},
    {"name": "resize_pane",
     "description": "Make the current pane wider, narrower, taller or shorter",
     "parameters": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": list(DIRECTIONS)},
         "amount": {"type": "integer", "minimum": 1, "maximum": 50,
                    "description": "number of cells, only if the user gives one"}},
         "required": ["direction"]}},
    {"name": "run_in_pane",
     "description": "Type a command into an existing pane and press enter. "
                    "Use for run X in a pane, type X",
     "parameters": {"type": "object", "properties": {
         "pane": _target("pane", "current, left, right, above, below"),
         "command": {"type": "string", "description": "the exact command text"}},
         "required": ["pane", "command"]}},
]

LEGACY_TOOLS = TOOLS.copy()  # Needle 2 checkpoints keep their original ten schemas.
TOOLS += [{'name': 'maximize_pane',
  'description': 'Show one pane full-size (zoom or maximize it), or restore the previous '
                 'layout',
  'parameters': {'type': 'object',
                 'properties': {'pane': {'type': 'string',
                                         'description': 'current, left, right, above, below, '
                                                        'or the name or program of a pane'},
                                'restore': {'type': 'boolean',
                                            'description': 'true to un-maximize and restore '
                                                           'the layout'}},
                 'required': []}},
 {'name': 'rename_pane',
  'description': 'Rename or title a pane',
  'parameters': {'type': 'object',
                 'properties': {'pane': {'type': 'string',
                                         'description': 'current, left, right, above, below, '
                                                        'or the name or program of a pane'},
                                'name': {'type': 'string', 'description': 'the new name'}},
                 'required': ['name']}},
 {'name': 'swap_panes',
  'description': 'Swap the current pane with its neighbour on one side',
  'parameters': {'type': 'object',
                 'properties': {'side': {'type': 'string',
                                         'enum': ['left', 'right', 'above', 'below'],
                                         'description': 'which neighbour to swap with'}},
                 'required': ['side']}},
 {'name': 'move_tab',
  'description': 'Move the current tab left or right in the tab bar, or to a position',
  'parameters': {'type': 'object',
                 'properties': {'direction': {'type': 'string', 'enum': ['left', 'right']},
                                'position': {'type': 'integer',
                                             'minimum': 1,
                                             'maximum': 9,
                                             'description': 'tab position, only if the user '
                                                            'gives one'}},
                 'required': []}}]

TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)
# Gerunds too ("would you mind closing the current tab", blind supplement). Not
# "running": it introduces a program name ("the pane running top").
_CLOSE_VERB = re.compile(r"\b(close|closing|kill|killing|quit|quitting|exit|exiting|shut|shutting)\b",
                         re.IGNORECASE)
_RUN_VERB = re.compile(r"\b(run|type|typing|execute|executing|enter|entering)\b", re.IGNORECASE)
_CURRENT = {"", "current", "this", "here", "active", "focused", "the current one"}
_ORDINALS = {"first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
             "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "last": "last"}
_RELATIVE = {"next": "next", "previous": "previous", "prev": "previous", "last one": "previous",
             "back": "previous"}
_SIDE_WORDS = {"left": "left", "right": "right", "left-hand": "left", "right-hand": "right",
               "above": "above", "up": "above", "top": "above",
               "upper": "above", "below": "below", "down": "below", "bottom": "below",
               "under": "below", "underneath": "below", "beneath": "below", "lower": "below"}
_CARDINALS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
              "seven": "7", "eight": "8", "nine": "9"}
_LEADING_VERBS = re.compile(r"^(?:running|run|with|start|starting|launch)\s+", re.IGNORECASE)
_FILLER = re.compile(r"\b(?:the|this|that|one|pane|panes|tab|tabs|window|on|to|of)\b",
                     re.IGNORECASE)


@dataclass(frozen=True)
class Action:
    kind: str
    args: dict = field(default_factory=dict)

    @property
    def risky(self) -> bool:
        """Closing, typing into a pane, or starting a program waits for a yes."""
        if self.kind in ("close_pane", "close_tab", "run_in_pane"):
            return True
        return self.kind in ("open_pane", "open_tab") and bool(self.args.get("program"))


@dataclass(frozen=True)
class Refusal:
    kind: str
    reason: str


_UNITS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
          "fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}


# Number words up to fifty for tab references ("close tab number twelve":
# measured, the words stopped at nine and "twelve" became a tab name).
for _n in range(10, 51):
    _t, _u = divmod(_n, 10)
    for _w in ([_UNITS[_n]] if _n < 20 else
               [_TENS[_t]] if _u == 0 else [f"{_TENS[_t]} {_UNITS[_u]}", f"{_TENS[_t]}-{_UNITS[_u]}"]):
        _CARDINALS[_w] = str(_n)


_MOVE = re.compile(r"\b(?:go|goes|going|gone|switch|switching|focus|focusing|jump|move|"
                   r"hop|flip|show|select|head|take|bring|back|return|visit|cycle|"
                   r"moving|jumping|hopping|flipping|showing|selecting|heading|taking|"
                   r"bringing|returning|visiting|cycling|"
                   r"activate|change to|over to|into)\b", re.I)
_BARE_REFERENCE = re.compile(r"^\s*(?:the\s+)?(?:(?:next|previous|prev|last|first|"
                             r"left|right|upper|lower|top|bottom)\s+(?:tab|pane|split|window)|"
                             r"(?:tab|pane)\s+(?:\w+)|\w+\s+(?:tab|pane))[\s.!?]*$", re.I)


_DIRECTION_WORDS = {
    "wider": r"wider|widen|widens|broaden|broader|bigger|larger|expand|stretch|grow|extend|"
             r"more width",
    "narrower": r"narrower|narrow|thinner|slimmer|shrink|squeeze|smaller|less width",
    "taller": r"taller|higher|heighten|bigger|larger|grow|stretch|expand|extend|more height",
    "shorter": r"shorter|lower|flatten|squash|shrink|smaller|less height",
}
_WIDTH = re.compile(r"\b(?:width|wide|horizontal\w*|sideways|side to side)\b", re.I)
_HEIGHT = re.compile(r"\b(?:height|tall|high|vertical\w*|up and down|upwards?|downwards?)\b",
                     re.I)


def _says_direction(direction: str, prompt: str) -> bool:
    """The request says this direction; "shrink" or "grow" name the axis only
    together with width or height words."""
    if not re.search(rf"\b(?:{_DIRECTION_WORDS[direction]})\b", prompt, re.I):
        return False
    horizontal = direction in ("wider", "narrower")
    if re.search(r"\b(?:wider|widen|broaden|broader|narrower|narrow|thinner|taller|shorter|"
                 r"heighten)\b", prompt, re.I):
        exact = {"wider": r"wider|widen|widens|broaden|broader",
                 "narrower": r"narrower|narrow|thinner|slimmer",
                 "taller": r"taller|heighten", "shorter": r"shorter"}[direction]
        return bool(re.search(rf"\b(?:{exact})\b", prompt, re.I))
    if _WIDTH.search(prompt) and not horizontal or _HEIGHT.search(prompt) and horizontal:
        return False
    return True


def _moves(prompt: str) -> bool:
    """A go-to needs a movement verb, or a bare reference such as "next tab".
    Measured (tuned model): "write a haiku about terminals" -> go to the tab
    named terminals."""
    return bool(_MOVE.search(prompt) or any(_BARE_REFERENCE.match(_POLITE_SUFFIX.sub("", c))
                                            for c in _clauses(prompt)))


def _says_number(n: int, prompt: str) -> bool:
    """n appears as a whole number or in words ("by twenty-five"), never as part
    of another number: 5 is not said by "15"."""
    forms = [str(n)]
    if n < 20:
        forms.append(_UNITS[n])
    elif n <= 59:
        tens, unit = divmod(n, 10)
        forms += [_TENS[tens]] if unit == 0 else [f"{_TENS[tens]}[ -]{_UNITS[unit]}"]
    return any(re.search(rf"(?<![\w-]){form}(?![\w-])", prompt, re.I) for form in forms)


def _grounded(value: str, prompt: str) -> bool:
    """The value occurs in the request as whole words, not inside a word.

    Measured (tuning run 2): "stack the panes" -> go_to pane "a", admitted by a
    substring match on the "a" in "stack".
    """
    return re.search(rf"(?<![\w]){re.escape(value.casefold())}(?![\w])",
                     prompt.casefold()) is not None


def _text(args: dict, key: str) -> str:
    value = args.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _target_value(raw: str, *, relative: bool) -> str | None:
    """Normalise a pane/tab reference; None if it is not usable."""
    lowered = " ".join(raw.casefold().split())
    if lowered in _CURRENT or lowered in ("this pane", "this tab", "current pane",
                                          "current tab", "this one"):
        return "current"
    words = re.sub(r"^(?:the\s+)?(?:tab\s+)?(?:number\s+)?", "", lowered).strip()
    if relative and words in _CARDINALS:
        return _CARDINALS[words]    # before fillers: "twenty-one" must keep its "one"
    stripped = " ".join(_FILLER.sub(" ", lowered).split())
    if not stripped:
        # Only filler ("pane", "the tab"): no reference at all. Measured (five-tool
        # schema): pane "pane" with command "below" would otherwise mean "this pane".
        return None
    if stripped in _CURRENT:
        return "current"
    if stripped in _SIDE_WORDS:
        return _SIDE_WORDS[stripped]
    if stripped in _RELATIVE:
        return _RELATIVE[stripped]
    if relative and stripped in _ORDINALS:
        return _ORDINALS[stripped]
    if relative and stripped in _CARDINALS:
        return _CARDINALS[stripped]
    if relative and re.fullmatch(r"(?:number\s+)?[0-9]{1,2}", stripped):
        return stripped.split()[-1]
    if re.fullmatch(r"[\w.+-][\w .+-]{0,62}", stripped):
        return "name:" + stripped
    return None


_TYPOGRAPHY = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201b": "'", "\u2032": "'",
                             "\u201c": '"', "\u201d": '"', "\u2033": '"',
                             "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
                             "\u2014": "-", "\u2015": "-", "\u2212": "-", "\u00a0": " "})


def normalize(prompt: str) -> str:
    """Typography as phones and word processors insert it, made plain: measured
    (review R2), "don\u2019t close tab 2" closed tab 2 because the checks
    knew only an ASCII apostrophe."""
    return unicodedata.normalize("NFKC", str(prompt)).translate(_TYPOGRAPHY)


# A plain instruction is one the request states in a canonical form of the
# admitted actions themselves: "close tab 2", "close the build pane", "run make
# in the left pane", "split right and run htop", optionally wrapped in
# politeness ("please", "can you", "thanks"). A yes given in advance (--yes,
# MCP confirm_risky) covers only that; anything else waits for a person whose
# question names the target.
#
# History, so it is not repeated. Review R1: refusal word lists were bypassed.
# Review R2: every list of words to refuse was one word short. Review R3: an
# allowlist of words still composes wrong meanings from allowed words alone
# ("would that close tab 2?", "close tab 2 in 5", "close tab 1 into tab 2").
# So the request is compared with what the actions say, not scanned for words.
_POLITE_HEAD = re.compile(r"^(?:(?:please|pls|kindly|ok|okay|alright|hey|just|can you|could you|"
                          r"would you|will you|go ahead and)[\s,]+)+")
_POLITE_TAIL = re.compile(r"(?:[\s,]+(?:please|pls|thanks|thank you|now|right now|for me))+$")
_CLAUSE_SPLIT = re.compile(r"\s*(?:,\s*and then|,\s*then|,\s*and|\s+and then|\s+then|\s+and|,|;)\s+")
_CLOSE_WORDS = r"(?:close|kill|shut|quit|exit)"
_RUN_WORDS = r"(?:run|type|execute|enter)"
# "top"/"bottom" are not canonical: Kilix resolves "above" to the adjacent
# pane, so in a stack of three "the top pane" would close the middle one
# (review KN-R4-02). Those requests wait for a person.
_SIDE_ALIAS = {"left": (), "right": (), "above": (), "below": ()}


def _alt(options) -> str:
    return "(?:" + "|".join(sorted(set(options), key=len, reverse=True)) + ")"


def _tab_phrases(target: str) -> list[str]:
    if target == "current":
        return [r"(?:this|the current|current) tab"]
    if target in ("next", "previous", "last"):
        return [rf"(?:the )?{target} tab"]
    if target.isascii() and target.isdigit():
        words = [w for w, n in _CARDINALS.items() if n == target]
        ordinals = [w for w, n in _ORDINALS.items() if n == target]
        out = [rf"tab (?:number )?{_alt([re.escape(target), *map(re.escape, words)])}"]
        if ordinals:
            out.append(rf"(?:the )?{_alt(ordinals)} tab")
        return out
    name = re.escape((target[5:] if target.startswith("name:") else target).casefold())
    return [rf"(?:the |that )?{name} tab", rf"(?:the )?tab (?:called|named|titled) {name}"]


def _pane_phrases(target: str) -> list[str]:
    unit = r"(?:pane|split|window)"
    if target == "current":
        return [rf"(?:this|the current|current) {unit}"]
    if target in ("next", "previous"):
        return [rf"(?:the )?{target} {unit}"]
    if target in SIDES:
        words = [target, *_SIDE_ALIAS[target]]
        out = [rf"(?:the )?{_alt(words)} {unit}"]
        if target in ("left", "right"):
            out.append(rf"(?:the )?{unit} (?:on|to) the {target}")
        else:
            out.append(rf"(?:the )?{unit} {target}")
        return out
    name = re.escape((target[5:] if target.startswith("name:") else target).casefold())
    return [rf"(?:the |that )?{name} {unit}", rf"(?:the )?{unit} (?:called|named|titled|running) {name}"]


# A word that is plainly an argument to a shell command, not English.
# Review KN-R6-03: "any word with . : @ ~ *" let "17:00", "@midnight",
# "~5min" and "9/25" through. Now: a flag; a path (/, ./, ../, ~/, or letters
# and a slash); a file name with an extension; VAR=value; a plain integer; an
# operator.
_SHELL_ARGUMENT = re.compile(
    r"-[\w-]*(?:=\S*)?"
    r"|(?:\.{1,2}|~)?/\S*"
    r"|[\w.-]*[A-Za-z][\w.-]*/[\w./-]*"
    r"|[\w-]+\.[A-Za-z][A-Za-z0-9]{0,7}"
    r"|[A-Za-z_][A-Za-z0-9_]*=\S*"
    r"|[0-9]+|\.{1,2}|\$[A-Za-z_]\w*|\$\{\w+\}"      # "cd ..", "echo $HOME" (R7 utility)
    r"|&&|\|\||\||;|>>?|<|2>&1")


def _quoted(value: str) -> str:
    """A command or program as the request gives it. Quoted, it is taken
    verbatim. Unquoted, it is plain only as one word, or when every later word
    is plainly a shell argument (a flag, a path, a number, `=`, an operator):
    English after a program may be a condition or a delay that would be typed
    along with it. Reviews KN-R4-05 and KN-R5-03 each found a list of such
    words one word short ("if", then "tomorrow"); this needs no list."""
    text = re.escape(value.casefold())
    words = value.split()
    if len(words) == 1 or all(_SHELL_ARGUMENT.fullmatch(w) for w in words[1:]):
        return rf"(?:{text}|`{text}`|\"{text}\"|'{text}')"
    return rf"(?:`{text}`|\"{text}\"|'{text}')"


def _clause_forms(action) -> list[str]:
    """Canonical wordings of one admitted action (regexes, whole clause)."""
    kind, args = action.kind, action.args
    if kind == "close_tab":
        return [rf"{_CLOSE_WORDS} {p}" for p in _tab_phrases(args["tab"])]
    if kind == "close_pane":
        return [rf"{_CLOSE_WORDS} {p}" for p in _pane_phrases(args["pane"])]
    if kind == "run_in_pane":
        command = _quoted(args["command"])
        forms = []
        for p in _pane_phrases(args["pane"]):
            forms += [rf"{_RUN_WORDS} {command} (?:in|into) {p}", rf"in {p},? {_RUN_WORDS} {command}"]
        if args["pane"] == "current":
            forms.append(rf"{_RUN_WORDS} {command} here")
        return forms
    if kind in ("open_pane", "open_tab"):
        unit = "tab" if kind == "open_tab" else r"(?:pane|split)"
        program = _quoted(args["program"]) if args.get("program") else None
        name = re.escape(args["name"].casefold()) if args.get("name") else None
        side = args.get("side")
        where = ""
        if side:
            where = (rf" (?:on the |to the )?{side}" if side in ("left", "right")
                     else rf" {_alt([side, *_SIDE_ALIAS[side]])}")
        called = rf" (?:called|named) {name}" if name else ""
        new = r"(?:a |a new |new )"
        forms = []
        if program:
            forms += [rf"(?:open|start|launch) {new}{unit}{where}{called} (?:running|with) {program}",
                      rf"(?:open|start|launch|run) {program} in {new}{unit}{where}{called}"]
            if kind == "open_pane" and side:
                forms.append(rf"split {_alt([side, *_SIDE_ALIAS[side]])} and (?:run|start) {program}")
        else:
            forms += [rf"(?:open|start) {new}?{unit}{where}{called}"]
            if kind == "open_pane" and side:
                forms.append(rf"split {_alt([side, *_SIDE_ALIAS[side]])}")
        return forms
    if kind == "go_to_tab":
        return [rf"(?:(?:go|switch|jump|move) to|focus) {p}" for p in _tab_phrases(args["tab"])]
    if kind == "go_to_pane":
        return [rf"(?:(?:go|switch|jump|move) to|focus) {p}" for p in _pane_phrases(args["pane"])]
    return []   # no plain wording: beside a risky action, the request is not plain


_MOVES_FOCUS = ("go_to_tab", "go_to_pane", "open_pane", "open_tab")


def plain(prompt: str, actions: list) -> str | None:
    """None when the request is a plain instruction; otherwise why it is not,
    for the note that asks for a person's yes."""
    text = normalize(prompt).casefold().strip()
    # A question mark is plain only after "can/could/would/will you": a bare
    # "close tab 2?" asks (review R4, row D).
    asks = re.match(r"(?:please[\s,]+)?(?:can|could|would|will) you\b", text)
    # Anywhere in the closing punctuation (review KN-R5-04: "close tab 2?!").
    if re.search(r"\?[\s.!?]*$", text) and not asks:
        return "it ends in a question mark"
    text = _POLITE_HEAD.sub("", text)
    text = re.sub(r"[.!?]+$", "", text).strip()
    text = _POLITE_TAIL.sub("", text).strip()
    risky = [a for a in actions if getattr(a, "risky", False)]
    if not risky:
        return None
    moved = False
    for action in actions:
        target = action.args.get("tab") or action.args.get("pane")
        if action.risky and moved:
            # Named targets too (review KN-R4-03): a name resolves in the
            # caller's tab first, not in the tab the request just moved to.
            return "a risky action after focus has moved"
        if action.kind in _MOVES_FOCUS:
            moved = True
    # Split outside quotes: 'run "if true; then ls; fi" in the build pane' is one clause.
    bounds = list(_CLAUSE_SPLIT.finditer(_unquoted(text)))
    starts = [0] + [m.end() for m in bounds]
    ends = [m.start() for m in bounds] + [len(text)]
    # One action may be written across a separator ("split right and run
    # htop", "in the build pane, run make"): an action takes one clause, or
    # two joined by the text that separated them.
    i, verb = 0, None
    for action in actions:
        if i >= len(starts):
            return "it names more actions than it states"
        forms = _clause_forms(action)
        taken = None
        for span in (1, 2):
            if i + span > len(starts):
                break
            clause = text[starts[i]:ends[i + span - 1]]
            head = re.match(r"(close|kill|shut|quit|exit|run|type|execute|enter)\b", clause)
            candidate = clause if head or not (verb and action.risky) else f"{verb} {clause}"
            # A safe clause beside a risky one is held to its canonical
            # wording too (review KN-R4-04: its words alone let "at 5 go to
            # tab 1 and close tab 2" through, and let rename_tab's own value
            # carry "if the tests pass").
            if any(re.fullmatch(form, candidate) for form in forms):
                taken, verb = span, (head.group(1) if head else verb)
                break
        if taken is None:
            if not forms:
                return f"no plain wording is known for {action.kind}"
            return f"{text[starts[i]:ends[i]]!r} is not a plain way to say {action.kind}"
        i += taken
    if i != len(starts):
        return f"{text[starts[i]:]!r} is more than the actions say"
    return None


def interpret(prompt: str, calls: list) -> list[Action | Refusal]:
    """Turn the engine's calls into admitted actions or named refusals."""
    prompt = normalize(prompt)
    results: list[Action | Refusal] = []
    for call in calls if isinstance(calls, list) else []:
        name = call.get("name") if isinstance(call, dict) else None
        args = call.get("arguments") if isinstance(call, dict) else None
        if not isinstance(name, str) or name not in TOOL_NAMES or not isinstance(args, dict):
            results.append(Refusal(str(name), "not a kilix-needle action"))
            continue
        schema = next(t["parameters"] for t in TOOLS if t["name"] == name)
        error = _shape_error(args, schema)
        results.append(Refusal(name, error) if error else _admit(name, args, prompt))
    return _merge_split_then_run(results, prompt)


def _merge_split_then_run(results: list, prompt: str) -> list:
    """"split right and run htop" is one pane, even when it arrives as two calls.

    Measured (tuned five-tool model): open_pane(side=right) then
    open_pane(program=htop) for that request; admitted separately they open two
    panes, one running a program nobody asked to put there. The pair is merged
    only when the program's own clause is a bare start ("run htop"), never when
    it asks for another pane ("and open a pane running htop").
    """
    merged = []
    # One opening noun in the whole request means one new pane or tab: "a fresh
    # pane on the right, named logs, running tail" came back as two opens, one
    # with the name and one with the program (measured, tuned model).
    single = len(re.findall(r"\b(?:splits?|panes?|tabs?|windows?|terminals?)\b", prompt, re.I)) == 1
    for item in results:
        previous = merged[-1] if merged else None
        if (single and isinstance(item, Action) and isinstance(previous, Action)
                and previous.kind == item.kind and item.kind in ("open_pane", "open_tab")
                and all(previous.args.get(k, v) == v for k, v in item.args.items())):
            merged[-1] = Action(item.kind, {**previous.args, **item.args})
            continue
        if (isinstance(item, Action) and isinstance(previous, Action)
                and previous.kind == item.kind == "open_pane"
                and set(previous.args) == {"side"} and set(item.args) == {"program"}):
            clause = next((c for c in _clauses(prompt) if item.args["program"] in c), "")
            if clause and not re.search(r"\b(?:open|pane|panes|split|window|tab)\b",
                                        clause.replace(item.args["program"], " "), re.I):
                merged[-1] = Action("open_pane", {**previous.args, **item.args})
                continue
        merged.append(item)
    return merged


_AS_NAME = r"(?:running|named|called|titled|with)\s+(?:the\s+)?"


def _named_target(name: str, key: str, args: dict, prompt: str) -> str | Refusal:
    raw = _text(args, key)
    # The introducing word is not part of the name: measured (fourteen tools),
    # "close the pane running top" -> pane "running top", which names no pane.
    raw = re.sub(rf"^{_AS_NAME}", "", raw.strip(), flags=re.IGNORECASE)
    word = " ".join(raw.casefold().split())
    if word and re.search(rf"\b{_AS_NAME}{re.escape(word)}(?![\w])", prompt, re.IGNORECASE):
        # Introduced as a name, it is a name: measured (held-out v3), "close the
        # pane running top" became the side "above", and "close the tab named
        # two" became tab 2.
        return "name:" + word
    value = _target_value(raw, relative=key == "tab")
    if value is None:
        return Refusal(name, f"cannot tell which {key} {raw!r} means")
    if value.startswith("name:") and not _grounded(value[5:], prompt):
        return Refusal(name, f"the {key} {value[5:]!r} is not in the request")
    # Names are matched without regard to case (kilix.Tree), so their case is
    # not kept. Reviews R6-R8 each found the previous rule for choosing a case
    # taking it from somewhere slightly wrong; there is no such rule now.
    return value


_CLAUSE = re.compile(r"\s*(?:,|;|\band then\b|\bthen\b|\band\b)\s*", re.IGNORECASE)


_TAB_WORD = re.compile(r"\btabs?\b", re.IGNORECASE)
_PANE_WORD = re.compile(r"\b(?:panes?|windows?|splits?)\b", re.IGNORECASE)


_UNIT_NAMES = {"tab", "new tab", "a new tab", "pane", "new pane", "a new pane", "terminal",
               "new terminal", "window", "new window", "split"}
_LOCATIVE = re.compile(r"\b(?:in|inside|within|at|from|of)\b", re.IGNORECASE)
_BARE_OBJECT = {"this", "that", "here", "now", "please", "the", "current", "active", "focused", "one",
                "pane", "tab", "window", "split"}


def _mentions(target: str) -> list[str]:
    """Words the request may use for this target."""
    if target == "current":
        return ["this", "current", "here", "active", "focused"]
    if target.startswith("name:"):
        return [target[5:]]
    token = target
    return [token,
            *(word for word, side in _SIDE_WORDS.items() if side == token),
            *(word for word, number in _ORDINALS.items() if number == token),
            *(word for word, number in _CARDINALS.items() if number == token)]


def _first(words: list[str], text: str) -> re.Match | None:
    found = [m for word in words
             for m in [re.search(rf"(?<![\w]){re.escape(word)}(?![\w])", text, re.IGNORECASE)]
             if m]
    return min(found, key=lambda m: m.start(), default=None)


def _bound_to_unit(mentions: list[str], text: str, unit_words: list[str]) -> bool:
    """The target is tied to a pane/tab word: "the vim pane", "the pane on the
    right", "tab 4", "the second tab", "the tab called logs".

    A bare word after a closing verb names a program, not its pane. Measured:
    "quit vim in the right pane" -> close_pane(vim), and "kill top in the left
    pane" -> close_pane(top), which a side word would have made "above".
    """
    units = "|".join(re.escape(word) for word in unit_words)
    for mention in mentions:
        word = re.escape(mention)
        if re.search(rf"(?<![\w]){word}\s+(?:{units})\b"
                     rf"|\b(?:{units})\s+(?:(?:called|named|running|with|number)\s+"
                     rf"|(?:on|to|at)\s+(?:the\s+)?)?{word}(?![\w])",
                     text, re.IGNORECASE):
            return True
    return False


_LOCATION = re.compile(r"(?:(?:\s+over)?\s+(?:in|into|on|at)\s+(?:the\s+)?(?:[\w.+-]+\s+){0,2}"
                       r"(?:pane|panes|window|split|tab)\b.*"
                       r"|\s+(?:here|there|in\s+here|in\s+there|in\s+it))$",
                       re.IGNORECASE)


def _unquoted(text: str) -> str:
    return re.sub(r"(?<![\w])(['\"`])(?:\\.|(?!\1).)*?\1",
                  lambda m: " " * len(m[0]), text)


def _clauses(prompt: str) -> list[str]:
    """Split conjunctions outside quoted commands; apostrophes in words are not quotes."""
    bounds = list(_CLAUSE.finditer(_unquoted(prompt)))
    starts = [0] + [m.end() for m in bounds]
    ends = [m.start() for m in bounds] + [len(prompt)]
    return [prompt[a:b] for a, b in zip(starts, ends)]


# "in the vim pane, run make": a location said before the command's clause.
_FRONTED = re.compile(r"^\s*(?:over\s+)?(?:in|into|on|at)\s+(?:the\s+)?(?:[\w.+-]+\s+){0,2}"
                      r"(?:pane|window|split)\s*$", re.I)
# "go to the chat pane and type clear there": "there" is the pane just gone to.
_GONE_TO = re.compile(r"^\s*(?:go|switch|jump|move|head|hop)\s+(?:over\s+)?to\s+"
                      r"((?:the\s+)?(?:[\w.+-]+\s+){0,2}(?:pane|window|split))\s*$", re.I)
# "tab five, close it": "it" is a bare reference in the clause before.
_DONE_WITH = re.compile(r"^\s*(?:i'?m\s+|i am\s+)?(?:done|finished)\s+with\s+", re.I)


def _run_units(prompt: str) -> list[str]:
    """The clauses, plus each run clause with a location its neighbour gives it."""
    clauses = _clauses(prompt)
    units = list(clauses)
    for before, clause in zip(clauses, clauses[1:]):
        if _FRONTED.match(before):
            units.append(f"{clause.rstrip(' .!?')} {before.strip()}")
        gone = _GONE_TO.match(before)
        if gone:
            there = re.sub(r"\b(?:in\s+)?there(?=[\s.!?]*$)", f"in {gone[1]}", clause,
                           count=1, flags=re.I)
            if there != clause:
                units.append(there)
    return units


def _resolve_it(verb: re.Pattern, clauses: list[str]) -> list[str]:
    """ "tab five, close it" -> "close tab five": only when the clause before is
    a bare reference, so "tab 2 has vim, close it" (vim) stays unsupported."""
    resolved = list(clauses)
    for i in range(1, len(clauses)):
        said = re.fullmatch(rf"\s*({verb.pattern})\s+it[\s.!?]*(?:please[\s.!?]*)?",
                            clauses[i], re.I)
        antecedent = _DONE_WITH.sub("", clauses[i - 1]).strip()
        if said and _BARE_REFERENCE.match(antecedent):
            resolved[i] = f"{said[1]} {antecedent}"
    return resolved


_NEGATION = re.compile(
    r"\b(?:not|never|don't|do not|dont|avoid|avoiding|without|stop|cannot|can't|cant|"
    r"won't|wont|shouldn't|no need to|neither|nor)\b", re.I)
# Reported or quoted speech before a close: "the error says close the left pane".
_REPORTED = re.compile(r"\b(?:echo|echoes|print|prints|says|said|say|saying|reads|writes|"
                       r"wrote|tells|told|note|notes)\b", re.I)


_OBJECT_END = re.compile(
    r"\s*(?:\(|(?<![\w-])(?:but|except|instead|rather|not|so|because|before|after|unless|"
    r"until|while|since|though|although|keep|keeping|leave|leaving|than|once|if|when|of|"
    r"next to|beside|besides|near)(?![\w-]))", re.I)   # "the leave-tracker tab" is a name


def _negated(prefix: str) -> bool:
    # Measured (review KN-02): "avoid closing tab 2" and "without closing tab
    # 2, go to tab 3" closed tab 2; the list knew five words.
    return bool(_NEGATION.search(prefix))


def _in_title(prefix: str) -> bool:
    # Names are data even without wrappers: renaming a pane to "close the
    # left pane" must not authorize an additional close proposed by a model.
    return bool(re.search(r"\b(?:rename|name|title|call|named|called|titled)\b", prefix, re.I))


_START_VERB = re.compile(r"\b(?:run|running|start|starting|launch|launching|with)\b|(?<!\S)w/", re.I)
_POLITE_SUFFIX = re.compile(r"\s+(?:please|pls|plz|thanks|thank you)[.!?]*$", re.I)
_OPEN_VERB = re.compile(r"\bopen\b", re.I)
_NAMED_SUFFIX = re.compile(r"\s+(?:called|named|titled)\s+\S.*$", re.I)


def _introduces_name(value: str, prompt: str) -> bool:
    """A new pane's or tab's name is said as a name: "called logs", "name it
    logs", "a tab for logs", "a logs tab". Measured (tuned model): "split the
    window and start less" -> name "the window".
    """
    v = re.escape(value)
    return re.search(rf"\b(?:called|named|titled|labelled|labeled|name it|call it|for)\s+"
                     rf"['\"`]?{v}['\"`]?(?![\w])"
                     rf"|\b(?:a|an|new|the)\s+{v}\s+(?:tab|pane|split|window)\b",
                     prompt, re.I) is not None


def _program_spans(prompt: str, name: str = "") -> set[str]:
    """What the request asks to start: the whole text after a start verb, up to a
    location or a name ("split right running htop -d 5 called mon" -> "htop -d 5"),
    or what "open" governs when a location follows ("open htop in a pane below").

    A program is started as argv, so it is held to the same standard as a typed
    command. Measured with the base model: "split the screen" -> program
    "screen", "open the pod bay doors" -> "bay doors", "type git status in the
    right pane" -> "type git", "switch to the next tab" -> "next".
    """
    spans = set()
    if name:
        # The admitted name is data, and nothing inside it is an instruction:
        # "open a tab called frontend running vim" starts vim, while in "open a
        # tab called run htop" the whole "run htop" is the name.
        prompt = re.sub(rf"\b(?:called|named|titled)\s+{re.escape(name)}(?![\w])", " ",
                        prompt, flags=re.I)
    for clause in _clauses(prompt):
        plain = _unquoted(clause)
        for verb, needs_location in ((_START_VERB, False), (_OPEN_VERB, True)):
            for match in verb.finditer(plain):
                if _negated(clause[:match.start()]) or _in_title(clause[:match.start()]):
                    continue
                rest = _POLITE_SUFFIX.sub("", _NAMED_SUFFIX.sub("", clause[match.end():]).strip())
                where = _LOCATION.search(" " + rest)
                if where and re.match(r"\s+(?:in|into|on|at)\s+the\s+", where.group(0), re.I):
                    continue    # "run ls -la in the right split": an existing pane, a command
                bare = _LOCATION.sub("", rest).strip().strip(".!?")
                if needs_location and bare == rest.strip(".!?"):
                    continue    # "open the pod bay doors": nothing says where
                if bare and not re.match(r"(?:a|an|the|another)?\s*(?:new\s+)?"
                                         r"(?:pane|tab|window|split|terminal)\b", bare, re.I):
                    spans.add(bare)
    return spans


# Common shell commands and builtins. Deterministic on purpose: a PATH lookup
# would make the same request admissible on one host and not another.
_KNOWN_COMMANDS = frozenset("""
    alias apt awk bash bat btop bun cargo cat cd chmod chown clear cmake cp curl cut date
    deno df diff dig docker du echo emacs env exit export fd find fish free gcc gdb git
    glances go gradle grep gzip head helix history hostname htop hx ip ipython irb java
    jobs journalctl jq kill kubectl lazygit less ln ls lsblk lsof make man mc mkdir more
    mv nano ncdu nc nix node npm npx nvim nvtop pacman perl php pip pip3 ping pip pnpm
    podman printenv ps psql pwd pytest python python3 ranger rg rm rmdir rsync ruby rustc
    scp sed sh sleep sort source sqlite3 ssh sudo systemctl tail tar tee tig tmux top
    touch tree uname uniq unzip uptime uv vi vim watch wc wget which whoami xargs yarn
    yay zip zsh basename dirname dmesg dd file groups id lscpu lspci lsusb mount nproc
    printf readlink realpath seq stat sync test true false umount whereis yes
""".split())


def _looks_like_command(command: str, prompt: str = "") -> bool:
    """A known command, a path, an assignment, or text the user quoted as code."""
    first = command.split()[0] if command.split() else ""
    quoted = any(f"{q}{command}{q}" in prompt for q in ("`", "'", '"'))
    return quoted or first in _KNOWN_COMMANDS or bool(re.match(r"[./~$]|.*[/=]", first))


def _command_spans(prompt: str) -> set[str]:
    """Return exact, case-sensitive command spans, retaining punctuation inside quotes."""
    spans = set()
    for clause in _clauses(prompt):
        # Only the first run verb: `run npm run dev` must not also admit `dev`.
        match = _RUN_VERB.search(_unquoted(clause))
        if (match is None or _negated(clause[:match.start()])
                or _in_title(clause[:match.start()])):
            continue
        span = clause[match.end():].strip()
        span = re.sub(r"^(?:the\s+command\s+)", "", span, flags=re.I)
        if span[:1] in ("'", '"', "`"):
            quoted = re.fullmatch(r"(['\"`])((?:\\.|(?!\1).)*)\1(.*)", span)
            if quoted is None:
                continue
            suffix = re.sub(r"\s+please[.!?]*$", "", quoted[3], flags=re.I)
            suffix = _LOCATION.sub("", suffix).strip().strip(".!?")
            if suffix:
                continue
            span = quoted[2]
        else:
            span = re.sub(r"\s+please[.!?]*$", "", span, flags=re.I)
            span = _LOCATION.sub("", span).strip()
            span = re.sub(r"\s+please[.!?]*$", "", span, flags=re.I)
        if span:
            spans.add(span)
    return spans


def _clause_supports(verb: re.Pattern, target: str, prompt: str, *, unit: str,
                     typed: str = "") -> bool:
    """Some one clause of the request holds the verb, this target and its unit.

    "close tab 2 and go to tab 1" has a closing verb, but only in the clause
    naming tab 2; a close of tab 1 is not supported by anything the user said.
    The unit guards the other measured confusion: "close the htop pane" came
    back as close_tab, and a tab is titled after its active pane, so a tab
    called htop can exist. A tab close needs a clause that says tab; a pane
    action is refused from a clause that names only a tab.

    A close also needs the pane or tab as the verb's object: in "exit vim in
    the left pane" (measured: close_pane(left)) what is exited is vim, and the
    pane is only where. For a typed command the target must be named outside
    the command text: "run htop" does not name a pane called htop (measured:
    run_in_pane(pane=htop, command=htop)).
    """
    clauses = _clauses(prompt) if typed else _resolve_it(verb, _clauses(prompt))
    for clause in clauses:
        if unit == "tab" and not _TAB_WORD.search(clause):
            continue
        if unit == "pane" and _TAB_WORD.search(clause) and not _PANE_WORD.search(clause):
            continue
        if typed:
            index = clause.casefold().find(typed.casefold())
            if index >= 0:
                clause = clause[:index] + " " + clause[index + len(typed):]
        match = verb.search(_unquoted(clause))
        if (match is None or _negated(clause[:match.start()])
                or _in_title(clause[:match.start()])
                or (not typed and _REPORTED.search(clause[:match.start()]))):
            continue
        if typed:
            if target == "current":
                # A default target must not override an explicit other pane.
                rest = verb.sub(" ", clause).casefold()
                words = set(re.findall(r"[\w-]+", rest))
                if words <= {"the", "a", "pane", "panes", "window", "split", "current",
                             "this", "here", "active", "focused", "in", "into", "on",
                             "at", "inside", "within", "please", "command"}:
                    return True
            elif _first(_mentions(target), clause):
                return True
            continue
        if _RUN_VERB.search(_unquoted(clause[:match.start()])):
            continue  # a close mentioned inside a command is not a pane instruction
        tail = clause[match.end():]
        # The verb's object ends where another thought begins. Measured (review
        # KN-01): "close tab 1 so I can focus on tab 2" closed tab 2, because
        # the target only had to be named somewhere after the verb.
        cut = _OBJECT_END.search(tail)
        if cut:
            tail = tail[:cut.start()]
        if target == "current":
            words = set(tail.casefold().strip().rstrip(".!?").split())
            if words and words <= _BARE_OBJECT and words - {"please", "now", "the"}:
                return True
            continue
        # The object is the first pane/tab word or target mention after the
        # verb, reached without a locative; the target itself must be named.
        unit_words = ["tab", "tabs"] if unit == "tab" else ["pane", "panes", "window", "split"]
        first = _first(_mentions(target) + unit_words, tail)
        if first is None or _LOCATIVE.search(tail[:first.start()]):
            continue
        # A directional alias inside an explicitly named target is not evidence
        # for a side: `pane running top` must never authorize `pane=above`.
        evidence = tail if target.startswith("name:") else re.sub(
            rf"\b{_AS_NAME}[\w.+-]+", "", tail, flags=re.I)
        # Bound to the unit implies the target is named at all.
        if not _bound_to_unit(_mentions(target), evidence, unit_words):
            continue
        return True
    return False


def _shape_error(args: dict, schema: dict) -> str:
    if set(args) - set(schema["properties"]):
        return "unknown argument"
    if any(key not in args for key in schema.get("required", [])):
        return "missing required argument"
    for key, value in args.items():
        kind = schema["properties"][key]["type"]
        if (kind == "string" and not isinstance(value, str)
                or kind == "integer" and type(value) is not int
                or kind == "boolean" and type(value) is not bool):
            return f"{key} must be {kind}"
        if isinstance(value, str) and any(ord(c) < 32 or ord(c) == 127 for c in value):
            return f"{key} contains a control character"
    return ""


def _new_action(name: str, args: dict, prompt: str) -> Action | Refusal:
    if name in ("maximize_pane", "rename_pane"):
        target = _named_target(name, "pane", {"pane": args.get("pane", "current")}, prompt)
        if isinstance(target, Refusal):
            return target
        if name == "rename_pane":
            title = _text(args, "name")
            if not title or title.casefold() in _UNIT_NAMES:
                return Refusal(name, "the request needs a pane title")
            for clause in _clauses(prompt):
                # Strip the new title before looking for the target: a title cannot
                # double as evidence that the user named a different pane to rename.
                match = re.search(r"(?:['\"`]?)" + re.escape(title) +
                                  r"(?:['\"`]?)[.!?]*$", clause)
                if not match:
                    continue
                prefix = re.sub(r"\s+(?:to|as|read)\s*$", "", clause[:match.start()]).strip()
                # "set the title of the pane left": the pane after "of" is the object.
                prefix = re.sub(r"\b(title|name|label) of\b", r"\1", prefix, flags=re.I)
                if _clause_supports(re.compile(r"\b(?:rename|retitle|title|call|name|label)\b",
                                               re.I), target, prefix, unit="pane"):
                    return Action(name, {"pane": target, "name": title})
                # The pane named before a noun: "set this pane title", "change the
                # pane name", "give the current pane the name" (blind corpus).
                if target == "current" and re.fullmatch(
                        r"(?:please )?(?:set|change|make|give) (?:the )?(?:(?:this|current|"
                        r"active|focused) )?pane(?:'s)? (?:the )?(?:title|name|label)", prefix, re.I):
                    return Action(name, {"pane": target, "name": title})
        else:
            restore = args.get("restore", False)
            pattern = (r"\b(?:restore|unmaximize|un-maximize|unzoom|un-zoom)\b" if restore
                       else r"(?<![\w-])(?:maximize|zoom)\b")
            verb = re.compile(pattern, re.I)
            # Phrasings that name no verb from the list but only mean this action.
            # Blind-authored corpus (kilix-ml): "make this pane fill the window",
            # "expand this pane to full size", "bring back the split view".
            whole = (re.compile(r"\b(?:bring back|return to|go back to) (?:the )?"
                                r"(?:split view|normal (?:pane )?layout|previous (?:pane )?layout)"
                                r"|\brestore all panes\b", re.I) if restore else
                     re.compile(r"\b(?:fill(?:s)? the (?:whole )?window|(?:to |at )?full[- ]"
                                r"(?:size|screen)|show only)\b", re.I))
            for clause in _clauses(prompt):
                if _clause_supports(verb, target, clause, unit="pane"):
                    return Action(name, {"pane": target, "restore": restore})
                if (whole.search(_unquoted(clause)) and _PANE_WORD.search(clause)
                        and not _TAB_WORD.search(clause) and not _negated(clause)
                        and not _RUN_VERB.search(_unquoted(clause))
                        and (target == "current" and not any(
                            _first(_mentions(side), clause) for side in SIDES)
                             or target != "current" and _first(_mentions(target), clause))):
                    return Action(name, {"pane": target, "restore": restore})
                if restore and target == "current" and whole.search(clause) \
                        and not _TAB_WORD.search(clause) and not _negated(clause):
                    return Action(name, {"pane": target, "restore": True})
                if restore and target == "current" and re.fullmatch(
                        r"(?:please )?restore (?:the )?(?:previous |last )?(?:pane )?layout[.!?]*",
                        clause, re.I):
                    return Action(name, {"pane": target, "restore": True})
    elif name == "swap_panes":
        side = _text(args, "side")
        if side in SIDES:
            # A swap names two panes, and clauses split at "and": "exchange this
            # pane | the lower pane". Adjacent clauses are also tried joined, but
            # never across a close or run: "swap this pane and close the left one".
            clauses = _clauses(prompt)
            joined = [f"{a} and {b}" for a, b in zip(clauses, clauses[1:])
                      if not _CLOSE_VERB.search(f"{a} {b}") and not _RUN_VERB.search(f"{a} {b}")]
            for clause in clauses + joined:
                verb = re.search(r"\b(?:swap|exchange|trade|reverse the positions)\b"
                                 r"|\bswitch (?:places|positions|this pane with|with)\b"
                                 r"|\bput this pane where\b", _unquoted(clause), re.I)
                if (verb and not _negated(clause[:verb.start()])
                        and not _RUN_VERB.search(_unquoted(clause[:verb.start()]))
                        and (_PANE_WORD.search(clause)
                             or re.search(r"\bneighbou?r\b", clause, re.I))
                        and not _TAB_WORD.search(clause) and _first(_mentions(side), clause)):
                    return Action(name, {"side": side})
    elif name == "move_tab":
        if ("direction" in args) == ("position" in args):
            return Refusal(name, "give exactly one of direction or position")
        direction, position = args.get("direction"), args.get("position")
        if direction is not None and direction not in ("left", "right"):
            return Refusal(name, "tab direction must be left or right")
        if position is not None and not 1 <= position <= 9:
            return Refusal(name, "tab position must be 1 to 9")
        for clause in _clauses(prompt):
            verb = re.search(r"\b(?:move|shift|reorder)\b", _unquoted(clause), re.I)
            if (not verb or _negated(clause[:verb.start()]) or not _TAB_WORD.search(clause)
                    or _RUN_VERB.search(_unquoted(clause[:verb.start()]))):
                continue
            # This action moves only the caller's tab. Never reinterpret a named
            # or numbered source tab as the current one.
            tail = clause[verb.end():].strip()
            source = re.match(r"(?:the )?(?:this |current |active |focused )?tab\b", tail, re.I)
            if not source:
                continue
            destination = tail[source.end():].strip()
            if direction and re.fullmatch(r"(?:to (?:the )?)?" + direction + r"[.!?]*", destination, re.I):
                return Action(name, {"direction": direction})
            if position is not None:
                forms = "|".join(re.escape(x) for x in _mentions(str(position)))
                if re.fullmatch(r"to (?:the )?(?:(?:position|slot) )?(?:" + forms +
                                r")(?: (?:position|slot))?[.!?]*", destination, re.I):
                    return Action(name, {"position": position})
    return Refusal(name, "no part of the request supports this action and its arguments")


def _admit(name: str, args: dict, prompt: str) -> Action | Refusal:
    if name in ("maximize_pane", "rename_pane", "swap_panes", "move_tab"):
        return _new_action(name, args, prompt)
    if name in ("close_pane", "close_tab") and not _CLOSE_VERB.search(prompt):
        return Refusal(name, "the request does not ask to close anything")
    if name == "run_in_pane" and not _RUN_VERB.search(prompt):
        return Refusal(name, "the request does not ask to run or type a command")

    if name in ("open_pane", "open_tab"):
        out = {}
        if name == "open_pane":
            side = _text(args, "side")
            if side and side not in SIDES:
                return Refusal(name, f"unknown side {side!r}")
            if side:
                out["side"] = side
        for key in ("program", "name"):
            value = _LEADING_VERBS.sub("", _text(args, key)) if key == "program" else _text(args, key)
            if key == "name" and value.casefold() in _UNIT_NAMES:
                value = ""  # measured: "new tab" -> open_tab(name="new tab")
            if value:
                if not _grounded(value, prompt):
                    return Refusal(name, f"the {key} {value!r} is not in the request")
                out[key] = value
        if name == "open_tab" and not _TAB_WORD.search(prompt):
            return Refusal(name, "the request does not ask for a tab")
        if name == "open_pane" and _TAB_WORD.search(prompt) and not re.search(
                r"\b(?:panes?|splits?|splitting|windows?|terminals?)\b", prompt, re.I):
            return Refusal(name, "the request asks for a tab, not a pane")
        if "name" in out and not _introduces_name(out["name"], prompt):
            return Refusal(name, f"nothing in the request names it {out['name']!r}")
        if "program" in out and out["program"] not in _program_spans(prompt, out.get("name", "")):
            return Refusal(name, f"the program {out['program']!r} is not what the request "
                                 "asks to start")
        if "side" in out:
            # The side must be said outside the program and the name: in "open a
            # pane running bottom" the model read "bottom" as below (measured).
            rest = prompt
            for key in ("program", "name"):
                if key in out:
                    rest = re.sub(rf"(?<![\w]){re.escape(out[key])}(?![\w])", " ", rest,
                                  flags=re.I)
            # "open up a new pane": a phrasal up/down is not a direction (measured,
            # tuned model: side above from "open up").
            rest = re.sub(r"\b(?:open|opens|opening|pull|bring|fire|spin|set|boot|start|"
                          r"call|look|shut|write|slow|calm|pop)\s+(?:up|down)\b", " ", rest,
                          flags=re.I)
            if not _first(_mentions(out["side"]), rest):
                return Refusal(name, f"the request does not say {out['side']}")
        return Action(name, out)

    if name in ("close_pane", "go_to_pane", "run_in_pane"):
        target = _named_target(name, "pane", args, prompt)
        if isinstance(target, Refusal):
            return target
        command = _text(args, "command") if name == "run_in_pane" else ""
        if name == "run_in_pane":
            if not command or not _grounded(command, prompt):
                return Refusal(name, f"the command {command!r} is not in the request")
            if _RUN_VERB.fullmatch(command):
                # measured: "run make test in the left pane" -> command "run"
                return Refusal(name, f"the command {command!r} is only the verb")
            if (target == "current" and not re.search(
                    r"\b(?:here|there|this (?:pane|window|split)|current (?:pane|window))\b",
                    prompt, re.I) and not _looks_like_command(command, prompt)):
                # Only implied "this pane" and no location: measured (tuned model),
                # "type faster, I'm bored" -> command "faster".
                return Refusal(name, f"{command!r} does not look like a command")
        if name == "run_in_pane":
            # One clause must hold the verb, this pane and exactly this command.
            # Measured (tuned model): "run make in the left pane and run make test
            # in the right pane" -> "make test" in both panes; each half was
            # supported somewhere, but not together.
            if not any(command in _command_spans(clause)
                       and _clause_supports(_RUN_VERB, target, clause, unit="pane", typed=command)
                       for clause in _run_units(prompt)):
                return Refusal(name, "no part of the request asks to type that into that pane")
        elif name == "close_pane" and not _clause_supports(_CLOSE_VERB, target, prompt,
                                                           unit="pane"):
            return Refusal(name, "no part of the request asks for this on that pane")
        if name == "go_to_pane" and not _moves(prompt):
            return Refusal(name, "the request does not ask to go anywhere")
        out = {"pane": target}
        if name == "run_in_pane":
            out["command"] = command
        return Action(name, out)

    if name in ("close_tab", "go_to_tab"):
        target = _named_target(name, "tab", args, prompt)
        if isinstance(target, Refusal):
            return target
        if name == "close_tab" and not _clause_supports(_CLOSE_VERB, target, prompt, unit="tab"):
            return Refusal(name, "no part of the request asks to close that tab")
        if name == "go_to_tab" and not _moves(prompt):
            return Refusal(name, "the request does not ask to go anywhere")
        return Action(name, {"tab": target})

    if name == "arrange_panes":
        layout = _text(args, "layout")
        if layout not in LAYOUTS:
            return Refusal(name, f"unknown layout {layout!r}")
        return Action(name, {"layout": layout})

    if name == "rename_tab":
        title = _text(args, "name")
        if not title or not _grounded(title, prompt):
            return Refusal(name, f"the name {title!r} is not in the request")
        if title.casefold() in _UNIT_NAMES or title.casefold() in ("panes", "tabs"):
            # measured (five-tool schema): "make this pane wider by 10" -> tab_name "pane"
            return Refusal(name, f"{title!r} is not a name for a tab")
        return Action(name, {"name": title})

    # resize_pane
    direction = _text(args, "direction")
    if direction not in DIRECTIONS:
        return Refusal(name, f"unknown direction {direction!r}")
    if not _says_direction(direction, prompt):
        # Measured (tuned model): "increase the font size" -> narrower, and
        # "widen this pane by 20" -> narrower. The direction was never checked.
        return Refusal(name, f"the request does not ask for {direction}")
    amount = args.get("amount", 2)
    if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= 50:
        return Refusal(name, "the size change must be 1 to 50 cells")
    if "amount" in args and not _says_number(amount, prompt):
        return Refusal(name, f"the amount {amount} is not in the request")
    return Action(name, {"direction": direction, "amount": amount})
