"""The local request history (history.py): what it records, where, and that
recording never changes a request."""
import io
import json
import os
import shutil
import stat
import sys
import unittest
from unittest import mock

import support  # noqa: F401

import history
import mcp_server
import needle_cli


class Engine:
    label = "stub"

    def __init__(self, calls=(), fail=False):
        self.calls, self.fail = list(calls), fail

    def start(self):
        pass

    def close(self):
        pass

    def reset(self):
        pass

    def complete(self, text):
        if self.fail:
            raise RuntimeError("engine broke")
        return {"function_calls": list(self.calls)}


def grammar():
    args = type("A", (), {"engine": None, "root": None})()
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KILIX_NEEDLE_ENGINE", None)
        return needle_cli.open_runtime(args, job="apps")


class History(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(history.directory(), ignore_errors=True)
        os.environ.pop("KILIX_NEEDLE_HISTORY", None)
        self.addCleanup(os.environ.pop, "KILIX_NEEDLE_HISTORY", None)

    def entries(self):
        path = history.directory() / "requests.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_every_job_records_its_request_calls_and_outcome(self):
        options = needle_cli.Options(dry_run=True)
        needle_cli.run_apps_request(grammar(), "hide the clock", options)
        needle_cli.run_request(Engine(), "close the left pane", options)
        needle_cli.run_agents_request(Engine(), "tell me a joke", options, cwd="/tmp")
        apps, panes, agents = self.entries()
        self.assertEqual((apps["job"], panes["job"], agents["job"]), ("apps", "panes", "agents"))
        self.assertEqual(apps["request"], "hide the clock")
        self.assertEqual(apps["engine"], "grammar")
        self.assertEqual(apps["calls"], [{"name": "show", "arguments": {"item": "clock", "on": False}}])
        self.assertEqual([i["outcome"] for i in apps["items"]], ["would"])
        self.assertEqual((apps["caller"], apps["dry_run"], apps["schema"]), ("person", True, 1))
        self.assertEqual(panes["engine"], "stub")
        self.assertGreaterEqual(apps["ms"], 0)

    def test_refusals_and_malformed_requests_are_recorded_too(self):
        options = needle_cli.Options(dry_run=True)
        needle_cli.run_apps_request(grammar(), "open doom later", options)
        needle_cli.run_apps_request(grammar(), "bad\x00request", options)
        later, bad = self.entries()
        self.assertEqual(later["items"][0]["outcome"], "refused")
        self.assertIn("when", later["items"][0]["reason"])
        self.assertIsNone(bad["calls"])
        self.assertEqual(bad["request"], "bad\x00request")

    def test_the_history_is_private(self):
        needle_cli.run_apps_request(grammar(), "hide the clock", needle_cli.Options(dry_run=True))
        folder = history.directory()
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((folder / "requests.jsonl").stat().st_mode), 0o600)

    def test_it_can_be_turned_off(self):
        for value in ("0", "off", "No", "false"):
            os.environ["KILIX_NEEDLE_HISTORY"] = value
            needle_cli.run_apps_request(grammar(), "hide the clock", needle_cli.Options(dry_run=True))
        self.assertEqual(self.entries(), [])

    def test_it_rotates_and_keeps_a_bounded_number_of_files(self):
        with mock.patch.object(history, "MAX_BYTES", 600), mock.patch.object(history, "KEEP", 3):
            for n in range(12):
                needle_cli.run_apps_request(grammar(), f"hide the clock {n}",
                                            needle_cli.Options(dry_run=True))
        names = sorted(p.name for p in history.directory().glob("requests*.jsonl"))
        self.assertEqual(names, ["requests.1.jsonl", "requests.2.jsonl", "requests.jsonl"])
        self.assertIn("hide the clock 11", (history.directory() / "requests.jsonl").read_text())

    def test_a_history_that_cannot_be_written_changes_nothing(self):
        history.directory().parent.mkdir(parents=True, exist_ok=True)
        history.directory().write_text("not a directory")
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err), mock.patch.object(history, "_warned", False):
            record = needle_cli.run_apps_request(grammar(), "hide the clock",
                                                 needle_cli.Options(dry_run=True))
            needle_cli.run_apps_request(grammar(), "hide the battery",
                                        needle_cli.Options(dry_run=True))
        history.directory().unlink()
        self.assertEqual(record["items"][0]["outcome"], "would")
        self.assertEqual(err.getvalue().count("history was not written"), 1)

    def test_a_request_whose_engine_fails_is_still_recorded(self):
        with self.assertRaises(RuntimeError):
            needle_cli.run_request(Engine(fail=True), "close the left pane",
                                   needle_cli.Options(dry_run=True))
        (entry,) = self.entries()
        self.assertEqual(entry["note"], "the request did not finish")
        self.assertIsNone(entry["status"])

    def test_mcp_requests_are_recorded_as_an_agents_requests(self):
        server = mcp_server.Server(lambda job="panes": grammar())
        server.call_tool("kilix_apps_plan", {"request": "hide the clock"})
        server.close()
        (entry,) = self.entries()
        self.assertEqual((entry["caller"], entry["job"], entry["dry_run"]), ("agent", "apps", True))


if __name__ == "__main__":
    unittest.main()
