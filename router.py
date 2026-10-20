"""Which kilix-needle job a request belongs to.

The router reads only the request text: no pane, file or process state goes
into it, and it never runs anything. Two stages live here:

- Stage 0, exact claims: each deterministic grammar is asked whether it reads
  the whole request (claims). Exactly one claim routes there, unless the
  classifier below is sure of a different job; then the two are unsure.
- Stage 1, the owned linear classifier: word 1-2-grams and within-word
  character 3-5-grams, log-TF x IDF, L2-normalised, scored by a multinomial
  logistic regression trained by router_train.py. The top job routes when its
  probability clears the frozen threshold.

Anything else is unsure. The caller (ask.py) then asks the candidate jobs'
plans (stage 2) or the person. A router failure is unsure, never a guess.

The artifact is router-model/router.json, checked in with its training-data
manifest (router-model/manifest.json) and reproduced by router_train.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re

# The jobs' grammars reach about 500 distinct patterns through re's module
# cache in a run of routes, and re keeps 512: each route evicted patterns the
# next one needed and compiled them again (route p95 6.1 ms against 4.4 ms with
# room for them). This only raises the ceiling.
re._MAXCACHE = max(getattr(re, "_MAXCACHE", 0), 2048)

LABELS = ("panes", "apps", "agents", "files", "system", "logs", "tmux", "pty", "desktop", "none")
# Jobs `ask` runs. The others are only named, with their explicit command and
# tools: tmux needs a socket, logs takes structured arguments, and the pty and
# desktop MCP tools are opt-in, so a routed request must not reach them.
RUNS = ("panes", "apps", "agents", "files", "system")
NAMED = {
    "logs": ("kilix-needle logs", "kilix_logs_read",
             "read or search one recorded pane session or agent session log"),
    "tmux": ("kilix-needle tmux --socket /abs/socket", "kilix_tmux_plan / kilix_tmux_act",
             "tmux sessions on an explicit private socket"),
    "pty": ("kilix-needle pty", "kilix_pty_read / kilix_pty_plan / kilix_pty_act (kilix-needle mcp --tools pty)",
            "persistent pane sessions by exact ID"),
    "desktop": ("kilix-needle desktop", "kilix_desktop_read / plan / act (kilix-needle mcp --tools desktop)",
                "X11 windows, pointer, keyboard and scrolling outside Kilix"),
}
MODEL_DIR = Path(__file__).resolve().parent / "router-model"
MAX_CHARS = 4000
_WORD = re.compile(r"[a-z0-9]+|[^\sa-z0-9]")


def norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def features(text: str) -> dict:
    """Counts of w:WORD, b:WORD WORD and c:3-5 letters of a space-padded word."""
    words = _WORD.findall(norm(text)[:MAX_CHARS])
    feats: dict = {}
    for i, word in enumerate(words):
        feats["w:" + word] = feats.get("w:" + word, 0) + 1
        if i:
            key = "b:" + words[i - 1] + " " + word
            feats[key] = feats.get(key, 0) + 1
    for word in words:
        padded = f" {word} "
        for n in (3, 4, 5):
            for j in range(max(1, len(padded) - n + 1)):
                key = "c:" + padded[j:j + n]
                feats[key] = feats.get(key, 0) + 1
    return feats


class RouterError(Exception):
    """The artifact is missing, altered or malformed."""


class Model:
    """The checked-in artifact: labels, IDF, per-feature weights, bias, threshold."""

    def __init__(self, data: dict):
        try:
            self.labels = tuple(data["labels"])
            self.idf = data["idf"]
            self.weights = data["weights"]
            self.bias = list(data["bias"])
            self.threshold = float(data["threshold"])
        except (KeyError, TypeError, ValueError) as error:
            raise RouterError(f"malformed router artifact: {error}") from None
        if self.labels != LABELS or len(self.bias) != len(LABELS) or not 0 < self.threshold < 1:
            raise RouterError("router artifact labels, bias or threshold do not match this code")

    @classmethod
    def load(cls, directory: Path | None = None) -> "Model":
        """Read the artifact and check it against the digest its manifest records."""
        directory = MODEL_DIR if directory is None else directory
        try:
            raw = (directory / "router.json").read_bytes()
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RouterError(f"router artifact unavailable: {error}") from None
        if hashlib.sha256(raw).hexdigest() != manifest.get("artifact", {}).get("sha256"):
            raise RouterError("router.json does not match the digest in manifest.json")
        try:
            return cls(json.loads(raw))
        except ValueError as error:
            raise RouterError(f"malformed router artifact: {error}") from None

    def vector(self, text: str) -> dict:
        cols = {}
        for key, count in features(text).items():
            idf = self.idf.get(key)
            if idf is not None:
                cols[key] = (1.0 + math.log(count)) * idf
        length = math.sqrt(sum(v * v for v in cols.values())) or 1.0
        return {key: value / length for key, value in cols.items()}

    def scores(self, text: str) -> list:
        z = list(self.bias)
        for key, value in self.vector(text).items():
            row = self.weights.get(key)
            if row:
                for k, w in enumerate(row):
                    z[k] += value * w
        top = max(z)
        e = [math.exp(v - top) for v in z]
        total = sum(e)
        return [v / total for v in e]

    def rank(self, text: str) -> list:
        """[(label, probability)], most likely first; ties keep label order."""
        p = self.scores(text)
        return sorted(zip(self.labels, p), key=lambda pair: -pair[1])

    def explain(self, text: str, job: str, n: int = 8) -> list:
        """The features that pushed `job` up most, as [(feature, contribution)]."""
        k = self.labels.index(job)
        parts = [(key, value * self.weights[key][k]) for key, value in self.vector(text).items()
                 if key in self.weights]
        return sorted(parts, key=lambda pair: -pair[1])[:n]


_model: Model | None = None


def model() -> Model:
    global _model
    if _model is None:
        _model = Model.load()
    return _model


def _claims_panes(text):
    import panes_exact
    return panes_exact.admitted(text) is not None


def _claims_agents(text):
    import agents
    return bool(agents.exact_calls(text))


_apps_names: list | None = None


def _apps_admitted(text):
    """apps.propose's reading and admitted actions, asking only the calls whose
    object the request names: (reading, [(order, call, action)]).

    propose tries all ~130 candidate calls, compiling patterns per request
    (8-50 ms); a call is admitted only for a request that names its object, so
    the others are skipped here. tests/test_router.py checks this against
    propose and interpret on the spent apps requests."""
    import apps
    global _apps_names
    if _apps_names is None:
        _apps_names = [(call, [(name, re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])"))
                               for name in apps._object_names(call)])
                       for call in apps.CANDIDATE_CALLS]
    if len(str(text)) > apps.MAX_REQUEST:
        return None, []
    plain = apps._plain_words(text)
    named = [call for call, names in _apps_names
             if any(name in plain and pattern.search(plain) for name, pattern in names)]
    if not named:
        return None, []
    reading = apps._read(text, widen=True)
    if reading.refusal:
        return reading, []
    admitted = []
    for call in named:
        if reading.way and call["name"] != "settings":
            continue
        action = apps._admit(call["name"], call["arguments"], reading)
        if isinstance(action, apps.Action):
            admitted.append((apps._order(reading, action, call), call, action))
    return reading, sorted(admitted, key=lambda entry: entry[0])


def _apps_calls(text):
    """The calls apps.propose would return, when it supports any."""
    return [call for _order, call, _action in _apps_admitted(text)[1]]


def _claims_apps(text):
    """The apps job's exact controls (audio, music, voice, text size) read it, or
    its launch and settings calls are all admitted and no clause they leave
    unread is another job's: "split right and open solitaire" is not an apps
    request, while "I'm bored, open solitaire" is. A controls status query
    about the system ("how much memory is free") is left to the system job."""
    import app_controls
    import apps
    control = app_controls.parse(text)
    if control is not None:
        return control.family != "system"
    reading, admitted = _apps_admitted(text)
    actions, seen = [], set()
    for _order, _call, action in admitted:
        key = (action.kind, tuple(sorted(action.args.items())))
        if key not in seen:
            seen.add(key)
            actions.append(action)
    if not actions:
        return False
    # interpret's checks between calls: one thing set two ways, or a launch of
    # a game the request makes unavailable, is refused.
    keys = [apps._key(action) for action in actions]
    disabled = {a.args["game"] for a in actions if a.kind == "game" and not a.args["available"]}
    if len(set(keys)) != len(keys) or any(a.kind == "launch" and a.args["app"] in disabled
                                          for a in actions):
        return False
    read = {action.at for action in actions}
    return not any(claim(part.text) for i, part in enumerate(reading.parts)
                   if i not in read for job, claim in CLAIMS.items() if job != "apps")


# Quoted text is one payload ("run 'make && make test' below"); an apostrophe
# inside a word ("I'm", "don't") opens no quote. So is everything after ": "
# when an agent is named before it, which the agents job keeps as the person's
# words ("start codex in ~/x: fix the tests and push").
_PAYLOAD = re.compile(r":\s.*", re.S)
_AGENT_NAMED: re.Pattern | None = None
_QUOTED = re.compile(r"(?<!\w)'[^'\n]*'(?!\w)|\"[^\"\n]*\"|`[^`\n]*`|“[^”\n]*”"
                     r"|(?<!\w)‘[^’\n]*’(?!\w)")
_BREAK = re.compile(r"\s*(?:[.;!?;；。]+(?=\s|$)|\n|\s[‒-―]|[‒-―]\s"
                    r"|\s--?(?=\s))\s*|\s*,\s*(?:(?:and\s+)?then\s+|and\s+)?"
                    r"|\s+(?:and\s+then|then|and)\s+", re.I)


def _agent_named(text: str) -> bool:
    import agents
    global _AGENT_NAMED
    if _AGENT_NAMED is None:
        _AGENT_NAMED = re.compile(rf"(?<![\w-])(?:{agents._AGENT_WORDS})(?![\w-])", re.I)
    return bool(_AGENT_NAMED.search(text))


def clauses(text: str) -> list:
    """The request's clauses: split at sentence ends, semicolons, newlines,
    dashes, commas, "and" and "then", never inside quotes. A list's words
    ("pdf and txt files") come apart too; that matters only when two jobs'
    grammars each read one of the pieces whole."""
    import apps
    text = str(text)
    if len(text) > apps.MAX_REQUEST:
        return []
    masked = _QUOTED.sub(lambda match: "x" * len(match.group(0)), text)
    payload = _PAYLOAD.search(masked)
    if payload and _agent_named(text[:payload.start()]):
        masked = masked[:payload.start() + 1] + "x" * (len(masked) - payload.start() - 1)
    parts, at = [], 0
    for match in _BREAK.finditer(masked):
        parts.append(text[at:match.start()])
        at = match.end()
    parts.append(text[at:])
    return [part.strip() for part in parts if part.strip(" .,;!?;；。")]


def _claims_files(text):
    import files_job
    try:
        return bool(files_job.parse(text))
    except ValueError:
        return False


def _claims_system(text):
    import system_job
    return bool(system_job.parse(text))


def _claims_pty(text):
    import pty_job
    try:
        return bool(pty_job.parse(text))
    except ValueError:
        return False


def _claims_tmux(text):
    import tmux_job
    try:
        return bool(tmux_job.parse(text))
    except ValueError:
        return False


def _claims_desktop(text):
    import desktop_grammar
    try:
        return desktop_grammar.parse(text) is not None
    except ValueError:          # desktop_job.DesktopError: not a desktop instruction
        return False


CLAIMS = {"panes": _claims_panes, "apps": _claims_apps, "agents": _claims_agents,
          "files": _claims_files, "system": _claims_system, "pty": _claims_pty,
          "tmux": _claims_tmux, "desktop": _claims_desktop}


def claims(text: str) -> list:
    """The jobs whose own grammar reads the whole request, in LABELS order.

    A grammar that fails raises: route() then fails closed, rather than letting
    another claim or the classifier decide without it."""
    return [job for job, claim in CLAIMS.items() if claim(text)]


@dataclass
class Route:
    """stage 0 = one grammar claims it; 1 = the classifier is sure; 2 = unsure:
    the candidates' plans decide (ask.py); "multi" = two jobs' work in one
    request; "error" = the router failed."""
    stage: object
    job: str | None
    p: float | None = None
    margin: float | None = None
    alternatives: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    note: str = ""

    def record(self) -> dict:
        out = {"stage": self.stage, "job": self.job}
        if self.p is not None:
            out["p"] = round(self.p, 3)
            out["margin"] = round(self.margin, 3)
        if self.alternatives:
            out["alternatives"] = [[job, round(p, 3)] for job, p in self.alternatives]
        if self.claims:
            out["claims"] = list(self.claims)
        if self.candidates:
            out["candidates"] = list(self.candidates)
        if self.note:
            out["note"] = self.note
        return out


CANDIDATE_FLOOR = 0.1       # a job below this probability is not a stage-2 candidate


def route(text: str, model_: Model | None = None) -> Route:
    """Stage 0 or 1 when sure; otherwise stage 2 with the candidate jobs.

    Any failure, a missing or altered artifact or a grammar that raises, is
    the "error" stage: it names no job, so nothing acts on it."""
    try:
        return _route(text, model_ or model())
    except Exception as error:      # noqa: BLE001 - fail closed
        return Route("error", None, note=f"router failed: {type(error).__name__}: {error}"[:300])


def _clause_reader(part: str, model_: Model, threshold: float) -> str | None:
    """The job ask runs whose grammar reads this clause, as stage 0 would
    route it alone: the classifier's top job when its grammar reads the clause;
    otherwise, when the classifier is unsure, the one such job whose grammar
    does. (Asking the top job's grammar first keeps this to one grammar for most
    clauses.)"""
    top, p = model_.rank(part)[0]
    if top in RUNS and CLAIMS[top](part):
        return top
    if p >= threshold:
        return None
    readers = [job for job in RUNS if job != top and CLAIMS[job](part)]
    return readers[0] if len(readers) == 1 else None


def _route(text: str, model_: Model) -> Route:
    ranking = model_.rank(text)
    threshold = model_.threshold
    found = claims(text)
    (top, p), (_second, q) = ranking[0], ranking[1]
    alternatives = [(job, prob) for job, prob in ranking[1:3]]
    common = dict(p=p, margin=p - q, alternatives=alternatives, claims=found)
    # Two jobs' work in one request (a non-goal): no grammar reads all of it,
    # and clauses are each read by a different job that ask runs. It goes back
    # as two requests, however sure the classifier is of one job: that job
    # would act on its own clause and drop the other.
    # (Sure of no job, or of one ask only names, nothing acts: no need to look.)
    if not found and (top in RUNS or p < threshold):
        parts = clauses(text)
        only = set()
        for part in parts if len(parts) > 1 else ():
            reader = _clause_reader(part, model_, threshold)
            if reader:
                only.add(reader)
                if len(only) > 1:
                    break
        if len(only) > 1:
            return Route("multi", None, candidates=[job for job in LABELS if job in only],
                         note="this asks two jobs; send it as two requests", **common)
    # One grammar's claim routes unless the classifier is sure of another job:
    # a grammar can read more than its job means ("send 'x' to the pane below"
    # is a well-formed tmux sentence), so that disagreement is settled by plans.
    if len(found) == 1 and (top == found[0] or p < threshold):
        return Route(0, found[0], **common)
    if len(found) == 1:
        return Route(2, None, candidates=[found[0], top], **common)
    if p >= threshold and (not found or top in found):
        return Route(1, top, **common)
    if found:
        candidates = found
    else:
        candidates = [job for job, prob in ranking[:3]
                      if job != "none" and (job == top or prob >= CANDIDATE_FLOOR)]
    return Route(2, None, candidates=candidates, **common)
