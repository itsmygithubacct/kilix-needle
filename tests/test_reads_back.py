"""A proposal for the tmux and pty jobs is admitted only if the job's own
sentence for it reads back as it (tmux_job.reads_back, pty_job.reads_back)."""
import unittest

import pty_job
import tmux_job

HEX = "0123456789abcdef0123"


class Tmux(unittest.TestCase):
    def test_every_form_reads_back(self):
        for request in ("list sessions", "new session build", 'new session build in "/home/pleb/src"',
                        "read build", "read build last 40 lines", "read %3", "read build:0.1 last 5 lines",
                        'send "make test" to build', "type 'echo \"hi\"' in build", "press Enter in build",
                        "rename session build to ci", "close session build"):
            action = tmux_job.parse(request)
            self.assertTrue(tmux_job.reads_back(action), request)

    def test_what_does_not_read_back(self):
        for action in ({"operation": "read", "target": "two words"},
                       {"operation": "read", "target": "build", "lines": 0},
                       {"operation": "read", "target": "build", "lines": "40"},
                       {"operation": "new", "name": "build", "cwd": "relative/dir"},
                       {"operation": "rename", "target": "build", "new_name": "has space"},
                       {"operation": "list", "extra": 1},
                       {"operation": "launch"}, {"operation": "read"}, None, "list sessions",
                       {"operation": "send", "target": "build", "text": "\"'`"}):
            self.assertFalse(tmux_job.reads_back(action), action)


class Pty(unittest.TestCase):
    def test_every_form_reads_back(self):
        for request in ("list sessions", "list archived journals", f"show session {HEX}",
                        'show session "build-1"', "which session is pane 12",
                        f"show the last 30 lines of session {HEX}", f"show archived journal {HEX}",
                        f"end session {HEX}"):
            action = pty_job.parse(request)
            self.assertTrue(pty_job.reads_back(action), request)

    def test_what_does_not_read_back(self):
        for action in ({"operation": "status", "id": "-x"}, {"operation": "status"},
                       {"operation": "observe", "id": HEX, "max_lines": 0},
                       {"operation": "observe", "id": HEX, "max_lines": 1001},
                       {"operation": "pane", "pane_id": "twelve"}, {"operation": "list", "all": True},
                       {"operation": "resolve"}, None):
            self.assertFalse(pty_job.reads_back(action), action)


if __name__ == "__main__":
    unittest.main()
