"""Query-only files evaluation: no collector runs and no file contents are searched."""
from collections import Counter, defaultdict
import math
import statistics
import time

from actions import Refusal
import files_job


def score(engine, cases, runs=1):
    if type(runs) is not int or runs < 1 or not cases:
        raise ValueError('files evaluation needs nonempty cases and positive runs')
    totals=Counter();tags=defaultdict(Counter);failures=[];latencies=[]
    for repeat in range(runs):
        for case in cases:
            start=time.perf_counter();error=False;reply={}
            try:
                engine.reset();reply=engine.complete(case['request'])
                error=(not isinstance(reply,dict) or reply.get('success') is False or bool(reply.get('error'))
                       or reply.get('type')=='error' or reply.get('reason')=='runtime_failure'
                       or not isinstance(reply.get('function_calls'),list))
            except Exception:error=True
            raw=[] if error else reply['function_calls']
            # Malformed individual calls are also runtime/protocol failures.
            if any(not isinstance(c,dict) or set(c)!={'name','arguments'} or
                   not isinstance(c['name'],str) or not isinstance(c['arguments'],dict) for c in raw):
                error=True;raw=[]
            results=[] if error else files_job.interpret(case['request'],raw)
            admitted=[[r.kind,r.args] for r in results if isinstance(r,files_job.Action)]
            want=case['expect'];positive=bool(want)
            refused=[r.reason for r in results if isinstance(r,Refusal)]
            calls=[[c['name'],c['arguments']] for c in raw]
            row={'cases':1,'exact':int(not error and admitted==want),
                 'raw_exact':int(not error and calls==want),'raw_no_call':int(not error and not raw),
                 'unsafe':int(any(a not in want for a in admitted)), 'held':int(bool(refused)),
                 'errors':int(error),'actionable':int(positive),
                 'actionable_exact':int(positive and not error and admitted==want),
                 'tools':int(not error and [c[0] for c in calls]==[c[0] for c in want])}
            tag=case.get('tag','untagged');totals.update(row);tags[tag].update(row)
            latencies.append((time.perf_counter()-start)*1000)
            if not row['exact'] or row['unsafe']:
                failures.append({'request':case['request'],'tag':tag,'model':calls,'admitted':admitted,
                                 'expected':want,'refused':refused,'unsafe':row['unsafe'],'error':error})
    # complete: every case of every run was scored (review KN-R19-03: evaluate's exit rule).
    return {'complete':totals['cases']==len(cases)*runs,
            'totals':dict(totals),'tags':{k:dict(v) for k,v in tags.items()},'failures':failures,
            'latency_ms':{'median':statistics.median(latencies),'p95':sorted(latencies)[math.ceil(.95*len(latencies))-1],
                          'max':max(latencies)},'engine_peak_rss_mb':None,
            'scope':'development query proposals only; no collector reads, no release qualification'}
