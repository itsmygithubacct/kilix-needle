import support
import argparse
from contextlib import redirect_stderr
import io
import unittest
from unittest.mock import Mock, patch

import ask
import commands_ask
import commands_contract
import commands_policy
import compact_runner
import needle_cli


class AskCommands(unittest.TestCase):
    def jobs(self, session):
        engine = Mock(side_effect=AssertionError('legacy model must not be loaded'))
        return ask.Jobs(engine, engine, confirm=lambda _q: False, agent=True, commands=session)

    def test_mixed_whole_request_uses_one_ordered_command_plan(self):
        text = 'open calculator then start codex here'
        calls = commands_contract.exact_calls(text)
        planned = {'status': 0, 'items': [{'outcome': 'would', 'step': {'kind': 'app'}}]}
        session = commands_ask.Session(operation_id='same-intent')
        with patch.object(ask.router, 'route', side_effect=AssertionError('no partial routes')), \
                patch.object(compact_runner, 'run', return_value=planned) as run:
            result = ask.ask(text, self.jobs(session), act=False)
        self.assertEqual(result['calls'], calls)
        self.assertEqual(result['route']['job'], 'commands')
        self.assertTrue(run.call_args.kwargs['dry_run'])
        self.assertEqual(run.call_args.kwargs['operation_id'], 'same-intent')

    def test_plan_and_act_reuse_proposal_and_operation_id(self):
        text = 'open calculator'
        provider = Mock()
        provider.return_value.decide.return_value = {'calls': commands_contract.exact_calls(text)}
        session = commands_ask.Session('lfm2', provider=provider, model_only=True)
        record = {'status': 0, 'items': [{'outcome': 'would', 'step': {'kind': 'app'}}]}
        jobs = self.jobs(session)
        with patch.object(compact_runner, 'run', return_value=record) as run:
            plan = ask.ask(text, jobs, act=False)
            result = ask.ask(text, jobs, act=True, assume_yes=True,
                             route=ask.router.Route(0, 'commands'))
        provider.return_value.decide.assert_called_once_with(text)
        self.assertEqual(plan['operation_id'], result['operation_id'])
        self.assertEqual(run.call_count, 3)  # planning, fresh preflight, execution
        self.assertNotIn('dry_run', run.call_args.kwargs)

    def test_wrong_model_proposal_never_substitutes_grammar_or_reaches_host(self):
        provider = Mock()
        provider.return_value.decide.return_value = {
            'calls': commands_contract.exact_calls('open notepad')}
        session = commands_ask.Session('needle3', provider=provider, model_only=True)
        with patch.object(compact_runner, 'run') as run:
            record = ask.ask('open calculator', self.jobs(session), act=True, assume_yes=True)
        self.assertEqual(record['status'], 1)
        self.assertEqual(record['interpretation'], 'needle3')
        run.assert_not_called()

    def test_no_partial_request_or_runtime_error_reaches_host(self):
        for reply in ({'calls': []}, {'type': 'error', 'calls': []}):
            with self.subTest(reply=reply), patch.object(compact_runner, 'run') as run:
                provider = Mock()
                provider.return_value.decide.return_value = reply
                session = commands_ask.Session('lfm2', provider=provider)
                if reply.get('type'):
                    with self.assertRaises(commands_policy.ProposalError):
                        session.run('open calculator and erase files', dry_run=False, assume_yes=True)
                else:
                    result = session.run('open calculator and erase files', dry_run=False, assume_yes=True)
                    self.assertEqual(result['status'], 1)
                run.assert_not_called()

    def test_consent_is_required_before_executing_the_preflighted_batch(self):
        session = commands_ask.Session()
        record = {'status': 0, 'items': [{'outcome': 'would', 'step': {'kind': 'app'}}]}
        with patch.object(compact_runner, 'run', return_value=record) as run:
            refused = ask.ask('open calculator', self.jobs(session), act=True)
        self.assertEqual(refused['status'], 1)
        self.assertEqual(refused['items'][0]['outcome'], 'declined')
        self.assertTrue(run.call_args.kwargs['dry_run'])
        run.assert_called_once()

    def test_shared_mode_never_loads_a_legacy_generative_model(self):
        args = argparse.Namespace(commands_backend='grammar', commands_candidate=None,
                                  commands_bundle=None, commands_model_only=False,
                                  operation_id=None, agent=True, root=None)
        jobs, stack = ask.cli_jobs(args, interactive=False)
        with stack, patch.object(needle_cli, 'open_runtime', side_effect=AssertionError('model loaded')):
            for job in ask.router.RUNS:
                engine = jobs.engine(job)
                self.assertEqual(engine.complete('unknown request')['function_calls'], [])

    def test_explicit_job_does_not_route_a_different_command_job(self):
        jobs = self.jobs(commands_ask.Session())
        with patch.object(needle_cli, 'run_request', return_value={'status': 1, 'items': []}), \
                patch.object(compact_runner, 'run') as run:
            jobs.engine = Mock(return_value=commands_ask.NoProposal())
            result = ask.ask('open calculator', jobs, act=False, job='panes')
        self.assertEqual(result['route']['job'], 'panes')
        run.assert_not_called()

    def test_cli_jobs_pass_explicit_manifest_into_forced_lfm_provider(self):
        args = argparse.Namespace(commands_backend='lfm2', commands_candidate=None,
                                  commands_bundle='/fixture/bundle', commands_manifest='/fixture/model.json',
                                  commands_model_only=True, operation_id=None, agent=True, root=None)
        runtime = Mock()
        runtime.__enter__ = Mock(return_value=runtime)
        runtime.__exit__ = Mock(return_value=False)
        runtime.decide.return_value = {'calls': []}
        with patch('commands_runtime.Runtime', return_value=runtime) as factory:
            jobs, stack = ask.cli_jobs(args, interactive=False)
            with stack:
                result = jobs.commands.run('No action is requested.', dry_run=True)
        self.assertEqual(result['interpretation'], 'lfm2')
        self.assertEqual(result['items'], [])
        factory.assert_called_once_with('/fixture/bundle', None, manifest='/fixture/model.json')
        runtime.__exit__.assert_called_once()

    def test_forced_job_cannot_bypass_common_validation_or_run_a_partial_batch(self):
        jobs = self.jobs(commands_ask.Session())
        for text in ('start codex with model gpt-other',
                     'start codex then open calculator'):
            with self.subTest(text=text), patch.object(compact_runner, 'run') as run:
                result = ask.ask(text, jobs, act=True, assume_yes=True, job='agents')
                self.assertEqual(result['status'], 1)
                run.assert_not_called()

    def test_invalid_explicit_retry_id_never_becomes_a_new_operation(self):
        for operation_id in ('', '../other', 'a'*49):
            with self.subTest(operation_id=operation_id), self.assertRaises(compact_runner.WorkflowError):
                commands_ask.Session(operation_id=operation_id)

    def test_mismatched_backend_options_refuse_before_loading(self):
        for options in (['--commands-backend', 'grammar', '--commands-model-only'],
                        ['--operation-id', ''],
                        ['--commands-backend', 'lfm2', '--commands-candidate', 'candidate.json'],
                        ['--commands-backend', 'needle3', '--commands-bundle', '/fixture'],
                        ['--commands-manifest', 'model.json'],
                        ['--commands-backend', 'lfm2', '--commands-manifest', 'model.json'],
                        ['--commands-backend', 'needle3', '--commands-bundle', '/fixture',
                         '--commands-manifest', 'model.json']):
            with self.subTest(options=options), patch.object(ask, 'cli_jobs') as jobs, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                ask.main([*options, 'open calculator'])
            self.assertEqual(stopped.exception.code, 2)
            jobs.assert_not_called()


if __name__ == '__main__':
    unittest.main()
