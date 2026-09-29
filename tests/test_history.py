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
from support import FakeKilix, desktop

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
        self.assertEqual((apps["caller"], apps["dry_run"], apps["schema"]), ("person", True, 2))
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



class ReviewR17(unittest.TestCase):
    """Review R17: the writer's file safety, bounds and failure isolation."""

    def setUp(self):
        shutil.rmtree(history.directory(), ignore_errors=True)
        os.environ.pop("KILIX_NEEDLE_HISTORY", None)
        history._warned = False
        self.addCleanup(setattr, history, "_warned", False)
        self.folder = history.directory()

    def run_one(self, request="hide the clock"):
        return needle_cli.run_apps_request(grammar(), request, needle_cli.Options(dry_run=True))

    def lines(self):
        path = self.folder / "requests.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def mode(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)

    def test_loose_modes_are_repaired_archives_included(self):              # KN-R17-01
        self.folder.mkdir(parents=True)
        os.chmod(self.folder, 0o777)
        for name, mode in (("requests.jsonl", 0o644), (".lock", 0o666)):
            (self.folder / name).write_text("")
            os.chmod(self.folder / name, mode)
        with mock.patch.object(history, "MAX_BYTES", 400):
            for n in range(4):
                self.run_one(f"hide the clock {n}")
        self.assertEqual(self.mode(self.folder), 0o700)
        for path in self.folder.iterdir():
            self.assertEqual(self.mode(path), 0o600, path.name)

    def test_links_and_special_files_are_never_used(self):                 # KN-R17-01
        elsewhere = self.folder.parent / "elsewhere"
        elsewhere.mkdir(parents=True)
        (elsewhere / "target").write_text("keep\n")
        os.symlink(elsewhere, self.folder)                  # a linked history directory
        self.run_one()
        self.assertEqual(os.listdir(elsewhere), ["target"])
        os.unlink(self.folder)
        self.folder.mkdir()
        os.symlink(elsewhere / "target", self.folder / "requests.jsonl")   # a linked file
        self.run_one()
        os.unlink(self.folder / "requests.jsonl")
        os.link(elsewhere / "target", self.folder / "requests.jsonl")      # a second hard link
        self.run_one()
        self.assertEqual((elsewhere / "target").read_text(), "keep\n")
        os.unlink(self.folder / "requests.jsonl")
        os.mkfifo(self.folder / "requests.jsonl")                           # never blocks
        record = self.run_one()
        self.assertEqual(record["items"][0]["outcome"], "would")

    def test_a_held_lock_drops_the_entry_without_delaying_the_request(self):  # KN-R17-02
        self.run_one()
        import fcntl
        import time
        import threading
        holder = os.open(self.folder / ".lock", os.O_WRONLY)
        fcntl.flock(holder, fcntl.LOCK_EX)
        done = []
        worker = threading.Thread(target=lambda: done.append(self.run_one("hide the battery")))
        try:
            started = time.monotonic()
            worker.start()
            worker.join(2.0)
            took = time.monotonic() - started
            waited = worker.is_alive()
        finally:
            os.close(holder)            # a blocked writer finishes now, not forever
            worker.join(5.0)
        self.assertFalse(waited, "the request waited on the history lock")
        self.assertEqual(done[0]["items"][0]["outcome"], "would")
        self.assertLess(took, 2.0)
        self.assertEqual([e["request"] for e in self.lines()], ["hide the clock"])

    def test_a_failing_warning_never_replaces_a_result_or_an_exception(self):  # KN-R17-03
        class Broken(io.StringIO):
            def write(self, _text):
                raise OSError(9, "stderr is gone")
        with mock.patch.object(sys, "stderr", Broken()), \
                mock.patch.object(history, "_append", side_effect=OSError(28, "disk full")):
            self.assertEqual(self.run_one()["items"][0]["outcome"], "would")
            history._warned = False
            with self.assertRaisesRegex(RuntimeError, "engine broke"):
                needle_cli.run_request(Engine(fail=True), "close the left pane",
                                       needle_cli.Options(dry_run=True))

    def test_a_short_write_leaves_whole_lines(self):                        # KN-R17-04
        self.run_one("hide the clock")
        real = os.write
        calls = []

        def short(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                return real(fd, data[: len(data) // 2])
            raise OSError(28, "disk full")
        with mock.patch.object(os, "write", short), mock.patch.object(sys, "stderr", io.StringIO()):
            self.run_one("hide the battery")
        self.run_one("hide the wifi")
        self.assertEqual([e["request"] for e in self.lines()], ["hide the clock", "hide the wifi"])

    def test_an_entry_is_bounded_however_long_the_request(self):           # KN-R17-05
        self.run_one("x" * 8_388_608)
        size = (self.folder / "requests.jsonl").stat().st_size
        self.assertLessEqual(size, history.MAX_ENTRY)
        (entry,) = self.lines()
        self.assertEqual(entry["request_chars"], 8_388_608)
        self.assertIn("chars]", entry["request"])

    def test_items_keep_only_their_meaning(self):                          # KN-R17-06
        item = {"kind": "launch", "args": {"agent": "codex"}, "outcome": "would",
                "summary": "start codex", "argv": ["kilix", "--expect-broker", "abc"],
                "cwd": "/tmp/private-work", "pane": 7}
        data = history.entry("agents", "start codex here", None, Engine(), [],
                             needle_cli.Options(), {"status": 0, "items": [item]}, 0.1)
        self.assertEqual(set(data["items"][0]), {"kind", "args", "outcome"})
        self.assertNotIn("abc", json.dumps(data))

    def test_the_request_is_kept_as_given(self):                            # KN-R17-07
        self.run_one("  hide the clock  ")
        self.run_one("bad \ud800 request")
        spaced, surrogate = self.lines()
        self.assertEqual((spaced["request"], spaced["checked"]), ("  hide the clock  ", "hide the clock"))
        self.assertEqual(surrogate["request"], "bad \ud800 request")


class ReviewR17Round2(unittest.TestCase):
    """Review R17 round 2: descriptors, resolved names, partial tails, archives."""

    def setUp(self):
        shutil.rmtree(history.directory(), ignore_errors=True)
        os.environ.pop("KILIX_NEEDLE_HISTORY", None)
        history._warned = False
        self.addCleanup(setattr, history, "_warned", False)
        self.folder = history.directory()

    def run_one(self, request="hide the clock"):
        with mock.patch.object(sys, "stderr", io.StringIO()):
            return needle_cli.run_apps_request(grammar(), request, needle_cli.Options(dry_run=True))

    def lines(self):
        return [json.loads(l) for l in (self.folder / "requests.jsonl").read_text().splitlines()]

    def test_no_descriptor_outlives_a_failure(self):                       # KN-R17-201
        self.run_one()
        real_fstat, real_fchmod = os.fstat, os.fchmod
        import fcntl
        faults = {"fstat": mock.patch.object(os, "fstat", side_effect=OSError(5, "EIO")),
                  "fchmod": mock.patch.object(os, "fchmod", side_effect=lambda fd, mode: (
                      real_fchmod(fd, mode) if mode != 0o600 else (_ for _ in ()).throw(
                          OSError(5, "EIO")))),
                  "flock": mock.patch.object(fcntl, "flock", side_effect=OSError(5, "EIO"))}
        for name, fault in faults.items():
            before = len(os.listdir("/proc/self/fd"))
            with fault:
                for _ in range(3):
                    self.run_one()
            self.assertEqual(len(os.listdir("/proc/self/fd")), before, name)

    def test_resolved_pane_names_stay_out(self):                             # KN-R17-202
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        with FakeKilix(desktop()):
            record = needle_cli.run_request(
                Engine([{"name": "go_to_pane", "arguments": {"pane": "left"}}]),
                "go to the left pane", needle_cli.Options(dry_run=True))
        # resolved to pane 301 'build (bash)': neither is in the request
        self.assertIn("build", record["items"][0]["summary"])
        (entry,) = self.lines()
        self.assertEqual(entry["items"], [{"kind": "go_to_pane", "args": {"pane": "left"},
                                           "outcome": "would"}])

    def test_a_partial_last_line_is_cut_before_the_next_entry(self):      # KN-R17-203
        self.run_one("hide the clock")
        with open(self.folder / "requests.jsonl", "ab") as handle:
            handle.write(b'{"schema": 2, "request": "half a rec')        # a failed rollback or a crash
        self.run_one("hide the wifi")
        self.assertEqual([e["request"] for e in self.lines()], ["hide the clock", "hide the wifi"])

    def test_archives_are_repaired_without_a_rotation(self):               # KN-R17-204
        self.run_one()
        for n in range(1, history.KEEP):
            path = self.folder / f"requests.{n}.jsonl"
            path.write_text("{}\n")
            os.chmod(path, 0o644)
        self.run_one()
        for n in range(1, history.KEEP):
            self.assertEqual(stat.S_IMODE((self.folder / f"requests.{n}.jsonl").stat().st_mode),
                             0o600, n)


class ReviewR17Round3(unittest.TestCase):
    """Review R17 round 3: reasons by origin; healing at a real boundary, before rotation."""

    def setUp(self):
        shutil.rmtree(history.directory(), ignore_errors=True)
        os.environ.pop("KILIX_NEEDLE_HISTORY", None)
        history._warned = False
        self.addCleanup(setattr, history, "_warned", False)
        self.folder = history.directory()
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def quiet(self):
        return mock.patch.object(sys, "stderr", io.StringIO())

    def lines(self, name="requests.jsonl"):
        return [json.loads(l) for l in (self.folder / name).read_text().splitlines()]

    def panes(self, request, pane):
        with FakeKilix(desktop()), self.quiet():
            return needle_cli.run_request(
                Engine([{"name": "go_to_pane", "arguments": {"pane": pane}}]), request,
                needle_cli.Options(dry_run=True))

    def test_unresolved_and_failed_reasons_stay_out(self):                 # KN-R17-301
        record = self.panes("go to the bash pane", "bash")
        self.assertIn("build (bash)", record["items"][0]["reason"])   # the caller still sees it
        import kilix
        with mock.patch.object(kilix, "snapshot",
                               side_effect=kilix.KilixError("error for pane 999: private-title")):
            self.panes("go to the left pane", "left")
        text = json.dumps(self.lines())
        for secret in ("build (bash)", "301", "private-title", "999"):
            self.assertNotIn(secret, text)

    def test_the_checks_reasons_are_kept(self):
        with self.quiet():
            needle_cli.run_apps_request(grammar(), "open doom later", needle_cli.Options(dry_run=True))
            needle_cli.run_apps_request(grammar(), "open doom", needle_cli.Options(agent=True))
        refused, skipped = self.lines()
        self.assertIn("when", refused["items"][0]["reason"])
        self.assertEqual(skipped["items"][0]["outcome"], "skipped")
        self.assertIn("person", skipped["items"][0]["reason"])

    def seed(self, tail: bytes):
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "requests.jsonl").write_bytes(b'{"request": "kept"}\n' + tail)

    def test_healing_cuts_only_at_a_newline_it_saw(self):                   # KN-R17-302
        edge = 2 * history.MAX_ENTRY
        for length, heals in ((1, True), (edge - 21, True), (edge - 20, True), (edge + 1, False)):
            with self.subTest(length=length):
                self.seed(b"x" * length)
                before = (self.folder / "requests.jsonl").read_bytes()
                with self.quiet():
                    needle_cli.run_apps_request(grammar(), "hide the clock",
                                                needle_cli.Options(dry_run=True))
                after = (self.folder / "requests.jsonl").read_bytes()
                if heals:
                    self.assertEqual([l.get("request") for l in self.lines()], ["kept", "hide the clock"])
                else:
                    self.assertEqual(after, before)        # untouched, the entry dropped

    def test_a_short_read_never_cuts(self):                                 # KN-R17-302
        self.seed(b'{"request": "half')
        before = (self.folder / "requests.jsonl").read_bytes()
        with mock.patch.object(os, "pread", side_effect=lambda fd, n, off: b""), self.quiet():
            needle_cli.run_apps_request(grammar(), "hide the clock", needle_cli.Options(dry_run=True))
        self.assertEqual((self.folder / "requests.jsonl").read_bytes(), before)

    def test_no_partial_line_is_rotated_into_an_archive(self):             # KN-R17-303
        self.seed(b'{"request": "half')
        with mock.patch.object(history, "MAX_BYTES", 300), self.quiet():
            needle_cli.run_apps_request(grammar(), "hide the clock", needle_cli.Options(dry_run=True))
        for name in ("requests.jsonl", "requests.1.jsonl"):
            path = self.folder / name
            if path.exists():
                self.lines(name)        # every line parses
        self.assertTrue((self.folder / "requests.1.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
