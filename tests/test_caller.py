"""Who "this pane" is, how Kilix is reached, and what an agent may do."""
import copy
import io
import json
import os
import sys
import unittest

from support import FakeKilix, desktop
from actions import Action
import kilix
import needle_cli


class Patched:
    """Temporarily replace module attributes and environment variables."""

    def __init__(self, env=None, **attrs):
        self.env, self.attrs = env or {}, attrs

    def __enter__(self):
        self.saved_attrs = {k: getattr(kilix, k) for k in self.attrs}
        self.saved_env = {k: os.environ.get(k) for k in self.env}
        for k, v in self.attrs.items():
            setattr(kilix, k, v)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_exc):
        for k, v in self.saved_attrs.items():
            setattr(kilix, k, v)
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class CallerDiscovery(unittest.TestCase):
    def test_without_kitty_window_id_the_caller_is_found_by_ancestry(self):
        with Patched(env={"KITTY_WINDOW_ID": None},
                     _ancestors=lambda: [os.getpid(), 5_000_301, 1]):
            tree = kilix.Tree(desktop())
        self.assertEqual(tree.caller["id"], 301)
        self.assertEqual(tree.active_tab["id"], 30)

    def test_no_ancestor_match_falls_back_to_the_focused_pane_not_a_guess(self):
        with Patched(env={"KITTY_WINDOW_ID": None}, _ancestors=lambda: [os.getpid(), 42]):
            tree = kilix.Tree(desktop())
        self.assertIsNone(tree.caller)
        self.assertEqual(tree.active_pane["id"], 200)

    def test_socket_from_the_kitty_ancestor(self):
        comms = {111: "python3", 222: "kitty-pty-broke", 333: "kitty"}
        with Patched(env={"KITTY_LISTEN_ON": None}, _ancestors=lambda: [111, 222, 333],
                     _comm=lambda pid: comms[pid]):
            self.assertEqual(kilix._target(), ["--to", "unix:@kilix-333"])

    def test_no_socket_and_no_kilix_ancestor_is_refused(self):
        with Patched(env={"KITTY_LISTEN_ON": None}, _ancestors=lambda: [111, 222],
                     _comm=lambda pid: "bash"):
            with self.assertRaisesRegex(kilix.KilixError, "not running inside Kilix"):
                kilix._target()

    def test_a_listen_address_is_used_as_is(self):
        self.assertEqual(kilix._target(), [])


class Overlay(unittest.TestCase):
    def tree(self, history):
        data = copy.deepcopy(desktop())
        tab = data[0]["tabs"][2]
        tab["active_window_history"] = history
        with Patched(env={"KITTY_WINDOW_ID": "300"}):
            return kilix.Tree(data, under_overlay=True)

    def test_current_is_the_pane_the_overlay_covers(self):
        tree = self.tree([302, 301, 300])
        self.assertEqual(tree.active_pane["id"], 301)
        self.assertEqual(tree.caller["id"], 300)

    def test_no_history_is_refused(self):
        with self.assertRaisesRegex(kilix.KilixError, "overlay was opened over"):
            self.tree([300])


class NextPane(unittest.TestCase):
    def test_next_and_previous_cycle_within_the_tab(self):
        with Patched(env={"KITTY_WINDOW_ID": "300"}):
            tree = kilix.Tree(desktop())
        self.assertEqual(tree.pane("next")["id"], 301)
        self.assertEqual(tree.pane("previous")["id"], 302)


class ScriptedEngine:
    def __init__(self, *calls):
        self.calls = list(calls)

    def reset(self):
        pass

    def complete(self, _text):
        return {"function_calls": self.calls}


class AgentMode(unittest.TestCase):
    def run_agent(self, prompt, *calls, yes=True, as_json=True, dry_run=False, caller="300"):
        out = io.StringIO()
        saved = sys.stdin
        # A stdin that fails if read: agent mode must never consume it.
        sys.stdin = type("NoRead", (), {"readline": lambda *a: self.fail("read stdin"),
                                        "isatty": lambda self: True})()
        try:
            with FakeKilix(desktop()) as fake, Patched(env={"KITTY_WINDOW_ID": caller}):
                status = needle_cli.handle(ScriptedEngine(*calls), prompt, assume_yes=yes,
                                           out=out, as_json=as_json, agent=True, dry_run=dry_run)
                return status, fake.calls(), out.getvalue()
        finally:
            sys.stdin = saved

    def test_an_agent_cannot_close_its_own_pane_even_with_yes(self):
        status, calls, out = self.run_agent(
            "close this pane", {"name": "close_pane", "arguments": {"pane": "this"}})
        self.assertEqual((status, calls), (1, []))
        record = json.loads(out)
        self.assertEqual(record["items"][0]["outcome"], "refused")
        self.assertIn("may not close its own pane", record["items"][0]["reason"])

    def test_with_no_known_caller_an_agent_closes_nothing_and_has_no_this(self):
        # Review KN-03: without KITTY_WINDOW_ID (and no ancestor match; the
        # fixture pids are above pid_max) "this pane" was the user's focused
        # pane, and the own-pane guard was off.
        for prompt, name, args in (("close this pane", "close_pane", {"pane": "this"}),
                                   ("close this tab", "close_tab", {"tab": "this tab"}),
                                   ("close the left pane", "close_pane", {"pane": "left"})):
            with self.subTest(prompt=prompt):
                status, calls, out = self.run_agent(prompt, {"name": name, "arguments": args},
                                                    caller=None)
                self.assertEqual((status, calls), (1, []))
                self.assertIn("agent's own", json.loads(out)["items"][0]["reason"])
        # Moving focus elsewhere is still allowed.
        status, calls, _ = self.run_agent("next tab", {"name": "go_to_tab",
                                                       "arguments": {"tab": "next"}}, caller=None)
        self.assertEqual(status, 0)

    def test_an_agent_cannot_close_the_tab_it_is_in(self):
        status, calls, _ = self.run_agent(
            "close this tab", {"name": "close_tab", "arguments": {"tab": "this tab"}})
        self.assertEqual((status, calls), (1, []))

    def test_an_agent_may_close_another_pane_with_yes(self):
        status, calls, out = self.run_agent(
            "close the left pane", {"name": "close_pane", "arguments": {"pane": "left"}})
        self.assertEqual((status, calls), (0, [(["close-window", "--match=id:301"], None)]))
        self.assertEqual(json.loads(out)["items"][0]["outcome"], "done")

    def test_without_yes_an_agent_is_never_prompted(self):
        status, calls, out = self.run_agent(
            "close the left pane", {"name": "close_pane", "arguments": {"pane": "left"}},
            yes=False)
        self.assertEqual((status, calls), (1, []))
        self.assertEqual(json.loads(out)["items"][0]["reason"],
                         "needs a yes and there is no one to ask")

    def test_json_record_shape(self):
        status, _, out = self.run_agent(
            "next tab", {"name": "go_to_tab", "arguments": {"tab": "next"}}, dry_run=True)
        record = json.loads(out)
        self.assertEqual(set(record), {"request", "status", "note", "items"})
        self.assertEqual(record["items"][0]["outcome"], "would")
        self.assertEqual(record["items"][0]["args"], {"tab": "next"})


class AgentWithoutCaller(AgentMode):
    """R2 KN-R2-04 and mutant R12: no known caller, nothing risky at all."""

    def test_no_typing_here_or_into_a_relative_pane(self):
        for prompt, args in (("run ls here", {"pane": "here", "command": "ls"}),
                             ("run rm -rf build in the right pane",
                              {"pane": "right", "command": "rm -rf build"})):
            with self.subTest(prompt=prompt):
                status, calls, out = self.run_agent(
                    prompt, {"name": "run_in_pane", "arguments": args}, caller=None)
                self.assertEqual((status, calls), (1, []))
                self.assertIn("agent's own", json.loads(out)["items"][0]["reason"])   # M80


class AgentWithoutCallerThis(AgentMode):
    def test_no_this_pane_even_for_a_safe_action(self):         # R3 mutant G02
        status, calls, out = self.run_agent(
            "go to this pane", {"name": "go_to_pane", "arguments": {"pane": "this"}}, caller=None)
        self.assertEqual((status, calls), (1, []))
        self.assertIn("agent's own", json.loads(out)["items"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
