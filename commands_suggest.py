"""Suggest a supported wording for a command the grammar cannot read.

A decision model (Ollama's d3 family) answers multiple-choice questions and
writes nothing. It is offered only continuations of the grammar's own plain
wording: every plan the request's words allow is listed first, using only
spans the request's lexical rules find (a path, quoted text, a model name),
and the model builds the sentence of one of them piece by piece. The
suggestion exists only if the grammar reads that sentence back as exactly
that plan.

Nothing here admits or runs anything. The suggestion is a sentence for the
user to accept; the grammar then reads it like any other request. Importing
this module contacts nothing; only `Decider.choose` talks to the local
Ollama server, and any failure there means no suggestion.
"""
import ipaddress
import json
import math
import os
import re
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import commands_contract as contract

DEFAULT_MODEL = 'd3:2b'
# A walk becomes a suggestion only when the request reads as a command, every
# choice was at least this sure, and the model agrees the plan is the request.
# docs/ask-suggestions.md has the measurements behind these limits.
GATE, STEP, VERIFY = 0.6, 0.5, 0.8

OPERATIONS = {
    'start_claude': 'start a new Claude coding session',
    'start_codex': 'start a new Codex coding session',
    'wait_for_agent': 'wait for an existing Claude or Codex session to finish its turn',
    'message_agent': 'send a message to an existing Claude or Codex session',
    'open_app': 'open an application',
}
PLACES = {'tab': 'a new tab', 'right': 'a split on the right', 'left': 'a split on the left',
          'down': 'a split below', 'up': 'a split above'}
APPS = {'kilix-pdf': 'kilix-pdf, the PDF viewer or PDF reader', 'kilix-calculator': 'kilix-calculator, the calculator',
        'kilix-notepad': 'kilix-notepad, the notepad', 'kilix-file': 'kilix-file, the file manager or file browser'}
AGENTS = {'claude': 'Claude', 'codex': 'Codex'}
KINDS = {
    'command': 'a plain instruction to do something now, which may be to wait for an agent',
    'question': 'a question, or a request for an explanation or advice',
    'conditional': 'something to do only if, when or unless something else happens first, or at a later time',
    'negative': 'an instruction not to do something, or to ignore something',
    'choice': 'it leaves open which of several things to do',
    'statement': 'a statement, a story about the past, a wish or a maybe',
    'incomplete': 'cut off, or missing a part it needs',
}
ORDINALS = ('first', 'second', 'third', 'fourth')
COUNTS = {'1': 'one', '2': 'two', '3': 'three', '4': 'four'}

# The request grammar's lexical rules: a quoted string or a bare path is copied, never reworded.
QUOTES = {'"': '"', "'": "'", '\u201c': '\u201d', '\u2018': '\u2019'}
BARE_PATH = re.compile(r'(?<![\w"./~])((?:~|\.\.?)?/[^\s;,?"\']+)')
# A name after a word of place is a directory unless it is one of the grammar's own words for a place.
_NAME = r'([^\s;,?"\'\u201c\u201d\u2018\u2019]+)'
_LEAD = r'(?:(?:the|my|a|an|this|that|our|your|its|new|fresh|current|same|working|present)\s+)*'
BARE_NAME = re.compile(r'\b(?:in|inside|within|under|at|from)\s+(?:of\s+)?' + _LEAD + _NAME
                       + r'|\b(?:directory|dir|folder|repo|repository)\s+(?:called\s+|named\s+)?' + _NAME, re.I)
LOCATED = re.compile(r'\b(?:in|inside|within|under|at|from|directory|dir|folder|repo|repository|project)\s*'
                     r'(?:called\s+|named\s+)?$', re.I)
NOT_NAMES = {'directory', 'dir', 'folder', 'repo', 'repository', 'project', 'workspace', 'split', 'pane', 'tab',
             'window', 'terminal', 'session', 'right', 'left', 'up', 'down', 'top', 'bottom', 'upper', 'lower',
             'right-hand', 'left-hand', 'vertical', 'horizontal', 'side', 'above', 'below', 'here', 'there', 'it',
             'which', 'pdf', 'kilix-pdf', 'kilix-calculator', 'kilix-notepad', 'kilix-file', 'viewer', 'reader',
             'calculator', 'notepad', 'file', 'claude', 'codex', 'parallel', 'background', 'order', 'turn',
             'sequence', 'addition', 'once', 'all', 'least', 'first', 'last', 'place', 'one', 'two', 'both',
             'each', 'time', 'the', 'my', 'a', 'an', 'this', 'that', 'our', 'your', 'its', 'new', 'fresh',
             'current', 'same', 'working', 'present', 'of', 'kilix', 'cwd'}
# Words the five tools cannot express: a request that uses one is not a plain command.
NOT_NOW = re.compile(r'\b(?:if|unless|provided|providing|assuming|in\s+case|as\s+long\s+as|when|whenever|once|or|'
                     r'never|later|tomorrow|tonight|should|maybe|how|what|why|which|not|every|each|daily|weekly|'
                     r'nightly|(?:mon|tues|wednes|thurs|fri|satur|sun)days?|mornings?|evenings?|noon|midnight|'
                     r'minutes?|hours?|o\'clock)\b', re.I)
# "start codex there", "both in ./x": the directory named is meant once more ("there is a session" names nothing)
THERE = re.compile(r'\bthere\b(?!\'s|\s+(?:is|are|was|were)\b)|\bboth\b', re.I)
# Ending something is not an operation; a request that asks for it is refused whole.
NOT_OFFERED = re.compile(r'\b(?:quit|stop|close|kill|exit|terminate|shut)(?:s|ting|ping|ing)?\b', re.I)
# An operation the request's own verb asks for must be in the plan.
VERBS = ((re.compile(r'\b(?:start|launch)\w*', re.I), ('start_claude', 'start_codex', 'open_app')),
         (re.compile(r'\bwait\w*', re.I), ('wait_for_agent',)),
         (re.compile(r'\b(?:tell|message|send)(?:s|ing)?\b', re.I), ('message_agent',)))
MODEL_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]*')
MODEL_CUE = re.compile(r'\b(?:model|using|with|on|use)\s+(?:the\s+)?(?:model\s+)?(' + MODEL_NAME.pattern + ')', re.I)
NOT_MODELS = {'a', 'an', 'the', 'model', 'claude', 'codex', 'new', 'session', 'tab', 'split', 'right', 'left',
              'it', 'this', 'that', 'here', 'task', 'me', 'my', 'its', 'top', 'bottom'}
MORE_CUE = re.compile(r'\b(?:and|then|also|plus|both|afterwards|next)\b|;', re.I)
PLACE_WORD = {'right': re.compile(r'\bright(?:-hand)?\b', re.I), 'left': re.compile(r'\bleft(?:-hand)?\b', re.I),
              'up': re.compile(r'\b(?:above|top|upper)\b|\bup\s+(?:split|pane)|\b(?:split|pane)\s+up\b', re.I),
              'down': re.compile(r'\b(?:down|below|beneath|under|underneath|bottom|lower)\b', re.I)}
PLACE_CUE = re.compile(r'\b(?:split|pane|tab|right|left|above|below|beneath|under|underneath|over|up|down|top|'
                       r'bottom|upper|lower|beside|side|vertical|vertically|horizontal|horizontally)\b', re.I)
# An agent or application can only be chosen when the request's own words name it.
AGENT_CUE = {'claude': re.compile(r'\bclaude\b', re.I), 'codex': re.compile(r'\bcodex\b', re.I)}
APP_CUE = {'kilix-pdf': re.compile(r'\bpdfs?\b|kilix-pdf', re.I),
           'kilix-calculator': re.compile(r'calculator', re.I),
           'kilix-notepad': re.compile(r'notepad', re.I),
           'kilix-file': re.compile(r'\bfiles?\s+(?:manager|browser|explorer)\b|kilix-file', re.I)}


class SuggestError(RuntimeError):
    """The decision model could not be asked."""


class Decider:
    """Multiple-choice questions to a decision model on the local Ollama server."""

    def __init__(self, model=DEFAULT_MODEL, host=None, timeout=20):
        host = host or os.environ.get('OLLAMA_HOST') or '127.0.0.1:11434'
        if '://' not in host:
            host = 'http://' + host
        parts = urlsplit(host)
        name = parts.hostname or ''
        try:
            local = name == 'localhost' or ipaddress.ip_address(name).is_loopback
        except ValueError:
            local = False
        if parts.scheme != 'http' or not local:
            raise SuggestError('suggestions use only an Ollama server on this computer')
        self.url = f'http://{parts.netloc}/v1/systemone'
        self.model, self.timeout = model, timeout
        self.questions = self.tokens = 0

    def choose(self, state, question, options):
        """[(key, probability)] for every option, most likely first."""
        body = {'model': self.model, 'state': state,
                'questions': {'q': {'type': 'choice', 'instructions': question, 'criteria': options}}}
        try:
            request = Request(self.url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=self.timeout) as response:
                answer = json.load(response)
            odds = answer['answers']['q']['probabilities']
            if set(odds) != set(options) or not all(isinstance(v, (int, float)) and math.isfinite(v)
                                                    for v in odds.values()):
                raise ValueError('the answer does not cover the options')
        except (OSError, URLError, ValueError, KeyError, TypeError) as error:
            raise SuggestError(f'the decision model {self.model} did not answer ({type(error).__name__})') from error
        self.questions += 1
        usage = answer.get('usage')
        self.tokens += usage.get('input_tokens', 0) if isinstance(usage, dict) else 0
        return sorted(odds.items(), key=lambda item: -item[1])


_GONE = '\x00'        # stands where a copied span was: neither a space nor part of a word


def available(model, host=None, timeout=2):
    """Whether the local Ollama server lists the decision model: True, False,
    or None when the server did not answer. Reads only."""
    try:
        url = Decider(model, host, timeout).url.rsplit('/v1/', 1)[0] + '/api/tags'
        with urlopen(Request(url), timeout=timeout) as response:
            listed = json.load(response).get('models', [])
        names = {item.get('name') for item in listed if isinstance(item, dict)}
        return model in names or model + ':latest' in names
    except (SuggestError, OSError, URLError, ValueError, AttributeError, TypeError):
        return None


def _blank(found):
    return _GONE * len(found.group(0))


def _quotes(request):
    """[(start, end, inner)] for each outermost quoted span. An apostrophe
    inside a word neither opens nor closes one."""
    found, at = [], 0
    while at < len(request):
        mark = request[at]
        close = QUOTES.get(mark)
        end = -1
        if close and not (mark == "'" and at and request[at - 1].isalnum()):
            end = at + 1
            while True:
                end = request.find(close, end)
                if end < 0:
                    break
                escaped = mark == '"' and request[end - 1] == '\\' and request[end - 2:end] != '\\\\'
                inside = close in "'\u2019" and request[end + 1:end + 2].isalnum()
                if not (escaped or inside):
                    break
                end += 1
        if end > at + 1:
            found.append((at, end + 1, request[at + 1:end]))
            at = end + 1
        else:
            at += 1
    return found


_PANE_SPAN = re.compile(contract.PANE_SPAN, re.I)


def targets(request):
    """[{'pane', 'broker'}]: each session the request names by its Kilix pane,
    outside quotes, as written; 'broker' is None when it names none. A
    suggestion copies these, never a number of its own."""
    outside = request
    for start, end, _ in _quotes(request):
        outside = outside[:start] + _GONE * (end - start) + outside[end:]
    found = []
    for match in _PANE_SPAN.finditer(outside):
        broker = match['broker'] or match['paren']
        found.append({'pane': match['pane'],
                      'broker': broker if broker and contract._BROKER_ID.fullmatch(broker) else None})
    return found


def spans(request):
    """(paths, texts, models, masked, plain): the request's copyable spans, the
    request with each quoted text replaced by a placeholder, and its own words
    with texts and paths blanked."""
    paths, texts, models, masked, plain = _scan(request)
    return list(dict.fromkeys(paths)), texts, models, masked, plain


def _scan(request):
    """spans(), with a path listed once for each time the request names it."""
    quotes = _quotes(request)
    outside = request
    for start, end, _ in quotes:
        outside = outside[:start] + _GONE * (end - start) + outside[end:]
    # "Tell Codex: run the tests" - after an agent's name, an unquoted rest is the text
    colon = re.search(r':\s', outside)
    rest = request[colon.end():].strip() if colon else ''
    tail = ''
    if rest and rest[0] not in QUOTES and (any(cue.search(outside[:colon.start()]) for cue in AGENT_CUE.values())
                                           or _PANE_SPAN.search(outside[:colon.start()])):
        tail = rest
        quotes = [quote for quote in quotes if quote[1] <= colon.start()]
        outside = outside[:colon.end()]
    paths, texts, pieces, at = [], [], [], 0

    def text(inner):
        if inner not in texts:
            texts.append(inner)
        return f'[TEXT {texts.index(inner) + 1}]'

    for start, end, inner in quotes:
        pieces.append(request[at:start])
        at = end
        if (inner.startswith(('/', '~/', './', '../')) or inner.lower().endswith('.pdf')
                or LOCATED.search(outside[:start])):
            paths.append(inner)
            pieces.append(request[start:end])
        else:
            pieces.append(text(inner))
    pieces.append(request[at:colon.end()] + text(tail) if tail else request[at:])
    masked = ''.join(pieces)
    for found in BARE_PATH.finditer(outside):
        paths.append(found.group(1).rstrip('.,:!)'))
    plain = BARE_PATH.sub(_blank, outside)
    plain = _PANE_SPAN.sub(_blank, plain)      # a pane number or broker id is no name or model
    for found in list(BARE_NAME.finditer(plain)):
        group = 1 if found.group(1) else 2
        name = found.group(group).rstrip('.,:!)')
        if name.lower() not in NOT_NAMES and re.search(r'\w', name):
            paths.append(name)
            plain = plain[:found.start(group)] + _GONE * len(found.group(group)) + plain[found.end(group):]
    models = []
    for found in MODEL_CUE.finditer(plain):
        name = found.group(1).rstrip('.,:')
        if name.lower() not in NOT_MODELS and name not in models:
            models.append(name)
    for found in MODEL_NAME.finditer(plain):        # anything that looks like a version
        name = found.group(0).rstrip('.,:')
        if re.search(r'\d', name) and re.search(r'[A-Za-z]', name) and name not in models:
            models.append(name)
    return paths, texts, models, masked, plain


def describe(call, texts=()):
    """One call in plain words, for the plan the model reads back. With
    `texts`, copied text is shown as its placeholder."""
    def shown(text):
        return f'[TEXT {list(texts).index(text) + 1}]' if text in texts else f'"{text}"'
    name, args = call['name'], call['arguments']
    where = f" in {args['directory']}" if 'directory' in args else ''
    if name.startswith('start_'):
        text = OPERATIONS[name] + where
        if args.get('place', 'tab') != 'tab':
            text += f", in {PLACES[args['place']]}"
        if 'model' in args:
            text += f", with the model {args['model']}"
        if 'task' in args:
            text += f", giving it the task {shown(args['task'])}"
        return text
    who = f"the existing {AGENTS[args['agent']]} session" if args.get('agent') in AGENTS else None
    if 'pane' in args:
        who = (f"the existing {args['agent'].capitalize()} session" if 'agent' in args
               else 'the existing coding-agent session')
        where = f" in pane {args['pane']}" + (f" with broker {args['broker']}" if 'broker' in args else '')
    if name == 'wait_for_agent':
        return f"wait for {who}{where} to finish its turn"
    if name == 'message_agent':
        return f"send {who}{where} the message {shown(args['text'])}"
    text = 'open ' + APPS[args['app']]
    return text + (f" with the file {args['file_path']}" if 'file_path' in args else '')


def plan_text(calls, texts=()):
    return '\n'.join(f'{number}. {describe(call, texts)}' for number, call in enumerate(calls, 1))


def _identity(call):
    args = call['arguments']
    return call['name'], args.get('agent') or args.get('app'), args.get('directory')


def _in_order(calls, plain):
    """Calls in the order the request first names their agent or application,
    when every call names a different one."""
    def place(call):
        key = call['arguments'].get('agent') or call['arguments'].get('app') or call['name'].split('_')[1]
        cue = AGENT_CUE.get(key) or APP_CUE.get(key)
        found = cue.search(plain) if cue else None      # a session named only by its pane has no name to place
        return found.start() if found else None     # a viewer named only by its file has no place
    places = [place(call) for call in calls]
    if None in places or len(set(places)) < len(places):
        return calls
    return [call for _, call in sorted(zip(places, calls), key=lambda pair: pair[0])]


GATE_QUESTION = 'What kind of request is this?'
VERIFY_QUESTION = 'Does the plan do exactly what the request asks?'
VERIFY_OPTIONS = {'yes': 'yes: it does everything asked, with the right details, and nothing else',
                  'no': 'no: it leaves something out, adds something, gets a detail wrong, or should not be done now'}


def _frames(request, masked, texts):
    """(full, base): the request as the model is shown it, whole and with its quoted texts hidden."""
    full = f'A user typed this request to a terminal assistant:\n{request}'
    base = f'A user typed this request to a terminal assistant:\n{masked}'
    if texts:
        base += '\n([TEXT 1] and the like stand for text the request quotes, to be passed on word for word.)'
    return full, base


def _named(paths, plain):
    """(agents, apps, directions) the request's own words name."""
    agents = [agent for agent, cue in AGENT_CUE.items() if cue.search(plain)]
    apps = [app for app, cue in APP_CUE.items() if cue.search(plain)
            or app == 'kilix-pdf' and any(path.lower().endswith('.pdf') for path in paths)]
    return agents, apps, {place for place, cue in PLACE_WORD.items() if cue.search(plain)}


def _left_out(calls, paths, texts, directions):
    """Whether the plan leaves out a path, a quoted text or a direction the request names."""
    used = [value for call in calls for key, value in call['arguments'].items()
            if key in ('directory', 'file_path', 'task', 'text')]
    return (any(path not in used for path in paths) or any(not any(text in value for value in used) for text in texts)
            or bool(directions - {call['arguments'].get('place') for call in calls}))


# A request that stops on a joining word was cut off.
CUT_OFF = re.compile(r'\b(?:in|to|with|for|at|from|on|of|the|a|an|and|then)[\s.!]*$', re.I)


def refusal(request, calls):
    """Why the request's own words rule this plan out, or None. Every
    suggested plan is held to these rules, whichever model made it."""
    mentions, texts, models, _masked, plain = _scan(request)
    paths = list(dict.fromkeys(mentions))
    agents, apps, directions = _named(paths, plain)
    panes = targets(request)
    if not (agents or apps or panes):
        return 'unnamed'
    if NOT_NOW.search(plain):
        return 'conditional'
    if CUT_OFF.search(plain):
        return 'cut off'
    if NOT_OFFERED.search(plain):
        return 'unsupported'
    pools = {'directory': paths, 'file_path': paths, 'task': texts, 'text': texts, 'model': models,
             'pane': [found['pane'] for found in panes], 'broker': [found['broker'] for found in panes]}
    for call in calls:
        name, args = call['name'], call['arguments']
        agent = name.split('_')[1] if name.startswith('start_') else args.get('agent')
        if agent is not None and agent not in agents or 'app' in args and args['app'] not in apps:
            return 'unnamed'
        if any(key in args and args[key] not in pool for key, pool in pools.items()):
            return 'reworded'       # an argument the request does not hold word for word
        if args.get('place', 'tab') != 'tab' and directions and args['place'] not in directions:
            return 'unnamed'
        if args.get('place', 'tab') != 'tab' and not PLACE_CUE.search(plain):
            return 'unnamed'
    if len({_identity(call) for call in calls}) < len(calls):
        return 'repeated'
    if _left_out(calls, paths, texts, directions):
        return 'unused'
    if any({key: call['arguments'].get(key) for key in ('pane', 'broker')} not in panes for call in calls
           if 'pane' in call['arguments']) or any(found['pane'] not in [call['arguments'].get('pane') for call in calls]
                                                  for found in panes):
        return 'unused'     # a pane the plan leaves out, or a pane paired with another broker
    used = [call['name'].split('_')[1] if call['name'].startswith('start_') else
            call['arguments'].get('agent') or call['arguments'].get('app') for call in calls]
    if set(agents + apps) - set(used):
        return 'unused'     # an agent or application the request names and the plan does not use
    spoken = [name.casefold() for name in re.findall(r'(?<![\w.-])(?:claude|codex)(?![\w-])', plain, re.I)]
    if any(used.count(agent) < spoken.count(agent) for agent in agents):
        return 'unused'     # an agent named twice is meant twice
    places = [value for call in calls for key, value in call['arguments'].items() if key in ('directory', 'file_path')]
    again = [found[0].casefold() for found in THERE.finditer(plain)]
    if (any(places.count(path) < mentions.count(path) for path in paths)
            or len(places) < len(mentions) + sum(word != 'both' or bool(mentions) for word in again)):
        return 'unused'     # a directory named twice, or named and then called "there", is meant twice
    said = [value for call in calls for key, value in call['arguments'].items() if key in ('task', 'text')]
    if any(said.count(text) > request.count(text) for text in said):
        return 'reused'     # a quoted text is passed on as often as the request quotes it
    names = {call['name'] for call in calls}
    if any(verb.search(plain) and not names & set(operations) for verb, operations in VERBS):
        return 'unused'
    return None


def possible(request):
    """Whether any plan could be suggested for the request: the grammar does
    not read it, it names an agent, an application or a pane, and it is a
    plain, complete instruction for now. Costs no model."""
    if not isinstance(request, str) or not request.strip() or len(request) > 2048:
        return False
    try:
        if contract.exact_calls(request):
            return False
    except ValueError:
        return False
    paths, _texts, _models, _masked, plain = spans(request)
    agents, apps, _directions = _named(paths, plain)
    named = agents or apps or targets(request)       # a pane names a session too
    return bool(named) and not NOT_NOW.search(plain) and not CUT_OFF.search(plain)


def tidy(calls):
    """A model's proposal without the defaults it may spell out, or None when it is not a list of calls."""
    out = []
    try:
        for call in calls:
            args = {key: value for key, value in call['arguments'].items()
                    if not (key == 'place' and value == 'tab') and not (key == 'directory' and value in ('here', '.'))}
            if call['name'] not in OPERATIONS or not all(isinstance(value, str) for value in args.values()):
                return None
            out.append({'name': call['name'], 'arguments': args})
    except (TypeError, KeyError, AttributeError):
        return None
    return out or None


# The walk: the model picks each operation in turn, then each argument the
# request's words leave open, and last ranks its plan among near misses.
# What the five tools cannot express, in the request's own words (texts and
# paths blanked). The walk plans nothing for a request that says one: doing it
# without the qualifier is not what was asked.
WALK_LIMITS = (
    ('target', re.compile(r'\b(?:panes?|tabs?|windows?|sessions?|brokers?|workers?|threads?|stations?)\s*'
                          r'(?:#|no\.?\s*|number\s*)?\d+\b|\b(?:pane|tab|broker)\d'
                          r'|\b(?:claude|codex)\s+#?\d+\b|\b[0-9a-f]{8}-[0-9a-f]{4}-|\b[0-9a-f]{12,}\b', re.I)),
    ('effort', re.compile(r'\b(?:effort|reasoning|thinking)\b'
                          r'|\bmodel\s+[\w.-]+\s+(?:x?high|medium|low|max|maximum|minimal)\b', re.I)),
    ('unsupported', re.compile(r'\bas\s+(?:root|admin\w*|sudo|another\s+user)\b|\bsudo\b|\byourself\b'
                               r'|\b(?:memory|ram|cpus?|gpus?|priority)\b', re.I)),
    ('count', re.compile(r'\b(?:five|six|seven|eight|nine|ten|dozen|[5-9]|\d{2,})\s+(?:new\s+|more\s+)?'
                         r'(?:claude\s+|codex\s+)?(?:sessions|instances|agents|copies|panes|tabs|times)\b', re.I)),
    ('title', re.compile(r'\b(?:[Cc]laude|[Cc]odex)\s+session\s+[A-Z]')),       # "codex session Night Owl"
    ('resume', re.compile(r'\b(?:resum|restart|reopen|re-open|reattach|revive)\w*'
                          r'|(?<!\bi\s)(?<!\bwe\s)(?<!\byou\s)\bcontinu\w*', re.I)),       # not "before I continue"
    ('flag', re.compile(r'(?<![\w-])--[a-z]', re.I)),
    ('title', re.compile(r'\btitled?\b|\b(?:tab|session|agent|pane|window|instance|one)\s+(?:named|called|label+ed)\b',
                         re.I)),
    ('other agent', re.compile(r'\b(?:grok|gemini|aider|copilot|cursor|chatgpt|qwen|llama|mistral|deepseek|opencode|'
                               r'cline|supervisor|coordinator|orchestrator|reviewer|subagent|worker)s?\b'
                               r'|\bother\s+(?:claude|codex|one|session|agent|instance)\b', re.I)),
    ('host', re.compile(r'\bssh\b|\bhosts?\b|\bremote(?:ly)?\b', re.I)),
    ('not meant', re.compile(r'\b(?:suppos\w*|imagin\w*|hypothetical\w*|pretend\w*|theoretical\w*|thought\s+experiment|'
                             r'example|sample|tutorial|documentation|manual|readme|quot\w+|says|said|status)\b',
                             re.I)),
    ('negated', re.compile(r"\b(?:don'?t|do\s+not|never|refrain\s+from|avoid|no\s+need\s+to)\s+(?:\w+\s+){0,2}?"
                           r"(?:start|launch|open|messag|tell|send|wait|run|spin|ping|bring|fire|boot|summon|spawn)\w*",
                           re.I)),     # "don't start codex", not "don't do anything else, just wait"
    ('later', re.compile(r'\bin\s+(?:an?|one|two|three|five|ten|fifteen|twenty|thirty|a\s+few|a\s+couple\s+of|\d+)\s+'
                         r'(?:more\s+)?(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)\b'
                         r'|\bafter\s+(?:lunch|dinner|breakfast|the\s+meeting|work|standup)\b'
                         r'|\bthis\s+(?:afternoon|evening|morning)\b|\b(?:later|soon)\b', re.I)),
    ('past', re.compile(r'\b(?:yesterday|ago|earlier|previously|last\s+(?:week|night|time|month|year|session))\b'
                        r'|\b(?:i|we|they)\s+(?:already\s+|just\s+)?(?:started|launched|opened|messaged|told|sent|waited|'
                        r'kicked|relayed|pinged|ran|asked)\b|\b(?:talked|discussed|spoke|mentioned)\b', re.I)),
)
# Directions the walk also reads ("split upward"), and words that ask for a split without saying where.
WALK_PLACE = {**PLACE_WORD, 'up': re.compile(PLACE_WORD['up'].pattern + r'|\bupwards?\b', re.I),
              'down': re.compile(PLACE_WORD['down'].pattern + r'|\bdownwards?\b', re.I)}
WALK_SPLIT = re.compile(r'\b(?:split|pane|beside|side|vertical(?:ly)?|horizontal(?:ly)?)\b', re.I)
# Each join, list mark or new sentence allows one more operation.
WALK_JOIN = re.compile(r'(?:[,;:]\s*)?(?:\b(?:and|then|also|plus|but|afterwards|next|after\s+that|finally|lastly)\b[\s,]*)+'
                       r'|[,;:]|\s-\s|[.!?]\s+(?=\S)|(?<!\S)\(?\d[.)](?=\s)', re.I)
# A clause that is only courtesy asks for nothing; one that names an agent, an application, a copied span or
# an operation's verb is expected to be an operation of its own.
WALK_COURTESY = re.compile(r'(?:\s|\b(?:please|pls|thanks|thank\s+you|cheers|for\s+me|now|right\s+away|asap|ok|okay|'
                           r'hey|hi|hello|ta|if\s+you\s+can|when\s+you\s+can)\b)*', re.I)
WALK_CUE = re.compile(r'\bclaude\b|\bcodex\b|calculator|notepad|\bpdfs?\b|\bfiles?\s+(?:manager|browser|explorer)\b|kilix-'
                      r'|\b(?:start|launch|open|run|wait|hold|hang|pause|block|tell|message|send|ping|say|relay|'
                      r'forward|pass|let|give|spin|fire|boot|bring|pull|show|summon|kick|hand|nudge|loop|check|put|get|'
                      r'make|create|begin|add|set|write|type|do)\w*', re.I)
# The first word of a clause that names no agent or application must be one of these, or a word of place,
# courtesy, number or reference: any other verb ("push the branch", "email the team") asks for something none
# of the five tools does, and the whole request is refused.
WALK_VERBS = re.compile(r'(?:start|launch|open|run|wait|hold|hang|pause|block|tell|message|send|ping|say|relay|forward|'
                        r'pass|let|give|spin|fire|boot|bring|pull|show|summon|spawn|kick|hand|nudge|loop|check|put|get|'
                        r'begin|notify|inform|ask|have|keep|stand|sit|need|want|like|write|load|view|display|fetch|go|'
                        r'make|create|stay|await|follow|shoot|drop|alert|remind|update|warn|share|leave|hit|fill|'
                        r'whip|crack|pop|post|answer|set|notify|await|relay|ship|tag)\w*|(?:i|we|i\'\w+|we\'\w+|you|me|my|our|your|us|it|its|it\'s|this|that|these|'
                        r'those|the|a|an|and|then|also|plus|so|just|now|kindly|quickly|simply|first|second|third|fourth|'
                        r'next|finally|lastly|after|afterwards|once|one|two|three|four|both|all|each|here|there|over|in|'
                        r'inside|within|at|on|onto|into|with|without|using|under|from|for|to|of|by|as|while|via|split|'
                        r'pane|tab|right|left|up|down|above|below|beneath|underneath|top|bottom|upper|lower|side|'
                        r'vertical|horizontal|new|fresh|another|same|current|model|task|message|text|directory|folder|'
                        r'dir|repo|repository|project|path|cwd|again|too|please|thanks|hey|hi|ok|okay|then|yes|go|'
                        r'agents|apps|ask|terminal|session|sessions|instance|use|verbatim|word|would|could|will|can|'
                        r'exactly|its?|initial|context|background|fyi|note|long|short|quick|that\'s|thats)', re.I)
WALK_ENDED = re.compile(r'\b(?:task|message|text|saying|with|that|this|these|the\s+following)\s*[.!:]?\s*$', re.I)
# A place the copier does not copy ("in ~/", "the folder polenta-viaduct" read as a word): the request names a
# directory that no plan can carry.
WALK_UNREAD = re.compile(r'(?:^|\s)(?:~/?|\.{1,2}/?|/)(?=[\s.,;!?]|$)|\b(?:in|inside|at|from|under)\s+(?:the\s+)?'
                         r'(?:folder|directory|dir|repo|repository)\s+(?:called\s+|named\s+)?\w+[-._]\w', re.I)
# The rest after a colon is a text to pass on only when what comes before it says so.
WALK_TAIL = re.compile(r'(?<![\w-])(?:tell|message|send|ping|say|relay|forward|pass|know|inform|notify|write|ask|nudge|loop|'
                       r'remind|update|hear|note|reply|text|dm|word|task|job|start|launch|open|spin|fire|boot|bring|'
                       r'summon|spawn|kick|hand|give|new|fresh|run|get|put|post|answer|heads-up)\w*', re.I)
WALK_ASKS = re.compile(r'(?<![\w-])(?:tell|message|send|ping|say|relay|forward|pass|know|inform|notify|write|ask|nudge|'
                       r'remind|reply|text|dm|post|answer)\w*', re.I)
# "Open codex in x: ...": a request that begins with a launch verb and an agent begins with that launch.
WALK_LAUNCH = re.compile(r'^\W*(?:please\s+|hey\s+|ok\s+)?(?:open|start|launch|spin\s+up|fire\s+up|boot(?:\s+up)?|bring\s+up|'
                         r'run|summon|spawn|kick\s+off)\s+(?:up\s+)?(?:a\s+|an\s+)?(?:new\s+|fresh\s+)?(?:claude|codex)\b',
                         re.I)
# "then message both": "both" pointing back at the two agents named, not "both claude and codex" or "both here".
WALK_BOTH = re.compile(r'\bboth\b(?!\s+(?:of\s+)?(?:the\s+|them\s+)?(?:claude|codex|in|at|on|here|there|inside|under|from)\b)',
                       re.I)
# A time of day, which the copier may take for a directory ("at 6pm"), and words it may take for one
# ("in the other folder") that name no place.
WALK_LATER = re.compile(r'\b\d{1,2}(?::\d\d)?\s*(?:am|pm)\b|\b(?:tonight|tomorrow|noon|midnight)\b', re.I)
WALK_NOT_PLACES = {'other', 'another', 'previous', 'last', 'next', 'usual', 'default', 'original', 'old'}
WALK_ALONE = re.compile(r'(?:/|~/|\./|\.\./)\S*')       # a colon's rest that is only a path is no message
WALK_FIELD = re.compile(r'\b(?:directory|dir|folder|path|cwd|location|split|place|side|model)\s*:', re.I)
# A session named by its pane: the agent named right before it ("the Codex agent in pane 9") is that
# session's agent. Kilix pane spans are found by targets(); these read only the words around them.
WALK_BOUND = re.compile(r'\b(claude|codex|grok)\b(?:\'s)?\s+(?:(?:session|agent|instance|cli)\s+)?'
                        r'(?:(?:that\'s\s+|that\s+is\s+|sitting\s+|running\s+|working\s+)?(?:in|on|at|inside)\s+)?'
                        r'(?:kilix\s+)?pane\s+([1-9][0-9]{0,8})\b', re.I)
# "from pane 4, tell pane 9": the request says who sends, which Ask cannot claim.
WALK_SENDER = re.compile(r'\b(?:from|as|i\'m|i\s+am|this\s+is|we\'re|on\s+behalf\s+of)\s+(?:the\s+\w+\s+)?'
                         r'(?:(?:session|agent)\s+)?(?:in\s+)?(?:kilix\s+)?pane\s+\d', re.I)
# "the same pane", "that pane": a reference back to the one pane named; any other pane without its number
# ("the pane on the left", "my other pane") names no target.
WALK_SAME_PANE = re.compile(r'\b(?:the\s+same|that|this|the|its|that\s+same)\s+pane\b', re.I)
# A pane named without its number ("the pane on the left", "my other pane"), unlike a place for a new
# session ("codex in a pane above").
WALK_THAT_PANE = re.compile(r'\b(?:the|my|that|this|our|your|its|other|their)\s+(?:(?:left|right|top|bottom|upper|lower|'
                            r'other|same|last|first|second|next|previous|middle)\s+)?panes?\b', re.I)
# A text is a task or a message by the words just before it ("with the task '...'", "tell it: ...").
WALK_AS_TASK = re.compile(r'(?<![\w./~-])(?:task|job|assignment|to-do|todo)\b', re.I)
WALK_AS_MESSAGE = re.compile(r'(?<![\w./~-])(?:message|tell|telling|told|say|saying|send|sending|post|ping|relay|forward|'
                             r'note|answer|reply|heads-up|know|word|line|remind|notify|nudge|pass|ask|inform)\w*\b',
                             re.I)
# "right here", "right now": no direction.
WALK_RIGHT_AWAY = re.compile(r'\bright\s+(?=(?:here|now|away|there|then|off|after|before)\b)', re.I)
WALK_CHECK = 0.5        # the plan must win the ranking against its near misses with at least these odds
# Model names after "on", "with" or "using" that no tool takes ("summon claude on haiku").
WALK_MODEL_WORD = re.compile(r'haiku|fable|sonnet\S+|opus\S+|claude-\S+|gpt\S*|o\d\S*|gemini\S*|llama\S*|qwen\S*|'
                             r'mistral\S*|deepseek\S*|grok\S*|codex-\S+|mini|nano|turbo', re.I)
WALK_FILE = re.compile(r'\b[\w-]+\.(?:txt|md|csv|json|ya?ml|py|js|ts|log|docx?|rtf|png|jpe?g|html?)\b', re.I)
WALK_IT = re.compile(r'\b(?:it|its|that\s+one|that\s+session|the\s+session)\b', re.I)
WALK_VERIFY = 0.7       # the yes/no check, for a plan with no near miss to rank it against
WALK_NEAR = 7           # near misses ranked against the plan, at most
WALK_DONE = 'nothing more: the operations already planned are everything the request asks for'
WALK_OTHER = ('something that none of these can do, such as stopping, closing, deleting, editing, typing, '
              'calculating, or using another program')
WALK_QUESTION = 'Which plan does exactly what the request asks, no more and no less?'
WALK_NONE = 'none of these: the request asks for something else, or should not be done now'


def walk_limit(plain):
    """The first thing the request's own words ask for that the five tools cannot express, or None."""
    return next((why for why, cue in WALK_LIMITS if cue.search(plain)), None)


def _unasked(clause):
    """Whether a clause asks for something none of the five tools does: it names
    no agent or application, and its first word is a verb the walk does not know."""
    if any(cue.search(clause) for cue in (*AGENT_CUE.values(), *APP_CUE.values())):
        return False
    head = re.search(r"[A-Za-z][\w'-]*", clause.replace(_GONE, ' '))
    if not head or re.search(r'\d', clause[:head.start()]):
        return False
    word = head.group(0)
    if re.match(r"\s+(?:is|are|was|were|has|have|had|looks|seems|got)\b", clause.replace(_GONE, ' ')[head.end():]):
        return False        # "p99 is back under 200ms": a statement, not something asked for
    if word.lower() in ('run', 'execute'):
        return True         # "run git push": with no agent or application named, a command the tools do not run
    return not (WALK_VERBS.fullmatch(word) or word.lower().endswith(('ing', 'ed')) or WALK_COURTESY.fullmatch(word))


def walk_refuses(request):
    """Why the walk plans nothing for this request, read from its words alone, or None."""
    mentions, texts, models, masked, plain = _scan(request)
    paths = list(dict.fromkeys(mentions))
    agents, apps, _directions = _named(paths, plain)
    panes = targets(request)
    if not (agents or apps or panes):
        return 'unnamed'
    if NOT_NOW.search(plain):
        return 'conditional'
    why = _walk_panes(request, masked, plain, panes)
    if why:
        return why
    typed = masked
    for path in sorted(paths, key=len, reverse=True):
        if re.search(r'[/~.]', path):       # a path as typed, not a word the copier took for a name
            typed = typed.replace(path, ' ')
    why = (walk_limit(plain) or ('target' if agents and re.search(r'\bwindows?\b', plain, re.I) else None)
           or next((why for why, cue in WALK_LIMITS if why in ('negated', 'later') and cue.search(typed)), None))
    # (the copier may take "messaging" or "hour" for a name: those two read the words as typed)
    if why:
        return why
    words = plain.replace(_GONE, ' ').strip()
    polite = re.match(r'\W*(?:(?:hey|hi|hello|ok|okay|so|please|um)\W+)*(?:could|would|can|will|may)\s+(?:you|i|we|u)\b',
                      words, re.I)
    if words.endswith('?') and not polite or re.match(r'\W*(?:is|are|was|were|has|had|did|am)\b', words, re.I):
        return 'question'           # "is the codex in pane 9 done?", not "could you bring up claude ...?"
    if apps and WALK_FILE.search(plain):
        return 'unsupported'        # "open notes.txt in the notepad": a file no tool opens there
    first = WALK_IT.search(plain)
    if len(agents) == 2 and first and all(cue.search(plain[:first.start()]) for cue in AGENT_CUE.values()):
        return 'ambiguous'          # "start claude and codex, then tell it ...": which one
    allowed = {model for names in contract._MODELS.values() for model in names}
    cued = [found.group(1).rstrip('.,:') for found in MODEL_CUE.finditer(plain)]     # "with o3", "using 4o"
    if any(WALK_MODEL_WORD.fullmatch(name) and name.lower() not in allowed for name in cued):
        return 'model'      # "claude on haiku": a model outside the four
    if (any(name.rstrip('.,:') not in allowed for name in MODEL_SAID.findall(plain))
            or any(re.search(r'\d', name) and name not in allowed and (re.match(r'[A-Za-z]{2}', name) or name in cued)
                   for name in models)):
        return 'model'      # a model the grammar cannot carry, and leaving it out changes the request
    if any(re.fullmatch(r'#\S*|\d+', path) for path in paths):
        return 'target'     # "the codex agent in #2207": a number, not a directory
    if WALK_LATER.search(masked) or any(path.lower() in WALK_NOT_PLACES for path in paths):
        return 'later' if WALK_LATER.search(masked) else 'unnamed place'
    rest = request[request.find(':') + 1:].strip() if ':' in request else ''
    if rest in texts and (WALK_ALONE.fullmatch(rest.rstrip('.')) or WALK_FIELD.search(request)):
        return 'fields'     # "Directory: /x": the rest after a colon is not a text to pass on
    before = request[:request.find(':')] if rest in texts else ''
    if before and (not WALK_TAIL.search(before) or rest.endswith('?') and not WALK_ASKS.search(before)):
        return 'unread text'        # nothing before the colon says the rest is a message or a task
    directions = _walk_directions(plain)
    if WALK_SPLIT.search(WALK_SAME_PANE.sub(' ', plain) if panes else plain) and not directions and agents:
        return 'unplaced'   # a split, but not on which side
    if WALK_ENDED.search(request) or WALK_UNREAD.search(plain):
        return 'cut off'
    if any(_unasked(clause) for clause in WALK_JOIN.split(plain) if clause):
        return 'unsupported'
    return None


def _walk_bound(masked):
    """{pane: agent} for each pane the request names together with its agent ("codex in pane 9"),
    and the spans of those mentions; None when one pane is given two agents."""
    bound, at = {}, []
    for found in WALK_BOUND.finditer(masked):
        agent, pane = found.group(1).lower(), found.group(2)
        if bound.setdefault(pane, agent) != agent:
            return None, at
        at.append(found.span())
    return bound, at


def _walk_panes(request, masked, plain, panes):
    """Why a request that names a pane, or a pane without its number, gets no plan from the walk, or None."""
    if not panes:
        beside = re.sub(r'\b(?:below|above|beside|under|over|next\s+to|(?:right|left)\s+of)\s+this\s+pane\b', ' ', plain,
                        flags=re.I)        # "below this pane" is a place for a new session
        return 'target' if WALK_THAT_PANE.search(beside) or re.search(r'\bbroker\b', plain, re.I) else None      # "the pane on the left", "my other pane"
    if WALK_SENDER.search(masked):
        return 'sender'     # "from pane 4, tell pane 9": the pane that sends is no target
    if re.search(r'\bbroker\b', masked, re.I) and any(found['broker'] is None for found in panes):
        return 'target'     # a broker the copier cannot take as written (not lowercase hex)
    unique = {(found['pane'], found['broker']) for found in panes}
    if len({pane for pane, _ in unique}) < len(unique):
        return 'target'     # one pane with two brokers, or with and without one
    if re.search(r'\bpanes?\b', WALK_SAME_PANE.sub(' ', plain), re.I) or len(unique) > 1 and WALK_SAME_PANE.search(plain):
        return 'target'     # a pane not named by its number, or "the same pane" with two to choose from
    bound, at = _walk_bound(masked)
    if bound is None:
        return 'target'
    free = masked
    for start, end in reversed(at):
        free = free[:start] + ' ' * (end - start) + free[end:]
    if any(cue.search(free) for agent, cue in AGENT_CUE.items() if agent in bound.values()):
        return 'target'     # "tell claude in pane 2: x; then wait for claude": the second may be the first
    if 'grok' in bound.values():
        return 'other agent'        # the rules hold an agent to claude or codex
    return None


def _walk_directions(plain):
    """The directions the request says, without the "right" of "right here" or "right now"."""
    directions = {place for place, cue in WALK_PLACE.items() if cue.search(plain)}
    if 'right' in directions and not WALK_PLACE['right'].search(WALK_RIGHT_AWAY.sub(' ', plain)):
        directions.discard('right')
    return directions


def _walk_role(request, text):
    """'task' or 'message' when the words just before a text say which it is, else None."""
    at = request.find(text)
    before = request[max(0, at - 48):max(at, 0)]
    task = [found.end() for found in WALK_AS_TASK.finditer(before)]
    message = [found.end() for found in WALK_AS_MESSAGE.finditer(before)]
    if not (task or message):
        return None
    return 'task' if (task[-1] if task else -1) > (message[-1] if message else -1) else 'message'


def _walk_order(calls, plain):
    """_in_order(), except that a plan naming a pane keeps the order it was asked in."""
    return calls if any('pane' in call['arguments'] for call in calls) else _in_order(calls, plain)


def _walk_clauses(plain):
    """(most, expected): at most how many operations the request's clauses
    allow, and how many of its clauses look like an operation of their own."""
    clauses = [clause for clause in WALK_JOIN.split(plain) if clause and clause.strip(' .!?')
               and not WALK_COURTESY.fullmatch(clause.strip(' .!?'))]
    expected = sum(1 for clause in clauses if WALK_CUE.search(clause))
    most = min(4, max(1, len(clauses)) + len(WALK_BOTH.findall(plain)))     # "then wait for both" is two
    return most, min(most, max(1, expected))


def _walk_owed(calls, paths, mentions, again):
    """Directory mentions the plan does not yet use: [(path, times still owed)], with
    "there", "both" and "the same directory" owing one more use of a path."""
    places = [value for call in calls for key, value in call['arguments'].items() if key in ('directory', 'file_path')]
    owed = [(path, mentions.count(path) - places.count(path)) for path in paths]
    owed = [(path, times) for path, times in owed if times > 0]
    extra = len(mentions) + again - len(places) - sum(times for _, times in owed)
    return owed, max(extra, 0)


def _variants(calls, paths=(), directions=(), panes=()):
    """Near misses of a plan: one operation fewer, one argument dropped, the
    other agent, two operations swapped, a directory, direction or pane moved
    or exchanged. Each is a list of calls."""
    out = []
    def with_arg(number, key, value):
        args = {name: item for name, item in calls[number]['arguments'].items() if name != key}
        if value is not None:
            args[key] = value
        return calls[:number] + [{'name': calls[number]['name'], 'arguments': args}] + calls[number + 1:]
    for number, call in enumerate(calls):
        if 'pane' in call['arguments']:
            for target in panes:
                if target['pane'] != call['arguments']['pane']:
                    args = {key: value for key, value in call['arguments'].items() if key not in ('pane', 'broker')}
                    args.update({key: value for key, value in target.items() if value is not None})
                    out.append(calls[:number] + [{'name': call['name'], 'arguments': args}] + calls[number + 1:])
        elif call['name'] != 'open_app':
            for path in paths:
                if call['arguments'].get('directory') != path:
                    out.append(with_arg(number, 'directory', path))
        if call['name'].startswith('start_'):
            for place in directions:
                if call['arguments'].get('place') != place:
                    out.append(with_arg(number, 'place', place))
    if len(calls) > 1:
        out += [calls[:number] + calls[number + 1:] for number in range(len(calls))]
        out += [calls[:number] + [calls[number + 1], calls[number]] + calls[number + 2:]
                for number in range(len(calls) - 1)]
    for number, call in enumerate(calls):
        for key in ('directory', 'place', 'task', 'model', 'file_path'):
            if key in call['arguments']:
                args = {name: value for name, value in call['arguments'].items() if name != key}
                out.append(calls[:number] + [{'name': call['name'], 'arguments': args}] + calls[number + 1:])
        swap = {'claude': 'codex', 'codex': 'claude'}
        if call['name'].startswith('start_'):
            other = {'name': 'start_' + swap[call['name'].split('_')[1]], 'arguments': call['arguments']}
        elif 'agent' in call['arguments']:
            other = {'name': call['name'], 'arguments': {**call['arguments'], 'agent': swap[call['arguments']['agent']]}}
        else:
            continue
        out.append(calls[:number] + [other] + calls[number + 1:])
    unique = []
    for variant in out:
        if variant and variant != calls and variant not in unique:
            unique.append(variant)
    return unique


def walk(request, choose):
    """(calls, steps): the plan the model's choices make, and every question's
    answer with its odds. `calls` is empty when a step refuses the request.

    The model is asked the kind of request, then each operation in turn
    (after the first it may answer that nothing more is asked, or that the
    request asks for something the tools cannot do), and each argument the
    request's words leave open. An argument they fix (one agent named, the
    only path or text left for the last operation, the one direction named)
    is taken without a question. Last, the plan is ranked among its near
    misses, and must win. suggest(..., plan=walk) offers it when every answer
    was sure enough (walk_signals)."""
    steps = []
    mentions, texts, models, masked, plain = _scan(request)
    paths = list(dict.fromkeys(mentions))
    full, base = _frames(request, masked, texts)
    agents, apps, _directions = _named(paths, plain)
    directions = _walk_directions(plain)
    panes = []      # each pane named, once, as targets() gives it
    for found in targets(request):
        if found not in panes:
            panes.append(found)
    bound = _walk_bound(masked)[0] or {} if panes else {}
    free = [agent for agent in agents if agent not in bound.values()]     # agents not named by a pane
    roles = {text: _walk_role(request, text) for text in texts}
    offered = {name: text for name, text in OPERATIONS.items() if
               (name.startswith('start_') and name.split('_')[1] in free)
               or (name == 'wait_for_agent' and (free or panes))
               or (name == 'message_agent' and (free or panes) and any(roles[text] != 'task' for text in texts))
               or (name == 'open_app' and apps)}
    allowed = {model for names in contract._MODELS.values() for model in names}
    said = [name.rstrip('.,:') for name in MODEL_SAID.findall(plain)]
    again = sum(found[0].casefold() != 'both' or bool(mentions) for found in THERE.finditer(plain))
    again += len(SAME.findall(plain)) if mentions else 0
    dirs = [path for path in paths if not path.lower().endswith('.pdf') and path not in texts]
    order = sorted(apps, key=lambda app: (APP_CUE[app].search(plain) or re.search(r'$', plain)).start())

    def ask(kind, state, question, options):
        ranked = choose(state, question, options)
        steps.append({'kind': kind, 'pick': ranked[0][0], 'p': ranked[0][1],
                      'odds': {key: round(value, 4) for key, value in ranked}})
        return ranked[0][0]

    def fixed(kind, value):
        steps.append({'kind': 'fixed', 'pick': f'{kind}={value}', 'p': 1.0, 'odds': {}})
        return value

    def refused(kind):
        steps.append({'kind': 'refused', 'pick': kind, 'p': 0.0, 'odds': {}})
        return [], steps

    def span(kind, state, question, candidates, none, fix):
        """A copied span for an argument: fixed when `fix` names one, else the model's choice."""
        if fix is not None:
            return fixed(kind, fix)
        if not candidates:
            return None
        options = {str(number): candidate for number, candidate in enumerate(candidates, 1)}
        if none:
            options['0'] = none
        elif len(candidates) == 1:
            return fixed(kind, candidates[0])
        chosen = ask(kind, state, question, options)
        return None if chosen == '0' else candidates[int(chosen) - 1]

    why = 'unnamed' if not offered else walk_refuses(request)
    if why:
        return refused(why)
    ask('gate', base, GATE_QUESTION, KINDS)
    most, expected = _walk_clauses(plain)
    calls = []
    for number in range(most):
        last = number >= expected - 1      # the last operation the clauses lead one to expect
        note = '\n\nAlready planned:\n' + plan_text(calls, texts) if calls else ''
        state, whole = base + note, full + note
        options = dict(offered)
        if not calls and WALK_LAUNCH.search(request):
            options = {key: text for key, text in offered.items() if key.startswith('start_')}
        if not any(app not in [call['arguments'].get('app') for call in calls] for app in order):
            options.pop('open_app', None)       # every application named is already opened
        options['other'] = WALK_OTHER
        if calls and refusal(request, calls) is None:
            options['done'] = WALK_DONE       # offered once the plan so far leaves nothing named unused
        question = ('Which operation does the request ask for?' if most == 1 else
                    'Which operation does the request ask for first?' if not calls else
                    'What does the request ask for next, after the operations already planned?')
        name = ask('operation', state, question, options)
        if name == 'done':
            break
        if name == 'other':
            return refused('operation')     # an unsupported action refuses the whole request
        this = f'the {ORDINALS[number]} operation, to {OPERATIONS[name]}'
        args = {}
        if name in ('wait_for_agent', 'message_agent') and panes:
            # "Which session": each pane named, and each agent named without one.
            sessions = {f"pane {found['pane']}": f"the session in pane {found['pane']}"
                        + (f", a {AGENTS[bound[found['pane']]]} session" if bound.get(found['pane']) in AGENTS else '')
                        for found in panes}
            sessions.update({agent: f'the {AGENTS[agent]} session not named by a pane' for agent in free})
            key = (fixed('session', next(iter(sessions))) if len(sessions) == 1 else
                   ask('session', state, f'Which session is {this} about?', sessions))
            if key in AGENTS:
                args['agent'] = key
            else:
                target = next(found for found in panes if f"pane {found['pane']}" == key)
                if bound.get(target['pane']):
                    args['agent'] = bound[target['pane']]
                args['pane'] = target['pane']
                if target['broker']:
                    args['broker'] = target['broker']
        elif name in ('wait_for_agent', 'message_agent'):
            args['agent'] = (fixed('agent', agents[0]) if len(agents) == 1 else
                             ask('agent', state, f"Which agent's session is {this} about?",
                                 {agent: AGENTS[agent] for agent in agents}))
        used = [value for call in calls for key, value in call['arguments'].items() if key in ('task', 'text')]
        left = [text for text in texts if text not in used]
        if name == 'open_app':
            opened = [call['arguments']['app'] for call in calls if call['name'] == 'open_app']
            args['app'] = fixed('app', next(app for app in order if app not in opened))
            pdfs = [path for path in paths if path.lower().endswith('.pdf')]
            owed, _extra = _walk_owed(calls, pdfs, mentions, 0)
            if args['app'] == 'kilix-pdf' and pdfs:
                path = span('file', state, 'Which file should the PDF viewer open?', pdfs,
                            'no file: just open the viewer',
                            owed[0][0] if last and len(owed) == 1 else None)
                if path:
                    args['file_path'] = path
        elif 'pane' not in args:        # a session named by its pane has no directory
            owed, extra = _walk_owed(calls, dirs, mentions, again)
            fix = owed[0][0] if len(dirs) == 1 and owed and last else None
            if fix is None and len(dirs) == 1 and extra and last:
                fix = dirs[0]       # "there", "both", "the same directory": the one path, once more
            path = span('directory', state, f'Which directory is {this} for?', dirs,
                        'the current directory: the request names no directory for this operation', fix)
            if path:
                args['directory'] = path
        if name.startswith('start_'):
            places = sorted(directions - {call['arguments'].get('place') for call in calls})
            if directions:
                if last and len(places) == 1:
                    place = fixed('place', places[0])
                else:
                    place = ask('place', state, f'Where should the new session of {this} open?',
                                {**{place: PLACES[place] for place in sorted(directions)},
                                 'tab': 'a new tab, or the request does not say where for this one'})
                if place != 'tab':
                    args['place'] = place
            mine = [model for model in models if model in contract._MODELS[name]]
            model = span('model', whole, f'Which model does the request name for {this}?', mine,
                         'none: the request names no model for it',
                         mine[0] if len(mine) == 1 and last else None)      # a model named is always used
            if model:
                args['model'] = model
            tasks = [text for text in left if roles[text] != 'message']     # never a text said to be a message
            task = span('task', whole, f'Which text is the task that the new session of {this} should be given?',
                        tasks, 'none: the request gives it no task',
                        tasks[0] if len(tasks) == 1 and len(left) == 1 and last else None)
            if task:
                args['task'] = task
        if name == 'message_agent':
            said_ = [text for text in left or texts if roles[text] != 'task']     # never a text said to be a task
            if not said_:
                return refused('text')
            args['text'] = span('text', whole, f'Which text is the message for {this}?', said_, None, None)
        call = {'name': name, 'arguments': args}
        if call in calls and refusal(request, calls) is None:
            steps.append({'kind': 'fixed', 'pick': 'done=repeat', 'p': 1.0, 'odds': {}})
            break       # the same call again, once the plan is whole: nothing more was asked
        calls.append(call)
    if len({_identity(call) for call in calls}) < len(calls):
        return refused('repeated')      # the same operation twice is a misread "and", never a plan
    if _left_out(calls, paths, texts, directions):
        return refused('unused')        # the request names something the plan leaves out
    mentioned = [found['pane'] for found in targets(request)]
    if any([call['arguments'].get('pane') for call in calls].count(pane) < mentioned.count(pane) for pane in mentioned):
        return refused('unused')        # a pane named twice is meant twice ("wait on pane 7, then tell pane 7 ...")
    if any(name not in [call['arguments'].get('model') for call in calls] for name in said + models
           if name in allowed):
        return refused('unused')        # a model the request names is never left out
    who = [call['name'].split('_')[1] if call['name'].startswith('start_') else call['arguments'].get('agent')
           for call in calls]
    if len(agents) == 2 and WALK_BOTH.search(plain) and min(map(who.count, agents)) < 2:
        return refused('unused')        # "both" asks for one more operation on each of the two agents
    calls = _walk_order(calls, plain)
    if refusal(request, calls):
        return refused('rules')         # suggest() would refuse it: no need to ask about it
    # The near misses that are plans in their own right (the rules allow them too) compete with it.
    near = [plan for plan in _variants(calls, dirs, sorted(directions), panes)
            if _walk_order(plan, plain) == plan and refusal(request, plan) is None][:WALK_NEAR]
    if near:
        ranked = sorted([calls] + near, key=lambda plan: '; then '.join(describe(call) for call in plan))
        options = {str(number): '; then '.join(describe(call) for call in plan)
                   for number, plan in enumerate(ranked, 1)}
        options['0'] = WALK_NONE
        mine = str(ranked.index(calls) + 1)
        pick = ask('check', full, WALK_QUESTION, options)
        steps[-1]['plan'] = mine
        if pick != mine:
            return refused('check')
    else:       # nothing to rank it against: the yes/no check
        ask('verify', full + "\n\nThe assistant's plan:\n" + plan_text(calls), VERIFY_QUESTION, VERIFY_OPTIONS)
        if steps[-1]['odds'].get('yes', 0.0) < WALK_VERIFY:
            return refused('verify')
    return calls, steps


def walk_signals(steps):
    """(gate, least sure choice, final check) for a walk: the check is the
    plan's odds in the ranking against its near misses, or the yes of the
    yes/no check when the plan has no near miss."""
    gate = next((step['odds'].get('command', 0.0) for step in steps if step['kind'] == 'gate'), 0.0)
    check = next((step['odds'].get(step['plan'], 0.0) for step in steps if step['kind'] == 'check'), 1.0)
    inner = [step['p'] for step in steps if step['kind'] not in ('gate', 'verify', 'check', 'fixed')]
    return gate, min(inner) if inner else 1.0, check


def signals(steps):
    """(gate, least sure choice, verify) for a walk or a build."""
    gate = next((step['odds'].get('command', 0.0) for step in steps if step['kind'] == 'gate'), 0.0)
    verify = next((step['odds'].get('yes', 0.0) for step in steps if step['kind'] == 'verify'), 0.0)
    inner = [step['p'] for step in steps if step['kind'] not in ('gate', 'verify', 'count')]
    return gate, min(inner) if inner else 1.0, verify     # a plan with only one possible wording asks no choice


def _quote(text):
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


_PLACE_WORDS = {'right': ' in a split to the right', 'left': ' in a split to the left',
                'up': ' in a split above', 'down': ' in a split below', 'tab': ''}
_APP_WORDS = {'kilix-pdf': 'the pdf viewer', 'kilix-calculator': 'the calculator',
              'kilix-notepad': 'the notepad', 'kilix-file': 'the file manager'}


def sentence(calls):
    """The plan in the grammar's plain wording. The caller checks that the
    grammar reads it back; this does not promise it."""
    parts = []
    for call in calls:
        name, args = call['name'], call['arguments']
        where = f" in {_quote(args['directory'])}" if 'directory' in args else ''
        if name.startswith('start_'):
            text = 'start ' + name.split('_')[1] + where + _PLACE_WORDS[args.get('place', 'tab')]
            if 'model' in args:
                text += f" with model {args['model']}"
            if 'task' in args:
                text += f" with task {_quote(args['task'])}"
        elif name == 'wait_for_agent':
            text = f"wait for {contract.pane_words(args) if 'pane' in args else args['agent'] + where}"
        elif name == 'message_agent':
            text = f"message {contract.pane_words(args) if 'pane' in args else args['agent'] + where}: {_quote(args['text'])}"
        elif 'file_path' in args:
            text = f"open {_quote(args['file_path'])} in the pdf viewer"
        else:
            text = 'open ' + _APP_WORDS[args['app']]
        parts.append(text)
    return '; then '.join(parts)


def _carried(calls):
    """{'request', 'calls'}: the plan in the grammar's wording, when the grammar reads it back as that plan."""
    try:
        wording = sentence(calls)
        read = contract.exact_calls(wording)
        if not read or contract._canonical(read) != contract._canonical(calls):
            return None     # the grammar is the judge of what it supports
        if not contract.admit(wording, read).actions:
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return {'request': wording, 'calls': read}


# The grammar's sentence, built piece by piece. plans() lists every plan the
# request's own words allow and the grammar carries; their wordings form a tree
# of pieces ("start codex", ' in "dusk-ferry"', " in a split below", "; then
# wait for codex"), and the decision model only ever chooses among a node's
# children. Every complete path is a sentence the grammar reads as its plan.

# A split is offered only when the request says split, pane, side or a direction: "a new tab" says none.
SPLIT_CUE = re.compile(r'\b(?:split|pane|right|left|above|below|beneath|under|underneath|over|up|down|top|'
                       r'bottom|upper|lower|beside|side|vertical|vertically|horizontal|horizontally)\b', re.I)
# "in the same directory": the directory named earlier is meant again
SAME = re.compile(r'\b(?:the\s+)?same\s+(?:directory|dir|folder|repo|repository|project|place|path)\b', re.I)
# "model gpt-6", "with the model opus": a model named as one, which no plan may leave out
MODEL_SAID = re.compile(r'\bmodel\s+(?:to\s+|is\s+|=\s*)?(' + MODEL_NAME.pattern + ')', re.I)
# Each join ("and then", a comma, a semicolon) allows one more operation.
JOIN = re.compile(r'(?:[,;]\s*)?(?:\b(?:and|then|also|plus|afterwards|next)\b[\s,]*)+|[,;]', re.I)
MOST_PLANS = 4000       # a request with more possible plans than this gets none
MOST_STEPS = 20000      # nor does one whose search tries more partial plans than this
ENDING = '(complete)'
# A numbered or bulleted list item, or an ordinal that opens one, allows one more operation like a join.
LIST_MARK = re.compile(r'(?:^|(?<=[\s(]))(?:\(?\d[.):]|\(?[a-d]\)|[-*•])(?=\s)|\b(?:second|third|fourth|finally|'
                       r'lastly)\b', re.I)
# Details no plan's wording can say. A plan for a request that names one would quietly drop it, which changes
# the request ("tell codex in pane 9: ...", "resume claude ...", "with high effort", "codex --yolo"), so such a
# request gets no plan unless the plan's own sentence says the detail too (outside the text it copies).
UNSAID = re.compile(
    r'\b(?:pane|tab|session|window|thread)\s*#?\s*\d+\b|\bpane\d+'      # an address by number
    r'|\b(?:claude|codex)\s+\d+\b'
    r'|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{12,}\b'     # an id
    r'|\b(?:session|pane|tab|thread|id)\s*#?\s*(?=[\w-]*\d)[0-9a-f][\w-]*'      # "session 0b7e-44c1", "id 7f3c2a9e"
    r'|\bbroker\b|\bresum\w*|\b(?:effort|reasoning|thinking)\b|\bwindow\b|\bthe\s+other\b'
    r'|\b(?:(?<!directory )(?<!dir )(?<!folder )(?<!repo )(?<!repository )(?<!project )(?:titled|named|called)'
    r'|title)\b'
    r'|(?<!\S)--?[a-z][\w-]*|\bssh\b|\bhost\b|\bremote\b'
    r'|(?<!\S)(?:~|\.\.?)/?(?=[\s,;:!?]|\.?$)', re.I)       # a home or parent directory spans() cannot copy
# A model's own name, said without "model" or "with": "opus model", "claude, opus, ~/x". One spans() does not
# copy cannot be carried, so the request gets no plan rather than one without it.
MODEL_WORD = re.compile(r'(?<![\w.-])(?:opus|sonnet|haiku|fable|gpt-[\w.-]+|o\d(?:-mini)?)(?![\w-])', re.I)
# A session addressed by a title: "tell codex session Nightly Build: ...", "the claude agent rc-7".
TITLED = re.compile(r'\b(?i:claude|codex)\s+(?i:session|agent|instance)\s+(?!(?i:in|at|on|here|there|that|which|'
                    r'who|to|with|and|for|from|running|now|is|was|you|again|already|first|too|as|by|of|so|then|'
                    r'but|or|if)\b)'
                    r'(?:[A-Z][\w-]*|[a-z][\w-]*[\d-][\w-]*)')
# A level of effort said straight after a model's name: "with model gpt-5.5-mint high"
LEVEL = re.compile(r'\b(?:low|medium|high|xhigh|max|minimal)\b', re.I)
# A question asked with an auxiliary verb first is never a command: "is claude running in ~/x".
ASKED = re.compile(r'^\W*(?:is|are|was|were|do|does|did|has|have|had|isn\'t|aren\'t|doesn\'t|didn\'t)\b', re.I)
# A clause that opens with a verb none of the five tools does: "..., then delete the build folder".
EXTRA = re.compile(r'(?:^|[,;:.)\u2022]|(?<!\S)[-*]|\b(?:and|then|also|plus|next|afterwards|after\s+that|'
                   r'before\s+that|but|first)\b)\s*'
                   r'(?:(?:please|also|then|and|just|go\s+ahead\s+and|quickly)\s+)*'
                   r'(?:delete|remove|edit|commit|push|merge|rebase|deploy|build|compile|install|uninstall|rename|'
                   r'move|copy|fix|print|show\s+me\s+(?:the\s+)?(?:output|log|transcript|error|reply|status)|'
                   r'read\s+me|list|find|'
                   r'search|play|save|restart|reboot|work\s+out|calculate|compute|figure\s+out|check|test|tag|type|'
                   r'email|mail|ping\s+me|give\s+me\s+(?:a|the)\s+(?:status|last|output|log|reply|error)|'
                   r'pull\s+up\s+the\s+(?:log|output|transcript)|summari[sz]e|explain|translate|download|upload|'
                   r'switch|empty|clear|run\s+(?:the\s+)?(?:tests?|make|npm|pytest|build|suite|linter|script))\b',
                   re.I)
# Saying not to, or telling what happened before, is not a request to act now.
NEGATED = re.compile(r"\b(?:don'?t|doesn'?t|didn'?t|no\s+need|yesterday|earlier|ago|"
                     r"last\s+(?:week|night|month|time)|refrain|avoid|never|previously)\b"
                     r"|\b(?:i|we)\s+(?:did|already)\b|\b(?:talked|discussed|spoke|mentioned)\b"
                     r"|\b(?:i|we|they)\s+(?:just\s+)?(?:started|launched|opened|messaged|told|sent|waited|kicked|"
                     r"relayed|pinged|ran|asked)\b|\b(?:i|we)'d\s+(?:started|opened|asked|told)\b", re.I)
# A time to act other than now, or a limit no tool keeps: "in an hour", "after lunch", "no longer than 30 s".
TIMED = re.compile(r'\bin\s+(?:an?|one|two|five|ten|\d+)\s+(?:hours?|minutes?|mins?|seconds?|secs?)\b|'
                   r'\bafter\s+(?:lunch|dinner|breakfast|the\s+meeting)\b|\btomorrow\b|\btonight\b|'
                   r'\blater\s+(?:on|today)\b|\bno\s+(?:longer|more)\s+than\b|\bat\s+most\b|\btimeout\b|'
                   r'\bwhenever\b|\bin\s+case\b|\berrors?\s+out\b', re.I)
# Something no tool does to a session or the desktop: "as root", "take a screenshot", "resize it", "post it to
# Slack", "set the brightness"; or an application Kilix does not have.
BEYOND = re.compile(r'\b(?:as\s+root|sudo|screenshots?|resiz\w+|focus\w*|full[\s-]?screen|maximi[sz]\w*|minimi[sz]\w*|'
                    r'half\s+the\s+screen|brightness|volume|memory|ram|cpus?|gpus?|priority|admin\w*|theme|dark\s+mode|'
                    r'wallpaper|fonts?|reopen\w*|reattach\w*|reviv\w*|slack|git|close|kill|quit|exit|'
                    r'spreadsheet|calendar|(?<!file\s)browser|chrome|firefox|terminal|e-?mail|music|spotify|settings|camera|'
                    r'clock|photos)\b', re.I)
# A file's name left in the request's own words: "open notes.txt in the calculator".
FILE_NAME = re.compile(r'(?<![\w./-])[\w-]+\.(?:txt|md|py|json|csv|log|pdf|docx?|xlsx?|png|jpe?g|html?|js|ts|ya?ml|toml|'
                       r'sh|ini|cfg)\b', re.I)
# Many sessions at once: "five claude sessions".
MANY = re.compile(r'\b(?:three|four|five|six|seven|eight|nine|ten|dozen|[3-9]|\d\d+)\s+(?:\w+\s+)?'
                  r'(?:sessions|instances|agents|copies|claudes|codexes|tabs|splits|panes)\b', re.I)
# A pane a wait or message is for is named only by its number ("pane 9"): any other pane word names none.
PANE_WORD = re.compile(r'\bpanes?\b', re.I)
START_KINDS = ('start_claude', 'start_codex', 'open_app')
STEPPED = re.compile(r'(?:\b(?:step|item|part|phase)\s+\d+|^\s*\(?\w\)|[;.]\s*\(?\w\))\s*:?\s*\[TEXT', re.I)
# A sender named by its pane: "from pane 4", "I am pane 4".
SENDER = re.compile(r"\b(?:from|i\s+am|i'm|this\s+is|sent\s+by)\s+(?:the\s+\w+\s+in\s+)?(?:kilix\s+)?pane\b", re.I)
# A split or pane said without its direction: "start codex in a split" leaves where open, which no plan may decide.
UNPLACED = re.compile(r'\b(?:split|panes?)\b', re.I)
DIRECTION = re.compile(r'\b(?:right|left|above|below|beneath|under|underneath|top|bottom|upper|lower|down|upwards?|'
                       r'downwards?|vertical\w*|horizontal\w*|side)\b|\bup\s+(?:split|pane)|\b(?:split|pane)\s+up\b',
                       re.I)      # "bring up codex in a split" names no direction
# A time of day, or "the other one", which spans() may take for a directory's name, so it is looked for in the
# request with only its texts hidden: "At 6pm, start codex", "wait for the one in the other directory".
LOOSE = re.compile(r'\b\d{1,2}(?::\d\d)?\s*(?:am|pm)\b|\bthe\s+other\b', re.I)
# "then wait for it" after two sessions are named: which one is meant is left open.
SINGLE = re.compile(r'\b(?:for|tell|message|ping|ask|to|let)\s+(?:it|that\s+session|that\s+agent)\b'
                    r'|\b(?:to|for|tell|message|ping)\s+the\s+(claude|codex)\b', re.I)
# "start codex here and in ~/x": one more operation like the one before, in another directory.
AND_IN = re.compile(r'\b(?:and|then)\s+(?:also\s+)?(?:in|inside|under|at)\s+\x00', re.I)
# Another agent than the two: "start claude here and gemini in ~/x" asks for something no tool does.
OTHER_AGENT = re.compile(r'\b(?:gemini|grok|aider|copilot|cursor|chatgpt|llama|cline|devin|goose|opencode|qwen|mistral|'
                         r'deepseek|supervisor|coordinator|orchestrator|reviewer|subagent|worker)s?\b', re.I)
# "it", "them", "that session": a later wait or message for an agent named earlier means the same session.
BACK = re.compile(r'\b(?:it|its|them|their|both)\b'
                  r'|\b(?:that|this|the\s+same)\s+(?:same\s+)?(?:session|one|agent|claude|codex)\b', re.I)
# "In ~/x, start codex ... and claude ...": a directory said before the first operation is meant for every agent.
FRONTED = re.compile(r'^\W*(?:in|inside|within|from|under|for)\s+(?:the\s+)?(?:directory\s+|folder\s+|repo\s+)?'
                     r'\x00+\s*,', re.I)
# A split is offered only when the request says split, pane or side, or names a direction: "bring codex up" does not.
SPLIT_SAID = re.compile(r'\b(?:split|pane|side|beside)\b', re.I)
# Each operation verb outside the copied text asks for an operation of its kind, so a plan has at least as
# many operations as the request has such verbs: "wait for claude, tell it 'x', then wait for it again" is three.
OPERATION_VERBS = (
    (re.compile(r'(?<!it )(?<!them )(?<!to )'      # "have it start with ...", "ask it to open ..." is its task
                r'(?:\b(?:start|launch|open|load|view|boot)(?:s|ed|ing)?\b|\b(?:run|spin\s+up|fire\s+(?:up|off)|'
                r'kick\s+off|bring\s+up|pull\s+up|set\s+up)\b|\bget\s+(?:[\w\x00]+\s+){1,4}going\b)', re.I),
     ('start_claude', 'start_codex', 'open_app')),
    (re.compile(r'(?<!it )(?<!them )(?<!to )(?:\b(?:wait|await|hold\s+on|hold\s+off|hang\s+on|sit\s+tight)'
                r'(?:s|ed|ing)?\b|\blet\s+(?:[\w\x00]+\s+){1,4}(?:finish|wrap))', re.I),
     ('wait_for_agent',)),
    (re.compile(r'(?<!the )(?<!a )(?<!this )(?<!that )(?<!its )(?<!my )(?<!your )'      # "the message" is a noun
                r'(?<!it )(?<!them )(?<!to )'
                r'\b(?:tell|message|send|say|ask|ping|relay|reply|forward|notify|nudge|alert|inform|remind)'
                r'(?:s|d|ed|ing|ged)?\b|\bpass\s+(?:it\s+)?along\b|\bpass\s+(?:it|them)\s+(?:this|the|on)\b|'
                r'\bpass\s+(?:this|that|these)\s+(?:along|on)\b|'
                r'\blet\s+(?:[\w\x00]+\s+){1,4}know\b', re.I), ('message_agent',)))
# "wait for it", "tell that session": a back-reference is one more operation on an agent already named.
REFERRED = re.compile(r'\b(?:for|tell|message|ping|ask|to|let)\s+(?:it|them|that\s+(?:one|session|agent)'
                      r'|the\s+same\s+(?:one|session|agent)|that\s+same\s+(?:session|one))\b', re.I)
# "then wait for both": two more operations, one on each agent named.
BOTH = re.compile(r'\b(?:for|tell|message|ping|ask|to)\s+(?:both|them\s+both|both\s+of\s+them)\b'
                  r'(?!\s+(?:of\s+)?(?:the\s+)?(?:claude|codex|sessions|agents))', re.I)
# A join that makes a back-reference an operation of its own; a comma alone ("codex in ~/x, tell it") does not.
REFERRAL_JOIN = re.compile(r'\b(?:and|then|also|afterwards|next)\b|;|[.!]\s', re.I)
# A sentence break joins operations too: "Start codex in ~/a. Start claude in ~/b."
SENTENCE = re.compile(r'(?:[.!?]|\x00)\s+(?=[A-Z])')     # a copied path may end the sentence
# "there is a claude session in ~/x; tell it ...": an agent said to exist is not an operation of its own.
EXISTS = re.compile(r'\b(?:there\s+is|there\'s|there\s+are)\s+(?:a|an|one|the)?\s*(?:\w+\s+)?(?:claude|codex)\b',
                    re.I)
# "here" names the current directory as a place of its own: "start codex here and in ~/x" is two sessions.
HERE = re.compile(r'\bhere\b|\b(?:this|the\s+current)\s+(?:directory|folder|dir)\b', re.I)
# The text is a task ("Task: ...", "with this as its job") or a message ("tell", "ask"): only plans that use it so.
TASKED = re.compile(r'\b(?:tasks?|job|assignment|instructions?|prompt|brief)\b', re.I)
# Hypotheticals, examples and reports are not requests to act now.
UNREAL = re.compile(r'\b(?:suppose|supposing|imagine|imagining|hypothetical\w*|pretend\w*|theoretical\w*|thought\s+'
                    r'experiment|example|sample|tutorial|docs|documentation|quotes?|quoted|wrote|might|wish|wished|'
                    r'would(?!\s+(?:you|i|we|like|love))|could(?!\s+(?:you|i|we)))\b', re.I)
# A field label before a colon ("Directory: ~/x. Model: gpt-6.") is not a text to pass on.
FIELD = re.compile(r'\b(?:directory|dir|folder|cwd|path|location|model|placement|place|split|where)\s*:\s', re.I)
# "that folder we talked about": a directory referred to that the request never names.
UNNAMED_PLACE = re.compile(r'\b(?:that|the\s+same|the\s+usual|the\s+other)\s+(?:folder|directory|dir|repo|'
                           r'repository|project|place|path)\b', re.I)


def _context(request):
    mentions, texts, models, masked, plain = _scan(request)
    paths = list(dict.fromkeys(mentions))
    agents, apps, directions = _named(paths, plain)
    spoken = [name.casefold() for name in re.findall(r'(?<![\w.-])(?:claude|codex)(?![\w-])', plain, re.I)]
    again = sum(found[0].casefold() != 'both' or bool(mentions) for found in THERE.finditer(plain))
    again += len(SAME.findall(plain)) if mentions else 0
    outside = request
    for start, end, _ in _quotes(request):
        outside = outside[:start] + _GONE * (end - start) + outside[end:]
    panes = [(found.start(), found['pane']) for found in _PANE_SPAN.finditer(outside)]
    return {'request': request, 'masked': masked, 'paths': paths, 'mentions': mentions, 'texts': texts,
            'targets': targets(request), 'panes': panes, 'unpaned': _PANE_SPAN.sub(_blank, masked),
            'models': models,
            'plain': plain, 'agents': agents, 'apps': apps, 'directions': directions, 'spoken': spoken,
            'owed': len(mentions) + again, 'verbs': [ops for verb, ops in VERBS if verb.search(plain)],
            'said_models': _said_models(models, plain),
            'counts': [(ops, len(verb.findall(plain))) for verb, ops in OPERATION_VERBS],
            'occasions': len(spoken) - len(EXISTS.findall(plain)) + _referrals(plain),
            'here': bool(HERE.search(plain))}


def _said_models(models, plain):
    """The models no plan may leave out: one named as a model ("model gpt-6"), one of the grammar's own
    ("with opus"), anything spans() copies that looks like a version ("with gpt-5.5"), and a model's own name
    said bare ("opus model"), which a plan cannot carry when spans() does not copy it."""
    known = [model for allowed in contract._MODELS.values() for model in allowed]
    said = [name.rstrip('.,:') for name in MODEL_SAID.findall(plain)]
    said += [model for model in models if model in known or re.search(r'\d', model) and re.search(r'[A-Za-z]', model)]
    said += [found.group(0).rstrip('.,:') for found in MODEL_WORD.finditer(plain)]
    return list(dict.fromkeys(said))


def _referrals(plain):
    """How many back-references ("wait for it") follow a join after the agent they refer to: each is one
    more operation. "Codex in ~/x - wait for it" names one."""
    count = 0
    for found in REFERRED.finditer(plain):
        named = list(re.finditer(r'\b(?:claude|codex)\b', plain[:found.start()], re.I))
        if named and REFERRAL_JOIN.search(plain[named[-1].end():found.start()]):
            count += 1
    return count + 2 * len(BOTH.findall(plain))


def _clauses(context):
    """Every single call the request's spans allow that the grammar reads alone."""
    out, dirs = [], [None] + [path for path in context['paths'] if not path.lower().endswith('.pdf')]
    places = ['tab']
    if SPLIT_CUE.search(context['plain']) and (context['directions'] or SPLIT_SAID.search(context['plain'])):
        places += sorted(context['directions']) or ['right', 'left', 'down', 'up']
    for agent in context['agents']:
        name = 'start_' + agent
        models = [None] + [model for model in context['models'] if model in contract._MODELS[name]]
        for directory in dirs:
            for place in places:
                for model in models:
                    for task in [None] + context['texts']:
                        args = {key: value for key, value in (('directory', directory), ('place', place),
                                                              ('model', model), ('task', task))
                                if value is not None and value != 'tab'}
                        out.append({'name': name, 'arguments': args})
            where = {'directory': directory} if directory else {}
            out.append({'name': 'wait_for_agent', 'arguments': {'agent': agent, **where}})
            out += [{'name': 'message_agent', 'arguments': {'agent': agent, **where, 'text': text}}
                    for text in context['texts']]
    for target in context['targets']:      # a pane and broker copied as written; the agent only if named
        where = {key: value for key, value in target.items() if value is not None}
        for agent in [None] + context['agents']:
            who = {'agent': agent} if agent else {}
            out.append({'name': 'wait_for_agent', 'arguments': {**who, **where}})
            out += [{'name': 'message_agent', 'arguments': {**who, **where, 'text': text}}
                    for text in context['texts']]
    for app in context['apps']:
        out.append({'name': 'open_app', 'arguments': {'app': app}})
        if app == 'kilix-pdf':
            out += [{'name': 'open_app', 'arguments': {'app': app, 'file_path': path}}
                    for path in context['paths'] if path.lower().endswith('.pdf')]
    return [call for call in out if _carried([call])]


def _uses(calls):
    places = [value for call in calls for key, value in call['arguments'].items() if key in ('directory', 'file_path')]
    said = [value for call in calls for key, value in call['arguments'].items() if key in ('task', 'text')]
    who = [call['name'].split('_')[1] if call['name'].startswith('start_') else
           call['arguments'].get('agent') or call['arguments'].get('app') for call in calls]
    return places, said, who, {call['arguments'].get('place') for call in calls}, {call['name'] for call in calls}


def _gains(context, calls, call):
    """Whether `call` covers something the plan so far does not: a directory still
    owed, a quoted text, an agent or application as often as it is named, a
    direction, or an operation a verb of the request asks for."""
    places, said, who, dirs, names = _uses(calls)
    args = call['arguments']
    for key in ('directory', 'file_path'):
        if key in args and (len(places) < context['owed']
                            or places.count(args[key]) < context['mentions'].count(args[key])):
            return True
    if any(key in args and not any(args[key] in value for value in said) for key in ('task', 'text')):
        return True
    name = call['name'].split('_')[1] if call['name'].startswith('start_') else args.get('agent') or args.get('app')
    if name not in who or who.count(name) < context['spoken'].count(name):
        return True
    if args.get('place', 'tab') != 'tab' and args['place'] not in dirs:
        return True
    if any(call['name'] in ops and not names & set(ops) for ops in context['verbs']):
        return True
    kinds = [item['name'] for item in calls]
    if any(call['name'] in ops and sum(kind in ops for kind in kinds) < count for ops, count in context['counts']):
        return True     # a verb asks for one more operation of this kind
    if name in context['agents'] and sum(who.count(agent) for agent in context['agents']) < context['occasions']:
        return True     # "wait for it": one more operation on an agent already named
    return (context['here'] and 'directory' not in args and name in context['agents']
            and all('directory' in item['arguments'] for item in calls if 'app' not in item['arguments']))


def _owing(context, calls):
    """A lower bound on the operations still needed: each adds at most one
    directory, text, agent or application, and direction."""
    places, said, who, dirs, _names = _uses(calls)
    paths = max(context['owed'] - len(places),
                sum(max(0, context['mentions'].count(path) - places.count(path)) for path in context['paths']))
    texts = sum(1 for text in context['texts'] if not any(text in value for value in said))
    named = max(len(set(context['agents'] + context['apps']) - set(who)),
                sum(max(0, context['spoken'].count(agent) - who.count(agent)) for agent in context['agents']))
    kinds = [call['name'] for call in calls]
    verbs = sum(max(0, count - sum(kind in ops for kind in kinds)) for ops, count in context['counts'])
    occasions = context['occasions'] - sum(who.count(agent) for agent in context['agents'])
    here = context['here'] and bool(context['paths']) and all(
        'directory' in call['arguments'] for call in calls if 'app' not in call['arguments'])
    return max(paths, texts, named, len(context['directions'] - dirs), verbs, occasions, here + paths)


def _unsaid(context):
    """The details the request names that a plan's wording would have to say: an address by pane, tab or
    session number or id, a broker, resuming, an effort or level, a title, a flag, a host, a home or parent
    directory spans() does not copy."""
    plain = context['plain']
    found = [match.group(0) for pattern in (UNSAID, TITLED) for match in pattern.finditer(plain)]
    for model in context['models']:
        at = plain.find(model)
        after = LEVEL.match(plain[at + len(model):].lstrip(' ,')) if at >= 0 else None
        if after:
            found.append(after.group(0))
    return found


def _consistent(calls):
    """Whether the waits and messages for one agent name its directory alike: "start claude in ~/x; tell it
    ...; wait for it; tell it ..." never names ~/x for one of them and leaves it out for another. Two
    directories are two sessions, and may both be named; an agent named by its pane in one call and
    without it in another is not."""
    named, paned = {}, set()
    for call in calls:
        agent = call['arguments'].get('agent')
        if 'pane' in call['arguments']:
            paned.add(agent)
        elif agent:
            named.setdefault(agent, set()).add(call['arguments'].get('directory') is None)
            if len(named[agent]) > 1:
                return False
    return not paned & set(named)       # "tell claude in pane 2 ...; then wait for claude": which claude?


def _paned(context, calls):
    """Whether each pane goes to the wait or message whose own clause names it (_slots()), or to one that
    refers back to it by "it" or "that session" ("wait for pane 3, then tell it ..."), and to no other:
    "tell claude in pane 2 ..., then wait for claude" may mean another claude, so it has no plan. The
    agent beside a pane is the one named between the last join and the pane, if any."""
    if not context['panes']:
        return True
    slots, plain = _slots(context, calls), context['plain']
    if slots is None:
        return False
    bounds = list(zip(slots, slots[1:] + [len(context['request'])]))
    for number, ((start, end), call) in enumerate(zip(bounds, calls)):
        args, clause = call['arguments'], plain[start:end]
        talks = call['name'] in ('wait_for_agent', 'message_agent')
        for at, pane in context['panes']:
            here, used = start <= at < end, args.get('pane') == pane
            if here and talks and not used:
                return False
            back = talks and bool(BACK.search(clause)) and any(earlier['arguments'].get('pane') == pane
                                                               for earlier in calls[:number])
            if used and not here and not at < slots[0] and not back:
                return False
        if 'pane' in args:
            at = next(at for at, pane in context['panes'] if pane == args['pane'])
            joins = list(JOIN.finditer(plain[:at]))
            beside = plain[joins[-1].end() if joins else 0:at]      # "then nudge the codex in pane 9"
            said = {name for name, cue in AGENT_CUE.items() if cue.search(beside)}
            if said != ({args['agent']} if 'agent' in args else set()):
                return False    # the agent beside a pane is the one its clause names, and only that one
        elif talks and args.get('agent') in {other['arguments'].get('agent') for other in calls
                                             if 'pane' in other['arguments']}:
            return False        # one agent named by its pane and also without it
    return True


# Words that ask for a wait: its verbs, and what a session does when it is done ("let it finish", "is through").
WAITED = re.compile(r'\b(?:wait\w*|await\w*|hold\w*|hang\w*|sit\s+tight|finish\w*|done|through|wrap\w*|'
                    r'complete\w*|idle|'
                    r'settle\w*|ready|time\s+to)\b', re.I)


def _waited(context, calls):
    """Whether a plan that waits has a request that asks for a wait: "make pane 311 wider" never does."""
    return not any(call['name'] == 'wait_for_agent' for call in calls) or bool(WAITED.search(context['plain']))


# "wait for them", "tell both": one operation on every agent the plan has. "claude and codex": two agents
# one verb goes with.
PLURAL = re.compile(r'\b(?:(wait|hold|hang)\w*\s+(?:on\s+|off\s+)?(?:for\s+|on\s+|till\s+|until\s+)?|'
                    r'(tell|message|ping|ask|nudge|send|let)\w*\s+)(?:them|both|each|all|everyone)\b', re.I)
PAIRED = re.compile(r'\b(claude|codex)(?:\s+(?:in|at|inside|under)\s+\x00+)?\s*(?:,|\band\b|&|\bplus\b)\s*'
                    r'(?:the\s+|a\s+)?(claude|codex)\b', re.I)


def _whole(context, calls):
    """Whether "wait for them" waits for every agent the plan names, and "open claude and codex and
    wait" starts both rather than one of each."""
    named = {call['arguments'].get('agent') or call['name'].split('_')[1] for call in calls
             if call['name'] != 'open_app' and 'pane' not in call['arguments']}
    for found in PLURAL.finditer(context['plain']):
        kind = 'wait_for_agent' if found.group(1) else 'message_agent'
        if not named <= {call['arguments'].get('agent') for call in calls if call['name'] == kind}:
            return False
    for found in PAIRED.finditer(context['plain']):
        first = {}
        for call in calls:
            agent = call['arguments'].get('agent') or call['name'].split('_')[1]
            first.setdefault(agent, 'start' if call['name'].startswith('start_') else call['name'])
        one, two = found.group(1).lower(), found.group(2).lower()
        if one != two and first.get(one) != first.get(two):
            return False
    return True


def _keeps(context, calls):
    """Whether the plan uses every directory, file, text and pane the request copies: "open notes.txt in
    the calculator" has no plan that drops the file."""
    used = {value for call in calls for key, value in call['arguments'].items()
            if key in ('directory', 'file_path', 'task', 'text', 'pane')}
    return all(value in used for value in context['paths'] + context['texts'] + [pane for _, pane in context['panes']])


def _says(calls, detail, texts):
    """Whether the plan's sentence says the detail itself, outside the texts it copies."""
    wording = sentence(calls)
    for text in texts:
        wording = wording.replace(_quote(text), '')
    return re.sub(r'\s+', ' ', detail).casefold() in wording.casefold()


def _ruled_out(context):
    """Why the request's words leave no plan the generator may offer, beyond refusal(): a detail no wording
    says is checked per plan, so this covers a question asked with an auxiliary verb first and an extra
    action the five tools do not have."""
    if ASKED.search(context['plain']):
        return 'question'
    if re.fullmatch(r'\W*(?:claude|codex)\W*', context['plain'], re.I):
        return 'incomplete'     # an agent's name alone could mean starting it or waiting for it
    if EXTRA.search(context['plain']):
        return 'unsupported'
    if UNREAL.search(context['plain']) or NEGATED.search(context['plain']) or LOOSE.search(context['masked']) \
            or TIMED.search(context['masked']):
        return 'not now'
    if BEYOND.search(context['plain']) or MANY.search(context['plain']) or _unknown_clause(context):
        return 'unsupported'
    if re.search(r'(?:[,;:]|\b(?:and|then|also|next|second|third|finally))[\s.!?]*$', context['masked'], re.I):
        return 'incomplete'     # "start claude in ~/x; second," and nothing after it
    if OTHER_AGENT.search(context['plain']):
        return 'unnamed'
    for found in SINGLE.finditer(context['plain']):
        before = context['plain'][:found.start()]
        named = [name.casefold() for name in re.findall(r'\b(?:claude|codex)\b', before, re.I)]
        places = {path for path in context['paths'] if 0 <= context['masked'].find(path)
                  < context['masked'].find(found.group(0)) if found.group(0) in context['masked']}
        places |= {pane for at, pane in context['panes'] if at < found.start()}
        if found.group(1):      # "the claude": two claudes named before it
            if named.count(found.group(1).casefold()) > 1:
                return 'ambiguous'
        elif len(named) > 1 or len(places) > 1:
            return 'ambiguous'      # "it" after two sessions, by agent or by directory
    if FIELD.search(context['plain']) or TASKED.search(context['plain']) and not context['texts']:
        return 'unsaid'     # a labelled field the colon rule reads as text, or a task with no text to give
    if FILE_NAME.search(context['plain']):
        return 'unsaid'     # a file named that spans() does not copy, so no plan can carry it
    if context['request'].rstrip().endswith(':'):
        return 'unsaid'     # "let claude know:" and nothing after it
    if UNPLACED.search(context['unpaned']) and not DIRECTION.search(context['unpaned']):
        return 'unsaid'
    counts = dict(context['counts'])
    if PANE_WORD.search(context['unpaned']) and not counts[START_KINDS] and (
            counts[('wait_for_agent',)] or counts[('message_agent',)] or not DIRECTION.search(context['unpaned'])):
        return 'unsaid'     # a pane named other than by its number ("the pane on the left", "my other pane")
    if STEPPED.search(context['masked']):
        return 'unsaid'         # "Step 2: wait for it" is a step of the request, not a text to pass on
    if SENDER.search(context['unpaned']) or SENDER.search(context['masked']):
        return 'unsaid'     # "from pane 4, tell pane 9 ...": who sends is no part of a plan
    if UNNAMED_PLACE.search(context['plain']) and not context['mentions']:
        return 'unnamed'
    if (re.search(r'\?["\'\u201d]?\s*$', context['request']) and context['texts'] and not TASKED.search(context['plain'])
            and not dict(context['counts']).get(('message_agent',))):
        return 'question'       # "start codex in ../x: would that pick the parent?" asks; it gives no task
    return None


# The words a clause of a request for the five tools may open with: their verbs, and the fillers, joins,
# places and names around them. A clause that names nothing the tools act on and opens with another verb
# ("..., and set the theme to dark") asks for something none of them does.
OPENERS = re.compile(r"(?:start|launch|open|load|view|boot|run|spin|fire|kick|bring|pull|set|get|put|give|have|"
                     r"wait|await|hold|hang|sit|let|tell|message|send|say|ask|ping|relay|reply|forward|notify|pass|"
                     r"nudge|drop|inform|alert|remind|write|show|make|need|want|like|keep|stay|go|use|"
                     r"i|we|i'\w+|we'\w+|you|me|my|our|your|us|it|its|this|that|these|those|the|a|an|and|then|also|"
                     r"plus|so|just|now|kindly|quickly|simply|first|second|third|fourth|next|finally|lastly|after|"
                     r"afterwards|once|both|all|each|here|there|in|inside|within|at|on|into|with|using|under|from|for|"
                     r"to|of|by|as|via|split|pane|tab|right|left|up|down|above|below|beneath|underneath|top|bottom|"
                     r"upper|lower|side|new|fresh|another|same|current|model|task|message|text|directory|folder|dir|"
                     r"repo|repository|project|path|again|too|please|thanks|hey|hi|ok|okay|yes|could|would|can|will|"
                     r"instead|either|one|two|three|four|session|agent|step|item|part|phase|no)", re.I)


def _unknown_clause(context):
    """Whether a clause names no agent, application, pane or copied span and opens with a word that is
    none of OPENERS: "start codex in ~/x and set the theme to dark"."""
    for clause in JOIN.split(context['plain']):
        if not clause.strip() or _GONE in clause or any(cue.search(clause) for cue in (*AGENT_CUE.values(),
                                                                                      *APP_CUE.values())):
            continue
        if MODEL_WORD.search(clause) or MODEL_SAID.search(clause):
            continue        # "..., sonnet model": a model said for the clause before
        head = re.search(r"[A-Za-z][\w'-]*", clause)
        if head and not re.search(r'\d', clause[:head.start()]) and not OPENERS.fullmatch(head.group(0)) \
                and not head.group(0).lower().endswith(('ing', 'ed', 'ly')):
            return True
    return False


def _referred(context, calls):
    """Whether the plan follows the request's references: with "it" or "that session" in the request, a
    wait or message for an agent already in the plan is for that agent's session, in its directory; with a
    directory fronted before the first operation ("In ~/x, ..."), every agent's call names a directory."""
    if FRONTED.search(context['plain']):
        if any('agent' in call['arguments'] or call['name'].startswith('start_') for call in calls
               if 'directory' not in call['arguments']):
            return False
    if BACK.search(context['plain']):
        last = {}
        for call in calls:
            name, args = call['name'], call['arguments']
            agent = name.split('_')[1] if name.startswith('start_') else args.get('agent')
            if agent is None:
                continue
            if name in ('wait_for_agent', 'message_agent') and agent in last and args.get('directory') != last[agent]:
                return False
            last[agent] = args.get('directory')
    return True


# "Claude and codex in ./x": a clause that is only a name and "and" shares the next clause's directory.
BARE = re.compile(r'\W*(?:\w+\s+){0,2}?(?:the\s+)?(?:claude|codex)\s*,?\s*(?:and|&)\s*', re.I)
# Words that carry a directory named elsewhere to this clause: "the same folder", "there", "both in ~/x".
CARRY = re.compile(r'\b(?:same|there|both|each|all)\b', re.I)
# "wait for it", "tell that one": the operation is on the agent named last before it.
PRONOUN = re.compile(r'\s*(?:for\s+|to\s+)?(?:it|that\s+one|that\s+session|that\s+agent|the\s+same\s+'
                     r'(?:one|session))\b', re.I)


def _places(context):
    """[(position, kinds of call, name or None)] in the request that an operation can be matched to, in
    order. Each operation verb is one, for calls of its kind on the agent or application it names before the
    next verb ("open the file manager"), on the agent named last before it when it says "it" ("wait for
    it"), or on any. "For both" is one more of its verb's kind. A name no verb took is one for calls on it,
    where a verb of that kind comes before it ("start codex and claude") or the request has none ("notepad,
    then codex in ~/x")."""
    if '_places' in context:
        return context['_places']
    plain, first = context['plain'], {}
    names = sorted([(found.start(), found.end(), name) for name, cue in {**AGENT_CUE, **APP_CUE}.items()
                    for found in cue.finditer(plain)]
                   + [(at, at + 1, 'pane ' + pane) for at, pane in context.get('panes', [])])    # "pane 9" names one
    verbs = sorted((found.start(), found.end(), ops) for verb, ops in OPERATION_VERBS for found in verb.finditer(plain))
    for ops in [ops for _verb, ops in OPERATION_VERBS]:
        first[ops] = next((where for where, _end, kind in verbs if kind == ops), None)
    places, taken = [], set()
    for number, (where, end, ops) in enumerate(verbs):
        until = verbs[number + 1][0] if number + 1 < len(verbs) else len(plain)
        named = next(((at, name) for at, _end, name in names if end <= at < until), None)
        if named:
            taken.add(named[0])
            name = named[1]
        elif PRONOUN.match(plain, end):
            name = next((name for at, _end, name in reversed(names) if at < where and name in AGENT_CUE), None)
        else:
            name = None
        places.append((where, set(ops), name))
    places += [(found.start(), {'wait_for_agent' if found.group(0).lower().startswith('for') else 'message_agent'},
                None) for found in BOTH.finditer(plain)]     # "wait for both" is two waits, "tell both" two
    for found in AND_IN.finditer(plain):
        before = [place for place in places if place[0] < found.start()]
        if before:
            places.append((found.start(), *max(before, key=lambda place: place[0])[1:]))
    for at, _end, name in names:
        if at in taken:
            continue
        before = [ops for where, _end, ops in verbs if where < at]
        if name.startswith('pane '):     # a pane is only waited for or messaged, as the verb before it says
            kinds = {'wait_for_agent', 'message_agent'} & set(before[-1]) if before else \
                {'wait_for_agent', 'message_agent'}
        elif before:
            kinds = set(before[-1])     # "start codex and claude", "wait for codex, then for claude"
        else:
            kinds = {op for ops, verb in first.items() if verb is None for op in ops}     # "notepad, then codex"
        places.append((at, kinds, name))
    context['_places'] = sorted(places, key=lambda place: place[0])
    return context['_places']


def _stated(context, calls):
    """Whether the plan's calls come in the order the request states them: each call, in turn, can be matched
    to a later place in the request than the call before it (_places()). "Start codex in ~/x, then wait for
    it" is not "wait; then start"."""
    return _slots(context, calls) is not None


def _slots(context, calls):
    """The positions _stated() matches the calls to, or None."""
    places, used, last, out = _places(context), set(), -1, []
    for call in calls:
        args = call['arguments']
        who = {args.get('agent') or args.get('app') or call['name'].split('_')[1]}
        if 'pane' in args:      # a session named by its pane: by the pane, or by its agent beside it
            who = {'pane ' + args['pane']} | ({args['agent']} if 'agent' in args else set())
        at = next((number for number, (where, kinds, name) in enumerate(places) if number not in used
                   and where > last and call['name'] in kinds and (name is None or name in who)), None)
        if at is None:
            return None
        used.add(at)
        last = places[at][0]
        out.append(last)
    return out


def _occurs(request, value, others=()):
    """Where the request says a copied value whole, not as part of a longer path ("~/x" in "~/x/y") or of
    a longer text among others ("done" in "also done")."""
    inside = [(found.start(), found.end()) for other in others if other != value and value in other
              for found in re.finditer(re.escape(other), request)]
    return [found.start() for found in re.finditer(re.escape(value), request)
            if not re.match(r'[\w/-]|\.[\w/]', request[found.end():found.end() + 2])
            and not re.match(r'[\w/.~-]', request[found.start() - 1:found.start()] or ' ')
            and not any(start <= found.start() < end for start, end in inside)]


def _attached(context, calls):
    """Whether each call takes what the request says in its own clause, from its place to the next call's
    (_slots()): the directories, texts and directions said there, and no others. What is said before the
    first operation is for any; a directory said in another clause is the same agent's directory again
    for a wait or message ("wait for it"), or one carried to this clause ("in the same folder", "there",
    "both in ~/x"); a direction said in another clause is carried by "both", "each" or "all"; and a clause
    that is only an agent's name and "and" shares the next one's directory ("claude and codex in ./x"); and
    a wait or message for an agent this plan started in a directory names that
    directory ("start claude in ~/x, ..., then tell claude ...")."""
    slots, request, plain = _slots(context, calls), context['request'], context['plain']
    if slots is None:
        return False
    bounds = list(zip(slots, slots[1:] + [len(request)]))
    for number, ((start, end), call) in enumerate(zip(bounds, calls)):
        args, clause = call['arguments'], plain[start:end]
        carries = bool(CARRY.search(clause) or THERE.search(clause) or SAME.search(clause))
        if BARE.fullmatch(clause) and number + 1 < len(calls):
            following = calls[number + 1]
            if args.get('directory') != following['arguments'].get('directory') or \
                    call['name'].split('_')[0] != following['name'].split('_')[0]:
                return False        # "claude and codex in ./x": both started, or both waited for, in ./x
            continue
        again = {(earlier['arguments'].get('agent') or earlier['name'].split('_')[1],
                  earlier['arguments'].get('directory')) for earlier in calls[:number]}
        who = args.get('agent') or call['name'].split('_')[1]
        for path in context['paths']:
            said = _occurs(request, path)
            here = [at for at in said if start <= at < end]
            used = path in (args.get('directory'), args.get('file_path'))
            takes = 'agent' in args or call['name'].startswith('start_') or args.get('app') == 'kilix-pdf'
            if here and takes and not used:
                return False        # a directory said in this clause belongs to this call
            back = call['name'] in ('wait_for_agent', 'message_agent') and (who, path) in again
            if used and not here and not any(at < slots[0] for at in said) and not back:
                later = any(CARRY.search(plain[other:stop]) and any(other <= at < stop for at in said)
                            for other, stop in bounds)
                if not (carries or later):
                    return False    # nor is one said in another clause, unless it is carried here
        talks = call['name'] in ('wait_for_agent', 'message_agent')
        started = [earlier['arguments'].get('directory') for earlier in calls[:number]
                   if earlier['name'] == 'start_' + str(args.get('agent'))]
        if talks and 'pane' not in args and 'directory' not in args and started and started[-1] \
                and not HERE.search(clause):
            return False            # the session this plan started in ~/x is the one in ~/x
        if (SAME.search(clause) or THERE.search(clause)) and context['paths'] and 'directory' not in args \
                and 'app' not in args:
            return False            # "in the same folder": the directory named before
        for text in context['texts']:
            said = _occurs(request, text, context['texts'])
            here = [at for at in said if start <= at < end]
            used = text in (args.get('task'), args.get('text'))
            if here and not used and (call['name'].startswith('start_') or call['name'] == 'message_agent'):
                return False        # a text said in this clause is this call's task or message
            if used and not here and not any(at < slots[0] for at in said):
                return False
        place = args.get('place', 'tab')
        said = {direction for direction, cue in PLACE_WORD.items() if cue.search(clause)}
        fronted = {direction for direction, cue in PLACE_WORD.items() if cue.search(plain[:slots[0]])}
        shared = re.search(r'\b(?:both|each|all)\b', plain[slots[0]:], re.I)
        if place != 'tab' and said and place not in said:
            return False        # "claude on the left": not a split to the right
        if place != 'tab' and not said and len(calls) > 1 and place not in fronted and not shared:
            return False        # nor one said for another call
        if call['name'].startswith('start_') and said and place == 'tab':
            return False
    return _named_order(context, calls)


def _named_order(context, calls):
    """Whether calls of one kind that "for both" or "and" share go in the order their agents are named:
    "start claude and codex, then wait for both" waits for claude first."""
    plain = context['plain']
    first = {name: found.start() for name, cue in AGENT_CUE.items() for found in [cue.search(plain)] if found}
    for one, two in zip(calls, calls[1:]):
        if one['name'] == two['name'] and one['name'] in ('wait_for_agent', 'message_agent'):
            a, b = one['arguments'].get('agent'), two['arguments'].get('agent')
            if a != b and a in first and b in first and BOTH.search(plain) and first[a] > first[b]:
                return False
    return True


def _as_said(context, calls):
    """Whether the plan uses the request's texts as it says: as a task when it speaks of a task and no verb
    asks for a message, as a message when a verb asks for one and it speaks of no task; and whether it has a
    session in the current directory when the request says "here" besides naming directories."""
    tasks = [call['arguments']['task'] for call in calls if 'task' in call['arguments']]
    sent = [call['arguments']['text'] for call in calls if 'text' in call['arguments']]
    messages = dict(context['counts']).get(('message_agent',), 0)
    if TASKED.search(context['plain']) and not messages and sent:
        return False
    if messages and not TASKED.search(context['plain']) and tasks:
        return False
    return not (context['here'] and context['paths']
                and all('directory' in call['arguments'] for call in calls if 'app' not in call['arguments']))


def plans(request):
    """Every plan the request's own words allow (refusal() finds nothing
    against it) that the grammar reads back from its wording, with the fewest
    operations any such plan needs. Stricter than refusal():

    - a request _ruled_out() (a question, a time other than now, an action or
      agent no tool has, a split without a direction, "it" after two
      sessions, ...) or one naming a detail no plan's wording says
      (_unsaid()) has none;
    - each operation verb asks for an operation of its kind, "it" after a
      join and "for both" for more, and the calls come in the order the
      request states them (_stated());
    - a plan uses every directory and text the request copies (_keeps()) and
      names one agent's directory alike in all its waits and messages
      (_consistent());
    - where some plans follow the request's references ("it", a fronted
      directory), use the text as said (task or message), and give each call
      what its own clause says (_attached()), only those are kept.

    [] when there is none or the search grows too large."""
    if not possible(request):
        return []
    context = _context(request)
    if any(not any(model in allowed for allowed in contract._MODELS.values()) for model in context['said_models']):
        return []       # the grammar cannot carry a model the request names, and leaving it out changes the request
    if _ruled_out(context):
        return []
    carried = {}

    def read_back(calls):       # the grammar's reading is the slowest check, so it is made last and once
        key = json.dumps(calls, sort_keys=True)
        if key not in carried:
            carried[key] = bool(_carried(calls))
        return carried[key]

    for found in _search(request, context):     # by size: the fewest operations the grammar reads back
        if not any(read_back(calls) for calls in found):
            continue
        unsaid = _unsaid(context)
        found = [calls for calls in found if all(_says(calls, detail, context['texts']) for detail in unsaid)
                 and _keeps(context, calls) and _consistent(calls) and _paned(context, calls)
                 and _waited(context, calls) and _whole(context, calls)]
        for prefer in (lambda calls: _referred(context, calls), lambda calls: _as_said(context, calls),
                       lambda calls: _attached(context, calls)):
            preferred = [calls for calls in found if prefer(calls)]
            if any(read_back(calls) for calls in preferred):
                found = preferred
        return [calls for calls in found if read_back(calls)]
    return []


def _search(request, context):
    """For each number of operations in turn, the plans the request's words allow with that many, before
    the grammar reads them back and before the rules that read whole plans; nothing more once a search is
    too large."""
    clauses = _clauses(context)
    plain = context['plain']
    most = min(4, 1 + len(JOIN.findall(plain)) + len(LIST_MARK.findall(plain)) + len(SENTENCE.findall(plain))
               + len(BOTH.findall(plain)))
    judged = {}

    def allowed(calls):     # refusal() does not depend on order, so each set of calls is judged once
        key = tuple(sorted(json.dumps(call, sort_keys=True) for call in calls))
        if key not in judged:
            judged[key] = refusal(request, calls) is None
        return judged[key]

    steps = [0]

    def grow(calls, size, found):
        steps[0] += 1
        if steps[0] > MOST_STEPS:
            raise OverflowError
        if len(calls) == size:
            used = {call['arguments'].get('model') for call in calls}
            if (_owing(context, calls) == 0 and all(model in used for model in context['said_models'])
                    and _in_order(calls, context['plain']) == calls and allowed(calls)):
                found.append(list(calls))
                if len(found) > MOST_PLANS:
                    raise OverflowError
            return
        if _owing(context, calls) > size - len(calls):
            return
        seen = {_identity(call) for call in calls}
        said = [value for call in calls for key, value in call['arguments'].items() if key in ('task', 'text')]
        for call in clauses:
            text = call['arguments'].get('task') or call['arguments'].get('text')
            if (_identity(call) in seen or not _gains(context, calls, call)
                    or text is not None and said.count(text) >= request.count(text)):
                continue
            calls.append(call)
            if _stated(context, calls):     # a prefix out of the request's order never becomes a plan in it
                grow(calls, size, found)
            calls.pop()

    for size in range(1, most + 1):     # the fewest operations that say everything the request names
        found = []
        try:
            grow([], size, found)
        except OverflowError:
            return
        if found:
            yield found


def _clause_pieces(call):
    """The pieces whose concatenation is sentence([call])."""
    name, args = call['name'], call['arguments']
    where = [f" in {_quote(args['directory'])}"] if 'directory' in args else []
    if name.startswith('start_'):
        out = ['start ' + name.split('_')[1]] + where
        if args.get('place', 'tab') != 'tab':
            out.append(_PLACE_WORDS[args['place']])
        if 'model' in args:
            out.append(f" with model {args['model']}")
        if 'task' in args:
            out.append(f" with task {_quote(args['task'])}")
        return out
    who = contract.pane_words(args) if 'pane' in args else args.get('agent')     # "the session in pane 9"
    if name == 'wait_for_agent':
        return [f"wait for {who}"] + where
    if name == 'message_agent':
        return [f"message {who}"] + where + [f": {_quote(args['text'])}"]
    return [sentence([call])]


def pieces(calls):
    """sentence(calls), cut where the plans of one request can part."""
    out = []
    for number, call in enumerate(calls):
        part = _clause_pieces(call)
        out += (['; then ' + part[0]] + part[1:]) if number else part
    return out


class Node:
    """A partial sentence: the text so far, its continuations and the plan it completes, if any."""
    __slots__ = ('text', 'children', 'plan')

    def __init__(self, text=''):
        self.text, self.children, self.plan = text, {}, None


def tree(found):
    root = Node()
    for calls in found:
        node = root
        for piece in pieces(calls):
            node = node.children.setdefault(piece, Node(node.text + piece))
        node.plan = calls
    return root


def continuations(node):
    """[(piece, next node, or None for the end)]: what the grammar allows after this partial sentence."""
    out = list(node.children.items())
    return out + [(ENDING, None)] if node.plan is not None else out


def sentences(node):
    """The complete sentences under a node, in text order."""
    out = [node] if node.plan is not None else []
    for child in node.children.values():
        out += sentences(child)
    return sorted(out, key=lambda found: found.text)


BUILD_FRAME = ('A user typed this request to a terminal assistant:\n{request}\n\n'
               'The assistant rewrites the request as a command in its own fixed wording, one piece at a time. '
               'The command must do exactly what the request asks: the same operations in the same order, with '
               'the same details, and nothing more.')
STEP_QUESTION = 'Which continuation keeps the command true to the request?'
WHOLE_QUESTION = 'Which command does exactly what the request asks, no more and no less?'
WHOLE_GROUP = 24        # complete sentences ranked in one question


def build(request, choose):
    """(calls, steps): the plan whose sentence the model builds, and every
    question's answer with its odds. Two readings must agree: one choice of
    continuation at a time, each shown as the whole sentence so far plus the
    piece, and a ranking of the complete sentences as wholes. `calls` is empty
    when there is no plan, or when the two disagree."""
    steps = []

    def ask(kind, state, question, options):
        ranked = choose(state, question, options)
        steps.append({'kind': kind, 'pick': ranked[0][0], 'p': ranked[0][1],
                      'odds': {key: round(value, 4) for key, value in ranked}})
        return ranked[0][0]

    def refused(kind):
        steps.append({'kind': 'refused', 'pick': kind, 'p': 0.0, 'odds': {}})
        return [], steps

    found = plans(request)
    if not found:
        return refused('unnamed' if not possible(request) else 'no plan')
    root = tree(found)
    _paths, texts, _models, masked, _plain = spans(request)
    full, base = _frames(request, masked, texts)
    ask('gate', base, GATE_QUESTION, KINDS)
    state = BUILD_FRAME.format(request=request)
    node = root
    while True:     # left to right
        offered = {str(number): child for number, (_piece, child) in enumerate(continuations(node), 1)}
        if node.plan is not None:       # the end is offered as "0"
            offered['0'] = offered.pop(str(len(offered)))
        if len(offered) == 1:
            child = next(iter(offered.values()))
        else:
            options = {key: (node.text + '   ' + ENDING if child is None else
                             child.text + (' ...' if child.children else '   ' + ENDING))
                       for key, child in offered.items()}
            child = offered[ask('step', state, STEP_QUESTION, options)]
        if child is None:
            break
        node = child
    greedy = node.plan
    nodes = sentences(root)
    while len(nodes) > 1:       # whole sentences, a group at a time
        kept = []
        for start in range(0, len(nodes), WHOLE_GROUP):
            group = nodes[start:start + WHOLE_GROUP]
            kept.append(group[int(ask('whole', state, WHOLE_QUESTION,
                                      {str(number): found.text for number, found in enumerate(group, 1)})) - 1]
                        if len(group) > 1 else group[0])
        nodes = kept
    if nodes[0].plan != greedy:
        return refused('disagree')
    ask('verify', full + "\n\nThe assistant's plan:\n" + plan_text(greedy), VERIFY_QUESTION, VERIFY_OPTIONS)
    return greedy, steps


def suggest(request, choose=None, *, propose=None, limits=None, plan=None):
    """{'request': a wording the grammar reads, 'calls': what it reads it as,
    'source': where the plan came from} for a request the grammar cannot
    read, or None.

    `choose` asks a decision model, which builds the grammar's sentence for
    a plan of its own (build()). `plan` names another way to get the model's
    plan: walk, which asks for each operation and argument in turn; its
    suggestions have the source 'walk' and are held to walk_signals() and
    WALK_CHECK. `limits` overrides the (gate, choice, check) limits.
    `propose` returns the calls a command model proposes for the request.
    Every plan is held to refusal() and to the grammar. With a decision model
    alone, its plan is offered when every answer was sure enough ('built').
    With a command model alone, its proposal is offered ('proposal'). With
    both, a wording is offered only when the two plans are the same
    ('agreed'); the command model is not asked when the decision model gave
    no plan.
    A decision model that cannot be asked offers nothing."""
    if not possible(request):
        return None
    built = None
    if choose is not None:
        try:
            calls, steps = (plan or build)(request, choose)
        except SuggestError:
            return None
        built = _carried(calls) if calls and refusal(request, calls) is None else None
        if not built:
            return None
        if propose is None:
            read, bars = (walk_signals, (GATE, STEP, WALK_CHECK)) if plan is walk else (signals, (GATE, STEP, VERIFY))
            sure = all(value >= limit for value, limit in zip(read(steps), limits or bars))
            return {**built, 'source': 'walk' if plan is walk else 'built'} if sure else None
    plan = tidy(propose()) if propose is not None else None
    found = _carried(plan) if plan and refusal(request, plan) is None else None
    if not found:
        return None
    if built is None:
        return {**found, 'source': 'proposal'}
    same = contract._canonical(found['calls']) == contract._canonical(built['calls'])
    return {**found, 'source': 'agreed'} if same else None
