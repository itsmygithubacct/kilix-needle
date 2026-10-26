"""Explicit local generation-3 candidate loading for the compact contract.

The caller selects a local manifest. No download, installation or change to
the production model is performed. The library and tool contract are fixed;
each candidate supplies its exact weight size and SHA-256.
"""
from contextlib import ExitStack, contextmanager
import hashlib
import json
from pathlib import Path
import platform
import re

import asset
import compact
import engine
import libengine

LIB_SHA256='87476ff70e80334d7c766dbf236b72f42876072df22014c6ec8a9b087b82b260'
LIB_BYTES=1590584
RUST_LIB_SHA256='347c2f13e7b24c0549cec139aa7cfe5cc63a77537f84e25fb375de39f52f31b4'
RUST_LIB_BYTES=1141848
TOOLS_SHA256='10f930ad6989955aff5eb7f11d18f3a9371cd6ceb43f7434136d4793e54be998'


def _supports_x86_v3():
    if platform.machine()!='x86_64':return False
    try:
        lines=Path('/proc/cpuinfo').read_text().splitlines()
        flags=[set(line.split(':',1)[1].split()) for line in lines if line.startswith('flags')]
    except OSError:return False
    required={'avx','avx2','fma','bmi1','bmi2','f16c','movbe','popcnt','sse4_1','sse4_2','ssse3','cx16','lahf_lm'}
    return bool(flags) and all(required<=row and bool({'abm','lzcnt'}&row) for row in flags)


def _member(root,spec,*,maximum):
    if (not isinstance(spec,dict) or not isinstance(spec.get('path'),str)
            or type(spec.get('bytes')) is not int or not 0<spec['bytes']<=maximum
            or not isinstance(spec.get('sha256'),str)
            or not re.fullmatch('[0-9a-f]{64}',spec['sha256'])):
        raise ValueError('invalid candidate member')
    relative=Path(spec['path'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('candidate members must stay inside their bundle')
    path=root/relative
    if not path.resolve().is_relative_to(root):
        raise ValueError('candidate member resolves outside its bundle')
    return path


@contextmanager
def open_runtime(manifest_path, *, expected=None):
    """Keep one verified candidate worker alive for a caller-owned session."""
    manifest_path=Path(manifest_path).resolve()
    if expected is None:
        if manifest_path.stat().st_size>65536:raise ValueError('candidate manifest is too large')
        manifest=json.loads(manifest_path.read_text())
    else:
        # Installed candidates bind the manifest too: never verify it and then
        # reopen a mutable pathname to choose a different set of model pins.
        import os
        if not 0 < expected['bytes'] <= 65536:
            raise ValueError('candidate manifest is too large')
        with asset.load_verified(manifest_path, expected['sha256'], expected['bytes']) as image:
            manifest=json.loads(os.pread(image.fd, expected['bytes'], 0))
    if not isinstance(manifest,dict) or manifest.get('schema')!='kilix-compact-candidate-v1' or manifest.get('generation')!=3:
        raise ValueError('not a generation-3 compact candidate')
    backend=manifest.get('backend','native')
    if backend=='native':
        library_sha,library_bytes=LIB_SHA256,LIB_BYTES
    elif backend=='rust3':
        library_sha,library_bytes=RUST_LIB_SHA256,RUST_LIB_BYTES
        if manifest.get('cpu_isa')!='x86-64-v3' or not _supports_x86_v3():
            raise ValueError('this cached CPU candidate requires x86-64-v3 support')
    else:raise ValueError('unsupported candidate backend')
    root=manifest_path.parent
    lib=manifest.get('library');weights=manifest.get('weights');tools=manifest.get('tools')
    library_path=_member(root,lib,maximum=library_bytes)
    weights_path=_member(root,weights,maximum=256*1024*1024)
    tools_path=_member(root,tools,maximum=65536)
    if lib['sha256']!=library_sha or lib['bytes']!=library_bytes or tools['sha256']!=TOOLS_SHA256:
        raise ValueError('candidate library or tools differ from the supported contract')
    data=tools_path.read_bytes()
    if len(data)!=tools['bytes'] or hashlib.sha256(data).hexdigest()!=TOOLS_SHA256:
        raise ValueError('candidate tools do not match their recorded bytes')
    with ExitStack() as stack:
        library=stack.enter_context(asset.load_verified(library_path,library_sha,library_bytes))
        archive=stack.enter_context(asset.load_verified(weights_path,weights['sha256'],weights['bytes']))
        runtime=stack.enter_context(libengine.LibEngine(library,json.loads(data),archive,generation=3,backend=backend,timeout=30))
        yield runtime


def propose(request,manifest_path):
    with open_runtime(manifest_path) as runtime:
        runtime.reset()
        reply=runtime.complete(request)
    calls,error=engine.reply_calls(reply)
    if error:raise ValueError('candidate response could not be used: '+error)
    return calls,reply


def decide(request,*,candidate=None,model_only=False):
    if model_only and not candidate:raise ValueError('--model-only requires --candidate')
    calls=None if model_only else compact.exact_calls(request)
    if calls:return {'source':'grammar','calls':calls}
    if not candidate:return {'source':'grammar','calls':[]}
    calls,reply=propose(request,candidate)
    # The caller still applies complete admission and host preflight. A model
    # answer never establishes a target identity, readiness or permission.
    return {'source':'model','calls':calls,'reply':reply}
