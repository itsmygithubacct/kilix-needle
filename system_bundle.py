"""Familiar read-only system questions, read by a bundle of small nets, without the model.

The answer to a system request has a grammar: up to three read calls, each
one of five tools (system_job.TOOLS) and that tool's arguments. Every state of
that grammar has a small net of its own, picked by the state, never by a score:

    seg                    for every joining word (and, comma, then, also, plus):
                           does it separate two clauses?
    tool                   for a clause: which read tool, or none
    TOOL.ARG               for a clause of that tool: the argument's value from
                           its closed list, or whether it is given at all
    TOOL.ARG@              for a clause whose argument is copied text: which
                           word is the unit, program tag or package

What is systematic is code, not learned: numbers and number words, absolute
paths, and journal times ("since 2 hours ago", "in the last hour", "yesterday",
"2026-09-01"). Copied names keep their case.

The bundle answers only on ground it was trained on, and otherwise abstains
(returns None) so the request goes on to the model, whose reply stays a
proposal. It abstains when
- a word it never saw is not the copied name of its clause;
- two neighbouring words of a clause, with the copied name and numbers
  replaced by their marks, never stood side by side in a training clause of
  the same tool (the frame), or a clause is longer than any it was trained on;
- a joining word follows or precedes a word it never stood beside in
  training, or there are more clauses than any training request had;
- a word that, in training, only ever came with one value of an argument (or
  one tool) is in a clause read with another value (or tool), or a clause holds
  words for two values of the same argument;
- code cannot read the number, path or time the nets say is there, or finds
  two of them;
- its members (the same nets trained on different samples of the rows, in a
  different order) do not all give the same calls.

A read it gives is then checked like any proposal (system_normalize: bounds,
preflight, schema, grounding); only a read that passes every check is
collected. The artifact is system-bundle/bundle.json, checked in with its
manifest and reproduced by system_bundle_train.py from system-bundle/
training.jsonl; a file whose digest does not match the manifest is refused.
Weights are hexadecimal text (half floats), so the artifact is plain ASCII.
This module needs only the standard library.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import struct

DIRECTORY = Path(__file__).resolve().parent / "system-bundle"
FILE = "bundle.json"
MANIFEST = "manifest.json"
SCHEMA = "kilix-needle.system-bundle/v1"
ENV = "KILIX_NEEDLE_SYSTEM_BUNDLE"          # "1" turns the bundle on (off by default)

NONE = "<none>"
MARKS = frozenset({"<num>", "<path>", "<date>", "<unk>"})     # kinds of word, never a value or tool word
JOINERS = ("and", ",", "then", "also", "plus")
# Per tool: closed arguments (values come from the label, defaults included),
# presence arguments read by code, and copied arguments.
CLOSED = {
    "resources": {"kind": ("memory", "cpu", "disk", "all")},
    "processes": {"sort": ("memory", "cpu")},
    "services": {"state": ("failed", "all"), "scope": ("system", "user")},
    "journal": {"boot": ("current", "previous", "any"), "priority": ("all", "err", "warning"),
                "scope": ("system", "user")},
    "packages": {"operation": ("status", "owner")},
}
PRESENCE = {                                  # absent (default) or read by code / copied
    "resources": ("path",), "processes": ("limit",),
    "services": ("unit",), "journal": ("unit", "identifier", "since", "limit"), "packages": (),
}
COPIED = {"services": ("unit",), "journal": ("unit", "identifier"), "packages": ("target",)}
DEFAULT_LIMIT = {"processes": 10, "journal": 50}
TOOLS = tuple(CLOSED)

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
                 "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100}
_TIME_UNIT = {"min": "minute", "mins": "minute", "minute": "minute", "minutes": "minute",
              "hr": "hour", "hrs": "hour", "hour": "hour", "hours": "hour",
              "day": "day", "days": "day"}
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TOKEN = re.compile(r",|[^\s,]+")


class BundleError(RuntimeError):
    pass


# ---------------------------------------------------------------- words

def tokenize(text: str) -> list[str]:
    """Words as written (case kept); a comma is a word of its own."""
    out = []
    for match in _TOKEN.finditer(text):
        raw = match.group(0)
        if raw == ",":
            out.append(",")
            continue
        core = raw.strip("\"'`()[]").rstrip(".!?:;")
        if core.endswith("'s") and len(core) > 2 and not core.lower() in ("what's", "that's", "it's", "who's"):
            core = core[:-2]
        if core:
            out.append(core)
    return out


def number(word: str) -> int | None:
    low = word.lower()
    if re.fullmatch(r"[0-9]{1,4}", low):
        return int(low)
    return _NUMBER_WORDS.get(low)


def form(word: str, vocab) -> str:
    """The word as the nets see it: itself when known, else a mark for its kind."""
    low = word.lower()
    if low in JOINERS:
        return low
    if number(low) is not None:
        return "<num>"
    if low.startswith("/"):
        return "<path>"
    if _DATE.fullmatch(low):
        return "<date>"
    return low if low in vocab else "<unk>"


def shape(word: str) -> str:
    return "".join(flag for flag, test in (
        ("d", any(c.isdigit() for c in word)), ("p", "." in word), ("a", "@" in word),
        ("h", "-" in word), ("s", word.lower().endswith(".service")), ("/", word.startswith("/")),
        ("u", any(c.isupper() for c in word))) if test) or "w"


def clause_features(forms: list[str], index: int, count: int, previous: str) -> list[str]:
    padded = ["<s>"] + forms + ["</s>"]
    out = {"bias", f"ctx={min(index, 2)}/{min(count, 3)}", f"prev={previous}",
           "first=" + padded[1], "last=" + padded[-2]}
    out.update("w=" + w for w in forms)
    out.update(f"b={a}_{b}" for a, b in zip(padded, padded[1:]))
    return sorted(out)


def token_features(words: list[str], forms: list[str], i: int) -> list[str]:
    padded = ["<s>", "<s>"] + forms + ["</s>", "</s>"]
    j = i + 2
    return sorted({"bias", "w=" + forms[i], "shape=" + shape(words[i]),
                   "p1=" + padded[j - 1], "n1=" + padded[j + 1], "p2=" + padded[j - 2],
                   "n2=" + padded[j + 2], f"p1n1={padded[j - 1]}_{padded[j + 1]}",
                   f"wn1={forms[i]}_{padded[j + 1]}", f"p1w={padded[j - 1]}_{forms[i]}"})


def joiner_features(forms: list[str], j: int) -> list[str]:
    padded = ["<s>", "<s>"] + forms + ["</s>", "</s>"]
    k = j + 2
    return sorted({"bias", "j=" + forms[j], "l1=" + padded[k - 1], "r1=" + padded[k + 1],
                   "l2=" + padded[k - 2], "r2=" + padded[k + 2],
                   f"l1r1={padded[k - 1]}_{padded[k + 1]}", f"jr1={forms[j]}_{padded[k + 1]}",
                   f"l1j={padded[k - 1]}_{forms[j]}"})


def joiner_positions(forms: list[str]) -> list[int]:
    """Joining words that may split ("and then" counts once, at "and")."""
    out = []
    for i, w in enumerate(forms):
        if w in JOINERS and i > 0 and i < len(forms) - 1 and (not out or out[-1] != i - 1):
            if forms[i - 1] not in JOINERS:
                out.append(i)
    return out


def join_pairs(forms: list[str], j: int) -> tuple[str, str]:
    """The word before a joining word with it, and it with the word after."""
    return f"{forms[j - 1]}|{forms[j]}", f"{forms[j]}|{forms[j + 1]}"


def split(words: list[str], forms: list[str], cuts: list[int]) -> list[tuple[int, int]]:
    """Clauses as (start, end) word ranges; the joining words at a cut belong to none."""
    spans, start = [], 0
    for cut in cuts:
        end = cut
        spans.append((start, end))
        start = cut + 1
        while start < len(forms) and forms[start] in JOINERS:
            start += 1
    spans.append((start, len(forms)))
    return spans


# ---------------------------------------------------------------- code readers

def read_since(words: list[str]) -> tuple[str | None, set]:
    """The journal time a clause states, and the word positions it used, or (None, set())."""
    low = [w.lower() for w in words]
    found = []
    for i, w in enumerate(low):
        if w in ("today", "yesterday"):
            found.append((w, {i} | ({i - 1} if i and low[i - 1] == "since" else set())))
        elif _DATE.fullmatch(w):
            used = {i}
            stamp = w
            if i + 1 < len(low) and re.fullmatch(r"[0-9]{2}:[0-9]{2}(?::[0-9]{2})?", low[i + 1]):
                stamp += " " + low[i + 1]
                used.add(i + 1)
                if i + 2 < len(low) and low[i + 2] == "utc":
                    stamp += " UTC"
                    used.add(i + 2)
            found.append((stamp, used | ({i - 1} if i and low[i - 1] == "since" else set())))
        elif w in _TIME_UNIT:
            unit = _TIME_UNIT[w]
            n = number(low[i - 1]) if i else None
            if n is not None and i + 1 < len(low) and low[i + 1] == "ago":
                text = f"{n} {w if w in ('minute', 'minutes', 'hour', 'hours', 'day', 'days') else unit + ('s' if n != 1 else '')} ago"
                used = {i - 1, i, i + 1} | ({i - 2} if i > 1 and low[i - 2] == "since" else set())
                found.append((text, used))
                continue
            # (in) the last|past [N|a|an] unit
            k = i - 1
            if n is not None:
                k = i - 2
            elif k >= 0 and low[k] in ("a", "an", "one"):
                n, k = 1, k - 1
            elif k >= 0 and low[k] in ("last", "past"):
                n = 1
            if n is None or k < 0 or low[k] not in ("last", "past"):
                continue
            if n < 1 or n > 999:
                continue
            used = set(range(k, i + 1))
            if k > 0 and low[k - 1] == "the":
                used.add(k - 1)
                if k > 1 and low[k - 2] in ("in", "from", "over", "during", "for", "since"):
                    used.add(k - 2)
            elif k > 0 and low[k - 1] in ("in", "from", "over", "during", "for"):
                used.add(k - 1)
            found.append((f"{n} {unit}{'s' if n != 1 else ''} ago", used))
    if len(found) != 1:
        return None, set()
    return found[0]


def read_number(words: list[str], skip: set) -> int | None:
    values = [number(w) for i, w in enumerate(words) if i not in skip and number(w) is not None]
    return values[0] if len(values) == 1 else None


def read_path(words: list[str]) -> str | None:
    paths = [w for w in words if w.startswith("/")]
    return paths[0] if len(paths) == 1 else None


def copied_value(word: str) -> str:
    return word


MARGIN = 0.5


class _Abstain(Exception):
    pass


def read_clause(tool, words, forms, closed, present, pick):
    """One clause's arguments and the words they used: (args, marks) or (None, why).

    Shared by training, where closed/present/pick give the label's answers, and
    reading, where the nets give them, so both mark the same words. Code reads
    times and paths first, then a copied name is picked among the words left,
    then the limit is the one number left."""
    args, marks = {}, {}
    for arg in CLOSED[tool]:
        args[arg] = closed(arg)
    since_used = set()
    if "since" in PRESENCE[tool] and present("since"):
        value, since_used = read_since(words)
        if value is None:
            return None, "a journal time code cannot read"
        args["since"] = value
        marks.update((i, "<since>") for i in since_used)
    if "path" in PRESENCE[tool] and present("path"):
        value = read_path(words)
        if value is None:
            return None, "no single path"
        args["path"] = value
        marks[words.index(value)] = "<path>"
    for arg in COPIED.get(tool, ()):
        if arg != "target" and not present(arg):
            continue
        candidates = [i for i in range(len(words)) if i not in marks
                      and forms[i] not in JOINERS and forms[i] not in ("<num>", "<date>")]
        i = pick(arg, candidates) if candidates else None
        if i is None:
            return None, f"no single word for the {arg}"
        args[arg] = copied_value(words[i])
        marks[i] = f"<{arg}>"
    if "limit" in PRESENCE[tool] and present("limit"):
        value = read_number(words, set(marks))
        if value is None:
            return None, "no single number for the limit"
        args["limit"] = value
        for i, w in enumerate(words):
            if i not in marks and number(w) == value:
                marks[i] = "<num>"
    return args, marks


def frame_pairs(forms, marks) -> list[str]:
    frame = ["<s>"] + [marks.get(i, f) for i, f in enumerate(forms)] + ["</s>"]
    return [f"{a}|{b}" for a, b in zip(frame, frame[1:])]


# ---------------------------------------------------------------- nets

class Net:
    """Rows of half-float weights per feature, decoded only when touched."""

    def __init__(self, classes, features, hexes):
        self.classes = list(classes)
        self.width = max(1, len(self.classes))
        self.index = {f: i for i, f in enumerate(features)}
        self.hexes = hexes                     # one hex string per member
        self._fmt = f"<{self.width}e"
        self._stride = self.width * 4

    def scores(self, member: int, features) -> list[float]:
        total = [0.0] * self.width
        text = self.hexes[member]
        for f in features:
            row = self.index.get(f)
            if row is None:
                continue
            start = row * self._stride
            values = struct.unpack(self._fmt, bytes.fromhex(text[start:start + self._stride]))
            for c in range(self.width):
                total[c] += values[c]
        return total

    def best(self, member: int, features) -> str:
        s = self.scores(member, features)
        top = max(range(self.width), key=lambda c: (s[c], -c))
        return self.classes[top]


class Bundle:
    def __init__(self, data: dict):
        if data.get("schema") != SCHEMA:
            raise BundleError("not a system bundle of this version")
        self.vocab = frozenset(data["vocab"])
        self.frames = {tool: frozenset(pairs) for tool, pairs in data["frames"].items()}
        self.joins = frozenset(data["joins"])
        self.value_words = data["value_words"]       # tool -> arg -> word -> value
        self.tool_words = data["tool_words"]         # word -> tool
        self.max_clauses = data["max_clauses"]
        self.max_words = data["max_words"]
        self.members = data["members"]
        self.nets = {name: Net(spec["classes"], spec["features"], [m[name] for m in data["weights"]])
                     for name, spec in data["nets"].items()}

    # One member's whole reading, or (None, why).
    def _read(self, member: int, request: str):
        words = tokenize(request)
        if not words:
            return None, "no words"
        forms = [form(w, self.vocab) for w in words]
        cuts = []
        for j in joiner_positions(forms):
            for pair in join_pairs(forms, j):
                if pair not in self.joins:
                    return None, f"a joining word in a place it never joined: {pair}"
            if self.nets["seg"].best(member, joiner_features(forms, j)) == "cut":
                cuts.append(j)
        spans = split(words, forms, cuts)
        if len(spans) > self.max_clauses:
            return None, "more clauses than any training request had"
        calls, previous = [], NONE
        for index, (start, end) in enumerate(spans):
            cw, cf = words[start:end], forms[start:end]
            if not cw or len(cw) > self.max_words:
                return None, "a clause of a length never trained on"
            call, why = self._clause(member, cw, cf, index, len(spans), previous)
            if call is None:
                return None, why
            calls.append(call)
            previous = call[0]
        return calls, None

    def _clause(self, member, words, forms, index, count, previous):
        features = clause_features(forms, index, count, previous)
        tool = self.nets["tool"].best(member, features)
        if tool == NONE:
            return None, "not a read this bundle knows"
        for w in set(forms) - MARKS:
            other = self.tool_words.get(w)
            if other is not None and other != tool:
                return None, f"'{w}' only ever came with {other}"

        def closed(arg):
            value = self.nets[f"{tool}.{arg}"].best(member, features)
            named = {self.value_words.get(tool, {}).get(arg, {}).get(w) for w in set(forms) - MARKS} - {None}
            if len(named) > 1 or (named and value not in named):
                raise _Abstain(f"words name another {arg} than {value}")
            return value

        def present(arg):
            return self.nets[f"{tool}.{arg}"].best(member, features) != "absent"

        def pick(arg, candidates):
            net = self.nets[f"{tool}.{arg}@"]
            scores = sorted(((net.scores(member, token_features(words, forms, i))[0], i)
                             for i in candidates), reverse=True)
            if len(scores) > 1 and scores[0][0] - scores[1][0] < MARGIN:
                return None
            return scores[0][1]

        try:
            args, marks = read_clause(tool, words, forms, closed, present, pick)
        except _Abstain as error:
            return None, str(error)
        if args is None:
            return None, marks
        for i, f in enumerate(forms):
            if f == "<unk>" and i not in marks:
                return None, f"an unknown word that is not the copied name: {words[i]!r}"
        known = self.frames.get(tool, frozenset())
        for pair in frame_pairs(forms, marks):
            if pair not in known:
                return None, f"'{pair}' never stood together in a {tool} read"
        return [tool, args], None

    def read(self, request: str):
        """The calls every member gives, or None. Calls are proposals for the checks."""
        if not isinstance(request, str) or len(request) > 2048:
            return None
        first = None
        for member in range(self.members):
            calls, _why = self._read(member, request)
            if calls is None:
                return None
            if first is None:
                first = calls
            elif calls != first:
                return None
        return first

    def explain(self, request: str):
        """(calls or None, why) for the first member, for diagnosis only."""
        return self._read(0, request)


def load(directory: Path = DIRECTORY) -> Bundle:
    path, manifest = Path(directory) / FILE, Path(directory) / MANIFEST
    try:
        data = path.read_bytes()
        expected = json.loads(manifest.read_text(encoding="utf-8"))["sha256"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise BundleError(f"the system bundle is unavailable: {error}") from error
    if hashlib.sha256(data).hexdigest() != expected:
        raise BundleError("the system bundle does not match its manifest")
    return Bundle(json.loads(data))


_default = None


def enabled() -> bool:
    return os.environ.get(ENV, "").strip() == "1"


def default() -> Bundle | None:
    """The checked-in bundle when it is switched on and intact, else None (never raises)."""
    global _default
    if not enabled():
        return None
    if _default is None:
        try:
            _default = load()
        except BundleError:
            return None
    return _default
