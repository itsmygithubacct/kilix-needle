"""Shared command understanding for grammar and optional model providers.

Providers only propose calls. All proposals pass the same whole-request
admission before the caller resolves or executes host actions. Importing this
module neither loads a model nor contacts a host.
"""
from dataclasses import dataclass
import time

import commands_contract as contract


class ProposalError(RuntimeError):
    """A provider failed to produce a complete, usable response."""


@dataclass(frozen=True)
class Decision:
    source: str
    calls: list
    admission: contract.Admission
    seconds: float
    model_seconds: float | None = None


def decide(request, *, provider=None, backend="grammar", grammar_first=True, cwd=None):
    """Read one complete request, with an explicitly supplied optional provider.

    ``provider`` is a lazy callable returning an object with ``decide(text)``.
    The returned object belongs to the caller. Its response must contain
    ``calls``; explicit runtime errors are never converted into abstention.
    ``grammar_first=False`` is useful for measuring model proposals through
    precisely the same admission path.
    """
    started = time.monotonic()
    if not isinstance(request, str) or not request.strip() or len(request) > 2048:
        raise ValueError("a command must contain 1 to 2048 characters")
    if backend not in ("grammar", "needle3", "lfm2"):
        raise ValueError("unknown command backend")
    exact = contract.exact_calls(request)
    if backend == "grammar" or (grammar_first and exact):
        calls = exact or []
        return Decision("grammar", calls, contract.admit(request, calls, cwd=cwd),
                        time.monotonic() - started)
    if provider is None:
        raise ProposalError("the selected command model is unavailable")
    runtime = provider()
    model_started = time.monotonic()
    reply = runtime.decide(request)
    elapsed = time.monotonic() - model_started
    if (not isinstance(reply, dict) or "calls" not in reply or reply.get("error")
            or reply.get("type") == "error" or reply.get("success") is False
            or not isinstance(reply["calls"], list)):
        raise ProposalError("the command model returned an incomplete or error-marked response")
    calls = reply["calls"]
    admission = contract.admit(request, calls, cwd=cwd)
    return Decision(backend, calls, admission, time.monotonic() - started, elapsed)
