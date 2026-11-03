"""Navigation qualifiers constrain pure plans to the requested topology."""
import os
import unittest
from unittest import mock

from support import tab, window
import actions
import domain_bridge
import kilix
import needle_cli
import panes_exact


VERBS = ('jump to', 'take me to', 'switch over to', 'go to', 'focus', 'switch to')


def topology(local=(), remote=('Orchid',)):
    return [{'id': 1, 'tabs': [
        tab(51, 'origin', [window(510, 'caller', active=True)] +
            [window(511 + i, name) for i, name in enumerate(local)]),
        tab(61, 'elsewhere', [window(610 + i, name, active=i == 0)
                            for i, name in enumerate(remote or ('remote-shell',))], active=True),
    ]}]


class NavigationScope(unittest.TestCase):
    def setUp(self):
        self.caller = mock.patch.dict(os.environ, {'KITTY_WINDOW_ID': '510'})
        self.caller.start()
        self.addCleanup(self.caller.stop)

    def checked(self, request):
        found = panes_exact.admitted(request)
        self.assertIsNotNone(found, request)
        calls, sentence = found
        validated = domain_bridge.validate(sentence, calls)
        self.assertEqual(validated['refusals'], [], request)
        interpreted = actions.interpret(sentence, calls)
        self.assertEqual(len(interpreted), 1)
        self.assertIsInstance(interpreted[0], actions.Action)
        return interpreted[0], sentence

    def plan(self, request, data):
        action, _ = self.checked(request)
        return kilix.resolve(action, kilix.Tree(data))

    def test_current_tab_excludes_remote_names_for_every_navigation_verb(self):
        for verb in VERBS:
            for target in ('the Orchid pane', 'pane named Orchid', 'pane "Orchid"'):
                for suffix in ('in this tab', 'in the current tab', 'in this tab, not the tab itself'):
                    request = f'{verb} {target} {suffix}'
                    with self.subTest(request=request):
                        with self.assertRaises(kilix.KilixError):
                            self.plan(request, topology())

    def test_scope_survives_canonicalization_interpretation_and_validation(self):
        for verb in VERBS:
            request = f'{verb} pane named "Orchid" in the current tab'
            with self.subTest(verb=verb):
                action, sentence = self.checked(request)
                self.assertIn('in this tab', sentence)
                self.assertEqual(action.args, {'pane': 'name:orchid', 'pane_scope': 'current'})

    def test_one_local_match_wins_without_outside_ambiguity(self):
        for verb in VERBS:
            step = self.plan(f'{verb} pane named Orchid in this tab',
                             topology(('Orchid',), ('ORCHID', 'Orchid')))
            self.assertEqual(step.commands, ((("focus-window", "--match=id:511"), None),))
            self.assertFalse(step.elsewhere)

    def test_duplicate_local_or_missing_names_never_fall_back(self):
        for local, remote in [(('Orchid', 'ORCHID'), ('Orchid',)), ((), ()),
                              ((), ('Orchid', 'ORCHID'))]:
            with self.subTest(local=local, remote=remote):
                with self.assertRaises(kilix.KilixError):
                    self.plan('jump to pane named Orchid in this tab', topology(local, remote))

    def test_scope_filters_before_exact_and_whole_word_ranking(self):
        # An outside exact match must not shadow the unique allowed whole-word match.
        step = self.plan('jump to the Orchid pane in this tab', topology(('Orchid build',)))
        self.assertEqual(step.commands[0][0], ('focus-window', '--match=id:511'))
        self.assertTrue(step.fuzzy)
        with self.assertRaises(kilix.KilixError):
            self.plan('jump to the Orchid pane in this tab', topology((), ('Orchid build',)))

    def test_quotes_numeric_titles_unicode_and_scope_words_are_literals(self):
        for name in ('Build Log', '70', 'Café', '日志', 'tab', 'left'):
            for quote in ('"', "'", '`'):
                for verb in VERBS:
                    request = f'{verb} pane named {quote}{name}{quote} in this tab'
                    with self.subTest(request=request):
                        with self.assertRaises(kilix.KilixError):
                            self.plan(request, topology((), (name,)))
                        step = self.plan(request, topology((name,), (name,)))
                        self.assertEqual(step.commands[0][0], ('focus-window', '--match=id:511'))

    def test_numeric_id_is_restricted_but_an_unscoped_id_can_cross_tabs(self):
        for verb in VERBS:
            for form in ('pane 610', 'pane:610'):
                with self.subTest(verb=verb, form=form):
                    with self.assertRaises(kilix.KilixError):
                        self.plan(f'{verb} {form} in this tab', topology())
                    step = self.plan(f'{verb} {form}', topology())
                    self.assertEqual(step.commands[0][0], ('focus-window', '--match=id:610'))
            step = self.plan(f'{verb} pane named "610" in this tab', topology(('610',)))
            self.assertEqual(step.commands[0][0], ('focus-window', '--match=id:511'))

    def test_bare_titles_that_are_prepositions_are_not_scope_qualifiers(self):
        for name in ('in', 'from', 'inside', 'on', 'outside'):
            for verb in VERBS:
                with self.subTest(name=name, verb=verb):
                    step = self.plan(f'{verb} pane named {name} in this tab', topology((name,)))
                    self.assertEqual(step.commands[0][0], ('focus-window', '--match=id:511'))

    def test_relative_collisions_are_checked_only_within_explicit_scope(self):
        data = topology(('Orchid',), ('next',))
        step = self.plan('jump to the next pane in this tab', data)
        self.assertEqual(step.commands[0][0], ('focus-window', '--match=id:511'))
        with self.assertRaises(kilix.KilixError):
            self.plan('jump to the next pane', data)

    def test_unknown_agent_caller_cannot_infer_current_tab_from_viewed_tab(self):
        with mock.patch.dict(os.environ, {'KITTY_WINDOW_ID': ''}), \
                mock.patch.object(kilix, '_ancestors', return_value=[]):
            tree = kilix.Tree(topology())
        engine = mock.Mock()
        engine.complete.side_effect = AssertionError('model inference disabled')
        with mock.patch.object(kilix, 'snapshot', return_value=tree), \
                mock.patch.object(kilix, 'perform', side_effect=AssertionError('execution disabled')):
            record = needle_cli.run_request(engine, 'jump to the Orchid pane in this tab',
                                           needle_cli.Options(agent=True, dry_run=True))
        self.assertEqual(record['status'], 1)
        self.assertTrue(all(item['outcome'] == 'refused' for item in record['items']))
        engine.complete.assert_not_called()

    def test_unscoped_matching_and_explicit_tab_navigation_are_retained(self):
        step = self.plan('jump to pane named Orchid', topology())
        self.assertEqual(step.commands[0][0], ('focus-window', '--match=id:610'))
        step = self.plan('go to tab 2', topology())
        self.assertEqual(step.commands[0][0], ('focus-tab', '--match=id:61'))
        step = self.plan('jump to pane named Orchid', topology(('Orchid',)))
        self.assertEqual(step.commands[0][0], ('focus-window', '--match=id:511'))
        self.assertTrue(step.elsewhere)

    def test_unsupported_titles_do_not_become_scope_or_a_different_literal(self):
        # Existing title normalization cannot preserve these literals. The
        # route must decline them, not invent scope or navigate to "in".
        for verb in VERBS:
            for name in ('in this tab', 'Notes in this tab', 'current pane'):
                for suffix in ('', ' in this tab'):
                    with self.subTest(verb=verb, name=name, suffix=suffix):
                        self.assertIsNone(panes_exact.admitted(f'{verb} pane named "{name}"{suffix}'))

    def test_direct_model_proposals_cannot_drop_or_forge_scope(self):
        call = {'name': 'go_to_pane', 'arguments': {'pane': 'Orchid'}}
        [action] = actions.interpret('go to the Orchid pane in this tab', [call])
        with self.assertRaises(kilix.KilixError):
            kilix.resolve(action, kilix.Tree(topology()))
        forged = {'name': 'go_to_pane', 'arguments': {'pane': 'Orchid', 'pane_scope': 'current'}}
        self.assertIsInstance(actions.interpret('go to the Orchid pane', [forged])[0], actions.Refusal)
        for suffix in ('in tab 2', 'in another tab', 'within this tab', 'across all tabs',
                       'in this tab and in tab 2'):
            with self.subTest(suffix=suffix):
                self.assertIsInstance(actions.interpret(f'go to the Orchid pane {suffix}', [call])[0],
                                      actions.Refusal)

    def test_unsupported_scope_and_trailing_instructions_do_not_gain_exact_routes(self):
        for verb in VERBS:
            for suffix in ('in tab 2', 'in another tab', 'across all tabs', 'within this tab',
                           'in this tab and close it', 'in this tab; split below',
                           'in this tab if the build passes', 'in this tab tomorrow',
                           'in this tab in another window', 'in this tab and in tab 2'):
                with self.subTest(verb=verb, suffix=suffix):
                    self.assertIsNone(panes_exact.admitted(f'{verb} the Orchid pane {suffix}'))

    def test_cli_dry_run_never_plans_a_remote_scoped_target(self):
        for verb in VERBS:
            engine = mock.Mock()
            engine.complete.side_effect = AssertionError('model inference disabled')
            with mock.patch.object(kilix, 'snapshot', return_value=kilix.Tree(topology())), \
                    mock.patch.object(kilix, 'perform', side_effect=AssertionError('execution disabled')) as perform, \
                    mock.patch.object(kilix, '_run', side_effect=AssertionError('desktop disabled')):
                record = needle_cli.run_request(engine, f'{verb} the Orchid pane in this tab',
                                               needle_cli.Options(agent=True, dry_run=True))
            self.assertEqual(record['status'], 1)
            self.assertFalse(any(item['outcome'] == 'would' for item in record['items']))
            perform.assert_not_called()
            engine.complete.assert_not_called()


if __name__ == '__main__':
    unittest.main()
