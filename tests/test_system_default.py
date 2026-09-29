"""Default system routing never collects a model-derived query."""
import io
import json
import unittest
from unittest import mock

import support  # noqa: F401
import mcp_server
import needle_cli
import system_collect


UNKNOWN = "please list the processes eating memory lately"
REPLY = {"function_calls": [{"name": "processes", "arguments": {"sort": "memory"}}]}


class Runtime:
    label = "tuned system fixture"

    def __init__(self):
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def reset(self):
        pass

    def complete(self, _request):
        self.calls += 1
        return REPLY


class DefaultRoutes(unittest.TestCase):
    def test_cli_unknown_is_proposal_even_with_yes(self):
        runtime, output = Runtime(), io.StringIO()
        with mock.patch.object(needle_cli, "open_runtime", return_value=runtime) as factory, \
             mock.patch.object(system_collect.Collector, "collect", side_effect=AssertionError("collected")), \
             mock.patch("sys.stdout", output):
            self.assertEqual(needle_cli.main(["system", "--yes", "--json", UNKNOWN]), 0)
        record = json.loads(output.getvalue())
        self.assertEqual(record["query_source"], runtime.label)
        self.assertFalse(record["collection_performed"])
        self.assertEqual(record["items"][0]["outcome"], "proposed")
        self.assertEqual(record["items"][0]["trust"], "model_proposal")
        self.assertEqual(runtime.calls, 1)
        factory.assert_called_once()

    def test_cli_baseline_unknown_skips_model(self):
        output = io.StringIO()
        with mock.patch.object(needle_cli, "open_runtime", side_effect=AssertionError("model opened")), \
             mock.patch.object(system_collect.Collector, "collect", side_effect=AssertionError("collected")), \
             mock.patch("sys.stdout", output):
            self.assertEqual(needle_cli.main(["system", "--baseline", "--json", UNKNOWN]), 1)
        record = json.loads(output.getvalue())
        self.assertEqual(record["query_source"], "system grammar baseline")
        self.assertFalse(record["collection_performed"])

    def test_cli_recognized_collects_without_model(self):
        output = io.StringIO()
        observation = {"data": {"memory_bytes": {"MemTotal": 1}}}
        with mock.patch.object(needle_cli, "open_runtime", side_effect=AssertionError("model opened")), \
             mock.patch.object(system_collect.Collector, "collect", return_value=observation) as collect, \
             mock.patch("sys.stdout", output):
            self.assertEqual(needle_cli.main(["system", "--json", "show memory"]), 0)
        record = json.loads(output.getvalue())
        self.assertTrue(record["collection_performed"])
        self.assertEqual(record["items"][0]["outcome"], "done")
        collect.assert_called_once()

    def test_mcp_read_unknown_is_proposal_and_plan_is_dry(self):
        runtime = Runtime()
        factory = mock.Mock(return_value=runtime)
        server = mcp_server.Server(factory)
        with mock.patch.object(system_collect.Collector, "collect", side_effect=AssertionError("collected")):
            read = server.call_tool("kilix_system_read", {"request": UNKNOWN})
            plan = server.call_tool("kilix_system_plan", {"request": "show memory"})
        self.assertFalse(read["isError"])
        self.assertEqual(read["structuredContent"]["items"][0]["outcome"], "proposed")
        self.assertFalse(read["structuredContent"]["collection_performed"])
        self.assertEqual(read["structuredContent"]["query_source"], runtime.label)
        self.assertFalse(plan["structuredContent"]["collection_performed"])
        factory.assert_called_once_with("system")

    def test_mcp_runtime_failure_is_tool_error(self):
        server = mcp_server.Server(mock.Mock(side_effect=RuntimeError("missing model")))
        with mock.patch.object(system_collect.Collector, "collect", side_effect=AssertionError("collected")):
            result = server.call_tool("kilix_system_read", {"request": UNKNOWN})
        self.assertTrue(result["isError"])
        self.assertFalse(result["structuredContent"]["collection_performed"])


if __name__ == "__main__":
    unittest.main()
