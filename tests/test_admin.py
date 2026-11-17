"""The admin job: the checks read every verb and target; nothing runs on the host here.

Every runner below is a fake: no test runs systemctl, kill, sudo or apt-get.
"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401

import admin_cli
import admin_exec
import admin_job
import evaluate
import jobs
import needle_cli

A = admin_job.Action
REPO = Path(__file__).resolve().parent.parent


def service(op, unit, scope="system"):
    return A("service", {"operation": op, "unit": unit, "scope": scope})


def process(signal="term", **target):
    return A("process", {"signal": signal, **target})


def package(op, name):
    return A("packages", {"operation": op, "package": name})


def updates(op):
    return A("updates", {"operation": op})


class Reading(unittest.TestCase):
    def read(self, request):
        return admin_job.parse(request)

    def test_services(self):
        self.assertEqual(self.read("restart nginx"), [service("restart", "nginx")])
        self.assertEqual(self.read("please restart the nginx service."), [service("restart", "nginx")])
        self.assertEqual(self.read("can you reload apache2?"), [service("reload", "apache2")])
        self.assertEqual(self.read("bounce docker"), [service("restart", "docker")])
        self.assertEqual(self.read("sudo systemctl restart nginx.service"), [service("restart", "nginx")])
        self.assertEqual(self.read("restart postgresql@16-main"), [service("restart", "postgresql@16-main")])
        self.assertEqual(self.read("restart NetworkManager"), [service("restart", "NetworkManager")])
        self.assertEqual(self.read("start cron on this machine"), [service("start", "cron")])

    def test_user_scope_only_when_said(self):
        for request in ("restart the pipewire user service", "restart pipewire for my user",
                        "systemctl --user restart pipewire", "restart my user service pipewire"):
            self.assertEqual(self.read(request), [service("restart", "pipewire", "user")], request)
        self.assertEqual(self.read("restart pipewire"), [service("restart", "pipewire")])

    def test_processes(self):
        self.assertEqual(self.read("kill 4312"), [process(pid=4312)])
        self.assertEqual(self.read("stop process 4312"), [process(pid=4312)])
        self.assertEqual(self.read("kill -9 4312"), [process("kill", pid=4312)])
        self.assertEqual(self.read("force kill firefox"), [process("kill", name="firefox")])
        self.assertEqual(self.read("force-kill firefox"), [process("kill", name="firefox")])
        self.assertEqual(self.read("send hup to process 812"), [process("hup", pid=812)])
        self.assertEqual(self.read("pkill vlc"), [process(name="vlc")])
        self.assertEqual(self.read("end the htop process"), [process(name="htop")])

    def test_packages_and_updates(self):
        self.assertEqual(self.read("install curl, jq and htop"),
                         [package("install", "curl"), package("install", "jq"), package("install", "htop")])
        self.assertEqual(self.read("sudo apt install curl jq"), [package("install", "curl"), package("install", "jq")])
        self.assertEqual(self.read("uninstall nano"), [package("remove", "nano")])
        self.assertEqual(self.read("remove vim and its config files"), [package("purge", "vim")])
        self.assertEqual(self.read("completely remove vim"), [package("purge", "vim")])
        self.assertEqual(self.read("install Curl"), [package("install", "curl")])
        self.assertEqual(self.read("update the package lists and then upgrade"),
                         [updates("refresh"), updates("upgrade")])
        self.assertEqual(self.read("update the system"), [updates("upgrade")])
        self.assertEqual(self.read("sudo apt update"), [updates("refresh")])

    def test_shared_verbs_and_order(self):
        self.assertEqual(self.read("enable and start docker"), [service("enable", "docker"), service("start", "docker")])
        self.assertEqual(self.read("stop nginx, then start apache2"),
                         [service("stop", "nginx"), service("start", "apache2")])

    def test_refusals_never_read_an_action(self):
        for request in (
                "don't restart nginx", "never mind, restart nginx", "restart nginx, actually no",
                "restart nginx if it's down", "restart nginx at midnight", "restart nginx in 5 minutes",
                "restart or reload nginx", "should I restart nginx?", "my boss wants nginx restarted",
                "is nginx running?", "restart nginx and show its logs", "reboot", "shutdown now",
                "restart nginx on the web01 box", "apt update && apt upgrade", "restart nginx; reload x",
                "restart $(whoami)", "restart ngin*", "restart it", "enable nginx and start it",
                "kill whatever is using all the cpu", "restart all services", "remove the old kernel",
                "upgrade nginx", "restart nginx apache2", "install curl jq", "restart the web server",
                "kill 1", "apt install -y curl", "dist-upgrade", "autoremove", "edit the nginx config",
                "install curl from https://example.com/x.deb", "restart nginx\nreload x", "",
                "restart docker.socket and then " * 0 + "restart a b c d e f",
                "restart a and b and c and d and e and f",
                "restart nginx and restart nginx"):
            self.assertIsNone(self.read(request), request)

    def test_a_hard_refusal_is_never_given_to_a_model(self):
        for request in ("don't restart nginx", "restart nginx if needed", "reboot", "restart or reload x"):
            self.assertIsNotNone(admin_job.hard_refusal(request), request)
        self.assertIsNone(admin_job.hard_refusal("give nginx a kick"))

    def test_the_accepted_form_reads_back_exactly(self):
        for actions in ([service("restart", "nginx")], [service("stop", "pipewire", "user")],
                        [process(pid=4312)], [process("kill", name="firefox")], [process("hup", pid=8)],
                        [package("purge", "vim"), updates("refresh"), updates("upgrade")]):
            form = admin_job.sentence(actions)
            self.assertIsNotNone(form, actions)
            self.assertEqual(admin_job.parse(form), actions)

    def test_interpret_admits_only_the_complete_reading_in_order(self):
        request = "restart nginx and reload haproxy"
        calls = [{"name": "service", "arguments": {"operation": "restart", "unit": "nginx"}},
                 {"name": "service", "arguments": {"operation": "reload", "unit": "haproxy.service"}}]
        self.assertEqual(admin_job.interpret(request, calls),
                         [service("restart", "nginx"), service("reload", "haproxy")])
        for wrong in (calls[::-1], calls[:1], calls + calls[:1],
                      [calls[0], {"name": "service", "arguments": {"operation": "restart", "unit": "haproxy"}}],
                      [calls[0], {"name": "service", "arguments": {"operation": "reload", "unit": "haproxy",
                                                                  "scope": "user"}}]):
            result = admin_job.interpret(request, wrong)
            self.assertTrue(all(isinstance(r, admin_job.Refusal) for r in result), wrong)

    def test_normalize_is_strict(self):
        for kind, args in (("service", {"operation": "restart", "unit": "it"}),
                           ("service", {"operation": "explode", "unit": "nginx"}),
                           ("process", {"pid": 1}), ("process", {"pid": 5, "name": "x"}), ("process", {}),
                           ("process", {"name": "everything"}), ("packages", {"operation": "install", "package": "-y"}),
                           ("updates", {"operation": "dist-upgrade"}), ("reboot", {})):
            with self.assertRaises(admin_job.AdminError, msg=(kind, args)):
                admin_job.normalize(kind, args)

    def test_grounding_needs_every_target_written_in_the_request(self):
        self.assertIsNone(admin_job.grounded("give nginx a kick", [service("restart", "nginx")]))
        self.assertIsNotNone(admin_job.grounded("give the web thing a kick", [service("restart", "nginx")]))
        self.assertIsNotNone(admin_job.grounded("zap 43120", [process(pid=4312)]))


class Policy(unittest.TestCase):
    def test_what_keeps_a_session_or_a_login_is_refused(self):
        for act in (service("restart", "dbus"), service("stop", "systemd-logind"), service("stop", "ssh"),
                    service("disable", "NetworkManager"), service("restart", "getty@tty1"),
                    process(name="sshd"), process("kill", name="Xorg")):
            self.assertIsNotNone(admin_job.refused(act), act)
        for act in (service("restart", "ssh"), service("restart", "nginx"), process(pid=4312)):
            self.assertIsNone(admin_job.refused(act), act)

    def test_which_kinds_an_agent_may_confirm(self):
        agent_ok = (service("start", "x"), service("restart", "x"), service("reload", "x"), service("enable", "x"),
                    process(pid=9), process("hup", pid=9), package("install", "curl"), updates("refresh"))
        person = (service("stop", "x"), service("disable", "x"), process("kill", pid=9), process(name="vlc"),
                  package("remove", "curl"), package("purge", "curl"), updates("upgrade"))
        for act in agent_ok:
            self.assertIsNone(admin_job.needs_person(act), act)
        for act in person:
            self.assertIsNotNone(admin_job.needs_person(act), act)


class FakeHost(admin_exec.Host):
    """A /proc in a temporary folder and a runner that answers from a table."""

    def __init__(self, answers=None, *, procs=None, uid=1000, euid=1000, pid=50):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-admin-proc-")
        root = Path(self.dir.name)
        procs = {50: ("python3", 1000, 40), 40: ("bash", 1000, 1), 1: ("systemd", 0, 0), **(procs or {})}
        for p, (name, owner, parent) in procs.items():
            (root / str(p)).mkdir()
            (root / str(p) / "status").write_text(f"Name:\t{name}\nUid:\t{owner}\t{owner}\t{owner}\t{owner}\nPPid:\t{parent}\n")
        self.answers = answers or {}
        self.calls = []
        super().__init__(run=self._run, proc=root, uid=uid, euid=euid, pid=pid)

    def _run(self, argv, *, timeout, interactive=False):
        self.calls.append((list(argv), interactive))
        key = " ".join(Path(argv[0]).name if i == 0 else a for i, a in enumerate(argv))
        for prefix, answer in self.answers.items():
            if key.startswith(prefix):
                return answer
        return 0, "", ""

    def ran(self):
        return [" ".join(argv) for argv, _ in self.calls if "-s" not in argv[1:3] and "show" not in argv
                and Path(argv[0]).name not in ("pgrep", "dpkg-query")]


def _paths(name):
    return f"/usr/bin/{name}"


class Planning(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(admin_exec, "_executable", _paths)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_service_that_does_not_exist_is_refused(self):
        host = FakeHost({"systemctl show": (0, "not-found\n", "")})
        steps, refusals = admin_exec.plan([service("restart", "nginx")], host)
        self.assertEqual(steps, [])
        self.assertIn("no system service nginx.service", refusals[0][1])

    def test_system_scope_needs_root_and_user_scope_does_not(self):
        host = FakeHost({"systemctl show": (0, "loaded\n", ""), "systemctl --user show": (0, "loaded\n", "")})
        steps, refusals = admin_exec.plan([service("restart", "nginx"), service("restart", "pipewire", "user")], host)
        self.assertEqual(refusals, [])
        self.assertEqual(steps[0].argv, ["/usr/bin/systemctl", "restart", "--", "nginx.service"])
        self.assertTrue(steps[0].root)
        self.assertEqual(steps[1].argv, ["/usr/bin/systemctl", "--user", "restart", "--", "pipewire.service"])
        self.assertFalse(steps[1].root)
        self.assertEqual(admin_exec.privileged(steps[0], interactive=False, euid=1000)[:3],
                         ["/usr/bin/sudo", "-n", "--"])
        self.assertEqual(admin_exec.privileged(steps[0], interactive=True, euid=1000)[:2], ["/usr/bin/sudo", "--"])
        self.assertEqual(admin_exec.privileged(steps[0], interactive=False, euid=0)[0], "/usr/bin/systemctl")

    def test_a_masked_unit_is_not_started_and_other_unit_kinds_are_refused(self):
        host = FakeHost({"systemctl show": (0, "masked\n", "")})
        self.assertTrue(admin_exec.plan([service("start", "x")], host)[1])
        self.assertTrue(admin_exec.plan([service("restart", "docker.socket")], FakeHost())[1])

    def test_processes_by_pid(self):
        host = FakeHost(procs={4312: ("firefox", 1000, 40), 900: ("nginx", 33, 1)})
        steps, refusals = admin_exec.plan([process(pid=4312), process("hup", pid=900)], host)
        self.assertEqual(refusals, [])
        self.assertEqual(steps[0].argv, ["/usr/bin/kill", "-s", "TERM", "--", "4312"])
        self.assertFalse(steps[0].root)
        self.assertEqual(steps[1].argv, ["/usr/bin/kill", "-s", "HUP", "--", "900"])
        self.assertTrue(steps[1].root)

    def test_the_caller_and_its_ancestors_are_never_signalled(self):
        host = FakeHost()
        for pid in (50, 40):
            self.assertIn("own shell or caller", admin_exec.plan([process(pid=pid)], host)[1][0][1])
        self.assertIn("no process 777", admin_exec.plan([process(pid=777)], host)[1][0][1])

    def test_a_program_name_lists_its_processes_and_refuses_too_many(self):
        procs = {p: ("vlc", 1000, 40) for p in range(100, 108)}
        host = FakeHost({"pgrep -x -- vlc": (0, "100\n101\n", "")}, procs=procs)
        steps, refusals = admin_exec.plan([process(name="vlc")], host)
        self.assertEqual(steps[0].argv, ["/usr/bin/kill", "-s", "TERM", "--", "100", "101"])
        host = FakeHost({"pgrep -x -- vlc": (0, "\n".join(map(str, range(100, 108))), "")}, procs=procs)
        self.assertIn("8 processes", admin_exec.plan([process(name="vlc")], host)[1][0][1])
        host = FakeHost({"pgrep -x -- vlc": (1, "", "")})
        self.assertIn("no process is named vlc", admin_exec.plan([process(name="vlc")], host)[1][0][1])

    def test_installs_are_batched_and_one_that_removes_is_refused(self):
        host = FakeHost({"apt-get -s install curl jq": (0, "Inst curl (8.0)\nInst jq (1.7)\nInst libjq1 (1.7)\n", "")})
        steps, refusals = admin_exec.plan([package("install", "curl"), package("install", "jq")], host)
        self.assertEqual(refusals, [])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].argv, ["/usr/bin/apt-get", "-y", "install", "curl", "jq"])
        self.assertIn("installs 3", steps[0].details[0])
        host = FakeHost({"apt-get -s install foo": (0, "Remv bar [1]\nInst foo (2)\n", "")})
        self.assertIn("would remove bar", admin_exec.plan([package("install", "foo")], host)[1][0][1])

    def test_removals_of_essential_or_many_packages_are_refused(self):
        host = FakeHost({"apt-get -s remove coreutils": (0, "Remv coreutils [9]\n", ""),
                         "dpkg-query": (0, "coreutils\tyes\trequired\n", "")})
        self.assertIn("essential", admin_exec.plan([package("remove", "coreutils")], host)[1][0][1])
        many = "".join(f"Remv dep{i} [1]\n" for i in range(11)) + "Remv big [1]\n"
        host = FakeHost({"apt-get -s remove big": (0, many, "")})
        self.assertIn("11 other packages", admin_exec.plan([package("remove", "big")], host)[1][0][1])
        host = FakeHost({"apt-get -s purge vim": (0, "", "")})
        self.assertIn("not installed", admin_exec.plan([package("purge", "vim")], host)[1][0][1])
        host = FakeHost({"apt-get -s remove vim": (0, "Remv vim [9]\nRemv vim-runtime [9]\n", ""),
                         "dpkg-query": (0, "vim\t\toptional\nvim-runtime\t\toptional\n", "")})
        steps, refusals = admin_exec.plan([package("remove", "vim")], host)
        self.assertEqual(refusals, [])
        self.assertIn("removes 2", steps[0].details[0])

    def test_an_upgrade_that_removes_anything_is_refused(self):
        host = FakeHost({"apt-get -s upgrade": (0, "Inst a (2)\nRemv b [1]\n", "")})
        self.assertIn("would remove b", admin_exec.plan([updates("upgrade")], host)[1][0][1])

    def test_policy_refusals_stop_the_whole_request(self):
        host = FakeHost({"systemctl show": (0, "loaded\n", "")})
        steps, refusals = admin_exec.plan([service("restart", "nginx"), service("stop", "ssh")], host)
        self.assertEqual(len(refusals), 1)


class Running(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(admin_exec, "_executable", _paths)
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self, **kw):
        base = dict(agent=True, yes=False, dry_run=False, baseline=False, engine=None, root=None, json=True)
        return SimpleNamespace(**{**base, **kw})

    def no_model(self, *_):
        raise AssertionError("the model must not be asked")

    def test_an_agent_yes_runs_what_it_may_confirm(self):
        host = FakeHost({"systemctl show": (0, "loaded\n", "")})
        record = admin_cli.run("restart nginx", args=self.args(yes=True), host=host, interactive=False,
                               propose=self.no_model)
        self.assertEqual(record["status"], 0, record)
        self.assertEqual(host.ran(), ["/usr/bin/sudo -n -- /usr/bin/systemctl restart -- nginx.service"])

    def test_an_agent_yes_does_not_cover_what_needs_a_person(self):
        host = FakeHost({"systemctl show": (0, "loaded\n", "")})
        record = admin_cli.run("stop nginx", args=self.args(yes=True), host=host, interactive=False,
                               propose=self.no_model)
        self.assertEqual(record["status"], 1)
        self.assertIn("a person must confirm", record["note"])
        self.assertEqual(host.ran(), [])

    def test_without_yes_an_agent_runs_nothing(self):
        host = FakeHost({"systemctl show": (0, "loaded\n", "")})
        record = admin_cli.run("restart nginx", args=self.args(), host=host, interactive=False, propose=self.no_model)
        self.assertEqual(record["status"], 1)
        self.assertEqual(host.ran(), [])

    def test_a_person_confirms_once_and_can_decline(self):
        host = FakeHost({"systemctl show": (0, "loaded\n", "")})
        with mock.patch("sys.stdout", io.StringIO()):
            record = admin_cli.run("stop nginx", args=self.args(agent=False), host=host, interactive=True,
                                   ask=lambda q: True, propose=self.no_model)
        self.assertEqual(record["status"], 0)
        self.assertEqual(host.ran(), ["/usr/bin/sudo -- /usr/bin/systemctl stop -- nginx.service"])
        self.assertTrue(host.calls[-1][1])     # interactive: sudo prompts on the terminal
        host = FakeHost({"systemctl show": (0, "loaded\n", "")})
        with mock.patch("sys.stdout", io.StringIO()):
            record = admin_cli.run("stop nginx", args=self.args(agent=False), host=host, interactive=True,
                                   ask=lambda q: False, propose=self.no_model)
        self.assertEqual(record["note"], "declined")
        self.assertEqual(host.ran(), [])

    def test_a_dry_run_looks_and_runs_nothing(self):
        host = FakeHost({"apt-get -s purge vim": (0, "Purg vim [9]\n", ""),
                         "dpkg-query": (0, "vim\t\toptional\n", "")})
        record = admin_cli.run("purge vim", args=self.args(dry_run=True), host=host, interactive=False,
                               propose=self.no_model)
        self.assertEqual(record["status"], 0)
        self.assertEqual(record["note"], "planned, not run")
        self.assertEqual(host.ran(), [])

    def test_steps_stop_at_the_first_failure(self):
        host = FakeHost({"systemctl show": (0, "loaded\n", ""),
                         "sudo -n -- /usr/bin/systemctl restart -- foo.service": (1, "", "sudo: a password is required\n")})
        record = admin_cli.run("restart foo and restart bar", args=self.args(yes=True), host=host, interactive=False,
                               propose=self.no_model)
        self.assertEqual([i["outcome"] for i in record["items"]], ["failed", "not run"])
        self.assertIn("run it at a terminal", record["items"][0]["reason"])

    def test_a_process_that_changed_is_not_signalled_and_nothing_after_it_runs(self):
        host = FakeHost(procs={4312: ("firefox", 1000, 40), 4313: ("vlc", 1000, 40)})
        steps, _ = admin_exec.plan([process(pid=4312), process(pid=4313)], host)
        (Path(host.dir.name) / "4312" / "status").write_text("Name:\tbash\nUid:\t1000\nPPid:\t1\n")
        items = admin_exec.execute(steps, host, interactive=False)
        self.assertEqual([i["outcome"] for i in items], ["failed", "not run"])
        self.assertEqual(host.ran(), [])

    def test_a_negated_request_never_reaches_the_model(self):
        record = admin_cli.run("don't restart nginx", args=self.args(yes=True), host=FakeHost(),
                               interactive=False, propose=self.no_model)
        self.assertEqual(record["status"], 1)

    def test_a_model_proposal_needs_a_person(self):
        def propose(_args, request):
            return [service("restart", "nginx")], "base", None
        host = FakeHost({"systemctl show": (0, "loaded\n", "")})
        record = admin_cli.run("give nginx a kick", args=self.args(yes=True), host=host, interactive=False,
                               propose=propose)
        self.assertEqual(record["trust"], "model_proposal")
        self.assertEqual(record["status"], 1)
        self.assertIn('accepted form: "restart the nginx service"', record["hint"])
        self.assertEqual(host.ran(), [])
        with mock.patch("sys.stdout", io.StringIO()):
            record = admin_cli.run("give nginx a kick", args=self.args(agent=False), host=host, interactive=True,
                                   ask=lambda q: True, propose=propose)
        self.assertEqual(record["status"], 0)
        self.assertEqual(host.ran(), ["/usr/bin/sudo -- /usr/bin/systemctl restart -- nginx.service"])

    def test_baseline_never_asks_the_model(self):
        record = admin_cli.run("give nginx a kick", args=self.args(baseline=True), host=FakeHost(),
                               interactive=False, propose=self.no_model)
        self.assertEqual(record["status"], 1)

    def test_the_default_host_is_never_used_by_these_tests(self):
        self.assertIs(admin_exec.Host().run, admin_exec.command)


class Wiring(unittest.TestCase):
    def test_the_verb_is_dispatched_and_is_not_a_routed_or_tuned_job(self):
        self.assertNotIn("admin", jobs.JOBS)
        with mock.patch("admin_cli.main", return_value=7) as main:
            self.assertEqual(needle_cli.main(["admin", "restart", "nginx"]), 7)
        main.assert_called_once_with(["restart", "nginx"])

    def test_the_fixture_sets_score_exact_and_safe(self):
        for name in ("dev", "test"):
            cases = [json.loads(line) for line in (REPO / "evals" / "admin" / f"{name}.jsonl").read_text().splitlines()
                     if line.strip()]
            result = evaluate.score(admin_job.Baseline(), cases, job="admin")
            self.assertEqual(result["totals"].get("unsafe", 0), 0, name)
            self.assertEqual(result["totals"]["exact"], len(cases), [f["request"] for f in result["failures"]])

    def test_every_tool_schema_is_one_normalize_accepts(self):
        for tool in admin_job.TOOLS:
            self.assertIn(tool["name"], admin_job.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
