"""The agents job: launch coding-agent sessions, wait for them, message them.

The model sees three tools (TOOLS). Every call is untrusted. A request is
admitted only when it is *accounted for*:

1. Payloads come out first. A launch prompt or a message (`prompt`, `text`)
   must appear verbatim in the request; that span is data, never read for
   verbs, and is cut out. So are the names the calls use: the agent, the
   directory, a model, a session id.
2. What remains is the command. Every word of it must come from a closed
   vocabulary of command and connective words (`_VOCABULARY`). A word
   outside it ("translate", "pretend", "friend") means the request says
   something no action accounts for, and nothing is admitted. This is the
   structural form of review R12's lesson; word lists alone were always one
   word short.
3. The command may not negate, report, cancel, ask, install or change
   permissions (`_REFUSE`), checked in the command only.

Owner, 2026-09-27: a request to launch a coding agent is itself the consent,
and sessions launch, wait for and message other sessions. So an admitted
request needs no second yes. See AGENTS-JOB-DESIGN.md in the research notes.

Directories and sessions are resolved outside this module (`Resolver`): the
checks only require that the request names them. The eval sets use a fixed
fixture workspace (FIXTURE_DIRS).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from actions import Refusal, normalize

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
    "kilix": ["kilix", "the kilix repo", "~/gpu_terminal/kilix"],
    "kilix-needle": ["kilix-needle", "kilix needle", "needle", "the needle repo"],
    "kilix-content": ["kilix-content", "kilix content", "the catalog", "content"],
    "kilix-95": ["kilix-95", "kilix 95", "the desktop repo"],
    "plebian-os": ["plebian-os", "plebian os", "the os repo", "plebian"],
    "kilix-ml": ["kilix-ml", "kilix ml", "the ml repo"],
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
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)


@dataclass(frozen=True)
class Action:
    kind: str
    args: dict = field(default_factory=dict)

    @property
    def risky(self) -> bool:
        return False            # the request is the consent (owner, 2026-09-27)


# ---------------------------------------------------------------------------
# The command's closed vocabulary. Anything else in the command, once
# payloads and names are cut out, means the request isn't accounted for.

_VOCABULARY = frozenset("""
a an the new fresh another second separate my our your this that its it them his her
of for in into at on from to with using via and then also too as
please pls kindly could can would will you me us i we just now quickly quick go ahead
ok okay hey so let lets let's up over there right away
session sessions instance agent reviewer review worker helper copy pane tab split window
repo repository dir directory folder checkout project workspace model cli
open start launch spawn fire spin run bring kick off get boot create resume continue
reopen pick back
wait until till when once it's is has done finished finishes finish idle complete
completes completed ready asks asking question questions needs input approval waiting
blocked reaches be seconds second minutes minute mins min secs sec hours hour most
timeout time limit longer than max maximum up
tell ask send message ping know instruct say notify have saying saying: telling
right left below above down beside next side vertical horizontal
task turn goes go gets becomes hold block put give after but work working stop
own under underneath top needs input
""".split())
_NUMBER_WORDS = frozenset("""one two three four five six seven eight nine ten eleven twelve
fifteen twenty thirty forty forty-five fifty sixty ninety a an half""".split())
_COURTESY = frozenset("thanks thank thx ty cheers please pls".split())

# Refused in the command (never in a payload).
_REFUSE = [
    (re.compile(r"\b(?:not|never|don'?t|do not|no|nope|without|cannot|can'?t|won'?t|\w+n't)\b"),
     "the request says not to"),
    (re.compile(r"\b(?:said|says|say so|told|tells|wrote|writes|asked me|according to|"
                r"someone|somebody|anyone|friend|boss|colleague)\b"),
     "the request reports someone else's words"),
    (re.compile(r"\b(?:cancel|never ?mind|nvm|scratch that|actually|wait,? no|undo|jk|kidding)\b"),
     "the request takes something back"),
    (re.compile(r"\b(?:re ?install\w*|install\w*|uninstall\w*|updat\w*|upgrad\w*|download\w*|"
                r"set up|setup)\b"),
     "installing or updating an agent is not done here"),
    (re.compile(r"\b(?:yolo|dangerous\w*|bypass\w*|skip\w* (?:the )?(?:approvals?|permissions?|"
                r"prompts?)|auto.?approve\w*|always.?approve|permission\w*|sandbox\w*)\b"),
     "permissions follow Kilix's coding-yolo setting and are not set by a request"),
    (re.compile(r"\b(?:kill|close|quit|exit|terminate)\b|\b(?:stop|end)\s+(?:it|them|that|the|this|"
                r"those|\u2063)"),
     "closing or stopping sessions is not done here"),
]
_QUESTION_HEAD = re.compile(r"^(?:should|shall|how|what|why|which|who|whose|is|are|does|did|"
                            r"do (?:i|we|you))\b")
_POLITE_ASK = re.compile(r"^(?:(?:please|pls|ok|okay|hey|hi)\s+)*(?:can|could|would|will) you\b")

_LAUNCH_VERB = re.compile(r"\b(?:open|start|launch|spawn|fire|spin|run|bring|kick|get|boot|"
                          r"create|resume|continue|reopen|pick|put)\b")
_RESUME_WORD = re.compile(r"\b(?:resume|continue|reopen|pick\b.*\bback|back)\b")
_WAIT_WORD = re.compile(r"\b(?:wait|until|till|when|once|after)\b")
_TELL_WORD = re.compile(r"\b(?:tell|ask|send|message|ping|let\b.*\bknow|instruct|say|notify|"
                        r"have|give\b.*\binput)\b")
_IDLE_WORD = re.compile(r"\b(?:done|finished|finishes|finish|idle|complete|completes|"
                        r"completed|ready)\b")
_WAITING_WORD = re.compile(r"\b(?:asks|asking|question|questions|needs|approval|"
                           r"waiting|blocked)\b")
_PLACE_WORDS = {"right": r"\bright\b", "left": r"\bleft\b",
                "down": r"\b(?:below|down|under(?:neath)?)\b",
                "up": r"\b(?:above|up|on top)\b(?! to)"}
_SPLIT_WORD = re.compile(r"\b(?:split|beside|next to|side)\b")
_UNIT = {"second": 1, "seconds": 1, "sec": 1, "secs": 1, "minute": 60, "minutes": 60,
         "min": 60, "mins": 60, "hour": 3600, "hours": 3600}
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
            "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50,
            "sixty": 60, "ninety": 90, "half": 0.5, "a": 1, "an": 1}


def _fold(text: str) -> str:
    return " ".join(normalize(text).casefold().split())


def _said(phrase: str, text: str) -> re.Match | None:
    return re.search(rf"(?<![\w~/-]){re.escape(phrase)}(?![\w/-])", text)


def _names(table: dict, key: str) -> list[str]:
    return sorted({_fold(key)} | {_fold(n) for n in table.get(key, [])}, key=len, reverse=True)


def _resolve_name(table: dict, value) -> str | None:
    if not isinstance(value, str):
        return None
    said = _fold(value)
    hits = [key for key in table if said in _names(table, key)]
    return hits[0] if len(hits) == 1 else None


def _mentioned(table: dict, key: str, text: str) -> list[str]:
    """The names of key the text says, longest first, never a shorter name
    inside a longer one of another key."""
    found = []
    for phrase in _names(table, key):
        if _said(phrase, text):
            longer = [p for other in table if other != key for p in _names(table, other)
                      if len(p) > len(phrase) and phrase in p and _said(p, text)]
            if not longer:
                found.append(phrase)
    return found


@dataclass
class Reading:
    command: str = ""               # the request with payloads and names cut out
    refusal: str | None = None
    unaccounted: tuple = ()


def _cut(text: str, span: str) -> str:
    """Cut a verbatim span (and quotes right around it) out of folded text."""
    index = text.find(span)
    if index < 0:
        return text
    start, end = index, index + len(span)
    while start > 0 and text[start - 1] in "\"'`":
        start -= 1
    while end < len(text) and text[end] in "\"'`":
        end += 1
    return text[:start] + " ⁣ " + text[end:]


def read(request: str, calls: list, dirs: dict) -> Reading:
    """Cut payloads and names out; check what remains."""
    text = _fold(request)
    for call in calls:
        args = call.get("arguments") if isinstance(call, dict) else None
        if not isinstance(args, dict):
            continue
        for key in ("prompt", "text"):
            value = args.get(key)
            if isinstance(value, str) and value.strip() and _fold(value) in text:
                text = _cut(text, _fold(value))
    names = [n for key in AGENT_NAMES for n in _names(AGENT_NAMES, key)]
    names += [n for key in dirs for n in _names(dirs, key)]
    for call in calls:
        args = call.get("arguments") if isinstance(call, dict) else {}
        for key in ("model", "resume"):
            value = args.get(key) if isinstance(args, dict) else None
            if isinstance(value, str) and value.strip():
                names.append(_fold(value))
    for name in sorted(set(names), key=len, reverse=True):
        while True:
            match = _said(name, text)
            if not match:
                break
            text = text[:match.start()] + " ⁣ " + text[match.end():]
    command = " ".join(text.replace("⁣", " ").split())
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", command) if s.strip(" .!?")]
    if len([s for s in sentences if set(re.findall(r"[\w'-]+", s)) - _COURTESY]) > 1:
        return Reading(command, "the request says more than one sentence")
    if "?" in command and not _POLITE_ASK.match(command):
        return Reading(command, "the request is a question")
    if _QUESTION_HEAD.match(command) and not _POLITE_ASK.match(command):
        return Reading(command, "the request is a question")
    softened = re.sub(r"\bno (?:longer|more) than\b", " ", command)
    for pattern, reason in _REFUSE:
        if pattern.search(softened):
            return Reading(command, reason)
    words = re.findall(r"[a-z0-9'][a-z0-9'-]*", command)
    unaccounted = tuple(w for w in words if w not in _VOCABULARY and w not in _NUMBER_WORDS
                        and w not in _COURTESY and not w.isdigit())
    if unaccounted:
        return Reading(command, "the request says more than these actions: "
                                + ", ".join(dict.fromkeys(unaccounted)), unaccounted)
    return Reading(command)


# ---------------------------------------------------------------------------


def _session(value, request_text: str, launched: set, dirs: dict) -> str | None:
    """'it' (a session this request launches) or agent@dir, both named."""
    if not isinstance(value, str):
        return None
    value = _fold(value)
    if value == "it":
        return "it" if launched and re.search(r"\b(?:it|them|that session|the session)\b",
                                              request_text) else None
    agent, sep, place = value.partition("@")
    agent = _resolve_name(AGENT_NAMES, agent)
    directory = _resolve_name(dirs, place)
    if not sep or agent is None or directory is None:
        return None
    if not _mentioned(AGENT_NAMES, agent, request_text) or not _mentioned(dirs, directory,
                                                                          request_text):
        return None
    return f"{agent}@{directory}"


def _admit(name: str, args: dict, request_text: str, launched: set, dirs: dict):
    if name == "agent":
        agent = args.get("agent")
        directory = _resolve_name(dirs, args.get("dir"))
        if agent not in AGENTS or directory is None:
            return Refusal(name, "not a known agent or directory")
        if not _mentioned(AGENT_NAMES, agent, request_text):
            return Refusal(name, f"the request doesn't name {agent}")
        if not _mentioned(dirs, directory, request_text):
            return Refusal(name, f"the request doesn't name the directory {directory}")
        # A verb, or the terse form that opens with the agent ("qwen omp in kilix ml").
        terse = any(request_text.startswith(n) for n in _names(AGENT_NAMES, agent))
        if not _LAUNCH_VERB.search(request_text) and not terse:
            return Refusal(name, "no launch verb")
        out = {"agent": agent, "dir": directory}
        prompt = args.get("prompt")
        if prompt not in (None, ""):
            if not isinstance(prompt, str) or _fold(prompt) not in request_text:
                return Refusal(name, "the prompt is not the request's own words")
            if prompt.lstrip().startswith("-") or "\n" in prompt:
                return Refusal(name, "a prompt is one line and never starts with '-'")
            out["prompt"] = _fold(prompt)
        resume = args.get("resume")
        if resume not in (None, ""):
            if (not isinstance(resume, str) or not _said(_fold(resume), request_text)
                    or not _RESUME_WORD.search(request_text)):
                return Refusal(name, "resume needs a resume word and the session the request names")
            if _fold(resume) in ("last", "latest", "the last one", "last one", "previous"):
                return Refusal(name, "resume needs a session id or title, not 'the last one'")
            out["resume"] = _fold(resume)
        model = args.get("model")
        if model not in (None, ""):
            if not isinstance(model, str) or not _said(_fold(model), request_text):
                return Refusal(name, "the model is not one the request names")
            out["model"] = _fold(model)
        place = args.get("place")
        if place not in (None, "", "tab"):
            if place not in PLACES or not _SPLIT_WORD.search(request_text) and not re.search(
                    _PLACE_WORDS[place], request_text):
                return Refusal(name, "the request doesn't say that side")
            if not re.search(_PLACE_WORDS[place], request_text):
                return Refusal(name, "the request doesn't say that side")
            out["place"] = place
        return Action(name, out)
    if name == "wait":
        session = _session(args.get("session"), request_text, launched, dirs)
        state = args.get("for")
        if session is None or state not in STATES:
            return Refusal(name, "not a session the request names, or not idle/waiting")
        if not _WAIT_WORD.search(request_text):
            return Refusal(name, "no wait word")
        words = _IDLE_WORD if state == "idle" else _WAITING_WORD
        if not words.search(request_text):
            return Refusal(name, f"the request doesn't say to wait until it's {state}")
        out = {"session": session, "for": state}
        timeout = args.get("timeout")
        if timeout is not None:
            said = _timeout(request_text)
            if not isinstance(timeout, int) or said != timeout:
                return Refusal(name, "the timeout is not the one the request says")
            out["timeout"] = timeout
        return Action(name, out)
    # tell
    session = _session(args.get("session"), request_text, launched, dirs)
    text = args.get("text")
    if session is None:
        return Refusal(name, "not a session the request names")
    if not isinstance(text, str) or not text.strip() or _fold(text) not in request_text:
        return Refusal(name, "the message is not the request's own words")
    if "\n" in text or len(text.encode()) > 1024:
        return Refusal(name, "a message is one line of at most 1024 bytes")
    if not _TELL_WORD.search(_cut(request_text, _fold(text))):
        return Refusal(name, "no telling verb")
    out = {"session": session, "text": _fold(text)}
    if args.get("wait") is True:
        if not (_WAIT_WORD.search(request_text) and _IDLE_WORD.search(request_text)):
            return Refusal(name, "the request doesn't say to wait first")
        out["wait"] = True
    return Action(name, out)


def _timeout(text: str) -> int | None:
    if re.search(r"\bhalf an? hour\b", text):
        return 1800
    match = re.search(r"\b(\d+|" + "|".join(map(re.escape, _NUMBERS)) + r")\s+(" +
                      "|".join(_UNIT) + r")\b", text)
    if not match:
        return None
    count = int(match[1]) if match[1].isdigit() else _NUMBERS[match[1]]
    return int(count * _UNIT[match[2]])


def said_dirs(request: str, calls: list) -> dict:
    """Production: each directory a call names, when the request says those
    words, is its own name (the runner resolves it on disk); "here" and its
    kin are one name."""
    text = _fold(request)
    table = {"here": list(FIXTURE_DIRS["here"])}
    for call in calls:
        args = call.get("arguments") if isinstance(call, dict) else None
        if not isinstance(args, dict):
            continue
        values = [args.get("dir")] + [str(args.get("session") or "").partition("@")[2]]
        for value in values:
            if isinstance(value, str) and value.strip() and _said(_fold(value), text) \
                    and _fold(value) not in table["here"]:
                table[_fold(value)] = [_fold(value)]
    return table


def interpret(request: str, calls: list, dirs: dict | None = None) -> list:
    """Turn the engine's calls into admitted actions or named refusals.

    `dirs` is a table of directory names; the eval sets pass FIXTURE_DIRS.
    Without one, the directories the request itself says are the names.
    """
    calls = calls if isinstance(calls, list) else []
    dirs = said_dirs(request, calls) if dirs is None else dirs
    reading = read(request, calls, dirs)
    text = _fold(request)
    results = []
    launched: set = set()
    for call in calls:
        name = call.get("name") if isinstance(call, dict) else None
        args = call.get("arguments") if isinstance(call, dict) else None
        if name not in TOOL_NAMES or not isinstance(args, dict):
            results.append(Refusal(str(name), "not a kilix-needle agents action"))
            continue
        if reading.refusal:
            results.append(Refusal(name, reading.refusal))
            continue
        result = _admit(name, args, text, launched, dirs)
        if isinstance(result, Action) and result.kind == "agent":
            launched.add(f"{result.args['agent']}@{result.args['dir']}")
        results.append(result)
    return results
