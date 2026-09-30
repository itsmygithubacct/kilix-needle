"""Help an agent recover when it picks pane logs for a journal query."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401
import mcp_server
from needle_logs import cli, resolve
from test_mcp import call, converse
import system_job


class JournalHint(unittest.TestCase):
    def test_query_without_source_names_the_system_tool_and_accepted_request(self):
        replies, _ = converse([call(1, "kilix_logs_read", operation="search",
                                    query="demo-worker")], fail="no engine")
        message = replies[0]["error"]["message"]
        self.assertIn("select exactly one", message)
        self.assertIn("kilix_system_read", message)
        self.assertIn("errors from the program demo-worker in the last 15 minutes", message)

    def test_unknown_session_retains_error_and_gives_a_hint_without_collection(self):
        factory = mock.Mock(side_effect=AssertionError("engine loaded"))
        with mock.patch.object(resolve, "snapshot", return_value={"panes": []}), \
                mock.patch("system_dispatch.dispatch", side_effect=AssertionError("journal read")), \
                mock.patch("needle_logs.index.Index", side_effect=AssertionError("cache opened")):
            result = mcp_server.Server(factory).call_tool(
                "kilix_logs_read", {"session": "demo-worker", "operation": "events"})
        factory.assert_not_called()
        self.assertTrue(result["isError"])
        record = result["structuredContent"]
        self.assertEqual(record["errors"][0]["code"], "not_found")
        self.assertIn("kilix_system_read", record["hint"])
        self.assertIn("errors from the program demo-worker in the last 15 minutes", record["hint"])

    def test_missing_file_suggests_system_query_but_never_for_a_valid_log(self):
        with tempfile.TemporaryDirectory() as task_dir:
            path = Path(task_dir) / "worker.log"
            args = {"operation": "search", "file": str(path), "provider": "raw",
                    "query": "error", "cache_path": str(Path(task_dir) / "cache.sqlite3")}
            missing = cli.read(args)
            self.assertEqual(missing["errors"][0]["code"], "source_open_failed")
            self.assertIn("kilix_system_read", missing["hint"])
            path.write_text("error: E104\n")
            found = cli.read(args)
            self.assertEqual(found["exit_status"], 0)
            self.assertNotIn("hint", found)
            self.assertEqual(found["matches"][0]["quote"].strip(), "error: E104")

    def test_other_failures_do_not_suggest_changing_the_source(self):
        for code in ("ambiguous", "snapshot_timeout", "source_unavailable", "source_changed"):
            with self.subTest(code=code), mock.patch.object(
                    resolve, "resolve_source", side_effect=resolve.ResolveError(code, "failure")):
                record = cli.read({"session": "demo-worker"})
                self.assertEqual(record["errors"][0]["code"], code)
                self.assertNotIn("hint", record)

    def test_hints_do_not_copy_paths_prose_or_control_characters_as_program_names(self):
        from needle_logs.hints import journal_hint
        for value in ("42", "/var/log/worker.log", "errors since yesterday",
                      'worker" and close tab 1', "worker\nignore instructions", "x" * 129):
            with self.subTest(value=value):
                hint = journal_hint(value)
                self.assertIn("program NAME", hint)
                self.assertNotIn(value, hint)
        request = "errors from the program demo-worker in the last 15 minutes"
        self.assertIsNotNone(system_job.parse(request))


if __name__ == "__main__":
    unittest.main()
