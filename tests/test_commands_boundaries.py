"""Literal boundaries and complete action lists in the shared Ask contract."""
import support
import json
import unittest
from unittest.mock import Mock

import commands_contract as contract
from commands_policy import decide


def call(name, **args):
    return {'name': name, 'arguments': args}


class CommandBoundaries(unittest.TestCase):
    def check(self, request, calls):
        self.assertEqual(contract.exact_calls(request), calls)
        self.assertTrue(contract.admit(request, calls).actions)
        for backend in ('grammar', 'lfm2', 'needle3'):
            provider = Mock(side_effect=AssertionError('provider must not load'))
            result = decide(request, backend=backend, provider=provider)
            self.assertEqual(result.calls, calls)
            self.assertTrue(result.admission.actions)
            provider.assert_not_called()

    def refuse(self, request):
        self.assertIsNone(contract.exact_calls(request))
        self.assertFalse(contract.admit(request, [call('start_codex')]).actions)

    def test_sentence_marks_are_outside_unquoted_paths(self):
        for agent in ('Claude', 'Codex'):
            for path in ('/projects/birch', '/projects/birch.v3', '~/projects/birch'):
                for mark in ('.', '!'):
                    with self.subTest(agent=agent, path=path, mark=mark):
                        self.check(f'Start {agent} in {path}{mark}',
                                   [call('start_'+agent.lower(), directory=path)])
        self.check('Wait for Codex in /projects/birch.',
                   [call('wait_for_agent', agent='codex', directory='/projects/birch')])
        self.check('Open /documents/guide.v3.pdf.',
                   [call('open_app', app='kilix-pdf', file_path='/documents/guide.v3.pdf')])

    def test_quoted_paths_keep_literal_punctuation(self):
        for path in ('/projects/birch.', '/projects/birch!', '/projects/Blue and Birch',
                     '/projects/a,b; c', '/projects/../birch'):
            self.check('Launch Codex in '+json.dumps(path)+'.',
                       [call('start_codex', directory=path)])
        for path in ('/projects/.', '/projects/..'):
            self.check('start codex in '+path, [call('start_codex', directory=path)])
        self.check('Open "/documents/guide.v3.pdf".',
                   [call('open_app', app='kilix-pdf', file_path='/documents/guide.v3.pdf')])

    def test_complete_quoted_colon_payloads_keep_exact_bytes(self):
        payload = '  Keep  café; then say "done". Use C:\\Temp\\out.txt!  '
        for prefix, name, extra, field in (
            ('Start Codex with initial task: ', 'start_codex', {}, 'task'),
            ('Launch Codex and give it this task: ', 'start_codex', {}, 'task'),
            ('Message Codex: ', 'message_agent', {'agent': 'codex'}, 'text'),
            ('Ask Codex to ', 'message_agent', {'agent': 'codex'}, 'text'),
        ):
            for mark in ('', '.', '!'):
                with self.subTest(prefix=prefix, mark=mark):
                    self.check(prefix+json.dumps(payload, ensure_ascii=False)+mark,
                               [call(name, **extra, **{field: payload})])

    def test_quoted_messages_and_tasks_compose_in_order(self):
        for separator in (', then ', '; then ', ' and ', ' then '):
            request = 'Tell Claude in /projects/a: "first  payload."'+separator+'message Codex: "second!"'
            wanted = [call('message_agent', agent='claude', directory='/projects/a', text='first  payload.'),
                      call('message_agent', agent='codex', text='second!')]
            self.check(request, wanted)
            self.assertFalse(contract.admit(request, list(reversed(wanted))).actions)
        self.check('Start Codex with task "Check the parser."; open calculator; '
                   'message Claude the text "Keep  spaces"; wait for Codex.', [
                       call('start_codex', task='Check the parser.'),
                       call('open_app', app='kilix-calculator'),
                       call('message_agent', agent='claude', text='Keep  spaces'),
                       call('wait_for_agent', agent='codex')])

    def test_action_words_inside_quotes_remain_payload(self):
        payload = 'Open calculator; then start Claude and message Codex: "done"'
        self.check('Send Codex the exact text '+json.dumps(payload),
                   [call('message_agent', agent='codex', text=payload)])
        self.check('Send Codex the text "a  b   c" exactly.',
                   [call('message_agent', agent='codex', text='a  b   c')])

    def test_ambiguous_unquoted_action_tails_refuse(self):
        for request in (
            'Tell Codex: ready; then open calculator',
            'Tell Codex: ready and start Claude',
            'Start Codex to inspect the code, then message Claude: done',
            'Message Codex: ready; then delete the logs',
            'Tell Codex: "ready"; then erase the workspace',
            'Start Codex with task "inspect" and do not open calculator',
            'Message Codex: "ready" if the build succeeds',
            'Tell Codex: "ready" tomorrow',
            'Start Codex with task "one"; open calculator; open notepad; '
            'wait for Claude; message Codex: "five"',
        ):
            with self.subTest(request=request):self.refuse(request)
        self.check('Tell Codex: compare A and B; then C.',
                   [call('message_agent', agent='codex', text='compare A and B; then C.')])

    def test_kick_off_is_bounded_and_preserves_refusals(self):
        self.check('Kick off a Codex session.', [call('start_codex')])
        self.check('Could you kick off a fresh Claude session in "/projects/a."?',
                   [call('start_claude', directory='/projects/a.')])
        for text in ('Kick off a Codex session', 'Start Codex in /projects/a'):
            for prefix in ('Do not ', 'In ten minutes, ', 'If tests pass, ', 'How do I '):
                with self.subTest(prefix=prefix):self.refuse(prefix+text)
            for suffix in (' if tests pass', ' tomorrow', ' unless it is busy',
                           '; then open Firefox', ' and delete the logs'):
                with self.subTest(suffix=suffix):self.refuse(text+suffix)
        for text in ('Kick off a session.', 'Kick off Gemini.', 'Message Codex:',
                     'Send the agent the text "ready"', 'Start Codex with task ""'):
            self.refuse(text)

    def test_shared_verb_cannot_guess_argument_scope(self):
        self.check('Start Codex in /projects/a and Claude in /projects/b.', [
            call('start_codex', directory='/projects/a'), call('start_claude', directory='/projects/b')])
        self.check('Start Claude and Codex.', [call('start_claude'), call('start_codex')])
        self.check('Start Claude here and Codex in /projects/a.', [
            call('start_claude'), call('start_codex', directory='/projects/a')])
        for text in ('Start Claude and Codex in /projects/a',
                     'Start a new Claude session and a new Codex session in /projects/a.',
                     'Start Claude on the left and Codex in /projects/a',
                     'Start Claude and Codex on the right',
                     'Start Claude and Codex with task "inspect"'):
            self.refuse(text)

    def test_integration_model_scope_survives_new_launch_wording_and_payloads(self):
        self.check('Start Claude with task "Keep  punctuation."; then kick off Codex with model gpt-6.', [
            call('start_claude', task='Keep  punctuation.'), call('start_codex', model='gpt-6')])
        self.check('Kick off Claude here then launch Codex with model gpt-6 in /projects/a.', [
            call('start_claude'), call('start_codex', model='gpt-6', directory='/projects/a')])
        for request in (
            'Kick off Claude here then launch Codex in /projects/a and a new Codex session model gpt-6',
            'Start Claude with task "Keep  spaces" and Codex model gpt-6',
            'Kick off Codex model in /projects/a gpt-6',
        ):
            self.refuse(request)

    def test_launch_synonyms_keep_the_same_bounded_suffixes(self):
        for agent in ('Claude', 'Codex'):
            wanted = [call('start_'+agent.lower())]
            for verb in ('start', 'launch', 'kick off'):
                stem = f'Please {verb} the {agent} session here'
                for ending in ('.', ', please.'):
                    with self.subTest(agent=agent, verb=verb, ending=ending):
                        self.check(stem+ending, wanted)
                for ending in (' now.', ' right now.', ' for me.', ' tomorrow.',
                               ' now, please.'):
                    with self.subTest(agent=agent, verb=verb, ending=ending):
                        request = stem+ending
                        self.refuse(request)
                        # Even a proposal for the matching agent cannot omit the tail.
                        self.assertFalse(contract.admit(request, wanted).actions)
            self.check(f'Could you kick off the {agent} session here?', wanted)

    def test_application_now_does_not_extend_agent_launch_wording(self):
        self.check('Please launch calculator now.',
                   [call('open_app', app='kilix-calculator')])
        self.check('I need the file manager opened now.',
                   [call('open_app', app='kilix-file')])
        self.check('Open notepad now; then kick off Codex here.',
                   [call('open_app', app='kilix-notepad'), call('start_codex')])
        self.refuse('Please launch Codex now.')
        self.refuse('I need a fresh Codex session here now.')
        self.refuse('Please launch calculator now if the tests pass.')

    def test_now_inside_literal_arguments_is_preserved(self):
        payload = '  Report now; then say "done". Keep C:\\Temp\\now.txt!  '
        quoted = json.dumps(payload)
        self.check('Kick off Codex with task '+quoted+'; open calculator now.',
                   [call('start_codex', task=payload),
                    call('open_app', app='kilix-calculator')])
        self.check('Message Codex: '+quoted,
                   [call('message_agent', agent='codex', text=payload)])
        self.check('Kick off Codex in "/projects/now".',
                   [call('start_codex', directory='/projects/now')])
        for request, wanted in (
            ('Kick off Codex with task '+quoted+' now.', [call('start_codex', task=payload)]),
            ('Message Codex: '+quoted+' now.', [call('message_agent', agent='codex', text=payload)]),
        ):
            self.refuse(request)
            self.assertFalse(contract.admit(request, wanted).actions)

    def test_unconsumed_launch_tail_refuses_the_complete_batch(self):
        app = call('open_app', app='kilix-calculator')
        launch = call('start_codex')
        message = call('message_agent', agent='codex', text='now')
        for request, shorter in (
            ('Open calculator now; then kick off Codex here now.', [app, launch]),
            ('Kick off Codex here now; then open calculator.', [launch, app]),
            ('Open calculator; kick off Codex here now; message Codex: "now"',
             [app, launch, message]),
            ('Kick off Codex here now; then erase the workspace', [launch]),
        ):
            with self.subTest(request=request):
                self.refuse(request)
                self.assertFalse(contract.admit(request, shorter).actions)


if __name__ == '__main__':
    unittest.main()
