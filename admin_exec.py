"""Run admin actions: look first, show the exact commands, ask, then run them in order.

Nothing here reads the request; admin_job.py does. For each action this module

1. looks before it changes anything, with read-only commands that need no root:
   the unit exists (`systemctl show`), the process exists and who owns it
   (/proc), what a package change would install or remove (`apt-get -s`);
2. refuses what policy refuses (admin_job.refused) and what the look found
   unsafe: a removal of an Essential or required package, a removal that takes
   more than EXTRA_REMOVALS other packages, an install or upgrade that removes
   anything, a program name matching more than MAX_MATCHES processes, a process
   that is an ancestor of this request (the shell or agent that asked);
3. builds argv for fixed executables, never a shell, with `--` before targets
   where the program supports it, and `sudo` in front only when the change
   needs root: `sudo` (its own password prompt) for a person at a terminal,
   `sudo -n` for anyone else, which works only where sudo was set up to allow
   it. No password is ever read, stored or passed;
4. runs nothing until consent: one yes from a person at a terminal, or an
   advance yes (--yes) when every action allows one (admin_job.needs_person);
5. runs the steps in order and stops at the first failure.

`runner` is injectable so the tests never run systemctl, kill, sudo or apt-get.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import selectors
import shlex
import signal as signals
import subprocess
import time

import admin_job

EXTRA_REMOVALS = 10
MAX_MATCHES = 5
LOOK_TIMEOUT = 60.0
RUN_TIMEOUT = {"service": 120.0, "process": 30.0, "packages": 1800.0, "updates": 1800.0}
MAX_BYTES = 256 * 1024
_SIGNAL = {"term": "TERM", "kill": "KILL", "hup": "HUP"}
_OTHER_UNIT = re.compile(r"\.(?:socket|timer|target|mount|automount|path|slice|scope|device|swap)$")


class AdminRunError(RuntimeError):
    pass


def _executable(name: str) -> str:
    for folder in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        path = os.path.join(folder, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise AdminRunError(f"{name} is not installed")


def environment(*, interactive: bool) -> dict:
    uid = os.getuid()
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "SYSTEMD_COLORS": "0", "SYSTEMD_PAGER": "cat", "SYSTEMD_PAGERSECURE": "1",
           "XDG_RUNTIME_DIR": f"/run/user/{uid}",
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus"}
    if interactive and os.environ.get("TERM"):
        env["TERM"] = os.environ["TERM"]
    if not interactive:
        env["DEBIAN_FRONTEND"] = "noninteractive"
    return env


def command(argv, *, timeout: float, interactive: bool = False):
    """(exit status, stdout tail, stderr tail). Interactive runs keep the terminal."""
    env = environment(interactive=interactive)
    if interactive:
        try:
            return subprocess.run(argv, env=env, check=False).returncode, "", ""
        except OSError as error:
            raise AdminRunError(f"could not start {Path(argv[0]).name}: {error}") from error
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env, start_new_session=True)
    except OSError as error:
        raise AdminRunError(f"could not start {Path(argv[0]).name}: {error}") from error
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as poll:
            for label, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                poll.register(pipe, selectors.EVENT_READ, label)
            while poll.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdminRunError(f"{Path(argv[0]).name} did not finish in {int(timeout)} s")
                for key, _ in poll.select(remaining):
                    data = os.read(key.fileobj.fileno(), 16384)
                    if not data:
                        poll.unregister(key.fileobj)
                    else:
                        buffer = chunks[key.data]
                        buffer.extend(data)
                        if len(buffer) > MAX_BYTES:
                            del buffer[:len(buffer) - MAX_BYTES]
            code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return code, *(bytes(chunks[k]).decode("utf-8", "replace") for k in ("stdout", "stderr"))
    except subprocess.TimeoutExpired as error:
        raise AdminRunError(f"{Path(argv[0]).name} did not finish in {int(timeout)} s") from error
    finally:
        try:
            os.killpg(process.pid, signals.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        process.stderr.close()


@dataclass
class Step:
    actions: list                 # admin_job.Action (packages of one operation are batched)
    argv: list                    # what runs, without any sudo prefix
    root: bool                    # needs root
    summary: str                  # one line for the plan
    person: str | None = None     # why only a person may confirm, or None
    details: list = field(default_factory=list)   # what the look found, shown in the plan
    expect: dict = field(default_factory=dict)    # facts re-checked just before running


class Host:
    """What the look and the run touch. Tests replace every member."""

    def __init__(self, *, run=command, proc: Path = Path("/proc"), uid: int | None = None,
                 euid: int | None = None, pid: int | None = None):
        self.run, self.proc = run, Path(proc)
        self.uid = os.getuid() if uid is None else uid
        self.euid = os.geteuid() if euid is None else euid
        self.pid = os.getpid() if pid is None else pid

    def status(self, pid: int) -> dict | None:
        try:
            text = (self.proc / str(pid) / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        fields = {}
        for line in text.splitlines():
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        try:
            return {"name": fields["Name"], "uid": int(fields["Uid"].split()[0]),
                    "ppid": int(fields["PPid"])}
        except (KeyError, ValueError, IndexError):
            return None

    def ancestors(self) -> set:
        """This process and every parent up to PID 1: the shell or agent that asked."""
        out, pid = set(), self.pid
        while pid and pid not in out and len(out) < 4096:
            out.add(pid)
            status = self.status(pid)
            if status is None:
                break
            pid = status["ppid"]
        return out


def _look(host: Host, argv) -> tuple[int, str, str]:
    return host.run(argv, timeout=LOOK_TIMEOUT)


def _apt_simulation(text: str):
    installs, removals = [], []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Inst":
            installs.append(parts[1])
        elif len(parts) >= 2 and parts[0] in ("Remv", "Purg"):
            if parts[1] not in removals:
                removals.append(parts[1])
    return installs, removals


def _service_step(host: Host, act) -> Step:
    a = act.args
    if _OTHER_UNIT.search(a["unit"]):
        raise AdminRunError("only services are controlled from here, not sockets, timers or other units")
    unit = a["unit"] + ".service"
    user = ["--user"] if a["scope"] == "user" else []
    systemctl = _executable("systemctl")
    code, out, err = _look(host, [systemctl, *user, "show", "--property=LoadState", "--value", "--", unit])
    state = out.strip()
    if code != 0 or state in ("", "not-found"):
        raise AdminRunError(f"there is no {a['scope']} service {unit}")
    if state == "masked" and a["operation"] in ("start", "restart", "reload", "enable"):
        raise AdminRunError(f"{unit} is masked; unmasking is not done from here")
    scope = "user service" if user else "service"
    return Step([act], [systemctl, *user, a["operation"], "--", unit],
                root=not user and host.euid != 0,
                summary=f"{a['operation']} the {scope} {unit}", person=admin_job.needs_person(act))


def _process_step(host: Host, act) -> Step:
    a = act.args
    kill = _executable("kill")
    mine = host.ancestors()
    if "pid" in a:
        pids = [a["pid"]]
    else:
        pgrep = _executable("pgrep")
        code, out, err = _look(host, [pgrep, "-x", "--", a["name"]])
        if code not in (0, 1):
            raise AdminRunError(f"pgrep failed: {err.strip()[:300]}")
        pids = [int(p) for p in out.split() if p.isdigit()]
        pids = [p for p in pids if p not in mine]
        if not pids:
            raise AdminRunError(f"no process is named {a['name']}")
        if len(pids) > MAX_MATCHES:
            raise AdminRunError(f"{len(pids)} processes are named {a['name']}; name one PID")
    owners, names = {}, {}
    for pid in pids:
        if pid in mine:
            raise AdminRunError(f"process {pid} is this request's own shell or caller")
        status = host.status(pid)
        if status is None:
            raise AdminRunError(f"there is no process {pid}")
        if status["name"] in admin_job._UNTOUCHABLE_PROGRAMS:
            raise AdminRunError(f"process {pid} is {status['name']}, which keeps the session running")
        owners[pid], names[pid] = status["uid"], status["name"]
    others = sorted({uid for uid in owners.values() if uid != host.uid})
    root = bool(others) and host.euid != 0
    who = ", ".join(f"{pid} ({names[pid]})" for pid in pids)
    sig = _SIGNAL[a["signal"]]
    details = [f"owned by uid {', '.join(map(str, others))}"] if others else []
    return Step([act], [kill, "-s", sig, "--", *map(str, pids)], root=root,
                summary=f"send {sig} to process {who}", person=admin_job.needs_person(act),
                details=details, expect={"names": names})


def _packages_step(host: Host, acts) -> Step:
    op = acts[0].args["operation"]
    names = [x.args["package"] for x in acts]
    apt = _executable("apt-get")
    code, out, err = _look(host, [apt, "-s", op, *names])
    if code != 0:
        raise AdminRunError(f"apt-get cannot {op} {' '.join(names)}: "
                            f"{(err.strip() or out.strip()).splitlines()[-1][:300] if (err.strip() or out.strip()) else 'failed'}")
    installs, removals = _apt_simulation(out)
    if "essential packages will be removed" in (out + err).lower():
        raise AdminRunError("this would remove an essential package")
    details = []
    if op == "install":
        if removals:
            raise AdminRunError(f"installing would remove {', '.join(removals[:20])}; not done from here")
        details.append(f"installs {len(installs)}: {' '.join(installs[:30])}" + (" ..." if len(installs) > 30 else ""))
    else:
        missing = [n for n in names if n not in removals]
        if missing:
            raise AdminRunError(f"{', '.join(missing)} is not installed")
        extra = [r for r in removals if r not in names]
        if len(extra) > EXTRA_REMOVALS:
            raise AdminRunError(f"removing {' '.join(names)} would also remove {len(extra)} other packages")
        if removals:
            query = _executable("dpkg-query")
            code, out, err = _look(host, [query, "-W", "-f=${Package}\t${Essential}\t${Priority}\n", "--", *removals])
            if code != 0:
                raise AdminRunError("could not read the priorities of the packages to remove")
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) == 3 and (parts[1] == "yes" or parts[2] == "required"):
                    raise AdminRunError(f"{parts[0]} is essential to the system; it is not removed from here")
        details.append(f"removes {len(removals)}: {' '.join(removals)}")
    person = next((admin_job.needs_person(x) for x in acts if admin_job.needs_person(x)), None)
    return Step(acts, [apt, "-y", op, *names], root=host.euid != 0,
                summary=f"{op} {' '.join(names)}", person=person, details=details)


def _updates_step(host: Host, act) -> Step:
    apt = _executable("apt-get")
    if act.args["operation"] == "refresh":
        return Step([act], [apt, "update"], root=host.euid != 0,
                    summary="refresh the package lists", person=admin_job.needs_person(act))
    code, out, err = _look(host, [apt, "-s", "upgrade"])
    if code != 0:
        raise AdminRunError("apt-get cannot simulate the upgrade")
    installs, removals = _apt_simulation(out)
    if removals:
        raise AdminRunError(f"the upgrade would remove {', '.join(removals[:20])}; not done from here")
    return Step([act], [apt, "-y", "upgrade"], root=host.euid != 0,
                summary=f"upgrade {len(installs)} package(s)", person=admin_job.needs_person(act),
                details=[" ".join(installs[:30]) + (" ..." if len(installs) > 30 else "")] if installs else
                ["nothing to upgrade right now"])


def plan(actions, host: Host) -> tuple[list, list]:
    """(steps, refusals). A refusal is (action, reason); any refusal means nothing runs."""
    steps, refusals = [], []
    i = 0
    while i < len(actions):
        act = actions[i]
        reason = admin_job.refused(act)
        if reason:
            refusals.append((act, reason))
            i += 1
            continue
        try:
            if act.kind == "packages":
                batch = [act]
                while (i + len(batch) < len(actions) and actions[i + len(batch)].kind == "packages"
                       and actions[i + len(batch)].args["operation"] == act.args["operation"]):
                    batch.append(actions[i + len(batch)])
                steps.append(_packages_step(host, batch))
                i += len(batch)
                continue
            steps.append({"service": _service_step, "process": _process_step,
                          "updates": _updates_step}[act.kind](host, act))
        except AdminRunError as error:
            refusals.append((act, str(error)))
        i += 1
    return steps, refusals


def privileged(step: Step, *, interactive: bool, euid: int) -> list:
    if not step.root or euid == 0:
        return list(step.argv)
    sudo = _executable("sudo")
    return [sudo, "--", *step.argv] if interactive else [sudo, "-n", "--", *step.argv]


def shown(argv) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def _still_same(host: Host, step: Step) -> str | None:
    for pid, name in step.expect.get("names", {}).items():
        status = host.status(pid)
        if status is None or status["name"] != name:
            return f"process {pid} is no longer {name}; nothing was sent"
    return None


def execute(steps, host: Host, *, interactive: bool) -> list:
    """Run every step in order; stop at the first failure. One item per action."""
    items = []
    failed = False
    for step in steps:
        argv = privileged(step, interactive=interactive, euid=host.euid)
        base = [{"kind": a.kind, "args": a.args, "command": shown(argv)} for a in step.actions]
        if failed:
            items.extend({**b, "outcome": "not run", "reason": "an earlier step failed"} for b in base)
            continue
        changed = _still_same(host, step)
        if changed:
            items.extend({**b, "outcome": "failed", "reason": changed} for b in base)
            failed = True
            continue
        try:
            code, out, err = host.run(argv, timeout=RUN_TIMEOUT[step.actions[0].kind],
                                      interactive=interactive)
        except AdminRunError as error:
            items.extend({**b, "outcome": "failed", "reason": str(error)} for b in base)
            failed = True
            continue
        if code == 0:
            items.extend({**b, "outcome": "done"} for b in base)
            continue
        failed = True
        tail = (err.strip() or out.strip()).splitlines()[-1][:300] if (err.strip() or out.strip()) else ""
        if step.root and not interactive and "password is required" in (err or ""):
            reason = "needs root, and sudo asks for a password here: run it at a terminal"
        else:
            reason = f"exited {code}" + (f": {tail}" if tail else "")
        items.extend({**b, "outcome": "failed", "reason": reason} for b in base)
    return items
