"""Round 2 runner regressions for the coding-agents job."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401

import agents
import agents_kilix
import needle_cli


class DirectoryResolution(unittest.TestCase):
    def setUp(self):
        agents_kilix._DIR_SCAN_CACHE.clear()
        self.scratch = tempfile.TemporaryDirectory()
        self.home = Path(self.scratch.name)

    def tearDown(self):
        self.scratch.cleanup()

    def repo(self, relative: str) -> Path:
        path = self.home / relative
        (path / ".git").mkdir(parents=True)
        return path

    def test_resolution_order_includes_explicit_here_and_the_map(self):
        explicit = self.home / "explicit"
        explicit.mkdir()
        here = self.home / "calling"
        here.mkdir()
        mapped = self.home / "elsewhere" / "mapped"
        mapped.mkdir(parents=True)
        aliased = self.home / "elsewhere" / "plebian-os"
        aliased.mkdir()
        config = self.home / ".config" / "kilix-needle"
        config.mkdir(parents=True)
        (config / "dirs.json").write_text(json.dumps({
            "Needle": str(mapped), "the os repo": str(aliased)}))

        self.assertEqual(agents_kilix.resolve_dir("~/explicit", home=self.home), explicit)
        self.assertEqual(agents_kilix.resolve_dir("here", cwd=str(here), home=self.home), here)
        self.assertEqual(agents_kilix.resolve_dir("Needle", home=self.home), mapped)
        self.assertEqual(agents_kilix.resolve_dir("the os repo", home=self.home), aliased)

    def test_a_duplicate_at_any_scanned_depth_is_ambiguous(self):
        shallow = self.repo("gpu_terminal/team/widget")
        deep = self.repo("gpu_terminal/archive/old/widget")
        with self.assertRaises(agents_kilix.AgentsError) as raised:
            agents_kilix.resolve_dir("widget", home=self.home)
        self.assertIn(str(shallow), str(raised.exception))
        self.assertIn(str(deep), str(raised.exception))

    def test_ambiguity_lists_every_candidate_and_the_map_hint(self):
        first = self.repo("gpu_terminal/a/widget")
        second = self.repo("gpu_terminal/b/widget")
        deep = self.repo("gpu_terminal/archive/old/widget")
        with self.assertRaises(agents_kilix.AgentsError) as raised:
            agents_kilix.resolve_dir("widget", home=self.home)
        message = str(raised.exception)
        self.assertIn(str(first), message)
        self.assertIn(str(second), message)
        self.assertIn(str(deep), message)
        self.assertIn("~/.config/kilix-needle/dirs.json", message)

    def test_scan_is_bounded_and_skips_non_top_level_and_work_directories(self):
        fake = self.home / "gpu_terminal" / "fake"
        fake.mkdir(parents=True)
        (fake / ".git").write_text("gitdir: elsewhere")
        self.repo("gpu_terminal/node_modules/pkg")
        self.repo("gpu_terminal/scratch-workers/pkg")
        self.repo("gpu_terminal/a/b/c/too-deep")
        with self.assertRaisesRegex(agents_kilix.AgentsError, "no matching"):
            agents_kilix.resolve_dir("pkg", home=self.home)
        with self.assertRaisesRegex(agents_kilix.AgentsError, "no matching"):
            agents_kilix.resolve_dir("fake", home=self.home)
        with self.assertRaisesRegex(agents_kilix.AgentsError, "no matching"):
            agents_kilix.resolve_dir("too-deep", home=self.home)

    def test_checkout_index_expires_after_sixty_seconds(self):
        original = self.repo("gpu_terminal/a/widget")
        with mock.patch.object(agents_kilix.time, "monotonic",
                               side_effect=(100.0, 120.0, 161.0)):
            self.assertEqual(agents_kilix.resolve_dir("widget", home=self.home), original)
            added = self.repo("gpu_terminal/b/widget")
            self.assertEqual(agents_kilix.resolve_dir("widget", home=self.home), original)
            with self.assertRaises(agents_kilix.AgentsError) as raised:
                agents_kilix.resolve_dir("widget", home=self.home)
        self.assertIn(str(added), str(raised.exception))

    def test_one_and_two_character_names_never_scan(self):
        self.repo("gpu_terminal/a/os")
        with mock.patch.object(agents_kilix, "_git_directories") as scan, \
                self.assertRaises(agents_kilix.AgentsError) as raised:
            agents_kilix.resolve_dir("os", home=self.home)
        scan.assert_not_called()
        self.assertIn("dirs.json", str(raised.exception))


class RunnerTransitions(unittest.TestCase):
    @staticmethod
    def pane(activity="idle", coding_session=None, pane_id=3):
        if coding_session is None:
            coding_session = {"provider": "codex", "cwd": "/w/kilix"}
        return {"pane_id": pane_id, "cwd": "/w/kilix", "activity": activity,
                "coding_session": coding_session,
                "broker": {"session_id": "b" * 16}}

    def run_actions(self, actions, panes=None, *, wait_failure=False):
        calls = []
        panes = panes if panes is not None else [self.pane()]

        def run(argv, timeout=30):
            calls.append(list(argv))
            if argv[:2] == ["agent-control", "list"]:
                return json.dumps({"caller_pane": 1, "panes": [
                    {"pane_id": 1, "broker": "a" * 16, "cwd": "/w"}]})
            if argv[:2] == ["panes", "list"]:
                return json.dumps({"panes": panes})
            if argv[:2] in (["agent-control", "new-tab"], ["agent-control", "split"]):
                return json.dumps({"pane": {"pane_id": 9}})
            if argv[:2] == ["panes", "wait"]:
                if wait_failure and argv[argv.index("--for") + 1] == "working":
                    raise agents_kilix.AgentsError("timed out")
                return "{}"
            if argv[:2] == ["agent-control", "send"]:
                return "{}"
            raise AssertionError(argv)

        with mock.patch.object(agents_kilix, "_run", side_effect=run), \
                mock.patch.object(agents_kilix, "resolve_dir", return_value=Path("/w/kilix")):
            results = agents_kilix.perform(actions, cwd="/w")
        return results, calls

    @staticmethod
    def wait_states(calls):
        return [argv[argv.index("--for") + 1] for argv in calls
                if argv[:2] == ["panes", "wait"]]

    def test_an_idle_tell_confirms_delivery_before_a_following_idle_wait(self):
        actions = [agents.Action("tell", {"session": "codex@kilix", "text": "run it"}),
                   agents.Action("wait", {"session": "codex@kilix", "for": "idle"})]
        results, calls = self.run_actions(actions)
        self.assertEqual([item["outcome"] for item in results], ["done", "done"])
        self.assertEqual(results[0]["delivery"], "delivered")
        self.assertEqual(self.wait_states(calls), ["working", "idle"])
        working = next(argv for argv in calls if argv[:2] == ["panes", "wait"])
        self.assertEqual(working[working.index("--timeout") + 1], "30")

    def test_an_unobserved_idle_delivery_is_a_failed_step(self):
        tell = agents.Action("tell", {"session": "codex@kilix", "text": "run it"})
        results, _calls = self.run_actions([tell], wait_failure=True)
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertIn("delivery not confirmed", results[0]["reason"])

    def test_resume_observes_working_before_idle(self):
        actions = [agents.Action("agent", {"agent": "codex", "dir": "kilix",
                                            "resume": "abc-123"}),
                   agents.Action("wait", {"session": "it", "for": "idle"})]
        results, calls = self.run_actions(actions, panes=[])
        self.assertEqual([item["outcome"] for item in results], ["done", "done"])
        self.assertEqual(self.wait_states(calls), ["working", "idle"])

    def test_a_prompted_launch_observes_working_before_idle(self):
        actions = [agents.Action("agent", {"agent": "codex", "dir": "kilix",
                                            "prompt": "run the suite"}),
                   agents.Action("wait", {"session": "it", "for": "idle"})]
        results, calls = self.run_actions(actions, panes=[])
        self.assertEqual([item["outcome"] for item in results], ["done", "done"])
        self.assertEqual(self.wait_states(calls), ["working", "idle"])

    def test_a_working_tell_crosses_idle_then_working_before_following_wait(self):
        actions = [agents.Action("tell", {"session": "claude@kilix", "text": "next"}),
                   agents.Action("wait", {"session": "claude@kilix", "for": "idle"})]
        pane = self.pane(activity="working", coding_session={"provider": "claude", "cwd": "/w/kilix"})
        results, calls = self.run_actions(actions, panes=[pane])
        self.assertEqual([item["outcome"] for item in results], ["done", "done"])
        self.assertEqual(results[0]["delivery"], "queued")
        self.assertEqual(self.wait_states(calls), ["idle", "working", "idle"])

    def test_only_readers_with_waiting_support_may_steer_working_sessions(self):
        for provider, agent, outcome in (("claude", "claude", "done"),
                                         ("grok", "grok", "done"),
                                         ("codex", "codex", "failed"),   # idle-only until verified live
                                         ("omp", "qwen-omp", "failed"),
                                         ("kimi", "kimi", "failed")):
            action = agents.Action("tell", {"session": f"{agent}@kilix", "text": "next"})
            pane = self.pane(activity="working",
                             coding_session={"provider": provider, "cwd": "/w/kilix"})
            results, _calls = self.run_actions([action], panes=[pane])
            self.assertEqual(results[0]["outcome"], outcome, provider)

    def test_a_waiting_tell_after_resume_observes_the_resume_transition(self):
        actions = [agents.Action("agent", {"agent": "codex", "dir": "kilix",
                                            "resume": "abc-123"}),
                   agents.Action("tell", {"session": "it", "text": "next", "wait": True})]
        results, calls = self.run_actions(actions, panes=[self.pane(pane_id=9)])
        self.assertEqual([item["outcome"] for item in results], ["done", "done"])
        self.assertEqual(self.wait_states(calls), ["working", "idle", "working"])

    def test_oversize_launch_prompt_fails_before_any_kilix_call(self):
        action = agents.Action("agent", {"agent": "codex", "dir": "kilix",
                                          "prompt": "é" * 513})
        with mock.patch.object(agents_kilix, "_run") as run, \
                mock.patch.object(agents_kilix, "resolve_dir") as resolve:
            results = agents_kilix.perform([action])
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertIn("1024 bytes", results[0]["reason"])
        run.assert_not_called()
        resolve.assert_not_called()

    def test_malformed_coding_session_is_a_failed_step(self):
        tell = agents.Action("tell", {"session": "codex@kilix", "text": "go"})
        results, _calls = self.run_actions([tell], panes=[self.pane(coding_session="codex")])
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertIn("no live codex", results[0]["reason"])


class HelpText(unittest.TestCase):
    def test_agents_help_documents_the_directory_map(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            needle_cli.main(["agents", "--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("~/.config/kilix-needle/dirs.json", output.getvalue())


if __name__ == "__main__":
    unittest.main()


class GenericAndResearchNames(unittest.TestCase):
    """Review R14 round 4 (KN-R14-54): ~/research is never scanned, and generic
    names never pick a checkout."""

    def test_research_and_generic_names_do_not_scan(self):
        with tempfile.TemporaryDirectory() as home_string:
            home = Path(home_string)
            for rel in ("research/seat/kn/base", "research/refs/widget", "gpu_terminal/x/checkout"):
                (home / rel / ".git").mkdir(parents=True)
            agents_kilix._DIR_SCAN_CACHE.clear()
            for name in ("widget", "base", "checkout", "the checkout"):
                with self.assertRaises(agents_kilix.AgentsError, msg=name):
                    agents_kilix.resolve_dir(name, home=home)


class RunnerSurvivors(unittest.TestCase):
    """Review R14 round 5's runner survivors (K01, K03, R08, R10) and KN-R14-64."""

    def perform(self, actions, panes, wait_fails=False):
        sent = []

        def fake(argv, timeout=30):
            sent.append(argv)
            if argv[:2] == ["agent-control", "list"]:
                return json.dumps({"caller_pane": 1, "panes": [{"pane_id": 1, "broker": "a" * 16,
                                                                "cwd": "/w"}]})
            if argv[:2] == ["panes", "list"]:
                return json.dumps({"panes": panes})
            if argv[:2] == ["panes", "wait"] and wait_fails:
                raise agents_kilix.AgentsError("timed out")
            if argv[0] == "agent-control" and argv[1] in ("new-tab", "split"):
                return json.dumps({"pane": {"pane_id": 7}})
            return "{}"
        with mock.patch.object(agents_kilix, "_run", side_effect=fake), \
                mock.patch.object(agents_kilix, "resolve_dir",
                                  side_effect=lambda said, cwd=None: Path("/w/kilix")):
            results = agents_kilix.perform(actions, cwd="/w/kilix")
        return results, [a for a in sent if a[:2] == ["agent-control", "send"]]

    def pane(self, **extra):
        pane = {"pane_id": 7, "cwd": "/w/kilix", "activity": "idle",
                "broker": {"session_id": "b" * 16},
                "coding_session": {"provider": "codex", "cwd": "/w/kilix"}}
        pane.update(extra)
        return pane

    def test_it_holds_when_the_launched_pane_reports_another_agent(self):          # K01
        actions = [agents.Action("agent", {"agent": "codex", "dir": "kilix"}),
                   agents.Action("tell", {"session": "it", "text": "go"})]
        results, sends = self.perform(actions, [self.pane(coding_session={
            "provider": "claude", "cwd": "/w/kilix"})])
        self.assertEqual(results[1]["outcome"], "failed")
        self.assertEqual(sends, [])

    def test_a_malformed_session_record_holds(self):                               # R10
        actions = [agents.Action("agent", {"agent": "codex", "dir": "kilix"}),
                   agents.Action("tell", {"session": "it", "text": "go"})]
        results, sends = self.perform(actions, [self.pane(coding_session="codex")])
        self.assertEqual(results[1]["outcome"], "failed")
        self.assertEqual(sends, [])

    def test_a_pane_back_at_its_shell_is_never_typed_into(self):                   # K03
        tell = agents.Action("tell", {"session": "claude@kilix", "text": "rm -rf build"})
        results, sends = self.perform([tell], [self.pane(pane_id=3, activity="shell",
                                                         coding_session={"provider": "claude",
                                                                         "cwd": "/w/kilix"})])
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertEqual(sends, [])

    def test_a_symlinked_checkout_is_not_indexed(self):                            # R08
        with tempfile.TemporaryDirectory() as home_string:
            home = Path(home_string)
            (home / "gpu_terminal").mkdir()
            (home / "elsewhere" / "secretproj" / ".git").mkdir(parents=True)
            (home / "gpu_terminal" / "link").symlink_to(home / "elsewhere")
            agents_kilix._DIR_SCAN_CACHE.clear()
            with self.assertRaises(agents_kilix.AgentsError):
                agents_kilix.resolve_dir("secretproj", home=home)

    def test_a_failed_wait_says_the_message_was_sent(self):                        # KN-R14-64
        actions = [agents.Action("tell", {"session": "claude@kilix", "text": "go"}),
                   agents.Action("wait", {"session": "claude@kilix", "for": "idle"})]
        pane = self.pane(pane_id=3, activity="working",
                         coding_session={"provider": "claude", "cwd": "/w/kilix"})
        results, sends = self.perform(actions, [pane], wait_fails=True)
        self.assertEqual(len(sends), 1)
        self.assertEqual(results[-1]["outcome"], "failed")
        self.assertIn("message was sent", results[-1]["reason"])


class GateSurvivesAHungCase(unittest.TestCase):
    """A case the engine doesn't answer is scored as no answer, the worker is
    restarted, and the timeout is counted (the first agents gate crashed on one)."""

    def test_timeout_is_no_answer_and_the_engine_restarts(self):
        import evaluate
        import libengine

        class Engine:
            def __init__(self):
                self.starts, self.calls = 0, 0

            def reset(self):
                pass

            def complete(self, text):
                self.calls += 1
                if self.calls == 1:
                    raise libengine.LibEngineError("did not answer")
                return {"function_calls": [{"name": "agent",
                                            "arguments": {"agent": "codex", "dir": "kilix"}}]}

            def close(self):
                pass

            def start(self):
                self.starts += 1

        engine = Engine()
        cases = [{"request": "open codex in kilix", "expect": [["agent", {"agent": "codex",
                                                                           "dir": "kilix"}]],
                  "tag": "launch"}] * 2
        report = evaluate.score(engine, cases, 1, job="agents")
        self.assertEqual(engine.starts, 1)
        self.assertEqual(report["totals"]["timeouts"], 1)
        self.assertEqual(report["totals"]["exact"], 1)
