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

The same reader reads a bundle for each of the other jobs (format v6: the
file says which job it reads for). Their tools and arguments are those of
job_schemas.py, made for reading; job_schemas.to_job writes a reading as the
job's own call, and the job's own checks decide whether it is admitted, as
they decide a model's proposal. Those bundles live in job-bundles/<job>/.

The bundle answers only what it was trained on, said with other names. It
proposes nothing, and the model reads the request as before, unless all of
this holds:
- every clause is the wording of a training clause of its tool, read as that
  clause was labelled: with each copied text replaced by a mark for its
  argument (the frame), the words are the same words in the same order and
  every value from a closed list is the one that clause had (a reading). A
  clause that stood first or alone in training says all it means by itself
  and may stand anywhere. One that only ever followed another ("go to it")
  leans on that one, and is read only after a clause of the same tool.
  The nets say where the clauses are, which tool each one is and which
  words are copied; the table of readings says whether that is something
  the bundle was shown. Two reviews found what looser holds let through:
  held to known words and pairs of neighbours it renamed the caller's tab
  for "rename the logs tab to api", and held to pairs of neighbours of its
  own tool's clauses it opened a tab for "not a tab";
- every word and pair of neighbouring words stood in its training rows, or
  is part of a copied text of a tool whose text may be new;
- two clauses are joined by a word that joined two in training, there are
  no more clauses than a training row had, and no copied text is longer
  than any copied for that argument in training;
- a copied name, title or command holds no word the bundle knows only as
  wording ("rename this tab please" does not name it "please"), and ends
  in no mark that ended the sentence ("api!"; a mark just inside a closing
  quote is the quoted text's own: "'api!'" is api!);
- every word of a clause that names a value is in that clause's call: one
  that always named a value in training, or that alone named a side, a
  place, a layout or a direction ("the top pane to the left" is not the
  pane to the left). In a job other than panes, a clause that is a
  training clause of its tool, read with the values it was labelled with,
  is held to that label instead: those jobs' closed lists hold everyday
  words ("status", "modified", the "up" of "spin up"), and the label says
  where they name nothing. The panes job keeps the rule for every clause,
  and a word that names nothing in its answer leaves the request to the
  model;
- no clause goes by what the clause before it changes. Every call is
  resolved before the first one runs, so "go to the logs tab and rename it
  to api" would rename the tab the request came from, and "close tab 1 and
  go to tab 2" would go to what is tab 1 by then (_CHANGES);
- a rename that does not say whether it renames a tab or a pane does not
  follow a clause about the other one ("maximize the left pane and call it
  api": read alone, "call it api" is a tab's rename);
- a tool fits every clause, no required argument is missing, every number
  is one the tool takes and is written in digits the reader reads;
- no resize's verb and direction pull opposite ways ("grow this pane
  lower", "shrink this pane broader": OPPOSED);
- its members (the same nets trained from different seeds) all give the
  same calls.
So what decides whether it answers is a lookup of wordings it has seen, and
agreement, never a confidence.

A list of the panes or tabs (or both) is a kind of its own (LIST), answered
only as the request's one clause, with the list the exact list reader gives.
A tab's number after "tab id" is the tab's id ("id:4"), never its place, and
that id is the number in the call (stands_for).

The second reader, for the kinds the manifest switches on (second_reader;
none unless it names them, each one measured against the model first),
answers a clause that is no training clause of its tool when every other
hold above holds and (Member.second): every run of three neighbours of its
frame stood side by side in a training frame of the tool (RUN; pairs gave
"not a new tab, thanks" and "i need one" again), its values from closed
lists are those of a training clause of the tool, and a side, place, layout
or direction it gives is named by a word of the clause. It never answers a
call that closes, types or starts a program, nor a request with one in it
(risky). Two clauses are a kind of their own (COMPOUND), and in them it
answers a clause only where it is switched on for that clause's tool too:
beside another clause, a clause in new wording is no more answered than
alone. Its answers carry a label of their own.

Its calls are proposals like the model's: they pass every check
(actions.interpret), resolution against the live tree and the same
confirmation. The checks read the whole request and add what they find in
it, a "tab 2" as the tab of a rename; a proposal is used only when what they
admit is what the bundle read (needle_cli.checks_agree). The artifact is
panes-bundle/, checked in with its manifest and reproduced by
panes_bundle_train.py; a file whose digest does not match the manifest is
refused. This module needs only the standard library.

The file (bundle.bin): a magic line, the length of a JSON header, the header,
then what all members share (every feature name once, the word pairs, and for
each net the numbers of its features, sorted) and each member's weights as
little-endian half floats. A request decodes only the table rows it touches.
The header also holds the words the reader goes by: the known words, the
tools' arguments, the tools whose copied text may be new, the words that
name a value, the words that joined two clauses, the largest number of
clauses in a training row, the most words copied for each argument, the
words that were copied for each argument, for each tool where its readings
are, and the second reader's ground: for each tool where the runs of its
training frames are (and their length), the values from closed lists its
training clauses had together, and the values a word of the clause must
name.
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
import stat
import struct
import sys

DIRECTORY = Path(__file__).resolve().parent / "panes-bundle"
# The bundles of the other jobs (job_schemas.py says what each one reads).
JOB_DIRECTORY = Path(__file__).resolve().parent / "job-bundles"
JOBS = ("panes", "files", "system", "agents", "apps", "tmux", "pty", "logs")
SCHEMA = "kilix-needle.panes-bundle/v6"
MANIFEST_SCHEMA = "kilix-needle.panes-bundle-manifest/v6"
MAGIC = b"KNPB6\n"
# A file of the format before (v5) is a panes bundle: v6 only added the job
# a bundle reads for. Each older magic with its schema and manifest schema.
OLDER = {b"KNPB5\n": ("kilix-needle.panes-bundle/v5", "kilix-needle.panes-bundle-manifest/v5")}
FILE = "bundle.bin"
MAX_BYTES = 64 << 20        # no bundle is near this size; a manifest that states more is refused
MANIFEST_MAX = 1 << 20      # and no manifest is near this one
MEMBERS_MIN = 2             # one member agrees with itself about anything
NONE, ABSENT, COPY = "<none>", "<absent>", "<copy>"
# The bundle's own kind: a list of the panes or tabs (or both), which the
# route answers with the list the exact list reader gives (needle_cli). It
# changes nothing, and it is answered only as the request's one clause.
LIST = "list"
# The bundle's other kind of its own: a read of a pane's text (pane_reads.py),
# which changes nothing either, and which is also answered only alone.
READ = "read_pane"
ALONE_KINDS = (LIST, READ)
# What the second reader may answer: a tool's name, or this for two clauses.
COMPOUND = "compound"
OK, OK_SECOND = "ok", "ok, by the second reader"
# Words that name a pane or tab by place; any other pane or tab is copied.
CANON = ("current", "left", "right", "above", "below", "next", "previous", "last")
# Closing and typing act on the pane or tab they name. The bundle names that
# pane or tab only with words it was trained on (or a number): a word it has
# never seen may be a title, but it may as well be a misspelt "right" or a
# "3rd", and the bundle cannot tell. Such a request is left to the model.
KNOWN_TARGET = {"close_pane": "pane", "close_tab": "tab", "run_in_pane": "pane"}
# The tools that rename, and the word for what each renames. A rename that
# does not say that word ("then call it api") renames what the clause before
# it was about (Member.read).
RENAMES = {"rename_tab": "tab", "rename_pane": "pane"}
# Every call of a request is resolved against the desktop as it was before
# the first one ran (kilix.resolve), and every call is sent from the pane the
# request was made in. So a call that goes by something an earlier call
# changes would act on the wrong pane or tab: after "go to the logs tab",
# "rename it to api" is resolved in the tab the request came from.
# What a call changes for the calls after it, beside the pane or tab it names:
#   pane, tab   which pane and which tab "this", "it" and "here" are
#   sides       which pane is to the left, right, above or below, or the next
#   order       which tab is tab 2, the next one or the last
#   layout      whether a tab shows one pane at full size (kilix.resolve
#               reads that from the snapshot for a maximize and a restore)
#   focus       where the keyboard is, after a call that is made to put it
#               somewhere or that opens something. A move of a tab and a
#               swap are made through the caller's pane and put it back there
#   caller      whether the pane the request was made in is still there
#   a title     the name it gives, the program it starts or the command it
#               types is what that pane or tab is called from then on, and
#               the title it was found by is what it is called no more
# A close is taken to change the pane, and a close of a tab the tab, whichever
# one it closes: the reader does not know whether it is the caller's own. A
# pane named by its side is in the caller's tab; one named by a title or a
# number may be in another, may be the last of its tab, so that the tab goes
# with it, and may be the caller's own (changes).
_CHANGES = {"go_to_pane": ("pane", "focus"), "go_to_tab": ("pane", "tab", "focus"),
            "open_pane": ("pane", "sides", "focus"), "open_tab": ("pane", "tab", "order", "focus"),
            "close_pane": ("pane", "sides"), "close_tab": ("pane", "tab", "order"),
            "maximize_pane": ("sides", "layout"), "arrange_panes": ("sides", "layout"),
            "swap_panes": ("sides",), "move_tab": ("order",)}
# What a call goes by without an argument that says so: the caller's own pane
# or tab. (A new tab takes only its directory from the caller's pane.) Every
# call goes by the caller (goes_by).
_GOES_BY = {"open_pane": ("pane",), "arrange_panes": ("tab",), "swap_panes": ("pane", "sides", "focus"),
            "move_tab": ("tab", "order", "focus"), "maximize_pane": ("layout",)}
SIDES = ("left", "right", "above", "below")
# A pane or tab named this way is never the one the request was made in: a
# pane on a side of the caller's, and the one after or before it (a close
# does not wrap round to the caller's own). Such a pane is in the caller's
# tab, which is still there when it is closed.
NOT_THE_CALLERS = {"close_pane": (*SIDES, "next", "previous"), "close_tab": ("next", "previous")}
# Values the tool's labels give the other way round from kitty. kitty's
# "vertical" layout shows the panes one below another and its "horizontal"
# layout side by side (kitty/layout/vertical.py: Vertical lays its windows
# out along y, Horizontal along x). The tool's own description says
# "vertical is side by side; horizontal is one above another" (actions.py),
# and the training rows are labelled by it. The bundle would repeat those
# labels; where these layouts are enabled the panes would be arranged the
# other way round from what was asked. The model is trained on the same
# labels: this is the tool's to fix, not the bundle's.
CROSSED = {("arrange_panes", "layout"): ("vertical", "horizontal")}
# A resize whose verb grows the pane while its direction shrinks it, or the
# other way round, is not read: "grow this pane lower" asks for a taller pane
# in the words of a person and a shorter one in the words the bundle was
# trained on ("lower" is shorter there), and "shrink this pane broader" says
# both. The direction would decide alone (the fourth review). The tuner's own
# rows hold four such, all of one template ("stretch this pane lower",
# labelled shorter). "Narrow" is left out: it is as often what a pane is.
_GROWS = frozenset({"grow", "expand", "enlarge", "stretch", "extend", "lengthen", "increase", "widen",
                    "broaden", "heighten"})
_SHRINKS = frozenset({"shrink", "reduce", "decrease", "compress", "squeeze", "shorten", "lessen",
                      "contract", "trim"})
_GROWING = {"wider": True, "taller": True, "narrower": False, "shorter": False}
# A name, or a title a pane or tab is found by (a list's `about` among them),
# of more than one word is made of words that were copied for such an
# argument in training. One that holds a word the bundle has never seen is
# that one word (Member.read).
ONE_NEW = ("name", "pane", "tab", "about")
# A mark that ended the sentence or a part of it, split from the word before.
_CLOSING = frozenset(",;?!:.")
_MARK = re.compile(r"<[a-z_]+#?>")

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


_DIGIT_ORDINAL = re.compile(r"([0-9]{1,6})(?:st|nd|rd|th)")


def number_of(text) -> int | None:
    """The number a copied text spells: as_int, and "3rd" for 3. The nets see
    "3rd" as a word they do not know (the features are as they were trained);
    only what a copy of it means is read here."""
    text = str(text)
    number = as_int(text)
    if number is None:
        match = _DIGIT_ORDINAL.fullmatch(text.lower())
        number = int(match.group(1)) if match else None
    return number


def stands_for(word: str, values, canon=()) -> bool:
    """Whether a word of the request is in the answer: as (part of) a text
    value, as the number it spells (a tab's id, `id:N`, among them: "tab id
    4"), or as the value it is another word for (`canon`: "lower" for
    below)."""
    number = number_of(word)
    for value in values:
        if isinstance(value, bool):
            continue
        text = str(value)
        if text in canon or word == text.lower() or any(word == t[0].lower() for t in tokenize(text)):
            return True
        if number is not None and text in (str(number), f"id:{number}"):
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


def frame_of(words: list[str], spans: dict) -> list[str]:
    """A clause as its answer reads it: each copied text is one mark that
    names its argument (and says whether it was read as a number), every
    other word stays as it is. Every occurrence of a copied text is its
    mark, the longest text first: in "name this tab api, it's where the api
    stuff lives" the name stands twice, and neither is a word of the wording.

    spans: {argument: (first word, last word, read as a number)}."""
    mark = {}
    for key, (first, last, number) in sorted(spans.items(), key=lambda item: (item[1][0] - item[1][1], item[1][0])):
        text, label = words[first:last + 1], f"<{key}#>" if number else f"<{key}>"
        places = [first] + [i for i in range(len(words) - len(text) + 1)
                            if i != first and words[i:i + len(text)] == text]
        for at in places:
            if all(i not in mark for i in range(at, at + len(text))):
                mark.update((i, (label, at)) for i in range(at, at + len(text)))
    out = []
    for i, word in enumerate(words):
        if i not in mark:
            out.append(word)
        elif mark[i][1] == i:
            out.append(mark[i][0])
    return out


def acts_on(tool: str) -> str:
    """"tab" or "pane": what a tool acts on, by its name."""
    return "tab" if tool.endswith("_tab") else "pane"


def reading_line(before: str, frame: list[str], closed: dict) -> str:
    """One reading as a line of the file: the tool of the clause before it
    ("" for a clause that stands first or alone), the frame, then every
    argument that was read from a closed list with its value (a flag that
    is set has the value "true"). Copied texts are in the frame as their
    marks."""
    return before + "\t" + " ".join(frame) + "".join(f"\t{key}={closed[key]}" for key in sorted(closed))


def _titles(text) -> set:
    """Each word of a title, as changes() and goes_by() name it: a pane or
    tab is found by its whole title or by a whole word of it."""
    return {"title " + word for word in str(text).casefold().split()}


def changes(call: dict) -> set:
    """What a call changes for the calls after it (_CHANGES). A pane or tab
    it names is what "it" means from there on, a name it gives, a program it
    starts or a command it types is a title that was not there before, and
    the title it found that pane or tab by is one that is there no more."""
    given = call["arguments"]
    out = set(_CHANGES.get(call["name"], ()))
    out.update(key for key in ("pane", "tab") if given.get(key) not in (None, "current"))
    if call["name"] == "close_pane" and given.get("pane") == "current":
        out.add("tab")      # the caller's own pane is gone, and its tab may be
    if call["name"] in ("go_to_pane", "maximize_pane") and given.get("pane") not in (None, *CANON):
        out.add("tab")      # the focus goes to a pane that may be in another tab
    if call["name"] == "maximize_pane" and given.get("pane") not in (None, "current"):
        out.add("focus")    # the pane shown at full size is the one in focus
    for tool, key in (("close_pane", "pane"), ("close_tab", "tab")):
        if call["name"] == tool and given.get(key) not in NOT_THE_CALLERS[tool]:
            # It may close the pane the request was made in, and then nothing
            # after it is sent. And a pane may be the last of its tab.
            out.update(("caller", "order"))
    for key in ("name", "program", "command"):
        if isinstance(given.get(key), str):
            out |= _titles(given[key])
    # A pane or tab that gets a new name, or a command typed into it, is no
    # longer called what this call found it by: after "call the build pane
    # api", "the build pane" names nothing, and the route would still act on
    # the pane that was called so.
    if call["name"] in RENAMES or call["name"] == "run_in_pane":
        for key in ("pane", "tab"):
            ref = given.get(key)
            if isinstance(ref, str) and ref not in CANON and not (ref.isascii() and ref.isdigit()):
                out |= _titles(ref)
    return out


def goes_by(call: dict, refs) -> set:
    """What a call is resolved by (_GOES_BY and its pane or tab). `refs` are
    the tool's arguments that hold a pane or a tab; one that is left out is
    the caller's own. A pane's number names it wherever the focus is, a
    tab's number is its place in the row of tabs, and a pane with a title is
    looked for in the caller's tab first."""
    given = call["arguments"]
    out = {"caller", *_GOES_BY.get(call["name"], ())}
    for key in refs:
        ref = given.get(key)
        if ref in (None, "current"):
            out.add(key)
        elif ref in SIDES:
            out.update(("pane", "sides"))
        elif ref in ("next", "previous", "last"):
            out.update(("tab", "order") if key == "tab" else ("pane", "sides"))
        elif str(ref).isascii() and str(ref).isdigit():
            out.update(("order",) if key == "tab" else ())
        else:
            out |= _titles(ref) | ({"tab"} if key == "pane" else set())
    return out


def forms_of(words: list[str], known) -> list[str]:
    """A word the bundle was trained on stands as itself; any other is only a
    place. A number is always only a place: which number it is is read by
    code, not by the nets. (A number that stood as itself where the training
    rows had it as wording, "kill process 20", made "close pane 20" a pair of
    words the nets never saw beside each other, where "close pane 21" was
    one they had.)"""
    return [("<num>" if w.isdigit() else "<x>") if w not in known or w.isdigit() else w for w in words]


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


# Why a request the nets could read is not answered. A request can fail
# several holds; the one reported is the first of these it fails, so each
# count says what that hold alone turned away of what the earlier ones let by.
PAIR = "an untrained word pair"
WORD = "an untrained word"
LONG = "a copied text longer than any in training"
UNSHOWN = "wording this tool was not trained on"
CARRIED = "a rename that does not say what it renames, after a clause about the other kind"
UNNAMED = "a word that names a value is not in its clause's call"
PLAIN = "a copied text holds a word the bundle knows only as wording"
MIXED = "a copied name or title of several words holds one the bundle has never seen"
CLOSED = "a copied text ends in a mark that ended the sentence"
LEANS = "a clause that goes by what the clause before it changes"
TURNED = "a layout kitty shows the other way round from the tool's labels"
OPPOSED = "a resize whose verb and direction pull opposite ways"
ALONE = "a list or a read of a pane beside another clause"
_HOLDS = (LONG, UNSHOWN, CARRIED, UNNAMED, PLAIN, MIXED, CLOSED, LEANS, TURNED, OPPOSED)
# Why the second reader does not answer a clause that is not a training
# clause of its tool (reported in the place of UNSHOWN, for a kind it may
# answer).
SECOND_RUN = "three neighbours together that no training clause of this tool had"
SECOND_SET = "values from closed lists no training clause of this tool had together"
SECOND_SAID = "a value from a closed list that no word of the clause names"
SECOND_RISKY = "a call that closes, types or starts a program: only whole training clauses ask for one"
# How many neighbours of a frame, side by side, the second reader holds to
# a training frame of the tool. Two are not enough: a walk over pairs of
# neighbours of a tool's own clauses gives "not a new tab, thanks" and "i
# need one", and both opened something (the second review).
RUN = 3
# The calls that close, type into a pane, start a program, start or message
# a coding agent, or launch an app (the bundles of the agents and apps jobs).
_RISKY = ("close_pane", "close_tab", "run_in_pane", "agent", "tell", "launch")


def risky(call: dict) -> bool:
    """Whether a call closes something, types into a pane or starts a
    program: what the checks hold for a yes (actions.Action.risky). The
    second reader never answers one."""
    return call["name"] in _RISKY or (call["name"] in ("open_pane", "open_tab")
                                      and bool(call["arguments"].get("program")))


def runs(frame: list[str]) -> list[str]:
    """The runs of RUN neighbours of a frame, from its start to its end (a
    shorter frame is one run), as the second reader's table holds them."""
    padded = ["<s>"] + frame + ["</s>"]
    k = min(RUN, len(padded))
    return [" ".join(padded[i:i + k]) for i in range(len(padded) - k + 1)]


def _after_id(words: list[str], at: int) -> bool:
    """Whether the word before `at` is "id" (a ":" or "#" between them aside):
    "tab id 4", "tab id: 4". A tab's number after it is the tab's id, never
    its place: "go to tab id 14" is not the fourteenth tab."""
    at -= 1
    while at >= 0 and words[at] in (":", "#"):
        at -= 1
    return at >= 0 and words[at] == "id"


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

    def read(self, request: str, texts: list | None = None) -> tuple[list, str]:
        """(calls, reason). calls is [] when nothing is proposed; reason says
        why. `texts`, when given and calls are proposed, is set to the text
        of the clause each call was read from."""
        toks = tokenize(request)
        if not toks:
            return [], "empty"
        words = [t[0].lower() for t in toks]
        # The nets take any run of digits for a number (forms_of); the reader
        # reads the digits 0 to 9 only (as_int). A request with other digits
        # is not ground it was trained on.
        if any(word.isdigit() and not word.isascii() for word in words):
            return [], "digits the reader does not read"
        # A word spelt like a mark of the frame would stand in a frame as one.
        if any(_MARK.fullmatch(word) for word in words):
            return [], "a word spelt like one of the reader's marks"
        bundle, failed = self.bundle, set()
        forms = forms_of(words, self.known)
        seg = self.net("seg")
        is_sep = [seg.best(word_features(forms, i)) == "sep" for i in range(len(forms))]
        # Only a word that joined two clauses in training may join two here:
        # the net would take "or", or any word it never saw, for one.
        if any(sep and words[i] not in self.bundle.separators for i, sep in enumerate(is_sep)):
            return [], "clauses joined by a word that never joined two in training"
        clauses = split_clauses(is_sep)
        if not clauses:
            return [], "clauses"
        if len(clauses) > self.bundle.clauses_max:
            return [], "more clauses than any training row had"
        tool_net = self.net("tool")
        calls, accounted, places, unshown = [], {i for i, sep in enumerate(is_sep) if sep}, [], []
        unshown_tools: list = []     # the tool of each clause in `unshown`
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
            arguments, closed, copied, spans = {}, {}, set(), {}
            for key, kind, low, high in spec["args"]:
                net = self.net(f"arg {tool} {key}")
                if net is None:
                    continue
                label = net.best(features)
                if label == ABSENT:
                    continue
                if label != COPY:
                    arguments[key] = True if kind == "flag" else label
                    closed[key] = label
                    if label in CROSSED.get((tool, key), ()):
                        failed.add(TURNED)
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
                number = None
                if kind == "int":
                    number = number_of(text)
                    if number is None or not low <= number <= high:
                        return [], f"{key} is not a number the tool takes"
                    text = number
                elif kind == "ref":
                    number = number_of(text)
                    if number is not None:
                        text = f"id:{number}" if key == "tab" and _after_id(words, lo + first) else str(number)
                # No copy is longer than the longest text copied for this
                # argument in training: a title of nine words is the rest of
                # the sentence, not a title.
                if last - first + 1 > bundle.copy_max.get(tool, {}).get(key, 0):
                    failed.add(LONG)
                if number is None:
                    # A name, a title or a command is made of words new to
                    # the bundle or of words that were copied for such an
                    # argument in training. A word it knows only as wording
                    # is wording here too: "rename this tab please" gives no
                    # name. And no copy ends in the mark that ended the
                    # sentence ("api!"): a mark split from the word before
                    # it, or standing alone as the whole text.
                    held = bundle.copied.get(key, ())
                    if any(word in self.known and word not in held for word in words[lo + first:lo + last + 1]):
                        failed.add(PLAIN)
                    # A word the bundle has never seen is a name only when it
                    # stands alone where a name stands. Beside another word it
                    # may as well be wording the bundle does not know: "api
                    # thx", "uh api" and "api pronto" are not names of two
                    # words. A command or a program is not held to this: it
                    # is made of new words, and it waits for a yes that shows
                    # it.
                    if key in ONE_NEW and "<x>" in mine[first:last + 1] and len(text.split()) > 1:
                        failed.add(MIXED)
                    end = toks[lo + last]
                    after = toks[lo + last + 1] if lo + last + 1 < len(toks) else None
                    quoted = after is not None and after[0] in "\"'`" and after[1] == end[2]
                    if set(end[0]) <= _CLOSING and (last == first or end[1] == toks[lo + last - 1][2]) \
                            and not quoted:
                        failed.add(CLOSED)
                arguments[key] = text
                spans[key] = (first, last, number is not None)
                copied.update(range(first, last + 1))
            if any(key not in arguments for key in spec["required"]):
                return [], "a required argument is missing"
            direction = arguments.get("direction") if tool == "resize_pane" else None
            if direction in _GROWING and set(words[lo:hi]) & (_SHRINKS if _GROWING[direction] else _GROWS):
                failed.add(OPPOSED)
            # The clause as this answer reads it must be a training clause
            # of this tool, read as that clause was labelled: the same words
            # in the same order around the copied texts and the same values
            # from the closed lists. Words and pairs the bundle knows are not
            # enough, and neither are pairs of neighbours of this tool's own
            # clauses: a walk over those pairs gives "not a tab" and "i need
            # one", and both opened something. The reading must have stood
            # first or alone in training, or after a clause of the tool that
            # stands before it here: one that only ever followed another
            # leans on it ("go to it" alone names no pane).
            frame, shown = frame_of(words[lo:hi], spans), bundle.readings(tool)
            trained = reading_line("", frame, closed) in shown or bool(
                calls and reading_line(calls[-1]["name"], frame, closed) in shown)
            if not trained:
                # Not a training clause of its tool. Only the second reader
                # may answer it, and only for a kind it is switched on for
                # (below): this is whether it would.
                unshown.append(self.second(tool, frame, closed, words[lo:hi], calls[-1]["name"] if calls else ""))
                unshown_tools.append(tool)
            # Nothing the clause says may be dropped: a word that in training
            # always named a value ("lower", a program, a number), or that
            # alone named a side, a place, a layout or a direction ("left",
            # "top", "grid"), has to be in this clause's call: as that value,
            # or as part of a phrase that named it ("one above another"). The
            # other clause's call does not count: "make the left pane wider
            # and go to the left pane" still widens the caller's pane. In a
            # job other than panes a training clause read as it was labelled
            # is held to its label instead (the module's docstring).
            values = list(arguments.values())
            for word in words[lo:hi] if bundle.job == "panes" or not trained else ():
                canon = bundle.value_words.get(word)
                if canon is not None and not stands_for(word, values, canon):
                    failed.add(UNNAMED)
            # A call must not go by anything a call before it changes: the
            # route resolves it as if that call had not run (_CHANGES). After
            # "go to the logs tab", "it", "here", "this tab" and "the next
            # one" mean the place the first call went to, and the route would
            # act where the request came from. After "rename this tab to
            # api", "the api tab" is this one, and the route would look for
            # another.
            changed = set().union(*map(changes, calls))
            if changed & goes_by({"name": tool, "arguments": arguments},
                                 [key for key, kind, _, _ in spec["args"] if kind == "ref"]):
                failed.add(LEANS)
            # "then call it api" does not say what it renames: it is what the
            # clause before was about. The nets read a clause's tool from its
            # own words and take such a clause for the same rename wherever it
            # stands, and every training row that has one has it after a
            # clause about a tab. After a clause about the other kind of
            # thing ("go to the left pane and rename it to api") that reading
            # renames the wrong one.
            kind = RENAMES.get(tool)
            if kind and calls and acts_on(calls[-1]["name"]) != kind \
                    and not any(word in (kind, kind + "s", kind + "'s") for word in frame):
                failed.add(CARRIED)
            if unknown:
                # A pair inside or beside copied text is still a pair the nets
                # never saw. It is let through only for a tool listed as one
                # whose text may be new, and only when copied text accounts
                # for every such pair.
                if not (tool in self.free_text and all(n - 1 in copied or n in copied for n in unknown)):
                    failed.add(PAIR)
            accounted.update(lo + i for i in copied)
            calls.append({"name": tool, "arguments": arguments})
            places.append((toks[lo][1], toks[hi - 1][2]))
        # A list is the whole request: "list the panes and close pane 4" is
        # not a list, and the list changes nothing a clause after it goes by.
        if len(calls) > 1 and any(c["name"] in ALONE_KINDS for c in calls):
            return [], ALONE
        if PAIR in failed:
            return [], PAIR
        # A word the bundle was never trained on must have been copied as an
        # argument; otherwise this request is not ground it knows.
        for i, form in enumerate(forms):
            if form in ("<x>", "<num>") and words[i] not in self.known and i not in accounted:
                return [], WORD
        # A clause that is not a training clause of its tool is answered by
        # the second reader or not at all. It answers only the kinds it is
        # switched on for (the manifest's second_reader), each one measured
        # against the model before it was.
        # It never answers a call that closes, types or starts a program, nor
        # anything beside one.
        # Two clauses are a kind of their own (`compound`), and the second
        # reader answers a clause of them only where it is switched on for
        # that clause's tool as well: beside another clause, a clause in new
        # wording is no more answered than alone. Without this, `compound`
        # let it answer resizes, swaps, maximizes and renames of panes beside
        # a second clause, kinds its standard had turned down, and it resized
        # a pane the wrong way ("rename this tab to api and grow this pane
        # lower"; the fourth review).
        second = None
        if unshown:
            kind = calls[0]["name"] if len(calls) == 1 else COMPOUND
            if kind not in bundle.second_kinds or any(t not in bundle.second_kinds for t in unshown_tools):
                failed.add(UNSHOWN)
            else:
                second = next((why for why in unshown if why), None) or \
                    (SECOND_RISKY if any(map(risky, calls)) else None)
                if second:
                    failed.add(UNSHOWN)
        for reason in _HOLDS:
            if reason in failed:
                return [], (second or reason) if reason == UNSHOWN else reason
        if texts is not None:
            texts[:] = [request[start:end] for start, end in places]
        return calls, OK_SECOND if unshown else OK

    def second(self, tool: str, frame: list[str], closed: dict, words: list[str], before: str) -> str | None:
        """Whether the second reader may answer a clause that is no training
        clause of its tool: None if it may, else why not. Every other hold of
        the reader still holds, and it answers no call that closes, types or
        starts a program (read, above); beside them:
        - every run of three neighbours of its frame (marks and words, from
          its start to its end: runs) stood side by side in a training clause
          of the same tool that stood first or alone, or that followed a
          clause of the tool this one follows;
        - its values from closed lists are those of some training clause of
          the tool, all together;
        - a side, a place, a layout or a direction it gives is named by a
          word of the clause: by itself, or by a word that named it in
          training ("top" for above). "current" needs no word."""
        bundle = self.bundle
        held = bundle.frame_runs(tool)
        for run in runs(frame):
            if f"\t{run}" not in held and not (before and f"{before}\t{run}" in held):
                return SECOND_RUN
        if "".join(f"\t{key}={closed[key]}" for key in sorted(closed)) not in bundle.closed_sets.get(tool, ()):
            return SECOND_SET
        for value in closed.values():
            if value in bundle.said and not any(stands_for(word, [value], bundle.value_words.get(word, ()))
                                                for word in words):
                return SECOND_SAID
        return None


class Bundle:
    """The file and its members. It answers only when all members agree."""

    def __init__(self, raw: bytes, manifest: dict | None = None):
        magic = raw[:len(MAGIC)]
        if magic != MAGIC and magic not in OLDER:
            raise BundleError("not a panes bundle file")
        size = int.from_bytes(raw[len(MAGIC):len(MAGIC) + 4], "little")
        start = len(MAGIC) + 4
        try:
            head = json.loads(raw[start:start + size].decode("utf-8"))
        except ValueError as error:
            raise BundleError(f"the bundle header is unreadable: {error}") from None
        want = SCHEMA if magic == MAGIC else OLDER[magic][0]
        if not isinstance(head, dict) or head.get("schema") != want:
            raise BundleError(f"unknown bundle schema {head.get('schema') if isinstance(head, dict) else head!r}")
        # The job whose requests it reads: a v5 file is a panes bundle.
        self.job = head.get("job") if magic == MAGIC else "panes"
        if self.job not in JOBS:
            raise BundleError(f"the bundle is for no job this reader knows ({self.job!r})")
        self.raw, self.head, self.body = raw, head, start + size
        self.manifest = manifest or {}
        try:
            self.known = frozenset(head["known"])
            self.tools = head["tools"]
            self.free_text = frozenset(head.get("free_text", ()))
            # {word: the values it is another word for}: the words that named a
            # value of the answer in every training row that asked for an
            # action, and the words that alone named a value of a closed list
            # (a side, a place, a layout, a direction) in some row.
            self.value_words = dict(head["value_words"])
            # {argument: the words that were copied for an argument of that
            # name in training}: what a name, a title or a command may be
            # made of beside words new to the bundle.
            self.copied = {key: frozenset(words) for key, words in head["copied"].items()}
            # The words that joined two clauses in training, and the largest
            # number of clauses in a training row.
            self.separators = frozenset(head["separators"])
            self.clauses_max = int(head["clauses_max"])
            # {tool: {argument: the most words copied for it in a training row}}
            self.copy_max = {tool: {key: int(n) for key, n in most.items()}
                             for tool, most in head["copy_max"].items()}
            # The second reader's ground: for each tool where the runs of
            # neighbours of its training frames are, the values from closed
            # lists its training clauses had together, and the values that
            # a word of the clause must name.
            self.closed_sets = {tool: frozenset(lines) for tool, lines in head["closed_sets"].items()}
            self.said = frozenset(head["said"])
            if set(head["frame_runs"]) != set(self.tools):
                raise ValueError("the second reader's runs are not those of the bundle's tools")
            if head["frame_run"] != RUN:
                raise ValueError(f"the second reader's runs are of {head['frame_run']} neighbours, "
                                 f"and this reader holds to runs of {RUN}")
            # The kinds the second reader answers: none unless the manifest
            # names them, each one measured before it was.
            kinds = (self.manifest.get("second_reader") or {}).get("kinds", [])
            if not isinstance(kinds, list) or not all(k in self.tools or k == COMPOUND for k in kinds):
                raise ValueError("the manifest names kinds the second reader cannot answer")
            self.second_kinds = frozenset(kinds)
            self._names = self._pairs = None
            self._ids: dict = {}
            self._readings: dict = {}
            self._frame_runs: dict = {}
            self.members = [Member(self, entry) for entry in head["members"]]
            self._check_places()
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise BundleError(f"the bundle header is not as the reader expects ({type(error).__name__}: {error})") from None
        if len(self.members) < MEMBERS_MIN:
            raise BundleError(f"the bundle holds {len(self.members)} member(s): it answers when its members "
                              f"agree, and that takes {MEMBERS_MIN}")
        self.label = f"bundle {self.manifest.get('name', '')}".strip()

    def _check_places(self) -> None:
        """Everything the header points at lies inside the file. A file cut
        short is refused when it is loaded, not when a request reaches the
        missing part."""
        room, head = len(self.raw) - self.body, self.head
        dim, hidden = int(head["dim"]), int(head["hidden"])
        if dim < 1 or hidden < 1:
            raise BundleError("the nets of the bundle have no size")
        weights: list = [[] for _ in head["members"]]
        blocks = [head["names"], head["pairs"]] + [head["readings"][tool] for tool in self.tools] \
            + [head["frame_runs"][tool] for tool in self.tools]
        for at, size in blocks:
            if not 0 <= at <= at + size <= room:
                raise BundleError("a table of the bundle lies outside the file")
        for key, spec in head["nets"].items():
            at, count = spec["ids"]
            if not 0 <= at <= at + 4 * count <= room:
                raise BundleError("a net's features lie outside the file")
            floats = (count + 1) * dim + dim * hidden + hidden + hidden * len(spec["outputs"]) + len(spec["outputs"])
            for mine, member in zip(weights, head["members"]):
                at = member["weights"][key]
                if not 0 <= at <= at + 2 * floats <= room:
                    raise BundleError("a net's weights lie outside the file")
                mine.append((self.body + at, self.body + at + 2 * floats))
        # Members that are the same nets agree about anything, as one member
        # does with itself.
        view = memoryview(self.raw)
        for n, mine in enumerate(weights):
            for other in weights[:n]:
                if all(view[a:b] == view[c:d] for (a, b), (c, d) in zip(mine, other)):
                    raise BundleError("two members of the bundle are the same nets: their agreement says nothing")

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

    def readings(self, tool: str) -> frozenset:
        """Every training clause of one tool as it was labelled, each a line
        (reading_line): its frame and its values from the closed lists."""
        if tool not in self._readings:
            self._readings[tool] = frozenset(self._text(self.head["readings"][tool]))
        return self._readings[tool]

    def frame_runs(self, tool: str) -> frozenset:
        """Every run of neighbours of the training frames of one tool (runs),
        each a line: the tool of the clause before ("" for one that stood
        first or alone), a tab, and the marks or words of the run."""
        if tool not in self._frame_runs:
            self._frame_runs[tool] = frozenset(self._text(self.head["frame_runs"][tool]))
        return self._frame_runs[tool]

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
        # Neither file is read before it is known to be a regular file of a
        # size it can have: a pipe, a device or a file without end in either
        # place would hold the request up, and the model would never be asked.
        try:
            manifest = json.loads(_regular_file(directory / "manifest.json", "the bundle manifest",
                                                at_most=MANIFEST_MAX).decode("utf-8"))
        except (ValueError, RecursionError) as error:
            raise BundleError(f"the bundle manifest is unreadable: {type(error).__name__}") from None
        if not isinstance(manifest, dict) or manifest.get("schema") not in (
                MANIFEST_SCHEMA, *(older for _, older in OLDER.values())):
            raise BundleError("unknown bundle manifest schema")
        # The manifest names a file of its own directory, and how large it
        # is. A path is not followed out of the directory, and nothing is
        # read from a pipe, a device or a file of another size.
        name, size = manifest.get("file", FILE), manifest.get("bytes")
        if not isinstance(name, str) or name in ("", ".", "..") or "\0" in name \
                or any(mark and mark in name for mark in ("/", os.sep, os.altsep)):
            raise BundleError("manifest.json does not name a file of its own directory")
        if type(size) is not int or not 0 < size <= MAX_BYTES:
            raise BundleError("manifest.json states no size a bundle can have")
        raw = _regular_file(directory / name, "the bundle file", size=size)
        if hashlib.sha256(raw).hexdigest() != manifest.get("sha256"):
            raise BundleError("the bundle file does not match the digest in manifest.json")
        return cls(raw, manifest)

    def read(self, request: str, texts: list | None = None) -> tuple[list, str]:
        """(calls, reason), as Member.read; `texts` is set to each call's
        clause when calls are proposed."""
        clauses: list = []
        calls, reason = self.members[0].read(request, clauses)
        if not calls:
            return [], reason
        for member in self.members[1:]:
            other, why = member.read(request)
            if other != calls:
                return [], "the members do not agree"
            if why == OK_SECOND:
                reason = OK_SECOND
        if texts is not None:
            texts[:] = clauses
        return calls, reason


def _regular_file(path: Path, what: str, *, size: int | None = None, at_most: int | None = None) -> bytes:
    """The bytes of a regular file: of exactly `size` bytes, or of no more
    than `at_most`. What kind of file it is and how large is asked of the
    open file before anything is read, the open does not wait for a pipe's
    other end, and no more than the size is read."""
    try:
        handle = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except OSError as error:
        raise BundleError(f"{what} is unreadable: {error}") from None
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            raise BundleError(f"{what} is not a regular file")
        if size is None:
            size = info.st_size
            if size > at_most:
                raise BundleError(f"{what} is larger than one can be")
        elif info.st_size != size:
            raise BundleError(f"{what} does not have the size in manifest.json")
        parts, left = [], size
        while left:
            part = os.read(handle, left)
            if not part:
                raise BundleError(f"{what} ended before its size")
            parts.append(part)
            left -= len(part)
        return b"".join(parts)
    except OSError as error:
        raise BundleError(f"{what} is unreadable: {error}") from None
    finally:
        os.close(handle)


_LOADED: dict = {}


def directory(job: str) -> Path:
    """Where the bundle of a job is checked in."""
    if job not in JOBS:
        raise ValueError(f"no such job: {job!r}")
    return DIRECTORY if job == "panes" else JOB_DIRECTORY / job


def _not_used(error: Exception) -> None:
    print(f"kilix-needle: the panes bundle is not used ({type(error).__name__}: {error})", file=sys.stderr)
    _LOADED["bundle"] = None


def default() -> Bundle | None:
    """The checked-in bundle, loaded once; None when there is none to use.
    Whoever asks has switched the bundle on, so a bundle that is missing is
    said on stderr like one that is damaged."""
    if "bundle" not in _LOADED:
        try:
            _LOADED["bundle"] = Bundle.load()
        except Exception as error:      # whatever is wrong with the file, the model reads the request
            _not_used(error)
    return _LOADED["bundle"]


def read(request: str, texts: list | None = None) -> tuple[list, str] | None:
    """(calls, the bundle's label) for a request the checked-in bundle knows,
    or None. Whether it is asked at all is needle_cli's to say. `texts`, when
    given, is set to the text of the clause each call was read from.

    Nothing that goes wrong in the bundle may stop a request: a file that
    turns out damaged while it is read is given up for this process, and the
    model reads the request as it would have without the bundle."""
    bundle = default()
    if bundle is None or "\n" in request or "\r" in request:
        return None
    try:
        calls, reason = bundle.read(request, texts)
    except Exception as error:
        _not_used(error)
        return None
    if not calls:
        return None
    # What the second reader answers is recorded under a label of its own.
    return calls, bundle.label + (" (second reader)" if reason == OK_SECOND else "")


def main(argv: list[str]) -> int:
    if not argv:
        print('usage: python3 panes_bundle.py "split right and run htop"', file=sys.stderr)
        return 2
    try:
        bundle = Bundle.load()
    except BundleError as error:
        print(f"panes_bundle: {error}", file=sys.stderr)
        return 1
    for request in argv:
        calls, reason = bundle.read(request)
        print(json.dumps({"request": request, "calls": calls, "reason": reason}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
