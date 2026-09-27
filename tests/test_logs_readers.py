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
