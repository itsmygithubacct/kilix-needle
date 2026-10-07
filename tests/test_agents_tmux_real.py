"""A real private tmux server (never the default socket) and real processes under it.

Proves what the fakes cannot: tmux itself, not the Kilix pane or the tmux client, delivers to the
identified pane, and a client command prompt, a prefix table, choose-tree, copy mode or an active-pane
switch cannot receive or redirect the text. Skipped where tmux or `script` is missing.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import support  # noqa: F401
import agents
import agents_detect as detect
import agents_kilix

AGENT = r'''
import os, signal, sys, tty
OUT = sys.argv[1]
def replace(signum, frame):
    os.execv(sys.executable, [sys.executable, __file__, OUT, "--resume", "replacement-session"])
signal.signal(signal.SIGUSR1, replace)
RULE = "─" * 40
def draw(working=False):
    sys.stdout.write("\x1b[2J\x1b[Hassistant says hello\n\n" + RULE + "\n❯ \n" + RULE + "\n"
                     "  ⏵⏵ bypass permissions on" + (" · esc to interrupt" if working else "") + "\n")
    sys.stdout.flush()
tty.setcbreak(0)
draw()
buf = b""
while True:
    buf += os.read(0, 4096)
    while b"\r" in buf or b"\n" in buf:
        index = min(i for i in (buf.find(b"\r"), buf.find(b"\n")) if i >= 0)
        line, buf = buf[:index], buf[index + 1:]
        with open(OUT, "ab") as handle:
            handle.write(line + b"\n")
        draw(True)
'''

# A foreground reader that starts the agent in a group of its own: a background process.
BACKGROUND = r'''
import subprocess, sys
subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], process_group=0, stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for line in sys.stdin:
    with open(sys.argv[3], "a") as handle:
        handle.write(line)
'''


def until(test, limit=8.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        result = test()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("fixture condition timed out")


@unittest.skipUnless(shutil.which("tmux") and shutil.which("script"), "needs tmux and script")
class RealTmux(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="kn-tm-", dir="/tmp"))
        os.chmod(self.root, 0o700)
        self.socket = str(self.root / "s")
        self.clients = []
        self.log = []
        for name in ("a", "b"):
            (self.root / name).mkdir()
        (self.root / "claude").write_text(AGENT)
        (self.root / "launcher.py").write_text(BACKGROUND)
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "xterm-256color",
                    "HOME": str(self.root), "LANG": "C.UTF-8"}
        self.addCleanup(self.cleanup)
        self.hook = None
        patches = [mock.patch.object(agents_kilix, "_tmux", self.tmux),
                   mock.patch.object(agents_kilix, "resolve_dir", lambda said, cwd=None: self.directory),
                   mock.patch.object(agents_kilix, "POLL_SECONDS", 0.1)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.directory = self.root / "a"

    def cleanup(self):
        try:
            subprocess.run(["tmux", "-S", self.socket, "kill-server"], env=self.env, capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for process, handle in self.clients:
            process.stdin.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            handle.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # tmux, only ever on the private socket
    def tmux(self, args):
        assert list(args[:2]) == ["-S", self.socket], f"not the private socket: {args}"
        if self.hook:
            self.hook(args)
        done = subprocess.run(["tmux", *args], env=self.env, capture_output=True, text=True, timeout=10)
        self.log.append((list(args), done.returncode, done.stdout))
        if done.returncode:
            raise agents_kilix.AgentsError(done.stderr.strip() or f"tmux exited {done.returncode}")
        return done.stdout

    def t(self, *args):
        saved, self.hook = self.hook, None
        try:
            return self.tmux(["-S", self.socket, *args])
        finally:
            self.hook = saved

    def command(self, name):
        return f"exec python3 {shlex.quote(str(self.root / 'claude'))} {shlex.quote(str(self.root / (name + '.input')))}"

    def start(self):
        """One session with agent A (directory a) and agent B (directory b) side by side, one attached client."""
        out = self.t("-f", "/dev/null", "new-session", "-d", "-s", "main", "-x", "120", "-y", "24", "-P", "-F",
                     "#{session_id}\t#{pane_id}", "-c", str(self.root / "a"), self.command("a"))
        self.session, self.a = out.strip().split("\t")
        self.b = self.t("split-window", "-d", "-h", "-t", self.a, "-c", str(self.root / "b"), "-P", "-F",
                        "#{pane_id}", self.command("b")).strip()
        for pane in (self.a, self.b):
            until(lambda pane=pane: "❯" in self.t("capture-pane", "-p", "-t", pane))
        self.client_pid, self.client_tty = self.attach("one")
        self.outer = {"pane_id": 17, "activity": "running", "coding_session": None, "broker": {"session_id": "b" * 16},
                      "process": {"foreground": [{"pid": self.client_pid, "argv": ["tmux", "-S", self.socket, "attach"]}]}}

    def attach(self, label):
        before = {row.split("\t")[0] for row in self.t("list-clients", "-F", "#{client_pid}").splitlines()}
        handle = (self.root / f"{label}.out").open("wb")
        command = f"stty rows 24 cols 120; exec tmux -S {shlex.quote(self.socket)} attach-session -t {shlex.quote(self.session)}"
        process = subprocess.Popen(["script", "-q", "-f", "-c", command, str(self.root / f"{label}.typescript")],
                                   stdin=subprocess.PIPE, stdout=handle, stderr=subprocess.STDOUT, env=self.env)
        self.clients.append((process, handle))

        def new():
            for row in self.t("list-clients", "-F", "#{client_pid}\t#{client_tty}").splitlines():
                pid, tty = row.split("\t")
                if pid not in before:
                    return int(pid), tty
        return until(new)

    def input(self, name):
        path = self.root / f"{name}.input"
        return path.read_text() if path.exists() else ""

    def perform(self, text, *, pane=None, directory=None):
        calls = []

        def run(argv, timeout=30):
            if argv[:2] == ["agent-control", "list"]:
                return json.dumps({"caller_pane": 1, "panes": [{"pane_id": 1, "broker": "a" * 16, "cwd": str(self.root)}]})
            if argv[:2] == ["panes", "list"]:
                return json.dumps({"panes": [pane or self.outer]})
            if argv[:2] == ["panes", "wait"]:
                return "{}"
            calls.append(argv)             # agent-control send: must never happen for a tmux-hosted session
            return "{}"
        self.directory = directory or self.root / "a"
        with mock.patch.object(agents_kilix, "_run", side_effect=run):
            results = agents_kilix.perform([agents.Action("tell", {"session": "claude@fixture", "text": text,
                                                                  "timeout": 3})], cwd=str(self.root))
        self.assertEqual(calls, [], "the Kilix pane must not be used to deliver into tmux")
        return results[0]

    def hosted(self, pane=None):
        return detect.tmux_hosted(pane or self.outer, self.tmux)

    def agent(self, pane_id):
        return next(item for item in self.hosted() if item["tmux_pane"] == pane_id)

    # ---------------------------------------------------------------- the normal path

    def test_a_message_reaches_only_the_identified_panes_composer_exactly(self):
        self.start()
        found = {item["tmux_pane"]: item for item in self.hosted()}
        self.assertEqual(set(found), {self.a, self.b})
        self.assertTrue(found[self.a]["active"] and not found[self.b]["active"])
        text = "review 'this'; #{pane_pid} $(touch /nope) \"x\" `y` \\n é"
        result = self.perform(text)
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(self.input("a"), text + "\n")
        self.assertEqual(self.input("b"), "")
        self.assertFalse(any("kill-server" in call[0] for call in self.log))
        self.assertTrue(all(call[0][:2] == ["-S", self.socket] for call in self.log))
        self.assertFalse((self.root / "nope").exists())

    # ---------------------------------------------------------------- client-local state cannot take it

    def test_a_client_command_prompt_does_not_receive_the_text(self):
        self.start()
        marker = self.root / "command-executed"
        self.t("command-prompt", "-b", "-t", self.client_tty)
        hosted = self.agent(self.a)
        self.assertFalse(hosted["in_mode"])          # tmux does not call this a pane mode
        text = f"run-shell 'printf cn5 > {marker}'"
        result = self.perform(text)
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(self.input("a"), text + "\n")      # the agent got it; the prompt did not
        self.assertFalse(marker.exists())

    def test_a_pending_prefix_does_not_redirect_the_text(self):
        self.start()
        windows = self.t("list-windows", "-t", self.session).splitlines()
        self.t("switch-client", "-c", self.client_tty, "-T", "prefix")
        self.assertIn("prefix", self.t("list-clients", "-F", "#{client_key_table}"))
        result = self.perform("c")
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(self.input("a"), "c\n")
        self.assertEqual(self.t("list-windows", "-t", self.session).splitlines(), windows)

    def test_a_pane_mode_holds_what_tmux_itself_checks(self):
        self.start()
        for label, enter, leave in (
                ("copy mode", lambda: self.t("copy-mode", "-t", self.a),
                 lambda: self.t("send-keys", "-t", self.a, "-X", "cancel")),
                ("choose-tree", lambda: self.t("choose-tree", "-t", self.a),
                 lambda: self.t("send-keys", "-t", self.a, "q"))):
            hosted = self.agent(self.a)                # read while the pane is in no mode
            enter()
            until(lambda: self.t("display-message", "-p", "-t", self.a, "#{pane_in_mode}").strip() == "1")
            # the guard in tmux's own command refuses, whatever was read earlier
            self.assertFalse(detect.tmux_type(hosted, "FROM-STALE-READ", self.tmux), label)
            self.assertFalse(detect.tmux_enter(hosted, self.tmux), label)
            self.assertEqual(self.input("a"), "", label)
            self.assertEqual(self.t("list-buffers", "-F", "#{buffer_name}").strip(), "", label)
            # and the whole path holds too
            result = self.perform("go")
            self.assertEqual(result["outcome"], "failed", label)
            self.assertEqual(self.input("a"), "", label)
            leave()
            until(lambda: self.t("display-message", "-p", "-t", self.a, "#{pane_in_mode}").strip() == "0")

    def test_switching_the_active_pane_after_the_state_read_cannot_redirect_the_text(self):
        self.start()
        switched = []
        # the switch happens after tmux captured pane A, before the message is delivered
        original = self.tmux

        def after_capture(args):
            answer = original(args)
            if "capture-pane" in args and not switched:
                switched.append(True)
                self.t("select-pane", "-t", self.b)
            return answer
        with mock.patch.object(agents_kilix, "_tmux", after_capture):
            result = self.perform("CN5-RACE")
        self.assertTrue(switched)
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(self.input("a"), "CN5-RACE\n")
        self.assertEqual(self.input("b"), "")

    # ---------------------------------------------------------------- identity

    def test_a_background_agent_under_a_foreground_reader_is_never_found(self):
        self.start()
        (self.root / "bg").mkdir()
        self.t("kill-pane", "-t", self.b)
        launcher = f"exec python3 {self.root / 'launcher.py'} {self.root / 'claude'} {self.root / 'bg.input'} {self.root / 'shell.input'}"
        pane = self.t("split-window", "-d", "-h", "-t", self.a, "-c", str(self.root / "bg"), "-P", "-F", "#{pane_id}",
                      launcher).strip()
        time.sleep(0.5)                                   # the launcher has started its background agent
        found = {item["tmux_pane"] for item in self.hosted()}
        self.assertEqual(found, {self.a})                 # the pane whose agent owns the terminal; not the background one
        self.assertNotIn(pane, found)
        result = self.perform("CN5-SHELL", directory=self.root / "bg")
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(self.input("shell"), "")

    def test_an_agent_replaced_by_exec_in_the_same_process_is_not_sent_to(self):
        self.start()
        hosted = self.agent(self.a)
        before = detect.process_argv(hosted["pid"])
        replaced = []
        original = self.tmux

        def swap(args):
            answer = original(args)
            if "capture-pane" in args and not replaced:
                replaced.append(True)
                os.kill(hosted["pid"], signal.SIGUSR1)           # a pid recorded from this fixture
                until(lambda: detect.process_argv(hosted["pid"]) != before)
                until(lambda: "❯" in original(["-S", self.socket, "capture-pane", "-p", "-t", self.a]))
            return answer
        with mock.patch.object(agents_kilix, "_tmux", swap):
            result = self.perform("CN5-EXEC")
        self.assertTrue(replaced)
        self.assertEqual(result["outcome"], "failed", result)
        self.assertIn("changed", result["reason"])
        self.assertEqual(self.input("a"), "")

    def test_the_session_id_and_the_pane_are_the_ones_tmux_reports(self):
        self.start()
        for item in self.hosted():
            self.assertRegex(item["session_id"], r"^\$[0-9]+$")
            self.assertRegex(item["window_id"], r"^@[0-9]+$")
            self.assertRegex(item["tmux_pane"], r"^%[0-9]+$")
            self.assertEqual(item["client_pid"], self.client_pid)
            self.assertEqual(item["pgrp"], detect.proc_stat(item["pane_pid"]).pgrp)
            self.assertTrue(item["members"][0][1] > 0)


if __name__ == "__main__":
    unittest.main()
