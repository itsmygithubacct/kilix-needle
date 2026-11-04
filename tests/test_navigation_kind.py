"""Reference kinds survive navigation paraphrases through checked pure plans."""
import os
import unittest
from unittest import mock

import actions
import domain_bridge
import kilix
import needle_cli
import panes_exact
from support import tab, window


VERBS = ('go to', 'focus', 'switch to', 'jump to', 'take me to', 'switch over to')


def topology(title='archive', remote='elsewhere', program='bash'):
    caller = window(510, 'caller')
    caller['neighbors'] = {'left': [51], 'right': [52], 'top': [53], 'bottom': [54]}
    local = tab(5, 'origin', [caller] + [window(i, 'neighbor-' + str(i)) for i in range(511, 515)]
                + [window(520, title, program=program)])
    local['groups'] = [dict(id=i - 460, windows=[i]) for i in range(511, 515)]
    return [dict(id=1, is_focused=True, tabs=[local, tab(6, 'viewed', [window(610, remote)], active=True)])]


class NavigationKind(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {'KITTY_WINDOW_ID': '510'})
        patch.start()
        self.addCleanup(patch.stop)

    def action(self, request):
        found = panes_exact.admitted(request)
        self.assertIsNotNone(found, request)
        calls, canonical = found
        checked = domain_bridge.validate(canonical, calls)
        self.assertEqual(checked['refusals'], [], request)
        [action] = actions.interpret(canonical, calls)
        self.assertIsInstance(action, actions.Action)
        return action

    def test_both_orders_preserve_relative_current_and_normalized_references(self):
        refs = {'left': ('left', 511), 'right': ('right', 512), 'up': ('above', 513),
                'down': ('below', 514), 'next': ('next', 511), 'previous': ('previous', 520),
                'current': ('current', 510), 'here': ('current', 510)}
        for verb in VERBS:
            for word, (ref, dest) in refs.items():
                for body in (f'pane {word}', f'the {word} pane'):
                    for suffix in ('', ' in this tab'):
                        request = f'{verb} {body}{suffix}'
                        with self.subTest(request=request):
                            action = self.action(request)
                            expected = {'pane': ref}
                            if suffix:
                                expected['pane_scope'] = 'current'
                            self.assertEqual(action.args, expected)
                            step = kilix.resolve(action, kilix.Tree(topology()))
                            self.assertEqual(step.commands, ((("focus-window", f"--match=id:{dest}"), None),))

    def test_bare_direction_keeps_title_and_program_collision_refusal(self):
        for verb in VERBS:
            for word in ('left', 'right', 'above', 'below', 'next', 'previous'):
                for by_program in (False, True):
                    request = f'{verb} pane {word} in this tab'
                    with self.subTest(request=request, by_program=by_program):
                        data = topology(title='archive' if by_program else word,
                                        program=word if by_program else 'bash')
                        action = self.action(request)
                        self.assertEqual(action.args['pane'], word)
                        with self.assertRaises(kilix.KilixError):
                            kilix.resolve(action, kilix.Tree(data))

    def test_explicit_direction_current_and_numeric_titles_stay_literal(self):
        for verb in VERBS:
            for name in ('left', 'above', 'current', 'next', '510'):
                for body in (f'pane named "{name}"', f'pane "{name}"'):
                    request = f'{verb} {body} in this tab'
                    with self.subTest(request=request):
                        action = self.action(request)
                        self.assertEqual(action.args, {'pane': 'name:' + name, 'pane_scope': 'current'})
                        step = kilix.resolve(action, kilix.Tree(topology(title=name)))
                        self.assertEqual(step.commands[0][0], ('focus-window', '--match=id:520'))

    def test_current_precedence_and_id_are_distinct_from_titles(self):
        for verb in VERBS:
            for body, ref, dest, title in [('pane current', 'current', 510, 'current'),
                                          ('pane 510', 'id:510', 510, '510'),
                                          ('pane:510', 'id:510', 510, '510'),
                                          ('the 510 pane', 'name:510', 520, '510')]:
                with self.subTest(verb=verb, body=body):
                    action = self.action(f'{verb} {body}')
                    self.assertEqual(action.args, {'pane': ref})
                    self.assertEqual(kilix.resolve(action, kilix.Tree(topology(title=title))).commands[0][0],
                                     ('focus-window', f'--match=id:{dest}'))

    def test_current_tab_filters_collision_before_resolution(self):
        for verb in VERBS:
            data = topology(remote='left')
            with self.subTest(verb=verb):
                action = self.action(f'{verb} pane left in this tab')
                self.assertEqual(kilix.resolve(action, kilix.Tree(data)).commands[0][0],
                                 ('focus-window', '--match=id:511'))
                with self.assertRaises(kilix.KilixError):
                    kilix.resolve(self.action(f'{verb} pane left'), kilix.Tree(data))

    def test_find_then_preserves_kind(self):
        for verb in ('focus', 'go to', 'switch to'):
            with self.subTest(verb=verb):
                action = self.action(f'find pane left in this tab and {verb} it')
                self.assertEqual(action.args, {'pane': 'left', 'pane_scope': 'current'})

    def test_extrema_malformed_numbers_and_tails_decline_consistently(self):
        for verb in VERBS:
            for body in ('pane top', 'pane bottom', 'pane -1', 'pane 1.5', 'pane 1234567',
                         'pane left in tab 2', 'pane left in this tab and close it'):
                with self.subTest(verb=verb, body=body):
                    self.assertIsNone(panes_exact.admitted(f'{verb} {body}'))

    def test_exact_route_uses_checked_kind_without_inference_or_execution(self):
        for verb in VERBS:
            engine = mock.Mock()
            engine.complete.side_effect = AssertionError('inference disabled')
            with mock.patch.object(kilix, 'snapshot', return_value=kilix.Tree(topology())), \
                    mock.patch.object(kilix, 'perform', side_effect=AssertionError('execution disabled')) as perform, \
                    mock.patch.object(kilix, '_run', side_effect=AssertionError('desktop disabled')):
                record = needle_cli.run_request(engine, f'{verb} pane left in this tab',
                                               needle_cli.Options(agent=True, dry_run=True))
            self.assertEqual(record['status'], 0)
            self.assertEqual(record['items'][0]['args'], {'pane': 'left', 'pane_scope': 'current'})
            self.assertEqual(record['items'][0]['outcome'], 'would')
            engine.complete.assert_not_called()
            perform.assert_not_called()


if __name__ == '__main__':
    unittest.main()
