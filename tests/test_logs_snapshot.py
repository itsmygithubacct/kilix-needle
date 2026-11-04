"""Real bounded subprocess snapshots preserve safe host refusal diagnostics."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401
from needle_logs import cli, resolve


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.host = self.root / 'kilix'
        patcher = mock.patch.dict(os.environ, {'KILIX_NEEDLE_KILIX': str(self.host)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def response(self, payload, code=1, stderr='private fixture must not escape'):
        self.host.write_text('#!' + sys.executable + '\nimport sys\n'
            'assert sys.argv[1:] == ["panes", "list", "--json"]\n'
            f'print({payload!r})\nprint({stderr!r}, file=sys.stderr)\n'
            f'raise SystemExit({code})\n')
        self.host.chmod(0o700)

    def failure(self, expected):
        with self.assertRaises(resolve.ResolveError) as raised:
            resolve.snapshot()
        self.assertEqual(raised.exception.code, expected)
        self.assertNotIn('private fixture', str(raised.exception))
        return str(raised.exception)

    def test_shared_caller_diagnostic_survives_cli_without_raw_output(self):
        self.response(json.dumps({'schema': 'kilix.panes.error/v1',
            'code': 'caller_context_unverified', 'message': 'private fixture'}))
        message = self.failure('caller_context_unverified')
        self.assertIn('--no-daemon', message)
        with mock.patch.object(cli, 'Index') as index:
            result = cli.read({'session': '23', 'operation': 'brief'})
        index.assert_not_called()
        self.assertEqual(result['events'], [])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['exit_status'], 1)
        self.assertEqual(result['errors'][0]['code'], 'caller_context_unverified')
        self.assertNotIn('private fixture', json.dumps(result))

    def test_unavailable_connection_has_safe_recovery_advice(self):
        self.response(json.dumps({'schema': 'kilix.panes.error/v1',
                                  'code': 'pane_context_unavailable'}))
        self.assertIn('authenticated', self.failure('pane_context_unavailable'))

    def test_unknown_malformed_and_old_host_errors_stay_generic(self):
        for payload in ('', 'private fixture', '[]', 'null',
                        '{"schema":"kilix.panes.error/v1","code":[]}',
                        '{"schema":"kilix.panes.error/v1","code":"private fixture"}',
                        '{"schema":"unknown","code":"caller_context_unverified"}'):
            with self.subTest(payload=payload):
                self.response(payload)
                self.failure('snapshot_unavailable')

    def test_failed_command_never_returns_success_looking_stale_panes(self):
        self.response(json.dumps({'schema': 'kilix.panes/v1', 'panes': [{'pane_id': 23}]}))
        self.failure('snapshot_unavailable')

    def test_zero_exit_error_document_cannot_be_used_as_a_snapshot(self):
        self.response(json.dumps({'schema': 'kilix.panes.error/v1',
                                  'code': 'caller_context_unverified'}), code=0)
        self.failure('snapshot_invalid')

    def test_valid_snapshot_remains_unchanged(self):
        tree = {'schema': 'kilix.panes/v1', 'panes': [{'pane_id': 23}]}
        self.response(json.dumps(tree), code=0, stderr='')
        self.assertEqual(resolve.snapshot(), tree)


if __name__ == '__main__':
    unittest.main()
