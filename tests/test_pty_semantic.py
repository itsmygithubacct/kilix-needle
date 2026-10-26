"""Structured semantic reads stay read-only and label their observation scope."""
from pathlib import Path
import json
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pty_backend
import pty_cli
import pty_job


class SemanticRoutes(unittest.TestCase):
    def test_explicit_grammar_labels_raw_observation_and_plural_foreground(self):
        self.assertEqual(pty_job.parse('which session last showed raw marker build-finished-ok'),
                         {'operation': 'marker', 'marker': 'build-finished-ok'})
        self.assertEqual(pty_job.parse('show foreground processes of session "owned"'),
                         {'operation': 'foreground', 'id': 'owned'})
        with self.assertRaises(pty_job.Refused):
            pty_job.parse('which session most recently printed build-finished-ok')

    def test_structured_marker_has_complete_census_route_and_no_timeout_override(self):
        action = pty_job.structured({'operation': 'marker', 'marker': 'build-finished-ok'})
        self.assertEqual(pty_backend.argv_for(action),
                         ['pty', 'marker', 'build-finished-ok', '--json'])
        for bad in ({'operation': 'marker', 'marker': 'x', 'id': 'selected'},
                    {'operation': 'marker', 'marker': 'x', 'timeout_seconds': 1},
                    {'operation': 'marker', 'marker': 'x\n'}):
            with self.assertRaises(pty_job.Refused):
                pty_job.structured(bad)

    def test_controller_read_and_external_cli_validate_semantic_schema(self):
        marker = {'schema': 'kilix.pty.semantic/v1', 'operation': 'marker_occurrences',
                  'marker': 'token', 'match_mode': 'raw_pty_bytes',
                  'criterion': 'broker_observed_output', 'result': 'unique',
                  'winner_session_id': 'a', 'candidate_ids': ['a', 'b'], 'sessions': []}
        controller = mock.Mock()
        controller.read_cli.return_value = {'code': 0, 'stdout': json.dumps(marker), 'stderr': ''}
        record = pty_cli.run({'operation': 'marker', 'marker': 'token'}, controller=controller)
        self.assertEqual(record['status'], 0)
        controller.read_cli.assert_called_once_with(['marker', 'token', '--json'])
        with mock.patch.object(pty_backend, 'call', return_value=pty_backend.Reply('ok', 0, marker)):
            record = pty_cli.run({'operation': 'marker', 'marker': 'token'})
        self.assertEqual(record['status'], 0)
        changed = {**marker, 'criterion': 'application_print_time'}
        with self.assertRaises(ValueError):
            pty_backend.checked({'operation': 'marker', 'marker': 'token'}, changed, 0)

    def test_foreground_is_plural_and_bound_to_exact_id(self):
        action = pty_job.structured({'operation': 'foreground', 'id': 'owned'})
        self.assertEqual(pty_backend.argv_for(action), ['pty', 'foreground', 'owned', '--json'])
        response = {'schema': 'kilix.pty.semantic/v1',
                    'operation': 'foreground_process_sample', 'id': 'owned',
                    'state': 'complete', 'foreground_processes': [{}, {}]}
        self.assertIs(pty_backend.checked(action, response, 0), response)
        with self.assertRaises(ValueError):
            pty_backend.checked(action, {**response, 'id': 'other'}, 0)


if __name__ == '__main__':
    unittest.main()
