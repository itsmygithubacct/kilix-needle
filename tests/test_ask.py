"""`ask`: a routed request runs its job unchanged; an unsure route never acts."""
import io
import json
import os
import sys
import unittest
from unittest import mock

from support import FakeKilix, desktop
import ask
import history
import mcp_server
import needle_cli
import router


def route(stage, job=None, candidates=(), claims=(), p=0.95):
    return router.Route(stage, job, p, 0.5, [("none", 0.01)], list(claims), list(candidates))


class FakeJobs:
    """Records each run; answers with a scripted record per job."""

    def __init__(self, records=None):
        self.records = records or {}
        self.runs = []

    def run(self, job, request, *, dry_run, assume_yes=False):
        self.runs.append((job, dry_run, assume_yes, history._quiet.get()))
        return dict(self.records.get(job, {"request": request, "status": 0, "note": "",
                                           "items": [{"kind": "k", "outcome": "would" if dry_run
                                                      else "done"}]}))


REFUSED = {"request": "x", "status": 1, "note": "", "items": [{"kind": "k", "outcome": "refused"}]}


class Pipeline(unittest.TestCase):
    def test_admits_reads_each_jobs_plan_record(self):
        self.assertTrue(ask.admits("panes", {"items": [{"outcome": "would"}]}))
        self.assertTrue(ask.admits("apps", {"items": [{"outcome": "skipped"}]}))
        self.assertFalse(ask.admits("panes", {"items": [{"outcome": "refused"}]}))
        self.assertFalse(ask.admits("panes", {"items": [{"outcome": "unresolved"}]}))
        self.assertTrue(ask.admits("files", {"status": 0, "plan": [{"kind": "find_files"}]}))
        self.assertFalse(ask.admits("files", {"status": 1, "plan": []}))
        self.assertFalse(ask.admits("system", None))

    def test_a_sure_route_runs_its_job_once_and_says_so(self):
        jobs = FakeJobs()
        with mock.patch.object(router, "route", return_value=route(1, "apps")):
            plan = ask.ask("open the pdf viewer", jobs, act=False)
            done = ask.ask("open the pdf viewer", jobs, act=True, assume_yes=True)
        self.assertEqual(jobs.runs, [("apps", True, False, False), ("apps", False, True, False)])
        self.assertEqual(plan["route"]["job"], "apps")
        self.assertEqual(done["route"]["stage"], 1)

    def test_no_yes_widens_the_agents_jobs_consent(self):
        jobs = ask.Jobs(lambda job: None, lambda: None, confirm=needle_cli._never, agent=True)
        seen = {}

        def agents(engine, request, options, confirm, cwd=None):
            seen["options"] = options
            return {"status": 0, "items": []}
        with mock.patch.object(needle_cli, "run_agents_request", agents):
            jobs.run("agents", "start codex in /x", dry_run=False, assume_yes=True)
        self.assertFalse(seen["options"].assume_yes)

    def test_none_and_named_jobs_run_nothing(self):
        jobs = FakeJobs()
        with mock.patch.object(router, "route", return_value=route(1, "none")):
            record = ask.ask("what is a monad", jobs, act=True)
        self.assertEqual((record["status"], jobs.runs), (1, []))
        for job, cli in (("tmux", "kilix-needle tmux"), ("pty", "kilix-needle pty"),
                         ("desktop", "kilix-needle desktop"), ("logs", "kilix-needle logs")):
            with mock.patch.object(router, "route", return_value=route(0, job, claims=[job])):
                record = ask.ask("do the thing", jobs, act=True, assume_yes=True)
            self.assertEqual(record["status"], 1)
            self.assertIn(cli, record["hint"])
            self.assertEqual(jobs.runs, [])

    def test_stage_2_plans_quietly_and_never_acts(self):
        jobs = FakeJobs({"system": REFUSED})
        with mock.patch.object(router, "route",
                               return_value=route(2, candidates=["system", "panes"], p=0.5)):
            plan = ask.ask("show me htop", jobs, act=False)
            act = ask.ask("show me htop", jobs, act=True, assume_yes=True)
        self.assertTrue(all(dry and quiet for _job, dry, _yes, quiet in jobs.runs))
        self.assertEqual(plan["route"], {**plan["route"], "stage": 2, "job": "panes"})
        self.assertEqual(act["status"], 1)
        self.assertIn("planned, not run", act["note"])
        self.assertIn("kilix-needle ", act["hint"])

    def test_two_or_no_admitting_plans_ask_the_person(self):
        for records in ({}, {"system": REFUSED, "panes": REFUSED}):
            jobs = FakeJobs(records)
            with mock.patch.object(router, "route",
                                   return_value=route(2, candidates=["system", "panes"], p=0.5)):
                record = ask.ask("show me htop", jobs, act=True, assume_yes=True)
            self.assertEqual((record["status"], record["route"]["stage"]), (1, "clarify"))
            self.assertIn("system: show memory", record["hint"])
            self.assertIn("panes: split right and run htop", record["hint"])

    def test_a_router_failure_fails_closed(self):
        jobs = FakeJobs()
        with mock.patch.object(router, "route", side_effect=RuntimeError("boom")):
            record = ask.ask("anything at all", jobs, act=True, assume_yes=True)
        self.assertEqual((record["status"], record["route"]["stage"]), (1, "clarify"))
        self.assertEqual(jobs.runs, [])

    def test_the_history_records_the_route(self):
        entries = []
        with mock.patch.object(history, "_append",
                               lambda folder, line: entries.append(json.loads(line))):
            jobs = FakeJobs()

            class Recording(FakeJobs):
                def run(self, job, request, **kw):
                    history.record(job, request, None, None, None, needle_cli.Options(),
                                   {"status": 0, "items": []}, 0.0)
                    return super().run(job, request, **kw)
            jobs = Recording()
            with mock.patch.object(router, "route", return_value=route(1, "files")):
                ask.ask("find pdf files in Downloads", jobs, act=False)
            with history.quiet():
                history.record("panes", "x", None, None, None, needle_cli.Options(),
                               {"status": 0, "items": []}, 0.0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["route"]["job"], "files")
        self.assertEqual(entries[0]["route"]["stage"], 1)


class Mcp(unittest.TestCase):
    def setUp(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def call(self, name, **arguments):
        server = mcp_server.Server(lambda job="panes": (_ for _ in ()).throw(AssertionError("model")))
        return server.call_tool(name, arguments)["structuredContent"]

    def test_ask_plan_routes_an_exact_pane_command_and_runs_nothing(self):
        with FakeKilix(desktop()) as fake:
            record = self.call("kilix_ask_plan", request="close tab 2")
            self.assertEqual([c for c in fake.calls() if c[0][0] not in ("ls", "list")], [])
        self.assertEqual((record["route"]["stage"], record["route"]["job"]), (0, "panes"))
        self.assertEqual(record["items"][0]["outcome"], "would")

    def test_ask_act_takes_exactly_the_panes_jobs_consent(self):
        with FakeKilix(desktop()) as fake:
            refused = self.call("kilix_ask_act", request="close tab 2")
            self.assertEqual([c for c in fake.calls() if c[0][0] not in ("ls", "list")], [])
            done = self.call("kilix_ask_act", request="close tab 2", confirm_risky=True)
            self.assertEqual(len([c for c in fake.calls() if c[0][0] == "close-tab"]), 1)
        self.assertEqual(refused["items"][0]["outcome"], "skipped")
        self.assertEqual(done["items"][0]["outcome"], "done")

    def test_ask_tools_refuse_other_arguments(self):
        server = mcp_server.Server(lambda job="panes": None)
        with self.assertRaises(ValueError):
            server.call_tool("kilix_ask_plan", {"request": "x", "confirm_risky": True})
        with self.assertRaises(ValueError):
            server.call_tool("kilix_ask_act", {"request": "x", "socket": "/tmp/s"})
        with self.assertRaises(ValueError):
            server.call_tool("kilix_ask_act", {"request": "  "})


class Cli(unittest.TestCase):
    def setUp(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def main(self, argv, stdin=""):
        saved = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(stdin), io.StringIO()
        try:
            status = needle_cli.main(argv)
            return status, sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = saved

    def test_the_no_verb_default_answers_with_panes_when_unsure(self):
        unsure = route(2, candidates=["system", "panes"], p=0.5)
        with FakeKilix(desktop()), mock.patch.object(router, "route", return_value=unsure):
            status, out = self.main(["--dry-run", "close tab 2"])
            status_json, out_json = self.main(["--dry-run", "--json", "close tab 2"])
        with FakeKilix(desktop()):
            _s, panes = self.main(["panes", "--dry-run", "close tab 2"])
        self.assertEqual((status, out), (0, panes))         # exactly the panes job's output
        record = json.loads(out_json)
        self.assertEqual((record["route"]["stage"], record["route"]["job"]), ("default", "panes"))

    def test_the_no_verb_default_sends_a_sure_route_elsewhere(self):
        with mock.patch.object(router, "route", return_value=route(1, "none")):
            status, out = self.main(["--json", "what is the capital of france"])
        record = json.loads(out)
        self.assertEqual((status, record["route"]["job"]), (1, "none"))

    def test_the_panes_verb_is_not_routed(self):
        with FakeKilix(desktop()), mock.patch.object(router, "route",
                                                      side_effect=AssertionError("routed")):
            status, out = self.main(["panes", "--dry-run", "close tab 2"])
        self.assertEqual(status, 0)
        self.assertIn("would close", out)

    def test_interactive_acts_only_on_yes(self):
        with FakeKilix(desktop()) as fake, mock.patch.object(sys, "stdin") as stdin:
            stdin.isatty.return_value = True
            stdin.readline.return_value = "n\n"
            saved = sys.stdout
            sys.stdout = io.StringIO()
            try:
                declined = ask.main(["--interactive", "close tab 2"])
                out = sys.stdout.getvalue()
                stdin.readline.return_value = "y\n"
                accepted = ask.main(["--interactive", "--", "close tab 2"])
            finally:
                sys.stdout = saved
            closed = [c for c in fake.calls() if c[0][0] == "close-tab"]
        self.assertEqual(declined, 1)
        self.assertIn("route: panes", out)
        self.assertIn("not run", out)
        self.assertEqual(accepted, 0)
        self.assertEqual(len(closed), 1)

    def test_interactive_never_offers_yes_for_an_unsure_route(self):
        unsure = route(2, candidates=["system", "panes"], p=0.5)
        with FakeKilix(desktop()) as fake, mock.patch.object(router, "route", return_value=unsure), \
                mock.patch.object(ask, "_yes", side_effect=AssertionError("asked")):
            saved = sys.stdout
            sys.stdout = io.StringIO()
            try:
                status = ask.main(["--interactive", "close tab 2"])
            finally:
                sys.stdout = saved
            self.assertEqual([c for c in fake.calls() if c[0][0] == "close-tab"], [])
        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main()
