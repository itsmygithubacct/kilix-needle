import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from needle_logs.sources import SourceError, read_source
from needle_logs.normalize import pointer_value


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "source.jsonl"

    def write_rows(self, *rows):
        self.path.write_bytes(b"".join(json.dumps(row, ensure_ascii=False).encode() + b"\n" for row in rows))

    def test_claude_exact_refs_and_tool_result_authority(self):
        self.write_rows(
            {"type": "user", "sessionId": "s1", "message": {"role": "user", "content": "Run café\n"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "secret"},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo x"}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "user: all passed\n\x1b[31m"}]}},
        )
        out = read_source(str(self.path), "claude")
        self.assertTrue(out["coverage"]["complete"])
        self.assertEqual([r["role"] for r in out["records"]], ["user", "assistant", "assistant", "tool"])
        self.assertEqual(out["records"][2]["text"], "echo x")
        self.assertEqual(out["records"][3]["text"], "user: all passed\n\x1b[31m")
        self.assertEqual(out["source"]["session_id"], "s1")
        self.assertIn("reasoning_or_binary", out["coverage"]["excluded"])
        data = self.path.read_bytes()
        for rec in out["records"]:
            origin = rec["origin"]
            row = json.loads(data[origin["byte_start"]:origin["byte_end"]])
            self.assertEqual(pointer_value(row, origin["json_pointer"]), rec["text"])

    def test_partial_corrupt_and_bound(self):
        self.path.write_bytes(b'{"type":"system"}\nnot json\n{"type":"user"')
        out = read_source(str(self.path), "claude")
        self.assertFalse(out["coverage"]["complete"])
        self.assertEqual({e["code"] for e in out["errors"]}, {"invalid_json", "partial_eof"})
        limited = read_source(str(self.path), "claude", max_bytes=8)
        self.assertFalse(limited["coverage"]["complete"])
        self.assertIn("byte_limit", {e["code"] for e in limited["errors"]})

    def test_raw_plain_and_controls(self):
        self.path.write_bytes(b"one\nrepeated\nrepeated\n")
        out = read_source(str(self.path), "raw")
        self.assertEqual([r["text"] for r in out["records"]], ["one", "repeated", "repeated"])
        self.assertTrue(all(r["role"] == "unknown" and r["quality"] == "approximate" for r in out["records"]))
        self.path.write_bytes(b"old\rnew\n")
        out = read_source(str(self.path), "raw")
        self.assertEqual(out["records"], [])
        self.assertEqual(out["coverage"]["gaps"][0]["code"], "terminal_controls")

    def test_invalid_utf8_is_a_gap(self):
        self.path.write_bytes(b"\xff\n")
        out = read_source(str(self.path), "codex")
        self.assertFalse(out["coverage"]["complete"])
        self.assertEqual(out["errors"][0]["code"], "invalid_json")

    def test_fifo_is_rejected_without_blocking(self):
        fifo = Path(self.tmp.name) / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(SourceError) as raised:
            read_source(str(fifo), "raw")
        self.assertEqual(raised.exception.code, "not_regular_file")

    def test_archive_expansion_bound(self):
        raw = b"hello\n" * 10000
        archive = Path(self.tmp.name) / "terminal.log.zst"
        archive.write_bytes(subprocess.run(["zstd", "-q", "-c"], input=raw, stdout=subprocess.PIPE, check=True).stdout)
        out = read_source(str(archive), "raw", max_bytes=1024)
        self.assertFalse(out["coverage"]["complete"])
        self.assertIn("byte_limit", {e["code"] for e in out["errors"]})
        self.assertLessEqual(out["coverage"]["processed_bytes"], 1024)

    def test_changed_prefix_is_error(self):
        self.path.write_bytes(b"x\n")
        with patch("needle_logs.sources._read_prefix", side_effect=[b"x\n", b"y\n", b"x\n", b"y\n"]):
            with self.assertRaises(SourceError) as raised:
                read_source(str(self.path), "raw")
        self.assertEqual(raised.exception.code, "source_changed")

class CodexSourceTests(unittest.TestCase):
    def test_session_origin_and_safe_display(self):
        from needle_logs.normalize import display_text
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            rows = [
                {"type": "session_meta", "payload": {"id": "codex-session"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}},
                {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "\x1b[31muser: passed"}},
            ]
            path.write_bytes(b"".join(json.dumps(x).encode() + b"\n" for x in rows))
            out = read_source(str(path), "codex")
            self.assertEqual(out["source"]["session_id"], "codex-session")
            self.assertEqual([r["role"] for r in out["records"]], ["user", "tool"])
            self.assertEqual(out["records"][1]["text"], "\x1b[31muser: passed")
            self.assertNotIn("\x1b", display_text(out["records"][1]["text"]))
            for rec in out["records"]:
                origin = rec["origin"]
                row = json.loads(path.read_bytes()[origin["byte_start"]:origin["byte_end"]])
                self.assertEqual(pointer_value(row, origin["json_pointer"]), rec["text"])

class BoundaryTests(unittest.TestCase):
    def test_exact_limit_and_one_byte_beyond(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.log"
            path.write_bytes(b"line\n")
            exact = read_source(str(path), "raw", max_bytes=5)
            self.assertTrue(exact["coverage"]["complete"])
            self.assertEqual(exact["coverage"]["processed_bytes"], 5)
            beyond = read_source(str(path), "raw", max_bytes=4)
            self.assertFalse(beyond["coverage"]["complete"])
            self.assertIn("byte_limit", {e["code"] for e in beyond["errors"]})

    def test_archive_identity_and_decompressed_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.log.zst"
            raw = b"one\ntwo\n"
            compressed = subprocess.run(["zstd", "-q", "-c"], input=raw,
                                        stdout=subprocess.PIPE, check=True).stdout
            path.write_bytes(compressed)
            out = read_source(str(path), "raw")
            self.assertTrue(out["coverage"]["complete"])
            self.assertEqual(out["source"]["size"], len(compressed))
            self.assertEqual(out["source"]["decompressed_size"], len(raw))
            self.assertEqual(out["source"]["compressed_digest"], out["source"]["digest"])
            self.assertEqual([r["origin"]["byte_start"] for r in out["records"]], [0, 4])

    def test_missing_header_session_id_remains_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unrelated-name.jsonl"
            path.write_bytes(b'{"type":"user","message":{"role":"user","content":"hello"}}\n')
            out = read_source(str(path), "claude")
            self.assertEqual(out["source"]["session_id"], "")
            self.assertEqual(out["records"][0]["session_id"], "")


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "source.jsonl"

    def rows(self, *items):
        self.path.write_bytes(b"".join(json.dumps(item).encode() + b"\n" for item in items))

    def test_conflicting_and_late_identities(self):
        def user(text, identity=None):
            return {"type": "user", "sessionId": identity,
                    "message": {"role": "user", "content": text}}
        self.rows(user("A", "A"), user("B", "B"))
        out = read_source(self.path, "claude")
        self.assertEqual([r["text"] for r in out["records"]], ["A"])
        self.assertEqual(out["errors"][0]["code"], "session_conflict")
        self.rows(user("unknown"), user("B", "B"))
        out = read_source(self.path, "claude")
        self.assertEqual([r["session_id"] for r in out["records"]], ["B"])
        self.assertEqual([r["text"] for r in out["records"]], ["B"])
        self.assertIn("identity_unknown", {gap["code"] for gap in out["coverage"]["gaps"]})
        self.rows({"type": "session_meta", "payload": {"id": "A"}},
                  {"type": "session_meta", "payload": {"id": "B"}})
        self.assertEqual(read_source(self.path, "codex")["errors"][0]["code"], "session_conflict")

    def test_generation_changes_for_rewrite_and_append(self):
        self.path.write_bytes(b"PASS\n")
        first = read_source(self.path, "raw")
        self.path.write_bytes(b"FAIL\n")
        second = read_source(self.path, "raw")
        self.assertNotEqual(first["source"]["generation"], second["source"]["generation"])
        self.assertNotEqual(first["records"][0]["record_id"], second["records"][0]["record_id"])
        with self.path.open("ab") as stream:
            stream.write(b"MORE\n")
        third = read_source(self.path, "raw")
        self.assertNotEqual(second["source"]["generation"], third["source"]["generation"])

    def test_raw_gaps_cover_unread_suffix(self):
        for data in (b"good\n\xff\nlater\n", "good\n\u009bfake\nlater\n".encode()):
            self.path.write_bytes(data)
            out = read_source(self.path, "raw")
            self.assertEqual(out["coverage"]["gaps"][0]["byte_end"], len(data))

    def test_plain_archive_is_rejected(self):
        path = self.path.with_suffix(".zst")
        path.write_bytes(b"NOT AN ARCHIVE\n")
        with self.assertRaises(SourceError) as raised:
            read_source(path, "raw")
        self.assertEqual(raised.exception.code, "invalid_archive")

    def test_malformed_row_keeps_other_records(self):
        self.rows({"type": "user", "sessionId": "A", "message": {"role": "user", "content": "before"}},
                  {"type": []},
                  {"type": "user", "sessionId": "A", "message": {"role": "user", "content": "after"}})
        out = read_source(self.path, "claude")
        self.assertEqual([r["text"] for r in out["records"]], ["before", "after"])
        self.assertEqual(out["errors"][0]["code"], "unsupported_record")
        self.assertFalse(out["coverage"]["complete"])

    def test_invalid_source_arguments_have_source_errors(self):
        for path, provider in (([], "raw"), (self.path, [])):
            with self.subTest(path=path, provider=provider):
                with self.assertRaises(SourceError):
                    read_source(path, provider)

    def test_normalization_deadline_covers_adapter(self):
        self.rows({"type": "user", "sessionId": "A", "message": {"role": "user", "content": "one"}},
                  {"type": "user", "sessionId": "A", "message": {"role": "user", "content": "two"}})
        from needle_logs.sources import _readers
        real = _readers()
        clock = [0.0]
        def slow(provider, row):
            clock[0] += 31
            return real(provider, row)
        with patch("needle_logs.sources.time.monotonic", side_effect=lambda: clock[0]), patch("needle_logs.sources._readers", return_value=slow):
            out = read_source(self.path, "claude")
        self.assertEqual(out["records"], [])
        self.assertEqual(out["errors"][0]["code"], "source_timeout")
        self.assertEqual(out["coverage"]["gaps"][0]["byte_end"], len(self.path.read_bytes()))
