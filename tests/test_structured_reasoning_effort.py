"""Transport preserves typed reasoning effort without weakening receipts."""
import copy
import unittest
from unittest import mock
import action_backend as backend
import action_cli
import mcp_server
from test_structured_actions import request, receipt


class ReasoningEffortTests(unittest.TestCase):
    def test_schema_exposes_typed_effort(self):
        field = action_cli.request_schema()["properties"]["params"]["properties"]["reasoning_effort"]
        self.assertEqual(field["enum"], ["low", "medium", "high", "xhigh"])

    def test_cli_mcp_preserve_each_requested_setting(self):
        server = mcp_server.Server(mock.Mock(side_effect=AssertionError("no model")), tools="actions")
        for model, efforts in (("gpt-6.1-sol", ("low", "medium", "high", "xhigh")),
                              ("gpt-6-astra", ("low", "medium", "high"))):
            for effort in efforts:
                req = request("agent.launch", model=model, reasoning_effort=effort)
                def execute(normalized):
                    self.assertEqual(normalized["params"], req["params"])
                    out = receipt(normalized, "planned" if normalized["dry_run"] else "created")
                    out.setdefault("evidence", {})["requested"] = {
                        "agent": "codex", "model": model, "reasoning_effort": effort}
                    return backend.checked(out, normalized)
                with mock.patch.object(backend, "dispatch", side_effect=execute):
                    cli = action_cli.run(req, assume_yes=True)
                    mcp = server.call_tool("kilix_action_act", {"request": req, "confirm_risky": True})
                    plan = action_cli.run(req, dry_run=True)
                self.assertEqual(cli, mcp["structuredContent"])
                self.assertEqual(cli["status"], "created")
                self.assertEqual(plan["status"], "planned")
                self.assertFalse(cli["agent_startup_verified"])

    def test_bad_or_unsupported_values_refused_before_dispatch(self):
        bad = [request("agent.launch", reasoning_effort=e) for e in (None, True, 1, [], {}, "HIGH", "-c x=1")]
        bad += [request("agent.launch", agent="claude", reasoning_effort="high"),
                request("pane.open", reasoning_effort="high")]
        for req in bad:
            with mock.patch.object(backend, "dispatch") as dispatch:
                result = action_cli.run(req, assume_yes=True)
            self.assertEqual(result["status"], "blocked")
            dispatch.assert_not_called()

    def test_mismatched_or_missing_settings_are_not_accepted(self):
        req = backend.validate(request("agent.launch", model="gpt-6-astra", reasoning_effort="high"))
        good = receipt(req, "created")
        good["evidence"]["requested"] = {"agent": "codex", "model": "gpt-6-astra", "reasoning_effort": "high"}
        self.assertEqual(backend.checked(good, req), good)
        for key, value in (("model", "gpt-6.1-sol"), ("reasoning_effort", "low"), ("reasoning_effort", [])):
            bad = copy.deepcopy(good)
            bad["evidence"]["requested"][key] = value
            with self.assertRaises(ValueError):backend.checked(bad, req)
        good["evidence"].pop("requested")
        with self.assertRaisesRegex(ValueError, "omits"):
            backend.checked(good, req)

    def test_legacy_and_status_receipts_remain_compatible(self):
        req = backend.validate(request("agent.launch"))
        old = receipt(req, "created")
        self.assertEqual(backend.checked(old, req), old)
        old["evidence"]["requested"] = {"agent": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "high"}
        status = backend.validate(request("operation.status"))
        self.assertEqual(backend.checked(old, status), old)


if __name__ == "__main__":unittest.main()
