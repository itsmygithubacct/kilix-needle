"""Installed libraries win over the checkout copies."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import asset
import tuning
from needle_logs.library import PACKAGED_INVENTORY, inventory_module, inventory_path
from needle_logs.sources import (
    PACKAGED_RECORDS, PACKAGED_REPLAY, _REPLAY_MODULE, _readers, _records_path, _replay_package, _replayer)


class InstalledLibraryTests(unittest.TestCase):
    def test_content_uses_the_installed_tree_before_the_checkout(self):
        with tempfile.TemporaryDirectory(prefix="needle-content-") as raw:
            packaged = Path(raw) / "src"
            (packaged / "kilix_content").mkdir(parents=True)
            (packaged / "kilix_content" / "__init__.py").write_text("marker = 'installed'\n")
            with mock.patch.object(asset, "PACKAGED_CONTENT", packaged):
                self.assertEqual(asset.content_src(), packaged)
        missing = Path(raw) / "absent"
        with mock.patch.object(asset, "PACKAGED_CONTENT", missing):
            self.assertEqual(
                asset.content_src(),
                Path(asset.__file__).resolve().parent / "third_party" / "kilix-content" / "src")

    def test_ml_uses_the_installed_tree_unless_a_home_is_set(self):
        with tempfile.TemporaryDirectory(prefix="needle-ml-") as raw:
            packaged = Path(raw) / "share"
            (packaged / "domains" / "kilix_panes").mkdir(parents=True)
            env = {key: value for key, value in os.environ.items() if key != "KILIX_ML_HOME"}
            with mock.patch.object(tuning, "PACKAGED_ML", packaged), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(tuning.library_path(), packaged / "domains" / "kilix_panes")
            home = Path(raw) / "home"
            with mock.patch.object(tuning, "PACKAGED_ML", packaged), \
                    mock.patch.dict(os.environ, {"KILIX_ML_HOME": str(home)}):
                self.assertEqual(tuning.library_path(), home / "domains" / "kilix_panes")
        with mock.patch.object(tuning, "PACKAGED_ML", Path(raw) / "absent"), \
                mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                tuning.library_path(),
                tuning.REPO / "third_party" / "kilix-ml" / "domains" / "kilix_panes")

    def test_rollout_and_replay_prefer_installed_files(self):
        with tempfile.TemporaryDirectory(prefix="needle-adapters-") as raw:
            root = Path(raw)
            records = root / "records.py"
            records.write_text("def adapt_record(row):\n    return 'installed-record'\n")
            replay = root / "replay"
            replay.mkdir()
            replay.joinpath("__init__.py").write_text(textwrap.dedent("""\
                def clean_bytes(data, rows=None, provider=None):
                    return 'installed-replay'
                """))
            inventory = root / "inventory.py"
            inventory.write_text("MARKER = 'installed-inventory'\n")
            sys.modules.pop(_REPLAY_MODULE, None)
            with mock.patch.object(sys.modules["needle_logs.sources"], "PACKAGED_RECORDS", records), \
                    mock.patch.object(sys.modules["needle_logs.sources"], "PACKAGED_REPLAY", replay), \
                    mock.patch.object(sys.modules["needle_logs.library"], "PACKAGED_INVENTORY", inventory):
                self.assertEqual(_records_path(), records)
                self.assertEqual(_readers()(None), "installed-record")
                self.assertEqual(_replay_package(), replay)
                self.assertEqual(_replayer()(b"x"), "installed-replay")
                self.assertEqual(inventory_path(), inventory)
                self.assertEqual(inventory_module().MARKER, "installed-inventory")
            sys.modules.pop(_REPLAY_MODULE, None)
            missing = root / "missing"
            with mock.patch.object(sys.modules["needle_logs.sources"], "PACKAGED_RECORDS", missing / "records.py"), \
                    mock.patch.object(sys.modules["needle_logs.sources"], "PACKAGED_REPLAY", missing), \
                    mock.patch.object(sys.modules["needle_logs.library"], "PACKAGED_INVENTORY", missing / "inventory.py"):
                self.assertTrue(str(_records_path()).endswith(
                    "third_party/kilix-tui-utils/src/kilix_rollout/records.py"))
                self.assertTrue(str(_replay_package()).endswith(
                    "third_party/kilix-transcript-clean/src/kilix_transcript_clean"))
                self.assertTrue(str(inventory_path()).endswith(
                    "third_party/kilix-tui-utils/src/kilix_rollout/inventory.py"))
                self.assertTrue(callable(_readers()))

    def test_help_starts_without_the_command_model_or_a_checkout(self):
        root = Path(asset.__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="needle-help-") as raw:
            dest = Path(raw) / "usr/share/kilix-needle"
            (dest / "needle_logs").mkdir(parents=True)
            (dest / "tools").mkdir()
            (dest / "corpus-supplement").mkdir()
            bindir = Path(raw) / "usr/bin"
            bindir.mkdir(parents=True)
            for path in root.glob("*.py"):
                shutil.copy2(path, dest / path.name)
            for path in (root / "needle_logs").glob("*.py"):
                shutil.copy2(path, dest / "needle_logs" / path.name)
            for path in (root / "tools").iterdir():
                if path.is_file():
                    shutil.copy2(path, dest / "tools" / path.name)
            shutil.copytree(root / "corpus-supplement", dest / "corpus-supplement", dirs_exist_ok=True)
            for name in ("kilix-needle", "kilix-desktop",
                         "commands-model.json", "commands-lfm3-model.json",
                         "commands-tools.json", "needle3-model.json"):
                shutil.copy2(root / name, dest / name)
            os.chmod(dest / "kilix-needle", 0o755)
            launcher = bindir / "kilix-needle"
            launcher.symlink_to("../share/kilix-needle/kilix-needle")
            self.assertFalse((dest / "router-model").exists())
            self.assertFalse((dest / "third_party").exists())
            ran = subprocess.run(
                [str(launcher), "--help"], check=False, capture_output=True, text=True)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn("usage: kilix-needle", ran.stdout)


if __name__ == "__main__":
    unittest.main()
