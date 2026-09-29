"""The agents job: its checks, its runner and its request flow."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401

import agents
import agents_kilix
import mcp_server
import needle_cli
from actions import Refusal

REPO = Path(__file__).resolve().parents[1]

# Reduced from `kilix panes list --json` on 2026-09-27. Titles are redacted;
# the nesting and broker shape are kept verbatim for the runner contract.
REAL_PANES_FIXTURE = {"schema": "kilix.panes/v1", "self_pane_id": 81, "panes": [{
    "pane_id": 7, "page": {"id": 6, "index": 4, "title": "<redacted>", "os_window_id": 1},
    "focused": False, "title": "<redacted>", "cwd": "/w/kilix", "activity": "idle",
    "doing": "<redacted>", "coding_session": {"provider": "codex", "state": "live",
                                                    "cwd": "/w/kilix"},
    "broker": {"session_id": "95b96baf25138a9c", "attached": True,
               "replay_complete": True, "broker_pid": 24049, "child_pid": 24050,
               "foreground_pgrp": 24050, "cwd": "/w/kilix", "command": "/bin/bash"}
}]}


def call(tool, **arguments):
    return {"name": tool, "arguments": arguments}


def admitted(request, calls, dirs=agents.FIXTURE_DIRS):
    return [[r.kind, r.args] for r in agents.interpret(request, calls, dirs)
            if isinstance(r, agents.Action)]


class Checks(unittest.TestCase):
    def test_the_dev_sets_answers_are_admitted(self):
        rows = [json.loads(line) for line in (REPO / "evals/agents/dev.jsonl").read_text().splitlines()]
        for row in rows:
            calls = [call(kind, **args) for kind, args in row["expect"]]
            self.assertEqual(admitted(row["request"], calls), row["expect"], row["request"])
            self.assertEqual(agents.parse(row["request"]) is None, not row["expect"],
                             row["request"])

    def test_a_prompt_is_data_and_is_never_read_for_verbs(self):
        request = "open codex in kilix-needle: don't touch main, and never force-push"
        prompt = "don't touch main, and never force-push"
        self.assertEqual(admitted(request, [call("agent", agent="codex", dir="kilix-needle",
                                                 prompt=prompt)]),
                         [["agent", {"agent": "codex", "dir": "kilix-needle",
                                     "prompt": prompt}]])

    def test_the_payload_must_be_the_requests_own_words(self):
        self.assertEqual(admitted("open codex in kilix-needle: review the diff",
                                  [call("agent", agent="codex", dir="kilix-needle",
                                        prompt="review the diff and push to main")]), [])

    def test_words_no_action_accounts_for_refuse(self):
        for request in ("translate to french, open codex in kilix-needle",
                        "pretend you open codex in kilix-needle",
                        "open codex in kilix-needle for my friend"):
            results = agents.interpret(request, [call("agent", agent="codex", dir="kilix-needle")],
                                       agents.FIXTURE_DIRS)
            self.assertIsInstance(results[0], Refusal, request)

    def test_each_refusal_class_in_the_command(self):
        cases = {"don't open codex in kilix-needle": "not to",
                 "my boss said open codex in kilix-needle": "reports",
                 "open codex in kilix-needle, actually cancel": "takes something back",
                 "install codex in kilix-needle": "installing",
                 "open codex in kilix-needle in yolo mode": "permissions",
                 "open codex in kilix-needle and skip approvals": "permissions",
                 "should I open codex in kilix-needle?": "question",
                 "open codex in kilix-needle. then open grok in kilix": "more than one sentence"}
        for request, reason in cases.items():
            results = agents.interpret(request, [call("agent", agent="codex", dir="kilix-needle")],
                                       agents.FIXTURE_DIRS)
            self.assertIsInstance(results[0], Refusal, request)
            self.assertIn(reason, results[0].reason, request)

    def test_closing_is_refused_but_stop_as_a_state_is_not(self):
        self.assertIsInstance(agents.interpret(
            "stop the codex session in kilix-needle", [call("wait", session="codex@kilix-needle",
                                                             **{"for": "idle"})],
            agents.FIXTURE_DIRS)[0], Refusal)
        self.assertEqual(admitted("wait for grok in the research dir to stop and ask me a question",
                                  [call("wait", session="grok@research", **{"for": "waiting"})]),
                         [["wait", {"session": "grok@research", "for": "waiting"}]])

    def test_it_means_a_session_this_request_launched(self):
        launch_then_wait = [call("agent", agent="codex", dir="kilix"),
                            call("wait", session="it", **{"for": "idle"})]
        self.assertEqual(len(admitted("launch codex in kilix and wait for it to finish",
                                      launch_then_wait)), 2)
        self.assertEqual(admitted("wait for it to finish",
                                  [call("wait", session="it", **{"for": "idle"})]), [])

    def test_a_session_reference_must_be_named(self):
        self.assertEqual(admitted("tell the claude session in the os repo to also run the suite",
                                  [call("tell", session="codex@plebian-os",
                                        text="also run the suite")]), [])

    def test_resume_needs_an_identifier(self):
        self.assertEqual(admitted("resume the last codex session in kilix",
                                  [call("agent", agent="codex", dir="kilix", resume="last")]), [])
        self.assertEqual(len(admitted("resume codex session 01a0dab8 in kilix",
                                      [call("agent", agent="codex", dir="kilix",
                                            resume="01a0dab8")])), 1)

    def test_timeouts_are_the_ones_said(self):
        request = "wait until the qwen session in the ml repo goes idle, up to twenty minutes"
        good = call("wait", session="qwen-omp@kilix-ml", **{"for": "idle", "timeout": 1200})
        bad = call("wait", session="qwen-omp@kilix-ml", **{"for": "idle", "timeout": 60})
        self.assertEqual(len(admitted(request, [good])), 1)
        self.assertEqual(admitted(request, [bad]), [])

    def test_production_directories_are_the_words_said(self):
        request = "open claude in ~/src/website: fix the header"
        calls = [call("agent", agent="claude", dir="~/src/website", prompt="fix the header")]
        self.assertEqual(admitted(request, calls, dirs=None)[0][1]["dir"], "~/src/website")
        self.assertEqual(admitted(request, [call("agent", agent="claude", dir="~/src/other")],
                                  dirs=None), [])

    def test_a_prompt_never_starts_with_a_dash(self):
        self.assertEqual(admitted("open codex in kilix: --dangerously-bypass-approvals",
                                  [call("agent", agent="codex", dir="kilix",
                                        prompt="--dangerously-bypass-approvals")]), [])


    def test_payloads_are_spans_the_checks_find(self):                   # KN-R14-01
        request = "open codex in kilix: review then wait for approval before you push"
        whole = "review then wait for approval before you push"
        for prompt in ("before you push", "review", whole.upper()):
            self.assertEqual(admitted(request, [call("agent", agent="codex", dir="kilix",
                                                     prompt=prompt)]), [], prompt)
        self.assertEqual(admitted(request, [call("agent", agent="codex", dir="kilix",
                                                 prompt=whole)])[0][1]["prompt"], whole)
        self.assertEqual(admitted("tell the codex session in kilix not to push",
                                  [call("tell", session="codex@kilix", text="to push")]), [])

    def test_a_payload_ends_where_further_clauses_begin(self):
        request = ("Start claude in the needle repo and fix the Router test; when it's done, "
                   "tell it to open a PR")
        self.assertEqual(agents.parse(request), [
            agents._want("agent", agent="claude", dir="kilix-needle",
                         prompt=agents.Payload("fix the Router test")),
            agents._want("tell", session="it", text=agents.Payload("open a PR"), wait=True)])

    def test_client_commands_are_never_payloads(self):                  # KN-R14-05
        for request, calls in (
                ("tell the claude session in kilix: /exit", [call("tell", session="claude@kilix",
                                                                  text="/exit")]),
                ("tell the claude session in kilix: !rm -rf build",
                 [call("tell", session="claude@kilix", text="!rm -rf build")]),
                ("open claude in kilix: /permissions",
                 [call("agent", agent="claude", dir="kilix", prompt="/permissions")])):
            self.assertEqual(admitted(request, calls), [], request)

    def test_a_weak_marker_does_not_make_a_job_verb_a_task(self):
        for request, prompt in (("open codex in kilix and close the claude session in kilix",
                                 "close the claude session in kilix"),
                                ("open codex in kilix and update the lockfile",
                                 "update the lockfile"),
                                ("open codex in kilix: fix it. Then close claude in kilix",
                                 "fix it. Then close claude in kilix")):
            self.assertEqual(admitted(request, [call("agent", agent="codex", dir="kilix",
                                                     prompt=prompt)]), [], request)

    def test_agents_and_directories_pair_within_a_clause(self):         # KN-R14-02
        request = "open claude in research and codex in plebian"
        swapped = [call("agent", agent="claude", dir="plebian"),
                   call("agent", agent="codex", dir="research")]
        right = [call("agent", agent="claude", dir="research"),
                 call("agent", agent="codex", dir="plebian")]
        self.assertEqual(admitted(request, swapped), [])
        self.assertEqual(len(admitted(request, right)), 2)

    def test_it_is_the_session_of_its_own_clause(self):                 # KN-R14-03
        request = ("open codex in research, then when the claude session in plebian is done "
                   "tell it to rebase")
        self.assertEqual(admitted(request, [call("agent", agent="codex", dir="research"),
                                            call("tell", session="it", text="rebase",
                                                 wait=True)]), [])
        self.assertEqual(len(admitted(request, [
            call("agent", agent="codex", dir="research"),
            call("tell", session="claude@plebian", text="rebase", wait=True)])), 2)

    def test_a_condition_can_not_be_dropped(self):                      # KN-R14-09
        self.assertEqual(admitted("when codex in kilix is done tell it to commit",
                                  [call("tell", session="codex@kilix", text="commit")]), [])
        self.assertEqual(admitted("open claude in kilix after codex in kilix is done",
                                  [call("agent", agent="claude", dir="kilix")]), [])

    def test_a_status_line_is_not_a_launch(self):                       # KN-R14-04
        for request in ("claude in kilix is done", "claude here is waiting for approval",
                        "put codex in kilix on hold", "get codex in kilix to stop"):
            self.assertEqual(admitted(request, [call("agent", agent="claude", dir="kilix")]),
                             [], request)

    def test_one_action_per_clause(self):
        self.assertEqual(admitted("open codex in kilix", [call("agent", agent="codex", dir="kilix"),
                                                         call("agent", agent="codex",
                                                              dir="kilix")]), [])

    def test_names_come_from_their_own_positions(self):                 # KN-R14-06/08/10
        self.assertEqual(admitted("don't open codex in kilix",
                                  [call("agent", agent="codex", dir="kilix", model="don't")]), [])
        self.assertEqual(admitted("open codex in kilix: continue the refactor",
                                  [call("agent", agent="codex", dir="kilix", resume="refactor")]),
                         [])
        self.assertEqual(admitted("wait until codex in kilix is done, 1.5 hours max",
                                  [call("wait", session="codex@kilix", **{"for": "idle",
                                                                          "timeout": 18000})]),
                         [])
        self.assertEqual(admitted("open codex in ~/src/kilix.new",
                                  [call("agent", agent="codex", dir="~/src/kilix")], dirs=None), [])
        self.assertEqual(admitted("open codex in ~/src/my project",
                                  [call("agent", agent="codex", dir="~/src/my")], dirs=None), [])


    def test_a_payload_is_sent_in_the_requests_own_characters(self):    # KN-R14-04
        request = "tell the codex session in kilix: rename FooBar in CHANGELOG.md, set \u201cDEBUG=1\u201d"
        sent = admitted(request, [call("tell", session="codex@kilix",
                                       text='rename FooBar in CHANGELOG.md, set "DEBUG=1"')])
        self.assertEqual(sent[0][1]["text"], "rename FooBar in CHANGELOG.md, set \u201cDEBUG=1\u201d")
        self.assertEqual(admitted(request, [call("tell", session="codex@kilix",
                                                 text="rename foobar in changelog.md, set "
                                                      '"debug=1"')]), [])

    def test_control_and_format_characters_refuse(self):
        for request in ("tell the codex session in kilix: push\nnow",
                        "tell the codex session in kilix: push \u202enow",
                        "tell the codex session in kilix: push\u2028now"):
            self.assertIsNone(agents.parse(request), repr(request))


    def test_it_is_the_latest_clauses_session(self):
        request = "Tell omp in the catalog to validate every URL, then wait for it to ask for input"
        self.assertEqual(len(admitted(request, [
            call("tell", session="qwen-omp@kilix-content", text="validate every URL"),
            call("wait", session="qwen-omp@kilix-content", **{"for": "waiting"})])), 2)
        self.assertIsNone(agents.parse("open claude in kilix and codex in research and wait "
                                       "for it to finish"))

    def test_waiting_then_telling_is_one_message_that_waits(self):
        request = "Wait until codex here is finished, then tell it to update the changelog entry"
        as_two = [call("wait", session="codex@here", **{"for": "idle"}),
                  call("tell", session="codex@here", text="update the changelog entry")]
        as_one = [call("tell", session="codex@here", text="update the changelog entry",
                       wait=True)]
        self.assertEqual(len(admitted(request, as_two)), 2)
        self.assertEqual(admitted(request, as_one),
                         [["tell", {"session": "here" and "codex@here",
                                    "text": "update the changelog entry", "wait": True}]])
        self.assertEqual(admitted(request, [call("tell", session="codex@here",
                                                 text="update the changelog entry")]), [])

    def test_more_ways_to_say_it(self):
        cases = {
            "In the ml repo, open codex cli with gpt-6 to compare the two tokenizer configs":
                [call("agent", agent="codex", dir="kilix-ml", model="gpt-6",
                      prompt="compare the two tokenizer configs")],
            "Resume the codex session titled sidebar cleanup in kilix 95 using gpt-6":
                [call("agent", agent="codex", dir="kilix-95", resume="sidebar cleanup",
                      model="gpt-6")],
            "Bring back Codex run a03d77cc in the current folder below me":
                [call("agent", agent="codex", dir="here", resume="a03d77cc", place="down")],
            "For at most 45 seconds, wait for qwen in research to need input":
                [call("wait", session="qwen-omp@research", **{"for": "waiting", "timeout": 45})],
            "Give the claude session in the os repo up to five minutes to become idle":
                [call("wait", session="claude@plebian-os", **{"for": "idle", "timeout": 300})],
            "Watch grok in the ml repo until it is waiting for approval":
                [call("wait", session="grok@kilix-ml", **{"for": "waiting"})],
            "Send this to Claude in the OS repo: keep the compatibility shim for now":
                [call("tell", session="claude@plebian-os",
                      text="keep the compatibility shim for now")],
            "After CC here finishes, send: add a regression test before wrapping up":
                [call("tell", session="claude@here", text="add a regression test before "
                      "wrapping up", wait=True)],
            "Spawn codex here to clean up the test fixture and grok here to review that fixture":
                [call("agent", agent="codex", dir="here", prompt="clean up the test fixture"),
                 call("agent", agent="grok", dir="here", prompt="review that fixture")],
        }
        for request, calls in cases.items():
            self.assertEqual(len(admitted(request, calls)), len(calls), request)


    def test_a_condition_in_a_payload_keeps_what_follows(self):
        # A conditional launch inside a message has no single reading either.
        self.assertIsNone(agents.parse("tell claude here: if tests fail, open codex in kilix"))
        # A later part that starts a launch has no single reading (R14 round 5).
        self.assertIsNone(agents.parse("tell claude here: review the diff, open codex in kilix"))
        # After a weak marker the conditional clause names another agent: no reading.
        self.assertIsNone(agents.parse("open codex in kilix to review, and if it fails, "
                                       "tell claude here to fix"))
        self.assertEqual(len(agents.parse("tell claude here: fix it; then open codex in kilix")),
                         2)


class ReviewR14(unittest.TestCase):
    """Review R14's attack rows (tests/data/agents-r14-rows.json), with the
    verdict each must get in the fixture and in production. Four rows are the
    reviewer's controls: their calls are the request's own reading."""

    ROWS = json.loads((REPO / "tests/data/agents-r14-rows.json").read_text())

    def test_each_row_gets_its_verdict(self):
        for row in self.ROWS:
            for mode, want in row["admit"].items():
                dirs = agents.FIXTURE_DIRS if mode == "fixture" else None
                results = agents.interpret(row["request"], row["calls"], dirs)
                got = bool(results) and all(isinstance(r, agents.Action) for r in results)
                self.assertEqual(got, want, f"{mode}: {row['request']} {row['calls']}")


class Runner(unittest.TestCase):
    def test_launch_argv_trusts_the_folder_and_follows_the_yolo_setting(self):
        action = agents.Action("agent", {"agent": "grok", "dir": "kilix", "place": "right",
                                         "model": "grok-4.6", "prompt": "review it"})
        argv = agents_kilix.launch_argv(action, Path("/w/kilix"), 7, "b" * 16)
        self.assertEqual(argv[:3], ["agent-control", "split", "7"])
        for flag in ("--trust-folder", "--coding-yolo"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--direction") + 1], "right")
        self.assertIn("--prompt=review it", argv)
        self.assertEqual(argv[argv.index("--cwd") + 1], "/w/kilix")

    def test_directories_resolve_to_exactly_one(self):
        with tempfile.TemporaryDirectory() as home:
            home = Path(home)
            repo = home / "gpu_terminal" / "kilix-apps" / "kilix-needle"
            (repo / ".git").mkdir(parents=True)
            shallow = home / "gpu_terminal" / "old" / "kilix-needle"
            (shallow / ".git").mkdir(parents=True)
            with self.assertRaises(agents_kilix.AgentsError) as raised:
                agents_kilix.resolve_dir("the kilix-needle repo", home=home)
            self.assertIn(str(repo), str(raised.exception))
            self.assertIn(str(shallow), str(raised.exception))
            (home / "research").mkdir()
            self.assertEqual(agents_kilix.resolve_dir("here", cwd=str(home / "research"),
                                                      home=home), (home / "research").resolve())
            other = home / "gpu_terminal" / "kilix-needle"
            (other / ".git").mkdir(parents=True)
            agents_kilix._DIR_SCAN_CACHE.clear()
            with self.assertRaises(agents_kilix.AgentsError) as raised:
                agents_kilix.resolve_dir("kilix-needle", home=home)
            for candidate in (repo, shallow, other):
                self.assertIn(str(candidate), str(raised.exception))
            with self.assertRaises(agents_kilix.AgentsError):
                agents_kilix.resolve_dir("nowhere", home=home)

    def test_directory_map_precedes_safe_git_roots_and_names_are_exact(self):
        with tempfile.TemporaryDirectory() as home_string:
            home = Path(home_string)
            mapped = home / "elsewhere" / "Needle"
            mapped.mkdir(parents=True)
            config = home / ".config" / "kilix-needle"
            config.mkdir(parents=True)
            (config / "dirs.json").write_text(json.dumps({"Needle": str(mapped)}))
            self.assertEqual(agents_kilix.resolve_dir("Needle", home=home), mapped.resolve())
            with self.assertRaises(agents_kilix.AgentsError):
                agents_kilix.resolve_dir("needle", home=home)

            for unsafe in (home / "scratch-workers" / "repo",
                           home / "research" / ".hidden" / "repo"):
                (unsafe / ".git").mkdir(parents=True)
            outside = home / "outside"
            (outside / "repo" / ".git").mkdir(parents=True)
            (home / "gpu_terminal").mkdir(exist_ok=True)
            (home / "gpu_terminal" / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(agents_kilix.AgentsError, "no matching"):
                agents_kilix.resolve_dir("repo", home=home)

    def test_here_requires_a_known_calling_directory(self):
        with self.assertRaisesRegex(agents_kilix.AgentsError, "calling pane"):
            agents_kilix.resolve_dir("here", cwd=None)

    def test_a_session_is_exactly_one_live_agent_pane_there(self):
        panes = {"panes": [
            {"pane_id": 3, "activity": "idle", "broker": "c" * 16, "cwd": "/w/kilix",
             "coding_session": {"provider": "codex", "cwd": "/w/kilix"}},
            {"pane_id": 4, "activity": "working", "broker": "d" * 16, "cwd": "/w/kilix",
             "coding_session": {"provider": "omp", "cwd": "/w/kilix"}}]}
        def run(argv, timeout=30):
            if argv[:2] == ["agent-control", "list"]:
                return json.dumps({"caller_pane": 1, "panes": [
                    {"pane_id": 1, "broker": "a" * 16, "cwd": "/w/other"}]})
            return json.dumps(panes)
        with mock.patch.object(agents_kilix, "_run", side_effect=run):
            self.assertEqual(agents_kilix.find_session("codex", Path("/w/kilix"))["pane_id"], 3)
            self.assertEqual(agents_kilix.find_session("qwen-omp", Path("/w/kilix"))["pane_id"], 4)
            with self.assertRaises(agents_kilix.AgentsError):
                agents_kilix.find_session("claude", Path("/w/kilix"))

    def run_actions(self, actions, panes, *, cwd="/w", dry_run=False, launch_reply=None):
        sent = []

        def run(argv, timeout=30):
            sent.append(argv)
            if argv[:2] == ["panes", "list"]:
                return json.dumps(panes)
            if argv[:2] == ["agent-control", "list"]:
                return json.dumps({"caller_pane": 1, "panes": [
                    {"pane_id": 1, "broker": "a" * 16, "cwd": cwd}]})
            if argv[0] == "agent-control" and argv[1] in ("new-tab", "split"):
                return launch_reply if launch_reply is not None else json.dumps(
                    {"pane": {"pane_id": 9}, "folder_trust": "trusted"})
            return "{}"
        with mock.patch.object(agents_kilix, "_run", side_effect=run), \
                mock.patch.object(agents_kilix, "resolve_dir", return_value=Path("/w/kilix")):
            return agents_kilix.perform(actions, cwd=cwd, dry_run=dry_run), sent

    def test_steering_a_working_session_is_allowed_but_an_approval_holds(self):
        tell = agents.Action("tell", {"session": "claude@kilix", "text": "also run the suite"})
        for activity, outcome in (("working", "done"), ("idle", "done"), ("waiting", "failed")):
            panes = {"panes": [{"pane_id": 3, "activity": activity,
                                "broker": {"session_id": "c" * 16},
                                "coding_session": {"provider": "claude", "cwd": "/w/kilix"}}]}
            results, sent = self.run_actions([tell], panes)
            self.assertEqual(results[0]["outcome"], outcome, activity)
            delivered = [a for a in sent if a[:2] == ["agent-control", "send"]]
            self.assertEqual(bool(delivered), outcome == "done", activity)

    def test_unknown_states_and_a_busy_omp_hold_the_message(self):               # KX-R13-16
        tell = agents.Action("tell", {"session": "qwen-omp@kilix", "text": "also run the suite"})
        for provider, activity, outcome in (("omp", "working", "failed"), ("omp", "idle", "done"),
                                            ("codex", "unknown", "failed"), ("codex", "agent", "failed"),
                                            ("codex", "shell", "failed"), ("codex", "running", "failed"),
                                            ("codex", "remote", "failed"), ("codex", "waiting", "failed")):
            panes = {"panes": [{"pane_id": 3, "activity": activity, "broker": "c" * 16,
                                "coding_session": {"provider": provider, "cwd": "/w/kilix"}}]}
            action = tell if provider == "omp" else agents.Action(
                "tell", {"session": "codex@kilix", "text": "x y"})
            results, _ = self.run_actions([action], panes)
            self.assertEqual(results[0]["outcome"], outcome, (provider, activity))

    def test_real_panes_shape_supplies_the_broker_session_id(self):
        tell = agents.Action("tell", {"session": "codex@kilix", "text": "run the suite"})
        results, sent = self.run_actions([tell], REAL_PANES_FIXTURE)
        self.assertEqual(results[0]["outcome"], "done")
        send = next(argv for argv in sent if argv[:2] == ["agent-control", "send"])
        self.assertEqual(send[send.index("--expect-broker") + 1], "95b96baf25138a9c")

    def test_it_is_the_pane_this_request_launched(self):
        actions = [agents.Action("agent", {"agent": "codex", "dir": "kilix"}),
                   agents.Action("wait", {"session": "it", "for": "idle"})]
        results, sent = self.run_actions(actions, {"panes": []})
        self.assertEqual([r["outcome"] for r in results], ["done", "done"])
        self.assertEqual(sent[-1][:3], ["panes", "wait", "9"])

    def test_plan_resolves_it_to_the_session_it_would_launch(self):
        actions = [agents.Action("agent", {"agent": "codex", "dir": "kilix"}),
                   agents.Action("wait", {"session": "it", "for": "idle"}),
                   agents.Action("tell", {"session": "it", "text": "run tests", "wait": True})]
        results, sent = self.run_actions(actions, {"panes": []}, dry_run=True)
        self.assertEqual([r["outcome"] for r in results], ["would", "would", "would"])
        self.assertFalse(any(argv[:2] == ["agent-control", "send"] for argv in sent))

    def test_qwen_it_launch_needs_an_idle_wait_before_a_message(self):
        launch = agents.Action("agent", {"agent": "qwen-omp", "dir": "kilix"})
        tell = agents.Action("tell", {"session": "it", "text": "run tests"})
        results, _ = self.run_actions([launch, tell], {"panes": []}, dry_run=True)
        self.assertEqual([r["outcome"] for r in results], ["would", "failed"])

    def test_every_wait_passes_an_explicit_timeout(self):
        wait = agents.Action("wait", {"session": "codex@kilix", "for": "idle"})
        results, _ = self.run_actions([wait], REAL_PANES_FIXTURE, dry_run=True)
        argv = results[0]["argv"]
        self.assertEqual(argv[argv.index("--timeout") + 1], "3600")
        wait = agents.Action("wait", {"session": "codex@kilix", "for": "idle", "timeout": 90})
        results, _ = self.run_actions([wait], REAL_PANES_FIXTURE, dry_run=True)
        self.assertEqual(results[0]["argv"][results[0]["argv"].index("--timeout") + 1], "90")
        tell_wait = agents.Action("tell", {"session": "codex@kilix", "text": "go", "wait": True})
        _results, sent = self.run_actions([tell_wait], REAL_PANES_FIXTURE)
        wait_argv = next(argv for argv in sent if argv[:2] == ["panes", "wait"])
        self.assertEqual(wait_argv[wait_argv.index("--timeout") + 1], "3600")

    def test_find_session_excludes_the_callers_own_pane(self):
        panes = {"panes": [dict(REAL_PANES_FIXTURE["panes"][0], pane_id=1)]}
        with mock.patch.object(agents_kilix, "_run", return_value=json.dumps(panes)):
            with self.assertRaisesRegex(agents_kilix.AgentsError, "no live"):
                agents_kilix.find_session("codex", Path("/w/kilix"), caller_pane=1)

    def test_bad_launch_shapes_and_json_are_failed_steps(self):
        launch = agents.Action("agent", {"agent": "codex", "dir": "kilix"})
        for reply in (json.dumps({"pane": None}), "not json", {"pane": {"pane_id": 9}}):
            results, _ = self.run_actions([launch], {"panes": []}, launch_reply=reply)
            self.assertEqual(results[0]["outcome"], "failed", reply)

    def test_a_failure_stops_what_follows(self):
        actions = [agents.Action("tell", {"session": "codex@kilix", "text": "x y"}),
                   agents.Action("agent", {"agent": "codex", "dir": "kilix"})]
        results, sent = self.run_actions(actions, {"panes": []})
        self.assertEqual([r["outcome"] for r in results], ["failed"])


class Flow(unittest.TestCase):
    def test_a_refusal_runs_nothing(self):
        with mock.patch.object(agents_kilix, "perform") as perform:
            record = needle_cli.run_agents_calls(
                "open codex in kilix-needle and tell the grok session in kilix-content to stop it",
                [call("agent", agent="codex", dir="kilix-needle")], needle_cli.Options())
        perform.assert_not_called()
        self.assertEqual(record["status"], 1)

    def test_an_admitted_request_runs_with_no_second_yes(self):
        with mock.patch.object(agents_kilix, "perform", return_value=[
                {"kind": "agent", "args": {"agent": "codex", "dir": "kilix-needle"},
                 "outcome": "done"}]) as perform:
            record = needle_cli.run_agents_calls(
                "open codex in kilix-needle", [call("agent", agent="codex", dir="kilix-needle")],
                needle_cli.Options(agent=True))
        perform.assert_called_once()
        self.assertEqual((record["status"], record["items"][0]["outcome"]), (0, "done"))


class Mcp(unittest.TestCase):
    def test_the_agents_tools_use_the_agents_jobs_engine(self):
        made = []

        def factory(job="panes"):
            made.append(job)
            runtime = mock.MagicMock()
            runtime.complete.return_value = {"function_calls": []}
            return runtime
        server = mcp_server.Server(factory)
        with mock.patch.object(needle_cli, "run_agents_request",
                               return_value={"request": "x", "status": 0, "note": "",
                                             "items": []}) as run, \
                mock.patch.object(agents_kilix, "calling_cwd", return_value="/caller/repo"):
            server.call_tool("kilix_agents_plan", {"request": "open codex here"})
            self.assertTrue(run.call_args.args[2].dry_run)
            self.assertEqual(run.call_args.kwargs["cwd"], "/caller/repo")
            server.call_tool("kilix_agents_act", {"request": "open codex in kilix-needle"})
            self.assertFalse(run.call_args.args[2].dry_run)
        self.assertEqual(made, ["agents"])

    def test_unknown_mcp_caller_does_not_substitute_the_server_cwd_for_here(self):
        runtime = mock.MagicMock()
        runtime.complete.return_value = {"function_calls": [
            call("agent", agent="codex", dir="here")]}
        server = mcp_server.Server(lambda job="panes": runtime)
        with mock.patch.object(agents_kilix, "calling_cwd",
                               side_effect=agents_kilix.AgentsError("unknown")), \
                mock.patch.object(agents_kilix, "caller_info",
                                  side_effect=agents_kilix.AgentsError("unknown")):
            result = server.call_tool("kilix_agents_plan", {"request": "open codex here"})
        self.assertEqual(result["structuredContent"]["status"], 1)
        self.assertIn("calling pane", result["structuredContent"]["items"][0]["reason"])


if __name__ == "__main__":
    unittest.main()


class ReviewR14Round2(unittest.TestCase):
    """Review R14 round 2's attack rows and its distinguishing inputs
    (tests/data/agents-r14r2-rows.json), each with the verdict it must get.
    Every admitted row was checked by hand as the request's one reading."""

    ROWS = json.loads((REPO / "tests/data/agents-r14r2-rows.json").read_text())

    def test_each_row_gets_its_verdict(self):
        for row in self.ROWS:
            dirs = agents.FIXTURE_DIRS if row["mode"] == "fixture" else None
            results = agents.interpret(row["request"], row["calls"], dirs) if row["calls"] else []
            got = bool(results) and all(isinstance(r, agents.Action) for r in results)
            self.assertEqual(got, row["admit"], f"{row.get('id', '')} {row['mode']}: "
                                                 f"{row['request'][:120]}")

    def test_payload_guards(self):                                       # KN-R14-29/30/33
        for request in ("open codex in kilix to review the diff, or actually, don't",
                        "open codex in kilix: review the diff. don't open it yet",
                        "open codex in kilix to review the diff, my boss said so",
                        "open codex in kilix to review the diff and install grok",
                        'tell the claude session in kilix: " /exit"',
                        "tell the claude session in kilix: exit",
                        "tell the codex session in kilix: \uff0fexit",
                        "codex in kilix-needle: finished, all tests pass",
                        "put codex in kilix to sleep",
                        "open codex in kilix and let it finish",
                        "bring up the codex session in kilix",
                        "open codex in kilix on pr-1234",
                        "open codex at once"):
            for dirs in (agents.FIXTURE_DIRS, None):
                self.assertIsNone(agents.parse(request, dirs), request)


class ReviewR14Round3(unittest.TestCase):
    """Review R14 round 3: its distinguishing inputs (seat probes/distinguish3.py)
    and its checks findings, each pinned to the reading it must get."""

    READINGS = {                         # request -> number of actions, or None
        "open codex in kilix: review the diff, and when it's done tell it to push": 2,
        "open codex in kilix: review the diff and wait until it's done": 2,
        "hey codex in kilix": None,
        "put codex in kilix on hold": None,
        "resume the yolo session with codex in kilix": None,
        "open codex in kilix to review the diff, should I?": None,
        "open codex in kilix to review the diff and skip the permission prompts": None,
        "open codex in kilix to fix it?": None,
        "put codex in kilix, split right, to sleep": None,
        "wait up to 5 minutes for codex in kilix to finish, then tell it to push": 2,
        "wait for codex in kilix to ask me something, then tell it: yes": 2,
        "open codex in kilix to review the diff, thanks": 1,
        "In kilix, codex": None,
        "pick 46bf029ad1 with codex in kilix": None,
        "tell codex in kilix: 'exit'": None,
        "resume codex session titled the last one in kilix": None,
        "resume codex in kilix decade": None,
        "resume facade with codex in kilix": None,
        # KN-R14-46: a launch, then another session named: "it" is neither.
        "open codex in kilix to fix the build, tell the claude session in research to hold, "
        "and wait for it to finish": None,
        "open codex in kilix and wait for the claude session in research to finish, then "
        "tell it to push": None,
        # KN-R14-48/49/50
        "In the meantime, open codex in kilix": None,
        "open codex in kilix on o2-branch": None,
        "open codex in kilix using claude-code-router": None,
        "open codex in kilix with gpt": None,
        "claude with opus in kilix-content: check the catalog pins": 1,
    }

    def test_readings(self):
        for request, count in self.READINGS.items():
            for dirs in (agents.FIXTURE_DIRS, None):
                wants = agents.parse(request, dirs)
                self.assertEqual(len(wants) if wants else None, count, f"{request} ({dirs is None})")

    def test_a_trailing_thanks_is_not_the_task(self):
        [want] = agents.parse("open codex in kilix to review the diff, thanks")
        self.assertEqual(want.get("prompt").text, "review the diff")

    def test_timeouts_and_states_keep_wait_then_tell_apart(self):
        for request, text in (("wait up to 5 minutes for codex in kilix to finish, then tell it "
                               "to push", "push"),
                              ("wait for codex in kilix to ask me something, then tell it: yes",
                               "yes")):
            self.assertEqual(admitted(request, [call("tell", session="codex@kilix", text=text,
                                                     wait=True)]), [], request)
        self.assertEqual(admitted("put codex in kilix", [call("agent", agent="codex",
                                                               dir="kilix")]), [])
        self.assertEqual(admitted("open codex in kilix: review the diff, or should we wait",
                                  [call("agent", agent="codex", dir="kilix",
                                        prompt="review the diff, or should we wait")]), [])


class ReviewR14Round4(unittest.TestCase):
    """Review R14 round 4: waits and conditions never hide in a payload (53),
    plain messages that mention waiting stay messages (57), no stray "and
    then" (60), and N19's close/install guard."""

    READINGS = {
        "open codex in kilix: review the diff. Wait for it to finish.": None,
        "open codex in kilix: review the diff. When it's done, tell it to push": None,
        "open codex in kilix: review the diff and let me know when it finishes": None,
        "open codex in kilix to review the diff, and also wait for it to finish": 2,
        "tell codex in kilix to rebase and after that run the suite": 1,
        "tell codex in kilix to fix the tests, then wait for CI": 1,
        "open codex in kilix to review the diff and then, when it's done, tell it to push": 2,
        "open codex in kilix: uninstall grok": None,
    }

    def test_readings(self):
        for request, count in self.READINGS.items():
            wants = agents.parse(request)
            self.assertEqual(len(wants) if wants else None, count, request)

    def test_no_stray_connective_in_the_prompt(self):
        wants = agents.parse("open codex in kilix to review the diff and then, when it's done, "
                             "tell it to push")
        self.assertEqual(wants[0].get("prompt").text, "review the diff")


class ReviewR14Round5(unittest.TestCase):
    """Review R14 round 5 (KN-R14-61/62/63): clause heads from the grammar's own
    tables never hide in a payload; plain task text stays a payload."""

    READINGS = {
        "open codex in kilix: review the diff and hold until it's done": None,
        "open codex in kilix: review the diff and sit tight": None,
        "open codex in kilix: review the diff and watch it": None,
        "open codex in kilix: review the diff; once done, tell it to push": None,
        "open codex in kilix: review the diff, and when finished tell it to push": None,
        "open codex in kilix: review the diff. After that, tell it to push.": None,
        "open codex in kilix: review the diff when the review is done tell it to push": None,
        "open codex in kilix to review the diff, give it five minutes, then tell it to push": None,
        "open codex in kilix to review the diff, wait a bit, then tell it to push": None,
        "tell the claude session in research: summarize; when that's done, tell codex in kilix "
        "to merge": None,
        "open codex in kilix: review the diff, and open claude in research": None,     # N02
        "open codex in kilix: review it. then open claude in research to help": None,  # M46
        "open codex in kilix: add a retry, and when it times out log the error": 1,
        "tell codex in kilix to fix the tests, then wait for CI": 1,
        "tell codex in kilix to rebase and after that run the suite": 1,
        # M46: lead words don't hide a controlling verb after a weak marker.
        "open codex in kilix and just stop it": None,
        "open codex in kilix to please stop": None,
    }

    def test_readings(self):
        for request, count in self.READINGS.items():
            wants = agents.parse(request)
            self.assertEqual(len(wants) if wants else None, count, request)


class ReviewR14Round6(unittest.TestCase):
    """Review R14 round 6 (KN-R14-65..69): a payload cut before a later clause
    holds no wait, condition or timed wait at any word; plain task text that
    mentions waiting stays a payload when nothing follows it."""

    READINGS = {
        "tell the codex session in kilix: run the migration (wait for it to finish); then tell it: "
        "deploy": None,
        "open codex in kilix: review the diff (wait for it to finish); then tell it to push": None,
        "open codex in kilix to review the diff, wait a bit, then tell it to push": None,
        "open codex in kilix: review the diff, pause until it finishes": None,
        "open codex in kilix: review the diff - then wait for it to finish": 2,
        "open codex in kilix: add a lock, and block a second writer": 1,
        "open codex in kilix: wait 30 seconds between retries": 1,
        "open codex in kilix: give the session tokens a TTL": 1,
    }

    def test_readings(self):
        for request, count in self.READINGS.items():
            wants = agents.parse(request)
            self.assertEqual(len(wants) if wants else None, count, request)

    def test_no_stray_dash(self):
        wants = agents.parse("open codex in kilix: review the diff - then wait for it to finish")
        self.assertEqual(wants[0].get("prompt").text, "review the diff")


class ReviewR14Round7(unittest.TestCase):
    """Review R14 round 7: one scan per request (70), and a wait or condition on
    a session anywhere in a payload that reaches the end refuses (71)."""

    READINGS = {
        "open codex in kilix: review the diff (and wait for it to finish)": None,
        "open codex in kilix: review the diff; but wait for it to finish": None,
        "open codex in kilix: wait for it to finish": None,
        "open codex in kilix: review & wait for it to finish": None,
        "open codex in kilix to review the diff and wait for the reviewer to finish": None,
        "open codex in kilix to review the diff and wait for it to finish": 2,
        "tell codex in kilix to fix the tests, then wait for CI": 1,
        "open codex in kilix: wait 30 seconds between retries": 1,
    }

    def test_readings(self):
        for request, count in self.READINGS.items():
            wants = agents.parse(request)
            self.assertEqual(len(wants) if wants else None, count, request)

    def test_a_long_crafted_request_parses_quickly(self):
        import time
        tail = "zz. open codex in kilix"
        for unit in ("tell codex in kilix: wait x and ", "tell codex in kilix to sleep x then ",
                     "tell codex in kilix: " + "a; " * 30):
            request = (unit * (agents.MAX_REQUEST // len(unit) + 1))[:agents.MAX_REQUEST - len(tail)]
            started = time.process_time()
            agents.parse(request + tail)
            self.assertLess(time.process_time() - started, 5.0, unit)


class ReviewR14Round8(unittest.TestCase):
    """Review R14 round 8 (KN-R14-74/75): a wait "until" something that isn't a
    session is task text; "the tab/pane" name the session."""

    READINGS = {
        "open codex in kilix: make the retry loop wait until the socket is ready": 1,
        "open codex in kilix: the CLI should block until the lock is free": 1,
        "open codex in kilix to review the diff and wait for the tab to finish": None,
        "open codex in kilix: review the diff and wait until codex is done": None,
        "open codex in kilix: review the diff and hold until it's done": None,
        # KN-R14-77/78: a wait until the turn ends, and a long wait phrase before a tell.
        "open codex in kilix: review the diff and wait until done": None,
        "open codex in kilix: review the diff and wait until the review is complete": None,
        "open codex in kilix to review the diff, wait for the extremely thorough and careful and "
        "complete and exhaustive review of every single changed file by it, then tell it to push":
            None,
    }

    def test_readings(self):
        for request, count in self.READINGS.items():
            wants = agents.parse(request)
            self.assertEqual(len(wants) if wants else None, count, request)


class RouteBenchmarkLaunches(unittest.TestCase):
    """How agents asked for a launch in the Codex route benchmark (2026-09-29)."""

    def test_agent_wordings_of_a_launch_read_as_the_canonical_form(self):
        want = [agents.Want("agent", (("agent", "codex"), ("dir", "/tmp/w1")))]
        for request in (
                "Start an interactive Codex coding-agent session in a new tab, working in the "
                "directory /tmp/w1. Do not give it a task.",
                "start a new codex session in /tmp/w1 with no task",
                "start codex in /tmp/w1, no prompt",
                "launch codex in the directory /tmp/w1. Give the agent no task."):
            with self.subTest(request=request):
                self.assertEqual(agents.parse(request, None), want)

    def test_other_negations_and_existing_sessions_still_refuse(self):
        for request in ("don't start codex in /tmp/w1", "start codex in /tmp/w1. Do not.",
                        "start codex in /tmp/w1 and do not close tab 2",
                        "bring up the codex session in kilix"):
            with self.subTest(request=request):
                self.assertIsNone(agents.parse(request, None))

    def test_the_same_path_however_the_model_words_it(self):
        request = "start codex in /tmp/w1"
        for value, ok in (("/tmp/w1", True), ("/tmp/w1/", True), ("the directory /tmp/w1", True),
                          ("/tmp/w2", False), ("/tmp", False)):
            with self.subTest(value=value):
                got = agents.interpret(request, [{"name": "agent",
                                                  "arguments": {"agent": "codex", "dir": value}}], None)
                self.assertEqual(not isinstance(got[0], Refusal), ok)
