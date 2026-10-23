import support
from contextlib import contextmanager
import unittest
from unittest.mock import Mock, patch

import commands_models
import commands_policy
import compact_model
import commands_runtime


class ProviderLifecycle(unittest.TestCase):
    def test_grammar_never_opens_either_runtime(self):
        for backend in ('grammar', 'needle3', 'lfm2'):
            with self.subTest(backend=backend), commands_models.Providers(backend) as providers, \
                    patch.object(compact_model, 'open_runtime') as needle, \
                    patch.object(commands_runtime, 'Runtime') as lfm:
                decision = commands_policy.decide('open calculator', backend=backend,
                                                   provider=providers.get)
                self.assertTrue(decision.admission.actions)
                needle.assert_not_called()
                lfm.assert_not_called()

    def test_needle_reuses_worker_and_resets_each_request(self):
        runtime = Mock()
        runtime.complete.return_value = {'type': 'call', 'function_calls': []}
        opened, closed = [], []

        @contextmanager
        def open_runtime(candidate):
            opened.append(candidate)
            try:
                yield runtime
            finally:
                closed.append(candidate)

        with patch.object(compact_model, 'open_runtime', open_runtime):
            with commands_models.Providers('needle3', candidate='candidate.json') as providers:
                for request in ('explain a command', 'what is a pane'):
                    result = providers.get().decide(request)
                    self.assertEqual(result['calls'], [])
                self.assertEqual(runtime.reset.call_count, 2)
                self.assertEqual(opened, ['candidate.json'])
                self.assertEqual(closed, [])
            self.assertEqual(closed, opened)
            with self.assertRaises(commands_policy.ProposalError):
                providers.get()

    def test_needle_error_is_not_an_empty_answer(self):
        runtime = Mock()
        runtime.complete.return_value = {'type': 'error', 'error': 'unfinished'}
        with self.assertRaises(commands_policy.ProposalError):
            commands_models.Needle3(runtime).decide('open calculator')

    def test_lfm_is_opened_once_and_closed_on_exception(self):
        runtime = Mock()
        runtime.__enter__ = Mock(return_value=runtime)
        runtime.__exit__ = Mock(return_value=False)
        with patch.object(commands_runtime, 'Runtime', return_value=runtime) as factory:
            with self.assertRaisesRegex(RuntimeError, 'caller failed'):
                with commands_models.Providers('lfm2', bundle='/fixture', manifest='/fixture/model.json') as providers:
                    self.assertIs(providers.get(), providers.get())
                    raise RuntimeError('caller failed')
        factory.assert_called_once_with('/fixture', None, manifest='/fixture/model.json')
        runtime.__exit__.assert_called_once()


if __name__ == '__main__':
    unittest.main()
