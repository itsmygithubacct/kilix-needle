"""kilix-needle admin: change this machine. Services, processes, packages, updates.

    kilix-needle admin "restart the nginx service"
    kilix-needle admin "enable and start docker"
    kilix-needle admin "restart the pipewire user service"
    kilix-needle admin "kill process 4312"          # TERM; "force kill" is KILL
    kilix-needle admin "install curl and jq"
    kilix-needle admin "update the package lists and upgrade"
    kilix-needle admin --dry-run "purge vim"       # look and show; run nothing

Every request is read whole by the admin checks (admin_job.py). A request they
read runs after one confirmation. Wording they do not read goes to the Needle
model, whose proposal is only offered: a person at a terminal may confirm it,
and anyone may send its accepted form, which the checks read.

Before asking, kilix-needle looks: the service exists, who owns the process,
what apt would install or remove. It refuses removals of essential packages,
removals that take more than ten others along, upgrades or installs that remove
anything, and changes to what keeps the session or a remote login running.

Consent: a person types y. --yes answers in advance for an agent, but only for
start, restart, reload or enable of a service, TERM or HUP to a PID, an
install and a refresh. Stop, disable, a forced kill, a program by name, a
removal and an upgrade always wait for a person. Root changes run through
sudo: its own prompt at a terminal, `sudo -n` otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from types import SimpleNamespace

import admin_exec
import admin_job


def _proposal(args, request):
    """(actions, label, why not) from the Needle model, never trusted."""
    import needle_cli
    from engine import reply_calls
    try:
        with needle_cli.open_runtime(args, may_install=False, job="admin") as runtime:
            runtime.reset()
            reply = runtime.complete(request)
            label = runtime.label
    except Exception as error:      # noqa: BLE001 - no model means no proposal, never a crash
        return None, None, f"the model is unavailable ({type(error).__name__})"
    raw, error = reply_calls(reply)
    if error:
        return None, label, error
    if not raw:
        return None, label, "the model proposed nothing"
    try:
        actions = [admin_job.Action(c["name"], admin_job.normalize(c["name"], c.get("arguments", {})))
                   for c in raw]
    except (admin_job.AdminError, KeyError, TypeError) as error:
        return None, label, f"the model's proposal does not fit the admin tools ({error})"
    why = admin_job.grounded(request, actions)
    if why:
        return None, label, why
    return actions, label, None


def _ask(question: str) -> bool:
    sys.stdout.write(question)
    sys.stdout.flush()
    answer = sys.stdin.readline(16)
    return answer.strip().casefold() in ("y", "yes")


def run(request: str, *, args, host=None, ask=_ask, interactive=None, propose=_proposal) -> dict:
    host = host or admin_exec.Host()
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.agent
    record = {"request": request, "job": "admin", "status": 1, "source": "admin request checks",
              "trust": "checks", "plan": [], "items": [], "note": ""}
    actions, reason = admin_job.parse_explain(request)
    if actions is None:
        hard = admin_job.hard_refusal(request)
        if hard or args.baseline:
            record.update(note=reason, hint="accepted forms: " + admin_job.FORMS,
                          items=[{"kind": "admin", "outcome": "refused", "reason": reason}])
            return record
        actions, label, why = propose(args, request)
        if actions is None:
            record.update(note=f"{reason}; {why}", source=label or "admin request checks",
                          hint="accepted forms: " + admin_job.FORMS,
                          items=[{"kind": "admin", "outcome": "refused", "reason": reason}])
            return record
        record.update(source=label, trust="model_proposal")
        form = admin_job.sentence(actions)
        record["hint"] = (f'accepted form: "{form}" (send it to run it as read by the checks)' if form
                          else "accepted forms: " + admin_job.FORMS)
    steps, refusals = admin_exec.plan(actions, host)
    record["plan"] = [{"summary": s.summary, "command": admin_exec.shown(
        admin_exec.privileged(s, interactive=interactive, euid=host.euid)),
        "needs_person": s.person, "details": s.details} for s in steps]
    if refusals:
        record["note"] = "; ".join(r for _, r in refusals)
        record["items"] = ([{"kind": a.kind, "args": a.args, "outcome": "refused", "reason": r}
                            for a, r in refusals]
                           + [{"kind": a.kind, "args": a.args, "outcome": "not run",
                               "reason": "another action was refused"} for s in steps for a in s.actions])
        return record
    planned = [{"kind": a.kind, "args": a.args, "outcome": "planned"} for s in steps for a in s.actions]
    if args.dry_run:
        record.update(status=0, items=planned, note="planned, not run")
        return record
    persons = [s.person for s in steps if s.person]
    if record["trust"] == "model_proposal":
        persons.insert(0, "the model's reading is not one the checks make")
    if args.yes and not persons:
        consent = True
    elif interactive:
        _print_plan(record, sys.stdout)
        consent = ask(f"Run {'this change' if len(planned) == 1 else f'these {len(planned)} changes'}? [y/N] ")
        if not consent:
            record.update(note="declined", items=[{**p, "outcome": "skipped", "reason": "declined"}
                                                  for p in planned])
            return record
    else:
        why = ("; ".join(persons) + ": a person must confirm at a terminal") if persons else \
              "needs --yes, given only for an action the user asked for"
        record.update(note=why, items=[{**p, "outcome": "skipped", "reason": why} for p in planned])
        return record
    record["items"] = admin_exec.execute(steps, host, interactive=interactive)
    failed = [i for i in record["items"] if i["outcome"] != "done"]
    record["status"] = 1 if failed else 0
    record["note"] = "; ".join(sorted({i["reason"] for i in failed if i.get("reason")})) or "done"
    return record


def _print_plan(record, out):
    trust = record.get("trust")
    if trust == "model_proposal":
        print(f"The checks did not read this request; {record['source']} proposes "
              "(unverified):", file=out)
    for step in record["plan"]:
        mark = "  (needs a person)" if step["needs_person"] else ""
        print(f"  - {step['summary']}{mark}\n      {step['command']}", file=out)
        for detail in step["details"]:
            print(f"      {detail}", file=out)


def render(record) -> str:
    lines = []
    for item in record["items"]:
        what = admin_job.sentence([admin_job.Action(item["kind"], item["args"])]) if "args" in item else "request"
        reason = f": {item['reason']}" if item.get("reason") else ""
        lines.append(f"{item['outcome']}: {what}{reason}")
    if record.get("hint") and record["status"] != 0:
        lines.append(record["hint"])
    return "\n".join(lines) or record.get("note", "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kilix-needle admin", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request", nargs="+")
    parser.add_argument("--dry-run", action="store_true", help="look and show the plan; run nothing")
    parser.add_argument("--yes", action="store_true",
                        help="confirm in advance (an agent: only the kinds listed above)")
    parser.add_argument("--agent", action="store_true", help="an agent is calling: never read stdin")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--baseline", action="store_true", help="the checks only; never load a model")
    parser.add_argument("--engine", metavar="FILE")
    parser.add_argument("--root")
    args = parser.parse_args(argv)
    request = " ".join(args.request)
    started = time.monotonic()
    record = run(request, args=args)
    try:
        import history
        calls = [{"name": i["kind"], "arguments": i["args"]} for i in record["items"] if "args" in i]
        history.record("admin", request, request, SimpleNamespace(label=record["source"]), calls,
                       SimpleNamespace(agent=args.agent, dry_run=args.dry_run, assume_yes=args.yes),
                       record, time.monotonic() - started)
    except Exception:       # noqa: BLE001 - recording never changes the outcome
        pass
    if args.json:
        print(json.dumps(record, ensure_ascii=False))
    else:
        if args.dry_run or record["status"] != 0:
            _print_plan(record, sys.stdout)
        print(render(record))
    return record["status"]


if __name__ == "__main__":
    sys.exit(main())
