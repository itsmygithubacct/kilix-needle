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
        workers = [context.Process(target=write, args=(job, 60)) for job in ("panes", "apps")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(60)
        self.assertEqual([w.exitcode for w in workers], [0, 0])
        self.assertEqual(tuning.selected("panes")["run"], "panes-59")
        self.assertEqual(tuning.selected("apps")["run"], "apps-59")

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
        with self.assertRaises(SystemExit) as stop, mock.patch.object(sys, "stderr", io.StringIO()):
            evaluate.main(["evals/dev.jsonl", "--job", "apps"])
        self.assertEqual(stop.exception.code, 2)

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
                mock.patch.object(tuning, "stage_gates", return_value={"failures": []}) as gates:
            target = tuning.select_run(run, "apps")
        self.assertEqual(gates.call_args.args[-1], "apps")
        self.assertEqual(target, self.home / "tuning" / "jobs" / "apps" / "a1")
        self.assertEqual(tuning.selected("apps")["sha256"], digest)
        self.assertEqual(tuning.selected("panes")["run"], "qat-6")

    def test_a_selected_run_is_never_replaced_or_deleted(self):             # KN-R11-03
        first, digest = self._incoming("same", b"first")
        with mock.patch("asset.installed_library", return_value=mock.MagicMock()), \
                mock.patch.object(tuning, "stage_gates", return_value={"failures": []}):
            target = tuning.select_run(first, "apps")
        second, _ = self._incoming("same-2", b"second")
        (self.home / "incoming2").mkdir()
        moved = self.home / "incoming2" / "same"
        second.rename(moved)
        with mock.patch("asset.installed_library", return_value=mock.MagicMock()), \
                mock.patch.object(tuning, "stage_gates", return_value={"failures": ["no"]}):
            with self.assertRaisesRegex(tuning.TuneError, "selected already"):
                tuning.select_run(moved, "apps")
        self.assertEqual((target / "tuned.cact").read_bytes(), b"first")
        self.assertEqual(tuning.selected("apps")["sha256"], digest)

    def test_the_refusal_names_the_job_a_report_without_one_was_gated_for(self):   # KN-R11-08
        run, _ = self._incoming("old", b"w")
        report = json.loads((run / "gates.json").read_text())
        del report["job"]
        (run / "gates.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(tuning.TuneError, "gated for the 'panes' job"):
            tuning.select_run(run, "apps")


if __name__ == "__main__":
    unittest.main()
