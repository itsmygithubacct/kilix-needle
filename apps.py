"""The apps job: launch Kilix apps and games, and change Kilix settings.

The job has five tools (TOOLS). propose() offers every call they can make and
keeps the ones the request supports; no model answers apps requests (a model
can still be run on them, for benchmarks). Every call is untrusted, whoever
makes it: this module admits a call only when the request itself says it, in
one clause, with a verb that means it. The names a model may use are resolved through tables of
what Kilix really has (the catalog's content ids, kilix-settings' controls), so
nothing outside them can run. See APPS-JOB-DESIGN.md in the research notes.

Excluded on purpose, whatever a model calls: power, installs and downloads
other than a launch a person confirms, bulk changes, transcript budgets and
turning session logging off (data loss), voice engines, models and devices,
volume levels, and closing apps (that is the panes job).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import functools
import re

from actions import Refusal, normalize

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
    "kilix-session-center": ["session center", "session centre", "session tools center",
                             "session tools centre"],
    "kilix-model-store": ["model store", "models store"],
    "kilix-camera-wall": ["camera wall"],
    "kilix-region-painter": ["region painter", "mask painter", "mask editor"],
    "kilix-voice-studio": ["voice studio"],
    "kilix-camera-manager": ["camera manager"],
    "kilix-virtualbox-manager": ["virtualbox manager", "virtualbox", "vm manager"],
    "kilix-weather": ["weather app", "weather"],
    "kilix-calculator": ["calculator", "calc"],
    "kilix-music-control": ["music control", "music controls", "music remote"],
    "kilix-chawan": ["chawan", "text browser", "web browser", "text-mode browser",
                     "text web browser", "terminal web browser"],
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
    "kilix-techno": ["techno", "music workstation", "sequencer", "drum machine",
                     "sequencer workstation"],
    "launcher": ["launcher", "app menu", "application menu", "applications menu"],
    "temps": ["temps", "temperatures", "temperature", "temperatures view", "temperature view"],
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
# Names for several items at once: "remove the split buttons".
ITEM_GROUPS = {"split buttons": ("split_left", "split_right", "split_up", "split_down"),
               "font buttons": ("font_increase", "font_decrease"),
               "font size buttons": ("font_increase", "font_decrease")}
STAT_NAMES = {"cpu": ["cpu", "processor"], "memory": ["memory", "ram", "mem"]}
MODE_NAMES = {"always": ["always", "all the time", "permanently", "at all times", "constantly",
                         "continuously", "on screen", "visible at all times", "visible"],
              "off": ["off", "never", "hide", "hidden", "disable", "stop showing", "no longer",
                      "don't need", "do not need", "anymore"],
              "auto": ["auto", "automatic", "automatically", "only when busy", "when busy",
                       "when needed", "only when needed"]}
SECTION_NAMES = {"top-bar": ["top bar", "top-bar", "topbar", "status bar", "bar at the top"],
                 "pane-buttons": ["pane buttons", "pane-buttons", "pane button",
                                  "buttons on panes", "buttons on the panes", "pane controls",
                                  "buttons shown on panes"],
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
    # The clause of the request that admitted it (apps.propose orders by it;
    # review KN-R16-01). Not part of what the action is.
    at: int = field(default=-1, compare=False, repr=False)

    @property
    def risky(self) -> bool:
        """Starts a program (and, if it is not installed, would install it)."""
        return self.kind == "launch"


# ---------------------------------------------------------------------------
# Reading a request.
#
# History, so it is not repeated. Review R12 found that looking for the right
# words in the clause that names a thing is not enough: "do not, under any
# circumstances, open doom", "my friend said, open doom", "open doom. actually
# cancel that", "should I hide the clock?", "hide the clock, not the battery"
# and "disable every game except doom" were all admitted. So:
#   - a negation, report, cancel, question, condition, contrast, install word,
#     second sentence or non-Latin letter anywhere refuses the whole request;
#   - each verb starts its clause (after polite words), and a launch takes the
#     name as its object ("the memory load is high" opens nothing);
#   - a bare continuation ("... and the battery") takes the verb before it,
#     unless it says its own on or off.

_GAP = r"(?:\s+[\w']+){0,6}?\s+"      # "put the clock back", "turn the text to speech button on"
# Negated wishes that mean "off", said as one phrase: "I don't need the clock",
# "never show cpu on panes"; not "I don't want to play doom".
_NEGATED_WANT = r"\b(?:don'?t|do not|no longer|never)\s+(?:show|want|need|display)\b(?!\s+to\b)"
_ON = [rf"\b(?:turn|switch){_GAP}on\b", rf"\b(?:put|bring|add){_GAP}back\b",
       rf"\bmake{_GAP}available\b", rf"\bmake{_GAP}visible\b", r"\bturn on\b", r"\bswitch on\b", r"\bput back\b",
       r"\bbring back\b", r"\bshow\b(?!\s+me\b)", r"\bdisplay\b", r"\bre ?enable\b",
       r"\benable\b", r"\bunhide\b", r"\brestore\b", r"\breveal\b", r"\b(?:want|like) to see\b",
       r"\ballow\b", r"\bunblock\b", rf"\badd{_GAP}to\b",
       rf"\bput{_GAP}(?:in|on|onto|into) (?:the |my )?(?:top bar|status bar|bar|panes?|games list)\b"]
# A wish reads as on only when nothing in the clause says off: "I want a
# dictation button on my panes", not "I want the clock hidden" or "I'd like
# the battery indicator off the bar"; and not "I want to play doom".
_WISH = [r"\bi (?:want|need)\b(?!\s+to\b)", r"\bi'?d like\b(?!\s+to\b)",
         r"\bi would like\b(?!\s+to\b)"]
_OFF = [rf"\b(?:turn|switch){_GAP}off\b", rf"\bmake{_GAP}unavailable\b",
        rf"\bmake{_GAP}(?:invisible|hidden)\b", rf"\bkeep{_GAP}out of\b",
        rf"\btake{_GAP}(?:off|away|out)\b", rf"\bput{_GAP}away\b", r"\bturn off\b",
        r"\bswitch off\b", r"\bget rid of\b", r"\bstop showing\b", _NEGATED_WANT, r"\bhide\b",
        r"\bremove\b", r"\bdisable\b", r"\bdrop\b", r"\bditch\b", r"\blose\b", r"\bblock\b"]
# "I want the clock gone", but not "the clock is gone".
_OFF_STATE = [r"\bhidden\b", r"\bgone\b", r"\bremoved\b", r"\baway\b", r"\bdisappear\b",
              r"\bvanish\b"]
_NOT_ON = [r"\bstop hiding\b", r"\bun hide\b"]   # read as on
# Third person only: "keep cpu visible" and "get rid of the clock" are instructions,
# and so is "have the panes show cpu" (a "have" that starts the clause).
_FINITE = re.compile(r"\b(?:is|are|was|were|be|been|has|(?<=\s)have|had|looks|seems|feels|"
                     r"gets|got|keeps|stays|works|crashed|broke|died)\b")
# A bare continuation's own word for on or off: "turn the clock on and the battery off".
_PARTICLE_ON = re.compile(r"\b(?:on|back)$|\b(?:available|visible|shown|enabled)\b")
_PARTICLE_OFF = re.compile(r"\b(?:off|hidden|gone|removed|away|unavailable|invisible|disappear|"
                           r"vanish|out of|disabled)\b")
_PARTICLE_VERB = re.compile(r"(?:turn|switch)\b")    # "turn the clock and the battery off"

# Polite words before a verb.
_HEAD = re.compile(r"^(?:(?:please|pls|kindly|ok|okay|alright|hey|hi|yo|just|now|so|"
                   r"(?:can|could|would|will) you(?: please| kindly| be so kind as to)?|can we|"
                   r"i (?:need|want) you to|i'?d like you to|i (?:have|need|want) to|help me|"
                   r"go ahead and|let me|let'?s)\s+)*")
# Verbs, not their -ing forms: "I love playing chess bash" asks for nothing.
_OPEN = re.compile(r"(?:open|pop open|launch|start|run|play|fire up|boot up|boot|bring up|"
                   r"pull up|load up|load|show me|give me|get me|spin up|let'?s play|"
                   r"(?:i )?(?:really |just )?(?:wanna|want to) play|"
                   r"(?:i'?d |i would )?like to play|(?:i'?m |i am )?in the mood for|"
                   r"(?:i )?(?:feel like|fancy)(?: a game of| a round of| some| playing)?|"
                   r"(?:i'?m |i am |anyone |who'?s )?up for (?:a round of|a game of|some)|"
                   r"how about (?:a game of|a round of|some)|a round of|a game of)\b")
# Offers that may end in a question mark: "up for a round of chess?"
_INVITE = re.compile(r"^(?:anyone |who'?s |i'?m )?(?:up for|how about|fancy|feel like)\b")
_SET_VERB = re.compile(r"(?:always |only |never )?(?:set|change|make|put|pin|turn|switch|show|"
                       r"display|hide|keep|leave|stop showing)\b")
_NAV = re.compile(r"(?:open|show me|show|go (?:straight )?(?:to|into)|take me (?:straight )?(?:to|into)|"
                  r"bring up|pull up|jump (?:straight )?(?:to|into)|navigate(?: settings)? to|switch to|head (?:over )?to|"
                  r"get me (?:to|into)|get into|display|configure|change|adjust|tweak|edit|"
                  r"i (?:need|want)(?: to see)?)\b")
# "where are the games settings": a question that only asks the way.
_WAY_QUESTION = re.compile(r"^(?:where (?:are|is|can i|do i)|how (?:do|can) i (?:get to|find|reach|"
                           r"open|change))\b")
_GAME_VERB = re.compile(r"\b(?:enable|disable|re ?enable|available|unavailable|allow|block|"
                        r"unblock)\b")
# What may stand between a launch verb and the name, and what may follow it.
_FILLER = r"(?:(?:the|a|an|my|me|us|some|of|up|round|game|quick|little|new|our|this|that|full|"\
          r"overall)\s+)*"
_TAIL = re.compile(r"(?:please|pls|now|right now|right away|for me|for a bit|for a while|again|"
                   r"thanks|thank you|quickly|real quick|quick|up|too|asap|game|app|tool|program|"
                   r"in (?:a |another |its own )?(?:new )?tab)\b")
_SETTINGS_WORD = re.compile(r"\b(?:settings?|preferences|prefs|options|config(?:ure|uration)?|"
                            r"section|page|screen|panel)\b")

# Request-wide refusals, as (pattern, reason).
_NEGATION = re.compile(r"\b(?:not|never|no|nope|nah|nay|none|nothing|don'?t|do not|avoid|without|cannot|"
                       r"can'?t|won'?t|shouldn'?t|no need to|neither|nor|\w+n't)\b")
_CANCEL = re.compile(r"\b(?:cancel|never ?mind|nvm|scratch that|forget (?:it|that|about it)|"
                     r"actually|wait|hold on|undo|on second thoughts?|changed my mind|"
                     r"ignore (?:that|this|me)|just kidding|kidding|joking|jk|lol|psych|"
                     r"disregard|strike that|belay|abort|oops|maybe|perhaps)\b")
_REPORTED = re.compile(r"\b(?:anyone|anybody|someone|somebody|everyone|everybody|siri|alexa|"
                       r"says|said|say|saying|told|tells|telling|tell me to|asked|asks|"
                       r"asking|wrote|writes|written|reads|read out|according to|claims|"
                       r"claimed|wants me to|suggest\w*|recommend\w*|mention\w*|quot\w*|"
                       r"instruct\w*)\b|\btyp(?:es|ed|ing)\s*:|\bin the story\b|[\"\u00ab\u00bb\u2039\u203a\u201a\u201e]|"
                       r"(?<!\w)'[^']+'(?!\w)")    # quotation marks or a quoted span
_QUESTION = re.compile(r"^(?:(?:so|and|but|ok|okay|hey|hmm|um|well)\s+)*(?:should|shall|"
                       r"how(?! about)|what|why|when|where|which|who|whose|is|are|am|was|were|"
                       r"does|did|do (?:i|we|you)|has|had|have (?:you|i|we)|will (?:i|it|that)|"
                       r"would (?:it|that|i)|could (?:i|it)|can i|may i|might)\b")
_POLITE_ASK = re.compile(r"^(?:(?:please|pls|ok|okay|hey|hi)\s+)*(?:can|could|would|will) you\b")
_CONDITION = re.compile(r"\b(?:if|unless|whether|in case|suppose|supposing|imagine|pretend|"
                        r"hypothetical\w*|theoretically|in theory|simulate|assuming|once i|when i|"
                        r"provided|as long as|as soon as|ever|how (?:to|do|does|can|could|would|should)|"
                        r"how about (?!a game of|a round of|some\b))\b")
# "hide the clock later": a when is not now. Sequence words are fine.
_WHEN = re.compile(r"\b(?:later|tonight|tomorrow|today|yesterday|after|before|during|until|till|"
                   r"whenever|while (?:i|you|we|he|she|they|the|my)|midnight|noon|o'?clock|\d+ ?(?:am|pm)|in \d+|in an? (?:hour|"
                   r"minute|second|bit|while|moment)|in a few|soon|eventually|someday|next|"
                   r"weekends?|weekdays?|every (?:day|night|morning|evening))\b")
_SEQUENCE = re.compile(r"\b(?:(?:and )?(?:after that|afterwards?)|before i forget)\b")
# Alternatives, and "all games bar doom".
_LEVEL = re.compile(r"\b(?:to|by|at)\s+(?:half|max(?:imum)?|min(?:imum)?|zero|full|\d+)\b|"
                    r"\d\s*%|\bpercent\b|\blouder\b|\bquieter\b")
_EITHER = re.compile(r"\b(?:or|either)\b")
_BAR_SAVE = re.compile(r"\b(?:all|every|everything|each|any)\b.*(?<!top )(?<!status )(?<!the )"
                       r"(?<!task )(?<!menu )(?<!my )(?<!panes )\b(?:bar|save)\b")
_CONTRAST = re.compile(r"\b(?:except|excepting|excluding|rather than|instead of|instead|swap\w*|"
                       r"replac\w*|exchange|"
                       r"but not|other than|apart from|aside from|besides|save for|in place of|"
                       r"as opposed to|versus|vs)\b")
_INSTALL = re.compile(r"\b(?:re ?install\w*|install\w*|uninstall\w*|updat\w*|upgrad\w*|"
                      r"download\w*|set up|setup|grab|fetch|delete|purge|from the store|"
                      r"remove the app|build|compile)\b")
# "if you could open doom, that would be great" is a polite instruction.
_POLITE_IF = re.compile(r"if you (?:could|can|would)(?: please)? (?P<ask>[^,]+?)(?:,? (?:that would be|"
                        r"that'?d be) (?:great|nice|lovely|awesome|good|perfect))?[.!]*")
_COURTESY = re.compile(r"(?:thanks|thank you|thx|ty|cheers|please|pls|ta|hi|hello|hey|ok|okay)"
                       r"(?: (?:so much|a lot|very much|there))?[.!]*")
# Where a program opens, other than a new tab, is the panes job.
_PLACED = re.compile(r"\b(?:in|into|on|at|to|beside|next to|below|above|under)\s+(?:(?:the|a|an|"
                     r"this|that|my|another|current|same|other|its own)\s+)?(?:[\w']+\s+)?"
                     r"(?:pane|split|window|column|tab)\b(?!\s+(?:buttons?|transcripts?|stats?|"
                     r"left|right|up|down|above|below|list|bar)\b)")
_NEW_TAB = re.compile(r"\b(?:in|into)\s+(?:a|another|its own)\s+(?:new\s+)?tab\b")


# "turn off the microphone" is the device, not the pane button.
_DEVICE_ITEMS = frozenset({"volume", "dictate", "speak", "network"})   # "turn off the wifi"
# Only a display verb or a widget word makes a device word the indicator
# (held-out v2: "drop the volume to half" read "drop" as hide).
# Only the plain display verbs: "restore the volume", "bring the wifi back",
# "remove the microphone" and "take the mic off mute" mean the device
# (review R12 round 9).
_DISPLAY_VERB = re.compile(r"\b(?:show|hide|hidden|display|unhide|reveal|stop showing)\b")
_WIDGET = re.compile(r"\b(?:buttons?|icons?|indicators?|widgets?|bar|panes?)\b")


def _alternation(table: dict) -> str:
    return "|".join(re.escape(" ".join(re.sub(r"[_\-]+", " ", w).split()))
                    for w in sorted({w for key in table for w in [key, *table[key]]},
                                    key=len, reverse=True))


# Terse forms with no verb that still say one thing: "pane ram always",
# "cpu on panes off", "tools options please", "settings for the speech".
_TERSE_STAT = re.compile(rf"(?:the )?(?:pane )?(?:{_alternation(STAT_NAMES)})(?: (?:stat|meter|usage|"
                         rf"readout))?(?: on (?:the |my )?panes?)?(?: (?:on|to|at))? "
                         rf"(?:{_alternation(MODE_NAMES)})(?: please)?")
_TERSE_SETTINGS = re.compile(rf"(?:(?:the )?(?:settings|options|preferences) for (?:the )?"
                             rf"(?:{_alternation(SECTION_NAMES)})|(?:the )?(?:{_alternation(SECTION_NAMES)}) "
                             rf"(?:settings|options|preferences|section|page))(?: please)?")


_CLAUSE_SPLIT = re.compile(r"\s*(?:,|;|:|\band then\b|\bthen\b|\bfollowed by\b|\band\b)\s*")


@functools.lru_cache(maxsize=65536)
def _plain_words(text: str) -> str:
    """Lower case, hyphens and underscores as spaces, backticks as apostrophes, single spaces."""
    # U+037E is the Greek question mark: NFKC makes it ";", a clause separator
    # (review R12 round 6), so it is read as the question it is.
    text = str(text).replace("\u037e", "?")
    return " ".join(re.sub(r"[_\-]+", " ", normalize(text).casefold().replace("`", "'")).split())


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


def _is_name(table: dict, key: str, text: str) -> bool:
    """The text is exactly one of key's names (or, for items, a group holding it)."""
    said = _plain_words(text)
    if table is ITEM_NAMES and key in ITEM_GROUPS.get(said, ()):
        return True
    return said in _names(table, key)


# Phrases that say when, holding an item's name: "all the time" names no
# clock (found by apps.propose on held-out v1: "I want memory usage visible on
# panes all the time" admitted showing the clock to any caller that asked).
_NOT_AN_ITEM = re.compile(r"(?<![\w])(?:(?:all|most|some|much|half|part|none) of the time|"
                          r"all the time|half the time|at all times|at times|from time to time|"
                          r"time to time|any ?time|every ?time|some ?time|this time|next time|"
                          r"last time|in time|on time|the whole time|full time|part time|"
                          r"up to date|out of date|to date)(?![\w])")
# An item's name counts only as the whole object of its clause: the name,
# then any widget nouns ("the clock icon", "the battery percentage"), then
# the end of the clause or a word that ends the object. "temp files",
# "network status page", "the clock 2 files" name something else (reviews
# KN-R16-02, KN-R16-202).
_WIDGET_NOUN = re.compile(
    r"(?:buttons?|icons?|indicators?|widgets?|controls?|toggles?|symbols?|badges?|labels?|"
    r"readouts?|meters?|displays?|gauges?|sliders?|applets?|percentage|status|list|size|"
    r"figures?|numbers?|counters?|tiles?|things?|ones?|bits?|pieces?|stuff|info|information|"
    r"panes?|bar)$")
_OBJECT_END = re.compile(
    r"(?:and|or|nor|on|off|back|in|into|onto|from|to|at|for|too|again|please|pls|now|then|also|"
    r"but|so|while|when|if|as|with|without|is|are|be|was|were|the|a|an|my|our|this|that|it|"
    r"visible|hidden|shown|showing|displayed|displaying|appear|appears|disappear|gone|removed|"
    r"available|unavailable|enabled|disabled|active|inactive|"
    r"away|out|always|never|auto|automatically|anymore|any|more|there|here|top|bottom|up|down|"
    r"thanks|thank|completely|entirely|altogether|permanently|fully|totally|immediately|"
    r"instead|right|for good)$")


# Masks when-phrases. A NUL never reaches the checks: engine.check_prompt
# refuses control characters in every CLI and MCP request (review KN-R16-4).
_MASK = "\x00"


@functools.lru_cache(maxsize=4096)
def _without_when(clause: str) -> str:
    """The clause with its when-phrases blanked, the same length, so every
    position still points into it."""
    return _NOT_AN_ITEM.sub(lambda m: _MASK * len(m.group()), clause)


# Where a widget is: after "on", "in", "from"... only these may follow, so
# "the clock icon on the poster" names no indicator (review KN-R16-303).
_PLACE = re.compile(
    r"(?:(?:the|my|our|your|a|an|each|every|all|of|kilix|kilix's|top|bottom|upper|lower|"
    r"left|right|status|task|menu|title|"
    r"pane|panes|bar|taskbar|panel|screen|header|headers|desktop|corner|tray|tab|tabs|"
    r"button|buttons|list|area|row|strip|toolbar|window|windows|terminal|here|there|"
    r"controls?|icons?|indicators?|widgets?|topbar|statusbar|at|in|on|for|me|us)(?:\s+|$))+")
_PREPOSITION = re.compile(r"(?:on|in|from|at|into|onto|to|out of|off of|off)$")
_PLACE_TAIL = re.compile(r"completely|entirely|altogether|permanently|fully|totally|now|"
                         r"please|again|too|for good|immediately")
# The object of these is what they describe: "pictures of the clock", "files
# about the clock" (review KN-R16-303).
_DESCRIBED = re.compile(r"(?:^|\s)(?:(?<!rid )(?<!out )(?<!off )(?<!instead )of|about|regarding|"
                        r"named|called|containing|pictures? of|files? of)"
                        r"\s+(?:(?:the|my|a|an|this|that|some)\s+)?$")


_LIST_QUALIFIER = (r"(?:in|on|from|off|into|out of) (?:the |my |our |this |that )?"
                   r"(?:games? )?(?:list|picker|menu)")


def _game_object(text: str) -> bool:
    """The clause is one game's name, and at most where it is listed:
    "pong", "pong in the games list" (review KN-R16-501)."""
    shape = re.fullmatch(rf"(?:(?:the|my|a|an) )?(?P<name>.+?)(?: {_LIST_QUALIFIER})?"
                         r"(?: (?:please|too|as well))?", text)
    return bool(shape) and any(_is_name(LAUNCH_NAMES, game, shape["name"])
                               for game in AVAILABILITY)


def _names_the_item(clause: str, match: re.Match) -> bool:
    """The name is the whole object of its clause: nothing before it makes it a
    description, and after it come only widget nouns, then the end of the
    object: sentence punctuation, a masked when-phrase, a word that ends it,
    or a place a widget can be (reviews KN-R16-02, -202, -301, -303)."""
    if _DESCRIBED.search(clause[:match.start()]):
        return False
    rest = clause[match.end():]
    while True:
        if not rest.strip() or re.match(rf"\s*(?:[,;:.!?](?:\s|$)|{_MASK})", rest):
            return True
        token = re.match(r"\s+([a-z']+)(?=\s|$|[,;:.!?]|" + _MASK + ")", rest)
        if token is None:
            return False                    # a number, a path, a file name, a symbol
        word = token.group(1)
        if _WIDGET_NOUN.match(word):
            rest = rest[token.end():]       # "the clock icon ..."
            continue
        two = re.match(r"\s+(out of|off of)\b", rest)
        if _PREPOSITION.match(word) or two:
            after = rest[(two or token).end():]
            place = re.match(r"\s*(.*?)\s*(?:[,;:.!?](?:\s|$)|$|" + _MASK + r"|\b(?:and|then|"
                             r"but|please|too|now|again|so|when|while)\b)", after)
            words = place.group(1) if place else after
            if not words:
                # "turn the clock on", "take it off": a particle, not a place
                return word in ("on", "off")
            # "from the top bar completely": an adverb may close a place,
            # never stand for one (reviews KN-R16-402, -502).
            tail = words.split()
            while tail and _PLACE_TAIL.fullmatch(tail[-1]):
                tail.pop()
            return bool(tail) and bool(_PLACE.fullmatch(" ".join(tail)))
        return bool(_OBJECT_END.match(word))


def _mentions(table: dict, key: str, clause: str) -> re.Match | None:
    """Where the clause names this key, by its longest name there, never a
    shorter name inside a longer one of another key ('chess' in 'chess bash' is
    chess bash; 'browser' in 'file browser' is the file browser)."""
    if table is ITEM_NAMES:
        clause = _without_when(clause)
    for phrase in _names(table, key):
        match = _said(phrase, clause)
        if match and table is ITEM_NAMES and not _names_the_item(clause, match):
            match = None
        if match:
            longer = [other for other in table if other != key
                      for p in _names(table, other)
                      if len(p) > len(phrase) and phrase in p and _said(p, clause)]
            if not longer:
                return match
    return None


def _mentions_item(item: str, clause: str) -> bool:
    """The clause names the item, or a group holding it, as its whole object
    (a group is checked like a name: review KN-R16-302)."""
    if _mentions(ITEM_NAMES, item, clause):
        return True
    masked = _without_when(clause)
    for group, members in ITEM_GROUPS.items():
        if item in members:
            match = _said(group, masked)
            if match and _names_the_item(masked, match):
                return True
    return False


def _polarity(clause: str) -> bool | None:
    """True, False, or None when the clause says neither or both."""
    text = clause
    found = set()
    lists = [(_NOT_ON, True), (_OFF, False), (_ON, True)]
    if not _FINITE.search(clause):
        lists.append((_OFF_STATE, False))
    if not _PARTICLE_OFF.search(clause):
        lists.append((_WISH, True))
    for patterns, value in lists:
        for pattern in patterns:
            if re.search(pattern, text):
                found.add(value)
                text = re.sub(pattern, " ", text)
    return found.pop() if len(found) == 1 else None


def _body(clause: str) -> str:
    """The clause after its polite words."""
    return clause[_HEAD.match(clause).end():]


def _opening(clause: str) -> str | None:
    """What follows the opening verb, when the clause starts with one."""
    body = _body(clause)
    match = _OPEN.match(body)
    return body[match.end():].strip() if match else None


def _object(text: str, table: dict, key: str) -> bool:
    """The text is filler words, then a name of key, then at most a tail that
    says nothing more about what it is ("doom for me", not "memory tests")."""
    for phrase in _names(table, key):
        match = re.match(rf"{_FILLER}{re.escape(phrase)}(?!\w)\s*", text)
        if match and (match.end() == len(text) or _TAIL.match(text, match.end())):
            return bool(_mentions(table, key, text))
    return False


def _particle(clause: str) -> bool | None | str:
    on, off = bool(_PARTICLE_ON.search(clause)), bool(_PARTICLE_OFF.search(clause))
    return "both" if on and off else True if on else False if off else None


@dataclass(frozen=True)
class Part:
    """One clause, the verb clause it belongs to, and the on/off it says."""
    text: str
    verb: str
    on: bool | None
    bare: bool = False


@dataclass(frozen=True)
class Reading:
    parts: tuple = ()
    refusal: str | None = None     # refuses every call
    placed: bool = False           # says where a program opens: the panes job
    way: bool = False              # only asks where something is ("where are the games settings")


def _has_verb(clause: str) -> bool:
    body = _body(clause)
    return (_polarity(clause) is not None or _opening(clause) is not None
            or bool(_SET_VERB.match(body) or _NAV.match(body) or _PARTICLE_VERB.match(body)))


def _is_bare(clause: str) -> bool:
    return len(clause.split()) <= 7 and not _FINITE.search(clause)


def _refusal(text: str) -> str | None:
    """Why the whole request is refused, if it is."""
    if any(c.isalpha() and not ("a" <= c <= "z" or "ß" <= c <= "ɏ") for c in text):
        return "the request has letters outside the Latin alphabet"
    if _REPORTED.search(text):
        return "the request reports someone else's words"
    if _CANCEL.search(text):
        return "the request takes something back"
    unwanted = text
    for pattern in [_NEGATED_WANT, *_NOT_ON]:
        unwanted = re.sub(pattern, " ", unwanted)
    if _NEGATION.search(unwanted):
        return "the request says not to, or not what: say just what to do"
    if _CONTRAST.search(text):
        return "the request makes an exception or a contrast: name just what to change"
    if _CONDITION.search(text):
        return "the request is a condition or a supposition, not an instruction"
    if _WHEN.search(_SEQUENCE.sub(" ", text)):
        return "the request says when, not now"
    if _LEVEL.search(text):
        return "volume and other levels are not set here"
    if _EITHER.search(text) or _BAR_SAVE.search(text):
        return "the request offers alternatives or exceptions: name just what to change"
    if _INSTALL.search(text):
        return "installs, updates and removals are not done here"
    return None


def _context_ok(segment: str) -> bool:
    """A sentence before the instruction that sets the scene and asks for
    nothing ("It's cold out", "what's the weather doing?"). It must pass every
    whole-request check (review R15: a negation, report, take-back, condition
    or time there was ignored), name no app, item, stat, section or game (so a
    thing is never said both ways across sentences), and speak of nobody else
    and no message, note or example."""
    body = segment.strip(" .!")
    # Only one small shape: "I'm bored", "it's so cold out", "long day"
    # (review R15 round 3: any bag of allowed words could frame, time or take
    # back the instruction: "Write it down.", "One day.", "Just playing.").
    if not body or "?" in body or not _SCENE.fullmatch(body):
        return False
    if _refusal(body) or _names_something(body):
        return False
    # "The date display is cluttering the top bar": a statement, whatever
    # words it holds, unless it starts with an instruction verb.
    statement = bool(_FINITE.search(body)) and not (
        _OPEN.match(_body(body)) or _SET_VERB.match(_body(body)) or _NAV.match(_body(body))
        or _PARTICLE_VERB.match(_body(body)))
    if not statement and (_has_verb(body) or _opening(body) is not None):
        return False
    return True


# The only closing purposes: "so I can check these numbers", "for this document".
_PURPOSE_SHAPE = re.compile(r"(?:so (?:that )?(?:i|we) can (?:take a look|have a look|check (?:something|"
                            r"it|this|these numbers|the numbers|my numbers)|see (?:it|what's there))"
                            r"|for (?:this|my|our) (?:document|file|files|work|sums))")
# The only scene-setting sentences: a state of the speaker or the weather, and
# a few fixed phrases.
_SCENE_STATE = (r"bored|tired|sleepy|cold|chilly|freezing|hot|warm|raining|snowing|sunny|windy|"
                r"stressed|hungry|restless|stuck|busy|free|back|home|done|relaxed|curious|excited|"
                r"cheerful|grumpy|sad|happy")
_SCENE = re.compile(rf"(?:(?:ok|okay|alright|well|so|phew|ugh|hmm)[, ]+)?"
                    rf"(?:(?:i'?m|i am|it'?s|it is|i feel|feeling|we'?re|we are)(?: (?:so|really|a bit|"
                    rf"a little|pretty|quite|very|too|kind of))? (?:{_SCENE_STATE})(?: out| outside)?"
                    rf"|long day|what a day|need a break|quick one|phew|ugh)")
# "Only on Friday. Open doom.": a day is a when (review R15).
_DAYS = re.compile(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend|morning|"
                   r"afternoon|evening|night|once|first|then|later|soon)\b")
# Context that brings in someone else's words or a text (review R15, KN-R15-03
# and -10): "Message from my son. Open doom.", "Example sentence for the manual".
_OTHERS = re.compile(r"\b(?:son|daughter|kids?|child(?:ren)?|wife|husband|partner|mom|mum|dad|"
                     r"mother|father|brother|sister|boss|friend|colleague|roommate|he|she|they|"
                     r"his|her|their|them|someone|somebody|anyone|message|note|email|e mail|text|"
                     r"texted|sms|chat|example|sample|manual|story|script|following|quote|typed|"
                     r"sent|wrote|written|reads?|says?|test|demo|prompt|instructions?)\b")


# Where a request ends in why ("... so I can check these numbers", "... for
# this document"): the reason, when it names nothing and asks for nothing.
_PURPOSE = re.compile(r"\s+(?:so (?:that )?(?:i|we)\b|because\b|since\b|as i\b|(?:for|to) "
                      r"(?:this|these|that|those|my|our|some|a|an)\b)(?P<why>.*)$")


def _without_purpose(sentence: str) -> str:
    """The sentence without a closing purpose that passes every whole-request
    check, is short, names nothing, places nothing and goes on to nothing
    (review R15, KN-R15-05: "for a minute, no, don't", "for a bit tomorrow",
    "for a minute, then off", "to a new pane")."""
    match = _PURPOSE.search(sentence)
    if not match:
        return sentence
    why = match["why"].strip(" .!")
    if not _PURPOSE_SHAPE.fullmatch(match.group(0).strip(" .!")):
        return sentence             # only the fixed purposes (review R15 rounds 2 and 3)
    # Only after opening something: "hide the clock so I can see it" says
    # both ways (review R15 round 4, KN-R15-23).
    head = _body(sentence[:match.start()])
    if not (_OPEN.match(head) or _NAV.match(head)) or _polarity(sentence[:match.start()]) is False:
        return sentence
    if len(why.split()) > 8 or _refusal(match.group(0)) or _OTHERS.search(why) \
            or _names_something(why) or _has_verb(why) or _opening(why) is not None \
            or _PLACED.search(match.group(0)) or re.search(r"[,;:]|\b(?:then|and|or|but)\b", why) \
            or re.search(r"\b(?:minute|minutes|hour|hours|second|seconds|bit|while|moment)\b", why) \
            or _particle(why) is not None:
        return sentence
    return sentence[:match.start()]


def _segments(request: str) -> list[str]:
    """Sentences, and the part before a dash that sets the scene ("Need to jot
    something down - launch the text editor"), each in plain words."""
    return [_plain_words(p) for p in _pieces(request) if p and _plain_words(p).strip(" .!?")]


def _pieces(request: str) -> list[str]:
    raw = str(request).replace("\u037e", "?")
    parts = re.split(r"\s*[\u2014\u2013\u2012\u2015]\s*|\s+-{1,2}\s+|(?<=\w)-{2,}(?=\w)", raw)
    out = []
    for part in parts:
        out += re.split(r"(?<=[.!?;])\s+|\s*;\s*", normalize(part))
    return [p for p in out if p and p.strip()]


_PANE_WORD = re.compile(r"\bpanes?\b|\bper pane\b")
# The words that said "always" before the apps widening, with or without "pane".
_ALWAYS_BEFORE = ("always", "all the time", "permanently", "at all times", "constantly")
_FROM_LIST = re.compile(r"\b(?:from|off|out of) (?:the |my )?available games\b")


def _read(request: str, widen: bool = False) -> Reading:
    text = _plain_words(request)
    if widen:
        text = _FROM_LIST.sub("from the games list", text)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip(" .!?")]
    asked = [s for s in sentences if not _COURTESY.fullmatch(s)]
    if widen and len(asked) > 1 or widen and len(_segments(request)) > 1:
        # Scene-setting sentences around one instruction are set aside
        # ("It's cold out; what's the weather doing? Pull up the weather app").
        # Every question mark stays with words that are not the instruction or
        # courtesy ("? open doom", "open doom. ?", "can you open doom? thanks?"
        # are still questions), and a negation is a reason only before the
        # instruction ("no graphics needed; start the text browser"), never a
        # take-back after it ("hide the clock - no, don't").
        pieces = _pieces(request)
        segments = [_plain_words(p) for p in pieces]
        if any("?" in p and not _plain_words(p).strip(" .!?") for p in pieces) \
                or any("?" in s and _COURTESY.fullmatch(s.rstrip(" ?")) for s in segments):
            return Reading(refusal="the request is a question, not an instruction")
        segments = [s for s in segments if s.strip(" .!?") and not _COURTESY.fullmatch(s)]
        kinds = [_context_ok(s) for s in segments]
        instructions = [s for s, context in zip(segments, kinds) if not context]
        # A piece that is neither scene-setting nor an instruction refuses:
        # "Example sentence for the manual; open doom" (review R15, KN-R15-10).
        if any(not context and not (_has_verb(s.strip(" .!?")) or _opening(s.strip(" .!?")))
               for s, context in zip(segments, kinds)):
            return Reading(refusal="the request says more than one sentence: one instruction at a time")
        if len(instructions) == 1:
            at = segments.index(instructions[0])
            if segments[at + 1:]:
                # After the instruction only courtesy (review R15, KN-R15-01:
                # "Open doom. I take that back.", "Open doom. Sorry, solitaire.").
                return Reading(refusal="the request says more after the instruction: "
                                       "one instruction at a time")
            asked = instructions
            text = instructions[0]
        elif not instructions:
            return Reading()
    if len(asked) > 1:
        return Reading(refusal="the request says more than one sentence: one instruction at a time")
    if not asked:
        return Reading()
    sentence = asked[0]
    if widen:
        sentence = _without_purpose(sentence)
    polite = _POLITE_IF.fullmatch(sentence)
    if polite:
        sentence = polite["ask"]
    way = bool(_WAY_QUESTION.match(sentence))
    # Any question mark anywhere makes it a question (review R12 round 5:
    # "open doom?!", "? open doom"), except the one that closes a polite ask
    # or an offer ("can you open doom?"), or a way-finding question.
    closing = re.search(r"[.!?\s]*$", sentence).group()
    polite_mark = (text.count("?") == 1 and closing.strip() == "?"
                   and bool(_POLITE_ASK.match(sentence) or _INVITE.match(sentence)))
    if not way and (_QUESTION.match(sentence) or "?" in text and not polite_mark):
        return Reading(refusal="the request is a question, not an instruction")
    # A way-finding question's own "how do I" is not a supposition.
    reason = _refusal(_WAY_QUESTION.sub("", sentence, count=1) if way else sentence)
    if reason:
        return Reading(refusal=reason)
    sentence = sentence.rstrip(" .!?")
    clauses = []
    for index, chunk in enumerate(re.split(r"\s*,?\s*\bbut\b\s*", sentence)):
        # Its own split, not the panes job's: that one blanks quoted text, and a
        # blanked span vanished from the reading (review R12 round 4).
        pieces = [c.strip() for c in _CLAUSE_SPLIT.split(chunk) if c.strip()]
        clauses += [(c, index > 0 and n == 0) for n, c in enumerate(pieces)]
    parts, last = [], None
    for index, (clause, after_but) in enumerate(clauses):
        if _has_verb(clause):
            on = _polarity(clause)
            if on is None and _PARTICLE_VERB.match(_body(clause)):
                # "turn the clock and the battery off": the group's last word says.
                group = []
                for following, _ in clauses[index + 1:]:
                    if _has_verb(following) or not _is_bare(following):
                        break
                    group.append(following)
                said = _particle(group[-1]) if group else None
                on = said if isinstance(said, bool) else None
            last = Part(clause, clause, on)
            parts.append(last)
        elif last is not None and _is_bare(clause):
            if after_but:
                # "disable every game but doom": "but" there means except.
                return Reading(refusal="the request makes an exception: name just what to change")
            if _particle(clause) is not None and not _names_something(clause):
                # "turn the clock off and on": an on or off that names nothing.
                return Reading(refusal="the request says on or off without saying what")
            own = _particle(clause)
            if own is None:
                on = last.on
            elif own == "both":
                on = None
            elif _PARTICLE_VERB.match(_body(last.verb)) or own == last.on:
                on = own
            else:
                on = None                  # "enable doom and solitaire off"
            parts.append(Part(clause, last.verb, on, bare=True))
        else:
            last = None                    # "the battery is low" ends the verb's reach
            parts.append(Part(clause, clause, None))
    placed = bool(_PLACED.search(_NEW_TAB.sub(" ", sentence)))
    return Reading(tuple(parts), None, placed, way)


# ---------------------------------------------------------------------------


def _admit(name: str, args: dict, reading: Reading) -> Action | Refusal:
    parts = reading.parts
    if name == "launch":
        app = _resolve(LAUNCH_NAMES, LAUNCHABLE, args.get("app"))
        if app is None:
            return Refusal(name, f"{args.get('app')!r} is not a Kilix app, game or tool")
        if reading.placed:
            return Refusal(name, "where a program opens in a pane is the panes job; "
                                 "this job opens it in a new tab")
        # "I don't want doom; open doom": another part turns this app off.
        if any(part.on is False and _mentions(LAUNCH_NAMES, app, part.text)
               and not any(_mentions_item(item, part.text) for item in ITEMS) for part in parts):
            return Refusal(name, f"the request says {app} both ways")
        for index, part in enumerate(parts):
            if part.bare:
                if _opening(part.verb) is not None and _object(part.text, LAUNCH_NAMES, app) \
                        and _names_kind(LAUNCH_NAMES, LAUNCHABLE, part.verb):
                    return Action(name, {"app": app}, at=index)
                continue
            rest = _opening(part.text)
            if rest is None:
                continue
            if _object(rest, LAUNCH_NAMES, app):
                return Action(name, {"app": app}, at=index)
            # "enable joustix, then launch it": "it" is the app the clause
            # before named, and only that one.
            it = re.match(r"(?:it|that one|that game|that app)(?!\w)\s*", rest)
            if it and index and (it.end() == len(rest) or _TAIL.match(rest, it.end())) \
                    and _mentions(LAUNCH_NAMES, app, parts[index - 1].text):
                return Action(name, {"app": app}, at=index)
        return Refusal(name, f"no part of the request asks to open {app}")
    if name == "show":
        item, on = args.get("item"), args.get("on")
        if item not in ITEMS or not isinstance(on, bool):
            return Refusal(name, "not a known indicator or button, or no on/off")
        # Said both ways anywhere in the request, it is refused (held-out v1:
        # "...: clock hidden, battery shown" admitted hiding the battery).
        if _both_ways(parts, lambda text: _mentions_item(item, text)):
            return Refusal(name, f"the request says {item} both ways")
        for index, part in enumerate(parts):
            # The widget word may be in the verb's clause or its continuations
            # ("remove the read aloud and wifi icons from the top bar").
            # A widget word that is part of another item's name ("the close
            # button") does not say this one is an indicator (round 9).
            group = _without_other_names(item, " ".join(p.text for p in parts
                                                        if p.verb == part.verb))
            # "show the wifi password": a device word with a noun of its own.
            if item in _DEVICE_ITEMS and re.search(
                    rf"(?:{'|'.join(map(re.escape, _names(ITEM_NAMES, item)))})\s+{_DEVICE_NOUN}",
                    part.text):
                continue
            if item in _DEVICE_ITEMS and (not _DISPLAY_VERB.search(part.verb)
                                          and not _WIDGET.search(group)
                                          or _MUTE.search(" ".join(p.text for p in parts))):
                continue
            # "hide the clock and doom": a bare name continues only its own kind.
            if part.bare and not any(_mentions_item(other, part.verb) for other in ITEMS):
                continue
            if _mentions_item(item, part.text) and part.on is on:
                return Action(name, {"item": item, "on": on}, at=index)
        return Refusal(name, f"no part of the request asks to {'show' if on else 'hide'} {item}")
    if name == "pane_stat":
        stat, mode = args.get("stat"), args.get("mode")
        if stat not in STATS or mode not in MODES:
            return Refusal(name, "not cpu or memory, or not auto, always or off")
        other = "cpu" if stat == "memory" else "memory"
        # Two modes for this stat anywhere ("set pane cpu to always: cpu off").
        if len({m for p in parts if _mentions(STAT_NAMES, stat, p.text)
                for m in MODES if _mentions(MODE_NAMES, m, p.text)}) > 1:
            return Refusal(name, f"the request says two modes for pane {stat}")
        for index, part in enumerate(parts):
            if not _mentions(STAT_NAMES, stat, part.text) \
                    or part.bare and not _names_kind(STAT_NAMES, STATS, part.verb):
                continue
            # A verb that sets or shows, in an instruction: not "my memory is always full".
            if not (_SET_VERB.match(_body(part.verb)) or _polarity(part.verb) is not None
                    or _TERSE_STAT.fullmatch(part.text)) or _FINITE.search(part.verb):
                continue

            def modes(text):
                return {m for m in MODES if _mentions(MODE_NAMES, m, text)}
            said = modes(part.text)
            if not said and part.bare:
                said = modes(part.verb)
            if not said and part.on is False and _PANE_WORD.search(part.text) \
                    and not _particle(part.text) == "both":
                said = {"off"}              # "remove memory consumption from pane headers"
            if said & {"always"} and not _PANE_WORD.search(part.text) \
                    and _mentions(MODE_NAMES, "always", part.text) \
                    and not any(_said(w, part.text) for w in _ALWAYS_BEFORE):
                continue                    # "keep the memory view visible" is not a pane setting
            # "..., switch it off": the next clause says the mode of this one.
            if not said and index + 1 < len(parts) and re.search(r"\bit\b", parts[index + 1].text) \
                    and not _mentions(STAT_NAMES, other, parts[index + 1].text):
                said = modes(parts[index + 1].text)
            if said != {mode} or (part.on is False and mode != "off"):
                continue
            # Everything said up to the next clause that names something must
            # not say another mode: "always, or maybe off" says two.
            following = []
            for later in parts[index + 1:]:
                if _names_something(later.text):
                    break
                following.append(later.text)
            if not any(modes(text) - {mode} for text in following):
                return Action(name, {"stat": stat, "mode": mode}, at=index)
        return Refusal(name, f"no part of the request sets pane {stat} to {mode}")
    if name == "game":
        game = _resolve(LAUNCH_NAMES, AVAILABILITY, args.get("game"))
        available = args.get("available")
        if game is None or not isinstance(available, bool):
            return Refusal(name, f"{args.get('game')!r} is not a Kilix game, or no on/off")
        if _both_ways(parts, lambda text: _mentions(LAUNCH_NAMES, game, text)):
            return Refusal(name, f"the request says {game} both ways")
        for index, part in enumerate(parts):
            named = _mentions(LAUNCH_NAMES, game, part.text)
            # "fire up pong and afterwards make it unavailable"; not "enable doom then hide it".
            if not named and index and re.search(r"\b(?:it|that one|that game)\b", part.text) \
                    and _GAME_VERB.search(part.text) \
                    and _mentions(LAUNCH_NAMES, game, parts[index - 1].text):
                named = True
            if part.bare and not _names_kind(LAUNCH_NAMES, AVAILABILITY, part.verb):
                continue
            # A wish for a game ("I want mines", "I don't want mines; open
            # mines") requests no availability change unless it says where:
            # the games list, picker or menu. Bare continuations carry their
            # verb's wish: "I don't need doom and pong" changes neither
            # (reviews KN-R16-03, KN-R16-201).
            wish = (re.search(_NEGATED_WANT, part.verb)
                    or any(re.search(pattern, part.verb) for pattern in _WISH))
            # A list named by a game object in the coordinated group scopes
            # all of its games: "I want doom and pong in the games list"
            # (review KN-R16-304). A continuation is a game object only when
            # it is a game's name and at most that list: "a screenshot of
            # the games list" and "the pong games list screenshot" are other
            # objects, lend no scope and change no game (KN-R16-401, -501).
            if wish and part.bare and not _game_object(part.text):
                continue
            group = " ".join(p.text for p in parts if p.verb == part.verb
                             and (not p.bare or _game_object(p.text)))
            list_scope = re.search(
                rf"\b{_LIST_QUALIFIER}\b|\bgames? (?:list|picker|menu)\b",
                part.verb + " " + group)
            wish_only = wish and not (
                _GAME_VERB.search(part.verb) or _GAME_VERB.search(part.text)
                or list_scope)
            if named and part.on is available and not wish_only:
                return Action(name, {"game": game, "available": available}, at=index)
        return Refusal(name, f"no part of the request makes {game} "
                             f"{'available' if available else 'unavailable'}")
    # settings
    section = args.get("section")
    if section not in SECTIONS:
        return Refusal(name, "not a settings section")
    for index, part in enumerate(parts):
        asks = _NAV.match(_body(part.verb)) or reading.way or _TERSE_SETTINGS.fullmatch(part.text)
        if asks and _mentions(SECTION_NAMES, section, part.text) \
                and _SETTINGS_WORD.search(part.text) \
                and (reading.way or not _FINITE.search(part.text)) \
                and "center" not in part.text and "centre" not in part.text:
            return Action(name, {"section": section}, at=index)
    return Refusal(name, f"no part of the request opens the {section} settings")


_DEVICE_NOUN = (r"(?:password|passcode|speed|signal|strength|connection|network|name|level|"
                r"settings?|card|driver|device|adapter|router|output|input|source)\b")
# Muting is the device, wherever the request says it.
_MUTE = re.compile(r"\b(?:un)?mut(?:e|ed|es|ing)\b")


def _without_other_names(item: str, text: str) -> str:
    """The text with every other item's names, and item groups, taken out
    (not section names: "the top bar" says indicator; review R12 round 10)."""
    names = [n for other in ITEMS if other != item for n in _names(ITEM_NAMES, other)]
    names += list(ITEM_GROUPS)
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", " ", text)
    return text


def _both_ways(parts: tuple, names) -> bool:
    """Whether the request says a thing on in one place and off in another,
    counting what each clause reads as and every on/off word it says."""
    said = {p.on for p in parts if names(p.text) and p.on is not None}
    # Every clause's own on/off word counts, not only a bare one's (review R12
    # round 8: "clock off, show the clock").
    said |= {_particle(p.text) for p in parts if names(p.text)}
    return {True, False} <= said or "both" in said


def _names_kind(table: dict, keys: tuple, clause: str) -> bool:
    return any(_mentions(table, key, clause) for key in keys)


def _names_something(clause: str) -> bool:
    """The clause is about some stat, item, app, game or section."""
    return any(_mentions(table, key, clause)
               for table, keys in ((STAT_NAMES, STATS), (ITEM_NAMES, ITEMS),
                                   (LAUNCH_NAMES, LAUNCHABLE), (SECTION_NAMES, SECTIONS))
               for key in keys)


def _key(action: Action) -> tuple:
    """What an action sets: two admitted actions setting it differently clash."""
    args = action.args
    return {"show": ("show", args.get("item")), "pane_stat": ("pane_stat", args.get("stat")),
            "game": ("game", args.get("game"))}.get(action.kind, (action.kind, id(action)))


MAX_REQUEST = 2000        # characters; review R15, KN-R15-08


def interpret(request: str, calls: list) -> list[Action | Refusal]:
    """Turn the engine's calls into admitted actions or named refusals."""
    reading = (_read(request, widen=True) if len(str(request)) <= MAX_REQUEST
               else Reading(refusal="the request is too long"))
    results: list[Action | Refusal] = []
    seen = set()
    for call in calls if isinstance(calls, list) else []:
        name = call.get("name") if isinstance(call, dict) else None
        args = call.get("arguments") if isinstance(call, dict) else None
        if name not in TOOL_NAMES or not isinstance(args, dict):
            results.append(Refusal(str(name), "not a kilix-needle apps action"))
            continue
        if reading.refusal:
            result = Refusal(name, reading.refusal)
        elif reading.way and name != "settings":
            result = Refusal(name, "the request asks where something is; only a settings "
                                   "screen opens for that")
        else:
            result = _admit(name, args, reading)
        if isinstance(result, Action):
            key = (result.kind, tuple(sorted(result.args.items())))
            if key in seen:
                continue            # the same action twice is one action
            seen.add(key)
        results.append(result)
    admitted = [r for r in results if isinstance(r, Action)]
    clashing = {_key(a) for a in admitted for b in admitted if a is not b and _key(a) == _key(b)}
    # Whatever order the model called them in (review R12 round 2).
    disabled = {a.args["game"] for a in admitted if a.kind == "game" and not a.args["available"]}
    for index, result in enumerate(results):
        if isinstance(result, Action) and _key(result) in clashing:
            results[index] = Refusal(result.kind, "the request sets this two ways")
        elif isinstance(result, Action) and result.kind == "launch" \
                and result.args["app"] in disabled:
            # "disable doom and open it": the tab would only say doom is disabled.
            results[index] = Refusal("launch", f"the request makes {result.args['app']} "
                                               "unavailable")
    return results


# A yes given in advance (--yes, MCP confirm_risky) covers only an action the
# request states outright, in a canonical form of the action itself. Each form
# is (pattern, value), where value is the on/off or mode the form says, or
# None when the pattern's "mode" group says it.
_P_HEAD = r"^(?:(?:please|pls|can you|could you|would you)\s+)?"
_P_TAIL = r"(?:\s+(?:please|pls|now|for me|thanks|thank you))?$"
_PLAIN_FORMS = {
    "launch": [(r"(?:open|launch|start|play|run) (?:the )?(?P<name>.+?)", None)],
    "show": [(r"(?:show|unhide) (?:the )?(?P<name>.+?)", True), (r"hide (?:the )?(?P<name>.+?)", False),
             (r"(?:turn|switch) on (?:the )?(?P<name>.+?)", True),
             (r"(?:turn|switch) off (?:the )?(?P<name>.+?)", False),
             (r"(?:turn|switch) (?:the )?(?P<name>.+?) on", True),
             (r"(?:turn|switch) (?:the )?(?P<name>.+?) off", False)],
    "game": [(r"enable (?P<name>.+?)", True), (r"disable (?P<name>.+?)", False),
             (r"make (?P<name>.+?) available", True), (r"make (?P<name>.+?) unavailable", False)],
    "pane_stat": [(r"set (?:the )?pane (?P<name>.+?) to (?P<mode>.+?)", None),
                  (r"show (?:the )?pane (?P<name>.+?) (?P<mode>always|auto)", None),
                  (r"(?:hide|stop showing) (?:the )?pane (?P<name>.+?)", "off"),
                  (r"always show (?:the )?pane (?P<name>.+?)", "always")],
}
_SCREEN_FORM = re.compile(_P_HEAD + r"(?:open|show me|take me to|go to) (?:the )?(?:(?P<name>[\w ]+?) "
                          r"(?:settings|section|page)|settings (?:for|at) (?:the )?(?P<name2>[\w ]+?))"
                          + _P_TAIL)
_PLAIN_TABLE = {"launch": ("app", LAUNCH_NAMES), "show": ("item", ITEM_NAMES),
                "game": ("game", LAUNCH_NAMES), "pane_stat": ("stat", STAT_NAMES)}


def _value(action: Action):
    return {"show": action.args.get("on"), "game": action.args.get("available"),
            "pane_stat": action.args.get("mode")}.get(action.kind)


def _form(kind: str, text: str, value) -> re.Match | None:
    """The canonical form of this kind the text is, saying this value."""
    for pattern, says in _PLAIN_FORMS[kind]:
        match = re.match(_P_HEAD + pattern + _P_TAIL, text)
        if not match:
            continue
        if kind == "pane_stat" and says is None:
            if not _is_name(MODE_NAMES, value, match["mode"]):
                continue
        elif says is not None and says != value:
            continue
        return match
    return None


def _plainly(action: Action, part: Part) -> bool:
    field_name, table = _PLAIN_TABLE[action.kind]
    key, value = action.args[field_name], _value(action)
    if part.bare:
        # "hide the clock and the battery": a bare name after a plain verb clause.
        name = re.match(r"(?:the )?(?P<name>.+)$", part.text)["name"]
        return action.kind != "launch" and _is_name(table, key, name) \
            and _form(action.kind, part.verb, value) is not None
    match = _form(action.kind, part.text, value)
    return bool(match) and _is_name(table, key, match["name"])


def plain(request: str, actions: list) -> str | None:
    """None when the whole request is plain; otherwise why not.

    Plain is a property of the request, not of one clause (review R12 round 2:
    "translate to french, open doom" and "open doom, just kidding" were plain
    because one clause was). Every launch and settings change must be stated
    in a canonical form, and every clause of the request must be accounted
    for: a canonical form of an admitted action, a bare name continuing one,
    a settings screen admitted from it, or courtesy.
    """
    changes = [a for a in actions if isinstance(a, Action) and a.kind in SIDE_EFFECT_KINDS]
    if not changes:
        return None
    reading = _read(request)
    if reading.refusal or reading.way:
        return "it is not an instruction"
    accounted = set()
    for action in changes:
        found = {index for index, part in enumerate(reading.parts) if _plainly(action, part)}
        if not found:
            what = {"launch": "open", "show": "show or hide", "game": "change",
                    "pane_stat": "set"}[action.kind]
            return f"it does not simply say to {what} {next(iter(action.args.values()))}"
        accounted |= found
    # A settings screen accounts for a clause only in its own canonical form
    # (review R12 round 3: "open the voice settings as a dry run, open doom").
    screens = [a.args["section"] for a in actions
               if isinstance(a, Action) and a.kind == "settings"]
    for index, part in enumerate(reading.parts):
        if index in accounted or _COURTESY.fullmatch(part.text):
            continue
        screen = _SCREEN_FORM.fullmatch(part.text)
        if screen and any(_is_name(SECTION_NAMES, section, screen["name"] or screen["name2"])
                          for section in screens):
            continue
        return f"it says more than the actions do: {part.text!r}"
    return None


# ---------------------------------------------------------------------------
# The proposer. The checks above read the whole request themselves and admit a
# call only when that reading says it, so any call they admit is one the
# request supports, whoever proposed it; they hold against a proposer that
# calls anything (reviews R12-R15). Offering every call and keeping the ones a
# reading supports therefore admits no more than an adversarial model could,
# and never misspells a name. Measured on dev, test and held-out v3 and v4 it
# reaches the checks' own ceiling where a tuned Needle 2 does not (2026-09-28).

CANDIDATE_CALLS = tuple(
    [{"name": "launch", "arguments": {"app": app}} for app in LAUNCHABLE]
    + [{"name": "show", "arguments": {"item": item, "on": on}}
       for item in ITEMS for on in (True, False)]
    + [{"name": "pane_stat", "arguments": {"stat": stat, "mode": mode}}
       for stat in STATS for mode in MODES]
    + [{"name": "game", "arguments": {"game": game, "available": available}}
       for game in AVAILABILITY for available in (True, False)]
    + [{"name": "settings", "arguments": {"section": section}} for section in SECTIONS])



def _object_names(call: dict) -> list[str]:
    kind, args = call["name"], call["arguments"]
    if kind == "launch":
        return _names(LAUNCH_NAMES, args["app"])
    if kind == "game":
        return _names(LAUNCH_NAMES, args["game"])
    if kind == "show":
        return _names(ITEM_NAMES, args["item"]) + [
            group for group, members in ITEM_GROUPS.items() if args["item"] in members]
    if kind == "pane_stat":
        return _names(STAT_NAMES, args["stat"])
    return _names(SECTION_NAMES, args["section"])


def _order(reading: Reading, action: Action, call: dict) -> tuple:
    """The clause that admitted the call, then where that clause names its
    object: "open calculator, enable pong, launch pong" runs in that order
    (review KN-R16-01: ordering by first mention launched pong before
    enabling it)."""
    text = reading.parts[action.at].text if 0 <= action.at < len(reading.parts) else ""
    starts = [m.start() for m in (_said(name, text) for name in _object_names(call)) if m]
    return (action.at, min(starts, default=len(text) + 1))


def propose(request: str) -> list[dict]:
    """Every call the request's own reading supports, in the request's order.

    Each is admitted on its own; the checks between calls (a name said both
    ways, launching a game the request makes unavailable) run again in
    interpret, so they still refuse and hold the request. When nothing is
    supported, a call on something the request names is offered so that
    interpret says why it is refused ("the request says when").
    """
    text = _plain_words(request)
    reading = (_read(request, widen=True) if len(str(request)) <= MAX_REQUEST
               else Reading(refusal="the request is too long"))
    supported = []
    if not reading.refusal:
        for call in CANDIDATE_CALLS:
            if reading.way and call["name"] != "settings":
                continue
            action = _admit(call["name"], call["arguments"], reading)
            if isinstance(action, Action):
                supported.append((_order(reading, action, call), call))
    if supported:
        return [call for _key, call in sorted(supported, key=lambda pair: pair[0])]
    named = [call for call in CANDIDATE_CALLS
             if any(_said(name, text) for name in _object_names(call))]
    return named[:1]


class Proposer:
    """The apps job's engine: propose() behind the interface a model has."""

    def start(self) -> None:
        pass

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass

    def complete(self, text: str) -> dict:
        return {"function_calls": propose(text)}
