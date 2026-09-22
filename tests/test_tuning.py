"""The tuner: data consistency, the gates, and model selection."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401
import needle_cli
import toolset
import tuning


def result(exact, cases, unsafe=0, tags=None):
    return {"totals": {"cases": cases, "exact": exact, "unsafe": unsafe},
            "tags": tags or {"all": {"cases": cases, "exact": exact}}}


class Gates(unittest.TestCase):
    manifest = tuning.load_manifest()

    def gate(self, tuned, reference, others=None):
        results = {"dev": result(30, 40), "test": result(60, 86), "heldout": tuned}
        results.update(others or {})
        return tuning.gate(self.manifest, results, reference)

    def test_a_clear_gain_passes(self):
        self.assertEqual(self.gate(result(70, 100), result(60, 100)), [])

    def test_a_small_gain_fails(self):
        failures = self.gate(result(63, 100), result(60, 100))
        self.assertTrue(any("gain" in f for f in failures), failures)

    def test_any_unsafe_action_on_any_set_fails(self):
        failures = self.gate(result(90, 100), result(60, 100),
                             {"dev": result(40, 40, unsafe=1)})
        self.assertTrue(any("dev: 1 unsafe" in f for f in failures), failures)

    def test_a_regressed_tag_fails_even_with_a_gain(self):
        tuned = result(80, 100, tags={"close_tab": {"cases": 6, "exact": 1},
                                      "open_pane": {"cases": 94, "exact": 79}})
        reference = result(60, 100, tags={"close_tab": {"cases": 6, "exact": 6},
                                          "open_pane": {"cases": 94, "exact": 54}})
        failures = self.gate(tuned, reference)
        self.assertTrue(any("close_tab lost 5" in f for f in failures), failures)


class Data(unittest.TestCase):
    def test_training_data_is_consistent_and_excludes_every_eval_request(self):
        manifest = tuning.load_manifest()
        with tempfile.TemporaryDirectory(prefix="kn-") as tmp:
            out = Path(tmp) / "train.jsonl"
            stats = tuning.build_data(tuning.LIBRARY, manifest, out)
            rows = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual(stats["inconsistent_dropped"], 0)
        self.assertEqual(stats["kept"], len(rows))
        evals = set()
        for rel in manifest["data"]["exclude"]:
            evals |= {" ".join(json.loads(l)["request"].casefold().split())
                      for l in open(rel) if l.strip()}
        queries = {" ".join(r["query"].casefold().split()) for r in rows}
        self.assertEqual(queries & evals, set())
        self.assertEqual(rows[0]["tools"], toolset.TOOLS)
        self.assertTrue(all(set(r) == {"query", "tools", "answers"} for r in rows))


class InconsistentExamples(unittest.TestCase):
    def test_an_example_the_checks_refuse_is_dropped_and_counted(self):
        rows = [{"query": "go to the left pane", "actions": [["close_pane", {"pane": "left"}]]},
                {"query": "next tab", "actions": [["go_to_tab", {"tab": "next"}]]}]
        fake = mock.Mock(generate=mock.Mock(return_value=(rows, 0)), _fold=lambda t: t)
        manifest = dict(tuning.load_manifest())
        manifest["data"] = dict(manifest["data"], exclude=[])
        with tempfile.TemporaryDirectory(prefix="kn-") as tmp, \
                mock.patch.dict(sys.modules, {"generate": fake}):
            out = Path(tmp) / "train.jsonl"
            stats = tuning.build_data(Path(tmp), manifest, out)
            lines = out.read_text().splitlines()
        self.assertEqual((stats["kept"], stats["inconsistent_dropped"]), (1, 1))
        self.assertEqual(json.loads(lines[0])["query"], "next tab")


class Selection(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-")
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.multiple(tuning, APP_HOME=Path(self.dir.name),
                                      SELECTION=Path(self.dir.name) / "model.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_select_and_deselect(self):
        self.assertIsNone(tuning.selected())
        tuning.select(Path(self.dir.name) / "run1", "ab" * 32)
        self.assertEqual(tuning.selected()["sha256"], "ab" * 32)
        tuning.main(["--deselect"])
        self.assertIsNone(tuning.selected())

    def test_an_unavailable_tuned_model_falls_back_with_a_notice(self):
        tuning.select(Path(self.dir.name) / "missing-run", "ab" * 32)
        args = type("A", (), {"engine": None, "root": None})()
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr), \
                mock.patch.object(needle_cli, "_image", return_value=mock.Mock()) as base:
            runtime = needle_cli.open_runtime(args)
        self.assertEqual(runtime.label, "base")
        self.assertIn("using the base model", stderr.getvalue())
        base.assert_called_once()

    def test_an_explicit_engine_ignores_the_selection(self):
        tuning.select(Path(self.dir.name) / "run1", "ab" * 32)
        args = type("A", (), {"engine": "/some/engine", "root": None})()
        with mock.patch.object(needle_cli, "_image", return_value=mock.Mock()):
            runtime = needle_cli.open_runtime(args)
        self.assertEqual(runtime.label, "base")


if __name__ == "__main__":
    unittest.main()


class FirstUseOffer(unittest.TestCase):
    def offer(self, missing, answer):
        args = type("A", (), {"root": None})()
        out = io.StringIO()
        with mock.patch("asset.missing_for_tuning", return_value=missing), \
                mock.patch("builtins.input", return_value=answer) as asked, \
                mock.patch.object(tuning, "main") as start, \
                mock.patch.object(sys, "stdout", out):
            needle_cli._offer_tuning(args)
        return start, asked, out.getvalue()

    def test_missing_assets_are_named_and_nothing_starts(self):
        start, asked, out = self.offer(["needle2-train"], "y")
        start.assert_not_called()
        asked.assert_not_called()
        self.assertIn("kilix-needle install --tuning", out)

    def test_yes_or_enter_starts_in_the_background(self):
        for answer in ("", "y", "YES"):
            start, _, _ = self.offer([], answer)
            start.assert_called_once_with(["--background"])

    def test_no_starts_nothing(self):
        start, _, out = self.offer([], "n")
        start.assert_not_called()
        self.assertIn("tune --background", out)
