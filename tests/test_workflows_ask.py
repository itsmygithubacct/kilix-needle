"""Ask readiness and selection use isolated files, never live models or receipts."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
import ask
import asset
import commands_selection
import workflows


class PreferenceFixture(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        environment = patch.dict(os.environ, {
            'GPU_TERMINAL_HOME': str(self.base / 'state'),
            'KILIX_CONTENT_ROOT': str(self.base / 'content'),
            'KILIX_LICENSE_RECEIPTS': str(self.base / 'receipts'),
            'KILIX_ASK_COMMANDS_BACKEND': '',
        })
        environment.start()
        self.addCleanup(environment.stop)


class AskSelectionTests(PreferenceFixture):
    def test_reading_default_or_grammar_does_not_create_or_load_anything(self):
        self.assertEqual(commands_selection.resolve(), 'lfm2')
        with patch.object(asset, '_content', side_effect=AssertionError('no assets')):
            result = workflows.status(model='grammar')
        self.assertTrue(result['ready'])
        self.assertTrue(result['grammar_ready'])
        self.assertEqual(result['required_assets'], [])
        self.assertEqual(list(self.base.iterdir()), [])

    def test_explicit_environment_saved_default_precedence(self):
        commands_selection.save('needle3')
        self.assertEqual(commands_selection.resolve(), 'needle3')
        with patch.dict(os.environ, {'KILIX_ASK_COMMANDS_BACKEND': 'lfm2'}):
            self.assertEqual(commands_selection.resolve(), 'lfm2')
            self.assertEqual(commands_selection.resolve('grammar'), 'grammar')
        self.assertEqual(commands_selection.preference_path().stat().st_mode & 0o777, 0o600)

    def test_invalid_preference_refuses_but_explicit_choice_can_repair_it(self):
        commands_selection.save('grammar')
        path = commands_selection.preference_path()
        for raw in ('[]', '{}', 'x' * 4097, json.dumps({'schema': commands_selection.SCHEMA, 'backend': 'unknown'})):
            path.write_text(raw)
            with self.assertRaises(ValueError):
                commands_selection.resolve()
            self.assertFalse(workflows.status()['ready'])
            self.assertEqual(commands_selection.resolve('lfm2'), 'lfm2')
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(ValueError):
            commands_selection.resolve()
        path.unlink()
        path.symlink_to(self.base / 'missing')
        with self.assertRaises(OSError):
            commands_selection.resolve()
        workflows.select('grammar')
        self.assertFalse(path.is_symlink())
        self.assertEqual(commands_selection.resolve(), 'grammar')

    def test_nonterminal_grammar_setup_is_saved_and_used_by_ask(self):
        plan = {'status': 0, 'items': [{'outcome': 'would', 'step': {'kind': 'app'}}]}
        with patch.object(asset, '_content', side_effect=AssertionError('no model acquisition')), \
                patch('commands_models.Providers.get', side_effect=AssertionError('no model')), \
                patch('compact_runner.run', return_value=plan), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workflows.main(['setup', '--model', 'grammar']), 0)
            self.assertEqual(ask.main(['--plan', '--json', 'open calculator']), 0)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['interpretation'], 'grammar')
        self.assertFalse((self.base / 'content').exists())
        self.assertFalse((self.base / 'receipts').exists())

    def test_real_catalog_missing_models_need_their_own_agreement_without_writes(self):
        for backend, asset_id in commands_selection.ASSETS.items():
            with self.subTest(backend=backend):
                result = workflows.status(model=backend)
                self.assertEqual(result['required_assets'], [asset_id])
                self.assertEqual(result['assets'][0]['state'], 'agreement-required')
                self.assertFalse(result['ready'])
        self.assertEqual(list(self.base.iterdir()), [])


class AskAssetTests(PreferenceFixture):
    def setUp(self):
        super().setUp()
        self.root = self.base / 'content'
        self.content, self.first_use, self.lic = asset._content()
        self.payload = b'verified synthetic model and runtime'
        member = SimpleNamespace(path='payload', bytes=len(self.payload),
                                 sha256=hashlib.sha256(self.payload).hexdigest())
        self.specs = {name: SimpleNamespace(asset_id=name, version='fixture-v1', files=[member])
                      for name in commands_selection.ASSETS.values()}
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(self.content, 'verified_packaged_catalog',
            return_value=SimpleNamespace(require_asset=lambda name: self.specs[name])))
        self.patches.enter_context(patch.object(commands_selection, 'release', return_value={
            'version': 'fixture-v1', 'files': {'payload': {'bytes': member.bytes, 'sha256': member.sha256}}}))
        self.patches.enter_context(patch.object(commands_selection, 'require_platform'))
        self.agreement = self.patches.enter_context(patch.object(self.first_use, 'needs_agreement', return_value=False))

    def install(self, backend):
        path = self.root / 'assets' / commands_selection.ASSETS[backend] / 'payload'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)
        return path

    def test_each_backend_checks_its_own_bytes_and_not_the_other_models(self):
        self.install('needle3')
        self.assertFalse(workflows.status()['ready'])
        self.assertTrue(workflows.status(model='needle3')['ready'])
        path = self.install('lfm2')
        with patch.object(workflows, '_model_status', side_effect=AssertionError('legacy is separate')):
            self.assertTrue(workflows.status()['ready'])
        path.write_bytes(b'corrupt')
        self.assertEqual(workflows.status()['assets'][0]['state'], 'corrupt')
        self.assertTrue(workflows.status(model='needle3')['ready'])

    def test_ready_bytes_never_substitute_for_consent(self):
        for backend in commands_selection.ASSETS:
            self.install(backend)
            self.agreement.return_value = True
            self.assertEqual(workflows.status(model=backend)['assets'][0]['state'], 'agreement-required')
            with self.assertRaises(workflows.WorkflowError):
                workflows.select(backend)
        self.assertFalse(commands_selection.preference_path().exists())

    def test_selection_only_changes_after_exact_backend_is_ready(self):
        commands_selection.save('grammar')
        with self.assertRaises(workflows.WorkflowError):
            workflows.select('needle3')
        self.assertEqual(commands_selection.resolve(), 'grammar')
        self.install('needle3')
        with patch.object(asset, 'install', side_effect=AssertionError('no acquisition')):
            result = workflows.select('needle3')
        self.assertEqual(result['backend'], 'needle3')
        self.assertEqual(commands_selection.resolve(), 'needle3')

    def test_release_pin_or_platform_mismatch_cannot_be_selected(self):
        self.install('lfm2')
        self.specs[commands_selection.ASSETS['lfm2']].version = 'wrong'
        self.assertIn('pins differ', workflows.status()['errors'][0])
        with patch.object(commands_selection, 'require_platform', side_effect=ValueError('unsupported CPU')):
            self.assertIn('unsupported CPU', workflows.status()['errors'])
        self.assertFalse(commands_selection.preference_path().exists())

    def test_setup_installs_only_selected_asset_then_saves_it(self):
        from test_workflows import Terminal
        for backend in commands_selection.ASSETS:
            self.agreement.return_value = True
            def acquire(root, **kwargs):
                self.assertEqual(kwargs['asset_id'], commands_selection.ASSETS[backend])
                self.install(backend)
                self.agreement.return_value = False
            with patch.object(asset, 'install', side_effect=acquire) as install:
                result = workflows.setup(model=backend, stdin=Terminal(), stdout=Terminal())
            self.assertTrue(result['ready'])
            self.assertEqual(install.call_count, 1)
            self.assertEqual(commands_selection.resolve(), backend)


if __name__ == '__main__':
    unittest.main()
