"""Pinned CPU LFM2 completion; proposals are returned, never executed here."""
from __future__ import annotations

import ast
from contextlib import ExitStack
import json
import os
import platform
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, build_opener, ProxyHandler

import asset

ROOT = Path(__file__).resolve().parent
ASSET_ID = 'lfm2-kilix-commands'
SYSTEM = ('Use the available tools for the user request. Preserve explicit arguments exactly. '
          'Use only the tools that satisfy the entire request. If no complete supported action is requested, return no tool calls.')
TOOLS = json.loads((ROOT / 'commands-tools.json').read_text())


class CommandModelError(RuntimeError):
    pass


def parse_calls(text):
    if not isinstance(text, str) or len(text.encode()) > 32768:
        raise CommandModelError('invalid or oversized model output')
    text=text.strip()
    if text.endswith('<|im_end|>'):text=text[:-len('<|im_end|>')].rstrip()
    start,end='<|tool_call_start|>','<|tool_call_end|>'
    if not text.startswith(start) or not text.endswith(end):
        raise CommandModelError('incomplete native tool-call output')
    try:
        node=ast.parse(text[len(start):-len(end)],mode='eval').body
        if not isinstance(node,ast.List) or len(node.elts)>4:raise ValueError('expected at most four calls')
        calls=[]
        for item in node.elts:
            if not isinstance(item,ast.Call) or not isinstance(item.func,ast.Name) or item.args:
                raise ValueError('expected a named call with keyword arguments')
            args={}
            for kw in item.keywords:
                if kw.arg is None or kw.arg in args:raise ValueError('expanded or duplicate argument')
                value=ast.literal_eval(kw.value)
                if not isinstance(value,str):raise ValueError('expected string argument')
                args[kw.arg]=value
            calls.append({'name':item.func.id,'arguments':args})
        return calls
    except (ValueError,TypeError,SyntaxError,RecursionError) as exc:
        raise CommandModelError('invalid literal tool-call output: '+str(exc)) from exc


class NativeClient:
    def __init__(self,url,key='',timeout=180):
        self.url=url.rstrip('/');self.key=key;self.timeout=timeout
        self.opener=build_opener(ProxyHandler({}))

    def post(self,path,payload):
        request=Request(self.url+path,data=json.dumps(payload).encode(),
                        headers={'Content-Type':'application/json','Authorization':'Bearer '+self.key})
        try:
            with self.opener.open(request,timeout=self.timeout) as response:
                raw=response.read(4*1024*1024+1)
            if len(raw)>4*1024*1024:raise ValueError('oversized response')
            value = json.loads(raw)
            if not isinstance(value, dict):raise ValueError('expected a response object')
            return value
        except (OSError,ValueError,URLError) as exc:
            raise CommandModelError('local model request failed: '+str(exc)) from exc

    def prompt_tokens(self,request):
        wrapped=[{'type':'function','function':tool} for tool in TOOLS]
        rendered=self.post('/apply-template',{'messages':[{'role':'system','content':SYSTEM},
                           {'role':'user','content':request}], 'tools':wrapped,
                           'chat_template_kwargs':{'tools':TOOLS},'add_generation_prompt':True})
        if not isinstance(rendered,dict) or not isinstance(rendered.get('prompt'),str):
            raise CommandModelError('model template did not return a prompt')
        tokens=self.post('/tokenize',{'content':rendered['prompt'],'add_special':True,'parse_special':True}).get('tokens')
        if not isinstance(tokens,list) or not tokens or len(tokens)>4096 or any(type(x) is not int for x in tokens):
            raise CommandModelError('invalid or oversized prompt tokens')
        return tokens

    def decide(self,request):
        began=time.monotonic()
        tokens=self.prompt_tokens(request)
        raw=self.post('/completion',{'prompt':tokens,'temperature':0,'top_p':1,'top_k':0,
                      'min_p':0,'seed':17,'n_predict':512,'stream':False,
                      'cache_prompt':True,'return_tokens':True})
        if raw.get('truncated') or raw.get('stop_type')!='eos':
            raise CommandModelError('model completion did not finish normally')
        settings=raw.get('generation_settings',{})
        if not isinstance(settings,dict):
            raise CommandModelError('invalid generation settings')
        if settings.get('grammar') or settings.get('json_schema'):
            raise CommandModelError('unexpected generation grammar')
        output=raw.get('tokens')
        if not isinstance(output,list) or len(output)>512 or any(type(x) is not int for x in output):
            raise CommandModelError('invalid output token list')
        content=self.post('/detokenize',{'tokens':output}).get('content')
        calls=parse_calls(content)
        return {'calls':calls,'content':content,'seconds':time.monotonic()-began,
                'timings':raw.get('timings'),'tokens':output}


def selection(manifest=None):
    try:
        path=ROOT/'commands-model.json' if manifest is None else Path(manifest)
        if path.stat().st_size>65536:
            raise ValueError('command model manifest is too large')
        value=json.loads(path.read_text())
    except (OSError,ValueError) as exc:
        raise CommandModelError('command model selection is unavailable') from exc
    if (not isinstance(value,dict) or value.get('schema')!='kilix.commands.model/v1'
            or not isinstance(value.get('files'),dict) or not value['files']):
        raise CommandModelError('no valid command model is selected')
    for name,item in value['files'].items():
        path=Path(name)
        if (path.is_absolute() or '..' in path.parts or not isinstance(item,dict)
                or not isinstance(item.get('sha256'),str) or len(item['sha256'])!=64
                or any(c not in '0123456789abcdef' for c in item['sha256'])
                or type(item.get('bytes')) is not int or item['bytes']<=0):
            raise CommandModelError('invalid command model member')
    if value.get('model_member') not in value['files'] or 'runtime/llama-server' not in value['files']:
        raise CommandModelError('command model or runtime member is missing')
    return value


def require_platform():
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64') or not hasattr(os, 'memfd_create'):
        raise CommandModelError('the command runtime requires x86-64 Linux Python with memfd_create')


def inspect_bundle(bundle=None,root=None,*,manifest=None):
    require_platform()
    if manifest is not None and bundle is None:
        raise CommandModelError('an explicit command manifest needs a local bundle')
    chosen=selection(manifest)
    if bundle is None:
        spec,destination=asset._installed_spec(ASSET_ID,root)
        import commands_selection
        try:
            commands_selection.verify_catalog('lfm2', spec, chosen)
        except ValueError as error:
            raise CommandModelError(str(error)) from error
        bundle=destination
    path=Path(bundle)
    if not path.is_absolute() or path.is_symlink():
        raise CommandModelError('model bundle must be an absolute regular directory')
    return chosen,path


class Runtime(NativeClient):
    """One private authenticated localhost server; all files are verified first."""
    def __init__(self,bundle=None,root=None,*,manifest=None):
        self.resources=ExitStack();self.process=None
        try:
            chosen,path=inspect_bundle(bundle,root,manifest=manifest)
            temporary=Path(self.resources.enter_context(tempfile.TemporaryDirectory(prefix='kilix-lfm2-')))
            self.log=self.resources.enter_context((temporary/'server.log').open('w+b'))
            self.images={}
            for name,item in chosen['files'].items():
                image=self.resources.enter_context(asset.load_verified(path/name,item['sha256'],item['bytes']))
                self.images[name]=image
                if name.startswith('runtime/'):
                    target=temporary/name;target.parent.mkdir(exist_ok=True)
                    with target.open('wb') as stream:
                        offset=0
                        while block:=os.pread(image.fd,1024*1024,offset):
                            stream.write(block);offset+=len(block)
                    target.chmod(0o500)
            model=self.images[chosen['model_member']]
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            key=secrets.token_hex(24)
            super().__init__(f'http://127.0.0.1:{port}',key)
            runtime=temporary/'runtime'
            argv=[str(runtime/'llama-server'),'-m',model.path,'--device','none','--gpu-layers','0',
                  '--no-kv-offload','--no-op-offload','--ctx-size','8192','--parallel','1',
                  '--batch-size','256','--ubatch-size','16','--fit','off','--jinja',
                  '--reasoning','off','--host','127.0.0.1','--port',str(port),'--no-webui','--api-key',key]
            env=dict(os.environ,CUDA_VISIBLE_DEVICES='',HIP_VISIBLE_DEVICES='',ROCR_VISIBLE_DEVICES='',
                     LD_LIBRARY_PATH=str(runtime))
            self.process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=self.log,stderr=subprocess.STDOUT,
                                          env=env,pass_fds=(model.fd,),start_new_session=True)
            deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                if self.process.poll() is not None:raise CommandModelError('CPU model server exited during startup')
                try:
                    request=Request(self.url+'/health',headers={'Authorization':'Bearer '+key})
                    with self.opener.open(request,timeout=1) as response:
                        if response.status==200:break
                except (OSError,URLError):time.sleep(.1)
            else:raise CommandModelError('CPU model startup timed out')
        except BaseException:
            self.close();raise

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
        self.resources.close()

    def __enter__(self):return self
    def __exit__(self,*_args):self.close()
