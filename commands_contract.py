"""Complete admission for the five-tool local model contract.

The original request supplies every argument. Model proposals must match the
complete reading, including order, literal payloads and host-owned defaults.
This module never executes a command or installs an application.
"""
from dataclasses import dataclass
import json
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
_APPS = set(_SCHEMAS['open_app']['properties']['app']['enum'])
_APP_WORDS = {"pdf viewer":"kilix-pdf", "pdf reader":"kilix-pdf",
              "calculator":"kilix-calculator", "notepad":"kilix-notepad",
              "file manager":"kilix-file", **{x:x for x in _APPS}}
_NOUN = "(?:" + "|".join(map(re.escape, sorted(_APP_WORDS, key=len, reverse=True))) + ")"
_PATH = r'(?:"(?:[^"\\]|\\.)+"|~?/[^\s;?]+)'
_PREAMBLE = re.compile(r"^(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|I\s+(?:want|would like)\s+to\s+)?", re.I)
_SEPARATOR = re.compile(r";\s*(?:(?:then|next|also|after that)\s+)?|,?\s+(?:and\s+then|and|then)\s+", re.I)
_DIRECTORY = r'(?:"(?:[^"\\]|\\.)+"|~?/[^\s;,?]+)'
_LOCATION = re.compile(r'\b(?:in|at|inside|within)\s+(?P<path>'+_DIRECTORY+r')|\b(?P<here>here|in (?:the )?(?:current directory|current folder|this folder|this directory))\b', re.I)
_PLACEMENTS = [
    ('right', r'(?:in\s+)?(?:a\s+)?split\s+(?:to\s+)?(?:the\s+|my\s+)?right|on\s+(?:the\s+|my\s+)?right'),
    ('left', r'(?:in\s+)?(?:a\s+)?split\s+(?:to\s+)?(?:the\s+|my\s+)?left|on\s+(?:the\s+|my\s+)?left'),
    ('up', r'(?:in\s+)?(?:a\s+)?split\s+(?:above(?:\s+me)?|up)|above\s+me'),
    ('down', r'(?:in\s+)?(?:a\s+)?split\s+(?:below(?:\s+me)?|down)|below\s+me'),
    ('tab', r'(?:in\s+)?(?:a\s+)?(?:new\s+)?tab'),
]


def _literal(raw):
    try:
        return json.loads(raw) if raw.startswith('"') else raw
    except ValueError:
        return None


def _take_location(text):
    matches = [m for m in _LOCATION.finditer(text) if _outside_quotes(text, m.start())]
    if len(matches) > 1:
        return None, None
    if not matches:
        return text, None
    match = matches[0]
    directory = _literal(match['path']) if match['path'] else 'here'
    if not directory:
        return None, None
    return text[:match.start()] + ' ' + text[match.end():], directory


def _extended_agent_calls(request):
    """Parse explicit fields without normalizing the literal task or message."""
    text = request.strip()
    preamble = _PREAMBLE.match(text)
    polite = bool(re.search(r'\b(?:can|could|would|will)\s+you\b', preamble[0], re.I))
    text = text[preamble.end():]
    text = re.sub(r'^I need a new (?=(?:claude|codex)\b)', 'start a new ', text, flags=re.I)
    if re.match(r'^a new (?:claude|codex) (?:session|agent)\b',text,re.I):
        text='start '+re.sub(r',?\s+please[.!]?$', '', text, flags=re.I)
    # A final question mark belongs to the polite request only when no payload
    # is present. Payload punctuation is preserved below.
    if polite and ':' not in text and text.endswith('?'):
        text = text[:-1].rstrip()
    head = re.match(r'^(?:start(?: up)?|launch|open|run|bring up|get(?: me)?|create|fire up|spin up)\s+'
                    r'(?:(?:a|an|the|new|fresh)\s+)*(?P<agent>claude|codex)\b'
                    r'(?:\s+(?:coding\s+)?(?:client|session|workspace|agent|instance))?'
                    r'(?:\s+(?:running|started))?', text, re.I)
    if head:
        rest = text[head.end():]
        task = None
        markers = re.compile(r';\s*(?:initial\s+)?(?:task|instruction):\s*|'
                             r'\s+with\s+(?:this\s+)?(?:initial\s+)?(?:task|instruction):\s*|'
                             r':\s*|\s+and\s+(?:ask\s+it\s+to|give\s+it\s+this\s+task:)\s*|'
                             r'\s+to\s+', re.I)
        for marker in markers.finditer(rest):
            if not _outside_quotes(rest, marker.start()):continue
            # "to the right" is placement, not an initial task.
            tail = rest[marker.end():]
            if marker[0].strip().casefold()=='to' and re.match(r'(?:the |my )?(?:right|left)\b', tail, re.I):continue
            task = tail
            rest = rest[:marker.start()]
            break
        rest, directory = _take_location(rest)
        if rest is None:return None
        args = {} if directory in (None,'here') else {'directory':directory}
        if task is not None:
            if not task or len(task.encode())>1024:return None
            args['task']=task
        for place, pattern in _PLACEMENTS:
            matches=list(re.finditer(r'\b(?:'+pattern+r')\b',rest,re.I))
            if matches:
                if len(matches)!=1 or 'place' in args:return None
                match=matches[0];args['place']=place
                rest=rest[:match.start()]+' '+rest[match.end():]
        model = re.search(r'\b(?:with\s+model|using(?:\s+model)?|on\s+model)\s+([A-Za-z0-9][A-Za-z0-9._/-]*)\b',rest,re.I)
        if model:
            args['model']=model[1]
            rest=rest[:model.start()]+' '+rest[model.end():]
        if rest.strip(' ,.!'):return None
        return [_call('start_'+head['agent'].casefold(),**args)]

    # Messages require a named agent; no pronoun or unnamed-session fallback.
    front = re.match(r'^for\s+(?:the\s+)?(?:running\s+|existing\s+)?(?P<agent>claude|codex)(?:\s+session)?\s+(?P<location>.+?),\s*send(?:\s+this\s+message)?:\s*',text,re.I)
    if front:
        rest,directory=_take_location(front['location'])
        if rest is not None and not rest.strip() and directory is not None:
            payload=text[front.end():]
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
        delimiter=re.compile(r'\s+(?:with|saying|the following(?: instruction| text| message)?|this(?: exact)?(?: message| text| instruction)?):\s*|'
                             r':\s*|\s+to\s+',re.I)
        for marker in delimiter.finditer(rest):
            if not _outside_quotes(rest,marker.start()):continue
            prefix,directory=_take_location(rest[:marker.start()])
            if prefix is None or prefix.strip(' ,'):continue
            payload=rest[marker.end():]
            if not payload or len(payload.encode())>1024:return None
            args={'agent':message['agent'].casefold(),'text':payload}
            if directory not in (None,'here'):args['directory']=directory
            return [_call('message_agent',**args)]
        return None

    waiting=re.fullmatch(r'(?:wait(?:\s+(?:for|until|on))?|block until|hold(?:\s+(?:on|off))? until|pause until|let)\s+'
                         r'(?:the\s+)?(?:current\s+|running\s+|existing\s+)?(?P<agent>claude|codex)\b'
                         r'(?P<rest>.*)',text,re.I)
    if waiting:
        rest=waiting['rest']
        rest=re.sub(r'^\s+(?:session|turn)\b','',rest,flags=re.I)
        rest,directory=_take_location(rest)
        if rest is None:return None
        tail=' '.join(rest.strip(' .!').casefold().split())
        if tail not in ('','is idle','is done','is finished','becomes idle','reaches idle',
                        'finishes','finishes its turn','finishes its current turn','to finish',
                        'to finish its turn','to finish its current turn','to become idle',
                        'to complete its turn','completes its current turn','finish before continuing',
                        'finish before we continue','ends'):
            return None
        args={'agent':waiting['agent'].casefold()}
        if directory not in (None,'here'):args['directory']=directory
        return [_call('wait_for_agent',**args)]
    return None


def _call(name, **args):
    return {"name":name, "arguments":args}


def _app_calls(request):
    text = request.strip()
    preamble = _PREAMBLE.match(text)
    polite = bool(re.search(r'\b(?:can|could|would|will)\s+you\b', preamble[0], re.I))
    text = text[preamble.end():]
    if text.endswith('?'):
        if not polite:return None
        text = text[:-1].rstrip()
    text = re.sub(r'\s+(?:please|for me|now)$', '', text, flags=re.I)
    text = re.sub(r'^bring\s+(?:the\s+)?('+_NOUN+r')\s+up$',r'bring up \1',text,flags=re.I)
    noun = re.fullmatch(r'(?:open|launch|start(?: up)?|bring up|show|get)\s+(?:the\s+)?('+_NOUN+r')(?:\s+application|\s+running)?[.!]?', text, re.I)
    if noun:
        return [_call('open_app', app=_APP_WORDS[noun[1].casefold()])]
    patterns = [
        r'(?:open|load|show)\s+(?P<path>'+_PATH+r')\s+(?:in|with|using)\s+(?:the\s+)?PDF (?:viewer|reader)[.!]?',
        r'(?:use\s+)?(?:the\s+)?PDF (?:viewer|reader)\s+to\s+(?:open|load|show)\s+(?P<path>'+_PATH+r')',
        r'open\s+(?P<path>'+_PATH+r')',
        r'(?:launch|bring up|start)\s+(?:the\s+)?PDF (?:viewer|reader)\s+(?:with|and (?:load|show))\s+(?P<path>'+_PATH+r')',
        r'(?:get\s+)?(?:the\s+)?PDF (?:viewer|reader)\s+to\s+(?:display|open|show)\s+(?P<path>'+_PATH+r')',
        r'(?:display|show)\s+(?P<path>'+_PATH+r')\s+(?:in|with|using)\s+(?:the\s+)?PDF (?:viewer|reader)',
    ]
    for pattern in patterns:
        matched = re.fullmatch(pattern,text,re.I)
        if not matched:continue
        raw = matched['path']
        try:path = json.loads(raw) if raw.startswith('"') else raw
        except ValueError:return None
        if (not isinstance(path,str) or not path.startswith(('/','~/'))
                or not path.casefold().endswith('.pdf')
                or any(unicodedata.category(ch) in ('Cc','Cf','Zl','Zp') for ch in path)):
            return None
        return [_call('open_app',app='kilix-pdf',file_path=path)]
    return None


def _outside_quotes(text, position):
    quote = None
    escaped = False
    for ch in text[:position]:
        if escaped:escaped=False;continue
        if ch == '\\' and quote:escaped=True;continue
        if quote:
            if ch == quote:quote=None
        elif ch in ('"', "'"):quote=ch
    return quote is None


def exact_calls(request, *, _depth=0):
    """A complete reading of supported actions, or None; no partial match."""
    if (not isinstance(request,str) or not request.strip() or len(request)>2048 or _depth>3
            or any(unicodedata.category(ch) in ('Cc','Cf','Zl','Zp') for ch in request)):
        return None
    found = _extended_agent_calls(request) or _app_calls(request)
    if found:return found if len(found)<=4 else None
    pair=re.fullmatch(r'give me\s+(claude|codex)\s+in\s+('+_DIRECTORY+r')\s+plus\s+(claude|codex)\s+in\s+('+_DIRECTORY+r')',request,re.I)
    if pair:
        first,second=_literal(pair[2]),_literal(pair[4])
        if first and second:
            return [_call('start_'+pair[1].casefold(),directory=first),_call('start_'+pair[3].casefold(),directory=second)]
    for separator in _SEPARATOR.finditer(request):
        if not _outside_quotes(request,separator.start()):continue
        left,right=request[:separator.start()].strip(),request[separator.end():].strip()
        first=exact_calls(left,_depth=_depth+1)
        if not first:continue
        if any('task' in x['arguments'] or 'text' in x['arguments'] for x in first):continue
        rest=exact_calls(right,_depth=_depth+1)
        if rest and len(first+rest)<=4:return first+rest
    return None


def _canonical(calls, cwd=None):
    if not isinstance(calls,list) or not 1<=len(calls)<=4:
        raise ValueError('calls must contain one to four operations')
    out=[]
    for call in calls:
        if not isinstance(call,dict) or set(call)!={'name','arguments'}:
            raise ValueError('invalid call object')
        name,args=call['name'],call['arguments']
        if not isinstance(name,str) or name not in _PROPERTIES or not isinstance(args,dict):
            raise ValueError('unknown operation or arguments')
        if (set(args)-_PROPERTIES[name] or _REQUIRED.get(name,set())-set(args)
                or any(not isinstance(v,str) or not v for v in args.values())):
            raise ValueError('unexpected, missing or non-string arguments')
        if any(unicodedata.category(ch) in ('Cc','Cf','Zl','Zp') for value in args.values() for ch in value):
            raise ValueError('control characters are not command arguments')
        args=dict(args)
        if name=='open_app':
            if args['app'] not in _APPS or ('file_path' in args and args['app']!='kilix-pdf'):
                raise ValueError('unknown application or incompatible file path')
        else:
            args.setdefault('directory','here')
            if args['directory']=='.':args['directory']='here'
            if cwd and args['directory']=='here':args['directory']=cwd
            if name.startswith('start_'):
                args.setdefault('place','tab')
                if args['place'] not in ('tab','right','left','up','down'):
                    raise ValueError('unknown placement')
                if 'model' in args and args['model'] not in (
                        ('sonnet','opus') if name=='start_claude' else ('gpt-6.1-sol','gpt-6')):
                    raise ValueError('model outside the compact contract')
            elif args['agent'] not in ('claude','codex'):
                raise ValueError('unknown agent')
        out.append(_call(name,**args))
    return out


def admit(request, calls, *, cwd=None):
    wanted=exact_calls(request)
    if not wanted:return Admission(None,reason='the whole request has no supported reading')
    try:
        if _canonical(calls,cwd)!=_canonical(wanted,cwd):
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
        else:
            jobs.add('agents');translated={'session':args['agent']+'@'+args.get('directory','here')}
            if name=='wait_for_agent':translated['for']='idle';kind='wait'
            else:translated['text']=args['text'];kind='tell'
            actions.append(agents.Action(kind,translated))
    return Admission(next(iter(jobs)) if len(jobs)==1 else 'mixed',tuple(actions))
