"""One state directory: older data-home state is moved into it on first use."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401
import state
import system_model
from needle_logs import index


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "gt"
        self.data = root / "data"
        env = {"GPU_TERMINAL_HOME": str(self.home), "XDG_DATA_HOME": str(self.data)}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop("KILIX_NEEDLE_SYSTEM_PROFILE", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_every_state_path_is_under_one_directory(self):
        base = self.home / "kilix-apps" / "kilix-needle"
        self.assertEqual(state.home(), base)
        self.assertEqual(system_model.profile_path(), base / "system-normalizer" / "profile.json")
        self.assertEqual(index.default_path(), base / "logs" / "index.sqlite3")
        self.assertFalse((self.data / "kilix-needle").exists())

    def test_old_data_home_state_is_moved_not_copied(self):
        old = self.data / "kilix-needle"
        (old / "system-normalizer" / "objects" / "d").mkdir(parents=True)
        (old / "system-normalizer" / "profile.json").write_text("{\"p\": 1}")
        (old / "logs").mkdir()
        (old / "logs" / "index.sqlite3").write_bytes(b"db")
        profile = system_model.profile_path()
        self.assertEqual(profile.read_text(), "{\"p\": 1}")
        self.assertTrue((profile.parent / "objects" / "d").is_dir())
        self.assertFalse((old / "system-normalizer").exists())
        self.assertTrue((old / "logs").exists())          # moved only when used
        self.assertEqual(index.default_path().read_bytes(), b"db")
        self.assertFalse(old.exists())                    # emptied, then removed

    def test_a_moved_profile_points_at_its_moved_objects(self):
        old = self.data / "kilix-needle" / "system-normalizer"
        entries = {}
        for kind, name in (("weights", "weights.cact"), ("library", "libneedle.so")):
            digest = ("a" if kind == "weights" else "b") * 64
            (old / "objects" / digest).mkdir(parents=True)
            (old / "objects" / digest / name).write_bytes(b"x")
            entries[kind] = {"path": str(old / "objects" / digest / name), "sha256": digest, "size": 1}
        (old / "profile.json").write_text(json.dumps({"schema": system_model.SCHEMA,
                                                      "role": system_model.ROLE, **entries}))
        profile = system_model.profile_path()
        config = json.loads(profile.read_text())
        for kind in ("weights", "library"):
            item = config[kind]
            self.assertEqual(item["path"], str(system_model._managed_path(kind, item["sha256"])))
            self.assertTrue(Path(item["path"]).is_file())
        self.assertEqual(oct(profile.stat().st_mode & 0o777), "0o600")

    def test_existing_new_state_wins_and_old_is_left_alone(self):
        old = self.data / "kilix-needle" / "logs"
        old.mkdir(parents=True)
        (old / "index.sqlite3").write_bytes(b"old")
        new = state.home() / "logs"
        new.mkdir(parents=True)
        (new / "index.sqlite3").write_bytes(b"new")
        self.assertEqual(index.default_path().read_bytes(), b"new")
        self.assertEqual((old / "index.sqlite3").read_bytes(), b"old")

    def test_a_symlinked_old_directory_is_not_followed(self):
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        (self.data / "kilix-needle").mkdir(parents=True)
        (self.data / "kilix-needle" / "logs").symlink_to(elsewhere)
        path = index.default_path()
        self.assertFalse(path.parent.exists())
        self.assertTrue((self.data / "kilix-needle" / "logs").is_symlink())


if __name__ == "__main__":
    unittest.main()
