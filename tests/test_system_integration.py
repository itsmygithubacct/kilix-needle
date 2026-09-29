"""Entry-point guarantees for the system grammar and planning-only fallback."""
import io
import json
import unittest
from unittest import mock

import support  # noqa: F401
import mcp_server
import needle_cli
import system_collect
import system_job


UNKNOWN = "please list the biggest memory processes"
PROPOSAL = {"function_calls": [{"name": "processes", "arguments": {"sort": "memory"}}]}


class Runtime:
    def __init__(self, reply):
        self.reply = reply
        self.reset_count = 0
        self.complete_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def reset(self):
        self.reset_count += 1

    def complete(self, _request):
        self.complete_count += 1
        return self.reply


class CliIntegration(unittest.TestCase):
    def test_default_recognized_plan_skips_runtime_and_collector(self):
        with mock.patch.object(needle_cli, "open_runtime", side_effect=AssertionError("runtime opened")), \
             mock.patch.object(system_collect.Collector, "collect", side_effect=AssertionError("collected")), \
             mock.patch.object(needle_cli, "handle", wraps=needle_cli.handle) as handle, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(needle_cli.main(["system", "--dry-run", "show memory"]), 0)
        self.assertIsInstance(handle.call_args.args[0], system_job.Baseline)

    def test_default_unknown_refuses_without_runtime(self):
        with mock.patch.object(needle_cli, "open_runtime", side_effect=AssertionError("runtime opened")), \
             mock.patch.object(system_collect.Collector, "collect", side_effect=AssertionError("collected")), \
             mock.patch.object(needle_cli, "handle", wraps=needle_cli.handle), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(needle_cli.main(["system", UNKNOWN]), 1)

    def test_suggest_recognized_skips_runtime_and_collector_without_dry_run(self):
        output = io.StringIO()
        with mock.patch.object(needle_cli, "open_runtime", side_effect=AssertionError("runtime opened")), \
             mock.patch.object(system_collect.Collector, "collect", side_effect=AssertionError("collected")), \
             mock.patch("sys.stdout", output):
            self.assertEqual(needle_cli.main(["system", "--suggest", "--json", "show memory"]), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual((plan["path"], plan["trust"]), ("grammar", "grammar"))

    def test_suggest_unknown_uses_full_call_runtime_once_and_never_collects(self):
        runtime = Runtime(PROPOSAL)
        output = io.StringIO()
        with mock.patch.object(needle_cli, "open_runtime", return_value=runtime) as factory, \
             mock.patch.object(system_collect.Collector, "collect", side_effect=AssertionError("collected")), \
             mock.patch("sys.stdout", output):
            self.assertEqual(needle_cli.main(["system", "--suggest", "--json", UNKNOWN]), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual((plan["path"], plan["trust"]), ("model", "model_proposal"))
        self.assertEqual(plan["actions"][0][0], "processes")
        self.assertEqual((runtime.reset_count, runtime.complete_count), (1, 1))
        factory.assert_called_once()

    def test_suggest_unavailable_reports_error(self):
        output = io.StringIO()
        with mock.patch.object(needle_cli, "open_runtime", side_effect=RuntimeError("missing model")), \
             mock.patch("sys.stdout", output):
            self.assertEqual(needle_cli.main(["system", "--suggest", "--json", UNKNOWN]), 1)
        self.assertTrue(json.loads(output.getvalue())["runtime_error"])

    def test_suggest_human_errors_give_reasons(self):
        for reply, expected in ((RuntimeError("missing model"), "classifier failed: RuntimeError"),
                                ({"function_calls": "invalid"}, "missing function_calls list")):
            with self.subTest(reply=reply):
                error = io.StringIO()
                runtime_factory = mock.Mock(side_effect=reply) if isinstance(reply, Exception) \
                    else mock.Mock(return_value=Runtime(reply))
                with mock.patch.object(needle_cli, "open_runtime", runtime_factory), \
                     mock.patch("sys.stderr", error):
                    self.assertEqual(needle_cli.main(["system", "--suggest", UNKNOWN]), 1)
                self.assertIn(expected, error.getvalue())
                self.assertNotIn(": True", error.getvalue())

    def test_suggest_human_output_escapes_controls(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            needle_cli._render_system_suggestion({"path": "model", "trust": "model_proposal",
                "actions": [["resources", {"path": "/tmp/\x1b[2J", "kind": "disk"}]],
                "reasons": ["unsafe\x1b[2J"]})
        self.assertNotIn("\x1b", output.getvalue())
        self.assertIn("\\u001b", output.getvalue())

    def test_incompatible_flags_and_missing_request(self):
        for args in (["--suggest", "--baseline", "show memory"], ["--suggest"]):
            with self.subTest(args=args), mock.patch("sys.stderr", new_callable=io.StringIO), \
                 self.assertRaises(SystemExit) as error:
                needle_cli.main(["system", *args])
            self.assertEqual(error.exception.code, 2)


class McpIntegration(unittest.TestCase):
    def test_registered_suggest_schema(self):
        spec = next(tool for tool in mcp_server.TOOL_LIST if tool["name"] == "kilix_system_suggest")
        self.assertEqual(spec["inputSchema"]["required"], ["request"])
        self.assertEqual(set(spec["inputSchema"]["properties"]), {"request"})
        self.assertFalse(spec["inputSchema"]["additionalProperties"])

    def test_default_plan_and_suggest_recognized_skip_runtime(self):
        factory = mock.Mock(side_effect=AssertionError("runtime opened"))
        server = mcp_server.Server(factory)
        with mock.patch.object(system_collect.Collector, "collect", side_effect=AssertionError("collected")):
            result = server.call_tool("kilix_system_plan", {"request": "show memory"})
            suggestion = server.call_tool("kilix_system_suggest", {"request": "show memory"})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["items"][0]["outcome"], "would")
        self.assertEqual(suggestion["structuredContent"]["trust"], "grammar")
        factory.assert_not_called()

    def test_suggest_unknown_calls_system_runtime_and_never_collects(self):
        runtime = Runtime(PROPOSAL)
        factory = mock.Mock(return_value=runtime)
        server = mcp_server.Server(factory)
        with mock.patch.object(system_collect.Collector, "collect", side_effect=AssertionError("collected")):
            result = server.call_tool("kilix_system_suggest", {"request": UNKNOWN})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["trust"], "model_proposal")
        factory.assert_called_once_with("system")
        self.assertEqual((runtime.reset_count, runtime.complete_count), (1, 1))

    def test_suggest_rejects_extra_arguments(self):
        server = mcp_server.Server(mock.Mock())
        for arguments in ({"request": "show memory", "baseline": True},
                          {"request": []}, {}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                server.call_tool("kilix_system_suggest", arguments)


if __name__ == "__main__":
    unittest.main()
