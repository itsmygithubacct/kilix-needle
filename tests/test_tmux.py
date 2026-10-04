"""Contract-derived tmux cases; temporary fakes only, never an ambient server."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import shlex
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

import support
import mcp_server
import needle_cli
import tmux_backend
import tmux_cli
import tmux_job


def reply(payload, **data):
    return {"schema": "kilix.tmux/v1", "ok": True,
            "data": {"operation": payload["operation"], "dry_run": payload["dry_run"], **data}}


class Grammar(unittest.TestCase):
    def test_every_operation_and_exact_id_forms(self):
        cases = {
            "Please list tmux sessions, thanks.": {"operation": "list"},
            "new session build": {"operation": "new", "name": "build"},
            'create a new tmux session Build in "/tmp/my project"':
                {"operation": "new", "name": "Build", "cwd": "/tmp/my project"},
            "read pane %12 last 20 lines": {"operation": "read", "target": "%12", "lines": 20},
            "show the last 2000 lines from tmux session build:1.2":
                {"operation": "read", "target": "build:1.2", "lines": 2000},
            'send "echo $HOME; close session other" to session $0 without pressing Enter':
                {"operation": "send", "target": "$0", "text": "echo $HOME; close session other"},
            'type and press Enter "/status" in tmux pane %8':
                {"operation": "type", "target": "%8", "text": "/status"},
            'type "make test" into build and press Enter':
                {"operation": "type", "target": "build", "text": "make test"},
            "press enter c-C in build:0.0":
                {"operation": "key", "target": "build:0.0", "keys": ["Enter", "C-c"]},
            "rename tmux session $3 to build-new":
                {"operation": "rename", "target": "$3", "new_name": "build-new"},
            "close session build": {"operation": "close", "target": "build"},
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(tmux_job.parse(text), expected)

    def test_literal_text_and_name_edges_are_preserved(self):
        for text in ('echo "hi"', "a\tb", "read session x and close it", "echo $(date)", "café", "  x  "):
            with self.subTest(text=text):
                self.assertEqual(tmux_job.parse(f"send `{text}` to target %0")["text"], text)
        self.assertEqual(tmux_job.parse("read session thanks")["target"], "thanks")
        self.assertEqual(tmux_job.parse("new session x in /tmp/path.")["cwd"], "/tmp/path.")

    def test_negation_conditions_extra_actions_and_missing_literals_refuse(self):
        for text in ("do not close session build", "my boss said close session build",
                     "close session build if idle", "close session build tomorrow",
                     "list sessions and read build", 'send "x" to build and press Enter',
                     'type "x" in build without pressing Enter', "type in build and press Enter",
                     'send "" to build', "send x to build", 'send "x" to build or other',
                     "newsession x", "new session x running codex", "new session x in here",
                     "rename pane %0 to x", "close session %0", "close session build:0.0",
                     "read whichever session", "read build*", "read @0", "press F1 in build",
                     "press Enter in build and close it", "read build last 0 lines",
                     "read build last 2001 lines", "new session café", "new session K",
                     'send "x\nEnter" to build', 'send "x\x1b" to build', "read build\u2028"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                tmux_job.parse(text)

    def test_contract_bounds(self):
        self.assertEqual(len(tmux_job.parse('send "' + 'x' * 65536 + '" to build')["text"]), 65536)
        for request in ('send "' + 'x' * 65537 + '" to build',
                        "press " + "Enter " * 17 + "in build", "new session " + "x" * 129):
            with self.assertRaises(ValueError):
                tmux_job.parse(request)


class Dispatch(unittest.TestCase):
    def test_structured_literals_keep_consent_and_reject_ambiguous_requests(self):
        payload = {'operation': 'send', 'target': '%9', 'text': ' café\t\'"`$HOME; '}
        backend = mock.Mock(side_effect=lambda r: reply(r, target='%9', sent=len(r['text']), submitted=False))
        self.assertEqual(tmux_cli.run(payload, socket='/tmp/s', agent=True, backend=backend)['status'], 1)
        backend.assert_not_called()
        with mock.patch('tmux_backend.dispatch', backend):
            self.assertEqual(tmux_cli.mcp({'request': payload, 'socket': '/tmp/s', 'confirm_risky': True})['status'], 0)
        self.assertEqual(backend.call_args.args[0]['text'], payload['text'])
        backend.reset_mock()
        for bad in (None, [], {**payload, 'extra': 1}, {**payload, 'operation': 'key'},
                    {**payload, 'target': 'ambiguous*'}, {**payload, 'target': '%' + '1'*161},
                    *({**payload, 'text': t} for t in ('', 'x'*65537, 'x\n', 'x\r', '\0', '\ud800'))):
            with self.subTest(bad=str(bad)[:100]):
                self.assertEqual(tmux_cli.run(bad, socket='/tmp/s', assume_yes=True, backend=backend)['status'], 2)
        backend.assert_not_called()

    def test_json_stdin_is_data_and_invalid_json_never_dispatches(self):
        payload = {'operation': 'send', 'target': '%9', 'text': ' café\t\'"`$HOME; '}
        backend = mock.Mock(side_effect=lambda r: reply(r, target='%9', sent=len(r['text']), submitted=False))
        with mock.patch('tmux_backend.dispatch', backend), mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(tmux_cli.main(['--socket', '/tmp/s', '--yes', '--json', '--request-json', '-']), 0)
        self.assertEqual(backend.call_args.args[0]['text'], payload['text'])
        self.assertEqual(json.loads(out.getvalue())['status'], 0)
        backend.reset_mock()
        for raw in ('[]', 'null', '{broken', json.dumps({**payload, 'text': 'x\n'})):
            with mock.patch('tmux_backend.dispatch', backend), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tmux_cli.main(['--socket', '/tmp/s', '--yes', '--json', '--request-json', raw]), 2)
        backend.assert_not_called()

    def test_refusals_and_socket_validation_precede_all_backend_access(self):
        backend = mock.Mock(side_effect=AssertionError("backend must not run"))
        for request, socket in (("list sessions", None), ("list sessions", "relative"),
                                ("list sessions", "/tmp/x\0"), ("close session x and y", "/tmp/s")):
            result = tmux_cli.run(request, socket=socket, assume_yes=True, backend=backend)
            self.assertEqual(result["status"], 2)
        backend.assert_not_called()

    def test_risky_operations_need_explicit_confirmation_but_plan_mutates_nothing(self):
        backend = mock.Mock(side_effect=lambda r: reply(r, request=r, target="%9"))
        for request in ('send "x" to x', 'type "x" in x', "press Enter in x", "close session x"):
            result = tmux_cli.run(request, socket="/tmp/s", agent=True, backend=backend)
            self.assertEqual(result["status"], 1)
        backend.assert_not_called()
        result = tmux_cli.run('type "x" in x', socket="/tmp/s", dry_run=True, backend=backend)
        self.assertEqual(result["status"], 0)
        self.assertTrue(backend.call_args.args[0]["dry_run"])

    def test_send_type_and_keys_never_claim_command_completion(self):
        seen = []

        def backend(request):
            seen.append(request)
            data = {"target": "%4", "sent": 2}
            if request["operation"] == "send":
                return reply(request, **data, submitted=False)
            if request["operation"] == "key":
                data["keys"] = request["keys"]
            return reply(request, **data, submitted=True, completion="unknown")

        for text in ('send "hi" to %4', 'type "hi" in %4', "press Enter in %4"):
            result = tmux_cli.run(text, socket="/tmp/private-socket", assume_yes=True, backend=backend)
            self.assertEqual(result["status"], 0)
        self.assertEqual([r["operation"] for r in seen], ["send", "type", "key"])
        self.assertTrue(all(r["socket"] == "/tmp/private-socket" for r in seen))

    def test_backend_failures_preserve_contract_error_codes(self):
        for code, status in tmux_backend.ERRORS.items():
            result = tmux_cli.run("read build", socket="/tmp/s",
                                  backend=lambda r: tmux_backend.error(code, "detail"))
            self.assertEqual(result["status"], status)
            self.assertEqual(result["result"]["code"], code)

    def test_malformed_or_wrong_mode_responses_are_errors(self):
        for result in (None, [], {"schema": "wrong", "ok": True},
                       {"schema": "kilix.tmux/v1", "ok": True, "data": {"operation": "read", "dry_run": True}},
                       {"schema": "kilix.tmux/v1", "ok": False, "code": "ENOENT", "exit": True, "error": "x"}):
            record = tmux_cli.run("read x", socket="/tmp/s", backend=lambda r: result)
            self.assertEqual(record["status"], 7)
        request = {"operation": "type", "socket": "/tmp/s", "dry_run": False}
        for data in ({"submitted": False}, {"submitted": True, "completion": "done"}):
            with self.assertRaises(ValueError):
                tmux_backend.checked(reply(request, **data), request)


class Surfaces(unittest.TestCase):
    def test_cli_routes_without_any_model_runtime(self):
        backend = mock.Mock(side_effect=lambda r: reply(r, sessions=[]))
        output = io.StringIO()
        with mock.patch("tmux_backend.dispatch", backend), mock.patch("needle_cli.open_runtime") as engine:
            with contextlib.redirect_stdout(output):
                status = needle_cli.main(["tmux", "--socket", "/tmp/s", "--json", "list sessions"])
        self.assertEqual(status, 0)
        engine.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["result"]["data"]["sessions"], [])

    def test_mcp_plan_resolves_only_and_act_requires_risky_consent(self):
        engine = mock.Mock(side_effect=AssertionError("no runtime"))
        server = mcp_server.Server(engine)
        backend = mock.Mock(side_effect=lambda r: reply(r, request=r, target="%1"))
        args = {"request": 'type "ls" in build', "socket": "/tmp/s"}
        with mock.patch("tmux_backend.dispatch", backend):
            plan = server.call_tool("kilix_tmux_plan", args)
            refused = server.call_tool("kilix_tmux_act", args)
        self.assertFalse(plan["isError"])
        self.assertTrue(plan["structuredContent"]["result"]["data"]["dry_run"])
        self.assertTrue(refused["isError"])
        self.assertEqual(backend.call_count, 1)
        engine.assert_not_called()
        self.assertNotIn("request", plan["structuredContent"])

    def test_mcp_invalid_params_are_rejected(self):
        for arguments in ({"request": "list sessions"}, {"request": "x", "socket": 1},
                          {"request": "x", "socket": "/tmp/s", "confirm_risky": "true"},
                          {"request": "x", "socket": "/tmp/s", "extra": 1}):
            with self.assertRaises(ValueError):
                tmux_cli.mcp(arguments)
        with self.assertRaises(ValueError):
            tmux_cli.mcp({"request": "x", "socket": "/tmp/s", "confirm_risky": True}, plan=True)


class Transport(unittest.TestCase):
    def test_invalid_explicit_discovery_never_falls_back(self):
        request = {"operation": "list", "socket": "/tmp/s", "dry_run": False}
        with tempfile.TemporaryDirectory(prefix="needle-tmux-discovery-") as temp:
            for selectors in ({"KILIX_TMUX_MODULE_ROOT": temp},
                              {"KILIX_TMUX_MODULE_ROOT": "relative"},
                              {"KILIX_TMUX_CLI": "", "KILIX_TMUX_MODULE_ROOT": temp},
                              {"KILIX_TMUX_CLI": temp + "/missing"},
                              {"KILIX_NEEDLE_KILIX": temp + "/missing"}):
                env = {"PATH": "/usr/bin:/bin", **selectors}
                with self.subTest(selectors=selectors), mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(tmux_backend.dispatch(request)["code"], "ETMUX")

    def test_explicit_module_root_loads_api_in_an_isolated_worker(self):
        with tempfile.TemporaryDirectory(prefix="needle-tmux-module-") as temp:
            package = Path(temp) / "kilix_tmux"
            package.mkdir()
            (package / "__init__.py").write_text(
                'def dispatch(r):\n'
                ' return {"schema":"kilix.tmux/v1","ok":True,"data":'
                '{"operation":r["operation"],"dry_run":r["dry_run"],"sessions":[]}}\n')
            with mock.patch.dict(os.environ, {"KILIX_TMUX_MODULE_ROOT": temp}), \
                    mock.patch.dict(os.environ, {}, clear=False):
                saved = os.environ.pop("KILIX_TMUX_CLI", None)
                try:
                    result = tmux_backend.dispatch({"operation": "list", "socket": "/tmp/s", "dry_run": False})
                finally:
                    if saved is not None:
                        os.environ["KILIX_TMUX_CLI"] = saved
            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["sessions"], [])

    def test_cli_uses_argv_and_separates_literal_text_from_options(self):
        request = {"operation": "type", "socket": "/tmp/private socket", "dry_run": True,
                   "target": "%7", "text": '--flag "$(date)"; echo x'}
        self.assertEqual(tmux_backend.cli_args(request),
                         ["--socket", "/tmp/private socket", "--json", "--dry-run", "type", "%7",
                          "--", '--flag "$(date)"; echo x'])
        process = mock.Mock(returncode=0, stdout=json.dumps(reply(request, request=request, target="%7")))
        with tempfile.NamedTemporaryFile() as cli, \
                mock.patch.dict(os.environ, {"KILIX_TMUX_CLI": cli.name}), \
                mock.patch("tmux_backend.subprocess.run", return_value=process) as run:
            Path(cli.name).chmod(0o700)
            result = tmux_backend.dispatch(request)
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0], [cli.name, *tmux_backend.cli_args(request)])
        self.assertNotIn("shell", run.call_args.kwargs)


@unittest.skipUnless(os.environ.get("KILIX_NEEDLE_TMUX_TEST_ROOT"), "explicit private-test backend not supplied")
class PrivateIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="needle-tmux-private-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.socket = str(self.root / "server.sock")
        self.backend_root = Path(os.environ["KILIX_NEEDLE_TMUX_TEST_ROOT"]).resolve()
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "guard", "/bin/sh")
        self.addCleanup(lambda: subprocess.run(["tmux", "-S", self.socket, "kill-server"], capture_output=True))
        self.tmux("set-option", "-g", "default-shell", "/bin/sh")

    def tmux(self, *args):
        return subprocess.check_output(["tmux", "-S", self.socket, *args], text=True, timeout=5)

    def select(self, mode):
        env = os.environ.copy()
        env.pop("KILIX_TMUX_CLI", None)
        env.pop("KILIX_TMUX_MODULE_ROOT", None)
        if mode == "cli":
            env["KILIX_TMUX_CLI"] = str(self.backend_root / "bin" / "kilix-tmux")
        elif mode in ("bundled", "selected"):
            source = self.root / "kilix-source"
            if not source.exists():
                (source / "config").mkdir(parents=True)
                shutil.copytree(self.backend_root / "kilix_tmux", source / "config" / "kilix_tmux")
                executable = source / "kilix"
                executable.write_text("#!/bin/sh\nexit 99\n")
                executable.chmod(0o755)
                (self.root / "bin").mkdir()
                (self.root / "bin" / "kilix").symlink_to(executable)
            # Keep tmux/python available but hide any installed kilix-tmux.
            env["PATH"] = str(self.root / "bin") + ":/usr/bin:/bin"
            env.pop("KILIX_NEEDLE_KILIX", None)
            if mode == "selected":
                env["KILIX_NEEDLE_KILIX"] = str(source / "kilix")
        else:
            env["KILIX_TMUX_MODULE_ROOT"] = str(self.backend_root)
        return mock.patch.dict(os.environ, env, clear=True)

    def cli(self, request, *options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = needle_cli.main(["tmux", "--socket", self.socket, "--json", "--agent", *options, request])
        record = json.loads(output.getvalue())
        self.assertEqual(status, record["status"])
        return record

    def mcp(self, request, *, plan=False, confirm=False):
        engine = mock.Mock(side_effect=AssertionError("tmux must never load a model"))
        args = {"request": request, "socket": self.socket}
        if not plan:
            args["confirm_risky"] = confirm
        result = mcp_server.Server(engine).call_tool("kilix_tmux_plan" if plan else "kilix_tmux_act", args)
        engine.assert_not_called()
        self.assertEqual(result["isError"], result["structuredContent"]["status"] != 0)
        return result["structuredContent"]

    def wait_file(self, path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.exists(), f"receipt did not appear: {path}")

    def test_all_operations_cli_and_mcp_and_actual_shell_receipts(self):
        for mode in ("module", "cli", "bundled", "selected"):
            with self.subTest(mode=mode), self.select(mode):
                name = "Contract_" + mode
                data = self.cli(f"new session {name} in {self.root}")["result"]["data"]
                self.assertEqual(data["session"]["name"], name)
                ident = data["session"]["id"]
                pane = data["session"]["panes"][0]["id"]
                self.assertEqual(self.cli(f"new session {name}")["result"]["code"], "EEXIST")
                before = self.cli(f"read {pane}")["result"]["data"]["text"]
                marker = self.root / (mode + "-shell-receipt")
                command = "printf needle-receipt > " + shlex.quote(str(marker))
                plan = self.mcp(f'type "{command}" in {pane}', plan=True)
                self.assertTrue(plan["result"]["data"]["dry_run"])
                self.assertEqual(self.cli(f"read {pane}")["result"]["data"]["text"], before)
                self.assertFalse(marker.exists())
                sent = self.cli(f'send "{command}" to {pane}', "--yes")
                self.assertFalse(sent["result"]["data"]["submitted"])
                self.assertFalse(marker.exists())
                entered = self.mcp(f"press Enter in {pane}", confirm=True)
                self.assertEqual(entered["result"]["data"]["completion"], "unknown")
                self.wait_file(marker)
                self.assertEqual(marker.read_text(), "needle-receipt")
                typed = self.cli(f'type "printf second >> {marker}" in {pane}', "--yes")
                self.assertEqual(typed["result"]["data"]["completion"], "unknown")
                deadline = time.monotonic() + 5
                while marker.read_text() != "needle-receiptsecond" and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(marker.read_text(), "needle-receiptsecond")
                renamed = self.mcp(f"rename session {ident} to Renamed_{mode}")
                self.assertEqual(renamed["result"]["data"]["session"]["id"], ident)
                closing = self.cli(f"close session {ident}", "--dry-run")
                self.assertTrue(closing["result"]["data"]["dry_run"])
                self.assertIn(ident, [s["id"] for s in self.mcp("list sessions")["result"]["data"]["sessions"]])
                self.assertEqual(self.mcp(f"close session {ident}", confirm=True)["result"]["data"]["closed"], ident)
                self.assertNotIn(ident, [s["id"] for s in self.cli("list sessions")["result"]["data"]["sessions"]])

    def test_missing_targets_ambiguity_refusals_and_absent_socket_nonmutation(self):
        with self.select("module"):
            session = self.cli("new session ExactName")["result"]["data"]["session"]
            self.tmux("split-window", "-d", "-t", session["panes"][0]["id"], "/bin/sh")
            self.assertEqual(self.cli("read ExactName")["result"]["code"], "EAMBIGUOUS")
            self.assertEqual(self.cli("read Exact")["result"]["code"], "ENOENT")
            self.assertEqual(self.cli("read Missing")["result"]["code"], "ENOENT")
            self.assertEqual(self.cli("read ExactName:0.0")["status"], 0)
            backend = mock.Mock(side_effect=AssertionError("no dispatch for grammar refusal"))
            with mock.patch("tmux_backend.dispatch", backend):
                for request in ("read", "list sessions and close session ExactName", 'send "x" to ExactName tomorrow'):
                    self.assertEqual(self.cli(request, "--yes")["status"], 2)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
                    needle_cli.main(["tmux", "list sessions"])
                self.assertEqual(exit.exception.code, 2)
            backend.assert_not_called()
            unused = str(self.root / "absent.sock")
            self.assertEqual(tmux_cli.run("list sessions", socket=unused)["result"]["data"]["sessions"], [])
            self.assertEqual(tmux_cli.run("new session Planned", socket=unused, dry_run=True)["status"], 0)
            self.assertFalse(Path(unused).exists())
            self.assertEqual(tmux_cli.run("read Missing", socket=unused)["result"]["code"], "ENOSERVER")

    def test_literal_utf8_bytes_quotes_tab_and_trailing_semicolon_with_separate_enter(self):
        payload = '  café\t"$(echo x)" `quoted`;'
        listener = self.root / "listener.py"
        listener.write_text(
            'import os,sys,tty\nfrom pathlib import Path\n'
            'tty.setraw(0)\nroot=Path(sys.argv[1]); size=int(sys.argv[2])\n'
            '(root/"ready").write_text("ready")\n'
            'data=b""\nwhile len(data)<size: data+=os.read(0,size-len(data))\n'
            '(root/"payload").write_bytes(data)\n'
            '(root/"enter").write_bytes(os.read(0,1))\n')
        for mode in ("module", "cli", "bundled", "selected"):
            with self.subTest(mode=mode), self.select(mode):
                for operation in ("send", "type"):
                    receipts = self.root / (mode + "-" + operation)
                    receipts.mkdir()
                    name = mode + "_" + operation
                    self.tmux("new-session", "-d", "-s", name, sys.executable, str(listener),
                              str(receipts), str(len(payload.encode())))
                    self.wait_file(receipts / "ready")
                    # Payload contains double quotes/backticks, so use a single-quoted literal.
                    result = self.cli(f"{operation} '{payload}' in {name}" if operation == "type" else
                                      f"send '{payload}' to {name}", "--yes")
                    self.assertEqual(result["status"], 0)
                    self.wait_file(receipts / "payload")
                    self.assertEqual((receipts / "payload").read_bytes(), payload.encode())
                    if operation == "send":
                        self.assertFalse((receipts / "enter").exists())
                        self.assertFalse(result["result"]["data"]["submitted"])
                        self.mcp(f"press Enter in {name}", confirm=True)
                    else:
                        self.assertEqual(result["result"]["data"]["completion"], "unknown")
                    self.wait_file(receipts / "enter")
                    self.assertEqual((receipts / "enter").read_bytes(), b"\r")

    def test_structured_and_file_literal_transport_all_quote_styles(self):
        payload = ' café\t\'"`$HOME $(never_run) ;'
        listener = self.root / 'raw_listener.py'
        listener.write_text('import os,sys,tty\nfrom pathlib import Path\n'
                            'tty.setraw(0)\nr=Path(sys.argv[1]); size=int(sys.argv[2])\n'
                            '(r/"ready").touch()\ndata=b""\n'
                            'while len(data)<size: data+=os.read(0,size-len(data))\n'
                            '(r/"payload").write_bytes(data)\n'
                            '(r/"enter").write_bytes(os.read(0,1))\n')
        text_file = self.root / 'text'
        text_file.write_text(payload, encoding='utf-8')
        for mode in ('module', 'cli'):
            with self.select(mode):
                for route in ('json', 'mcp', 'file', 'stdin'):
                    receipts = self.root / (mode + '-' + route)
                    receipts.mkdir()
                    name = mode + '_' + route
                    self.tmux('new-session', '-d', '-s', name, sys.executable, str(listener),
                              str(receipts), str(len(payload.encode())))
                    self.wait_file(receipts / 'ready')
                    request = {'operation': 'send', 'target': name, 'text': payload}
                    if route == 'mcp':
                        result = self.mcp(request, confirm=True)
                        self.assertEqual(result['status'], 0)
                    elif route == 'json':
                        with mock.patch('sys.stdin', io.StringIO(json.dumps(request))), contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(needle_cli.main(['tmux', '--socket', self.socket, '--yes', '--request-json', '-']), 0)
                    else:
                        proc = subprocess.run([str(self.backend_root/'bin/kilix-tmux'), '--socket', self.socket,
                                               '--json', 'send', name, '--text-file',
                                               '-' if route == 'stdin' else str(text_file)],
                                              input=payload, capture_output=True, text=True, timeout=5)
                        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                    self.wait_file(receipts / 'payload')
                    self.assertEqual((receipts / 'payload').read_bytes(), payload.encode())
                    self.assertFalse((receipts / 'enter').exists())
                    self.mcp(f'press Enter in {name}', confirm=True)
                    self.wait_file(receipts / 'enter')
                    self.assertEqual((receipts / 'enter').read_bytes(), b'\r')


if __name__ == "__main__":
    unittest.main()
