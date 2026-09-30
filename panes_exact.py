"""Canonical pane commands, read without a model.

An agent's exact command ("Close the pane titled bench-target in this tab.",
"run 'make' in pane 70", "go to tab 2") needs no inference: the model once
proposed close_tab for the first, twice (token-cost finding 13, route benchmark
2026-09-29), and in the gpt-6-luna benchmark every failed pane task was a
wording like these that reached the model. These forms are turned into calls
directly. The calls still pass every check (actions.interpret), resolution
against the live tree and the same confirmation as a model's; if the checks
refuse any of them, this route steps aside and the model reads the request as
before.
"""
from __future__ import annotations

import re

from actions import _SHELL_ARGUMENT, Action, interpret, normalize

_HEAD = re.compile(r"^(?:(?:please|pls|kindly|ok|okay|just|can you|could you|would you)[\s,]+)+", re.I)
_TAIL = re.compile(r"(?:[\s,]+(?:please|pls|thanks|thank you|now))*[\s.!]*$", re.I)
_WORD = r"[\w.+@:-]+"
# A pane's name, bare or in quotes/backticks: "the pane titled "bench-target"".
_NAME = rf"(?:\"(?P<{{g}}q>[^\"\n]+)\"|'(?P<{{g}}s>[^'\n]+)'|`(?P<{{g}}b>[^`\n]+)`|(?P<{{g}}w>{_WORD}))"
_HERE = r"(?:\s+in\s+(?:this|the current)\s+tab)?(?:,?\s+not\s+the\s+tab(?:\s+itself)?)?"


def _pane(prefix: str = "") -> str:
    """A pane: "the pane titled X", "the X pane", "pane 70" / "pane:70", "pane X"."""
    titled = _NAME.format(g=f"{prefix}t")
    bare = _NAME.format(g=f"{prefix}n")
    return (rf"(?:(?:the\s+)?pane\s+(?:titled|named|called|labell?ed)\s+{titled}"
            rf"|(?:the\s+)?pane[:\s]\s*(?P<{prefix}id>[0-9]{{1,6}})"
            rf"|(?:the\s+)?pane\s+(?!(?:titled|named|called|labell?ed)\b){bare}"
            rf"|(?:the\s+)?(?P<{prefix}name>{_WORD})\s+pane){_HERE}")


_QUOTED = r"(?:'(?P<sq>[^'\n]+)'|\"(?P<dq>[^\"\n]+)\"|`(?P<bq>[^`\n]+)`)"
# An unquoted command: a program, then only plain shell arguments ("touch /x/y").
_UNQUOTED = r"(?P<uq>[A-Za-z_][\w.+-]*(?:\s+\S+)*?)"
_TYPE = (r"(?:type\s+and\s+press\s+enter\s+on:|"
         r"(?:type|run|enter|execute)(?:\s+and\s+(?:run|press\s+enter))?"
         r"(?:\s+(?:the\s+command|exactly))?:?)")
_ENTER = (r"(?:\s+at\s+(?:the|its)\s+(?:shell\s+)?prompt)?"
          r"(?:,?\s+(?:and\s+)?(?:then\s+)?(?:press|hit)\s+(?:enter|return))?")
_SIDE = r"(?P<side>right|left|below|above|down|up)"
_SHELLS = {"bash", "sh", "zsh", "fish", "dash", "shell", "a shell", "the shell", "a new shell",
           "a terminal", "terminal"}
_FORMS = (
    ("close_pane", re.compile(rf"(?:close|kill)\s+{_pane()}", re.I)),
    ("go_to_pane", re.compile(rf"(?:go\s+to|focus|switch\s+to)\s+{_pane()}", re.I)),
    ("run_in_pane", re.compile(rf"{_TYPE}\s+(?:{_QUOTED}|{_UNQUOTED})\s+(?:in|into)\s+{_pane()}{_ENTER}",
                               re.I)),
    ("run_in_pane", re.compile(rf"in\s+{_pane()},?\s+{_TYPE}\s+(?:{_QUOTED}|{_UNQUOTED}){_ENTER}", re.I)),
    ("run_in_pane", re.compile(rf"(?:type\s+and\s+press\s+enter|type\s+and\s+run):?\s+(?:{_QUOTED}|{_UNQUOTED})"
                               rf"\s+(?:in|into)\s+{_pane()}", re.I)),
    ("open_pane", re.compile(
        rf"(?:split(?:\s+(?:the\s+)?(?:pane|window))?|open\s+(?:a\s+)?(?:new\s+)?(?:shell\s+)?pane)"
        rf"(?:\s+directly)?\s+(?:to\s+the\s+|on\s+the\s+)?{_SIDE}"
        rf"(?:\s+of\s+(?:this|the\s+current|my|the)\s+pane(?:\s+you\s+are\s+running\s+in)?)?"
        rf"(?:(?:,?\s+and\s+(?:run|start)|[.;]\s+it\s+should\s+run|,?\s+running|\s+with)\s+"
        rf"(?P<prog>'[^'\n]+'|`[^`\n]+`|[\w.+-]+(?:\s+[\w.+-]+)?)(?:\s+in\s+(?:it|the\s+new\s+pane))?)?", re.I)),
    ("close_tab", re.compile(r"close\s+tab\s+(?P<tab>[0-9]{1,2})", re.I)),
    ("go_to_tab", re.compile(r"(?:go\s+to|switch\s+to)\s+tab\s+(?P<tab>[0-9]{1,2})", re.I)),
)
_SIDES = {"down": "below", "up": "above"}

# Relative navigation names an order, not a title or a spatial neighbour.
# Match the whole request so a time, condition or second action stays with
# the model. Resolution still counts from the caller and checks ambiguity.
_NAVIGATE = r"(?:(?:go\s+to|switch\s+to|focus|jump\s+to)\s+)?"
_RELATIVE_FORMS = (
    ("previous", re.compile(_NAVIGATE + r"(?:the\s+)?(?P<unit>tab|pane)\s+before\s+this\s+one", re.I)),
    ("next", re.compile(_NAVIGATE + r"(?:the\s+)?(?P<unit>tab|pane)\s+after\s+this\s+one", re.I)),
    ("next", re.compile(_NAVIGATE + r"(?:whichever|the)\s+(?P<unit>tab|pane)\s+comes\s+next", re.I)),
    ("previous", re.compile(_NAVIGATE + r"(?:the\s+)?previous\s+(?P<unit>tab|pane)", re.I)),
    ("next", re.compile(_NAVIGATE + r"(?:the\s+)?next\s+(?P<unit>tab|pane)", re.I)),
)


def _relative_navigation(text: str):
    for target, form in _RELATIVE_FORMS:
        found = form.fullmatch(text)
        if found:
            unit = found["unit"].lower()
            return [{"name": f"go_to_{unit}", "arguments": {unit: target}}], f"go to the {target} {unit}"
    return None


def _first(found: dict, *keys):
    return next((found[k] for k in keys if found.get(k)), None)


def _quote(text: str) -> str:
    for q in ("'", "`", '"'):
        if q not in text:
            return f"{q}{text}{q}"
    return text


def _pane_words(found: dict, pane: str) -> str:
    """How the canonical sentence names the pane the request named."""
    if found.get("id"):
        return f"pane {pane}"
    if found.get("name"):
        return f"the {pane} pane"
    return f"the pane titled {_quote(pane) if ' ' in pane else pane}"


# "Find the pane titled X in this tab and close it": finding is how an agent
# says which pane; the action is the verb that follows, on "it".
_FIND_THEN = re.compile(rf"(?:find|locate|look\s+up|identify|get)\s+(?P<pane>{_pane('f')})"
                        r"(?:\s*,)?\s+(?:and\s+)?(?:then\s+)?(?P<verb>close|kill|focus|go\s+to|switch\s+to)\s+it",
                        re.I)


_EMPTY_COMMAND = r"(?:\s+(?:''|\"\"|``))?"
_MISSING_COMMAND = (
    re.compile(rf"(?:in|into)\s+{_pane()},?\s+{_TYPE}{_EMPTY_COMMAND}{_ENTER}", re.I),
    re.compile(rf"{_TYPE}{_EMPTY_COMMAND}\s+(?:in|into)\s+{_pane()}{_ENTER}", re.I),
)


def missing_command(request: str) -> bool:
    """A complete typing form with its command absent, not a command of 'in' or 'and'."""
    text = _TAIL.sub("", _HEAD.sub("", normalize(request).strip()))
    for _ in range(3):
        text = _RUN_NEUTRAL.sub("", text).strip()
    return any(form.fullmatch(text) for form in _MISSING_COMMAND)


def read(request: str) -> tuple[list, str] | None:
    """The one call an exact pane command states, and the canonical sentence it
    was read as, or None. The fullmatch allows only fixed filler around the
    values, so the canonical sentence says exactly what the request says."""
    if missing_command(request):
        return None
    text = _TAIL.sub("", _HEAD.sub("", normalize(request).strip()))
    found = _FIND_THEN.fullmatch(text)
    if found:
        text = f"{found['verb']} {found['pane']}"
    for kind, form in _FORMS:
        m = form.fullmatch(text)
        if not m:
            continue
        found = m.groupdict()
        if kind in ("close_tab", "go_to_tab"):
            verb = "close" if kind == "close_tab" else "go to"
            return [{"name": kind, "arguments": {"tab": found["tab"]}}], f"{verb} tab {found['tab']}"
        if kind == "open_pane":
            args = {"side": _SIDES.get(found["side"].lower(), found["side"].lower())}
            program = (found.get("prog") or "").strip("'`").strip()
            if program and program.lower() not in _SHELLS:
                args["program"] = program
            shown = program if re.fullmatch(r"[\w.+-]+", program) else _quote(program)
            canonical = f"split {args['side']}" + (f" and run {shown}" if "program" in args else "")
            return [{"name": kind, "arguments": args}], canonical
        pane = _first(found, "tq", "ts", "tb", "tw", "id", "nq", "ns", "nb", "nw", "name")
        args = {"pane": pane}
        if kind == "run_in_pane":
            command = _first(found, "sq", "dq", "bq")
            if command is None:
                command = found.get("uq") or ""
                words = command.split()
                # Unquoted, only a program and plain shell arguments: English after
                # a program may be a condition or a delay that would be typed too.
                if not words or not all(_SHELL_ARGUMENT.fullmatch(w) for w in words[1:]):
                    return _run_slots(text)     # it reads the command's end by its parts
            args["command"] = command
            return [{"name": kind, "arguments": args}], f"run {_quote(command)} in {_pane_words(found, pane)}"
        verb = "close" if kind == "close_pane" else "go to"
        return [{"name": kind, "arguments": args}], f"{verb} {_pane_words(found, pane)}"
    return _relative_navigation(text) or _open_slots(text) or _run_slots(text)


# A plain shell pane opened to one side of the calling pane, however an agent
# words it ("open a new shell pane directly to the right of the pane I am
# running in", "split the pane I am running in to the right and open a shell in
# the new pane"): one side word, an opening verb, and only words that name a
# pane, a shell or the calling pane. A program, another pane's name, a number
# or any other word leaves the request to the model.
_OPEN_VERB = re.compile(r"\b(?:open|split|create|add|make|start|spawn)\b")
_OPEN_SIDE = {"right": "right", "left": "left", "below": "below", "above": "above",
              "down": "below", "up": "above", "under": "below", "beneath": "below"}
_OPEN_WORDS = frozenset("""
open opens opening split splits splitting create add make start spawn new a an another one fresh
empty plain blank interactive default shell terminal pane panes split window it
directly immediately just right to on of at side hand the this current my own that which where
i i'm am you you're are we running working sitting in from and then with inside next please now
""".split())


_OPEN_SHELL_SENTENCE = re.compile(
    r"[.;,]?\s*(?:and\s+)?(?:it\s+should\s+(?:run|start|open|have)|(?:then\s+)?(?:run|start|open)|with)\s+"
    r"(?:a\s+|an\s+)?(?:new\s+|plain\s+|interactive\s+)?(?:shell|terminal|bash)"
    r"(?:\s+(?:there|(?:in|inside)\s+(?:it|the\s+new\s+pane|that\s+pane|the\s+pane)))?\s*[.!]?\s*$", re.I)
_OPEN_STOP = re.compile(r"[.;,]?\s*(?:and\s+)?then\s+stop\s*[.!]?\s*$", re.I)


def _open_slots(text: str):
    # "... to the right of the pane I am running in. It should run a shell. Then stop."
    for _ in range(3):
        text = _OPEN_STOP.sub("", _OPEN_SHELL_SENTENCE.sub("", text)).strip()
    low = text.lower()
    if re.search(r"[0-9\"'`:]|\b(?:not|no|don'?t|never|tab|tabs|run|running\s+(?!in\b)|"
                 r"with\s+(?!a\b|an\b)|titled|named|called)\b", low):
        return None
    if not _OPEN_VERB.search(low) or not re.search(r"\b(?:pane|shell|terminal|split)\b", low):
        return None
    # "open up a new pane": "up" after the verb is a particle, not a side
    low = re.sub(r"\b(open|start|spin|split|make)\s+up\b", r"\1", low)
    words = re.findall(r"[a-z][a-z']*", low)
    sides = {_OPEN_SIDE[w] for w in words if w in _OPEN_SIDE}
    if len(sides) != 1:
        return None
    if any(w not in _OPEN_WORDS and w not in _OPEN_SIDE for w in words):
        return None
    side = sides.pop()
    return [{"name": "open_pane", "arguments": {"side": side}}], f"split {side}"


# Typing a command into a titled pane, however an agent words it ("In the pane
# titled X in this tab, type exactly: CMD then press Enter. Then stop.", "Type
# `CMD` and press Enter in the pane titled `X`"): exactly one titled or
# numbered pane, one command (the only quoted text, or a program and plain
# shell arguments after the typing verb), and only typing, Enter and filler
# words around them. Anything else leaves the request to the model.
_RUN_PANE = re.compile(r"(?:the\s+)?pane\s+(?:(?:titled|named|called|labell?ed)\s+"
                       r"(?:\"(?P<q>[^\"\n]+)\"|'(?P<s>[^'\n]+)'|`(?P<b>[^`\n]+)`|(?P<w>[\w.+@-]+(?::[\w.+@-]+)*))"
                       r"|(?P<id>[0-9]{1,6})\b"
                       # "in pane bench-target": a bare name, never a word of the sentence
                       r"|(?P<n>(?!(?:titled|named|called|labell?ed|the|this|that|and|or|i|you|it|to|in|into|on|"
                       r"with|which|where|below|above|left|right|here|there|now|please|then|of|at|is)\b)"
                       r"[\w.+@-]+(?::[\w.+@-]+)*))", re.I)
_RUN_QUOTED = re.compile(r"`([^`\n]+)`|'([^'\n]+)'|\"([^\"\n]+)\"")
_RUN_VERB = re.compile(r"\b(?:type|run|enter|execute)(?:\s+and\s+(?:run|execute|press\s+enter))?"
                       r"(?:\s+(?:the\s+|this\s+)?command)?(?:\s+exactly)?\s*:?\s+", re.I)
_RUN_END = re.compile(r"\s+\(?(?:and\s+(?:then\s+)?(?:press|hit)|then\s+(?:press|hit)|in(?:to)?\s+(?:the\s+)?"
                      r"(?:shell\s+prompt|@PANE@)|@PANE@)(?=\W|$)|[.;]\s|[.;]?\s*$", re.I)
_RUN_NEUTRAL = re.compile(r"[.;,]?\s*(?:then\s+stop|do\s+not\s+do\s+anything\s+else|nothing\s+else|"
                          r"that'?s\s+all)\s*[.!]?\s*$", re.I)
_RUN_WORDS = frozenset("""
in into the this tab type run enter execute and press hit return key then at shell prompt of its
exactly command please now it following
""".split())


def _run_slots(text: str):
    for _ in range(3):
        text = _RUN_NEUTRAL.sub("", text).strip()
    panes = list(_RUN_PANE.finditer(text))
    if len(panes) != 1:
        return None
    found = panes[0].groupdict()
    pane = _first(found, "q", "s", "b", "w", "id", "n")
    rest = text[:panes[0].start()] + " @PANE@ " + text[panes[0].end():]
    quoted = list(_RUN_QUOTED.finditer(rest))
    if len(quoted) > 1:
        return None
    colon = re.search(r":\s+(?=[A-Za-z_/])", rest) if not quoted else None
    if colon and "@PANE@" in rest[:colon.start()] and re.search(
            r"\b(?:type|run|enter|execute)\b", rest[:colon.start()], re.I):
        # "Type this command in the pane titled X and press Enter: CMD"; the
        # command ends where any other does ("then press Enter", the end)
        end = _RUN_END.search(rest, colon.end())
        command = rest[colon.end():end.start()].strip()
        words = command.split()
        if not words or not re.fullmatch(r"[A-Za-z_/][\w./+-]*", words[0]) \
                or not all(_SHELL_ARGUMENT.fullmatch(w) for w in words[1:]):
            return None
        rest = rest[:colon.start()] + " @CMD@ " + rest[end.start():]
    elif quoted:
        command = next(g for g in quoted[0].groups() if g)
        rest = rest[:quoted[0].start()] + " @CMD@ " + rest[quoted[0].end():]
    else:
        verb = _RUN_VERB.search(rest)
        if not verb:
            return None
        end = _RUN_END.search(rest, verb.end())
        command = rest[verb.end():end.start()].strip() if end else ""
        words = command.split()
        if not words or "@PANE@" in command or not re.fullmatch(r"[A-Za-z_/][\w./+-]*", words[0]) \
                or not all(_SHELL_ARGUMENT.fullmatch(w) for w in words[1:]):
            return None
        rest = rest[:verb.end()] + " @CMD@ " + rest[end.start():]
    if not _RUN_VERB.search(rest.replace("@CMD@", "x")) and not re.search(r"\b(?:type|run|enter|execute)\b", rest, re.I):
        return None
    left = [w for w in re.findall(r"[A-Za-z@][\w@']*", rest) if w not in ("@PANE@", "@CMD@")]
    if any(w.lower() not in _RUN_WORDS for w in left):
        return None
    words_found = {"id": pane} if found.get("id") else {"t": pane}
    args = {"pane": pane, "command": command}
    return [{"name": "run_in_pane", "arguments": args}], \
        f"run {_quote(command)} in {_pane_words(words_found, pane)}"


def calls(request: str) -> list | None:
    got = read(request)
    return got[0] if got else None


def admitted(request: str) -> tuple[list, str] | None:
    """Those calls and the canonical sentence, only when the checks admit every
    call for that sentence."""
    got = read(request)
    if not got:
        return None
    found, canonical = got
    results = interpret(canonical, found)
    if not results or not all(isinstance(r, Action) for r in results):
        return None
    return found, canonical
