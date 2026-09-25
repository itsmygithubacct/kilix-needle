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


if __name__ == "__main__":
    unittest.main()
