"""Complete admission for the five-tool local model contract.

The original request supplies every argument. Model proposals must match the
complete reading, including order, literal payloads and host-owned defaults.
This module never executes a command or installs an application. Its only
filesystem access is a stat that tells whether an unquoted spaced path exists.
"""
import contextvars
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import unicodedata

import agents
import apps


@dataclass(frozen=True)
class Admission:
    job: str | None
    actions: tuple = ()
    reason: str = ""


TOOLS = json.loads(Path(__file__).with_name('commands-tools.json').read_text())
_SCHEMAS = {tool['name']: tool['parameters'] for tool in TOOLS}
_PROPERTIES = {name: set(schema['properties']) for name, schema in _SCHEMAS.items()}
_REQUIRED = {name: set(schema.get('required', ())) for name, schema in _SCHEMAS.items()}
# Grammar-only arguments (2026-11-24): a wait or a message may name its session
# by Kilix pane number, optionally with the pane's broker id. commands-tools.json
# is pinned (compact_model.TOOLS_SHA256, the Needle3 manifest) and is what the
# command models are prompted with, so it does not change: these arguments exist
# only in what the grammar itself reads, and a command model's proposal is held
# to the published tools (admit(published=True)). With a pane, "agent" is
# optional (the runner names the agent it finds there and checks a named one)
# and "directory" is not allowed. The runner verifies the pane before anything
# is typed (agents_kilix.find_pane).
GRAMMAR_ONLY = {'wait_for_agent': frozenset({'pane', 'broker'}),
                'message_agent': frozenset({'pane', 'broker'})}
PANE_AGENTS = ('claude', 'codex', 'grok')
_PANE_NUMBER = re.compile(r'[1-9][0-9]{0,8}')
_BROKER_ID = re.compile(r'[0-9a-f]{16,64}')     # Kilix's own agent-control accepts these
# "pane 143", "Kilix pane 9", with its broker: "pane 32 broker <id>", "on broker
# <id>", ", broker <id>", "with broker session <id>", "(broker <id>)". The number
# is copied, never inferred: "pane503", "session 518" or "the pane on the left"
# is no target. Matched without case, so a broker id is checked again by
# _BROKER_ID, which takes lowercase hex only.
PANE_SPAN = (r'\b(?:kilix\s+)?pane\s+(?P<pane>[1-9][0-9]{0,8})(?![\w-]|\.[\w-])'
             r'(?:\s*,?\s*(?:(?:on|with)\s+)?(?:the\s+)?broker(?:\s+session)?(?:\s+id)?\s+(?P<broker>[0-9a-fA-F]{16,64})(?![\w-]|\.[\w-])'
             r'|\s*\(\s*broker\s+(?P<paren>[0-9a-fA-F]{16,64})\s*\))?')
_APPS = set(_SCHEMAS['open_app']['properties']['app']['enum'])
_APP_WORDS = {"pdf viewer":"kilix-pdf", "pdf reader":"kilix-pdf",
              "calculator":"kilix-calculator", "notepad":"kilix-notepad",
              "file manager":"kilix-file", **{x:x for x in _APPS}}
_NOUN = "(?:" + "|".join(map(re.escape, sorted(_APP_WORDS, key=len, reverse=True))) + ")"
_PATH = r'(?:"(?:[^"\\]|\\.)+"|~?/[^\s;?]+)'
_PREAMBLE = re.compile(r"^(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|"
                       r"I\s+(?:want|need|would like)\s+(?:you\s+)?to\s+|I['’]d\s+like\s+(?:you\s+)?to\s+)?", re.I)
_LAUNCH_VERB = r'(?:start(?: up)?|launch|open|run|bring up|get(?: me)?|create|fire up|spin up|kick off)'
_SEPARATOR = re.compile(r";\s*(?:(?:then|next|also|after that|afterwards)\s*,?\s+)?|"
                        r",\s*(?=(?:open|launch|start|bring|show|get|wait|tell|send|message)\b)|"
                        r",?\s+(?:and\s+then|and|then)\s+", re.I)
_DIRECTORY = r'(?:"(?:[^"\\]|\\.)+"|~?/[^\s;,?]+)'
_LOCATION = re.compile(r'\b(?:working\s+)?(?:in|at|inside|within)\s+(?P<path>'+_DIRECTORY+r')|'
                       r'\bwith\s+(?P<working>'+_DIRECTORY+r')\s+as\s+(?:(?:its|the)\s+)?working\s+directory|'
                       r'\b(?P<here>here|in (?:the )?(?:current directory|current folder|this folder|this directory))\b', re.I)
# Models the compact contract admits for each agent (see _canonical).
_MODELS = {'start_claude': ('sonnet', 'opus'), 'start_codex': ('gpt-6.1-sol', 'gpt-6')}
_MODEL_NAME = r'[A-Za-z0-9][A-Za-z0-9._/-]*'
_MODEL = re.compile(r'(?:\b(?:with\s+model|using(?:\s+model)?|on\s+model)\s+|'
                    r'\bmodel\s*=\s*|\bmodel\s+|(?<!\S)--model(?:=|\s+))('+_MODEL_NAME+r')\b', re.I)
_PLACEMENTS = [
    ('right', r'(?:in\s+)?(?:a\s+)?split\s+(?:to\s+)?(?:the\s+|my\s+)?right|(?:on|to)\s+(?:the\s+|my\s+)?right'),
    ('left', r'(?:in\s+)?(?:a\s+)?split\s+(?:to\s+)?(?:the\s+|my\s+)?left|(?:on|to)\s+(?:the\s+|my\s+)?left'),
    ('up', r'(?:in\s+)?(?:a\s+)?split\s+(?:above(?:\s+(?:me|this pane|the current pane))?|up)|above\s+(?:me|this pane|the current pane)'),
    ('down', r'(?:in\s+)?(?:a\s+)?split\s+(?:below(?:\s+(?:me|this pane|the current pane))?|down)|below\s+(?:me|this pane|the current pane)'),
    ('tab', r'(?:in\s+)?(?:a\s+)?(?:(?:new|fresh)\s+)?tab'),
]
# Trailing idle wording on a launch that captured no task. A removed field leaves
# a NUL gap, and these clauses do not cross it. A task plus one of them is said
# both ways and is not a reading.
_COMPACT_NO_TASK = re.compile(
    r'(?:\s+with\s+no\s+task\b(?:\s+or\s+(?:initial\s+)?prompt\b)?'
    r'|(?:[;,.]\s*|\s+)leave\s+the\s+session\s+open\b)\s*[.!]?\s*$',
    re.I)
_CLAUDE_CODE_NOUN = re.compile(
    r'\s+(?:(?:kilix\s+)?pane|(?:coding\s+)?(?:client|session|workspace|agent|instance|worker))\b',
    re.I)
# Owner decision 2026-11-07: an unquoted path may contain single spaces. Words
# after a path cannot be told apart from a task, a courtesy or a second sentence
# ("in ~/proj fix the tests"), so a spaced reading is taken only when that exact
# path exists: a directory for an agent, a file for the PDF viewer. The longest
# existing reading wins, and the words after it are parsed as before. This is
# the only filesystem access here: a stat, never a create, open or listing.
_SPACED_WORD = re.compile(r' [^\s;,?:="\'\\/~\-][^\s;,?:="\\]*')


def _path_exists(path, *, directory):
    """Whether an absolute or home path names an existing directory or file."""
    try:
        target = os.path.expanduser(path) if path.startswith('~/') else path
        if not target.startswith('/') or len(target.encode()) > 4096:
            return False
        return os.path.isdir(target) if directory else os.path.isfile(target)
    except (OSError, ValueError):
        return False


# One reading of a request at a time: which existing path to take when two
# readings of the same unquoted path both exist (see exact_calls).
_PATH_CHOICE = contextvars.ContextVar('_PATH_CHOICE', default=None)


def _spaced_words(tail):
    """The single-spaced words directly after an unquoted path."""
    words = []
    match = _SPACED_WORD.match(tail)
    while match:
        words.append(match[0])
        match = _SPACED_WORD.match(tail, match.end())
    return words


def _literal(raw, *, sentence_end=False):
    try:
        if raw.startswith('"'):
            return json.loads(raw)
        # Outside quotes a final full stop/exclamation mark ends the sentence.
        # Preserve dot path components; quote a filename ending in punctuation.
        return re.sub(r'(?<=[^/.!])[.!]$', '', raw) if sentence_end else raw
    except ValueError:
        return None


def _without_idle_clause(text):
    """Drop a trailing idle clause. The flag is true when at least one was removed."""
    removed = False
    for _ in range(4):
        shorter = _COMPACT_NO_TASK.sub('', text)
        if shorter == text:
            return text, removed
        text = shorter
        removed = True
    return text, removed


def _without_courtesy(text):
    """Remove complete courtesy suffixes outside literals, never message text."""
    while True:
        match = re.search(r',?\s+(?:for\s+me|please|now)[.!]?$', text, re.I)
        if not match or not _outside_quotes(text, match.start()):
            return text
        text = text[:match.start()]


def _take_location(text, gap=' ', *, sentence_end=False):
    matches = [m for m in _LOCATION.finditer(text) if _outside_quotes(text, m.start())]
    if len(matches) > 1:
        return None, None
    if not matches:
        return text, None
    match = matches[0]
    raw = match['path'] or match['working']
    end = match.end()
    if match['path'] and not raw.startswith('"'):
        words = _spaced_words(text[end:])
        found = []
        for count in range(len(words), 0, -1):
            extension = ''.join(words[:count])
            stop = end + len(extension)
            candidate = _literal(raw + extension,
                                 sentence_end=sentence_end and stop==len(text.rstrip()))
            if candidate and _path_exists(candidate, directory=True):
                found.append((raw + extension, stop))
        if found:
            # The path as written, without the spaced words, is the other reading
            # when it exists too ("~/proj new tab" beside a folder "~/proj new").
            base = _literal(raw, sentence_end=sentence_end and end==len(text.rstrip()))
            if base and _path_exists(base, directory=True):
                found.append((raw, end))
            choice = _PATH_CHOICE.get()
            if choice is not None and len(found) > 1:
                choice['ambiguous'] = True
            raw, end = found[-1] if choice is not None and choice['shortest'] else found[0]
    directory = _literal(raw, sentence_end=sentence_end and end==len(text.rstrip())) if raw else 'here'
    if not directory:
        return None, None
    return text[:match.start()] + gap + text[end:], directory


def _quoted_payload(text):
    """Decode an explicit quoted argument, never silently discard its tail."""
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    if (not isinstance(value, str) or not value
            or not re.fullmatch(r'(?:exactly)?[.!]?', text[end:].strip(), re.I)):
        return None
    return value


# A whole payload inside one pair of single or typographic quotes: the quotes
# delimit it, as double quotes do, and are not sent. An apostrophe inside a
# word ("don't") is text. Anything else in single quotes stays literal.
_WRAPPED = re.compile(r"(?:'((?:[^']|(?<=\w)'(?=\w))+)'|\u2018([^\u2018\u2019]+)\u2019|\u201c([^\u201c\u201d]+)\u201d)[.!]?\s*\Z")


def _payload(text, *, closed=False):
    """A complete quoted argument has a boundary; an unquoted tail does not."""
    quoted = _quoted_payload(text)
    if quoted is not None:
        return quoted
    wrapped = _WRAPPED.match(text)
    if wrapped and next(filter(None, wrapped.groups())).strip():
        return next(filter(None, wrapped.groups()))
    if closed:
        return None
    if text.startswith('"'):
        # Preserve the existing literal colon form: '"words", please.'
        # Other text outside a quoted argument needs its own complete reading.
        try:
            value, end = json.JSONDecoder().raw_decode(text)
        except ValueError:
            return None
        if not isinstance(value, str) or not re.fullmatch(r',\s+please[.!]?', text[end:], re.I):
            return None
    return text


# A session named by its pane: "the Codex session in Kilix pane 9", "claude in
# pane 2", "existing agent pane 32 broker <id>", "pane 143". An agent or a noun
# may come first; with neither, the pane alone is the target.
_P_AGENT = r'(?P<agent>claude(?:\s+code)?|codex|grok)'
_P_NOUN = r'(?:coding[\s-]+agent(?:\s+session)?|coding\s+session|agent|session|instance)'
_P_TARGET = (r'(?:the\s+)?(?:(?:existing|running|current|live)\s+)?'
             r'(?:(?:' + _P_AGENT + r'(?:\s+' + _P_NOUN + r')?|' + _P_NOUN + r')\s+(?:(?:in|on|at)\s+)?)?'
             + PANE_SPAN)
_P_TELL = re.compile(r'(?:tell|message|ping|ask|instruct|send|relay|pass)'
                     r'(?:\s+(?:this|the|following|exact|message|text|instruction|a|along))*(?:\s+to)?\s+'
                     + _P_TARGET, re.I)
# The payload starts directly after the target: the message readers' markers.
_P_MARKER = re.compile(r'\s*,?\s*(?:with\s+this\s+exact\s+(?:text|message|instruction)\s*:'
                       r'|(?:with|saying|the\s+following(?:\s+instruction|\s+text|\s+message)?'
                       r'|this(?:\s+exact)?(?:\s+message|\s+text|\s+instruction)?)\s*:'
                       r'|(?P<named>(?:the|this)(?:\s+exact)?\s+(?:message|text|instruction))(?=\s+")'
                       r'|\.\s+message\s*:|:|(?<=\s)to(?=\s))\s*', re.I)
_P_WAIT = re.compile(r'(?:wait(?:\s+(?:for|until|on))?|block\s+until|hold(?:\s+(?:on|off))?\s+until|pause\s+until)\s+'
                     + _P_TARGET + r'(?P<tail>.*)', re.I | re.S)
_P_WAIT_TAILS = ('', 'to finish', 'finishes', 'is idle', 'is done', 'is finished', 'becomes idle', 'to be idle',
                 'to become idle', 'to finish its turn', 'finishes its turn', 'to finish its current turn',
                 'finishes its current turn', 'to complete its turn', 'completes its turn')


def _pane_args(found):
    """The target's arguments, or None when a broker id is not lowercase hex."""
    broker = found['broker'] or found['paren']
    if broker is not None and not _BROKER_ID.fullmatch(broker):
        return None
    args = {'agent': found['agent'].split()[0].casefold()} if found['agent'] else {}
    args['pane'] = found['pane']
    if broker:
        args['broker'] = broker
    return args


def pane_words(args):
    """A pane target in the grammar's plain wording: "codex in pane 9 with broker <id>"."""
    words = (args['agent'] if 'agent' in args else 'the session') + ' in pane ' + args['pane']
    return words + (' with broker ' + args['broker'] if 'broker' in args else '')


def _pane_calls(text, closed_payload=False):
    """(addressed, calls): whether the text messages or waits for a session
    named by its pane, and its reading (None when it has none). A request that
    addresses a pane is read only here."""
    told = _P_TELL.match(text)
    if told:
        args = _pane_args(told)
        rest = text[told.end():]
        marker = _P_MARKER.match(rest)
        if args is None or not marker:
            return True, None
        payload = rest[marker.end():]
        payload = _quoted_payload(payload) if marker['named'] else _payload(payload, closed=closed_payload)
        if not payload or len(payload.encode()) > 1024:
            return True, None
        return True, [_call('message_agent', **args, text=payload)]
    waiting = _P_WAIT.fullmatch(text)
    if waiting:
        args = _pane_args(waiting)
        tail = ' '.join(_without_courtesy(waiting['tail']).strip(' .!').casefold().split())
        if args is None or tail not in _P_WAIT_TAILS:
            return True, None
        return True, [_call('wait_for_agent', **args)]
    return False, None


def _extended_agent_calls(request, *, closed_payload=False, _meta=None):
    """Parse explicit fields without normalizing the literal task or message.

    _meta, when given, receives how a launch was said, for the scope rules
    in _read_parts: whether its directory was fronted ("in /w, start ..."),
    whether it states a location of its own ("here" too, which the call
    omits), whether its own directory or placement follows its model (the model is
    inside the launch), and whether a comma sets the model off.
    """
    text = request.strip()
    preamble = _PREAMBLE.match(text)
    polite = bool(re.search(r'\b(?:can|could|would|will)\s+you\b', preamble[0], re.I))
    text = text[preamble.end():]
    text = re.sub(r'^I\s+need\s+a\s+(?:new|fresh)\s+(?=(?:claude|codex)\b)',
                  'start a new ', text, flags=re.I)
    if re.match(r'^a new (?:claude|codex) (?:session|agent)\b',text,re.I):
        text='start '+re.sub(r',?\s+please[.!]?$', '', text, flags=re.I)
    # A final question mark belongs to the polite request only when no payload
    # is present. Payload punctuation is preserved below.
    if polite and ':' not in text and text.endswith('?'):
        text = text[:-1].rstrip()
    front = re.match(r'^(?:in|at|inside|within)\s+(?P<path>'+_DIRECTORY+r'),\s*', text, re.I)
    front_directory = None
    if front:
        front_directory = _literal(front['path'])
        if not front_directory:return None
        text = text[front.end():]
    head = re.match(r'^'+_LAUNCH_VERB+r'\s+'
                    r'(?:one\s+)?(?:(?:a|an|the|new|fresh)\s+)*(?P<agent>claude|codex)\b'
                    r'(?:\s+(?:coding\s+)?(?:client|session|workspace|agent|instance|worker))?'
                    r'(?:\s+(?:running|started))?', text, re.I)
    if head:
        rest = text[head.end():]
        named_session = bool(re.search(
            r'\bthe\b|\b(?:client|session|workspace|agent|instance|worker|running|started)\b',
            head[0], re.I))
        # "claude code" is the product name. "code" is not part of the codex name,
        # and a following noun is still the session, not a task.
        if head['agent'].casefold() == 'claude':
            coded = re.match(r'\s+code\b', rest, re.I)
            if coded:
                rest = rest[coded.end():]
                noun = _CLAUDE_CODE_NOUN.match(rest)
                if noun:
                    rest = rest[noun.end():]
                running = re.match(r'\s+(?:running|started)\b', rest, re.I)
                if running:
                    rest = rest[running.end():]
                named_session = named_session or bool(noun or running)
        # Owner decision 2026-11-07: a bare direction directly after the agent and
        # directly before an explicit path is its placement: "start codex left in /x".
        # "right now" and "right here" are not placements, and "start claude up"
        # is the phrasal verb, so "up" is never a bare placement. After "the" or a
        # session noun the word may describe that session ("the Codex session left
        # in ~/proj"), so the request is refused.
        bare = re.match(r'\s+(right|left|down)(?=\s+(?:in|at|inside|within)\s+(?:"|~?/))', rest, re.I)
        bare_place = None
        if bare:
            if named_session:
                return None
            bare_place = bare[1].casefold()
            rest = ' \0' + rest[bare.end():]
        # A contract model name may follow the agent directly: "start codex gpt-6".
        named = re.match(r'\s+('+'|'.join(map(re.escape, _MODELS['start_'+head['agent'].casefold()]))+r')(?![\w.-])',
                         rest, re.I)
        if named:
            rest = ' --model '+named[1]+rest[named.end():]
        task = None
        quoted = re.search(r'\s+with\s+(?:this\s+)?(?:initial\s+)?(?:task|instruction)\s+(?=")', rest, re.I)
        if quoted and _outside_quotes(rest, quoted.start()):
            task = _quoted_payload(rest[quoted.end():])
            if task is None:return None
            rest = rest[:quoted.start()]
        markers = re.compile(r';\s*(?:initial\s+)?(?:task|instruction):\s*|'
                             r'\s+with\s+(?:this\s+)?(?:initial\s+)?(?:task|instruction):\s*|'
                             r':\s*|\s+and\s+(?:ask\s+it\s+to|give\s+it\s+this\s+task:)\s*|'
                             r'\s+to\s+', re.I)
        for marker in markers.finditer(rest):
            if not _outside_quotes(rest, marker.start()):continue
            # "to the right" is placement, not an initial task.
            tail = rest[marker.end():]
            if marker[0].strip().casefold()=='to' and re.match(r'(?:the |my )?(?:right|left)\b', tail, re.I):continue
            if task is not None:return None
            task = _payload(tail, closed=closed_payload)
            if task is None:return None
            rest = rest[:marker.start()]
            break
        without_courtesy = _without_courtesy(rest)
        sentence_end = task is None and without_courtesy == rest
        rest = without_courtesy
        # Removed spans leave a NUL gap that no model phrase can cross.
        # Requests never contain NUL: exact_calls refuses control characters.
        rest, directory = _take_location(rest, gap=' \0 ', sentence_end=sentence_end)
        if rest is None:return None
        located = directory is not None
        if front_directory is not None:
            if directory is not None:return None
            directory = front_directory
        args = {} if directory in (None,'here') else {'directory':directory}
        if task is not None:
            if not task or len(task.encode())>1024:return None
            args['task']=task
        for place, pattern in _PLACEMENTS:
            matches=list(re.finditer(r'\b(?:'+pattern+r')\b',rest.replace('\0',' '),re.I))
            if matches:
                if len(matches)!=1 or 'place' in args:return None
                match=matches[0];args['place']=place
                rest=rest[:match.start()]+' \0 '+rest[match.end():]
        if bare_place:
            if 'place' in args:return None
            args['place']=bare_place
        model = _MODEL.search(rest)
        inside = comma = False
        if model:
            args['model']=model[1]
            # A removed directory or placement after the model is the launch's own.
            inside = '\0' in rest[model.end():]
            comma = rest[:model.start()].replace('\0',' ').rstrip().endswith(',')
            rest=rest[:model.start()]+' '+rest[model.end():]
        rest, idle = _without_idle_clause(rest)
        if idle and task is not None:
            return None
        if rest.replace('\0',' ').strip(' ,.!'):return None
        if _meta is not None:
            _meta.update(front=front_directory is not None, located=located, inside=inside,
                         comma_model=comma and not inside)
        return [_call('start_'+head['agent'].casefold(),**args)]

    # A fronted location scopes one explicit launch, never an implicit action.
    if front:return None

    addressed, paned = _pane_calls(text, closed_payload)
    if addressed:
        return paned

    # Messages require a named agent; no pronoun or unnamed-session fallback.
    front = re.match(r'^(?:for|to)\s+(?:the\s+)?(?:running\s+|existing\s+)?(?P<agent>claude|codex)(?:\s+session)?\s+(?P<location>.+?),\s*send(?:\s+this\s+message)?:\s*',text,re.I)
    if front:
        rest,directory=_take_location(front['location'])
        if rest is not None and not rest.strip() and directory is not None:
            payload=_payload(text[front.end():], closed=closed_payload)
            if payload and len(payload.encode())<=1024:
                args={'agent':front['agent'].casefold(),'text':payload}
                if directory!='here':args['directory']=directory
                return [_call('message_agent',**args)]
        return None
    message = re.match(r'^(?:tell|message|ask|instruct|give|send|relay|pass)(?:\s+(?:this|the|following|exact|message|text|instruction|a|along))*'
                       r'(?:\s+to)?\s+(?:the\s+)?(?:running\s+|current\s+|existing\s+)?'
                       r'(?P<agent>claude|codex)\b(?:\s+session)?',text,re.I)
    if message:
        rest=text[message.end():]
        delimiter=re.compile(r'\s+with\s+this\s+exact\s+(?:text|message|instruction):\s*|'
                             r'\s+(?:with|saying|the following(?: instruction| text| message)?|this(?: exact)?(?: message| text| instruction)?):\s*|'
                             r'\s+(?:the|this)(?: exact)? (?:message|text|instruction)\s+(?=")|'
                             r':\s*|\s+to\s+',re.I)
        for marker in delimiter.finditer(rest):
            if not _outside_quotes(rest,marker.start()):continue
            prefix,directory=_take_location(rest[:marker.start()])
            if prefix is None or prefix.strip(' ,'):continue
            payload=rest[marker.end():]
            if marker[0].rstrip().endswith(('message', 'text', 'instruction')):
                payload = _quoted_payload(payload)
            else:
                payload = _payload(payload, closed=closed_payload)
            if not payload or len(payload.encode())>1024:return None
            args={'agent':message['agent'].casefold(),'text':payload}
            if directory not in (None,'here'):args['directory']=directory
            return [_call('message_agent',**args)]
        return None

    text = re.sub(r'^await\s+(?:the\s+)?end\s+of\s+(?:the\s+)?(claude|codex)\s+(?:current\s+)?turn\b',
                  r'wait for \1', text, flags=re.I)
    waiting=re.fullmatch(r'(?:wait(?:\s+(?:for|until|on))?|block until|hold(?:\s+(?:on|off))? until|pause until|let)\s+'
                         r'(?:the\s+)?(?:current\s+|running\s+|existing\s+)?(?P<agent>claude|codex)\b'
                         r'(?P<rest>.*)',text,re.I)
    if waiting:
        rest=waiting['rest']
        guard = re.search(r";\s*(?:do not|don['’]t)\s+(?:start|launch|open|create)\s+"
                          r"(?:(?:a\s+)?new|another)\s+(?:session|agent)[.!]?$", rest, re.I)
        if guard and _outside_quotes(rest, guard.start()):
            rest = rest[:guard.start()]
        rest=re.sub(r'^\s+(?:session|turn)\b','',rest,flags=re.I)
        without_courtesy = _without_courtesy(rest)
        sentence_end = guard is None and without_courtesy == rest
        rest,directory=_take_location(without_courtesy, sentence_end=sentence_end)
        if rest is None:return None
        tail=' '.join(rest.strip(' .!').casefold().split())
        if tail not in ('','is idle','is done','is finished','becomes idle','reaches idle',
                        'finishes','finishes its turn','finishes its current turn','to finish',
                        'to finish its turn','to finish its current turn','to become idle','to be idle',
                        'to complete its turn','completes its turn','completes its current turn','finish before continuing',
                        'finish before we continue','ends'):
            return None
        args={'agent':waiting['agent'].casefold()}
        if directory not in (None,'here'):args['directory']=directory
        return [_call('wait_for_agent',**args)]
    return None


def _call(name, **args):
    return {"name":name, "arguments":args}


def _app_calls(request):
    text = _without_courtesy(request.strip())
    sentence_end = text == request.strip()
    session = r'(?:\s+(?:in|for)\s+this\s+session)?'
    desired = re.fullmatch(r"(?:I would like|I['’]d like|I need|I want)\s+(?:the\s+)?("+_NOUN+
                           r")\s+(?:opened|launched|started)(?:\s+now)?"+session+r'[.!]?', text, re.I)
    if desired:
        return [_call('open_app', app=_APP_WORDS[desired[1].casefold()])]
    preamble = _PREAMBLE.match(text)
    polite = bool(re.search(r'\b(?:can|could|would|will)\s+you\b', preamble[0], re.I))
    text = text[preamble.end():]
    if text.endswith('?'):
        if not polite:return None
        text = text[:-1].rstrip()
    without_courtesy = _without_courtesy(text)
    sentence_end = sentence_end and without_courtesy == text
    text = without_courtesy
    text = re.sub(r'^bring\s+(?:the\s+)?('+_NOUN+r')\s+up$',r'bring up \1',text,flags=re.I)
    noun = re.fullmatch(r'(?:open|launch|start(?: up)?|bring up|show|get)\s+(?:the\s+)?('+_NOUN+r')(?:\s+application|\s+running|\s+open)?'+session+r'[.!]?', text, re.I)
    if noun:
        return [_call('open_app', app=_APP_WORDS[noun[1].casefold()])]
    shapes = [
        r'(?:open|load|show|view)\s+(?P<path>{path})\s+(?:in|with|using)\s+(?:the\s+)?PDF (?:viewer|reader)[.!]?',
        r'(?:use\s+)?(?:the\s+)?PDF (?:viewer|reader)\s+to\s+(?:open|load|show)\s+(?P<path>{path})[.!]?',
        r'open\s+(?P<path>{path})[.!]?',
        r'(?:launch|bring up|start)\s+(?:the\s+)?PDF (?:viewer|reader)\s+(?:with|for|and (?:load|show))\s+(?P<path>{path})[.!]?',
        r'(?:get\s+)?(?:the\s+)?PDF (?:viewer|reader)\s+to\s+(?:display|open|show)\s+(?P<path>{path})[.!]?',
        r'(?:display|show)\s+(?P<path>{path})\s+(?:in|with|using)\s+(?:the\s+)?PDF (?:viewer|reader)[.!]?',
    ]
    # An unquoted PDF path may hold single spaces when that exact file exists
    # (owner decision 2026-11-07). Single-word paths are tried first and keep
    # their reading.
    spaced = r'~?/[^\s;?"]+(?: [^\s;?"]+)+'
    for path_pattern, is_spaced in ((_PATH, False), (spaced, True)):
        for shape in shapes:
            matched = re.fullmatch(r'(?:'+shape.format(path=path_pattern)+r')[.!]?',text,re.I)
            if not matched:continue
            raw = matched['path']
            path = _literal(raw, sentence_end=sentence_end and matched.end('path')==len(text))
            if is_spaced and not (isinstance(path,str) and _path_exists(path, directory=False)):
                return None
            if (not isinstance(path,str) or not path.startswith(('/','~/'))
                    or not path.casefold().endswith('.pdf')
                    or any(unicodedata.category(ch) in ('Cc','Cf','Zl','Zp') for ch in path)):
                return None
            return [_call('open_app',app='kilix-pdf',file_path=path)]
    return None


def _outside_quotes(text, position):
    quote = None
    escaped = False
    for index, ch in enumerate(text[:position]):
        if escaped:escaped=False;continue
        if ch == '\\' and quote:escaped=True;continue
        if quote:
            if ch == quote:quote=None
        elif ch in ('"', "'"):
            if ch == "'" and index and text[index-1].isalnum() and text[index+1:index+2].isalnum():
                continue
            quote=ch
    return quote is None


# ---------------------------------------------------------------------------
# Frames: more ways to say one action, read by slots (grown 2026-11-24).
#
# The readers above take each operation in a few fixed shapes. A frame reads
# the request's copyable parts first (quoted text, paths, a name after a word
# of place, a split direction, a model, the agent or application named), puts
# a token where each one was, and then requires what is left, the request's
# own words, to be one of a closed set of shapes for that operation. Every
# word outside the parts must be in the shape: a condition, a time, a
# negation, a question, a second agent or an unsupported verb is no reading.
#
# A frame never reads an unquoted message or task: a payload must be inside
# quotes, so its end is not a guess. Frames are tried only when the readers
# above read nothing, and a reading that uses one must also pass
# commands_suggest.refusal() on the whole request (see _read_calls).
# ---------------------------------------------------------------------------
_F_QUOTES = {'"': '"', "'": "'", '“': '”', '‘': '’'}
# A word of place directly before a quoted or bare name makes it a directory.
_F_PLACE_CUE = re.compile(r'(?:\b(?:in|inside|within|at)\s+(?:the\s+)?(?:(?:directory|folder|dir|repo|repository)\s+)?'
                          r'|\b(?:directory|dir|folder)\s+(?:called\s+|named\s+)?)$', re.I)
_F_PATH = re.compile(r'(?<![^\s(])((?:~|\.\.?)?/[^\s;,?"\'“”‘’]+)')
# A bare directory name: words joined by "-" or ".", starting with a letter
# ("pewter-anchor", "topaz-kiln.v7", "桜-工房"). A single plain word after
# "in" is not taken ("in parallel", "in order").
_F_BARE = r'[^\W\d_][\w]*(?:[-.][\w]+)+'
# Hyphenated words that say how, not where ("start codex in read-only"), and
# names that are a model or a place word.
_F_NOT_BARE = re.compile(r'(?:read|write)-only|full-auto|auto-(?:edit|approve)|dry-run|real-time|(?:full|split|half)-screen|'
                         r'side-by-side|follow-up|step-by-step|one-shot|non-interactive|(?:high|low)-priority|'
                         r'claude-code|(?:gpt|o\d|claude|sonnet|opus|haiku)\b.*|.*-(?:mode|only|split|pane|tab|window|screen)'
                         r'|e\.g|i\.e|etc|[ap]\.m|vs'
                         # a time is not a place: "in mid-december", "at half-past"
                         r'|(?:.*[-.])?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july'
                         r'|august|september|october|november|december|mon|tue|wed|thu|fri|sat|sun|(?:mon|tues|wednes|thurs|fri|satur|sun)day'
                         r'|today|tomorrow|tonight|yesterday|morning|afternoon|evening|night|noon|midnight|mid|half|quarter|past'
                         r'|minutes?|hours?|days?|weeks?|months?|years?|now|later|soon|eod)(?:[-.].*)?', re.I)
# Words of their own that a bare name never is: an application, a side.
_F_OWN_WORDS = re.compile(r'(?:right|left)-hand|kilix-(?:pdf|calculator|notepad|file)', re.I)
_F_LOC_INTRO = (r'(?:\b(?:working\s+)?(?:in|inside|within|at)\s+(?:the\s+)?(?:(?:directory|folder|dir|repo|repository)\s+)?'
                r'|\b(?:(?:working|the)\s+)?(?:directory|dir|folder)\s+(?:called\s+|named\s+)?)')
_F_LOC_NOUN = r'(?:\s+(?:directory|folder|dir|repo|repository))?'
_F_HERE = re.compile(r'\bin\s+(?:this|the\s+current|my\s+current)\s+(?:directory|folder)\b'
                     r'|(?:\b(?:working\s+)?(?:directory|folder)\s+)?\bhere\b(?![\'’])'
                     r'|\b(?:the\s+)?current\s+(?:working\s+)?(?:directory|folder)\b', re.I)
_F_SIDE = {'right': 'right', 'right-hand': 'right', 'left': 'left', 'left-hand': 'left', 'up': 'up', 'upper': 'up',
           'top': 'up', 'above': 'up', 'down': 'down', 'lower': 'down', 'bottom': 'down', 'below': 'down',
           'underneath': 'down', 'beneath': 'down'}
_F_PLACE = re.compile(
    r'(?:(?:and\s+)?put\s+it\s+)?(?:(?:in|into|as|using|use|with|on|open\s+it\s+in)\s+)?(?:(?:a|an|the)\s+)?(?:new\s+)?'
    r'(?:(?P<adj>right-hand|left-hand|right|left|up|down|upper|lower|top|bottom)\s+(?:split|pane)'
    r'|(?:split|pane)\s+(?:(?:to|on)\s+(?:the\s+|my\s+)?)?(?P<after>right|left|above|below|underneath|beneath|up|down)'
    r'(?:\s+(?:me|this\s+(?:pane|one)|the\s+current\s+(?:pane|one)))?)\b'
    r'|\b(?:on|to)\s+(?:the\s+|my\s+)(?P<side>right|left)\b'
    r'|\b(?P<tab>(?:in\s+)?(?:a\s+)?(?:(?:new|fresh)\s+)?tab)\b', re.I)
_F_MODEL = re.compile(r'(?:\b(?:with|using|on)\s+(?:the\s+)?model\s+|\b(?:and\s+)?(?:set|switch)\s+the\s+model\s+to\s+'
                      r'|\b(?:and\s+)?use\s+(?:the\s+)?model\s+|\bmodel\s+)(' + _MODEL_NAME + r')', re.I)
_F_AGENT = re.compile(r'\b(claude)(?:\s+code)?\b|\b(codex)\b', re.I)
_F_APP = re.compile(r'\b(?:kilix[\s-])?(?:(?P<pdf>pdf\s+(?:viewer|reader)|kilix-pdf(?:\s+(?:viewer|reader))?)'
                    r'|(?P<calc>calculator|kilix-calculator)|(?P<note>notepad|kilix-notepad)'
                    r'|(?P<file>file\s+(?:manager|browser)|kilix-file(?:\s+manager)?))'
                    r'(?:\s+(?:app|application|window))?\b', re.I)
_F_APPS = {'pdf': 'kilix-pdf', 'calc': 'kilix-calculator', 'note': 'kilix-notepad', 'file': 'kilix-file'}
_F_GERUND = {'starting': 'start', 'launching': 'launch', 'opening': 'open', 'waiting': 'wait', 'sending': 'send',
             'telling': 'tell', 'messaging': 'message', 'bringing': 'bring', 'firing': 'fire', 'spinning': 'spin',
             'kicking': 'kick', 'running': 'run', 'popping': 'pop', 'pulling': 'pull', 'relaying': 'relay',
             'passing': 'pass', 'forwarding': 'forward'}
_F_LEAD = re.compile(r'^(?:(?:hey|ok|okay|so|alright)\s*,?\s+)?(?:(?:first|then|also|next|and|after\s+that|afterwards)\s*,?\s+)?(?:go\s+ahead\s+and\s+)?'
                     r'(?:just\s+)?(?:please\s+)?(?:(?P<mind>would\s+you\s+mind\s+)|(?P<polite>(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:just\s+)?)'
                     r'|i\s+(?:want|need|would\s+like)\s+you\s+to\s+|i[\'’](?:d\s+like|ll\s+need)\s+you\s+to\s+)?'
                     r'(?:please\s+)?(?:just\s+)?(?:also\s+)?')
_F_TAIL = re.compile(r'(?:\s*,?\s+(?:please|thanks|thank\s+you|for\s+me|now|would\s+you|too))*\s*(?P<end>[.!?]*)$')

# Shapes over the tokenized words. \0a<agent>\0, \0d<n>\0 (directory), \0p<side>\0,
# \0m<n>\0 (model), \0t<n>\0 (quoted text), \0x<app>\0, \0f<n>\0 (a PDF path).
_A = r'\0a(?:claude|codex)\0'
_D = r'\0d\d+\0'
_T = r'\0t\d+\0'
_X = r'\0x[\w-]+\0'
_F_ART = r'(?:(?:a|an|the|one)\s+)?(?:(?:new|fresh)\s+)?'
_F_NOUN = r'(?:\s+(?:coding\s+)?(?:session|agent|instance|client|worker))?'
_F_LAUNCH_HEAD = (r'(?:(?:start(?:\s+up)?|launch|open(?:\s+up)?|run|begin|bring\s+up|fire\s+up|spin\s+up|kick\s+off|boot\s+up'
                  r'|give\s+me|get\s+me|put|create|i\s+need|i\s+want|i\s+would\s+like|i[\'’]d\s+like)\s+' + _F_ART + _A + _F_NOUN +
                  r'|(?:get|let[\'’]?s\s+get|have|i\s+need|i\s+want|i\s+would\s+like|i[\'’]d\s+like)\s+' + _F_ART + _A
                  + _F_NOUN + r'\s+(?:going|running|started|up\s+and\s+running)'
                  r'|(?:a\s+)?(?:new|fresh)\s+' + _A + _F_NOUN + r')')
_F_TASK = (r'(?:(?:(?:with|and\s+give\s+it|give\s+it|and\s+hand\s+it|hand\s+it|and)\s+)?(?:(?:the|a|this)\s+)?'
           r'(?:(?:first|initial)\s+)?task(?:\s*:)?\s*' + _T + r'|\.\s*task\s*:\s*' + _T + r')')
_F_SLOT = r'(?:' + _D + r'|\0p\w+\0|\0m\d+\0|' + _F_TASK + r')'
_F_SEP = r'(?:\s*,?\s*(?:please|for\s+me)\s*,?\s*|\s*,\s*(?:and\s+)?|\s+and\s+|\s*)'
_F_LAUNCH = re.compile(_F_LAUNCH_HEAD + r'(?:' + _F_SEP + _F_SLOT + r')*')
_F_APP_OPEN = r'(?:open(?:\s+up)?|launch|start(?:\s+up)?|run|bring\s+up|fire\s+up|pull\s+up|pop\s+open|get\s+me|get|show)'
_F_ONE_APP = r'(?:(?:the|a|an)\s+)?' + _X
_F_APP_LIST = _F_ONE_APP + r'(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+)' + _F_ONE_APP + r')*'
_F_APP_SHAPES = re.compile(
    _F_APP_OPEN + r'\s+' + _F_APP_LIST +
    r'|(?:pull|bring|get)\s+' + _F_ONE_APP + r'\s+up'
    r'|(?:i\s+need\s+)?' + _F_ONE_APP + r'\s*,?\s*(?:please\s+)?open\s+it')
_F_PDF_SHAPES = re.compile(
    r'(?:open|load|view|display|show|pull\s+up)\s+\0f\d+\0\s+(?:in|with|using|into|on)\s+' + _F_ONE_APP +
    r'|(?:open(?:\s+up)?|launch|start|pull\s+up|bring\s+up|fire\s+up)\s+' + _F_ONE_APP + r'\s+(?:with|on|for)\s+\0f\d+\0'
    r'|open\s+this\s+pdf\s+in\s+' + _F_ONE_APP + r'\s*:\s*\0f\d+\0')
_F_WAIT_OPEN = (r'(?:wait(?:\s+it\s+out)?(?:\s+(?:for|on|till|until))?|(?:pause|block|hold(?:\s+on|\s+off)?|hang\s+(?:on|tight)'
                r'|sit\s+tight|stand\s+by)\s+(?:until|till)|stand\s+by\s+for)')
_F_WAIT_SUBJ = (r'(?:(?:the|my)\s+)?(?:(?:current|active|running|existing)\s+)?' + _A + r'(?:[\'’]s)?'
                r'(?:\s+(?:session|agent|instance))?(?:\s+(?:(?:current|active)\s+)?turn)?')
_F_DESC = (r'(?:\s*(?:,|—|-)?\s*(?:the\s+one\s+)?(?:(?:that[\'’]?s|that\s+is|who[\'’]?s|which\s+is)\s+)?(?:already\s+)?'
           r'(?:(?:working|running|over)\s+)?' + _D + r'(?:\s*(?:,|—|-))?)')
_F_END = (r'(?:\s+(?:until\s+it\s+|to\s+|is\s+|has\s+)?(?:be\s+)?(?:finish(?:es|ed)?(?:\s+(?:up|responding|replying|working))?|complete[sd]?|end(?:s|ed)?'
          r'|done|finished|over|idle|wrap(?:s|ped)?\s+up)(?:\s+(?:with\s+)?(?:its|the)\s+(?:(?:current|active)\s+)?turn)?)')
_F_WAIT = re.compile(_F_WAIT_OPEN + r'\s+' + _F_WAIT_SUBJ + r'(?:' + _F_DESC + r'?' + _F_END + r'?|' + _F_END + _F_DESC + r')')
_F_AR = (r'(?:the\s+)?(?:(?:running|current|existing)\s+)?' + _A + r'(?:\s+(?:session|agent|instance))?'
         r'(?:\s+(?:(?:that[\'’]?s|that\s+is|who[\'’]?s)\s+)?(?:already\s+)?(?:(?:working|running|over)\s+)?' + _D + r')?')
_F_INTRO = (r'(?:exactly\s+this\s*:|exactly\s*:|exactly|verbatim\s*,?|with|saying\s*:?|this\s*:|:|text\s*:|'
            r'(?:the|this|a)\s+(?:message|text|note)(?:\s+saying)?\s*:?|the\s+following\s*:?)')
_F_MESSAGE = re.compile(
    r'(?:tell|message|ping|text|ask|send|write\s+to|instruct)\s+' + _F_AR + r'\s*,?\s*(?:' + _F_INTRO + r'\s*)?' + _T
    + r'(?:\s*' + _D + r')?(?:\s*,?\s*(?:verbatim|word\s+for\s+word|exactly))?'
    r'|(?:say|send|relay|forward|pass|write)\s+(?:(?:the\s+(?:text|message|following)|this\s+message)\s+)?' + _T +
    r'\s+(?:along\s+)?to\s+' + _F_AR + r'(?:\s*,?\s*(?:verbatim|word\s+for\s+word|exactly))?'
    r'|let\s+' + _F_AR + r'\s+know\s*:?\s*' + _T +
    r'|shoot\s+' + _F_AR + r'\s+a\s+(?:message|note)(?:\s+saying)?\s*:?\s*' + _T +
    r'|(?:(?:relay|forward|pass|send)\s+(?:this|the\s+following)(?:\s+(?:note|text|message))?(?:\s+along)?|have\s+this\s+sent)\s+to\s+'
    + _F_AR + r'\s*(?::|—|-)?\s*' + _T +
    r'|' + _D + r'\s*,\s*(?:send|tell|message)\s+' + _A + r'(?:\s+this)?\s*:?\s*' + _T +
    r'|to\s+' + _F_AR + r'\s*,\s*send(?:\s+this)?\s*:?\s*' + _T +
    r'|(?:send|pass)\s+' + _A + r'\s+(?:this|the)\s+(?:message|text|note)\s+' + _D + r'\s*:?\s*' + _T)


# A pane target in a frame (grammar-only, see GRAMMAR_ONLY): \0n<n>\0 stands for
# "in Kilix pane 9 broker <id>", and the message and wait shapes take it where
# they take an agent ("tell the codex session in pane 9 'x'", "wait for pane 9
# to finish"). Built from the shapes above, so they stay one set of words.
_F_PANE = re.compile(r'(?:\b(?:in|on|at)\s+)?' + PANE_SPAN, re.I)
_N = r'\0n\d+\0'
_F_PANE_REF = (r'(?:the\s+)?(?:(?:running|current|existing)\s+)?'
               r'(?:' + _A + r'(?:\s+(?:session|agent|instance))?\s*|(?:coding\s+)?(?:session|agent|instance)\s*)?' + _N)
_F_MESSAGE_PANE = re.compile(_F_MESSAGE.pattern.replace(_F_AR, r'(?:' + _F_PANE_REF + r')'))
_F_WAIT_PANE = re.compile(_F_WAIT.pattern.replace(_F_WAIT_SUBJ, r'(?:' + _F_PANE_REF + r'(?:\s+(?:(?:current|active)\s+)?turn)?)'))


def _frame_pane(folded, tokens, values, agents, kinds, meta):
    """(calls, meta) for a frame whose target is a pane, or None."""
    if (kinds.count('n') != 1 or len(agents) > 1 or 'grok' in agents
            or {'d', 'x', 'f', 'm', 'p'} & set(kinds)):
        return None     # a pane and a directory, two panes, or a launch's words
    pane, broker = values[int(next(value for kind, value in tokens if kind == 'n'))]
    if broker is not None and not _BROKER_ID.fullmatch(broker):
        return None
    args = {'agent': agents[0]} if agents else {}
    args['pane'] = pane
    if broker:
        args['broker'] = broker
    if 't' not in kinds and _F_WAIT_PANE.fullmatch(folded):
        return [_call('wait_for_agent', **args)], meta
    if 't' in kinds and _F_MESSAGE_PANE.fullmatch(folded):
        message = next(values[int(value)] for kind, value in tokens if kind == 't')
        if len(message.encode()) > 1024:
            return None
        return [_call('message_agent', **args, text=message)], meta
    return None


def _f_quotes(text):
    """[(start, end, inner)] for each outermost quoted span, or None when a
    quote mark is left open. An apostrophe inside a word is not a quote."""
    found, at = [], 0
    while at < len(text):
        mark = text[at]
        close = _F_QUOTES.get(mark)
        if mark in '”’' and not (mark == '’' and at and text[at - 1].isalnum()):
            return None     # a closing mark with no opening one
        if not close or (mark == "'" and at and text[at - 1].isalnum()):
            at += 1
            continue
        end = at + 1
        while True:
            end = text.find(close, end)
            if end < 0:
                return None
            if close in "'’" and text[end + 1:end + 2].isalnum():
                end += 1
                continue
            break
        found.append((at, end + 1, text[at + 1:end]))
        at = end + 1
    return found


def _frame_calls(clause):
    """(calls, meta) for one clause read by a frame, or None."""
    text = clause.strip()
    quotes = _f_quotes(text)
    if quotes is None:
        return None
    values = []

    def token(kind, value):
        values.append(value)
        return f' \0{kind}{len(values) - 1}\0 '

    # 1. Quoted spans: a directory after a word of place, a PDF, or text.
    words, at = '', 0
    for start, end, inner in quotes:
        if not inner.strip() or '\\' in inner:
            return None     # escapes inside quotes are not guessed at
        words += text[at:start]
        kind = 'L' if _F_PLACE_CUE.search(words) else 'f' if inner.casefold().endswith('.pdf') else 't'
        words += token(kind, inner)
        at = end
    words += text[at:]

    # 2. Unquoted paths. A final full stop or "!" is the sentence's, and is
    # dropped only at the end of the clause. A path that a following word could
    # extend to an existing spaced path is left to the readers above.
    refused = []

    def unquoted(found):
        raw, rest = found.group(1), found.string[found.end(1):]
        if raw.endswith(')') and '(' not in raw:
            refused.append(raw)     # "(in ~/x)": the bracket is not the path's
        colon = raw.endswith(':')
        raw = raw[:-1] if colon else raw
        if raw[-1:] in ('.', '!'):
            if rest.strip():
                refused.append(raw)
            raw = raw.rstrip('.!')
        spaced = _spaced_words(rest)
        if (raw in ('', '/', '~', '~/', '.', './', '..', '../')
                or any(_path_exists(raw + ''.join(spaced[:count]), directory=directory)
                       for count in range(1, len(spaced) + 1) for directory in (True, False))):
            refused.append(raw)
        return token('f' if raw.casefold().endswith('.pdf') else 'L', raw) + (':' if colon else '')
    words = _F_PATH.sub(unquoted, words)

    # 2b. A pane target, before a word of place can take "in pane".
    def paned(found):
        values.append((found['pane'], found['broker'] or found['paren']))
        return f' \0n{len(values) - 1}\0 '
    words = _F_PANE.sub(paned, words)

    # 3. Directories: a path or bare name after a word of place, or "here".
    def located(found):
        if found.groupdict().get('index') is not None:
            return token('d', values[int(found.group('index'))])
        if _F_OWN_WORDS.fullmatch(found['bare']):
            return found[0]
        if _F_NOT_BARE.fullmatch(found['bare']):
            refused.append(found.group('bare'))
        return token('d', found.group('bare'))
    words = re.sub(_F_LOC_INTRO + r'(?:\0L(?P<index>\d+)\0|(?P<bare>' + _F_BARE + r'))' + _F_LOC_NOUN + r'(?![\w-])',
                   located, words, flags=re.I)
    words = re.sub(r'\bwith\s+\0L(?P<index>\d+)\0\s+as\s+(?:its|the)\s+working\s+directory\b',
                   located, words, flags=re.I)
    # "a codex tab for ../x": "for" places only a written path, never a bare name.
    words = re.sub(r'\bfor\s+\0L(?P<index>\d+)\0', located, words, flags=re.I)
    if refused or re.search(r'\0L\d+\0', words):
        return None
    words = _F_HERE.sub(lambda found: token('d', 'here'), words)

    # 4. Placement, model, agent and application.
    def placed(found):
        if found.group('tab'):
            return ' \0ptab\0 '
        side = found.group('adj') or found.group('after') or found.group('side')
        return f' \0p{_F_SIDE[side.casefold()]}\0 '
    words = _F_PLACE.sub(placed, words)
    words = _F_MODEL.sub(lambda found: token('m', found.group(1)), words)
    words = _F_AGENT.sub(lambda found: f" \0a{(found.group(1) or found.group(2)).casefold()}\0 ", words)
    words = _F_APP.sub(lambda found: f" \0x{next(_F_APPS[key] for key in _F_APPS if found.group(key))}\0 ", words)

    # 5. The words left: one space apart, no space before punctuation.
    words = ' '.join(words.split())
    words = re.sub(r'\s+([,:.!?]|[\'’]s\b)', r'\1', words)
    lead = _F_LEAD.match(words.casefold())
    polite = bool(lead['polite'] or lead['mind'])
    body = words[lead.end():]
    if lead['mind']:
        verb = re.match(r'\w+', body)
        if not verb or verb[0].casefold() not in _F_GERUND:
            return None
        body = _F_GERUND[verb[0].casefold()] + body[verb.end():]
    tail = _F_TAIL.search(body)
    end, body = tail['end'], body[:tail.start()].strip()
    if len(end) > 1 or end == '?' and not polite:
        return None
    folded = body.casefold()
    tokens = re.findall(r'\0([a-z])([\w-]+)\0', body)
    kinds = [kind for kind, _ in tokens]
    if any(kinds.count(kind) > 1 for kind in 'dpmtf'):
        return None
    agents = [value for kind, value in tokens if kind == 'a']
    args, meta = {}, {'frame': True}
    for kind, value in tokens:
        if kind == 'd' and values[int(value)] != 'here':
            args['directory'] = values[int(value)]
    if 'n' in kinds:
        return _frame_pane(folded, tokens, values, agents, kinds, meta)
    if len(agents) == 1 and not {'x', 'f'} & set(kinds) and _F_LAUNCH.fullmatch(folded):
        for kind, value in tokens:
            if kind == 'p':
                args['place'] = value
            elif kind in 'mt':
                args['model' if kind == 'm' else 'task'] = values[int(value)]
        order = ''.join(kinds)
        inside = 'm' in order and bool({'d', 'p'} & set(order[order.find('m'):]))
        comma = bool(re.search(r',\s*\0m', body))
        meta.update(located='d' in kinds, inside=inside, comma_model=comma and not inside, front=False)
        if len(args.get('task', '').encode()) > 1024:
            return None
        return [_call('start_' + agents[0], **args)], meta
    if not agents and not {'d', 'p', 'm', 't'} & set(kinds):
        if 'f' not in kinds and _F_APP_SHAPES.fullmatch(folded):
            return [_call('open_app', app=value) for kind, value in tokens if kind == 'x'], meta
        if 'f' in kinds and _F_PDF_SHAPES.fullmatch(folded):
            named = [value for kind, value in tokens if kind == 'x']
            path = next(values[int(value)] for kind, value in tokens if kind == 'f')
            if named != ['kilix-pdf'] or not path.startswith(('/', '~/')) or len(path.encode()) > 4096:
                return None
            return [_call('open_app', app='kilix-pdf', file_path=path)], meta
        return None
    if len(agents) != 1 or {'x', 'f', 'm', 'p'} & set(kinds):
        return None
    if 't' not in kinds and _F_WAIT.fullmatch(folded):
        return [_call('wait_for_agent', agent=agents[0], **args)], meta
    if 't' in kinds and _F_MESSAGE.fullmatch(folded):
        message = next(values[int(value)] for kind, value in tokens if kind == 't')
        if len(message.encode()) > 1024:
            return None
        return [_call('message_agent', agent=agents[0], text=message, **args)], meta
    return None


def exact_calls(request, *, _depth=0, _bare=False):
    """A complete reading of supported actions, or None; no partial match.

    _bare: a launch earlier in the whole request states no model.
    """
    if _PATH_CHOICE.get() is not None:
        return _read_calls(request, _depth=_depth, _bare=_bare)
    # An unquoted spaced path takes its longest existing reading. When a shorter
    # existing path also reads the whole request, and the two readings differ,
    # the request is ambiguous and is refused; a reading only one of them
    # completes is that reading.
    choice = {'shortest': False, 'ambiguous': False}
    token = _PATH_CHOICE.set(choice)
    try:
        longest = _read_calls(request, _depth=_depth, _bare=_bare)
        if not choice['ambiguous']:
            return longest
        choice['shortest'] = True
        shortest = _read_calls(request, _depth=_depth, _bare=_bare)
    finally:
        _PATH_CHOICE.reset(token)
    if longest is None or shortest is None:
        return longest or shortest
    return longest if _canonical(longest) == _canonical(shortest) else None


def _read_calls(request, *, _depth=0, _bare=False):
    parts = _read_parts(request, _depth=_depth, _bare=_bare)
    if not parts and _depth == 0:
        listed = _enumerated(request)
        if listed:
            steps, count = listed
            parts = _read_parts(steps)
            if not parts or count is not None and len(parts) != count:
                return None
            parts = [(call, {**m, 'frame': True}) for call, m in parts]
    if not parts:
        return None
    calls = [call for call, _meta in parts]
    if _depth == 0 and any(meta.get('frame') for _call_, meta in parts):
        # A frame reads words by slots; the whole request must also pass the
        # rules every suggested plan passes: no word the tools cannot express,
        # and nothing it names left unused. Imported here: commands_suggest
        # imports this module.
        import commands_suggest
        if commands_suggest.refusal(request, calls) is not None or _two_sessions(calls):
            return None
    if _depth == 0 and _pane_mixed(calls):
        return None
    return calls


def _pane_mixed(calls):
    """Whether a session named by its pane shares the request with a wait or
    message naming an agent without a pane: in "tell claude in pane 2: x;
    then wait for claude" the second claude may be the one in pane 2."""
    paned = [call['arguments'] for call in calls if 'pane' in call['arguments']]
    return bool(paned) and any(
        call['name'] in ('wait_for_agent', 'message_agent') and 'pane' not in call['arguments']
        and any(args.get('agent', call['arguments']['agent']) == call['arguments']['agent'] for args in paned)
        for call in calls)


def _two_sessions(calls):
    """Whether a wait or message names an agent that an earlier step has in
    another directory: "start claude here and claude in /b, then tell the
    claude ..." leaves open which session is meant."""
    def where(call):
        if 'pane' in call['arguments']:
            return 'pane ' + call['arguments']['pane']
        return call['arguments'].get('directory', 'here')
    def agent(call):
        return call['name'].removeprefix('start_') if call['name'].startswith('start_') else call['arguments'].get('agent')
    # A pane names exactly one session; an earlier pane step still makes a
    # later "claude" ambiguous.
    return any(call['name'] in ('wait_for_agent', 'message_agent') and 'pane' not in call['arguments']
               and any(agent(earlier) == agent(call) and where(earlier) != where(call) for earlier in calls[:index])
               for index, call in enumerate(calls))


# A separator that ends a sentence or a step (";", "then") rather than joining
# a list ("," or "and"). A verbless launch after it is its own request, and a
# model set off by a comma after it cannot be shared with launches before it.
def _sequence(separator):
    return ';' in separator or separator.strip(' ,').casefold() == 'then'


# A right side that opens a new clause of its own: a request preamble
# ("please", "can you", "I'd like to") or "I need a new ...".
_OWN_CLAUSE = re.compile(r'(?:please|(?:can|could|would|will)\s+you|I\s+(?:want|need|would\s+like)|I[\'’]d\s+like)\b', re.I)
_GROUP_FIELDS = frozenset({'directory', 'place'})
_AGENT_CALLS = ('start_claude', 'start_codex', 'wait_for_agent', 'message_agent')


def _leaf(calls, meta=None):
    meta = meta or {}
    return [(call, {'shares': False, **meta}) for call in calls]


def _read_parts(request, *, _depth=0, _bare=False, _list_bare=False):
    """The reading as (call, meta) parts. meta['shares']: the call shares the
    verb of the call before it ("start codex and claude"); the other keys come
    from _extended_agent_calls.

    _bare: a launch earlier in the whole request states no model.
    _list_bare: a launch earlier in the same comma or "and" list states no model.
    """
    if (not isinstance(request,str) or not request.strip() or len(request)>2048 or _depth>3
            or any(unicodedata.category(ch) in ('Cc','Cf','Zl','Zp') for ch in request)):
        return None
    meta = {}
    found = _extended_agent_calls(request, _meta=meta) or _app_calls(request)
    # Fully matched actions without an open payload cannot swallow a clause.
    if found and (not any('task' in c['arguments'] or 'text' in c['arguments'] for c in found)
                  or _extended_agent_calls(request, closed_payload=True)):
        return _leaf(found, meta if len(found) == 1 else None)
    framed = None if found else _frame_calls(request)
    if framed:
        return _leaf(*framed)
    pair=re.fullmatch(r'give me\s+(claude|codex)\s+in\s+('+_DIRECTORY+r')\s+plus\s+(claude|codex)\s+in\s+('+_DIRECTORY+r')',request,re.I)
    if pair:
        first,second=_literal(pair[2]),_literal(pair[4], sentence_end=True)
        if first and second:
            return [(_call('start_'+pair[1].casefold(),directory=first), {'shares': False}),
                    (_call('start_'+pair[3].casefold(),directory=second), {'shares': True})]
    for separator in _SEPARATOR.finditer(request):
        if not _outside_quotes(request,separator.start()):continue
        left,right=request[:separator.start()].strip(),request[separator.end():].strip()
        left_meta = {}
        first=_extended_agent_calls(left, _meta=left_meta) or _app_calls(left)
        if not first:
            framed = _frame_calls(left)
            if not framed:continue
            first, left_meta = framed[0], dict(framed[1])
        word = separator[0].strip(' ,').casefold()
        if (word=='and'
                and all(c['name'].startswith('start_') for c in first)
                and re.match(r'^(?:ask it to|give it this task:)\s+',right,re.I)):
            continue  # A task delimiter belonging to this launch, not a second action.
        if not left_meta.get('frame') and any('task' in x['arguments'] or 'text' in x['arguments'] for x in first):
            if not _extended_agent_calls(left, closed_payload=True):
                # A bare "wait" names no session and is not a Kilix action.
                # Keep it in literal messages such as 'say "ready" and wait'.
                # A named/qualified wait still follows the ambiguity guard.
                if re.fullmatch(r'wait[.!]?', right, re.I):
                    continue
                # With an unquoted payload, an apparent second instruction
                # could be data or a new action. Neither interpretation wins.
                if re.match(r'^(?:(?:please|then|next|also)\s+)*(?:'
                            r'open|launch|start|bring|show|get|wait|tell|send|message|ask|'
                            r'kick|run|create|delete|erase|remove|close|quit|reboot|'
                            r'do not|don[\x27’]t|if|unless)\b',right,re.I):
                    return None
                continue
        launches = all(c['name'].startswith('start_') for c in first)
        left_bare = any(x['name'].startswith('start_') and 'model' not in x['arguments'] for x in first)
        bare = _bare or left_bare
        sequence = _sequence(separator[0])
        list_bare = False if sequence else (_list_bare or left_bare)
        rest=_read_parts(right,_depth=_depth+1,_bare=bare,_list_bare=list_bare)
        shared_verb = (word in ('and', 'and then') and launches
                       and re.match(r'^(?:(?:a|an|the|new|fresh)\s+)*(?:claude|codex)\b',right,re.I))
        if (not rest and word in ('and', 'and then') and launches
                and re.match(r'^(?:claude|codex)\b', right, re.I)):
            rest=_read_parts('start '+right,_depth=_depth+1,_bare=bare,_list_bare=list_bare)
        if (not rest and word in ('and', 'then', 'and then') and all(c['name'] == 'open_app' for c in first)
                and _F_APP.match(re.sub(r'^(?:the|a|an)\s+', '', right, flags=re.I))):
            # "open the notepad, then the calculator": the verb is shared.
            rest=_read_parts('open '+right,_depth=_depth+1,_bare=bare,_list_bare=list_bare)
        if not rest:
            referred = _referred(right, first)
            if referred:
                rest = _read_parts(referred,_depth=_depth+1,_bare=bare,_list_bare=list_bare)
                quoted = [inner for _a, _b, inner in _f_quotes(right) or ()]
                if rest and all(value in quoted for call, _m in rest for key, value in call['arguments'].items()
                                if key in ('task', 'text')):
                    rest = [(call, {**m, 'frame': True}) for call, m in rest]
                else:
                    rest = None     # as in a frame, a payload after "it" must be quoted
        if not rest or len(first)+len(rest)>4:
            return None
        rest = [(rest[0][0], {**rest[0][1], 'shares': bool(shared_verb)})] + rest[1:]
        lead = dict(first[0]['arguments'])
        group = 0       # how many calls of rest share the left launch's verb
        shared = False  # a directory reached the group by sharing
        if shared_verb:
            group = 1
            while group < len(rest) and rest[group][1]['shares']:
                group += 1
            # A field the left launch does not state may be meant for every
            # launch that shares the verb. Fields stated on both sides are
            # each launch's own.
            explicit_fields = set(lead)
            if any(_outside_quotes(left, m.start()) for m in _LOCATION.finditer(left)):
                explicit_fields.add('directory')  # An explicit "here" omits the call's default.
            members = [call['arguments'] for call, _meta in rest[:group]]
            extra = set().union(*(set(args) - explicit_fields for args in members))
            if not extra:
                pass
            elif (not explicit_fields and word == 'and' and members[0]
                    and set(members[0]) <= _GROUP_FIELDS
                    and all(args == members[0] for args in members)
                    and not re.search(r'\bas\s+its\s+working\s+directory\b', right, re.I)):
                # One directory or placement after a list of launches that share
                # a verb and state nothing else applies to all of them:
                # "start codex and claude in /w".
                lead = dict(members[0])
                shared = 'directory' in lead
            elif extra == {'model'} and all(
                    m.get('inside') for call, m in rest[:group] if 'model' in call['arguments']):
                # A model followed by the launch's own directory or placement,
                # of a kind the left launch also states, is that launch's own:
                # "start codex in /a and codex using gpt-6 in /b".
                pass
            else:
                return None
        if left_meta.get('front'):
            # "in /w, start codex and claude": a fronted directory scopes the
            # launches that share its verb when they state nothing themselves,
            # not even "here". Any other agent action after it must state its
            # own directory, since the fronted one may be meant for it too.
            for index, (call, m) in enumerate(rest):
                if call['name'] not in _AGENT_CALLS:
                    continue
                if index < group:
                    if word != 'and' or call['arguments'] or m.get('located'):
                        return None
                elif 'directory' not in call['arguments']:
                    return None
        if shared and any(call['name'] in ('wait_for_agent', 'message_agent')
                          and 'directory' not in call['arguments'] for call, _m in rest[group:]):
            # "start codex and claude in /w and wait for codex": the wait may be
            # meant for the codex just started in /w, as after a fronted one.
            return None
        first = [_call(first[0]['name'], **lead)] + first[1:]
        # Retain the integration base's model-scope guard across nested splits.
        head_call, head_meta = rest[0]
        own_clause = (sequence or re.match(_LAUNCH_VERB+r'\s',right[_PREAMBLE.match(right).end():],re.I)
                      or _OWN_CLAUSE.match(right))
        if (bare and not own_clause and 'model' in head_call['arguments']
                and not head_meta.get('inside')):
            return None
        # "start codex in /a, start codex in /b, model gpt-6": a model set off by
        # a comma at the end of a list may be meant for every launch in it.
        if list_bare and head_meta.get('comma_model') and 'model' in head_call['arguments']:
            return None
        if left_meta.get('front') and group:
            rest = [(_call(call['name'], **{**call['arguments'], 'directory': lead['directory']}), m)
                    if index < group else (call, m) for index, (call, m) in enumerate(rest)]
        return _leaf(first, left_meta) + rest
    return _leaf(found, meta if len(found) == 1 else None) if found else None


def _referred(right, first):
    """The clause after a single call with "it" or "there" written out: "it"
    as the session that call waited for or messaged, "there" as its
    directory. None when the clause refers to nothing that way."""
    if len(first) != 1:
        return None
    call, args = first[0], first[0]['arguments']
    where = ' in ' + json.dumps(args['directory'], ensure_ascii=False) if 'directory' in args else ''
    if call['name'] in ('wait_for_agent', 'message_agent'):
        it = re.match(r'((?:(?:then|also|after\s+that)\s*,?\s+)?(?:tell|message|send|ping|wait\s+(?:for|on|till|until)))'
                      r'\s+it\b', right, re.I)
        if it:
            target = pane_words(args) if 'pane' in args else args['agent'] + where
            return it[1] + ' ' + target + right[it.end():]
    there = [m for m in re.finditer(r'\bthere\b', right, re.I) if _outside_quotes(right, m.start())]
    if (len(there) == 1 and where
            and not re.match(r'\s*(?:is|are|was|were)\b|[\'’]s\b', right[there[0].end():], re.I)):
        return right[:there[0].start()] + where.strip() + right[there[0].end():]
    return None


_ORDINALS = ('first', 'second', 'third', 'fourth')
_COUNT_WORDS = {'two': 2, 'three': 3, 'four': 4}


def _enumerated(request):
    """(steps joined by "; then ", count or None) for a request that lists its
    steps as "first ..., second ...", "1) ... 2) ..." or "Three things: ...".
    The markers must run in order from the start, each opening a clause."""
    text = request.strip()
    count = None
    head = re.match(r'(two|three|four)\s+things\s*:\s*', text, re.I)
    if head:
        count, text = _COUNT_WORDS[head[1].casefold()], text[head.end():]
    marks = [m for m in re.finditer(r'(?:^|(?<=[,;])\s*|\s+)(?:and\s+)?(?:(first|second|third|fourth)\b\s*,?|([1-4])\))\s*',
                                    text, re.I) if _outside_quotes(text, m.start())]
    marks = [m for m in marks if m.start() == 0 or text[:m.start()].rstrip().endswith((',', ';')) or m[2]]
    if len(marks) < 2:
        return (text, count) if head else None
    numbered = [m[2] for m in marks]
    if any(numbered) and not all(numbered):
        return None
    wanted = [str(n) for n in range(1, len(marks) + 1)] if all(numbered) else list(_ORDINALS[:len(marks)])
    if [(m[1] or m[2]).casefold() for m in marks] != wanted or marks[0].start() != 0:
        return None
    steps = [text[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(text))].strip().rstrip(',;').strip()
             for i, m in enumerate(marks)]
    if not all(steps):
        return None
    return '; then '.join(steps), count


def _canonical(calls, cwd=None, *, published=False):
    """The calls with host-owned defaults filled in. published: hold them to
    commands-tools.json alone, without the grammar-only pane arguments."""
    if not isinstance(calls,list) or not 1<=len(calls)<=4:
        raise ValueError('calls must contain one to four operations')
    out=[]
    for call in calls:
        if not isinstance(call,dict) or set(call)!={'name','arguments'}:
            raise ValueError('invalid call object')
        name,args=call['name'],call['arguments']
        if not isinstance(name,str) or name not in _PROPERTIES or not isinstance(args,dict):
            raise ValueError('unknown operation or arguments')
        extension=frozenset() if published else GRAMMAR_ONLY.get(name,frozenset())
        required=_REQUIRED.get(name,set())-({'agent'} if 'pane' in args and 'pane' in extension else set())
        if (set(args)-_PROPERTIES[name]-extension or required-set(args)
                or any(not isinstance(v,str) or not v for v in args.values())):
            raise ValueError('unexpected, missing or non-string arguments')
        if any(unicodedata.category(ch) in ('Cc','Cf','Zl','Zp') for value in args.values() for ch in value):
            raise ValueError('control characters are not command arguments')
        args=dict(args)
        if name=='open_app':
            if args['app'] not in _APPS or ('file_path' in args and args['app']!='kilix-pdf'):
                raise ValueError('unknown application or incompatible file path')
        elif 'pane' in args:
            if ('directory' in args or not _PANE_NUMBER.fullmatch(args['pane'])
                    or 'broker' in args and not _BROKER_ID.fullmatch(args['broker'])
                    or args.get('agent','claude') not in PANE_AGENTS):
                raise ValueError('invalid pane target')
        elif 'broker' in args:
            raise ValueError('a broker id belongs to a pane target')
        else:
            args.setdefault('directory','here')
            if args['directory']=='.':args['directory']='here'
            if cwd and args['directory']=='here':args['directory']=cwd
            if name.startswith('start_'):
                args.setdefault('place','tab')
                if args['place'] not in ('tab','right','left','up','down'):
                    raise ValueError('unknown placement')
                if 'model' in args and args['model'] not in _MODELS[name]:
                    raise ValueError('model outside the compact contract')
            elif args['agent'] not in ('claude','codex'):
                raise ValueError('unknown agent')
        out.append(_call(name,**args))
    return out


def admit(request, calls, *, cwd=None, published=False):
    """Admit calls that are exactly the grammar's reading of the whole request.
    published: the calls come from a command model, which knows only
    commands-tools.json, so a grammar-only argument in them is refused and
    what a released model proposes is admitted exactly as before."""
    wanted=exact_calls(request)
    if not wanted:return Admission(None,reason='the whole request has no supported reading')
    try:
        if _canonical(calls,cwd,published=published)!=_canonical(wanted,cwd):
            return Admission(None,reason='the operations or literal arguments differ from the request')
    except ValueError as error:return Admission(None,reason=str(error))
    actions=[];jobs=set()
    for c in wanted:
        name,args=c['name'],c['arguments']
        if name=='open_app':
            jobs.add('apps');actions.append(apps.Action('launch',dict(args)))
        elif name.startswith('start_'):
            jobs.add('agents')
            translated={'agent':name.removeprefix('start_'),'dir':args.get('directory','here')}
            translated.update({new:args[old] for old,new in [('place','place'),('task','prompt'),('model','model')] if old in args})
            actions.append(agents.Action('agent',translated))
        elif 'pane' in args:
            # The runner resolves and verifies the pane (agents_kilix.find_pane).
            jobs.add('agents');translated={key:args[key] for key in ('pane','broker','agent') if key in args}
            if name=='wait_for_agent':translated['for']='idle';kind='wait'
            else:translated['text']=args['text'];kind='tell'
            actions.append(agents.Action(kind,translated))
        else:
            jobs.add('agents');translated={'session':args['agent']+'@'+args.get('directory','here')}
            if name=='wait_for_agent':translated['for']='idle';kind='wait'
            else:translated['text']=args['text'];kind='tell'
            actions.append(agents.Action(kind,translated))
    return Admission(next(iter(jobs)) if len(jobs)==1 else 'mixed',tuple(actions))
