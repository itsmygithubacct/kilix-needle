"""Owned helper cleanup after timeouts and cancellation; no live sessions."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pty_backend


class Cleanup(unittest.TestCase):
    def test_timeout_cleans_helper_after_leader_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "launcher"
            pidfile = Path(directory) / "child"
            script.write_text("#!/bin/sh\nsleep 30 &\necho $! > " + str(pidfile) + "\nexit 0\n")
            script.chmod(0o700)
            start = time.monotonic()
            with mock.patch.object(pty_backend, "kilix_executable", return_value=str(script)):
                reply = pty_backend.call(["pty", "list"], wall=0.3)
            self.assertEqual(reply.kind, "timeout")
            self.assertLess(time.monotonic() - start, 4)
            pid = int(pidfile.read_text())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                except (FileNotFoundError, ProcessLookupError):
                    break
                if state == "Z":
                    break
                time.sleep(0.01)
            else:
                self.fail("owned helper remained running")

    def test_cancellation_cleans_owned_child_then_propagates(self):
        process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        original = process.communicate
        calls = 0
        def interrupt_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt()
            return original(*args, **kwargs)
        try:
            with mock.patch.object(pty_backend, "kilix_executable", return_value="owned"), \
                    mock.patch.object(pty_backend, "_OwnedProcess", return_value=process), \
                    mock.patch.object(process, "communicate", side_effect=interrupt_once):
                with self.assertRaises(KeyboardInterrupt):
                    pty_backend.call(["pty", "list"])
            self.assertEqual(process.returncode, -9)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)

    def test_reaped_leader_is_not_signalled_and_drain_is_bounded(self):
        process = mock.Mock(returncode=0)
        process.communicate.side_effect = subprocess.TimeoutExpired("owned", 1)
        with mock.patch.object(pty_backend, "kilix_executable", return_value="owned"), \
                mock.patch.object(pty_backend, "_OwnedProcess", return_value=process), \
                mock.patch.object(pty_backend.os, "killpg") as signal_group:
            self.assertEqual(pty_backend.call(["pty", "list"], wall=0.2).kind, "timeout")
        signal_group.assert_not_called()
        self.assertEqual(process.communicate.call_args_list, [mock.call(timeout=0.2), mock.call(timeout=1)])
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()
        process.wait.assert_called_once_with(timeout=1)

    def test_real_interrupt_after_leader_exit_cleans_pipe_holding_helper(self):
        # Isolate subreaper state and the signal handler from the test runner.
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                 "--interrupt-fixture"], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("owned helper killed and reaped", result.stdout)

    def test_externally_reaped_child_is_not_signalled(self):
        process = pty_backend._OwnedProcess(["/bin/true"], start_new_session=True)
        os.waitpid(process.pid, 0)
        self.assertIsNone(process.returncode)
        with mock.patch.object(pty_backend.os, "killpg") as signal_group:
            pty_backend._signal_owned_group(process)
        signal_group.assert_not_called()
        process.poll()


def interrupt_fixture():
    import ctypes
    import shlex
    # Only this subprocess adopts its own orphan helper, so even a failing
    # regression can prove ownership and reap it without leaving live children.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot adopt fixture helper")
    with tempfile.TemporaryDirectory(prefix="pty-interrupt-") as directory:
        script = Path(directory) / "launcher"
        identities = Path(directory) / "identities"
        script.write_text("#!/bin/sh\nsleep 30 &\necho $! $$ > "
                          + shlex.quote(str(identities)) + "\nexit 0\n")
        script.chmod(0o700)
        observed_exit = False

        def interrupt(signum, frame):
            nonlocal observed_exit
            fields = identities.read_text().split() if identities.exists() else []
            if len(fields) == 2:
                helper, leader = map(int, fields)
                exited = os.waitid(os.P_PID, leader,
                                   os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None:
                    observed_exit = True
                    raise KeyboardInterrupt()
            signal.setitimer(signal.ITIMER_REAL, 0.01)

        old = signal.signal(signal.SIGALRM, interrupt)
        start = time.monotonic()
        cancelled = False
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.05)
            with mock.patch.object(pty_backend, "kilix_executable", return_value=str(script)):
                try:
                    pty_backend.call(["pty", "list"], wall=5)
                except KeyboardInterrupt:
                    cancelled = True
            assert cancelled and observed_exit, "real transport did not propagate cancellation"
            assert time.monotonic() - start < 4, "cancellation cleanup exceeded its bound"
            helper, leader = map(int, identities.read_text().split())
            exited = os.waitid(os.P_PID, helper, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            assert exited is not None, "owned pipe-holding helper remained running"
            assert exited.si_code == os.CLD_KILLED and exited.si_status == signal.SIGKILL
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
            if identities.exists():
                helper, leader = map(int, identities.read_text().split())
                try:
                    exited = os.waitid(os.P_PID, helper, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if exited is None:
                        os.kill(helper, signal.SIGKILL)
                    os.waitpid(helper, 0)
                except ChildProcessError:
                    pass
        print("owned helper killed and reaped")


if __name__ == "__main__":
    if sys.argv[1:] == ["--interrupt-fixture"]:
        interrupt_fixture()
    else:
        unittest.main()
