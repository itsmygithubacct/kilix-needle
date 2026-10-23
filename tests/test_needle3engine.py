"""Generation-3 ABI, withheld-call and process-lifetime regressions."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import support  # noqa: F401
import asset
import engine
import libengine

FAKE = r'''
#include <stdio.h>
#include <string.h>
#include <unistd.h>
static int turn = 0;
int needle_load(const char *data, unsigned long long n) {
    return n >= 4 && memcmp(data, "GOOD", 4) == 0 ? 0 : -1;
}
int needle_init(const char *s, const char *tools, const char *index) { return 1; }
void needle_reset(void) { turn = 0; }
int needle_complete(const char *text, const float *pcm, int samples, int limit,
                    char *out, int cap) {
    if (pcm || samples || limit != 512) return -7;
    if (!strcmp(text, "hang")) sleep(30);
    if (!strcmp(text, "error")) return -1;
    return snprintf(out, cap,
        "{\"function_calls\":[],\"suppressed_calls\":[{\"name\":\"start_claude\",\"arguments\":{}}],"
        "\"confidence\":0.999,\"turn\":%d}", ++turn);
}
'''


@unittest.skipUnless(shutil.which('cc'), 'needs a C compiler')
class Generation3(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kn3-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'fake.c').write_text(FAKE)
        subprocess.run(['cc', '-shared', '-fPIC', '-o', str(self.root/'fake.so'),
                        str(self.root/'fake.c')], check=True)
        (self.root/'weights').write_bytes(b'GOOD-weights')
        self.library = self.image('fake.so')
        self.weights = self.image('weights')

    def image(self, name):
        path = self.root/name
        image = asset.load_verified(path, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
        self.addCleanup(image.close)
        return image

    def runtime(self, timeout=3):
        return libengine.LibEngine(self.library, [], self.weights, generation=3, timeout=timeout)

    def test_six_argument_abi_reset_and_withheld_calls(self):
        with self.runtime() as runtime:
            first = runtime.complete('start claude')
            self.assertEqual(first['turn'], 1)
            self.assertEqual(runtime.complete('next')['turn'], 2)
            runtime.reset()
            self.assertEqual(runtime.complete('unrelated')['turn'], 1)
            self.assertIsNone(first['confidence'])
            self.assertTrue(first['suppressed_calls'])
            self.assertEqual(engine.reply_calls(first), ([], None))

    def test_a_native_failure_never_becomes_an_action(self):
        with self.runtime() as runtime:
            reply = runtime.complete('error')
            calls, error = engine.reply_calls(reply)
            self.assertEqual(calls, [])
            self.assertIsNotNone(error)

    def test_hung_completion_is_killed_reaped_and_closed(self):
        with self.runtime(timeout=.2) as runtime:
            process = runtime._process
            with self.assertRaisesRegex(libengine.LibEngineError, 'did not answer'):
                runtime.complete('hang')
            self.assertIsNotNone(process.returncode)
            self.assertTrue(process.stdin.closed and process.stdout.closed)

    def test_partial_reply_cannot_escape_the_deadline(self):
        worker = self.root/'partial.py'
        worker.write_text('import sys,time\nsys.stdin.readline()\nprint(\'{"ok":true}\',flush=True)\n'
                          'sys.stdin.readline()\nsys.stdout.write(\'{"partial":\');sys.stdout.flush()\ntime.sleep(30)\n')
        with patch.object(libengine, '_WORKER', str(worker)), self.runtime(timeout=.2) as runtime:
            process = runtime._process
            with self.assertRaisesRegex(libengine.LibEngineError, 'did not answer'):
                runtime.complete('anything')
            self.assertIsNotNone(process.returncode)

    def test_generation_and_weights_must_be_explicit(self):
        with self.assertRaises(ValueError):
            libengine.LibEngine(self.library, [], generation=3)
        with self.assertRaises(ValueError):
            libengine.LibEngine(self.library, [], self.weights, generation=4)

    def test_worker_exits_when_parent_dies_inside_native_call(self):
        code = f'''
import hashlib,sys
from pathlib import Path
sys.path.insert(0, {str(Path(libengine.__file__).parent)!r})
import asset,libengine
def load(path):
    p=Path(path)
    return asset.load_verified(p,hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_size)
with load({str(self.root/'fake.so')!r}) as lib, load({str(self.root/'weights')!r}) as weights:
    with libengine.LibEngine(lib,[],weights,generation=3) as runtime:
        print(runtime._process.pid,flush=True)
        runtime.complete('hang')
'''
        parent = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
        try:
            child = int(parent.stdout.readline())
            parent.kill()
            parent.wait(timeout=3)
            exited = False
            for _ in range(100):
                try:
                    exited = Path(f'/proc/{child}/stat').read_text().rsplit(')',1)[1].split()[0] == 'Z'
                except FileNotFoundError:
                    exited = True
                if exited:
                    break
                time.sleep(.02)
            self.assertTrue(exited, 'worker survived its parent')
        finally:
            if parent.poll() is None:
                parent.kill(); parent.wait()
            parent.stdout.close()


if __name__ == '__main__':
    unittest.main()
