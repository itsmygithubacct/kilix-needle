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

    def test_bare_wait_remains_literal_but_a_named_wait_is_ambiguous(self):
        for separator in (' and ', '; then '):
            payload = 'Store "A  B"'+separator+'wait.'
            self.check('Tell Codex: '+payload,
                       [call('message_agent', agent='codex', text=payload)])
            for tail in ('wait for Codex', 'wait until Claude finishes', 'wait five minutes'):
                self.refuse('Tell Codex: ready'+separator+tail)

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
        # One directory or placement after launches that share a verb and
        # state nothing else is every launch's (0.2.3).
        self.check('Start Claude and Codex in /projects/a', [
            call('start_claude', directory='/projects/a'), call('start_codex', directory='/projects/a')])
        self.check('Start a new Claude session and a new Codex session in /projects/a.', [
            call('start_claude', directory='/projects/a'), call('start_codex', directory='/projects/a')])
        self.check('Start Claude and Codex on the right', [
            call('start_claude', place='right'), call('start_codex', place='right')])
        for text in ('Start Claude on the left and Codex in /projects/a',
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
                for ending in ('.', ', please.', ' now.', ' for me.', ' now, please.'):
                    with self.subTest(agent=agent, verb=verb, ending=ending):
                        self.check(stem+ending, wanted)
                for ending in (' right now.', ' tomorrow.', ' tomorrow, please.'):
                    with self.subTest(agent=agent, verb=verb, ending=ending):
                        request = stem+ending
                        self.refuse(request)
                        # Even a proposal for the matching agent cannot omit the tail.
                        self.assertFalse(contract.admit(request, wanted).actions)
            self.check(f'Could you kick off the {agent} session here?', wanted)

    def test_complete_immediate_requests_keep_conditions_outside_the_action(self):
        self.check('Please launch calculator now.',
                   [call('open_app', app='kilix-calculator')])
        self.check('I need the file manager opened now.',
                   [call('open_app', app='kilix-file')])
        self.check('Open notepad now; then kick off Codex here.',
                   [call('open_app', app='kilix-notepad'), call('start_codex')])
        self.check('Please launch Codex now.', [call('start_codex')])
        self.check('I need a fresh Codex session here now.', [call('start_codex')])
        self.check('Open calculator now; then kick off Codex here now.',
                   [call('open_app', app='kilix-calculator'), call('start_codex')])
        self.refuse('Please launch Codex now if the tests pass.')
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
            ('Open calculator now; then kick off Codex here tomorrow.', [app, launch]),
            ('Kick off Codex here tomorrow; then open calculator.', [launch, app]),
            ('Open calculator; kick off Codex here tomorrow; message Codex: "now"',
             [app, launch, message]),
            ('Kick off Codex here now; then erase the workspace', [launch]),
        ):
            with self.subTest(request=request):
                self.refuse(request)
                self.assertFalse(contract.admit(request, shorter).actions)


if __name__ == '__main__':
    unittest.main()


class FrameFences(unittest.TestCase):
    """What a frame reading must never do (see the Frames section of
    commands_contract): read an unquoted payload without a marker, skip the
    suggestion rules, pick a session that is not the only one meant, or read
    past an existing spaced path. An unquoted payload after a marker is read
    under the rule tested in MarkedPayload."""

    def test_a_frame_reads_no_unquoted_message_or_task_without_a_marker(self):
        for request in ('ping codex in pewter-x run the tests', 'say hello to codex in pewter-x',
                        'Let Claude know the API key rotation is done', 'Kick off Codex with the task add unit tests'):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))

    def test_quoted_escapes_are_not_guessed(self):
        self.assertIsNone(contract.exact_calls('Send Claude in /w/t the message: "Use path C:\\Temp\\out.txt"'))
        self.assertIsNone(contract.exact_calls('say "' + 'x' * 1025 + '" to codex'))
        self.assertEqual(contract.exact_calls('say "' + 'x' * 1024 + '" to codex'),
                         [call('message_agent', agent='codex', text='x' * 1024)])

    def test_every_frame_reading_passes_the_suggestion_rules(self):
        import commands_suggest
        request = 'launch codex in pewter-x please'
        self.assertEqual(contract.exact_calls(request), [call('start_codex', directory='pewter-x')])
        original = commands_suggest.refusal
        seen = []
        def refusing(text, calls):
            seen.append(text)
            return 'conditional'
        commands_suggest.refusal = refusing
        try:
            self.assertIsNone(contract.exact_calls(request))
            self.assertEqual(seen, [request])
            # A reading the fixed shapes make is not held to them.
            self.assertEqual(contract.exact_calls('start codex in /srv/x'), [call('start_codex', directory='/srv/x')])
        finally:
            commands_suggest.refusal = original

    def test_a_wait_or_message_with_two_sessions_in_play_refuses(self):
        for request in ('Start claude here and claude in ../brisket-x, then forward "pause deploys" to the claude',
                        'start codex in pewter-x, then tell codex "go"',
                        'wait for claude in pewter-x and then wait for claude'):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))
        self.assertEqual(contract.exact_calls('wait for claude in pewter-x, then tell codex "go"'),
                         [call('wait_for_agent', agent='claude', directory='pewter-x'),
                          call('message_agent', agent='codex', text='go')])

    def test_an_existing_spaced_path_is_left_to_the_fixed_shapes(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, 'proj'))
            request = 'Begin a codex session in ' + root + '/proj new tab'
            self.assertEqual(contract.exact_calls(request),
                             [call('start_codex', directory=root + '/proj', place='tab')])
            os.mkdir(os.path.join(root, 'proj new'))
            self.assertIsNone(contract.exact_calls(request))

    def test_a_question_mark_needs_a_polite_opener(self):
        self.assertEqual(contract.exact_calls('Could you launch codex in pewter-x?'),
                         [call('start_codex', directory='pewter-x')])
        for request in ('launch codex in pewter-x?', 'Can I launch codex in pewter-x?', 'launch codex in pewter-x?!'):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))

    def test_a_frame_refuses_on_its_own_before_the_suggestion_rules(self):
        # Each of these is refused by the frame itself, not only by the rules applied after it.
        for clause in ('Open the notepad or the calculator', 'start codex in pewter-x in birch-y',
                       'Kick off codex with model gpt-6, model gpt-6.1-sol', 'start codex in a right split in a left split',
                       'say "x" "y" to codex', 'would you mind start claude', 'begin a codex session in ~/fig-x)'):
            with self.subTest(clause=clause):
                self.assertIsNone(contract._frame_calls(clause))
                self.assertIsNone(contract.exact_calls(clause))
        self.assertIsNone(contract._referred('start codex there', [call('wait_for_agent', agent='claude')]))
        self.assertEqual(contract._referred('start codex there', [call('wait_for_agent', agent='claude', directory='a-b')]),
                         'start codex in "a-b"')


class MarkedPayloadFences(unittest.TestCase):
    """Where an unquoted payload after a marker is not taken: the request has
    a second instruction for Ask, a condition or time outside the payload, or a
    payload that is no message at all (docs/ask-phrasing.md, "Unquoted payloads")."""

    def refuse(self, *texts):
        for text in texts:
            with self.subTest(text=text):
                self.assertIsNone(contract.exact_calls(text))

    def test_a_later_clause_for_ask_is_a_second_reading(self):
        self.refuse('Tell claude in /srv/oast-galliard: tidy the notes, then tell codex in /srv/fen-x: rebase',
                    'Tell codex in /srv/oast-galliard: fix it, then wait for it to finish',
                    'Tell codex in /srv/oast-galliard: fix it; then open the calculator',
                    'Tell codex in /srv/oast-galliard: fix it. Then start claude in /srv/fen-x.',
                    'Tell codex in /srv/oast-galliard: ship it, then close the session',
                    'Start codex in /srv/oast-galliard and tell it: Read NOTES.md.')

    def test_the_head_keeps_its_conditions_times_and_negations(self):
        self.refuse('If the build is green, tell codex in /srv/oast-galliard: ship it',
                    'Tell codex in /srv/oast-galliard if it is idle: ship it',
                    'Do not tell codex in /srv/oast-galliard: ship it',
                    'Tomorrow, tell codex in /srv/oast-galliard: ship it',
                    'I told codex in /srv/oast-galliard: ship it',
                    'Tell codex or claude in /srv/oast-galliard: ship it',
                    'Tell grok in /srv/oast-galliard: ship it',
                    'Tell codex in pane 3: ship it',
                    'Start codex in /srv/oast-galliard with reasoning effort high. Task: Read NOTES.md.',
                    "Tell codex in 'oast galliard': ship it")     # only double quotes in the head

    def test_payloads_that_are_no_message(self):
        self.refuse('Start codex in /srv/oast-galliard with these instructions: /compact',
                    'Start codex in /srv/oast-galliard. Task: -h',
                    'Let codex in /srv/oast-galliard know: !make clean',
                    'Let codex in /srv/oast-galliard know: quit',
                    'Start codex in /srv/oast-galliard with these instructions: Exit!',
                    'Let codex in /srv/oast-galliard know: ' + 'x' * 1025,
                    'Tell codex in /srv/oast-galliard:',
                    'Start codex in /srv/oast-galliard. Task:')
        self.assertEqual(contract.exact_calls('Let codex in /srv/oast-galliard know: quit the old approach.'),
                         [call('message_agent', agent='codex', text='quit the old approach.', directory='/srv/oast-galliard')])

    def test_a_launch_task_that_is_a_question_is_asked_of_ask(self):
        self.refuse('Start codex in ../oast-galliard: would that pick the parent folder?')
        self.assertEqual(contract.exact_calls('Let codex in /srv/oast-galliard know: is the build green?'),
                         [call('message_agent', agent='codex', text='is the build green?', directory='/srv/oast-galliard')])

    def test_no_shape_sends_a_client_command_or_an_exit(self):
        # The fixed shapes too: what a session would run as a command of its
        # own, or that ends it, is not a message (agents.py's payload rule).
        self.refuse('Tell codex in /srv/oast-galliard: /compact', 'Tell claude in /srv/oast-galliard: !make clean',
                    'Tell codex here: -h', 'Message Codex: "/clear"', 'Tell codex in /srv/oast-galliard: exit',
                    'Start codex in /srv/oast-galliard with task "/resume"',
                    'Start codex in /srv/oast-galliard/x: is that the same folder the last session used?')
        self.assertEqual(contract.exact_calls('Start codex in /srv/oast-galliard with task "why does the parser fail?"'),
                         [call('start_codex', directory='/srv/oast-galliard', task='why does the parser fail?')])
        self.assertEqual(contract.exact_calls('Message Codex: "exit early when the cache is warm"'),
                         [call('message_agent', agent='codex', text='exit early when the cache is warm')])

    def test_every_shape_keeps_a_later_instruction_for_ask_out_of_a_payload(self):
        # The fixed shapes read "tell X: ..." themselves; the same clause rule
        # holds there ("Afterwards spin up ..." is not in their own list).
        self.refuse('Tell codex in /srv/oast-galliard: fix it. Afterwards spin up claude in /srv/fen-x.',
                    'Tell codex in /srv/oast-galliard: fix it, and pop open the calculator.',
                    'Tell codex in /srv/oast-galliard: fix it. Just wait for it to finish.',
                    'Wait for codex in /srv/oast-galliard, then tell it: fix it. Afterwards spin up claude in /srv/fen-x.',
                    'Start codex in /srv/oast-galliard, nothing else; open the calculator',
                    'Start codex in /srv/oast-galliard: fix it? "x"',
                    'Start codex in /srv/oast-galliard. with model opus. Task: fix it')
        for text, payload in (('Tell codex in /srv/oast-galliard: ', 'change nothing else in the file; reply OK.'),
                              ('Tell codex in /srv/oast-galliard: ', 'why does it fail?')):
            self.assertEqual(contract.exact_calls(text + payload),
                             [call('message_agent', agent='codex', text=payload, directory='/srv/oast-galliard')])
        self.assertEqual(contract.exact_calls('Start codex in /srv/oast-galliard. Task: fix the "why?" banner'),
                         [call('start_codex', directory='/srv/oast-galliard', task='fix the "why?" banner')])
        self.assertEqual(contract.exact_calls('Tell Codex: "ready, then start claude"'),
                         [call('message_agent', agent='codex', text='ready, then start claude')])
