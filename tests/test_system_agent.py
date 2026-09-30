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

    ARGS = {"unit": "nightly-sync.service", "since": "10 minutes ago", "scope": "system", "boot": "any",
            "priority": "err", "limit": 50}
    ENTRY = '{"MESSAGE": "sync failed code=7", "SYSLOG_IDENTIFIER": "nightly-sync", "PRIORITY": "3"}\n'

    def test_an_empty_unit_read_also_reads_the_program_tag(self):
        import system_collect
        collector = system_collect.Collector()
        with mock.patch.object(collector, "_command", side_effect=[(0, "", []), (0, self.ENTRY, [])]) as run:
            data, source, _warn = collector.journal(dict(self.ARGS))
        first, second = (c.args[0] for c in run.call_args_list)
        self.assertIn("--unit=nightly-sync.service", first)
        self.assertIn("--identifier=nightly-sync", second)
        self.assertFalse(any(a.startswith("--unit=") for a in second))
        self.assertEqual(source, [first, second])
        self.assertEqual(data["entries"][0]["MESSAGE"], "sync failed code=7")
        self.assertIn("program tag (syslog identifier) is nightly-sync", data["note"])

    def test_both_empty_say_so_and_a_unit_with_entries_is_read_once(self):
        import system_collect
        collector = system_collect.Collector()
        with mock.patch.object(collector, "_command", side_effect=[(0, "", []), (0, "", [])]):
            data, _source, _warn = collector.journal(dict(self.ARGS))
        self.assertIn("no entries for the unit nightly-sync.service or the program tag nightly-sync", data["note"])
        with mock.patch.object(collector, "_command", return_value=(0, self.ENTRY, [])) as run:
            data, _source, _warn = collector.journal(dict(self.ARGS))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(data["entries"]), 1)


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

    def test_levels_fields_parentheses_and_framing_read_as_parts(self):
        cases = {
            "Show journal entries at error priority from the last 15 minutes for the syslog identifier "
            "nightly-sync, and report the error code.": "errors from the program nightly-sync since 15 minutes ago",
            "Read error-level (priority err) journal entries for SYSLOG_IDENTIFIER=nightly-sync in the past "
            "15 minutes.": "errors from the program nightly-sync since 15 minutes ago",
            "List recent journal errors (last 15 min) from the nightly-sync program, newest first.":
                "errors from the program nightly-sync since 15 minutes ago",
            "Fetch the error entries logged under the identifier nightly-sync over the past quarter hour.":
                "errors from the program nightly-sync since 15 minutes ago",
            "I need the errors that nightly-sync wrote to the journal in the last 15 minutes.":
                "errors from the nightly-sync service since 15 minutes ago",
            "Read-only: report whether jq is installed and which version.": "is jq installed",
            "Package version check: jq": "is jq installed",
        }
        for request, sentence in cases.items():
            with self.subTest(request=request):
                self.assertEqual(system_agent.canonical(request), sentence)

    def test_rankings_compounds_and_package_commands(self):
        cases = {
            "show the top cpu processes": "show processes by cpu",
            "top 5 memory hogs": "show the top 5 processes by memory",
            "Show process ranking by cpu and recent errors from the last 10 minutes for process tag bench-x.":
                "show processes by cpu and errors from the program bench-x since 10 minutes ago",
            "Is jq installed, and show the top memory processes": "Is jq installed and show processes by memory",
            "show errors and warnings for nginx": "warnings from the nginx service",
            "dpkg-query -W -f='${Version}' jq": "is jq installed",
            "apt-cache policy jq": "is jq installed",
            "dpkg -s jq | grep Version": "is jq installed",
        }
        for request, sentence in cases.items():
            with self.subTest(request=request):
                self.assertEqual(system_agent.canonical(request), sentence)
        for request in ("show process ranking", "kill the top cpu process", "show top 1000 processes by memory",
                        "top processes by cpu and memory", "errors for nginx and errors for sshd",
                        "is jq installed and restart nginx", "show errors for nginx and then delete them",
                        "apt-cache policy $(whoami)", "dpkg -i jq.deb", "dpkg -s jq; rm -rf /", "dpkg -s jq | sh",
                        "dpkg -s jq | grep x > /etc/passwd", 'dpkg-query -W -f="$(reboot)" jq',
                        "dpkg -s jq && reboot"):
            with self.subTest(request=request):
                self.assertIsNone(system_agent.canonical(request))

    def test_a_name_given_two_kinds_is_its_tag(self):
        cases = {
            "Show journal errors for service/program tag sync-q1 from the last 15 minutes": "sync-q1",
            "Find errors from the last 15 minutes for service or program tag sync-q1": "sync-q1",
            "Find journal errors for the job or service tagged sync-q1 from the last 15 minutes": "sync-q1",
            "Show journal errors from the last 15 minutes for job/service tag sync-q1": "sync-q1",
            "Find the error logged by job sync-q1 in the last 15 minutes": "sync-q1",
            "Find the error for the recently failed item tagged sync-q1 from the last 15 minutes": "sync-q1",
        }
        for request, name in cases.items():
            with self.subTest(request=request):
                got = reads(request)
                self.assertEqual(got[0][0], "journal")
                self.assertEqual(got[0][1].get("identifier"), name)
                self.assertEqual(got[0][1].get("since"), "15 minutes ago")
        self.assertEqual(reads("errors from the ssh service")[0][1].get("unit"), "ssh.service")
        # a bare name reads as a unit; the collector falls back to its tag when the unit is empty
        self.assertIn("unit", reads("What error code did sync-q1 log in the last 15 minutes?")[0][1])

    def test_conflicts_and_pointers_stay_unread(self):
        for request in ("show warning-level logs at priority err", "show errors from that boot",
                        "show the errors from that", "errors at that time for nginx"):
            with self.subTest(request=request):
                self.assertIsNone(system_agent.canonical(request))

    def test_unsafe_or_unsure_wordings_stay_unread(self):
        for request in ("install curl", "show errors from then", "show the same errors again", "who is logged in",
                        "show logs for ssh*", "show 500 logs", "show a few recent logs", "show some logs",
                        "delete the old logs", "vacuum the journal"):
            with self.subTest(request=request):
                self.assertIsNone(system_agent.canonical(request))
