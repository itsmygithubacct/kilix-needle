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
            want = [[k, {key: agents._fold(v) if key in ("prompt", "text", "resume", "model")
                         and isinstance(v, str) else v for key, v in a.items()}]
                    for k, a in row["expect"]]
            self.assertEqual(admitted(row["request"], calls), want, row["request"])

    def test_a_prompt_is_data_and_is_never_read_for_verbs(self):
        request = "open codex in kilix-needle: don't touch main, and never force-push"
        prompt = "don't touch main, and never force-push"
        self.assertEqual(admitted(request, [call("agent", agent="codex", dir="kilix-needle",
                                                 prompt=prompt)]),
                         [["agent", {"agent": "codex", "dir": "kilix-needle",
                                     "prompt": agents._fold(prompt)}]])

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
            (home / "gpu_terminal" / "kilix-apps" / "kilix-needle").mkdir(parents=True)
            (home / "research").mkdir()
            self.assertEqual(agents_kilix.resolve_dir("the kilix-needle repo", home=home),
                             (home / "gpu_terminal" / "kilix-apps" / "kilix-needle").resolve())
            self.assertEqual(agents_kilix.resolve_dir("here", cwd=str(home / "research"),
                                                      home=home), (home / "research").resolve())
            (home / "src" / "kilix-needle").mkdir(parents=True)
            with self.assertRaisesRegex(agents_kilix.AgentsError, "2 matching"):
                agents_kilix.resolve_dir("kilix-needle", home=home)
            with self.assertRaises(agents_kilix.AgentsError):
                agents_kilix.resolve_dir("nowhere", home=home)

    def test_a_session_is_exactly_one_live_agent_pane_there(self):
        panes = {"panes": [
            {"pane_id": 3, "activity": "idle", "broker": "c" * 16, "cwd": "/w/kilix",
             "coding_session": {"provider": "codex", "cwd": "/w/kilix"}},
            {"pane_id": 4, "activity": "working", "broker": "d" * 16, "cwd": "/w/kilix",
             "coding_session": {"provider": "omp", "cwd": "/w/kilix"}}]}
        with mock.patch.object(agents_kilix, "_run", return_value=json.dumps(panes)):
            self.assertEqual(agents_kilix.find_session("codex", Path("/w/kilix"))["pane_id"], 3)
            self.assertEqual(agents_kilix.find_session("qwen-omp", Path("/w/kilix"))["pane_id"], 4)
            with self.assertRaises(agents_kilix.AgentsError):
                agents_kilix.find_session("claude", Path("/w/kilix"))

    def run_actions(self, actions, panes, *, cwd="/w"):
        sent = []

        def run(argv, timeout=30):
            sent.append(argv)
            if argv[:2] == ["panes", "list"]:
                return json.dumps(panes)
            if argv[:2] == ["agent-control", "list"]:
                return json.dumps({"caller_pane": 1, "panes": [{"pane_id": 1, "broker": "a" * 16}]})
            if argv[0] == "agent-control" and argv[1] in ("new-tab", "split"):
                return json.dumps({"pane": {"pane_id": 9}, "folder_trust": "trusted"})
            return "{}"
        with mock.patch.object(agents_kilix, "_run", side_effect=run), \
                mock.patch.object(agents_kilix, "resolve_dir", return_value=Path("/w/kilix")):
            return agents_kilix.perform(actions, cwd=cwd), sent

    def test_steering_a_working_session_is_allowed_but_an_approval_holds(self):
        tell = agents.Action("tell", {"session": "codex@kilix", "text": "also run the suite"})
        for activity, outcome in (("working", "done"), ("idle", "done"), ("waiting", "failed")):
            panes = {"panes": [{"pane_id": 3, "activity": activity, "broker": "c" * 16,
                                "coding_session": {"provider": "codex", "cwd": "/w/kilix"}}]}
            results, sent = self.run_actions([tell], panes)
            self.assertEqual(results[0]["outcome"], outcome, activity)
            delivered = [a for a in sent if a[:2] == ["agent-control", "send"]]
            self.assertEqual(bool(delivered), outcome == "done", activity)

    def test_it_is_the_pane_this_request_launched(self):
        actions = [agents.Action("agent", {"agent": "codex", "dir": "kilix"}),
                   agents.Action("wait", {"session": "it", "for": "idle"})]
        results, sent = self.run_actions(actions, {"panes": []})
        self.assertEqual([r["outcome"] for r in results], ["done", "done"])
        self.assertEqual(sent[-1][:3], ["panes", "wait", "9"])

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
                                             "items": []}) as run:
            server.call_tool("kilix_agents_plan", {"request": "open codex in kilix-needle"})
            self.assertTrue(run.call_args.args[2].dry_run)
            server.call_tool("kilix_agents_act", {"request": "open codex in kilix-needle"})
            self.assertFalse(run.call_args.args[2].dry_run)
        self.assertEqual(made, ["agents"])


if __name__ == "__main__":
    unittest.main()
