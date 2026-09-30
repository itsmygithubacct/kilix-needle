"""kilix-needle: plain requests to Kilix, one job per kind of request.

    kilix-needle close the pane titled build        # panes (this command)
    kilix-needle agents "start codex in /abs/dir"   # coding-agent sessions
    kilix-needle apps "open the pdf viewer"         # apps, games, settings
    kilix-needle system "is jq installed"           # read-only machine state
    kilix-needle files 'find files named "x" in here'
    kilix-needle logs search --session NAME --query TEXT
    kilix-needle tmux --socket /abs/socket 'list sessions'
    kilix-needle action --yes --request-json -     # structured, no model

Agents: act directly with `--agent --json --yes`. A refused request runs
nothing, so a `--dry-run` first only adds a call; a refused record carries
`hint`, the accepted form or the job to use, so one retry is enough. Exact
pane commands need no model: "close pane 70", "run 'make' in the pane titled
build", "split right and run htop", "go to tab 2".

Each request's calls pass the checks in `actions`, resolve against a fresh
`kilix @ ls`, and are printed before anything runs. Opening, focusing,
arranging, renaming and resizing run straight away. Closing, typing into a
pane and starting a program wait for `y`; `--yes` answers that for scripts and
agents, but never overrides a refusal.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
import os
import sys
import time
from typing import Callable

from actions import LEGACY_TOOLS, Action, Refusal, interpret, plain
import app_controls
import asset
import history
import jobs
from engine import Engine, EngineError, check_prompt, reply_calls
import kilix
from libengine import LibEngine, LibEngineError
import toolset
import tuning


class Runtime:
    """The engine in use, what its calls are translated through, and its images."""

    def __init__(self, engine, images, translate=lambda calls: calls, label="base"):
        self.engine, self.images, self.translate, self.label = engine, images, translate, label

    def __enter__(self) -> "Runtime":
        self.engine.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        self.engine.close()
        for image in self.images:
            image.close()

    def reset(self) -> None:
        self.engine.reset()

    def complete(self, text: str) -> dict:
        return self.engine.complete(text)


class LazyRuntime:
    """A runtime opened on first use and closed once, whatever happens."""

    def __init__(self, factory):
        self.factory, self.runtime = factory, None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        if self.runtime is not None:
            self.runtime.close()

    def __getattr__(self, name):
        if self.runtime is None:
            runtime = self.factory()
            try:
                runtime.__enter__()
            except BaseException:
                runtime.close()
                raise
            self.runtime = runtime
        return getattr(self.runtime, name)


def open_runtime(args, *, may_install: bool = False, job: str = jobs.DEFAULT) -> Runtime:
    """The tuned model if one passed its gates and is selected, else the base engine."""
    if job == "system":
        import system_model
        return system_model.open_runtime(args)
    explicit = getattr(args, "engine", None) or os.environ.get("KILIX_NEEDLE_ENGINE")
    if job == "apps" and not explicit:
        # The apps checks propose as well as admit (apps.propose): measured,
        # no model reaches what they read, and none is loaded. An explicit
        # engine still runs a model, for benchmarks.
        import apps
        return Runtime(apps.Proposer(), [], label="grammar")
    if job == "files":
        import files_job
        if not explicit:
            return Runtime(files_job.Baseline(), [], label="files grammar baseline")
        tuned_tools, tuned_translate, base_tools = files_job.TOOLS, (lambda calls: calls), files_job.TOOLS
    elif job == "apps":
        import apps
        tuned_tools, tuned_translate, base_tools = apps.TOOLS, (lambda calls: calls), apps.TOOLS
    elif job == "agents":
        import agents
        tuned_tools, tuned_translate, base_tools = (agents.TOOLS, (lambda calls: calls),
                                                    agents.TOOLS)
    else:
        tuned_tools, tuned_translate, base_tools = toolset.TOOLS, toolset.to_actions, LEGACY_TOOLS
    choice = None if explicit else tuning.selected(job)
    if choice is not None:
        library = weights = None
        try:
            dev_library = os.environ.get("KILIX_NEEDLE_LIBRARY")
            library = asset.library_from_file(dev_library) if dev_library \
                else asset.installed_library(getattr(args, "root", None))
            weights = asset.load_verified(choice["weights"], choice["sha256"],
                                          os.path.getsize(choice["weights"]))
            return Runtime(LibEngine(library, tuned_tools, weights), [library, weights],
                           tuned_translate, label=f"tuned {choice.get('run', '')}".strip())
        except (asset.AssetError, OSError, KeyError) as error:
            for image in (library, weights):
                if image is not None:
                    image.close()
            print(f"kilix-needle: the selected tuned model is unavailable ({error}); "
                  "using the base model", file=sys.stderr)
    image = _image(args, may_install=may_install)
    return Runtime(Engine(image, base_tools), [image])


@dataclass(frozen=True)
class Options:
    dry_run: bool = False
    assume_yes: bool = False
    # An agent harness is calling: it may never close its own pane or tab, and
    # nothing is ever read from stdin (for MCP, stdin is the protocol stream).
    agent: bool = False
    under_overlay: bool = False


def _terminal_confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    sys.stdout.write(question)
    sys.stdout.flush()
    answer = sys.stdin.readline(16)
    return answer.strip().casefold() in ("y", "yes")


def _never(_question: str) -> bool:
    return False


def run_request(engine: Engine, request: str, options: Options,
                confirm: Callable[[str], bool] = _terminal_confirm) -> dict:
    """One request, as a record: {"request", "status", "note", "items": [...]}.

    status 0 = done or nothing to do, 1 = something was refused, skipped or failed.
    Each item has "outcome": refused | unresolved | would | skipped | done | failed.
    """
    def exact(request):
        # "help" sent as a request answers with the jobs, not a refused open_tab.
        if re.fullmatch(r"\s*(?:(?:show|print|get)\s+(?:me\s+)?(?:the\s+)?)?(?:help|usage|--help|-h)"
                        r"(?:\s+(?:please|text|page))?[\s.!?]*", request, re.I):
            return {"request": request, "status": 0, "items": [],
                    "note": "kilix-needle --help lists the jobs; this command is the panes job",
                    "hint": "accepted forms: close the pane titled NAME | run 'CMD' in pane N | "
                            "split right and run htop | go to tab 2 | kilix-needle agents \"start codex in "
                            "/abs/dir\" | kilix-needle system \"is jq installed\" | kilix-needle apps "
                            "\"open the pdf viewer\" | kilix-needle files 'find files named \"x\" in here'"}
        # A canonical pane command needs no model (panes_exact); its calls go
        # through the same checks, resolution and confirmation.
        import panes_exact
        if panes_exact.missing_command(request):
            reason = "the typing request is missing the command text"
            return {"request": request, "status": 1, "note": reason,
                    "items": [{"kind": "run_in_pane", "args": {}, "outcome": "refused", "reason": reason}],
                    "hint": 'include the command; e.g. kilix-needle --agent \'run "make test" in pane 70\'; '
                            'single-quote the whole request so the shell preserves its command text'}
        found = panes_exact.admitted(request)
        if not found:
            # An agents request sent to the panes job (the bare CLI): run it as the
            # agents job when that job's grammar reads it completely (gpt-6-luna
            # rerun: CLI launches 1/9, the panes model ran out of its token budget).
            import agents
            launch = agents.exact_calls(request)
            if launch:
                record = run_agents_calls(request, launch, options)
                record["job"] = "agents"
                return record
            return None
        calls, canonical = found
        # Checked and confirmed as the canonical sentence it was read as; the
        # history keeps the words the person or agent sent.
        record = run_calls(canonical, calls, options, confirm)
        record["request"] = request
        if canonical.casefold() != " ".join(request.split()).strip(" .!").casefold():
            record["read_as"] = canonical       # only when it differs: no echo
        return record

    return _recorded("panes", engine, request, options, lambda request, calls: run_calls(
        request, getattr(engine, "translate", lambda c: c)(calls), options, confirm), exact=exact)


# One accepted phrasing per refused kind, so an agent can restate the request
# once instead of guessing (route benchmark 2026-09-29: runs rephrased 8-20
# times, up to 3.1M tokens).
_EXAMPLES = {
    "panes": {
        "run_in_pane": "run 'make test' in the build pane  |  run 'make test' in pane 70",
        "open_pane": "split right and run htop",
        "open_tab": "open a new tab running htop",
        "close_pane": "close the logs pane  |  close pane 70",
        "close_tab": "close tab 2",
        "go_to_pane": "go to the logs pane  |  go to pane 70",
        "go_to_tab": "go to tab 2",
        "rename_tab": "rename this tab to api",
        "rename_pane": "rename this pane to api",
        "arrange_panes": "arrange the panes in a grid",
        "resize_pane": "make this pane wider by 10",
        "maximize_pane": "maximize this pane",
        "swap_panes": "swap this pane with the one on the right",
        "move_tab": "move this tab to position 1",
    },
    "agents": "start codex in /abs/dir  |  wait for codex in /abs/dir to finish  |  "
              "tell codex in /abs/dir: run the tests",
    "apps": "open the pdf viewer  |  play super kilix  |  hide the clock  |  set the font size to 14",
    "system": "show memory  |  show failed services  |  is curl installed?",
    "files": "find pdf files in Downloads  |  find text \"TODO\" in projects",
}
_MCP = {"agents": "kilix_agents_act", "system": "kilix_system_read", "files": "kilix_files_read"}
_CLI = {"panes": "kilix-needle", "agents": "kilix-needle agents", "apps": "kilix-needle apps",
        "system": "kilix-needle system", "files": "kilix-needle files"}


_JOB_FLAGS = ("--agent", "--json", "--yes", "--dry-run", "--baseline")


def _named_job(job: str, request: str) -> tuple[str, list, str] | None:
    """(job, flags, request) when the request opens with a job's name: "agents
    ...", or a whole "kilix-needle [flags] system [flags] ..." command line. A
    bare job word counts only under the default job; under a named job only a
    command line does ("agents in tab 2 ..." is an agents request)."""
    words = request.split()
    cli = words[:1] == ["kilix-needle"]
    words = words[1:] if cli else words
    flags = []
    while words and words[0] in _JOB_FLAGS:
        flags.append(words.pop(0))
    if not words or words[0] not in ("agents", "apps", "system") or len(words) < 2:
        return None
    target = words.pop(0)
    if not cli and job != jobs.DEFAULT:
        return None
    while words and words[0] in _JOB_FLAGS:
        flags.append(words.pop(0))
    if "--baseline" in flags and target != "system":
        return None
    return (target, flags, " ".join(words)) if words else None


_COMMAND_LINE = re.compile(r"^\s*kilix-needle\s+(?:--[\w-]+\s+)*(?P<job>agents|apps|system|files)\s+"
                           r"(?:--[\w-]+\s+)*(?P<q>[\"']?)(?P<request>.+?)(?P=q)\s*$")


def _unwrapped(request: str) -> tuple[str | None, str]:
    """A whole "kilix-needle system --json "is jq installed"" line sent as a
    request: the job it names and the request inside it."""
    m = _COMMAND_LINE.match(request)
    return (m["job"], m["request"]) if m else (None, request)


def _other_job(job: str, request: str) -> str | None:
    """A job whose own grammar reads the request, when it is not this one."""
    named, request = _unwrapped(request)
    if named and named != job:
        return named
    try:
        if job != "agents":
            import agents
            if agents.parse(request, None):
                return "agents"
        if job != "system":
            import system_job
            if system_job.parse(request):
                return "system"
        if job != "files":
            import files_job
            try:
                files_job.parse(request)
                return "files"
            except ValueError:
                pass
    except Exception:       # noqa: BLE001 - a hint never breaks a request
        return None
    return None


def _hint(job: str, request: str, result: dict) -> str | None:
    items = result.get("items") or []
    # A panes request that read as nothing returns status 0 and still needs a hint.
    if (not result.get("status") and (job != "panes" or items)) \
            or any(i.get("outcome") in ("done", "would") for i in items):
        return None
    other = _other_job(job, request)
    if other:
        inner = _unwrapped(request)[1]
        sentence = None
        if other == "system":
            # the system job's own accepted sentence, when it reads the request back
            try:
                import system_agent
                import system_job
                sentence = system_agent.canonical(inner) or (inner if system_job._parse(inner) else None)
            except Exception:       # noqa: BLE001 - a hint never breaks a request
                sentence = None
        said = json.dumps(sentence or inner) if (sentence or inner != request) else "REQUEST"
        return (f"this reads as a request for the {other} job: {_CLI[other]} {said} "
                f"(MCP: {_MCP.get(other, _CLI[other])})")
    examples = _EXAMPLES.get(job)
    if isinstance(examples, dict):
        kinds = [i.get("kind") for i in items if i.get("kind") in examples]
        if kinds:
            return "accepted form: " + examples[kinds[0]]
        if job == "panes" and not items:
            # Nothing to do was read ("find the pane titled X and report its id"):
            # this job acts on panes and reports nothing, so name what it does
            # with the pane the request names.
            named = re.search(r"\bpane\s+(?:titled|named|called)\s+(\"[^\"]+\"|'[^']+'|[\w.+@:-]+)", request, re.I)
            pane = f"the pane titled {named.group(1)}" if named else "the logs pane"
            return (f"this job acts on panes and does not report them (pane ids and titles: "
                    f"`kilix pane list`); accepted forms: go to {pane}  |  "
                    f"close {pane}  |  run 'make test' in {pane}  |  split right and run htop")
        return None
    return f"accepted forms: {examples}" if examples else None


def _recorded(job: str, engine, request: str, options: Options, run, exact=None) -> dict:
    """Check the prompt, ask the engine, run its calls, and record the request
    in the local history (history.py) whatever happens."""
    started = time.monotonic()
    given, checked, calls, result, private = request, None, None, None, None
    try:
        try:
            request = checked = check_prompt(request)
        except ValueError as error:
            result = {"request": request, "status": 1, "note": str(error), "items": []}
            return result
        if exact is not None and (result := exact(request)) is not None:
            engine = type("Exact", (), {"label": "control"})()   # recorded as the exact route
            return result
        engine.reset()
        calls, unusable = reply_calls(engine.complete(request))
        if unusable:
            # nothing from an error-marked or malformed reply runs (review KN-R18-04)
            result = {"request": request, "status": 1, "items": [],
                      "note": f"the engine's reply could not be used: {unusable}"}
            # the caller sees the engine's words; the history keeps none of them
            # (review KN-R18-201)
            private = dict(result, note="the engine's reply could not be used")
            return result
        result = run(request, calls)
        hint = _hint(job, request, result) if isinstance(result, dict) else None
        if hint:
            result["hint"] = hint
        return result
    finally:
        try:        # never replaces the result or the exception on its way out
            history.record(job, given, checked, engine, calls, options,
                           private or result or {"status": None,
                                                 "note": "the request did not finish"},
                           time.monotonic() - started)
        except Exception:       # noqa: BLE001
            pass


def run_calls(request: str, calls: list, options: Options,
              confirm: Callable[[str], bool] = _terminal_confirm) -> dict:
    """Shared execution path for Needle 2 and external kilix-ml inference.

    Calls are untrusted model proposals. They always pass interpretation,
    resolution, confirmation and the agent's own-pane protection here.
    """
    record = {"request": request, "status": 0, "note": "", "items": []}
    items = record["items"]
    try:
        from domain_bridge import check_request, validate
        request = check_request(request)
        validate(request, calls)
    except ValueError as error:
        record.update(status=1, note=str(error))
        return record
    if options.agent:
        confirm = _never
    results = interpret(request, calls)
    if not results:
        record["note"] = "There is no pane or tab action in that request."
        return record

    refused = [item for item in results if isinstance(item, Refusal)]
    for item in refused:
        items.append({"kind": item.kind, "outcome": "refused", "reason": item.reason})
    actions = [item for item in results if isinstance(item, Action)]
    hold = bool(refused)
    unplain = plain(request, actions)
    if refused:
        record["status"] = 1
    if hold and actions:
        record["note"] = "part of the request was refused, so nothing runs without a yes"

    # One snapshot per request: every action is resolved against the desktop
    # the person was looking at, and performed by id. Review KN-R4-01: with a
    # snapshot per action, "close tab 2 and close tab 3" closed tab 2 and then
    # the tab that had been tab 4, because Kilix renumbers at once.
    try:
        tree = kilix.snapshot(under_overlay=options.under_overlay) if actions else None
    except kilix.KilixError as error:
        tree, snapshot_error = None, str(error)
    # Once any action is refused, unresolved or fails, nothing later runs on a
    # yes given in advance (review KN-R4-04: an unresolved go-to was skipped
    # and the close after it ran).
    broken = False
    for action in actions:
        entry = {"kind": action.kind, "args": dict(action.args)}
        items.append(entry)
        if tree is None:
            entry.update(outcome="unresolved", reason=snapshot_error)
            record["status"] = 1
            broken = True
            continue
        # Without a known caller the own-pane guard below cannot work, and
        # "current" would mean the user's focused pane (review KN-03: an agent
        # with no KITTY_WINDOW_ID closed the user's vim pane in another tab).
        # Relative panes too (review KN-R2-04: "run rm -rf build in the right
        # pane" typed into the user's pane): every reference resolves against
        # a pane that is not the agent's, so nothing risky runs at all.
        if options.agent and tree.caller is None and (
                action.risky or "current" in action.args.values()):
            entry.update(outcome="refused",
                         reason="cannot tell which pane is the agent's own, so nothing "
                                "risky and no 'this pane' from an agent here")
            record["status"] = 1
            broken = True
            continue
        try:
            step = kilix.resolve(action, tree)
        except kilix.KilixError as error:
            entry.update(outcome="unresolved", reason=str(error))
            record["status"] = 1
            broken = True
            continue
        entry["summary"] = step.summary
        if options.agent and tree.caller is not None and tree.caller.get("id") in step.closes:
            entry.update(outcome="refused",
                         reason="an agent may not close its own pane or the tab it is in")
            record["status"] = 1
            broken = True
            continue
        if options.dry_run:
            entry["outcome"] = "would"
            continue
        # --yes answers for a risky action; a partly refused request still needs a
        # person, because what survived may be the wrong half of a misreading.
        needs_yes = hold or action.risky
        # A yes given in advance (--yes, MCP confirm_risky) covers only a plain
        # instruction; anything else waits for a person who sees the target
        # (review R2: word lists of what to refuse were always one word short).
        # Never on a yes given in advance: a target found only by a whole word
        # of a title, or a close of the requester's own pane or tab (review
        # KN-R5-01; an agent is refused that outright, above).
        own = tree.caller is not None and tree.caller.get("id") in step.closes
        waived = (options.assume_yes and not hold and not broken and unplain is None
                  and not step.fuzzy and not step.elsewhere and not own)
        if needs_yes and not waived \
                and not confirm(f"  {step.summary}? [y/N] "):
            entry["outcome"] = "skipped"
            entry["reason"] = ("declined" if confirm is _terminal_confirm and sys.stdin.isatty()
                               else "needs a person's yes: an earlier action in the request "
                                    "did not go as asked" if broken and options.assume_yes
                               else "needs a person's yes: the target was found only by a word "
                                    "of its title" if step.fuzzy and options.assume_yes
                               else "needs a person's yes: another tab has a pane by that "
                                    "name too" if step.elsewhere and options.assume_yes
                               else "needs a person's yes: it closes the pane or tab this "
                                    "request was made from" if own and options.assume_yes
                               else "needs a yes and there is no one to ask" if unplain is None
                               or not options.assume_yes
                               else f"needs a person's yes: the request is not a plain "
                                    f"instruction ({unplain})")
            record["status"] = 1
            continue
        if step.types_into is not None:
            try:
                ready = kilix.still_at_prompt(step.types_into, under_overlay=options.under_overlay)
            except kilix.KilixError:
                ready = False
            if not ready:
                entry.update(outcome="unresolved",
                             reason="that pane is no longer at a shell prompt; nothing typed")
                record["status"] = 1
                broken = True
                continue
        try:
            kilix.perform(step)
        except kilix.KilixError as error:
            entry.update(outcome="failed", reason=str(error))
            record["status"] = 1
            return record
        # A later numbered tab still means the tab it was when the request was
        # made: its step was resolved above to an id, never to a position.
        entry["outcome"] = "done"
    return record


def run_agents_request(engine, request: str, options: Options,
                       confirm: Callable[[str], bool] = _terminal_confirm, *,
                       cwd: str | None = None) -> dict:
    """One agents-job request, as the same record as run_request."""
    def exact(request):
        # A request the agents grammar reads completely needs no model; its
        # calls are admitted by the same checks (agents.exact_calls).
        import agents
        found = agents.exact_calls(request)
        return run_agents_calls(request, found, options, cwd=cwd) if found else None

    return _recorded("agents", engine, request, options,
                     lambda request, calls: run_agents_calls(request, calls, options, cwd=cwd),
                     exact=exact)


def run_agents_calls(request: str, calls: list, options: Options, *,
                     cwd: str | None = None) -> dict:
    """Interpret and perform agents-job calls.

    The request is the consent (owner, 2026-09-27): an admitted request runs
    with no second yes, from a person or an agent. Anything refused means
    nothing in the request runs. Actions run in order and stop at the first
    that fails.
    """
    import agents
    import agents_kilix
    import os as _os
    record = {"request": request, "status": 0, "note": "", "items": []}
    results = agents.interpret(request, calls)
    if not results:
        record["note"] = "There is no coding-session action in that request."
        return record
    refused = [r for r in results if isinstance(r, Refusal)]
    if refused:
        record["status"] = 1
        record["note"] = "part of the request was refused, so nothing runs"
        record["items"] = [{"kind": r.kind, "outcome": "refused", "reason": r.reason}
                           for r in refused]
        return record
    for entry in agents_kilix.perform(results, cwd=cwd, dry_run=options.dry_run):
        entry["summary"] = agents_kilix.summary(entry)
        record["items"].append(entry)
        if entry["outcome"] == "failed":
            record["status"] = 1
    return record


def run_apps_request(engine, request: str, options: Options,
                     confirm: Callable[[str], bool] = _terminal_confirm) -> dict:
    """One apps-job request, as the same record as run_request."""
    def control(request):
        # Exact, model-independent controls (audio, music, voice, text size,
        # status): one whole request, one typed action (app_controls.py).
        found = app_controls.parse(request)
        return None if found is None else app_controls.run(found, request, options, confirm)
    return _recorded("apps", engine, request, options, lambda request, calls: run_apps_calls(
        request, getattr(engine, "translate", lambda c: c)(calls), options, confirm),
        exact=control)


def run_apps_calls(request: str, calls: list, options: Options,
                   confirm: Callable[[str], bool] = _terminal_confirm) -> dict:
    """Interpret, confirm and perform apps-job calls.

    Every launch and settings change needs a yes (review R12: "should I hide
    the clock?" once hid it with no one asked). A yes given in advance (--yes,
    MCP confirm_risky) gives it only for an action the request states plainly,
    and for a launch only of something already installed: a launch that may
    install always waits for a person. Opening the settings screen changes
    nothing and needs no yes. Once anything in the request is refused or
    fails, nothing later runs without a person's yes.
    """
    import apps
    import apps_kilix
    record = {"request": request, "status": 0, "note": "", "items": []}
    items = record["items"]
    if options.agent:
        confirm = _never
    results = apps.interpret(request, calls)
    if not results:
        record["note"] = "There is no app or settings action in that request."
        return record
    refused = [r for r in results if isinstance(r, Refusal)]
    for item in refused:
        items.append({"kind": item.kind, "outcome": "refused", "reason": item.reason})
    actions = [r for r in results if isinstance(r, apps.Action)]
    hold = bool(refused)
    unplain = apps.plain(request, actions)
    if refused:
        record["status"] = 1
    if hold and actions:
        record["note"] = "part of the request was refused, so nothing runs without a yes"
    elif unplain:
        # A proposal keeps only what the checks admit, so part of the request
        # may go unaccounted for: a plan says so too (review KN-R16-204).
        record["note"] = f"a person's confirmation is required: {unplain}"
    broken = False
    for action in actions:
        entry = {"kind": action.kind, "args": dict(action.args)}
        items.append(entry)
        step = apps_kilix.resolve(action)
        entry["summary"] = step.summary
        if options.dry_run:
            entry["outcome"] = "would"
            continue
        needs_yes = hold or broken or action.kind in apps.SIDE_EFFECT_KINDS
        waived = (options.assume_yes and not hold and not broken and unplain is None
                  and step.ready)
        # A request that isn't plain is shown whole, so the person sees what
        # else it says ("hide the clock later") and not only the action.
        question = (f"  {step.summary}? [y/N] " if unplain is None
                    else f"  {request!r} asks to {step.summary}? [y/N] ")
        if needs_yes and not waived and not confirm(question):
            entry["outcome"] = "skipped"
            entry["reason"] = ("declined" if confirm is _terminal_confirm and sys.stdin.isatty()
                               else "needs a person's yes: it may install first" if not step.ready
                               else "needs a person's yes: an earlier action in the request "
                                    "did not go as asked" if (hold or broken) and options.assume_yes
                               else f"needs a person's yes: the request is not a plain "
                                    f"instruction ({unplain})" if unplain and options.assume_yes
                               else "needs a yes and there is no one to ask")
            record["status"] = 1
            continue
        try:
            # Installing only on a person's own yes: a yes given in advance
            # never reaches an install (waived needs step.ready).
            apps_kilix.perform(step, install=not step.ready)
        except kilix.KilixError as error:
            entry.update(outcome="failed", reason=str(error))
            record["status"] = 1
            broken = True
            continue
        entry["outcome"] = "done"
    return record


def render(record: dict) -> str:
    """The human form of a request record."""
    lines = []
    items = record["items"]
    if not items and record["note"]:
        prefix = "kilix-needle: " if record["status"] else ""
        return prefix + record["note"]
    for item in items:
        if item["outcome"] == "refused" and "summary" not in item:
            lines.append(f"  refused {item['kind']}: {item['reason']}")
    if record["note"]:
        lines.append(f"  {record['note']}")
    for item in items:
        outcome = item["outcome"]
        if outcome == "refused" and "summary" not in item:
            continue
        if outcome == "unresolved":
            lines.append(f"  cannot {item['kind'].replace('_', ' ')}: {item['reason']}")
        elif outcome == "refused":
            lines.append(f"  refused: {item['summary']}: {item['reason']}")
        elif outcome == "would":
            lines.append(f"  would {item['summary']}")
        elif outcome == "skipped":
            lines.append("  skipped" if item["reason"] == "declined" else
                         f"  not run, {item['reason']}: {item['summary']}"
                         if item["reason"].startswith("needs a person's yes") else
                         f"  not run without a terminal to confirm: {item['summary']}")
        elif outcome == "failed":
            lines.append(f"  failed: {item['reason']}")
        else:
            lines.append(f"  done: {item['summary']}")
            if "result" in item:
                lines.append(json.dumps(item["result"], ensure_ascii=True, indent=2))
    return "\n".join(lines)


def handle(engine: Engine, request: str, *, dry_run: bool = False, assume_yes: bool = False,
           out=None, as_json: bool = False, agent: bool = False,
           under_overlay: bool = False, job: str = jobs.DEFAULT,
           baseline: bool = False, model_label=None) -> int:
    """Run one request and print it. 0 = done or nothing to do, 1 = otherwise."""
    options = Options(dry_run=dry_run, assume_yes=assume_yes, agent=agent,
                      under_overlay=under_overlay)
    import system_collect
    run = {"apps": run_apps_request, "agents": run_agents_request}.get(job, run_request)
    if job == "system":
        import system_dispatch
        record = system_dispatch.dispatch(request, engine, options, baseline=baseline,
                                          model_label=model_label)
    elif job == "agents":
        record = run(engine, request, options, _never if agent else _terminal_confirm,
                     cwd=os.getcwd())
    else:
        record = run(engine, request, options, _never if agent else _terminal_confirm)
    print(json.dumps(record, ensure_ascii=False) if as_json else
          system_collect.render(record) if job == "system" else render(record), file=out)
    return record["status"]


def _handle_system(args, request: str, modes: dict) -> int:
    """Open the tuned system runtime only when this request needs inference."""
    source = {"label": None}

    def classify(text):
        with open_runtime(args, job="system") as runtime:
            source["label"] = runtime.label
            runtime.reset()
            return runtime.complete(text)

    return handle(classify, request, **modes, model_label=lambda: source["label"])


def _offer_tuning(args) -> None:
    """After the first install, at a terminal only: offer to fine-tune in the background."""
    missing = asset.missing_for_tuning(getattr(args, "root", None))
    print("\nkilix-needle can fine-tune Needle 2 for Kilix on this machine. It runs in the\n"
          "background at the lowest priority, takes several hours, peaks near 9 GB of\n"
          "memory, and is used only if it beats the base model on every benchmark gate.")
    if missing:
        print(f"It needs {', '.join(missing)} first: run `kilix-needle install --tuning`,\n"
              "then `kilix-needle tune --background`.\n")
        return
    answer = input("Start fine-tuning now? [Y/n] ").strip().casefold()
    if answer in ("", "y", "yes"):
        tuning.main(["--background"])
    else:
        print("Not started. Run `kilix-needle tune --background` at any time.\n")


def _image(args, *, may_install: bool = False):
    """The engine to run: --engine, then KILIX_NEEDLE_ENGINE, then the installed asset.

    On first use at a terminal, a missing asset leads into `install`: the
    licence screen and typed agreement, then the download.
    """
    engine_file = getattr(args, "engine", None) or os.environ.get("KILIX_NEEDLE_ENGINE")
    if engine_file:
        return asset.from_file(engine_file)
    try:
        return asset.from_installed(args.root)
    except asset.AssetError as error:
        missing = "is not installed" in str(error) or "has not been accepted" in str(error)
        if not (may_install and missing and sys.stdin.isatty() and sys.stdout.isatty()):
            raise
        print(f"kilix-needle: {error}\nFirst use: installing the Needle 2 engine.\n")
        asset.install(args.root)
        _offer_tuning(args)
        return asset.from_installed(args.root)


def _render_system_suggestion(plan: dict) -> None:
    """Display a proposal without implying that any observation was collected."""
    def safe(value) -> str:
        return json.dumps(str(value), ensure_ascii=True)[1:-1]

    trust = plan.get("trust")
    path = plan.get("path")
    if plan.get("runtime_error"):
        reasons = "; ".join(safe(reason) for reason in plan.get("reasons") or [])
        print(f"kilix-needle: suggestion unavailable: {reasons or 'classifier failed'}",
              file=sys.stderr)
        return
    if plan.get("protocol_error"):
        reasons = "; ".join(safe(reason) for reason in plan.get("reasons") or [])
        print(f"kilix-needle: invalid suggestion: {reasons or 'invalid classifier reply'}",
              file=sys.stderr)
        return
    if path == "grammar" and trust == "grammar":
        print("Grammar plan (no observations collected):")
    elif path == "model" and trust == "model_proposal":
        print("Model-proposed read-only queries (unverified; no observations collected):")
    else:
        print("No read-only query suggested; no observations collected.")
    for kind, args in plan.get("actions") or []:
        print(f"  {safe(kind)}: {json.dumps(args, ensure_ascii=True, sort_keys=True)}")
    for reason in plan.get("reasons") or []:
        print(f"  {safe(reason)}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["action"]:
        from action_cli import main as action_main
        return action_main(argv[1:])
    if argv[:1] == ["tmux"]:
        from tmux_cli import main as tmux_main
        return tmux_main(argv[1:])
    if argv[:1] == ["files"]:
        from files_cli import main as files_main
        return files_main(argv[1:])
    if argv[:1] == ["logs"]:
        from needle_logs.cli import main as logs_main
        return logs_main(argv[1:])
    if argv[:1] in (["contract"], ["bridge"]):
        import domain_bridge
        return domain_bridge.main(argv)
    if argv[:1] == ["setup"]:
        import setup_surfaces
        parser = argparse.ArgumentParser(prog="kilix-needle setup",
                                         description=setup_surfaces.__doc__,
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--undo", action="store_true")
        parser.add_argument("--only", default=",".join(setup_surfaces.SURFACES))
        args = parser.parse_args(argv[1:])
        only = [name.strip() for name in args.only.split(",") if name.strip()]
        unknown = [name for name in only if name not in setup_surfaces.SURFACES]
        if unknown:
            parser.error(f"unknown surface: {', '.join(unknown)}")
        status, lines = setup_surfaces.setup(only, undo=args.undo, dry_run=args.dry_run)
        print("\n".join(lines))
        return status
    if argv[:1] == ["tune"]:
        return tuning.main(argv[1:])
    if argv[:1] == ["system-model"]:
        import system_model
        return system_model.main(argv[1:])
    if argv[:1] == ["install"]:
        parser = argparse.ArgumentParser(prog="kilix-needle install",
                                         description="Accept the Needle 2 licence and install "
                                                     "the engine (the needle2 content asset).")
        parser.add_argument("--from", dest="supplied", metavar="DIR",
                            help="read the files from DIR instead of downloading them")
        parser.add_argument("--tuning", action="store_true",
                            help="also install what fine-tuning needs: the base checkpoint "
                                 "and tokenizer (needle2-train) and libneedle.so "
                                 "(needle2-runtime), each with its own licence screen")
        parser.add_argument("--runtime", action="store_true",
                            help="also install libneedle.so (needle2-runtime), which runs a "
                                 "tuned model, with its own licence screen")
        parser.add_argument("--root")
        args = parser.parse_args(argv[1:])
        wanted = [asset.ASSET_ID] + (list(asset.TUNING_ASSETS) if args.tuning
                                     else [asset.RUNTIME_ID] if args.runtime else [])
        for asset_id in wanted:
            # An asset already accepted and installed is not asked about again:
            # `install --runtime` after `install` re-showed the needle2 screen,
            # and declining it stopped before the runtime (measured).
            try:
                _spec, where = asset._installed_spec(asset_id, args.root)
            except asset.AssetError:
                pass
            else:
                print(f"already installed: {where}")
                continue
            try:
                where = asset.install(args.root, supplied=args.supplied, asset_id=asset_id)
            except asset.AssetError as error:
                print(f"kilix-needle: {error}", file=sys.stderr)
                return 1
            print(f"installed: {where}")
        return 0
    if argv[:1] == ["mcp"]:
        import mcp_server
        parser = argparse.ArgumentParser(prog="kilix-needle mcp",
                                         description=mcp_server.__doc__,
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("--engine", metavar="FILE")
        parser.add_argument("--root")
        args = parser.parse_args(argv[1:])
        return mcp_server.serve(lambda job=jobs.DEFAULT: open_runtime(args, job=job))

    job = jobs.DEFAULT
    if argv[:1] == ["apps"]:
        # Launch Kilix apps and games and change Kilix settings (the apps job).
        job, argv = "apps", argv[1:]
    elif argv[:1] == ["agents"]:
        # Launch, wait for and message coding-agent sessions (the agents job).
        job, argv = "agents", argv[1:]
    elif argv[:1] == ["system"]:
        job, argv = "system", argv[1:]
    agents_help = ("Directory resolution order: an explicit ~/ or absolute path; 'here' "
                   "(the calling pane's directory); exact entries in "
                   "~/.config/kilix-needle/dirs.json; then a unique exact checkout name "
                   "across all scanned depths under ~/gpu_terminal or ~/research (to depth "
                   "3). Names shorter than 3 characters are not scanned. dirs.json is a "
                   "JSON object mapping request names and aliases to absolute paths.")
    parser = argparse.ArgumentParser(prog=f"kilix-needle {job}" if job != "panes" else "kilix-needle",
                                     description=__doc__,
                                     epilog=agents_help if job == "agents" else None,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request", nargs="*", help="what to do; omit for a prompt loop")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and run nothing")
    parser.add_argument("--yes", action="store_true",
                        help="run closing, typing and program starts without asking; "
                             "never overrides a refusal")
    parser.add_argument("--json", action="store_true", help="print one JSON record per request")
    parser.add_argument("--agent", action="store_true",
                        help="called by an agent: never prompts, and never closes its own "
                             "pane or tab")
    parser.add_argument("--under-overlay", action="store_true",
                        help="opened as an overlay (a hotkey): 'this pane' is the one beneath")
    parser.add_argument("--engine", metavar="FILE",
                        help="a local copy of the pinned engine instead of the installed asset")
    parser.add_argument("--root", help="the Kilix content root, if not inherited from Kilix")
    if job == "system":
        parser.description = "Read-only Linux diagnostics: resources, processes, services, journal and installed packages."
        parser.add_argument("--baseline", action="store_true",
                            help="use only the explicit grammar; disable model fallback")
        parser.add_argument("--suggest", action="store_true",
                            help="suggest read-only queries for an unfamiliar request; collect nothing")
    args = parser.parse_args(argv)
    named = _named_job(job, " ".join(args.request))
    if named:
        # The request itself opens with a job ("agents start codex in /x", or a
        # whole "kilix-needle system --json ..." line): run it as that job.
        target, flags, rest = named
        kept = [f"--{k.replace('_', '-')}" for k in ("dry_run", "yes", "json", "agent", "under_overlay")
                if getattr(args, k, False)]
        kept += [x for k in ("engine", "root") if getattr(args, k, None) for x in (f"--{k}", getattr(args, k))]
        return main([target, *dict.fromkeys(kept + flags), rest])
    if job == "system" and args.suggest and args.baseline:
        parser.error("--suggest and --baseline cannot be combined")
    if job == "system" and args.suggest and not args.request:
        parser.error("--suggest requires a request")
    if job == "system" and args.suggest:
        import system_normalize

        def classify(request):
            with open_runtime(args, may_install=not args.agent, job="system") as engine:
                engine.reset()
                return engine.complete(request)

        request = " ".join(args.request)
        suggestion = system_normalize.plan(request, classify)
        if args.json:
            print(json.dumps(suggestion, ensure_ascii=False))
        else:
            _render_system_suggestion(suggestion)
        return int(bool(suggestion.get("runtime_error") or suggestion.get("protocol_error")))
    modes = dict(dry_run=args.dry_run, assume_yes=args.yes, as_json=args.json,
                 agent=args.agent, under_overlay=args.under_overlay, job=job)
    if job == "system":
        modes["baseline"] = args.baseline
    try:
        # An exact request needs no engine at all (review KN-R18-03): an apps
        # control, an exact pane command, an agents launch its grammar reads. The
        # runtime opens on the first request that needs it: loading it first made
        # every exact panes CLI request wait ~3 s, and under load agents gave up on
        # the command before it answered (luna-full benchmark, 2026-09-30). The
        # system job runs without this runtime (_handle_system).
        if job != "system":
            runtime = LazyRuntime(lambda: open_runtime(args, may_install=not args.agent, job=job))
    except asset.AssetError as error:
        print(f"kilix-needle: {error}", file=sys.stderr)
        return 2
    try:
        from contextlib import nullcontext
        with (nullcontext(None) if job == "system" else runtime) as engine:
            if args.request:
                if job == "system":
                    return _handle_system(args, " ".join(args.request), modes)
                return handle(engine, " ".join(args.request), **modes)
            if args.agent or not sys.stdin.isatty():
                print("kilix-needle: give a request, or run it in a terminal", file=sys.stderr)
                return 2
            status = 0
            while True:
                try:
                    line = input("needle> ")
                except EOFError:
                    print()
                    return status
                if line.strip() in ("quit", "exit", ":q"):
                    return status
                if line.strip():
                    status = (_handle_system(args, line, modes) if job == "system" else
                              handle(engine, line, **modes))
    except (asset.AssetError, EngineError, LibEngineError) as error:
        print(f"kilix-needle: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nkilix-needle: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
