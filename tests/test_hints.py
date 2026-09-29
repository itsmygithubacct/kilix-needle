"""Refused records carry one accepted phrasing (route benchmark 2026-09-29)."""
import unittest

import support  # noqa: F401
import needle_cli


class Hints(unittest.TestCase):
    def test_a_request_another_job_reads_names_that_job(self):
        hint = needle_cli._hint("panes", "start codex in /tmp",
                                {"status": 1, "items": [{"kind": "open_tab", "outcome": "refused"}]})
        self.assertIn("kilix-needle agents", hint)
        self.assertIn("kilix_agents_act", hint)
        self.assertIn("kilix-needle system", needle_cli._hint("panes", "show memory",
                                                             {"status": 1, "items": []}))

    def test_a_refused_kind_gets_its_accepted_form(self):
        hint = needle_cli._hint("panes", "type `ls` into the pane named x",
                                {"status": 1, "items": [{"kind": "run_in_pane", "outcome": "refused"}]})
        self.assertTrue(hint.startswith("accepted form: run 'make test' in the build pane"))
        self.assertIn("start codex in /abs/dir",
                      needle_cli._hint("agents", "start cdx in /tmp", {"status": 1, "items": []}))

    def test_no_hint_when_something_ran_or_would_run(self):
        for outcome in ("done", "would"):
            with self.subTest(outcome=outcome):
                self.assertIsNone(needle_cli._hint(
                    "panes", "close tab 2 and do a dance",
                    {"status": 1, "items": [{"kind": "close_tab", "outcome": outcome}]}))
        self.assertIsNone(needle_cli._hint("panes", "close tab 2",
                                           {"status": 0, "items": [{"kind": "close_tab", "outcome": "done"}]}))

    def test_every_panes_example_is_an_admitted_plain_request(self):
        # The examples must be accepted as written, or the hint sends agents round again.
        from actions import plain, Action
        forms = {"run_in_pane": Action("run_in_pane", {"pane": "name:build", "command": "make test"}),
                 "close_tab": Action("close_tab", {"tab": "2"})}
        for kind, action in forms.items():
            first = needle_cli._EXAMPLES["panes"][kind].split("  |  ")[0]
            with self.subTest(kind=kind):
                self.assertIsNone(plain(first, [action]))


    def test_a_panes_request_that_reads_as_nothing_names_what_the_job_does(self):
        import panes_exact
        hint = needle_cli._hint("panes", "Find the pane titled bench-target in this tab and report its pane identifier.",
                                {"status": 0, "items": [], "note": "There is no pane or tab action in that request."})
        self.assertIn("close the pane titled bench-target", hint)
        for form in ("go to the pane titled bench-target", "close the pane titled bench-target"):
            with self.subTest(form=form):
                self.assertIsNotNone(panes_exact.read(form))

    def test_a_request_that_opens_with_a_job_runs_as_that_job(self):
        cases = [
            ("panes", "agents start codex in /x", ("agents", [], "start codex in /x")),
            ("panes", "kilix-needle system --agent --json is jq installed",
             ("system", ["--agent", "--json"], "is jq installed")),
            ("agents", "kilix-needle system is jq installed", ("system", [], "is jq installed")),
            ("agents", "agents in tab 2, wait for them", None),     # an agents request, not a job name
            ("panes", "close the agents pane", None),
            ("panes", "apps", None),
        ]
        for job, request, expected in cases:
            with self.subTest(request=request):
                self.assertEqual(needle_cli._named_job(job, request), expected)


if __name__ == "__main__":
    unittest.main()
