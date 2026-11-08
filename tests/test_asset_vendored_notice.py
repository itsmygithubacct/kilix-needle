"""The installed app reads LFM2's terms from its own vendored kilix-content.

Owner determination 2026-11-07: LFM2's terms are a notice. kilix-needle
loads the catalog and licence records from third_party/kilix-content, so
that copy must carry the same delivery and notice record as the image. A
review found the copy one delivery behind with the old agreement record:
the image's notice receipt did not cover it, and `install --commands`
demanded a typed agreement. Nothing here is mocked except the terminal and
the final download.
"""
import support  # noqa: F401
import io
import tempfile
import unittest
from unittest.mock import Mock, patch

import asset

DELIVERY = 'lfm2-kilix-commands-20261106-v9'
RECORD = 'e4d08e2c53ab514e1e2ca2a812c64895519487d707c441703978bd0f245b5016'


def terminal(answer):
    reader, output = Mock(), Mock()
    reader.isatty.return_value = output.isatty.return_value = True
    reader.readline.return_value = answer + '\n'
    output.buffer = io.BytesIO()
    written = []
    output.write.side_effect = written.append
    return reader, output, written


class VendoredNotice(unittest.TestCase):
    def setUp(self):
        self.content, self.first_use, self.lic = asset._content()
        self.catalog = self.content.verified_packaged_catalog()
        self.records = self.lic.load_determined_records()

    def record(self, asset_id):
        return self.first_use.license_record_for(self.catalog.require_asset(asset_id), self.records)

    def test_the_vendored_catalog_is_the_image_delivery_with_the_notice_record(self):
        spec = self.catalog.require_asset('lfm2-kilix-commands')
        self.assertEqual(spec.version, DELIVERY)
        record = self.record('lfm2-kilix-commands')
        self.assertEqual(record.digest, RECORD)
        self.assertEqual(record.expected_decision, 'record')
        self.assertEqual(self.record('lfm3-kilix-commands').expected_decision, 'accept')

    def install(self, asset_id, answer):
        reader, output, written = terminal(answer)
        with tempfile.TemporaryDirectory() as root, \
                patch.object(self.first_use, 'install_with_agreement') as install:
            try:
                asset.install(root, stdin=reader, stdout=output, asset_id=asset_id)
            except asset.AssetError as error:
                return install, ''.join(written), output.buffer.getvalue(), error
        return install, ''.join(written), output.buffer.getvalue(), None

    def test_install_shows_the_vendored_terms_and_takes_no_typed_agreement(self):
        install, prompt, screen, error = self.install('lfm2-kilix-commands', 'y')
        self.assertIsNone(error)
        self.assertIn(b'version: ' + DELIVERY.encode(), screen)
        self.assertIn(b'decision: informational', screen)
        self.assertIn('notice and take no agreement', prompt)
        self.assertNotIn('To accept', prompt)
        self.assertIsNone(install.call_args.kwargs['typed_text'])
        # LFM3 keeps its agreement: a bare yes is refused.
        install, prompt, _screen, error = self.install('lfm3-kilix-commands', 'y')
        self.assertIn('To accept, type exactly', prompt)
        self.assertRegex(str(error), 'not accepted')
        install.assert_not_called()

    def test_a_notice_receipt_like_the_image_writes_covers_the_vendored_asset(self):
        from kilix_content.receipt import _CATALOG_SHA256, release_digest
        spec = self.catalog.require_asset('lfm2-kilix-commands')
        record = self.record('lfm2-kilix-commands')
        with tempfile.TemporaryDirectory() as root:
            store = self.lic.ReceiptStore(root)
            self.assertTrue(self.first_use.needs_agreement(spec, records=self.records, store=store))
            store.write(self.lic.receipt_from_agreement(
                record, self.lic.capture_agreement(record), manifest_digest=spec.manifest_digest,
                catalogue_digest=_CATALOG_SHA256, release_digest=release_digest()))
            self.assertFalse(self.first_use.needs_agreement(spec, records=self.records, store=store))


if __name__ == '__main__':
    unittest.main()
