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
            repo = home / "gpu_terminal" / "kilix-apps" / "kilix-needle"
            (repo / ".git").mkdir(parents=True)
            (home / "research").mkdir()
            self.assertEqual(agents_kilix.resolve_dir("the kilix-needle repo", home=home),
                             repo.resolve())
            self.assertEqual(agents_kilix.resolve_dir("here", cwd=str(home / "research"),
                                                      home=home), (home / "research").resolve())
            other = home / "research" / "kilix-needle"
            (other / ".git").mkdir(parents=True)
            with self.assertRaisesRegex(agents_kilix.AgentsError,
                                        rf"2 matching.*{repo}.*{other}"):
                agents_kilix.resolve_dir("kilix-needle", home=home)
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
        tell = agents.Action("tell", {"session": "codex@kilix", "text": "also run the suite"})
        for activity, outcome in (("working", "done"), ("idle", "done"), ("waiting", "failed")):
            panes = {"panes": [{"pane_id": 3, "activity": activity,
                                "broker": {"session_id": "c" * 16},
                                "coding_session": {"provider": "codex", "cwd": "/w/kilix"}}]}
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
