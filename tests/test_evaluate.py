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

    def test_standalone_transport_error_is_counted(self):
        self.assert_error(self.score(evaluate.EngineError("cannot reach engine")))

    def test_timeout_is_distinct_from_worker_exit(self):
        for message, timeout in (("worker exited", 0), ("library did not answer in 120 s", 1)):
            result = self.score(LibEngineError(message))
            self.assertEqual(result["totals"]["timeouts"], timeout)
            self.assertEqual(result["tags"]["fixture"]["timeouts"], timeout)
            self.assertEqual(result["totals"]["transport_errors"], 1)

    def test_failed_restart_retains_case_and_marks_incomplete(self):
        class Broken(ReplyEngine):
            def start(self):
                raise LibEngineError("cannot restart")
        case = {"request": "do nothing", "expect": []}
        result = evaluate.score(Broken(LibEngineError("worker exited")), [case, case], job="agents")
        self.assertFalse(result["complete"])
        self.assertEqual(result["totals"]["cases"], 1)
        self.assertEqual(result["totals"]["errors"], 1)
        self.assertEqual(len(result["failures"]), 1)
        self.assertIn("cannot restart", result["fatal_error"])


class ScoringSemantics(unittest.TestCase):
    def test_equivalent_wait_message_forms_score_the_same(self):
        request = "wait for codex in kilix to finish, then tell it to push"
        separate = [["wait", {"session": "codex@kilix", "for": "idle"}],
                    ["tell", {"session": "codex@kilix", "text": "push"}]]
        combined = [["tell", {"session": "codex@kilix", "text": "push", "wait": True}]]
        for gold, predicted in ((separate, combined), (combined, separate)):
            reply = {"function_calls": [{"name": k, "arguments": a} for k, a in predicted]}
            result = evaluate.score(ReplyEngine(reply), [{"request": request, "expect": gold}], job="agents")
            self.assertEqual(result["totals"]["exact"], 1)
            self.assertEqual(result["totals"]["unsafe"], 0)
            self.assertEqual(result["totals"]["raw_exact"], 0)
            self.assertEqual(result["totals"]["actionable_exact"], 1)

    def test_timeout_and_other_session_waits_are_not_folded(self):
        _, fold, _, _ = evaluate._rules("agents")
        for wait in ({"session": "codex@kilix", "for": "idle", "timeout": 30},
                     {"session": "grok@kilix", "for": "idle"},
                     {"session": "codex@kilix", "for": "input"}):
            pairs = [["wait", wait], ["tell", {"session": "codex@kilix", "text": "push"}]]
            self.assertEqual(fold(pairs), pairs)

    def test_validator_rejection_is_not_a_raw_no_call(self):
        reply = {"function_calls": [{"name": "agent", "arguments": {"agent": "codex", "dir": "kilix"}}]}
        result = evaluate.score(ReplyEngine(reply), [{"request": "do nothing", "expect": []}], job="agents")
        totals = result["totals"]
        self.assertEqual((totals["exact"], totals["held"], totals["raw_any_call"]), (1, 1, 1))
        self.assertEqual((totals["raw_no_call"], totals["raw_exact"], totals["any_admitted"]), (0, 0, 0))

    def test_explicit_default_tab_is_equivalent_to_omitted_tab(self):
        args = {"agent": "codex", "dir": "kilix", "place": "tab"}
        case = {"request": "open codex in kilix in a new tab", "expect": [["agent", args]]}
        result = evaluate.score(ReplyEngine({"function_calls": [{"name": "agent", "arguments": args}]}), [case], job="agents")
        self.assertEqual(result["totals"]["exact"], 1)
        self.assertEqual(result["totals"]["unsafe"], 0)

    def test_standalone_timeout_cause_is_counted(self):
        error = evaluate.EngineError("cannot reach the Needle engine: timed out")
        error.__cause__ = TimeoutError("timed out")
        result = evaluate.score(ReplyEngine(error), [{"request": "do nothing", "expect": []}], job="agents")
        self.assertEqual(result["totals"]["timeouts"], 1)


if __name__ == "__main__":
    unittest.main()
