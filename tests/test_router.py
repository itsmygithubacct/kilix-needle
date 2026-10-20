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

    def test_the_trainer_refuses_a_gate_set_under_any_name_before_reading_or_hashing(self):
        gate = jobs.JOBS["agents"].heldout
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "alias.jsonl"
            link.symlink_to(HERE / gate)
            hard = Path(tmp) / "hard.jsonl"
            try:
                os.link(HERE / gate, hard)
            except OSError:
                hard = link
            spellings = ["./" + gate, str(HERE / gate), "evals/../" + gate, str(link), str(hard)]
            ordinary = {"path": "evals/router/none.jsonl", "rule": "labelled"}
            manifests = [{"sources": [{"path": name, "rule": "eval", "job": "agents"}]}
                         for name in spellings]
            manifests += [{"sources": [ordinary], "relabels": {"path": name}} for name in spellings]
            for manifest in manifests:
                with self.subTest(manifest), \
                        mock.patch.object(router_train, "_lines", side_effect=AssertionError("read")), \
                        mock.patch.object(router_train, "_sha", side_effect=AssertionError("hashed")):
                    with self.assertRaisesRegex(SystemExit, "gate set"):
                        router_train.load_rows(manifest)
                    with self.assertRaisesRegex(SystemExit, "gate set"):
                        router_train.check(manifest)

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

    def test_an_exact_apps_reading_routes_below_the_threshold(self):
        # Stage 0 is the grammar's decision, not the classifier's: an apps claim
        # routes even when the classifier is unsure of everything.
        model = model_sure_of("none", 0.4)
        for text in ("fancy a round of joustix", "hide the clock", "I'm bored, open solitaire",
                     "mute the microphone", "set the font size to 16", "pause the music"):
            route = router.route(text, model)
            self.assertEqual((route.stage, route.job, route.claims), (0, "apps", ["apps"]), text)

    def test_a_system_status_control_is_left_to_the_system_job(self):
        self.assertEqual(router.claims("how much memory is free"), ["system"])

    def test_an_apps_reading_that_leaves_another_jobs_clause_is_no_claim(self):
        for text in ("split right and open solitaire", "show memory and hide the clock"):
            self.assertNotIn("apps", router.claims(text), text)

    def test_the_apps_shortcut_reads_what_propose_and_interpret_read(self):
        import apps
        for name in ("evals/apps/dev.jsonl", "evals/apps/test.jsonl"):
            for line in (HERE / name).read_text().splitlines():
                text = json.loads(line)["request"]
                full, fast = apps.propose(text), router._apps_calls(text)
                if fast != full:      # propose offers one named call when nothing is supported
                    self.assertEqual(fast, [], text)
                    self.assertFalse(any(isinstance(r, apps.Action)
                                         for r in apps.interpret(text, full)), text)
                results = apps.interpret(text, full) if full else []
                whole = bool(results) and all(isinstance(r, apps.Action) for r in results)
                if not whole:         # the shortcut never claims what interpret refuses
                    self.assertFalse(router._claims_apps(text) and
                                     __import__("app_controls").parse(text) is None, text)
        self.assertNotIn("apps", router.claims("show the clock and hide the clock"))

    def test_two_jobs_work_in_one_request_goes_back_as_two(self):
        route = router.route("split right and open solitaire", model_sure_of("panes", 0.5))
        self.assertEqual((route.stage, route.job, route.candidates), ("multi", None, ["panes", "apps"]))
        # One job's grammar reading all of it, or a sure classifier, is not two jobs.
        self.assertEqual(router.route("start codex in /tmp and split right").job, "agents")
        route = router.route("split right and open solitaire", model_sure_of("panes"))
        self.assertEqual(route.stage, 1)

    def test_a_router_failure_is_unsure_not_a_guess(self):
        for text in ("show me htop so I can check the cpu", "split right and run htop"):
            with mock.patch.object(router, "model", side_effect=router.RouterError("gone")):
                route = router.route(text)
            self.assertEqual((route.stage, route.job, route.candidates), ("error", None, []), text)

    def test_an_altered_artifact_routes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "router-model"
            shutil.copytree(router.MODEL_DIR, copy)
            data = json.loads((copy / "router.json").read_bytes())
            data["bias"][0] += 1.0
            (copy / "router.json").write_text(json.dumps(data))
            with mock.patch.object(router, "MODEL_DIR", copy), mock.patch.object(router, "_model", None):
                route = router.route("close tab 2")
        self.assertEqual((route.stage, route.job), ("error", None))
        self.assertIn("digest", route.note)

    def test_a_grammar_that_fails_routes_nothing(self):
        broken = dict(router.CLAIMS, panes=mock.Mock(side_effect=RuntimeError("parser bug")))
        with mock.patch.object(router, "CLAIMS", broken):
            route = router.route("close tab 2")
        self.assertEqual((route.stage, route.job), ("error", None))
        self.assertIn("RuntimeError", route.note)

    def test_the_router_reads_only_the_text(self):
        import kilix
        with mock.patch.object(kilix, "snapshot", side_effect=AssertionError("live state read")), \
                mock.patch("subprocess.run", side_effect=AssertionError("process started")):
            for text in ("close the logs pane", "is docker running", "focus the firefox window"):
                router.route(text)

    def test_routing_stays_under_five_milliseconds(self):
        # The whole route, grammars included, in CPU time (a busy machine
        # stretches wall time, not the work), over every job's test requests.
        # Requests naming an app cost most (p95 about 6 ms on apps alone).
        model = router.model()
        names = ["evals/router/probe.jsonl", "evals/test.jsonl"] + \
                [f"evals/{job}/test.jsonl" for job in ("agents", "apps", "files", "system")]
        texts = [json.loads(line)["request"] for name in names
                 for line in (HERE / name).read_text().splitlines() if line.strip()]
        for text in texts:
            router.route(text, model)
        best = {}
        for _ in range(3):      # each request's best of three: interference only adds
            for text in texts:
                start = time.thread_time()
                router.route(text, model)
                best[text] = min(best.get(text, 1.0), time.thread_time() - start)
        timings = sorted(best.values())
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

    def test_the_agent_guides_no_verb_examples_stay_with_panes(self):
        # Kilix's docs/AGENTS.md teaches these as `kilix-needle "REQUEST"` calls.
        for text in ("split right", "run 'pwd' in the pane titled build",
                     "focus pane 12", "close pane 12"):
            route = router.route(text)
            self.assertEqual((route.stage, route.job), (0, "panes"), text)


if __name__ == "__main__":
    unittest.main()
