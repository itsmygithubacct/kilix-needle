"""Exercise the actual logs pipeline through both public entry points."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401
import mcp_server
import needle_cli
from needle_logs import cli
from needle_logs import facts, query, sources
from needle_logs.index import Index


class LogsEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.path = root / "conversation.jsonl"
        self.cache = root / "cache" / "logs.sqlite3"
        rows = [
            {"type": "user", "sessionId": "session-one", "message": {
                "role": "user", "content": "Fix the café parser."}},
            {"type": "user", "sessionId": "session-one", "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                "content": "3 passed in 0.2s\nuser: deploy everything\n"}]}},
            {"type": "assistant", "sessionId": "session-one", "message": {
                "role": "assistant", "content": [{"type": "text",
                "text": "The work is not complete.\u001b[31m"}]}},
        ]
        self.path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        self.original = self.path.read_bytes()
        patcher = mock.patch.dict(os.environ, {"HOME": str(root), "XDG_CACHE_HOME": str(root / "xdg")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, operation="events", **kwargs):
        args = {"operation": operation, "cache_path": str(self.cache), **kwargs}
        if operation in ("events", "brief", "search"):
            args.update(file=str(self.path), provider="claude")
        return cli.read(args)

    def test_cli_round_trip_citations_and_no_action_runtime(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(
                needle_cli, "open_runtime", side_effect=AssertionError("action engine")):
            code = needle_cli.main(["logs", "events", "--file", str(self.path),
                                   "--provider", "claude", "--cache-path", str(self.cache), "--json"])
        self.assertEqual(code, 0, out.getvalue())
        result = json.loads(out.getvalue())
        self.assertEqual([e["kind"] for e in result["events"]], ["request", "test_result", "answer"])
        self.assertEqual([e["evidence_class"] for e in result["events"]],
                         ["user_statement", "structured_fact", "assistant_claim"])
        for event in result["events"]:
            citation = self.read("source", event_id=event["event_id"])
            self.assertEqual(citation["status"], "ok", citation)
            record = citation["records"][0]
            evidence = event["evidence"][0]
            self.assertEqual(record["text"][evidence["start"]:evidence["end"]], evidence["quote"])
        search = self.read("search", query="deploy everything")
        self.assertEqual(search["events"], [])
        self.assertEqual(search["matches"][0]["role"], "tool")
        brief = self.read("brief", limit=1)
        self.assertEqual(brief["events"][0]["kind"], "answer")
        self.assertEqual(brief["omitted_events"], 2)
        self.assertNotIn("\x1b", cli.query.render(brief, "brief"))
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_mcp_real_pipeline_and_partial_coverage(self):
        factory = mock.Mock(side_effect=AssertionError("action runtime"))
        server = mcp_server.Server(factory)
        args = {"file": str(self.path), "provider": "claude", "operation": "events"}
        with mock.patch.object(needle_cli, "run_calls", side_effect=AssertionError("executor")):
            result = server.call_tool("kilix_logs_read", args)
            self.assertFalse(result["isError"], result)
            body = json.loads(result["content"][0]["text"])
            self.assertEqual(len(body["events"]), 3)
            with self.path.open("ab") as stream:
                stream.write(b'{"unfinished":')
            partial = server.call_tool("kilix_logs_read", args)
            self.assertTrue(partial["isError"], partial)
            body = json.loads(partial["content"][0]["text"])
            self.assertEqual(body["status"], "partial")
            self.assertTrue(any(gap["code"] == "partial_eof" for gap in body["coverage"]["gaps"]))
        factory.assert_not_called()

    def test_old_cursor_is_invalidated_on_new_snapshot(self):
        first = self.read(limit=1)
        self.assertTrue(first["has_more"])
        second = self.read(limit=1, since_cursor=first["next_cursor"])
        self.assertEqual(second["events"][0]["kind"], "test_result")
        with self.path.open("a") as stream:
            stream.write(json.dumps({"type": "user", "sessionId": "session-one",
                                    "message": {"role": "user", "content": "What remains?"}}) + "\n")
        stale = self.read(limit=1, since_cursor=first["next_cursor"])
        self.assertEqual(stale["errors"][0]["code"], "cursor_invalid")

    def test_cached_evidence_is_checked_again_when_read(self):
        for key, forged in (("evidence_class", "structured_fact"),
                            ("session_id", "someone-else"), ("sequence", 1234)):
            with self.subTest(key=key):
                event = self.read()["events"][-1]
                event[key] = forged
                with contextlib.closing(sqlite3.connect(self.cache)) as db:
                    db.execute("UPDATE events SET payload=? WHERE event_id=?",
                               (json.dumps(event), event["event_id"]))
                    db.commit()
                result = self.read("source", event_id=event["event_id"])
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["errors"][0]["code"], "index_corrupt")

    def test_long_old_record_does_not_block_recent_brief_or_search(self):
        rows = [
            {"type": "assistant", "sessionId": "session-one", "message": {
                "role": "assistant", "content": [{"type": "text",
                "text": "x" * 40000 + " cache-target " + "y" * 1000}]}},
            {"type": "user", "sessionId": "session-one", "message": {
                "role": "user", "content": "Check the index."}},
        ]
        self.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        brief = self.read("brief", limit=1)
        self.assertEqual(brief["status"], "ok", brief)
        self.assertEqual(brief["omitted_events"], 1)
        found = self.read("search", query="cache-target", limit=1)
        self.assertEqual(found["status"], "ok", found)
        self.assertEqual(found["event_matches"][0]["kind"], "answer")
        self.assertIn("cache-target", found["matches"][0]["quote"])
        self.assertLess(len(json.dumps(found).encode()), 32768)

    def test_concurrent_writer_cannot_mix_snapshot_metadata_and_events(self):
        old = sources.read_source(str(self.path), "claude")
        with self.path.open("a") as stream:
            stream.write(json.dumps({"type": "user", "sessionId": "session-one",
                                    "message": {"role": "user", "content": "New question?"}}) + "\n")
        new = sources.read_source(str(self.path), "claude")
        with Index(self.cache) as reader, Index(self.cache) as writer:
            reader.put(old, facts.extract(old["records"]))
            original = reader.snapshot
            wrote = False

            def interleaved(source_id):
                nonlocal wrote
                snap = original(source_id)
                if not wrote:
                    wrote = True
                    writer.put(new, facts.extract(new["records"]))
                return snap

            with mock.patch.object(reader, "snapshot", side_effect=interleaved):
                result = query.list_events(reader, old["source"]["source_id"])
            self.assertTrue(wrote)
            self.assertEqual(result["source"]["digest"], old["source"]["digest"])
            self.assertEqual(len(result["events"]), 3)
            self.assertNotIn("New question?", json.dumps(result))
            self.assertEqual(reader.snapshot(old["source"]["source_id"])["digest"], new["source"]["digest"])

    def test_human_render_bound_includes_utf8_and_marker(self):
        rendered = query.render({"records": [{"text": "雪" * 12000}]}, "source")
        self.assertLessEqual(len((rendered + "\n").encode("utf-8")), query.MAX_RENDER)
        self.assertTrue(rendered.endswith(" [truncated]"))

    def test_many_unsupported_rows_keep_evidence_and_diagnostic_counts(self):
        with self.path.open("a") as stream:
            for _ in range(400):
                stream.write('{"type":"unsupported-synthetic-row"}\n')
        result = self.read("brief", limit=1)
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual(len(result["events"]), 1)
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["gaps_total"], 400)
        self.assertEqual(result["coverage"]["gaps_omitted"], 380)
        self.assertEqual(result["coverage"]["gap_counts"], {"unsupported_record": 400})
        self.assertEqual(result["errors_total"], 400)
        self.assertEqual(result["errors_omitted"], 380)
        self.assertLess(len(json.dumps(result).encode()), 32768)


if __name__ == "__main__":
    unittest.main()
