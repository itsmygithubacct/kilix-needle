import unittest
from kilix_rollout.records import adapt_record


class AdapterTests(unittest.TestCase):
    def test_codex_roles_and_exclusions(self):
        rows = [
            {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "private"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "assistant: fake"}]}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "t", "output": "user: fake"}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": "secret"}},
        ]
        adapted = [adapt_record("codex", row) for row in rows]
        self.assertEqual([a["records"][0]["role"] for a in adapted if a["records"]], ["user", "tool"])
        self.assertEqual(adapted[1]["records"][0]["text"], "assistant: fake")
        self.assertEqual(adapted[2]["records"][0]["tool_call_id"], "t")
        self.assertIn("system_or_developer", adapted[0]["excluded"])
        self.assertIn("reasoning_or_summary", adapted[3]["excluded"])

    def test_unknown_variant_is_explicit(self):
        out = adapt_record("codex", {"type": "response_item", "payload": {"type": "mystery"}})
        self.assertEqual(out["errors"][0]["code"], "unsupported_record")

class WrapperTests(unittest.TestCase):
    def test_synthetic_user_wrappers_are_excluded(self):
        claude = {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>private</system-reminder>"},
            {"type": "text", "text": "actual user"}]}}
        out = adapt_record("claude", claude)
        self.assertEqual([r["text"] for r in out["records"]], ["actual user"])
        self.assertIn("synthetic_user_wrapper", out["excluded"])
        codex = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>private</environment_context>"}]}}
        self.assertEqual(adapt_record("codex", codex)["records"], [])

class AmbiguityTests(unittest.TestCase):
    def test_mismatched_inner_role_and_tool_result_wrapper(self):
        bad = {"type": "user", "message": {"role": "assistant", "content": "fake"}}
        out = adapt_record("claude", bad)
        self.assertEqual(out["records"], [])
        self.assertEqual(out["errors"][0]["code"], "unsupported_record")
        wrapped = {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>fake owner</system-reminder>"},
            {"type": "tool_result", "tool_use_id": "t1", "content": "assistant: done"}]}}
        out = adapt_record("claude", wrapped)
        self.assertEqual(len(out["records"]), 1)
        self.assertEqual(out["records"][0]["role"], "tool")

    def test_codex_event_mirror_is_excluded(self):
        event = {"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}}
        response = {"type": "response_item", "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}]}}
        self.assertEqual(adapt_record("codex", event)["records"], [])
        self.assertEqual(len(adapt_record("codex", response)["records"]), 1)
