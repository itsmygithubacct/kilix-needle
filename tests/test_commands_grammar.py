"""Whole-request coverage, quoting and adverse tails for the shared grammar."""
import support
import json
import unittest

import commands_contract as contract


def call(name, **arguments):
    return {'name': name, 'arguments': arguments}


class CommandGrammar(unittest.TestCase):
    def check(self, request, wanted):
        self.assertEqual(contract.exact_calls(request), wanted)
        self.assertTrue(contract.admit(request, wanted).actions)

    def test_politeness_location_and_placement_compose(self):
        for prefix in ("I'd like you to", 'I would like to', 'Could you please'):
            request = prefix + ' launch Codex with "/projects/Blue Birch" as its working directory in a fresh tab, please.'
            with self.subTest(prefix=prefix):
                self.check(request, [call('start_codex', directory='/projects/Blue Birch', place='tab')])
        for phrase, place in (('to my right', 'right'), ('to the left', 'left'),
                              ('above the current pane', 'up'), ('in a split below this pane', 'down')):
            self.check('Please start Codex '+phrase+'.', [call('start_codex', place=place)])

    def test_quoted_arguments_preserve_unicode_spaces_quotes_and_separators(self):
        payload = 'Keep  “café”; say "done", then stop. Please!'
        encoded = json.dumps(payload, ensure_ascii=False)
        request = 'Launch Codex here with initial instruction '+encoded
        self.check(request, [call('start_codex', task=payload)])
        request = 'Send Codex in /projects/birch this exact text '+encoded+'.'
        self.check(request, [call('message_agent', agent='codex', directory='/projects/birch', text=payload)])
        self.assertFalse(contract.admit(request, [call('message_agent', agent='codex',
            directory='/projects/birch', text=payload.replace('  ', ' '))]).actions)

    def test_colon_payload_keeps_its_own_quotes_and_polite_suffix(self):
        payload = '"Keep  spaces", please.'
        self.check('start codex here: '+payload, [call('start_codex', task=payload)])
        self.check('tell codex here: '+payload, [call('message_agent', agent='codex', text=payload)])

    def test_unclosed_quotes_extra_tails_and_controls_are_not_silently_dropped(self):
        for suffix in ('"unfinished', '"finished" then reboot', '"finished" if tests pass',
                       '"one" "two"', '""', '"line\\nbreak"'):
            for head in ('start codex with task ', 'send codex the message '):
                request = head + suffix
                with self.subTest(request=request):
                    calls = contract.exact_calls(request)
                    self.assertFalse(contract.admit(request, calls or []).actions)

    def test_duplicate_fields_and_conflicting_directions_refuse(self):
        for request in (
            'start codex in /a with "/b" as its working directory',
            'start codex to the right to the left',
            'start codex with task "one" with task "two"',
            'start codex with "/a" as its working directory after deleting /b',
        ):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))

    def test_wait_only_accepts_a_complete_non_launch_constraint(self):
        self.check('wait for codex to be idle; do not launch a new agent.',
                   [call('wait_for_agent', agent='codex')])
        for request in ('wait for codex; do not close its pane',
                        'wait for codex; do not start another session then reboot',
                        'do not wait for codex; start another session'):
            self.assertIsNone(contract.exact_calls(request))

    def test_ordered_lists_and_verb_sharing_do_not_change_payloads(self):
        self.check('Open notepad, launch calculator, and open file manager.', [
            call('open_app', app='kilix-notepad'), call('open_app', app='kilix-calculator'),
            call('open_app', app='kilix-file')])
        self.check('I would like to start Codex in "/a" and Codex in "/b".', [
            call('start_codex', directory='/a'), call('start_codex', directory='/b')])
        # A bare agent name is not independently an instruction to launch it.
        self.assertIsNone(contract.exact_calls('Codex in /b'))
        self.assertIsNone(contract.exact_calls('open notepad and Codex in /b'))

    def test_desired_application_and_final_politeness_require_full_match(self):
        self.check("I'd like the file manager launched.", [call('open_app', app='kilix-file')])
        self.check('Please open notepad for me!', [call('open_app', app='kilix-notepad')])
        for request in ("I wouldn't like the file manager launched.",
                        "I'd like the file manager launched if there is room.",
                        'Launch notepad now and erase it'):
            self.assertIsNone(contract.exact_calls(request))

    def test_need_a_fresh_session_preserves_agent_and_quoted_directory(self):
        for agent in ('Claude', 'Codex'):
            for adjective in ('new', 'fresh'):
                request = f'I need a {adjective} {agent} session in "/projects/Blue and Birch".'
                with self.subTest(request=request):
                    wanted = [call('start_'+agent.lower(), directory='/projects/Blue and Birch')]
                    self.check(request, wanted)
                    self.assertFalse(contract.admit(request, [call('start_'+agent.lower(),
                        directory='/projects/Blue')]).actions)

    def test_with_this_exact_text_preserves_the_entire_colon_payload(self):
        payload = 'Compare A, B; then C. Keep  “café” and say "ready", please.'
        for noun in ('text', 'message', 'instruction'):
            request = f'Please message Codex in "/projects/Blue Birch" with this exact {noun}: '+payload
            with self.subTest(noun=noun):
                self.check(request, [call('message_agent', agent='codex',
                    directory='/projects/Blue Birch', text=payload)])
                for changed in (payload.replace('  ', ' '), payload.split(';')[0]):
                    self.assertFalse(contract.admit(request, [call('message_agent', agent='codex',
                        directory='/projects/Blue Birch', text=changed)]).actions)
        for suffix in ('', ' ', '\t'):
            self.assertIsNone(contract.exact_calls('message codex here with this exact text:'+suffix))

    def test_need_an_application_opened_now_and_ordered_session_compose(self):
        self.check('I need the file manager opened now.', [call('open_app', app='kilix-file')])
        self.check('I need calculator launched!', [call('open_app', app='kilix-calculator')])
        self.check('I need a fresh Codex session here, then I need the file manager opened now.',
                   [call('start_codex'), call('open_app', app='kilix-file')])
        self.assertFalse(contract.admit('I need the file manager opened now.',
                         [call('open_app', app='kilix-notepad')]).actions)

    def test_new_request_forms_do_not_drop_conditions_negation_or_extra_actions(self):
        supported = (
            'I need a fresh Codex session in "/projects/Blue Birch"',
            'I need the file manager opened now',
            'Please message Codex in /projects/birch with this exact text: Keep  spaces',
        )
        for request in supported:
            for prefix in ('Do not act on this: ', 'How would I say ',
                           'If the tests pass, ', 'Tomorrow afternoon, ',
                           'The documentation says: '):
                with self.subTest(prefix=prefix, request=request):
                    text = prefix+request
                    self.assertIsNone(contract.exact_calls(text))
                    self.assertFalse(contract.admit(text, contract.exact_calls(request)).actions)
        # A message's colon payload is literal; tails on launch/app requests
        # remain separate request text and must be understood in full.
        for request in supported[:2]:
            for suffix in (' if the tests pass', ' tomorrow', ' and erase the logs',
                           '; then open Firefox', ' or perhaps not'):
                with self.subTest(request=request, suffix=suffix):
                    self.assertIsNone(contract.exact_calls(request+suffix))
        for request in (
            "I don't need a fresh Codex session here.",
            "I don't need the file manager opened now.",
            'I need the file manager opened now?',
            'I need a fresh Codex session here?',
            'I need a fresh Codex session in /a in /b.',
            'Please message Codex if tests pass with this exact text: ready',
            'Please message Codex tomorrow with this exact text: ready',
            'I need Firefox opened now.',
        ):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))


if __name__ == '__main__':
    unittest.main()
