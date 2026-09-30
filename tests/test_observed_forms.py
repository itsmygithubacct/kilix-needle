"""Product regressions from exported development requests, with synthetic paths."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from support import FakeKilix, desktop
import agents
import agents_kilix
import mcp_server
import needle_cli
import panes_exact


LAUNCHES = (
    "Open a new tab in {dir} and start an interactive Codex coding-agent session there. "
    "Leave the session with no task or prompt.",
    "Start an interactive Codex coding-agent session in a new tab, working directory {dir}. "
    "Do not give it any task or prompt; leave it at its interactive prompt.",
    "Start an interactive Codex coding-agent session in a new tab, working in the directory {dir}. "
    "Do not give it a task. Leave the session at its interactive prompt, then stop.",
    "Start an interactive Codex coding-agent session in a new tab, working in {dir}. "
    "Leave the session without a task.",
    "Start an interactive Codex coding-agent session in a new tab, working in the directory {dir}. "
    "Leave the new session without a task.",
)
SPLIT = "Open a new pane directly to the right of the pane I am running in and run a shell there."


class LaunchForms(unittest.TestCase):
    def test_each_launch_keeps_directory_and_omits_task_on_cli_and_mcp(self):
        with tempfile.TemporaryDirectory(prefix="kn-launch-forms-") as temp:
            directory = Path(temp) / ("project-" + "x" * 90)
            directory.mkdir()
            with mock.patch.object(agents_kilix, "caller_info", return_value=(300, "a" * 16, temp)), \
                    mock.patch.object(agents_kilix, "_run", side_effect=AssertionError("plan must not launch")):
                for template in LAUNCHES:
                    request = template.format(dir=directory)
                    with self.subTest(request=request):
                        self.assertEqual(agents.exact_calls(request), [{"name": "agent", "arguments":
                            {"agent": "codex", "dir": str(directory)}}])
                        engine = mock.Mock(side_effect=AssertionError("exact launch must not load a model"))
                        engine.complete.side_effect = AssertionError("exact launch must not use a model")
                        records = [needle_cli.run_request(engine, request, needle_cli.Options(dry_run=True)),
                                   needle_cli.run_agents_request(engine, request, needle_cli.Options(dry_run=True))]
                        server = mcp_server.Server(engine)
                        for tool in ("kilix_plan", "kilix_agents_plan"):
                            records.append(server.call_tool(tool, {"request": request})["structuredContent"])
                        for record in records:
                            self.assertEqual(record["status"], 0)
                            self.assertEqual(len(record["items"]), 1)
                            item = record["items"][0]
                            self.assertEqual(item["outcome"], "would")
                            self.assertEqual(item["args"], {"agent": "codex", "dir": str(directory)})
                            self.assertEqual(item["argv"][:2], ["agent-control", "new-tab"])
                            self.assertEqual(item["argv"][item["argv"].index("--cwd") + 1], str(directory))
                            self.assertFalse(any(arg.startswith("--prompt") for arg in item["argv"]))
                        engine.assert_not_called()
                        engine.complete.assert_not_called()

    def test_other_actions_conditions_and_task_contradictions_still_refuse(self):
        for suffix in (". Leave the session without a task and close tab 2.",
                       ". Leave the session at its interactive prompt if ready.",
                       ". Leave the new session without a task tomorrow.",
                       ". Leave the other session without a task.",
                       " to fix the build. Leave the session without a task.",
                       " to fix the build. Leave the new session with no task or prompt.",
                       ". Do not give it a task; leave it at its interactive prompt and delete it."):
            with self.subTest(suffix=suffix):
                self.assertIsNone(agents.exact_calls("start codex in /tmp/project" + suffix))
        request = "start codex in /tmp/project: leave the session without a task"
        self.assertEqual(agents.exact_calls(request)[0]["arguments"]["prompt"],
                         "leave the session without a task")
        request = "start codex in /tmp/project: review working directory /tmp/other"
        self.assertEqual(agents.exact_calls(request)[0]["arguments"]["prompt"],
                         "review working directory /tmp/other")


class PaneForms(unittest.TestCase):
    def setUp(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def test_typing_retains_literal_and_separate_enter_without_model(self):
        command = "touch /tmp/product-marker-" + "x" * 140
        request = "In the pane titled build, type and press Enter on: " + command
        self.assertEqual(panes_exact.admitted(request)[0], [{"name": "run_in_pane", "arguments":
                         {"pane": "build", "command": command}}])
        engine = mock.Mock(side_effect=AssertionError("no model"))
        engine.complete.side_effect = AssertionError("no model")
        for route in ("cli", "mcp"):
            with self.subTest(route=route), FakeKilix(desktop()) as fake:
                if route == "cli":
                    record = needle_cli.run_request(engine, request, needle_cli.Options(agent=True, assume_yes=True))
                else:
                    record = mcp_server.Server(engine).call_tool("kilix_act", {
                        "request": request, "confirm_risky": True})["structuredContent"]
                self.assertEqual(record["status"], 0)
                self.assertEqual(record["items"][0]["outcome"], "done")
                sent = [data for argv, data in fake.calls() if argv[0] == "send-text"]
                self.assertEqual(sent[-2:], [command.encode(), b"\r"])
        engine.assert_not_called()
        engine.complete.assert_not_called()

    def test_shell_there_uses_caller_split_without_a_program(self):
        self.assertEqual(panes_exact.admitted(SPLIT),
                         ([{"name": "open_pane", "arguments": {"side": "right"}}], "split right"))
        engine = mock.Mock(side_effect=AssertionError("no model"))
        with FakeKilix(desktop()):
            record = mcp_server.Server(engine).call_tool("kilix_plan", {"request": SPLIT})["structuredContent"]
        self.assertEqual(record["status"], 0)
        self.assertEqual(record["items"][0]["args"], {"side": "right"})
        engine.assert_not_called()

    def test_new_forms_still_refuse_missing_text_and_extra_actions(self):
        for request in ("In the pane titled build, type and press Enter on:",
                        'In the pane titled build, type and press Enter on: ""'):
            with self.subTest(request=request):
                self.assertTrue(panes_exact.missing_command(request))
                self.assertIsNone(panes_exact.admitted(request))
                engine = mock.Mock()
                with FakeKilix(desktop()) as fake:
                    record = needle_cli.run_request(engine, request, needle_cli.Options(agent=True, assume_yes=True))
                    self.assertEqual(record["items"][0]["outcome"], "refused")
                    self.assertEqual(fake.calls(), [])
                engine.complete.assert_not_called()
        for request in ("In the pane titled build, type and press Enter on: touch /tmp/x if idle",
                        "In the pane titled build, type and press Enter on: touch /tmp/x and close tab 2",
                        SPLIT.rstrip(".") + " if idle.",
                        SPLIT.rstrip(".") + " and close this pane.",
                        SPLIT.replace("a shell there", "a shell in the logs pane"),
                        SPLIT.replace("a shell there", "vim there")):
            with self.subTest(request=request):
                self.assertIsNone(panes_exact.admitted(request))


if __name__ == "__main__":
    unittest.main()
