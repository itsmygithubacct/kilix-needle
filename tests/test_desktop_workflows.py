import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import desktop_cli
import desktop_control
import desktop_grammar
import desktop_job as job
import mcp_server
import setup_surfaces
from test_desktop import Engine, FakeX11, c


class GrammarTests(unittest.TestCase):
    def test_all_action_families_and_literal_binding(self):
        cases = [
            ('list windows', c('window', action='list')),
            ('focus window "Notes 88"', c('window', action='focus', target='title:Notes 88')),
            ('move window id 123 to 40, 70', c('window', action='move', target='id:123', x=40, y=70)),
            ('resize the current window to 800x600', c('window', action='resize', target='active', width=800, height=600)),
            ('minimize the active window', c('window', action='minimize', target='active')),
            ('maximize window "Notes"', c('window', action='maximize', target='title:Notes')),
            ('restore the active window', c('window', action='restore', target='active')),
            ('close window "Notes"', c('window', action='close', target='title:Notes')),
            ('move the pointer to 140, 250', c('pointer', action='move', x=140, y=250)),
            ('click', c('pointer', action='click', button='left')),
            ('right-click at 10, 20', c('pointer', action='click', button='right', x=10, y=20)),
            ('double click', c('pointer', action='double_click', button='left')),
            ('drag to 150, 260', c('pointer', action='drag', button='left', x=150, y=260)),
            ('type "x; then close window \\"Notes\\""', c('keyboard', action='type', text='x; then close window "Notes"')),
            ('press Control Shift S', c('keyboard', action='press', keys='ctrl+shift+s')),
            ('scroll left five steps', c('scroll', direction='left', steps=5)),
            ('wait 0.125 seconds', c('wait', ms=125)),
        ]
        for request, expected in cases:
            with self.subTest(request=request):
                self.assertEqual(desktop_grammar.parse(request), [expected])

    def test_hybrid_is_lazy_and_falls_back_once_for_the_whole_request(self):
        engine = Engine([c('window', action='focus', target='title:Notes')])
        with patch('desktop_cli.open_runtime', side_effect=AssertionError('must stay lazy')):
            from needle_cli import LazyRuntime
            with LazyRuntime(lambda: desktop_cli.open_runtime()) as lazy:
                result = desktop_cli.run('click', engine=lazy, parser_mode='hybrid', parse_only=True)
                self.assertEqual(result['parser'], 'grammar')
        result = desktop_cli.run('focus Notes', engine=engine, parser_mode='hybrid', parse_only=True)
        self.assertEqual(result['status'], 0)
        self.assertEqual(result['parser'], 'needle')
        self.assertEqual(engine.count, 1)
        result = desktop_cli.run('focus Notes', engine=engine, parser_mode='grammar', parse_only=True)
        self.assertEqual(result['status'], 1)
        self.assertEqual(engine.count, 1)
        engine.calls = [c('keyboard', action='press', keys='Return')]
        result = desktop_cli.run('click then press Return', engine=engine, parser_mode='needle', parse_only=True)
        self.assertEqual(result['status'], 1)

    def test_unsafe_or_unaccounted_requests_never_get_a_partial_plan(self):
        for text in ('do not click', 'click if ready', 'type "hello" then install an app',
                     'click then click the blue button', 'click 1, 2, 3', 'scroll down 21 steps',
                     'wait 3000 ms then wait 3000 ms then wait 1 ms',
                     'focus window "Notes" and "Other"', 'press Return then erase my files'):
            with self.subTest(text=text):
                backend = FakeX11()
                result = desktop_cli.run(text, parser_mode='grammar', backend=backend, execute=True)
                self.assertEqual(result['status'], 1)
                self.assertEqual(backend.commands, [])

    def test_execution_failure_does_not_fall_back(self):
        backend = FakeX11(); backend.fail_command = 'windowactivate'
        engine = Engine([])
        result = desktop_cli.run('focus window "Browser"', parser_mode='hybrid', engine=engine, backend=backend, execute=True)
        self.assertNotEqual(result['status'], 0)
        self.assertEqual(engine.count, 0)


class InterfaceTests(unittest.TestCase):
    def invoke(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = desktop_control.main(argv)
        return code, json.loads(output.getvalue())

    def test_typed_literal_and_modes(self):
        text = '--yes; $(literal) "naïve" '
        code, record = self.invoke(['--parse-only', '--json', 'keyboard', 'type', '--text='+text])
        self.assertEqual(code, 0)
        self.assertEqual(record['actions'], [c('keyboard', action='type', text=text)])
        code, record = self.invoke(['--yes', 'window', 'focus', '--parse-only'])
        self.assertEqual(code, 1)
        self.assertEqual(record['actions'], [])
        code, record = self.invoke(['pointer', 'click', '--x', '2', '--parse-only'])
        self.assertEqual(code, 1)

    def test_json_and_file_text_fail_closed_without_trimming(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'input'
            path.write_text('hello\n')
            code, _ = self.invoke(['keyboard', 'type', '--text-file', str(path), '--parse-only'])
            self.assertEqual(code, 1)
            path.write_text('[{"name":"wait","arguments":{"ms":1,"ms":2}}]')
            code, _ = self.invoke(['--parse-only','batch',str(path)])
            self.assertEqual(code, 1)
            path.write_text(json.dumps([c('wait',ms=10), c('wait',ms=20)]))
            code, record = self.invoke(['batch',str(path),'--parse-only'])
            self.assertEqual(code, 0)
            self.assertEqual(len(record['actions']), 2)

    def test_mcp_structured_and_grammar_do_not_load_model(self):
        def forbidden(*_): raise AssertionError('model must not load')
        server = mcp_server.Server(forbidden, tools='desktop', desktop_parser='grammar')
        with patch('desktop_cli.X11', lambda **_: FakeX11()):
            for arguments in ({'request':'press Return', 'confirm_risky':True},
                              {'actions':[c('keyboard', action='press', keys='Return')], 'confirm_risky':True}):
                result = server.call_tool('kilix_desktop_act', arguments)
                self.assertFalse(result['isError'], result)
            with self.assertRaises(ValueError):
                server.call_tool('kilix_desktop_act', {'request':'click','actions':[], 'confirm_risky':True})
            result = server.call_tool('kilix_desktop_act', {'actions':[c('wait',ms=1)]})
            self.assertTrue(result['isError'])
        server.close()

    def test_typed_command_install_and_undo_own_only_their_link(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory)/'kilix-needle'
            with patch.object(setup_surfaces, 'BIN', binary):
                code, _ = setup_surfaces.setup(only=['desktop-command'])
                self.assertEqual(code, 0)
                desktop = binary.with_name('kilix-desktop')
                self.assertTrue(desktop.is_symlink())
                setup_surfaces.setup(only=['desktop-command'], undo=True)
                self.assertFalse(desktop.exists())
                desktop.write_text('owner data')
                code, _ = setup_surfaces.setup(only=['desktop-command'])
                self.assertEqual(code, 1)
                self.assertEqual(desktop.read_text(), 'owner data')


if __name__ == '__main__':
    unittest.main()
