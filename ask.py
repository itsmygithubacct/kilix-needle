"""Route a plain request to the job that answers it, then run that job unchanged.

    kilix-needle ask [--plan] [--json] [--explain] [--job JOB] [--agent] [--yes] REQUEST
    kilix-needle ask --interactive [--hold] [--] REQUEST

The router (router.py) reads the request text only. Stage 0 and stage 1 routes
run the chosen job exactly as its own command or MCP tool would, with that job's
own checks and consent. Below the router's threshold, stage 2 asks the candidate
jobs' plans, which run nothing: when exactly one admits the request, its plan is
shown, but an unsure route never acts. Otherwise the answer is a clarification
naming each candidate job with one accepted phrasing. Two jobs' work in one
request runs nothing and asks for two requests. The one exception is the
no-verb CLI default (`default="panes"`): at stage 2, when only the panes plan
admits the request, the panes job answers as it did before routing.

Jobs that need arguments a sentence cannot carry (tmux: a socket; logs: a file or
session) and the opt-in pty and desktop tools are named, never run, so routing
grants no access their own entry points do not.

--interactive (the `kilix ask` launcher verb, the Run dialog, voice) shows the
route and the plan and acts only on an explicit yes, for every act. Exit status:
0 when done or nothing to do, 1 when refused, unsure or declined, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import history
import router

# One accepted phrasing per job, for clarifications and refusals.
PHRASES = {
    "panes": "split right and run htop",
    "apps": "open the pdf viewer",
    "agents": "start codex in /abs/dir",
    "files": "find pdf files in Downloads",
    "system": "show memory",
    "logs": 'search the session in pane 12 for "error" (structured: kilix_logs_read)',
    "tmux": "list sessions (with --socket /abs/socket)",
    "pty": "list sessions",
    "desktop": 'focus window "Notes"',
}
_CLI = {"panes": "kilix-needle", "apps": "kilix-needle apps", "agents": "kilix-needle agents",
        "files": "kilix-needle files", "system": "kilix-needle system"}
_MCP = {"panes": "kilix_plan / kilix_act", "apps": "kilix_apps_plan / kilix_apps_act",
        "agents": "kilix_agents_plan / kilix_agents_act", "files": "kilix_files_plan / kilix_files_read",
        "system": "kilix_system_plan / kilix_system_read"}


def admits(job: str, record: dict) -> bool:
    """Whether a job's plan (dry run) would do something for the request."""
    if not isinstance(record, dict):
        return False
    if job == "files":
        return record.get("status") == 0 and bool(record.get("plan"))
    return any(isinstance(item, dict) and item.get("outcome") in ("would", "done", "skipped")
               for item in record.get("items") or [])


class Jobs:
    """Runs one job's plan or act, given each caller's engines and consent.

    engine(job) returns the job's (lazy) engine; system(job) the system
    classifier; confirm asks a person; agents_cwd is the caller's directory for
    the agents job's 'here'; files_cwd is the files job's 'here'."""

    def __init__(self, engine, classify_system, *, confirm, agent: bool,
                 agents_cwd=None, files_cwd=None, under_overlay: bool = False,
                 commands=None):
        self.engine, self.classify_system = engine, classify_system
        self.confirm, self.agent = confirm, agent
        self.agents_cwd, self.files_cwd, self.under_overlay = agents_cwd, files_cwd, under_overlay
        self.commands = commands

    def run(self, job: str, request: str, *, dry_run: bool, assume_yes: bool = False) -> dict:
        import needle_cli
        if self.commands is not None and self.commands.recognizes(request):
            decision = self.commands.decide(request)
            if decision.admission.job not in (None, job):
                return {'request': request, 'status': 1, 'items': [],
                        'note': 'the complete command does not belong to the selected job'}
            return self.commands.run(request, dry_run=dry_run, assume_yes=assume_yes,
                                     confirm=self.confirm)
        # The agents job's consent is the request itself; no yes widens it.
        options = needle_cli.Options(dry_run=dry_run,
                                     assume_yes=assume_yes and not dry_run and job != "agents",
                                     agent=self.agent, under_overlay=self.under_overlay)
        if job == "system":
            import system_dispatch
            source = {"label": None}

            def classify(text):
                engine = self.classify_system()
                source["label"] = getattr(engine, "label", "model proposal")
                engine.reset()
                return engine.complete(text)

            return system_dispatch.dispatch(request, classify, options,
                                            model_label=lambda: source["label"])
        if job == "files":
            import files_cli
            return files_cli.run(request, dry_run=dry_run, cwd=self.files_cwd, agent=self.agent)
        if job == "agents":
            record = needle_cli.run_agents_request(self.engine("agents"), request, options,
                                                   self.confirm, cwd=self.agents_cwd)
        elif job == "apps":
            record = needle_cli.run_apps_request(self.engine("apps"), request, options, self.confirm)
        else:
            record = None
        if record is not None:
            if self.commands is not None and not admits(job, record):
                return self.commands.run(request, dry_run=dry_run, assume_yes=assume_yes,
                                         confirm=self.confirm)
            return record
        if job == "panes":
            return needle_cli.run_request(self.engine("panes"), request, options, self.confirm)
        raise ValueError(f"ask does not run the {job} job")


# Inside WSL the desktop is Windows': a desktop request is the windows job's.
_WINDOWS = ("kilix-needle windows", "kilix_windows_read / plan / act (kilix-needle mcp --tools windows)",
            "Windows applications and their windows, from WSL")
_WINDOWS_PHRASE = 'open notepad then type "hello"'


def _named(job: str, request: str) -> dict:
    cli, tools, what = router.NAMED[job]
    phrase = PHRASES[job]
    if job == "desktop":
        import desktop_windows
        if desktop_windows.in_wsl():
            job, phrase = "windows", _WINDOWS_PHRASE
            cli, tools, what = _WINDOWS
    return {"request": request, "status": 1, "items": [],
            "note": f"this reads as a request for the {job} job ({what}); ask names it but does not run it",
            "hint": f"{cli} {json.dumps(request)}  (MCP: {tools}); accepted form: {phrase}"}


def _none(request: str) -> dict:
    return {"request": request, "status": 1, "items": [],
            "note": "no kilix-needle job does this",
            "hint": "jobs: " + "  |  ".join(f"{job}: {PHRASES[job]}" for job in router.RUNS)}


def _clarify(request: str, candidates: list, note: str = "") -> dict:
    shown = [job for job in candidates if job in PHRASES] or list(router.RUNS)
    return {"request": request, "status": 1, "items": [],
            "note": note or "not sure which job this is for; nothing was run",
            "hint": "say it as one of: " + "  |  ".join(f"{job}: {PHRASES[job]}" for job in shown)
                    + "  (two jobs' work goes as two requests)"}


def ask(request: str, jobs: Jobs, *, act: bool, assume_yes: bool = False,
        job: str | None = None, route: router.Route | None = None,
        default: str | None = None) -> dict:
    """One routed request, as the chosen job's record plus a `route` block.

    `job` skips routing; `route` reuses one already made (an interactive act
    after its plan), so the act runs on exactly the job the plan showed.

    `default` is the job an entry point always meant before routing (the
    no-verb CLI: panes). At stage 2, when that job's plan is the only one that
    admits the request, it runs as it did before routing, with its own checks
    and consent. A clarification or a router error never acts."""
    if not isinstance(request, str) or not request.strip():
        raise ValueError("ask needs a request")
    commands = getattr(jobs, 'commands', None)
    if commands is not None and (
            route is None and job is None and commands.recognizes(request)
            or route is not None and route.stage == 0 and route.job == 'commands'):
        record = commands.run(request, dry_run=not act, assume_yes=assume_yes,
                              confirm=jobs.confirm)
        record['route'] = router.Route(0, 'commands').record()
        with history.routed(record['route']):
            _record_unsure(request, record, act)
        return record
    if route is not None:
        # A route made by the caller: an interactive act after its plan.
        if route.stage not in (0, 1, "forced") or not (
                route.job in router.RUNS or route.job == "none" or route.job in router.NAMED):
            raise ValueError("ask runs only a sure route to a job")
    elif job is not None:
        if job not in router.RUNS:
            raise ValueError(f"--job takes one of {', '.join(router.RUNS)}")
        route = router.Route("forced", job)
    else:
        try:
            route = router.route(request)
        except Exception as error:      # noqa: BLE001 - a router failure fails closed
            route = router.Route("error", None, note=f"router failed: {type(error).__name__}")
    if route.stage in (0, 1, "forced"):
        if route.job == "none":
            record = _none(request)
        elif route.job in router.NAMED:
            record = _named(route.job, request)
        else:
            with history.routed(route.record()):
                record = jobs.run(route.job, request, dry_run=not act, assume_yes=assume_yes)
        record["route"] = route.record()
        return record
    if route.stage == "multi":
        record = _clarify(request, route.candidates,
                          f"this asks for {' and '.join(route.candidates)} work at once; "
                          "nothing was run. Send each part as its own request")
        record["route"] = route.record()
        with history.routed(route.record()):
            _record_unsure(request, record, act)
        return record
    # Stage 2: the candidates' plans decide; a plan runs nothing and is not recorded.
    plans, failures = {}, {}
    for candidate in route.candidates:
        if candidate in router.RUNS:
            try:
                with history.quiet():
                    plans[candidate] = jobs.run(candidate, request, dry_run=True)
            except Exception as error:      # noqa: BLE001 - a failing plan admits nothing
                plans[candidate], failures[candidate] = None, error
    admitted = [c for c in route.candidates
                if (c in plans and admits(c, plans[c])) or (c in router.NAMED and c in route.claims)]
    if not admitted and default in failures:
        # The entry point's own job could not plan (its model is missing, say):
        # its error, with its recovery hint, is the answer it always gave.
        raise failures[default]
    if len(admitted) == 1 and admitted[0] in plans:
        chosen = admitted[0]
        route.job = chosen
        if act and route.stage == 2 and chosen == default:
            route.note = f"unsure; only the {chosen} job's plan admits it, and this entry point is that job"
            with history.routed(route.record()):
                record = jobs.run(chosen, request, dry_run=False, assume_yes=assume_yes)
            record["route"] = route.record()
            return record
        record = dict(plans[chosen])
        if act:
            record["status"] = 1
            record["note"] = (f"unsure route: only the {chosen} job's plan admits this, so it was "
                              "planned, not run")
            record["hint"] = f"to run it, send it to that job: {_CLI[chosen]} {json.dumps(request)} " \
                             f"(MCP: {_MCP[chosen]})"
    elif len(admitted) == 1:
        route.job = admitted[0]
        record = _named(admitted[0], request)
    else:
        route.stage = "clarify"
        record = _clarify(request, route.candidates,
                          route.note and "the router is unavailable; nothing was run")
    record["route"] = route.record()
    with history.routed(route.record()):
        _record_unsure(request, record, act)
    return record


def _record_unsure(request: str, record: dict, act: bool) -> None:
    """A stage-2 or clarified request is recorded once, under the job `ask`."""
    try:
        options = type("Options", (), {"agent": True, "dry_run": not act, "assume_yes": False})()
        engine = type("Engine", (), {"label": "router"})()
        history.record("ask", request, None, engine, None, options, record, 0.0)
    except Exception:       # noqa: BLE001
        pass


def describe(route: dict) -> str:
    """One line for a person: where the request went and why."""
    stage, job = route.get("stage"), route.get("job")
    p = f" (p={route['p']:.2f})" if route.get("p") is not None else ""
    if stage == 0:
        head = f"route: {job}, read exactly by its grammar{p}"
    elif stage == 1:
        head = f"route: {job}{p}"
    elif stage == "forced":
        head = f"route: {job} (chosen with --job)"
    elif stage == 2 and job:
        head = f"route: unsure; only the {job} job's plan admits it{p}"
    elif stage == "multi":
        head = f"route: two jobs' work ({', '.join(route.get('candidates') or [])})"
    else:
        head = "route: unsure" + (f"; candidates {', '.join(route.get('candidates') or [])}"
                                  if route.get("candidates") else "")
    alternatives = route.get("alternatives")
    if alternatives and stage != "forced":
        head += "; next: " + ", ".join(f"{j} {q:.2f}" for j, q in alternatives)
    return head


def render(record: dict) -> str:
    import needle_cli
    route = record.get("route") or {}
    job = route.get("job")
    if 'operation_id' in record and 'interpretation' in record:
        import commands_ask
        body = commands_ask.render(record)
    elif job == "system" and "query_source" in record:
        import system_collect
        body = system_collect.render(record)
    elif job == "files" and "plan" in record:
        import files_cli
        body = files_cli.render(record)
    else:
        body = needle_cli.render(record)
    lines = [describe(route), body]
    if record.get("hint"):
        lines.append(f"  hint: {record['hint']}")
    return "\n".join(line for line in lines if line)


def explain(request: str) -> str:
    model = router.model()
    lines = [f"claims: {', '.join(router.claims(request)) or 'none'}"]
    for job, p in model.rank(request)[:4]:
        top = ", ".join(f"{k!r} {v:+.2f}" for k, v in model.explain(request, job, 5))
        lines.append(f"{job:8} {p:.3f}  {top}")
    lines.append(f"threshold {model.threshold:.2f}")
    return "\n".join(lines)


def cli_jobs(args, *, interactive: bool, engines: dict | None = None) -> tuple:
    """Jobs for the CLI: lazy engines per job, the person's terminal for consent.
    `engines` supplies engines the caller already holds (and closes)."""
    import contextlib
    import needle_cli
    stack = contextlib.ExitStack()
    engines = dict(engines or {})
    backend = getattr(args, 'commands_backend', None)
    commands = None
    if backend:
        import commands_ask
        import commands_models
        providers = stack.enter_context(commands_models.Providers(
            backend, candidate=args.commands_candidate, bundle=args.commands_bundle,
            root=getattr(args, 'root', None)))
        commands = commands_ask.Session(
            backend, provider=providers.get, cwd=os.getcwd(),
            model_only=args.commands_model_only, operation_id=args.operation_id)

    def engine(job):
        if commands is not None:
            return commands_ask.NoProposal()
        if job not in engines:
            engines[job] = stack.enter_context(needle_cli.LazyRuntime(
                lambda: needle_cli.open_runtime(args, may_install=not args.agent and not interactive,
                                                job=job)))
        return engines[job]

    def classify_system():
        return engine("system")

    confirm = needle_cli._never if args.agent else needle_cli._terminal_confirm
    jobs = Jobs(engine, classify_system, confirm=confirm, agent=args.agent,
                agents_cwd=os.getcwd(), files_cwd=None,
                under_overlay=getattr(args, "under_overlay", False), commands=commands)
    return jobs, stack


def _yes(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    sys.stdout.write(question)
    sys.stdout.flush()
    return sys.stdin.readline(16).strip().casefold() in ("y", "yes")


def interactive(request: str, jobs: Jobs) -> int:
    """Route, plan, show; act only on an explicit yes, for every act."""
    plan = ask(request, jobs, act=False)
    print(render(plan))
    route = plan["route"]
    runnable = set(router.RUNS) | ({'commands'} if getattr(jobs, 'commands', None) else set())
    if route.get("stage") not in (0, 1) or route.get("job") not in runnable \
            or not admits(route["job"], plan):
        return 1
    if not _yes("Do it? [y/N] "):
        print("not run")
        return 1
    done = ask(request, jobs, act=True, assume_yes=True,
               route=router.Route(route["stage"], route["job"], route.get("p"), route.get("margin"),
                                  [tuple(a) for a in route.get("alternatives") or []],
                                  list(route.get("claims") or [])))
    print(render(done))
    return 0 if done.get("status") == 0 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kilix-needle ask", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request", nargs="*", help="what to do, in plain words")
    parser.add_argument("--plan", "--dry-run", dest="plan", action="store_true",
                        help="route and plan; run nothing")
    parser.add_argument("--json", action="store_true", help="print the record as JSON")
    parser.add_argument("--explain", action="store_true",
                        help="print the router's ranking and the features behind it")
    parser.add_argument("--job", choices=router.RUNS, help="skip routing and use this job")
    parser.add_argument("--yes", action="store_true",
                        help="run closing, typing and program starts without asking; "
                             "never overrides a refusal or an unsure route")
    parser.add_argument("--agent", action="store_true", help="called by an agent: never prompts")
    parser.add_argument("--interactive", action="store_true",
                        help="show the route and plan, then ask before acting")
    parser.add_argument("--hold", action="store_true",
                        help="with --interactive: wait for Enter before exiting")
    parser.add_argument("--under-overlay", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--engine", metavar="FILE", help=argparse.SUPPRESS)
    parser.add_argument("--root", help=argparse.SUPPRESS)
    parser.add_argument('--commands-backend', choices=('grammar', 'needle3', 'lfm2'),
                        default=os.environ.get('KILIX_ASK_COMMANDS_BACKEND'),
                        help='use the shared command contract; other jobs keep deterministic parsing')
    parser.add_argument('--commands-candidate', help='verified local Needle3 candidate manifest')
    parser.add_argument('--commands-bundle', help='local LFM2 bundle matching the selected release pins')
    parser.add_argument('--commands-model-only', action='store_true',
                        help='measure model proposals without the command grammar fast path')
    parser.add_argument('--operation-id', help='reuse an incomplete command operation without replaying actions')
    args = parser.parse_args(argv)
    request = " ".join(args.request).strip()
    if not request:
        parser.error("give a request")
    if not args.commands_backend and (args.commands_candidate or args.commands_bundle
                                     or args.commands_model_only or args.operation_id):
        parser.error('command options require --commands-backend')
    if args.explain:
        try:
            print(explain(request))
        except router.RouterError as error:
            print(f"kilix-needle ask: {error}", file=sys.stderr)
            return 1
        return 0
    if args.interactive and (args.agent or args.json or args.yes or args.plan or args.job):
        parser.error("--interactive cannot be combined with --agent, --json, --yes, --plan or --job")
    jobs, stack = cli_jobs(args, interactive=args.interactive)
    try:
        with stack:
            if args.interactive:
                status = interactive(request, jobs)
            else:
                record = ask(request, jobs, act=not args.plan, assume_yes=args.yes, job=args.job)
                print(json.dumps(record, ensure_ascii=False) if args.json else render(record))
                status = 0 if record.get("status") == 0 else 1
    except KeyboardInterrupt:
        print("\nkilix-needle ask: interrupted", file=sys.stderr)
        status = 1
    except Exception as error:      # noqa: BLE001 - engines and assets fail with a message
        print(f"kilix-needle ask: {error}", file=sys.stderr)
        status = 2 if not args.interactive else 1
    if args.interactive and args.hold and sys.stdin.isatty():
        try:
            input("Press Enter to close.")
        except EOFError:
            pass
    return status
