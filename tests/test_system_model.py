"""The system profile is explicit, integrity checked, and independent of jobs."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import support  # noqa: F401
import asset
import system_model


FAKE_C = r'''
#include <string.h>
#include <stdio.h>
int needle_load(const char *b, unsigned long long n) {
    return n >= 4 && memcmp(b, "GOOD", 4) == 0 ? 0 : -1;
}
int needle_init(const char *s, const char *tools, const char *idx) { return 1; }
void needle_reset(void) {}
int needle_complete(const char *in, int max, char *out, int cap) {
    return snprintf(out, cap, "{\"type\":\"call\",\"function_calls\":[],\"echo\":\"%s\"}", in);
}
'''


@unittest.skipUnless(shutil.which("cc"), "needs a C compiler")
class SystemProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kn-system-profile-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = self.root / "profile.json"
        self.env = mock.patch.dict(os.environ, {"KILIX_NEEDLE_SYSTEM_PROFILE": str(self.profile)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("KILIX_NEEDLE_ENGINE", None)
        src = self.root / "fake.c"
        src.write_text(FAKE_C)
        self.library = self.root / "libneedle.so"
        subprocess.run(["cc", "-shared", "-fPIC", "-o", str(self.library), str(src)], check=True)
        data = self.library.read_bytes()
        self.pins = mock.patch.multiple(asset, PINNED_LIB_SHA256=hashlib.sha256(data).hexdigest(),
                                        PINNED_LIB_BYTES=len(data))
        self.pins.start()
        self.addCleanup(self.pins.stop)
        self.weights = self.root / "tuned.cact"
        self.weights.write_bytes(b"GOOD tuned weights")
        self.sha = hashlib.sha256(self.weights.read_bytes()).hexdigest()

    def test_configure_and_runtime_are_explicit_and_lazy(self):
        self.assertEqual(system_model.profile_path(), self.profile)
        self.assertFalse(system_model.status()["available"])
        with self.assertRaisesRegex(system_model.SystemModelError, "missing"):
            system_model.open_runtime()
        config = system_model.configure(self.weights, self.library, self.sha)
        self.assertEqual((config["schema"], config["role"]),
                         (system_model.SCHEMA, "proposal-only"))
        self.assertEqual(config["weights"]["sha256"], self.sha)
        self.assertTrue(system_model.status()["available"])
        runtime = system_model.open_runtime()
        self.assertIsNone(runtime.engine._process)
        with runtime as running:
            self.assertEqual(running.label, f"system-normalizer tuned {self.sha[:12]}")
            self.assertEqual(running.complete("hello")["echo"], "hello")
            running.reset()

    def test_bad_candidate_preserves_current_profile(self):
        system_model.configure(self.weights, self.library, self.sha)
        original = self.profile.read_bytes()
        bad = self.root / "bad.cact"
        bad.write_bytes(b"JUNK tuned weights")
        bad_sha = hashlib.sha256(bad.read_bytes()).hexdigest()
        with self.assertRaisesRegex(Exception, "refusing to fall back"):
            system_model.configure(bad, self.library, bad_sha)
        self.assertEqual(self.profile.read_bytes(), original)
        with self.assertRaisesRegex(system_model.SystemModelError, "does not match"):
            system_model.configure(bad, self.library, self.sha)
        self.assertEqual(self.profile.read_bytes(), original)

    def test_tampered_bytes_or_config_do_not_fall_back(self):
        config = system_model.configure(self.weights, self.library, self.sha)
        Path(config["weights"]["path"]).write_bytes(b"JUNK tuned weights")
        self.assertFalse(system_model.status()["available"])
        with self.assertRaises(asset.AssetError):
            system_model.open_runtime()
        self.profile.write_text(json.dumps({**config, "role": "collector"}))
        with self.assertRaisesRegex(system_model.SystemModelError, "schema or role"):
            system_model.open_runtime()

    def test_engine_override_is_rejected_and_job_selection_is_untouched(self):
        selection = self.root / "selection.json"
        selection.write_text('{"system": "sentinel"}')
        system_model.configure(self.weights, self.library, self.sha)
        self.assertEqual(selection.read_text(), '{"system": "sentinel"}')
        with self.assertRaisesRegex(system_model.SystemModelError, "override"):
            system_model.open_runtime(types.SimpleNamespace(engine="other"))
        with mock.patch.dict(os.environ, {"KILIX_NEEDLE_ENGINE": "other"}):
            with self.assertRaisesRegex(system_model.SystemModelError, "override"):
                system_model.open_runtime()

    def test_status_handles_invalid_override_and_cli_reports_bad_candidate(self):
        with mock.patch.dict(os.environ, {"KILIX_NEEDLE_SYSTEM_PROFILE": "relative.json"}):
            result = system_model.status()
            self.assertFalse(result["available"])
            self.assertIn("absolute", result["error"])
        bad = self.root / "bad.cact"
        bad.write_bytes(b"JUNK")
        with mock.patch("sys.stderr") as stderr:
            code = system_model.main(["configure", "--weights", str(bad),
                                      "--library", str(self.library), "--sha256",
                                      hashlib.sha256(bad.read_bytes()).hexdigest()])
        self.assertEqual(code, 1)
        self.assertTrue(stderr.write.called)


if __name__ == "__main__":
    unittest.main()
