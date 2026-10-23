"""Owned helper cleanup after timeouts and cancellation; no live sessions."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

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
                    mock.patch.object(pty_backend.subprocess, "Popen", return_value=process), \
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
                mock.patch.object(pty_backend.subprocess, "Popen", return_value=process), \
                mock.patch.object(pty_backend.os, "killpg") as signal_group:
            self.assertEqual(pty_backend.call(["pty", "list"], wall=0.2).kind, "timeout")
        signal_group.assert_not_called()
        self.assertEqual(process.communicate.call_args_list, [mock.call(timeout=0.2), mock.call(timeout=1)])
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()
        process.wait.assert_called_once_with(timeout=1)
