"""An uncovered asset is described by what its licence takes.

Owner determination 2026-11-07: LFM2's terms are a notice, shown and shipped,
with no agreement taken or recorded. A seat review found that the runtime
path (commands_runtime.inspect_bundle -> asset._installed_spec) still said
the LFM2 licence "has not been accepted". A notice licence's missing receipt
is terms not yet shown; only a licence that takes agreement can be "not
accepted". The catalog and records are the real vendored ones; only the
receipt store is a fresh empty directory.
"""
import support  # noqa: F401
import io
import tempfile
import unittest
from unittest.mock import patch

import asset
import needle_cli


class UncoveredWording(unittest.TestCase):
    def setUp(self):
        self.content, self.first_use, self.lic = asset._content()
        self.catalog = self.content.verified_packaged_catalog()
        self.records = self.lic.load_determined_records()

    def decision(self, asset_id):
        spec = self.catalog.require_asset(asset_id)
        return self.first_use.license_record_for(spec, self.records).expected_decision

    def refusal(self, call):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as root, \
                patch.object(self.lic.ReceiptStore, 'shared',
                             return_value=self.lic.ReceiptStore(store)):
            with self.assertRaises(asset.AssetError) as caught:
                call(root)
        return str(caught.exception)

    def test_the_notice_licence_is_not_called_unaccepted(self):
        self.assertEqual(self.decision('lfm2-kilix-commands'), 'record')
        message = self.refusal(lambda root: asset._installed_spec('lfm2-kilix-commands', root))
        self.assertEqual(message, 'the lfm2-kilix-commands licence terms have not been shown; '
                                  'install it with: kilix-needle install --commands')
        self.assertNotIn('accept', message)

    def test_a_licence_that_takes_agreement_is_still_unaccepted(self):
        self.assertEqual(self.decision('lfm3-kilix-commands'), 'accept')
        message = self.refusal(lambda root: asset._installed_spec('lfm3-kilix-commands', root))
        self.assertTrue(message.startswith('the lfm3-kilix-commands licence has not been accepted; '),
                        message)

    def test_the_engine_message_follows_its_own_record(self):
        expected = {'accept': 'licence has not been accepted',
                    'record': 'licence terms have not been shown'}[self.decision(asset.ASSET_ID)]
        message = self.refusal(asset.from_installed)
        self.assertIn(f'the {asset.ASSET_ID} {expected}; install it with: kilix-needle install',
                      message)

    def test_first_use_still_leads_into_install_on_either_wording(self):
        for message in ('the needle2 licence terms have not been shown; install it with: x',
                        'the needle2 licence has not been accepted; install it with: x'):
            with self.subTest(message=message), \
                    patch.object(asset, 'from_installed',
                                 side_effect=[asset.AssetError(message), 'engine']), \
                    patch.object(asset, 'install') as install, \
                    patch.object(needle_cli, '_offer_tuning'), \
                    patch('sys.stdin') as stdin, patch('sys.stdout', new_callable=io.StringIO):
                stdin.isatty.return_value = True
                with patch('sys.stdout.isatty', return_value=True, create=True):
                    args = type('Args', (), {'engine': None, 'root': None})()
                    with patch.dict('os.environ', {}, clear=False) as env:
                        env.pop('KILIX_NEEDLE_ENGINE', None)
                        self.assertEqual(needle_cli._image(args, may_install=True), 'engine')
                install.assert_called_once_with(None)


if __name__ == '__main__':
    unittest.main()
