"""Transport byte limits use owned local helpers, never desktop sessions."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pty_backend


class OutputBounds(unittest.TestCase):
    def helper(self, code, argv, limit=None):
        with tempfile.TemporaryDirectory(prefix="pty-bounds-") as directory:
            path = Path(directory) / "helper with spaces"
            path.write_text("#!/usr/bin/python3\n" + code)
            path.chmod(0o700)
            with patch.object(pty_backend, "kilix_executable", return_value=str(path)):
                if limit is None:
                    return pty_backend.call(argv, wall=2)
                with patch.object(pty_backend, "MAX_OUTPUT", limit):
                    return pty_backend.call(argv, wall=2)

    def test_selector_overflow_stops_before_helper_deadline(self):
        start = time.monotonic()
        reply = self.helper("import os,time\nos.write(1,b'x'*65537)\ntime.sleep(20)\n",
                            ["pty", "resolve", "--selector-json", "{}"])
        self.assertEqual(reply.kind, "failed")
        self.assertIn("64 KiB", reply.error)
        self.assertLess(time.monotonic() - start, 1.5)

    def test_stdout_and_stderr_are_independently_bounded(self):
        for fd in (1, 2):
            with self.subTest(fd=fd):
                reply = self.helper(f"import os,time\nos.write({fd},b'x'*1025)\ntime.sleep(20)\n",
                                    ["pty", "list"], limit=1024)
                self.assertEqual(reply.kind, "failed")
                self.assertIn("4 MiB", reply.error)
                self.assertEqual("stderr" in reply.error, fd == 2)

    def test_exact_limit_valid_json_and_literal_arguments(self):
        args = ["pty", "status", "a b'\";$(false)\\☃😀"]
        payload = json.dumps({"argv": args}, ensure_ascii=False).encode()
        code = "import json,os,sys\nos.write(2,b'notice\\n')\nos.write(1,json.dumps({'argv':sys.argv[1:]},ensure_ascii=False).encode())\n"
        reply = self.helper(code, args, limit=len(payload))
        self.assertEqual(reply.kind, "ok")
        self.assertEqual(reply.document, {"argv": args})

    def test_overflow_keeps_memory_bounded_during_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper"
            path.write_text("#!/usr/bin/python3\nimport os,time\nos.write(1,b'x'*1000000)\ntime.sleep(20)\n")
            path.chmod(0o700)
            processes = []
            original = pty_backend._OwnedProcess
            def record(*args, **kwargs):
                process = original(*args, **kwargs)
                processes.append(process)
                return process
            with patch.object(pty_backend, "kilix_executable", return_value=str(path)), \
                    patch.object(pty_backend, "_OwnedProcess", side_effect=record):
                reply = pty_backend.call(["pty", "resolve", "--selector-json", "{}"], wall=2)
            self.assertEqual(reply.kind, "failed")
            process = processes[0]
            self.assertIsNotNone(process.returncode)
            self.assertLessEqual(len(process._bounded_buffers[process.stdout]), 65536)
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)


if __name__ == "__main__":
    unittest.main()
