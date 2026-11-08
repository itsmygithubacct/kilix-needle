"""Whole-request navigation aliases preserve the existing target contract."""
import unittest

import actions
import panes_exact


class NavigationAliases(unittest.TestCase):
    VERBS = ('jump to', 'take me to', 'switch over to')

    def admitted_target(self, request):
        found = panes_exact.admitted(request)
        self.assertIsNotNone(found, request)
        calls, canonical = found
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['name'], 'go_to_pane')
        checked = actions.interpret(canonical, calls)
        self.assertEqual(len(checked), 1)
        self.assertIsInstance(checked[0], actions.Action)
        return checked[0].args['pane']

    def test_literal_names_are_not_generated_or_shortened(self):
        for verb in self.VERBS:
            for name in ('btop', 'htop', 'monitor', 'build-log', 'a.b+c', 'Café', '日志'):
                with self.subTest(verb=verb, name=name):
                    self.assertEqual(self.admitted_target(f'{verb} the {name} pane'),
                                     'name:' + name.casefold())

    def test_titles_quotes_spaces_and_typography(self):
        for verb in self.VERBS:
            for quote in ('"', "'", '`'):
                for name in ('Build Log', 'next', 'left', '70', 'Café'):
                    with self.subTest(verb=verb, quote=quote, name=name):
                        self.assertEqual(self.admitted_target(f'{verb} the pane named {quote}{name}{quote}'),
                                         'name:' + name.casefold())
        self.assertEqual(self.admitted_target('Please JUMP TO the pane named “Build Log”, thanks!'),
                         'name:build log')
        for name in ('service:2', 'user@host'):
            self.assertEqual(self.admitted_target(f'jump to pane named {name}'), 'name:' + name)

    def test_unsupported_titles_are_not_silently_normalized_to_other_targets(self):
        for name in ('the build', 'current pane', 'Build Log.'):
            with self.subTest(name=name):
                self.assertIsNone(panes_exact.admitted(f'jump to the pane named "{name}"'))

    def test_numeric_ids_are_distinct_from_numeric_titles(self):
        for verb in self.VERBS:
            for target, expected in (('pane 300', 'id:300'), ('pane:00300', 'id:300'),
                                     ('pane named 300', 'name:300'), ('pane "300"', 'name:300'),
                                     ('the 300 pane', 'name:300')):
                with self.subTest(verb=verb, target=target):
                    self.assertEqual(self.admitted_target(f'{verb} {target}'), expected)

    def test_relative_and_spatial_targets_are_not_rewritten_as_titles(self):
        for verb in self.VERBS:
            for target in ('left', 'right', 'above', 'below', 'next', 'previous', 'current'):
                for words in (f'the {target} pane', f'pane {target}'):
                    with self.subTest(verb=verb, words=words):
                        self.assertEqual(self.admitted_target(f'{verb} {words}'), target)

    def test_scope_is_only_the_existing_current_tab_scope(self):
        for verb in self.VERBS:
            for suffix in ('', ' in this tab', ' in the current tab', ' in this tab, not the tab itself'):
                with self.subTest(verb=verb, suffix=suffix):
                    self.assertEqual(self.admitted_target(f'{verb} the monitor pane{suffix}'), 'name:monitor')

    def test_unsupported_or_ambiguous_targets_do_not_gain_a_route(self):
        for verb in self.VERBS:
            for target in ('pane 1234567', 'pane -1', 'pane 1.5', 'the top pane', 'the bottom pane',
                           'the other pane', 'whichever pane', 'the monitor pane in tab 2',
                           'the pane named', 'pane id 300', 'tab 2', 'the tab named monitor',
                           'the monitor pane in another tab', 'the monitor pane across all tabs'):
                with self.subTest(verb=verb, target=target):
                    self.assertIsNone(panes_exact.admitted(f'{verb} {target}'))

    def test_noncommands_and_extra_instructions_are_not_exact(self):
        for verb in self.VERBS:
            command = f'{verb} the monitor pane'
            for request in (f"don't {command}", f'do not {command}', f'never {command}',
                            f'how do I {command}?', f'explain how to {command}',
                            f'"{command}"', f'my teammate said {command}',
                            command+' if the build passes', command+' tomorrow',
                            command+' in five seconds', command+' and close it',
                            command+'; close tab 2', command+' then split below',
                            command+' or the editor pane', command+' - never mind'):
                with self.subTest(request=request):
                    self.assertIsNone(panes_exact.admitted(request))


class NavigationAliasRoute(unittest.TestCase):
    def setUp(self):
        import os
        from support import desktop
        from unittest import mock
        import kilix
        import needle_cli
        self.desktop, self.kilix, self.cli = desktop, kilix, needle_cli
        self.mock = mock
        self.env = mock.patch.dict(os.environ, {'KITTY_WINDOW_ID': '300'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_fixture(self, request, data):
        engine = self.mock.Mock()
        engine.complete.side_effect = AssertionError('exact navigation must not load the model')
        with self.mock.patch.object(self.kilix, 'snapshot', return_value=self.kilix.Tree(data)), \
                self.mock.patch.object(self.kilix, 'perform', return_value=None) as perform, \
                self.mock.patch.object(self.kilix, '_run', side_effect=AssertionError('execution disabled')):
            record = self.cli.run_request(engine, request, self.cli.Options(agent=True))
        engine.complete.assert_not_called()
        return record, perform

    def test_aliases_use_current_tab_resolution_without_model_or_desktop_execution(self):
        for verb in NavigationAliases.VERBS:
            data = self.desktop()
            data[0]['tabs'][2]['windows'][2]['title'] = 'Monitor'
            data[0]['tabs'][1]['windows'][1]['title'] = 'Monitor'
            record, perform = self.run_fixture(f'{verb} the monitor pane in this tab', data)
            self.assertEqual(record['status'], 0)
            self.assertEqual(perform.call_args.args[0].commands[0][0],
                             ('focus-window', '--match=id:302'))

    def test_numeric_title_and_id_resolve_to_different_panes(self):
        data = self.desktop()
        data[0]['tabs'][2]['windows'][2]['title'] = '300'
        for request, ident in [('jump to pane 300', 300), ('jump to pane named "300"', 302)]:
            with self.subTest(request=request):
                record, perform = self.run_fixture(request, data)
                self.assertEqual(record['status'], 0)
                self.assertEqual(perform.call_args.args[0].commands[0][0],
                                 ('focus-window', f'--match=id:{ident}'))

    def test_keyword_collision_remains_unresolved_but_an_explicit_title_is_not_relative(self):
        data = self.desktop()
        data[0]['tabs'][2]['windows'][2]['title'] = 'left'
        record, perform = self.run_fixture('jump to the left pane', data)
        self.assertEqual(record['status'], 1)
        self.assertIn('ambiguous', record['items'][0]['reason'])
        perform.assert_not_called()
        record, perform = self.run_fixture('jump to the pane named "left"', data)
        self.assertEqual(record['status'], 0)
        self.assertEqual(perform.call_args.args[0].commands[0][0],
                         ('focus-window', '--match=id:302'))

    def test_duplicate_titles_remain_unresolved(self):
        data = self.desktop()
        for window in data[0]['tabs'][2]['windows'][1:]:
            window['title'] = 'Monitor'
        record, perform = self.run_fixture('take me to the monitor pane', data)
        self.assertEqual(record['status'], 1)
        self.assertIn('matches several panes', record['items'][0]['reason'])
        perform.assert_not_called()


if __name__ == '__main__':
    unittest.main()
