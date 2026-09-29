"""System history records planning outcomes, never observation payloads."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401
import history
import needle_cli
import system_dispatch


class SystemHistory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = mock.patch.dict(os.environ, {"GPU_TERMINAL_HOME": self.temp.name,
                                               "KILIX_NEEDLE_HISTORY": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def last(self):
        return json.loads((history.directory() / "requests.jsonl").read_text().splitlines()[-1])

    def test_model_proposal_is_recorded_without_collecting(self):
        classify = mock.Mock(return_value={"function_calls": [
            {"name": "processes", "arguments": {"sort": "memory"}}]})
        collector = mock.Mock()
        result = system_dispatch.dispatch("please list the processes eating memory lately", classify,
                                          needle_cli.Options(assume_yes=True), collector=collector,
                                          model_label="tuned normalizer test")
        collector.collect.assert_not_called()
        row = self.last()
        self.assertEqual(result["items"][0]["outcome"], "proposed")
        self.assertEqual(row["job"], "system")
        self.assertEqual(row["engine"], "tuned normalizer test")
        self.assertEqual(row["items"][0]["outcome"], "proposed")
        self.assertEqual(row["calls"], classify.return_value["function_calls"])

    def test_grammar_observation_payload_is_never_recorded(self):
        classify = mock.Mock(side_effect=AssertionError("model called"))
        collector = mock.Mock()
        collector.collect.return_value = {"secret_observation": "private-sample-value"}
        result = system_dispatch.dispatch("show memory", classify, needle_cli.Options(),
                                          collector=collector)
        classify.assert_not_called()
        collector.collect.assert_called_once()
        self.assertIn("observation", result["items"][0])
        row = self.last()
        self.assertEqual(row["items"][0]["outcome"], "done")
        self.assertNotIn("private-sample-value", json.dumps(row))
        self.assertNotIn("observation", row["items"][0])

    def test_runtime_exception_detail_is_not_recorded(self):
        result = system_dispatch.dispatch("can you look at cpu load for me",
            mock.Mock(side_effect=RuntimeError("private-runtime-detail")), needle_cli.Options())
        self.assertEqual(result["status"], 1)
        self.assertIn("private-runtime-detail", result["note"])
        self.assertNotIn("private-runtime-detail", json.dumps(self.last()))

    def test_history_failure_and_opt_out_leave_result_unchanged(self):
        with mock.patch.object(history, "record", side_effect=OSError("unwritable")):
            result = system_dispatch.dispatch("show memory", None, needle_cli.Options(dry_run=True))
        self.assertEqual(result["status"], 0)
        with mock.patch.dict(os.environ, {"KILIX_NEEDLE_HISTORY": "0"}):
            result = system_dispatch.dispatch("show memory", None, needle_cli.Options(dry_run=True))
        self.assertEqual(result["status"], 0)
        self.assertFalse((history.directory() / "requests.jsonl").exists())
