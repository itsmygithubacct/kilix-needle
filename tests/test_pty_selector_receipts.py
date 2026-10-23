"""Synthetic selector receipts, with no broker, runtime reads or agent launch."""
import copy
import json
import unittest

import support
import pty_backend
import pty_cli


def success(ident="target-7", attached=False):
    action = {"operation": "end", "selector": {"id": ident, "attached": attached}, "operation_id": "fixture-key"}
    document = {"schema": "kilix.pty.selector/v1", "operation": "end", "runtime": "/tmp/fixture-runtime",
                "selector": copy.deepcopy(action["selector"]), "operation_id": action["operation_id"],
                "caller": {"source": "environment", "authenticated": False, "status": "valid", "id": "fixture-caller"},
                "result": "verified_absent", "reason": None, "request_sent": True, "receiver_refused": False,
                "completion": "verified_absent", "duplicate": False,
                "resolved": {"id": ident, "started_millis": 123, "attached": attached},
                "census": {"complete": True, "census_count": 1, "matches": 1, "possible": 0,
                           "candidates": [{"id": ident, "reachable": True, "possible": False}],
                           "candidates_truncated": False}}
    return action, document


class ReceiptSemantics(unittest.TestCase):
    def test_reported_fake_success_is_rejected(self):
        action, doc = success()
        doc["resolved"]["id"] = "../invalid"; doc["census"]["candidates"] = [None]
        with self.assertRaises(ValueError):
            pty_backend.checked(action, doc, 0)

    def test_protocol_opaque_ids_keep_leading_hyphens_case_and_full_length(self):
        for ident in ("-leading", "a", ".dot", "A_B-7", "X" * 64):
            action, doc = success(ident)
            doc["caller"]["id"] = "-caller"
            self.assertEqual(pty_backend.checked(action, doc, 0), doc)

    def test_ids_callers_rows_counts_and_success_relationships_are_checked(self):
        changes = [lambda d: d["resolved"].update(id="different-id"),
                   lambda d: d["resolved"].update(id="."), lambda d: d["resolved"].update(id="x" * 65),
                   lambda d: d["resolved"].update(attached=True), lambda d: d["resolved"].update(started_millis=True),
                   lambda d: d["caller"].update(id="../invalid"), lambda d: d["caller"].update(status="missing"),
                   lambda d: d["caller"].update(id="target-7"), lambda d: d["caller"].update(id=None),
                   lambda d: d["census"].update(candidates=[None]),
                   lambda d: d["census"]["candidates"][0].update(id="different-id"),
                   lambda d: d["census"]["candidates"][0].update(possible=True),
                   lambda d: d["census"]["candidates"][0].update(reachable=False),
                   lambda d: d["census"]["candidates"][0].update(reachable=1),
                   lambda d: d["census"].update(candidates=[]),
                   lambda d: d["census"].update(candidates_truncated=True),
                   lambda d: d["census"].update(census_count=True),
                   lambda d: d.update(completion="not_sent"), lambda d: d.update(reason="lost_reply"),
                   lambda d: d.update(request_sent=None), lambda d: d.update(receiver_refused=True)]
        for i, change in enumerate(changes):
            action, doc = success(); change(doc)
            with self.subTest(case=i), self.assertRaises(ValueError):
                pty_backend.checked(action, doc, 0)

    def test_preview_unknown_unreachable_and_truncated_output_are_legitimate(self):
        action, doc = success()
        action = {"operation": "resolve", "selector": {"startup_words": ["Sleep", "literal"]}}
        doc.update(operation="resolve", selector=action["selector"], result="unresolved", reason="none",
                   request_sent=False, completion="read", resolved=None)
        doc.pop("operation_id"); doc.pop("duplicate")
        doc["caller"].update(status="missing", id=None)
        for possible, reachable in ((True, True), (True, False), (False, False)):
            row = {"id": "unknown-7", "reachable": reachable, "possible": possible}
            doc["census"].update(matches=0 if possible else 1, possible=1 if possible else 0, candidates=[row])
            self.assertEqual(pty_backend.checked(action, doc, 0), doc)
        doc.update(result="ambiguous")
        doc["census"].update(census_count=37, matches=37, possible=0, candidates_truncated=True,
                            candidates=[{"id": f"item-{i}", "reachable": True, "possible": False} for i in range(32)])
        self.assertEqual(pty_backend.checked(action, doc, 0), doc)
        doc["census"]["candidates_truncated"] = False
        with self.assertRaises(ValueError): pty_backend.checked(action, doc, 0)

    def test_refusal_can_report_bound_target_after_changed_census(self):
        action, doc = success()
        action["selector"] = doc["selector"] = {"id_prefix": "target-"}
        doc.update(result="refused", reason="started_mismatch", completion="not_sent", request_sent=False)
        doc["census"]["candidates"][0]["id"] = "target-new"
        self.assertEqual(pty_backend.checked(action, doc, 3), doc)

    def test_uncertainty_and_negative_ack_completions_remain_distinct(self):
        action, doc = success()
        doc.update(result="uncertain", reason="store_unavailable", request_sent=None, completion="unknown",
                   attempt_sent=False, resolved=None, census=None)
        self.assertEqual(pty_backend.checked(action, doc, 1), doc)
        doc.pop("request_sent")
        with self.assertRaises(ValueError): pty_backend.checked(action, doc, 1)
        doc["request_sent"] = None
        doc["completion"] = "not_sent"
        with self.assertRaises(ValueError): pty_backend.checked(action, doc, 1)
        action, doc = success()
        doc.update(result="refused", reason="attachment_mismatch", receiver_refused=True, completion="receiver_refused")
        self.assertEqual(pty_backend.checked(action, doc, 3), doc)
        doc.update(request_sent=False)
        with self.assertRaises(ValueError): pty_backend.checked(action, doc, 3)

    def test_captured_caller_echo_mismatch_does_not_authorize_a_second_call(self):
        action, doc = success()
        doc["caller"]["id"] = "different-caller"
        calls = []
        def backend(argv, wall=None):
            calls.append(argv)
            return pty_backend.Reply("ok", 0, doc)
        result = pty_cli.run(action, assume_yes=True, agent=True,
                             environ={"KITTY_PTY_BROKER_SESSION": "fixture-caller"}, backend=backend)
        self.assertNotEqual(result["status"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.get("completion"), "unknown")

    def test_selector_json_transport_rejects_duplicate_fields(self):
        action, doc = success()
        raw = json.dumps(doc)[:-1] + ',"result":"verified_absent"}'
        # Parser boundary exercised without starting a process by a fake communicate fixture.
        from unittest import mock
        process = mock.Mock(returncode=0, pid=999)
        process.communicate.return_value = (raw.encode(), b"")
        with mock.patch.object(pty_backend, "kilix_executable", return_value="/tmp/unused-fixture-agent"), \
                mock.patch.object(pty_backend.subprocess, "Popen", return_value=process):
            reply = pty_backend.call(pty_backend.argv_for(action))
        self.assertNotEqual(reply.kind, "ok")


if __name__ == "__main__":
    unittest.main()
