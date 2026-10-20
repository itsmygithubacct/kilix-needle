"""Deterministic proposals for the bounded desktop language.

Parsing is all-or-nothing and still passes the same grounding validator as
neural proposals. A miss is None, never a shortened or repaired action list.
"""
from __future__ import annotations

from decimal import Decimal
import re

import desktop_job as job


def _clause(clause, literals):
    quoted = [literals[int(m[1])] for m in re.finditer(r"LITERAL(\d+)", clause)]
    plain = re.sub(r"LITERAL\d+", "", clause).lower()
    verb = plain.split()[0]
    args = {}
    if verb in ("type", "enter", "write", "insert"):
        if len(quoted) != 1:
            return None
        return {"name": "keyboard", "arguments": {"action": "type", "text": quoted[0]}}
    if verb in ("press", "hit", "tap", "hold"):
        tail = re.sub(r"^\S+\s+", "", clause)
        tail = re.sub(r"\b(?:the|key|keys|chord|shortcut|together)\b", "", tail, flags=re.I).strip()
        keys = quoted[0] if len(quoted) == 1 else tail
        return {"name": "keyboard", "arguments": {"action": "press", "keys": job.key_chord(keys)}}
    if verb in ("wait", "pause"):
        match = re.fullmatch(r"(?:wait|pause)(?: for)? (\d+(?:\.\d+)?)\s*(milliseconds?|ms|seconds?|s)", plain.strip())
        if not match:
            return None
        ms = Decimal(match[1]) * (1 if match[2].startswith("m") else 1000)
        if ms != int(ms):
            return None
        return {"name": "wait", "arguments": {"ms": int(ms)}}
    if verb in ("scroll", "wheel"):
        directions = re.findall(r"\b(up|down|left|right)\b", plain)
        amounts = re.findall(r"\b\d+\b", plain)
        if len(directions) != 1 or len(amounts) > 1:
            return None
        return {"name": "scroll", "arguments": {"direction": directions[0], "steps": int(amounts[0]) if amounts else 3}}
    pointer = bool(re.search(r"\b(?:mouse|pointer|cursor|click|drag)\b", plain))
    name = "pointer" if pointer else "window"
    kinds = [kind for kind, pattern in job._INTENTS.items()
             if kind.startswith(name + ".") and re.search(pattern, clause, re.I)]
    if name == "pointer" and "pointer.double_click" in kinds:
        kinds = [kind for kind in kinds if kind != "pointer.click"]
    if len(kinds) != 1:
        return None
    args["action"] = kinds[0].split(".")[1]
    if name == "window" and args["action"] != "list":
        ids = re.findall(r"\b(?:window\s+)?id\s+(\d+)\b", plain)
        if len(quoted) == 1 and not ids:
            args["target"] = "title:" + quoted[0]
        elif len(ids) == 1 and not quoted:
            args["target"] = "id:" + ids[0]
        elif not quoted and not ids and re.search(r"\b(active|current|this|foreground)\b", plain):
            args["target"] = "active"
        else:
            return None
    numeric = re.sub(r"\b(?:window\s+)?id\s+\d+", "", plain)
    numeric = re.sub(r"(?<=\d)[x×](?=\d)", " ", numeric)
    numbers = [int(n) for n in re.findall(r"(?<![\w.-])\d+(?![\w.-])", numeric)]
    if args["action"] in ("move", "resize", "drag") or name == "pointer" and numbers:
        if len(numbers) != 2:
            return None
        fields = ("width", "height") if args["action"] == "resize" else ("x", "y")
        args.update(zip(fields, numbers))
    if name == "pointer" and args["action"] != "move":
        args["button"] = "right" if re.search(r"\bright\b", plain) else "middle" if re.search(r"\bmiddle\b", plain) else "left"
    return {"name": name, "arguments": args}


def parse(request):
    # Eligibility failures are terminal in every mode, including hybrid.
    clauses = job.request_clauses(request)
    try:
        calls = [_clause(clause, literals) for clause, literals in clauses]
        if any(call is None for call in calls):
            return None
        return job.validate(request, calls)
    except job.DesktopError:
        return None
