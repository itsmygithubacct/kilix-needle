"""Familiar pane requests, read by a bundle of small nets, without the model.

The answer to a pane request has a grammar: a list of calls, each one tool
and that tool's arguments, each argument a value from a closed list or words
copied from the request. Every state of that grammar has one small net of its
own, and the state picks the net exactly; nothing is routed by a score:

    seg                    for every word: does it separate two clauses?
    tool                   for a clause: which pane tool, or none
    (tool, argument)       for a clause of that tool: absent, one value of the
                           argument's closed list, or "copy"
    (tool, argument) span  for every word of the clause: is it the first, is
                           it the last word of the copied text?

What is systematic is code, not learned: splitting words, reading a number
word as a number, copying text character for character.

The bundle answers only on ground it was trained on. It proposes nothing, and
the model reads the request as before, when
- two neighbouring words of a clause never stood side by side in its training
  rows (the pair mark), or a word it never saw is not part of a copied text;
- no tool fits a clause, a required argument is missing, or a number is not
  one the tool takes;
- its members (the same nets trained from different seeds) do not all give
  the same calls.
So what decides whether it answers is a lookup of words and word pairs it has
seen, and agreement, never a confidence.

Its calls are proposals like the model's: they pass every check
(actions.interpret), resolution against the live tree and the same
confirmation. The artifact is panes-bundle/, checked in with its manifest and
reproduced by panes_bundle_train.py; a file whose digest does not match the
manifest is refused. This module needs only the standard library.

The file (bundle.bin): a magic line, the length of a JSON header, the header,
then what all members share (every feature name once, the word pairs, and for
each net the numbers of its features, sorted) and each member's weights as
little-endian half floats. A request decodes only the table rows it touches.
The header also holds the words the reader goes by: the known words, the
tools' arguments, the tools whose copied text may be new, and the words that
always named a value.
"""
from __future__ import annotations

import array
from bisect import bisect_left
import hashlib
import json
import math
from operator import mul
import os
from pathlib import Path
import re
import struct
import sys

DIRECTORY = Path(__file__).resolve().parent / "panes-bundle"
SCHEMA = "kilix-needle.panes-bundle/v2"
MANIFEST_SCHEMA = "kilix-needle.panes-bundle-manifest/v2"
MAGIC = b"KNPB2\n"
FILE = "bundle.bin"
NONE, ABSENT, COPY = "<none>", "<absent>", "<copy>"
# Words that name a pane or tab by place; any other pane or tab is copied.
CANON = ("current", "left", "right", "above", "below", "next", "previous", "last")
# Closing and typing act on the pane or tab they name. The bundle names that
# pane or tab only with words it was trained on (or a number): a word it has
# never seen may be a title, but it may as well be a misspelt "right" or a
# "3rd", and the bundle cannot tell. Such a request is left to the model.
KNOWN_TARGET = {"close_pane": "pane", "close_tab": "tab", "run_in_pane": "pane"}

_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
          "nineteen", "twenty"]
_ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]
NUMBERS = {word: i for i, word in enumerate(_UNITS)}
NUMBERS.update({word: i + 1 for i, word in enumerate(_ORDINALS)})
NUMBERS.update({"thirty": 30, "forty": 40, "fifty": 50})


class BundleError(RuntimeError):
    """The artifact is missing, damaged or not the one the manifest names."""


def as_int(word: str):
    word = word.lower()
    if word.isdigit() and word.isascii():
        return int(word)
    return NUMBERS.get(word)


def stands_for(word: str, values, canon=()) -> bool:
    """Whether a word of the request is in the answer: as (part of) a text
    value, as the number it spells, or as the value it is another word for
    (`canon`: "lower" for below)."""
    number = as_int(word)
    for value in values:
        if isinstance(value, bool):
            continue
        text = str(value)
        if text in canon or word == text.lower() or any(word == t[0].lower() for t in tokenize(text)):
            return True
        if number is not None and text == str(number):
            return True
    return False


def tokenize(text: str) -> list[tuple[str, int, int]]:
    """Words with their places in the request: [(text, start, end)]."""
    out = []
    for match in re.finditer(r"\S+", text):
        start, end = match.start(), match.end()
        while end - start > 1 and text[start] in "\"'`(":
            out.append((text[start], start, start + 1))
            start += 1
        tail = []
        while end - start > 1:
            last = text[end - 1]
            if last in ",;?!:\"'`)":
                tail.append((last, end - 1, end))
                end -= 1
                continue
            if last == ".":
                core = text[start:end].rstrip(".")
                if core and core[-1].isalnum() and "/" not in core:
                    tail.append((text[start + len(core):end], start + len(core), end))
                    end = start + len(core)
                    continue
            break
        out.append((text[start:end], start, end))
        out.extend(reversed(tail))
    return out


def forms_of(words: list[str], known) -> list[str]:
    """A word the bundle was trained on stands as itself; any other is only a place."""
    return [w if w in known else ("<num>" if w.isdigit() else "<x>") for w in words]


def clause_features(forms: list[str], context: list[str], index: int, count: int) -> list[str]:
    padded = ["<s>"] + forms + ["</s>"]
    features = ["u=" + w for w in forms]
    features += ["b=" + a + "_" + b for a, b in zip(padded, padded[1:])]
    features += ["c=" + w for w in context]
    features += [f"n={count}", f"i={index}"]
    return features


def word_features(forms: list[str], i: int) -> list[str]:
    def at(j):
        return forms[j] if 0 <= j < len(forms) else ("<s>" if j < 0 else "</s>")
    features = [f"w{d:+d}={at(i + d)}" for d in (-3, -2, -1, 0, 1, 2, 3)]
    features += [f"b-1={at(i - 1)}_{at(i)}", f"b+1={at(i)}_{at(i + 1)}",
                 f"b-2={at(i - 2)}_{at(i - 1)}", f"b+2={at(i + 1)}_{at(i + 2)}",
                 f"pos={min(i, 6)}", f"rev={min(len(forms) - 1 - i, 6)}"]
    return features


def split_clauses(is_sep: list[bool]) -> list[tuple[int, int]]:
    clauses, start = [], 0
    for i in range(len(is_sep) + 1):
        if i == len(is_sep) or is_sep[i]:
            if i > start:
                clauses.append((start, i))
            start = i + 1
    return clauses


class Net:
    """Features -> the sum of their rows of a narrow table, over the root of
    their number -> one hidden layer -> one score for each output.

    The table stays in the file's bytes. `names` is the bundle's
    {feature: number}; `ids` holds this net's feature numbers in rising order,
    and the feature ids[r] has row r + 1 (row 0 belongs to no feature)."""

    def __init__(self, outputs, names, ids, raw, at, dim, hidden):
        self.outputs, self.names, self.ids, self.raw, self.at, self.dim = outputs, names, ids, raw, at, dim
        small = dim * hidden + hidden + hidden * len(outputs) + len(outputs)
        start = at + 2 * (len(ids) + 1) * dim
        if start + 2 * small > len(raw):
            raise BundleError("a net's weights do not have the size its header states")
        rest = struct.unpack_from(f"<{small}e", raw, start)
        cut = [dim * hidden, dim * hidden + hidden, dim * hidden + hidden + hidden * len(outputs)]
        self.w1 = [rest[j * dim:(j + 1) * dim] for j in range(hidden)]
        self.b1 = rest[cut[0]:cut[1]]
        self.w2 = [rest[cut[1] + k * hidden:cut[1] + (k + 1) * hidden] for k in range(len(outputs))]
        self.b2 = rest[cut[2]:]
        self.row = struct.Struct(f"<{dim}e")

    def scores(self, features: list[str]) -> list[float]:
        names, ids, known, dim = self.names, self.ids, len(self.ids), self.dim
        x, count = [0.0] * dim, 0
        for feature in features:
            number = names.get(feature)
            if number is None:
                continue
            r = bisect_left(ids, number)
            if r == known or ids[r] != number:
                continue
            count += 1
            for d, value in enumerate(self.row.unpack_from(self.raw, self.at + 2 * (r + 1) * dim)):
                x[d] += value
        scale = 1.0 / math.sqrt(max(1, count))
        x = [value * scale for value in x]
        hidden = [max(0.0, b + sum(map(mul, x, row))) for b, row in zip(self.b1, self.w1)]
        return [b + sum(map(mul, hidden, row)) for b, row in zip(self.b2, self.w2)]

    def best(self, features: list[str]) -> str:
        got = self.scores(features)
        return self.outputs[max(range(len(got)), key=got.__getitem__)]


class Member:
    """One trained set of nets: its own weights over what the bundle shares.
    Nets are set up when first asked."""

    def __init__(self, bundle: "Bundle", entry: dict):
        self.bundle, self.entry = bundle, entry
        self.known, self.tools = bundle.known, bundle.tools
        # Tools whose copied text may hold word pairs never seen in training.
        self.free_text = bundle.free_text
        self._nets: dict = {}

    def pairs(self) -> frozenset:
        return self.bundle.pairs()

    def net(self, key: str):
        if key not in self._nets:
            bundle, at = self.bundle, self.entry["weights"].get(key)
            spec = bundle.head["nets"].get(key)
            self._nets[key] = None if spec is None or at is None else Net(
                spec["outputs"], bundle.names(), bundle.ids(key), bundle.raw, bundle.body + at,
                bundle.head["dim"], bundle.head["hidden"])
        return self._nets[key]

    def read(self, request: str) -> tuple[list, str]:
        """(calls, reason). calls is [] when nothing is proposed; reason says why."""
        toks = tokenize(request)
        if not toks:
            return [], "empty"
        words = [t[0].lower() for t in toks]
        forms = forms_of(words, self.known)
        seg = self.net("seg")
        is_sep = [seg.best(word_features(forms, i)) == "sep" for i in range(len(forms))]
        clauses = split_clauses(is_sep)
        if not clauses or len(clauses) > 3:
            return [], "clauses"
        tool_net = self.net("tool")
        calls, accounted = [], {i for i, sep in enumerate(is_sep) if sep}
        unknown_pairs = []
        for index, (lo, hi) in enumerate(clauses):
            mine = forms[lo:hi]
            context = [w for j, w in enumerate(forms) if not lo <= j < hi and not is_sep[j]]
            features = clause_features(mine, context, index, len(clauses))
            # Pair n stands between word n - 1 and word n of the clause.
            padded, pairs = ["<s>"] + mine + ["</s>"], self.pairs()
            unknown = [n for n in range(len(padded) - 1) if padded[n] + "_" + padded[n + 1] not in pairs]
            tool = tool_net.best(features)
            if tool == NONE:
                return [], "no tool fits" if not unknown else "an untrained word pair"
            spec = self.tools[tool]
            arguments, copied = {}, set()
            for key, kind, low, high in spec["args"]:
                net = self.net(f"arg {tool} {key}")
                if net is None:
                    continue
                label = net.best(features)
                if label == ABSENT:
                    continue
                if label != COPY:
                    arguments[key] = True if kind == "flag" else label
                    continue
                pointer = self.net(f"span {tool} {key}")
                if pointer is None:
                    return [], "no span net"
                got = [pointer.scores(word_features(mine, i)) for i in range(len(mine))]
                first = max(range(len(mine)), key=lambda i: got[i][0])
                last = max(range(first, len(mine)), key=lambda i: got[i][1])
                text = request[toks[lo + first][1]:toks[lo + last][2]]
                if KNOWN_TARGET.get(tool) == key and "<x>" in mine[first:last + 1]:
                    return [], "an untrained word names what to close or type into"
                if kind == "int":
                    number = as_int(text)
                    if number is None or not low <= number <= high:
                        return [], f"{key} is not a number the tool takes"
                    text = number
                elif kind == "ref":
                    number = as_int(text)
                    if number is not None:
                        text = str(number)
                arguments[key] = text
                copied.update(range(first, last + 1))
            if any(key not in arguments for key in spec["required"]):
                return [], "a required argument is missing"
            if unknown:
                # A pair inside or beside copied text is still a pair the nets
                # never saw. It is let through only for a tool listed as one
                # whose text may be new, and only when copied text accounts
                # for every such pair.
                if not (tool in self.free_text and all(n - 1 in copied or n in copied for n in unknown)):
                    unknown_pairs.append(index)
            accounted.update(lo + i for i in copied)
            calls.append({"name": tool, "arguments": arguments})
        if unknown_pairs:
            return [], "an untrained word pair"
        # A word the bundle was never trained on must have been copied as an
        # argument; otherwise this request is not ground it knows.
        for i, form in enumerate(forms):
            if form in ("<x>", "<num>") and i not in accounted:
                return [], "an untrained word"
        return calls, "ok"


class Bundle:
    """The file and its members. It answers only when all members agree."""

    def __init__(self, raw: bytes, manifest: dict | None = None):
        if raw[:len(MAGIC)] != MAGIC:
            raise BundleError("not a panes bundle file")
        size = int.from_bytes(raw[len(MAGIC):len(MAGIC) + 4], "little")
        start = len(MAGIC) + 4
        try:
            head = json.loads(raw[start:start + size].decode("utf-8"))
        except ValueError as error:
            raise BundleError(f"the bundle header is unreadable: {error}") from None
        if head.get("schema") != SCHEMA:
            raise BundleError(f"unknown bundle schema {head.get('schema')!r}")
        self.raw, self.head, self.body = raw, head, start + size
        self.manifest = manifest or {}
        self.known = frozenset(head["known"])
        self.tools = head["tools"]
        self.free_text = frozenset(head.get("free_text", ()))
        # {word: the values it is another word for}: the words that named a
        # value of the answer in every training row that asked for an action.
        self.value_words = head.get("value_words", {})
        self._names = self._pairs = None
        self._ids: dict = {}
        self.members = [Member(self, entry) for entry in head["members"]]
        if not self.members:
            raise BundleError("the bundle holds no member")
        self.label = f"bundle {self.manifest.get('name', '')}".strip()

    def _text(self, place) -> list[str]:
        at, size = place
        text = self.raw[self.body + at:self.body + at + size].decode("utf-8")
        return text.split("\n") if text else []

    def names(self) -> dict:
        """Every feature any net knows, numbered in the order of the file."""
        if self._names is None:
            self._names = {name: number for number, name in enumerate(self._text(self.head["names"]))}
        return self._names

    def pairs(self) -> frozenset:
        """Every pair of neighbouring words of a clause in the training rows."""
        if self._pairs is None:
            self._pairs = frozenset(self._text(self.head["pairs"]))
        return self._pairs

    def ids(self, key: str):
        """The numbers of one net's features, in rising order."""
        if key not in self._ids:
            at, count = self.head["nets"][key]["ids"]
            numbers = array.array("I")
            if numbers.itemsize != 4:
                raise BundleError("this platform has no 4-byte unsigned integer array")
            held = self.raw[self.body + at:self.body + at + 4 * count]
            if len(held) != 4 * count:
                raise BundleError("a net's features do not have the size its header states")
            numbers.frombytes(held)
            if sys.byteorder != "little":
                numbers.byteswap()
            self._ids[key] = numbers
        return self._ids[key]

    @classmethod
    def load(cls, directory: Path | None = None) -> "Bundle":
        directory = Path(directory) if directory else DIRECTORY
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise BundleError(f"the bundle manifest is unreadable: {error}") from None
        if manifest.get("schema") != MANIFEST_SCHEMA:
            raise BundleError("unknown bundle manifest schema")
        try:
            raw = (directory / str(manifest.get("file", FILE))).read_bytes()
        except OSError as error:
            raise BundleError(f"the bundle file is unreadable: {error}") from None
        if len(raw) != manifest.get("bytes") or hashlib.sha256(raw).hexdigest() != manifest.get("sha256"):
            raise BundleError("the bundle file does not match the digest in manifest.json")
        return cls(raw, manifest)

    def read(self, request: str) -> tuple[list, str]:
        calls, reason = self.members[0].read(request)
        if not calls:
            return [], reason
        for member in self.members[1:]:
            other, _ = member.read(request)
            if other != calls:
                return [], "the members do not agree"
        # Nothing the request says may be dropped: a word that in training
        # always named a value ("lower", a program, a number) has to be in the
        # answer. If it is not, the nets read a sentence of a kind they were
        # not trained on, and the model reads it.
        values = [value for call in calls for value in call["arguments"].values()]
        for token in tokenize(request) if self.value_words else ():
            word = token[0].lower()
            canon = self.value_words.get(word)
            if canon is not None and not stands_for(word, values, canon):
                return [], "a word that always named a value is not in the answer"
        return calls, "ok"


_LOADED: dict = {}


def default() -> Bundle | None:
    """The checked-in bundle, loaded once; None when there is none to use."""
    if "bundle" not in _LOADED:
        try:
            _LOADED["bundle"] = Bundle.load() if (DIRECTORY / "manifest.json").is_file() else None
        except BundleError as error:
            print(f"kilix-needle: the panes bundle is not used ({error})", file=sys.stderr)
            _LOADED["bundle"] = None
    return _LOADED["bundle"]


def read(request: str) -> tuple[list, str] | None:
    """(calls, the bundle's label) for a request the checked-in bundle knows,
    or None. Whether it is asked at all is needle_cli's to say."""
    bundle = default()
    if bundle is None or "\n" in request or "\r" in request:
        return None
    calls, _ = bundle.read(request)
    return (calls, bundle.label) if calls else None


def main(argv: list[str]) -> int:
    if not argv:
        print('usage: python3 panes_bundle.py "split right and run htop"', file=sys.stderr)
        return 2
    bundle = Bundle.load()
    for request in argv:
        calls, reason = bundle.read(request)
        print(json.dumps({"request": request, "calls": calls, "reason": reason}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
