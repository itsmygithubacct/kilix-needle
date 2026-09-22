"""The engine client against a recording fake of the upstream server."""
import json
import os
import socket
import stat
import tempfile
import unittest

import support  # noqa: F401
import engine
from engine import Engine, EngineError, check_prompt

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
LOG = {log!r}
MODE = {mode!r}
args = sys.argv[1:]
if MODE == "never-listen":
    import time; time.sleep(30)
with open(LOG + ".start", "w") as out:
    json.dump({{"argv": args, "env": dict(os.environ),
               "tools": json.load(open(args[args.index("--tools") + 1]))}}, out)
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        with open(LOG, "ab") as out:
            out.write(self.path.encode() + b" " + body + b"\n")
        if MODE == "bad-json" and self.path == "/complete":
            payload = b"not json"
        elif self.path == "/complete":
            payload = json.dumps({{"type": "call", "function_calls":
                [{{"name": "open_tab", "arguments": {{}}}}]}}).encode()
        else:
            payload = b"{{}}"
        self.send_response(200); self.send_header("Content-Length", str(len(payload)))
        self.end_headers(); self.wfile.write(payload)
HTTPServer(("127.0.0.1", int(args[args.index("--port") + 1])), H).serve_forever()
'''


class FakeImage:
    def __init__(self, directory, mode="ok"):
        self.log = os.path.join(directory, "requests")
        self.path = os.path.join(directory, "fake-needle")
        with open(self.path, "w") as handle:
            handle.write(FAKE.format(log=self.log, mode=mode))
        os.chmod(self.path, stat.S_IRWXU)
        self.fd = os.open(self.path, os.O_RDONLY)

    def close(self):
        os.close(self.fd)


class EngineClient(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-")
        self.addCleanup(self.dir.cleanup)

    def image(self, mode="ok"):
        image = FakeImage(self.dir.name, mode)
        self.addCleanup(image.close)
        return image

    def test_requests_are_compact_raw_utf8(self):
        image = self.image()
        with Engine(image, [{"name": "t"}]) as needle:
            reply = needle.complete('play "björk" now')
            needle.reset()
        self.assertEqual(reply["function_calls"][0]["name"], "open_tab")
        with open(image.log, "rb") as handle:
            lines = handle.read().splitlines()
        # The upstream parser reads only this exact shape (measured).
        self.assertEqual(lines[0], '/complete {"input":"play \\"björk\\" now"}'.encode())
        self.assertEqual(lines[1], b"/reset {}")

    def test_tools_and_a_scrubbed_environment_reach_the_engine(self):
        os.environ["NEEDLE_DEBUG"] = "1"
        self.addCleanup(os.environ.pop, "NEEDLE_DEBUG", None)
        image = self.image()
        with Engine(image, [{"name": "only"}]) as needle:
            needle.reset()
        with open(image.log + ".start") as handle:
            start = json.load(handle)
        self.assertEqual(start["tools"], [{"name": "only"}])
        self.assertNotIn("NEEDLE_DEBUG", start["env"])
        self.assertNotIn("HOME", start["env"])

    def test_a_port_held_by_another_process_is_never_trusted(self):
        squatter = socket.socket()
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("127.0.0.1", 0))
        squatter.listen()
        self.addCleanup(squatter.close)
        saved = engine._free_port
        engine._free_port = lambda: squatter.getsockname()[1]
        self.addCleanup(setattr, engine, "_free_port", saved)
        with self.assertRaises(EngineError):
            Engine(self.image(), [], startup=3).start()

    def test_a_live_engine_is_not_trusted_with_someone_elses_listener(self):
        # The engine process is alive and a listener answers on its port, but
        # the listener is not the engine's: nothing may be sent to it.
        squatter = socket.socket()
        squatter.bind(("127.0.0.1", 0))
        squatter.listen()
        self.addCleanup(squatter.close)
        saved = engine._free_port
        engine._free_port = lambda: squatter.getsockname()[1]
        self.addCleanup(setattr, engine, "_free_port", saved)
        with self.assertRaisesRegex(EngineError, "did not start listening"):
            Engine(self.image("never-listen"), [], startup=0.5).start()

    def test_an_engine_that_never_listens_times_out(self):
        needle = Engine(self.image("never-listen"), [], startup=0.5)
        with self.assertRaisesRegex(EngineError, "did not start listening"):
            needle.start()

    def test_malformed_reply_is_an_error(self):
        with Engine(self.image("bad-json"), []) as needle:
            with self.assertRaisesRegex(EngineError, "malformed JSON"):
                needle.complete("new tab")

    def test_calls_after_close_are_refused(self):
        needle = Engine(self.image(), [])
        needle.start()
        needle.close()
        with self.assertRaisesRegex(EngineError, "not running"):
            needle.reset()


class Prompts(unittest.TestCase):
    def test_accepted(self):
        self.assertEqual(check_prompt("  split right  "), "split right")
        self.assertEqual(check_prompt('open "björk" tab'), 'open "björk" tab')

    def test_refused(self):
        for text, reason in (("", "empty"), ("   ", "empty"), ("ac\\dc", "backslash"),
                             ("a\x1b[2Jb", "control"), ("a‮b", "control"),
                             ("x" * 401, "longer")):
            with self.assertRaisesRegex(ValueError, reason):
                check_prompt(text)


if __name__ == "__main__":
    unittest.main()
