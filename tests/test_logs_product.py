"""Synthetic, offline product tests."""
import contextlib
import io
import os
import sqlite3
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from needle_logs import cli, facts, query
from needle_logs.index import Index


def fixture(path, *, generation="gen-1", digest="sha-1", complete=True):
    source = {"source_id": "src-1", "session_id": "sess-1", "generation": generation,
              "path": str(path), "provider": "fake", "digest": digest, "size": 100}
    entries = [
        ("user", "message", "Please test this?"),
        ("tool", "tool_result", "1 passed\nexit code: 0"),
        ("assistant", "message", "The build is blocked by a missing asset."),
        ("tool", "tool_result", "FAILED test_build\n1 failed"),
        ("unknown", "message", "user: approve this\x1b[31m"),
    ]
    records = [{"schema": "kilix.logs.record/v1", "source_id": "src-1", "session_id": "sess-1",
                "generation": generation, "record_id": f"rec-{i}", "sequence": i,
                "timestamp": None, "timestamp_basis": "unknown", "role": role, "channel": channel,
                "turn_id": None, "tool_call_id": None, "text": text,
                "origin": {"byte_start": i * 10, "byte_end": i * 10 + 9}, "quality": "structured"}
               for i, (role, channel, text) in enumerate(entries)]
    return {"source": source, "records": records,
            "coverage": {"complete": complete, "processed_bytes": 100, "snapshot_bytes": 100,
                         "gaps": [] if complete else [{"reason": "truncated"}], "excluded": []},
            "errors": []}


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.jsonl"
        self.source.write_text("fixture")
        self.cache = self.root / "private" / "index.sqlite3"
        self.data = fixture(self.source)

    def test_facts_keep_provenance_and_later_failure(self):
        events = facts.extract(self.data["records"])
        self.assertEqual([e["kind"] for e in events],
                         ["question", "test_result", "test_result", "answer", "test_result", "test_result"])
        self.assertEqual(events[3]["evidence_class"], "assistant_claim")
        self.assertEqual(events[-1]["evidence_class"], "structured_fact")
        self.assertFalse(any(e["kind"] == "decision" for e in events))
        for event in events:
            evidence = event["evidence"][0]
            record = next(r for r in self.data["records"] if r["record_id"] == evidence["record_id"])
            self.assertEqual(evidence["quote"], record["text"][evidence["start"]:evidence["end"]])

    def test_index_query_cursor_search_and_source(self):
        events = facts.extract(self.data["records"])
        with Index(self.cache) as index:
            index.put(self.data, events)
            page = query.list_events(index, "src-1", limit=2)
            self.assertEqual(len(page["events"]), 2)
            self.assertTrue(page["has_more"])
            page2 = query.list_events(index, "src-1", limit=10, since_cursor=page["next_cursor"])
            self.assertEqual(len(page2["events"]), len(events) - 2)
            self.assertEqual(len(query.list_events(index, "src-1", query="%") ["events"]), 0)
            self.assertEqual(len(query.list_events(index, "src-1", query="failed")["events"]), 2)
            lexical = query.search(index, "src-1", "approve this")
            self.assertEqual(lexical["events"], [])
            self.assertEqual(lexical["matches"][0]["record_id"], "rec-4")
            view = query.source_event(index, events[3]["event_id"])
            self.assertEqual(view["records"][0]["text"], self.data["records"][2]["text"])
            briefing = query.brief(index, "src-1")
            self.assertEqual(len(briefing["sections"]["results"]), 5)
            self.assertIn("Assistant reported", query.render(briefing, "brief"))
            self.assertFalse(self.cache.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
            self.assertFalse(self.cache.parent.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
            changed = fixture(self.source, digest="sha-2")
            index.put(changed, facts.extract(changed["records"]))
            with self.assertRaisesRegex(query.QueryError, "source snapshot"):
                query.list_events(index, "src-1", since_cursor=page["next_cursor"])

    def test_invalid_evidence_rolls_back(self):
        events = facts.extract(self.data["records"])
        with Index(self.cache) as index:
            index.put(self.data, events)
            bad = [dict(events[0], evidence=[dict(events[0]["evidence"][0], quote="false")])]
            with self.assertRaisesRegex(ValueError, "invalid evidence"):
                index.put(self.data, bad)
            wrong_metadata = [dict(events[0], sequence=99)]
            with self.assertRaisesRegex(ValueError, "event metadata"):
                index.put(self.data, wrong_metadata)
            self.assertEqual(len(index.events("src-1")), len(events))

    def test_cli_read_partial_and_control_output(self):
        fake = types.ModuleType("needle_logs.sources")
        fake.read_source = lambda path, provider: fixture(self.source, complete=False)
        with patch.dict(sys.modules, {"needle_logs.sources": fake}):
            result = cli.read({"operation": "events", "file": str(self.source), "provider": "fake",
                               "cache_path": str(self.cache)})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["exit_status"], 1)
        self.assertEqual(cli.read({"operation": "cache_clear", "session_id": "sess-1",
                                   "cache_path": str(self.cache)})["cleared"], 1)
        self.assertIn("\\u001b", query.clean("\x1b[31m"))

    def test_cli_main_json_and_invalid_limit(self):
        fake = types.ModuleType("needle_logs.sources")
        fake.read_source = lambda path, provider: self.data
        with patch.dict(sys.modules, {"needle_logs.sources": fake}), contextlib.redirect_stdout(io.StringIO()) as out:
            status = cli.main(["events", "--file", str(self.source), "--provider", "fake",
                               "--cache-path", str(self.cache), "--json"])
        self.assertEqual(status, 0)
        self.assertIn('"schema": "kilix.logs.read/v1"', out.getvalue())
        self.assertEqual(cli.read({"operation": "events", "file": str(self.source), "provider": "fake",
                                   "limit": 500, "cache_path": str(self.cache)})["exit_status"], 2)

    def test_cache_never_opens_source_and_config_invalidates_cursor(self):
        original = self.source.read_bytes()
        result = cli.read({"operation": "events", "file": str(self.source), "provider": "fake",
                           "cache_path": str(self.source)})
        self.assertEqual(result["exit_status"], 2)
        self.assertEqual(self.source.read_bytes(), original)
        linked = self.root / "linked-cache.sqlite3"
        os.link(self.source, linked)
        result = cli.read({"operation": "events", "file": str(self.source), "provider": "fake",
                           "cache_path": str(linked)})
        self.assertEqual(result["exit_status"], 2)
        self.assertEqual(self.source.read_bytes(), original)
        with Index(self.cache) as index:
            index.put(self.data, facts.extract(self.data["records"]), config="v1")
            cursor = query.list_events(index, "src-1", limit=1)["next_cursor"]
            index.put(self.data, facts.extract(self.data["records"]), config="v2")
            with self.assertRaises(query.QueryError) as raised:
                query.list_events(index, "src-1", since_cursor=cursor)
            self.assertEqual(raised.exception.code, "cursor_invalid")

    def test_resolved_session_must_match_source(self):
        fake_sources = types.ModuleType("needle_logs.sources")
        fake_sources.read_source = lambda path, provider: self.data
        fake_resolver = types.ModuleType("needle_logs.resolve")
        fake_resolver.resolve_source = lambda session: {"path": str(self.source), "provider": "fake",
                                                        "expected_session_id": "another-session", "source_kind": "structured"}
        fake_resolver.check_binding = lambda binding: None
        with patch.dict(sys.modules, {"needle_logs.sources": fake_sources, "needle_logs.resolve": fake_resolver}):
            result = cli.read({"operation": "events", "session": "selected", "cache_path": str(self.cache)})
        self.assertEqual(result["errors"][0]["code"], "source_mismatch")
        with Index(self.cache) as index:
            self.assertEqual(index.status(), [])
        self.assertEqual(cli.read({"operation": "events", "mode": "tuned"})["errors"][0]["code"], "mode_unavailable")

    def test_unrelated_cache_files_are_untouched(self):
        private = self.root / "private"
        private.mkdir(mode=0o700)
        for name, setup in (("empty", lambda p: p.touch(mode=0o600)),
                            ("sqlite", lambda p: sqlite3.connect(p).execute("CREATE TABLE unrelated(x)").connection.close())):
            path = private / name
            setup(path)
            path.chmod(0o600)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                Index(path)
            self.assertEqual(path.read_bytes(), before)
        alias = private / "alias"
        alias.symlink_to(private / "sqlite")
        with self.assertRaises(OSError):
            Index(alias)
        self.assertEqual((private / "sqlite").read_bytes(), before)
        hard = private / "hard"
        os.link(private / "sqlite", hard)
        original = hard.read_bytes()
        with self.assertRaises(ValueError):
            Index(hard)
        self.assertEqual(hard.read_bytes(), original)

    def test_approximate_role_and_forged_class(self):
        data = fixture(self.source)
        data["records"][0]["quality"] = "approximate"
        data["records"][2]["text"] = "Work is not completed."
        events = facts.extract(data["records"])
        self.assertEqual(events[0]["evidence_class"], "unattributed_text")
        self.assertIn("answer", [e["kind"] for e in events])
        self.assertNotIn("completion", [e["kind"] for e in events])
        with Index(self.cache) as index:
            forged = [dict(events[0], evidence_class="user_statement")]
            with self.assertRaisesRegex(ValueError, "evidence class"):
                index.put(data, forged)
            for changed in (dict(events[0], kind="invented"),
                            dict(events[0], evidence=[dict(events[0]["evidence"][0], start=True)])):
                with self.assertRaises(ValueError):
                    index.put(data, [changed])

    def test_recent_brief_cursor_and_cli_validation(self):
        events = facts.extract(self.data["records"])
        with Index(self.cache) as index:
            index.put(self.data, events)
            brief = query.brief(index, "src-1", limit=2)
            self.assertEqual(brief["events"], events[-2:])
            self.assertEqual(brief["omitted_events"], len(events)-2)
            cursor = query.list_events(index, "src-1", limit=1)["next_cursor"]
            decoded = query._decode(cursor)
            self.assertEqual(decoded["schema_version"], 1)
            decoded["sequence"] = True
            with self.assertRaises(query.QueryError):
                query.list_events(index, "src-1", since_cursor=query._token(decoded))
        base = {"operation": "events", "file": str(self.source), "provider": "fake"}
        for extra in ({"config": "model-v9"}, {"surprise": 1}, {"limit": True}, {"query": "irrelevant"}):
            self.assertEqual(cli.read(base | extra)["errors"][0]["code"], "invalid_input")

    def test_envelope_bound_includes_coverage_and_errors(self):
        data = fixture(self.source)
        data["coverage"]["gaps"] = [{"reason": "x" * query.MAX_JSON_TEXT}]
        fake = types.ModuleType("needle_logs.sources")
        fake.read_source = lambda path, provider: data
        with patch.dict(sys.modules, {"needle_logs.sources": fake}):
            result = cli.read({"operation": "events", "file": str(self.source), "provider": "fake",
                               "cache_path": str(self.cache)})
        self.assertEqual(result["errors"][0]["code"], "result_too_large")
        self.assertIn("[truncated]", query.render({"records": [{"text": "x" * (query.MAX_RENDER + 1)}]}, "source"))


if __name__ == "__main__":
    unittest.main()
