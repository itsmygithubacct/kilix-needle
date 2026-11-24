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

    def test_worker_one_and_model_forms_read_as_one_launch(self):
        launch = [call('start_codex', directory='/w', model='gpt-6.1-sol')]
        for request in ('start one codex worker in /w using model gpt-6.1-sol',
                        'launch a Codex worker using gpt-6.1-sol in /w',
                        'start codex in /w model gpt-6.1-sol', 'start codex in /w, model gpt-6.1-sol',
                        'start codex in /w model=gpt-6.1-sol',
                        'start codex --model gpt-6.1-sol in /w', 'start codex in /w --model=gpt-6.1-sol',
                        'start codex gpt-6.1-sol in /w'):
            with self.subTest(request=request):
                self.check(request, launch)
        self.check('start claude opus to the right', [call('start_claude', model='opus', place='right')])
        self.check('start codex gpt-6', [call('start_codex', model='gpt-6')])

    def test_model_forms_refuse_missing_repeated_or_foreign_models(self):
        for request in ('start codex in /w model', 'start codex --model gpt-6 --model gpt-6.1-sol',
                        'start codex with model gpt-6 using gpt-6.1-sol', 'start codex gpt-6.1-sol gpt-6 in /w',
                        'start codex gpt-6.1-solx in /w', 'start claude gpt-6 in /w',
                        'start codex modelling in /w', 'start codex in /w; model gpt-6.1-sol',
                        'start two codex workers', 'start one one codex', 'start a one codex',
                        'start codex worker pool in /w',
                        # A colon opens an initial task, so this model is left over.
                        'start codex in /w model: gpt-6.1-sol',
                        'Open exactly one Codex worker in /w using gpt-6.1-sol.'):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))
        # A stated model outside the contract is read, then refused by admission.
        calls = contract.exact_calls('start codex in /w model gpt-5')
        self.assertEqual(calls, [call('start_codex', directory='/w', model='gpt-5')])
        self.assertFalse(contract.admit('start codex in /w model gpt-5', calls).actions)

    def test_one_model_for_several_launches_refuses(self):
        for request in ('start claude and claude, model opus',
                        'start codex in /a and codex in /b, model gpt-6.1-sol',
                        'start claude in /a and claude in /b --model opus',
                        'start codex in /a and codex in /b using gpt-6.1-sol',
                        # These read on their own without a verb, so they share the left one.
                        'start claude in /a and a new claude session in /b, model opus',
                        'start codex and a new codex session, model gpt-6',
                        'start claude and a new claude agent --model opus',
                        'start codex in /a and a new codex session in /b using gpt-6',
                        # An action in between still leaves the first launch without a model.
                        'start codex in /a and open the calculator and a new codex session, model gpt-6',
                        'start codex in /a and wait for codex in /b and a new codex session in /c, model gpt-6',
                        'start codex in /a and start codex gpt-6 and a new codex session, model gpt-6.1-sol',
                        'start codex in /a; open the calculator and a new codex session using gpt-6',
                        'start codex in /a, open the calculator and a new codex session in /b using gpt-6',
                        'start codex in /a; open the calculator and a new codex session, model gpt-6 and open notepad'):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))
        self.check('start codex in /a using gpt-6 and codex in /b using gpt-6.1-sol', [
            call('start_codex', directory='/a', model='gpt-6'),
            call('start_codex', directory='/b', model='gpt-6.1-sol')])
        self.check('start codex in /a and claude in /b', [
            call('start_codex', directory='/a'), call('start_claude', directory='/b')])

    def test_a_model_inside_its_own_launch_still_reads(self):
        self.check('start codex using gpt-6 and claude', [
            call('start_codex', model='gpt-6'), call('start_claude')])
        self.check('start codex in /a using gpt-6.1-sol and claude in /b', [
            call('start_codex', directory='/a', model='gpt-6.1-sol'), call('start_claude', directory='/b')])
        self.check('start codex with model gpt-6; start claude in /b', [
            call('start_codex', model='gpt-6'), call('start_claude', directory='/b')])
        self.check('start codex with model gpt-6, then start claude', [
            call('start_codex', model='gpt-6'), call('start_claude')])
        self.check('open the calculator and start codex using gpt-6 and start claude', [
            call('open_app', app='kilix-calculator'), call('start_codex', model='gpt-6'), call('start_claude')])
        for separator in ('; ', ' and ', ' and please '):
            self.check('start codex in /a'+separator+'start codex in /b with model gpt-6', [
                call('start_codex', directory='/a'), call('start_codex', directory='/b', model='gpt-6')])
        # Apps, waits and launches with their own model are not launches the model could cover.
        self.check('open the calculator and a new codex session, model gpt-6', [
            call('open_app', app='kilix-calculator'), call('start_codex', model='gpt-6')])
        self.check('wait for codex in /b and a new codex session, model gpt-6', [
            call('wait_for_agent', agent='codex', directory='/b'), call('start_codex', model='gpt-6')])
        self.check('start codex using gpt-6 and open the calculator and a new codex session, model gpt-6.1-sol', [
            call('start_codex', model='gpt-6'), call('open_app', app='kilix-calculator'),
            call('start_codex', model='gpt-6.1-sol')])

    def test_one_directory_or_placement_for_launches_that_share_a_verb(self):
        # Gap 2 (0.2.3): stated once after launches that share one verb and state
        # nothing else, a directory or placement is each launch's.
        both = lambda **args: [call('start_codex', **args), call('start_claude', **args)]
        self.check('start codex and claude in /w', both(directory='/w'))
        self.check('start codex and claude to the right', both(place='right'))
        self.check('start codex and claude in /w to the right', both(directory='/w', place='right'))
        self.check('start codex and a new claude session in /w', both(directory='/w'))
        self.check('start codex and claude and codex in /w',
                   both(directory='/w') + [call('start_codex', directory='/w')])
        self.check('start codex and claude in /w and open the calculator',
                   both(directory='/w') + [call('open_app', app='kilix-calculator')])
        self.check('open the calculator and start codex and claude in /w',
                   [call('open_app', app='kilix-calculator')] + both(directory='/w'))
        # A fronted directory scopes the launches that share its verb.
        self.check('in /w, start codex and claude', both(directory='/w'))
        self.check('in /w, start codex and open the calculator', [
            call('start_codex', directory='/w'), call('open_app', app='kilix-calculator')])
        self.check('in /w, start codex; start claude in /v', [
            call('start_codex', directory='/w'), call('start_claude', directory='/v')])
        # Only launches that share the verb are compared; a later action with
        # its own verb keeps its own reading.
        self.check('start codex in /a and claude in /b; open the calculator', [
            call('start_codex', directory='/a'), call('start_claude', directory='/b'),
            call('open_app', app='kilix-calculator')])
        self.check('start codex and claude; start codex in /c', both() + [call('start_codex', directory='/c')])
        self.check('start codex in /a and claude in /b; start codex using gpt-6', [
            call('start_codex', directory='/a'), call('start_claude', directory='/b'),
            call('start_codex', model='gpt-6')])
        # Unchanged per-launch readings.
        self.check('start codex here and claude in /w', [call('start_codex'), call('start_claude', directory='/w')])
        self.check('start codex in /a and claude', [call('start_codex', directory='/a'), call('start_claude')])
        for request in (
                # A launch states its own field beside one that does not.
                'start codex in /a and claude to the right',
                'start codex to the right and claude in /w',
                'start codex and claude in /w and codex',
                'start codex and claude in /w and codex in /v',
                # A sequence, a singular "its", a task or a model are not shared.
                'start codex and then claude in /w',
                'start codex and claude with /w as its working directory',
                'start codex and claude with task "x"',
                'start codex and claude gpt-6',
                # A fronted directory, then an agent action that states none.
                'in /w, start codex and then claude',
                'in /w, start codex and then start claude',
                'in /w, start codex; start claude',
                'in /w, start codex and claude in /v',
                'in /w, start codex and claude to the right',
                'in /w, start codex and wait for codex',
                'in /w, start codex; tell claude: hi',
                # A member's own "here" is never replaced by the fronted directory.
                'in /w, start codex and claude here',
                'in /w, start codex and claude in the current directory',
                'in ~/w, start claude and codex in this folder',
                'in /w, start codex and a new claude session here',
                # After a shared directory, a wait or message that states none
                # may be meant for an agent just started there.
                'start codex and claude in /w and wait for codex',
                'start codex and claude in /w, then tell codex to fix the tests',
                'start codex and claude in /w; wait for claude',
                'start codex and claude in /w; send codex: hi',
                'start codex and claude in /w and open the calculator and wait for codex',
                'open the calculator and start codex and claude in /w and wait for codex',
                'start codex and claude and codex in /w and wait for codex'):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))
        # A wait that states its directory, or a shared placement only, still reads.
        self.check('start codex and claude in /w and wait for codex in /w',
                   both(directory='/w') + [call('wait_for_agent', agent='codex', directory='/w')])
        self.check('start codex and claude to the right and wait for codex',
                   both(place='right') + [call('wait_for_agent', agent='codex')])

    def test_a_model_before_a_verb_sharing_launchs_own_field_is_its_own(self):
        # Gap 3 (0.2.3): a model followed by the launch's own directory or
        # placement, of a kind the left launch also states, is that launch's.
        self.check('start codex in /a and codex using gpt-6 in /b', [
            call('start_codex', directory='/a'), call('start_codex', directory='/b', model='gpt-6')])
        self.check('start codex in /a and codex gpt-6 in /b', [
            call('start_codex', directory='/a'), call('start_codex', directory='/b', model='gpt-6')])
        self.check('start codex to the right and codex using gpt-6 to the left', [
            call('start_codex', place='right'), call('start_codex', place='left', model='gpt-6')])
        self.check('start codex in /a and a new codex session using gpt-6 in /b', [
            call('start_codex', directory='/a'), call('start_codex', directory='/b', model='gpt-6')])
        self.check('start codex in /a and codex using gpt-6 in /b and claude in /c', [
            call('start_codex', directory='/a'), call('start_codex', directory='/b', model='gpt-6'),
            call('start_claude', directory='/c')])
        for request in (
                # The left launch states no directory, so "in /a" may be shared.
                'start codex and codex using gpt-6 in /a',
                # The placement is of a kind the left launch does not state.
                'start codex in /a and codex with model gpt-6 to the right',
                # The model trails its launch.
                'start codex in /a and codex in /b using gpt-6',
                'start codex in /a and then a new codex session in /b using gpt-6',
                # "in /c" after a launch with only a model may be shared.
                'start codex in /a and codex using gpt-6 and claude in /c'):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))

    def test_a_verbless_launch_in_a_clause_of_its_own_keeps_its_model(self):
        # Gap 4 (0.2.3): after ";" or "then", or opening with "please" or
        # "I need", a launch is a request of its own, so its model is its own.
        second = [call('start_codex', directory='/a'), call('start_codex', directory='/b', model='gpt-6')]
        for request in ('start codex in /a; a new codex session in /b using gpt-6',
                        'start codex in /a then a new codex session in /b using gpt-6',
                        'start codex in /a, then a new codex session in /b using gpt-6',
                        'start codex in /a and please a new codex session in /b using gpt-6',
                        'start codex in /a and I need a new codex in /b using gpt-6',
                        'start codex in /a; I need a new codex in /b using gpt-6'):
            with self.subTest(request=request):
                self.check(request, second)
        self.check('start codex in /a and open the calculator; a new codex session, model gpt-6', [
            call('start_codex', directory='/a'), call('open_app', app='kilix-calculator'),
            call('start_codex', model='gpt-6')])
        # After "and" (or "and then") a verbless launch still shares the verb.
        for request in ('start codex in /a and a new codex session in /b using gpt-6',
                        'start codex in /a and then a new codex session in /b using gpt-6',
                        'start codex in /a; open the calculator and a new codex session using gpt-6'):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))

    def test_a_comma_model_after_a_list_of_launches_refuses(self):
        # Gap 5 (0.2.3): ", model X" after a comma or "and" list of complete
        # launches may be meant for each of them when an earlier one states none.
        for request in ('start codex in /a, start codex in /b, model gpt-6',
                        'start codex in /a and start codex in /b, model gpt-6',
                        # "and then" joins a list here, as it does for a shared verb.
                        'start codex in /a and then start codex in /b, model gpt-6',
                        'start codex in /a, start codex in /b, --model gpt-6',
                        'start codex in /a, start codex in /b, using gpt-6',
                        'start codex in /a, open the calculator, start codex in /b, model gpt-6',
                        'start codex in /a, start codex in /b, model gpt-6, then open the calculator'):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))
        two = [call('start_codex', directory='/a'), call('start_codex', directory='/b', model='gpt-6')]
        # Without the comma, after ";" or "then", the model is the last launch's.
        for request in ('start codex in /a, start codex in /b with model gpt-6',
                        'start codex in /a; start codex in /b, model gpt-6',
                        'start codex in /a then start codex in /b, model gpt-6'):
            with self.subTest(request=request):
                self.check(request, two)
        # No earlier launch in the list lacks a model.
        self.check('start codex gpt-6.1-sol in /a, start codex in /b, model gpt-6', [
            call('start_codex', directory='/a', model='gpt-6.1-sol'), two[1]])
        self.check('open the calculator, start codex in /b, model gpt-6', [
            call('open_app', app='kilix-calculator'), two[1]])
        self.check('start codex in /a; open the calculator, start codex in /b, model gpt-6', [
            two[0], call('open_app', app='kilix-calculator'), two[1]])
        self.check('start codex in /b, model gpt-6', [two[1]])
        # A comma-set model followed by the launch's own field is inside it,
        # as at base: the comma does not set off a trailing model.
        self.check('start codex in /a, start codex, using gpt-6 in /b', two)

    def test_a_model_is_not_read_across_a_removed_location_or_placement(self):
        for request in ('start codex model in /w gpt-6', 'start codex --model on the right gpt-6',
                        'start codex model here gpt-6', 'start codex using in /w gpt-6',
                        'start codex in /srv/model gpt-6',
                        'start codex model in "/model   gpt-6" gpt-6',
                        'start codex --model in "/a --model   gpt-6.1-sol" gpt-6.1-sol',
                        'start codex model with "/model   gpt-6" as working directory gpt-6',
                        'start codex using in "/w using   gpt-6" gpt-6'):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))
        self.check('start codex in a split in /w to the right', [
            call('start_codex', directory='/w', place='right')])

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

    def test_fronted_location_scopes_one_launch_and_preserves_its_literals(self):
        payload = 'Keep  café, [], and "quotes".'
        request = 'In "/projects/Blue, Birch.", start Codex with task '+json.dumps(payload)+'.'
        wanted = [call('start_codex', directory='/projects/Blue, Birch.', task=payload)]
        self.check(request, wanted)
        self.assertFalse(contract.admit(request, [call('start_codex',
            directory='/projects/Blue, Birch.', task=payload[:-1])]).actions)
        self.check('In "/a", start Codex; then start Claude in /b.', [
            call('start_codex', directory='/a'), call('start_claude', directory='/b')])
        for text in ('In "/a", start Codex in /b.', 'In "/a", start Codex here.',
                     'In "/a", Codex.', 'In /Blue Birch, start Codex.',
                     'In "/a", start Codex if tests pass.', 'In "/a", do not start Codex.'):
            with self.subTest(text=text):self.assertIsNone(contract.exact_calls(text))

    def test_complete_turn_and_current_session_app_wording(self):
        self.check('Wait until Codex in "/projects/Blue Birch" completes its turn.',
                   [call('wait_for_agent', agent='codex', directory='/projects/Blue Birch')])
        for text in ('Open the calculator in this session.',
                     'Launch the calculator for this session.', 'I want the calculator opened.',
                     'I want the calculator opened for this session.'):
            with self.subTest(text=text):self.check(text, [call('open_app', app='kilix-calculator')])
        for text in ('Open calculator in another session.', 'Open calculator for the Codex session.',
                     'Wait until Codex completes its turn tomorrow.',
                     'I want the calculator opened if tests pass.', "I don't want calculator opened."):
            with self.subTest(text=text):self.assertIsNone(contract.exact_calls(text))

    def test_pdf_punctuation_outside_quotes_does_not_change_the_path(self):
        path = '/projects/Blue and Birch/notes.v2.pdf'
        wanted = [call('open_app', app='kilix-pdf', file_path=path)]
        for form in ('Use the PDF viewer to open {}', 'Open {}', 'Launch the PDF viewer with {}',
                     'Get the PDF viewer to display {}', 'Show {} in the PDF viewer'):
            for mark in ('.', '!'):
                text = form.format(json.dumps(path))+mark
                with self.subTest(text=text):self.check(text, wanted)
        for text in ('Use the PDF viewer to open "'+path+'.".',
                     'Use the PDF viewer to open "'+path+'" if tests pass.',
                     'Use the PDF viewer to open "'+path+'" and erase the logs.'):
            with self.subTest(text=text):self.assertIsNone(contract.exact_calls(text))

    def test_new_coverage_keeps_request_boundaries_and_literal_checks(self):
        forms = ('In "/a", start Codex', 'Wait until Codex completes its turn',
                 'Open calculator in this session', 'I want calculator opened',
                 'Use the PDF viewer to open "/a/report.pdf"')
        for form in forms:
            wanted = contract.exact_calls(form)
            self.assertTrue(wanted)
            for text in [prefix+form for prefix in ('Do not ', 'If tests pass, ', 'The guide says: ')]+[
                    form+tail for tail in (' if tests pass', ' tomorrow', '; reboot', ' or perhaps not')]:
                with self.subTest(text=text):self.assertFalse(contract.admit(text, wanted).actions)
        message = 'Tell Codex here: Preserve  two spaces.'
        for payload in ('Preserve two spaces.', 'Preserve  two spaces'):
            self.assertFalse(contract.admit(message, [call('message_agent', agent='codex', text=payload)]).actions)

    def test_idle_launch_and_claude_code_pane_read_without_fitting_a_task(self):
        self.check('start codex in /tmp/tmp.T9u08L7Bn5 with no task; leave the session open',
                   [call('start_codex', directory='/tmp/tmp.T9u08L7Bn5')])
        self.check('start codex in /tmp/w1 with no task or initial prompt',
                   [call('start_codex', directory='/tmp/w1')])
        self.check('start codex in /tmp/w1 with no task or prompt',
                   [call('start_codex', directory='/tmp/w1')])
        self.check('launch a claude code kilix pane on the right',
                   [call('start_claude', place='right')])
        self.check('start claude code session on the right',
                   [call('start_claude', place='right')])
        self.check('start codex in /tmp/w1 with no task; open calculator',
                   [call('start_codex', directory='/tmp/w1'),
                    call('open_app', app='kilix-calculator')])
        # "to <words>" remains the task. Idle wording inside that payload stays there.
        self.check('start codex in /tmp/w1 to fix the build',
                   [call('start_codex', directory='/tmp/w1', task='fix the build')])
        self.check('start codex in /tmp/w1 to fix the build. Do not send a prompt',
                   [call('start_codex', directory='/tmp/w1',
                         task='fix the build. Do not send a prompt')])
        self.check('start codex in /tmp/w1 with task "fix with no task"',
                   [call('start_codex', directory='/tmp/w1', task='fix with no task')])
        for request in (
            "don't start codex in /tmp/w1 with no task",
            'start codex in /tmp/w1 with task "fix the build" with no task',
            'start codex in /tmp/w1 with no task with task "fix the build"',
            'start codex in /tmp/w1 with no task; leave the session open and delete it',
            'start claude code the tests',
            'start codex code in /tmp/w1',
            'launch a claude code kilix pane on the right tomorrow',
        ):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))

    def test_single_and_typographic_quotes_around_a_whole_payload_delimit_it(self):
        text = call('message_agent', agent='claude', text='if the build breaks, revert the last commit')
        for quoted in ("'if the build breaks, revert the last commit'",
                       "'if the build breaks, revert the last commit'.",
                       '\u2018if the build breaks, revert the last commit\u2019',
                       '\u201cif the build breaks, revert the last commit\u201d!',
                       '"if the build breaks, revert the last commit"'):
            with self.subTest(quoted=quoted):
                self.assertEqual(contract.exact_calls('Please pass this along to claude here: ' + quoted), [text])
        self.assertEqual(contract.exact_calls("tell claude: 'don't stop'"),
                         [call('message_agent', agent='claude', text="don't stop")])
        self.assertEqual(contract.exact_calls("start codex here with task: 'fix it'"), [call('start_codex', task='fix it')])
        self.assertEqual(contract.exact_calls("tell codex: 'run it', then wait for codex"),
                         [call('message_agent', agent='codex', text='run it'), call('wait_for_agent', agent='codex')])

    def test_quote_marks_that_do_not_wrap_the_whole_payload_stay_text(self):
        for payload in ("'tis done", "it's 'fine'", "fix the 'tests'", "' '", "\u2018a\u2019 or \u2018b\u2019 first"):
            with self.subTest(payload=payload):
                self.assertEqual(contract.exact_calls('tell claude: ' + payload),
                                 [call('message_agent', agent='claude', text=payload)])
        self.assertIsNone(contract.exact_calls("tell claude: 'a' and 'b'"))


if __name__ == '__main__':
    unittest.main()


class PaneTargets(unittest.TestCase):
    """A wait or message may name its session by Kilix pane (grammar-only
    arguments); the runner verifies the pane before anything is typed."""
    BROKER = '0c9e1f2a3b4d5e6f'

    def check(self, request, wanted):
        self.assertEqual(contract.exact_calls(request), wanted, request)
        self.assertTrue(contract.admit(request, wanted).actions, request)

    def test_pane_messages_said_the_ways_agents_say_them(self):
        text = 'the fixture rebuild is done; rerun step 4 in /tmp/sandpiper-bay and report.'
        for head, target in (
                ('Tell pane 812', {'pane': '812'}),
                ('Send this message to the Codex session in Kilix pane 77', {'agent': 'codex', 'pane': '77'}),
                ('Send a message to the existing Codex agent in pane 39', {'agent': 'codex', 'pane': '39'}),
                ('Message the session in pane 7', {'pane': '7'}),
                ('tell the coding agent in pane 31', {'pane': '31'}),
                ('Tell Claude in pane 2', {'agent': 'claude', 'pane': '2'}),
                ('tell claude code in pane 2', {'agent': 'claude', 'pane': '2'}),
                ('Tell the grok session in pane 119', {'agent': 'grok', 'pane': '119'}),
                ('Message existing agent pane 32 broker ' + self.BROKER, {'pane': '32', 'broker': self.BROKER}),
                ('Tell existing Codex pane 544 broker ' + self.BROKER, {'agent': 'codex', 'pane': '544', 'broker': self.BROKER}),
                ('tell pane 32 on broker ' + self.BROKER, {'pane': '32', 'broker': self.BROKER}),
                ('Send this message to the existing agent in pane 32, broker ' + self.BROKER, {'pane': '32', 'broker': self.BROKER}),
                ('message the claude session in pane 4 (broker ' + self.BROKER + ')',
                 {'agent': 'claude', 'pane': '4', 'broker': self.BROKER}),
                ('tell the agent in pane 32 with broker session ' + self.BROKER, {'pane': '32', 'broker': self.BROKER})):
            with self.subTest(head=head):
                self.check(head + ': ' + text, [call('message_agent', **target, text=text)])
        self.check('Send a message to the existing agent in pane 32 with broker session ' + self.BROKER + '. Message: ' + text,
                   [call('message_agent', pane='32', broker=self.BROKER, text=text)])

    def test_quoted_and_short_forms(self):
        self.check("message pane 59 'hello'", [call('message_agent', pane='59', text='hello')])
        self.check('Tell pane 498: "Source INCOMPLETE, keep the reader."',
                   [call('message_agent', pane='498', text='Source INCOMPLETE, keep the reader.')])
        self.check('tell the codex session in pane 9 "run the tests"',
                   [call('message_agent', agent='codex', pane='9', text='run the tests')])
        self.check('send "x" to the session in pane 4', [call('message_agent', pane='4', text='x')])
        self.check('tell pane 5 to rerun the linter', [call('message_agent', pane='5', text='rerun the linter')])

    def test_pane_waits(self):
        self.check('wait for pane 5 to finish', [call('wait_for_agent', pane='5')])
        self.check('wait for the codex session in pane 9', [call('wait_for_agent', agent='codex', pane='9')])
        self.check('wait until the claude in pane 2 is idle', [call('wait_for_agent', agent='claude', pane='2')])
        self.check('wait for grok in pane 4 to finish', [call('wait_for_agent', agent='grok', pane='4')])
        self.check('wait for pane 31 to finish, then tell it "next batch"',
                   [call('wait_for_agent', pane='31'), call('message_agent', pane='31', text='next batch')])
        self.check('wait for the codex session in pane 3, then tell it "go"',
                   [call('wait_for_agent', agent='codex', pane='3'), call('message_agent', agent='codex', pane='3', text='go')])

    def test_what_is_no_pane_target(self):
        for request in (
                'tell codex in pane 5 in /tmp/sandpiper-bay: hi',        # a pane and a directory
                'tell the codex session here in pane 4 "x"',
                'tell pane 4 "x" in ./sandpiper-bay', 'tell pane 4 "x" here',
                "wait for pane 4 that's working in ./sandpiper-bay to finish",
                'tell pane 5 "x" and pane 6 "y"',
                'tell the grok session in pane 18 and the grok session in pane 19 to say hello',
                'Tell pane503: hi', 'Tell session 518: hi', 'tell tab 7: hi', 'tell window 3: hi',
                'tell the codex session in the pane on the left: hi', 'tell my other pane: hi',
                'Tell pane 503 broker 0C9E1F2A3B4D5E6F: hi',             # a broker id is lowercase hex
                'Tell pane 503 broker 0c9e1f2a: hi',                     # too short to be one
                'Tell pane503 broker' + self.BROKER + ' from pane493: hi',
                'Tell pane 3 from pane 9: hi', 'tell pane 0: hi',
                'if it is idle, tell pane 4 "x"', 'is pane 7 idle?', 'close pane 7', 'run ls in pane 5',
                'tell the supervisor in pane 4 to go on',
                'Message existing Grok pane 377 only. Do not open a new agent. Read the brief.',
                'tell pane 5:', 'wait for pane 5 for ten minutes', 'wait for pane 5 to ask for approval',
                'tell pane 5: ' + 'x' * 1025):
            with self.subTest(request=request):
                self.assertIsNone(contract.exact_calls(request))

    def test_a_pane_and_an_unpaned_session_of_that_agent_have_no_reading(self):
        # "wait for claude" may mean the claude in pane 2.
        self.assertIsNone(contract.exact_calls('tell claude in pane 2: "x"; then wait for claude'))
        self.assertIsNone(contract.exact_calls('tell pane 2: "x"; then wait for codex'))
        self.check('start claude here, then tell claude in pane 5: "x"',
                   [call('start_claude'), call('message_agent', agent='claude', pane='5', text='x')])

    def test_admission_is_complete_and_held_to_the_request(self):
        request = 'Tell existing Codex pane 544 broker ' + self.BROKER + ': waiters deferred'
        wanted = contract.exact_calls(request)
        for changed in ({'pane': '545'}, {'broker': 'f' * 16}, {'agent': 'claude'}, {'text': 'waiters deferred.'}):
            proposal = [call('message_agent', **{**wanted[0]['arguments'], **changed})]
            self.assertFalse(contract.admit(request, proposal).actions, changed)
        dropped = {key: value for key, value in wanted[0]['arguments'].items() if key != 'broker'}
        self.assertFalse(contract.admit(request, [call('message_agent', **dropped)]).actions)
        action = contract.admit(request, wanted).actions[0]
        self.assertEqual((action.kind, action.args),
                         ('tell', {'pane': '544', 'broker': self.BROKER, 'agent': 'codex', 'text': 'waiters deferred'}))
        wait = contract.admit('wait for pane 5', [call('wait_for_agent', pane='5')]).actions[0]
        self.assertEqual((wait.kind, wait.args), ('wait', {'pane': '5', 'for': 'idle'}))

    def test_a_command_model_is_held_to_the_published_tools(self):
        request = 'tell codex in pane 179: test'
        wanted = contract.exact_calls(request)
        self.assertTrue(contract.admit(request, wanted).actions)
        refused = contract.admit(request, wanted, published=True)
        self.assertFalse(refused.actions)
        self.assertEqual(refused.reason, 'unexpected, missing or non-string arguments')
        # a five-tool proposal is no reading of a pane request
        self.assertFalse(contract.admit(request, [call('message_agent', agent='codex', text='test')], published=True).actions)
        # and the published path admits exactly what it did before
        self.assertTrue(contract.admit('tell codex here: run it', [call('message_agent', agent='codex', text='run it')],
                                       published=True).actions)

    def test_canonical_form_of_a_pane_target(self):
        good = [call('message_agent', pane='5', text='x')]
        self.assertEqual(contract._canonical(good), good)
        for args in ({'pane': '5', 'directory': '/x', 'text': 'x'}, {'pane': '05', 'text': 'x'},
                     {'pane': 'five', 'text': 'x'}, {'pane': '5', 'broker': 'XYZ', 'text': 'x'},
                     {'pane': '5', 'agent': 'kimi', 'text': 'x'}, {'agent': 'grok', 'text': 'x'},
                     {'agent': 'codex', 'broker': self.BROKER, 'text': 'x'}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                contract._canonical([call('message_agent', **args)])
        with self.assertRaises(ValueError):
            contract._canonical([call('start_codex', pane='5')])
        with self.assertRaises(ValueError):
            contract._canonical(good, published=True)
