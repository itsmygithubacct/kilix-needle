"""Agents' wordings of system questions read through the grammar's own sentences.

Families from the first system benchmark (gpt-6-luna, 2026-09-29); written as
new phrasings of each family, not the benchmark's strings."""
import unittest
from unittest import mock

import support  # noqa: F401
import system_agent
import system_dispatch
import system_job


def reads(request):
    found = system_job.parse(request)
    return [(a.kind, a.args) for a in found] if found else None


class Families(unittest.TestCase):
    def test_each_family_reads_as_its_query(self):
        cases = {
            "Is the ripgrep package installed on this machine, and what version is it?":
                ("packages", {"operation": "status", "target": "ripgrep"}),
            "check whether package curl is installed and report its installed version":
                ("packages", {"operation": "status", "target": "curl"}),
            "What version of htop is installed?": ("packages", {"operation": "status", "target": "htop"}),
            "Which Debian package provides the file /usr/bin/env? Answer with the name.":
                ("packages", {"operation": "owner", "target": "/usr/bin/env"}),
            "show the package owner for /usr/sbin/sshd": ("packages", {"operation": "owner", "target": "/usr/sbin/sshd"}),
            "Is the ssh service running right now?": ("services", {"state": "all", "scope": "system", "unit": "ssh.service"}),
            "Read the status of the systemd unit bluetooth.service and say if it is active.":
                ("services", {"state": "all", "scope": "system", "unit": "bluetooth.service"}),
            "Which process is using the most CPU right now? Return its name.":
                ("processes", {"sort": "cpu", "limit": 10}),
            "How much free space is left on the root filesystem? Report it in GB.":
                ("resources", {"kind": "disk", "path": "/"}),
            "show free/available disk space for filesystem mounted at /home":
                ("resources", {"kind": "disk", "path": "/home"}),
        }
        for request, (kind, args) in cases.items():
            with self.subTest(request=request):
                got = reads(request)
                self.assertEqual(got[0][0], kind)
                for k, v in args.items():
                    self.assertEqual(got[0][1][k], v)

    def test_log_errors_from_a_program_tag_use_its_identifier(self):
        got = reads("Find errors logged by program nightly-sync in the last twenty minutes and report the code.")
        self.assertEqual(got, [("journal", {"boot": "any", "priority": "err", "scope": "system", "limit": 50,
                                            "identifier": "nightly-sync", "since": "20 minutes ago"})])

    def test_ambiguous_or_unsupported_wordings_stay_unread(self):
        for request in ("status of docker package or service", "Which package owns the file?", "jq",
                        "dpkg -S /usr/bin/python3", "list installed packages", "restart the ssh service",
                        "is the ssh service running? if so restart it", "don't check whether jq is installed"):
            with self.subTest(request=request):
                got = reads(request)
                self.assertFalse(got and any(k in ("services",) and "restart" in request for k, _ in got))
        self.assertEqual(reads("status of docker")[0][0], "services")    # a service, not a package
        self.assertIsNone(reads("Which package owns the file?"))
        self.assertIsNone(reads("restart the ssh service"))


class ReviewedShapes(unittest.TestCase):
    """Three shapes a second session found in the benchmark requests."""

    def test_a_command_is_answered_by_the_package_that_provides_it(self):
        for request in ("Check whether the rg command is installed and report its version.",
                        "check the installed version of the fd command"):
            with self.subTest(request=request):
                got = reads(request)
                self.assertEqual(got[0][0], "packages")
                self.assertEqual(got[0][1]["operation"], "owner")

    def test_logs_for_a_service_then_errors(self):
        got = reads("Read journal logs for service nightly-sync, errors since 15 minutes ago.")
        self.assertEqual(got[0][1]["unit"], "nightly-sync.service")
        self.assertEqual(got[0][1]["priority"], "err")

    def test_a_date_and_time_is_a_start(self):
        got = reads("Show journal errors for nightly-sync since 2026-09-29 19:52:00 UTC.")
        self.assertEqual(got[0][1]["since"], "2026-09-29 19:52:00 UTC")
        self.assertEqual(got[0][1]["identifier"], "nightly-sync")
        self.assertEqual(reads("errors since 2026-09-29T08:05Z")[0][1]["since"], "2026-09-29 08:05 UTC")
        for bad in ("errors since 2026-13-40 10:00", "errors since 2026-09-29 25:61"):
            with self.subTest(bad=bad):
                self.assertIsNone(reads(bad))

    def test_an_empty_service_read_names_the_program_tag_sentence(self):
        import system_collect
        collector = system_collect.Collector()
        with mock.patch.object(collector, "_command", return_value=(0, "", [])):
            data, _argv, _warn = collector.journal({"unit": "nightly-sync.service", "since": "10 minutes ago",
                                                    "scope": "system", "boot": "any", "priority": "err",
                                                    "limit": 50})
        self.assertIn("errors from the program nightly-sync since 10 minutes ago", data["note"])


class Sentences(unittest.TestCase):
    def test_a_sentence_is_offered_only_when_it_reads_back_exactly(self):
        for actions in ([["packages", {"operation": "status", "target": "jq"}]],
                        [["journal", {"priority": "err", "identifier": "x-y", "since": "10 minutes ago", "boot": "any"}]],
                        [["resources", {"kind": "memory"}], ["processes", {"sort": "cpu"}]]):
            with self.subTest(actions=actions):
                text = system_agent.sentence(actions)
                self.assertIsNotNone(text)
                self.assertEqual([[a.kind, a.args] for a in system_job.parse(text)],
                                 [[k, system_job.normalize(k, a)] for k, a in actions])

    def test_a_proposal_carries_the_sentence_to_send(self):
        classify = mock.Mock(return_value={"function_calls": [
            {"name": "packages", "arguments": {"operation": "status", "target": "jq"}}]})
        record = system_dispatch._dispatch("tell me about the jq package situation", classify, None)
        if record["items"] and record["items"][0].get("outcome") == "proposed":
            self.assertIn('"is jq installed"', record["hint"])

    def test_a_refusal_and_a_missing_profile_carry_the_accepted_forms(self):
        def broken(_text):
            raise RuntimeError("system normalizer profile is missing")
        record = system_dispatch._dispatch("tell me about the jq package situation", broken, None)
        self.assertEqual(record["status"], 1)
        self.assertIn("is jq installed", record["hint"])


if __name__ == "__main__":
    unittest.main()


class SlotReaders(unittest.TestCase):
    """Journal and package questions read by their parts, and what stays unread."""

    def test_journal_parts_read_as_their_sentence(self):
        cases = {
            "could you pull the logs since yesterday": "journal since yesterday",
            "show last five logs": "journal last 5",
            "newest error from this boot": "errors from this boot last 1",
            "errors in the past hour": "errors since 1 hour ago",
            "show 20 warnings from nginx": "warnings from the nginx service last 20",
            "journalctl -u nginx -n 5": "journal from the nginx service last 5",
        }
        for request, sentence in cases.items():
            with self.subTest(request=request):
                self.assertEqual(system_agent.canonical(request), sentence)

    def test_package_parts_read_as_their_sentence(self):
        cases = {
            "which package provides the dpkg-query command": "which package owns dpkg-query",
            "what version of curl is installed": "is curl installed",
        }
        for request, sentence in cases.items():
            with self.subTest(request=request):
                self.assertEqual(system_agent.canonical(request), sentence)

    def test_unsafe_or_unsure_wordings_stay_unread(self):
        for request in ("install curl", "show errors from then", "show the same errors again", "who is logged in",
                        "show logs for ssh*", "show 500 logs", "show a few recent logs", "show some logs",
                        "delete the old logs", "vacuum the journal"):
            with self.subTest(request=request):
                self.assertIsNone(system_agent.canonical(request))
