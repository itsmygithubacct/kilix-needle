"""The library worker protocol, against a fake libneedle built at test time."""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

import support  # noqa: F401
import asset
import libengine

FAKE_C = r'''
#include <string.h>
#include <stdio.h>
static int loaded = 0;
int needle_load(const char *b, unsigned long long n) {
    if (n >= 4 && memcmp(b, "GOOD", 4) == 0) { loaded = 1; return 0; }
    return -1;
}
int needle_init(const char *s, const char *tools, const char *idx) { return INIT_RESULT; }
void needle_reset(void) {}
#include <unistd.h>
int needle_complete(const char *in, int max, char *out, int cap) {
#ifdef HANG
    sleep(30);
#endif
    return snprintf(out, cap, "{\"type\":\"call\",\"function_calls\":[],\"echo\":\"%s\",\"tuned\":%d}",
                    in, loaded);
}
'''


@unittest.skipUnless(shutil.which("cc"), "needs a C compiler")
class Worker(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-")
        self.addCleanup(self.dir.cleanup)
        src = os.path.join(self.dir.name, "fake.c")
        lib = os.path.join(self.dir.name, "libfake.so")
        open(src, "w").write(FAKE_C)
        subprocess.run(["cc", "-shared", "-fPIC", "-DINIT_RESULT=1", "-o", lib, src], check=True)
        self.lib = self.image(lib)
        zero = os.path.join(self.dir.name, "libzero.so")
        subprocess.run(["cc", "-shared", "-fPIC", "-DINIT_RESULT=0", "-o", zero, src], check=True)
        self.zero_lib = self.image(zero)

    def image(self, path):
        data = open(path, "rb").read()
        img = asset.load_verified(path, hashlib.sha256(data).hexdigest(), len(data))
        self.addCleanup(img.close)
        return img

    def weights(self, content):
        path = os.path.join(self.dir.name, "w.cact")
        open(path, "wb").write(content)
        return self.image(path)

    def test_a_library_that_never_answers_times_out(self):
        # Review KN-15: the timeout was never used, so this hung forever.
        src = os.path.join(self.dir.name, "fake.c")
        hang = os.path.join(self.dir.name, "libhang.so")
        subprocess.run(["cc", "-shared", "-fPIC", "-DINIT_RESULT=1", "-DHANG", "-o", hang, src],
                       check=True)
        engine = libengine.LibEngine(self.image(hang), [], timeout=1.0)
        engine.start()
        self.addCleanup(engine.close)
        with self.assertRaisesRegex(libengine.LibEngineError, "did not answer"):
            engine.complete("hello")

    def test_base_weights_answer(self):
        with libengine.LibEngine(self.lib, []) as engine:
            reply = engine.complete("split right")
        self.assertEqual((reply["echo"], reply["tuned"]), ("split right", 0))

    def test_tuned_weights_are_used(self):
        with libengine.LibEngine(self.lib, [], self.weights(b"GOOD-weights")) as engine:
            self.assertEqual(engine.complete("x")["tuned"], 1)

    def test_init_must_report_a_positive_result(self):
        # the real library returns 1 with ten tools; 0 is not success
        with self.assertRaisesRegex(libengine.LibEngineError, "needle_init 0"):
            libengine.LibEngine(self.zero_lib, []).start()

    def test_rejected_weights_never_fall_back_to_the_base_model(self):
        with self.assertRaisesRegex(libengine.LibEngineError, "refusing to fall back"):
            libengine.LibEngine(self.lib, [], self.weights(b"JUNK")).start()


if __name__ == "__main__":
    unittest.main()
