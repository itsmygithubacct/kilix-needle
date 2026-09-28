"""Read-only scoped file discovery, with deterministic planning and optional model comparison.

Examples (quote the entire request at the shell):
  kilix-needle files 'find pdf files in Downloads modified yesterday'
  kilix-needle files 'find files named "needle" in research'
  kilix-needle files 'find text "PipeWire" in projects'
  kilix-needle files 'show largest files in Downloads'
  kilix-needle files 'show recent files in here'
  kilix-needle files 'preview "README.md" in here'

Scope aliases: here (caller directory), Downloads, Documents, research,
projects (~/gpu_terminal), or an explicit absolute/~/./ directory. Quote
paths containing spaces within the request. Preview accepts a relative path.
Search is bounded, skips hidden entries and symlinks, and never changes files.
Text search/preview supports UTF-8 text, not PDF/office contents. Modification
time is not download time. Results are observations, not semantic summaries.
"""
from __future__ import annotations
import argparse
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import unicodedata

from actions import Refusal
import files_job


def run(request, *, engine=None, dry_run=False, cwd=None, home=None, limit=20, agent=False):
    """One files request, recorded in the local request history (history.py):
    the request and its planned query only, never the names or contents found."""
    import time
    import history
    started = time.monotonic()
    record = None
    try:
        record = _run(request, engine=engine, dry_run=dry_run, cwd=cwd, home=home, limit=limit)
        return record
    finally:
        try:
            options = type("Options", (), {"agent": agent, "dry_run": dry_run, "assume_yes": False})()
            plan = (record or {}).get("plan") or []
            items = [{"kind": step.get("kind", "files"), "args": step.get("args", {}),
                      "outcome": "would" if dry_run else ("done" if record.get("status") == 0 else "failed")}
                     for step in plan if isinstance(step, dict)]
            label = type("Engine", (), {"label": "files grammar" if engine is None
                                        else str(getattr(engine, "label", "model"))})()
            history.record("files", request, None, label, plan or None, options,
                           {"status": (record or {}).get("status"), "note": (record or {}).get("note", ""),
                            "items": items}, time.monotonic() - started)
        except Exception:       # noqa: BLE001 - recording never changes a request
            pass


def _run(request, *, engine=None, dry_run=False, cwd=None, home=None, limit=20):
    record={'request':request,'status':1,'mode':'model' if engine is not None else 'baseline',
            'plan':[],'observation':None,'note':''}
    if type(limit) is not int or not 1<=limit<=100:raise ValueError('limit must be an integer from 1 to 100')
    if type(dry_run) is not bool:raise ValueError('dry_run must be boolean')
    # Parse before model access or filesystem reads. No unsupported clause is dropped.
    try:files_job.parse(request)
    except ValueError as e:record['note']=str(e);return record
    try:
        if engine is None:reply=files_job.Baseline().complete(request)
        else:
            engine.reset();reply=engine.complete(request)
        # The one reply rule for every job (engine.reply_calls, review KN-R18-04/-05).
        from engine import reply_calls
        calls, unusable = reply_calls(reply)
        if unusable:
            record['note']='model/runtime failure; no file query performed';return record
        results=files_job.interpret(request,calls)
    except Exception as e:
        # Runtime implementation exceptions cannot authorize partial reads.
        record['note']=f'query proposal failed: {type(e).__name__}';return record
    if any(isinstance(r,Refusal) for r in results):
        record['note']='; '.join(r.reason for r in results if isinstance(r,Refusal));return record
    record['plan']=[dataclasses.asdict(a) for a in results]
    if dry_run:record['status']=0;record['note']='plan only; no filesystem query performed';return record
    payload={'query':record['plan'][0],'cwd':cwd or os.getcwd(),'home':home or str(Path.home()),'limit':limit}
    try:
        proc=subprocess.run([sys.executable,'-I','-B',str(Path(__file__).with_name('files_backend.py'))],
            input=json.dumps(payload),text=True,capture_output=True,timeout=5,check=False)
        observation=json.loads(proc.stdout)
        if not isinstance(observation,dict):raise ValueError('invalid collector result')
        record['observation']=observation
        record['status']=0 if proc.returncode==0 and observation.get('complete') else 1
        record['note']='query complete' if record['status']==0 else 'partial or failed query; inspect coverage and errors'
    except subprocess.TimeoutExpired:
        record['note']='file collection exceeded the 5-second wall limit; no complete result available'
    except (OSError,ValueError) as e:record['note']=f'file collection failed: {type(e).__name__}'
    return record


def _safe(value):
    """Escape terminal controls, including bidi format characters from file contents/names."""
    return ''.join(f'\\u{ord(c):04x}' if unicodedata.category(c) in ('Cc','Cf','Cs','Zl','Zp') else c for c in str(value))


def render(record):
    lines=[record['note']]
    if record['observation'] is None:
        for a in record['plan']:lines.append(json.dumps(a,ensure_ascii=True))
    else:
        o=record['observation'];lines.append('Scope: '+_safe(o.get('scope','unresolved')))
        for result in o['results']:
            lines.append(f"{_safe(result['path'])} ({result['size']} bytes, {result['modified_at']})")
            if 'excerpt' in result:lines.append(f"  line {result['line']}: {_safe(result['excerpt'])}")
            if 'text' in result:
                lines.extend('  '+_safe(line) for line in result['text'].split('\n'))
                if result['truncated']:lines.append('  [preview truncated]')
        if not o['results']:lines.append('No results within the reported coverage.')
        if o.get('results_truncated'):lines.append('Result limit reached; additional matches or text omitted.')
        if o.get('skipped'):lines.append('Skipped: '+json.dumps(o['skipped'],sort_keys=True))
        for error in o.get('errors',[]):lines.append('Error: '+_safe(json.dumps(error,ensure_ascii=True)))
    return '\n'.join(lines)


def mcp(arguments, *, plan=False, engine=None):
    if not isinstance(arguments,dict) or set(arguments)-{'request','limit','cwd'}:
        raise ValueError('files accepts only request, limit and optional absolute cwd')
    request=arguments.get('request')
    if not isinstance(request,str):raise ValueError('files request must be a string')
    cwd=arguments.get('cwd')
    if cwd is not None and (not isinstance(cwd,str) or not cwd.startswith('/') or len(cwd)>4096):
        raise ValueError('files cwd must be an absolute bounded path')
    return run(request,engine=engine,dry_run=plan,cwd=cwd,limit=arguments.get('limit',20),agent=True)


def main(argv=None):
    p=argparse.ArgumentParser(prog='kilix-needle files',description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('request',nargs='*')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--json',action='store_true')
    p.add_argument('--limit',type=int,default=20,choices=range(1,101),metavar='1..100')
    group=p.add_mutually_exclusive_group()
    group.add_argument('--baseline',action='store_true',help='deterministic parser (default); no model needed')
    group.add_argument('--engine',metavar='FILE',help='explicit Needle engine for experimental query proposals')
    args=p.parse_args(argv)
    def process(request,engine):
        record=run(request,engine=engine,dry_run=args.dry_run,limit=args.limit)
        print(json.dumps(record,ensure_ascii=True) if args.json else render(record))
        return record['status']
    def session(engine=None):
        if args.request:return process(' '.join(args.request),engine)
        if not sys.stdin.isatty():p.error('provide a request, or use a terminal for the prompt loop')
        status=0
        while True:
            try:q=input('files> ')
            except EOFError:return status
            if q.strip() in ('quit','exit',':q'):return status
            if q.strip():status=process(q,engine)
    try:
        if not args.engine:return session()
        import asset
        from engine import Engine
        with asset.from_file(args.engine) as image, Engine(image,files_job.TOOLS) as engine:return session(engine)
    except KeyboardInterrupt:return 130
    except Exception as e:
        print(f'kilix-needle files: {e}',file=sys.stderr);return 2
