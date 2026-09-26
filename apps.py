"""The apps job: launch Kilix apps and games, and change Kilix settings.

The model sees five tools (TOOLS). Every call it makes is untrusted: this
module admits a call only when the request itself says it, in one clause, with
a verb that means it. The names a model may use are resolved through tables of
what Kilix really has (the catalog's content ids, kilix-settings' controls), so
nothing outside them can run. See APPS-JOB-DESIGN.md in the research notes.

Excluded on purpose, whatever a model calls: power, installs and downloads
other than a launch a person confirms, bulk changes, transcript budgets and
turning session logging off (data loss), voice engines, models and devices,
volume levels, and closing apps (that is the panes job).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from actions import Refusal, _clauses, normalize

# ---------------------------------------------------------------------------
# What Kilix has. tests/test_apps.py holds these to the vendored catalog and,
# when an installed Kilix is found, to its kilix-settings controls.

GAMES = ("minesweeper", "solitaire", "doom", "bashed-earth", "kilix-jpak", "kilix-rancher",
         "kilix-pong", "kilix-lights", "super-kilix", "joustix", "chess-bash", "kilix-fishtank",
         "terminal-lander", "kitty-brokeout", "kilix-land")
APPS = ("dosbox", "kilix-file", "kilix-system-center", "kilix-settings-center",
        "kilix-software-center", "kilix-session-center", "kilix-model-store", "kilix-camera-wall",
        "kilix-region-painter", "kilix-voice-studio", "kilix-camera-manager",
        "kilix-virtualbox-manager", "kilix-weather", "kilix-calculator", "kilix-music-control",
        "kilix-chawan", "kilix-rollout-resume", "kilix-character-map", "kilix-notepad",
        "kilix-find-files", "kilix-session-log", "kilix-amp", "kilix-pdf", "kilix-pdf-conversion",
        "kilix-rtsp", "kilix-nvr", "kilix-object-detect", "kilix-tmux-manager", "kilix-graphs",
        "kilix-techno")
HOST_TOOLS = ("launcher", "temps", "memory", "mixer", "transcripts")
LAUNCHABLE = GAMES + APPS + HOST_TOOLS
AVAILABILITY = GAMES + ("dosbox",)          # kilix-settings' game switches
ITEMS = ("clock", "calendar", "battery", "network", "temperature", "volume", "windows", "speak",
         "dictate", "split_left", "split_right", "split_up", "split_down", "maximize", "close",
         "font_increase", "font_decrease", "synchronize_input")
STATS = ("cpu", "memory")
MODES = ("auto", "always", "off")
SECTIONS = ("top-bar", "pane-buttons", "voice", "games", "tools")

# Words a request may use for each name, first-party. Ids and their spaced
# forms are always included (see _names). Longer phrases win over shorter ones.
LAUNCH_NAMES = {
    "minesweeper": ["minesweeper", "mine sweeper", "mines"],
    "solitaire": ["solitaire", "klondike", "patience"],
    "doom": ["doom"],
    "bashed-earth": ["bashed earth", "artillery game"],
    "kilix-jpak": ["jpak", "vault game"],
    "kilix-rancher": ["rancher", "fire kitten", "fire-kitten"],
    "kilix-pong": ["pong", "paddle game"],
    "kilix-lights": ["kilix lights", "lights out"],
    "super-kilix": ["super kilix", "platformer"],
    "joustix": ["joustix", "joust", "jousting game"],
    "chess-bash": ["chess bash", "chess"],
    "kilix-fishtank": ["fishtank", "fish tank", "aquarium"],
    "terminal-lander": ["terminal lander", "lunar lander", "moon lander", "lander"],
    "kitty-brokeout": ["brokeout", "kitty brokeout", "breakout", "brick breaker"],
    "kilix-land": ["kilix land"],
    "dosbox": ["dosbox", "dos box", "ms-dos", "msdos", "dos prompt"],
    "kilix-file": ["file browser", "file manager", "files app", "kilix file"],
    "kilix-system-center": ["system center", "system centre", "system overview"],
    "kilix-settings-center": ["settings center", "settings centre", "settings app"],
    "kilix-software-center": ["software center", "software centre", "software store",
                              "app store"],
    "kilix-session-center": ["session center", "session centre"],
    "kilix-model-store": ["model store", "models store"],
    "kilix-camera-wall": ["camera wall"],
    "kilix-region-painter": ["region painter", "mask painter", "mask editor"],
    "kilix-voice-studio": ["voice studio"],
    "kilix-camera-manager": ["camera manager"],
    "kilix-virtualbox-manager": ["virtualbox manager", "virtualbox", "vm manager"],
    "kilix-weather": ["weather app", "weather"],
    "kilix-calculator": ["calculator", "calc"],
    "kilix-music-control": ["music control", "music controls", "music remote"],
    "kilix-chawan": ["chawan", "text browser", "web browser", "text-mode browser"],
    "kilix-rollout-resume": ["rollout resume", "rollout-resume"],
    "kilix-character-map": ["character map", "charmap", "char map"],
    "kilix-notepad": ["notepad", "text editor"],
    "kilix-find-files": ["find files", "file search", "search for files"],
    "kilix-session-log": ["session log", "session logs"],
    "kilix-amp": ["kilix amp", "amp", "music player", "media player"],
    "kilix-pdf": ["pdf viewer", "pdf reader", "kilix pdf"],
    "kilix-pdf-conversion": ["pdf conversion", "pdf converter", "pdf to markdown"],
    "kilix-rtsp": ["rtsp"],
    "kilix-nvr": ["nvr", "camera recorder"],
    "kilix-object-detect": ["object detect", "object detection", "object detector"],
    "kilix-tmux-manager": ["tmux manager", "tmux"],
    "kilix-graphs": ["kilix graphs", "graphs", "graph", "charts"],
    "kilix-techno": ["techno", "music workstation", "sequencer", "drum machine"],
    "launcher": ["launcher", "app menu", "application menu", "applications menu"],
    "temps": ["temps", "temperatures", "temperature"],
    "memory": ["memory usage", "ram usage", "memory", "ram"],
    "mixer": ["volume mixer", "sound mixer", "audio mixer", "mixer"],
    "transcripts": ["transcripts", "transcript list", "pane transcripts"],
}
ITEM_NAMES = {
    "clock": ["clock", "time"],
    "calendar": ["calendar", "date"],
    "battery": ["battery"],
    "network": ["network", "wifi", "wi-fi", "internet"],
    "temperature": ["temperature", "temp", "temps", "thermals"],
    "volume": ["volume"],
    "windows": ["windows", "window list"],
    "speak": ["speak", "read aloud", "read-aloud", "text to speech"],
    "dictate": ["dictate", "dictation", "microphone", "mic", "speech to text"],
    "split_left": ["split left", "split-left"],
    "split_right": ["split right", "split-right"],
    "split_up": ["split up", "split-up", "split above"],
    "split_down": ["split down", "split-down", "split below"],
    "maximize": ["maximize", "maximise"],
    "close": ["close button", "close buttons", "x button"],
    "font_increase": ["font increase", "bigger font", "larger font", "font size up",
                      "increase font", "font plus"],
    "font_decrease": ["font decrease", "smaller font", "font size down", "decrease font",
                      "font minus"],
    "synchronize_input": ["synchronize input", "synchronise input", "sync input",
                          "broadcast input", "synchronized input"],
}
STAT_NAMES = {"cpu": ["cpu", "processor"], "memory": ["memory", "ram", "mem"]}
MODE_NAMES = {"always": ["always", "all the time", "permanently", "at all times", "constantly"],
              "off": ["off", "never", "hide", "hidden", "disable", "stop showing", "no longer",
                      "don't need", "do not need", "anymore"],
              "auto": ["auto", "automatic", "automatically", "only when busy", "when busy",
                       "when needed", "only when needed"]}
SECTION_NAMES = {"top-bar": ["top bar", "top-bar", "topbar", "status bar", "bar at the top"],
                 "pane-buttons": ["pane buttons", "pane-buttons", "pane button",
                                  "buttons on panes", "buttons on the panes"],
                 "voice": ["voice", "speech", "dictation", "text to speech"],
                 "games": ["games", "game"],
                 "tools": ["tools", "tool"]}

# ---------------------------------------------------------------------------
# The tools the model sees. Closed sets are enums; names that people say in
# many ways are strings, resolved above.

TOOLS = [
    {"name": "launch",
     "description": "Open a Kilix app, game or tool in a new tab",
     "parameters": {"type": "object", "properties": {
         "app": {"type": "string", "description": "the app, game or tool, as the request names it"}},
         "required": ["app"]}},
    {"name": "show",
     "description": "Show or hide an indicator on the Kilix top bar or a button on panes",
     "parameters": {"type": "object", "properties": {
         "item": {"type": "string", "enum": list(ITEMS)},
         "on": {"type": "boolean", "description": "true to show it, false to hide it"}},
         "required": ["item", "on"]}},
    {"name": "pane_stat",
     "description": "How each pane shows its CPU or memory use",
     "parameters": {"type": "object", "properties": {
         "stat": {"type": "string", "enum": list(STATS)},
         "mode": {"type": "string", "enum": list(MODES)}},
         "required": ["stat", "mode"]}},
    {"name": "game",
     "description": "Make a game available or unavailable in the Kilix games list (not play it)",
     "parameters": {"type": "object", "properties": {
         "game": {"type": "string", "description": "the game, as the request names it"},
         "available": {"type": "boolean"}},
         "required": ["game", "available"]}},
    {"name": "settings",
     "description": "Open the Kilix settings screen at a section",
     "parameters": {"type": "object", "properties": {
         "section": {"type": "string", "enum": list(SECTIONS)}},
         "required": ["section"]}},
]
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)
# What changes something if it runs unasked; opening the settings screen does not.
SIDE_EFFECT_KINDS = frozenset({"launch", "show", "pane_stat", "game"})


@dataclass(frozen=True)
class Action:
    kind: str
    args: dict = field(default_factory=dict)

    @property
    def risky(self) -> bool:
        """Starts a program (and, if it is not installed, would install it)."""
        return self.kind == "launch"


# ---------------------------------------------------------------------------
# Verbs. A launch needs a verb that opens or plays in the same clause; a show
# or game change needs one polarity, said once, in the clause that names it.

# Verbs, not their -ing forms: "I love playing chess bash" asks for nothing.
_OPEN = re.compile(r"\b(?:open|launch|start|run|play|"
                   r"fire up|boot up|boot|bring up|pull up|load|load up|show me|give me|"
                   r"get me|spin up|let'?s play|wanna play|want to play|like to play|"
                   r"in the mood for|up for a round of|up for some|a round of|a game of)\b",
                   re.I)
_GAP = r"(?:\s+[\w'-]+){0,4}?\s+"      # "put the clock back", "turn cpu off"
_ON = [rf"\b(?:turn|switch){_GAP}on\b", rf"\b(?:put|bring|add){_GAP}back\b",
       rf"\bmake{_GAP}available\b", r"\bturn on\b", r"\bswitch on\b", r"\bput back\b",
       r"\bbring back\b", r"\bshow\b", r"\bdisplay\b", r"\bre-?enable\b", r"\benable\b",
       r"\bunhide\b", r"\brestore\b", r"\breveal\b", r"\b(?:want|like) to see\b",
       r"\bi (?:want|need)\b", r"\bi'?d like\b", r"\bi would like\b", r"\ballow\b",
       r"\bunblock\b", r"\badd\b", r"\bput\b"]
_OFF = [rf"\b(?:turn|switch){_GAP}off\b", rf"\bmake{_GAP}unavailable\b",
        rf"\btake{_GAP}(?:off|away|out)\b", r"\bturn off\b", r"\bswitch off\b",
        r"\bget rid of\b", r"\bstop showing\b", r"\b(?:don'?t|do not|no longer) (?:show|want|need)\b",
        r"\bhide\b", r"\bremove\b", r"\bdisable\b", r"\bdrop\b", r"\bditch\b", r"\blose\b",
        r"\bblock\b"]
_NOT_ON = [r"\b(?:don'?t|do not) hide\b", r"\bstop hiding\b", r"\bun-hide\b"]   # read as on
_SETTINGS_WORD = re.compile(r"\b(?:settings?|preferences|prefs|options|config(?:ure|uration)?|"
                            r"section|page|screen|panel)\b", re.I)
_UNSAFE_CONTEXT = re.compile(r"\b(?:install|installing|uninstall|uninstalling|update|updating|"
                             r"upgrade|remove the app|delete|download|downloading)\b", re.I)
_NEGATION = re.compile(r"\b(?:not|never|don'?t|do not|avoid|without|cannot|can'?t|won'?t|"
                       r"shouldn'?t|no need to|neither|nor)\b", re.I)
_REPORTED = re.compile(r"\b(?:says|said|say|saying|told|tells|asked me|wrote|writes|reads|"
                       r"according to|claims|claimed)\b", re.I)


def _plain_words(text: str) -> str:
    """Lower case, hyphens and underscores as spaces, single spaces."""
    return " ".join(re.sub(r"[_\-]+", " ", normalize(text).casefold()).split())


def _names(table: dict, key: str) -> list[str]:
    words = {_plain_words(key)} | {_plain_words(w) for w in table.get(key, [])}
    return sorted(words, key=len, reverse=True)


def _said(phrase: str, text: str) -> re.Match | None:
    return re.search(rf"(?<![\w]){re.escape(phrase)}(?![\w])", text)


def _resolve(table: dict, universe: tuple, value) -> str | None:
    """The one name a model's value means: an id, or the longest alias it holds."""
    if not isinstance(value, str) or not value.strip():
        return None
    said = _plain_words(value)
    exact = [key for key in universe if said == _plain_words(key)
             or said in {_plain_words(w) for w in table.get(key, [])}]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    best = sorted(((len(phrase), key) for key in universe for phrase in _names(table, key)
                   if _said(phrase, said)), reverse=True)
    if not best or (len(best) > 1 and best[0][0] == best[1][0] and best[0][1] != best[1][1]):
        return None
    return best[0][1]


def _clause_parts(request: str) -> list[str]:
    return [_plain_words(c) for c in _clauses(normalize(request)) if c.strip()]


def _mentions(table: dict, key: str, clause: str) -> re.Match | None:
    """Where the clause names this key, by its longest name there, never a
    shorter name inside a longer one of another key ('chess' in 'chess bash' is
    chess bash; 'browser' in 'file browser' is the file browser)."""
    for phrase in _names(table, key):
        match = _said(phrase, clause)
        if match:
            longer = [other for other in table if other != key
                      for p in _names(table, other)
                      if len(p) > len(phrase) and phrase in p and _said(p, clause)]
            if not longer:
                return match
    return None


def _polarity(clause: str) -> bool | None:
    """True, False, or None when the clause says neither or both."""
    text = clause
    found = set()
    for patterns, value in ((_NOT_ON, True), (_OFF, False), (_ON, True)):
        for pattern in patterns:
            if re.search(pattern, text):
                found.add(value)
                text = re.sub(pattern, " ", text)
    if len(found) != 1:
        return None
    # A negation left over after the polarity phrases took theirs flips or
    # muddles the meaning: refuse rather than guess.
    if _NEGATION.search(text):
        return None
    return found.pop()


def _clauses_with_verbs(request: str) -> list[tuple[str, str]]:
    """Each clause, with the verb clause it belongs to: a bare noun phrase after
    a verb clause ("hide the clock and the battery") takes that clause's verb."""
    out, last = [], ""
    for clause in _clause_parts(request):
        has_verb = bool(_OPEN.search(clause) or _polarity(clause) is not None
                        or _SETTINGS_WORD.search(clause))
        if has_verb:
            last = clause
            out.append((clause, clause))
        else:
            out.append((clause, last if len(clause.split()) <= 4 else clause))
    return out


def _refused_context(clause: str, verb_clause: str) -> str | None:
    if _REPORTED.search(verb_clause):
        return "the request reports someone else's words"
    if _UNSAFE_CONTEXT.search(verb_clause):
        return "installs, updates and removals are not done here"
    return None


# ---------------------------------------------------------------------------


def _admit(name: str, args: dict, request: str) -> Action | Refusal:
    parts = _clauses_with_verbs(request)
    if name == "launch":
        app = _resolve(LAUNCH_NAMES, LAUNCHABLE, args.get("app"))
        if app is None:
            return Refusal(name, f"{args.get('app')!r} is not a Kilix app, game or tool")
        for index, (clause, verb_clause) in enumerate(parts):
            named = _mentions(LAUNCH_NAMES, app, clause)
            # "enable joustix, then launch it": "it" is the app the clause
            # before named, and only that one.
            if not named and index and re.search(r"\b(?:it|that one|that game|that app)\b", clause) \
                    and _mentions(LAUNCH_NAMES, app, parts[index - 1][0]):
                named, verb_clause = True, clause
            if not named:
                continue
            # "download and open X": a bare verb clause just before governs X too.
            before = parts[index - 1][0] if index else ""
            if before and len(before.split()) <= 2 and _UNSAFE_CONTEXT.search(before):
                return Refusal(name, "installs, updates and removals are not done here")
            reason = _refused_context(clause, verb_clause)
            if reason:
                return Refusal(name, reason)
            verb = _OPEN.search(verb_clause)
            if not verb or _NEGATION.search(verb_clause[:verb.start()]):
                continue
            return Action(name, {"app": app})
        return Refusal(name, f"no part of the request asks to open {app}")
    if name == "show":
        item, on = args.get("item"), args.get("on")
        if item not in ITEMS or not isinstance(on, bool):
            return Refusal(name, "not a known indicator or button, or no on/off")
        for clause, verb_clause in parts:
            if not _mentions(ITEM_NAMES, item, clause):
                continue
            reason = _refused_context(clause, verb_clause)
            if reason:
                return Refusal(name, reason)
            if _polarity(verb_clause) is on:
                return Action(name, {"item": item, "on": on})
        return Refusal(name, f"no part of the request asks to {'show' if on else 'hide'} {item}")
    if name == "pane_stat":
        stat, mode = args.get("stat"), args.get("mode")
        if stat not in STATS or mode not in MODES:
            return Refusal(name, "not cpu or memory, or not auto, always or off")
        for index, (clause, verb_clause) in enumerate(parts):
            if not _mentions(STAT_NAMES, stat, clause) or _REPORTED.search(verb_clause):
                continue
            # The mode in this clause, else in its verb clause, else in the next
            # when that one only says it ("..., switch it off").
            nearby = [clause, verb_clause]
            if index + 1 < len(parts) and re.search(r"\bit\b", parts[index + 1][0]) \
                    and not _mentions(STAT_NAMES, "cpu" if stat == "memory" else "memory",
                                      parts[index + 1][0]):
                nearby.append(parts[index + 1][0])
            where = next((text for text in nearby if _mentions(MODE_NAMES, mode, text)), None)
            # Everything said about this stat, up to the next clause that names a
            # stat, must say one mode: "always, or maybe off" says two.
            span = [where or ""] + [c for c, _ in parts[index + 1:][:next(
                (i for i, (c, _) in enumerate(parts[index + 1:])
                 if any(_mentions(STAT_NAMES, s, c) for s in STATS)), len(parts))]]
            if where and not any(_mentions(MODE_NAMES, other, text)
                                 for other in MODES if other != mode for text in span):
                return Action(name, {"stat": stat, "mode": mode})
        return Refusal(name, f"no part of the request sets pane {stat} to {mode}")
    if name == "game":
        game = _resolve(LAUNCH_NAMES, AVAILABILITY, args.get("game"))
        available = args.get("available")
        if game is None or not isinstance(available, bool):
            return Refusal(name, f"{args.get('game')!r} is not a Kilix game, or no on/off")
        for clause, verb_clause in parts:
            if not _mentions(LAUNCH_NAMES, game, clause):
                continue
            reason = _refused_context(clause, verb_clause)
            if reason:
                return Refusal(name, reason)
            if _polarity(verb_clause) is available:
                return Action(name, {"game": game, "available": available})
        return Refusal(name, f"no part of the request makes {game} "
                             f"{'available' if available else 'unavailable'}")
    # settings
    section = args.get("section")
    if section not in SECTIONS:
        return Refusal(name, "not a settings section")
    for clause, verb_clause in parts:
        if _mentions(SECTION_NAMES, section, clause) and _SETTINGS_WORD.search(verb_clause) \
                and not _REPORTED.search(verb_clause) and not _NEGATION.search(verb_clause) \
                and "center" not in clause and "centre" not in clause:
            return Action(name, {"section": section})
    return Refusal(name, f"no part of the request opens the {section} settings")


def interpret(request: str, calls: list) -> list[Action | Refusal]:
    """Turn the engine's calls into admitted actions or named refusals."""
    results: list[Action | Refusal] = []
    seen = set()
    for call in calls if isinstance(calls, list) else []:
        name = call.get("name") if isinstance(call, dict) else None
        args = call.get("arguments") if isinstance(call, dict) else None
        if name not in TOOL_NAMES or not isinstance(args, dict):
            results.append(Refusal(str(name), "not a kilix-needle apps action"))
            continue
        result = _admit(name, args, request)
        if isinstance(result, Action):
            key = (result.kind, tuple(sorted(result.args.items())))
            if key in seen:
                continue            # the same action twice is one action
            seen.add(key)
        results.append(result)
    return results


# A yes given in advance covers only a launch the request states outright:
# an opening verb with the app named right after it, nothing reported,
# negated or refused. Settings changes are reversible and never need it.
_PLAIN_LAUNCH = re.compile(r"^(?:please |can you |could you |pls )?(?:open|launch|start|play|run)"
                           r" (?:the |a |up )?(?P<name>[\w .+-]+?)(?: please| now| for me)?[.!]?$")


def plain(request: str, actions: list) -> str | None:
    """None when every launch is stated plainly; otherwise why not."""
    launches = [a for a in actions if isinstance(a, Action) and a.kind == "launch"]
    if not launches:
        return None
    parts = _clause_parts(request)
    for action in launches:
        if not any((m := _PLAIN_LAUNCH.match(clause))
                   and _resolve(LAUNCH_NAMES, LAUNCHABLE, m["name"]) == action.args["app"]
                   for clause in parts):
            return f"it does not simply say to open {action.args['app']}"
    return None
