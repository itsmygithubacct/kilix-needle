"""Lazy, caller-owned providers for the shared command policy.

Neither constructing this collection nor using the grammar starts a model.
Needle3 requires an explicit verified candidate; LFM2 uses release pins or an
explicit local artifact manifest. Both paths verify every loaded asset.
"""
from contextlib import ExitStack
import time

import commands_policy


class Needle3:
    def __init__(self, runtime):
        self.runtime = runtime

    def decide(self, request):
        import engine
        started = time.monotonic()
        self.runtime.reset()
        reply = self.runtime.complete(request)
        calls, error = engine.reply_calls(reply)
        if error:
            raise commands_policy.ProposalError('Needle3 response could not be used: ' + error)
        return {'calls': calls, 'reply': reply, 'seconds': time.monotonic() - started}


class Providers:
    def __init__(self, backend, *, candidate=None, bundle=None, root=None, manifest=None):
        if backend not in ('grammar', 'needle3', 'lfm2'):
            raise ValueError('unknown command backend')
        self.backend = backend
        self.candidate, self.bundle, self.root = candidate, bundle, root
        self.manifest = manifest
        self.resources = ExitStack()
        self.runtime = None
        self.closed = False

    def get(self):
        if self.closed:
            raise commands_policy.ProposalError('the command providers are closed')
        if self.backend == 'grammar':
            raise commands_policy.ProposalError('grammar has no model provider')
        if self.runtime is None:
            try:
                if self.backend == 'needle3':
                    if self.candidate:
                        import compact_model
                        opened = compact_model.open_runtime(self.candidate)
                    else:
                        import commands_needle3
                        opened = commands_needle3.open_runtime(self.root)
                    runtime = self.resources.enter_context(opened)
                    self.runtime = Needle3(runtime)
                else:
                    import commands_runtime
                    self.runtime = self.resources.enter_context(commands_runtime.Runtime(
                        self.bundle, self.root, manifest=self.manifest))
            except BaseException:
                self.close()
                raise
        return self.runtime

    def close(self):
        self.closed = True
        self.resources.close()
        self.runtime = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
