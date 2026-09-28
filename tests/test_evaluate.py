"""Runtime failures must not become successful no-action evaluations."""
import unittest

import support  # noqa: F401
import evaluate
from libengine import LibEngineError


class ReplyEngine:
    def __init__(self, reply):
        self.reply = reply
        self.starts = 0

    def reset(self):
        pass

    def complete(self, _request):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def close(self):
        pass

    def start(self):
        self.starts += 1


class RuntimeAccounting(unittest.TestCase):
    def score(self, reply, expect=None, job="agents"):
        case = {"request": "open codex in kilix" if expect else "do nothing",
                "expect": expect or [], "tag": "fixture"}
        return evaluate.score(ReplyEngine(reply), [case], job=job)

    def assert_error(self, result):
        totals = result["totals"]
        self.assertEqual((totals["cases"], totals["exact"], totals["errors"]), (1, 0, 1))
        self.assertEqual((totals["tools"], totals["held"], totals["unsafe"]), (0, 0, 0))
        self.assertEqual(result["tags"]["fixture"]["errors"], 1)
        self.assertEqual(result["failures"][0]["admitted"], [])
        self.assertTrue(result["failures"][0]["error"])

    def test_model_error_is_not_a_correct_no_action_reply_in_any_job(self):
        reply = {"success": False, "error": "tool call truncated: token budget exhausted",
                 "reason": "runtime_failure", "function_calls": []}
        for job in ("agents", "apps", "panes"):
            with self.subTest(job=job):
                self.assert_error(self.score(reply, job=job))

    def test_valid_empty_reply_keeps_no_action_credit(self):
        for reply in ({"success": True, "function_calls": []}, {"function_calls": []}, {}):
            with self.subTest(reply=reply):
                result = self.score(reply)
                self.assertEqual((result["totals"]["exact"], result["totals"]["errors"]), (1, 0))
                self.assertEqual(result["failures"], [])

    def test_error_on_positive_case_does_not_admit_partial_calls(self):
        args = {"agent": "codex", "dir": "kilix"}
        reply = {"success": False, "error": "truncated",
                 "function_calls": [{"name": "agent", "arguments": args}]}
        self.assert_error(self.score(reply, [["agent", args]]))

    def test_transport_error_is_not_no_action_credit_and_restarts(self):
        engine = ReplyEngine(LibEngineError("the library worker exited"))
        result = evaluate.score(engine, [{"request": "do nothing", "expect": [], "tag": "fixture"}], job="agents")
        self.assert_error(result)
        self.assertEqual(engine.starts, 1)
        self.assertEqual(result["failures"][0]["error"], "the library worker exited")

    def test_error_markers_do_not_require_an_error_string(self):
        for reply in ({"success": False}, {"type": "error"}, {"reason": "runtime_failure"}):
            with self.subTest(reply=reply):
                self.assert_error(self.score(reply))

    def test_malformed_replies_are_errors(self):
        for reply in (None, [], "bad", {"function_calls": {}}, {"function_calls": "bad"}):
            with self.subTest(reply=reply):
                self.assert_error(self.score(reply))

    def test_valid_positive_call_still_scores_exact(self):
        args = {"agent": "codex", "dir": "kilix"}
        result = self.score({"function_calls": [{"name": "agent", "arguments": args}]}, [["agent", args]])
        self.assertEqual((result["totals"]["exact"], result["totals"]["errors"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
