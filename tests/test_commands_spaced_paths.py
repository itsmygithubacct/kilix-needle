"""Unquoted spaced paths and bare placement (owner decision 2026-11-07).

Words after a path cannot be told apart from a task, a courtesy or a second
sentence, so an unquoted path with spaces is read only when that exact path
exists: a directory for an agent, a file for the PDF viewer. The longest
existing reading wins and the words after it are parsed as before, unless a
shorter existing path also reads the whole request differently: that request
is ambiguous and refused. A bare right, left or down directly after the agent
name and before an explicit path is the placement; "up" is never a bare
placement ("start claude up"), and after "the" or a session noun the word may
describe the session, so the request is refused.
"""
import support  # noqa: F401
from pathlib import Path
import tempfile
import unittest

import commands_contract as contract


def call(name, **arguments):
    return {'name': name, 'arguments': arguments}


class Checks:
    def check(self, text, expected):
        got = contract.exact_calls(text)
        self.assertIsNotNone(got, text)
        self.assertEqual(contract._canonical(got), contract._canonical(expected), text)
        self.assertTrue(contract.admit(text, expected).actions, text)

    def refuse(self, text):
        self.assertIsNone(contract.exact_calls(text), text)


class SpacedPaths(Checks, unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        (self.root / 'my project').mkdir()
        (self.root / 'my project' / 'Release Notes.pdf').write_bytes(b'%PDF-1.4\n')
        (self.root / 'river delta').mkdir()

    def path(self, name):
        return str(self.root / name)

    def test_an_existing_spaced_directory_is_read_whole(self):
        here, delta = self.path('my project'), self.path('river delta')
        for text, expected in [
            (f'Start Codex in {here}', [call('start_codex', directory=here)]),
            (f'start claude in {here}.', [call('start_claude', directory=here)]),
            (f'Start Codex in {here}, please', [call('start_codex', directory=here)]),
            (f'Please launch Codex in {here} now.', [call('start_codex', directory=here)]),
            (f'Wait for Codex in {here}', [call('wait_for_agent', agent='codex', directory=here)]),
            (f'Wait until Claude finishes in {delta}.',
             [call('wait_for_agent', agent='claude', directory=delta)]),
            (f'Wait until Codex in {here} is done',
             [call('wait_for_agent', agent='codex', directory=here)]),
            (f'Tell Codex in {here}: run the tests',
             [call('message_agent', agent='codex', directory=here, text='run the tests')]),
            (f'Tell Codex in {here} to run the tests',
             [call('message_agent', agent='codex', directory=here, text='run the tests')]),
            (f'Start Codex in {here} to fix the build',
             [call('start_codex', directory=here, task='fix the build')]),
            (f'Start Codex in {here} on the right', [call('start_codex', directory=here, place='right')]),
            (f'Start Codex in {here} with model gpt-6',
             [call('start_codex', directory=here, model='gpt-6')]),
            (f'Start Codex in {here}; then open calculator',
             [call('start_codex', directory=here), call('open_app', app='kilix-calculator')]),
            (f'Start Claude in {here} and Codex in {delta}',
             [call('start_claude', directory=here), call('start_codex', directory=delta)]),
        ]:
            with self.subTest(text=text):
                self.check(text, expected)

    def test_words_that_do_not_make_an_existing_path_are_not_part_of_it(self):
        here = self.path('my project')
        for tail in ('fix failing tests', 'thanks', 'tomorrow', 'again', 'haiku', 'new window',
                     'urgent: build failed', '. Fix lint errors.', 'deploy to staging'):
            with self.subTest(tail=tail):
                self.refuse(f'Start Codex in {here} {tail}')
        for text in (f'Tell Claude in {self.root}/my we need to rebase',
                     f'Start Codex in {self.root}/my  project',
                     f'Start Codex in {self.root}/no such place'):
            with self.subTest(text=text):
                self.refuse(text)
        # A wait tail is never swallowed when the longer path does not exist.
        self.check(f'Wait until Codex in {here} is done',
                   [call('wait_for_agent', agent='codex', directory=here)])

    def test_two_existing_readings_that_differ_are_refused(self):
        # Review round 2, M1: "~/proj new tab" beside a folder "~/proj new".
        for base, spaced in (('proj', 'proj new'), ('data', 'data split'), ('thesis', 'thesis fresh')):
            (self.root / base).mkdir()
            (self.root / spaced).mkdir()
        proj, data = self.path('proj'), self.path('data')
        for text in (f'Start Claude in {proj} new tab',
                     f'Start Claude in {proj} new tab please',
                     f'Start Codex in {self.path("thesis")} fresh tab to fix the citations',
                     f'Start Claude in {data} split below me',
                     f'Start Claude in {proj} new tab and open the calculator'):
            with self.subTest(text=text):
                self.refuse(text)
        # Quoting either folder says which one is meant.
        self.check(f'Start Claude in "{proj}" new tab',
                   [call('start_claude', directory=proj, place='tab')])
        self.check(f'Start Claude in "{proj} new" in a new tab',
                   [call('start_claude', directory=proj + ' new', place='tab')])
        # One reading only one of the two paths completes is that reading.
        self.check(f'Start Codex in {proj} new', [call('start_codex', directory=proj + ' new')])
        self.check(f'Wait until Codex in {proj} is done',
                   [call('wait_for_agent', agent='codex', directory=proj)])
        (self.root / 'Research').mkdir()
        (self.root / 'Research and Development').mkdir()
        research = self.path('Research and Development')
        self.check(f'Start Codex in {research}', [call('start_codex', directory=research)])

    def test_quoted_paths_keep_their_exact_spelling(self):
        self.check('Start Codex in "/work/no such project."',
                   [call('start_codex', directory='/work/no such project.')])

    def test_an_existing_spaced_pdf_is_read_whole(self):
        notes = self.path('my project/Release Notes.pdf')
        self.check(f'Open {notes} in the PDF viewer', [call('open_app', app='kilix-pdf', file_path=notes)])
        self.check(f'Open {notes}.', [call('open_app', app='kilix-pdf', file_path=notes)])
        self.check(f'Launch the PDF viewer for {notes}.',
                   [call('open_app', app='kilix-pdf', file_path=notes)])
        self.check(f'Open {notes} and open calculator',
                   [call('open_app', app='kilix-pdf', file_path=notes),
                    call('open_app', app='kilix-calculator')])
        base = self.path('my project')
        for text in (f'Open {base}/draft.pdf, final.pdf', f'Open {base}/a.pdf b.pdf',
                     f'Open {base}/Release Notes.pdf and /x/b.pdf',
                     f'Open {base}/Missing File.pdf in the PDF viewer'):
            with self.subTest(text=text):
                self.refuse(text)


class BarePlacement(Checks, unittest.TestCase):
    def test_a_bare_direction_before_an_explicit_path_is_the_placement(self):
        for agent in ('claude', 'codex'):
            for place in ('right', 'left', 'down'):
                text = f'Start {agent.title()} {place} in /srv/juniper/release.'
                with self.subTest(text=text):
                    self.check(text, [call('start_' + agent, place=place,
                                           directory='/srv/juniper/release')])
        self.check('Start Codex left in "/srv/x y"', [call('start_codex', place='left', directory='/srv/x y')])
        self.check('Start Claude Code right in /srv/x', [call('start_claude', place='right', directory='/srv/x')])
        self.check('Start Claude right in /srv/x and Codex left in /srv/y',
                   [call('start_claude', place='right', directory='/srv/x'),
                    call('start_codex', place='left', directory='/srv/y')])

    def test_other_bare_directions_refuse(self):
        for text in ('Start Claude right now', 'Start Claude right here', 'Start Claude right',
                     'Start Claude right in this folder', 'Start Claude right in /srv/x on the left',
                     'Start Claude right in /srv/x in a new tab', 'Start Claude up',
                     'Start Claude up in /srv/x', 'Get Codex up in /work/api',
                     'Start Claude right away in /srv/x', 'Tell Claude right in /srv/x: hello',
                     # Review round 2, M2: the session someone left there.
                     'Open the Codex session left in ~/proj', 'Start the Codex session left in ~/proj',
                     'Open the Claude left in ~/proj', 'Get the Codex left in ~/proj',
                     'Start Codex session left in /srv/x', 'Open Claude Code session left in /srv/x',
                     'Start Claude Code running right in /srv/x'):
            with self.subTest(text=text):
                self.refuse(text)

    def test_explicit_placements_are_unchanged(self):
        self.check('Start Codex in a split to my right in /projects/ridge',
                   [call('start_codex', place='right', directory='/projects/ridge')])
        self.check('Start Codex in a split above in /projects/ridge',
                   [call('start_codex', place='up', directory='/projects/ridge')])


if __name__ == '__main__':
    unittest.main()
