"""Both model backends share the exact grammar, admission and failure policy."""
import unittest
from unittest.mock import Mock

import compact
import commands_contract as contract
from commands_policy import ProposalError, decide


def call(name, **args):
    return {"name": name, "arguments": args}


class CommandPolicyTests(unittest.TestCase):
    def test_existing_compact_entrypoint_uses_shared_contract(self):
        self.assertIs(compact.admit, contract.admit)
        self.assertIs(compact.exact_calls, contract.exact_calls)

    def test_grammar_leg_never_initializes_a_provider(self):
        provider = Mock(side_effect=AssertionError("model initialized"))
        self.assertTrue(decide("open calculator", provider=provider).admission.actions)
        self.assertFalse(decide("make me a sandwich", provider=provider).admission.actions)
        provider.assert_not_called()

    def test_both_grammar_first_legs_skip_model_on_exact_request(self):
        for backend in ("needle3", "lfm2"):
            with self.subTest(backend=backend):
                provider = Mock(side_effect=AssertionError("model initialized"))
                result = decide("start codex here", backend=backend, provider=provider)
                self.assertEqual(result.source, "grammar")
                self.assertTrue(result.admission.actions)
                provider.assert_not_called()

    def test_model_does_not_override_literal_request(self):
        for backend in ("needle3", "lfm2"):
            with self.subTest(backend=backend):
                model = Mock()
                model.decide.return_value = {"calls": [call("start_codex", place="left")]}
                result = decide("start codex here on the right", backend=backend,
                                grammar_first=False, provider=lambda: model)
                self.assertFalse(result.admission.actions)
                self.assertEqual(result.calls[0]["arguments"]["place"], "left")

    def test_new_request_forms_share_the_grammar_fast_path(self):
        for backend in ('grammar', 'needle3', 'lfm2'):
            for request in (
                'I need a fresh Codex session in "/projects/Blue Birch".',
                'Please message Codex in /projects/birch with this exact text: Compare A, B; then C',
                'I need the file manager opened now.',
            ):
                with self.subTest(backend=backend, request=request):
                    provider = Mock(side_effect=AssertionError('model initialized'))
                    result = decide(request, backend=backend, provider=provider)
                    self.assertEqual(result.source, 'grammar')
                    self.assertTrue(result.admission.actions)
                    provider.assert_not_called()

    def test_order_and_payload_survive_both_model_paths(self):
        request = 'open calculator then tell codex here: keep "a  b"'
        calls = [call("open_app", app="kilix-calculator"),
                 call("message_agent", agent="codex", text='keep "a  b"')]
        for backend in ("needle3", "lfm2"):
            model = Mock()
            model.decide.return_value = {"calls": calls}
            result = decide(request, backend=backend, grammar_first=False, provider=lambda: model)
            self.assertEqual([a.kind for a in result.admission.actions], ["launch", "tell"])
            self.assertEqual(result.admission.actions[1].args["text"], 'keep "a  b"')
            model.decide.return_value = {"calls": list(reversed(calls))}
            self.assertFalse(decide(request, backend=backend, grammar_first=False,
                                    provider=lambda: model).admission.actions)

    def test_errors_remain_errors_and_empty_output_is_distinct(self):
        model = Mock()
        for reply in ({}, None, {"calls": None}, {"calls": [], "error": "timeout"},
                      {"calls": [], "success": False}, {"calls": [], "type": "error"}):
            model.decide.return_value = reply
            with self.subTest(reply=reply), self.assertRaises(ProposalError):
                decide("make me a sandwich", backend="lfm2", provider=lambda: model)
        model.decide.return_value = {"calls": []}
        result = decide("make me a sandwich", backend="lfm2", provider=lambda: model)
        self.assertFalse(result.admission.actions)
        self.assertEqual(result.source, "lfm2")

    def test_absent_model_does_not_silently_choose_another(self):
        with self.assertRaises(ProposalError):
            decide("make me a sandwich", backend="needle3")


if __name__ == "__main__":
    unittest.main()
