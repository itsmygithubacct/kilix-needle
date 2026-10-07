"""Round 2 runner regressions for the coding-agents job."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
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


class UnreadableStates(unittest.TestCase):
    """A session whose state Kilix cannot read (`agent`, as every Codex pane is) takes no message and no wait."""

    pane = RunnerTransitions.__dict__["pane"]
    run_actions = RunnerTransitions.run_actions

    def test_tell_and_wait_refuse_an_unreadable_codex_session_without_sending_or_waiting(self):
        for action in (agents.Action("tell", {"session": "codex@kilix", "text": "go"}),
                       agents.Action("tell", {"session": "codex@kilix", "text": "go", "wait": True}),
                       agents.Action("wait", {"session": "codex@kilix", "for": "idle"})):
            results, calls = self.run_actions([action], panes=[self.pane(activity="agent")])
            self.assertEqual(results[0]["outcome"], "failed", action.kind)
            self.assertIn("cannot read the state of this codex session", results[0]["reason"])
            self.assertIn("exposes no exact current-session identity", results[0]["reason"])
            self.assertFalse([c for c in calls if c[:2] in (["agent-control", "send"], ["panes", "wait"])], calls)

    def test_other_unreadable_agents_are_refused_the_same_way(self):
        pane = self.pane(activity="agent", coding_session={"provider": "claude", "cwd": "/w/kilix"})
        results, calls = self.run_actions([agents.Action("tell", {"session": "claude@kilix", "text": "go"})], panes=[pane])
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertNotIn("exposes no exact", results[0]["reason"])

    def test_a_session_that_becomes_unreadable_between_the_lookup_and_the_send_is_held(self):
        listings = [[self.pane(activity="idle")], [self.pane(activity="agent")]]
        sent = []

        def run(argv, timeout=30):
            if argv[:2] == ["agent-control", "list"]:
                return json.dumps({"caller_pane": 1, "panes": [{"pane_id": 1, "broker": "a" * 16, "cwd": "/w"}]})
            if argv[:2] == ["panes", "list"]:
                return json.dumps({"panes": listings.pop(0) if len(listings) > 1 else listings[0]})
            sent.append(argv)
            return "{}"
        tell = agents.Action("tell", {"session": "codex@kilix", "text": "go"})
        with mock.patch.object(agents_kilix, "_run", side_effect=run), \
                mock.patch.object(agents_kilix, "resolve_dir", return_value=Path("/w/kilix")):
            result = agents_kilix.perform([tell], cwd="/w")[0]
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("cannot read the state", result["reason"])
        self.assertEqual(sent, [])

    def test_a_readable_session_is_still_served(self):
        results, _calls = self.run_actions([agents.Action("wait", {"session": "codex@kilix", "for": "idle"})])
        self.assertEqual(results[0]["outcome"], "done")


class InsideTmuxHint(unittest.TestCase):
    """A coding agent inside tmux is not readable by Kilix: say so, and how to reach it."""

    @staticmethod
    def tmux_pane(pane_id=6, argv=("tmux", "new-session", "-s", "x", "claude"), activity="running"):
        return {"pane_id": pane_id, "cwd": "/w/kilix", "activity": activity, "coding_session": None,
                "broker": {"session_id": "b" * 16},
                "process": {"foreground": [{"pid": 4000 + pane_id - 6, "argv": list(argv), "cwd": "/w/kilix"}]}}

    def run_tell(self, panes):
        def run(argv, timeout=30):
            if argv[:2] == ["agent-control", "list"]:
                return json.dumps({"caller_pane": 1, "panes": [{"pane_id": 1, "broker": "a" * 16, "cwd": "/w"}]})
            if argv[:2] == ["panes", "list"]:
                return json.dumps({"panes": panes})
            raise AssertionError(argv)          # nothing may be sent, waited on or typed
        tell = agents.Action("tell", {"session": "claude@kilix", "text": "go"})
        with mock.patch.object(agents_kilix, "_run", side_effect=run), \
                mock.patch.object(agents_kilix, "resolve_dir", return_value=Path("/w/kilix")):
            return agents_kilix.perform([tell], cwd="/w")[0]

    def test_a_pane_running_a_tmux_client_is_named_with_the_way_to_reach_it(self):
        reason = self.hint([self.tmux_pane()], self.fake_proc(4000, {"HOME": "/h"}))
        for word in ("tmux", "pane 6", "cannot read its state", "no message is sent into tmux",
                     "kilix-needle tmux --socket", "attach"):
            self.assertIn(word, reason)
        self.assertNotIn("no live claude sessions", reason)

    def fake_proc(self, pid, environ, *, uid_of=None):
        root = Path(tempfile.mkdtemp(prefix="kn-proc-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / str(pid)).mkdir()
        (root / str(pid) / "environ").write_bytes(
            b"\0".join(f"{k}={v}".encode() for k, v in environ.items()) + b"\0")
        return root

    def live_clients(self, panes, proc):
        """Make /proc agree with the listing: each listed client runs its listed command in its directory."""
        root = Path(proc) if proc else Path(tempfile.mkdtemp(prefix="kn-proc-"))
        if not proc:
            self.addCleanup(shutil.rmtree, root, True)
        for pane in panes:
            for item in (pane.get("process") or {}).get("foreground") or []:
                pid = item.get("pid")
                if not isinstance(pid, int):
                    continue
                folder = root / str(pid)
                folder.mkdir(exist_ok=True)
                (folder / "cmdline").write_bytes(b"\0".join(str(a).encode() for a in item["argv"]) + b"\0")
                link = folder / "cwd"
                if link.is_symlink():
                    link.unlink()
                if item.get("cwd"):
                    link.symlink_to(item["cwd"])
        return root

    def hint(self, panes, proc=None):
        root = self.live_clients(panes, proc)
        with mock.patch.object(agents_kilix, "PROC_ROOT", str(root)):
            return self.run_tell(panes)["reason"]

    def test_the_sockets_of_the_clients_are_used_in_the_hint(self):
        got = self.hint([self.tmux_pane(argv=("tmux", "-S", "/srv/t/sock", "attach"))])
        self.assertIn("--socket /srv/t/sock ", got)
        self.assertIn("tmux -S /srv/t/sock attach", got)

    def test_a_relative_socket_is_resolved_against_the_clients_directory(self):
        pane = self.tmux_pane(argv=("tmux", "-S", "run/sock", "attach"))
        pane["process"]["foreground"][0]["cwd"] = "/w/kilix/sub"
        self.assertIn("--socket /w/kilix/sub/run/sock ", self.hint([pane]))
        del pane["process"]["foreground"][0]["cwd"]
        got = self.hint([pane])
        self.assertIn("is not established", got)
        self.assertNotIn("--socket", got)

    def test_options_before_the_socket_do_not_hide_it(self):
        for argv, expected in ((("tmux", "-f", "/c/conf", "-S", "/s/one", "attach"), "/s/one"),
                               (("tmux", "-uv", "-S/s/two", "attach"), "/s/two"),
                               (("tmux", "-2", "-c", "sh -c x", "-S", "/s/three", "new"), "/s/three"),
                               (("tmux", "-L", "name", "-S", "/s/four", "attach"), "/s/four"),     # -S wins
                               (("tmux", "-S", "/s/old", "-S", "/s/five", "attach"), "/s/five")):
            self.assertIn(f"--socket {expected} ", self.hint([self.tmux_pane(argv=argv)]), argv)

    def test_a_socket_after_the_command_belongs_to_the_command(self):
        got = self.hint([self.tmux_pane(argv=("tmux", "new-session", "-S", "/not/a/socket", "claude"))])
        self.assertNotIn("/not/a/socket ", got.split("cannot")[0])
        self.assertNotIn("--socket /not/a/socket", got)

    def test_an_option_that_cannot_be_read_gives_no_socket(self):
        got = self.hint([self.tmux_pane(argv=("tmux", "-Z", "-S", "/s/x", "attach"))])
        self.assertIn("is not established", got)
        self.assertNotIn("--socket", got)
        self.assertIn("is not established", self.hint([self.tmux_pane(argv=("tmux", "-S"))]))

    def test_the_default_and_named_sockets_come_from_the_clients_environment(self):
        proc = self.fake_proc(4000, {"TMUX_TMPDIR": "/clients/tmp"})
        uid = os.stat(proc / "4000").st_uid
        got = self.hint([self.tmux_pane(argv=("tmux", "-L", "work", "attach"))], proc)
        self.assertIn(f"--socket /clients/tmp/tmux-{uid}/work ", got)
        got = self.hint([self.tmux_pane(argv=("/usr/bin/tmux", "attach"))], proc)
        self.assertIn(f"--socket /clients/tmp/tmux-{uid}/default ", got)
        plain = self.fake_proc(4000, {"HOME": "/h"})
        self.assertIn(f"--socket /tmp/tmux-{os.stat(plain / '4000').st_uid}/default ",
                      self.hint([self.tmux_pane(argv=("tmux", "attach"))], plain))

    def test_needles_own_tmux_environment_is_never_used(self):
        proc = self.fake_proc(4000, {"TMUX_TMPDIR": "/clients/tmp"})
        with mock.patch.dict(os.environ, {"TMUX_TMPDIR": "/needles/tmp", "TMUX": "/needles/sock,1,0"}):
            got = self.hint([self.tmux_pane(argv=("tmux", "attach"))], proc)
        self.assertIn("/clients/tmp/", got)
        self.assertNotIn("/needles/", got)

    def test_a_client_inside_tmux_uses_the_socket_of_its_TMUX(self):
        proc = self.fake_proc(4000, {"TMUX": "/inner/sock,123,0", "TMUX_TMPDIR": "/ignored"})
        self.assertIn("--socket /inner/sock ", self.hint([self.tmux_pane(argv=("tmux", "attach"))], proc))
        got = self.hint([self.tmux_pane(argv=("tmux", "-L", "x", "attach"))], proc)
        self.assertNotIn("/inner/sock", got)             # -L overrides $TMUX

    def test_an_unreadable_environment_gives_no_default_socket(self):
        got = self.hint([self.tmux_pane(argv=("tmux", "attach"))])
        self.assertIn("is not established", got)
        self.assertNotIn("--socket", got)
        self.assertNotIn("tmux-", got)
        proc = self.fake_proc(4000, {})                 # the process exists but its environment is unreadable
        (proc / "4000" / "environ").unlink()
        got = self.hint([self.tmux_pane(argv=("tmux", "attach"))], proc)
        self.assertIn("environment cannot be read", got)
        self.assertNotIn("--socket", got)

    def test_the_socket_is_shell_quoted(self):
        got = self.hint([self.tmux_pane(argv=("tmux", "-S", "/s/with space; rm x", "attach"))])
        self.assertIn("--socket '/s/with space; rm x' ", got)
        self.assertIn("tmux -S '/s/with space; rm x' attach", got)

    def test_only_clients_in_the_requested_directory_are_named_when_there_are_any(self):
        near = self.tmux_pane(pane_id=6, argv=("tmux", "-S", "/s/near", "attach"))
        far = self.tmux_pane(pane_id=7, argv=("tmux", "-S", "/s/far", "attach"))
        far["cwd"] = "/elsewhere"
        far["process"]["foreground"][0]["cwd"] = "/elsewhere"
        got = self.hint([far, near])
        self.assertIn("pane 6", got)
        self.assertIn("/s/near", got)
        self.assertNotIn("/s/far", got)
        self.assertNotIn("pane 7", got)
        got = self.hint([far])                      # none here: elsewhere is named as a lead, and says so
        self.assertIn("No pane running tmux is in that directory", got)
        self.assertIn("/s/far", got)

    def test_a_socket_is_left_as_written_so_the_filesystem_resolves_dotdot_through_symlinks(self):
        root = Path(tempfile.mkdtemp(prefix="kn-sock-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "other" / "nested").mkdir(parents=True)
        (root / "work").mkdir()
        (root / "work" / "alias").symlink_to(root / "other" / "nested")
        pane = self.tmux_pane(argv=("tmux", "-S", "alias/../sock", "attach"))
        pane["process"]["foreground"][0]["cwd"] = str(root / "work")
        got = self.hint([pane])
        self.assertIn(f"--socket {root}/work/alias/../sock ", got)
        self.assertEqual(Path(f"{root}/work/alias/../sock").resolve(), root / "other" / "sock")

    def test_evidence_that_does_not_prove_a_socket_names_none_and_says_how_to_find_it(self):
        cases = (
            ("an empty -S", ("tmux", "-S", "", "attach"), {"HOME": "/h"}),
            ("a relative $TMUX", ("tmux", "attach"), {"TMUX": "run/sock,123,0"}),
            ("a $TMUX that is not PATH,PID,SESSION", ("tmux", "attach"), {"TMUX": "/s/sock,part,123,0"}),
            ("a $TMUX with no pid and session", ("tmux", "attach"), {"TMUX": "/s/sock"}),
            ("a relative TMUX_TMPDIR", ("tmux", "attach"), {"TMUX_TMPDIR": "rel"}),
            ("a -L name with a slash", ("tmux", "-L", "a/b", "attach"), {"HOME": "/h"}),
        )
        for name, argv, environ in cases:
            proc = self.fake_proc(4000, environ)
            got = self.hint([self.tmux_pane(argv=argv)], proc)
            self.assertNotIn("--socket", got, name)
            self.assertNotIn("tmux -S", got, name)
            self.assertIn("is not established", got, name)
            self.assertIn("display-message -p '#{socket_path}'", got, name)
            self.assertIn("no message is sent into tmux", got, name)

    def test_an_environment_that_is_not_valid_text_names_no_socket(self):
        proc = self.fake_proc(4000, {})
        (proc / "4000" / "environ").write_bytes(b"TMUX=/fixture/\xff,123,0\0")
        got = self.hint([self.tmux_pane(argv=("tmux", "attach"))], proc)
        self.assertNotIn("--socket", got)
        (proc / "4000" / "environ").write_bytes(b"TMUX_TMPDIR=/t/\xff\0")
        self.assertNotIn("--socket", self.hint([self.tmux_pane(argv=("tmux", "-L", "x", "attach"))], proc))

    def test_a_well_formed_TMUX_gives_the_socket_up_to_its_first_comma(self):
        proc = self.fake_proc(4000, {"TMUX": "/inner/sock,4242,3"})
        self.assertIn("--socket /inner/sock ", self.hint([self.tmux_pane(argv=("tmux", "attach"))], proc))

    def test_an_empty_label_is_refused_not_read_as_default(self):
        proc = self.fake_proc(4000, {"TMUX_TMPDIR": "/clients/tmp"})
        got = self.hint([self.tmux_pane(argv=("tmux", "-L", "", "attach"))], proc)
        self.assertNotIn("--socket", got)
        self.assertIn("-L name is empty", got)
        got = self.hint([self.tmux_pane(argv=("tmux", "-L", "x", "attach"))], proc)
        self.assertIn("/clients/tmp/tmux-", got)

    def test_a_client_that_is_no_longer_what_the_listing_says_names_no_socket(self):
        pane = self.tmux_pane(argv=("tmux", "-S", "/fixture/old", "attach"))
        root = self.live_clients([pane], None)
        self.assertIn("--socket /fixture/old ", self.hint([pane], root))
        # the process now runs another command line, another directory, is gone, or the listing has no pid
        (root / "4000" / "cmdline").write_bytes(b"tmux\0-S\0/fixture/current\0attach\0")
        with mock.patch.object(agents_kilix, "PROC_ROOT", str(root)):
            got = self.run_tell([pane])["reason"]
        self.assertNotIn("--socket", got)
        self.assertIn("command line is not the one listed", got)
        (root / "4000" / "cmdline").write_bytes(b"tmux\0-S\0/fixture/old\0attach\0")
        (root / "4000" / "cwd").unlink()
        (root / "4000" / "cwd").symlink_to("/elsewhere")
        with mock.patch.object(agents_kilix, "PROC_ROOT", str(root)):
            got = self.run_tell([pane])["reason"]
        self.assertNotIn("--socket", got)
        self.assertIn("directory is not the one listed", got)
        (root / "4000" / "cmdline").unlink()
        with mock.patch.object(agents_kilix, "PROC_ROOT", str(root)):
            self.assertNotIn("--socket", self.run_tell([pane])["reason"])
        for bad in (0, -3, True, "4000", 1.5):
            invalid = dict(pane, process={"foreground": [{"pid": bad, "argv": ["tmux", "-S", "/fixture/old"], "cwd": "/w/kilix"}]})
            got = self.hint([invalid])
            self.assertNotIn("--socket", got, bad)
            self.assertIn("process id is not valid", got, bad)
        # a degraded listing entry with no pid at all has nothing to read again: its listed argv stands
        nopid = dict(pane, process={"foreground": [{"argv": ["tmux", "-S", "/fixture/old"], "cwd": "/w/kilix"}]})
        self.assertIn("--socket /fixture/old ", self.hint([nopid]))

    def test_a_command_line_that_is_not_valid_text_names_no_socket(self):
        pane = self.tmux_pane(argv=("tmux", "-S", "/fixture/old", "attach"))
        root = self.live_clients([pane], None)
        (root / "4000" / "cmdline").write_bytes(b"tmux\0-S\0/fixture/\xff\0attach\0")
        with mock.patch.object(agents_kilix, "PROC_ROOT", str(root)):
            self.assertNotIn("--socket", self.run_tell([pane])["reason"])

    def test_each_distinct_server_gets_its_own_command(self):
        one = self.tmux_pane(pane_id=6, argv=("tmux", "-S", "/s/a", "attach"))
        two = self.tmux_pane(pane_id=8, argv=("tmux", "-S", "/s/b", "attach"))
        three = self.tmux_pane(pane_id=9, argv=("tmux", "-S", "/s/a", "attach"))
        got = self.hint([one, two, three])
        self.assertIn("--socket /s/a ", got)
        self.assertIn("--socket /s/b ", got)
        self.assertIn("pane 9: the same server", got)

    def test_without_a_tmux_pane_the_old_message_stays(self):
        plain = dict(self.tmux_pane(), process={"foreground": [{"pid": 1, "argv": ["vim", "x"]}]})
        self.assertIn("no live claude sessions", self.run_tell([plain])["reason"])
        self.assertIn("no live claude sessions", self.run_tell([])["reason"])

    def test_a_readable_session_is_used_whatever_else_runs_tmux(self):
        listed = {"pane_id": 3, "cwd": "/w/kilix", "activity": "idle", "broker": {"session_id": "b" * 16},
                  "coding_session": {"provider": "claude", "cwd": "/w/kilix"}}
        sent = []

        def run(argv, timeout=30):
            if argv[:2] == ["agent-control", "list"]:
                return json.dumps({"caller_pane": 1, "panes": [{"pane_id": 1, "broker": "a" * 16, "cwd": "/w"}]})
            if argv[:2] == ["panes", "list"]:
                return json.dumps({"panes": [self.tmux_pane(), listed]})
            sent.append(argv)
            return "{}"
        tell = agents.Action("tell", {"session": "claude@kilix", "text": "go"})
        with mock.patch.object(agents_kilix, "_run", side_effect=run), \
                mock.patch.object(agents_kilix, "resolve_dir", return_value=Path("/w/kilix")):
            self.assertEqual(agents_kilix.perform([tell], cwd="/w")[0]["outcome"], "done")
        self.assertEqual(sent[0][:3], ["agent-control", "send", "3"])      # to the listed pane only

    def test_the_callers_own_tmux_pane_and_listed_ones_are_not_named(self):
        own = self.tmux_pane(pane_id=1)
        self.assertIn("no live claude sessions", self.run_tell([own])["reason"])
        coded = dict(self.tmux_pane(), coding_session={"provider": "codex", "cwd": "/w/kilix"})
        self.assertIn("no live claude sessions", self.run_tell([coded])["reason"])


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
                    raise libengine.LibEngineError("the Needle library did not answer in 120 s")
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
