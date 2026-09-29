"""Focused tests for independent system planning and proposal validation."""
import unittest
from unittest import mock

import support  # noqa: F401
import system_normalize as normalizer


def proposal(*calls):
    return {"function_calls": [{"name": name, "arguments": args} for name, args in calls]}


class PlanTests(unittest.TestCase):
    def test_grammar_bypasses_classifier(self):
        classify = mock.Mock(side_effect=AssertionError("called"))
        result = normalizer.plan("show memory", classify)
        self.assertEqual((result["path"], result["decision"], result["trust"]),
                         ("grammar", "read", "grammar"))
        classify.assert_not_called()
        for request in ("show update service",):
            with self.subTest(request=request):
                self.assertEqual(normalizer.plan(request, classify)["path"], "grammar")
        classify.assert_not_called()
        for request in ("show memory\n", "show\t memory"):
            with self.subTest(request=request):
                result = normalizer.plan(request, lambda _: proposal(("resources", {"kind": "memory"})))
                self.assertEqual((result["path"], result["decision"]), ("model", "read"))

    def test_natural_unknown_wording_can_be_a_proposal(self):
        reply = proposal(("processes", {"sort": "cpu", "limit": 12}))
        result = normalizer.plan("Could you rank processes by CPU, first 12?", lambda _: reply)
        self.assertEqual((result["path"], result["decision"], result["trust"]),
                         ("model", "read", "model_proposal"))
        self.assertEqual(result["raw_reply"], reply)
        self.assertEqual(result["actions"], [["processes", {"sort": "cpu", "limit": 12}]])

    def test_preflight_rejects_mutations_mixed_requests_and_bad_input(self):
        classify = mock.Mock(side_effect=AssertionError("called"))
        for request in ("restart ssh", "show memory and reboot",
                        "show memory\x1b[2J", "show memory " * 300,
                        "show memory; touch /tmp/x", "fix the service and show its status",
                        "check wifi signal and show memory", "please do not query memory usage"):
            with self.subTest(request=request):
                result = normalizer.plan(request, classify)
                self.assertEqual((result["path"], result["decision"]), ("refused", "decline"))
                self.assertEqual(result["actions"], [])
        classify.assert_not_called()

    def test_negated_write_followed_by_read(self):
        reply = proposal(("services", {"unit": "ssh"}))
        result = normalizer.plan("don't restart ssh, just show ssh service status", lambda _: reply)
        self.assertEqual(result["decision"], "read")
        self.assertEqual(result["actions"][0][1]["unit"], "ssh.service")

    def test_unresolved_referents_need_clarification(self):
        classify = mock.Mock(side_effect=AssertionError("called"))
        for request in ("continue checking the same thing", "actually the other service", "what about it"):
            with self.subTest(request=request):
                self.assertEqual(normalizer.plan(request, classify)["decision"], "clarify")
        classify.assert_not_called()

    def test_within_turn_correction_uses_final_literal(self):
        reply = proposal(("services", {"unit": "cron"}))
        result = normalizer.plan("check ssh service, actually cron service status", lambda _: reply)
        self.assertEqual(result["decision"], "read")
        changed = normalizer.plan("check ssh service, actually cron service status",
                                  lambda _: proposal(("services", {"unit": "nginx"})))
        self.assertEqual(changed["decision"], "clarify")
        old = normalizer.plan("check ssh service, actually cron service status",
                              lambda _: proposal(("services", {"unit": "ssh"})))
        self.assertEqual(old["decision"], "clarify")

    def test_schema_limits_grounding_and_whole_list(self):
        request = "rank processes by memory, first 12, and show disk space on /Data"
        valid = proposal(("processes", {"sort": "memory", "limit": 12}),
                         ("resources", {"kind": "disk", "path": "/Data"}))
        self.assertEqual(normalizer.plan(request, lambda _: valid)["decision"], "read")
        bad = [
            proposal(("processes", {"sort": "memory", "limit": True})),
            proposal(("resources", {"kind": "disk", "path": "/Data", "execute": "rm"})),
            proposal(("resources", {"kind": "disk", "path": "/Data"}),
                     ("resources", {"kind": "disk", "path": "/Data"})),
            proposal(("resources", {"kind": "disk", "path": "/Data"}),
                     ("processes", {"sort": "memory", "limit": 12}),
                     ("services", {}), ("journal", {})),
            {"function_calls": [{"name": "services", "arguments": {}, "extra": 1}]},
        ]
        for reply in bad:
            with self.subTest(reply=reply):
                result = normalizer.plan(request, lambda _: reply)
                self.assertEqual(result["path"], "refused")
                self.assertTrue(result["protocol_error"])
                self.assertEqual(result["actions"], [])
        grounding_bad = [
            proposal(("processes", {"sort": "memory", "limit": 13})),
            proposal(("processes", {"sort": "memory"})),
            proposal(("resources", {"kind": "disk"})),
            proposal(("resources", {"kind": "disk", "path": "/data"})),
            proposal(("processes", {"sort": "cpu", "limit": 12})),
        ]
        for reply in grounding_bad:
            with self.subTest(reply=reply):
                result = normalizer.plan(request, lambda _: reply)
                self.assertTrue(result["validation_error"])
                self.assertFalse(result["protocol_error"])

    def test_explicit_filters_and_compound_targets(self):
        cases = [
            ("show current memory figures", ("resources", {"kind": "all"})),
            ("show overall resources on /srv", ("resources", {"kind": "all"})),
            ("could you pull the logs since yesterday", ("journal", {"boot": "current"})),
            ("could you pull the logs since yesterday", ("journal", {"boot": "any"})),
            ("show last five logs", ("journal", {"limit": 50})),
            ("could you pull logs since 2 hours ago", ("journal", {"limit": 2, "since": "2 hours ago", "boot": "any"})),
        ]
        for request, call in cases:
            with self.subTest(request=request, call=call):
                result = normalizer.plan(request, lambda _: proposal(call))
                self.assertTrue(result["validation_error"])
                self.assertFalse(result["protocol_error"])
        good = normalizer.plan("check whether bash is installed and which package owns /bin/bash",
                               lambda _: proposal(("packages", {"operation": "status", "target": "bash"}),
                                                  ("packages", {"operation": "owner", "target": "/bin/bash"})))
        self.assertEqual((good["path"], good["decision"]), ("model", "read"))

    def test_model_errors_distinct_from_bad_protocol(self):
        request = "could you show the current memory figures"
        for classify in (lambda _: None, lambda _: {"error": "runtime"},
                         mock.Mock(side_effect=RuntimeError("offline"))):
            with self.subTest(classify=classify):
                self.assertTrue(normalizer.plan(request, classify)["runtime_error"])
        for reply in ({}, {"function_calls": {}}, {"function_calls": [None]}):
            with self.subTest(reply=reply):
                self.assertTrue(normalizer.plan(request, lambda _: reply)["protocol_error"])

    def test_tool_family_intent_and_missing_targets(self):
        denied = [
            ("screen went blank. what happened", ("resources", {"kind": "disk"})),
            ("cpu hogs", ("resources", {"kind": "cpu"})),
            ("show top ten cpu consumers", ("resources", {"kind": "cpu"})),
            ("check whether cups is running", ("resources", {"kind": "all"})),
            ("check syncthing in the user session, just its status", ("services", {"scope": "user"})),
            ("which services failed yesterday", ("services", {"state": "failed"})),
            ("newest error from this boot", ("journal", {"priority": "err"})),
            ("check whether bash is installed", ("packages", {"operation": "owner", "target": "bash"})),
            ("the errors from then", ("processes", {"sort": "memory"})),
        ]
        for request, call in denied:
            with self.subTest(request=request):
                self.assertNotEqual(normalizer.plan(request, lambda _: proposal(call))["decision"], "read")
        classify = mock.Mock(side_effect=AssertionError("called"))
        for request in ("check free disk space and inode usage", "show caches and large directories",
                        "check my backup service, I don't remember its name", "space left on that drive"):
            with self.subTest(request=request):
                self.assertEqual(normalizer.plan(request, classify)["path"], "refused")
        classify.assert_not_called()

    def test_positive_intent_and_within_turn_boot_context(self):
        good = [
            ("not this boot, the previous one. show its last 10 journal entries",
             ("journal", {"boot": "previous", "limit": 10})),
            ("give me the overall resource figures", ("resources", {"kind": "all"})),
            ("show the five biggest memory hogs", ("processes", {"sort": "memory", "limit": 5})),
            ("which package did /usr/bin/ssh come from",
             ("packages", {"operation": "owner", "target": "/usr/bin/ssh"})),
        ]
        for request, call in good:
            with self.subTest(request=request):
                self.assertEqual(normalizer.plan(request, lambda _: proposal(call))["decision"], "read")


if __name__ == "__main__":
    unittest.main()
