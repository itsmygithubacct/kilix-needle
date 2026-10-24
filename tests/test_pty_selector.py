"""Additive selector grammar/MCP transport; real integration is explicitly opt-in."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import support  # isolates every default live route
import mcp_server
import pty_backend
import pty_cli
import pty_job


def document(action, result="ambiguous", verb=None):
    operation = verb or action["operation"]
    count = 2 if result == "ambiguous" else 1
    selector = action["selector"]
    ident = selector.get("id", selector.get("id_prefix", "") + "target")
    rows = [{"id": ident if count == 1 else selector.get("id_prefix", "") + f"item-{i}",
             "reachable": True, "possible": False} for i in range(count)]
    return {"schema": "kilix.pty.selector/v1", "operation": operation, "runtime": "/tmp/private",
            "selector": action["selector"], "caller": {"source": "environment", "authenticated": False,
                                                       "status": "valid", "id": "caller"},
            "result": result, "reason": None, "request_sent": result == "verified_absent",
            "receiver_refused": False,
            "completion": "read" if operation == "resolve" else "verified_absent" if result == "verified_absent" else "not_sent",
            "resolved": {"id": ident, "started_millis": 123, "attached": selector.get("attached", False)} if count == 1 else None,
            "census": {"complete": True, "census_count": count, "matches": count, "possible": 0,
                       "candidates": rows,
                       "candidates_truncated": False},
            **({"operation_id": action["operation_id"], "duplicate": False} if operation == "end" else {})}


class SelectorSyntax(unittest.TestCase):
    def test_literal_forms_preserve_discriminator_case_words_and_qualifiers(self):
        for selector in ({"id": "-X"}, {"id_prefix": "."}, {"id_prefix": "Ab_"},
                         {"startup_words": ["Sleep", "é🚀"], "attached": False}):
            for operation in ("resolve", "end"):
                action = {"operation": operation, "selector": selector}
                if operation == "end":
                    action["operation_id"] = "op_7"
                request = f"{operation} selector " + json.dumps(selector, ensure_ascii=True)
                if operation == "end":
                    request += " operation op_7"
                self.assertEqual(pty_job.parse(request), action)
                self.assertEqual(pty_job.structured(action), action)

    def test_unknown_fields_types_duplicates_dynamic_and_extra_words_refuse_whole(self):
        invalid = [{"id": "x", "force": True}, {"id": "x", "id_prefix": "x"}, {"id": "x", "attached": 0},
                   {"startup_words": ["sleep 5"]}, {"startup_words": ["x\0y"]},
                   {"startup_words": ["x\x1by"]}, {"startup_words": []}, {"id": "."},
                   {"title": "x"}, {"current_process": "sleep"}, {"startup_words": ["x"] * 65}]
        for selector in invalid:
            with self.subTest(selector=selector), self.assertRaises(pty_job.Refused):
                pty_job.structured({"operation": "end", "selector": selector, "operation_id": "op"})
        for text in ('end selector {"id":"x","id":"y"} operation op',
                     'do not end selector {"id":"x"} operation op',
                     'Sam says end selector {"id":"x"} operation op',
                     'end selector {"id":"x"} operation op if idle',
                     'end selector {"id":"x"} operation op and list sessions',
                     'resolve selector {"id_prefix":"x"} operation op'):
            with self.subTest(text=text), self.assertRaises(pty_job.Refused):
                pty_job.parse(text)
        for request in ({"operation": "end", "selector": {"id": "x"}, "operation_id": "op", "caller": "override"},
                        {"operation": "resolve", "selector": {"id": "x"}, "operation_id": "op"}):
            with self.assertRaises(pty_job.Refused):
                pty_job.structured(request)

    def test_backend_transmits_original_selector_and_key_without_status_or_selected_id(self):
        action = pty_job.structured({"operation": "end", "selector": {"id_prefix": "MiX", "attached": False},
                                    "operation_id": "key", "timeout_seconds": 0.3})
        argv = pty_backend.argv_for(action)
        self.assertEqual(json.loads(argv[argv.index("--selector-json") + 1]), action["selector"])
        self.assertEqual(argv[argv.index("--operation-id") + 1], "key")
        self.assertNotIn("kill", argv); self.assertNotIn("status", argv); self.assertIn("--yes", argv)
        self.assertEqual(argv[:3], ["pty", "--timeout", "0.3"])


class SelectorTools(unittest.TestCase):
    def run_action(self, action, result="ambiguous", **kwargs):
        calls = []
        def backend(argv, wall=None):
            calls.append(argv)
            return pty_backend.Reply("ok", 3 if result == "ambiguous" else 0, document(action, result))
        answer = pty_cli._run_supplied(action, backend=backend, environ={"KITTY_PTY_BROKER_SESSION": "caller"}, **kwargs)
        return answer, calls

    def test_end_ambiguity_is_single_selector_call_and_no_fallback(self):
        action = {"operation": "end", "selector": {"id_prefix": "original"}, "operation_id": "key"}
        answer, calls = self.run_action(action, assume_yes=True, agent=True)
        self.assertEqual((answer["status"], answer["result"]["selector"]), (3, action["selector"]))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "end")

    def test_read_and_consent_tools_refuse_without_backend(self):
        action = {"operation": "end", "selector": {"startup_words": ["sleep", "60"], "attached": False}, "operation_id": "key"}
        for args in ({"reads_only": True}, {"agent": True}):
            answer, calls = self.run_action(action, **args)
            self.assertNotEqual(answer["status"], 0)
            self.assertEqual(calls, [])
        for env in ({}, {"KITTY_PTY_BROKER_SESSION": "../bad"}):
            answer = pty_cli.run(action, assume_yes=True, agent=True, environ=env,
                                 backend=lambda *a, **k: self.fail("unknown caller cannot dispatch"))
            self.assertEqual(answer["status"], 3)
            self.assertFalse(answer["caller"]["authenticated"])

    def test_plan_only_previews_then_preserves_original_end_arguments(self):
        action = {"operation": "end", "selector": {"id_prefix": "keep", "attached": True}, "operation_id": "key"}
        calls = []
        def backend(argv, wall=None):
            calls.append(argv)
            preview = document(action, "ambiguous", "resolve")
            preview["caller"].update(status="missing", id=None)
            return pty_backend.Reply("ok", 0, preview)
        answer = pty_cli.run(action, dry_run=True, agent=True, environ={}, backend=backend)
        self.assertEqual((answer["status"], len(calls), calls[0][1]), (0, 1, "resolve"))
        self.assertNotIn("--operation-id", calls[0])
        self.assertEqual(json.loads(answer["plan"]["argv"][3]), action["selector"])
        self.assertEqual(answer["preview"]["result"], "ambiguous")

    def test_new_receipt_checks_uncertainty_receiver_refusal_and_no_false_success(self):
        action = {"operation": "end", "selector": {"id": "target", "attached": False}, "operation_id": "key"}
        success = document(action, "verified_absent")
        self.assertEqual(pty_backend.checked(action, success, 0), success)
        refused = {**success, "result": "refused", "reason": "attachment_mismatch",
                   "receiver_refused": True, "completion": "receiver_refused"}
        self.assertEqual(pty_backend.checked(action, refused, 3), refused)
        uncertain = {**success, "result": "uncertain", "reason": "terminate_timed_out", "completion": "unknown"}
        self.assertEqual(pty_backend.checked(action, uncertain, 1), uncertain)
        for mutate in (lambda d: d.update(selector={"id": "different"}),
                       lambda d: d.update(operation_id="different"),
                       lambda d: d.update(request_sent=False),
                       lambda d: d.update(resolved=None),
                       lambda d: d["census"].update(possible=1),
                       lambda d: d["caller"].update(authenticated=True),
                       lambda d: d["census"].update(candidates=[{}] * 33)):
            bad = copy.deepcopy(success); mutate(bad)
            with self.assertRaises(ValueError):
                pty_backend.checked(action, bad, 0)

    def test_mcp_schema_contains_new_fields_with_bounded_three_tool_menu(self):
        for tool in mcp_server.PTY_TOOL_LIST:
            prop = tool["inputSchema"]["properties"]["request"]["oneOf"][1]["properties"]
            self.assertIn("resolve", prop["operation"]["enum"])
            self.assertIn("selector", prop)
            self.assertEqual(prop["selector"]["additionalProperties"], False)
            if tool["name"] != "kilix_pty_read":
                self.assertIn("end", prop["operation"]["enum"])
                self.assertIn("operation_id", prop)
            self.assertLessEqual(len(json.dumps(tool, separators=(",", ":")).encode()), 2048)
            self.assertLessEqual(len(tool["description"].encode()), 800)
            self.assertIn("Preserve the original discriminator and qualifiers", tool["description"])
        self.assertEqual(len(mcp_server.PTY_TOOL_LIST), 3)
        self.assertFalse(any(t["name"].startswith("kilix_pty") for t in mcp_server.TOOL_LIST))


class RealSelectorIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        kilix = os.environ.get("SELECTOR_KILIX_SOURCE")
        broker = os.environ.get("SELECTOR_BROKER_SOURCE")
        if not kilix or not broker:
            raise unittest.SkipTest("set private SELECTOR_KILIX_SOURCE and SELECTOR_BROKER_SOURCE")
        cls.kilix = Path(kilix)
        cls.broker = Path(broker)
        subprocess.run(["make", "-s", "-C", str(cls.broker)], check=True, timeout=60)
        spec = importlib.util.spec_from_file_location("private_broker_fixture", cls.broker / "tests/test_selector.py")
        cls.support = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.support)
        cls.support.CLI = os.environ.get("SELECTOR_BROKER_EXECUTABLE", str(cls.broker / "build/kitty-pty-broker"))

    def setUp(self):
        self.fx = self.support.RealBroker("runTest"); self.fx.setUp()
        environment = {"PATH": os.environ["PATH"], "HOME": str(self.fx.root / "home"), "TMPDIR": os.environ["TMPDIR"],
                       "KILIX_NEEDLE_KILIX": str(self.kilix / "kilix"),
                       "KILIX_STORAGE_HOME": str(self.fx.root / "storage"),
                       "KITTY_PTY_BROKER_EXECUTABLE": self.support.CLI,
                       "KITTY_PTY_BROKER_RUNTIME": str(self.fx.runtime), "KITTY_PTY_BROKER_SESSION": "caller_unrelated",
                       "XDG_RUNTIME_DIR": str(self.fx.root / "xdg")}
        self.patch = mock.patch.dict(os.environ, environment, clear=True); self.patch.start()
        self.addCleanup(self.patch.stop)

    def controller(self):
        import sys
        sys.path.insert(0, str(self.kilix / "config"))
        import kilix_pty
        import kilix_pty_controller
        return kilix_pty_controller.Controller(
            kilix_pty.Broker(self.support.CLI, str(self.fx.runtime), "0.3", 5),
            str(self.fx.root / "controller-state"))

    def select(self, controller, action):
        return controller.select({"schema": "kilix.pty.request/v2", "verb": "end",
                                  "selector": action["selector"], "operation_id": action["operation_id"]})

    def tearDown(self):
        self.fx.tearDown()

    def test_real_grammar_and_mcp_require_direct_selection(self):
        self.fx.start("n1.a"); self.fx.start("n1.b")
        c = self.controller()
        ambiguous = {"operation": "end", "selector": {"id_prefix": "n1."}, "operation_id": "ambiguous"}
        self.assertFalse(self.select(c, ambiguous)["issued"])
        record = pty_cli.mcp({"request": ambiguous, "confirm_risky": True}, controller=c)
        self.assertEqual(record["result"]["reason"], "authority_required")
        self.assertTrue(self.fx.alive("n1.a")); self.assertTrue(self.fx.alive("n1.b"))
        exact = {"operation": "end", "selector": {"id": "n1.b"}, "operation_id": "explicit"}
        self.assertTrue(self.select(c, exact)["issued"])
        record = pty_cli.mcp({"request": exact, "confirm_risky": True}, controller=c)
        self.assertEqual((record["status"], record["result"]["result"]), (0, "verified_absent"))
        self.assertFalse(record["result"]["caller"]["authenticated"])
        self.assertTrue(self.fx.alive("n1.a"))

    def test_raw_cli_then_server_handle_preserve_selector_and_replay(self):
        self.fx.start("raw.a"); self.fx.start("raw.b")
        action = {"operation": "end", "selector": {"id_prefix": "raw.", "attached": False},
                  "operation_id": "raw-cli"}
        cli = Path(__file__).resolve().parents[1] / "kilix-needle"
        result = subprocess.run([str(cli), "pty", "--agent", "--yes", "--json",
                                 "--request-json", json.dumps(action)],
                                capture_output=True, text=True, timeout=10)
        self.assertTrue(result.stdout, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual((result.returncode, record["refused"]), (3, "authority_required"))
        self.assertEqual(record["request"], action)
        server = mcp_server.Server(lambda *args: self.fail("PTY must not load a model"), tools="pty")
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "kilix_pty_act", "arguments": {
                                      "request": action, "confirm_risky": True}}})
        record = response["result"]["structuredContent"]
        self.assertEqual(record["refused"], "authority_required")
        self.assertTrue(self.fx.alive("raw.a")); self.assertTrue(self.fx.alive("raw.b"))

    def test_real_command_and_detached_then_retry_replays(self):
        self.fx.start("attached", ["/bin/sh", "-c", "sleep 55"])
        self.fx.start("detached", ["/bin/sh", "-c", "sleep 55"])
        self.fx.attach("attached")
        action = {"operation": "end", "selector": {"startup_words": ["sleep", "55"], "attached": False},
                  "operation_id": "command"}
        c = self.controller()
        self.assertTrue(self.select(c, action)["issued"])
        first = pty_cli.mcp({"request": action, "confirm_risky": True}, controller=c)
        self.assertEqual((first["status"], first["result"]["resolved"]["id"]), (0, "detached"))
        second = pty_cli.mcp({"request": action, "confirm_risky": True}, controller=c)
        self.assertTrue(second["result"]["duplicate"])
        self.assertTrue(self.fx.alive("attached"))


if __name__ == "__main__":
    unittest.main()
