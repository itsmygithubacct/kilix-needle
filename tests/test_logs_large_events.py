"""One message too large for any response still pages, briefs and cites.

A recorded user or assistant message can exceed the 32 KiB response bound on
its own. Events and briefs then return it abridged: its exact span, no quote,
and an exact excerpt. Source returns every cited record in exact chunks behind
a cursor bound to the snapshot. These run the real readers, index and both
public entry points; every response is measured as it is printed.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401
import mcp_server
from needle_logs import cli, facts, query
from needle_logs.index import Index
from needle_logs.normalize import pointer_value

SESSION = "00000000-0000-4000-8000-00000000000a"
# Every JSON cost class: plain ASCII, an escaped quote and backslash, two-byte
# escapes, six-byte \u00XX escapes, two-, three- and four-byte UTF-8, and a
# separator JSON leaves raw. A cut in the wrong unit would show up here.
UNIT = 'ab"c\\d\n\te\x01\x1b\x7fé雪🐈  '
LARGE = "large-marker " + (UNIT * 3000)


def user(text):
    return {"type": "user", "sessionId": SESSION, "message": {"role": "user", "content": text}}


def assistant(text):
    return {"type": "assistant", "sessionId": SESSION,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def printed(result):
    """Bytes `logs ... --json` prints, before its newline: the bound applies here."""
    return len(json.dumps(result, ensure_ascii=False).encode("utf-8"))


class LargeEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "conversation.jsonl"
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.root), "XDG_CACHE_HOME": str(self.root / "xdg")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertGreater(len(LARGE.encode("utf-8")), 2 * query.MAX_JSON_TEXT)

    def write(self, rows):
        self.path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))

    def cache(self, indexed):
        return self.root / ("library" if indexed else "preview") / "index.sqlite3"

    def read(self, operation, *, indexed=False, **kwargs):
        args = {"operation": operation, "cache_path": str(self.cache(indexed)), **kwargs}
        if operation in ("events", "brief"):
            args.update(file=str(self.path), provider="claude", indexed=indexed)
        result = cli.read(args)
        self.assertLessEqual(printed(result), query.MAX_JSON_TEXT, result.get("errors"))
        return result

    def page_all(self, *, indexed=False, limit=1):
        pages, cursor = [], None
        while True:
            extra = {"since_cursor": cursor} if cursor else {}
            page = self.read("events", indexed=indexed, limit=limit, **extra)
            self.assertEqual(page["status"], "ok", page)
            self.assertTrue(page["events"], "every page must advance")
            pages.append(page)
            if not page["has_more"]:
                self.assertIsNotNone(page["next_cursor"])
                return pages
            self.assertNotEqual(page["next_cursor"], cursor)
            cursor = page["next_cursor"]

    def source_all(self, event_id, *, indexed=False):
        """Rebuild each cited record from bounded continuation responses."""
        texts, responses, cursor = {}, [], None
        while True:
            extra = {"since_cursor": cursor} if cursor else {}
            result = self.read("source", indexed=indexed, event_id=event_id, **extra)
            self.assertEqual(result["status"], "ok", result)
            responses.append(result)
            if "has_more" not in result:            # whole records: the unchanged form
                self.assertIsNone(cursor)
                return {i: r["text"] for i, r in enumerate(result["records"])}, responses
            self.assertTrue(result["records"], "every continuation must carry text")
            for entry in result["records"]:
                self.assertNotIn("text", entry)     # a chunk never poses as a whole record
                index = entry["evidence_index"]
                self.assertEqual(entry["chunk_start"], len(texts.get(index, "")), "chunks are contiguous")
                self.assertEqual(entry["chunk_end"] - entry["chunk_start"], len(entry["chunk"]))
                self.assertGreater(len(entry["chunk"]), 0)
                texts[index] = texts.get(index, "") + entry["chunk"]
                self.assertLessEqual(len(texts[index]), entry["total_chars"])
            if not result["has_more"]:
                self.assertIsNone(result["next_cursor"])
                return texts, responses
            cursor = result["next_cursor"]

    def stored_events(self, source_id, indexed):
        with Index(self.cache(indexed)) as index:
            return index.events(source_id, limit=1000)

    def file_text(self, record):
        origin = record["origin"]
        row = json.loads(self.path.read_bytes()[origin["byte_start"]:origin["byte_end"]])
        return pointer_value(row, origin["json_pointer"])

    def check_abridged(self, item, text, start=0):
        self.assertNotIn("quote", item)
        self.assertIs(item["quote_omitted"], True)
        self.assertEqual((item["start"], item["end"]), (start, start + len(text)))
        self.assertEqual(item["excerpt"], text[:query.EXCERPT_CHARS])
        self.assertEqual(item["excerpt_end"], start + query.EXCERPT_CHARS)

    def test_large_first_middle_or_last_event_pages_one_at_a_time(self):
        small = [user("first small request?"), assistant("first small answer"),
                 user("second small request"), assistant("second small answer")]
        for position in (0, 2, 4):
            for indexed in (False, True):        # MCP's preview path and the CLI's library
                with self.subTest(position=position, indexed=indexed):
                    rows = list(small)
                    rows.insert(position, user(LARGE))
                    self.write(rows)
                    pages = self.page_all(indexed=indexed)
                    events = [page["events"][0] for page in pages]
                    stored = self.stored_events(pages[0]["source"]["source_id"], indexed)
                    self.assertEqual(len(events), len(rows))
                    self.assertEqual([e["event_id"] for e in events], [e["event_id"] for e in stored])
                    self.assertEqual([e["sequence"] for e in events], sorted({e["sequence"] for e in events}))
                    for number, (shown, original) in enumerate(zip(events, stored)):
                        if number == position:
                            # Identity, order and offsets are the stored event's;
                            # only the long quote is replaced by an exact excerpt.
                            self.assertEqual({k: v for k, v in shown.items() if k != "evidence"},
                                             {k: v for k, v in original.items() if k != "evidence"})
                            self.check_abridged(shown["evidence"][0], LARGE)
                            self.assertEqual(pages[number]["events_abridged"], 1)
                        else:
                            self.assertEqual(shown, original)
                            self.assertNotIn("events_abridged", pages[number])

    def test_a_page_keeps_small_neighbours_whole_around_an_abridged_event(self):
        self.write([user("before?"), assistant(LARGE), user("after")])
        page = self.read("events", limit=20)
        self.assertEqual(page["status"], "ok", page)
        self.assertEqual([e["kind"] for e in page["events"]], ["question", "answer", "request"])
        self.assertFalse(page["has_more"])
        self.assertNotIn("events_trimmed_to_fit", page)
        self.assertEqual(page["events_abridged"], 1)
        self.check_abridged(page["events"][1]["evidence"][0], LARGE)
        self.assertEqual([e["evidence"][0]["quote"] for e in (page["events"][0], page["events"][2])],
                         ["before?", "after"])
        rendered = query.render(page, "events")
        self.assertIn("answer [", rendered)
        self.assertIn("[truncated]", rendered)

    def test_brief_shows_a_large_latest_event_and_the_events_before_it(self):
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                self.write([user("what changed?"), assistant("the parser"), user(LARGE)])
                one = self.read("brief", indexed=indexed, limit=1)
                self.assertEqual(one["status"], "ok", one)
                self.assertEqual((len(one["events"]), one["omitted_events"], one["has_more"]), (1, 2, True))
                self.check_abridged(one["events"][0]["evidence"][0], LARGE)
                section = one["sections"]["requests"][0]
                self.assertEqual(section["excerpt"], query.clean(LARGE)[:800])
                self.assertIs(section["excerpt_truncated"], True)
                every = self.read("brief", indexed=indexed, limit=20)
                self.assertEqual((len(every["events"]), every["omitted_events"]), (3, 0))
                self.assertEqual(every["events_abridged"], 1)
                self.assertEqual([e["evidence"][0].get("quote") for e in every["events"]],
                                 ["what changed?", "the parser", None])
                # Second-newest: still abridged, not a reason to drop older events.
                self.write([user("what changed?"), user(LARGE), assistant("done")])
                every = self.read("brief", indexed=indexed, limit=20)
                self.assertEqual((len(every["events"]), every["omitted_events"]), (3, 0))
                self.check_abridged(every["events"][1]["evidence"][0], LARGE)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["brief", "--file", str(self.path), "--provider", "claude",
                             "--cache-path", str(self.cache(True))])
        self.assertEqual(code, 0)
        self.assertIn("[truncated]", out.getvalue())
        self.assertLessEqual(len(out.getvalue().encode("utf-8")), query.MAX_RENDER)

    def test_source_chunks_rebuild_the_exact_cited_record(self):
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                self.write([user("short?"), assistant(LARGE), user("thanks")])
                events = [page["events"][0] for page in self.page_all(indexed=indexed)]
                large = events[1]
                texts, responses = self.source_all(large["event_id"], indexed=indexed)
                self.assertGreater(len(responses), 1)
                self.assertEqual(texts, {0: LARGE})
                record = responses[-1]["records"][-1]
                self.assertEqual(self.file_text(record), LARGE)     # the bytes on disk
                item = large["evidence"][0]
                self.assertEqual(texts[0][item["start"]:item["end"]], LARGE)
                for result in responses:
                    # Source pages may omit the excerpt to make room for text;
                    # identity and the exact cited span must remain unchanged.
                    reference = result["event"]["evidence"][0]
                    self.assertEqual(result["event"], dict(large, evidence=[reference]))
                    self.assertEqual({k: v for k, v in reference.items() if k not in ("excerpt", "excerpt_end")},
                                     {k: v for k, v in item.items() if k not in ("excerpt", "excerpt_end")})
                    self.assertEqual(reference["excerpt"], LARGE[reference["start"]:reference["excerpt_end"]])
                    self.assertEqual({(e["record_id"], e["total_chars"]) for e in result["records"]},
                                     {(item["record_id"], len(LARGE))})
                # A small event keeps the unchanged, whole-record response.
                whole = self.read("source", indexed=indexed, event_id=events[0]["event_id"])
                self.assertEqual(set(whole), {"schema", "status", "exit_status", "event", "records",
                                              "source", "coverage", "errors", "operation"})
                self.assertEqual(whole["records"][0]["text"], "short?")
                self.assertEqual(whole["event"], events[0])

    def test_human_source_output_continues_without_hiding_text(self):
        self.write([assistant(LARGE)])
        event = self.read("events", indexed=True)["events"][0]
        args = ["source", "--event", event["event_id"], "--cache-path", str(self.cache(True))]
        shown, cursor, calls = "", None, 0
        while True:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(args + (["--since-cursor", cursor] if cursor else []))
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertLessEqual(len(text.encode("utf-8")), query.MAX_RENDER)
            self.assertNotIn(" [truncated]", text)
            calls += 1
            body, marker, cursor = text.rstrip("\n").rpartition("\nContinue with --since-cursor ")
            if not marker:
                shown += text[:-1]
                break
            shown += body
        self.assertGreater(calls, 2)
        self.assertEqual(shown, query.clean(LARGE))
        # Cursors name positions, not a presentation: JSON continues a printed one.
        first = self.read("source", indexed=True, event_id=event["event_id"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(args + ["--since-cursor", first["next_cursor"]])
        offset = first["records"][-1]["chunk_end"]
        self.assertTrue(query.clean(LARGE[offset:]).startswith(out.getvalue().split("\nContinue with")[0]))

    def test_stale_foreign_and_malformed_cursors_are_refused(self):
        self.write([user("first?"), assistant(LARGE)])
        page = self.read("events", indexed=True, limit=1)
        large = self.read("events", indexed=True, limit=1, since_cursor=page["next_cursor"])["events"][0]
        first = self.read("source", indexed=True, event_id=large["event_id"])
        cursor = first["next_cursor"]
        payload = query._decode(cursor)
        self.assertEqual(payload["op"], "source")

        def refused(result, code="cursor_invalid"):
            self.assertEqual((result["status"], result["exit_status"], result["errors"][0]["code"]),
                             ("error", 2, code), result)

        forged = [dict(payload, evidence=1), dict(payload, evidence=-1), dict(payload, evidence=True),
                  dict(payload, offset=-1), dict(payload, offset=len(LARGE)), dict(payload, offset=1.5),
                  dict(payload, offset="7"), dict(payload, offset=None), dict(payload, snapshot="0" * 32),
                  dict(payload, op="events"), {k: v for k, v in payload.items() if k != "op"}]
        for value in [query._token(item) for item in forged] + [
                "x", "abc.def", cursor[:-1] + ("0" if cursor[-1] != "0" else "1"), page["next_cursor"]]:
            with self.subTest(cursor=value[:40]):
                refused(self.read("source", indexed=True, event_id=large["event_id"], since_cursor=value))
        refused(self.read("source", indexed=True, event_id=page["events"][0]["event_id"], since_cursor=cursor))
        refused(self.read("events", indexed=True, since_cursor=cursor))
        for value in ("", "x" * 4097):
            refused(self.read("source", indexed=True, event_id=large["event_id"], since_cursor=value),
                    "invalid_input")
        # An append changes the snapshot: the old position is not trusted,
        # even though the same event (same generation) can be read afresh.
        with self.path.open("a") as stream:
            stream.write(json.dumps(user("appended")) + "\n")
        self.assertEqual(len(self.page_all(indexed=True)), 3)
        refused(self.read("source", indexed=True, event_id=large["event_id"], since_cursor=cursor))
        texts, _ = self.source_all(large["event_id"], indexed=True)
        self.assertEqual(texts, {0: LARGE})

    def test_worst_case_escaping_and_widest_utf8_stay_bounded(self):
        for text in ("\x01" * 30000, "🐈" * 30000, '"\\' * 15000):
            with self.subTest(text=text[:2]):
                self.write([user(text)])
                pages = self.page_all()
                self.check_abridged(pages[0]["events"][0]["evidence"][0], text)
                texts, responses = self.source_all(pages[0]["events"][0]["event_id"])
                self.assertEqual(texts, {0: text})
                # Chunks are as large as the bound allows, not a fixed count:
                # all but the last fill the response to within one character.
                for result in responses[:-1]:
                    self.assertGreater(printed(result), query.MAX_JSON_TEXT - 300)

    def test_invalid_limits_are_refused_before_any_read(self):
        self.write([user(LARGE)])
        for limit in (0, -1, 101, True, 1.5, "1"):
            for operation in ("events", "brief"):
                with self.subTest(limit=limit, operation=operation):
                    result = self.read(operation, limit=limit)
                    self.assertEqual((result["exit_status"], result["errors"][0]["code"]), (2, "invalid_input"))
        self.assertFalse(self.cache(False).exists())

    def test_mcp_pages_a_large_event_and_continues_its_source(self):
        self.write([user("please summarize"), user(LARGE)])
        server = mcp_server.Server(mock.Mock(side_effect=AssertionError("action runtime")))
        spec = next(t for t in mcp_server.TOOL_LIST if t["name"] == "kilix_logs_read")
        self.assertIn("since_cursor=next_cursor", spec["description"])

        def call(**arguments):
            reply = server.call_tool("kilix_logs_read", arguments)
            self.assertFalse(reply["isError"], reply)
            text = reply["content"][0]["text"]
            self.assertLessEqual(len(text.encode("utf-8")), query.MAX_JSON_TEXT)
            self.assertEqual(json.loads(text), reply["structuredContent"])
            return reply["structuredContent"]

        base = {"file": str(self.path), "provider": "claude"}
        first = call(operation="events", limit=1, **base)
        second = call(operation="events", limit=1, since_cursor=first["next_cursor"], **base)
        self.assertFalse(second["has_more"])
        event = second["events"][0]
        self.check_abridged(event["evidence"][0], LARGE)
        rebuilt, cursor = "", None
        while True:
            reply = call(operation="source", event_id=event["event_id"],
                         **({"since_cursor": cursor} if cursor else {}))
            rebuilt += "".join(entry["chunk"] for entry in reply["records"])
            if not reply["has_more"]:
                break
            cursor = reply["next_cursor"]
        self.assertEqual(rebuilt, LARGE)
        brief = call(operation="brief", limit=1, **base)
        self.assertEqual(brief["events"][0]["event_id"], event["event_id"])


class AbridgedBoundsTests(unittest.TestCase):
    """The degradation order under tight bounds, measured on a synthetic index."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.index = Index(Path(tmp.name) / "private" / "index.sqlite3")
        self.addCleanup(self.index.close)
        # 36 KB of three-byte characters: oversized alone at the real bound.
        self.texts = ["雪" * 12000 + "end", "short note", "tail " * 1000]
        records = [{"schema": "kilix.logs.record/v1", "source_id": "src-1", "session_id": "sess-1",
                    "generation": "gen-1", "record_id": f"rec-{i}", "sequence": i, "timestamp": None,
                    "timestamp_basis": "unknown", "role": "assistant", "channel": "message",
                    "turn_id": None, "tool_call_id": None, "text": text,
                    "origin": {"byte_start": i, "byte_end": i + 1}, "quality": "structured"}
                   for i, text in enumerate(self.texts)]
        events = facts.extract(records)
        # One event citing three records, one of them twice: a long span, a
        # short record, and a short span inside the long record.
        joint = dict(events[0], event_id="evt-joint", evidence=[
            {"record_id": "rec-0", "start": 0, "end": len(self.texts[0]), "quote": self.texts[0]},
            {"record_id": "rec-1", "start": 0, "end": 5, "quote": "short"},
            {"record_id": "rec-0", "start": 100, "end": 110, "quote": self.texts[0][100:110]}])
        self.events = events + [joint]
        self.index.put({"source": {"source_id": "src-1", "session_id": "sess-1", "generation": "gen-1",
                                   "path": str(Path(tmp.name) / "source.jsonl"), "provider": "fake",
                                   "digest": "sha-1", "size": 1},
                        "records": records, "coverage": {"complete": True, "gaps": [], "excluded": []}},
                       self.events)

    def size(self, result):
        return len(json.dumps(result, ensure_ascii=False).encode("utf-8"))

    def rebuild(self, event_id, budget):
        texts, cursor, sizes = {}, None, []
        with mock.patch.object(query, "MAX_JSON_TEXT", budget):
            while True:
                result = query.source_event(self.index, event_id, since_cursor=cursor)
                sizes.append(self.size(result))
                for entry in result["records"]:
                    texts[entry["evidence_index"]] = texts.get(entry["evidence_index"], "") + entry["chunk"]
                if not result["has_more"]:
                    return texts, sizes
                cursor = result["next_cursor"]

    def test_multiple_evidence_records_abridge_only_long_quotes_and_rebuild_in_order(self):
        page = query.list_events(self.index, "src-1", limit=10)
        self.assertEqual(len(page["events"]), 4)
        self.assertEqual(page["events_abridged"], 2)
        joint = next(event for event in page["events"] if event["event_id"] == "evt-joint")
        self.assertEqual([("quote" in item, item.get("quote_omitted", False)) for item in joint["evidence"]],
                         [(False, True), (True, False), (True, False)])
        self.assertEqual(joint["evidence"][0]["excerpt"], self.texts[0][:query.EXCERPT_CHARS])
        self.assertEqual(joint["evidence"][1:], self.events[-1]["evidence"][1:])
        texts, sizes = self.rebuild("evt-joint", query.MAX_JSON_TEXT)
        self.assertEqual(texts, {0: self.texts[0], 1: self.texts[1], 2: self.texts[0]})
        self.assertLessEqual(max(sizes), query.MAX_JSON_TEXT)

    def test_tiny_bounds_shrink_the_excerpt_then_refuse(self):
        everything = query.list_events(self.index, "src-1", limit=10)["events"]
        page = query.list_events(self.index, "src-1", limit=1)
        self.assertEqual(page["events"][0]["evidence"][0]["excerpt_end"], query.EXCERPT_CHARS)
        abridged = self.size(page)
        bare = self.size(query.bounded(dict(page, events=[query._abridge(self.events[0], 0)])))
        # Below the default excerpt: a shorter exact excerpt, never a refusal.
        for budget in (abridged - 1, (abridged + bare) // 2, bare):
            with self.subTest(budget=budget):
                with mock.patch.object(query, "MAX_JSON_TEXT", budget):
                    shown = query.list_events(self.index, "src-1", limit=1)
                item = shown["events"][0]["evidence"][0]
                self.assertLess(item["excerpt_end"], query.EXCERPT_CHARS)
                self.assertEqual(item["excerpt"], self.texts[0][:item["excerpt_end"]])
                self.assertLessEqual(self.size(shown), budget)
                # The cursor still names the abridged event: paging resumes after it.
                follow = query.list_events(self.index, "src-1", limit=1, since_cursor=shown["next_cursor"])
                self.assertEqual(follow["events"][0]["event_id"], everything[1]["event_id"])
        with mock.patch.object(query, "MAX_JSON_TEXT", bare - 1):
            with self.assertRaises(query.QueryError) as raised:
                query.list_events(self.index, "src-1", limit=1)
            self.assertEqual(raised.exception.code, "result_too_large")

    def test_tight_brief_keeps_the_newest_event_abridged(self):
        whole = query.brief(self.index, "src-1", limit=1)
        self.assertNotIn("events_abridged", whole)          # 5,000 characters fit at the real bound
        with mock.patch.object(query, "MAX_JSON_TEXT", self.size(whole) - 1000):
            tight = query.brief(self.index, "src-1", limit=4)
        self.assertLessEqual(self.size(tight), self.size(whole) - 1000)
        newest = tight["events"][-1]
        self.assertEqual(newest["event_id"], self.events[2]["event_id"])
        item = newest["evidence"][0]
        self.assertIs(item["quote_omitted"], True)
        self.assertEqual(item["excerpt"], self.texts[2][:item["excerpt_end"] - item["start"]])
        self.assertEqual(tight["omitted_events"] + len(tight["events"]), 4)

    def test_source_under_a_tight_bound_still_rebuilds_or_refuses_cleanly(self):
        for budget in (6000, 2500):
            with self.subTest(budget=budget):
                texts, sizes = self.rebuild(self.events[0]["event_id"], budget)
                self.assertEqual(texts, {0: self.texts[0]})
                self.assertLessEqual(max(sizes), budget)
        with mock.patch.object(query, "MAX_JSON_TEXT", 300):
            with self.assertRaises(query.QueryError) as raised:
                query.source_event(self.index, self.events[0]["event_id"])
            self.assertEqual(raised.exception.code, "result_too_large")


if __name__ == "__main__":
    unittest.main()
