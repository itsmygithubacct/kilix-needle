"""The routes of experiment 11, job by job, on the functions the real routes
call: what each arm admits for one request, and which stage answered it.
Nothing acts on Kilix and no model runs here: a model's replies are read from
a cache taken beforehand (by the request's digest, as the route asks it).

    A  today's route: the exact readers or the job's grammar, then the job's
       model where one is loaded (panes: the tuned panes model; agents: the
       tuned agents model).
    B  the bundle, then the job's model where one is loaded. The stages of
       today's route that produce calls without a model are left out; those
       that only refuse are kept (the prompt check; for panes, "help" and a
       typing request with no command; for system, the request's bounds).
    C  the exact readers or the grammar, then the bundle, then the model.

A bundle's answer is a proposal like a model's, held to the job's own checks
(job_schemas.to_job writes it as the job's call):

    panes   needle_cli.checks_agree; a list is the exact reader's list and a
            read of a pane is checked against the desktop (pane_reads.check)
    files   files_job.interpret: it must equal the files grammar's reading
    system  system_job.interpret: it must equal the system grammar's reading
    agents  agents.interpret
    apps    apps.interpret
    tmux    tmux_job.reads_back: the grammar's own sentence for the call reads
            back as the call
    pty     pty_job.reads_back, and the read tool refuses an end or a kill
    logs    logs_words.check (the logs tool's argument checks)

Where the checks do not take the bundle's answer, the request goes on to the
job's model; where the job has none, it is refused.

`normalize(job, calls)` writes a label or an answer as the scorer compares
them: as the job's own checks normalise a call. `unsafe(kind, args)` says
whether an admitted call closes something, types or sends text, starts a
program or an agent, messages an agent, launches an app or ends a session.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re

ARMS = ("A", "B", "C")
# The tool an agent calls, and the job that answers it.
TOOLS = {"kilix_act": "panes", "kilix_files_read": "files", "kilix_system_read": "system",
         "kilix_agents_act": "agents", "kilix_apps_act": "apps", "kilix_tmux_act": "tmux",
         "kilix_pty_read": "pty", "kilix_logs_read": "logs"}
MODELS = ("panes", "agents")        # the jobs with a model behind their grammar today
UNITS = {"panes": "panes", "tabs": "tabs", "panes and tabs": "panes and tabs", "tabs and panes": "panes and tabs"}


def key_of(request: str) -> str:
    """The model cache's key: the sha256 of the request as the route asks the model."""
    return hashlib.sha256(request.encode("utf-8")).hexdigest()


class Desktop:
    """The panes a read is checked against (pane_reads.check asks the tree
    for its panes): the tabs of `kitty @ ls` ("kitty_ls": OS windows, each
    with its tabs, each with its windows), as the bench's desktop holds them."""

    def __init__(self, desktop: dict):
        self.tabs = [tab for os_window in desktop.get("kitty_ls", []) for tab in os_window.get("tabs", [])]

    def _all_panes(self):
        return [(tab, window) for tab in self.tabs for window in tab.get("windows", [])]


def answer(stage, calls=(), *, refused=False, held=False, error=None, missing=False, job=None, label=None):
    """What an arm did with a request: the stage that answered ("exact",
    "grammar", "bundle", "bundle second", "model", or a refusal's), the calls
    the checks admitted as [[kind, args]], and whether it was refused, partly
    refused (held), or had no model reply in the cache (missing). `label`
    is the kind the bundle read (its tool, or "compound") when it answered."""
    return {"stage": stage, "calls": [list(c) for c in calls], "refused": refused, "held": held,
            "error": error, "missing": missing, "job": job, "label": label}


class Routes:
    """The arms over one checkout. `bundles`: {job: panes_bundle.Bundle};
    `replies`: {job: {key_of(request): {"ok": reply} | {"error": text}}};
    `desktop`: what a read of a pane is checked against. `engines`, for
    timing whole calls: {job: an engine (reset, complete)} asked in place of
    the cache, and for "system" the normalizer, whose proposal A asks for as
    a hint when the grammar does not read the request, as the route does."""

    def __init__(self, bundles: dict, replies: dict, desktop: dict | None = None, engines: dict | None = None):
        os.environ.setdefault("KILIX_NEEDLE_BUNDLE", "0")
        self.bundles, self.replies = dict(bundles), dict(replies)
        self.desktop = Desktop(desktop or {})
        self.engines = dict(engines or {})
        self._reads: dict = {}

    # ---- shared ---------------------------------------------------------

    def read(self, job: str, request: str):
        """The job's bundle's reading: (calls, reason, texts); no calls when there is no bundle."""
        bundle = self.bundles.get(job)
        if bundle is None:
            return [], "no bundle", []
        key = (job, request, bundle.second_kinds)
        if key not in self._reads:
            texts: list = []
            if "\n" in request or "\r" in request:
                self._reads[key] = ([], "more than one line", [])
            else:
                calls, reason = bundle.read(request, texts)
                self._reads[key] = (calls, reason, texts)
        return self._reads[key]

    def model_calls(self, job: str, request: str):
        """(calls, error, missing) from the cached reply of the job's model."""
        from engine import reply_calls
        if job in self.engines:
            engine = self.engines[job]
            engine.reset()
            entry = {"ok": engine.complete(request)}
        else:
            entry = self.replies.get(job, {}).get(key_of(request))
        if entry is None:
            return [], None, True
        if "error" in entry:
            return [], entry["error"], False
        calls, unusable = reply_calls(entry["ok"])
        if unusable:
            return [], unusable, False
        if job == "panes":
            import toolset
            calls = toolset.to_actions(calls)       # the tuned panes model's translation (open_runtime)
        return calls, None, False

    def run(self, arm: str, job: str, request: str) -> dict:
        if arm not in ARMS:
            raise ValueError(arm)
        return getattr(self, "_" + job)(arm, request)

    # ---- panes ------------------------------------------------------------

    def _panes_checked(self, stage, sentence, calls, label=None):
        """run_calls: the request's bounds and the calls' form, then the checks."""
        from actions import Action, Refusal, interpret
        from domain_bridge import check_request, validate
        try:
            sentence = check_request(sentence)
            validate(sentence, calls)
        except ValueError as error:
            return answer(stage, refused=True, error=str(error), job="panes", label=label)
        results = interpret(sentence, calls)
        admitted = [[r.kind, dict(r.args)] for r in results if isinstance(r, Action)]
        held = any(isinstance(r, Refusal) for r in results)
        return answer(stage, admitted, refused=held and not admitted, held=held, job="panes", label=label)

    def _pane_read(self, stage, args, label=None):
        import pane_reads
        reason = pane_reads.check(args, self.desktop)
        if reason:
            return answer(stage, refused=True, error=reason, job="panes", label=label)
        return answer(stage, [[pane_reads.NAME, dict(args)]], job="panes", label=label)

    def _panes(self, arm, request):
        import actions
        import agents
        import needle_cli
        import pane_reads
        import panes_bundle
        import panes_exact
        from engine import check_prompt
        try:
            request = check_prompt(request)
        except ValueError as error:
            return answer("refused prompt", refused=True, error=str(error), job="panes")
        if arm != "B":
            unit = actions.inventory_unit(request)
            if unit is not None:
                return answer("exact", [["list", {"what": UNITS[unit]}]], job="panes")
            wanted = pane_reads.parse(request)
            if wanted is not None:
                return self._pane_read("exact", wanted)
        # The stages that only refuse, in every arm.
        if needle_cli.HELP_REQUEST.fullmatch(request):
            return answer("help", job="panes")
        if panes_exact.missing_command(request):
            return answer("refused", refused=True, error="missing command", job="panes")
        if arm != "B":
            found = panes_exact.admitted(request)
            if found:
                calls, canonical = found
                return self._panes_checked("exact", canonical, calls)
            launch = agents.exact_calls(request)
            if launch:
                # An agents request sent to the panes tool runs as the agents job.
                return self._agents_checked("exact agents", request, launch)
        if arm != "A":
            calls, reason, texts = self.read("panes", request)
            if calls:
                stage = "bundle second" if reason == panes_bundle.OK_SECOND else "bundle"
                names = [call["name"] for call in calls]
                kind = names[0] if len(names) == 1 else panes_bundle.COMPOUND
                if names == [panes_bundle.LIST]:
                    return answer(stage, [["list", {"what": calls[0]["arguments"]["what"]}]], job="panes", label=kind)
                if names == [panes_bundle.READ]:
                    return self._pane_read(stage, calls[0]["arguments"], label=kind)
                if needle_cli.checks_agree(request, calls, texts):
                    return self._panes_checked(stage, request, calls, label=kind)
        calls, error, missing = self.model_calls("panes", request)
        if missing or error is not None:
            return answer("model", refused=error is not None, error=error, missing=missing, job="panes")
        return self._panes_checked("model", request, calls)

    # ---- agents -----------------------------------------------------------

    def _agents_checked(self, stage, request, calls):
        import agents
        from actions import Refusal
        results = agents.interpret(request, calls)
        if not results:
            return answer(stage, job="agents")
        if any(isinstance(r, Refusal) for r in results):
            return answer(stage, refused=True, job="agents")       # anything refused: nothing runs
        return answer(stage, [[r.kind, dict(r.args)] for r in results], job="agents")

    def _agents(self, arm, request):
        import agents
        import job_schemas
        import panes_bundle
        from actions import Refusal
        from engine import check_prompt
        try:
            request = check_prompt(request)
        except ValueError as error:
            return answer("refused prompt", refused=True, error=str(error), job="agents")
        if arm != "B":
            found = agents.exact_calls(request)
            if found:
                return self._agents_checked("exact", request, found)
        if arm != "A":
            calls, reason, _ = self.read("agents", request)
            converted = job_schemas.to_job("agents", calls) if calls else None
            if converted:
                results = agents.interpret(request, converted)
                if results and not any(isinstance(r, Refusal) for r in results):
                    stage = "bundle second" if reason == panes_bundle.OK_SECOND else "bundle"
                    kind = calls[0]["name"] if len(calls) == 1 else panes_bundle.COMPOUND
                    return answer(stage, [[r.kind, dict(r.args)] for r in results], job="agents", label=kind)
        calls, error, missing = self.model_calls("agents", request)
        if missing or error is not None:
            return answer("model", refused=error is not None, error=error, missing=missing, job="agents")
        return self._agents_checked("model", request, calls)

    # ---- the jobs without a model ------------------------------------------

    def _bundle_or_refuse(self, job, request, admit):
        """B's and C's bundle stage for a job without a model: the bundle's
        answer as `admit` takes it ([[kind, args]] or None), else refused."""
        import job_schemas
        import panes_bundle
        calls, reason, _ = self.read(job, request)
        if not calls:
            return answer("refused", refused=True, error=reason, job=job)
        converted = job_schemas.to_job(job, calls)
        admitted = admit(converted) if converted else None
        if not admitted:
            return answer("refused", refused=True, error="the checks do not take the bundle's answer", job=job)
        stage = "bundle second" if reason == panes_bundle.OK_SECOND else "bundle"
        kind = calls[0]["name"] if len(calls) == 1 else panes_bundle.COMPOUND
        return answer(stage, admitted, job=job, label=kind)

    def _apps(self, arm, request):
        import app_controls
        import apps
        from actions import Refusal
        from engine import check_prompt, reply_calls
        try:
            request = check_prompt(request)
        except ValueError as error:
            return answer("refused prompt", refused=True, error=str(error), job="apps")

        def admit(calls):
            results = apps.interpret(request, calls)
            if not results or any(isinstance(r, Refusal) for r in results):
                return None
            return [[r.kind, dict(r.args)] for r in results]

        if arm != "B":
            control = app_controls.parse(request)
            if control is not None:
                return answer("exact", [["control", {"family": control.family, "operation": control.operation,
                                                     "target": control.target, "value": control.value}]],
                              job="apps")
            calls, unusable = reply_calls(apps.Proposer().complete(request))
            if calls and not unusable:
                results = apps.interpret(request, calls)
                admitted = [[r.kind, dict(r.args)] for r in results if isinstance(r, apps.Action)]
                held = any(isinstance(r, Refusal) for r in results)
                if admitted or held or arm == "A":
                    return answer("grammar", admitted, refused=held and not admitted, held=held, job="apps")
            elif arm == "A":
                return answer("refused", refused=True, job="apps")
        return self._bundle_or_refuse("apps", request, admit)

    def _files(self, arm, request):
        import files_job
        from actions import Refusal

        def admit(calls):
            results = files_job.interpret(request, calls)
            if any(isinstance(r, Refusal) for r in results):
                return None
            return [[a.kind, dict(a.args)] for a in results]

        if arm != "B":
            try:
                found = files_job.parse(request)
            except ValueError as error:
                if arm == "A":
                    return answer("refused", refused=True, error=str(error), job="files")
            else:
                return answer("grammar", [[a.kind, dict(a.args)] for a in found], job="files")
        return self._bundle_or_refuse("files", request, admit)

    def _system(self, arm, request):
        import system_job
        import system_normalize
        from actions import Refusal
        bounds = system_normalize._bounds(request)
        if bounds:
            return answer("refused", refused=True, error=bounds[1], job="system")

        def admit(calls):
            results = system_job.interpret(request, calls)
            if any(isinstance(r, Refusal) for r in results):
                return None
            return [[a.kind, dict(a.args)] for a in results]

        if arm != "B":
            found = system_job.parse(request)
            if found:
                return answer("grammar", [[a.kind, dict(a.args)] for a in found], job="system")
            if arm == "A":
                # The tuned normalizer's proposals are hints, not answers.
                if "system" in self.engines:
                    engine = self.engines["system"]
                    system_normalize.plan(request, lambda text: (engine.reset(), engine.complete(text))[1])
                return answer("hint", refused=True, job="system")
        return self._bundle_or_refuse("system", request, admit)

    def _operation(self, job, arm, request, parse, admit_one):
        """tmux, pty and logs: one operation, as {"operation", ...}."""
        if arm != "B":
            try:
                found = parse(request)
            except Exception as error:      # the grammar's refusal (Refused, ValueError)
                found, why = None, f"{type(error).__name__}: {error}"
            else:
                why = "the grammar does not read it"
            if found is not None:
                return answer("grammar", operations([found]), job=job)
            if arm == "A":
                return answer("refused", refused=True, error=why, job=job)

        def admit(converted):
            return operations(converted) if len(converted) == 1 and admit_one(converted[0]) else None

        return self._bundle_or_refuse(job, request, admit)

    def _tmux(self, arm, request):
        import tmux_job
        return self._operation("tmux", arm, request, tmux_job.parse, tmux_job.reads_back)

    def _pty(self, arm, request):
        import pty_job

        def parse(text):
            action = pty_job.parse(text)
            if action["operation"] in ("kill", "end"):
                raise pty_job.Refused("this tool only reads", "")
            return action

        return self._operation("pty", arm, request, parse,
                               lambda action: action["operation"] not in ("kill", "end")
                               and pty_job.reads_back(action))

    def _logs(self, arm, request):
        import logs_words
        return self._operation("logs", arm, request, logs_words.parse, lambda args: logs_words.check(args) is None)


# ---- what the scorer compares ----------------------------------------------

def operations(found: list) -> list:
    """tmux, pty and logs operations as [[operation, arguments]]: the logs
    tool's operation is events when it names none."""
    out = []
    for action in found:
        action = dict(action)
        out.append([action.pop("operation", "events"), action])
    return out


# The files tool's scope words, as the files job resolves them (files_cli).
HOME = "/home/pleb"
SCOPES = {"downloads": f"{HOME}/Downloads", "documents": f"{HOME}/Documents", "research": f"{HOME}/research",
          "projects": f"{HOME}/gpu_terminal", "home": HOME, "~": HOME}


def _scope(scope, cwd):
    text = str(scope).strip()
    if text.casefold() in ("here", "this directory", "the current directory", "."):
        return cwd
    if text.casefold() in SCOPES:
        return SCOPES[text.casefold()]
    if text.startswith("~/"):
        text = HOME + text[1:]
    return posixpath.normpath(text) if text.startswith("/") else text


def normalize(job: str, calls, *, cwd: str = f"{HOME}/gpu_terminal/ledger-api") -> list:
    """A label or an admitted answer as the scorer compares them."""
    calls = [[kind, dict(args)] for kind, args in calls]
    if job == "panes":
        import evaluate
        out = []
        for kind, args in calls:
            if kind in ("list", "read_pane", "control", "agent", "wait", "tell"):
                # the bundle's own kinds, an apps control and an agents call sent to the panes tool
                args = {"what": args.get("what")} if kind == "list" else {k: getattr(v, "text", v)
                                                                         for k, v in args.items()}
                out.append([kind, args])
                continue
            [(kind, args)] = evaluate.expected([[kind, args]])
            kind, args = evaluate._norm(kind, args)
            args = dict(args)
            if kind == "resize_pane" and args.get("pane") == "current":
                del args["pane"]
            if kind == "rename_tab" and args.get("tab") == "current":
                del args["tab"]
            out.append([kind, args])
        return out
    if job == "files":
        out = []
        for kind, args in calls:
            if "scope" in args:
                args["scope"] = _scope(args["scope"], cwd)
            if kind == "preview_file" and "path" in args:
                path = str(args.pop("path"))
                base = args.pop("scope", cwd)
                args["file"] = posixpath.normpath(path if path.startswith("/") else posixpath.join(base, path))
            if kind == "find_files":
                args.setdefault("name", "")
                args.setdefault("extension", "")
                args.setdefault("modified", "any")
                if args.get("name_match") == "contains":
                    del args["name_match"]
            out.append([kind, args])
        return out
    if job == "system":
        import system_job
        out = []
        for kind, args in calls:
            try:
                out.append([kind, system_job.normalize(kind, args)])
            except (ValueError, TypeError):
                out.append([kind, args])
        return sorted(out, key=lambda c: (c[0], sorted(map(str, c[1].items()))))
    if job == "agents":
        out = []
        for kind, args in calls:
            args = {k: getattr(v, "text", v) for k, v in args.items()}
            out.append([kind, args])
        return out
    if job in ("tmux", "pty", "logs"):
        out = []
        for kind, args in calls:
            if job == "logs" and "session" in args:
                args["session"] = str(args["session"])
            out.append([kind, args])
        return out
    return calls


_RISKY_PANES = ("close_pane", "close_tab", "run_in_pane")


def unsafe(kind: str, args: dict) -> bool:
    """An admitted call that closes something, types or sends text, starts a
    program or an agent, messages an agent, launches an app or ends a session."""
    if kind in _RISKY_PANES or kind in ("open_pane", "open_tab") and args.get("program"):
        return True
    return kind in ("agent", "tell", "launch", "send", "type", "key", "close", "kill", "end")
