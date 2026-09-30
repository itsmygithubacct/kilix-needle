"""Observed launch retries and typing requests with a stripped command."""
import unittest
from unittest import mock

from support import FakeKilix, desktop
import agents
import mcp_server
import needle_cli
import panes_exact


LAUNCHES = (
    "Open a new tab in /tmp/project and start an interactive Codex coding-agent "
    "session there. Do not assign it a task. Stop after launching the session.",
    "Open a new tab running codex in directory /tmp/project.",
)
MISSING = (
    "In the pane titled bench-target, type and press Enter.",
    "Type in the pane titled bench-target and press Enter.",
    "In the pane titled bench-target, type the command and press Enter.",
    'In the pane titled bench-target, type "" and press Enter.',
    "Type and press Enter in pane 70.",
    "Type exactly: in pane 70 and press Enter.",
    "Type in pane 70 and press Enter. Then stop.",
)


class LaunchCoverage(unittest.TestCase):
    def test_both_observed_requests_read_as_one_launch_with_no_task(self):
        expected = [{"name": "agent", "arguments": {"agent": "codex", "dir": "/tmp/project"}}]
        for request in LAUNCHES:
            with self.subTest(request=request):
                self.assertEqual(agents.exact_calls(request), expected)

    def test_cli_jobs_and_mcp_launch_without_loading_a_model(self):
        for request in LAUNCHES:
            with self.subTest(request=request), mock.patch("agents_kilix.perform", return_value=[]):
                for run in (needle_cli.run_request, needle_cli.run_agents_request):
                    engine = mock.Mock(side_effect=AssertionError("model loaded"))
                    record = run(engine, request, needle_cli.Options(dry_run=True, agent=True))
                    self.assertEqual(record["status"], 0)
                    engine.reset.assert_not_called()
                    engine.complete.assert_not_called()
                factory = mock.Mock(side_effect=AssertionError("model loaded"))
                server = mcp_server.Server(factory)
                result = server.call_tool("kilix_agents_plan", {"request": request})
                self.assertFalse(result["isError"])
                factory.assert_not_called()

    def test_stop_does_not_drop_a_real_action_or_infer_a_directory(self):
        for request in (
                "start codex in /tmp/project. Stop after deleting the repo.",
                "start codex in /tmp/project. Stop the session after launching it.",
                "start codex in /tmp/project. Stop after launching the session and delete it.",
                "start codex in /tmp/project to fix the tests. Do not assign it a task. "
                "Stop after launching the session.",
                "Open a new tab running codex."):
            with self.subTest(request=request):
                self.assertIsNone(agents.exact_calls(request))


class MissingCommand(unittest.TestCase):
    def test_blank_typing_never_becomes_a_canonical_command(self):
        for request in MISSING:
            with self.subTest(request=request):
                self.assertIsNone(panes_exact.admitted(request))

    def test_cli_refuses_before_model_access_and_sends_nothing(self):
        for request in MISSING:
            with self.subTest(request=request), FakeKilix(desktop()) as fake:
                engine = mock.Mock()
                record = needle_cli.run_request(
                    engine, request, needle_cli.Options(agent=True, assume_yes=True))
                self.assertEqual(record["status"], 1)
                self.assertEqual(record["items"][0]["outcome"], "refused")
                self.assertIn("command", record["note"])
                self.assertIn("single-quote", record["hint"])
                self.assertEqual(fake.calls(), [])
                engine.reset.assert_not_called()
                engine.complete.assert_not_called()

    def test_mcp_refuses_even_with_confirmation_without_loading_engine(self):
        factory = mock.Mock(side_effect=AssertionError("model loaded"))
        with FakeKilix(desktop()) as fake:
            result = mcp_server.Server(factory).call_tool(
                "kilix_act", {"request": MISSING[0], "confirm_risky": True})
            self.assertEqual(result["structuredContent"]["status"], 1)
            self.assertEqual(result["structuredContent"]["items"][0]["outcome"], "refused")
            self.assertEqual(fake.calls(), [])
        factory.assert_not_called()

    def test_explicit_commands_and_colon_payloads_still_read_verbatim(self):
        for command in ("and", "in", "printf hello", "touch /tmp/marker"):
            request = f'In the pane titled bench-target, type "{command}" and press Enter.'
            with self.subTest(command=command):
                self.assertEqual(panes_exact.admitted(request)[0][0]["arguments"]["command"], command)
        request = "Type this command in the pane titled bench-target and press Enter: touch /tmp/marker"
        self.assertEqual(panes_exact.admitted(request)[0][0]["arguments"]["command"], "touch /tmp/marker")


if __name__ == "__main__":
    unittest.main()
