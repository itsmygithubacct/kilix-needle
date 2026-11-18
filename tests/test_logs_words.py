"""Plain-language requests for kilix_logs_read (logs_words)."""
import unittest

import logs_words


class Grammar(unittest.TestCase):
    def test_the_forms_it_reads(self):
        for request, args in (
                ("show the brief of pane 12", {"operation": "brief", "session": "12"}),
                ("give me a brief for the claude session in pane 7", {"operation": "brief", "session": "7"}),
                ("summarize pane 12's session", {"operation": "brief", "session": "12"}),
                ('brief on the session titled "nightly build"', {"operation": "brief", "session": "nightly build"}),
                ("show the events of pane 12", {"operation": "events", "session": "12"}),
                ("read the last 30 events of pane 12", {"operation": "events", "session": "12", "limit": 30}),
                ("show me the log of session 3f2a9c1e-77b0", {"operation": "events", "session": "3f2a9c1e-77b0"}),
                ("read pane 12's transcript", {"operation": "events", "session": "12"}),
                ('search the log of pane 12 for "connection refused"',
                 {"operation": "search", "session": "12", "query": "connection refused"}),
                ("search pane 12 for timeout", {"operation": "search", "session": "12", "query": "timeout"}),
                ("find 'OOM' in the log of pane 4", {"operation": "search", "session": "4", "query": "OOM"}),
                ("show the source of event e41", {"operation": "source", "event_id": "e41"}),
                ("read the raw log file /home/pleb/logs/build.log",
                 {"operation": "events", "file": "/home/pleb/logs/build.log", "provider": "raw"}),
                ("read the log file /var/log/app.log", {"operation": "events", "file": "/var/log/app.log", "provider": "raw"}),
                ("show the codex session file /home/pleb/.codex/sessions/a.jsonl",
                 {"operation": "events", "file": "/home/pleb/.codex/sessions/a.jsonl", "provider": "codex"}),
                ('search the log file /tmp/x.log for "error 42"',
                 {"operation": "search", "file": "/tmp/x.log", "provider": "raw", "query": "error 42"}),
                ("please show the brief of pane 12, thanks", {"operation": "brief", "session": "12"})):
            self.assertEqual(logs_words.parse(request), args, request)

    def test_what_it_does_not_read(self):
        for request in ("show the brief", "show the brief of this pane", "show the events of pane 12 and pane 13",
                        "read the last 500 events of pane 12", "search pane 12", "show the source of event",
                        "read the log file relative/path.log", "tail the journal", "show the system journal",
                        "search the log of pane 12 for \"a\nb\"", "", None, 12):
            self.assertIsNone(logs_words.parse(request), request)

    def test_the_tool_s_own_checks_decide(self):
        self.assertIsNone(logs_words.check({"operation": "brief", "session": "12"}))
        self.assertIsNotNone(logs_words.check({"operation": "search", "session": "12"}))       # no query
        self.assertIsNotNone(logs_words.check({"operation": "events", "file": "/x.log"}))       # no provider
        self.assertIsNotNone(logs_words.check({"operation": "events", "session": "1", "file": "/x.log",
                                               "provider": "raw"}))
        self.assertIsNotNone(logs_words.check({"operation": "events", "session": "1", "limit": 101}))


if __name__ == "__main__":
    unittest.main()
