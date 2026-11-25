"""Full-history indexing, BM25 relevance, append safety and source citations."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from needle_logs import cli, library_cli, query
from needle_logs.index import Index
from needle_logs.library import Library, inventory_module
from needle_logs.normalize import pointer_value
from needle_logs.sources import SourceError


def message(text, role="assistant"):
    return {"type": "message", "message": {"role": role, "content": text}}


def encode(row):
    return (json.dumps(row, ensure_ascii=False) + "\n").encode()


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache/index.sqlite3"
        self.index = Index(self.cache)
        self.addCleanup(self.index.close)
        self.library = Library(self.index)
        self.path = self.root / "omp/project/session.jsonl"
        self.path.parent.mkdir(parents=True)

    def write(self, *rows, path=None):
        path = path or self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode({"type": "session", "id": path.stem}) + b"".join(encode(row) for row in rows))
        return path

    def refresh(self, path=None, **kwargs):
        item = self.library.inventory.describe(path or self.path, "omp")
        return self.library.refresh(item, **kwargs)

    def test_unchanged_is_cached_and_append_only_parses_new_rows(self):
        self.write(message("original parser request", "user"))
        first = self.refresh()
        event = query.list_events(self.index, first["source"]["source_id"])["events"][0]
        page = query.list_events(self.index, first["source"]["source_id"])
        with mock.patch("needle_logs.library._hash", side_effect=AssertionError("reread")):
            self.assertEqual(self.refresh()["index_action"], "cached")
        with self.path.open("ab") as file:
            file.write(encode(message("parser repaired")))
        with mock.patch.object(self.library, "adapter", wraps=self.library.adapter) as adapter:
            second = self.refresh()
            self.assertEqual(adapter.call_count, 1)
        self.assertEqual((second["index_action"], second["added_records"]), ("appended", 1))
        self.assertEqual(len(self.library.search("parser")["matches"]), 2)
        self.assertEqual(query.source_event(self.index, event["event_id"])["records"][0]["text"], "original parser request")
        with self.assertRaises(query.QueryError) as raised:
            query.list_events(self.index, second["source"]["source_id"], since_cursor=page["next_cursor"])
        self.assertEqual(raised.exception.code, "cursor_invalid")

    def test_rewrite_before_append_is_verified_and_rebuilds(self):
        self.write(message("obsolete token"))
        first = self.refresh()
        data = self.path.read_bytes().replace(b"obsolete", b"replaced") + encode(message("fresh tail"))
        self.path.write_bytes(data)
        second = self.refresh()
        self.assertEqual(second["index_action"], "rebuilt")
        self.assertNotEqual(first["source"]["generation"], second["source"]["generation"])
        self.assertEqual(self.library.search("obsolete")["matches"], [])
        self.assertEqual(len(self.library.search("replaced")["matches"]), 1)
        self.assertEqual(len(self.library.search("fresh")["matches"]), 1)

    def test_truncation_replacement_and_parser_upgrade_invalidate_old_data(self):
        self.write(message("obsolete text"))
        self.refresh()
        self.write(message("new"))
        self.assertEqual(self.refresh()["index_action"], "rebuilt")
        replacement = self.root / "replacement"
        self.write(message("replacement content"), path=replacement)
        replacement.replace(self.path)
        self.assertEqual(self.refresh()["index_action"], "rebuilt")
        self.assertEqual(self.library.search("obsolete")["matches"], [])
        self.library.parser += ":next"
        self.assertEqual(self.refresh()["index_action"], "rebuilt")

    def test_incomplete_last_row_is_retried_without_duplicates(self):
        self.write(message("first complete"))
        with self.path.open("ab") as file:
            file.write(encode(message("pending reply"))[:-1])
        first = self.refresh()
        self.assertEqual([e["code"] for e in first["errors"]], ["partial_eof"])
        self.assertEqual(self.library.search("pending")["matches"], [])
        with self.path.open("ab") as file:
            file.write(b"\n")
        second = self.refresh()
        self.assertTrue(second["coverage"]["complete"])
        self.assertEqual(len(self.library.search("pending")["matches"]), 1)
        self.assertEqual(second["added_records"], 1)

    def test_bad_and_oversized_rows_do_not_hide_later_records(self):
        self.write(message("earlier"))
        with self.path.open("ab") as file:
            file.write(b"not json\n" + encode(message("x" * 1000)) + encode(message("newest")))
        with mock.patch("needle_logs.library.MAX_ROW_BYTES", 256):
            result = self.refresh()
        self.assertEqual({e["code"] for e in result["errors"]}, {"invalid_json", "row_too_large"})
        self.assertEqual(len(self.library.search("newest")["matches"]), 1)
        self.assertEqual(result["coverage"]["processed_bytes"], self.path.stat().st_size)

    def test_changed_source_rolls_back_and_is_not_returned_as_fresh(self):
        self.write(message("original"))
        self.refresh()
        with self.path.open("ab") as file:
            file.write(encode(message("new work")))
        adapter = self.library.adapter
        def rewrite(provider, row):
            self.path.write_bytes(self.path.read_bytes().replace(b"original", b"modified"))
            return adapter(provider, row)
        with mock.patch.object(self.library, "adapter", side_effect=rewrite):
            with self.assertRaises(SourceError) as raised:
                self.refresh()
        self.assertEqual(raised.exception.code, "source_changed")
        self.assertEqual(self.library.search("original")["matches"], [])
        self.assertEqual(self.refresh()["index_action"], "rebuilt")

    def test_append_preserves_capped_gaps_and_retries_oversized_tail_once(self):
        self.write(message("first"))
        with self.path.open("ab") as file:
            file.write(b"invalid\n" * 4 + encode(message("x" * 1000))[:-1])
        with mock.patch("needle_logs.library.MAX_GAPS", 2), mock.patch("needle_logs.library.MAX_ROW_BYTES", 256):
            first = self.refresh()
            self.assertEqual(first["coverage"]["unlisted_gaps"], 2)
            self.assertEqual(first["coverage"]["gaps"][-1]["code"], "partial_eof")
            with self.path.open("ab") as file:
                file.write(b"\n" + encode(message("newest")))
            second = self.refresh()
        self.assertEqual(second["coverage"]["unlisted_gaps"], 3)
        self.assertNotIn("partial_eof", [g["code"] for g in second["coverage"]["gaps"]])
        self.assertEqual(len(self.library.search("newest")["matches"]), 1)

    def test_failed_open_and_parser_upgrade_are_stale_in_inventory(self):
        self.write(message("visible"))
        self.refresh()
        self.assertFalse(self.library.list()["sessions"][0]["stale"])
        self.library.parser += ":next"
        self.assertTrue(self.library.list()["sessions"][0]["stale"])
        self.refresh()
        item = self.library.inventory.describe(self.path, "omp")
        self.path.unlink()
        with self.assertRaises(OSError):
            self.library.refresh(item)
        self.assertTrue(self.library.list()["sessions"][0]["stale"])
        self.assertEqual(self.library.search("visible")["matches"], [])

    def test_large_first_metadata_row_keeps_session_identity(self):
        self.path.write_bytes(encode({"type": "session", "id": "large-header", "data": "x" * (2 * 1024 * 1024)})
                             + encode(message("visible")))
        result = self.refresh()
        self.assertTrue(result["coverage"]["complete"], result)
        self.assertEqual(result["source"]["session_id"], "large-header")
        self.assertEqual(len(self.library.search("visible")["matches"]), 1)

    def test_session_conflict_never_relabels_cached_records(self):
        self.write(message("original"))
        self.refresh()
        with self.path.open("ab") as file:
            file.write(encode({"type": "session", "id": "other-session"}))
        with self.assertRaises(SourceError) as raised:
            self.refresh()
        self.assertEqual(raised.exception.code, "session_conflict")
        self.assertEqual(self.library.search("original")["matches"], [])

    def test_codex_declared_fork_ancestry_preserves_child_identity_and_citations(self):
        from needle_logs.sources import read_source
        meta = lambda identity, **extra: {"type": "session_meta", "payload": {"id": identity, **extra}}
        text = lambda value: {"type": "response_item", "payload": {"type": "message", "role": "user",
                               "content": [{"type": "input_text", "text": value}]}}
        self.path.write_bytes(b"".join(encode(row) for row in (
            meta("child", forked_from_id="parent"), meta("parent", forked_from_id="ancestor"),
            meta("ancestor"), text("inherited history"), text("child request"))))
        preview = read_source(str(self.path), "codex")
        self.assertTrue(preview["coverage"]["complete"], preview)
        self.assertEqual(preview["source"]["session_id"], "child")
        item = self.library.inventory.describe(self.path, "codex")
        result = self.library.refresh(item)
        self.assertTrue(result["coverage"]["complete"], result)
        self.assertEqual(result["source"]["session_id"], "child")
        self.assertEqual(result["coverage"]["inherited_session_ids"], ["ancestor", "parent"])
        for found in self.library.search("history request", match="any")["matches"]:
            self.assertEqual(found["session_id"], "child")
            origin = found["origin"]
            row = json.loads(self.path.read_bytes()[origin["byte_start"]:origin["byte_end"]])
            self.assertEqual(pointer_value(row, origin["json_pointer"]), self.library.record(found["record_id"])["text"])
        with self.path.open("ab") as stream:
            stream.write(encode(meta("unrelated")))
        with self.assertRaises(SourceError):
            self.library.refresh(self.library.inventory.describe(self.path, "codex"))
        self.assertEqual(self.library.search("history")["matches"], [])
        self.path.write_bytes(encode(meta("child", parent_thread_id="parent")) + encode(meta("parent")))
        with self.assertRaises(SourceError):
            self.library.refresh(self.library.inventory.describe(self.path, "codex"))

    def test_bm25_relevance_recency_literal_tokens_and_source_filter(self):
        first = self.write(message("parser bug fix"))
        os.utime(first, (100, 100))
        self.refresh(first)
        second = self.write(message("parser " + "irrelevant " * 100), path=self.root / "omp/project/newer.jsonl")
        os.utime(second, (200, 200))
        self.refresh(second)
        ranked = self.library.search("parser")
        self.assertEqual(ranked["matches"][0]["path"], str(first))
        self.assertGreater(ranked["matches"][0]["score"], ranked["matches"][1]["score"])
        recent = self.library.search("parser", rank="recent")
        self.assertEqual(recent["matches"][0]["path"], str(second))
        self.assertEqual(len(self.library.search("parser bug")["matches"]), 1)
        self.assertEqual(len(self.library.search("parser bug", match="any")["matches"]), 2)
        self.assertEqual(self.library.search('" OR * --')["matches"], [])
        self.assertEqual(self.library.search("parser", provider="codex")["matches"], [])
        selected = self.library.search("parser", source_id=ranked["matches"][0]["source_id"])
        self.assertEqual(len(selected["matches"]), 1)

    def test_discovery_failure_does_not_remove_inventory_and_deleted_files_disappear(self):
        self.write(message("visible"))
        roots = {"omp": [self.root / "omp"]}
        self.library.discover(locations=roots)
        self.refresh()
        with mock.patch.object(self.library.inventory, "discover", return_value={
                "sessions": [], "complete": False, "errors": [{"code": "inventory_incomplete"}]}):
            self.library.discover()
        self.assertEqual(len(self.library.search("visible")["matches"]), 1)
        self.path.unlink()
        self.library.discover(locations=roots)
        self.assertEqual(self.library.search("visible")["matches"], [])

    def test_record_pagination_preserves_unicode_and_json_pointer(self):
        text = "café 🐈 " * 3000
        self.write(message(text))
        self.refresh()
        match = self.library.search("cafe")["matches"][0]
        chunks, start = [], 0
        while True:
            part = library_cli.read({"operation": "show", "record_id": match["record_id"],
                                     "cache_path": str(self.cache), "start": start, "chars": 8000})
            self.assertEqual(part["status"], "ok", part)
            chunks.append(part["text"])
            if part["next_start"] is None:
                break
            start = part["next_start"]
        self.assertEqual("".join(chunks), text)
        origin = part["record"]["origin"]
        row = json.loads(self.path.read_bytes()[origin["byte_start"]:origin["byte_end"]])
        self.assertEqual(pointer_value(row, origin["json_pointer"]), text)

    def test_clear_removes_search_documents_and_inventory(self):
        self.write(message("parser"))
        self.refresh()
        self.index.clear()
        self.assertEqual(self.library.search("parser")["matches"], [])
        self.assertEqual(self.library.list()["total"], 0)
        self.assertEqual(self.index.db.execute("SELECT count(*) FROM log_progress").fetchone()[0], 0)

    def test_long_unbroken_token_does_not_hide_a_search_page(self):
        self.write(message("parser " + "z" * 100000))
        self.refresh()
        result = library_cli.read({"operation": "search", "query": "parser", "cached": True,
                                   "limit": 1, "cache_path": str(self.cache)})
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(len(result["matches"]), 1)
        self.assertLess(len(result["matches"][0]["snippet"]), 1100)
        self.assertIsNone(result["next_offset"])

    def test_cli_global_inventory_search_and_cached_query(self):
        self.write(message("parser issue"))
        inventory = inventory_module()
        inventory.roots = lambda: {"omp": [self.root / "omp"]}
        with mock.patch("needle_logs.library.inventory_module", return_value=inventory):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = cli.main(["search", "--query", "parser", "--cache-path", str(self.cache), "--json"])
            self.assertEqual(status, 0, out.getvalue())
            result = json.loads(out.getvalue())
            self.assertEqual(len(result["matches"]), 1)
            self.assertEqual(result["rank"], "bm25")
            with mock.patch.object(inventory, "discover", side_effect=AssertionError("discovery")), \
                    mock.patch("needle_logs.library._hash", side_effect=AssertionError("read")):
                result = library_cli.read({"operation": "search", "query": "parser", "cached": True,
                                           "cache_path": str(self.cache)})
                self.assertEqual(result["status"], "ok", result)
            listing = library_cli.read({"operation": "list", "cache_path": str(self.cache)})
            self.assertEqual(listing["sessions"][0]["title"], "omp · session")
            self.assertTrue(listing["sessions"][0]["indexed"])
            inventory_id = listing["sessions"][0]["inventory_id"]
            with mock.patch("needle_logs.resolve.snapshot", side_effect=AssertionError("pane listing")):
                selected = cli.read({"operation": "brief", "session": inventory_id, "cache_path": str(self.cache)})
            self.assertEqual(selected["status"], "ok", selected)
            self.assertIn("parser issue", json.dumps(selected["events"]))

    def _large_source(self):
        self.write(message("ancient evidence"))
        with self.path.open("ab") as file:
            excluded = encode({"type": "custom", "data": "x" * 1024 * 1024})
            for _ in range(34):
                file.write(excluded)
            file.write(encode(message("newest evidence")))

    def test_entire_history_beyond_32_mib_is_searchable_and_pageable(self):
        self._large_source()
        result = cli.read({"operation": "events", "file": str(self.path), "provider": "omp",
                           "cache_path": str(self.cache), "limit": 1})
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["coverage"]["window"], "full")
        self.assertIn("ancient evidence", json.dumps(result["events"]))
        next_page = cli.read({"operation": "events", "file": str(self.path), "provider": "omp",
                             "cache_path": str(self.cache), "limit": 1, "since_cursor": result["next_cursor"]})
        self.assertEqual(next_page["status"], "ok", next_page)
        self.assertIn("newest evidence", json.dumps(next_page["events"]))
        self.assertEqual(len(self.library.search("ancient")["matches"]), 1)
        self.assertEqual(len(self.library.search("newest")["matches"]), 1)

    @unittest.skipUnless(shutil.which("zstd"), "zstd required")
    def test_compressed_full_history_preserves_decompressed_origins(self):
        self._large_source()
        archive = self.path.with_suffix(".jsonl.zst")
        with self.path.open("rb") as source, archive.open("wb") as target:
            subprocess.run(["zstd", "-qc"], stdin=source, stdout=target, check=True)
        result = self.refresh(archive)
        self.assertTrue(result["coverage"]["complete"], result)
        self.assertEqual(result["coverage"]["snapshot_bytes"], self.path.stat().st_size)
        match = self.library.search("newest")["matches"][0]
        origin = match["origin"]
        with self.path.open("rb") as source:
            source.seek(origin["byte_start"])
            row = json.loads(source.read(origin["byte_end"] - origin["byte_start"]))
        self.assertEqual(pointer_value(row, origin["json_pointer"]), "newest evidence")

