"""No real audio, desktop or microphone writes: commands are fakes throughout."""
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import support  # noqa: F401
import app_controls as controls
import app_controls_backend as backend
import kilix
import mcp_server
import needle_cli


def device(name="speaker.one", index=1, **extra):
    return {"name": name, "index": index, "description": "Desk Speakers", "mute": False,
            "volume": {"front-left": {"value": 32768}, "front-right": {"value": 16384}}, **extra}


class Audio:
    def __init__(self):
        self.rows = {"sink": [device()], "source": [device("mic.one", 2, description="USB Mic"),
                    device("speaker.one.monitor", 3, monitor_of_sink=1)]}
        self.default = {"sink": "speaker.one", "source": "mic.one"}
        self.calls = []

    def __call__(self, argv, **_kwargs):
        argv = tuple(argv)
        self.calls.append(argv)
        if argv[:3] == ("pactl", "--format=json", "list"):
            return json.dumps(self.rows[argv[3][:-1]])
        if argv[0] != "pactl":
            raise AssertionError(argv)
        op = argv[1]
        kind = "source" if "source" in op else "sink"
        if op.startswith("get-default-"):
            return self.default[kind]
        if op.startswith("set-default-"):
            self.default[kind] = argv[2]
            return ""
        row = next(r for r in self.rows[kind] if r["name"] == argv[2])
        if op.endswith("-mute"):
            row["mute"] = argv[3] == "1"
        elif op.endswith("-volume"):
            for channel, value in zip(row["volume"].values(), argv[3:]):
                channel["value"] = int(value)
        else:
            raise AssertionError(argv)
        return ""

    @property
    def writes(self):
        return [c for c in self.calls if c[1].startswith("set-")]


class Controls(unittest.TestCase):
    def setUp(self):
        # Every accidental external command fails the test, even queries.
        self.guard = mock.patch.object(backend, "command", side_effect=AssertionError("unmocked backend"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def run_request(self, request, **options):
        return needle_cli.run_apps_request(None, request, needle_cli.Options(**options), lambda _: False)

    def test_parser_preserves_literals_and_distinguishes_controls(self):
        examples = {
            'Please set default input to "USB Mic".': ("audio", "default", "source", "USB Mic"),
            'say "Hello, Bob. $(touch /tmp/nope)"': ("voice", "speak", "", "Hello, Bob. $(touch /tmp/nope)"),
            "mute microphone": ("audio", "mute", "source", True),
            "unmute speakers": ("audio", "mute", "sink", False),
            "set system volume to 40%": ("audio", "volume", "sink", 40),
            "decrease microphone volume by 5 percentage points": ("audio", "adjust", "source", -5),
            "pause music": ("media", "pause", "", None),
            "resume music": ("media", "play", "", None),
            "set music volume to 20": ("media", "volume", "", 20),
            "set repeat to track": ("media", "repeat", "", 2),
            "turn shuffle off": ("media", "shuffle", "", False),
            "stop dictation": ("voice", "stop-dictation", "", None),
            "set speech rate to 200 wpm": ("voice_setting", "set", "wpm", "200"),
            "read this pane aloud": ("voice", "read", "", "screen"),
            "make text larger": ("font", "larger", "", 2),
            "how much RAM is free?": ("system", "status", "memory", None),
            "show system status": ("system", "status", "system", None),
        }
        for text, expected in examples.items():
            with self.subTest(text=text):
                c = controls.parse(text)
                self.assertEqual((c.family, c.operation, c.target, c.value), expected)

    def test_font_size_read_by_its_parts(self):
        got = controls.parse("please make the font size 14")
        self.assertEqual((got.family, got.operation, got.value), ("font", "set", 14))
        for request in ("Set the Kilix text size to 14 (absolute size, not relative).",
                        "Change the terminal font size in Kilix to 14 pt and confirm the change.",
                        "Set the Kilix terminal text size to 14 points for all windows.",
                        "Set the terminal text size to 14 points in Kilix and leave everything else unchanged.",
                        "Make the Kilix font size exactly 14."):
            with self.subTest(request=request):
                self.assertEqual(controls.parse(request).value, 14)
        for request in ("make the text bigger by 2", "font size 3", "set the font size to 14 in this pane",
                        "font size 14 if it is not already", "set font size to 14, not 12",
                        "set font size to 14 and confirm the clock is hidden",
                        "set text size to 14 for all windows except this one", "font size 14 and show the battery"):
            with self.subTest(request=request):
                self.assertIsNone(controls.parse(request))

    def test_abstains_without_partial_execution(self):
        for text in ["should I mute microphone?", "don't mute microphone", "mute microphone and pause music",
                     "mute microphone if music stops", "mute microphone; reboot", "mute microphone\n",
                     "set system volume to 101%", "set system volume to -1%", "set system volume to 1e2",
                     "set text size to 111", "dictation for 121 seconds", "set dictation submit to always",
                     "set speech rate to 999", "seek music to 999999 seconds", "make text larger by 0 points",
                     'set default output to "Desk Speakers" and reboot', "mute", "set volume to 30",
                     "hide microphone", "open music", "stop music and close apps", "show cpu usage then reboot"]:
            with self.subTest(text=text):
                self.assertIsNone(controls.parse(text))

    def test_typed_contract_rejects_invalid_values(self):
        for args in [("audio", "volume", "sink", 101), ("audio", "volume", "sink", True),
                     ("audio", "mute", "sink", "toggle"), ("voice_setting", "set", "shell", "rm"),
                     ("media", "shuffle", "", None), ("system", "status", "power", None),
                     ("voice", "dictate", "", 999), ("media", "quit", "", None)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                controls.Control(*args)

    def test_audio_dry_run_and_confirmation_never_write(self):
        audio = Audio()
        with mock.patch.object(backend, "command", side_effect=audio):
            for options, outcome in [({"dry_run": True, "assume_yes": True}, "would"),
                                     ({}, "skipped"), ({"agent": True}, "skipped")]:
                result = self.run_request("mute microphone", **options)
                self.assertEqual(result["items"][0]["outcome"], outcome)
                self.assertFalse(audio.writes)
            result = self.run_request("mute microphone", assume_yes=True)
        self.assertEqual(result["status"], 0)
        self.assertEqual(audio.writes, [("pactl", "set-source-mute", "mic.one", "1")])
        self.assertTrue(result["items"][0]["result"]["muted"])

    def test_agent_never_calls_confirmation(self):
        with mock.patch.object(backend, "command", side_effect=Audio()):
            result = needle_cli.run_apps_request(None, "mute microphone", needle_cli.Options(agent=True),
                                                mock.Mock(side_effect=AssertionError("asked stdin")))
        self.assertEqual(result["status"], 1)

    def test_queries_need_no_confirmation_or_engine(self):
        with mock.patch.object(backend, "command", side_effect=Audio()):
            result = self.run_request("list audio devices", agent=True)
        self.assertEqual(result["status"], 0)
        self.assertEqual([r["name"] for r in result["items"][0]["result"]["inputs"]], ["mic.one"])
        self.assertIn("USB Mic", needle_cli.render(result))

    def test_volume_adjustment_preserves_channel_difference_with_bounds(self):
        audio = Audio()
        with mock.patch.object(backend, "command", side_effect=audio):
            result = self.run_request("increase system volume by 10%", assume_yes=True)
            self.assertEqual(result["status"], 0)
            self.assertEqual(result["items"][0]["result"]["volume_percent"], [60.0, 35.0])
            result = self.run_request("increase system volume by 100%", assume_yes=True)
            self.assertEqual(result["items"][0]["result"]["volume_percent"], [100.0, 100.0])
            result = self.run_request("set system volume to 0%", assume_yes=True)
            self.assertEqual(result["items"][0]["result"]["volume_percent"], [0.0, 0.0])

    def test_default_device_exact_name_or_unique_description(self):
        audio = Audio()
        audio.rows["sink"].append(device("speaker.two", 5, description="USB Speaker"))
        with mock.patch.object(backend, "command", side_effect=audio):
            result = self.run_request('set default output to "USB Speaker"', assume_yes=True)
            self.assertEqual(result["status"], 0)
            self.assertEqual(audio.default["sink"], "speaker.two")
            audio.rows["sink"][0]["description"] = "USB Speaker"
            before = len(audio.writes)
            result = self.run_request('set default output to "USB Speaker"', assume_yes=True)
            self.assertEqual(result["status"], 1)
            self.assertEqual(len(audio.writes), before)
            self.assertEqual(self.run_request('set default input to "speaker.one.monitor"', assume_yes=True)["status"], 1)

    def test_plan_pins_device_and_rejects_replaced_identity(self):
        audio = Audio()
        audio.rows["sink"].append(device("speaker.two", 4))
        with mock.patch.object(backend, "command", side_effect=audio):
            step = backend.prepare(controls.parse("mute speakers"))
            audio.default["sink"] = "speaker.two"
            backend.perform(step)
            self.assertTrue(audio.rows["sink"][0]["mute"])
            self.assertFalse(audio.rows["sink"][1]["mute"])
            audio.rows["sink"][0]["index"] = 99
            before = len(audio.writes)
            with self.assertRaisesRegex(backend.ControlError, "identity"):
                backend.perform(step)
            self.assertEqual(len(audio.writes), before)

    def test_readback_and_failures_are_not_success(self):
        audio = Audio()
        def ignored_write(argv, **kwargs):
            return "" if argv[1].startswith("set-") else audio(argv, **kwargs)
        with mock.patch.object(backend, "command", side_effect=ignored_write):
            result = self.run_request("mute speakers", assume_yes=True)
        self.assertEqual(result["status"], 1)
        self.assertIn("readback", result["items"][0]["reason"])
        self.assertIn("may have changed", result["items"][0]["reason"])
        with mock.patch.object(backend, "command", side_effect=backend.ControlError("no pactl")):
            self.assertEqual(self.run_request("show system volume")["items"][0]["outcome"], "unresolved")

    def test_voice_font_and_system_dry_runs_make_no_calls(self):
        for request in ["set speech rate to 200 wpm", "turn spoken punctuation off", "say \"hello\"",
                        "dictation for 30 seconds", "stop voice", "stop dictation", "set text size to 14",
                        "show system status"]:
            with self.subTest(request=request):
                self.assertEqual(self.run_request(request, dry_run=True)["items"][0]["outcome"], "would")

    def test_speech_literal_is_stdin_not_an_option_or_shell(self):
        with mock.patch.object(backend, "command", return_value="accepted") as call:
            result = self.run_request('say "--help $(touch /tmp/nope)"', assume_yes=True)
        self.assertEqual(result["status"], 0)
        call.assert_called_once_with((kilix.KILIX, "speak", "-"),
                                     data="--help $(touch /tmp/nope)", timeout=15)

    def test_dictation_returns_transcript_never_types(self):
        with mock.patch.object(backend, "command", return_value="a transcript") as call:
            result = self.run_request("dictation for 15 seconds", assume_yes=True)
        self.assertEqual(result["items"][0]["result"], {"transcript": "a transcript"})
        self.assertEqual(call.call_args.args[0], (kilix.KILIX, "dictate", "--seconds", "15"))
        self.assertGreater(call.call_args.kwargs["timeout"], 15)

    def test_read_requires_caller_and_respects_overlay_target(self):
        tree = mock.Mock(caller=None)
        with mock.patch.object(kilix, "snapshot", return_value=tree):
            self.assertEqual(self.run_request("read this pane aloud", dry_run=True)["status"], 1)
            tree.caller = {"id": 10}
            tree.active_pane = {"id": 9}
            result = self.run_request("read selection aloud", dry_run=True, under_overlay=True)
            self.assertIn("9", result["items"][0]["plan"]["argv"])

    def test_setting_readback_uses_owned_key(self):
        with mock.patch.object(backend, "command", return_value="KILIX_VOICE_STT_PUNCTUATION=off\n"):
            self.assertEqual(self.run_request("turn spoken punctuation off", assume_yes=True)["status"], 0)
        with mock.patch.object(backend, "command", return_value="KILIX_VOICE_TTS_RATE=170\n"):
            self.assertEqual(self.run_request("set speech rate to 200", assume_yes=True)["status"], 1)

    def test_music_settings_use_explicit_values_and_check_readback(self):
        cases = [("set music volume to 40", {"cmd": "volume", "level": 40}, "volume", 40),
                 ("turn shuffle off", {"cmd": "shuffle", "on": False}, "shuffle", False),
                 ("set repeat to track", {"cmd": "repeat", "mode": 2}, "repeat", 2)]
        for request, payload, key, value in cases:
            with self.subTest(request=request), mock.patch.object(backend, "media_request",
                    return_value={"ok": True, "protocol": 1, key: value}) as call:
                result = self.run_request(request, assume_yes=True)
                self.assertEqual(result["status"], 0)
                self.assertEqual(call.call_args.args[1], {"protocol": 1, **payload})
            with mock.patch.object(backend, "media_request", return_value={"ok": True, "protocol": 1}):
                self.assertEqual(self.run_request(request, assume_yes=True)["status"], 1)

    def test_font_readback_and_system_schema(self):
        with mock.patch.object(backend, "command", side_effect=["font_size 14", "font_size 14"]):
            result = self.run_request("set text size to 14", assume_yes=True)
            self.assertEqual(result["items"][0]["result"]["saved_font_size_points"], 14)
        with mock.patch.object(backend, "command", return_value='{"schema_version":1,"memory":{"available":123}}'):
            result = self.run_request("show memory usage")
            self.assertEqual(result["items"][0]["result"], {"memory": {"available": 123}})
        for bad in ["[]", "{", '{"schema_version":true}', '{"schema_version":2}', '{"schema_version":1}']:
            with mock.patch.object(backend, "command", return_value=bad):
                self.assertEqual(self.run_request("show memory usage")["status"], 1)

    def test_mcp_controls_never_ask_the_engine_and_obey_confirm_flag(self):
        runtime = mock.MagicMock()
        runtime.complete.side_effect = AssertionError("the engine was asked")
        server = mcp_server.Server(lambda job="panes": runtime)
        with mock.patch.object(backend, "command", side_effect=Audio()):
            plan = server.call_tool("kilix_apps_plan", {"request": "mute microphone"})["structuredContent"]
            act = server.call_tool("kilix_apps_act", {"request": "mute microphone"})["structuredContent"]
            yes = server.call_tool("kilix_apps_act", {"request": "mute microphone", "confirm_risky": True})["structuredContent"]
            query = server.call_tool("kilix_apps_act", {"request": "show microphone status"})["structuredContent"]
        self.assertEqual([r["items"][0]["outcome"] for r in [plan, act, yes, query]],
                         ["would", "skipped", "done", "done"])
        runtime.complete.assert_not_called()
        server.close()

    def test_cli_one_shot_and_interactive_controls_never_ask_the_engine(self):
        runtime = mock.MagicMock()
        runtime.complete.side_effect = AssertionError("the engine was asked")
        runtime.__enter__.return_value = runtime
        with mock.patch.object(needle_cli, "open_runtime", return_value=runtime), \
                mock.patch.object(backend, "command", side_effect=Audio()):
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(needle_cli.main(["apps", "--dry-run", "mute microphone"]), 0)
            with mock.patch("sys.stdin.isatty", return_value=True), \
                    mock.patch("builtins.input", side_effect=["show microphone status", "exit"]), \
                    mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(needle_cli.main(["apps"]), 0)
        runtime.complete.assert_not_called()

    def test_existing_request_still_uses_model_and_existing_validator(self):
        engine = mock.Mock()
        engine.complete.return_value = {"function_calls": []}
        engine.translate = lambda calls: calls
        with mock.patch.object(needle_cli, "run_apps_calls", return_value={"status": 0}) as runner:
            self.assertEqual(needle_cli.run_apps_request(engine, "open solitaire", needle_cli.Options()), {"status": 0})
        engine.complete.assert_called_once_with("open solitaire")
        runner.assert_called_once()


class CommandAdapter(unittest.TestCase):
    def test_checked_argv_no_shell_no_inherited_stdin(self):
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as run:
            self.assertEqual(backend.command(["pactl", "get-default-sink"]), "ok")
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")

    def test_failure_timeout_missing_and_oversize_are_checked(self):
        for failure in [OSError("missing"), subprocess.TimeoutExpired("pactl", 15)]:
            with mock.patch.object(subprocess, "run", side_effect=failure), self.assertRaises(backend.ControlError):
                backend.command(["pactl"])
        for completed in [subprocess.CompletedProcess([], 1, "", "denied"),
                          subprocess.CompletedProcess([], 0, "x" * (1024 * 1024 + 1), "")]:
            with mock.patch.object(subprocess, "run", return_value=completed), self.assertRaises(backend.ControlError):
                backend.command(["pactl"])


class SocketAdapters(unittest.TestCase):
    def serve(self, responses, kind=socket.SOCK_STREAM):
        directory = tempfile.TemporaryDirectory(prefix="needle-control-")
        self.addCleanup(directory.cleanup)
        path = str(Path(directory.name) / "control.sock")
        listener = socket.socket(socket.AF_UNIX, kind)
        listener.bind(path)
        listener.listen(1)
        listener.settimeout(2)
        received, errors = [], []
        def server():
            try:
                with listener, listener.accept()[0] as peer:
                    peer.settimeout(2)
                    for response in responses:
                        data = peer.recv(65536)
                        if not data:
                            break
                        received.append(json.loads(data))
                        if response is None:
                            break
                        peer.sendall(response if isinstance(response, bytes) else json.dumps(response).encode() + b"\n")
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        self.addCleanup(lambda: thread.join(3))
        return path, received, thread, errors

    def test_amp_handshake_then_exact_command_same_connection(self):
        path, received, thread, errors = self.serve([
            {"protocol": 1, "ok": True, "state": "playing"},
            {"protocol": 1, "ok": True, "state": "paused"}])
        reply = backend.media_request(path, {"protocol": 1, "cmd": "pause"})
        thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(reply["state"], "paused")
        self.assertEqual([r["cmd"] for r in received], ["state", "pause"])

    def test_bad_handshake_never_sends_mutation(self):
        for bad in [{"protocol": 2, "ok": True}, {"protocol": True, "ok": True},
                    {"protocol": 1, "ok": False, "error": "not ready"}, b"not json\n", b"x" * 65537, None]:
            path, received, thread, _ = self.serve([bad])
            with self.assertRaises(backend.ControlError):
                backend.media_request(path, {"protocol": 1, "cmd": "next"})
            thread.join(3)
            self.assertEqual([r["cmd"] for r in received], ["state"])

    def test_lost_mutation_reply_is_not_retried(self):
        path, received, thread, errors = self.serve([{"protocol": 1, "ok": True}, None])
        with self.assertRaises(backend.ControlError):
            backend.media_request(path, {"protocol": 1, "cmd": "next"})
        thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(len(received), 2)

    def test_voice_stop_is_one_seqpacket_and_does_not_stop_other_operation(self):
        path, received, thread, errors = self.serve([{"ok": True}], socket.SOCK_SEQPACKET)
        step = backend.Step(controls.parse("stop dictation"), "stop dictation", socket_path=path,
                            payload={"op": "stop-dictation"})
        backend.perform(step)
        thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(received, [{"op": "stop-dictation"}])

    def test_media_plan_only_queries_and_no_confirmation_never_mutates(self):
        for dry in [True, False]:
            path, received, thread, errors = self.serve([{"protocol": 1, "ok": True}])
            with mock.patch.dict(os.environ, {"KILIX_AMP_SOCKET": path}):
                result = needle_cli.run_apps_request(None, "pause music", needle_cli.Options(dry_run=dry, agent=True))
            thread.join(3)
            self.assertFalse(errors)
            self.assertEqual([r["cmd"] for r in received], ["state"])
            self.assertEqual(result["items"][0]["outcome"], "would" if dry else "skipped")

    def test_regular_file_is_not_a_control_endpoint(self):
        with tempfile.NamedTemporaryFile() as file, self.assertRaises(backend.ControlError):
            backend.connect(file.name, socket.SOCK_STREAM)

    def test_split_json_reply_and_socket_timeout(self):
        sock = mock.Mock()
        sock.recv.side_effect = [b'{"protocol":1,', b'"ok":true}', b'\n']
        self.assertEqual(backend.exchange(sock, {"cmd": "state"}), {"protocol": 1, "ok": True})
        with mock.patch.object(backend, "connect", side_effect=TimeoutError("timeout")):
            with self.assertRaisesRegex(backend.ControlError, "timeout"):
                backend.media_request("/unused", {"cmd": "state"})


if __name__ == "__main__":
    unittest.main()


class ReviewR18(unittest.TestCase):
    """Review R18: agent authority, linked socket paths, no engine for controls,
    and one reply rule for running and scoring."""

    def mocked(self):
        step = mock.Mock(summary="mute the microphone", public=lambda: {})
        return (mock.patch.object(backend, "prepare", return_value=step),
                mock.patch.object(backend, "perform", return_value={"ok": True}))

    def test_an_agent_changes_only_with_the_advance_yes(self):             # KN-R18-01
        prepare, perform = self.mocked()
        with prepare, perform as done:
            held = needle_cli.run_apps_request(None, "mute microphone", needle_cli.Options(agent=True))
            done.assert_not_called()
            yes = needle_cli.run_apps_request(None, "mute microphone",
                                              needle_cli.Options(agent=True, assume_yes=True))
        self.assertEqual((held["items"][0]["outcome"], yes["items"][0]["outcome"]), ("skipped", "done"))

    def test_a_linked_parent_directory_is_refused(self):                     # KN-R18-02
        import socket as socketlib
        import tempfile
        with tempfile.TemporaryDirectory(prefix="kn-") as tmp:
            real = os.path.join(tmp, "real")
            os.mkdir(real)
            server = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
            server.bind(os.path.join(real, "control.sock"))
            server.listen(1)
            try:
                os.symlink(real, os.path.join(tmp, "alias"))
                with self.assertRaisesRegex(backend.ControlError, "link"):
                    backend.connect(os.path.join(tmp, "alias", "control.sock"), socketlib.SOCK_STREAM)
                backend.connect(os.path.join(real, "control.sock"), socketlib.SOCK_STREAM).close()
            finally:
                server.close()

    def test_controls_need_no_engine_from_mcp_or_the_cli(self):            # KN-R18-03
        broken = mock.Mock(side_effect=needle_cli.asset.AssetError("engine missing"))
        server = mcp_server.Server(broken)
        with mock.patch.object(backend, "command", return_value="font_size 14"):
            plan = server.call_tool("kilix_apps_plan", {"request": "show text size"})
        self.assertFalse(plan["isError"])
        broken.assert_not_called()
        with mock.patch.object(needle_cli, "open_runtime", broken), \
                mock.patch.object(backend, "command", return_value="font_size 14"), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(needle_cli.main(["apps", "show", "text", "size"]), 0)
            self.assertEqual(needle_cli.main(["apps", "hide", "the", "clock"]), 2)

    def test_an_error_marked_or_malformed_reply_runs_and_scores_nothing(self):  # KN-R18-04, -05
        import evaluate
        replies = [{"success": False, "error": "truncated",
                    "function_calls": [{"name": "launch", "arguments": {"app": "doom"}}]},
                   {"function_calls": None}, {"function_calls": [123]}, {"function_calls": [{}]},
                   {"function_calls": [{"name": "launch", "arguments": None}]}]
        for reply in replies:
            engine = mock.Mock()
            engine.complete.return_value = reply
            engine.translate = lambda calls: calls
            with mock.patch.object(needle_cli, "run_apps_calls") as runner:
                record = needle_cli.run_apps_request(engine, "open doom", needle_cli.Options())
            runner.assert_not_called()
            self.assertEqual((record["status"], record["items"]), (1, []), reply)
            report = evaluate.score(engine, [{"request": "open doom", "expect": [], "tag": "bait"}],
                                    job="apps")
            totals = report["totals"] if "totals" in report else report
            self.assertEqual((totals["errors"], totals["exact"]), (1, 0), reply)
        engine = mock.Mock()
        engine.complete.return_value = {"success": True}      # no calls key: no calls, no error
        with mock.patch.object(needle_cli, "run_apps_calls", return_value={"status": 0}) as runner:
            needle_cli.run_apps_request(engine, "tell me a joke", needle_cli.Options())
        runner.assert_called_once()



class ReviewR18Round2(unittest.TestCase):
    def test_runtime_error_text_never_reaches_the_history(self):            # KN-R18-201
        import json, shutil
        import history
        shutil.rmtree(history.directory(), ignore_errors=True)
        engine = mock.Mock()
        engine.complete.return_value = {"success": False, "error": "private pane title zq-secret-9",
                                        "function_calls": []}
        record = needle_cli.run_request(engine, "shut the left pane", needle_cli.Options(dry_run=True))
        self.assertIn("zq-secret-9", record["note"])                       # the caller still sees it
        text = (history.directory() / "requests.jsonl").read_text()
        self.assertNotIn("zq-secret-9", text)
        self.assertEqual(json.loads(text.splitlines()[-1])["note"], "the engine's reply could not be used")

    def test_a_control_with_trailing_space_needs_no_engine_over_mcp(self):  # KN-R18-202
        broken = mock.Mock(side_effect=needle_cli.asset.AssetError("engine missing"))
        server = mcp_server.Server(broken)
        with mock.patch.object(backend, "command", return_value="font_size 14"):
            plan = server.call_tool("kilix_apps_plan", {"request": "show text size\n"})
        self.assertFalse(plan["isError"])
        broken.assert_not_called()
