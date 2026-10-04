"""Bounded file collector subprocess. No shell, writes, symlink following or special-file reads."""
from __future__ import annotations
import datetime as dt
import heapq
import json
import os
import re
from pathlib import Path
import stat
import sys
import time

MAX_ENTRIES = 10000
MAX_DEPTH = 12
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_PREVIEW_BYTES = 16384
MAX_SECONDS = 3.0
MAX_RESULTS = 100
SKIP_DIRS = {'node_modules', '__pycache__', 'venv', 'env'}


def resolve_scope(scope, cwd, home):
    aliases = {'here': cwd, 'Downloads': str(Path(home) / 'Downloads'),
               'Documents': str(Path(home) / 'Documents'), 'research': str(Path(home) / 'research'),
               'projects': str(Path(home) / 'gpu_terminal')}
    value = aliases.get(scope, scope)
    if value.startswith('~/'): value = str(Path(home) / value[2:])
    if value.startswith('./'): value = str(Path(cwd) / value[2:])
    if not value.startswith('/') or '..' in value.split('/'):
        raise ValueError('scope must resolve to an absolute directory without parent traversal')
    return os.path.normpath(value)


def _directory(path):
    """Open every component with O_NOFOLLOW, keeping the chosen directory anchored."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.split('/'):
            if not part: continue
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = new
        return fd
    except BaseException:
        os.close(fd); raise


def _regular(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd); raise ValueError('not a regular file')
    return fd


def _dates(value, now, *, local_calendar=False):
    def midnight(day):
        if local_calendar:
            # astimezone() without a zone returns today's fixed UTC offset.
            # libc resolves the offset at the requested calendar boundary.
            return time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))
        return dt.datetime.combine(day, dt.time(), tzinfo=now.tzinfo).timestamp()
    day = now.date()
    if value == 'today':
        return midnight(day), midnight(day + dt.timedelta(days=1))
    if value == 'yesterday':
        return midnight(day - dt.timedelta(days=1)), midnight(day)
    if value.startswith('last '):
        return now.timestamp() - int(value.split()[1]) * 86400, now.timestamp()
    return float('-inf'), float('inf')


def collect(query, *, cwd, home, limit=20, now=None):
    fields = {'find_files': {'scope', 'name', 'extension', 'modified'},
              'search_text': {'scope', 'text'}, 'list_files': {'scope', 'order'},
              'preview_file': {'scope', 'path'}}
    def valid_fields(kind, args):
        required = fields[kind]
        allowed = required | ({'name_match'} if kind == 'find_files' else set())
        return required <= set(args) <= allowed
    if (not isinstance(query, dict) or set(query) != {'kind', 'args'}
            or not isinstance(query['kind'], str) or query['kind'] not in fields
            or not isinstance(query['args'], dict) or not valid_fields(query['kind'], query['args'])
            or any(not isinstance(v, str) or len(v.encode('utf-8')) > 4096 for v in query['args'].values())
            or type(limit) is not int or not 1 <= limit <= MAX_RESULTS):
        raise ValueError('invalid bounded file query')
    a = query['args']; k = query['kind']
    if k == 'preview_file' and (a['path'].startswith('/') or
            any(not p or p.startswith('.') for p in a['path'].split('/'))):
        raise ValueError('preview path must be relative without hidden or parent components')
    if k == 'list_files' and a['order'] not in ('size', 'modified'):
        raise ValueError('unsupported listing order')
    if k == 'search_text' and not a['text']:
        raise ValueError('text search requires a literal substring')
    if k == 'find_files' and (not re.fullmatch(r'[a-z0-9]*', a['extension']) or
            not re.fullmatch(r'any|today|yesterday|last [1-9][0-9]? days', a['modified']) or
            a.get('name_match', 'contains') not in ('contains', 'prefix') or
            (a.get('name_match') == 'prefix' and not a['name'])):
        raise ValueError('unsupported file filters')
    local_calendar = now is None
    started = time.monotonic(); now = now or dt.datetime.now().astimezone()
    root = resolve_scope(query['args']['scope'], cwd, home)
    def display_path(value):
        return os.fsencode(value).decode('utf-8', errors='backslashreplace')
    out = {'scope': display_path(root), 'scope_bytes_hex': os.fsencode(root).hex(), 'observed_at': now.isoformat(), 'results': [], 'complete': True,
           'limits': {'entries': MAX_ENTRIES, 'depth': MAX_DEPTH, 'file_bytes': MAX_FILE_BYTES,
                      'total_bytes': MAX_TOTAL_BYTES, 'seconds': MAX_SECONDS, 'results': limit},
           'visited': 0, 'matched': 0, 'bytes_read': 0, 'skipped': {}, 'errors': [], 'results_truncated': False,
           'content_is_untrusted': True}
    def skip(reason, n=1): out['skipped'][reason] = out['skipped'].get(reason, 0)+n
    def error(path, e):
        out['complete'] = False
        if len(out['errors']) < 20: out['errors'].append({'path': display_path(path), 'error': str(e)})
        else: skip('additional_errors')
    def budget():
        if out['visited'] >= MAX_ENTRIES or time.monotonic()-started >= MAX_SECONDS:
            out['complete'] = False; skip('scan_budget'); return False
        return True
    def row(rel, st):
        return {'path': display_path(str(Path(root)/rel)),
                'path_bytes_hex': os.fsencode(str(Path(root)/rel)).hex(),
                'relative_path': display_path(rel), 'size': st.st_size,
                'modified_at': dt.datetime.fromtimestamp(st.st_mtime, now.tzinfo).isoformat(),
                'mtime_ns': st.st_mtime_ns}
    args=query['args']; kind=query['kind']; heap=[]; serial=0
    def add(rec, key):
        nonlocal serial
        out['matched'] += 1; serial+=1
        item=(key, serial, rec)
        if len(heap)<limit: heapq.heappush(heap,item)
        elif key>heap[0][0]: heapq.heapreplace(heap,item)
    def read_text(parent, name, cap):
        fd=_regular(parent,name)
        try:
            st=os.fstat(fd)
            # Bounded reads even if a regular file grows between stat and read.
            data=b''
            while len(data)<cap+1:
                chunk=os.read(fd,min(65536,cap+1-len(data)))
                if not chunk: break
                data+=chunk
            out['bytes_read']+=len(data)
            if b'\0' in data: raise UnicodeError('binary file; only UTF-8 text is supported')
            return data,st
        finally:os.close(fd)
    rootfd=_directory(root)
    try:
        if kind=='preview_file':
            parts=args['path'].split('/'); fd=os.dup(rootfd)
            try:
                for part in parts[:-1]:
                    nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                    os.close(fd);fd=nxt
                data,st=read_text(fd,parts[-1],MAX_PREVIEW_BYTES)
                # Ignore an incomplete trailing UTF-8 codepoint only on a capped preview.
                import codecs
                clipped=len(data)>MAX_PREVIEW_BYTES
                text=codecs.getincrementaldecoder('utf-8')().decode(data[:MAX_PREVIEW_BYTES],final=not clipped)
                rec=row(args['path'],st);rec.update(text=text,truncated=clipped)
                out['results']=[rec];out['matched']=1;out['visited']=1
                out['results_truncated']=clipped
            except (OSError,ValueError) as e:error(args['path'],e)
            finally:os.close(fd)
            return out
        lo,hi=_dates(args.get('modified','any'),now,local_calendar=local_calendar)
        def walk(fd, prefix='', depth=0):
            try:
                with os.scandir(fd) as entries:
                    for entry in entries:
                        if not budget():return
                        out['visited']+=1
                        name=entry.name;rel=f'{prefix}/{name}' if prefix else name
                        if name.startswith('.'):skip('hidden');continue
                        try:
                            st=entry.stat(follow_symlinks=False)
                            if stat.S_ISLNK(st.st_mode):skip('symlink');continue
                            if stat.S_ISDIR(st.st_mode):
                                if name in SKIP_DIRS:skip('excluded_directory');continue
                                if depth>=MAX_DEPTH:out['complete']=False;skip('depth');continue
                                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                                try:walk(child,rel,depth+1)
                                finally:os.close(child)
                                continue
                            if not stat.S_ISREG(st.st_mode):skip('special');continue
                            if kind=='find_files':
                                wanted = args['name'].casefold()
                                found = name.casefold()
                                if args.get('name_match') == 'prefix':
                                    if not found.startswith(wanted):continue
                                elif wanted not in found:continue
                                if args['extension'] and Path(name).suffix.casefold()!='.'+args['extension']:continue
                                if not lo<=st.st_mtime<hi:continue
                            if kind=='search_text':
                                if st.st_size>MAX_FILE_BYTES:skip('large_file');out['complete']=False;continue
                                if out['bytes_read']+st.st_size+1>MAX_TOTAL_BYTES:skip('content_budget');out['complete']=False;continue
                                cap=min(MAX_FILE_BYTES,MAX_TOTAL_BYTES-out['bytes_read']-1)
                                data,st=read_text(fd,name,cap)
                                if len(data)>cap:skip('growing_file');out['complete']=False;continue
                                try:text=data.decode('utf-8')
                                except UnicodeError:skip('non_utf8');continue
                                pos=text.find(args['text'])
                                if pos<0:continue
                                rec=row(rel,st);rec.update(line=text.count('\n',0,pos)+1,
                                    excerpt=text[max(0,pos-100):pos+min(len(args['text']),200)+100])
                            else:rec=row(rel,st)
                            key=st.st_size if kind=='list_files' and args['order']=='size' else st.st_mtime_ns
                            add(rec,key)
                        except UnicodeError:skip('binary_or_non_utf8')
                        except (OSError,ValueError) as e:error(rel,e)
            except OSError as e:error(prefix,e)
        walk(rootfd)
        out['results']=[item[2] for item in sorted(heap,key=lambda x:(x[0],x[1]),reverse=True)]
        out['results_truncated']=out['matched']>limit
        return out
    finally:os.close(rootfd)


if __name__=='__main__':
    try:
        data=json.loads(sys.stdin.buffer.read(16385))
        result=collect(**data)
        print(json.dumps(result,ensure_ascii=True))
    except (OSError,ValueError,KeyError) as e:
        print(json.dumps({'complete':False,'results':[],'errors':[{'error':str(e)}]}));sys.exit(1)
