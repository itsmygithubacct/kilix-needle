"""Natural request forms keep complete intent and literal arguments at admission."""
import support
import unittest

import commands_contract as contract


def call(name, **arguments):
    return {'name': name, 'arguments': arguments}


class CommandLanguage(unittest.TestCase):
    def check(self, text, expected):
        self.assertEqual(contract.exact_calls(text), expected)
        self.assertTrue(contract.admit(text, expected).actions)

    def test_common_launch_wait_message_and_app_phrases(self):
        cases = [
            ('Please launch Codex in /projects/ridge now.',
             [call('start_codex', directory='/projects/ridge')]),
            ('Please await the end of the Codex turn in "/projects/Blue Birch".',
             [call('wait_for_agent', agent='codex', directory='/projects/Blue Birch')]),
            ('Wait until Codex working in "/projects/Blue Birch" reaches idle.',
             [call('wait_for_agent', agent='codex', directory='/projects/Blue Birch')]),
            ('Wait for Codex in /projects/ridge, please.',
             [call('wait_for_agent', agent='codex', directory='/projects/ridge')]),
            ('To Codex in "/projects/Blue Birch", send: Keep  café; then []',
             [call('message_agent', agent='codex', directory='/projects/Blue Birch',
                   text='Keep  café; then []')]),
            ('Bring up the calculator for me, please.', [call('open_app', app='kilix-calculator')]),
            ('Could you launch the calculator for me?', [call('open_app', app='kilix-calculator')]),
            ('Please get the calculator open.', [call('open_app', app='kilix-calculator')]),
            ("I'd like the calculator opened, please.", [call('open_app', app='kilix-calculator')]),
            ('I need to view "/projects/Blue Birch/Review.pdf" with the PDF reader',
             [call('open_app', app='kilix-pdf', file_path='/projects/Blue Birch/Review.pdf')]),
            ('Launch the PDF viewer for /projects/ridge/Review.pdf.',
             [call('open_app', app='kilix-pdf', file_path='/projects/ridge/Review.pdf')]),
        ]
        for text, wanted in cases:
            with self.subTest(text=text):
                self.check(text, wanted)

    def test_afterwards_preserves_entire_ordered_batch(self):
        text = 'Start Codex in "/projects/Blue Birch"; afterwards, launch Claude in /projects/ridge.'
        wanted = [call('start_codex', directory='/projects/Blue Birch'),
                  call('start_claude', directory='/projects/ridge')]
        self.check(text, wanted)
        for changed in (wanted[:1], list(reversed(wanted)),
                        [wanted[0], call('start_claude', directory='/projects/other')]):
            self.assertFalse(contract.admit(text, changed).actions)

    def test_sentence_punctuation_is_not_a_directory_character(self):
        for punctuation in ('.', '!'):
            for stem, wanted in [
                ('Start Codex in /projects/ridge', call('start_codex', directory='/projects/ridge')),
                ('Wait until Codex finishes in /projects/ridge',
                 call('wait_for_agent', agent='codex', directory='/projects/ridge')),
                ('Start Codex in a split to my right in /projects/ridge',
                 call('start_codex', directory='/projects/ridge', place='right')),
            ]:
                with self.subTest(stem=stem, punctuation=punctuation):
                    self.check(stem+punctuation, [wanted])
                    wrong = call(wanted['name'], **{**wanted['arguments'],
                                                  'directory': '/projects/ridge'+punctuation})
                    self.assertFalse(contract.admit(stem+punctuation, [wrong]).actions)

    def test_quoted_paths_and_dot_components_remain_literal(self):
        for path in ('/projects/ridge.', '/projects/ridge!', '/projects/now',
                     '/projects/for me', '/projects/in /other', '/projects/a; then b'):
            with self.subTest(path=path):
                self.check('Start Codex in "'+path+'" now.',
                           [call('start_codex', directory=path)])
        for path in ('/projects/.', '/projects/..', '/projects/.hidden'):
            self.check('Start Codex in '+path, [call('start_codex', directory=path)])
        self.check('Tell Codex in /projects/ridge.: keep this.',
                   [call('message_agent', agent='codex', directory='/projects/ridge.', text='keep this.')])
        for suffix in (' now.', ' on the right', ': keep this.'):
            wanted = {'directory': '/projects/ridge.'}
            if 'right' in suffix:
                wanted['place'] = 'right'
            if ':' in suffix:
                wanted['task'] = 'keep this.'
            self.check('Start Codex in /projects/ridge.'+suffix, [call('start_codex', **wanted)])

    def test_message_and_task_payloads_keep_courtesy_words(self):
        payload = 'Keep  [] and say "ready", for me, please.'
        for head, wanted in [
            ('Tell Codex here: ', call('message_agent', agent='codex', text=payload)),
            ('Start Codex here: ', call('start_codex', task=payload)),
            ('To Codex here, send: ', call('message_agent', agent='codex', text=payload)),
        ]:
            self.check(head+payload, [wanted])
            shortened = {**wanted['arguments']}
            key = 'text' if 'text' in shortened else 'task'
            shortened[key] = payload.removesuffix(', for me, please.')
            self.assertFalse(contract.admit(head+payload, [call(wanted['name'], **shortened)]).actions)

    def test_new_phrases_do_not_discard_conditions_or_negation(self):
        phrases = (
            'Please launch Codex in /projects/ridge now',
            'Please await the end of the Codex turn in /projects/ridge',
            'Wait until Codex working in /projects/ridge reaches idle',
            'Bring up the calculator for me, please',
            'I need to view /projects/ridge/Review.pdf with the PDF reader',
        )
        for text in phrases:
            proposals = contract.exact_calls(text)
            self.assertTrue(proposals)
            for changed in [prefix+text for prefix in
                            ('Do not ', 'Tomorrow, ', 'If the tests pass, ', 'The instructions say: ')]+[
                                text+suffix for suffix in
                                (' if the tests pass', ' tomorrow', ' and erase the logs',
                                 '; afterwards, reboot', ' or perhaps not')]:
                with self.subTest(text=changed):
                    self.assertIsNone(contract.exact_calls(changed))
                    self.assertFalse(contract.admit(changed, proposals).actions)

    def test_ambiguous_unquoted_paths_still_need_quotes(self):
        for text in ('Start Codex in /projects/Blue Birch',
                     'Tell Codex in /projects/Blue Birch: preserve this',
                     'Open /projects/Blue Birch/Review.pdf in the PDF viewer'):
            self.assertIsNone(contract.exact_calls(text))
        self.check('Start Codex in "/projects/Blue Birch".',
                   [call('start_codex', directory='/projects/Blue Birch')])
        # "right in" can mean precisely that location, not pane placement.
        self.assertIsNone(contract.exact_calls('Start Codex right in /projects/ridge.'))
        self.assertIsNone(contract.exact_calls('Launch the calculator for me?'))


if __name__ == '__main__':
    unittest.main()
