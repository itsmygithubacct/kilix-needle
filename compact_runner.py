"""Execute the bounded five-tool contract with durable, at-most-once intents.

The operation ID belongs to the caller and must be reused for a retry. A new
intent gets a new ID. A created pane is not a ready application. Unknown
startup and uncertain delivery remain explicit and never cause a replay.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

import action_backend
import agents
import agents_kilix as ak
import apps
import apps_kilix
import compact
import compact_ready
import kilix


class WorkflowError(RuntimeError):
    pass


def check_operation_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,47}', value):
        raise WorkflowError('operation ID must be 1–48 ASCII identifier characters')


class Journal:
    """Private, locked, bounded and atomically replaced operation records."""
    def __init__(self, root=None):
        self.root = Path(root or Path(os.environ.get('XDG_STATE_HOME', Path.home()/'.local/state'))/'kilix-needle/compact')

    @contextmanager
    def open(self, operation_id):
        check_operation_id(operation_id)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        directory=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        lock=None
        try:
            info=os.fstat(directory)
            if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
                raise WorkflowError('operation directory must be private and owned by this user')
            lock=os.open(operation_id+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=directory)
            info=os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)&0o077:
                raise WorkflowError('invalid operation lock')
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as error:raise WorkflowError('this operation is already running; query it later') from error
            name=operation_id+'.json'
            try:
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
            except FileNotFoundError:record=None
            else:
                with os.fdopen(fd,'r') as stream:
                    info=os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)&0o077 or info.st_size>65536:
                        raise WorkflowError('invalid operation record')
                    record=json.load(stream)
            def save(value):
                data=json.dumps(value,ensure_ascii=True).encode()
                if len(data)>65536:raise WorkflowError('operation record exceeds its bound')
                temporary=operation_id+'.tmp'
                # A stale temporary is never read or followed.
                try:os.unlink(temporary,dir_fd=directory)
                except FileNotFoundError:pass
                fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
                with os.fdopen(fd,'wb') as stream:
                    stream.write(data);stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,name,src_dir_fd=directory,dst_dir_fd=directory)
                os.fsync(directory)
            yield record,save
        finally:
            if lock is not None:os.close(lock)
            os.close(directory)


class Host:
    def __init__(self,*,pdf_core=None,tui_root=None):
        self.pdf_core=str(Path(pdf_core).resolve()) if pdf_core else None
        self.tui_root=str(Path(tui_root).resolve()) if tui_root else None
        if self.pdf_core and not os.access(self.pdf_core,os.X_OK):
            raise WorkflowError('the selected PDF core is not executable')

    def caller(self):
        pane,broker,cwd=ak.caller_info()
        return {'pane_id':pane,'broker':broker},cwd

    def snapshot(self):
        return ak._listing(['panes','list','--json'])['panes']

    def dispatch(self,request):
        return action_backend.dispatch(request)

    def _target(self,step):
        pane=next((p for p in self.snapshot() if p.get('pane_id')==step['target']['pane_id']),None)
        if pane is None:raise WorkflowError('the target pane is gone')
        if ak._broker_id(pane)!=step['target']['broker']:
            raise WorkflowError('the target pane identity changed')
        coding=pane.get('coding_session') or {}
        observed_cwd=coding.get('cwd') or pane.get('cwd')
        if (ak.PROVIDER_AGENT.get(coding.get('provider'))!=step['agent']
                or not isinstance(observed_cwd,str) or not Path(observed_cwd).is_absolute()
                or Path(observed_cwd).resolve()!=Path(step['cwd'])):
            raise WorkflowError('the target agent or directory changed')
        return pane

    def prepare(self,action,cwd,source,operation_id):
        a=action.args
        step={'kind':action.kind,'args':dict(a)}
        if isinstance(action,apps.Action):
            if not apps_kilix._ready('app',a['app']):
                raise WorkflowError(a['app']+' is not installed; nothing was started')
            argv=['/usr/bin/env',*(k+'='+v for k,v in apps_kilix.NO_INSTALL.items()),kilix.KILIX,'app','run',a['app']]
            step['app_location']=compact_ready.app_location(a['app'])
            if self.tui_root and a['app'] in ('kilix-calculator','kilix-notepad','kilix-file'):
                binary=Path(self.tui_root)/'.runtime/bin'/a['app']
                if not os.access(binary,os.X_OK):raise WorkflowError('the selected TUI application is not executable')
                argv=argv[:-4]+[str(binary)]
                step['app_location']={'root':self.tui_root,'binary':str(binary)}
            if a['app']=='kilix-pdf':
                argv.insert(1,'KILIX_PDF_READINESS=1')
                if self.pdf_core:
                    argv.insert(1,'KILIX_PDF_VIEWER_CORE='+self.pdf_core)
                    step['pdf_core']=self.pdf_core
            if 'file_path' in a:
                path=Path(a['file_path']).expanduser()
                if not path.is_absolute() or not path.is_file() or not os.access(path,os.R_OK):
                    raise WorkflowError('the requested PDF is not a readable file')
                # No shell expansion or punctuation rewriting of file names.
                argv+=['--',str(path)]
            params={'argv':argv,'cwd':str(ak.resolve_dir('here',cwd=cwd)),
                    'title':a['app'],'placement':'new-tab'}
            step.update(kind='app',operation='pane.open',params=params)
        elif action.kind=='agent':
            directory=str(ak.resolve_dir(a['dir'],cwd=cwd))
            place=a.get('place','tab')
            params={'agent':a['agent'],'cwd':directory,'title':a['agent']+' · '+Path(directory).name,
                    'placement':'new-tab' if place=='tab' else 'split','trust_folder':False}
            if place!='tab':params['direction']=place
            for name in ('model','prompt'):
                if name in a:params[name]=a[name]
            step.update(operation='agent.launch',params=params,agent=a['agent'],cwd=directory)
        else:
            agent,_,directory=a['session'].partition('@')
            directory=ak.resolve_dir(directory,cwd=cwd)
            pane=ak.find_session(agent,directory,caller_pane=source['pane_id'])
            if pane.get('activity') not in ('idle','working'):
                raise WorkflowError('the existing agent state is unreadable or waiting for input')
            if action.kind=='tell' and agent not in ak._WORKING_STEER_AGENTS and pane['activity']!='idle':
                raise WorkflowError('this agent can receive a message only while idle')
            step.update(agent=agent,cwd=str(directory),target={'pane_id':pane['pane_id'],'broker':ak._broker_id(pane)})
            return step
        request={'schema':action_backend.SCHEMA,'operation_id':operation_id,'operation':step['operation'],
                 'source':source,'target':source,'params':step['params'],'timeout':15,'dry_run':True}
        preview=self.dispatch(request)
        if preview['status']!='planned':raise WorkflowError(preview.get('error','the host refused the launch'))
        step['request']=dict(request,dry_run=False)
        return step

    def start(self,step):
        if 'request' in step:return self.dispatch(step['request'])
        pane=self._target(step)
        if step['kind']=='wait':return {'status':'observing'}
        if (pane.get('activity') not in ('idle','working')
                or step['agent'] not in ak._WORKING_STEER_AGENTS and pane['activity']!='idle'):
            raise WorkflowError('the target is no longer accepting messages')
        ak._run(['agent-control','send',str(step['target']['pane_id']),'--expect-broker',step['target']['broker'],
                 '--text',step['args']['text'],'--submit'])
        return {'status':'submitted','identity_verified':True,'delivery_verified':True,
                'acknowledgment_verified':False,'completion_verified':False}

    def recover(self,step):
        if 'request' not in step:
            return {'status':'observing'} if step['kind']=='wait' else {'status':'uncertain'}
        request=dict(step['request'],operation='operation.status',params={},dry_run=False)
        return self.dispatch(request)

    def observe(self,step,receipt):
        status=receipt['status']
        if status=='submitted':return {'outcome':'submitted','receipt':receipt}
        if status in ('blocked','not_found','uncertain'):
            return {'outcome':'failed' if status=='blocked' else 'uncertain','receipt':receipt}
        if step['kind']=='wait':
            pane=self._target(step)
            activity=pane.get('activity')
            return {'outcome':'done' if activity=='idle' else 'pending' if activity=='working' else 'failed',
                    'evidence':{'pane':step['target'],'activity':activity}}
        if status!='created':return {'outcome':'pending','receipt':receipt}
        identity=receipt['evidence']['pane']
        if step['kind']=='app':
            return {**compact_ready.observe(step,identity),'receipt':receipt}
        check=dict(step,target={'pane_id':identity['pane_id'],'broker':identity['broker']})
        current=next((p for p in self.snapshot() if p.get('pane_id')==identity['pane_id']),None)
        if current is None:
            return {'outcome':'failed','receipt':receipt,'reason':'the created pane is gone'}
        if ak._broker_id(current)!=identity['broker']:
            return {'outcome':'failed','receipt':receipt,'reason':'the created pane identity changed'}
        if not current.get('coding_session'):
            return {'outcome':'failed' if current.get('activity')=='failed' else 'pending','receipt':receipt,
                    'reason':'agent startup has not been observed'}
        try:pane=self._target(check)
        except WorkflowError as error:
            return {'outcome':'failed','receipt':receipt,'reason':str(error)}
        activity=pane.get('activity')
        return {'outcome':'done' if activity in ('idle','working') else 'failed' if activity=='failed' else 'pending',
                'receipt':receipt,'evidence':{'pane':check['target'],'activity':activity,
                                           'agent_startup_verified':activity in ('idle','working'),
                                           'completion_verified':False}}


def run(request,calls,*,operation_id,cwd=None,dry_run=False,host=None,journal=None):
    """Run only a fully admitted batch; a pending member pauses later actions."""
    admission=compact.admit(request,calls,cwd=cwd)
    if not admission.actions:
        return {'operation_id':operation_id,'status':1,'items':[],'note':admission.reason}
    host=host or Host();journal=journal or Journal()
    source,observed_cwd=host.caller();cwd=cwd or observed_cwd
    fingerprint=hashlib.sha256(json.dumps({'request':request,'calls':calls,'cwd':cwd,'source':source,
                                         'pdf_core':getattr(host,'pdf_core',None),
                                         'tui_root':getattr(host,'tui_root',None)},sort_keys=True).encode()).hexdigest()
    with journal.open(operation_id) as (saved,save):
        reused=saved is not None
        if saved is not None and saved.get('fingerprint')!=fingerprint:
            raise WorkflowError('operation ID was already used for a different request or caller')
        if saved is None:
            # Complete host preflight before the first side effect.
            steps=[host.prepare(a,cwd,source,operation_id+'.'+str(i)) for i,a in enumerate(admission.actions)]
            previous=set()
            for step in steps:
                target=(step.get('agent'),step.get('cwd'))
                if step['kind'] in ('wait','tell') and target in previous:
                    raise WorkflowError('this batch depends on a new agent turn; request its follow-up separately')
                if step['kind'] in ('agent','tell'):previous.add(target)
            saved={'operation_id':operation_id,'fingerprint':fingerprint,'steps':steps,'items':[],'created_unix':time.time()}
        if dry_run:
            return {'operation_id':operation_id,'status':0,'items':[{'outcome':'would','step':s} for s in saved['steps']]}
        for index,step in enumerate(saved['steps']):
            prior=saved['items'][index] if index<len(saved['items']) else None
            if prior and prior['outcome'] in ('done','submitted'):continue
            if prior and prior['outcome']=='failed':break
            try:
                if prior:
                    receipt=prior.get('receipt')
                    if not receipt or receipt.get('status') not in ('created','submitted','observing'):
                        receipt=host.recover(step)
                else:
                    prior={'outcome':'uncertain','reason':'intent recorded; result not yet received','started_unix':time.time()}
                    saved['items'].append(prior)
                    save(saved)
                    receipt=host.start(step)
                result=host.observe(step,receipt)
            except (OSError,ValueError,ak.AgentsError,kilix.KilixError,WorkflowError) as error:
                result={'outcome':'uncertain','reason':str(error)[:240]}
            result['started_unix']=prior.get('started_unix',time.time())
            if result['outcome']=='pending' and time.time()-result['started_unix']>=30:
                result.update(outcome='timed_out',reason='readiness was not verified within 30 seconds; retrying this operation ID only observes it')
            saved['items'][index]=result;save(saved)
            if result['outcome'] not in ('done','submitted'):break
        complete=len(saved['items'])==len(saved['steps']) and all(i['outcome'] in ('done','submitted') for i in saved['items'])
        return {'operation_id':operation_id,'reused_operation':reused,'status':0 if complete else 1,'items':saved['items'],
                'note':'' if complete else 'operation is incomplete; reuse this operation ID to check it without repeating actions'}
