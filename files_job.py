"""Bounded read-only file queries. Proposals must equal the independently parsed intent."""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from actions import Refusal


@dataclass(frozen=True)
class Action:
    kind: str
    args: dict


COMMON = {"scope": {"type": "string", "description": "here, Downloads, Documents, research, projects, or an explicit directory"}}

def _tool(name, description, extra, *, optional=()):
    props = {**COMMON, **extra}
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props,
                           "required": [key for key in props if key not in optional],
                           "additionalProperties": False}}

TOOLS = [
    _tool("find_files", "Find filenames by literal substring or prefix and optional extension/date", {
        "name": {"type": "string"}, "extension": {"type": "string"},
        "modified": {"type": "string", "description": "any, today, yesterday, or last N days"},
        "name_match": {"type": "string", "enum": ["contains", "prefix"], "default": "contains"}},
          optional=("name_match",)),
    _tool("search_text", "Find a literal substring in bounded UTF-8 text files", {"text": {"type": "string"}}),
    _tool("list_files", "List largest or most recently modified files", {"order": {"type": "string", "enum": ["size", "modified"]}}),
    _tool("preview_file", "Preview one explicit relative UTF-8 text file", {"path": {"type": "string"}}),
]

_SCOPES = {"here": "here", "this project": "here", "the current directory": "here",
           "this directory": "here", "the directory": "here", "this folder": "here",
           "current directory": "here", "the current folder": "here",
           "downloads": "Downloads", "my downloads": "Downloads", "documents": "Documents",
           "my documents": "Documents", "research": "research", "projects": "projects"}
_TYPE_WORDS = {'markdown': 'md', 'python': 'py', 'text': 'txt', 'javascript': 'js',
               'typescript': 'ts', 'shell': 'sh', 'bash': 'sh', 'rust': 'rs', 'ruby': 'rb',
               'perl': 'pl', 'golang': 'go', 'excel': 'xlsx', 'word': 'docx', 'powerpoint': 'pptx',
               'jpeg': 'jpg'}
_CATEGORIES = frozenset({'image', 'images', 'photo', 'photos', 'picture', 'pictures', 'video', 'videos',
                         'audio', 'music', 'sound', 'document', 'documents', 'office', 'archive',
                         'archives', 'source', 'code', 'binary', 'executable', 'media', 'config',
                         'backup', 'data', 'temp', 'temporary', 'old', 'new', 'big', 'small', 'empty',
                         'duplicate', 'other', 'important', 'these', 'those', 'the', 'any', 'every'})
_LITERAL = r'''(?:"[^"\n]+"|'[^'\n]+')'''
_SCOPE = rf'(?:{_LITERAL}|[\w~./ -]+?)'
_DATE = r'(?:today|yesterday|last [1-9][0-9]? days)'


def _literal(s):
    return s[1:-1]


def _scope(s):
    value = _literal(s) if s[:1] in ('"', "'") else s
    if value.casefold() in _SCOPES:
        return _SCOPES[value.casefold()]
    if not (value.startswith(('/', '~/', './'))):
        raise ValueError('name a scope: here, Downloads, Documents, research, projects, or an explicit path')
    if s[:1] not in ('\"', "'") and any(c.isspace() for c in value):
        raise ValueError('quote directory paths containing spaces')
    if any(p in ('.', '..') for p in value.removeprefix('./').split('/')):
        raise ValueError('scope must not contain dot or parent components')
    return value


USAGE = ('files requests: find files named "NAME" in SCOPE [modified today|yesterday|last N days] '
         '| find files starting with "PREFIX" in SCOPE '
         '| find pdf files in SCOPE | find text "TEXT" in SCOPE | list largest|recent files in SCOPE '
         '| preview "rel/path" in SCOPE; SCOPE is here, Downloads, Documents, research, projects '
         'or an absolute path')
_HELP = re.compile(r'(?:files\s+)?(?:--help|-h|help)(?:\s*[;:].*)?', re.I)
# How agents ask for a name (gpt-6-luna route benchmark: 1 of 9 file searches got
# through; "find files named quartz-ledger-*", "find files whose name starts with
# X under DIR", "search filename X"). Preserve prefix constraints: a substring
# search can also match unrelated names with the requested text in the middle.
_WORDY_NAME = re.compile(
    r'(?:find|search for|look for|locate|search)\s+(?:the\s+|all\s+)?(?:files?|filenames?)\s+'
    r'(?:(?:anywhere|somewhere|recursively)\s+)?'
    r'(?:(?:in|under|below|within|inside)\s+(?P<scope1>.+?)\s+)?'
    r'(?P<relation>whose\s+(?:base\s*)?name\s+(?:starts|begins)\s+with|(?:with\s+(?:a\s+)?)?(?:base\s*)?names?\s+'
    r'(?:starting|beginning)\s+with|starting\s+with|beginning\s+with|named|called|matching|'
    r'containing|with\s+names?\s+(?:matching|containing))\s+'
    r'(?P<name>"[^"\n]+"|\'[^\'\n]+\'|[\w.+@-]+\*?)'
    r'(?:\s+(?:(?:anywhere|somewhere|recursively)\s+)?(?:in|under|below|within|inside)\s+(?P<scope2>.+?))?'
    r'(?:\s+(?:anywhere|somewhere|recursively))?',
    re.I)
_RETURN_TAIL = re.compile(r'[.,;]?\s*(?:and\s+)?return\s+(?:only\s+)?(?:the\s+)?(?:full\s+)?(?:absolute\s+)?'
                          r'paths?(?:\s*\(s\))?(?:\s+only)?[.!]*$|[.!]+$', re.I)
# These qualifiers restate collector behavior. Match the complete trailing
# clause; do not discard arbitrary filters or text inside a quoted path/name.
_DEFAULT_VISIBILITY = re.compile(
    r'\s+\((?:visible regular files only(?:,\s*skip hidden(?: entries| files)? and symlinks)?'
    r'|skip hidden(?: entries| files)? and symlinks)\)\s*[.!]?$', re.I)


_SHORT_NAME = re.compile(
    r'(?:(?:find|locate|look\s+for)\s+(?P<glob>[\w.+@-]+\*)\s+files?'
    r'|search\s+(?:for\s+)?filenames?\s+(?P<plain>"[^"\n]+"|\'[^\'\n]+\'|[\w.+@-]+\*?))'
    r'(?:\s+(?:(?:anywhere|somewhere|recursively)\s+)?(?:in|under|below|within|inside)\s+(?P<scope2>.+?))?',
    re.I)


def _canonical(q: str) -> str:
    """Rewrite an agent's wording of a name search to the canonical form, or return it unchanged."""
    text = _RETURN_TAIL.sub('', q).strip()
    m = _WORDY_NAME.fullmatch(text)
    if not m and (short := _SHORT_NAME.fullmatch(text)):
        m = {'name': short['glob'] or short['plain'], 'scope1': None,
             'scope2': short['scope2'], 'relation': ''}
    if not m:
        return q
    name = m['name']
    quoted = name[:1] in ('"', "'")
    literal = name[1:-1] if quoted else name
    prefix = bool(re.search(r'\b(?:starts|begins|starting|beginning)\b', m['relation'], re.I))
    if not quoted and literal.endswith('*'):
        literal = literal[:-1]
        prefix = True
    if not literal or (not quoted and any(c in literal for c in '*?"')):
        return q
    if m['scope1'] and m['scope2']:
        raise ValueError('state one file scope; multiple scopes are ambiguous')
    scope = m['scope1'] or m['scope2']
    relation = 'starting with' if prefix else 'named'
    if not scope:
        raise ValueError(f'name a scope, e.g.: find files {relation} "{literal}" in here  ({USAGE})')
    value = name if quoted else f'"{literal}"'
    return f'find files {relation} {value} in {scope}'


def parse(request: str) -> list[Action]:
    if not isinstance(request, str) or not request.strip() or len(request.encode('utf-8')) > 4096:
        raise ValueError('provide a request of 1–4096 UTF-8 bytes')
    if any(unicodedata.category(c) in ('Cc', 'Cf', 'Zl', 'Zp') for c in request):
        raise ValueError('control characters are not supported')
    q = request.strip()
    if _HELP.fullmatch(q):
        raise ValueError(USAGE)
    q = _DEFAULT_VISIBILITY.sub('', q)
    q = _canonical(q)
    # Full matches preserve quoted payloads and reject unaccounted instructions.
    patterns = [
        (rf'(?:find|search for) files starting with ({_LITERAL}) in ({_SCOPE})(?: modified ({_DATE}))?', 'prefix'),
        (rf'(?:find|search for) files named ({_LITERAL}) in ({_SCOPE})(?: modified ({_DATE}))?', 'find'),
        (rf'(?:find|show) (?:the )?([A-Za-z0-9]+) files in ({_SCOPE})(?: modified ({_DATE}))?', 'type'),
        (rf'(?:find|show) files in ({_SCOPE}) modified ({_DATE})', 'date'),
        (rf'(?:find|search) (?:text )?({_LITERAL}) in ({_SCOPE})', 'text'),
        (rf'(?:show|list) (largest|large|recent|newest) files in ({_SCOPE})', 'list'),
        (rf'preview ({_LITERAL}) in ({_SCOPE})', 'preview'),
    ]
    for pattern, mode in patterns:
        m = re.fullmatch(pattern, q, flags=re.I)
        if not m:
            continue
        if mode in ('find', 'prefix'):
            args = {'scope': _scope(m[2]), 'name': _literal(m[1]), 'extension': '',
                    'modified': (m[3] or 'any').lower()}
            if mode == 'prefix':
                args['name_match'] = 'prefix'
            return [Action('find_files', args)]
        if mode == 'type':
            extension = m[1].lower()
            if extension in ('all', 'my', 'some', 'hidden', 'system', 'deleted', 'large', 'largest', 'recent', 'newest'):
                continue
            if extension in _CATEGORIES:
                raise ValueError('name one file extension; a category spans several')
            # A type word is not an extension: "markdown files" are *.md, not *.markdown.
            extension = _TYPE_WORDS.get(extension, extension)
            return [Action('find_files', {'scope': _scope(m[2]), 'name': '', 'extension': extension, 'modified': (m[3] or 'any').lower()})]
        if mode == 'date':
            return [Action('find_files', {'scope': _scope(m[1]), 'name': '', 'extension': '', 'modified': m[2].lower()})]
        if mode == 'text':
            return [Action('search_text', {'scope': _scope(m[2]), 'text': _literal(m[1])})]
        if mode == 'list':
            return [Action('list_files', {'scope': _scope(m[2]), 'order': 'size' if m[1].lower() in ('large', 'largest') else 'modified'})]
        path = _literal(m[1])
        if path.startswith('/') or any(p in ('', '.', '..') or p.startswith('.') for p in path.split('/')):
            raise ValueError('preview needs a relative path without hidden or parent components')
        return [Action('preview_file', {'scope': _scope(m[2]), 'path': path})]
    raise ValueError('unsupported or ambiguous files request; ' + USAGE)


def interpret(request, calls):
    try:
        want = parse(request)
    except ValueError as e:
        return [Refusal('files', str(e))]
    expected = [{'name': a.kind, 'arguments': a.args} for a in want]
    if not isinstance(calls, list) or calls != expected:
        return [Refusal('files', 'proposal does not exactly match the requested query, filters and scope')]
    return want


class Baseline:
    def start(self): pass
    def close(self): pass
    def reset(self): pass
    def complete(self, request):
        try:
            calls = [{'name': a.kind, 'arguments': a.args} for a in parse(request)]
        except ValueError:
            calls = []
        return {'success': True, 'function_calls': calls}
