"""Reading logs is independent of every action engine and executor."""
import io
import sys
import types
import unittest
from unittest import mock

import support  # noqa: F401
import mcp_server
import needle_cli


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.api = types.ModuleType("needle_logs.cli")
        self.api.read = mock.Mock(return_value={"schema": "kilix.logs.read/v1",
                                                "exit_status": 0, "events": []})
        self.api.main = mock.Mock(return_value=0)
        patcher = mock.patch.dict(sys.modules, {"needle_logs.cli": self.api})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cli_does_not_construct_an_action_runtime(self):
        with mock.patch.object(needle_cli, "open_runtime", side_effect=AssertionError("action runtime")):
            self.assertEqual(needle_cli.main(["logs", "events", "--file", "/x", "--provider", "raw"]), 0)
        self.api.main.assert_called_once_with(["events", "--file", "/x", "--provider", "raw"])

    def test_mcp_has_no_engine_or_confirmation_path(self):
        factory = mock.Mock(side_effect=AssertionError("action runtime"))
        server = mcp_server.Server(factory)
        with mock.patch.object(needle_cli, "run_calls", side_effect=AssertionError("executor")):
            result = server.call_tool("kilix_logs_read", {"session": "review", "operation": "brief"})
        self.assertFalse(result["isError"])
        factory.assert_not_called()
        self.api.read.assert_called_once_with({"session": "review", "operation": "brief"})

    def test_mcp_rejects_invalid_and_action_arguments(self):
        server = mcp_server.Server(mock.Mock(side_effect=AssertionError("action runtime")))
        invalid = [None, {}, {"session": "x", "confirm_risky": True},
                   {"session": "x", "operation": "cache_clear"},
                   {"file": "/x"}, {"file": "/x", "provider": "raw", "session": "x"},
                   {"session": "x", "limit": True}, {"session": "x", "limit": 101},
                   {"session": "x", "operation": "search"},
                   {"operation": "source"}, {"session": "x", "provider": "nope"}]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                server.call_tool("kilix_logs_read", args)
        self.api.read.assert_not_called()

    def test_partial_read_is_reported_as_a_tool_error(self):
        self.api.read.return_value = {"exit_status": 1, "status": "partial", "events": []}
        result = mcp_server.Server(mock.Mock()).call_tool("kilix_logs_read", {"session": "x"})
        self.assertTrue(result["isError"])


if __name__ == "__main__":
    unittest.main()
