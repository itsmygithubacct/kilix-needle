"""Needle 3 (development): its pins, the runtime switch and evaluate's --generation 3."""
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401  (scrubs the environment and state)

import asset
import evaluate
import needle_cli
import toolset
import tuning


class Pins(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kn3-")
        self.addCleanup(self.dir.cleanup)

    def write(self, name, data):
        path = os.path.join(self.dir.name, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_the_library_is_held_to_its_pin(self):
        path = self.write("libneedle3.so", b"\0" * asset.NEEDLE3_LIB_BYTES)
        with self.assertRaisesRegex(asset.AssetError, "pinned SHA-256"):
            asset.needle3_library_from_file(path)
        # The Needle 2 library's pin is a different file, and it is refused too.
        with self.assertRaisesRegex(asset.AssetError, "bytes, expected"):
            asset.needle3_library_from_file(self.write("small.so", b"x"))

    def test_base_weights_are_held_to_the_needle3_cact_pin(self):
        path = self.write("needle3.cact", b"\0" * 64)
        with self.assertRaisesRegex(asset.AssetError, f"expected {asset.NEEDLE3_WEIGHTS_BYTES}"):
            asset.needle3_weights(path)

    def test_tuned_weights_are_held_to_the_given_digest(self):
        data = b"tuned weights"
        path = self.write("tuned.cact", data)
        with asset.needle3_weights(path, hashlib.sha256(data).hexdigest()) as image:
            self.assertEqual(image.sha256, hashlib.sha256(data).hexdigest())
        with self.assertRaisesRegex(asset.AssetError, "pinned SHA-256"):
            asset.needle3_weights(path, "0" * 64)

    def test_missing_tuned_weights_are_an_asset_error(self):
        with self.assertRaisesRegex(asset.AssetError, "cannot open the weights"):
            asset.needle3_weights(os.path.join(self.dir.name, "absent.cact"), "0" * 64)

    def test_the_pins_match_the_reference_download_when_present(self):
        refs = os.path.expanduser("~/research/refs/needle3/hf")
        cact = os.path.join(refs, "needle3.cact")
        if not os.path.isfile(cact):
            self.skipTest("no reference download")
        with asset.needle3_weights(cact) as image:
            self.assertEqual(image.sha256, asset.NEEDLE3_WEIGHTS_SHA256)


class Runtime(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kn3-")
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.multiple(tuning, APP_HOME=Path(self.dir.name),
                                      SELECTION=Path(self.dir.name) / "model.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.args = type("A", (), {"engine": None, "root": None})()

    def open(self, env, **patches):
        library, weights = mock.Mock(name="library"), mock.Mock(name="weights")
        with mock.patch.dict(os.environ, env), \
                mock.patch("asset.needle3_library_from_file", return_value=library) as lib, \
                mock.patch("asset.needle3_weights", return_value=weights, **patches) as w, \
                mock.patch.object(needle_cli, "LibEngine") as engine, \
                mock.patch.object(needle_cli, "_image", return_value=mock.Mock()) as base:
            try:
                runtime = needle_cli.open_runtime(self.args)
            except asset.AssetError as error:
                runtime = error
        return runtime, lib, w, engine, base, library

    def test_the_needle3_library_runs_the_five_tool_schema(self):
        runtime, lib, w, engine, base, _ = self.open(
            {"KILIX_NEEDLE3_LIBRARY": "/x/libneedle3.so", "KILIX_NEEDLE3_WEIGHTS": "/x/needle3.cact"})
        self.assertEqual(runtime.label, "needle3")
        lib.assert_called_once_with("/x/libneedle3.so")
        w.assert_called_once_with("/x/needle3.cact", None)
        self.assertIs(engine.call_args.args[1], toolset.TOOLS)
        self.assertIs(runtime.translate, toolset.to_actions)
        base.assert_not_called()

    def test_a_tuned_needle3_is_loaded_at_its_digest(self):
        _, _, w, _, _, _ = self.open({"KILIX_NEEDLE3_LIBRARY": "/x/l.so", "KILIX_NEEDLE3_WEIGHTS": "/x/t.cact",
                                      "KILIX_NEEDLE3_WEIGHTS_SHA256": "ab" * 32})
        w.assert_called_once_with("/x/t.cact", "ab" * 32)

    def test_needle3_takes_precedence_over_a_selected_tuned_needle2(self):
        tuning.select(Path(self.dir.name) / "run1", "ab" * 32)
        runtime, *_ = self.open({"KILIX_NEEDLE3_LIBRARY": "/x/l.so", "KILIX_NEEDLE3_WEIGHTS": "/x/w"})
        self.assertEqual(runtime.label, "needle3")

    def test_an_explicit_engine_wins_over_needle3(self):
        self.args.engine = "/some/engine"
        runtime, lib, *_ = self.open({"KILIX_NEEDLE3_LIBRARY": "/x/l.so"})
        self.assertEqual(runtime.label, "base")
        lib.assert_not_called()

    def test_bad_weights_are_an_error_not_a_fall_back_and_the_library_is_closed(self):
        runtime, _, _, engine, base, library = self.open(
            {"KILIX_NEEDLE3_LIBRARY": "/x/l.so", "KILIX_NEEDLE3_WEIGHTS": "/x/w"},
            side_effect=asset.AssetError("the engine does not match its pinned SHA-256"))
        self.assertIsInstance(runtime, asset.AssetError)
        library.close.assert_called_once()
        engine.assert_not_called()
        base.assert_not_called()

    def test_without_the_variable_nothing_changes(self):
        runtime, lib, *_ = self.open({})
        self.assertEqual(runtime.label, "base")
        lib.assert_not_called()


class Evaluate(unittest.TestCase):
    def test_generation_3_needs_library_and_weights(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as cases:
            cases.write('{"request": "new tab", "expect": [["open_tab", {}]]}\n')
            cases.flush()
            for extra in ([], ["--library", "/x/l.so"], ["--weights", "/x/w"]):
                with self.assertRaises(SystemExit) as stop, mock.patch.object(sys, "stderr", io.StringIO()):
                    evaluate.main([cases.name, "--generation", "3", *extra])
                self.assertEqual(stop.exception.code, 2)

    def test_generation_3_loads_the_needle3_pins(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as cases:
            cases.write('{"request": "new tab", "expect": [["open_tab", {}]]}\n')
            cases.flush()
            with mock.patch("asset.needle3_library_from_file",
                            side_effect=asset.AssetError("pinned")) as lib, \
                    self.assertRaises(asset.AssetError):
                evaluate.main([cases.name, "--generation", "3", "--library", "/x/l.so", "--weights", "/x/w"])
            lib.assert_called_once_with("/x/l.so")


if __name__ == "__main__":
    unittest.main()
