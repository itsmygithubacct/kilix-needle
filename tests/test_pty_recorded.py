"""Startup metadata survives the launcher, CLI and both MCP result forms."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pty_cli


def recorded_document():
    rows = []
    variants = (
        {},
        {"recorded": None},
        {"recorded": {"argv": None, "cwd": None, "started_millis": None,
                      "truncated": False}},
        {"recorded": {"argv": ["python3", "-c", "print('build ok')"],
                      "cwd": "/srv/work", "started_millis": 4242,
                      "truncated": False}},
        {"recorded": {"argv": ["python3"], "cwd": None,
                      "started_millis": 4242, "truncated": True}},
        {"recorded": {"argv": [], "cwd": None, "started_millis": None,
                      "truncated": True}},
        {"recorded": {"argv": ["sh", "-c", "$(id)\n\x1b[2J; echo café"],
                      "cwd": "/srv/with\nnewline", "started_millis": 4242,
                      "truncated": False}},
    )
    for index, fields in enumerate(variants, 1):
        rows.append({"id": f"{index:016x}", "reachable": False,
                     "error": "timeout", **fields})
    return {"schema": "kilix.pty/v1", "runtime": "/srv/fixture-runtime",
            "timeout_seconds": 2.0, "sessions": [], "unreachable": rows}


class RecordedReads(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="needle-recorded-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.repo = Path(__file__).resolve().parent.parent
        self.document = recorded_document()
        source = self.root / "document.json"
        source.write_text(json.dumps(self.document))
        self.log = self.root / "calls.jsonl"
        launcher = self.root / "kilix"
        launcher.write_text(f"""#!{sys.executable}
import json, sys
with open({str(self.log)!r}, 'a') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
sys.stdout.write(open({str(source)!r}).read())
""")
        launcher.chmod(0o700)
        patch = mock.patch.dict(os.environ, {"KILIX_NEEDLE_KILIX": str(launcher)})
        patch.start()
        self.addCleanup(patch.stop)

    def assert_calls(self, count):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(calls, [["pty", "list", "--json"]] * count)

    def test_read_and_plain_render_preserve_all_recorded_variants(self):
        record = pty_cli.run("list sessions")
        self.assertEqual(record["status"], 0)
        self.assertEqual(record["result"], self.document)
        rendered = pty_cli.render(record)
        self.assertEqual(json.loads(rendered), self.document)
        self.assertNotIn("\x1b", rendered)
        self.assert_calls(1)

    def test_cli_plain_and_json_output_preserve_all_recorded_variants(self):
        for flags in ([], ["--json"]):
            with self.subTest(flags=flags):
                done = subprocess.run(
                    [sys.executable, "-B", str(self.repo / "needle_cli.py"),
                     "pty", *flags, "list sessions"],
                    capture_output=True, text=True, timeout=10, check=True)
                output = json.loads(done.stdout)
                self.assertEqual(output["result"] if flags else output, self.document)
                self.assertNotIn("\x1b", done.stdout)
        self.assert_calls(2)

    def test_stdio_mcp_text_and_structured_results_preserve_recorded(self):
        messages = [
            {"jsonrpc": "2.0", "id": index, "method": "tools/call",
             "params": {"name": name, "arguments": {"request": request}}}
            for index, (name, request) in enumerate(
                (("kilix_pty_read", "list sessions"),
                 ("kilix_pty_read", {"operation": "list"}),
                 ("kilix_pty_act", "list sessions")), 1)]
        done = subprocess.run(
            [sys.executable, "-B", str(self.repo / "needle_cli.py"),
             "mcp", "--tools", "pty"],
            input="".join(json.dumps(item) + "\n" for item in messages),
            capture_output=True, text=True, timeout=10, check=True)
        answers = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual(len(answers), len(messages))
        for answer in answers:
            result = answer["result"]
            self.assertFalse(result["isError"])
            structured = result["structuredContent"]
            self.assertEqual(structured["result"], self.document)
            self.assertEqual(json.loads(result["content"][0]["text"]), structured)
            self.assertNotIn("request", structured)
        self.assert_calls(3)


if __name__ == "__main__":
    unittest.main()
