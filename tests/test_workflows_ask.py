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
        # Nothing chosen and no model on this machine: the grammar, read from
        # the real catalog and receipt store without creating either.
        self.assertEqual(commands_selection.choice(), ('grammar', 'not-installed'))
        self.assertEqual(commands_selection.resolve(), 'grammar')
        self.assertFalse(commands_selection.installed())
        self.assertEqual(list(self.base.iterdir()), [])
        with patch.object(asset, '_content', side_effect=AssertionError('no assets')):
            result = workflows.status(model='grammar')
        self.assertTrue(result['ready'])
        self.assertTrue(result['grammar_ready'])
        self.assertEqual(result['required_assets'], [])
        self.assertEqual(list(self.base.iterdir()), [])

    def test_explicit_environment_saved_default_precedence(self):
        commands_selection.save('needle3')
        self.assertEqual(commands_selection.choice(), ('needle3', 'saved'))
        with patch.dict(os.environ, {'KILIX_ASK_COMMANDS_BACKEND': 'lfm2'}):
            self.assertEqual(commands_selection.choice(), ('lfm2', 'environment'))
            self.assertEqual(commands_selection.choice('grammar'), ('grammar', 'option'))
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
        with self.assertRaises(commands_selection.PreferenceError) as raised:
            commands_selection.resolve()
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertIn('workflows select --model', str(raised.exception))
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

    def test_unusable_preference_names_the_repair_and_is_never_rewritten(self):
        path = commands_selection.preference_path()
        path.parent.mkdir(parents=True)
        raw = json.dumps({'schema': commands_selection.SCHEMA, 'backend': 'needle2'}).encode() + b'\n'
        path.write_bytes(raw)
        inode = path.stat().st_ino
        with self.assertRaises(commands_selection.PreferenceError) as raised:
            commands_selection.resolve()
        for words in ('--commands-backend', 'kilix ask --model', 'workflows select --model'):
            self.assertIn(words, str(raised.exception))
        result = workflows.status()
        self.assertFalse(result['ready'])
        self.assertIn('workflows select --model', result['errors'][0])
        with self.assertRaises(commands_selection.PreferenceError):
            workflows.setup(stdin=io.StringIO(), stdout=io.StringIO())
        error = io.StringIO()
        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as refused:
            ask.main(['--plan', '--json', 'open calculator'])
        self.assertEqual(refused.exception.code, 2)
        self.assertIn('workflows select --model', error.getvalue())
        # An explicit or environment choice bypasses the file and leaves it as it is.
        plan = {'status': 0, 'items': [{'outcome': 'would', 'step': {'kind': 'app'}}]}
        with patch.object(asset, '_content', side_effect=AssertionError('no model acquisition')), \
                patch('commands_models.Providers.get', side_effect=AssertionError('no model')), \
                patch('compact_runner.run', return_value=plan), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ask.main(['--commands-backend', 'grammar', '--plan', '--json', 'open calculator']), 0)
            with patch.dict(os.environ, {'KILIX_ASK_COMMANDS_BACKEND': 'grammar'}):
                self.assertEqual(ask.main(['--plan', '--json', 'open calculator']), 0)
                self.assertTrue(workflows.setup(stdin=io.StringIO(), stdout=io.StringIO())['ready'])
        self.assertEqual((path.read_bytes(), path.stat().st_ino), (raw, inode))
        workflows.select('grammar')
        self.assertEqual(commands_selection.resolve(), 'grammar')

    def test_environment_or_saved_grammar_setup_saves_nothing(self):
        path = commands_selection.preference_path()
        with patch.object(asset, '_content', side_effect=AssertionError('no assets')):
            with patch.dict(os.environ, {'KILIX_ASK_COMMANDS_BACKEND': 'grammar'}):
                result = workflows.setup(stdin=io.StringIO(), stdout=io.StringIO())
                self.assertEqual((result['ready'], result['backend']), (True, 'grammar'))
                self.assertFalse(path.exists())
                commands_selection.save('needle3')
                saved = (path.read_bytes(), path.stat().st_ino)
                self.assertTrue(workflows.setup(stdin=io.StringIO(), stdout=io.StringIO())['ready'])
                self.assertEqual((path.read_bytes(), path.stat().st_ino), saved)
            commands_selection.save('grammar')
            saved = (path.read_bytes(), path.stat().st_ino)
            self.assertEqual(workflows.setup(stdin=io.StringIO(), stdout=io.StringIO())['backend'], 'grammar')
            self.assertEqual((path.read_bytes(), path.stat().st_ino), saved)

    def test_a_missing_model_is_never_chosen_by_default_and_an_explicit_one_still_is(self):
        with patch.object(asset, '_content', side_effect=AssertionError('unreadable catalog')):
            self.assertEqual(commands_selection.choice(), ('grammar', 'not-installed'))
            self.assertEqual(commands_selection.choice('lfm2'), ('lfm2', 'option'))
        commands_selection.save('lfm2')
        with patch.object(commands_selection, 'installed', side_effect=AssertionError('not consulted')):
            self.assertEqual(commands_selection.choice(), ('lfm2', 'saved'))
        result = workflows.status()
        self.assertEqual((result['backend'], result['selection'], result['offer']), ('lfm2', 'saved', None))
        self.assertFalse(result['ready'])

    def test_status_and_choice_name_the_stand_in_grammar_and_the_offer(self):
        result = workflows.status()
        self.assertEqual((result['ready'], result['backend'], result['selection'], result['offer']),
                         (True, 'grammar', 'not-installed', 'lfm2'))
        self.assertEqual(result['required_assets'], [])
        self.assertIn('not installed', result['detail'])
        self.assertIn('workflows setup --model lfm2', result['detail'])
        chosen = workflows.status(model='grammar')
        self.assertEqual((chosen['selection'], chosen['offer']), ('option', None))
        self.assertNotIn('not installed', chosen['detail'])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workflows.main(['choice']), 0)
        self.assertEqual(output.getvalue(), 'grammar not-installed\n')
        commands_selection.save('grammar')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workflows.main(['choice']), 0)
        self.assertEqual(output.getvalue(), 'grammar saved\n')
        commands_selection.preference_path().write_text('{}')
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(workflows.main(['choice']), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('workflows select --model', error.getvalue())
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), ['state'])

    def test_ask_without_a_choice_or_model_reads_with_the_grammar_and_says_so_on_a_miss(self):
        plan = {'status': 0, 'items': [{'outcome': 'would', 'step': {'kind': 'app'}}]}
        with patch('commands_models.Providers.get', side_effect=AssertionError('no model')), \
                patch('compact_runner.run', return_value=plan), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(ask.main(['--plan', '--json', 'open calculator']), 0)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['interpretation'], 'grammar')
        self.assertEqual(error.getvalue(), '')
        self.assertFalse((self.base / 'content').exists())
        self.assertFalse((self.base / 'receipts').exists())
        for options, hinted in (([], True), (['--commands-backend', 'grammar'], False)):
            with patch.object(ask, 'cli_jobs', return_value=(None, contextlib.ExitStack())), \
                    patch.object(ask, 'ask', return_value={'status': 1, 'route': {'stage': 0, 'job': 'none'}}), \
                    patch.object(ask, 'render', return_value=''), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(ask.main([*options, '--yes', 'paint the fence']), ask.NO_JOB)
            self.assertEqual('LFM2 command model is not installed' in error.getvalue(), hinted)

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
        self.specs = {name: SimpleNamespace(asset_id=name, install_name=name, version='fixture-v1', files=[member])
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
        spec = self.specs[commands_selection.ASSETS[backend]]
        path = self.root / 'assets' / spec.install_name / 'payload'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)
        return path

    def test_versioned_status_requires_the_new_bytes_and_preserves_the_old_model(self):
        old = self.install('lfm2')
        spec = self.specs[commands_selection.ASSETS['lfm2']]
        spec.install_name = spec.asset_id + '--' + hashlib.sha256(self.payload).hexdigest()
        self.assertEqual(workflows.status(model='lfm2')['assets'][0]['state'], 'missing')
        # Nothing chosen: an earlier delivery's bytes do not select the model.
        fallback = workflows.status()
        self.assertEqual((fallback['ready'], fallback['backend'], fallback['selection'], fallback['offer']),
                         (True, 'grammar', 'not-installed', 'lfm2'))
        self.assertFalse(old.parent.with_name(spec.install_name).exists())
        new = self.install('lfm2')
        result = workflows.status()
        self.assertTrue(result['ready'])
        self.assertEqual((result['backend'], result['selection'], result['offer']), ('lfm2', 'installed', None))
        self.assertEqual(result['assets'][0]['path'], str(new.parent))
        self.assertEqual(old.read_bytes(), self.payload)

    def test_each_backend_checks_its_own_bytes_and_not_the_other_models(self):
        self.install('needle3')
        self.assertFalse(workflows.status(model='lfm2')['ready'])
        # Another model's bytes never make LFM2 the default.
        self.assertEqual(commands_selection.choice(), ('grammar', 'not-installed'))
        self.assertTrue(workflows.status(model='needle3')['ready'])
        path = self.install('lfm2')
        with patch.object(workflows, '_model_status', side_effect=AssertionError('legacy is separate')):
            self.assertTrue(workflows.status()['ready'])
        self.assertEqual(commands_selection.choice(), ('lfm2', 'installed'))
        # Same size, other bytes: still the default, and reported corrupt, never run.
        path.write_bytes(bytes(len(self.payload)))
        self.assertEqual(workflows.status()['assets'][0]['state'], 'corrupt')
        # A member of the wrong size is not an installed model at all.
        path.write_bytes(b'corrupt')
        self.assertEqual(commands_selection.choice(), ('grammar', 'not-installed'))
        self.assertEqual(workflows.status(model='lfm2')['assets'][0]['state'], 'corrupt')
        self.assertTrue(workflows.status(model='needle3')['ready'])

    def test_the_installed_default_needs_a_receipt_a_real_directory_and_a_supported_platform(self):
        path = self.install('lfm2')
        self.assertTrue(commands_selection.installed())
        self.agreement.return_value = True
        self.assertEqual(commands_selection.choice(), ('grammar', 'not-installed'))
        self.agreement.return_value = False
        with patch.object(commands_selection, 'require_platform', side_effect=ValueError('unsupported CPU')):
            self.assertEqual(commands_selection.choice(), ('grammar', 'not-installed'))
        moved = path.parent.with_name('elsewhere')
        path.parent.rename(moved)
        path.parent.symlink_to(moved)
        self.assertEqual(commands_selection.choice(), ('grammar', 'not-installed'))
        path.parent.unlink()
        moved.rename(path.parent)
        path.unlink()
        path.symlink_to(moved.with_name('payload-elsewhere'))
        self.assertEqual(commands_selection.choice(), ('grammar', 'not-installed'))
        self.assertFalse(commands_selection.installed('grammar'))
        self.assertFalse(commands_selection.preference_path().exists())

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
        self.assertIn('pins differ', workflows.status(model='lfm2')['errors'][0])
        self.assertEqual(commands_selection.choice(), ('grammar', 'not-installed'))
        with patch.object(commands_selection, 'require_platform', side_effect=ValueError('unsupported CPU')):
            self.assertIn('unsupported CPU', workflows.status(model='lfm2')['errors'])
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

    def setup_acquiring(self, backend=None, *, model=None, complete=True):
        """Run setup at a terminal; the fake installer accepts and installs the asset it is asked for."""
        from test_workflows import Terminal
        backends = {asset_id: name for name, asset_id in commands_selection.ASSETS.items()}
        self.agreement.return_value = True
        def acquire(root, **kwargs):
            if backend is not None:
                self.assertEqual(kwargs['asset_id'], commands_selection.ASSETS[backend])
            if complete:
                self.install(backends[kwargs['asset_id']])
                self.agreement.return_value = False
        with patch.object(asset, 'install', side_effect=acquire):
            return workflows.setup(model=model, stdin=Terminal(), stdout=Terminal())

    def test_implicit_setup_without_the_model_stays_on_grammar_and_acquires_nothing(self):
        from test_workflows import Terminal
        with patch.object(asset, 'install', side_effect=AssertionError('no acquisition')):
            result = workflows.setup(stdin=Terminal(), stdout=Terminal())
        self.assertEqual((result['ready'], result['backend'], result['selection']),
                         (True, 'grammar', 'not-installed'))
        self.assertFalse(commands_selection.preference_path().exists())
        self.assertFalse(self.root.exists())

    def test_implicit_setup_with_the_model_installed_verifies_lfm2_without_saving(self):
        from test_workflows import Terminal
        self.install('lfm2')
        with patch.object(asset, 'install', side_effect=AssertionError('no acquisition')):
            result = workflows.setup(stdin=Terminal(), stdout=Terminal())
        self.assertEqual((result['ready'], result['backend'], result['selection']),
                         (True, 'lfm2', 'installed'))
        self.assertFalse(commands_selection.preference_path().exists())

    def test_environment_only_setup_never_creates_or_changes_the_preference(self):
        path = commands_selection.preference_path()
        with patch.dict(os.environ, {'KILIX_ASK_COMMANDS_BACKEND': 'needle3'}):
            self.assertEqual(self.setup_acquiring('needle3')['backend'], 'needle3')
            self.assertFalse(path.exists())
            commands_selection.save('grammar')
            saved = (path.read_bytes(), path.stat().st_ino)
            self.assertTrue(self.setup_acquiring('needle3')['ready'])
            self.assertEqual((path.read_bytes(), path.stat().st_ino), saved)
        self.assertEqual(commands_selection.resolve(), 'grammar')

    def test_explicit_setup_saves_over_environment_and_saved_choices(self):
        commands_selection.save('grammar')
        with patch.dict(os.environ, {'KILIX_ASK_COMMANDS_BACKEND': 'needle3'}):
            self.assertEqual(self.setup_acquiring('lfm2', model='lfm2')['backend'], 'lfm2')
        self.assertEqual(commands_selection.resolve(), 'lfm2')

    def test_setup_that_does_not_become_ready_keeps_the_saved_choice(self):
        path = commands_selection.preference_path()
        commands_selection.save('grammar')
        saved = (path.read_bytes(), path.stat().st_ino)
        with self.assertRaises(workflows.WorkflowError):
            self.setup_acquiring('needle3', model='needle3', complete=False)
        with patch.dict(os.environ, {'KILIX_ASK_COMMANDS_BACKEND': 'lfm2'}), self.assertRaises(workflows.WorkflowError):
            self.setup_acquiring('lfm2', complete=False)
        with self.assertRaises(workflows.WorkflowError):
            workflows.select('lfm2')
        self.assertEqual((path.read_bytes(), path.stat().st_ino), saved)
        self.assertEqual(commands_selection.resolve(), 'grammar')


if __name__ == '__main__':
    unittest.main()
