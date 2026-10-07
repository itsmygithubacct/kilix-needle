"""Session-state detection beyond Kilix's reader: Codex and Claude screens, and tmux-hosted sessions.

Screens are fixtures in tests/data/agent_screens (the `real_*` ones are trimmed captures of live panes,
paths replaced). tmux and /proc are fakes here; tests/test_agents_tmux_real.py drives a real private tmux.
Rule under test: a false hold is acceptable; a false idle/working, or bytes reaching anything but the
identified agent's empty composer, is not.
"""
import json
import os
from pathlib import Path
import re
import tempfile
import textwrap
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
        ("codex", "codex_working", "working"), ("codex", "codex_idle", "idle"), ("codex", "codex_approval", "waiting"),
        ("codex", "codex_draft", None), ("codex", "codex_multiline_draft", None),
        ("claude", "claude_idle_monitor", "idle"), ("claude", "claude_idle_plain", "idle"),
        ("claude", "claude_working", "working"), ("claude", "claude_approval", "waiting"),
        ("claude", "claude_approval_wrapped", "waiting"), ("claude", "claude_draft", None),
        ("claude", "claude_multiline_draft", None), ("claude", "claude_working_with_draft", None),
        # trimmed captures of live panes
        ("claude", "real_claude_idle", "idle"), ("claude", "real_claude_working", "working"),
        ("codex", "real_codex_working", "working"),
    )

    def test_the_captured_screens(self):
        for provider, name, expected in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(detect.screen_state(provider, screen(name)), expected)

    def test_a_screen_is_read_only_for_claude_and_codex_and_not_across_them(self):
        self.assertIsNone(detect.screen_state("claude", screen("codex_idle")))
        self.assertIsNone(detect.screen_state("codex", screen("claude_idle_monitor")))
        self.assertIsNone(detect.screen_state("codex", screen("claude_idle_plain")))
        for provider in ("grok", "omp", "kimi", ""):
            self.assertIsNone(detect.screen_state(provider, screen("codex_idle")))
            self.assertIsNone(detect.screen_state(provider, screen("claude_idle_plain")))

    def test_the_bottom_region_decides_not_a_marker_in_the_transcript(self):
        rule = "─" * 60
        footer = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
        # a quoted working marker in the transcript above an empty prompt: not working, and it holds
        quoted = f"The docs say: esc to interrupt\n\n{rule}\n❯\n{rule}\n{footer}\n"
        self.assertIsNone(detect.screen_state("claude", quoted))
        # an earlier empty prompt in the transcript above a draft is not idle
        draft = f"Earlier example:\n{rule}\n❯\n{rule}\nassistant: ok\n{rule}\n❯ 1. fix\n{rule}\n{footer}\n"
        self.assertNotIn(detect.screen_state("claude", draft), ("idle", "working"))
        # a draft in the composer of a working Claude is not steerable
        self.assertIsNone(detect.screen_state("claude", screen("claude_working_with_draft")))
        # an old Codex working marker far above an empty composer: not idle, it holds
        old = "• Working (1s • esc to interrupt)\n" + "\n".join(f"transcript {n}" for n in range(30)) + \
              "\n\n› Ask Codex to do anything\n\n  gpt · /srv/x\n  ← for agents · ? for shortcuts\n"
        self.assertIsNone(detect.screen_state("codex", old))

    def test_wrapped_markers_are_read_across_lines_or_hold(self):
        rule = "─" * 60
        wrapped_question = "\n".join(textwrap.wrap("Would you like to run the following command?", 28)) + "\n1. Yes\n2. No\n"
        self.assertEqual(detect.screen_state("codex", wrapped_question + "› Ask Codex to do anything\n"), "waiting")
        self.assertEqual(detect.screen_state("claude", "Do you want to allow this\noperation?\nYes / No\n❯\n"), "waiting")
        for provider, text in (("claude", "esc to\ninterrupt\n❯\n"),
                               ("codex", "• Working\n(2s • esc to interrupt)\n› Ask Codex to do anything\n"),
                               ("claude", f"esc to\ninterrupt\n{rule}\n❯\n{rule}\n  ? for shortcuts\n")):
            self.assertNotEqual(detect.screen_state(provider, text), "idle", text)

    def test_the_composer_is_found_by_the_last_two_rules_not_the_first(self):
        """A horizontal rule in the transcript above does not move the composer."""
        rule = "─" * 60
        footer = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
        text = f"● Here is a section break:\n{rule}\nand more text after it\n\n{rule}\n❯\u00a0\n{rule}\n{footer}\n"
        self.assertEqual(detect.screen_state("claude", text), "idle")
        two = f"{rule}\nquoted block\n{rule}\n● ok\n\n{rule}\n❯ typing\n{rule}\n{footer}\n"
        self.assertIsNone(detect.screen_state("claude", two))

    def test_a_numbered_list_on_the_screen_holds_even_above_an_empty_composer(self):
        """A false hold, by design: a numbered choice anywhere is a menu until proven otherwise."""
        rule = "─" * 60
        footer = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
        text = f"● Steps:\n  1. First\n  2. Second\n\n{rule}\n❯\u00a0\n{rule}\n{footer}\n"
        self.assertIsNone(detect.screen_state("claude", text))
        self.assertIsNone(detect.screen_state("codex", "1. First\n2. Second\n\n› Ask Codex to do anything\n\n  gpt · /srv/x\n"))

    def test_unclear_screens_are_not_a_state(self):
        for provider in ("codex", "claude"):
            for text in ("", "   \n\n", "$ ls\nfile\n", "\x1b[2J garbage", "Working", "Ask Codex to do anything",
                         "• working (", "nothing here", "\x1b[2J", "❯ 1. fix", "› 1. fix"):
                with self.subTest(provider=provider, text=text):
                    self.assertIsNone(detect.screen_state(provider, text))

    def test_a_modal_or_a_truncated_region_holds(self):
        self.assertIsNone(detect.screen_state("codex", "Select an account:\npersonal | work\n› Ask Codex to do anything\n"))
        self.assertIsNone(detect.screen_state("claude", "Choose a project:\nalpha | beta\n❯\n"))
        rule = "─" * 60
        for text in (f"{rule}\n❯\n", f"❯\n{rule}\n  ? for shortcuts\n", f"{rule}\n❯\n{'─' * 40}\n  ? for shortcuts\n",
                     f"{rule}\n\n\n\n\n\n\n\n\n\n\n\n❯\n{rule}\n  ? for shortcuts\n",
                     f"{rule}\n❯\n{rule}\n  ? for shortcuts\n  extra\n  more\n  and more\n"):
            with self.subTest(text=text[:40]):
                self.assertIsNone(detect.screen_state("claude", text))
        self.assertIsNone(detect.screen_state("codex", "• done\n\n› Ask Codex to do anything\n  and then some more words\n"))

    def test_the_generated_oracle_cases_never_read_as_a_false_idle_or_working(self):
        """The reviewer's 36-case probe (rebuilt here): each case reads as its oracle or holds."""
        C, I = "❯", "› Ask Codex to do anything"
        W = "• Working (2s • esc to interrupt)"
        cases = [
            ("codex", I, "idle"), ("claude", C + "\n? for shortcuts 1 monitor", "idle"),
            ("codex", W + "\n" + "\n".join(f"progress {i}" for i in range(14)) + "\n" + I, "working"),
            ("codex", "Would you like to run the following command?\n" + "\n".join(f"command line {i}" for i in range(14)) + "\n" + I, "waiting"),
            ("codex", "\n".join(textwrap.wrap("Would you like to run the following command?", 28)) + "\n1. Yes\n2. No\n" + I, "waiting"),
            ("claude", "Do you want to allow this\noperation?\nYes / No\n" + C, "waiting"),
            ("claude", "Do you want to run this?\n" + "\n".join(f"command line {i}" for i in range(14)) + "\n" + C, "waiting"),
            ("codex", I + "\n1. fix\n2. run tests", "draft"), ("claude", C + "\n1. fix the failure", "draft"),
            ("claude", "Earlier prompt example:\n❯\nAssistant: explanation\n❯ 1. fix the failure", "draft"),
            ("codex", "Example composer:\n" + I + "\n› 1. fix the failure", "draft"),
            ("claude", "The status string is esc to interrupt\n❯ 1. fix the failure", "draft"),
            ("claude", "❯ 1. fix\n2. run tests", "draft"),
            ("claude", "Do you want to read about this?\nThis was a quoted example.\n" + C, "idle"),
            ("codex", "Would you like to learn more?\nThis was a quoted example.\n" + I, "idle"),
            ("claude", "The docs mention esc to interrupt\n" + C, "idle"),
            ("codex", W + "\n› 1. fix", "draft"), ("claude", "Working (esc to interrupt)\n❯ 1. fix", "draft"),
            ("codex", "Select an account:\npersonal | work\n" + I, "modal"), ("claude", "Choose a project:\nalpha | beta\n" + C, "modal"),
            ("claude", "esc to\ninterrupt\n" + C, "working"), ("codex", "• Working\n(2s • esc to interrupt)\n" + I, "working"),
        ]
        for provider in ("codex", "claude"):
            prompt = I if provider == "codex" else C
            cases.append((provider, prompt.split(" Ask")[0] + " 1. fix", "draft"))
            cases += [(provider, "", None), (provider, "unrecognized screen", None), (provider, "\x1b[2J", None)]
        for provider, text, oracle in cases:
            actual = detect.screen_state(provider, text)
            with self.subTest(provider=provider, text=text[:50], oracle=oracle):
                if actual in ("idle", "working"):
                    self.assertEqual(actual, oracle)

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
    """A fake /proc: a directory of numeric process folders with real-shaped stat lines."""

    def __init__(self, test):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-proc-")
        test.addCleanup(self.dir.cleanup)
        self.root = self.dir.name

    def add(self, pid, parent, argv, cwd, *, pgrp=None, tpgid=None, start=None):
        pgrp = pid if pgrp is None else pgrp
        tpgid = pgrp if tpgid is None else tpgid
        start = pid * 10 if start is None else start
        folder = Path(self.root) / str(pid)
        folder.mkdir(exist_ok=True)
        (folder / "stat").write_text(
            f"{pid} ({os.path.basename(argv[0])}) S {parent} {pgrp} {pgrp} 34816 {tpgid} 4194304 "
            f"0 0 0 0 0 0 0 0 20 0 1 0 {start}\n")
        (folder / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
        link = folder / "cwd"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(cwd)


class FakeTmux:
    """tmux's questions and its atomic conditional, answered from a table; every call recorded."""

    def __init__(self):
        self.clients = {4000: "$3"}
        # pane, pid, active, window_active, in_mode, window, dead
        self.panes = [["%7", 4100, 1, 1, 0, "@1", 0]]
        self.input_off = {}
        self.screens = {}
        self.buffers = {}
        self.typed = []
        self.enters = []
        self.calls = []
        self.before_dispatch = None
        self.after_enter = None
        self.any_session = False     # answer list-panes for any -t, as a tmux given an empty target might

    def row(self, pane):
        return next((r for r in self.panes if r[0] == pane), None)

    def __call__(self, args):
        self.calls.append(list(args))
        args = list(args)
        for flag in ("-S", "-L"):
            if flag in args[:2]:
                del args[args.index(flag):args.index(flag) + 2]
        verb = args[0]
        if verb == "list-clients":
            return "".join(f"{pid}\t{session}\n" for pid, session in self.clients.items())
        if verb == "list-panes":
            if args[args.index("-t") + 1] != "$3" and not self.any_session:
                return ""
            return "".join("\t".join(str(v) for v in r) + "\n" for r in self.panes)
        if verb == "capture-pane":
            return self.screens.get(args[args.index("-t") + 1], "")
        if verb == "set-buffer":
            self.buffers[args[args.index("-b") + 1]] = args[args.index("--") + 1]
            return ""
        if verb == "delete-buffer":
            self.buffers.pop(args[args.index("-b") + 1], None)
            return ""
        if verb == "if-shell":
            if self.before_dispatch:
                self.before_dispatch(args)
            pane, guard, then, otherwise = args[args.index("-t") + 1], args[-3], args[-2], args[-1]
            row = self.row(pane)
            ok = bool(row)
            if ok:
                expect = re.search(r"pane_id\},(%\d+)\},#\{==:#\{pane_pid\},(\d+)\}", guard)
                window = re.search(r"window_id\},(@\d+)\}", guard).group(1)
                ok = (expect.group(1) == row[0] and int(expect.group(2)) == row[1] and row[4] == 0 and row[6] == 0
                      and not self.input_off.get(pane) and window == row[5])
            if ok:
                if "paste-buffer" in then:
                    self.typed.append((pane, self.buffers.pop(re.search(r"-b (\S+)", then).group(1))))
                if "send-keys" in then:
                    self.enters.append(pane)
                    if self.after_enter:
                        self.after_enter()
            return "NEEDLE-SENT\n" if ok else "NEEDLE-HELD\n"
        raise AssertionError(args)


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
        patches = [
            mock.patch.object(detect, "PROC_ROOT", self.procs.root),
            mock.patch.object(agents_kilix, "_tmux", self.tmux),
            mock.patch.object(agents_kilix, "_screen", self.read_screen),
            mock.patch.object(agents_kilix, "resolve_dir", lambda said, cwd=None: DIRECTORY),
            # A virtual clock: a wait that is never satisfied ends at its timeout at once.
            mock.patch.object(agents_kilix.time, "sleep", self.advance),
            mock.patch.object(agents_kilix.time, "monotonic", lambda: self.clock[0]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def advance(self, seconds):
        self.clock[0] += seconds

    def read_screen(self, pane_id):
        self.screen_reads.append(pane_id)
        value = self.screens.get(pane_id, "")
        return value() if callable(value) else value

    on_send = None

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

    def perform(self, actions, panes, **kwargs):
        with mock.patch.object(agents_kilix, "_run", side_effect=self.kilix(panes)):
            return agents_kilix.perform(actions, cwd="/w", **kwargs)

    @staticmethod
    def agent_pane(provider, activity="agent", pane_id=5, cwd="/w/kilix", **coding):
        return {"pane_id": pane_id, "cwd": cwd, "activity": activity, "broker": {"session_id": BROKER},
                "coding_session": {"provider": provider, "cwd": cwd, **coding}}

    @staticmethod
    def tmux_pane(pane_id=6, client=4000, cwd="/w/kilix", activity="running"):
        return {"pane_id": pane_id, "cwd": cwd, "activity": activity, "coding_session": None,
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
        self.assertEqual(self.waits, [])

    def test_codex_working_holds_a_message_it_cannot_steer(self):
        self.screens[5] = screen("codex_working")
        results = self.perform([self.tell("codex")], [self.agent_pane("codex")])
        self.assertIn("only when idle", results[0]["reason"])
        self.assertEqual(self.sent, [])

    def test_codex_approval_is_reported_as_waiting_for_approval(self):
        self.screens[5] = screen("codex_approval")
        results = self.perform([self.tell("codex")], [self.agent_pane("codex")])
        self.assertIn("waiting for an approval", results[0]["reason"])
        self.assertEqual(self.sent, [])

    def test_claude_idle_at_the_prompt_with_a_background_monitor_takes_a_message(self):
        for name in ("claude_idle_monitor", "claude_idle_plain", "real_claude_idle"):
            self.sent.clear()
            self.screens[5] = lambda: screen(name) if not self.sent else screen("claude_working")
            results = self.perform([self.tell("claude")], [self.agent_pane("claude")])
            self.assertEqual((results[0]["outcome"], results[0].get("delivery")), ("done", "delivered"), name)

    def test_claude_working_with_an_empty_composer_may_be_steered_but_never_into_a_draft(self):
        self.screens[5] = screen("claude_working")
        results = self.perform([self.tell("claude")], [self.agent_pane("claude")])
        self.assertEqual((results[0]["outcome"], results[0]["delivery"]), ("done", "queued"))
        self.sent.clear()
        for name in ("claude_working_with_draft", "claude_approval", "claude_draft", "claude_multiline_draft"):
            self.screens[5] = screen(name)
            results = self.perform([self.tell("claude")], [self.agent_pane("claude")])
            self.assertEqual(results[0]["outcome"], "failed", name)
        self.assertEqual(self.sent, [])

    def test_an_unclear_screen_holds_the_message(self):
        for name, provider in (("codex_draft", "codex"), ("claude_draft", "claude"), ("codex_multiline_draft", "codex")):
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
        self.screens[5] = screen("claude_idle_plain")
        results = self.perform([self.tell("claude")], [self.agent_pane("claude", activity="waiting")])
        self.assertIn("waiting for an approval", results[0]["reason"])
        results = self.perform([self.tell("claude")], [self.agent_pane("claude", activity="shell")])
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertEqual((self.screen_reads, self.sent), ([], []))

    def test_only_generic_states_are_supplemented(self):
        self.assertEqual(detect.GENERIC_DIRECT, {"agent", "unknown"})
        self.assertEqual(detect.GENERIC_HOSTED, {"agent", "unknown", "running"})
        self.screens[5] = lambda: screen("claude_idle_plain") if not self.sent else screen("claude_working")
        for activity in ("unknown", "agent"):
            self.sent.clear()
            results = self.perform([self.tell("claude")], [self.agent_pane("claude", activity=activity)])
            self.assertEqual(results[0]["outcome"], "done", activity)
        for activity in ("running", "remote", "shell", ""):
            results = self.perform([self.tell("claude")], [self.agent_pane("claude", activity=activity)])
            self.assertEqual(results[0]["outcome"], "failed", activity)

    def test_only_claude_and_codex_screens_are_read(self):
        self.screens[5] = screen("codex_idle")
        for provider, agent in (("grok", "grok"), ("omp", "qwen-omp"), ("kimi", "kimi")):
            self.assertEqual(self.perform([self.tell(agent)], [self.agent_pane(provider)])[0]["outcome"], "failed")
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
        self.assertIn("timed out after 3s", results[0]["reason"])
        self.assertLess(self.clock[0] - 1000.0, 10)

    def test_the_default_wait_gives_up_after_an_hour_of_polling_not_never(self):
        self.screens[5] = screen("codex_working")
        action = agents.Action("wait", {"session": "codex@kilix", "for": "idle"})
        self.assertIn("timed out after 3600s", self.perform([action], [self.agent_pane("codex")])[0]["reason"])

    def test_kilix_tracked_panes_still_use_kilix_waits(self):
        action = agents.Action("wait", {"session": "codex@kilix", "for": "idle"})
        results = self.perform([action], [self.agent_pane("codex", activity="working")])
        self.assertEqual((results[0]["outcome"], self.waits), ("done", ["idle"]))


class DirectSendRechecksTheIdentity(Harness):
    """Before a direct send: the same session, in the same directory, as when it was found."""

    def swap(self, change):
        calls = [0]

        def panes():
            calls[0] += 1
            pane = self.agent_pane("claude", activity="idle", session_id="s1", live_pids=[11])
            if calls[0] >= 2:
                change(pane)
            return [pane]
        return panes

    def test_the_unchanged_session_is_sent_to(self):
        results = self.perform([self.tell("claude")], self.swap(lambda pane: None))
        self.assertEqual(results[0]["outcome"], "done", results)
        self.assertEqual(len(self.sent), 1)

    def test_a_directory_session_or_process_change_between_find_and_send_holds(self):
        for label, change in (("directory", lambda pane: pane["coding_session"].update(cwd="/w/other")),
                              ("session", lambda pane: pane["coding_session"].update(session_id="s2")),
                              ("pids", lambda pane: pane["coding_session"].update(live_pids=[12])),
                              ("provider", lambda pane: pane["coding_session"].update(provider="codex"))):
            self.sent.clear()
            results = self.perform([self.tell("claude")], self.swap(change))
            self.assertEqual(results[0]["outcome"], "failed", label)
            self.assertEqual(self.sent, [], label)

    def test_a_launched_session_is_checked_against_its_launch_directory(self):
        actions = [agents.Action("agent", {"agent": "codex", "dir": "kilix"}),
                   agents.Action("tell", {"session": "it", "text": "go"})]
        for cwd, outcome in (("/w/kilix", "done"), ("/w/other", "failed")):
            self.sent.clear()
            pane = {"pane_id": 9, "cwd": cwd, "activity": "idle", "broker": {"session_id": BROKER},
                    "coding_session": {"provider": "codex", "cwd": cwd}}

            def run(argv, timeout=30, pane=pane):
                if argv[:2] == ["agent-control", "list"]:
                    return json.dumps({"caller_pane": 1, "panes": [{"pane_id": 1, "broker": "a" * 16, "cwd": "/w"}]})
                if argv[:2] in (["agent-control", "new-tab"], ["agent-control", "split"]):
                    return json.dumps({"pane": {"pane_id": 9}})
                if argv[:2] == ["panes", "list"]:
                    return json.dumps({"panes": [pane]})
                if argv[:2] == ["agent-control", "send"]:
                    self.sent.append(argv)
                return "{}"
            with mock.patch.object(agents_kilix, "_run", side_effect=run):
                results = agents_kilix.perform(actions, cwd="/w")
            self.assertEqual(results[1]["outcome"], outcome, cwd)
            self.assertEqual(len(self.sent), 1 if outcome == "done" else 0)


class InsideTmux(Harness):
    def build(self, *, members=((4100, ["claude", "--resume", "abc"], "/w/kilix"),), panes=None, screens=None,
              clients=None):
        """Process 4100 is the tmux pane's root; each member is (pid, argv, cwd[, {parent, pgrp, tpgid, start}])."""
        self.procs.add(4000, 1, ["tmux", "new-session"], "/w", pgrp=4000)
        self.procs.add(4090, 1, ["tmux: server"], "/", pgrp=4090, tpgid=-1)
        self.procs.add(4101, 4090, ["bash"], "/w", pgrp=4101, tpgid=4101)
        for pid, argv, cwd, *extra in members:
            options = extra[0] if extra else {}
            self.procs.add(pid, options.get("parent", 4090), argv, cwd, pgrp=options.get("pgrp", pid),
                           tpgid=options.get("tpgid", 4100), start=options.get("start"))
        if panes is not None:
            self.tmux.panes = panes
        self.tmux.screens = screens if screens is not None else {"%7": screen("claude_idle_monitor")}
        if clients is not None:
            self.tmux.clients = clients
        # once Enter was pressed the agent starts a turn
        self.tmux.after_enter = lambda: self.tmux.screens.__setitem__("%7", screen("claude_working"))

    def find(self, **kwargs):
        with mock.patch.object(agents_kilix, "_run", side_effect=self.kilix([self.tmux_pane(**kwargs)])):
            return agents_kilix.find_session("claude", DIRECTORY, caller_pane=1)

    def refuses(self, **kwargs):
        with self.assertRaises(agents_kilix.AgentsError):
            self.find(**kwargs)

    def test_a_claude_session_inside_tmux_is_found_through_its_clients_own_session(self):
        self.build()
        found = self.find()
        self.assertEqual(found["pane_id"], 6)
        hosted = found["_hosted"]
        self.assertEqual((hosted["tmux_pane"], hosted["session_id"], hosted["window_id"], hosted["client_pid"],
                          hosted["provider"], hosted["pane_pid"], hosted["pgrp"]),
                         ("%7", "$3", "@1", 4000, "claude", 4100, 4100))
        self.assertEqual(hosted["members"][0][:3], (4100, 41000, ("claude", "--resume", "abc")))

    def test_a_message_is_typed_by_tmux_into_the_exact_pane_and_never_through_the_kilix_pane(self):
        self.build()
        self.on_send = lambda: self.fail("the outer Kilix pane must not be used")
        text = "continue with 'quotes'; #{pane_pid} $(x) \"d\" `b` \\n"
        results = self.perform([self.tell("claude", text)], [self.tmux_pane()])
        self.assertEqual((results[0]["outcome"], results[0]["delivery"]), ("done", "delivered"), results)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.tmux.typed, [("%7", text)])
        self.assertEqual(self.tmux.enters, ["%7"])
        self.assertEqual(self.tmux.buffers, {})
        dispatches = [c for c in self.tmux.calls if "if-shell" in c]
        self.assertEqual(len(dispatches), 2)
        for call in dispatches:
            self.assertEqual(call[call.index("-t") + 1], "%7")
            for part in ("pane_id},%7", "pane_pid},4100", "pane_in_mode},0", "pane_dead},0", "pane_input_off},0",
                         "window_id},@1", "session_id},$3"):
                self.assertIn(part, call[-3])
            self.assertNotIn(text, " ".join(call))      # the text is never inside a tmux command string

    def test_tmux_holding_the_text_types_nothing_and_holding_enter_leaves_a_pending_line(self):
        self.build()
        # the pane enters a mode (copy mode, choose-tree...) between our read and the atomic command
        self.tmux.before_dispatch = lambda args: self.tmux.panes[0].__setitem__(4, 1)
        results = self.perform([self.tell("claude")], [self.tmux_pane()])
        self.assertIn("nothing was typed", results[0]["reason"])
        self.assertEqual((self.tmux.typed, self.tmux.enters, self.tmux.buffers), ([], [], {}))
        # a mode that starts after the text was typed holds Enter, and says the line is pending
        self.build()
        self.tmux.panes[0][4] = 0
        count = [0]

        def late(args):
            count[0] += 1
            if count[0] == 2:
                self.tmux.panes[0][4] = 1
        self.tmux.before_dispatch = late
        results = self.perform([self.tell("claude")], [self.tmux_pane()])
        self.assertIn("pending", results[0]["reason"])
        self.assertEqual((len(self.tmux.typed), self.tmux.enters), (1, []))

    def test_every_guard_condition_holds_the_message(self):
        for label, change in (("dead", lambda t: t.panes[0].__setitem__(6, 1)),
                              ("input off", lambda t: t.input_off.__setitem__("%7", True)),
                              ("other window", lambda t: t.panes[0].__setitem__(5, "@9")),
                              ("other pid", lambda t: t.panes[0].__setitem__(1, 4999)),
                              ("pane gone", lambda t: t.panes.clear())):
            self.build(panes=[["%7", 4100, 1, 1, 0, "@1", 0]])
            self.tmux.input_off.clear()
            self.tmux.before_dispatch = lambda args, change=change: change(self.tmux)
            results = self.perform([self.tell("claude")], [self.tmux_pane()])
            self.assertEqual(results[0]["outcome"], "failed", label)
            self.assertEqual((self.tmux.typed, self.tmux.enters), ([], []), label)

    def test_the_active_pane_is_irrelevant_because_the_pane_is_addressed_by_id(self):
        self.build(panes=[["%7", 4100, 0, 0, 0, "@1", 0]], members=((4100, ["claude"], "/w/kilix"),),
                   screens={"%7": screen("claude_idle_plain")})
        results = self.perform([self.tell("claude", "go")], [self.tmux_pane()])
        self.assertEqual(results[0]["outcome"], "done", results)
        self.assertEqual((self.tmux.typed, self.sent), ([("%7", "go")], []))

    def test_text_that_would_not_type_as_one_line_is_held(self):
        self.build()
        for text in ("one\ntwo", "tab\there", "esc\x1b[2J", "nul\x00", "del\x7f"):
            results = self.perform([self.tell("claude", text)], [self.tmux_pane()])
            self.assertIn("control characters", results[0]["reason"], repr(text))
        self.assertEqual((self.tmux.typed, self.sent), ([], []))

    def test_the_state_comes_from_that_tmux_pane_and_unclear_or_waiting_holds(self):
        for name, expected in (("claude_approval", "waiting for an approval"), ("claude_draft", "not idle or working"),
                               ("claude_working_with_draft", "not idle or working"),
                               ("claude_multiline_draft", "not idle or working")):
            self.build(screens={"%7": screen(name)})
            results = self.perform([self.tell("claude")], [self.tmux_pane()])
            self.assertIn(expected, results[0]["reason"], name)
        self.assertEqual((self.tmux.typed, self.sent), ([], []))

    def test_named_kilix_states_win_for_tmux_panes_too(self):
        def named(activity):
            """Found with Kilix's generic `running`; by the time of the message Kilix names `activity`."""
            reads = [0]

            def listing():
                reads[0] += 1
                return [self.tmux_pane(activity=activity if reads[0] == 2 else "running")]
            return listing
        self.build(screens={"%7": screen("claude_idle_plain")})
        results = self.perform([self.tell("claude")], named("waiting"))
        self.assertIn("waiting for an approval", results[0]["reason"])
        # a named working that the screen contradicts holds, and so does a named idle it does not confirm
        self.assertEqual(self.perform([self.tell("claude")], named("working"))[0]["outcome"], "failed")
        self.build(screens={"%7": screen("claude_draft")})
        self.assertEqual(self.perform([self.tell("claude")], named("idle"))[0]["outcome"], "failed")
        self.assertEqual(self.tmux.typed, [])
        # a named idle that the screen confirms is used as named
        self.build(screens={"%7": screen("claude_idle_plain")})
        self.assertEqual(self.perform([self.tell("claude")], named("idle"))[0]["outcome"], "done")
        # shell/remote panes are not searched through tmux at all
        for activity in ("shell", "remote"):
            with mock.patch.object(agents_kilix, "_run", side_effect=self.kilix([self.tmux_pane(activity=activity)])):
                with self.assertRaises(agents_kilix.AgentsError):
                    agents_kilix.find_session("claude", DIRECTORY, caller_pane=1)

    def test_only_the_exact_directory_counts(self):
        for cwd in ("/w/kilix-other", "/w/kilix/sub", "/w"):
            self.build(members=((4100, ["claude"], cwd),))
            self.refuses()

    def test_another_provider_or_a_plain_shell_in_tmux_is_not_a_claude_session(self):
        self.build(members=((4100, ["codex"], "/w/kilix"),))
        self.refuses()
        self.build(members=())
        self.refuses()

    def test_two_matches_refuse_whether_both_are_in_tmux_or_one_is_listed_by_kilix(self):
        self.build(members=((4100, ["claude"], "/w/kilix"), (4200, ["claude"], "/w/kilix")),
                   panes=[["%7", 4100, 1, 1, 0, "@1", 0], ["%8", 4200, 0, 1, 0, "@1", 0]])
        self.procs.add(4200, 4090, ["claude"], "/w/kilix", pgrp=4200, tpgid=4200)
        with self.assertRaises(agents_kilix.AgentsError) as caught:
            self.find()
        self.assertIn("2 live claude sessions", str(caught.exception))
        self.build(panes=[["%7", 4100, 1, 1, 0, "@1", 0]])
        both = [self.tmux_pane(), self.agent_pane("claude", pane_id=9)]
        with mock.patch.object(agents_kilix, "_run", side_effect=self.kilix(both)):
            with self.assertRaises(agents_kilix.AgentsError) as caught:
                agents_kilix.find_session("claude", DIRECTORY, caller_pane=1)
        self.assertIn("2 live claude sessions", str(caught.exception))

    def test_agent_processes_of_one_tmux_pane_must_agree_on_a_directory(self):
        self.build(members=((4100, ["claude"], "/w/kilix"),
                            (4110, ["node", "/opt/claude"], "/w/elsewhere", {"parent": 4100, "pgrp": 4100})))
        self.assertEqual(detect.tmux_hosted(self.tmux_pane(), self.tmux, proc_root=self.procs.root), [])
        self.refuses()
        self.build(members=((4100, ["claude"], "/w/kilix"),
                            (4110, ["node", "/opt/claude"], "/w/kilix", {"parent": 4100, "pgrp": 4100})))
        self.assertEqual(self.find()["_hosted"]["tmux_pane"], "%7")

    def test_a_background_agent_never_qualifies(self):
        """A foreground shell with a background `claude` descendant: the shell owns the terminal."""
        self.build(members=((4100, ["bash"], "/w/kilix"),
                            (4150, ["claude"], "/w/kilix", {"parent": 4100, "pgrp": 4150})))
        self.refuses()
        # the same process in the terminal's foreground group qualifies
        self.build(members=((4100, ["bash"], "/w/kilix"),
                            (4150, ["claude"], "/w/kilix", {"parent": 4100, "pgrp": 4100})))
        self.assertEqual(self.find()["_hosted"]["pid"], 4150)

    def test_unknown_or_inconsistent_foreground_ownership_holds(self):
        for pgrp, tpgid in ((4100, -1), (4100, 0), (0, 0), (4100, 4999), (4999, 4100)):
            self.build(members=((4100, ["claude"], "/w/kilix", {"pgrp": pgrp, "tpgid": tpgid}),))
            self.refuses()
        # an agent child whose own terminal group disagrees with the pane root's
        self.build(members=((4100, ["bash"], "/w/kilix"),
                            (4150, ["claude"], "/w/kilix", {"parent": 4100, "pgrp": 4100, "tpgid": 4999})))
        self.refuses()
        self.build()
        (Path(self.procs.root) / "4100" / "stat").write_text("garbage")
        self.refuses()

    def test_malformed_tmux_identities_hold(self):
        bad_panes = (["7", 4100, 1, 1, 0, "@1", 0], ["%7", "x", 1, 1, 0, "@1", 0], ["%7", 4100, 1, 1, 0, "1", 0],
                     ["%7", 4100, 1, 1, 0, "", 0], ["%7", 4100, 2, 1, 0, "@1", 0], ["%7", 4100, 1, 1, 0, "@1", 1],
                     ["%7", 4100, 1, 1, 0, "@1", ""], ["%", 4100, 1, 1, 0, "@1", 0])
        for row in bad_panes:
            self.build(panes=[row])
            self.refuses()
        for session in ("", "3", "$", "$x", "main", "$3 "):
            self.build(clients={4000: session})
            self.refuses()
        self.build(clients={4000: "$3"}, panes=[["%7", 4100, 1, 1, 0, "@1", 0]])
        self.assertEqual(self.find()["pane_id"], 6)

    def test_a_session_id_that_is_not_dollar_n_is_never_queried(self):
        for session in ("", "main", "3", "$", "$x", "@1", "%1", "$3;kill-server"):
            self.build(clients={4000: session})
            self.tmux.any_session = True               # even a tmux that would answer for any target
            self.tmux.calls.clear()
            self.refuses()
            self.assertFalse([c for c in self.tmux.calls if "list-panes" in c], repr(session))

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
            self.refuses()

    def test_a_pane_kilix_lists_as_a_coding_session_is_not_searched_through_tmux(self):
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
            self.refuses()

    def test_every_identity_field_is_checked_again_at_dispatch(self):
        """Provider, pid, start time, argv, cwd, process group, session, window, client."""
        cases = (
            ("start time", lambda: self.procs.add(4100, 4090, ["claude", "--resume", "abc"], "/w/kilix", start=1)),
            ("argv (exec to another session)", lambda: self.procs.add(4100, 4090, ["claude", "--resume", "other"], "/w/kilix")),
            ("cwd", lambda: self.procs.add(4100, 4090, ["claude", "--resume", "abc"], "/w/kilix-swapped")),
            ("provider", lambda: self.procs.add(4100, 4090, ["codex"], "/w/kilix")),
            ("pid", lambda: (self.procs.add(4102, 4090, ["claude", "--resume", "abc"], "/w/kilix"),
                             self.tmux.panes.__setitem__(0, ["%7", 4102, 1, 1, 0, "@1", 0]))),
            ("window", lambda: self.tmux.panes[0].__setitem__(5, "@2")),
            ("session", lambda: self.tmux.clients.update({4000: "$4"})),
            ("client", lambda: self.tmux.clients.clear()),
            ("foreground group", lambda: self.procs.add(4100, 4090, ["claude", "--resume", "abc"], "/w/kilix", pgrp=4100, tpgid=4999)),
        )
        for label, change in cases:
            self.build()
            lists = [0]
            original = self.tmux.__call__

            def swapped(args, change=change, lists=lists, original=original):
                if "list-clients" in args:
                    lists[0] += 1
                    if lists[0] == 3:           # find (1), state (2), then the re-read at dispatch (3)
                        change()
                return original(args)
            with mock.patch.object(agents_kilix, "_tmux", swapped):
                results = self.perform([self.tell("claude")], [self.tmux_pane()])
            self.assertEqual(results[0]["outcome"], "failed", label)
            self.assertEqual((self.tmux.typed, self.tmux.enters), ([], []), label)

    def test_kilix_listing_a_coding_session_after_the_search_holds_the_message(self):
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
        self.assertEqual((self.tmux.typed, self.sent), ([], []))

    def test_a_wait_for_idle_polls_the_tmux_pane(self):
        self.build()
        frames = iter([screen("claude_working"), screen("claude_working"), screen("claude_idle_monitor")])
        original = self.tmux.__call__

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
        results = self.perform([agents.Action("tell", {"session": "claude@kilix", "text": "go", "wait": True})],
                               [self.tmux_pane()], dry_run=True)
        self.assertEqual(results[0]["outcome"], "would")
        self.assertEqual(results[0]["argv"][0], "tmux")
        self.assertIn("%7", results[0]["argv"])
        self.assertEqual((self.tmux.typed, self.sent), ([], []))

    def test_the_real_proc_reader_reads_this_process(self):
        me = os.getpid()
        self.assertIn(me, detect.process_tree(me))
        self.assertTrue(detect.process_argv(me))
        self.assertEqual(detect.process_cwd(me), os.getcwd())
        stat = detect.proc_stat(me)
        self.assertEqual((stat.parent, stat.pgrp == os.getpgrp()), (os.getppid(), True))
        self.assertGreater(stat.start_ticks, 0)
        self.assertEqual(detect.process_argv(2 ** 22 + 1), [])
        self.assertEqual(detect.process_cwd(2 ** 22 + 1), "")
        self.assertIsNone(detect.proc_stat(2 ** 22 + 1))

    def test_process_tree_clients_and_stat_parsing(self):
        self.build()
        self.procs.add(4120, 4100, ["sh", "-c", "claude"], "/w")
        self.assertEqual(detect.process_tree(4090, self.procs.root), [4090, 4100, 4101, 4120])
        self.assertEqual(detect.process_argv(4100, self.procs.root), ["claude", "--resume", "abc"])
        stat = detect.proc_stat(4100, self.procs.root)
        self.assertEqual((stat.parent, stat.pgrp, stat.tpgid, stat.start_ticks), (4090, 4100, 4100, 41000))
        # a command name with spaces and parentheses does not shift the fields
        self.procs.add(4130, 4100, ["a (b) c"], "/w")
        self.assertEqual(detect.proc_stat(4130, self.procs.root).parent, 4100)
        self.assertEqual(detect.tmux_clients(self.tmux_pane()), [(4000, [])])
        pane = self.tmux_pane()
        pane["process"]["foreground"][0]["argv"] = ["tmux", "-S", "/srv/t/sock", "attach"]
        self.assertEqual(detect.tmux_clients(pane), [(4000, ["-S", "/srv/t/sock"])])
        self.assertEqual(detect.tmux_clients({"process": {"foreground": [{"pid": "x", "argv": ["tmux"]}, "junk"]}}), [])


if __name__ == "__main__":
    unittest.main()
