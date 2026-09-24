"""The tuner: data consistency, the gates, and model selection."""
import io
import json
import os
from pathlib import Path
import sys
import subprocess
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
                      for l in Path(rel).read_text().splitlines() if l.strip()}
        queries = {" ".join(r["query"].casefold().split()) for r in rows}
        self.assertEqual(queries & evals, set())
        self.assertEqual(rows[0]["tools"], toolset.TOOLS)
        self.assertTrue(all(set(r) == {"query", "tools", "reasoning", "answers"} for r in rows))
        # run 1 trained with empty reasoning and lost argument filling
        self.assertTrue(all(r["reasoning"] for r in rows))

    def test_reasoning_names_the_source_words(self):
        calls = toolset.from_actions([["open_pane", {"side": "left", "program": "htop"}]])
        text = tuning.reasoning_for(calls, [["on the left", "left"], ["htop", "htop"]])
        self.assertEqual(text, "User wants to open a pane. 'on the left' -> side 'left'; "
                               "'htop' -> program 'htop'.")
        self.assertTrue(tuning.reasoning_for([], []).startswith("No tool fits"))


class InconsistentExamples(unittest.TestCase):
    def test_an_example_the_checks_refuse_is_dropped_and_counted(self):
        rows = [{"query": "go to the left pane", "actions": [["close_pane", {"pane": "left"}]]},
                {"query": "next tab", "actions": [["go_to_tab", {"tab": "next"}]]}]
        fake = mock.Mock(generate=mock.Mock(return_value=(rows, 0)), _fold=lambda t: t)
        manifest = dict(tuning.load_manifest())
        manifest["data"] = dict(manifest["data"], exclude=[])
        with tempfile.TemporaryDirectory(prefix="kn-") as tmp, \
                mock.patch.object(tuning, "load_generator", return_value=fake):
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


class DomainPackMigration(unittest.TestCase):
    def test_the_library_is_kilix_mls_pack_from_the_pinned_submodule(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KILIX_ML_HOME", None)
            self.assertEqual(tuning.library_path(),
                             tuning.REPO / "third_party" / "kilix-ml" / "domains" / "kilix_panes")
        self.assertFalse((tuning.REPO / "tuning-library").exists())   # one source only
        self.assertEqual(tuning.SUPPLEMENT, tuning.REPO / "corpus-supplement")

    def test_an_explicit_ml_home_never_falls_back_silently(self):
        with tempfile.TemporaryDirectory(prefix="kn-") as tmp, \
                mock.patch.dict(os.environ, {"KILIX_ML_HOME": tmp}):
            expected = Path(tmp) / "domains" / "kilix_panes"
            self.assertEqual(tuning.library_path(), expected)
            with self.assertRaises(tuning.TuneError):
                tuning.load_manifest(expected)

    def test_generators_from_two_packs_do_not_share_import_cache(self):
        with tempfile.TemporaryDirectory(prefix="kn-") as tmp:
            roots = [Path(tmp) / "a", Path(tmp) / "b"]
            for i, root in enumerate(roots):
                root.mkdir()
                (root / "generate.py").write_text(f"value = {i}\n")
            self.assertEqual([tuning.load_generator(p).value for p in roots], [0, 1])

    def test_needle_recipe_counts_actions_it_cannot_express(self):
        rows = [{"query": "maximize this pane", "actions": [["maximize_pane", {}]]}]
        fake = mock.Mock(generate=mock.Mock(return_value=(rows, 0)))
        manifest = tuning.load_manifest()
        manifest["data"]["exclude"] = []
        with tempfile.TemporaryDirectory(prefix="kn-") as tmp, \
                mock.patch.object(tuning, "load_generator", return_value=fake):
            stats = tuning.build_data(Path(tmp), manifest, Path(tmp) / "train.jsonl")
        self.assertEqual((stats["kept"], stats["unsupported_dropped"]), (0, 1))


class Recipe(unittest.TestCase):
    def test_the_recipe_trains_quantisation_aware_and_excludes_every_eval_set(self):
        pack = tuning.load_manifest()
        manifest = tuning.recipe(pack)
        self.assertIs(manifest["train"]["qat"], True)
        self.assertEqual(manifest["train"]["epochs"], 4)
        self.assertEqual(manifest["gates"]["heldout"], "evals/heldout-v8.jsonl")
        evals = {str(p.relative_to(tuning.REPO)) for p in (tuning.REPO / "evals").glob("*.jsonl")}
        self.assertIn("evals/heldout-v8.jsonl", evals)
        self.assertLessEqual(evals, set(manifest["data"]["exclude"]))
        self.assertIn(manifest["gates"]["heldout"], manifest["data"]["exclude"])
        # the pack's own manifest is not changed
        self.assertNotIn("qat", pack["train"])

    def test_a_share_cap_subsamples_only_the_large_action(self):
        def row(name, i):
            args = {"direction": "wider"} if name == "resize_pane" else {"layout": "grid"}
            return {"query": f"{name} {i}", "answers": toolset.from_actions([[name, args]])}
        rows = [row("resize_pane", i) for i in range(12)] + [row("arrange_panes", i) for i in range(5)]
        kept, capped = tuning.cap_share(rows, 0.3, 0)
        self.assertEqual(capped, {"resize_pane": [12, 5]})  # int(0.3 * 17)
        shapes = [tuning._shape(r["answers"]) for r in kept]
        self.assertEqual((shapes.count("resize_pane"), shapes.count("arrange_panes")), (5, 5))
        self.assertEqual(tuning.cap_share(rows, 0.3, 0), (kept, capped))

    def test_a_supplement_never_replaces_a_pack_template(self):
        with tempfile.TemporaryDirectory(prefix="kn-") as tmp:
            pack, extra = Path(tmp) / "pack", Path(tmp) / "extra"
            (pack / "corpus/actions").mkdir(parents=True)
            (extra / "actions").mkdir(parents=True)
            (pack / "corpus/actions/open_pane.json").write_text("{}")
            (extra / "actions/open_pane_supp.json").write_text("{}")
            added = tuning.stage_library(pack, [extra], Path(tmp) / "staged")
            self.assertEqual(added, ["open_pane_supp.json"])
            self.assertTrue((Path(tmp) / "staged/corpus/actions/open_pane.json").exists())
            (extra / "actions/open_pane.json").write_text("{}")
            with self.assertRaises(tuning.TuneError):
                tuning.stage_library(pack, [extra], Path(tmp) / "staged")

    def test_the_recipe_data_uses_the_supplement_and_the_cap(self):
        manifest = tuning.recipe(tuning.load_manifest())
        with tempfile.TemporaryDirectory(prefix="kn-") as tmp:
            stats = tuning.build_data(tuning.LIBRARY, manifest, Path(tmp) / "train.jsonl")
            rows = [json.loads(line) for line in (Path(tmp) / "train.jsonl").read_text().splitlines()]
        self.assertIn("resize_supp2.json", stats["supplement_files"])
        self.assertEqual(stats["kept"], len(rows))
        self.assertIn("resize_pane", stats["capped"])
        shares = {}
        for r in rows:
            shares[tuning._shape(r["answers"])] = shares.get(tuning._shape(r["answers"]), 0) + 1
        self.assertLessEqual(max(shares.values()), stats["capped"]["resize_pane"][1])


class QuantisationAwareTraining(unittest.TestCase):
    """The training script, run against a stand-in for upstream's package."""

    FAKE = {
        "needle/__init__.py": "",
        "needle/model/__init__.py": "",
        "needle/model/run.py":
            "class C: weight_bits = 'embedding=4,default=2'\n"
            "def load_checkpoint(path): return None, C()\n",
        "needle/model/quantize.py":
            "def parse_bits_map(spec): return {'embedding': 4}, 2\n"
            "def cq_ste_mixed_params(p, bits, default): return ('quantised', p, bits, default)\n",
        "needle/model/finetune.py":
            "import json\n"
            "def merge_lora(params, lora, scale): return ('merged', params)\n"
            "def finetune_local(a):\n"
            "    import needle.model.finetune as f\n"
            "    json.dump({'merged': repr(f.merge_lora('P', 'L', 2.0)), 'epochs': a.epochs},"
            " open(a.out, 'w'))\n",
    }

    def run_script(self, qat):
        manifest = tuning.recipe(tuning.load_manifest())
        train = dict(manifest["train"], qat=qat)
        with tempfile.TemporaryDirectory(prefix="kn-") as tmp:
            for rel, text in self.FAKE.items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(text)
            out = Path(tmp) / "out.json"
            script = tuning._TRAIN.format(data="d.jsonl", out_dir=tmp, lora=str(out), **train)
            done = subprocess.run([sys.executable, "-B", "-c", script], cwd=tmp,
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            return json.loads(out.read_text())

    def test_training_goes_through_the_checkpoints_quantiser(self):
        result = self.run_script(True)
        self.assertEqual(result["merged"], "('quantised', ('merged', 'P'), {'embedding': 4}, 2)")
        self.assertEqual(result["epochs"], 4)

    def test_float_training_is_the_plain_merge(self):
        self.assertEqual(self.run_script(False)["merged"], "('merged', 'P')")

    def test_export_is_never_patched(self):
        self.assertNotIn("cq_ste", tuning._EXPORT)


class SelectRun(unittest.TestCase):
    """A run tuned elsewhere is used only with its own passing gate report."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-")
        self.addCleanup(self.dir.cleanup)
        home = Path(self.dir.name) / "home"
        patcher = mock.patch.multiple(tuning, APP_HOME=home, SELECTION=home / "model.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run_dir = Path(self.dir.name) / "qat-6"
        self.run_dir.mkdir()
        (self.run_dir / "tuned.cact").write_bytes(b"weights")
        self.sha = tuning.sha256_file(self.run_dir / "tuned.cact")

    def report(self, **fields):
        body = {"cact_sha256": self.sha, "failures": []}
        body.update(fields)
        (self.run_dir / "gates.json").write_text(json.dumps(body))

    def gated(self, failures=()):
        """The gates run here, on the copied bytes, with the installed runtime."""
        library = mock.MagicMock()
        return mock.patch.multiple(
            tuning, stage_gates=mock.Mock(return_value={"failures": list(failures)})), \
            mock.patch("asset.installed_library", return_value=library)

    def test_a_passing_run_is_copied_and_selected(self):
        self.report()
        gates, library = self.gated()
        with gates, library:
            target = tuning.select_run(self.run_dir)
            self.assertEqual(tuning.stage_gates.call_args.args[3], self.sha)
            self.assertEqual(tuning.stage_gates.call_args.args[0].root, target)
        choice = tuning.selected()
        self.assertEqual(choice["sha256"], self.sha)
        self.assertEqual(Path(choice["weights"]), target / "tuned.cact")
        self.assertTrue(str(target).startswith(str(tuning.APP_HOME)))

    def test_a_report_that_passes_is_not_enough_the_gates_run_here(self):
        # Review KN-06: random bytes with a hand-written passing report.
        self.report()
        gates, library = self.gated(["held-out exact gain -40.0 points, needs +5"])
        with gates, library, self.assertRaises(tuning.TuneError):
            tuning.select_run(self.run_dir)
        self.assertIsNone(tuning.selected())

    def test_status_says_what_answers_not_only_what_is_selected(self):
        # Hermetic (review KN-R3-04): the base engine is a stub here, never the
        # live store's installed asset.
        with mock.patch("asset.from_installed", return_value=mock.MagicMock()):
            self.assertEqual(tuning.in_use(), "base")
            tuning.select(self.run_dir, self.sha)
            with mock.patch("asset.installed_library",
                            side_effect=__import__("asset").AssetError("runtime not accepted")):
                self.assertIn("unavailable", tuning.in_use())

    def test_no_runtime_means_no_select_and_nothing_left_behind(self):      # R22
        import asset
        self.report()
        with mock.patch("asset.installed_library", side_effect=asset.AssetError("not accepted")):
            with self.assertRaisesRegex(tuning.TuneError, "need the needle2 runtime"):
                tuning.select_run(self.run_dir)
        self.assertIsNone(tuning.selected())
        self.assertFalse((tuning.APP_HOME / "tuning" / self.run_dir.name).exists())

    def test_weights_the_runtime_rejects_are_a_refusal_not_a_traceback(self):   # KN-R2-07
        from libengine import LibEngineError
        self.report()
        with mock.patch.object(tuning, "stage_gates", side_effect=LibEngineError("rejected")), \
                mock.patch("asset.installed_library", return_value=mock.MagicMock()):
            with self.assertRaisesRegex(tuning.TuneError, "rejected the weights"):
                tuning.select_run(self.run_dir)
        self.assertFalse((tuning.APP_HOME / "tuning" / self.run_dir.name).exists())

    def test_in_use_starts_what_a_request_starts(self):                      # R23, KN-R2-06
        import asset
        from libengine import LibEngineError
        tuning.select(self.run_dir, self.sha)
        engine_ok = mock.patch("asset.from_installed", return_value=mock.MagicMock())
        library = mock.patch("asset.installed_library", return_value=mock.MagicMock())
        with engine_ok, library, mock.patch("asset.load_verified", side_effect=asset.AssetError("digest")):
            self.assertTrue(tuning.in_use().startswith("base (the selected tuned model is unavailable"))
        with engine_ok, library, mock.patch("asset.load_verified", return_value=mock.MagicMock()), \
                mock.patch("libengine.LibEngine.start", side_effect=LibEngineError("bad weights")):
            self.assertTrue(tuning.in_use().startswith("none (the runtime rejected"))
        tuning.SELECTION.unlink()
        with mock.patch("asset.from_installed", side_effect=asset.AssetError("not installed")):
            self.assertEqual(tuning.in_use(), "none (not installed)")

    def test_a_failed_gate_is_refused(self):
        # The report screens before anything is copied or run (M62).
        self.report(failures=["held-out exact gain +3.0 points, needs +5"])
        gates, library = self.gated()
        with gates, library, self.assertRaisesRegex(tuning.TuneError, "did not pass its gates"):
            tuning.select_run(self.run_dir)
            tuning.stage_gates.assert_not_called()
        self.assertIsNone(tuning.selected())

    def test_other_bytes_than_the_gated_ones_are_refused(self):
        self.report()
        (self.run_dir / "tuned.cact").write_bytes(b"other weights")
        gates, library = self.gated()
        with gates, library, self.assertRaisesRegex(tuning.TuneError, "not the model"):   # M63
            tuning.select_run(self.run_dir)
        self.assertIsNone(tuning.selected())

    def test_no_report_is_refused(self):
        with self.assertRaises(tuning.TuneError):
            tuning.select_run(self.run_dir)
        self.assertIsNone(tuning.selected())


class CheckpointDigest(unittest.TestCase):
    """Review KN-10/KN-11: the pickle is never loaded unless its digest matches."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-")
        self.addCleanup(self.dir.cleanup)
        self.manifest = tuning.recipe(tuning.load_manifest())

    def test_stage_base_refuses_a_checkpoint_that_does_not_match(self):
        base = Path(self.dir.name) / "base"
        for rel in ("checkpoints/needle2.pkl", "tokenizer/tokenizer.model",
                    "tokenizer/tokenizer.vocab"):
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_bytes(b"not the pinned bytes")
        run = tuning.Run(Path(self.dir.name) / "run")
        with self.assertRaisesRegex(tuning.TuneError, "does not match its pinned digest"):
            tuning.stage_base(run, self.manifest, base)
        self.assertFalse((run.root / "base/checkpoints/needle2.pkl").exists())

    def test_a_resumed_run_rechecks_the_checkpoint_before_training(self):
        run = tuning.Run(Path(self.dir.name) / "run")
        (run.root / "src/checkpoints").mkdir(parents=True)
        (run.root / "src/checkpoints/needle2.pkl").write_bytes(b"swapped after base")
        with mock.patch.object(tuning, "_python_stage") as stage:
            with self.assertRaises(tuning.TuneError):
                tuning.stage_train(run, self.manifest)
            with self.assertRaises(tuning.TuneError):
                tuning.stage_export(run, self.manifest)
        stage.assert_not_called()


class NetworkNotice(unittest.TestCase):
    def test_a_stage_without_a_network_namespace_says_so(self):              # R27, KN-16
        with tempfile.TemporaryDirectory(prefix="kn-") as tmp:
            run = tuning.Run(Path(tmp))
            (run.root / "src").mkdir()
            with mock.patch.object(tuning, "_offline_prefix", return_value=[]), \
                    mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)):
                tuning._python_stage(run, "pass", "training")
            self.assertIn("has network access", run.log_path.read_text())
