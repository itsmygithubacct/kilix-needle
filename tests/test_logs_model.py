import unittest

from needle_logs.chunks import chunk_records
from needle_logs.extract import validate, extract_chunk, candidates, prompt
from evaluate_logs import evaluate


def record(text, rid="r1", role="assistant", source="s"):
    return {"schema": "kilix.logs.record/v1", "source_id": source, "session_id": "sess",
            "generation": "g", "record_id": rid, "sequence": 1, "timestamp": None,
            "role": role, "channel": "message", "quality": "structured", "text": text}


class FakeModel:
    def __init__(self, reply): self.reply = reply; self.resets = 0
    def reset(self): self.resets += 1
    def complete(self, text): return self.reply


class LogsModel(unittest.TestCase):
    def test_unicode_and_exact_quote(self):
        r = record('雪 \\ path\n{"ok":true}')
        chunk = chunk_records([r])["chunks"][0]
        p = [{"kind": "answer", "candidate": "c1"}]
        event = validate([r], p, chunk=chunk)["events"][0]
        self.assertEqual(event["evidence"][0]["quote"], "雪 \\ path")
        self.assertEqual(event["evidence_class"], "assistant_claim")
        self.assertEqual(event["source_id"], "s")

    def test_unowned_and_cross_record_rejected(self):
        rows = [record("one\ntwo", "r1"), record("other", "r2")]
        chunk = chunk_records(rows, max_chars=4)["chunks"][0]
        proposals = [{"kind": "change", "candidate": "c2"},
                     {"kind": "change", "candidate": "r2"}]
        self.assertEqual([e["code"] for e in validate(rows, proposals, chunk=chunk)["errors"]],
                         ["foreign_candidate", "foreign_candidate"])

    def test_cursor_covers_all_fragments(self):
        rows = [record("a\nb\nc", "r1")]
        first = chunk_records(rows, max_chars=2, max_chunks=1)
        second = chunk_records(rows, max_chars=2, max_chunks=3, cursor=first["next_cursor"])
        spans = first["chunks"] + second["chunks"]
        self.assertEqual(''.join(p["text"] for c in spans for p in c["extractable"]), "a\nb\nc")
        self.assertIsNone(second["next_cursor"])

    def test_invalid_reply_is_failure(self):
        r = record("Passed")
        model = FakeModel({"type": "call", "function_calls": []})
        result = extract_chunk([r], chunk_records([r])["chunks"][0], model)
        self.assertFalse(result["complete"])
        self.assertEqual(model.resets, 1)

    def test_evaluator_includes_failed_case_in_recall(self):
        r = record("Passed")
        gold = validate([r], [{"kind": "test_result", "candidate": "c1"}],
                        chunk=chunk_records([r])["chunks"][0])["events"]
        score = evaluate([{"records": [r], "gold": gold, "result": {"events": [], "complete": False}}])
        self.assertEqual(score["per_kind"]["test_result"]["recall"], 0)
        self.assertEqual(score["failures"]["incomplete_cases"], 1)

    def test_evaluator_rejects_unhashable_kind(self):
        r = record("Passed")
        score = evaluate([{"records": [r], "gold": [],
                           "result": {"events": [{"kind": [], "evidence": []}], "complete": True}}])
        self.assertEqual(score["failures"]["unknown_kind"], 1)

    def test_evaluator_flags_forged_source_and_false_completion(self):
        r = record("Finished")
        chunk = chunk_records([r])["chunks"][0]
        event = validate([r], [{"kind": "completion", "candidate": "c1"}], chunk=chunk)["events"][0]
        forged = {**event, "session_id": "other"}
        score = evaluate([{"records": [r], "gold": [], "result": {"events": [forged], "complete": True}}])
        self.assertEqual(score["evidence"]["validity"], 0)
        self.assertEqual(score["critical_misattributions"][0]["reason"], "cross_source_attribution")

    def test_validator_rejects_extra_field_and_foreign_candidate(self):
        r = record("Passed")
        chunk = chunk_records([r])["chunks"][0]
        p = [{"kind": "completion", "candidate": "c1", "start": True},
             {"kind": "completion", "candidate": "c2"}]
        self.assertEqual(len(validate([r], p, chunk=chunk)["errors"]), 2)

    def test_candidate_ids_preserve_offsets_on_multiline(self):
        r = record("First passed.\nLater failed: 雪")
        chunk = chunk_records([r])["chunks"][0]
        cs = candidates(chunk)
        self.assertEqual([r["text"][c["start"]:c["end"]] for c in cs],
                         ["First passed.", "Later failed: 雪"])
        event = validate([r], [{"kind": "error", "candidate": "c2"}], chunk=chunk)["events"][0]
        self.assertEqual(event["evidence"][0]["quote"], "Later failed: 雪")

if __name__ == "__main__": unittest.main()

class MalformedReplies(unittest.TestCase):
    def test_bad_shapes_and_rejected_success(self):
        r = record("Passed")
        chunk = chunk_records([r])["chunks"][0]
        replies = [None, [], {"type":"call","function_calls":[None]},
                   {"type":"call","success":False,"function_calls":[{"name":"extract_events","arguments":{"events":[]}}]},
                   {"type":"call","function_calls":[{"name":"extract_events","arguments":{"events":[{"kind":[],"candidate":"c1"}]}}]}]
        for reply in replies:
            with self.subTest(reply=reply):
                result = extract_chunk([r], chunk, FakeModel(reply))
                self.assertFalse(result["complete"])
                self.assertTrue(result["errors"])

    def test_saturated_output_is_not_complete(self):
        r = record("Passed")
        chunk = chunk_records([r])["chunks"][0]
        events = [{"kind":"completion","candidate":"c1"} for _ in range(4)]
        reply = {"type":"call","function_calls":[{"name":"extract_events","arguments":{"events":events}}]}
        result = extract_chunk([r], chunk, FakeModel(reply))
        self.assertFalse(result["complete"])
        self.assertTrue(result["saturated"])

class ChunkGuards(unittest.TestCase):
    def test_repeated_ids_and_mixed_sessions_refused(self):
        first = record("one", "r1")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            chunk_records([first, first])
        second = {**record("two", "r2"), "session_id": "another"}
        with self.assertRaisesRegex(ValueError, "mixed"):
            chunk_records([first, second])

    def test_extra_arguments_and_boolean_candidate_refused(self):
        r = record("Passed")
        chunk = chunk_records([r])["chunks"][0]
        reply = {"type":"call","function_calls":[{"name":"extract_events",
                 "arguments":{"events":[{"kind":"completion","candidate":"c1","start":True}]}}]}
        self.assertFalse(extract_chunk([r], chunk, FakeModel(reply))["complete"])

    def test_metadata_and_exclusion_survive_subdivision(self):
        import json
        from needle_logs.extract import extract_all
        r = {**record("First sentence. Second sentence.", role="assistant"),
             "quality": "approximate", "sequence": 7}
        chunk = chunk_records([r])["chunks"][0]
        row = json.loads(prompt(chunk).split("\n", 1)[1])[0]
        self.assertEqual((row["role"], row["channel"], row["quality"], row["sequence"]),
                         ("assistant", "message", "approximate", 7))
        self.assertEqual(validate([r], [{"kind": "answer", "candidate": "c1"}], chunk=chunk)["events"][0]["evidence_class"],
                         "unattributed_text")
        class Saturating(FakeModel):
            def complete(self, text):
                for candidate in json.loads(text.split("\n", 1)[1]):
                    self_outer.assertEqual(candidate["quality"], "approximate")
                return self.reply
        self_outer = self
        reply = {"type": "call", "function_calls": [{"name": "extract_events", "arguments":
                 {"events": [{"kind": "answer", "candidate": "c1"}] * 4}}]}
        extract_all([r], Saturating(reply), max_depth=1)
        forbidden = [{**record("secret", "sys", "system")},
                     {**record("secret", "dev", "developer")},
                     {**record("secret", "reason"), "channel": "reasoning"}]
        self.assertEqual(chunk_records(forbidden)["chunks"], [])

    def test_limits_and_unknown_remaining(self):
        for kwargs in ({"max_chars": True}, {"max_chunks": False}, {"max_chunks": 1.0}):
            with self.assertRaises(ValueError):
                chunk_records([record("abc")], **kwargs)
        view = chunk_records([record("abcdef")], max_chars=1, max_chunks=1)
        self.assertEqual(view["coverage"]["remaining_fragments"], None)
        self.assertIsNotNone(view["next_cursor"])

    def test_evaluator_unknown_and_forged_class(self):
        r = record("Finished")
        event = validate([r], [{"kind": "completion", "candidate": "c1"}],
                         chunk=chunk_records([r])["chunks"][0])["events"][0]
        forged = {**event, "evidence_class": "structured_fact"}
        score = evaluate([{"records": [r], "gold": [event], "result":
                           {"events": [forged, {"kind": []}, {"kind": "new"}], "complete": True}}])
        self.assertEqual(score["micro"]["predicted"], 3)
        self.assertEqual(score["micro"]["precision"], 0)
        self.assertEqual(score["failures"]["false_positives"], 3)
        empty = evaluate([{"records": [r], "gold": [], "result": {"events": []}}])
        self.assertFalse(empty["micro"]["precision_defined"])
        self.assertFalse(empty["micro"]["recall_defined"])

    def test_evaluator_malformed_ids_and_gold_do_not_crash(self):
        r = record("Finished")
        score = evaluate([{"records": [r, {"record_id": []}],
                           "gold": [{"kind": [], "evidence": []}, {"kind": "completion", "evidence": []}],
                           "result": {"events": [{"kind": "completion", "evidence":
                                       [{"record_id": [], "start": 0, "end": 1}]}], "complete": True}}])
        self.assertEqual(score["micro"]["predicted"], 1)
        self.assertEqual(score["micro"]["gold"], 0)
        self.assertEqual(score["failures"]["false_positives"], 1)
