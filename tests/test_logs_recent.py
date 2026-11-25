"""Recent bounded reads must preserve session identity and replayable origins."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from needle_logs.normalize import pointer_value
from needle_logs.sources import read_source, SourceError


class RecentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "source.jsonl"

    def rows(self, provider):
        if provider == "codex":
            header = {"type": "session_meta", "payload": {"id": "s1"}}
            message = lambda text: {"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "channel": "final",
                "content": [{"type": "output_text", "text": text}]}}
        elif provider == "claude":
            header = {"type": "system", "sessionId": "s1"}
            message = lambda text: {"type": "assistant", "sessionId": "s1", "message": {
                "role": "assistant", "content": [{"type": "text", "text": text}]}}
        elif provider == "grok":
            message = lambda text: {"method": "session/update", "params": {
                "sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk",
                                             "content": {"type": "text", "text": text}}}}
            header = message("beginning")
        else:
            header = {"type": "session", "id": "s1"}
            message = lambda text: {"type": "message", "message": {"role": "assistant", "content": text}}
        rows = [header] + [message("older café " * 20)] * 20 + [message("recent café"), message("newest reply")]
        return [(json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows]

    def check_recent(self, provider, compressed=False):
        rows = self.rows(provider)
        data = b"".join(rows)
        self.path.write_bytes(data)
        path = self.path
        if compressed:
            path = path.with_suffix(".jsonl.zst")
            path.write_bytes(subprocess.run(["zstd", "-q", "-c"], input=data, capture_output=True, check=True).stdout)
        for extra in (0, 2):
            limit = sum(map(len, rows[-2:])) + extra
            out = read_source(str(path), provider, max_bytes=limit)
            self.assertEqual([r["text"] for r in out["records"]], ["recent café", "newest reply"])
            self.assertEqual(out["source"]["session_id"], "s1")
            self.assertTrue(all(r["session_id"] == "s1" for r in out["records"]))
            self.assertEqual([e["code"] for e in out["errors"]], ["byte_limit"])
            coverage = out["coverage"]
            start = len(data) - sum(map(len, rows[-2:]))
            self.assertEqual((coverage["window_start"], coverage["window_end"], coverage["snapshot_bytes"]),
                             (start, len(data), len(data)))
            self.assertEqual((coverage["gaps"][0]["byte_start"], coverage["gaps"][0]["byte_end"]), (0, start))
            self.assertLessEqual(coverage["processed_bytes"], limit)
            for record in out["records"]:
                origin = record["origin"]
                row = json.loads(data[origin["byte_start"]:origin["byte_end"]])
                self.assertEqual(pointer_value(row, origin["json_pointer"]), record["text"])
            if compressed:
                self.assertEqual(out["source"]["decompressed_window_start"], start)
                self.assertEqual(out["source"]["decompressed_size"], len(data))

    def test_recent_window_all_providers(self):
        for provider in ("claude", "codex", "grok", "omp", "qwen"):
            with self.subTest(provider=provider):
                self.check_recent(provider)

    @unittest.skipUnless(shutil.which("zstd"), "zstd required")
    def test_recent_archive_keeps_decompressed_offsets(self):
        self.check_recent("omp", compressed=True)

    @unittest.skipUnless(shutil.which("zstd"), "zstd required")
    def test_archive_expansion_is_bounded(self):
        path = self.path.with_suffix(".zst")
        path.write_bytes(subprocess.run(["zstd", "-q", "-c"], input=b"{}\n" * 10000,
                                        capture_output=True, check=True).stdout)
        with patch("needle_logs.sources.MAX_EXPANDED_BYTES", 1000):
            with self.assertRaises(SourceError) as raised:
                read_source(str(path), "omp", max_bytes=1024)
        self.assertEqual(raised.exception.code, "source_too_large")

    def test_partial_last_row_and_oversized_row_are_explicit(self):
        rows = self.rows("codex")
        self.path.write_bytes(b"".join(rows) + b'{"type":')
        out = read_source(str(self.path), "codex", max_bytes=len(rows[-1]) + 9)
        self.assertEqual([r["text"] for r in out["records"]], ["newest reply"])
        self.assertEqual({e["code"] for e in out["errors"]}, {"byte_limit", "partial_eof"})
        self.path.write_bytes(b"".join(rows))
        out = read_source(str(self.path), "codex", max_bytes=8)
        self.assertEqual(out["records"], [])
        self.assertEqual(out["coverage"]["gaps"][0]["byte_end"], self.path.stat().st_size)

    def test_tail_must_not_override_header_session_identity(self):
        rows = self.rows("claude")
        rows[-1] = rows[-1].replace(b'"s1"', b'"s2"')
        self.path.write_bytes(b"".join(rows))
        # Limit the header to its first complete row, so the conflict is
        # encountered while reading the tail and uses absolute byte offsets.
        with patch("needle_logs.sources.HEADER_BYTES", len(rows[0])):
            out = read_source(str(self.path), "claude", max_bytes=sum(map(len, rows[-2:])))
        self.assertEqual([r["text"] for r in out["records"]], ["recent café"])
        self.assertEqual(out["coverage"]["gaps"][-1]["code"], "session_conflict")
        self.assertEqual(out["coverage"]["gaps"][-1]["byte_end"], self.path.stat().st_size)

    def test_append_invalidates_recent_generation(self):
        rows = self.rows("omp")
        self.path.write_bytes(b"".join(rows))
        first = read_source(str(self.path), "omp", max_bytes=512)
        with self.path.open("ab") as file:
            file.write(rows[-1])
        second = read_source(str(self.path), "omp", max_bytes=512)
        self.assertNotEqual(first["source"]["generation"], second["source"]["generation"])

    def test_rewrite_outside_selected_bytes_invalidates_generation(self):
        rows = self.rows("omp")
        self.path.write_bytes(b"".join(rows))
        with patch("needle_logs.sources.HEADER_BYTES", len(rows[0])):
            first = read_source(str(self.path), "omp", max_bytes=512)
            stamp = self.path.stat().st_mtime_ns
            rows[1] = rows[1].replace(b"older", b"other")
            self.path.write_bytes(b"".join(rows))
            os.utime(self.path, ns=(stamp, stamp + 1_000_000_000))
            second = read_source(str(self.path), "omp", max_bytes=512)
        self.assertEqual(first["source"]["digest"], second["source"]["digest"])
        self.assertNotEqual(first["source"]["generation"], second["source"]["generation"])

    def test_brief_includes_latest_reply_and_keeps_partial_coverage(self):
        from needle_logs.cli import read
        rows = self.rows("codex")
        self.path.write_bytes(b"".join(rows))
        limit = sum(map(len, rows[-2:]))
        with patch("needle_logs.sources.read_source", side_effect=lambda path, provider: read_source(
                path, provider, max_bytes=limit)):
            result = read({"operation": "brief", "file": str(self.path), "provider": "codex",
                           "cache_path": str(self.path.parent / "cache" / "index.sqlite3")})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["exit_status"], 1)
        self.assertIn("newest reply", json.dumps(result["events"]))
        self.assertEqual(result["coverage"]["gaps"][0]["code"], "byte_limit")

