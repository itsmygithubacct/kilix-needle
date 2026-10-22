"""Structured transport and consent contracts; fakes only, no ambient panes."""
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import action_backend as backend
import action_cli
import mcp_server
import needle_cli


def request(operation="agent.deliver", **params):
    defaults = {"agent.deliver": {"text": "run tests"}, "operation.status": {},
                "pane.open": {"argv": ["/bin/sh"], "cwd": "/tmp", "title": "build", "placement": "split"},
                "agent.launch": {"agent": "codex", "cwd": "/tmp", "title": "worker", "placement": "new-tab"}}
    return {"schema": backend.SCHEMA, "operation_id": "op-1", "operation": operation,
            "source": {"pane_id": 1, "broker": "a" * 16},
            "target": {"pane_id": 2, "broker": "b" * 16}, "params": {**defaults[operation], **params}}


def receipt(req, status=None):
    status = status or ("planned" if req["dry_run"] else "submitted")
    out = backend.error(req, "")
    out.pop("error")
    out.update(status=status, identity_verified=True)
    if status == "created":
        out.update(pane_created_verified=True, evidence={"pane": {
            "pane_id": 3, "broker": "c" * 16, "tab_id": 1, "os_window_id": 1}, "prompt_passed": False})
    if status in ("submitted", "deferred"):
        out.update(delivery_verified=True, evidence={"verification": "screen", "verified_at": 123})
    return out


class Adapters(unittest.TestCase):
    def test_finish_staged_id_is_schema_validated_and_survives_fake_dispatch(self):
        old_message_id = "message-001"
        req = request(finish_staged_message_id=old_message_id)
        req["operation_id"] = "finish-002"
        forwarded = []

        def fake_process(dispatched):
            forwarded.append(json.loads(json.dumps(dispatched)))
            return json.dumps(receipt(dispatched)).encode(), 0

        with mock.patch.object(backend, "_process", side_effect=fake_process) as process:
            result = action_cli.run(req, assume_yes=True)

        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["operation_id"], "finish-002")
        self.assertEqual(forwarded[0]["operation_id"], "finish-002")
        self.assertEqual(forwarded[0]["params"]["finish_staged_message_id"], old_message_id)
        process.assert_called_once()

        schema = action_cli.request_schema()
        field = schema["properties"]["params"]["properties"]["finish_staged_message_id"]
        self.assertEqual(field["type"], "string")
        self.assertEqual((field["minLength"], field["maxLength"]), (1, 64))
        self.assertEqual(field["pattern"], r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(?![\s\S])")
        field_pattern = re.compile(field["pattern"])
        self.assertIsNotNone(field_pattern.search(old_message_id))
        self.assertIsNone(field_pattern.search(old_message_id + "\n"))
        self.assertIsNone(field_pattern.search("é"))
        self.assertIn("new action ID", field["description"])
        for operation in ("pane.open", "agent.launch"):
            branch = next(rule for rule in schema["allOf"]
                          if rule.get("if", {}).get("properties", {}).get("operation", {}).get("const") == operation)
            self.assertEqual(branch["then"]["properties"]["params"]["not"]["required"],
                             ["finish_staged_message_id"])
        plan_schema = action_cli.request_schema(plan=True)
        self.assertTrue(any("not" in rule and
                            rule["not"].get("properties", {}).get("params", {}).get("required") ==
                            ["finish_staged_message_id"]
                            for rule in plan_schema["allOf"]))
        plan_tool = next(tool for tool in mcp_server.TOOL_LIST
                         if tool["name"] == "kilix_action_plan")
        self.assertEqual(plan_tool["inputSchema"]["properties"]["request"], plan_schema)
        status_schema = action_cli.request_schema(status=True)
        self.assertEqual(status_schema["properties"]["params"]["properties"], {})

    def test_finish_staged_invalid_requests_refuse_before_fake_backend(self):
        invalid_ids = (None, 17, "", "bad/id", "line\n", "x" * 65, "é")
        calls = []
        fake_backend = mock.Mock(side_effect=lambda value: calls.append(value) or receipt(value))
        for message_id in invalid_ids:
            with self.subTest(message_id=message_id):
                out = action_cli.run(request(finish_staged_message_id=message_id),
                                     assume_yes=True, backend=fake_backend)
                self.assertEqual(out["status"], "blocked")
                self.assertIn("finish_staged_message_id", out["error"])

        for bad_request, plan in (
                ({**request(finish_staged_message_id="message-001"), "dry_run": True}, False),
                (request(finish_staged_message_id="message-001"), True),
                (request("pane.open", finish_staged_message_id="message-001"), False),
                ({**request(finish_staged_message_id="message-001"), "extra": True}, False)):
            with self.subTest(request=bad_request, plan=plan):
                out = action_cli.run(bad_request, dry_run=plan, assume_yes=True,
                                     backend=fake_backend)
                self.assertEqual(out["status"], "blocked")
        fake_backend.assert_not_called()
        self.assertEqual(calls, [])

    def test_actions_menu_dispatches_only_selected_tools_without_loading_model(self):
        factory = mock.Mock(side_effect=AssertionError("model loaded"))
        server = mcp_server.Server(factory, tools="actions")
        full = mcp_server.Server(factory)
        listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertEqual(names, ["kilix_action_plan", "kilix_action_act", "kilix_action_status"])
        self.assertEqual(len(full.handle({"id": 2, "method": "tools/list"})["result"]["tools"]), 19)
        for tool in mcp_server.TOOL_LIST:
            if tool["name"] in names:
                continue
            result = server.handle({"id": 3, "method": "tools/call",
                                    "params": {"name": tool["name"], "arguments": {"request": "split right"}}})
            self.assertEqual(result["error"]["code"], -32602)
        with mock.patch("action_backend.dispatch", side_effect=lambda req: receipt(req)) as dispatch:
            denied = server.call_tool("kilix_action_act", {"request": request()})
            self.assertTrue(denied["isError"])
            dispatch.assert_not_called()
            accepted = server.call_tool("kilix_action_act", {"request": request(), "confirm_risky": True})
            self.assertTrue(accepted["structuredContent"]["delivery_verified"])
            dispatch.assert_called_once()
        factory.assert_not_called()
        self.assertEqual(server._runtimes, {})

    def test_actions_selection_round_trips_through_stdio_cli(self):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "kilix_act", "arguments": {"request": "split right"}}}]
        done = subprocess.run([sys.executable, "-B", str(Path(needle_cli.__file__)), "mcp", "--tools", "actions"],
                              input="".join(json.dumps(m) + "\n" for m in messages),
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        replies = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual(len(replies[0]["result"]["tools"]), 3)
        self.assertEqual(replies[1]["error"]["code"], -32602)
        with self.assertRaises(ValueError):
            mcp_server.Server(mock.Mock(), tools="action")

    def test_empty_stdin_and_millisecond_timeout_never_dispatch(self):
        for raw in ("", " \n", json.dumps({**request(), "timeout": 30000})):
            with mock.patch("sys.stdin", io.StringIO(raw)), \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as output, \
                    mock.patch("action_backend.dispatch") as dispatch:
                self.assertEqual(needle_cli.main(["action", "--yes", "--request-json", "-"]), 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "blocked")
            self.assertIn("seconds" if raw.strip() else "pipe JSON", result["error"])
            dispatch.assert_not_called()

    def test_cli_mcp_parity_and_no_engine_or_asset_loading(self):
        server = mcp_server.Server(mock.Mock(side_effect=AssertionError("no model")))
        for operation in backend.OPERATIONS:
            req = request(operation)
            status = "not_found" if operation == "operation.status" else "submitted" if operation == "agent.deliver" else "created"
            for plan in (False, True):
                if plan and operation == "operation.status":
                    continue
                argv = ["action", "--request-json", "-", "--yes"] + (["--dry-run"] if plan else [])
                tool = "kilix_action_status" if operation == "operation.status" else "kilix_action_plan" if plan else "kilix_action_act"
                args = {"request": req, **({"confirm_risky": True} if tool.endswith("_act") else {})}
                with mock.patch("action_backend.dispatch", side_effect=lambda r: receipt(r, "planned" if plan else status)), \
                        mock.patch("needle_cli.open_runtime", side_effect=AssertionError("engine")), \
                        mock.patch("needle_cli.asset.install", side_effect=AssertionError("install")), \
                        mock.patch("sys.stdin", io.StringIO(json.dumps(req))), \
                        mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                    exit_code = needle_cli.main(argv)
                    mcp = server.call_tool(tool, args)
                cli = json.loads(output.getvalue())
                self.assertEqual(cli, mcp["structuredContent"])
                self.assertEqual(cli, json.loads(mcp["content"][0]["text"]))
                self.assertEqual(exit_code, 1 if status == "not_found" else 0)
                self.assertLess(len(output.getvalue()), 1200)
                self.assertNotIn(", ", output.getvalue())
        self.assertEqual(server._runtimes, {})

    def test_consent_and_unknown_fields_never_dispatch(self):
        for req in (request(), {**request(), "extra": True}, request(extra=True),
                    request("agent.launch", trust_folder=True), {**request("operation.status"), "dry_run": True}):
            with mock.patch("action_backend.dispatch") as dispatch:
                out = action_cli.run(req, agent=True)
            dispatch.assert_not_called()
            self.assertEqual(out["status"], "blocked")
        for args in ({"request": request(), "extra": True}, {"request": request(), "confirm_risky": "yes"}):
            with self.assertRaises(ValueError):
                action_cli.mcp(args)

    def test_status_is_read_only_and_pending_uncertain_are_not_completed(self):
        for state in ("pending", "uncertain", "not_found"):
            with mock.patch("action_backend.dispatch", side_effect=lambda r: receipt(r, state)) as dispatch:
                result = action_cli.mcp({"request": request("operation.status")}, status=True)
            self.assertEqual(result["status"], state)
            self.assertEqual(backend.exit_status(result), 1)
            self.assertFalse(result["completion_verified"])
            self.assertFalse(result["acknowledgment_verified"])
            dispatch.assert_called_once()

    def test_invalid_claims_and_boolean_identity_are_rejected(self):
        req = backend.validate(request("pane.open"))
        cases = []
        bad = receipt(req, "created")
        bad["source"]["pane_id"] = True
        cases.append(bad)
        for flag in ("pane_created_verified", "agent_startup_verified", "acknowledgment_verified", "completion_verified"):
            bad = receipt(req, "created")
            bad[flag] = flag != "pane_created_verified"
            cases.append(bad)
        bad = receipt(req, "created")
        bad["evidence"]["pane"]["broker"] = "bad"
        cases.append(bad)
        for bad in cases:
            with self.assertRaises(ValueError):
                backend.checked(bad, req)

    def test_duplicate_keys_and_unbounded_invalid_input_return_compact_failure(self):
        for raw in ('{"schema":"x","schema":"y"}', "x" * (backend.MAX_REQUEST + 1),
                    json.dumps({"source": {"data": "x" * 8000}, "operation_id": "x" * 8000})):
            with mock.patch("sys.stdin", io.StringIO(raw)), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(needle_cli.main(["action", "--request-json", "-"]), 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "blocked")
            self.assertLess(len(output.getvalue()), 1200)


class Discovery(unittest.TestCase):
    def test_selected_and_path_symlinks_never_execute_launcher(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            config = root / "config"
            config.mkdir(parents=True)
            launcher = root / "kilix"
            launcher.write_text("#!/bin/sh\nexit 99\n")
            launcher.chmod(0o755)
            bin_dir = Path(temp) / "bin"
            bin_dir.mkdir()
            (bin_dir / "kilix").symlink_to(launcher)
            (config / "agent_actions.py").write_text(
                "def dispatch(r):\n return {**r, 'bad':True}\n")
            for env in ({"KILIX_NEEDLE_KILIX": str(root)}, {"KILIX_NEEDLE_KILIX": str(launcher)},
                        {"PATH": str(bin_dir)}, {"KILIX_ACTION_MODULE_ROOT": str(config)}):
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(backend.module_root(), config)
                    result = backend.dispatch(backend.validate({**request(), "dry_run": True}))
                    self.assertEqual(result["status"], "blocked")
                    self.assertIn("receipt", result["error"])

    def test_controller_flood_and_timeout_are_bounded_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "agent_actions.py"
            path.write_text("def dispatch(r):\n print('x'*1000000)\n return {}\n")
            with mock.patch.dict(os.environ, {"KILIX_ACTION_MODULE_ROOT": temp}, clear=True):
                out = backend.dispatch(request())
            self.assertEqual(out["status"], "uncertain")
            self.assertIn("exceeds", out["error"])
            self.assertLess(len(json.dumps(out)), 1200)
        with mock.patch("action_backend._process", side_effect=subprocess.TimeoutExpired("worker", 20)):
            out = backend.dispatch(request())
        self.assertEqual(out["status"], "uncertain")
        self.assertIn("operation.status", out["error"])


if __name__ == "__main__":
    unittest.main()
