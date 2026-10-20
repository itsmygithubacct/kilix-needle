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
    def load(cls, directory: Path = MODEL_DIR) -> "Model":
        """Read the artifact and check it against the digest its manifest records."""
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


def _claims_apps(text):
    import apps
    calls = apps.propose(text)
    results = apps.interpret(text, calls) if calls else []
    return bool(results) and all(isinstance(r, apps.Action) for r in results)


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
    return desktop_grammar.parse(text) is not None


CLAIMS = {"panes": _claims_panes, "apps": _claims_apps, "agents": _claims_agents,
          "files": _claims_files, "system": _claims_system, "pty": _claims_pty,
          "tmux": _claims_tmux, "desktop": _claims_desktop}


# The apps checks try every candidate call (about 50 ms); the others take
# under 2 ms. Apps is asked only when the classifier gives it this much.
APPS_CLAIM_FLOOR = 0.01


def claims(text: str, ranking: list | None = None) -> list:
    """The jobs whose own grammar reads the whole request, in LABELS order."""
    found = []
    p = dict(ranking or ())
    for job, claim in CLAIMS.items():
        if job == "apps" and ranking is not None and p.get("apps", 0.0) < APPS_CLAIM_FLOOR:
            continue
        try:
            if claim(text):
                found.append(job)
        except Exception:       # noqa: BLE001 - a grammar failure is no claim
            pass
    return found


@dataclass
class Route:
    """stage 0 = one grammar claims it; 1 = the classifier is sure; 2 = unsure:
    the candidates' plans decide (ask.py); "error" = the router failed."""
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
    """Stage 0 or 1 when sure; otherwise stage 2 with the candidate jobs."""
    try:
        ranking = (model_ or model()).rank(text)
        threshold = (model_ or model()).threshold
    except RouterError as error:
        found = claims(text)
        if len(found) == 1:
            return Route(0, found[0], claims=found)
        return Route("error", None, claims=found, candidates=found, note=str(error))
    found = claims(text, ranking)
    (top, p), (_second, q) = ranking[0], ranking[1]
    alternatives = [(job, prob) for job, prob in ranking[1:3]]
    common = dict(p=p, margin=p - q, alternatives=alternatives, claims=found)
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
