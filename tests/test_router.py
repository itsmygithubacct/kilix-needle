"""The job router: features, the checked-in artifact, stages, and its gates."""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401 - cuts every route to the live desktop and store
import jobs
import router
import router_train

HERE = Path(__file__).resolve().parent.parent


def model_sure_of(label: str, p: float = 0.99) -> router.Model:
    """A model that ranks `label` first with probability about p, whatever the text."""
    import math
    rest = (1 - p) / (len(router.LABELS) - 1)
    bias = [math.log(p if job == label else rest) for job in router.LABELS]
    return router.Model({"labels": list(router.LABELS), "idf": {}, "weights": {},
                         "bias": bias, "threshold": 0.7})


class Features(unittest.TestCase):
    def test_words_bigrams_and_letters_within_words(self):
        f = router.features("Go  to TAB")
        self.assertEqual(f["w:go"], 1)
        self.assertEqual(f["b:to tab"], 1)
        self.assertIn("c: go", f)
        self.assertIn("c:tab ", f)
        self.assertNotIn("c:o t", f)        # never across a word boundary

    def test_text_is_bounded(self):
        self.assertEqual(router.features("x " * 100000), router.features("x " * 2000))


class Artifact(unittest.TestCase):
    def test_artifact_loads_and_matches_the_code_and_manifest(self):
        model = router.Model.load()
        manifest = json.loads((router.MODEL_DIR / "manifest.json").read_text())
        self.assertEqual(model.labels, router.LABELS)
        self.assertEqual(tuple(manifest["labels"]), router.LABELS)
        self.assertEqual(model.threshold, manifest["trainer"]["threshold"])

    def test_an_altered_artifact_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "router-model"
            shutil.copytree(router.MODEL_DIR, copy)
            data = json.loads((copy / "router.json").read_bytes())
            data["bias"][0] += 1.0
            (copy / "router.json").write_text(json.dumps(data))
            with self.assertRaisesRegex(router.RouterError, "digest"):
                router.Model.load(copy)

    def test_the_manifest_never_lists_a_gate_set(self):
        manifest = json.loads((router.MODEL_DIR / "manifest.json").read_text())
        gates = {job.heldout for job in jobs.JOBS.values()}
        self.assertFalse(gates & {source["path"] for source in manifest["sources"]})
        self.assertEqual(set(manifest["excluded_gate_sets"]), gates)

    def test_the_trainer_refuses_a_gate_set_before_opening_it(self):
        gate = jobs.JOBS["panes"].heldout
        manifest = {"sources": [{"path": gate, "rule": "eval", "job": "panes"}]}
        with mock.patch.object(router_train, "_lines", side_effect=AssertionError("opened")):
            with self.assertRaisesRegex(SystemExit, "gate set"):
                router_train.load_rows(manifest)

    def test_the_trainer_refuses_one_text_with_two_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.jsonl", Path(tmp) / "b.jsonl"
            a.write_text(json.dumps({"request": "Go to tab 2", "job": "panes"}) + "\n")
            b.write_text(json.dumps({"request": "go to  tab 2", "job": "apps"}) + "\n")
            manifest = {"sources": [{"path": str(a), "rule": "labelled"},
                                    {"path": str(b), "rule": "labelled"}]}
            with self.assertRaisesRegex(SystemExit, "labelled both"):
                router_train.load_rows(manifest)

    @unittest.skipUnless(shutil.which("python3"), "python3")
    def test_retraining_from_the_manifest_reproduces_the_artifact(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("training needs NumPy")
        manifest = json.loads((router.MODEL_DIR / "manifest.json").read_text())
        self.assertEqual(router_train.check(manifest), 0)


class Stages(unittest.TestCase):
    def test_one_grammar_reading_routes_without_the_classifier_deciding(self):
        route = router.route("split right and run htop")
        self.assertEqual((route.stage, route.job), (0, "panes"))
        self.assertEqual(route.claims, ["panes"])

    def test_a_sure_classifier_routes_at_stage_1(self):
        route = router.route("what is the capital of france")
        self.assertEqual((route.stage, route.job), (1, "none"))
        self.assertGreaterEqual(route.p, router.model().threshold)

    def test_a_claim_against_a_sure_classifier_is_unsure(self):
        with mock.patch.object(router, "claims", return_value=["tmux"]):
            route = router.route("send 'npm run dev' to the pane below", model_sure_of("panes"))
        self.assertEqual((route.stage, route.job), (2, None))
        self.assertEqual(route.candidates, ["tmux", "panes"])

    def test_below_the_threshold_the_top_jobs_are_candidates(self):
        model = model_sure_of("system", 0.5)
        with mock.patch.object(router, "claims", return_value=[]):
            route = router.route("anything", model)
        self.assertEqual((route.stage, route.job), (2, None))
        self.assertEqual(route.candidates[0], "system")
        self.assertNotIn("none", route.candidates)

    def test_a_router_failure_is_unsure_not_a_guess(self):
        with mock.patch.object(router, "model", side_effect=router.RouterError("gone")):
            route = router.route("show me htop so I can check the cpu")
        self.assertEqual((route.stage, route.job), ("error", None))
        with mock.patch.object(router, "model", side_effect=router.RouterError("gone")):
            route = router.route("split right and run htop")
        self.assertEqual((route.stage, route.job), (0, "panes"))      # a grammar still reads it

    def test_the_router_reads_only_the_text(self):
        import kilix
        with mock.patch.object(kilix, "snapshot", side_effect=AssertionError("live state read")), \
                mock.patch("subprocess.run", side_effect=AssertionError("process started")):
            for text in ("close the logs pane", "is docker running", "focus the firefox window"):
                router.route(text)

    def test_classifier_latency_stays_under_five_milliseconds(self):
        model = router.model()
        texts = [json.loads(line)["request"]
                 for line in (HERE / "evals/router/probe.jsonl").read_text().splitlines()]
        timings = []
        for _ in range(5):
            for text in texts:
                start = time.perf_counter()
                model.rank(text)
                timings.append(time.perf_counter() - start)
        timings.sort()
        self.assertLess(timings[int(0.95 * len(timings))], 0.005)


class Compatibility(unittest.TestCase):
    """Every request in the spent panes sets that the panes job admits stays with
    panes: a sure route never sends one elsewhere (unsure ones are answered by
    the panes job in the no-verb default). The current gate set is never read."""

    def test_spent_panes_requests_are_never_routed_away(self):
        gate = jobs.JOBS["panes"].heldout
        names = ["evals/dev.jsonl", "evals/test.jsonl"] + \
                [f"evals/heldout-v{i}.jsonl" for i in range(2, 15)]
        self.assertNotIn(gate, names)
        model = router.model()
        away, seen = [], 0
        for name in names:
            for line in (HERE / name).read_text().splitlines():
                case = json.loads(line) if line.strip() else {}
                if not case.get("expect"):
                    continue
                seen += 1
                route = router.route(case["request"], model)
                if route.stage in (0, 1) and route.job != "panes":
                    away.append((name, case["request"], route.job))
        self.assertGreater(seen, 1500)
        self.assertEqual(away, [])


if __name__ == "__main__":
    unittest.main()
