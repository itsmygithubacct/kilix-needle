"""The jobs kilix-needle does. Each has its own eval sets, gate and selected model.

A job is one surface a person or an agent asks through (panes and tabs today).
Each is benched on its own sets, and the model that wins there is that job's
default: selecting a tuned model for one job never changes another's.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    name: str
    description: str
    evals: str        # directory of the job's eval sets, relative to the repository
    dev: str
    test: str
    heldout: str      # the gate: the newest held-out set nobody has consulted


JOBS = {
    "panes": Job("panes", "Kilix panes and tabs", "evals",
                 "evals/dev.jsonl", "evals/test.jsonl", "evals/heldout-v8.jsonl"),
    "apps": Job("apps", "Kilix apps, games and settings", "evals/apps",
                "evals/apps/dev.jsonl", "evals/apps/test.jsonl", "evals/apps/heldout-v3.jsonl"),
}
DEFAULT = "panes"


class UnknownJob(ValueError):
    pass


def get(name: str) -> Job:
    try:
        return JOBS[name]
    except KeyError:
        raise UnknownJob(f"unknown job {name!r}; known: {', '.join(sorted(JOBS))}") from None
