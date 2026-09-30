"""The agents job: launch coding-agent sessions, wait for them, message them.

The model sees three tools (TOOLS). Every call is untrusted, and the checks
never take the model's word for what the request says. They read the request
themselves (`parse`) and admit the calls only when they are exactly that
reading: the same actions in the same order, each with the same agent,
directory, session, state, place, model, resume id, timeout and payload.

The reading is a small grammar of clauses: launch or resume, wait, tell, and
"when <session> is done, tell it ...". Within a clause an agent and its
directory go together, so names can't be traded between clauses (review
R14). Anything the grammar doesn't take -- a negation, a report, a question,
a status line, "install", "yolo", "close", a leftover word -- means no reading,
and nothing is admitted.

Payloads (a launch's task, a message) are where the checks say they are: they
start at a marker (":", "task:", "tell it to", "to", "and") and run to the end
of the request, or to a separator after which the rest of the request reads
as further clauses ("; when it's done, tell it ..."). The model's payload
must equal that span, case preserved. A payload is data and is never read for
verbs, except that one introduced only by "and"/"to" may not start with a job
verb ("and close the claude session" is not a task), no payload starts with
"/", "!", "#", "@" or "-" (client commands), and a later sentence in a payload
may not start a job verb on an agent.

Owner, 2026-09-27: a request to launch a coding agent is itself the consent,
and sessions launch, wait for and message other sessions. So an admitted
request needs no second yes. See AGENTS-JOB-DESIGN.md in the research notes.

Directories: the eval sets use a fixed fixture workspace (FIXTURE_DIRS) and
admit its keys. Without a table (production) a directory is the words the
request says in a directory position (a path, "here", or a name, optionally
"the ... repo"); the runner resolves them on disk.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import bisect
import re
import unicodedata

from actions import _TYPOGRAPHY, Refusal, normalize

AGENTS = ("claude", "codex", "grok", "qwen-omp")
AGENT_NAMES = {
    "claude": ["claude", "claude code", "cc", "anthropic claude"],
    "codex": ["codex", "codex cli", "openai codex"],
    "grok": ["grok", "grok build", "grok cli"],
    "qwen-omp": ["qwen-omp", "qwen omp", "qwen", "omp", "oh my pi"],
}
PLACES = ("tab", "right", "left", "down", "up")
STATES = ("idle", "waiting")
# The eval sets' fixture workspace; production resolves names on disk.
FIXTURE_DIRS = {
    "kilix": ["kilix", "the kilix repo", "~/gpu_terminal/kilix", "kilix terminal"],
    "kilix-needle": ["kilix-needle", "kilix needle", "needle", "the needle repo"],
    "kilix-content": ["kilix-content", "kilix content", "the catalog", "content", "catalog",
                      "content catalog"],
    "kilix-95": ["kilix-95", "kilix 95", "the desktop repo", "desktop"],
    "plebian-os": ["plebian-os", "plebian os", "the os repo", "plebian", "os"],
    "kilix-ml": ["kilix-ml", "kilix ml", "the ml repo", "ml"],
    "research": ["research", "~/research", "the research dir", "research notes"],
    "here": ["here", "this repo", "this directory", "the current folder", "this folder",
             "the current directory", "this project"],
}

TOOLS = [
    {"name": "agent",
     "description": "Start a coding-agent session in a directory, optionally with a task, "
                    "resuming a previous session, with a model, or as a split",
     "parameters": {"type": "object", "properties": {
         "agent": {"type": "string", "enum": list(AGENTS)},
         "dir": {"type": "string", "description": "the directory, as the request names it"},
         "prompt": {"type": "string", "description": "the task text, exactly as written"},
         "resume": {"type": "string", "description": "the session id or title to resume"},
         "model": {"type": "string", "description": "a model name, as written"},
         "place": {"type": "string", "enum": list(PLACES)}},
         "required": ["agent", "dir"]}},
    {"name": "wait",
     "description": "Wait until a coding session is idle (its turn finished) or waiting "
                    "(it asks something)",
     "parameters": {"type": "object", "properties": {
         "session": {"type": "string",
                     "description": "'it' for the session just launched, else agent@dir"},
         "for": {"type": "string", "enum": list(STATES)},
         "timeout": {"type": "integer", "description": "seconds, when the request says"}},
         "required": ["session", "for"]}},
    {"name": "tell",
     "description": "Send a message to a coding session and submit it",
     "parameters": {"type": "object", "properties": {
         "session": {"type": "string",
                     "description": "'it' for the session just launched, else agent@dir"},
         "text": {"type": "string", "description": "the message, exactly as written"},
         "wait": {"type": "boolean", "description": "wait until it is idle first"}},
         "required": ["session", "text"]}},
]
MAX_REQUEST = 2048                 # a message is at most 1024 bytes; parsing is at worst cubic
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)
ARG_NAMES = {tool["name"]: frozenset(tool["parameters"]["properties"]) for tool in TOOLS}


@dataclass(frozen=True)
class Action:
    kind: str
    args: dict = field(default_factory=dict)

    @property
    def risky(self) -> bool:
        return False            # the request is the consent (owner, 2026-09-27)


def _fold(text: str) -> str:
    return " ".join(normalize(text).casefold().split())


def _plain(text: str) -> str:
    return " ".join(str(text).translate(_TYPOGRAPHY).split())


def _lower(text: str) -> str:
    """Lower case, one character for one, so positions match the original."""
    return "".join(ch.lower() if len(ch.lower()) == 1 else ch for ch in text)


def _alternation(phrases) -> str:
    return "|".join(re.escape(p) for p in sorted(set(phrases), key=len, reverse=True))


def _resolve_name(table: dict, value) -> str | None:
    if not isinstance(value, str):
        return None
    said = _fold(value)
    hits = [key for key in table if said == _fold(key) or said in map(_fold, table[key])]
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------------------
# Words and phrases of the grammar (all matched on lower-cased text).

_END = r"(?![\w/-]|\.\w)"                      # a name ends here: not "kilix.new", "kilix-2"
_AGENT_ALIAS = {_fold(n): key for key, names in AGENT_NAMES.items() for n in [key, *names]}
_AGENT = re.compile(rf"(?:{_alternation(_AGENT_ALIAS)}){_END}")
_HERE = tuple(FIXTURE_DIRS["here"])
_DIR_SUFFIX = ("repo", "repository", "checkout", "folder", "directory", "dir")
_PREAMBLE = re.compile(
    r"(?:(?:hey|hi|hello|ok|okay|so|alright|yo)[,!]? )*"
    r"(?:(?:please|pls|kindly|now|just|quickly|also) )*"
    r"(?P<ask>(?:can|could|would|will) you (?:please |kindly |just )*)?"
    r"(?:(?:i need you to|i want you to|go ahead and|let'?s|please|pls|kindly|just|now) )*")
_TRAILER = re.compile(r"(?:[.!]*,? (?:thanks|thank you|thx|ty|cheers|please|pls))?"
                      r"(?P<mark>[.!?]*) *$")
_SEP = re.compile(r"(?:,? and then |, then |; |,? and |, | then )")
_PAYLOAD_END = re.compile(r"(?:; |, (?:and )?then,? |,? and (?:then,? )?|, | then,? | -+ (?:then,? )?)")
# After ":" a payload also ends before a wait or a "when … is done" clause
# ("…: review the diff, and when it's done tell it to push"), which speak
# about sessions, not to one (KN-R14-45).
_WAIT_OR_WHEN = re.compile(r"(?:wait|block|hold on|hang on|when|once|after|as soon as)\b")
_CLAUSE_LEAD = re.compile(r"(?:(?:and|then|also|please|pls|just|meanwhile|so|now|next|"
                          r"afterwards|after (?:that|this)),? )*")
_SEQUENCE = re.compile(r"(?:; |, (?:and )?then,? | -+ then,? )")
_SUBORDINATE = re.compile(r"\b(?:if|when|whenever|once|unless|until|till|before|after|in case|"
                          r"as soon as|while)\b")
_DET = re.compile(r"(?:(?:a|an|the|another|one more|new|fresh|blank|second|separate|me a|"
                  r"us a|my|our) )*")
_ROLE = re.compile(r" (?P<role>session|instance|agent|reviewer|review|worker|helper|pane|"
                   r"window|tab|one|run)\b")
_PREP = r"(?:(?:over |right |up |down )?(?:in|at|inside|within|under) )"
_LAUNCH = re.compile(r"(?:open(?: up)?|start(?: up)?|launch|spawn|fire up|spin up|bring up|"
                     r"boot(?: up)?|kick off|run|create|put|give me|get me)\b")
_RESUME = re.compile(r"(?:resume|continue|reopen|pick (?:back )?up|bring back|pick)\b")
_WAIT = re.compile(r"(?P<verb>wait|block|hold on|hang on|hold|sit tight|watch|monitor|"
                   r"keep an eye on)\b")
_TELL = re.compile(r"(?P<verb>(?:a )?(?:quick )?(?:message|note) for|tell|ask|message|ping|"
                   r"instruct|send|let|steer|give|say|get)\b")
_NOTE = r"(?: this| this note| this message| this input| the message| a message| a note|"  \
        r" a quick message| a quick note)"
_TITLED = re.compile(r" (?:titled|called|named) (?P<t>[\w.-]+(?: [\w.-]+){0,5}?)"
                     r"(?= (?:in|at|inside|within|here|using|with|on|split|to)\b|,|:|[.!]*$)")
_THEN = re.compile(r"(?:then,? |and then,? )?(?:(?:immediately|also|now|next|afterwards) )?")
_COND = re.compile(r"(?:when|once|after|as soon as) ")
_PLACE = [
    (re.compile(r",? (?:in a |as a )?(?:new )?split(?: pane)? (?:to the |on the )?"
                r"(right|left|down|up|below|above)\b(?: of me| side)?"), None),
    (re.compile(r",? (?:in a (?:new )?(?:split|pane) |as a split )?(?:to|on) (?:the|my) "
                r"(right|left)(?: side)?(?: of me)?\b"), None),
    (re.compile(r",? (?:in a (?:new )?(?:split|pane) |as a split )?(?:to the |on the )?"
                r"(right|left) of me\b"), None),
    (re.compile(r",? (?:in a (?:new )?(?:split|pane) |as a split )?"
                r"(below|beneath|under|underneath|above) me\b"), None),
    (re.compile(r",? (?:in a (?:new )?(?:split|pane) )?on top(?: of (?:me|this pane))?\b"), "up"),
    (re.compile(r",? (below|beneath|under|underneath|above) this pane\b"), None),
    (re.compile(r",? (?:to the )?(right|left) of this pane\b"), None),
    (re.compile(r",? (below|beneath|underneath)(?= (?:in|at|inside|within)\b|,|:|$)"), None),
    (re.compile(r",? (?:(?:in|as) (?:a )?(?:new |separate |its own )?|(?:a )?new )tab\b"), "tab"),
]
_SIDE = {"right": "right", "left": "left", "down": "down", "up": "up", "below": "down",
         "beneath": "down", "under": "down", "underneath": "down", "above": "up"}
_MODEL_TOKEN = r"(?P<m>[a-z0-9][a-z0-9._:-]*[a-z0-9]|[a-z0-9])"
_MODEL = [re.compile(rf",? (?:using |with |on |via )?(?:the )?model:? {_MODEL_TOKEN}{_END}"),
          re.compile(rf",? (?:using|with|on|via) {_MODEL_TOKEN}{_END}")]
_MODEL_WORDS = frozenset("opus sonnet haiku fable mythos".split())
# Without the word "model", a model is named by its family ("with gpt-6",
# "on opus"), never any token with a digit ("on pr-1234", "using python3").
_MODEL_FAMILY = re.compile(
    r"gpt-?\d[\w.-]*|o[1-9](?:-(?:mini|pro|high|low|preview))?"
    r"|(?:claude-)?(?:opus|sonnet|haiku|fable|mythos)(?:-?\d[\w.-]*)?"
    r"|(?:grok|gemini|llama|kimi|mistral)-?k?\d[\w.-]*|qwen-?\d[\w.-]*|deepseek-?[a-z]*-?\d[\w.-]*"
    r"|codex-?\d[\w.-]*")
_STOP = frozenset("""a an the my our your this that it its last latest previous prior recent
old older other whatever same one most earlier in at inside within under here with using on to
and split for of""".split())
_ID = r"(?P<id>[a-z0-9][a-z0-9._-]*[a-z0-9])"
_HEX = re.compile(r" (?P<id>(?=[a-f]*\d)[0-9a-f]{6,}(?:-[0-9a-f]+)*)(?![\w.-])")
_TITLE = re.compile(r" the (?P<t>(?:[a-z0-9][\w.-]* ){0,4}[a-z0-9][\w.-]*) session\b")
_LEAD_ID = re.compile(r" (?:the )?(?:session )?(?P<id>(?=[a-f]*\d)[0-9a-f]{6,}(?:-[0-9a-f]+)*)"
                      r"(?: session)?"
                      r"(?![\w.-])")
_IDLE_STATE = re.compile(
    r"(?:'s| is| goes| went| gets| becomes| has| to be| to go| to get| to become)"
    r" (?:done|finished|idle|complete|completed|through|ready)(?: with (?:its|the) "
    r"(?:turn|task|work|job|review|run))?\b"
    r"|(?: to)? (?:finish|finishes|complete|completes)(?: (?:its|the) "
    r"(?:turn|task|work|job|review|run)| up)?\b"
    r"|(?: to)? (?:go|goes|reach|reaches) idle\b")
_WAITING_STATE = re.compile(
    r"(?: to| is)?(?: stop and)? (?:ask|asks|asking)(?: me)?(?: for)? (?:approval|input|"
    r"a question|questions|something|anything)\b"
    r"|(?: to| is)? (?:need|needs|needing) (?:approval|input|me|my (?:approval|input))\b"
    r"|(?: is| to be| gets| goes| becomes) (?:waiting|blocked)(?: (?:on|for) (?:approval|input|me|"
    r"us|my (?:approval|input)))?\b")
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
            "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50,
            "sixty": 60, "ninety": 90, "a": 1, "an": 1}
_UNIT = {"second": 1, "seconds": 1, "sec": 1, "secs": 1, "minute": 60, "minutes": 60,
         "min": 60, "mins": 60, "hour": 3600, "hours": 3600}
_AMOUNT = (r"(?P<half>half an hour)|(?P<n>\d+(?![.,]\d)|" + _alternation(_NUMBERS) +
           r") (?P<u>" + _alternation(_UNIT) + r")\b")
_TIMEOUT = [re.compile(r",? (?:but )?(?:give up after|(?:for )?up to|(?:for )?at most|a maximum of|"
                       r"no (?:longer|more) than|with a timeout of|timeout(?: of)?|within|"
                       r"max(?:imum)?(?: of)?) (?:" + _AMOUNT + ")"),
            re.compile(r",? (?:" + _AMOUNT + r") (?:max|maximum|at most|tops)\b"),
            re.compile(r",? with an? (?:" + _AMOUNT + r") (?:time )?(?:limit|timeout|cap)\b")]
_FRONT_TIMEOUT = re.compile(r"(?:for )?(?:at most|up to|no (?:longer|more) than) (?:" + _AMOUNT +
                            r"),? ")
_BARE_TIMEOUT = re.compile(r" (?:" + _AMOUNT + r")(?= (?:for|until|till)\b)")
_GIVE_TIMEOUT = re.compile(r" (?:up to|at most) (?:" + _AMOUNT + r")")
# A payload after a weak marker ("and", "to") may not start with these: "open
# codex in kilix and close the claude session" is not a task for codex.
_CONTROL_HEAD = frozenset("""close kill quit exit terminate stop end cancel pause hold halt abort
don't dont do not never no""".split())
_JOB_HEAD = _CONTROL_HEAD | frozenset("""open start launch spawn fire spin bring boot kick put resume
continue reopen pick wait block hang tell ask message ping instruct send let notify steer give
install uninstall reinstall update upgrade download skip bypass""".split())
# A payload after "and" may not start with a job verb ("and close the claude
# session"); after "to", not with a verb that controls a session ("get codex
# to stop"). Neither may touch permissions anywhere.
_WEAK_HEAD = {"and": _JOB_HEAD, "to": _CONTROL_HEAD | frozenset(
    "sleep bed completion rest finish finished idle wait it done work".split())}
_QUOTES_AND_BLANKS = " \t\"'`\u2018\u2019\u201c\u201d\u00ab\u00bb\u2800\u3000"
_EXIT_WORDS = frozenset("exit quit q bye logout :q :wq :q! :x".split())
# Whatever a payload says, it is refused when a part of it takes the request
# back, reports someone else, asks whether to go ahead, says not to do the
# action now, or closes/installs an agent: those speak to this tool, not to
# the session (review R14 round 2, KN-R14-29).
_TAKE_BACK = re.compile(r"\b(?:cancel (?:that|it|this)|never ?mind|nvm|scratch that|jk|just kidding|"
                        r"kidding|forget (?:it|that)|no wait|wait,? no|actually,? (?:no|don'?t|cancel)|"
                        r"or (?:actually,? )?(?:don'?t|not))\b")
_REPORTED = re.compile(r"\b(?:(?:my |the |our )?(?:boss|friend|colleague|manager|someone|somebody|"
                       r"he|she|they) (?:said|says|told|wants|asked)|said so|according to)\b")
_ASKING = re.compile(r"\b(?:should (?:i|we)\b|or should\b|or not\b|is (?:that|it|this) "
                     r"(?:ok|okay|safe|fine|allowed)\b)")
_NOT_NOW = re.compile(r"\b(?:don'?t|do not|never|no need to)\s+(?:open|start|launch|run|send|do) "
                      r"(?:it|that|this|them)\b|\bnot (?:yet|now)\b")
_COURTESY_TAIL = re.compile(r"(?:[,.!]? (?:thanks|thank you|thx|ty|cheers|please|pls))?[.!]*$")
_LEAD = frozenset("then and also now please pls so next afterwards after that just".split())
_CONTROL_AGENT = re.compile(
    r"\b(?:close|kill|quit|exit|stop|terminate|end|install|uninstall|reinstall|update|upgrade|"
    r"download)\b(?:\W+[\w'-]+){0,3}?\W+(?:" + _alternation(_AGENT_ALIAS) + r")" + _END +
    r"|\b(?:close|kill|quit|exit|terminate)\b(?:\W+(?:the|this|that|your|its|my|all))?\W+"
    r"(?:session|sessions|pane|panes|tab|tabs|window)\b")
# Words after "in"/"at" that are not directories ("at once", "in the background").
_NOT_A_DIR = frozenset("""once background foreground tmux screen parallel progress charge
supervision general time order place case turn full detail private public secret silence
peace sequence batch bulk total fact short advance addition particular front back middle
charge person mind review debug verbose quiet silent sandbox docker container meantime
meanwhile future past end beginning morning evening afternoon night event moment future
hurry rush way sense light terms touch line person practice theory principle""".split())
_PERMISSION = re.compile(r"yolo|danger|bypass|approv|permission|sandbox|unsafe|full-?auto|"
                         r"^auto$|trust")
_REFUSE = [
    (re.compile(r"\b(?:not|never|don'?t|do not|no|nope|without|cannot|can'?t|won'?t|\w+n't)\b"),
     "the request says not to"),
    (re.compile(r"\b(?:said|says|told|tells|wrote|writes|asked me|according to|someone|"
                r"somebody|anyone|friend|boss|colleague)\b"),
     "the request reports someone else's words"),
    (re.compile(r"\b(?:cancel|never ?mind|nvm|scratch that|actually|undo|jk|kidding)\b"),
     "the request takes something back"),
    (re.compile(r"\b(?:re ?install\w*|install\w*|uninstall\w*|updat\w*|upgrad\w*|download\w*|"
                r"set up|setup)\b"),
     "installing or updating an agent is not done here"),
    (re.compile(r"\b(?:yolo|dangerous\w*|bypass\w*|skip\w* (?:the )?(?:approvals?|permissions?|"
                r"prompts?)|auto.?approve\w*|always.?approve|permission\w*|sandbox\w*)\b"),
     "permissions follow Kilix's coding-yolo setting and are not set by a request"),
    (re.compile(r"\b(?:kill|close|quit|exit|terminate|stop|end)\b"),
     "closing or stopping sessions is not done here"),
    (re.compile(r"\?"), "the request is a question"),
]


# ---------------------------------------------------------------------------
# The reading.

@dataclass(frozen=True)
class Payload:
    """A span of the request, case preserved; the model may leave off one
    closing full stop or the quotes around it."""
    text: str

    def forms(self) -> dict:
        """The spans the model may give, by their plain spelling (typography
        made plain), each mapped to the request's own characters."""
        text = self.text.strip()
        forms = {text}
        plain = _plain(text)
        if len(plain) > 1 and plain[0] == plain[-1] and plain[0] in "\"'`":
            forms.add(text[1:-1].strip())
        forms |= {f[:-1].rstrip() for f in list(forms) if f.endswith((".", "!"))}
        return {_plain(f): f for f in forms if f}


@dataclass(frozen=True)
class Want:
    kind: str
    args: tuple                           # sorted (key, value) pairs

    def get(self, key, default=None):
        return dict(self.args).get(key, default)


def _want(kind, **args) -> Want:
    return Want(kind, tuple(sorted((k, v) for k, v in args.items() if v is not None)))


def _dir_forms(table: dict) -> dict:
    """Every way the request may say each directory: its names, with "the"
    in front and "repo" (or kin) after."""
    variants = []
    for key, names in table.items():
        for name in {_fold(key), *map(_fold, names)}:
            forms = [name]
            if not name.startswith(("the ", "this ", "~", "/")):
                forms.append("the " + name)
            for form in list(forms):
                if not form.endswith(_DIR_SUFFIX):
                    forms += [f"{form} {s}" for s in _DIR_SUFFIX]
            variants += [(f, key) for f in forms]
    return dict(variants)


class _Reader:
    def __init__(self, request: str, dirs: dict | None):
        self.org = request              # spans are cut from the request's own characters
        self.low = _lower(request.translate(_TYPOGRAPHY))       # one for one: same positions
        self.dirs = dirs
        self.n = len(request)
        self.polite = False
        self.dead = set()               # (pos, state) where the rest has no reading
        # Where, once for the whole request, a session clause (a wait, a
        # condition, a message or launch to a session) or a timed wait starts
        # at a word, and where a wait or condition on a session starts: a
        # payload looks its own range up here instead of re-scanning
        # (review R14 round 7, KN-R14-70).
        starts = [m.start() for m in re.finditer(r"\b\w", self.low)]
        self.clause_at = [p for p in starts if _SESSION_CLAUSE.match(self.low, p)
                          or _TIMED_WAIT.match(self.low, p)]
        self.wait_at = [p for p in starts if _WAIT_COND.match(self.low, p)]
        self.dir_key = _dir_forms(dirs if dirs is not None else {"here": list(_HERE)})
        self.dir_re = re.compile(rf"(?:{_alternation(self.dir_key)}){_END}")

    # -- names ------------------------------------------------------------
    def agent(self, pos):
        m = _AGENT.match(self.low, pos)
        return (_AGENT_ALIAS[m[0]], m.end()) if m else None

    def directory(self, pos, bare_ok=False):
        """A directory phrase at pos (with its leading space): the directory's
        value and the end. A preposition comes first unless it is "here"."""
        if not self.low.startswith(" ", pos):
            return None
        m = re.compile(r" " + _PREP).match(self.low, pos)
        return self.place_named(m.end() if m else pos + 1, bool(m), bare_ok)

    def place_named(self, start, prepped, bare_ok=False):
        """The directory named at start, after its preposition (or bare "here")."""
        hit = self.dir_re.match(self.low, start)
        if hit and (prepped or bare_ok and self.dir_key[hit[0]] == "here"):
            return self.dir_key[hit[0]], hit.end()
        if self.dirs is not None or not prepped:
            return None
        path = re.compile(r"~?/[^\s,;:'\"`]*").match(self.low, start)
        if path:
            end = path.end()
            while end > start and self.low[end - 1] == ".":
                end -= 1
            if end > start + 1 and self.low[start:end] not in ("~/",):
                return self.org[start:end], end
            return None
        name = re.compile(r"(?:the )?(?P<n>[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)"
                          rf"(?: (?:{'|'.join(_DIR_SUFFIX)}))?{_END}").match(self.low, start)
        if name and name["n"] not in _STOP and name["n"] not in _AGENT_ALIAS \
                and name["n"] not in _NOT_A_DIR \
                and name["n"] not in _MODEL_WORDS and name["n"] not in _SIDE \
                and not _PERMISSION.search(name["n"]):
            return self.org[start:name.end()], name.end()
        return None

    def session(self, pos, acts):
        """A session phrase at pos (after a space): 'it' or agent@dir."""
        if re.compile(r"it\b(?!')|it(?='s )").match(self.low, pos):
            named = _last_session(acts)
            return (named, pos + 2) if named else None
        det = re.compile(r"(?:the |that |this )?(?:same )?").match(self.low, pos)
        got = self.agent(det.end())
        if not got:
            return None
        agent, pos = got
        role = _ROLE.match(self.low, pos)
        if role and role["role"] not in ("tab", "pane", "window"):
            pos = role.end()
        where = self.directory(pos, bare_ok=True)
        if not where:
            return None
        return f"{agent}@{where[0]}", where[1]

    # -- clauses ----------------------------------------------------------
    def read(self):
        """The actions the request states, or None."""
        pre = _PREAMBLE.match(self.low)
        self.polite = bool(pre["ask"])
        return self.chain(pre.end(), ())

    def after(self, pos, acts):
        """What follows a finished clause: the end, or a separator and more."""
        if self.at_end(pos):
            return acts
        sep = _SEP.match(self.low, pos)
        return self.chain(sep.end(), acts) if sep else None

    def at_end(self, pos):
        tail = _TRAILER.match(self.low, pos)
        return bool(tail) and ("?" not in tail["mark"] or self.polite)

    def chain(self, pos, acts):
        # What the rest can mean depends only on where it starts, whether
        # "it" has exactly one launch to name, and whether a verbless launch
        # may follow; a failure there is remembered, so a long request can't
        # make the search explode.
        state = (pos, _last_session(acts), not acts or acts[-1].kind == "agent")
        if state in self.dead:
            return None
        if acts:
            pos = _THEN.match(self.low, pos).end()
        for clause in (self.launch, self.wait, self.tell, self.cond):
            got = clause(pos, acts)
            if got:
                return got
        self.dead.add(state)
        return None

    def payload(self, start, acts, make, weak=None, strict=False, tell=False):
        """A payload from start to the first separator after which the rest
        reads as clauses, else to the end; make(payload) is its action.
        After ":" (strict) only "; " or ", then" end it; and a payload that
        says if/when/unless/before/after is never cut, since what follows may
        be its condition's ("tell claude here: if tests fail, open codex")."""
        if start >= self.n or self.low[start] == " ":
            return None
        for sep in _PAYLOAD_END.finditer(self.low, start):
            text = Payload(self.org[start:sep.start()])
            if strict and not _SEQUENCE.fullmatch(sep[0]) and not _WAIT_OR_WHEN.match(
                    self.low, sep.end()) or not _payload_ok(text, weak, tell and not strict):
                continue
            if _SUBORDINATE.search(_lower(text.text)):
                break
            # What follows will run; a wait or condition anywhere inside the
            # part before it ("(wait for it to finish); then tell it …") would
            # be lost while the next clause runs at once (KN-R14-65).
            if _within(self.clause_at, start, sep.start()):
                continue
            rest = self.chain(sep.end(), acts + (make(text),))
            if rest:
                return rest
        # A closing ", thanks" is said to this tool, not part of the payload.
        end = _COURTESY_TAIL.search(self.low, start).start()
        text = Payload(self.org[start:end].rstrip())
        # Nothing runs after a payload that reaches the end, but a wait or a
        # condition on a session anywhere in it would still be lost: refuse
        # (KN-R14-71).
        if _within(self.wait_at, start, end):
            return None
        return acts + (make(text),) if _payload_ok(text, weak, tell and not strict) else None

    def launch(self, pos, acts):
        front = None
        lead = re.compile(r"(?:over )?(?:in|at|inside|within) ").match(self.low, pos)
        if lead:
            # "In the ml repo, open codex ..."
            got = self.place_named(lead.end(), True)
            if not got or not self.low.startswith(", ", got[1]):
                return None
            front, pos = got[0], got[1] + 2
        verb = _LAUNCH.match(self.low, pos) or _RESUME.match(self.low, pos)
        if front and not verb:
            return None
        resuming = bool(verb) and bool(_RESUME.match(self.low, pos))
        found = {"dir": front} if front else {}
        if verb:
            pos = verb.end()
            if resuming:
                # "continue the flake triage session ...", "resume 46bf029ad1 with omp ..."
                t = _TITLE.match(self.low, pos)
                if t and not _STOP & set(t["t"].split()) and not _AGENT_ALIAS.keys() & set(
                        t["t"].split()):
                    found["resume"], pos = self.org[t.start("t"):t.end("t")], t.end()
                else:
                    ident = _LEAD_ID.match(self.low, pos)
                    if ident:
                        found["resume"], pos = self.org[ident.start("id"):ident.end("id")], \
                            ident.end()
                if verb[0] == "pick":
                    back = re.compile(r" (?:back )?up\b").match(self.low, pos)
                    if "resume" not in found or not back:
                        return None
                    pos = back.end()
            if not self.low.startswith(" ", pos):
                return None
            pos += 1
        elif acts and acts[-1].kind != "agent" or not acts and pos != 0:
            return None             # terse "codex in kilix" opens a request or follows a launch
        det = _DET.match(self.low, pos)
        if not resuming and re.search(r"\b(?:the|my|our|this|that)\b", det[0]):
            return None             # "bring up the codex session": one that exists already
        got = self.agent(det.end())
        if got:
            agent, pos = got
        elif resuming and "resume" in found:
            agent, pos = None, pos - 1          # "... with codex" comes among the modifiers
        else:
            return None
        role = _ROLE.match(self.low, pos) if agent else None
        if role:
            pos = role.end()
            titled = _TITLED.match(self.low, pos)
            if resuming and "resume" not in found and titled and not _STOP & set(
                    titled["t"].split()):
                found["resume"], pos = self.org[titled.start("t"):titled.end("t")], titled.end()
            elif resuming and "resume" not in found and role["role"] in ("session", "run"):
                ident = re.compile(rf" (?:id )?{_ID}(?![\w.-])").match(self.low, pos)
                if ident and ident["id"] not in _STOP:
                    found["resume"], pos = self.org[ident.start("id"):ident.end("id")], ident.end()
        while True:
            for kind in ("agent", "dir", "place", "model", "resume"):
                if kind in found or kind == "resume" and not resuming or kind == "agent" and agent:
                    continue
                got = self.modifier(kind, pos)
                if got:
                    found[kind], pos = got
                    break
            else:
                break
        agent = agent or found.pop("agent", None)
        if not agent or "dir" not in found or resuming != ("resume" in found):
            return None
        if verb and verb[0] == "put" and "place" not in found:
            return None             # "put codex in kilix on hold / to sleep"
        if _PERMISSION.search(_lower(found.get("resume") or "")):
            return None
        terse = not verb and not acts
        if found.get("resume") and _STOP & set(_fold(found["resume"]).split()):
            return None

        def make(prompt=None):
            return _want("agent", agent=agent, dir=found["dir"], prompt=prompt,
                         resume=found.get("resume"), model=found.get("model"),
                         place=None if found.get("place") in (None, "tab") else found["place"])
        # A task: strong markers, then "and tell it to", then more clauses, then weak.
        strong = re.compile(r"(?::|,? -|,? (?:with |on )?(?:the |this |a )?(?P<task>task|prompt)[:,]?) "
                            ).match(self.low, pos)
        if terse and not (strong and (strong["task"] or "model" in found or "place" in found)):
            # A bare "codex in kilix: done" is a status line or an address,
            # not a launch; a terse launch takes only "task:".
            return self.after(pos, acts + (make(),)) if not strong else None
        if strong:
            return self.payload(strong.end(), acts, make, strict=True)
        told = re.compile(r",? and (?:tell it to|(?:tell|message|ping|send) it:|ask it to|have it|"
                          r"get it to) "
                          ).match(self.low, pos)
        if told:
            # An explicit task for the new session: data, like after ":".
            return self.payload(told.end(), acts, make, weak="to")
        plain = self.after(pos, acts + (make(),))
        if plain:
            return plain
        weak = re.compile(r",? (?P<w>and|to) ").match(self.low, pos)
        if weak:
            return self.payload(weak.end(), acts, make, weak=weak["w"])
        return None

    def modifier(self, kind, pos):
        if kind == "agent":
            m = re.compile(r",? (?:with|using|via) (?:the )?").match(self.low, pos)
            got = m and self.agent(m.end())
            if got:
                role = _ROLE.match(self.low, got[1])
                return got[0], role.end() if role and role["role"] in ("session", "agent") \
                    else got[1]
            return None
        if kind == "dir":
            return self.directory(pos, bare_ok=True)
        if kind == "place":
            for pattern, fixed in _PLACE:
                m = pattern.match(self.low, pos)
                if m:
                    return fixed or _SIDE[m[1]], m.end()
            return None
        if kind == "model":
            for index, pattern in enumerate(_MODEL):
                m = pattern.match(self.low, pos)
                if m and m["m"] not in _AGENT_ALIAS and not _PERMISSION.search(m["m"]) and (
                        index == 0 or _MODEL_FAMILY.fullmatch(m["m"])):
                    return self.org[m.start("m"):m.end("m")], m.end()
            return None
        # resume: "session <id>" or a bare hex id
        m = re.compile(rf",? session(?: id)? {_ID}(?![\w.-])").match(self.low, pos) \
            or _HEX.match(self.low, pos)
        if m and m["id"] not in _STOP:
            return self.org[m.start("id"):m.end("id")], m.end()
        return None

    def timeout(self, pos):
        for pattern in _TIMEOUT:
            m = pattern.match(self.low, pos)
            if m:
                seconds = _seconds(m)
                return (seconds, m.end()) if seconds else None
        return None

    def wait(self, pos, acts):
        limit = None
        front = _FRONT_TIMEOUT.match(self.low, pos)          # "For at most 45 seconds, wait ..."
        if front:
            limit, pos = _seconds(front), front.end()
            if not limit:
                return None
        give = re.compile(r"give ").match(self.low, pos)
        if give and limit is None:
            # "Give the claude session in the os repo up to five minutes to become idle"
            got = self.session(give.end(), acts)
            span = got and _GIVE_TIMEOUT.match(self.low, got[1])
            if not span or not _seconds(span):
                return None
            return self.state(span.end(), acts, got[0], _seconds(span))
        notify = re.compile(r"(?:notify me|let me know|tell me|ping me|alert me) "
                            r"(?:when|once|as soon as) ").match(self.low, pos)
        if notify and limit is None:
            # "let me know when claude in kilix-needle finishes": a wait
            got = self.session(notify.end(), acts)
            return got and self.state(got[1], acts, got[0], None)
        verb = _WAIT.match(self.low, pos)
        if not verb:
            return None
        pos = verb.end()
        if limit is None:
            early = self.timeout(pos) or (lambda m: m and _seconds(m) and (_seconds(m), m.end()))(
                _BARE_TIMEOUT.match(self.low, pos))
            if early:
                limit, pos = early
        if verb["verb"] in ("watch", "monitor", "keep an eye on"):
            # "Watch grok in the ml repo until it is waiting for approval"
            got = self.low.startswith(" ", pos) and self.session(pos + 1, acts)
            until = got and re.compile(r" until it(?=\b|')").match(self.low, got[1])
            if not until:
                return None
            return self.state(until.end(), acts, got[0], limit)
        until = re.compile(r" (?:until|till|for|on) ").match(self.low, pos)
        if not until:
            return None
        got = self.session(until.end(), acts)
        if not got:
            return None
        again = re.compile(r" (?:until|till) it(?=\b|')").match(self.low, got[1])
        return self.state(again.end() if again else got[1], acts, got[0], limit)

    def state(self, pos, acts, session, limit):
        idle, waiting = _IDLE_STATE.match(self.low, pos), _WAITING_STATE.match(self.low, pos)
        if bool(idle) == bool(waiting):
            return None
        state, pos = ("idle", idle.end()) if idle else ("waiting", waiting.end())
        if limit is None:
            late = self.timeout(pos)
            if late:
                limit, pos = late
        return self.after(pos, acts + (_want("wait", session=session, **{"for": state},
                                             timeout=limit),))

    def target(self, pos, acts, cond):
        """The session a message goes to. After "when <session> is done", only
        that session ("it", or its name again)."""
        if cond:
            it = re.compile(r"it\b").match(self.low, pos)
            got = (cond, it.end()) if it else self.session(pos, acts)
            return got if got and got[0] == cond else None
        return self.session(pos, acts)

    def tell(self, pos, acts, cond=None):
        verb = _TELL.match(self.low, pos)
        if not verb or not self.low.startswith((" ", ":"), verb.end()):
            return None
        kind, pos = verb["verb"], verb.end()
        marker = None
        if kind in ("send", "say"):
            to = re.compile(r" (?:this|this message|this note|the following|a message|a note|"
                            r"another message|another note) to ").match(self.low, pos)
            if kind == "say" and not to:
                return None
            bare = cond and re.compile(r"(?: it)?(?: this| this note| this message)?: "
                                       ).match(self.low, pos)
            if to:
                got = self.target(to.end(), acts, cond)
                marker = got and re.compile(r": ").match(self.low, got[1])
            elif bare:
                got, marker = (cond, bare.end()), bare
            else:
                got = self.target(pos + 1, acts, cond)
                marker = got and re.compile(_NOTE + r"?: ").match(self.low, got[1])
        else:
            got = self.target(pos + 1, acts, cond)
            if got and kind == "let":
                marker = re.compile(r" know(?: that| this)?:? ").match(self.low, got[1])
            elif got and kind == "give":
                marker = re.compile(_NOTE + r": ").match(self.low, got[1])
            elif got and kind == "get":
                marker = re.compile(r" to ").match(self.low, got[1])      # "get <session> to ..."
            elif got and kind.endswith(" for"):
                marker = re.compile(r": ").match(self.low, got[1])
            elif got and kind == "steer":
                marker = re.compile(r"(?: with this| with)?: ").match(self.low, got[1])
            elif got:
                marker = re.compile(r"(?::|,? saying:|,? -| with this:| this:) "
                                    r"|(?: to| that|,? saying) |(?= not to )|,? (?=[\"\u201c])"
                                    ).match(self.low, got[1])
                if marker and marker.end() == got[1]:
                    marker = re.compile(r" ").match(self.low, got[1])    # "... not to change X"
        if not got or not marker:
            return None
        session = got[0]
        strict = ":" in marker[0] or " -" in marker[0] or self.low[marker.end():marker.end() + 1] in "\"\u201c" or kind in ("send", "say", "steer", "give") \
            or kind.endswith(" for")
        return self.payload(marker.end(), acts, lambda text: _want(
            "tell", session=session, text=text, wait=True if cond else None), strict=strict,
            tell=True)

    def cond(self, pos, acts):
        head = _COND.match(self.low, pos)
        if not head:
            return None
        got = self.session(head.end(), acts)
        if not got:
            return None
        session, pos = got
        idle = _IDLE_STATE.match(self.low, pos)
        if not idle:
            return None
        sep = re.compile(r",? (?:then )?").match(self.low, idle.end())
        return self.tell(sep.end(), acts, cond=session)


# The heads of this grammar's own clauses, as they would start a later part
# of a payload: a wait (every wait verb), a condition on a turn ending, a
# message or launch to a session, "give it N minutes", "let me know when".
# Built from the grammar's verb tables so a new verb can't be missed
# (review R14 round 5, KN-R14-61).
_SESSION_WORD = (r"(?:it|its|it's|them|they|session|the (?:reviewer|worker|helper|agent|instance|"
                 r"new one|other one|tab|pane|new tab|new pane)|" + _alternation(_AGENT_ALIAS) + r")")
# Wait verbs, the grammar's own and the ones it doesn't read ("pause until",
# "stand by", "hang tight"): in a payload they still mean a wait.
_ANY_WAIT = (r"(?:" + _WAIT.pattern.replace("(?P<verb>", "(?:") +
             r"|pause|stand by|hang tight|sleep|poll|check back|check in)")
_SESSION_CLAUSE = re.compile(
    _ANY_WAIT + r"\b[^,;.()]{0,400}?\b(?:" + _SESSION_WORD[3:-1] + r")\b"
    r"|" + _ANY_WAIT + r"\b[^,;.()]{0,400}?\b(?:until|till) (?:(?:the|its) (?:review|run|job|turn|task|session|work) )?"
    r"(?:is |it's |gets |has )?(?:done|finished|complete|completed|idle|over|through)\b"
    r"|(?:when|once|after|as soon as|by the time) (?:it|it's|its|they|that|that's|this|"
    r"(?:the )?\w+ (?:session|review|run)|" + _alternation(_AGENT_ALIAS) + r")\b[^,;.()]{0,400}?\b"
    r"(?:done|finish\w*|complete\w*|idle|ready|through|asks?|waiting|blocked|needs?)\b"
    r"|(?:sit tight|hang on|hold on|hold|wait|block|stand by|hang tight)(?=\s*(?:[,;.!)]|$))"
    r"|give (?:it|them|" + _alternation(_AGENT_ALIAS) + r"|the \w+ session) (?:\w+ )?"
    r"(?:seconds?|minutes?|mins?|hours?|a (?:bit|while|moment|minute|sec\w*))\b"
    r"|(?:" + _TELL.pattern.replace("(?P<verb>", "(?:") + r") (?:the |that |this )?" + _SESSION_WORD
    + r"\b"
    r"|(?:" + _LAUNCH.pattern + r"|" + _RESUME.pattern + r") (?:(?:a|an|the|another|new|me a) )*"
    + r"(?:" + _alternation(_AGENT_ALIAS) + r")" + _END +
    r"|(?:let me know|notify me|tell me|ping me|alert me) (?:when|once|if|as soon as)\b")


# Before a later clause that will run, "wait a bit" / "give it five minutes"
# is a wait too, and the clause after it must not run at once.
_TIMED_WAIT = re.compile(_ANY_WAIT + r"\b[^,;.()]{0,400}?\b(?:a bit|a while|a moment|a minute|a sec\w*|"
                         r"(?:\d+|" + _alternation(_NUMBERS) + r") (?:seconds?|secs?|minutes?|mins?|"
                         r"hours?))\b")


# A wait on a session or a condition on its turn (the first alternatives of
# _SESSION_CLAUSE), anywhere in a payload.
_WAIT_COND = re.compile(
    _ANY_WAIT + r"\b[^,;.()]{0,400}?\b(?:" + _SESSION_WORD[3:-1] + r")\b"
    r"|" + _ANY_WAIT + r"\b[^,;.()]{0,400}?\b(?:until|till) (?:(?:the|its) (?:review|run|job|turn|task|session|work) )?"
    r"(?:is |it's |gets |has )?(?:done|finished|complete|completed|idle|over|through)\b"
    r"|(?:when|once|after|as soon as|by the time) (?:it|it's|its|they|that|that's|this|"
    r"(?:the )?\w+ (?:session|review|run)|" + _alternation(_AGENT_ALIAS) + r")\b[^,;.()]{0,400}?\b"
    r"(?:done|finish\w*|complete\w*|idle|ready|through|asks?|waiting|blocked|needs?)\b")


def _within(positions: list, start: int, end: int) -> bool:
    i = bisect.bisect_left(positions, start)
    return i < len(positions) and positions[i] < end


_COND_TELL = re.compile(
    r"\b(?:when|once|after|as soon as)\b[^.;]{0,120}?\b(?:done|finish\w*|complete\w*|idle|ready)\b"
    r"[^.;]{0,120}?\b(?:" + _TELL.pattern.replace("(?P<verb>", "(?:") + r") (?:the |that |this )?"
    + _SESSION_WORD + r"\b")


def _seconds(m) -> int | None:
    """The seconds a timeout phrase says (half an hour, 5 minutes), up to a day."""
    if m["half"]:
        return 1800
    count = int(m["n"]) if m["n"].isdigit() else _NUMBERS[m["n"]]
    seconds = count * _UNIT[m["u"]]
    return seconds if 0 < seconds <= 86400 else None


def _last_session(acts) -> str | None:
    """What "it" names: the session of the latest clause -- the launch, when
    it is the only one, or the session a wait or message named."""
    if not acts:
        return None
    launches = sum(a.kind == "agent" for a in acts)
    if acts[-1].kind == "agent":
        return "it" if launches == 1 else None
    named = acts[-1].get("session")
    # A launch earlier and another session named since: "it" could be
    # either, so it names neither (review R14 round 3, KN-R14-46).
    return named if named == "it" or not launches else None


def _payload_ok(payload: Payload, weak: str | None, tell: bool = False) -> bool:
    """Whether a span can be a payload at all (see the module docstring)."""
    text = _plain(payload.text)
    low = _lower(text)
    if not text or len(text.encode()) > 4096:
        return False
    # Client commands: whatever the model sends (quotes dropped, typography
    # plain, full-width forms folded), it must start with a letter or digit,
    # and it is never one of the words that end a client's session.
    bare = unicodedata.normalize("NFKC", text).lstrip(_QUOTES_AND_BLANKS)
    if not bare or not bare[0].isalnum():
        return False
    if _fold(bare).rstrip(_QUOTES_AND_BLANKS + ".!") in _EXIT_WORDS:
        return False
    if _REFUSE[4][0].search(low) or _TAKE_BACK.search(low) or _REPORTED.search(low) \
            or _NOT_NOW.search(low) or _CONTROL_AGENT.search(low) or _ASKING.search(low):
        return False
    if weak and "?" in text:
        return False
    if (weak or tell) and any(_AGENT.match(low, _DET.match(low, m.end()).end())
                              for m in re.finditer(r"(?:^|[,;] ?(?:and |then |and then )?| and (?:then )?| then )",
                                                  low)):
        return False            # "and codex in research" is another clause, not a task
    # A wait on a session, a "when it is done" or a "let me know when" in any
    # later part of any payload is a clause for this tool, left over: the
    # request has no single reading (review R14 round 4, KN-R14-53).
    for m in re.finditer(r"[,;.!?] ?(?:and |then |and then )?| and (?:then )?| then |"
                         r"(?<=[.!?])\s+", low):
        rest = _CLAUSE_LEAD.match(low, m.end()).end()
        if _SESSION_CLAUSE.match(low, rest):
            return False
    if _COND_TELL.search(low):
        return False            # "… when the review is done tell it to push", no separator
    if weak or tell:
        for segment in re.split(r"[,;] ?(?:and |then |and then )?| and (?:then )?| then ", low)[1:]:
            words = re.findall(r"[a-z][a-z']*", segment)
            while words and words[0] in _LEAD:
                words = words[1:]
            if words and words[0] in _JOB_HEAD and _AGENT.search(segment):
                return False    # ", tell the claude session in research …" is a clause
    sentences = re.split(r"(?<=[.!?;])\s+", low)
    for index, sentence in enumerate(sentences):
        words = re.findall(r"[a-z][a-z']*", sentence)
        while words and words[0] in _LEAD:
            words = words[1:]
        head = words[0] if words else ""
        if index == 0 and weak and head in _WEAK_HEAD[weak] or index and head in _JOB_HEAD \
                and _AGENT.search(sentence):
            return False
    return True


# ---------------------------------------------------------------------------
# Matching the model's calls to the reading.

def _dir_norm(value: str) -> str:
    value = _fold(value).removeprefix("the ")
    for suffix in _DIR_SUFFIX:
        value = value.removesuffix(" " + suffix)
    return value.replace(" ", "-")


def _same_dir(value, want: str, dirs: dict | None) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if dirs is not None:
        return _dir_forms(dirs).get(_fold(value)) == want
    if want == "here":
        return _fold(value) in map(_fold, ("here", *_HERE))
    if want.startswith(("~", "/", ".")):
        # The same path, however the model wrote the words around it: "the
        # directory /x", "/x/" (route benchmark: "not the directory the request
        # names" for its own path).
        got = re.sub(r"^(?:(?:in|at) )?(?:the )?(?:directory|folder|dir) ", "", value.strip(),
                     flags=re.I)
        return got.rstrip("/") == want.rstrip("/")
    return _dir_norm(value) == _dir_norm(want)


def _same_session(value, want: str, dirs) -> bool:
    if not isinstance(value, str):
        return False
    if want == "it":
        return _fold(value) == "it"
    agent, _, where = value.partition("@")
    want_agent, _, want_where = want.partition("@")
    return _resolve_name(AGENT_NAMES, agent) == want_agent and _same_dir(where, want_where, dirs)


def _match(call: dict, want: Want, dirs) -> tuple[Action | None, str]:
    name, args = call.get("name"), call.get("arguments")
    if name != want.kind:
        return None, f"the request asks for {want.kind} here, not {name}"
    extra = set(args) - ARG_NAMES[name]
    if extra:
        return None, "unknown arguments: " + ", ".join(sorted(map(str, extra)))

    def empty(key):
        return args.get(key) in (None, "")

    def payload(key):
        wanted = want.get(key)
        if wanted is None:
            return (None, "") if empty(key) else (False, f"the request gives no {key}")
        value, forms = args.get(key), wanted.forms()
        if not isinstance(value, str) or _plain(value) not in forms:
            return False, (f"the {key} is not the request's words from its marker to its end, "
                           f"as written")
        return forms[_plain(value)], ""

    if name == "agent":
        if _resolve_name(AGENT_NAMES, args.get("agent")) != want.get("agent"):
            return None, f"the request names {want.get('agent')}"
        if not _same_dir(args.get("dir"), want.get("dir"), dirs):
            return None, "not the directory the request names for that agent"
        out = {"agent": want.get("agent"), "dir": want.get("dir")}
        prompt, why = payload("prompt")
        if prompt is False:
            return None, why
        for key in ("resume", "model"):
            wanted, value = want.get(key), args.get(key)
            if key == "resume" and isinstance(value, str):
                # "the flake triage session" names the session "flake triage".
                value = _fold(value).removeprefix("the ").removesuffix(" session")
            if wanted is None and not empty(key) or wanted is not None and (
                    not isinstance(value, str) or _fold(value) != _fold(wanted)):
                return None, f"not the {key} the request says"
        place = args.get("place") or "tab"
        if place not in PLACES or place != (want.get("place") or "tab"):
            return None, "not the side the request says"
        out.update({k: v for k, v in (("prompt", prompt), ("resume", want.get("resume")),
                                      ("model", want.get("model")),
                                      ("place", want.get("place"))) if v is not None})
        return Action(name, out), ""
    if not _same_session(args.get("session"), want.get("session"), dirs):
        return None, "not the session the request names there"
    if name == "wait":
        if args.get("for") != want.get("for"):
            return None, f"the request waits until it is {want.get('for')}"
        timeout = args.get("timeout")
        if want.get("timeout") is None and timeout is not None or want.get("timeout") is not None \
                and (type(timeout) is not int or timeout != want.get("timeout")):
            return None, "not the time limit the request says"
        out = {"session": want.get("session"), "for": want.get("for")}
        if want.get("timeout") is not None:
            out["timeout"] = want.get("timeout")
        return Action(name, out), ""
    text, why = payload("text")
    if not text:
        return None, why or "a message needs its text"
    if len(text.encode()) > 1024:
        return None, "a message is at most 1024 bytes"
    flag = args.get("wait")
    if flag not in (None, True, False) or bool(flag) != bool(want.get("wait")):
        return None, ("the request says to wait until it is done first" if want.get("wait")
                      else "the request doesn't say to wait first")
    out = {"session": want.get("session"), "text": text}
    if want.get("wait"):
        out["wait"] = True
    return Action(name, out), ""


def _why_not(request: str) -> str:
    low = _lower(normalize(request))
    for pattern, reason in _REFUSE:
        if pattern.search(low):
            return reason
    if re.search(r"[.!;]\s+\S", low.rstrip(" .!")):
        return "the request says more than one sentence"
    return "the request isn't a launch, wait or message this job can read"


# How agents word a launch (Codex route benchmark, 2026-09-29): "start an
# interactive Codex coding-agent session in a new tab, working in the directory
# /x. Do not give it a task." Each rewrite keeps the meaning of the canonical
# form: a launch always opens its own tab, and a launch without a task is the
# default. Only a whole trailing no-task phrase is removed; any other "not" in
# the request still refuses it.
_AGENT_WORDS = _alternation(_AGENT_ALIAS)
_LAUNCH_REWRITES = (
    (re.compile(rf"\b(?:an? (?:new )?(?:interactive )?|(?:new )?interactive |new )({_AGENT_WORDS})"
                rf"(?: coding[- ]agent| agent)? session\b(?! (?:called|named|titled|labell?ed)\b)",
                re.I), r"\1"),
    # A tab running an agent in a directory is a launch there (candidate check,
    # CLI: "open a new tab in /x running codex" reached the panes job).
    (re.compile(rf"^(?:open|start|create|launch)\s+(?:a\s+)?(?:new\s+)?tab\s+in\s+(?P<d>[~/]\S*)\s+"
                rf"(?:running|with|and\s+(?:run|start|launch))\s+({_AGENT_WORDS})\b(?:\s+there\b)?", re.I),
     r"start \2 in \g<d>"),
    # "Open a new interactive Codex coding-agent tab in /x": the tab is the agent's.
    (re.compile(rf"^(?:open|start|create|launch)\s+(?:an?\s+)?(?:new\s+)?(?:interactive\s+)?({_AGENT_WORDS})"
                rf"(?:\s+coding[- ]agent|\s+agent)?\s+tab\s+in\s+(?P<d>[~/]\S*)", re.I), r"start \1 in \g<d>"),
    (re.compile(rf"^(?:open|start|create|launch)\s+(?:a\s+)?(?:new\s+)?tab\s+(?:running|with)\s+({_AGENT_WORDS})\s+"
                rf"in\s+(?:(?:the\s+)?(?:directory|folder|dir)\s+)?(?P<d>[~/]\S*)", re.I),
     r"start \1 in \g<d>"),
    # "start Codex as an interactive coding-agent session in a new tab in /x"
    (re.compile(rf"\b({_AGENT_WORDS})\s+as\s+an?\s+(?:new\s+)?(?:interactive\s+)?(?:coding[- ]agent\s+|agent\s+)?"
                rf"session\b", re.I), r"\1"),
    (re.compile(r",? in a new tab\b", re.I), ""),
    (re.compile(r",? (?:working |running )?in (?:the )?(?:directory|folder|dir) (?=[~/])", re.I), " in "),
    (re.compile(r",? working in (?=[~/])", re.I), " in "),
    (re.compile(r"^(?P<head>[^:]*?),? working directory (?=[~/])", re.I), r"\g<head> in "),
)


def exact_calls(request: str) -> list | None:
    """The calls this job's own reading of the request states, when the checks
    admit every one: no model is needed for them (route benchmark, gpt-6-luna:
    the tuned model rewrote long directories and every launch was refused)."""
    wants = parse(request, None)
    if not wants:
        return None
    calls = [{"name": w.kind,
              "arguments": {k: (v.text.strip() if isinstance(v, Payload) else v) for k, v in w.args}}
             for w in wants]
    results = interpret(request, calls)
    if not results or not all(isinstance(r, Action) for r in results):
        return None
    return calls


# A launch with no task, however it is said, at the end of the request. The rule
# is structural, not a list of phrasings: a clause whose negation's object is
# the task ("no task", "without a prompt", "do not <any verb> it a task or
# prompt after startup", "leave the task blank") removes the task and never
# negates the launch; "leave the session open", "then stop" and "start it" say
# nothing more. Only whole clauses at the end go; any other "not" still
# refuses the request.
_TASK = r"(?:task|prompt|instructions?|message|input|assignment)s?"
_TASK_MOD = r"(?:(?:initial|first|starting|opening|new|further)\s+)?"
_TASKS = (rf"(?:(?:a|an|any|the|its|some)\s+)?{_TASK_MOD}{_TASK}"
          rf"(?:\s*(?:/|or|and)\s*(?:(?:a|an|any)\s+)?{_TASK_MOD}{_TASK})*")
_WHO = rf"(?:(?:to\s+)?(?:it|them|the\s+(?:agent|session|(?:{_AGENT_WORDS})\s+session)|{_AGENT_WORDS})\s+)?"
_WHEN = (r"(?:\s+(?:into|to|for|in)\s+(?:it|them|the\s+(?:session|agent)))?"
         r"(?:\s+(?:after|at|on|upon|during|when|once)\s+(?:the\s+)?(?:startup|start-up|start|launch|"
         r"opening|it\s+(?:starts|opens|launches))|\s+(?:yet|for\s+now|at\s+all|initially))?")
_LEAVE_SESSION = rf"(?:leave|keep)\s+(?:the\s+(?:new\s+)?(?:session|agent|tab)|it|{_AGENT_WORDS})"
_NO_TASK_CLAUSE = (
    rf"(?:with\s+)?(?:no|zero)\s+{_TASKS}"
    rf"|without\s+(?:(?:giving|sending|passing|providing|assigning)\s+{_WHO})?{_TASKS}"
    rf"|(?:(?:please\s+)?(?:do\s+not|don'?t|never)|no\s+need\s+to)\s+[a-z]+(?:\s+(?:in|over|along|up))?\s+{_WHO}{_TASKS}"
    rf"|(?:give|send|pass|provide|assign)\s+{_WHO}no\s+{_TASKS}"
    rf"|(?:leave|keep)\s+(?:the\s+|its\s+)?{_TASKS}\s+(?:blank|empty|unset)"
    rf"|{_TASKS}\s+(?:blank|empty|none)"
    rf"|{_LEAVE_SESSION}\s+(?:with\s+no\s+|without\s+){_TASKS}"
    rf"|(?:it|{_AGENT_WORDS}|the\s+(?:agent|session))\s+(?:(?:should|will|must|can|is\s+to)\s+)?"
    rf"(?:get|gets|have|has|receive|receives|need|needs|take|takes)\s+no\s+{_TASKS}")
# The caller stopping after the launch: "do not wait, message, or perform any
# further actions". Only these verbs; any other "not" stays refused.
_FURTHER = r"(?:any(?:thing)?\s+)?(?:further|additional|other|more|else)(?:\s+(?:actions?|steps?))?"
_STOPPING = (r"(?:wait(?:\s+for\s+(?:it|the\s+(?:agent|session)))?|message(?:\s+(?:it|the\s+(?:agent|session)))?"
             r"|send\s+(?:it\s+)?(?:any\s+)?messages?|act\s+further"
             # a no-task verb in the same list: "do not wait or send it any task"
             rf"|(?:give|send|pass|provide|assign|type|enter)\s+{_WHO}{_TASKS}"
             rf"|(?:perform|do|take)\s+(?:any\s+|anything\s+)?{_FURTHER}|do\s+anything(?:\s+(?:further|else|more))?)")
_NEUTRAL_CLAUSE = (
    rf"(?:do\s+not|don'?t|never)\s+{_STOPPING}(?:(?:\s*,\s*|\s+)(?:(?:or|and|nor)\s+)?{_STOPPING})*"
    r"|(?:then\s+)?(?:stop(?:\s+there)?|that'?s\s+all|nothing\s+else)"
    r"|stop\s+after\s+(?:launching|starting|opening)\s+(?:the\s+)?(?:session|agent)"
    # "..., then stop the session": after a launch, agents mean they stop there;
    # stopping the session just started would undo the request. Only after "then".
    r"|(?:and\s+)?then\s+stop\s+(?:the\s+session|here)(?:\s+there)?"
    r"|(?:(?:and\s+)?then\s+)?(?:start|launch|open|run)\s+it(?:\s+up)?"
    rf"|{_LEAVE_SESSION}\s+at\s+(?:the|its)\s+interactive\s+prompt"
    rf"|(?:just\s+)?(?:leave|keep)\s+(?:the\s+(?:session|agent|tab)|it|{_AGENT_WORDS})\s+"
    r"(?:open|idle|running|there|waiting|as\s+is)(?:(?:\s*,\s*|\s+)(?:and\s+)?(?:open|idle|running|there|waiting))*"
    r"(?:\s+(?:with\s+no|without\s+(?:a\s+|any\s+)?)\s*" + _TASK + r")?")
_NO_TASK = re.compile(
    # "with no task" may follow plain space; every other clause needs a real break
    # (". Then stop", "; do not send it any task"): "open codex in X to please stop"
    # keeps its task text, and is refused as before.
    rf"(?:\s+(?:(?:with\s+)?(?:no|zero)\s+{_TASKS}"
    rf"|without\s+(?:(?:giving|sending|passing|providing|assigning)\s+{_WHO})?{_TASKS}){_WHEN}\s*[.!]*$)"
    rf"|(?:[.,;:]\s*(?:and\s+)?|\s+-\s+|\s+and\s+)(?:{_NO_TASK_CLAUSE}|{_NEUTRAL_CLAUSE}){_WHEN}\s*[.!]*$", re.I)


def _launch_plain(text: str) -> str:
    for pattern, replacement in _LAUNCH_REWRITES:
        text = pattern.sub(replacement, text)
    # Only a launch without a message: a payload after ":" (a task, or a message
    # to a session) is the person's words and is never edited.
    if ":" in text or not re.match(r"\s*(?:please\s+)?(?:start|launch|open|run|begin|spin\s+up|fire\s+up)\b",
                                   text, re.I):
        return " ".join(text.split()).strip()
    for _ in range(4):
        stripped = _NO_TASK.sub("", text)
        if stripped == text:
            break
        text = stripped
    return " ".join(text.split()).strip()


_SAID_NO_TASK = re.compile(rf"(?:^|[\s.,;:-])(?:{_NO_TASK_CLAUSE}){_WHEN}\s*[.!]*$", re.I)


def _says_no_task(original: str, plain: str) -> bool:
    """Whether a clause taken off the end said there is no task."""
    left = original
    for _ in range(4):
        if _SAID_NO_TASK.search(left):
            return True
        shorter = _NO_TASK.sub("", left)
        if shorter == left:
            return False
        left = shorter
    return False


def parse(request: str, dirs: dict | None = FIXTURE_DIRS) -> list | None:
    """The actions the request states, as Want tuples, or None."""
    text = str(request)
    # One line of visible text: no control or format characters (line breaks,
    # bidi overrides, zero-width marks) that would change what is sent.
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") and ch != "\t" for ch in text):
        return None
    text = " ".join(text.split())
    if len(text) > MAX_REQUEST:
        return None
    plain = _launch_plain(text)
    got = _Reader(plain, dirs).read() if plain else None
    # "start codex in X to fix the build. Do not send a prompt": a task and "no
    # task" both; the request says it both ways and is not read.
    if got and plain != " ".join(text.split()) and _says_no_task(" ".join(text.split()), plain) \
            and any(k == "prompt" for w in got for k, _ in w.args):
        return None
    return list(got) if got else None


def interpret(request: str, calls: list, dirs: dict | None = None) -> list:
    """Turn the engine's calls into admitted actions or named refusals.

    `dirs` is a table of directory names; the eval sets pass FIXTURE_DIRS.
    Without one, the directories the request itself says are the names.
    """
    calls = calls if isinstance(calls, list) else []
    results = []
    for call in calls:
        name = call.get("name") if isinstance(call, dict) else None
        if name not in TOOL_NAMES or not isinstance(call.get("arguments"), dict):
            results.append(Refusal(str(name), "not a kilix-needle agents action"))
    if results or not calls:
        return results + [Refusal(str(c.get("name") if isinstance(c, dict) else None),
                                  "refused with the rest of the request")
                          for c in calls if isinstance(c, dict) and c.get("name") in TOOL_NAMES
                          and isinstance(c.get("arguments"), dict)]
    wants = parse(request, dirs)
    if wants is None:
        reason = _why_not(request)
        return [Refusal(call["name"], reason) for call in calls]
    why = ""
    for reading in _readings(wants):
        if len(reading) != len(calls):
            why = why or (f"the request states {len(reading)} action"
                          f"{'s' * (len(reading) != 1)}, not {len(calls)}")
            continue
        admitted = []
        for call, want in zip(calls, reading):
            action, why = _match(call, want, dirs)
            if action is None:
                break
            admitted.append(action)
        else:
            return admitted
    return [Refusal(call["name"], why) for call in calls]


def _readings(wants: list) -> list:
    """The reading, and the same reading with "wait until S is idle, then tell
    S" written as one message that waits first (and the other way round):
    the spec's writers use both, and they do the same thing."""
    out = [wants]
    for i in range(len(wants)):
        want = wants[i]
        if want.kind == "tell" and want.get("wait"):
            wait = _want("wait", session=want.get("session"), **{"for": "idle"})
            tell = _want("tell", session=want.get("session"), text=want.get("text"))
            out.append(wants[:i] + [wait, tell] + wants[i + 1:])
        nxt = wants[i + 1] if i + 1 < len(wants) else None
        if want.kind == "wait" and want.get("for") == "idle" and want.get("timeout") is None \
                and nxt and nxt.kind == "tell" and not nxt.get("wait") \
                and nxt.get("session") == want.get("session"):
            tell = _want("tell", session=nxt.get("session"), text=nxt.get("text"), wait=True)
            out.append(wants[:i] + [tell] + wants[i + 2:])
    return out
