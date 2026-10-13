"""Noncaller mutations bind the requested identity and verify their effects."""
import copy
import os
import unittest
from unittest import mock

from support import desktop
from actions import Action, Refusal, interpret
import kilix
import panes_exact


class TargetAdmission(unittest.TestCase):
    def action(self, prompt, name, arguments):
        values = interpret(prompt, [{'name': name, 'arguments': arguments}])
        self.assertEqual(len(values), 1)
        self.assertIsInstance(values[0], Action)
        return values[0]

    def test_model_cannot_drop_explicit_pane_id(self):
        action = self.action('make pane 200 wider by 5 cells', 'resize_pane',
                             {'direction': 'wider', 'amount': 5})
        self.assertEqual(action.args['pane'], 'id:200')

    def test_model_cannot_drop_stable_tab_id(self):
        action = self.action('rename tab id 10 to build-output', 'rename_tab',
                             {'name': 'build-output'})
        self.assertEqual(action.args['tab'], 'id:10')

    def test_tab_containing_pane_is_an_explicit_target(self):
        action = self.action('rename the tab containing pane 200 to build-output',
                             'rename_tab', {'name': 'build-output'})
        self.assertEqual(action.args['tab'], 'pane:200')

    def test_new_title_is_not_mistaken_for_target_syntax(self):
        action = self.action('rename this tab to tab 2', 'rename_tab', {'name': 'tab 2'})
        self.assertNotIn('tab', action.args)
        action = self.action('rename tab id 10 to tab id 20', 'rename_tab', {'name': 'tab id 20'})
        self.assertEqual(action.args['tab'], 'id:10')

    def test_conflicting_target_ids_are_refused(self):
        values = interpret('make pane 200 wider by 5 cells, not pane 300',
                           [{'name': 'resize_pane', 'arguments':
                             {'direction': 'wider', 'amount': 5}}])
        self.assertTrue(any(isinstance(value, Refusal) for value in values))

    def test_exact_forms_do_not_load_a_model(self):
        for prompt in ('rename tab id 10 to build-output',
                       'make pane 200 wider by 5 cells',
                       'resize pane 200 narrower by 3'):
            with self.subTest(prompt=prompt):
                self.assertIsNotNone(panes_exact.admitted(prompt))

    def test_conditions_are_not_dropped_by_exact_forms(self):
        for prompt in ('rename tab id 10 to build-output if tests pass',
                       'do not make pane 200 wider by 5 cells',
                       'make pane 200 wider by 5 cells and close tab 2'):
            with self.subTest(prompt=prompt):
                self.assertIsNone(panes_exact.admitted(prompt))


class TargetResolution(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {'KITTY_WINDOW_ID': '300'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.data = desktop()
        for tab in self.data[0]['tabs']:
            for pane in tab['windows']:
                pane.update(columns=80, lines=30)

    def tree(self):
        return kilix.Tree(copy.deepcopy(self.data))

    def test_stable_id_differs_from_position(self):
        tree = kilix.Tree(desktop())
        self.assertEqual(tree.tab('id:10')['id'], 10)
        self.assertEqual(tree.tab('2')['id'], 20)
        self.assertEqual(tree.tab('pane:200')['id'], 20)

    def test_noncaller_rename_and_resize_match_target_ids(self):
        cases = [(Action('rename_tab', {'tab': 'id:10', 'name': 'output'}), 'id:10'),
                 (Action('resize_pane', {'pane': 'id:200', 'direction': 'wider', 'amount': 5}), 'id:200')]
        for action, target in cases:
            with self.subTest(action=action):
                step = kilix.resolve(action, self.tree())
                self.assertIn('--match=' + target, step.commands[0][0])

    def test_missing_identity_does_not_fall_back_to_caller(self):
        for action in (Action('rename_tab', {'tab': 'id:999', 'name': 'output'}),
                       Action('resize_pane', {'pane': 'id:999', 'direction': 'wider', 'amount': 5})):
            with self.subTest(action=action), self.assertRaises(kilix.KilixError):
                kilix.resolve(action, kilix.Tree(desktop()))

    def test_receipt_requires_observed_exact_effect(self):
        action = Action('resize_pane', {'pane': 'id:200', 'direction': 'wider', 'amount': 5})
        step = kilix.resolve(action, self.tree())
        def mutate(argv, data):
            self.data[0]['tabs'][1]['windows'][0]['columns'] += 5
        with mock.patch.object(kilix, 'snapshot', side_effect=self.tree), \
                mock.patch.object(kilix, '_run', side_effect=mutate) as run:
            receipt = kilix.perform(step)
        self.assertTrue(receipt['completion_verified'])
        self.assertEqual((receipt['target_id'], receipt['observed']), (200, 85))
        self.assertEqual(run.call_count, 1)

    def test_clamped_resize_is_not_repeated_or_claimed_complete(self):
        step = kilix.resolve(Action('resize_pane', {'pane': 'id:200', 'direction': 'wider', 'amount': 5}), self.tree())
        with mock.patch.object(kilix, 'snapshot', side_effect=self.tree), \
                mock.patch.object(kilix, '_run') as run, mock.patch.object(kilix.time, 'sleep'), \
                self.assertRaisesRegex(kilix.KilixError, 'exact effect'):
            kilix.perform(step)
        self.assertEqual(run.call_count, 1)

    def test_nested_split_scaled_increment_converges_to_exact_size(self):
        step = kilix.resolve(Action('resize_pane', {'pane': 'id:200', 'direction': 'wider', 'amount': 5}), self.tree())
        def mutate(argv, data=None):
            amount = int(next(a.split('=')[1] for a in argv if a.startswith('--increment=')))
            self.data[0]['tabs'][1]['windows'][0]['columns'] += round(amount / 2)
        with mock.patch.object(kilix, 'snapshot', side_effect=self.tree), \
                mock.patch.object(kilix, '_run', side_effect=mutate), mock.patch.object(kilix.time, 'sleep'):
            receipt = kilix.perform(step)
        self.assertEqual(receipt['observed'], 85)
        self.assertLessEqual(receipt['resize_corrections'], 4)

    def test_recycled_target_refuses_before_mutation(self):
        step = kilix.resolve(Action('rename_tab', {'tab': 'id:10', 'name': 'output'}), self.tree())
        self.data[0]['tabs'][0]['windows'][0]['pid'] += 1
        with mock.patch.object(kilix, 'snapshot', side_effect=self.tree), \
                mock.patch.object(kilix, '_run') as run, \
                self.assertRaisesRegex(kilix.KilixError, 'no mutation attempted'):
            kilix.perform(step)
        run.assert_not_called()

    def test_collateral_title_change_fails_receipt(self):
        step = kilix.resolve(Action('rename_tab', {'tab': 'id:10', 'name': 'output'}), self.tree())
        def mutate(argv, data):
            self.data[0]['tabs'][0]['title'] = 'output'
            self.data[0]['tabs'][2]['title'] = 'collateral'
        with mock.patch.object(kilix, 'snapshot', side_effect=self.tree), \
                mock.patch.object(kilix, '_run', side_effect=mutate) as run, \
                mock.patch.object(kilix.time, 'sleep'), \
                self.assertRaisesRegex(kilix.KilixError, 'neighboring titles'):
            kilix.perform(step)
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
