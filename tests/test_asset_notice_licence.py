"""First use of a notice licence takes no agreement (owner determination 2026-11-07)."""
import support  # noqa: F401
import io
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import asset


def terminal(answer):
    reader, output = Mock(), Mock()
    reader.isatty.return_value = output.isatty.return_value = True
    reader.readline.return_value = answer + '\n'
    output.buffer = io.BytesIO()
    written = []
    output.write.side_effect = written.append
    return reader, output, written


class NoticeLicenceInstall(unittest.TestCase):
    def run_install(self, answer, decision):
        content, first_use, lic = asset._content()
        record = SimpleNamespace(id='lfm2-kilix-commands', expected_decision=decision)
        reader, output, written = terminal(answer)
        with tempfile.TemporaryDirectory() as root, \
                patch.object(first_use, 'license_record_for', return_value=record), \
                patch.object(first_use, 'present_asset', return_value=b'the full terms\n'), \
                patch.object(lic, 'typed_agreement_line', return_value='accept lfm2-kilix-commands'), \
                patch.object(first_use, 'install_with_agreement') as install:
            try:
                asset.install(root, stdin=reader, stdout=output, asset_id='lfm2-kilix-commands')
            except asset.AssetError as error:
                return install, ''.join(written), error
        return install, ''.join(written), None

    def test_a_notice_is_confirmed_and_records_no_typed_agreement(self):
        install, shown, error = self.run_install('y', 'record')
        self.assertIsNone(error)
        self.assertIn('notice and take no agreement', shown)
        self.assertNotIn('To accept', shown)
        self.assertIsNone(install.call_args.kwargs['typed_text'])

    def test_declining_a_notice_writes_nothing(self):
        install, _, error = self.run_install('n', 'record')
        self.assertRegex(str(error), 'not installed; nothing was written')
        install.assert_not_called()

    def test_an_agreement_licence_still_needs_its_exact_line(self):
        install, shown, error = self.run_install('y', 'accept')
        self.assertRegex(str(error), 'not accepted')
        install.assert_not_called()
        install, shown, error = self.run_install('accept lfm2-kilix-commands', 'accept')
        self.assertIsNone(error)
        self.assertEqual(install.call_args.kwargs['typed_text'], 'accept lfm2-kilix-commands')


if __name__ == '__main__':
    unittest.main()
