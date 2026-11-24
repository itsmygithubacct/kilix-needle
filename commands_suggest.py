"""Suggest a supported wording for a command the grammar cannot read.

A decision model (Ollama's d3 family) answers multiple-choice questions and
writes nothing. Each step here offers it only what the five-tool contract
allows at that point: the kind of request, the operation, an enum value, or a
span the request's own lexical rules find (a path, quoted text, a model name).
The finished plan is put into the grammar's plain wording, and the suggestion
exists only if the grammar reads that wording back as exactly that plan.

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
    if rest and rest[0] not in QUOTES and any(cue.search(outside[:colon.start()]) for cue in AGENT_CUE.values()):
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
    if name == 'wait_for_agent':
        return f"wait for the existing {AGENTS[args['agent']]} session{where} to finish its turn"
    if name == 'message_agent':
        return f"send the existing {AGENTS[args['agent']]} session{where} the message {shown(args['text'])}"
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
        found = (AGENT_CUE.get(key) or APP_CUE[key]).search(plain)
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
    if not (agents or apps):
        return 'unnamed'
    if NOT_NOW.search(plain):
        return 'conditional'
    if CUT_OFF.search(plain):
        return 'cut off'
    if NOT_OFFERED.search(plain):
        return 'unsupported'
    pools = {'directory': paths, 'file_path': paths, 'task': texts, 'text': texts, 'model': models}
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
    not read it, it names an agent or an application, and it is a plain,
    complete instruction for now. Costs no model."""
    if not isinstance(request, str) or not request.strip() or len(request) > 2048:
        return False
    try:
        if contract.exact_calls(request):
            return False
    except ValueError:
        return False
    paths, _texts, _models, _masked, plain = spans(request)
    agents, apps, _directions = _named(paths, plain)
    return bool(agents or apps) and not NOT_NOW.search(plain) and not CUT_OFF.search(plain)


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


def walk(request, choose):
    """(calls, steps): the plan the model's choices make, and every question's
    answer with its odds. `calls` is empty when a step refuses the request."""
    steps = []
    paths, texts, models, masked, plain = spans(request)
    full, base = _frames(request, masked, texts)
    agents, apps, directions = _named(paths, plain)
    offered = {name: text for name, text in OPERATIONS.items() if
               (name.startswith('start_') and name.split('_')[1] in agents)
               or (name == 'wait_for_agent' and agents) or (name == 'message_agent' and agents and texts)
               or (name == 'open_app' and apps)}

    def ask(kind, state, question, options, log=steps):
        ranked = choose(state, question, options)
        log.append({'kind': kind, 'pick': ranked[0][0], 'p': ranked[0][1],
                    'odds': {key: round(value, 4) for key, value in ranked}})
        return ranked

    def refused(kind, log=steps):
        log.append({'kind': 'refused', 'pick': kind, 'p': 0.0, 'odds': {}})

    def build(total):
        """The plan with `total` operations and the steps that made it, or
        (None, steps) when a step says the request cannot be done."""
        log, calls = [], []

        def pick(kind, state, question, options):
            return ask(kind, state, question, options, log)[0][0]

        def pick_span(kind, state, question, candidates, none=None):
            if not candidates:
                return None
            options = {str(number): candidate for number, candidate in enumerate(candidates, 1)}
            if none:
                options['0'] = none
            elif len(candidates) == 1:
                return candidates[0]
            chosen = pick(kind, state, question, options)
            return None if chosen == '0' else candidates[int(chosen) - 1]

        for number in range(total):
            note = f"\n\nThe request asks for {COUNTS[str(total)]} operation{'s' if total > 1 else ''}."
            if calls:
                note += '\nAlready planned:\n' + plan_text(calls, texts)
            state, whole = base + note, full + note
            options = dict(offered)
            options['other'] = ('something that none of these can do, such as stopping, closing, deleting or '
                                'editing, or using another program')
            name = pick('operation', state, f'Which operation is the {ORDINALS[number]} one the request asks for?'
                        if total > 1 else 'Which operation does the request ask for?', options)
            if name == 'other':
                refused('operation', log)       # an unsupported action refuses the whole request
                return None, log
            args = {}
            this = (f'the {ORDINALS[number]} operation, to {OPERATIONS[name]}' if total > 1 else
                    f'the operation, to {OPERATIONS[name]}')
            other = 'another program, or none is named'
            if name in ('wait_for_agent', 'message_agent'):
                args['agent'] = pick('agent', state, f"Which agent's session is {this} about?",
                                     {**{agent: AGENTS[agent] for agent in agents}, 'other': other})
                if args['agent'] == 'other':
                    refused('agent', log)
                    return None, log
            if name == 'open_app':
                opened = [call['arguments']['app'] for call in calls if call['name'] == 'open_app']
                question = ('Which application does the request ask to open next, after those already planned?'
                            if opened else 'Which application does the request ask to open'
                            + (' first?' if total > 1 else '?'))
                args['app'] = pick('app', state, question, {**{app: APPS[app] for app in apps}, 'other': other})
                if args['app'] == 'other':
                    refused('app', log)
                    return None, log
                pdfs = [path for path in paths if path.lower().endswith('.pdf')]
                if args['app'] == 'kilix-pdf' and pdfs:
                    path = pick_span('file', state, 'Which file should the PDF viewer open?', pdfs,
                                     none='no file: just open the viewer')
                    if path:
                        args['file_path'] = path
            else:
                path = pick_span('directory', state, f'Which directory is {this} for?', paths,
                                 none='the current directory: the request names no directory for this operation')
                if path:
                    args['directory'] = path
            if name.startswith('start_'):
                if PLACE_CUE.search(plain):
                    named = {place: PLACES[place] for place, cue in PLACE_WORD.items() if cue.search(plain)}
                    place = pick('place', state, f'Where should the new session of {this} open?',
                                 {**(named or PLACES), 'tab': 'a new tab, or the request does not say where for '
                                  'this one'})
                    if place != 'tab':
                        args['place'] = place
                model = pick_span('model', whole, f'Which model does the request name for {this}?', models,
                                  none='none: the request names no model for it')
                if model:
                    args['model'] = model
                task = pick_span('task', whole, f'Which text is the task that the new session of {this} should '
                                 'be given?', texts, none='none: the request gives it no task')
                if task:
                    args['task'] = task
            if name == 'message_agent':
                args['text'] = pick_span('text', whole, f'Which text is the message for {this}?', texts)
            calls.append({'name': name, 'arguments': args})
        if len({_identity(call) for call in calls}) < len(calls):
            refused('repeated', log)        # the same operation twice is a misread "and", never a plan
            return None, log
        if _left_out(calls, paths, texts, directions):
            refused('unused', log)      # the request names something the plan leaves out
            return None, log
        return _in_order(calls, plain), log

    if not offered:
        refused('unnamed')      # no agent and no application is named, so no operation is possible
        return [], steps
    if NOT_NOW.search(plain):
        refused('conditional')
        return [], steps
    ask('gate', base, GATE_QUESTION, KINDS)
    totals = [1]
    if MORE_CUE.search(plain):      # only a request that joins clauses can ask for more than one operation
        ranked = ask('count', base, 'How many separate operations does the request ask the assistant to '
                     'carry out?', COUNTS)
        totals = list(dict.fromkeys([int(key) for key, odds in ranked[:2] if odds >= 0.15] + [1]))
    built = [build(total) for total in totals]
    plans = []
    for calls, log in built:
        if calls is not None and calls not in [plan for plan, _ in plans]:
            plans.append((calls, log))
    if not plans:
        return [], steps + built[0][1]
    chosen = plans[0]
    if len(plans) > 1:      # whole plans compete, which the model judges better than a count
        options = {str(number): '; then '.join(describe(call) for call in calls)
                   for number, (calls, _) in enumerate(plans, 1)}
        options['0'] = 'neither does what the request asks'
        pick = ask('plans', full, 'Which plan does exactly what the request asks, no more and no less?',
                   options)[0][0]
        if pick == '0':
            refused('plans')
            return [], steps
        chosen = plans[int(pick) - 1]
    calls, log = chosen
    steps += log
    ask('verify', full + "\n\nThe assistant's plan:\n" + plan_text(calls), VERIFY_QUESTION, VERIFY_OPTIONS)
    return calls, steps


def signals(steps):
    """(gate, least sure choice, verify) for a walk."""
    gate = next((step['odds'].get('command', 0.0) for step in steps if step['kind'] == 'gate'), 0.0)
    verify = next((step['odds'].get('yes', 0.0) for step in steps if step['kind'] == 'verify'), 0.0)
    inner = [step['p'] for step in steps if step['kind'] not in ('gate', 'verify', 'count')]
    return gate, min(inner) if inner else 0.0, verify


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
            text = f"wait for {args['agent']}{where}"
        elif name == 'message_agent':
            text = f"message {args['agent']}{where}: {_quote(args['text'])}"
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


def suggest(request, choose=None, *, propose=None, limits=(GATE, STEP, VERIFY)):
    """{'request': a wording the grammar reads, 'calls': what it reads it as,
    'source': where the plan came from} for a request the grammar cannot
    read, or None.

    `choose` asks a decision model, which walks to a plan of its own.
    `propose` returns the calls a command model proposes for the request.
    Every plan is held to refusal() and to the grammar. With a decision model
    alone, its plan is offered when every answer was sure enough ('walk').
    With a command model alone, its proposal is offered ('proposal'). With
    both, a wording is offered only when the two plans are the same
    ('agreed'); the command model is not asked when the walk gave no plan.
    A decision model that cannot be asked offers nothing."""
    if not possible(request):
        return None
    walked = None
    if choose is not None:
        try:
            calls, steps = walk(request, choose)
        except SuggestError:
            return None
        walked = _carried(calls) if calls and refusal(request, calls) is None else None
        if not walked:
            return None
        if propose is None:
            sure = all(value >= limit for value, limit in zip(signals(steps), limits))
            return {**walked, 'source': 'walk'} if sure else None
    plan = tidy(propose()) if propose is not None else None
    found = _carried(plan) if plan and refusal(request, plan) is None else None
    if not found:
        return None
    if walked is None:
        return {**found, 'source': 'proposal'}
    same = contract._canonical(found['calls']) == contract._canonical(walked['calls'])
    return {**found, 'source': 'agreed'} if same else None
