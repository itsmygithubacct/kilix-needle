"""The bundle widened to every job: its schemas (job_schemas), its training
rows (job_rows), and the arms of experiment 11 over the real routes'
functions (job_routes)."""
import hashlib
import io
import json
import os
import shutil
import sys
import unittest
from unittest import mock

from support import FakeKilix, desktop
import history
import job_routes as JR
import job_rows
import job_schemas
import needle_cli
import panes_bundle as PB
import panes_bundle_train as PBT

HOME = "/home/pleb"


class Schemas(unittest.TestCase):
    def test_every_job_is_named_alike(self):
        self.assertEqual(job_schemas.JOBS, PB.JOBS)
        self.assertEqual(set(JR.TOOLS.values()), set(PB.JOBS))

    def test_no_two_jobs_share_a_tool_name(self):
        from actions import TOOLS
        seen = {tool["name"]: "panes" for tool in TOOLS}
        seen.update({PB.LIST: "panes", PB.READ: "panes"})
        self.assertEqual(set(PBT.schema("panes")), set(seen))
        for job in PB.JOBS[1:]:
            for tool in job_schemas.SCHEMAS[job]:
                self.assertNotIn(tool, seen, (job, tool))
                seen[tool] = job

    def test_the_trainer_takes_each_job_s_schema(self):
        for job in PB.JOBS[1:]:
            self.assertEqual(PBT.schema(job), job_schemas.schema(job))
            for spec in job_schemas.SCHEMAS[job].values():
                for arg in spec["args"]:
                    self.assertTrue(set(map(str, arg["values"])) <= PBT.closed_values(job), (job, arg["key"]))
                self.assertTrue(set(spec["required"]) <= {arg["key"] for arg in spec["args"]})

    def test_a_read_of_a_pane_is_a_kind_of_the_panes_bundle(self):
        self.assertEqual(PBT.schema()[PB.READ], {"args": [["pane", "int", 1, 999_999_999],
                                                          ["lines", "int", 1, 1000]], "required": ["pane"]})
        self.assertIn(PB.READ, PB.ALONE_KINDS)

    def test_starting_or_messaging_an_agent_and_launching_an_app_are_risky(self):
        for name in ("agent", "tell", "launch"):
            self.assertTrue(PB.risky({"name": name, "arguments": {}}), name)
        for name in ("wait", "settings", "tmux_read", "preview_file", "read_pane", "list"):
            self.assertFalse(PB.risky({"name": name, "arguments": {}}), name)

    def test_each_job_has_a_directory_of_its_own(self):
        self.assertEqual(PB.directory("panes"), PB.DIRECTORY)
        self.assertEqual(PB.directory("tmux"), PB.JOB_DIRECTORY / "tmux")
        with self.assertRaises(ValueError):
            PB.directory("nothing")


def call(name, **arguments):
    return {"name": name, "arguments": arguments}


class ToJob(unittest.TestCase):
    def test_a_preview_of_an_absolute_path_is_its_directory_and_its_name(self):
        self.assertEqual(job_schemas.to_job("files", [call("preview_file", path="/srv/app/README.md")]),
                         [{"name": "preview_file", "arguments": {"scope": "/srv/app", "path": "README.md"}}])
        self.assertIsNone(job_schemas.to_job("files", [call("preview_file", path="README.md")]))
        self.assertEqual(job_schemas.to_job("files", [call("preview_file", path="README.md", scope="here")]),
                         [{"name": "preview_file", "arguments": {"scope": "here", "path": "README.md"}}])

    def test_a_find_gives_every_filter(self):
        self.assertEqual(job_schemas.to_job("files", [call("find_files", scope="Downloads", extension="pdf",
                                                           modified="days", days=7)]),
                         [{"name": "find_files", "arguments": {"scope": "Downloads", "name": "", "extension": "pdf",
                                                               "modified": "last 7 days"}}])
        self.assertIsNone(job_schemas.to_job("files", [call("find_files", scope="Downloads", modified="days")]))

    def test_two_system_queries_from_one_phrase(self):
        self.assertEqual(job_schemas.to_job("system", [call("slow_machine")]),
                         [{"name": "resources", "arguments": {"kind": "all"}},
                          {"name": "processes", "arguments": {"sort": "cpu"}}])

    def test_a_journal_time_is_written_as_the_grammar_keeps_it(self):
        self.assertEqual(job_schemas.to_job("system", [call("journal", priority="err", since="hour", since_n=1)]),
                         [{"name": "journal", "arguments": {"priority": "err", "since": "1 hour ago", "boot": "any"}}])
        self.assertIsNone(job_schemas.to_job("system", [call("journal", since="hours")]))

    def test_an_agent_session_is_agent_at_directory(self):
        self.assertEqual(job_schemas.to_job("agents", [call("wait", s_agent="codex", s_dir="quill", **{"for": "idle"},
                                                            timeout=5, minutes=True)]),
                         [{"name": "wait", "arguments": {"session": "codex@quill", "for": "idle", "timeout": 300}}])
        self.assertIsNone(job_schemas.to_job("agents", [call("tell", s_agent="codex", text="go")]))

    def test_a_log_s_session_is_its_pane_as_text(self):
        self.assertEqual(job_schemas.to_job("logs", [call("logs_brief", pane=57)]),
                         [{"operation": "brief", "session": "57"}])
        self.assertEqual(job_schemas.to_job("logs", [call("logs_events", file="/var/log/x.log")]),
                         [{"operation": "events", "file": "/var/log/x.log", "provider": "raw"}])
        self.assertIsNone(job_schemas.to_job("logs", [call("logs_events", pane=5, session="x")]))
        self.assertIsNone(job_schemas.to_job("logs", [call("logs_events", provider="codex")]))

    def test_a_tool_of_another_job_is_no_call(self):
        self.assertIsNone(job_schemas.to_job("tmux", [call("pty_list")]))
        self.assertIsNone(job_schemas.to_job("files", [call("close_pane", pane="left")]))


class Rows(unittest.TestCase):
    def test_every_row_is_one_its_job_takes_and_every_value_is_placed(self):
        for job in PB.JOBS:
            rows, counts = job_rows.rows(job, 0, per_template=3)
            self.assertTrue(rows, job)
            tools = PBT.schema(job)
            for row in rows:
                self.assertTrue(job_rows.admitted(job, row["query"], [{"name": k, "arguments": a}
                                                                      for k, a in row["actions"]]), row["query"])
                prepared = PBT.prepare(row, tools)
                self.assertIsNotNone(prepared, row["query"])
                for clause in prepared["clauses"]:
                    self.assertNotIn(None, clause["labels"].values(), row["query"])
                for surface, _ in row["spans"]:
                    self.assertIn(surface, row["query"])
            if job == "panes":
                self.assertEqual({row["actions"][0][0] for row in rows}, {PB.READ, PB.LIST})

    def test_a_row_the_grammar_reads_otherwise_is_not_kept(self):
        # The files grammar reads this as a search in here, not in Downloads.
        self.assertFalse(job_rows.admitted("files", 'find text "x" in here',
                                           [call("search_text", text="x", scope="Downloads")]))
        self.assertTrue(job_rows.admitted("files", 'find text "x" in here', [call("search_text", text="x", scope="here")]))

    def test_a_row_the_grammar_does_not_read_is_kept_when_arm_b_s_checks_take_it(self):
        self.assertTrue(job_rows.admitted("files", "cat src/app.py here", [call("preview_file", path="src/app.py",
                                                                                    scope="here")]))
        # a hidden path: the file collector refuses it
        self.assertFalse(job_rows.admitted("files", "cat .env here", [call("preview_file", path=".env", scope="here")]))
        self.assertTrue(job_rows.admitted("system", "am I running low on memory?", [call("resources", kind="memory")]))
        # the normalizer's checks: the request does not ask for the disk
        self.assertFalse(job_rows.admitted("system", "am I running low on memory?", [call("resources", kind="disk")]))

    def test_the_same_seed_makes_the_same_rows(self):
        self.assertEqual(job_rows.rows("tmux", 3, per_template=2), job_rows.rows("tmux", 3, per_template=2))
        self.assertNotEqual(job_rows.rows("tmux", 3, per_template=2)[0], job_rows.rows("tmux", 4, per_template=2)[0])

    def test_every_wording_of_a_template_is_made(self):
        self.assertEqual(sorted(job_rows._skeletons("[a|b] c( d)")), ["a c", "a c d", "b c", "b c d"])
        tmux = {job_rows.same(row["query"]) for row in job_rows.rows("tmux", 0)[0]}
        for text in ("list sessions", "list sessions.", "show the tmux sessions", "show me the list of tmux sessions",
                     "get a list of sessions"):
            self.assertIn(text, tmux)
        self.assertNotIn("get a sessions", tmux)

    def test_each_closed_value_and_quote_mark_is_made_in_each_wording(self):
        # the reader's frame holds the words of a closed value and the quote
        # marks around a copied text, so each is a wording of its own
        agents = {job_rows.same(row["query"]).split(" in ")[0] for row in job_rows.rows("agents", 0)[0]
                  if row["actions"][0][0] == "wait"}
        for agent in ("claude", "codex", "grok", "qwen"):
            self.assertIn(f"wait until the {agent} session", agents)
        files = [row["query"] for row in job_rows.rows("files", 0)[0] if row["actions"][0][0] == "search_text"]
        self.assertTrue(any('"' in q for q in files) and any("'" in q for q in files))

    def test_a_row_is_made_with_and_without_its_final_mark(self):
        self.assertEqual(job_rows._endings("list sessions"), ["list sessions", "list sessions."])
        self.assertEqual(job_rows._endings("is it up?"), ["is it up?", "is it up", "is it up."])
        # no mark after a path or a quote: the reader would not split it off
        self.assertEqual(job_rows._endings("show /home/pleb/x"), ["show /home/pleb/x"])
        self.assertEqual(job_rows._endings('search for "x"'), ['search for "x"'])
        system = {row["query"] for row in job_rows.rows("system", 0)[0]}
        self.assertTrue(any(q.startswith("is ") and q.endswith("?") for q in system))

    def test_rows_differ_when_their_final_marks_do(self):
        self.assertNotEqual(job_rows.same("list sessions."), job_rows.same("list sessions"))
        self.assertEqual(job_rows.same("List  sessions."), job_rows.same("list sessions."))
        self.assertEqual(job_rows.fold("list sessions."), job_rows.fold("list sessions"))

    def test_a_mark_that_ends_a_copied_value_stays(self):
        text = "tell claude in x: what is left to do?"
        self.assertEqual(job_rows._endings(text, [("what is left to do?", "what is left to do?")]), [text])
        self.assertEqual(job_rows._endings("new session docs", [("docs", "docs")]),
                         ["new session docs", "new session docs."])
        tells = [row for row in job_rows.rows("agents", 0)[0] if row["actions"][0][0] == "tell"]
        self.assertTrue(any(row["actions"][0][1]["text"].endswith("?") for row in tells))


class Proposes:
    """A bundle that proposes these calls for every request."""

    def __init__(self, calls, reason=PB.OK):
        self.calls, self.reason, self.second_kinds = calls, reason, frozenset()

    def read(self, request, texts=None):
        if texts is not None:
            texts[:] = [request] * len(self.calls)
        return [dict(c) for c in self.calls], self.reason


DESKTOP = {"kitty_ls": desktop()}


class Arms(unittest.TestCase):
    def routes(self, **bundles):
        return JR.Routes(bundles, {}, DESKTOP)

    def test_today_s_grammars_answer_in_a_and_c_and_not_in_b(self):
        routes = self.routes()
        for job, request, want in (
                ("files", 'find text "TODO" in here', [["search_text", {"scope": "here", "text": "TODO"}]]),
                ("tmux", "list sessions", [["list", {}]]),
                ("logs", "show the source of event ev-1", [["source", {"event_id": "ev-1"}]]),
                ("panes", "read pane 301", [["read_pane", {"pane": 301}]])):
            for arm in ("A", "C"):
                got = routes.run(arm, job, request)
                self.assertEqual((got["calls"], got["stage"]), (want, "grammar" if job != "panes" else "exact"),
                                 (arm, request))
            got = routes.run("B", job, request)
            self.assertEqual(got["calls"], [], request)
            self.assertTrue(got["refused"] or got["missing"], request)

    def test_a_job_with_a_model_asks_it_after_the_rest(self):
        got = self.routes().run("A", "panes", "stack the panes, the way i like them")
        self.assertEqual((got["stage"], got["missing"]), ("model", True))
        reply = {"function_calls": [{"name": "arrange_panes", "arguments": {"layout": "stack"}}]}
        request = "stack the panes, the way i like them"
        routes = JR.Routes({}, {"panes": {JR.key_of(request): {"ok": reply}}}, DESKTOP)
        got = routes.run("A", "panes", request)
        self.assertEqual(got["stage"], "model")

    def test_the_system_model_s_proposals_are_hints(self):
        got = self.routes().run("A", "system", "how is the box doing")
        self.assertEqual((got["stage"], got["calls"], got["refused"]), ("hint", [], True))
        # timed with the normalizer: it is asked, and its proposal is still a hint
        normalizer = mock.Mock()
        normalizer.complete.return_value = {"function_calls": [{"name": "resources", "arguments": {"kind": "all"}}]}
        routes = JR.Routes({}, {}, DESKTOP, engines={"system": normalizer})
        got = routes.run("A", "system", "how is the box doing")
        self.assertEqual((got["stage"], got["calls"], normalizer.complete.call_count), ("hint", [], 1))
        self.assertEqual(routes.run("A", "system", "show memory")["stage"], "grammar")
        self.assertEqual(normalizer.complete.call_count, 1)

    def test_an_engine_is_asked_in_place_of_the_cache(self):
        model = mock.Mock()
        model.complete.return_value = {"function_calls": [{"name": "arrange_panes", "arguments": {"layout": "stack"}}]}
        got = JR.Routes({}, {}, DESKTOP, engines={"panes": model}).run("A", "panes", "pile them up somehow")
        self.assertEqual((got["stage"], got["missing"], model.complete.call_count), ("model", False, 1))

    def test_the_pty_read_tool_never_ends_a_session(self):
        got = self.routes().run("A", "pty", "end session " + "ab" * 8)
        self.assertEqual((got["calls"], got["refused"]), ([], True))

    def test_the_stages_that_only_refuse_are_kept_in_b(self):
        got = self.routes().run("B", "panes", "help")
        self.assertEqual((got["stage"], got["calls"]), ("help", []))
        got = self.routes().run("B", "panes", "run in pane 301")
        self.assertEqual((got["stage"], got["refused"]), ("refused", True))

    def test_a_bundle_answer_meets_the_job_s_checks(self):
        routes = self.routes(tmux=Proposes([call("tmux_read", target="build:0.1", lines=40)]))
        got = routes.run("B", "tmux", "show me 40 lines of build:0.1")
        self.assertEqual((got["stage"], got["calls"]), ("bundle", [["read", {"target": "build:0.1", "lines": 40}]]))
        routes = self.routes(tmux=Proposes([call("tmux_read", target="a b")]))
        self.assertEqual(routes.run("B", "tmux", "show me a b")["calls"], [])
        # files: the grammar's own reading; in B also one query the file
        # collector's own checks take (the owner's eighth answer), in C not
        routes = self.routes(files=Proposes([call("search_text", text="TODO", scope="here")]))
        self.assertEqual(routes.run("B", "files", 'find text "TODO" in here')["calls"],
                         [["search_text", {"scope": "here", "text": "TODO"}]])
        self.assertEqual(routes.run("B", "files", "grep for TODO around here")["calls"],
                         [["search_text", {"scope": "here", "text": "TODO"}]])
        self.assertEqual(routes.run("C", "files", "grep for TODO around here")["calls"], [])
        routes = self.routes(files=Proposes([call("preview_file", path=".env", scope="here")]))
        self.assertEqual(routes.run("B", "files", "cat .env here")["calls"], [])
        routes = self.routes(files=Proposes([call("preview_file", path="a.md", scope="here"),
                                             call("preview_file", path="b.md", scope="here")]))
        self.assertEqual(routes.run("B", "files", "cat a.md and b.md here")["calls"], [])

    def test_each_job_s_refusal_of_a_whole_request_is_kept_in_every_arm(self):
        # a format character (right-to-left override): the files, tmux and pty
        # grammars refuse the request before they read it, so B does too
        routes = self.routes(files=Proposes([call("preview_file", path="src/app.py", scope="here")]),
                             tmux=Proposes([call("tmux_read", target="build:0.1")]))
        for arm in ("A", "B", "C"):
            self.assertEqual(routes.run(arm, "files", "cat src/app.py here\u202e")["calls"], [], arm)
            self.assertEqual(routes.run(arm, "tmux", "read build:0.1\x07")["calls"], [], arm)
        self.assertEqual(routes.run("B", "files", "cat src/app.py here")["calls"],
                         [["preview_file", {"scope": "here", "path": "src/app.py"}]])
        self.assertEqual(routes.run("B", "files", "help")["calls"], [])

    def test_in_b_a_system_answer_passes_the_normalizer_s_checks(self):
        import system_job
        routes = self.routes(system=Proposes([call("resources", kind="memory")]))
        self.assertEqual(routes.run("B", "system", "am I running low on memory?")["calls"],
                         [["resources", system_job.normalize("resources", {"kind": "memory"})]])
        self.assertEqual(routes.run("A", "system", "am I running low on memory?")["stage"], "hint")
        self.assertEqual(routes.run("C", "system", "am I running low on memory?")["calls"], [])
        # the preflight: a state change
        self.assertEqual(routes.run("B", "system", "restart nginx and show memory")["calls"], [])
        # grounded: the request does not ask for the disk
        routes = self.routes(system=Proposes([call("resources", kind="disk")]))
        self.assertEqual(routes.run("B", "system", "am I running low on memory?")["calls"], [])

    def test_a_bundle_s_read_of_a_pane_is_checked_against_the_desktop(self):
        routes = self.routes(panes=Proposes([call("read_pane", pane=301, lines=20)]))
        self.assertEqual(routes.run("B", "panes", "tail pane 301")["calls"], [["read_pane", {"pane": 301, "lines": 20}]])
        routes = self.routes(panes=Proposes([call("read_pane", pane=999)]))
        got = routes.run("B", "panes", "tail pane 999")
        self.assertEqual((got["calls"], got["refused"]), ([], True))

    def test_a_bundle_s_answer_the_panes_checks_refuse_goes_to_the_model(self):
        routes = self.routes(panes=Proposes([call("close_pane", pane="left")]))
        got = routes.run("B", "panes", "rename this tab to api")
        self.assertEqual((got["stage"], got["missing"]), ("model", True))


class SameAsTheRealRoutes(unittest.TestCase):
    """Arm A gives what the job's own route admits, in a plan."""

    def setUp(self):
        shutil.rmtree(history.directory(), ignore_errors=True)
        self.routes = JR.Routes({}, {}, DESKTOP)

    def test_files(self):
        import files_cli
        for request in ('find text "TODO" in here', 'find pdf files in Downloads modified last 3 days',
                        'preview "README.md" in research', "list largest files in Documents", "delete it"):
            record = files_cli.run(request, dry_run=True, cwd=HOME, home=HOME)
            mine = self.routes.run("A", "files", request)["calls"]
            self.assertEqual(mine, [[step["kind"], step["args"]] for step in record.get("plan") or []], request)

    def test_system(self):
        import system_dispatch

        def classify(_text):
            raise AssertionError("the model is not asked for what the grammar reads")

        for request in ("show memory", "top 5 processes by cpu", "is nginx running", "which package owns /usr/bin/jq",
                        "show errors since 2 hours ago"):
            record = system_dispatch.dispatch(request, classify, needle_cli.Options(dry_run=True, agent=True))
            mine = self.routes.run("A", "system", request)["calls"]
            self.assertEqual(mine, [[item["kind"], item["args"]] for item in record["items"] if "args" in item],
                             request)

    def test_apps(self):
        import apps
        for request in ("open the pdf viewer", "take me to the voice settings", "uninstall the pdf viewer"):
            record = needle_cli.run_apps_calls(request, apps.propose(request),
                                               needle_cli.Options(dry_run=True, agent=True), needle_cli._never)
            mine = self.routes.run("A", "apps", request)["calls"]
            self.assertEqual(mine, [[i["kind"], i["args"]] for i in record["items"] if i.get("outcome") == "would"],
                             request)

    def test_panes_without_the_model(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        before = os.environ.get("KILIX_NEEDLE_BUNDLE")
        os.environ["KILIX_NEEDLE_BUNDLE"] = "0"
        self.addCleanup(os.environ.__setitem__, "KILIX_NEEDLE_BUNDLE", before or "0")
        for request in ("read pane 301", "close pane 302", "go to tab 2", "split right and run htop", "list panes"):
            model = mock.Mock()
            model.complete.side_effect = AssertionError("the model is not asked")
            saved, sys.stdout = sys.stdout, io.StringIO()
            try:
                with FakeKilix(desktop()):
                    record = needle_cli.run_request(model, request, needle_cli.Options(dry_run=True, agent=True),
                                                    needle_cli._never)
            finally:
                sys.stdout = saved
            mine = self.routes.run("A", "panes", request)["calls"]
            items = [i for i in record["items"] if i.get("outcome") in ("would", "done")]
            if mine and mine[0][0] == "list":
                self.assertTrue(items and all(i["kind"].startswith("list_") for i in items), request)
                continue
            self.assertEqual([[i["kind"], i["args"]] for i in items], mine, request)


class TheCheckedInBundles(unittest.TestCase):
    """The bundles as checked in (job-bundles/README.md, "The files as
    checked in"), in arm B on the tests' desktop. The requests are made up."""

    ANSWERED = {
        ("files", "show me the contents of /etc/hosts"): [["preview_file", {"scope": "/etc", "path": "hosts"}]],
        ("files", "list the largest files in Documents"): [["list_files", {"scope": "Documents", "order": "size"}]],
        ("system", "am I low on memory"): [["resources", {"path": "/", "kind": "memory"}]],
        ("system", "is nginx running"): [["services", {"state": "all", "scope": "system", "unit": "nginx.service"}]],
        ("system", "which package owns /usr/bin/jq"): [["packages", {"operation": "owner", "target": "/usr/bin/jq"}]],
        ("agents", "wait until the claude session in ~/x finishes"): [["wait", {"session": "claude@~/x",
                                                                                "for": "idle"}]],
        ("apps", "take me to the voice settings"): [["settings", {"section": "voice"}]],
        ("tmux", "create session docs"): [["new", {"name": "docs"}]],
        ("tmux", "read %3"): [["read", {"target": "%3"}]],
        ("pty", "list pty sessions"): [["list", {}]],
        ("logs", "show the source of event ev-1"): [["source", {"event_id": "ev-1"}]],
        # a mark just inside a closing quote is the quoted text's own
        ("logs", 'search pane 301\'s log for "panic!"'): [["search", {"session": "301", "query": "panic!"}]],
        # risky, and asked for in a whole training clause
        ("agents", "start claude in ~/x"): [["agent", {"agent": "claude", "dir": "~/x"}]],
        ("apps", "open the pdf viewer"): [["launch", {"app": "kilix-pdf"}]],
    }
    # Nothing is answered: no tool of the job does it, or the job's checks
    # refuse what the bundle read (a hidden or parent path; "src" is no scope
    # the file collector resolves).
    NOT_ANSWERED = (("files", "delete src/app.py"), ("files", "show me .env"), ("files", "read ../secrets.txt"),
                    ("files", "search src for TODO"), ("system", "restart nginx"),
                    ("apps", "uninstall the pdf viewer"), ("tmux", "kill session docs"),
                    ("pty", "end pty session build"), ("logs", "delete the logs"),
                    ("agents", "tell me what claude is doing"))

    @classmethod
    def setUpClass(cls):
        cls.bundles = {}
        for job in job_schemas.JOBS:
            if not (PB.directory(job) / "manifest.json").is_file():
                raise unittest.SkipTest(f"no {job} bundle is checked in")
            cls.bundles[job] = PB.Bundle.load(PB.directory(job))
        cls.routes = JR.Routes(cls.bundles, {}, DESKTOP)

    def test_each_is_its_job_s_with_the_second_reader_s_kinds_of_experiment_11(self):
        for job, bundle in self.bundles.items():
            self.assertEqual(bundle.job, job)
            if job != "panes":
                self.assertEqual(set(bundle.tools), set(job_schemas.SCHEMAS[job]), job)
                self.assertEqual(bundle.second_kinds, frozenset({"wait"} if job == "agents" else ()), job)

    def test_each_was_written_by_this_reader_and_trainer_and_answers_its_rows_as_labelled(self):
        for job in self.bundles:
            manifest = json.loads((PB.directory(job) / "manifest.json").read_text())
            self.assertEqual(manifest["job"], job)
            self.assertEqual(sorted(manifest["code"]),
                             sorted(("panes_bundle.py", "panes_bundle_train.py") + PBT.ROW_CODE), job)
            for source in ("panes_bundle.py", "panes_bundle_train.py"):
                self.assertEqual(manifest["code"][source],
                                 hashlib.sha256((PBT.REPO / source).read_bytes()).hexdigest(), (job, source))
            for key in ("reads_its_rows", "reads_its_rows_with_the_second_reader"):
                own = manifest[key]
                self.assertEqual((own["as_labelled"], own["risky_and_not_labelled"]), (own["answered"], 0),
                                 (job, key))

    def test_what_they_answer_in_b(self):
        for (job, request), calls in self.ANSWERED.items():
            got = self.routes.run("B", job, request)
            self.assertEqual((got["stage"], got["calls"]), ("bundle", calls), (job, request))
        for job, request in self.NOT_ANSWERED:
            got = self.routes.run("B", job, request)
            self.assertEqual(got["calls"], [], (job, request))
            self.assertNotEqual(got["stage"], "bundle", (job, request))


class Scoring(unittest.TestCase):
    def test_a_file_is_compared_by_its_whole_path(self):
        cwd = HOME + "/gpu_terminal/ledger-api"
        label = JR.normalize("files", [["preview_file", {"scope": "here", "path": "README.md"}]], cwd=cwd)
        self.assertEqual(label, [["preview_file", {"file": cwd + "/README.md"}]])
        self.assertEqual(JR.normalize("files", [["preview_file", {"scope": cwd, "path": "README.md"}]], cwd=cwd), label)
        self.assertEqual(JR.normalize("files", [["search_text", {"scope": "research", "text": "x"}]]),
                         [["search_text", {"scope": HOME + "/research", "text": "x"}]])

    def test_a_pane_s_number_is_its_id(self):
        self.assertEqual(JR.normalize("panes", [["close_pane", {"pane": "70"}]]),
                         JR.normalize("panes", [["close_pane", {"pane": "id:70"}]]))

    def test_an_operation_is_its_name_and_its_arguments(self):
        self.assertEqual(JR.operations([{"operation": "read", "target": "x"}, {"session": "5"}]),
                         [["read", {"target": "x"}], ["events", {"session": "5"}]])

    def test_what_is_unsafe(self):
        for kind, args in (("close_pane", {}), ("run_in_pane", {}), ("open_tab", {"program": "htop"}), ("agent", {}),
                           ("tell", {}), ("launch", {}), ("send", {}), ("close", {}), ("kill", {})):
            self.assertTrue(JR.unsafe(kind, args), kind)
        for kind, args in (("open_tab", {}), ("read_pane", {}), ("wait", {}), ("settings", {}), ("read", {}),
                           ("preview_file", {}), ("journal", {})):
            self.assertFalse(JR.unsafe(kind, args), kind)


if __name__ == "__main__":
    unittest.main()
