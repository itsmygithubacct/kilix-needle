import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import desktop_cli
import desktop_job as job
import desktop_codec
from desktop_x11 import X11
import mcp_server


def c(name, **args):
    return {"name": name, "arguments": args}


class Engine:
    label = "test"
    def __init__(self, calls): self.calls, self.count = calls, 0
    def reset(self): pass
    def complete(self, _request):
        self.count += 1
        return {"function_calls": self.calls}


SNAPSHOT = dict(display=":999", width=1280, height=720, active=11,
                windows=[dict(id=11, title="Editor", pid=100, wm_class='"editor"'),
                         dict(id=22, title="Browser", pid=200, wm_class='"browser"')])


class FakeX11(X11):
    def __init__(self):
        self.display = ":999"
        self.deadline = time.monotonic()+30
        self.commands = []
        self.focus = 11
        self.fail_command = None
    def snapshot(self): return copy.deepcopy(SNAPSHOT)
    def active(self): return self.focus
    def identity(self, wid):
        return copy.deepcopy(next(w for w in SNAPSHOT["windows"] if w["id"] == wid))
    def geometry(self, wid): return dict(x=20, y=30, width=640, height=480)
    def xdo(self, *args, text=None, **kwargs):
        self.commands.append((args, text))
        if args[0] == self.fail_command:
            self.deadline = time.monotonic()-10
            raise job.DesktopError("injected failure")
        if args[0] == "windowactivate": self.focus = args[-1]
        if args[0] == "help": return "windowquit windowstate"
        return ""


class ContractTests(unittest.TestCase):
    def test_plan_encoding_preserves_actions_and_still_checks_intent(self):
        actions = [c('scroll', direction='down', steps=3)]
        encoded = [c('desktop_plan', plan=json.dumps(actions))]
        self.assertEqual(desktop_codec.decode(encoded), actions)
        self.assertEqual(desktop_codec.decode([]), [])
        engine = Engine(encoded); engine.translate = desktop_codec.decode
        self.assertEqual(desktop_cli.run('scroll down 3 steps', engine=engine, parse_only=True)['status'], 0)
        self.assertEqual(desktop_cli.run('scroll up 3 steps', engine=engine, parse_only=True)['status'], 1)

    def test_plan_encoding_rejects_ambiguous_or_malformed_frames(self):
        valid = c('desktop_plan', plan=json.dumps([c('wait', ms=100)]))
        for proposed in ([valid,valid], [c('wait',ms=100)], [c('desktop_plan',plan='[]')],
                         [c('desktop_plan',plan='{}')], [c('desktop_plan',plan='not JSON')],
                         [c('desktop_plan',plan='[{"name":"wait","name":"keyboard","arguments":{"ms":100}}]')]):
            with self.subTest(proposed=proposed), self.assertRaises(job.DesktopError):
                desktop_codec.decode(proposed)

    def test_literal_text_and_clause_order(self):
        calls = [c("keyboard", action="type", text='x; then close "Editor"'), c("keyboard", action="press", keys="Return")]
        request = 'type ' + json.dumps('x; then close "Editor"') + ' then press Return'
        self.assertEqual(job.validate(request, calls), calls)
        with self.assertRaises(job.DesktopError): job.validate(request, calls[::-1])

    def test_full_request_not_a_substring(self):
        close = [c("window", action="close", target="active")]
        for request in ("do not close the active window", "someone says close the active window",
                        "close the active window and delete my files", "close the active window tomorrow",
                        "close the active window if it is saved", "close the active window 17",
                        "close the active window then install an app", 'type "close the active window"'):
            with self.subTest(request=request), self.assertRaises(job.DesktopError): job.validate(request, close)

    def test_no_inferred_visual_target(self):
        for request in ("click the Save button", 'click "Save"', "click the blue icon", "click twice", "click here and erase everything"):
            with self.subTest(request=request), self.assertRaises(job.DesktopError):
                job.validate(request, [c("pointer", action="click")])

    def test_no_implicit_submit_or_target(self):
        calls = [c("keyboard", action="type", text="hello")]
        for request in ('type "hello" then press Return', 'type "hello" into Browser', 'type "hello" and submit it'):
            with self.subTest(request=request), self.assertRaises(job.DesktopError): job.validate(request, calls)
        self.assertEqual(job.validate('type "hello"', calls), calls)

    def test_keys_are_allowlisted_single_chords(self):
        for value in ("--window", "ctrl+wat", "a+b", "ctrl", "ctrl+ctrl+c", "Return;exec", "keydown+ctrl"):
            with self.subTest(value=value), self.assertRaises(job.DesktopError): job.key_chord(value)
        self.assertEqual(job.key_chord("Control Shift S"), "ctrl+shift+s")
        self.assertEqual(job.validate("press Delete", [c("keyboard", action="press", keys="Delete")])[0]["arguments"]["keys"], "Delete")

    def test_argument_binding_and_wrong_extra_call(self):
        expected = c("pointer", action="move", x=100, y=250)
        self.assertEqual(job.validate("move the mouse to 100, 250", [expected]), [expected])
        for wrong in ([c("pointer", action="move", x=250, y=100)], [expected, expected],
                      [c("window", action="move", target="active", x=100, y=250)]):
            with self.assertRaises(job.DesktopError): job.validate("move the mouse to 100, 250", wrong)

    def test_numeric_types_limits_and_unknown_fields(self):
        bad = [c("pointer", action="move", x=True, y=3), c("pointer", action="move", x=-1, y=3),
               c("window", action="close", target="active", shell="rm"), c("wait", ms=3001),
               c("scroll", direction="down", steps=21), c("keyboard", action="type", text="hello\n"),
               c("keyboard", action="press", keys="Return", text="hello")]
        for action in bad:
            with self.subTest(action=action), self.assertRaises(job.DesktopError): job.normalize([action])

    def test_whole_title_binding(self):
        calls = [c("window", action="focus", target="title:Notes")]
        self.assertEqual(job.validate('focus window "Notes"', calls), calls)
        for request in ('focus window "Notes" and "Browser"', 'focus window "Notes 2"', 'focus window "Browser"'):
            with self.assertRaises(job.DesktopError): job.validate(request, calls)
        with self.assertRaises(job.DesktopError):
            job.validate("focus the active window", [c("window", action="focus", target="title:active")])

    def test_dimensions_and_spelled_numbers(self):
        job.validate('resize the active window to 800x600', [c("window", action="resize", target="active", width=800, height=600)])
        job.validate('scroll down five steps', [c("scroll", direction="down", steps=5)])
        job.validate('wait 0.5 seconds', [c("wait", ms=500)])
        with self.assertRaises(job.DesktopError): job.validate('scroll down five steps', [c("scroll", direction="down", steps=3)])

    def test_total_pause_limit_also_applies_to_structured_actions(self):
        calls = [c('wait', ms=3000), c('wait', ms=3000)]
        self.assertEqual(job.normalize(calls), calls)
        backend = FakeX11()
        with patch('desktop_x11.time.sleep') as sleep:
            result = desktop_cli.run(None, calls=calls + [c('wait', ms=1)], backend=backend, execute=True)
        self.assertEqual(result['outcome'], 'refused')
        sleep.assert_not_called()


class ExecutionTests(unittest.TestCase):
    def test_ambiguous_title_and_missing_id(self):
        backend = FakeX11(); snap = backend.snapshot(); snap['windows'][1]['title'] = 'Editor'
        for target in ('title:Editor', 'id:999'):
            with self.assertRaises(job.DesktopError): backend.resolve(target, snap, 11)

    def test_preview_does_not_execute(self):
        backend = FakeX11()
        result = desktop_cli.run(None, calls=[c("keyboard", action="type", text="literal")], backend=backend)
        self.assertEqual(result['outcome'], 'planned'); self.assertEqual(backend.commands, [])

    def test_entire_plan_resolves_before_first_side_effect(self):
        backend = FakeX11()
        result = desktop_cli.run(None, calls=[c("keyboard", action="type", text="literal"), c("window", action="close", target="id:999")], backend=backend, execute=True)
        self.assertEqual(result['status'], 1); self.assertEqual(backend.commands, [])

    def test_focus_then_type_uses_resolved_target_and_stdin(self):
        backend = FakeX11()
        calls = [c("window", action="focus", target="title:Browser"), c("keyboard", action="type", text="--help; $(nothing)")]
        result = desktop_cli.run(None, calls=calls, backend=backend, execute=True)
        self.assertEqual(result['status'], 0)
        self.assertEqual(backend.focus, 22)
        self.assertEqual(backend.commands[-1][1], "--help; $(nothing)")
        self.assertEqual(backend.commands[-1][0][-2:], ('--file', '-'))
        self.assertFalse(any(r['application_completion_verified'] for r in result['results']))

    def test_focus_change_blocks_keyboard(self):
        backend = FakeX11()
        plan = backend.plan([c("keyboard", action="type", text="hello")], backend.snapshot())
        backend.focus = 22
        result = backend.execute(plan)
        self.assertEqual(result['status'], 1); self.assertEqual(backend.commands, [])

    def test_identity_change_blocks_action(self):
        backend = FakeX11()
        plan = backend.plan([c("window", action="close", target="active")], backend.snapshot())
        backend.commands.clear()
        with patch.object(backend, 'identity', return_value=dict(id=11, title='Other', pid=999, wm_class='"other"')):
            result = backend.execute(plan)
        self.assertEqual(result['status'], 1); self.assertEqual(backend.commands, [])

    def test_failure_stops_following_action_and_drag_releases(self):
        backend = FakeX11(); backend.fail_command = 'mousemove'
        calls = [c("pointer", action="drag", x=100, y=200), c("keyboard", action="type", text="never")]
        result = desktop_cli.run(None, calls=calls, backend=backend, execute=True)
        self.assertEqual(result['status'], 1)
        self.assertEqual([v[0][0] for v in backend.commands], ['mousedown', 'mousemove', 'mouseup'])
        self.assertEqual(result['results'][0]['status'], 'uncertain')

    def test_close_is_graceful_and_no_implicit_retarget(self):
        backend = FakeX11()
        result = desktop_cli.run(None, calls=[c("window", action="close", target="active")], backend=backend, execute=True)
        self.assertEqual(result['status'], 0)
        self.assertIn(('windowquit', 11), [a for a, _ in backend.commands])
        with self.assertRaises(job.DesktopError):
            backend.plan([c("window", action="close", target="active"), c("keyboard", action="press", keys="Return")], backend.snapshot())

    def test_unavailable_graceful_close_has_no_fallback(self):
        backend = FakeX11()
        with patch.object(backend, 'xdo', return_value='windowclose windowkill'):
            with self.assertRaises(job.DesktopError): backend.plan([c("window", action="close", target="active")], backend.snapshot())

    def test_unknown_mouse_coordinates_fail_before_execution(self):
        backend = FakeX11()
        result = desktop_cli.run(None, calls=[c("pointer", action="click", x=1280, y=10)], backend=backend, execute=True)
        self.assertEqual(result['status'], 1); self.assertEqual(backend.commands, [])

    def test_pause_stops_if_previous_action_consumed_the_deadline(self):
        backend = FakeX11()
        plan = backend.plan([c('pointer', action='move', x=100, y=200), c('wait', ms=3000)], backend.snapshot())
        with patch.object(backend, 'xdo', side_effect=lambda *a, **k: setattr(backend, 'deadline', time.monotonic()+1)), \
             patch('desktop_x11.time.sleep') as sleep:
            result = backend.execute(plan)
        self.assertEqual(result['outcome'], 'partial')
        self.assertEqual([r['status'] for r in result['results']], ['submitted', 'blocked'])
        sleep.assert_not_called()


class InterfaceTests(unittest.TestCase):
    def test_evaluation_timeout_is_not_a_correct_empty_refusal(self):
        import contextlib
        import importlib.util
        import io
        from libengine import LibEngineError
        path = Path(__file__).resolve().parents[1]/'experiments/desktop-actions/evaluate.py'
        spec = importlib.util.spec_from_file_location('desktop_evaluator_test', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        runtime = Mock()
        runtime.translate = lambda calls: calls
        runtime.complete.side_effect = [LibEngineError('worker timed out'),
                                       {'function_calls': [c('wait', ms=100)]}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); data = root/'data.jsonl'; out = root/'out'
            rows = [dict(query='never click', answers=[]),
                    dict(query='wait 100 ms', answers=[c('wait', ms=100)])]
            data.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            with patch('sys.argv', ['evaluate', '--data', str(data), '--out', str(out)]), \
                 patch('desktop_cli.open_runtime', return_value=contextlib.nullcontext(runtime)), \
                 contextlib.redirect_stdout(io.StringIO()):
                module.main()
            totals = json.loads((out/'report.json').read_text())['totals']
            self.assertEqual(totals['raw_exact'], 1)
            self.assertEqual(totals['negative_raw_exact'], 0)
            self.assertEqual(totals['invalid_or_runtime_error'], 1)
        runtime.engine.close.assert_called_once()
        runtime.engine.start.assert_called_once()

    def test_model_schema_mismatch_does_not_load_or_fall_back(self):
        image = Mock()
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'model.json').write_text(json.dumps(dict(schema='wrong', tools_sha256='wrong')))
            with patch('desktop_cli.asset.installed_library', return_value=image), self.assertRaises(job.DesktopError):
                desktop_cli.open_runtime(directory)
        image.close.assert_called_once()

    def test_model_runtime_mismatch_does_not_load_or_fall_back(self):
        image = Mock(sha256='a'*64)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'model.json').write_text(json.dumps(dict(
                schema=job.VERSION, codec=desktop_codec.DEFAULT, tools_sha256=desktop_cli.schema_sha(), runtime_sha256='b'*64)))
            with patch('desktop_cli.asset.installed_library', return_value=image), self.assertRaises(job.DesktopError):
                desktop_cli.open_runtime(directory)
        image.close.assert_called_once()

    def test_model_byte_mismatch_does_not_fall_back(self):
        image = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path/'model.cact').write_bytes(b'wrong-model')
            (path/'model.json').write_text(json.dumps(dict(schema=job.VERSION, codec=desktop_codec.DEFAULT, tools_sha256=desktop_cli.schema_sha(), sha256='0'*64, bytes=11)))
            with patch('desktop_cli.asset.installed_library', return_value=image), self.assertRaises(desktop_cli.asset.AssetError):
                desktop_cli.open_runtime(directory)
        image.close.assert_called_once()

    def test_no_model_for_ineligible_request(self):
        engine = Engine([c("window", action="close", target="active")])
        result = desktop_cli.run("never close the active window", engine=engine, parse_only=True)
        self.assertEqual(result['status'], 1); self.assertEqual(engine.count, 0)

    def test_parse_only_does_not_connect(self):
        engine = Engine([c("pointer", action="move", x=10, y=20)])
        with patch('desktop_cli.X11', side_effect=AssertionError('desktop connection')):
            result = desktop_cli.run('move the mouse to 10, 20', engine=engine, parse_only=True)
        self.assertEqual(result['outcome'], 'proposed')

    def test_mcp_desktop_is_opt_in(self):
        factory = lambda *a: (_ for _ in ()).throw(AssertionError('model loaded'))
        ordinary, desktop = mcp_server.Server(factory), mcp_server.Server(factory, tools='desktop')
        self.assertFalse(ordinary._tool_names & mcp_server._DESKTOP_NAMES)
        self.assertEqual(desktop._tool_names, mcp_server._DESKTOP_NAMES)
        result = desktop.call_tool('kilix_desktop_act', dict(request='click here', confirm_risky=False))
        self.assertTrue(result['isError']); self.assertEqual(result['structuredContent']['outcome'], 'blocked')

    def test_mcp_rejects_coerced_confirmation(self):
        for consent in ('yes', 1, [], None):
            with self.assertRaises(ValueError): desktop_cli.mcp(dict(request='click here', confirm_risky=consent))

    def test_mcp_preview_rejects_execution_only_fields(self):
        with self.assertRaises(ValueError):
            desktop_cli.mcp(dict(request='click here', confirm_risky=False), plan=True)

    def test_native_wayland_is_not_silently_used(self):
        with patch.dict(os.environ, {'XDG_SESSION_TYPE': 'wayland', 'DISPLAY': ':0'}):
            with self.assertRaises(job.DesktopError): X11()


if __name__ == '__main__': unittest.main()
