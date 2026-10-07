"""Session-state detection beyond Kilix's reader: Codex and Claude screens, and tmux-hosted sessions.

The screens are fixtures in tests/data/agent_screens, written from the real sessions that failed.
tmux and /proc are fakes; nothing here talks to a live Kilix, tmux or process.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401
import agents
import agents_detect as detect
import agents_kilix

SCREENS = Path(__file__).parent / "data" / "agent_screens"
BROKER = "b" * 16
DIRECTORY = Path("/w/kilix")


def screen(name):
    return (SCREENS / f"{name}.txt").read_text(encoding="utf-8")


class Screens(unittest.TestCase):
    CASES = (
        ("codex", "codex_working", "working"), ("codex", "codex_working_composer_hidden", "working"),
        ("codex", "codex_idle", "idle"), ("codex", "codex_approval", "waiting"), ("codex", "codex_draft", None),
        ("claude", "claude_idle_monitor", "idle"), ("claude", "claude_idle_plain", "idle"),
        ("claude", "claude_working", "working"), ("claude", "claude_approval", "waiting"),
        ("claude", "claude_draft", None),
    )

    def test_the_captured_screens(self):
        for provider, name, expected in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(detect.screen_state(provider, screen(name)), expected)

    def test_a_screen_is_only_read_as_the_provider_it_belongs_to(self):
        # The composer markers belong to one program each.
        self.assertIsNone(detect.screen_state("claude", screen("codex_idle")))
        self.assertIsNone(detect.screen_state("codex", screen("claude_idle_monitor")))
        self.assertIsNone(detect.screen_state("codex", screen("claude_idle_plain")))
        self.assertIsNone(detect.screen_state("claude", screen("codex_draft")))
        self.assertIsNone(detect.screen_state("grok", screen("codex_idle")))
        self.assertIsNone(detect.screen_state("omp", screen("claude_idle_plain")))

    def test_working_and_waiting_beat_an_idle_composer_on_the_same_screen(self):
        # Codex keeps its placeholder composer on screen while it works.
        self.assertIn("Ask Codex to do anything", screen("codex_working"))
        self.assertEqual(detect.screen_state("codex", screen("codex_working")), "working")
        self.assertEqual(detect.screen_state("codex", screen("codex_working") + "\n  Would you like to run the following command?\n"),
                         "waiting")

    def test_unclear_screens_are_not_a_state(self):
        for provider in ("codex", "claude"):
            for text in ("", "   \n\n", "$ ls\nfile\n", "\x1b[2J garbage", "Working", "Ask Codex to do anything",
                         "• working (", "nothing here"):
                with self.subTest(provider=provider, text=text):
                    self.assertIsNone(detect.screen_state(provider, text))
        # Only the screen's last lines count: a long-gone working marker above is history.
        old = "• Working (1s • esc to interrupt)\n" + "\n".join(f"line {n}" for n in range(40)) + "\n"
        self.assertIsNone(detect.screen_state("codex", old))
        self.assertEqual(detect.screen_state("codex", old + "› Ask Codex to do anything\n"), "idle")

    def test_a_draft_or_a_numbered_prompt_is_not_an_empty_composer(self):
        self.assertIsNone(detect.screen_state("claude", screen("claude_draft")))
        self.assertIsNone(detect.screen_state("codex", screen("codex_draft")))
        self.assertIsNone(detect.screen_state("claude", "  1. one\n"))
        self.assertIsNone(detect.screen_state("claude", "❯ 1. only one item\n"))
        # but a real menu of choices is a wait
        self.assertEqual(detect.screen_state("claude", "  1. one\n❯ 2. two\n"), "waiting")

    def test_the_provider_of_a_process(self):
        for argv, expected in ((["claude", "--resume", "x"], "claude"), (["/usr/bin/node", "/opt/bin/claude"], "claude"),
                               (["codex"], "codex"), (["bash", "-c", "claude"], ""), (["tmux", "new-session", "claude"], ""),
                               (["/usr/local/bin/codex", "--yolo"], "codex"), ([], "")):
            self.assertEqual(detect.process_agent(argv), expected, argv)

    def test_tmux_socket_arguments_come_from_the_clients_own_command_line(self):
        for argv, expected in ((["tmux", "new-session", "-s", "x"], []), (["tmux", "-S", "/srv/t/sock", "attach"], ["-S", "/srv/t/sock"]),
                               (["tmux", "-L", "work", "a"], ["-L", "work"]), (["tmux", "-Ssock", "a"], ["-S", "sock"]),
                               (["tmux", "attach", "-S", "/late"], [])):
            self.assertEqual(detect.tmux_socket_args(argv), expected, argv)


class Procs:
    """A fake /proc: a directory of numeric process folders."""

    def __init__(self, test):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-proc-")
        test.addCleanup(self.dir.cleanup)
        self.root = self.dir.name

    def add(self, pid, parent, argv, cwd):
        folder = Path(self.root) / str(pid)
        folder.mkdir(exist_ok=True)
        (folder / "stat").write_text(f"{pid} ({os.path.basename(argv[0])}) S {parent} 0 0 0\n")
        (folder / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
        link = folder / "cwd"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(cwd)


class FakeTmux:
    """tmux's two questions, answered from a table, recording every call."""

    def __init__(self, clients=None, panes=None, screens=None):
        self.clients = clients if clients is not None else {4000: "$3"}
        self.panes = panes if panes is not None else [("%7", 4100, 1, 1, 0)]
        self.screens = dict(screens or {})
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        tail = [a for a in args if a not in ("-S", "-L")]
        if "list-clients" in args:
            return "".join(f"{pid}\t{session}\n" for pid, session in self.clients.items())
        if "list-panes" in args:
            target = args[args.index("-t") + 1]
            return "".join(f"{pane}\t{pid}\t{a}\t{w}\t{m}\n" for pane, pid, a, w, m in self.panes
                           if target == "$3")
        if "capture-pane" in args:
            return self.screens.get(args[args.index("-t") + 1], "")
        raise AssertionError(tail)


class Harness(unittest.TestCase):
    """Runs `agents_kilix.perform` against fake kilix, screen, tmux and /proc."""

    def setUp(self):
        self.procs = Procs(self)
        self.clock = [1000.0]
        self.sent = []
        self.waits = []
        self.screen_reads = []
        self.screens = {}
        self.tmux = FakeTmux()
        self.activity = {}
        patches = [
            mock.patch.object(detect, "PROC_ROOT", self.procs.root),
            mock.patch.object(agents_kilix, "_tmux", self.tmux),
            mock.patch.object(agents_kilix, "_screen", self.read_screen),
            mock.patch.object(agents_kilix, "resolve_dir", lambda said, cwd=None: DIRECTORY),
            # A virtual clock: a wait that is never satisfied ends at its timeout at once,
            # instead of spinning for the real hour a default wait would take.
            mock.patch.object(agents_kilix.time, "sleep", self.advance),
            mock.patch.object(agents_kilix.time, "monotonic", lambda: self.clock[0]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    clock = None

    def advance(self, seconds):
        self.clock[0] += seconds

    def read_screen(self, pane_id):
        self.screen_reads.append(pane_id)
        value = self.screens.get(pane_id, "")
        return value() if callable(value) else value

    def kilix(self, panes):
        def run(argv, timeout=30):
            if argv[:2] == ["agent-control", "list"]:
                return json.dumps({"caller_pane": 1, "panes": [{"pane_id": 1, "broker": "a" * 16, "cwd": "/w"}]})
            if argv[:2] == ["panes", "list"]:
                return json.dumps({"panes": panes() if callable(panes) else panes})
            if argv[:2] == ["panes", "wait"]:
                self.waits.append(argv[argv.index("--for") + 1])
                return "{}"
            if argv[:2] == ["agent-control", "send"]:
                self.sent.append(argv)
                if self.on_send:
                    self.on_send()
                return "{}"
            raise AssertionError(argv)
        return run

    on_send = None

    def perform(self, actions, panes):
        with mock.patch.object(agents_kilix, "_run", side_effect=self.kilix(panes)):
            return agents_kilix.perform(actions, cwd="/w")

    @staticmethod
    def agent_pane(provider, activity="agent", pane_id=5, cwd="/w/kilix"):
        return {"pane_id": pane_id, "cwd": cwd, "activity": activity, "broker": {"session_id": BROKER},
                "coding_session": {"provider": provider, "cwd": cwd}}

    @staticmethod
    def tmux_pane(pane_id=6, client=4000, cwd="/w/kilix"):
        return {"pane_id": pane_id, "cwd": cwd, "activity": "running", "coding_session": None,
                "broker": {"session_id": BROKER},
                "process": {"name": "tmux", "argv": ["tmux", "new-session", "-s", "restore", "claude"],
                            "foreground": [{"pid": client, "argv": ["tmux", "new-session", "-s", "restore",
                                                                    "claude", "--resume", "abc"], "cwd": cwd}]}}

    def tell(self, agent, text="go", **extra):
        return agents.Action("tell", {"session": f"{agent}@kilix", "text": text, **extra})


class KilixListsTheSessionButNotItsState(Harness):
    def test_codex_idle_composer_takes_a_message(self):
        self.screens[5] = lambda: screen("codex_idle") if not self.sent else screen("codex_working")
        results = self.perform([self.tell("codex", "run the suite")], [self.agent_pane("codex")])
        self.assertEqual(results[0]["outcome"], "done", results)
        self.assertEqual(results[0]["delivery"], "delivered")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("--expect-broker", self.sent[0])
        self.assertEqual(self.waits, [])          # delivery was confirmed by reading the screen, not `panes wait`

    def test_codex_working_holds_a_message_it_cannot_steer(self):
        self.screens[5] = screen("codex_working")
        results = self.perform([self.tell("codex")], [self.agent_pane("codex")])
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertIn("only when idle", results[0]["reason"])
        self.assertEqual(self.sent, [])

    def test_codex_approval_is_reported_as_waiting_for_approval(self):
        self.screens[5] = screen("codex_approval")
        results = self.perform([self.tell("codex")], [self.agent_pane("codex")])
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertIn("waiting for an approval", results[0]["reason"])
        self.assertEqual(self.sent, [])

    def test_claude_idle_at_the_prompt_with_a_background_monitor_takes_a_message(self):
        for name in ("claude_idle_monitor", "claude_idle_plain"):
            self.sent.clear()
            self.screens[5] = lambda: screen(name) if not self.sent else screen("claude_working")
            results = self.perform([self.tell("claude")], [self.agent_pane("claude")])
            self.assertEqual((results[0]["outcome"], results[0].get("delivery")), ("done", "delivered"), name)

    def test_claude_working_may_be_steered_and_claude_approval_holds(self):
        self.screens[5] = screen("claude_working")
        results = self.perform([self.tell("claude")], [self.agent_pane("claude")])
        self.assertEqual((results[0]["outcome"], results[0]["delivery"]), ("done", "queued"))
        self.sent.clear()
        self.screens[5] = screen("claude_approval")
        results = self.perform([self.tell("claude")], [self.agent_pane("claude")])
        self.assertIn("waiting for an approval", results[0]["reason"])
        self.assertEqual(self.sent, [])

    def test_an_unclear_screen_holds_the_message(self):
        for name, provider in (("codex_draft", "codex"), ("claude_draft", "claude")):
            self.screens[5] = screen(name)
            results = self.perform([self.tell(provider)], [self.agent_pane(provider)])
            self.assertIn("not idle or working", results[0]["reason"], name)
        self.screens[5] = ""
        self.assertEqual(self.perform([self.tell("codex")], [self.agent_pane("codex")])[0]["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_a_state_kilix_names_is_never_second_guessed_by_the_screen(self):
        self.screens[5] = screen("codex_idle")
        results = self.perform([self.tell("codex")], [self.agent_pane("codex", activity="working")])
        self.assertIn("only when idle", results[0]["reason"])
        self.assertEqual(self.screen_reads, [])
        results = self.perform([self.tell("codex")], [self.agent_pane("codex", activity="shell")])
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertEqual(self.screen_reads, [])

    def test_only_claude_and_codex_screens_are_read(self):
        self.screens[5] = screen("codex_idle")
        for provider, agent in (("grok", "grok"), ("omp", "qwen-omp"), ("kimi", "kimi")):
            results = self.perform([self.tell(agent)], [self.agent_pane(provider)])
            self.assertEqual(results[0]["outcome"], "failed")
        self.assertEqual((self.screen_reads, self.sent), ([], []))

    def test_a_wait_for_idle_polls_the_screen_when_kilix_cannot_see_the_state(self):
        frames = iter([screen("codex_working"), screen("codex_working"), screen("codex_idle")])
        self.screens[5] = lambda: next(frames)
        action = agents.Action("wait", {"session": "codex@kilix", "for": "idle"})
        results = self.perform([action], [self.agent_pane("codex")])
        self.assertEqual(results[0]["outcome"], "done", results)
        self.assertEqual(self.waits, [])

    def test_a_wait_for_waiting_sees_an_approval_prompt(self):
        self.screens[5] = screen("codex_approval")
        action = agents.Action("wait", {"session": "codex@kilix", "for": "waiting"})
        self.assertEqual(self.perform([action], [self.agent_pane("codex")])[0]["outcome"], "done")

    def test_a_polled_wait_gives_up_at_its_timeout(self):
        self.screens[5] = screen("codex_working")
        action = agents.Action("wait", {"session": "codex@kilix", "for": "idle", "timeout": 3})
        results = self.perform([action], [self.agent_pane("codex")])
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertIn("timed out after 3s", results[0]["reason"])
        self.assertLess(self.clock[0] - 1000.0, 10)       # virtual seconds, not an hour

    def test_the_default_wait_gives_up_after_an_hour_of_polling_not_never(self):
        self.screens[5] = screen("codex_working")
        action = agents.Action("wait", {"session": "codex@kilix", "for": "idle"})
        results = self.perform([action], [self.agent_pane("codex")])
        self.assertIn("timed out after 3600s", results[0]["reason"])

    def test_kilix_tracked_panes_still_use_kilix_waits(self):
        action = agents.Action("wait", {"session": "codex@kilix", "for": "idle"})
        results = self.perform([action], [self.agent_pane("codex", activity="working")])
        self.assertEqual((results[0]["outcome"], self.waits), ("done", ["idle"]))


class InsideTmux(Harness):
    def build(self, *, agents_in=((4100, ["claude", "--resume", "abc"], "/w/kilix"),), panes=None, screens=None,
              clients=None):
        self.procs.add(4000, 1, ["tmux", "new-session"], "/w")
        self.procs.add(4090, 1, ["tmux: server"], "/")
        self.procs.add(4101, 4090, ["bash"], "/w")
        for pid, argv, cwd, *parent in agents_in:
            self.procs.add(pid, parent[0] if parent else 4090, argv, cwd)
        self.tmux.panes = panes if panes is not None else [("%7", 4100, 1, 1, 0)]
        self.tmux.screens = screens if screens is not None else {"%7": screen("claude_idle_monitor")}
        if clients is not None:
            self.tmux.clients = clients

    def find(self, **kwargs):
        with mock.patch.object(agents_kilix, "_run", side_effect=self.kilix([self.tmux_pane(**kwargs)])):
            return agents_kilix.find_session("claude", DIRECTORY, caller_pane=1)

    def test_a_claude_session_inside_tmux_is_found_through_its_clients_own_session(self):
        self.build()
        found = self.find()
        self.assertEqual(found["pane_id"], 6)
        hosted = found["_hosted"]
        self.assertEqual((hosted["tmux_pane"], hosted["session_id"], hosted["client_pid"], hosted["provider"]),
                         ("%7", "$3", 4000, "claude"))
        self.assertEqual(self.tmux.calls[0][-1], "#{client_pid}\t#{session_id}")

    def test_a_message_reaches_only_the_kilix_pane_of_that_session_and_only_while_tmux_shows_it(self):
        self.build(screens={"%7": screen("claude_idle_monitor")})
        self.on_send = lambda: self.tmux.screens.update({"%7": screen("claude_working")})
        results = self.perform([self.tell("claude", "continue")], [self.tmux_pane()])
        self.assertEqual((results[0]["outcome"], results[0]["delivery"]), ("done", "delivered"), results)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][:5], ["agent-control", "send", "6", "--expect-broker", BROKER])
        self.assertIn("capture-pane", [part for call in self.tmux.calls for part in call])
        self.assertTrue(all(call[call.index("-t") + 1] in ("$3", "%7") for call in self.tmux.calls if "-t" in call))

    def test_a_tmux_pane_that_is_not_the_one_tmux_shows_is_never_typed_into(self):
        for active, window, mode in ((0, 1, 0), (1, 0, 0), (1, 1, 1)):
            self.build(panes=[("%7", 4100, active, window, mode)])
            self.sent.clear()
            results = self.perform([self.tell("claude")], [self.tmux_pane()])
            self.assertEqual(results[0]["outcome"], "failed", (active, window, mode))
            self.assertIn("another pane or a mode", results[0]["reason"])
            self.assertEqual(self.sent, [])

    def test_the_state_comes_from_that_tmux_pane_and_unclear_or_waiting_holds(self):
        for name, expected in (("claude_approval", "waiting for an approval"), ("claude_draft", "not idle or working")):
            self.build(screens={"%7": screen(name)})
            results = self.perform([self.tell("claude")], [self.tmux_pane()])
            self.assertIn(expected, results[0]["reason"])
            self.assertEqual(self.sent, [])
        self.build(screens={"%7": screen("claude_idle_monitor"), "%8": screen("claude_approval")},
                   panes=[("%7", 4100, 1, 1, 0), ("%8", 4101, 0, 1, 0)])
        self.on_send = lambda: self.tmux.screens.update({"%7": screen("claude_working")})
        self.assertEqual(self.perform([self.tell("claude")], [self.tmux_pane()])[0]["outcome"], "done")

    def test_only_the_exact_directory_counts(self):
        self.build(agents_in=((4100, ["claude"], "/w/kilix-other"),))
        with self.assertRaises(agents_kilix.AgentsError) as caught:
            self.find()
        self.assertIn("no live claude sessions", str(caught.exception))
        self.build(agents_in=((4100, ["claude"], "/w/kilix/sub"),))
        with self.assertRaises(agents_kilix.AgentsError):
            self.find()
        self.build(agents_in=((4100, ["claude"], "/w"),))
        with self.assertRaises(agents_kilix.AgentsError):
            self.find()

    def test_another_provider_or_a_plain_shell_in_tmux_is_not_a_claude_session(self):
        self.build(agents_in=((4100, ["codex"], "/w/kilix"),))
        with self.assertRaises(agents_kilix.AgentsError):
            self.find()
        self.build(agents_in=())
        with self.assertRaises(agents_kilix.AgentsError):
            self.find()

    def test_two_matches_refuse_whether_both_are_in_tmux_or_one_is_listed_by_kilix(self):
        self.procs.add(4200, 4090, ["claude"], "/w/kilix")
        self.build(panes=[("%7", 4100, 1, 1, 0), ("%8", 4200, 0, 1, 0)])
        with self.assertRaises(agents_kilix.AgentsError) as caught:
            self.find()
        self.assertIn("2 live claude sessions", str(caught.exception))
        self.build()
        both = [self.tmux_pane(), self.agent_pane("claude", pane_id=9)]
        with mock.patch.object(agents_kilix, "_run", side_effect=self.kilix(both)):
            with self.assertRaises(agents_kilix.AgentsError) as caught:
                agents_kilix.find_session("claude", DIRECTORY, caller_pane=1)
        self.assertIn("2 live claude sessions", str(caught.exception))

    def test_agent_processes_of_one_tmux_pane_must_agree_on_a_directory(self):
        self.build(agents_in=((4100, ["claude"], "/w/kilix"), (4110, ["node", "/opt/claude"], "/w/elsewhere", 4100)))
        self.assertEqual(detect.tmux_hosted(self.tmux_pane(), self.tmux, proc_root=self.procs.root), [])
        with self.assertRaises(agents_kilix.AgentsError):
            self.find()
        self.build(agents_in=((4100, ["claude"], "/w/kilix"), (4110, ["node", "/opt/claude"], "/w/kilix", 4100)))
        self.assertEqual(self.find()["_hosted"]["tmux_pane"], "%7")

    def test_only_a_tmux_client_in_the_foreground_names_a_tmux_session(self):
        self.build()
        for argv in (["vim", "notes.txt"], ["bash"], ["tmuxinator", "start"], ["/usr/bin/not-tmux"]):
            pane = self.tmux_pane()
            pane["process"]["foreground"][0]["argv"] = argv
            self.assertEqual(detect.tmux_clients(pane), [], argv)
            with mock.patch.object(agents_kilix, "_run", side_effect=self.kilix([pane])):
                with self.assertRaises(agents_kilix.AgentsError):
                    agents_kilix.find_session("claude", DIRECTORY, caller_pane=1)
        self.assertEqual(self.tmux.calls, [])

    def test_a_client_tmux_does_not_list_names_no_session(self):
        for clients in ({}, {4001: "$3"}, {4000: "$9"}):
            self.build(clients=clients)
            with self.assertRaises(agents_kilix.AgentsError):
                self.find()

    def test_a_pane_listed_by_kilix_as_a_coding_session_is_not_searched_through_tmux(self):
        self.build()
        pane = self.tmux_pane()
        pane["coding_session"] = {"provider": "codex", "cwd": "/w/kilix"}
        with mock.patch.object(agents_kilix, "_run", side_effect=self.kilix([pane])):
            with self.assertRaises(agents_kilix.AgentsError):
                agents_kilix.find_session("claude", DIRECTORY, caller_pane=1)
        self.assertEqual(self.tmux.calls, [])

    def test_the_callers_own_pane_is_never_a_target(self):
        self.build()
        with mock.patch.object(agents_kilix, "_run", side_effect=self.kilix([self.tmux_pane(pane_id=1)])):
            with self.assertRaises(agents_kilix.AgentsError):
                agents_kilix.find_session("claude", DIRECTORY, caller_pane=1)

    def test_tmux_unavailable_is_no_evidence_not_a_guess(self):
        self.build()

        def broken(args):
            raise agents_kilix.AgentsError("tmux failed")
        with mock.patch.object(agents_kilix, "_tmux", broken):
            with self.assertRaises(agents_kilix.AgentsError):
                self.find()

    def test_the_session_must_still_be_the_same_process_when_the_message_is_sent(self):
        self.build()
        original = self.tmux
        panes = iter([[("%7", 4100, 1, 1, 0)]])

        def changed_after_find(args):
            if "list-panes" in args:
                try:
                    self.tmux.panes = next(panes)
                except StopIteration:
                    self.procs.add(4100, 4090, ["claude", "--resume", "other"], "/w/kilix-swapped")
            return original(args)
        with mock.patch.object(agents_kilix, "_tmux", changed_after_find):
            results = self.perform([self.tell("claude")], [self.tmux_pane()])
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertIn("changed", results[0]["reason"])
        self.assertEqual(self.sent, [])

    def test_kilix_listing_a_session_after_the_search_holds_the_message(self):
        self.build()
        first = [True]

        def listing():
            if first[0]:
                first[0] = False
                return [self.tmux_pane()]
            pane = self.tmux_pane()
            pane["coding_session"] = {"provider": "claude", "cwd": "/w/kilix"}
            return [pane]
        results = self.perform([self.tell("claude")], listing)
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_a_wait_for_idle_polls_the_tmux_pane(self):
        self.build()
        frames = iter([screen("claude_working"), screen("claude_working"), screen("claude_idle_monitor")])
        original = self.tmux

        def tmux(args):
            if "capture-pane" in args:
                return next(frames)
            return original(args)
        with mock.patch.object(agents_kilix, "_tmux", tmux):
            action = agents.Action("wait", {"session": "claude@kilix", "for": "idle"})
            results = self.perform([action], [self.tmux_pane()])
        self.assertEqual(results[0]["outcome"], "done", results)
        self.assertEqual(self.waits, [])

    def test_a_dry_run_names_the_exact_delivery_without_sending(self):
        self.build()
        with mock.patch.object(agents_kilix, "_run", side_effect=self.kilix([self.tmux_pane()])):
            results = agents_kilix.perform([agents.Action("tell", {"session": "claude@kilix", "text": "go", "wait": True})],
                                           cwd="/w", dry_run=True)
        self.assertEqual(results[0]["outcome"], "would")
        self.assertEqual(self.sent, [])

    def test_the_real_proc_reader_reads_this_process(self):
        me = os.getpid()
        self.assertIn(me, detect.process_tree(me))
        self.assertEqual(detect.process_argv(me)[0:1] != [], True)
        self.assertEqual(detect.process_cwd(me), os.getcwd())
        self.assertEqual(detect.process_argv(2 ** 22 + 1), [])
        self.assertEqual(detect.process_cwd(2 ** 22 + 1), "")

    def test_process_tree_and_clients(self):
        self.build()
        self.procs.add(4120, 4100, ["sh", "-c", "claude"], "/w")
        self.assertEqual(detect.process_tree(4090, self.procs.root), [4090, 4100, 4101, 4120])
        self.assertEqual(detect.process_argv(4100, self.procs.root), ["claude", "--resume", "abc"])
        self.assertEqual(detect.process_cwd(4100, self.procs.root), "/w/kilix")
        self.assertEqual(detect.tmux_clients(self.tmux_pane()), [(4000, [])])
        pane = self.tmux_pane()
        pane["process"]["foreground"][0]["argv"] = ["tmux", "-S", "/srv/t/sock", "attach"]
        self.assertEqual(detect.tmux_clients(pane), [(4000, ["-S", "/srv/t/sock"])])
        self.assertEqual(detect.tmux_clients({"process": {"foreground": [{"pid": "x", "argv": ["tmux"]}, "junk"]}}), [])


if __name__ == "__main__":
    unittest.main()
