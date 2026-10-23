"""Shared Ask planning and execution for the bounded command contract."""
import uuid

import commands_contract
import commands_policy
import compact_runner


class NoProposal:
    """Keep legacy deterministic jobs available without loading their models."""
    label = 'grammar only'

    def reset(self):
        pass

    def complete(self, _request):
        return {'type': 'call', 'function_calls': []}


class Session:
    def __init__(self, backend='grammar', *, provider=None, cwd=None,
                 model_only=False, operation_id=None, host=None, journal=None):
        if model_only and backend == 'grammar':
            raise ValueError('model-only needs Needle3 or LFM2')
        if operation_id is not None:
            compact_runner.check_operation_id(operation_id)
        self.backend, self.provider, self.cwd = backend, provider, cwd
        self.model_only, self.operation_id = model_only, operation_id
        self.host, self.journal = host, journal
        self.decisions, self.operations = {}, {}

    @staticmethod
    def recognizes(request, job=None):
        calls = commands_contract.exact_calls(request)
        if not calls:
            return False
        # A malformed supported command is still handled here and refused;
        # it must not be retried through an older, broader interpreter.
        if job is None:
            return True
        admission = commands_contract.admit(request, calls)
        return admission.job == job

    def decide(self, request):
        if request not in self.decisions:
            self.decisions[request] = commands_policy.decide(
                request, provider=self.provider, backend=self.backend,
                grammar_first=not self.model_only, cwd=self.cwd)
        return self.decisions[request]

    def run(self, request, *, dry_run, assume_yes=False, confirm=lambda _q: False):
        decision = self.decide(request)
        operation_id = self.operations.setdefault(
            request, self.operation_id or 'ask-' + uuid.uuid4().hex)
        metadata = {'request': request, 'operation_id': operation_id,
                    'interpretation': decision.source, 'calls': decision.calls,
                    'interpretation_seconds': decision.seconds,
                    'model_seconds': decision.model_seconds}
        if not decision.admission.actions:
            return {**metadata, 'status': 1, 'items': [], 'note': decision.admission.reason}
        kwargs = dict(operation_id=operation_id, cwd=self.cwd, host=self.host, journal=self.journal)
        plan = compact_runner.run(request, decision.calls, dry_run=True, **kwargs)
        if dry_run or plan['status']:
            return {**plan, **metadata}
        if not assume_yes and not confirm('Run the planned command actions?'):
            return {**plan, **metadata, 'status': 1, 'note': 'not run',
                    'items': [{**item, 'outcome': 'declined'} for item in plan['items']]}
        return {**compact_runner.run(request, decision.calls, **kwargs), **metadata}


def render(record):
    lines = []
    for item in record.get('items', []):
        step = item.get('step', {})
        detail = step.get('kind', '')
        if step.get('args'):
            detail += ' ' + repr(step['args'])
        lines.append((item.get('outcome', '') + ': ' + detail).rstrip(': '))
    if record.get('note'):
        lines.append(record['note'])
    if record.get('operation_id'):
        lines.append('operation: ' + record['operation_id'])
    return '\n'.join(lines)
