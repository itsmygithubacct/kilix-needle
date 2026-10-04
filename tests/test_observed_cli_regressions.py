import contextlib
import io
import json
import os
import unittest
from unittest import mock
import support
import agents
import actions
import asset
import needle_cli
import panes_exact
import tmux_job


class ObservedRequests(unittest.TestCase):
    def test_exact_typing_preserves_bytes_and_requires_explicit_consent(self):
        command = 'touch /tmp/the-complete-requested-marker'
        requests = [
            f'type the exact command `{command}` in the shell prompt of the pane titled "build" and press Enter',
            f'In this tab, find the pane titled "build" at a shell prompt. Type {command} and press Enter in that pane.',
            f'Type "{command}" and press Enter in pane "build".',
        ]
        engine = mock.Mock()
        with mock.patch.dict(os.environ, KITTY_WINDOW_ID='300'):
            for request in requests:
                with self.subTest(request=request), support.FakeKilix(support.desktop()) as fake:
                    held = needle_cli.run_request(engine, request, needle_cli.Options(agent=True), confirm=lambda _: False)
                    self.assertEqual((held['status'], fake.calls()), (1, []))
                    self.assertIn('confirm_risky=true', held['items'][0]['hint'])
                    record = needle_cli.run_request(engine, request, needle_cli.Options(agent=True, assume_yes=True))
                    self.assertEqual(record['status'], 0, record)
                    self.assertEqual([payload for _argv, payload in fake.calls()], [b'\x05\x15', command.encode(), b'\r'])
                    self.assertTrue(all('KITTY_PTY_BROKER_SESSION=' in argv[1] for argv, _payload in fake.calls()))
        engine.complete.assert_not_called()

    def test_ambiguous_targets_do_not_suggest_waiving_confirmation(self):
        tree = support.desktop()
        tree[0]['tabs'][2]['windows'][1]['title'] = 'build output'
        with mock.patch.dict(os.environ, KITTY_WINDOW_ID='300'), support.FakeKilix(tree) as fake:
            record = needle_cli.run_request(mock.Mock(), 'close the build pane',
                                            needle_cli.Options(agent=True), confirm=lambda _: False)
            self.assertEqual((record['status'], fake.calls()), (1, []))
            self.assertNotIn('hint', record['items'][0])

    def test_unusable_engine_reply_has_recovery_hint_and_executes_no_partial_call(self):
        engine = mock.Mock()
        engine.complete.return_value = {
            'success': False, 'error': 'truncated private-runtime-detail',
            'function_calls': [{'name': 'close_pane', 'arguments': {'pane': 'build'}}],
        }
        with support.FakeKilix(support.desktop()) as fake, mock.patch.object(needle_cli.history, 'record') as history:
            record = needle_cli.run_request(engine, 'arrange the build pane as requested earlier',
                                            needle_cli.Options(agent=True, assume_yes=True))
            self.assertEqual((record['status'], record['items'], fake.calls()), (1, [], []))
            self.assertIn('accepted forms:', record['hint'])
            self.assertNotIn('private-runtime-detail', json.dumps(history.call_args.args[-2]))

    def test_relative_shell_panes_need_no_runtime(self):
        for person in ('I am', 'you are', "I'm", "you're"):
            for side in ('right', 'left', 'above', 'below'):
                prompt = f'Open a new pane directly to the {side} of the pane {person} running in, running a shell'
                self.assertEqual(panes_exact.admitted(prompt)[0],
                                 [{'name': 'open_pane', 'arguments': {'side': side}}])
        for tail in (', running a shell and close tab 2', ', running a shell if the build fails', ', running sudo'):
            self.assertIsNone(panes_exact.admitted('Open a pane to the right of the pane I am running in' + tail))

    def test_launch_tab_and_agent_is_one_launch_without_task(self):
        for verb in ('start', 'run', 'launch'):
            prompt = f'Open a new tab and {verb} an interactive Codex coding-agent session in the directory /tmp/project. Do not give it a task.'
            self.assertEqual(agents.exact_calls(prompt),
                             [{'name': 'agent', 'arguments': {'agent': 'codex', 'dir': '/tmp/project'}}])
        for tail in (' and close tab 2', ' if the build fails'):
            self.assertIsNone(agents.exact_calls('Open a new tab and start codex in /tmp/project' + tail))

    def test_inspecting_a_named_pane_cannot_create_it(self):
        for verb in ('find', 'list', 'show', 'inspect', 'locate'):
            for kind, unit in (('open_pane', 'pane'), ('open_tab', 'tab')):
                prompt = f'{verb} the {unit} titled "bench-target"'
                result = actions.interpret(prompt, [{'name': kind, 'arguments': {'name': 'bench-target'}}])
                self.assertTrue(all(isinstance(item, actions.Refusal) for item in result), result)

    def test_literal_quoting_failure_explains_recovery(self):
        text = 'proof # literal $HOME `never_run` "quoted" ;'
        with self.assertRaisesRegex(ValueError, 'not shell escapes'):
            tmux_job.parse('send "' + text.replace('"', '\\"') + '" to %0')
        self.assertEqual(tmux_job.parse("send '" + text + "' to %0"),
                         {'operation': 'send', 'target': '%0', 'text': text})

    def test_missing_fallback_has_json_recovery_hint_without_installing(self):
        output = io.StringIO()
        with mock.patch.object(needle_cli, 'open_runtime', side_effect=asset.AssetError('needle2 is not installed')) as runtime, \
             mock.patch.object(asset, 'install') as install, \
             mock.patch.object(needle_cli.history, 'record'), contextlib.redirect_stdout(output):
            code = needle_cli.main(['--agent', '--json', 'arrange my workspace'])
        self.assertEqual(code, 2)
        record = json.loads(output.getvalue())
        self.assertEqual(record['status'], 2)
        self.assertEqual(record['items'], [])
        self.assertIn('kilix-needle agents', record['hint'])
        self.assertIn('workflows status', record['hint'])
        self.assertFalse(runtime.call_args.kwargs['may_install'])
        install.assert_not_called()
