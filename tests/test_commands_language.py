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

    def test_unquoted_spaced_paths_need_the_path_to_exist(self):
        # Owner decision 2026-11-07: an unquoted spaced path is read only when
        # that exact path exists (tests/test_commands_spaced_paths.py). These
        # do not exist, so they are refused as before; quotes always work.
        for text in ('Start Codex in /projects/Blue Birch',
                     'Tell Codex in /projects/Blue Birch: preserve this',
                     'Open /projects/Blue Birch/Review.pdf in the PDF viewer'):
            self.assertIsNone(contract.exact_calls(text))
        self.check('Start Codex in "/projects/Blue Birch".',
                   [call('start_codex', directory='/projects/Blue Birch')])
        # A bare right, left or down before an explicit path is the placement.
        self.check('Start Codex right in /projects/ridge.',
                   [call('start_codex', place='right', directory='/projects/ridge')])
        self.assertIsNone(contract.exact_calls('Start Codex up in /projects/ridge.'))
        self.assertIsNone(contract.exact_calls('Launch the calculator for me?'))

if __name__ == '__main__':
    unittest.main()


class FramedPhrasing(unittest.TestCase):
    """More ways to say one supported action, each with must-not-act neighbours:
    the same words with a condition, a time, a negation, a question, an extra
    action or a cut-off ending."""

    def check(self, text, expected):
        self.assertEqual(contract.exact_calls(text), expected)
        self.assertTrue(contract.admit(text, expected).actions)

    def refused(self, *texts):
        for text in texts:
            with self.subTest(refused=text):
                self.assertIsNone(contract.exact_calls(text))

    def test_launch_openers(self):
        for opener in ('Kick off', 'Fire up', 'Spin up', 'Bring up', 'Begin', 'Give me', 'Put'):
            with self.subTest(opener=opener):
                self.check(opener + ' a Codex session in ~/work/fig-lantern.',
                           [call('start_codex', directory='~/work/fig-lantern')])
        for text in ('Get claude going in ~/work/fig-lantern', "I'd like Claude started in ~/work/fig-lantern",
                     'I need a Claude session running in ~/work/fig-lantern', "let's get a fresh Claude going in ~/work/fig-lantern",
                     'would you mind starting claude in ~/work/fig-lantern?', 'go ahead and launch claude in ~/work/fig-lantern, thanks'):
            with self.subTest(text=text):
                self.check(text, [call('start_claude', directory='~/work/fig-lantern')])
        self.check('New Codex session, current directory.', [call('start_codex')])
        self.refused('Never kick off a Codex session in ~/work/fig-lantern', 'Should I spin up Claude in ~/work/fig-lantern?',
                     'I had Claude started in ~/work/fig-lantern', 'Get claude going in ~/work/fig-lantern tomorrow',
                     'Kick off Codex in ~/work/fig-lantern if the build is green', 'Fire up claude or codex',
                     'Spin up Claude in ~/work/fig-lantern and delete the old tab', 'Can I start Codex in a split on the right',
                     'would you mind stopping claude in ~/work/fig-lantern?', 'Kick off a Codex session in')

    def test_bare_and_relative_directory_names(self):
        for text, directory in (('launch codex in pewter-heron please', 'pewter-heron'),
                                ('start claude in the jalapeño-reef directory', 'jalapeño-reef'),
                                ('Begin a Codex session inside topaz-kite.v7, thanks', 'topaz-kite.v7'),
                                ('start claude in ./vendor/otter-grain', './vendor/otter-grain'),
                                ('start codex in ../burlap-violin', '../burlap-violin'),
                                ('start claude, new tab, directory /opt/wharf-cinder', '/opt/wharf-cinder'),
                                ('Bring up Claude in the directory "/mnt/data/Marsh Kettle"', '/mnt/data/Marsh Kettle'),
                                ('Start Codex in 桜-工房', '桜-工房')):
            with self.subTest(text=text):
                wanted = contract.exact_calls(text)
                self.assertEqual([c['arguments']['directory'] for c in wanted or []], [directory])
        # One plain word is not a directory; nor a mode, a model, a time or an application.
        self.refused('start codex in parallel', 'start codex in background', 'start codex in read-only',
                     'start codex in full-auto', 'start codex in dry-run mode', 'start codex in gpt-6',
                     'start codex in mid-december', 'start claude at half-past', 'start codex in e.g. a split',
                     'start claude in kilix-pdf', 'start codex in pewter-heron or birch-tally',
                     'start codex in pewter-heron in birch-tally')

    def test_splits_said_many_ways(self):
        for text, place in (('Open Codex in a split on the right.', 'right'), ('Put a new Claude in a lower split.', 'down'),
                            ('Start Codex in this folder in a split underneath.', 'down'),
                            ('run codex in salt-heron, right-hand split', 'right'),
                            ('Start Codex in ./vendor/otter-grain as an up split', 'up'),
                            ("I need Claude running in ~/x-y, put it in a split below", 'down'),
                            ('Kick off a Codex session in the current directory in a split above this one', 'up'),
                            ('START CODEX IN A LEFT SPLIT', 'left'), ('give me a new codex tab in señal-norte', 'tab')):
            with self.subTest(text=text):
                self.assertEqual([c['arguments'].get('place') for c in contract.exact_calls(text) or []], [place])
        self.refused('Start Codex in a split', 'Start Codex in a split on the right and a split on the left',
                     'Start Codex in a right split if there is room', 'Start Codex in a split on the')

    def test_tasks_and_models_after_a_framed_launch(self):
        self.check('Launch Codex in "plum harbor" with the task "open the changelog and start a 2.0 section"',
                   [call('start_codex', directory='plum harbor', task='open the changelog and start a 2.0 section')])
        self.check("open a claude session in /opt/zürich-dock and give it the task 'list every TODO under src/'",
                   [call('start_claude', directory='/opt/zürich-dock', task='list every TODO under src/')])
        self.check('start claude, new tab, task "wait for the build, then open a PR if tests pass"',
                   [call('start_claude', place='tab', task='wait for the build, then open a PR if tests pass')])
        self.check('Start Claude here. Task: "Don\'t open a PR yet"', [call('start_claude', task="Don't open a PR yet")])
        self.check('Get claude going in maple-heron in a down split, with model sonnet',
                   [call('start_claude', directory='maple-heron', place='down', model='sonnet')])
        # The condition is inside the quoted task, which is only passed on; outside it, it refuses.
        self.refused('Launch Codex with the task "x" if the build passes', 'Launch Codex with the task',
                     'Launch Codex with the task "x" and the task "y"', 'Launch Codex with the task: "x" tomorrow',
                     'Launch Codex with the task x y z and close it')

    def test_waits_said_many_ways(self):
        cases = [("Wait till Claude's current turn ends.", call('wait_for_agent', agent='claude')),
                 ('Pause until the Codex agent finishes.', call('wait_for_agent', agent='codex')),
                 ('Hold on until Claude in ivory-stork finishes its turn',
                  call('wait_for_agent', agent='claude', directory='ivory-stork')),
                 ('sit tight until the codex session in /scratch/sorbet.d completes its current turn',
                  call('wait_for_agent', agent='codex', directory='/scratch/sorbet.d')),
                 ('Wait for claude — the one working in /srv/fjäll-hut — to be done',
                  call('wait_for_agent', agent='claude', directory='/srv/fjäll-hut')),
                 ("could you wait on the codex agent that's working in \"~/clients/Noodle Raft\", please",
                  call('wait_for_agent', agent='codex', directory='~/clients/Noodle Raft')),
                 ('Wait until Codex\'s current turn is over in /tmp/k/linen-x.', 
                  call('wait_for_agent', agent='codex', directory='/tmp/k/linen-x')),
                 ('just wait till codex is done', call('wait_for_agent', agent='codex'))]
        for text, wanted in cases:
            with self.subTest(text=text):
                self.check(text, [wanted])
        self.refused('Wait for claude to start', 'Wait for claude to finish the migration', 'wait 5 minutes for codex',
                     'Did claude finish in ivory-stork?', 'hold on until claude or codex is done',
                     "Don't wait for codex", 'wait for codex in ivory-stork to be done, then quit it',
                     'Pause until the Codex agent finishes, unless it is stuck', 'sit tight until')

    def test_messages_with_quoted_text(self):
        cases = [("say 'looks good, ship it' to codex in ~/code/onyx-x", 'codex', '~/code/onyx-x', 'looks good, ship it'),
                 ('Relay "thanks, that\'s all!" to Claude in ~/src/reef-y, please', 'claude', '~/src/reef-y', "thanks, that's all!"),
                 ('Ping claude in piñata-loom with "what\'s left on your list?"', 'claude', 'piñata-loom', "what's left on your list?"),
                 ('Let Codex know: "don\'t start the server yet"', 'codex', None, "don't start the server yet"),
                 ('shoot codex a message saying "thanks"', 'codex', None, 'thanks'),
                 ('Write to Codex in "/srv/Larch Box": "Tomorrow we ship; freeze the branch."', 'codex', '/srv/Larch Box',
                  'Tomorrow we ship; freeze the branch.'),
                 ('Have this sent to codex in ~/oss/muffin-z: "rebase onto main"', 'codex', '~/oss/muffin-z', 'rebase onto main'),
                 ('To Claude in /srv/repos/glacier.v2, send "compare ./a.json with ./b.json"', 'claude', '/srv/repos/glacier.v2',
                  'compare ./a.json with ./b.json'),
                 ('Tell the codex in ~/thesis/gneiss-x “tighten section 4”', 'codex', '~/thesis/gneiss-x', 'tighten section 4')]
        for text, agent, directory, message in cases:
            with self.subTest(text=text):
                args = {'agent': agent, 'text': message, **({'directory': directory} if directory else {})}
                self.check(text, [call('message_agent', **args)])
        self.refused('say "x" to codex and claude', 'say "x" to codex if it is idle', 'Let Codex know',
                     'relay "x" to codex tomorrow', 'Did you say "x" to codex?', "don't say \"x\" to codex",
                     'say "x" to gemini', 'say "x" "y" to codex', 'say "x" to codex in ~/a in ~/b',
                     'Ping claude with "x" and close it', 'say "x to codex')

    def test_lists_of_applications(self):
        self.check('Open the notepad and the calculator.',
                   [call('open_app', app='kilix-notepad'), call('open_app', app='kilix-calculator')])
        self.check('open the calculator, the notepad and the file manager',
                   [call('open_app', app='kilix-calculator'), call('open_app', app='kilix-notepad'),
                    call('open_app', app='kilix-file')])
        self.check('Open the file manager, then the PDF viewer.',
                   [call('open_app', app='kilix-file'), call('open_app', app='kilix-pdf')])
        for text, app in (('Pop open the file manager, please.', 'kilix-file'), ('Fire up kilix-notepad.', 'kilix-notepad'),
                          ('pull the file manager up for me, would you', 'kilix-file'),
                          ('I need the calculator, open it please', 'kilix-calculator'),
                          ('get the PDF viewer up for me', 'kilix-pdf')):
            with self.subTest(text=text):
                self.check(text, [call('open_app', app=app)])
        self.check('Display "/home/pleb/x y/plan.4.pdf" in kilix-pdf.',
                   [call('open_app', app='kilix-pdf', file_path='/home/pleb/x y/plan.4.pdf')])
        self.refused('Open the notepad and the spreadsheet', 'Open the notepad or the calculator',
                     'open the calculator and work out 18% of 2,350', 'could I get the calculator open?',
                     'View ./exports/slate.pdf in kilix-pdf', 'Bring the calculator to the foreground',
                     'Open the notepad and the calculator tomorrow')

    def test_it_and_there_point_at_the_step_before(self):
        self.check('Wait for codex in ~/ml/pecan-x and then tell it "write up the results"',
                   [call('wait_for_agent', agent='codex', directory='~/ml/pecan-x'),
                    call('message_agent', agent='codex', directory='~/ml/pecan-x', text='write up the results')])
        self.check('Tell Claude in ~/work/daisy-x "pause", then wait for it to finish',
                   [call('message_agent', agent='claude', directory='~/work/daisy-x', text='pause'),
                    call('wait_for_agent', agent='claude', directory='~/work/daisy-x')])
        self.check('wait for claude in yarrow-x to finish, then start codex there in a right split',
                   [call('wait_for_agent', agent='claude', directory='yarrow-x'),
                    call('start_codex', directory='yarrow-x', place='right')])
        # "it" after a launch or after two sessions; "there" with no directory.
        # (An unquoted message after "tell it:" is read: see MarkedPayload.)
        self.refused('start codex in /x and tell it "y"', 'wait for claude and codex, then tell it "y"',
                     'wait for claude and codex then tell it: run the tests', 'wait for claude, then start codex there',
                     'wait for claude in /x, then start codex there is a bug', 'wait for codex in /x and then tell it')

    def test_listed_steps(self):
        self.check('First message codex "pause", second wait for claude in "Old Mill", third open kilix-pdf',
                   [call('message_agent', agent='codex', text='pause'),
                    call('wait_for_agent', agent='claude', directory='Old Mill'), call('open_app', app='kilix-pdf')])
        self.check('1) open the pdf viewer 2) wait for codex in rowan-x to finish',
                   [call('open_app', app='kilix-pdf'), call('wait_for_agent', agent='codex', directory='rowan-x')])
        self.check('Three things: start codex in a split on the right, start claude in a split down, and open the file manager',
                   [call('start_codex', place='right'), call('start_claude', place='down'), call('open_app', app='kilix-file')])
        self.refused('Two things: open the notepad, open the calculator, open the file manager',
                     'second open the notepad, first open the calculator', 'first open the notepad, third open the calculator',
                     'First open the notepad, second delete /tmp/x', '2) open the notepad 1) open the calculator',
                     '1) open the notepad 2) open the calculator 3)')


class MarkedPayload(unittest.TestCase):
    """An unquoted message or task after a marker (":", "Task:", "with the
    following instructions:", "let X know:") is the rest of the request, taken
    whole as data: its sentences, semicolons and "if"/"then" words are the
    receiving session's (docs/ask-phrasing.md, "Unquoted payloads")."""

    def check(self, text, expected):
        self.assertEqual(contract.exact_calls(text), expected)
        self.assertTrue(contract.admit(text, expected).actions)

    def test_a_message_runs_to_the_end(self):
        where = '/srv/oast-galliard/run-4'
        for text, payload in (
            ('Tell the Codex agent in /srv/oast-galliard/run-4: ', 'fix the flaky test; if it still fails, skip it and say why.'),
            ('Send the following to codex in /srv/oast-galliard/run-4: ', 'run the tests, and do not merge. Reply with the count.'),
            ('Let codex in /srv/oast-galliard/run-4 know: ', 'the fixture moved to fixtures/v2.'),
            ('Pass this along to the codex session in /srv/oast-galliard/run-4: ', 'is the build green?'),
            ('Message the Codex session that is working in /srv/oast-galliard/run-4: ', 'store the notes and wait.'),
        ):
            with self.subTest(text=text):
                self.check(text + payload, [call('message_agent', agent='codex', text=payload, directory=where)])

    def test_words_for_ask_inside_the_payload_are_data(self):
        # The receiving session is told to start something; Ask starts nothing.
        self.check('Tell claude in /srv/oast-galliard: start codex if the tests pass',
                   [call('message_agent', agent='claude', text='start codex if the tests pass', directory='/srv/oast-galliard')])

    def test_a_task_after_a_framed_launch(self):
        task = 'Read NOTES.md; reply with OK.'
        self.check('Start codex in /srv/oast-galliard/v2.1 using model gpt-6 in a new tab. Task: ' + task,
                   [call('start_codex', directory='/srv/oast-galliard/v2.1', model='gpt-6', place='tab', task=task)])
        self.check('Start claude in /srv/oast-galliard/v2.1. Task: ' + task,     # the path ends its sentence
                   [call('start_claude', directory='/srv/oast-galliard/v2.1', task=task)])
        self.check('Start codex in /srv/oast-galliard with these instructions: ' + task,
                   [call('start_codex', directory='/srv/oast-galliard', task=task)])
        self.check('Open a new tab with codex in /srv/oast-galliard: ' + task,
                   [call('start_codex', directory='/srv/oast-galliard', place='tab', task=task)])
        self.check('Start codex in a new Kilix tab in /srv/oast-galliard: ' + task,
                   [call('start_codex', directory='/srv/oast-galliard', place='tab', task=task)])

    def test_tell_it_after_a_wait(self):
        self.check('Wait for claude in /srv/oast-galliard to finish, then tell it: Read NOTES.md.',
                   [call('wait_for_agent', agent='claude', directory='/srv/oast-galliard'),
                    call('message_agent', agent='claude', text='Read NOTES.md.', directory='/srv/oast-galliard')])

    def test_job_prefixes_and_nothing_else(self):
        self.check('agents: start codex in /srv/oast-galliard in a new tab',
                   [call('start_codex', directory='/srv/oast-galliard', place='tab')])
        self.check('apps: open the calculator', [call('open_app', app='kilix-calculator')])
        self.check('Start codex in /srv/oast-galliard and do nothing else.', [call('start_codex', directory='/srv/oast-galliard')])
        for text in ('agents: open the calculator', 'apps: start codex', 'agents:'):
            with self.subTest(text=text):
                self.assertIsNone(contract.exact_calls(text))

    def test_the_fixed_shapes_still_read_first(self):
        # Where the fixed shapes read a request, the new reader never runs.
        self.check('Tell Codex: compare A and B; then C.',
                   [call('message_agent', agent='codex', text='compare A and B; then C.')])
