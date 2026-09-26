"""Jobs: each has its own selected model, and a selection never crosses jobs."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401

import evaluate
import jobs
import needle_cli
import tuning

APPS = jobs.Job("apps", "a second job, for the tests", "evals/apps",
                "evals/apps/dev.jsonl", "evals/apps/test.jsonl", "evals/apps/heldout-v1.jsonl")


class Selection(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-jobs-")
        self.addCleanup(self.dir.cleanup)
        self.home = Path(self.dir.name)
        for patcher in (mock.patch.multiple(tuning, APP_HOME=self.home,
                                            SELECTION=self.home / "model.json"),
                        mock.patch.dict(jobs.JOBS, {"apps": APPS})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def stored(self):
        return json.loads((self.home / "model.json").read_text())

    def test_a_selection_from_before_jobs_is_the_panes_selection(self):
        legacy = {"weights": "/x/qat-6/tuned.cact", "sha256": "ab" * 32, "toolset": "five",
                  "run": "qat-6"}
        (self.home / "model.json").write_text(json.dumps(legacy))
        self.assertEqual(tuning.selected("panes"), legacy)
        self.assertEqual(tuning.selected(), legacy)
        self.assertIsNone(tuning.selected("apps"))

    def test_selecting_one_job_keeps_the_others(self):
        legacy = {"weights": "/x/qat-6/tuned.cact", "sha256": "ab" * 32, "toolset": "five",
                  "run": "qat-6"}
        (self.home / "model.json").write_text(json.dumps(legacy))
        tuning.select(self.home / "tuning" / "apps-1", "cd" * 32, "apps")
        self.assertEqual(self.stored()["schema"], tuning.SELECTION_SCHEMA)
        self.assertEqual(tuning.selected("panes"), legacy)
        self.assertEqual(tuning.selected("apps")["sha256"], "cd" * 32)
        tuning.deselect("apps")
        self.assertEqual(tuning.selected("panes"), legacy)
        self.assertIsNone(tuning.selected("apps"))
        tuning.deselect("panes")
        self.assertFalse((self.home / "model.json").exists())

    def test_an_unknown_job_is_refused(self):
        with self.assertRaises(jobs.UnknownJob):
            tuning.selected("nothing")
        with self.assertRaises(jobs.UnknownJob):
            tuning.select(self.home / "r", "ab" * 32, "nothing")

    def test_a_model_gated_for_one_job_is_not_selected_for_another(self):
        run = self.home / "incoming"
        run.mkdir()
        (run / "tuned.cact").write_bytes(b"weights")
        digest = tuning.sha256_file(run / "tuned.cact")
        for report_job, wanted in (("apps", "panes"), ("panes", "apps"), (None, "apps")):
            report = {"cact_sha256": digest, "failures": []}
            if report_job:
                report["job"] = report_job
            (run / "gates.json").write_text(json.dumps(report))
            with self.assertRaisesRegex(tuning.TuneError, "was gated for"):
                tuning.select_run(run, wanted)
        self.assertFalse((self.home / "tuning" / "incoming").exists())

    def test_the_cli_selects_per_job_and_reports_every_job(self):
        tuning.select(self.home / "tuning" / "qat-6", "ab" * 32, "panes")
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            tuning.main(["--job", "apps", "--deselect"])
        self.assertEqual(tuning.selected("panes")["run"], "qat-6")
        out = io.StringIO()
        with mock.patch.object(tuning, "in_use", side_effect=lambda job="panes": f"x-{job}"), \
                mock.patch.object(sys, "stdout", out):
            tuning.main(["--status"])
        status = json.loads(out.getvalue())
        self.assertEqual(status["job"], "panes")
        self.assertEqual(status["selected"]["run"], "qat-6")
        self.assertEqual(sorted(status["jobs"]), ["apps", "panes"])
        self.assertIsNone(status["jobs"]["apps"]["selected"])
        with self.assertRaises(SystemExit), mock.patch.object(sys, "stderr", io.StringIO()):
            tuning.main(["--job", "nothing", "--status"])

    def test_the_runtime_asks_for_its_own_jobs_model(self):
        args = type("A", (), {"engine": None, "root": None})()
        with mock.patch.object(tuning, "selected", return_value=None) as chosen, \
                mock.patch.object(needle_cli, "_image", return_value=mock.Mock()):
            needle_cli.open_runtime(args, job="apps")
            chosen.assert_called_once_with("apps")
            chosen.reset_mock()
            needle_cli.open_runtime(args)
            chosen.assert_called_once_with("panes")


class EvalSets(unittest.TestCase):
    def test_every_jobs_eval_sets_are_kept_out_of_training_data(self):
        with tempfile.TemporaryDirectory(prefix="kn-jobs-") as tmp:
            root = Path(tmp)
            for rel in ("evals/dev.jsonl", "evals/apps/dev.jsonl", "evals/apps/heldout-v1.jsonl"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("{}\n")
            with mock.patch.object(tuning, "REPO", root):
                excluded = tuning.recipe(tuning.load_manifest())["data"]["exclude"]
        for rel in ("evals/dev.jsonl", "evals/apps/dev.jsonl", "evals/apps/heldout-v1.jsonl"):
            self.assertIn(rel, excluded)

    def test_the_panes_gate_is_the_jobs_heldout_set(self):
        self.assertEqual(tuning.recipe(tuning.load_manifest())["gates"]["heldout"],
                         jobs.JOBS["panes"].heldout)

    def test_evaluate_refuses_an_unknown_job(self):
        with self.assertRaises(SystemExit) as stop, mock.patch.object(sys, "stderr", io.StringIO()):
            evaluate.main(["evals/dev.jsonl", "--job", "nothing"])
        self.assertEqual(stop.exception.code, 2)


class ReviewR11(unittest.TestCase):
    """Review R11 (0.2.2-REVIEW-REPO-kilix-needle-R11.md)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-r11-")
        self.addCleanup(self.dir.cleanup)
        self.home = Path(self.dir.name)
        for patcher in (mock.patch.multiple(tuning, APP_HOME=self.home,
                                            SELECTION=self.home / "model.json"),
                        mock.patch.dict(jobs.JOBS, {"apps": APPS})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.file = self.home / "model.json"

    def test_only_panes_is_stored_in_the_old_form(self):                  # KN-R11-06
        tuning.select(self.home / "tuning" / "qat-6", "ab" * 32, "panes")
        stored = json.loads(self.file.read_text())
        self.assertEqual(stored["run"], "qat-6")
        self.assertNotIn("schema", stored)
        tuning.select(self.home / "tuning" / "jobs" / "apps" / "a1", "cd" * 32, "apps")
        self.assertEqual(json.loads(self.file.read_text())["schema"], tuning.SELECTION_SCHEMA)

    def test_a_flat_file_without_weights_is_not_a_selection(self):         # R11-M1
        self.file.write_text(json.dumps({"run": "x"}))
        self.assertIsNone(tuning.selected("panes"))

    def test_an_unreadable_or_unknown_file_is_never_overwritten(self):     # KN-R11-04
        for text in ("{not json", json.dumps({"schema": "kilix-needle.selection/v9", "jobs": {}}),
                     json.dumps(["panes"])):
            self.file.write_text(text)
            with self.assertRaises(tuning.TuneError):
                tuning.select(self.home / "r", "ab" * 32, "apps")
            with self.assertRaises(tuning.TuneError):
                tuning.deselect("panes")
            self.assertEqual(self.file.read_text(), text)
            self.assertIsNone(tuning.selected("panes"))   # requests use the base model

    def test_entries_it_does_not_understand_are_kept(self):                # R11-M12
        self.file.write_text(json.dumps({"schema": tuning.SELECTION_SCHEMA, "jobs": {
            "panes": {"weights": "/w", "sha256": "ab" * 32, "run": "qat-6"}, "future": "kept"}}))
        tuning.select(self.home / "a1", "cd" * 32, "apps")
        self.assertEqual(json.loads(self.file.read_text())["jobs"]["future"], "kept")
        out = io.StringIO()
        with mock.patch.object(tuning, "in_use", return_value="x"), \
                mock.patch.object(sys, "stdout", out):
            tuning.main(["--status"])
        self.assertEqual(json.loads(out.getvalue())["unknown_jobs_in_selection"], ["future"])

    def test_concurrent_writers_keep_each_others_jobs(self):               # KN-R11-05
        import multiprocessing

        def write(job, n):
            for i in range(n):
                tuning.select(self.home / f"{job}-{i}", f"{i:064x}", job)
        context = multiprocessing.get_context("fork")
        workers = [context.Process(target=write, args=(job, 150)) for job in ("panes", "apps")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(60)
        self.assertEqual([w.exitcode for w in workers], [0, 0])
        self.assertEqual(tuning.selected("panes")["run"], "panes-149")
        self.assertEqual(tuning.selected("apps")["run"], "apps-149")

    def test_in_use_and_status_ask_about_each_job(self):                   # R11-M7, R11-M8
        with mock.patch.object(tuning, "selected", return_value=None) as chosen, \
                mock.patch("asset.from_installed", return_value=mock.MagicMock()):
            tuning.in_use("apps")
        chosen.assert_called_with("apps")
        out = io.StringIO()
        with mock.patch.object(tuning, "in_use", side_effect=lambda job="panes": f"x-{job}"), \
                mock.patch.object(sys, "stdout", out):
            tuning.main(["--status"])
        status = json.loads(out.getvalue())
        self.assertEqual(status["jobs"]["apps"]["in_use"], "x-apps")
        self.assertEqual(status["jobs"]["panes"]["in_use"], "x-panes")

    def test_jobs_not_built_yet_are_refused_not_run_as_panes(self):        # KN-R11-01, -02, -07
        with self.assertRaisesRegex(tuning.TuneError, "tuning for the apps job"):
            tuning.tune(None, None, None, "apps")
        with self.assertRaisesRegex(tuning.TuneError, "gates for the apps job"):
            tuning.stage_gates(tuning.Run(self.home / "r"), {}, None, "ab" * 32, "apps")
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(tuning.main(["--job", "apps"]), 1)
        self.assertIsNone(tuning.selected("panes"))

    def _incoming(self, name, data):
        run = self.home / "incoming" / name
        run.mkdir(parents=True)
        (run / "tuned.cact").write_bytes(data)
        digest = tuning.sha256_file(run / "tuned.cact")
        (run / "gates.json").write_text(json.dumps({"job": "apps", "cact_sha256": digest,
                                                    "failures": []}))
        return run, digest

    def test_a_run_is_gated_and_selected_for_its_own_job(self):            # R11-M13, R11-M14
        run, digest = self._incoming("a1", b"apps weights")
        tuning.select(self.home / "tuning" / "qat-6", "ab" * 32, "panes")
        with mock.patch("asset.installed_library", return_value=mock.MagicMock()), \
                mock.patch.object(tuning, "_check_gated_job"), \
                mock.patch.object(tuning, "stage_gates", return_value={"failures": []}) as gates:
            target = tuning.select_run(run, "apps")
        self.assertEqual(gates.call_args.args[-1], "apps")
        self.assertEqual(target, self.home / "tuning" / "jobs" / "apps" / "a1")
        self.assertEqual(tuning.selected("apps")["sha256"], digest)
        self.assertEqual(tuning.selected("panes")["run"], "qat-6")

    def _select(self, run, job="panes", failures=()):
        with mock.patch("asset.installed_library", return_value=mock.MagicMock()), \
                mock.patch.object(tuning, "_check_gated_job"), \
                mock.patch.object(tuning, "stage_gates", return_value={"failures": list(failures)}):
            return tuning.select_run(run, job)

    def _panes_incoming(self, where, name, data):
        run = self.home / where / name
        run.mkdir(parents=True)
        (run / "tuned.cact").write_bytes(data)
        (run / "gates.json").write_text(json.dumps({
            "job": "panes", "cact_sha256": tuning.sha256_file(run / "tuned.cact"), "failures": []}))
        return run

    def test_a_run_that_exists_is_never_replaced_or_deleted(self):         # KN-R11-03, -13, -14
        first = self._panes_incoming("in1", "same", b"first")
        target = self._select(first)
        (target / "train.log").write_text("log")
        second = self._panes_incoming("in2", "same", b"second")
        with self.assertRaisesRegex(tuning.TuneError, "exists already"):
            self._select(second)
        self.assertEqual((target / "tuned.cact").read_bytes(), b"first")
        self.assertTrue((target / "train.log").exists())
        # the same bytes again: allowed, and a refusal then leaves the directory
        again = self._panes_incoming("in3", "same", b"first")
        with self.assertRaises(tuning.TuneError):
            self._select(again, failures=["no"])
        self.assertTrue((target / "train.log").exists())
        # and with the selection file corrupt, still nothing is deleted
        self.file.write_text("{broken")
        with self.assertRaises(tuning.TuneError):
            self._select(self._panes_incoming("in4", "same", b"other"))
        self.assertEqual((target / "tuned.cact").read_bytes(), b"first")

    def test_a_run_is_reselected_from_its_own_directory(self):            # KN-R11-18
        target = self._select(self._panes_incoming("in", "r1", b"w"))
        tuning.deselect("panes")
        self.assertEqual(self._select(target), target)
        self.assertEqual(tuning.selected("panes")["run"], "r1")
        (self.home / "tuning" / "link").symlink_to(self.home / "nowhere")
        with self.assertRaisesRegex(tuning.TuneError, "symbolic link"):
            self._select(self._panes_incoming("in5", "link", b"x"))

    def test_a_refused_regate_leaves_the_runs_report(self):                   # KN-R11-19
        target = self._select(self._panes_incoming("in", "r2", b"w"))
        before = (target / "gates.json").read_text()
        again = self._panes_incoming("in6", "r2", b"w")
        def failing_gates(run, *args):
            (run.root / "gates.json").write_text("FAIL")
            return {"failures": ["no"]}
        with mock.patch("asset.installed_library", return_value=mock.MagicMock()), \
                mock.patch.object(tuning, "stage_gates", side_effect=failing_gates):
            with self.assertRaises(tuning.TuneError):
                tuning.select_run(again)
        self.assertEqual((target / "gates.json").read_text(), before)
        self.assertEqual([p.name for p in target.parent.glob(".regate-*")], [])

    def test_tune_never_trains_into_a_selected_run(self):                    # KN-R11-20
        target = self._select(self._panes_incoming("in", "r3", b"w"))
        with self.assertRaisesRegex(tuning.TuneError, "not an unfinished tuning run"):
            tuning.tune(None, None, "r3")
        self.assertEqual((target / "tuned.cact").read_bytes(), b"w")

    def test_tune_resumes_only_its_own_unfinished_runs(self):              # KN-R11-21..24
        imported = self._select(self._panes_incoming("in", "r4", b"only copy"))
        tuning.deselect("panes")
        self.file.write_text("{corrupt")                                   # -22: no file needed
        for name in ("r4",):
            with self.assertRaisesRegex(tuning.TuneError, "not an unfinished"):
                tuning.tune(None, None, name)
        self.assertEqual((imported / "tuned.cact").read_bytes(), b"only copy")
        finished = self.home / "tuning" / "r5"
        finished.mkdir(parents=True)
        for stage in ("base", "export", "select"):
            (finished / f".{stage}.done").write_text("{}")
        with self.assertRaisesRegex(tuning.TuneError, "selected already"):
            tuning._check_resumable(finished)
        (finished / ".select.done").unlink()
        tuning._check_resumable(finished)             # exported, not selected: re-gates (-28)
        partial = self.home / "tuning" / "r6"
        partial.mkdir()
        (partial / ".base.done").write_text("{}")
        tuning._check_resumable(partial)                                   # resuming is allowed
        tuning._check_resumable(self.home / "tuning" / "new")              # a new run too
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):                         # -21: before the child
            self.assertEqual(tuning.main(["--background", "--run", "r4"]), 1)
        self.assertFalse((imported / "background.log").exists())

    def test_a_background_run_starts(self):                                   # KN-R11-27
        with mock.patch("subprocess.Popen") as child, mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(tuning.main(["--background", "--run", "fresh"]), 0)
        child.assert_called_once()
        root = self.home / "tuning" / "fresh"
        self.assertTrue((root / "background.log").exists())
        tuning._check_resumable(root)        # what the child checks first: accepted
        with mock.patch("subprocess.Popen"), mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(tuning.main(["--background"]), 0)
        named = [p for p in (self.home / "tuning").iterdir() if p.name != "fresh"]
        tuning._check_resumable(named[0])

    def test_a_background_log_does_not_make_a_run_new(self):                # KN-R11-31 (M57, M61)
        root = self.home / "tuning" / "bg-done"
        root.mkdir(parents=True)
        for name in ("background.log", ".base.done", ".select.done"):
            (root / name).write_text("{}")
        with self.assertRaisesRegex(tuning.TuneError, "selected already"):
            tuning._check_resumable(root)
        imported = self.home / "tuning" / "bg-imported"
        imported.mkdir()
        for name in ("background.log", "tuned.cact"):
            (imported / name).write_text("x")
        with self.assertRaisesRegex(tuning.TuneError, "not an unfinished"):
            tuning._check_resumable(imported)

    def test_a_missing_runtime_is_a_message_not_a_traceback(self):          # KN-R11-32
        import asset
        err = io.StringIO()
        with mock.patch.object(tuning, "tune", side_effect=asset.AssetError("no runtime")), \
                mock.patch.object(sys, "stderr", err):
            self.assertEqual(tuning.main([]), 1)
        self.assertIn("kilix-needle tune: no runtime", err.getvalue())

    def test_every_write_holds_the_lock(self):                               # KN-R11-31 (M19)
        import fcntl
        with mock.patch("fcntl.flock", wraps=fcntl.flock) as flock:
            tuning.select(self.home / "a6", "cd" * 32, "apps")
            tuning.deselect("apps")
        self.assertEqual([c.args[1] for c in flock.call_args_list], [fcntl.LOCK_EX, fcntl.LOCK_EX])

    def test_a_link_to_an_unfinished_run_is_not_resumed(self):               # KN-R11-29 (M49)
        elsewhere = self.home / "elsewhere-run"
        elsewhere.mkdir()
        (elsewhere / ".base.done").write_text("{}")
        (self.home / "tuning").mkdir(exist_ok=True)
        (self.home / "tuning" / "linked").symlink_to(elsewhere)
        with self.assertRaisesRegex(tuning.TuneError, "symbolic link"):
            tuning._check_resumable(self.home / "tuning" / "linked")

    def test_a_passing_regate_replaces_the_report_and_cleans_up(self):       # R11 M44, M45, M48, KN-R11-26
        target = self._select(self._panes_incoming("in", "r7", b"w"))
        stale = target.parent / ".regate-r7"
        stale.mkdir()
        (stale / "leftover").write_text("x")
        def passing_gates(run, *args):
            (run.root / "gates.json").write_text("NEW PASS")
            return {"failures": []}
        with mock.patch("asset.installed_library", return_value=mock.MagicMock()), \
                mock.patch.object(tuning, "stage_gates", side_effect=passing_gates) as gates:
            tuning.select_run(self._panes_incoming("in8", "r7", b"w"))
        self.assertNotIn("leftover", [p.name for p in Path(gates.call_args.args[0].root).glob("*")])
        self.assertEqual((target / "gates.json").read_text(), "NEW PASS")
        self.assertFalse(stale.exists())
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        stale.symlink_to(elsewhere)
        with mock.patch("asset.installed_library", return_value=mock.MagicMock()), \
                mock.patch.object(tuning, "stage_gates", side_effect=passing_gates):
            tuning.select_run(self._panes_incoming("in9", "r7", b"w"))
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_status_never_lists_a_dot_directory(self):                      # R11 M47
        (self.home / "tuning" / ".regate-x").mkdir(parents=True)
        (self.home / "tuning" / "qat-6").mkdir()
        out = io.StringIO()
        with mock.patch.object(tuning, "in_use", return_value="x"), mock.patch.object(sys, "stdout", out):
            tuning.main(["--status"])
        self.assertEqual(json.loads(out.getvalue())["runs"], ["qat-6"])

    def test_jobs_is_not_a_run_name(self):                                  # KN-R11-12
        for name in ("jobs", ".hidden"):
            run = self._panes_incoming("in", name, b"w")
            with self.assertRaisesRegex(tuning.TuneError, "can't name a run"):
                self._select(run)
        with self.assertRaisesRegex(tuning.TuneError, "can't name a run"):
            tuning.tune(None, None, "jobs")
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(tuning.main(["--background", "--run", "jobs"]), 1)
        self.assertFalse((self.home / "tuning" / "jobs").exists())

    def test_a_status_never_lists_the_jobs_directory_as_a_run(self):          # R11-M25
        (self.home / "tuning" / "jobs" / "apps").mkdir(parents=True)
        (self.home / "tuning" / "qat-6").mkdir()
        out = io.StringIO()
        with mock.patch.object(tuning, "in_use", return_value="x"), mock.patch.object(sys, "stdout", out):
            tuning.main(["--status"])
        self.assertEqual(json.loads(out.getvalue())["runs"], ["qat-6"])

    def test_nothing_is_created_for_a_job_not_built(self):                  # KN-R11-15
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(tuning.main(["--job", "apps", "--background"]), 1)
        run, _ = self._incoming("a2", b"w")
        with self.assertRaisesRegex(tuning.TuneError, "not built"):
            tuning.select_run(run, "apps")
        self.assertFalse((self.home / "tuning").exists())
        tuning.deselect("apps")
        self.assertFalse(self.file.exists())
        self.assertFalse((self.home / "model.json.lock").exists())

    def test_a_panes_entry_not_in_the_old_form_is_kept_in_the_new(self):      # KN-R11-11
        self.file.write_text(json.dumps({"schema": tuning.SELECTION_SCHEMA, "jobs": {
            "panes": {"run": "no-weights"}, "apps": {"weights": "/w", "sha256": "ab" * 32}}}))
        tuning.deselect("apps")
        self.assertEqual(json.loads(self.file.read_text())["schema"], tuning.SELECTION_SCHEMA)
        tuning.select(self.home / "a3", "cd" * 32, "apps")   # still readable, still writable

    def test_a_malformed_entry_never_reaches_the_runtime(self):              # R11-M12
        self.file.write_text(json.dumps({"schema": tuning.SELECTION_SCHEMA,
                                         "jobs": {"panes": {"run": "no-weights"}}}))
        self.assertIsNone(tuning.selected("panes"))
        args = type("A", (), {"engine": None, "root": None})()
        with mock.patch.object(needle_cli, "_image", return_value=mock.Mock()):
            self.assertEqual(needle_cli.open_runtime(args).label, "base")

    def test_an_unreadable_file_is_not_read_as_empty(self):                  # R11-M26
        self.file.write_text(json.dumps({"weights": "/w", "sha256": "ab" * 32}))
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(tuning.TuneError, "cannot read"):
                tuning.select(self.home / "a4", "cd" * 32, "apps")
        self.assertIn("/w", self.file.read_text())

    def test_a_failed_write_leaves_no_temp_file(self):                       # R11-M27
        with mock.patch("json.dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                tuning.select(self.home / "a5", "cd" * 32, "apps")
        self.assertEqual([p.name for p in self.home.glob(".model.*")], [])

    def test_the_refusal_names_the_job_a_report_without_one_was_gated_for(self):   # KN-R11-08
        run, _ = self._incoming("old", b"w")
        report = json.loads((run / "gates.json").read_text())
        del report["job"]
        (run / "gates.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(tuning.TuneError, "gated for the 'panes' job"):
            tuning.select_run(run, "apps")


if __name__ == "__main__":
    unittest.main()
