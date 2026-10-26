import support
from contextlib import contextmanager
import copy
import hashlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import asset
import commands_needle3
import commands_models
import compact_model


def spec_for(chosen):
    return SimpleNamespace(version=chosen['version'], files=[
        SimpleNamespace(path=name, **item) for name, item in chosen['files'].items()])


class InstalledNeedle3(unittest.TestCase):
    def test_archive_install_failure_is_a_cli_asset_error(self):
        content, first_use, lic = asset._content()
        record = lic.load_determined_records().by_id('needle3-kilix-commands')
        reader, output = Mock(), Mock()
        reader.isatty.return_value = output.isatty.return_value = True
        reader.readline.return_value = lic.typed_agreement_line(record) + '\n'
        output.buffer = io.BytesIO()
        with tempfile.TemporaryDirectory() as root, \
                patch.object(first_use, 'install_with_agreement', side_effect=content.InstallError('broken archive')):
            with self.assertRaisesRegex(asset.AssetError, 'installation failed: broken archive'):
                asset.install(root, stdin=reader, stdout=output,
                              asset_id='needle3-kilix-commands')

    def test_catalog_selects_the_release_files(self):
        chosen = commands_needle3.selection()
        content, _, _ = asset._content()
        spec = content.verified_packaged_catalog().require_asset(commands_needle3.ASSET_ID)
        actual = {item.path: {'bytes': item.bytes, 'sha256': item.sha256} for item in spec.files}
        self.assertEqual(spec.version, chosen['version'])
        for name, item in chosen['files'].items():
            self.assertEqual(actual[name], item)
        self.assertEqual(chosen['files']['libneedle3-cached.so']['sha256'], compact_model.RUST_LIB_SHA256)
        self.assertEqual(chosen['files']['tools.json']['sha256'], compact_model.TOOLS_SHA256)

    def test_no_local_candidate_uses_the_installed_model_and_reaps_it(self):
        chosen = commands_needle3.selection()
        runtime = Mock()
        runtime.__enter__ = Mock(return_value=runtime)
        runtime.__exit__ = Mock(return_value=False)
        with patch.object(asset, '_installed_spec', return_value=(spec_for(chosen), '/fixture/bundle')) as installed, \
                patch.object(compact_model, 'open_runtime', return_value=runtime) as open_model:
            with commands_models.Providers('needle3', root='/fixture/content') as providers:
                self.assertIs(providers.get(), providers.get())
                installed.assert_called_once_with('needle3-kilix-commands', '/fixture/content')
            open_model.assert_called_once_with(Path('/fixture/bundle/candidate.json'),
                                               expected=chosen['files']['candidate.json'])
            runtime.__exit__.assert_called_once()

    def test_catalog_mismatch_refuses_without_opening_any_runtime(self):
        chosen = commands_needle3.selection()
        for member in ('candidate.json', 'weights.cact', 'libneedle3-cached.so', 'tools.json'):
            broken = copy.deepcopy(chosen)
            broken['files'][member]['sha256'] = '0' * 64
            with self.subTest(member=member), \
                    patch.object(asset, '_installed_spec', return_value=(spec_for(broken), '/fixture')), \
                    patch.object(compact_model, 'open_runtime') as open_model:
                with self.assertRaisesRegex(ValueError, 'pins differ'):
                    with commands_needle3.open_runtime():
                        self.fail('opened a mismatched model')
                open_model.assert_not_called()

    def test_missing_asset_does_not_download_or_substitute_another_model(self):
        with patch.object(asset, '_installed_spec', side_effect=asset.AssetError('not installed')), \
                patch.object(asset, 'install') as install, \
                patch('commands_runtime.Runtime') as lfm:
            with commands_models.Providers('needle3') as providers:
                with self.assertRaisesRegex(asset.AssetError, 'not installed'):
                    providers.get()
            install.assert_not_called()
            lfm.assert_not_called()

    def test_changed_manifest_is_rejected_before_loading_native_code(self):
        original = b'{"schema":"kilix-compact-candidate-v1","generation":3}'
        expected = {'bytes': len(original), 'sha256': hashlib.sha256(original).hexdigest()}
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / 'candidate.json'
            manifest.write_bytes(original.replace(b':3', b':2'))
            with patch('compact_model.libengine.LibEngine') as runtime:
                with self.assertRaises(asset.AssetError):
                    with compact_model.open_runtime(manifest, expected=expected):
                        self.fail('loaded a changed manifest')
                runtime.assert_not_called()
