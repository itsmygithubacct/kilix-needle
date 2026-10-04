"""Redundant visibility wording preserves the exact bounded file query."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support
import files_job
import mcp_server


class VisibilityQualifiers(unittest.TestCase):
    def test_default_qualifiers_preserve_scope_name_and_filters(self):
        requests = (
            'find files starting with "ledger-" in here',
            'find files named "ledger-*" in "/tmp/project space Ω" modified yesterday',
            'find pdf files in Downloads modified last 7 days',
            'find text "visible regular files only" in here',
            'list largest files in research',
        )
        qualifiers = (
            '(visible regular files only)',
            '(visible regular files only, skip hidden and symlinks)',
            '(visible regular files only, skip hidden entries and symlinks)',
            '(skip hidden files and symlinks)',
        )
        for request in requests:
            for qualifier in qualifiers:
                with self.subTest(request=request, qualifier=qualifier):
                    self.assertEqual(files_job.parse(request + ' ' + qualifier),
                                     files_job.parse(request))

    def test_other_constraints_are_not_discarded(self):
        request = 'find files starting with "ledger-" in here'
        for suffix in (
            '(include hidden entries and symlinks)',
            '(visible regular files only, follow symlinks)',
            '(visible regular files only, skip hidden and symlinks, only empty files)',
            '(visible regular files only) and delete them',
            '(skip hidden and symlinks) in /tmp/other',
            '(skip hidden and symlinks) (only empty files)',
            'and delete them (skip hidden and symlinks)',
        ):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                files_job.parse(request + ' ' + suffix)
        with self.assertRaisesRegex(ValueError, 'one file scope'):
            files_job.parse('find files under /tmp/one named ledger in /tmp/two '
                            '(skip hidden and symlinks)')

    def test_quoted_qualifier_text_stays_literal(self):
        query = files_job.parse('find files named "(visible regular files only)" '
                               'in "/tmp/project (skip hidden and symlinks)"')[0]
        self.assertEqual(query.args['name'], '(visible regular files only)')
        self.assertEqual(query.args['scope'], '/tmp/project (skip hidden and symlinks)')

    def test_mcp_observed_wording_finds_only_matching_visible_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'notes').mkdir()
            (root / '.hidden').mkdir()
            for name in ('ledger-café.cfg', 'notes/ledger-copy [2].cfg',
                         'copy-ledger-decoy.cfg', '.hidden/ledger-hidden.cfg'):
                (root / name).write_text('fixture\n')
            (root / 'ledger-link.cfg').symlink_to(root / 'ledger-café.cfg')
            before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
            server = mcp_server.Server(lambda *_: self.fail('model loaded'))
            self.addCleanup(server.close)
            with patch('history.record'):
                reply = server.call_tool('kilix_files_read', {
                    'request': 'find files starting with "ledger-" in here '
                               '(visible regular files only, skip hidden entries and symlinks)',
                    'cwd': str(root), 'limit': 100})
            self.assertFalse(reply['isError'], reply)
            observation = reply['structuredContent']['observation']
            self.assertTrue(observation['complete'])
            self.assertEqual(sorted(r['relative_path'] for r in observation['results']),
                             ['ledger-café.cfg', 'notes/ledger-copy [2].cfg'])
            self.assertEqual(observation['skipped'], {'hidden': 1, 'symlink': 1})
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob('*') if p.is_file()})
            json.dumps(reply).encode('utf-8')
