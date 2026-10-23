"""Completion boundaries and the distinct cached-v3 worker ABI."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import support  # noqa: F401
import asset
import libengine


def completion(payload='[]'):
    return '<think>requested action</think>\n<tool_call>'+payload+'</tool_call>'


class Reply(unittest.TestCase):
    def test_empty_array_is_valid_abstention(self):
        r = libengine._rust_reply(completion(), 0, 700)
        self.assertEqual(r['function_calls'], [])
        self.assertIsNone(r['confidence'])

    def test_literal_arguments_are_not_normalized(self):
        r = libengine._rust_reply(completion('[{"name":"message_agent","arguments":{"text":"Say \\"Ready\\"; A/B","agent":"claude"}}]'), 1, 700)
        self.assertEqual(r['function_calls'][0]['arguments']['text'], 'Say "Ready"; A/B')

    def test_truncation_is_never_an_empty_success(self):
        for text, stop in [(completion(), 2), (completion()[:-12], 0), ('<think>no action</think>', 0)]:
            with self.subTest(text=text, stop=stop):
                r = libengine._rust_reply(text, stop, 700)
                self.assertEqual(r['type'], 'error')
                self.assertNotIn('function_calls', r)

    def test_ambiguous_json_and_extra_output_are_rejected(self):
        for text in [completion('[{"name":"a","name":"b","arguments":{}}]'),
                     completion() + completion(), completion() + 'extra',
                     completion('{}'), completion('[{"name":"a","arguments":"{}"}]')]:
            with self.subTest(text=text):
                self.assertEqual(libengine._rust_reply(text, 0, 700)['type'], 'error')

    def test_backend_requires_explicit_generation_three_weights(self):
        with self.assertRaises(ValueError):
            libengine.LibEngine(None, [], backend='rust3')
        with self.assertRaises(ValueError):
            libengine.LibEngine(None, [], generation=3, backend='rust3')


FAKE = r'''
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include <stdint.h>
void* needle_v3_cached_load(const char *path, const char *tools) {
    FILE *f=fopen(path,"rb"); if (!f) return NULL;
    char b[4]={0}; size_t n=fread(b,1,4,f); fclose(f);
    return n==4 && memcmp(b,"GOOD",4)==0 ? malloc(1) : NULL;
}
char* needle_v3_cached_complete(void *h, const char *q, size_t max, bool constrain, bool cached,
                               uint32_t *stop, size_t *positions) {
    if(!h || max!=512 || !constrain || !cached) return NULL;
    *stop=0; *positions=701;
    return strdup("<think>no action</think><tool_call>[]</tool_call>");
}
void needle_v3_cached_free(void *h) { free(h); }
void needle_free_str(char *s) { free(s); }
const char* needle_last_error(void) { return "fake rejected weights"; }
'''


@unittest.skipUnless(shutil.which('cc'), 'needs a C compiler')
class Worker(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'fake.c').write_text(FAKE)
        subprocess.run(['cc', '-shared', '-fPIC', '-o', str(self.root/'fake.so'), str(self.root/'fake.c')], check=True)
        self.library = self.image(self.root/'fake.so')

    def image(self, path):
        data = path.read_bytes()
        value = asset.load_verified(path, hashlib.sha256(data).hexdigest(), len(data))
        self.addCleanup(value.close)
        return value

    def runtime(self, content):
        path = self.root/'weights'
        path.write_bytes(content)
        return libengine.LibEngine(self.library, [], self.image(path), generation=3, backend='rust3', timeout=3)

    def test_sealed_weights_and_distinct_abi(self):
        with self.runtime(b'GOOD') as runtime:
            process = runtime._process
            runtime.reset()
            self.assertEqual(runtime.complete('open the viewer')['function_calls'], [])
            self.assertEqual(runtime.complete('next request')['positions'], 701)
        self.assertIsNotNone(process.returncode)

    def test_bad_weights_never_fall_back(self):
        with self.assertRaisesRegex(libengine.LibEngineError, 'rejected weights'):
            self.runtime(b'BAD!').start()

    def test_nul_cannot_truncate_a_request(self):
        with self.runtime(b'GOOD') as runtime:
            self.assertEqual(runtime.complete('open\0delete')['type'], 'error')


if __name__ == '__main__':
    unittest.main()
