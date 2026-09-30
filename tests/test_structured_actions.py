"""Structured transport and consent contracts; fakes only, no ambient panes."""
import io
import json
import os
from pathlib import Path
import subprocess
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
