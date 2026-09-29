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

def _tool(name, description, extra):
    props = {**COMMON, **extra}
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props,
                           "required": list(props), "additionalProperties": False}}

TOOLS = [
    _tool("find_files", "Find filenames by literal substring and optional extension/date", {
        "name": {"type": "string"}, "extension": {"type": "string"},
        "modified": {"type": "string", "description": "any, today, yesterday, or last N days"}}),
    _tool("search_text", "Find a literal substring in bounded UTF-8 text files", {"text": {"type": "string"}}),
    _tool("list_files", "List largest or most recently modified files", {"order": {"type": "string", "enum": ["size", "modified"]}}),
    _tool("preview_file", "Preview one explicit relative UTF-8 text file", {"path": {"type": "string"}}),
]

_SCOPES = {"here": "here", "this project": "here", "the current directory": "here",
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


def parse(request: str) -> list[Action]:
    if not isinstance(request, str) or not request.strip() or len(request.encode('utf-8')) > 4096:
        raise ValueError('provide a request of 1–4096 UTF-8 bytes')
    if any(unicodedata.category(c) in ('Cc', 'Cf', 'Zl', 'Zp') for c in request):
        raise ValueError('control characters are not supported')
    q = request.strip()
    # Full matches preserve quoted payloads and reject unaccounted instructions.
    patterns = [
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
        if mode == 'find':
            return [Action('find_files', {'scope': _scope(m[2]), 'name': _literal(m[1]), 'extension': '', 'modified': (m[3] or 'any').lower()})]
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
    raise ValueError('unsupported or ambiguous files request; use files --help for exact search forms')


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
