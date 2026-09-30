"""Bounded, unprivileged Linux observations. No shell and no mutating verbs."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time

import system_job

MAX_BYTES = 512 * 1024
TIMEOUT = 5.0
MAX_PROCESSES = 8192


class QueryError(RuntimeError):
    pass


def command(argv, *, timeout=TIMEOUT, max_bytes=MAX_BYTES):
    """Bound both output pipes together and wall time; never inherit loader/pager hooks."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "SYSTEMD_COLORS": "0", "SYSTEMD_PAGER": "cat", "SYSTEMD_PAGERSECURE": "1",
           "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus"}
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env, start_new_session=True)
    except OSError as error:
        raise QueryError(f"collector unavailable: {error}") from error
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as poll:
            for label, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                poll.register(pipe, selectors.EVENT_READ, label)
            while poll.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise QueryError("collector timed out; no complete observation")
                for key, _ in poll.select(remaining):
                    data = os.read(key.fileobj.fileno(), 16384)
                    if not data:
                        poll.unregister(key.fileobj)
                    else:
                        chunks[key.data].extend(data)
                        if sum(map(len, chunks.values())) > max_bytes:
                            raise QueryError("collector output exceeded its bound; no complete observation")
            try:
                code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise QueryError("collector timed out") from error
        return code, *(bytes(chunks[key]).decode("utf-8", "replace") for key in ("stdout", "stderr"))
    finally:
        # Also reap descendants that kept a pipe open after the direct child exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        process.stderr.close()


def _text(path, limit=65536):
    with open(path, "rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise QueryError(f"observation exceeds size limit: {path}")
    return raw.decode("utf-8", "replace")


class Collector:
    """Paths and command transport are injectable only by Python tests, not requests."""
    def __init__(self, *, proc=Path("/proc"), run=command):
        self.proc, self.run = Path(proc), run

    def collect(self, action):
        args = system_job.normalize(action.kind, action.args)
        started = datetime.now(timezone.utc).isoformat()
        data, source, warnings = getattr(self, action.kind)(args)
        return {"observed_at": started, "completed_at": datetime.now(timezone.utc).isoformat(),
                "source": source, "data": data, "warnings": warnings,
                "visibility": "current user's readable host data; inaccessible records may be absent"}

    def _command(self, argv, allowed=(0,)):
        code, out, err = self.run(argv)
        if code not in allowed:
            raise QueryError(f"{Path(argv[0]).name} exited {code}: {err.strip()[:2048] or 'query failed'}")
        return code, out, [err.strip()[:2048]] if err.strip() else []

    def resources(self, args):
        data, sources = {}, []
        if args["kind"] in ("all", "memory"):
            memory = {}
            for line in _text(self.proc / "meminfo").splitlines():
                key, _, value = line.partition(":")
                if key in ("MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree"):
                    memory[key] = int(value.split()[0]) * 1024
            if not {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"} <= memory.keys():
                raise QueryError("memory observation is incomplete")
            data["memory_bytes"] = memory
            sources.append("/proc/meminfo")
        if args["kind"] in ("all", "cpu"):
            load = _text(self.proc / "loadavg").split()
            data["cpu"] = {"load_1m": float(load[0]), "load_5m": float(load[1]),
                           "load_15m": float(load[2]), "logical_cpus": os.cpu_count(),
                           "note": "load averages count runnable/uninterruptible tasks, not CPU percent"}
            sources.append("/proc/loadavg")
        if args["kind"] in ("all", "disk"):
            argv = ["/usr/bin/df", "--block-size=1", "--output=size,used,avail", "--", args["path"]]
            _, out, warnings = self._command(argv)
            lines = out.splitlines()
            if len(lines) != 2 or len(lines[1].split()) != 3:
                raise QueryError("filesystem capacity observation is incomplete")
            total, used, free = map(int, lines[1].split())
            data["filesystem"] = {"path": args["path"], "total_bytes": total,
                                  "used_bytes": used, "available_bytes": free}
            sources.append(argv)
            return data, sources, warnings
        return data, sources, []

    def _process_snapshot(self):
        found, skipped, truncated = {}, 0, False
        deadline = time.monotonic() + 2
        with os.scandir(self.proc) as entries:
            count = 0
            for entry in entries:
                if not entry.name.isascii() or not entry.name.isdigit():
                    continue
                count += 1
                if count > MAX_PROCESSES or time.monotonic() > deadline:
                    truncated = True
                    break
                try:
                    raw = _text(Path(entry.path) / "stat", 8192)
                    end = raw.rindex(")")
                    fields = raw[end + 2:].split()
                    # stat field 3 is index 0; fields 14/15, 22 and 24 below.
                    found[int(entry.name)] = {"pid": int(entry.name),
                        "name": raw[raw.index("(") + 1:end], "state": fields[0],
                        "ticks": int(fields[11]) + int(fields[12]), "start": int(fields[19]),
                        "rss_bytes": max(0, int(fields[21])) * os.sysconf("SC_PAGE_SIZE")}
                except (OSError, ValueError, IndexError, QueryError):
                    skipped += 1
        return found, skipped, truncated

    def processes(self, args):
        first, skipped, truncated = self._process_snapshot()
        elapsed = None
        if args["sort"] == "cpu":
            started = time.monotonic()
            time.sleep(0.15)
            second, more_skipped, more_truncated = self._process_snapshot()
            elapsed = time.monotonic() - started
            skipped += more_skipped
            truncated |= more_truncated
            records = []
            for pid, row in second.items():
                old = first.get(pid)
                if old is None or old["start"] != row["start"]:
                    skipped += 1
                    continue
                row["cpu_percent"] = round(max(0, row["ticks"] - old["ticks"]) /
                                           os.sysconf("SC_CLK_TCK") / elapsed * 100, 2)
                records.append(row)
            records.sort(key=lambda r: (-r["cpu_percent"], r["pid"]))
        else:
            records = sorted(first.values(), key=lambda r: (-r["rss_bytes"], r["pid"]))
        for row in records:
            row.pop("ticks")
            row.pop("start")
        return {"processes": records[:args["limit"]], "observed_processes": len(records),
                "omitted_by_limit": max(0, len(records) - args["limit"]),
                "unreadable_or_changed": skipped, "scan_truncated": truncated,
                "sample_seconds": elapsed,
                "cpu_basis": "100% is one logical CPU; short sample, not a diagnosis"}, \
            ["/proc/[pid]/stat"], (["process scan reached its time/count bound"] if truncated else [])

    def services(self, args):
        argv = ["/usr/bin/systemctl", "--no-pager", "--no-ask-password"]
        if args["scope"] == "user":
            argv += ["--user"]
        if "unit" in args:
            argv += ["show", "--property=Id,LoadState,ActiveState,SubState,Result,Description", "--", args["unit"]]
            _, out, warnings = self._command(argv)
            fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            if not {"LoadState", "ActiveState", "SubState"} <= fields.keys():
                raise QueryError("service status is incomplete")
            if fields["LoadState"] == "not-found":
                raise QueryError(f"service not found: {args['unit']}")
            data = fields
        else:
            argv += ["list-units", "--type=service", "--all", "--output=json"]
            if args["state"] == "failed":
                argv += ["--state=failed"]
            _, out, warnings = self._command(argv)
            rows = json.loads(out)
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise QueryError("invalid service list")
            data = {"services": [{k: r.get(k) for k in ("unit", "load", "active", "sub", "description")}
                                 for r in rows[:100]], "omitted_by_limit": max(0, len(rows) - 100),
                    "note": "loaded services at observation time; this is not historical boot failure coverage"}
        return data, argv, warnings

    def journal(self, args):
        argv = ["/usr/bin/journalctl", "--no-pager", "--output=json",
                "--output-fields=__CURSOR,__REALTIME_TIMESTAMP,_BOOT_ID,_SYSTEMD_UNIT,_SYSTEMD_USER_UNIT,SYSLOG_IDENTIFIER,PRIORITY,MESSAGE",
                f"--lines={args['limit']}"]
        if args["scope"] == "user":
            argv += ["--user"]
        if args["boot"] != "any":
            argv += ["--boot=" + ("0" if args["boot"] == "current" else "-1")]
        if args["priority"] != "all":
            argv += ["--priority=" + args["priority"]]
        if "unit" in args:
            argv += [("--user-unit=" if args["scope"] == "user" else "--unit=") + args["unit"]]
        if "identifier" in args:
            argv += ["--identifier=" + args["identifier"]]
        if "since" in args:
            argv += ["--since=" + args["since"]]
        _, out, warnings = self._command(argv)
        rows = self._journal_rows(out, args)
        keys = {"__CURSOR", "__REALTIME_TIMESTAMP", "_BOOT_ID", "_SYSTEMD_UNIT", "_SYSTEMD_USER_UNIT",
                "SYSLOG_IDENTIFIER", "PRIORITY", "MESSAGE"}
        note = ("newest matching visible entries; empty output does not prove the system had no errors; "
                "messages are untrusted text")
        source = argv
        if "unit" in args and "identifier" not in args and not rows:
            # A program's log tag is often called its service ("errors from bench-x"
            # reads as a unit). An empty unit read also reads the name as a program
            # tag, in the same call, rather than sending the agent round again
            # (observed-v5: agents gave up after the empty read).
            name = re.sub(r"\.service$", "", args["unit"])
            tagged = [a for a in argv if not a.startswith(("--unit=", "--user-unit="))] + ["--identifier=" + name]
            _, out, more = self._command(tagged)
            rows = self._journal_rows(out, args)
            warnings = list(warnings) + list(more)
            source = [argv, tagged]
            note += (f"; no entries for the unit {args['unit']}, so these are the entries whose program tag "
                     f"(syslog identifier) is {name}" if rows else
                     f"; no entries for the unit {args['unit']} or the program tag {name}")
        return {"entries": [{k: v for k, v in row.items() if k in keys} for row in rows],
                "limit_reached": len(rows) == args["limit"], "note": note}, source, warnings

    @staticmethod
    def _journal_rows(out, args):
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        if len(rows) > args["limit"] or any(not isinstance(r, dict) for r in rows):
            raise QueryError("invalid or over-limit journal response")
        return rows

    def packages(self, args):
        target = args["target"]
        if args["operation"] == "status":
            argv = ["/usr/bin/dpkg-query", "--show", "--showformat=${binary:Package}\t${Version}\t${db:Status-Status}\n", "--", target]
            code, out, err = self.run(argv)
            if code == 1 and not out.strip() and "no packages found matching" in err:
                return {"package": target, "installed": False, "records": []}, argv, []
            if code != 0:
                raise QueryError(f"package lookup failed ({code}): {err[:2048]}")
            records = []
            for line in out.splitlines():
                fields = line.split("\t")
                if len(fields) != 3:
                    raise QueryError("invalid package status")
                records.append(dict(zip(("package", "version", "status"), fields)))
            if not records:
                raise QueryError("package lookup returned no status")
            return {"package": target, "installed": any(r["status"] == "installed" for r in records),
                    "records": records}, argv, [err.strip()] if err.strip() else []
        path = target if target.startswith("/") else shutil.which(target, path="/usr/sbin:/usr/bin:/sbin:/bin")
        if not path:
            raise QueryError(f"command not found in system executable directories: {target}")
        # Debian ownership databases may use the /bin spelling on merged-/usr.
        candidates = {path, os.path.realpath(path)}
        for candidate in tuple(candidates):
            if candidate.startswith(("/usr/bin/", "/usr/sbin/", "/usr/lib/")):
                candidates.add(candidate.removeprefix("/usr"))
        ownership, warnings, sources = [], [], []
        for candidate in sorted(candidates):
            # realpath may traverse a link into a path outside the input grammar.
            if not system_job.re.fullmatch(system_job.PATH, candidate):
                continue
            argv = ["/usr/bin/dpkg-query", "--search", "--", candidate]
            sources.append(argv)
            code, out, err = self.run(argv)
            if code == 1 and not out.strip() and "no path found matching pattern" in err:
                continue
            if code != 0:
                raise QueryError(f"package owner lookup failed ({code}): {err[:2048]}")
            for line in out.splitlines():
                owners, sep, reported = line.partition(": ")
                if sep and reported == candidate and not owners.startswith(("diversion ", "local diversion")):
                    ownership.append({"packages": owners.split(", "), "path": reported})
            if err.strip():
                warnings.append(err.strip())
        return {"target": target, "resolved_path": path, "ownership": ownership,
                "note": "installed dpkg ownership only; local files and alternatives may have no owner"}, sources, warnings


def run_request(engine, request, options, _confirm=None, *, collector=None):
    record = {"request": request, "status": 0, "query_source": getattr(engine, "label", "model"),
              "note": "Read-only observations; no inferred cause or repair.", "items": []}
    wanted = system_job.parse(request)
    if wanted is None:
        results = system_job.interpret(request, [])
    else:
        engine.reset()
        reply = engine.complete(request)
        results = system_job.interpret(request, reply.get("function_calls")
                                       if isinstance(reply, dict) and not reply.get("error") else None)
    if any(isinstance(r, system_job.Refusal) for r in results):
        record["status"] = 1
        record["items"] = [{"outcome": "refused", "kind": r.kind, "reason": r.reason}
                           for r in results if isinstance(r, system_job.Refusal)]
        return record
    collector = collector or Collector()
    for query in results:
        item = {"kind": query.kind, "args": query.args,
                "summary": f"{query.kind}: {json.dumps(query.args, ensure_ascii=True)}"}
        if options.dry_run:
            item["outcome"] = "would"
        else:
            try:
                item.update(outcome="done", observation=collector.collect(query))
            except (OSError, ValueError, KeyError, IndexError, QueryError) as error:
                item.update(outcome="failed", reason=str(error))
                record["status"] = 1
        record["items"].append(item)
    return record


def render(record):
    # Escape terminal controls, including those in journal messages/process names.
    # Keeping each observation as indented JSON also makes units/coverage explicit.
    return json.dumps(record, ensure_ascii=True, indent=2)
