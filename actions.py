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
_CLOSE_VERB = re.compile(r"\b(close|kill|quit|exit|shut)\b", re.IGNORECASE)
_RUN_VERB = re.compile(r"\b(run|type|execute|enter)\b", re.IGNORECASE)
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
    if relative and re.fullmatch(r"(?:number\s+)?\d{1,2}", stripped):
        return stripped.split()[-1]
    if re.fullmatch(r"[\w.+-][\w .+-]{0,62}", stripped):
        return "name:" + stripped
    return None


def interpret(prompt: str, calls: list) -> list[Action | Refusal]:
    """Turn the engine's calls into admitted actions or named refusals."""
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
    return results


_AS_NAME = r"(?:running|named|called|titled|with)\s+(?:the\s+)?"


def _named_target(name: str, key: str, args: dict, prompt: str) -> str | Refusal:
    raw = _text(args, key)
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


_LOCATION = re.compile(r"(?:\s+(?:in|into|on|at)\s+(?:the\s+)?(?:[\w.+-]+\s+){0,2}"
                       r"(?:pane|panes|window|split|tab)\b.*|\s+(?:here|in\s+here))$",
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


def _negated(prefix: str) -> bool:
    return bool(re.search(r"\b(?:not|never|don't|do not|dont)\b", prefix, re.I))


def _in_title(prefix: str) -> bool:
    # Names are data even without wrappers: renaming a pane to "close the
    # left pane" must not authorize an additional close proposed by a model.
    return bool(re.search(r"\b(?:rename|name|title|call|named|called|titled)\b", prefix, re.I))


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
    for clause in _clauses(prompt):
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
                or _in_title(clause[:match.start()])):
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
            if command not in _command_spans(prompt):
                return Refusal(name, f"the command {command!r} is not all of what the "
                                     "request asks to type")
        verb = {"close_pane": _CLOSE_VERB, "run_in_pane": _RUN_VERB}.get(name)
        if verb is not None and not _clause_supports(verb, target, prompt, unit="pane",
                                                     typed=command):
            return Refusal(name, "no part of the request asks for this on that pane")
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
    amount = args.get("amount", 2)
    if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= 50:
        return Refusal(name, "the size change must be 1 to 50 cells")
    if "amount" in args and str(amount) not in prompt:
        return Refusal(name, f"the amount {amount} is not in the request")
    return Action(name, {"direction": direction, "amount": amount})
