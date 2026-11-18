"""The panes bundle: its file, what it declines to read, and its place before the model.

The nets here are made by hand (two table columns, four hidden units that pass
the summed rows through), so every test says exactly which word decides what.
Nothing is trained and PyTorch is not needed.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from support import FakeKilix, desktop
from actions import Action
import history
import needle_cli
import panes_bundle as PB
import panes_bundle_train as PBT
import panes_exact

TOOLS = {"close_pane": {"args": [["pane", "ref", None, None]], "required": ["pane"]},
         "go_to_tab": {"args": [["tab", "ref", None, None]], "required": ["tab"]},
         "move_tab": {"args": [["direction", "enum", None, None], ["position", "int", 1, 9]], "required": []},
         "rename_tab": {"args": [["name", "text", None, None], ["tab", "ref", None, None]], "required": ["name"]}}
KNOWN = {"move", "to", "the", "next", "tab", "this", "position", "rename", "shut", "pane", "left", "and", "htop",
         "build", "logs"}
PAIRS = {"<s>_move", "move_to", "to_the", "the_next", "next_tab", "tab_</s>",          # move to the next tab
         "move_this", "this_tab", "tab_to", "to_position", "position_<num>", "<num>_</s>",
         "<s>_rename", "rename_this", "to_<x>", "<x>_</s>",                            # rename this tab to X
         "<s>_shut", "shut_the", "the_left", "left_pane", "pane_</s>", "the_<x>", "<x>_pane",
         "the_htop", "htop_pane"}                                                      # shut the htop pane


def reading(words, before="", **closed):
    """A training clause as the file holds it: copied text is a mark for its
    argument, `closed` are its values from closed lists, `before` the tool of
    the clause it followed ("" when it stood first or alone)."""
    return PB.reading_line(before, words.split(), closed)


READINGS = {"go_to_tab": [reading("move to the next tab", tab="next")],
            "move_tab": [reading("move this tab to position <position#>")],
            "rename_tab": [reading("rename this tab to <name>")],
            "close_pane": [reading("shut the left pane", pane="left"), reading("shut the <pane> pane")]}
GROUND = {"readings": READINGS, "separators": ["and"], "clauses_max": 2,
          "copy_max": {"move_tab": {"position": 1}, "rename_tab": {"name": 2}, "close_pane": {"pane": 1}},
          "copied": {"pane": ["htop"], "name": ["build", "logs"]}}
RIGHT, UP, LEFT, DOWN = [4.0, 0.0], [0.0, 4.0], [-4.0, 0.0], [0.0, -4.0]   # one hidden unit each


LIFT = [0.0]        # added to every output of the nets being made: another member, the same answers


def net(outputs, features, rows, biases=None):
    """features: {name: table row}; rows: {output: its weight on each hidden unit}."""
    names = sorted(features)
    flat = [0.0, 0.0]                                       # the row no feature has
    for name in names:
        flat += features[name]
    flat += [1.0, 0.0, 0.0, 1.0, -1.0, 0.0, 0.0, -1.0]      # hidden: right, up, left, down
    flat += [0.0] * 4
    for output in outputs:
        flat += rows.get(output, [0.0] * 4)
    flat += [(biases or {}).get(output, 0.0) + LIFT[0] for output in outputs]
    return names, list(outputs), flat


def nets(tool_says_none=False, lift=0.0) -> dict:
    """One member's nets. `lift` makes another member of them: every output
    of every net is that much higher, so each net answers as before and no
    net is the same."""
    LIFT[0] = lift
    try:
        return _nets(tool_says_none)
    finally:
        LIFT[0] = 0.0


def _nets(tool_says_none) -> dict:
    first = [1.0, 0.0, 0.0, 0.0]
    return {
        "seg": net(["sep", "word"], {"w+0=and": RIGHT}, {"sep": first}, {"word": 1.0}),
        "tool": net([PB.NONE, "go_to_tab", "move_tab", "rename_tab", "close_pane"],
                    {"u=next": RIGHT, "u=position": UP, "u=rename": LEFT, "u=shut": DOWN},
                    {} if tool_says_none else
                    {"go_to_tab": first, "move_tab": [0.0, 1.0, 0.0, 0.0],
                     "rename_tab": [0.0, 0.0, 1.0, 0.0], "close_pane": [0.0, 0.0, 0.0, 1.0]},
                    {PB.NONE: 1.0}),
        "arg go_to_tab tab": net([PB.ABSENT, "next", PB.COPY], {"u=next": RIGHT}, {"next": first}, {PB.ABSENT: 1.0}),
        "arg move_tab position": net([PB.ABSENT, PB.COPY], {"u=<num>": RIGHT}, {PB.COPY: first}, {PB.ABSENT: 1.0}),
        "span move_tab position": net(["first", "last"], {"w+0=<num>": RIGHT}, {"first": first, "last": first}),
        # a name is any word the nets do not know, or "left", "build" or "logs"
        "arg rename_tab name": net([PB.ABSENT, PB.COPY],
                                   {"u=<x>": RIGHT, "u=left": RIGHT, "u=build": RIGHT, "u=logs": RIGHT},
                                   {PB.COPY: first}, {PB.ABSENT: 1.0}),
        "span rename_tab name": net(["first", "last"],
                                    {"w+0=<x>": RIGHT, "w+0=left": RIGHT, "w+0=build": RIGHT, "w+0=logs": RIGHT,
                                     "w+1=</s>": UP},
                                    {"first": first, "last": [1.0, 1.0, 0.0, 0.0]}),
        "arg close_pane pane": net([PB.ABSENT, "left", PB.COPY], {"u=left": RIGHT, "u=<x>": UP, "u=htop": UP},
                                   {"left": first, PB.COPY: [0.0, 1.0, 0.0, 0.0]}, {PB.ABSENT: 1.0}),
        "span close_pane pane": net(["first", "last"], {"w+0=<x>": RIGHT, "w+0=htop": RIGHT},
                                    {"first": first, "last": first}),
    }


def packed(*, pairs=PAIRS, free_text=(), members=2, split=False, value_words=None, ground=GROUND,
           made=None) -> bytes:
    """The bundle file. `split`: the second member finds no tool where the first does."""
    made = made or [(seed, nets(tool_says_none=split and seed == 1, lift=seed / 4)) for seed in range(members)]
    return PBT.pack(made, KNOWN, set(pairs), TOOLS, list(free_text), dim=2, hidden=4, value_words=value_words,
                    ground=ground)


def directory(case, raw, *, lie=False, **stated):
    """A bundle directory. `stated`: what the manifest says otherwise than is true."""
    folder = Path(tempfile.mkdtemp(prefix="kn-bundle-"))
    case.addCleanup(shutil.rmtree, folder, True)
    (folder / PB.FILE).write_bytes(raw)
    (folder / "manifest.json").write_text(json.dumps(
        {"schema": PB.MANIFEST_SCHEMA, "name": "by hand", "file": PB.FILE, "bytes": len(raw),
         "sha256": hashlib.sha256(raw + (b"x" if lie else b"")).hexdigest(), **stated}))
    return folder


def with_readings(**more):
    """GROUND with readings added to a tool's own."""
    return {**GROUND, "readings": {**READINGS, **{tool: READINGS[tool] + lines for tool, lines in more.items()}}}


def call(tool, **arguments):
    return {"name": tool, "arguments": arguments}


class Reads(unittest.TestCase):
    def setUp(self):
        self.member = PB.Bundle(packed())

    def test_a_value_from_a_closed_list(self):
        self.assertEqual(self.member.read("move to the next tab"), ([call("go_to_tab", tab="next")], "ok"))

    def test_copied_text_is_the_request_s_own_characters(self):
        self.assertEqual(self.member.read("rename this tab to Build-2")[0], [call("rename_tab", name="Build-2")])

    def test_a_word_that_always_named_a_value_is_in_the_answer(self):
        request, answer = "move to the next tab", [call("go_to_tab", tab="next")]
        # "next" is in the answer as itself; "move" as the value it is another word for
        for always in ({"next": []}, {"move": ["next"]}):
            self.assertEqual(PB.Bundle(packed(value_words=always)).read(request), (answer, "ok"))
        # "to" named a value in every training row, and this answer has no value from it
        self.assertEqual(PB.Bundle(packed(value_words={"to": []})).read(request), ([], PB.UNNAMED))
        # a number counts in any spelling
        three = PB.Bundle(packed(value_words={"3": []})).read("move this tab to position 3")
        self.assertEqual(three, ([call("move_tab", position=3)], "ok"))
        self.assertTrue(PB.stands_for("third", [3]) and PB.stands_for("3", ["3"]) and PB.stands_for("make", ["make test"]))
        self.assertFalse(PB.stands_for("lower", ["node"]) or PB.stands_for("true", [True]))
        # a tab's id is the number it was read from ("rename tab id 4 to api"), and no other
        self.assertTrue(PB.stands_for("4", ["id:4"]) and PB.stands_for("four", ["id:4"]))
        self.assertFalse(PB.stands_for("4", ["id:14"]) or PB.stands_for("4", ["pane:4"]) or PB.stands_for("id", ["id:4"]))

    def test_the_trainer_finds_the_words_that_always_named_a_value(self):
        rows = [{"query": "new lower pane with node", "spans": [["lower", "below"], ["node", "node"]],
                 "actions": [["open_pane", {"side": "below", "program": "node"}]]},
                {"query": "close the lower pane", "spans": [["lower", "below"]],
                 "actions": [["close_pane", {"pane": "below"}]]},
                {"query": "new pane", "spans": [], "actions": [["open_pane", {}]]},
                {"query": "quit node", "spans": [], "actions": []}]        # asks for nothing: not counted
        self.assertEqual(PBT.value_words(rows), {"lower": ["below"], "node": ["node"]})

    def test_what_to_close_is_named_only_by_trained_words(self):
        # A word never seen may be a title, a misspelt "left" or a "3rd": the
        # bundle cannot tell, so it does not name what to close by it.
        self.assertEqual(self.member.read("shut the htop pane")[0], [call("close_pane", pane="htop")])
        for word in ("Build-2", "letf", "3rd"):
            self.assertEqual(self.member.read(f"shut the {word} pane"),
                             ([], "an untrained word names what to close or type into"))
        self.assertEqual(set(PB.KNOWN_TARGET), {"close_pane", "close_tab", "run_in_pane"})

    def test_a_number_is_read_by_code_and_held_to_the_tool_s_range(self):
        self.assertEqual(self.member.read("move this tab to position 3")[0], [call("move_tab", position=3)])
        self.assertEqual(self.member.read("move this tab to position 12"),
                         ([], "position is not a number the tool takes"))

    def test_two_clauses_give_two_calls(self):
        calls, reason = self.member.read("rename this tab to api and move to the next tab")
        self.assertEqual((calls, reason), ([call("rename_tab", name="api"), call("go_to_tab", tab="next")], "ok"))

    def test_a_word_that_names_a_value_is_in_its_own_clause_s_call(self):
        # "htop" has to be in the answer. In "rename this htop tab to api" it
        # is passed over, and the other clause's call holding an "htop" of
        # its own does not make up for that (the second review, 1.3).
        knows = PB.Bundle(packed(pairs=PAIRS | {"this_htop", "htop_tab"}, value_words={"htop": []},
                                 ground=with_readings(rename_tab=[reading("rename this htop tab to <name>")])))
        self.assertEqual(knows.read("shut the htop pane")[0], [call("close_pane", pane="htop")])
        self.assertEqual(knows.read("rename this htop tab to api"), ([], PB.UNNAMED))
        self.assertEqual(knows.read("rename this htop tab to api and shut the htop pane"), ([], PB.UNNAMED))
        self.assertEqual(knows.read("rename this tab to api and shut the htop pane")[1], "ok")

    def test_a_pair_of_words_never_seen_together_is_declined(self):
        # every word is known, the order is not
        self.assertEqual(self.member.read("move to the tab next"), ([], "an untrained word pair"))
        # the unknown word would even be copied: it is still not known ground
        self.assertEqual(self.member.read("shut the left pane now"), ([], "an untrained word pair"))

    def test_wording_of_another_tool_is_declined(self):
        # Every word and every pair is known: "shut the" from closing a pane,
        # "the next tab" from going to a tab. The nets settle on go_to_tab and
        # pass over "shut". No go_to_tab clause in training began like that.
        request = "shut the next tab"
        self.assertEqual(self.member.read(request), ([], PB.UNSHOWN))
        shown = PB.Bundle(packed(ground=with_readings(go_to_tab=[reading("shut the next tab", tab="next")])))
        self.assertEqual(shown.read(request)[0], [call("go_to_tab", tab="next")])     # what the rule declines

    def test_a_clause_is_a_whole_training_clause_read_as_that_one_was(self):
        # Pairs of neighbours of this tool's own clauses are not enough (the
        # second review: a walk over them gave "not a tab"). Shown "rename
        # this tab to X" and "rename the tab X", every two neighbouring words
        # of "rename the tab to X" stood side by side in a rename, and it is
        # neither clause.
        two = PB.Bundle(packed(pairs=PAIRS | {"rename_the", "the_tab", "tab_<x>"},
                               ground=with_readings(rename_tab=[reading("rename the tab <name>")])))
        self.assertEqual(two.read("rename this tab to api")[1], "ok")
        self.assertEqual(two.read("rename the tab api")[1], "ok")
        self.assertEqual(two.read("rename the tab to api"), ([], PB.UNSHOWN))
        # the same words read to another value are another reading
        other = PB.Bundle(packed(ground={**GROUND, "readings": {**READINGS, "go_to_tab": [
            reading("move to the next tab", tab="previous")]}}))
        self.assertEqual(other.read("move to the next tab"), ([], PB.UNSHOWN))
        self.assertEqual(reading("shut <pane>"), "\tshut <pane>")
        self.assertEqual(reading("go to it", before="open_pane", pane="below"), "open_pane\tgo to it\tpane=below")
        self.assertEqual(PB.reading_line("", ["a"], {"side": "left", "restore": "true"}), "\ta\trestore=true\tside=left")
        # a word spelt like a mark is never taken for one
        self.assertEqual(self.member.read("rename this tab to <name>")[0], [])
        self.assertEqual(self.member.read("shut the <pane> pane")[0], [])

    def test_a_clause_that_only_followed_another_is_read_only_after_that_tool(self):
        # "move to the next tab" was only ever a second clause here, after a
        # rename: alone it leans on something that is not there.
        leans = PB.Bundle(packed(ground={**GROUND, "readings": {**READINGS, "go_to_tab": [
            reading("move to the next tab", before="rename_tab", tab="next")]}}))
        self.assertEqual(leans.read("move to the next tab"), ([], PB.UNSHOWN))
        self.assertEqual(leans.read("rename this tab to api and move to the next tab")[1], "ok")
        self.assertEqual(leans.read("shut the left pane and move to the next tab"), ([], PB.UNSHOWN))
        # one that stood first says all it means by itself, and may stand second
        self.assertEqual(self.member.read("shut the left pane and rename this tab to api")[1], "ok")

    def test_a_clause_is_framed_as_its_answer_reads_it(self):
        self.assertEqual(PB.frame_of(["go", "to", "tab", "two"], {"tab": (3, 3, True)}), ["go", "to", "tab", "<tab#>"])
        self.assertEqual(PB.frame_of(["the", "tab", "named", "two"], {"tab": (3, 3, False)}),
                         ["the", "tab", "named", "<tab>"])
        # copied text of any length is one mark
        self.assertEqual(PB.frame_of(["rename", "it", "to", "build", "logs", "now"], {"name": (3, 4, False)}),
                         ["rename", "it", "to", "<name>", "now"])
        # every occurrence of a copied text is its mark ...
        self.assertEqual(PB.frame_of("name this tab api , it's where the api stuff lives".split(), {"name": (3, 3, False)}),
                         "name this tab <name> , it's where the <name> stuff lives".split())
        # ... the longest text first, so a title that is also a word of the
        # command is the title where it stands alone, wherever it was found
        for pane_at in (2, 5):
            self.assertEqual(PB.frame_of("run cargo build in the build pane".split(),
                                         {"pane": (pane_at, pane_at, False), "command": (1, 2, False)}),
                             "run <command> in the <pane> pane".split())
        # copied text is a mark, so any title stands where a title stood ...
        self.assertEqual(self.member.read("shut the htop pane")[0], [call("close_pane", pane="htop")])
        # ... and a number is not a title: position 3 is framed as a number
        self.assertEqual(self.member.read("move this tab to position 3")[0], [call("move_tab", position=3)])
        no_number = {**READINGS, "move_tab": [reading("move this tab to position <position>")]}
        self.assertEqual(PB.Bundle(packed(ground={**GROUND, "readings": no_number})).read("move this tab to position 3"),
                         ([], PB.UNSHOWN))

    def test_only_a_word_that_joined_two_clauses_in_training_joins_two(self):
        request = "rename this tab to api and move to the next tab"
        self.assertEqual(len(self.member.read(request)[0]), 2)
        # the same net, the same sentence: "and" never joined two clauses in this one's training
        other = PB.Bundle(packed(ground={**GROUND, "separators": ["then"]}))
        self.assertEqual(other.read(request), ([], "clauses joined by a word that never joined two in training"))

    def test_no_more_clauses_than_a_training_row_had(self):
        two = "rename this tab to api and move to the next tab"
        self.assertEqual(self.member.read(two + " and move to the next tab"),
                         ([], "more clauses than any training row had"))
        one = PB.Bundle(packed(ground={**GROUND, "clauses_max": 1}))
        self.assertEqual(one.read(two), ([], "more clauses than any training row had"))
        self.assertEqual(one.read("move to the next tab")[0], [call("go_to_tab", tab="next")])

    def test_a_rename_that_does_not_say_what_it_renames_follows_its_own_kind(self):
        # "rename this to api" is a tab's rename read alone, and after a
        # clause about a tab. After a clause about a pane, "this" is the
        # pane, and the reading would rename the tab.
        knows = PB.Bundle(packed(pairs=PAIRS | {"this_to"},
                                 ground=with_readings(rename_tab=[reading("rename this to <name>")])))
        rename = call("rename_tab", name="api")
        self.assertEqual(knows.read("rename this to api"), ([rename], "ok"))
        self.assertEqual(knows.read("move this tab to position 3 and rename this to api"),
                         ([call("move_tab", position=3), rename], "ok"))
        self.assertEqual(knows.read("shut the left pane and rename this to api"), ([], PB.CARRIED))
        # said in so many words, it is read wherever it stands
        self.assertEqual(knows.read("shut the left pane and rename this tab to api"),
                         ([call("close_pane", pane="left"), rename], "ok"))
        # a title is not the word: "rename this to tab" names nothing to rename
        self.assertEqual(knows.read("shut the left pane and rename this to tab")[0], [])
        self.assertEqual([PB.acts_on(tool) for tool in ("go_to_tab", "rename_tab", "run_in_pane", "arrange_panes")],
                         ["tab", "tab", "pane", "pane"])

    def test_no_clause_goes_by_what_the_clause_before_it_changes(self):
        # Every call is resolved before the first one runs. After a move to
        # another tab, "this tab" is that tab, and the route would rename the
        # one the request came from (the second review, 1.2).
        self.assertEqual(self.member.read("move to the next tab and rename this tab to api"), ([], PB.LEANS))
        # the other way round nothing leans: the rename changes no tab's place
        self.assertEqual(self.member.read("rename this tab to api and move to the next tab")[1], "ok")
        # what a call changes ...
        for changed, calls in (
                ({"pane", "tab", "focus"}, [call("go_to_tab", tab="2"), call("go_to_pane", pane="logs")]),
                ({"pane", "focus"}, [call("go_to_pane", pane="left")]),
                ({"pane"}, [call("resize_pane", pane="left", direction="wider")]),
                ({"pane", "sides", "focus"}, [call("open_pane", side="right")]),
                # a pane on a side of the caller's is in the caller's tab, and is not the caller's
                ({"pane", "sides"}, [call("close_pane", pane="left"), call("close_pane", pane="next")]),
                # any other may be the last of its tab, and may be the one the request is made from (the third
                # review: "close the notes pane and move this tab to position 1")
                ({"pane", "sides", "order", "caller"}, [call("close_pane", pane="notes"), call("close_pane", pane="70")]),
                ({"pane", "sides", "tab", "order", "caller"}, [call("close_pane", pane="current")]),
                ({"pane", "tab", "order", "caller"}, [call("close_tab", tab="2"), call("close_tab", tab="current"),
                                                      call("close_tab", tab="last"), call("close_tab", tab="logs")]),
                ({"pane", "tab", "order"}, [call("close_tab", tab="next"), call("close_tab", tab="previous")]),
                ({"pane", "tab", "order", "focus"}, [call("open_tab")]),
                # whether a tab shows one pane at full size (the third review: "maximize this pane and restore
                # the layout" restored nothing)
                ({"sides", "layout"}, [call("arrange_panes", layout="grid"), call("maximize_pane"),
                                       call("maximize_pane", restore=True)]),
                ({"pane", "sides", "layout", "focus"}, [call("maximize_pane", pane="left")]),
                ({"pane", "sides", "tab", "layout", "focus"}, [call("maximize_pane", pane="70")]),
                ({"sides"}, [call("swap_panes", side="left")]),
                ({"order"}, [call("move_tab", direction="left")]),
                ({"title api"}, [call("rename_tab", name="API"), call("rename_pane", name="api")]),
                ({"title build", "title logs"}, [call("rename_tab", name="Build  logs")]),
                ({"pane", "tab", "order", "focus", "title logs"}, [call("open_tab", name="logs")]),
                # a program that is started and a command that is typed are what the pane is called then
                ({"pane", "sides", "focus", "title htop"}, [call("open_pane", side="right", program="htop")]),
                ({"title make", "title test"}, [call("run_in_pane", pane="current", command="make test")]),
                # a pane that is renamed or typed into is no longer called what it was found by (the third
                # review's sweep: "call the build pane api and close the build pane")
                ({"pane", "title api", "title build"}, [call("rename_pane", pane="build", name="api")]),
                ({"pane", "title ls", "title my", "title notes"}, [call("run_in_pane", pane="my notes", command="ls")]),
                ({"pane", "title api"}, [call("rename_pane", pane="left", name="api"),
                                         call("rename_pane", pane="70", name="api")]),
                (set(), [call("resize_pane", direction="wider")])):
            for one in calls:
                self.assertEqual(PB.changes(one), changed, one)
        # ... and what a call is resolved by. Every call goes by the caller: it is sent from the caller's pane.
        for needed, refs, calls in (
                ({"tab"}, ["tab"], [call("rename_tab", name="api"), call("close_tab", tab="current")]),
                ({"pane"}, ["pane"], [call("resize_pane", direction="wider"), call("rename_pane", name="api"),
                                      call("close_pane", pane="current")]),
                ({"pane", "layout"}, ["pane"], [call("maximize_pane"), call("maximize_pane", restore=True)]),
                ({"pane"}, [], [call("open_pane", side="right")]),
                ({"pane", "sides"}, ["pane"], [call("go_to_pane", pane="left"), call("go_to_pane", pane="next")]),
                # a swap and a move of a tab are made through the caller's pane and leave the focus there
                ({"pane", "sides", "focus"}, [], [call("swap_panes", side="left")]),
                ({"tab", "order", "focus"}, [], [call("move_tab", direction="left")]),
                ({"tab", "order"}, ["tab"], [call("go_to_tab", tab="next"), call("go_to_tab", tab="last")]),
                ({"tab"}, [], [call("arrange_panes", layout="grid")]),
                ({"order"}, ["tab"], [call("go_to_tab", tab="2")]),
                ({"title logs"}, ["tab"], [call("go_to_tab", tab="Logs")]),
                # a pane with a title is looked for in the caller's tab first
                ({"title build", "title logs", "tab"}, ["pane"], [call("close_pane", pane="build logs")]),
                (set(), ["pane"], [call("close_pane", pane="70")]),
                (set(), [], [call("open_tab")])):
            for one in calls:
                self.assertEqual(PB.goes_by(one, refs), needed | {"caller"}, one)
        # After a close of a pane that may be the caller's own, nothing: the second call may never be sent.
        self.assertEqual(self.member.read("shut the htop pane and move to the next tab"), ([], PB.LEANS))
        # A pane to the left of the caller's is not the caller's, and its tab stays.
        self.assertEqual(self.member.read("shut the left pane and move to the next tab"),
                         ([call("close_pane", pane="left"), call("go_to_tab", tab="next")], "ok"))

    def test_the_trainer_finds_each_tool_s_readings(self):
        tools = {"go_to_tab": {"args": [["tab", "ref", None, None]], "required": ["tab"]},
                 "rename_tab": {"args": [["name", "text", None, None], ["tab", "ref", None, None]], "required": ["name"]}}
        rows = [{"query": "go to tab two", "actions": [["go_to_tab", {"tab": "2"}]], "spans": [["two", "2"]]},
                {"query": "go to the tab named logs", "actions": [["go_to_tab", {"tab": "logs"}]],
                 "spans": [["logs", "logs"]]},
                {"query": "go to the next tab", "actions": [["go_to_tab", {"tab": "next"}]], "spans": [["next", "next"]]},
                {"query": "go to tab 3, then rename this tab to build logs",
                 "actions": [["go_to_tab", {"tab": "3"}], ["rename_tab", {"name": "build logs"}]],
                 "spans": [["3", "3"], ["build logs", "build logs"]]},
                # the title is not in the request: this clause has no reading
                {"query": "go to my usual tab", "actions": [["go_to_tab", {"tab": "logs"}]], "spans": []}]
        ground = PBT.ground([PBT.prepare(row, tools) for row in rows])
        self.assertEqual(ground["readings"]["go_to_tab"], sorted([
            reading("go to tab <tab#>"), reading("go to the tab named <tab>"), reading("go to the next tab", tab="next")]))
        # the rename stood after a clause that goes to a tab, and nowhere else
        self.assertEqual(ground["readings"]["rename_tab"], [reading("rename this tab to <name>", before="go_to_tab")])
        self.assertEqual((ground["separators"], ground["clauses_max"]), ([",", "then"], 2))
        self.assertEqual(ground["copy_max"], {"go_to_tab": {"tab": 1}, "rename_tab": {"name": 2}})
        # the words that were copied as text for each argument; a number is not text
        self.assertEqual(ground["copied"], {"name": ["build", "logs"], "tab": ["logs"]})

    def test_a_copy_is_no_longer_than_any_in_training(self):
        free = PB.Bundle(packed(free_text=["rename_tab"]))
        self.assertEqual(free.read("rename this tab to build logs")[0], [call("rename_tab", name="build logs")])
        # three words where two were the most: the rest of a sentence, not a title
        self.assertEqual(free.read("rename this tab to build logs now"),
                         ([], "a copied text longer than any in training"))
        one = PB.Bundle(packed(free_text=["rename_tab"], ground={**GROUND, "copy_max": {"rename_tab": {"name": 1}}}))
        self.assertEqual(one.read("rename this tab to build logs"), ([], "a copied text longer than any in training"))
        self.assertEqual(one.read("rename this tab to api")[0], [call("rename_tab", name="api")])

    def test_a_copied_text_is_not_made_of_wording(self):
        # "left" is a word the bundle knows and never saw copied as a name:
        # "rename this tab to left" may be a name, and "rename this tab
        # please" is not one (the second review, 1.8). The model reads both.
        free = PB.Bundle(packed(free_text=["rename_tab"]))
        self.assertEqual(free.read("rename this tab to left"), ([], PB.PLAIN))
        self.assertEqual(free.read("rename this tab to Build left"), ([], PB.PLAIN))
        copied = PB.Bundle(packed(free_text=["rename_tab"], ground={**GROUND, "copied": {"name": ["left"]}}))
        self.assertEqual(copied.read("rename this tab to left")[0], [call("rename_tab", name="left")])
        # a word new to the bundle is a name where it stands alone
        self.assertEqual(free.read("rename this tab to Q3")[0], [call("rename_tab", name="Q3")])
        # copied as a pane's title is not copied as a name
        self.assertEqual(self.member.read("shut the htop pane")[0], [call("close_pane", pane="htop")])
        self.assertEqual(PB.Bundle(packed(ground={**GROUND, "copied": {"name": ["htop"]}})).read("shut the htop pane"),
                         ([], PB.PLAIN))

    def test_a_new_word_is_a_name_only_alone(self):
        # The third review: "rename this tab to api thx" named the tab "api
        # thx", and "uh api" and "api pronto" were names too. A word the
        # bundle has never seen may be a name or wording it does not know;
        # beside another word it is not taken for part of a name.
        free = PB.Bundle(packed(free_text=["rename_tab"]))
        for name in ("api thx", "uh api", "build thx", "thx build", "Build  thx"):
            self.assertEqual(free.read("rename this tab to " + name), ([], PB.MIXED), name)
        # words that were copied as names in training make a name of two
        self.assertEqual(free.read("rename this tab to build logs")[0], [call("rename_tab", name="build logs")])
        # one token is one word, whatever marks it holds
        for name in ("api-server", "v1.2", "a/b", "my_project"):
            self.assertEqual(free.read("rename this tab to " + name)[0], [call("rename_tab", name=name)], name)
        # What is left: a new word standing alone where a name stands is the
        # name. "thx" names the tab here, and no rule of this reader can tell
        # it from "api".
        self.assertEqual(free.read("rename this tab to thx")[0], [call("rename_tab", name="thx")])

    def test_a_copied_text_does_not_end_in_the_mark_that_ended_the_sentence(self):
        free = PB.Bundle(packed(free_text=["rename_tab"]))
        self.assertEqual(free.read("rename this tab to api!"), ([], PB.CLOSED))
        self.assertEqual(free.read("rename this tab to api")[0], [call("rename_tab", name="api")])
        # a mark inside the text is part of it
        self.assertEqual(free.read("rename this tab to v1.2")[0], [call("rename_tab", name="v1.2")])

    def test_a_number_written_with_digits_and_an_ordinal_ending(self):
        self.assertEqual([PB.number_of(text) for text in ("3rd", "1st", "22ND", "4th", "third", "3", "three")],
                         [3, 1, 22, 4, 3, 3, 3])
        self.assertEqual([PB.number_of(text) for text in ("rd", "3x", "x3rd", "3rdx", "", "build")], [None] * 6)
        # the nets do not see it as a number: their features are as trained
        self.assertIsNone(PB.as_int("3rd"))
        self.assertEqual(PB.forms_of(["tab", "3rd", "3"], {"tab"}), ["tab", "<x>", "<num>"])
        # in the answer it counts as the number it spells
        self.assertTrue(PB.stands_for("3rd", ["3"]) and PB.stands_for("3rd", [3]))
        self.assertFalse(PB.stands_for("3rd", ["4"]))

    def test_the_trainer_finds_the_words_that_alone_named_a_closed_value(self):
        rows = [{"query": "new pane on top", "spans": [["on top", "above"]], "actions": [["open_pane", {"side": "above"}]]},
                {"query": "close the top pane", "spans": [["top", "above"]], "actions": [["close_pane", {"pane": "above"}]]},
                {"query": "one above another", "spans": [["one above another", "horizontal"]],
                 "actions": [["arrange_panes", {"layout": "horizontal"}]]},
                {"query": "call it top", "spans": [["top", "top"]], "actions": [["rename_pane", {"name": "top"}]]}]
        closed = PBT.closed_words(rows)
        # "top" alone named a place; "on" and "one" never did alone
        self.assertEqual(closed["top"], ["above", "top"])
        self.assertNotIn("on", closed)
        self.assertNotIn("one", closed)
        # every closed value is in the table as itself, with what its phrases named
        self.assertEqual(closed["above"], ["horizontal"])
        self.assertIn("left", closed)
        self.assertNotIn("current", closed)
        # so "above" in "one above another" is in an answer that names rows, and nowhere else
        self.assertTrue(PB.stands_for("above", ["horizontal"], closed["above"]))
        self.assertTrue(PB.stands_for("above", ["above"], closed["above"]))
        self.assertFalse(PB.stands_for("above", ["wider"], closed["above"]))

    def test_an_unknown_word_that_is_not_copied_is_declined(self):
        loose = PB.Bundle(packed(pairs=PAIRS | {"tab_<x>"}))
        self.assertEqual(loose.read("move to the next tab quickly"), ([], PB.WORD))

    def test_a_missing_required_argument_is_declined(self):
        self.assertEqual(self.member.read("shut the pane"), ([], "a required argument is missing"))

    def test_no_tool_means_no_calls(self):
        self.assertEqual(self.member.read("what time is it")[0], [])
        self.assertEqual(self.member.read("")[0], [])

    def test_a_number_is_only_a_place(self):
        # Which number it is is read by code. A number the training rows had
        # as wording ("kill process 20") stood as itself, and "close pane 20"
        # was a pair of words never seen side by side where "close pane 21" was.
        self.assertEqual(PB.forms_of(["close", "pane", "20", "x"], {"close", "pane", "20"}),
                         ["close", "pane", "<num>", "<x>"])
        # one that stood as wording in training may stand as wording again
        known = PB.Bundle(packed(pairs=PAIRS | {"next_<num>", "<num>_tab"}))
        known.known = known.known | {"2"}
        for member in known.members:
            member.known = known.known
        self.assertEqual(known.read("move to the next 2 tab")[1], PB.UNSHOWN)
        self.assertEqual(self.member.read("move to the next 2 tab")[1], PB.PAIR)

    def test_a_tab_s_number_after_id_is_its_id(self):
        words = "rename tab id 4 to api".split()
        self.assertTrue(PB._after_id(words, 3))
        self.assertTrue(PB._after_id("go to tab id : 14".split(), 5))
        self.assertFalse(PB._after_id("go to tab 14".split(), 3))
        self.assertFalse(PB._after_id(["4"], 0))

    def test_new_text_inside_a_copy_only_for_a_tool_listed_for_it(self):
        request = "rename this tab to build logs"
        self.assertEqual(self.member.read(request), ([], "an untrained word pair"))
        free = PB.Bundle(packed(free_text=["rename_tab"]))
        self.assertEqual(free.read(request)[0], [call("rename_tab", name="build logs")])
        # and never for a pair outside the copied text
        self.assertEqual(free.read("rename this next tab to build logs")[0], [])


def frame_lines(words, before=""):
    """The second reader's lines for one training frame (panes_bundle_train.ground)."""
    return [f"{before}\t{run}" for run in PB.runs(words.split())]


class SecondReader(unittest.TestCase):
    """A clause that is not a training clause of its tool, answered only for
    a kind the manifest switches on, and only on the second reader's ground."""

    # "rename the tab to <name>" is no training clause; its runs stood in some
    GROUND2 = {**GROUND,
               "frame_runs": {"rename_tab": frame_lines("rename this tab to <name>")
                              + ["\t<s> rename the", "\trename the tab", "\tthe tab to"],
                              "go_to_tab": frame_lines("move to the next tab") + frame_lines("move to the tab")},
               "closed_sets": {"rename_tab": [""], "go_to_tab": ["\ttab=next"]},
               "said": ["next", "previous"]}
    PAIRS2 = PAIRS | {"rename_the", "the_tab"}

    def test_the_runs_of_a_frame(self):
        self.assertEqual(PB.RUN, 3)
        self.assertEqual(PB.runs(["<layout>"]), ["<s> <layout> </s>"])
        self.assertEqual(PB.runs("go left".split()), ["<s> go left", "go left </s>"])

    def bundle(self, kinds=None, ground=None):
        raw = packed(pairs=self.PAIRS2, ground=ground or self.GROUND2)
        return PB.Bundle(raw, None if kinds is None else {"name": "by hand", "second_reader": {"kinds": kinds}})

    def test_off_unless_the_manifest_names_the_kind(self):
        request = "rename the tab to api"
        self.assertEqual(self.bundle().read(request), ([], PB.UNSHOWN))
        self.assertEqual(self.bundle(["go_to_tab", "compound"]).read(request), ([], PB.UNSHOWN))
        self.assertEqual(self.bundle(["rename_tab"]).read(request), ([call("rename_tab", name="api")], PB.OK_SECOND))
        # what the first reader answers is answered as before
        self.assertEqual(self.bundle(["rename_tab"]).read("rename this tab to api"),
                         ([call("rename_tab", name="api")], PB.OK))

    def test_its_answers_have_a_label_of_their_own(self):
        saved = dict(PB._LOADED)
        self.addCleanup(lambda: (PB._LOADED.clear(), PB._LOADED.update(saved)))
        PB._LOADED["bundle"] = self.bundle(["rename_tab"])
        self.assertEqual(PB.read("rename the tab to api"), ([call("rename_tab", name="api")], "bundle by hand (second reader)"))
        self.assertEqual(PB.read("rename this tab to api"), ([call("rename_tab", name="api")], "bundle by hand"))

    def test_every_run_of_three_neighbours_stood_in_a_frame_of_the_tool(self):
        runs = frame_lines("rename this tab to <name>") + ["\t<s> rename the", "\trename the tab"]
        ground = {**self.GROUND2, "frame_runs": {**self.GROUND2["frame_runs"], "rename_tab": runs}}
        self.assertEqual(self.bundle(["rename_tab"], ground).read("rename the tab to api"), ([], PB.SECOND_RUN))
        # a run that only followed another tool's clause serves only after a clause of that tool
        ground = {**self.GROUND2, "frame_runs": {**self.GROUND2["frame_runs"],
                                                  "rename_tab": runs + ["close_pane\tthe tab to"]}}
        self.assertEqual(self.bundle(["rename_tab"], ground).read("rename the tab to api"), ([], PB.SECOND_RUN))
        member = self.bundle(["rename_tab"], ground).members[0]
        frame = "rename the tab to <name>".split()
        self.assertEqual(member.second("rename_tab", frame, {}, frame, "close_pane"), None)
        self.assertEqual(member.second("rename_tab", frame, {}, frame, "go_to_tab"), PB.SECOND_RUN)

    def test_pairs_of_neighbours_are_not_enough(self):
        # Every pair of "rename the tab to <name>" stood in one of these two
        # frames ("the tab" in the second, "tab to" in the first), and "the
        # tab to" in neither: a walk over pairs is how "not a new tab,
        # thanks" opened a tab.
        ground = {**self.GROUND2, "frame_runs": {**self.GROUND2["frame_runs"], "rename_tab":
                                                  frame_lines("rename this tab to <name>")
                                                  + frame_lines("rename the tab <name>")}}
        member = self.bundle(["rename_tab"], ground).members[0]
        frame = "rename the tab to <name>".split()
        self.assertEqual(member.second("rename_tab", frame, {}, frame, ""), PB.SECOND_RUN)

    def test_never_a_call_that_closes_types_or_starts_a_program(self):
        # "shut the <pane> pane" is no training clause here; its runs stood in one
        ground = {**GROUND, "readings": {**READINGS, "close_pane": [reading("shut the left pane", pane="left")]},
                  "frame_runs": {"close_pane": frame_lines("shut the <pane> pane")}, "closed_sets": {"close_pane": [""]}}
        bundle = PB.Bundle(packed(ground=ground), {"second_reader": {"kinds": ["close_pane"]}})
        self.assertEqual(bundle.read("shut the htop pane"), ([], PB.SECOND_RISKY))
        # the first reader answers its training clauses as before
        self.assertEqual(bundle.read("shut the left pane"), ([call("close_pane", pane="left")], PB.OK))

    def test_what_is_risky_is_what_the_checks_hold_for_a_yes(self):
        import actions
        for name in sorted(PBT.schema()):
            if name == PB.LIST:
                continue
            for arguments in ({}, {"program": "htop"}, {"program": ""}, {"command": "ls"}, {"pane": "left"}):
                self.assertEqual(PB.risky(call(name, **arguments)), actions.Action(name, arguments).risky,
                                 (name, arguments))

    def test_its_closed_values_were_had_together_and_are_named(self):
        member = self.bundle(["go_to_tab"]).members[0]
        words = "move to the tab".split()
        # a value no word of the clause names
        self.assertEqual(member.second("go_to_tab", words, {"tab": "next"}, words, ""), PB.SECOND_SAID)
        # values no training clause of the tool had
        self.assertEqual(member.second("go_to_tab", words, {"tab": "previous"}, words, ""), PB.SECOND_SET)
        named = "move to the next tab".split()
        self.assertIsNone(member.second("go_to_tab", named, {"tab": "next"}, named, ""))

    def test_the_manifest_names_only_kinds_it_can_answer(self):
        raw = packed(pairs=self.PAIRS2, ground=self.GROUND2)
        for kinds in (["no_such_tool"], "rename_tab", [1]):
            with self.assertRaises(PB.BundleError, msg=kinds):
                PB.Bundle(raw, {"second_reader": {"kinds": kinds}})
        self.assertEqual(PB.Bundle(raw, {"second_reader": {"kinds": ["compound"]}}).second_kinds, {"compound"})

    def test_the_trainer_finds_the_second_reader_s_ground(self):
        rows = [{"query": "rename tab id 4 to api", "actions": [["rename_tab", {"tab": "id:4", "name": "api"}]],
                 "spans": [["4", "id:4"], ["api", "api"]]},
                {"query": "move to the next tab", "actions": [["go_to_tab", {"tab": "next"}]], "spans": []}]
        tools = PBT.schema()
        found = PBT.ground([PBT.prepare(row, tools) for row in rows])
        # a tab named by its id is a number copied from the request
        self.assertEqual(found["readings"]["rename_tab"], [PB.reading_line("", "rename tab id <tab#> to <name>".split(), {})])
        self.assertIn("\ttab id <tab#>", found["frame_runs"]["rename_tab"])
        self.assertEqual(found["closed_sets"], {"rename_tab": [""], "go_to_tab": ["\ttab=next"]})
        self.assertIn("next", found["said"])
        self.assertNotIn("current", found["said"])
        self.assertEqual(tools["list"], {"args": [["what", "enum", None, None], ["about", "ref", None, None]],
                                         "required": ["what"]})


class AgentRows(unittest.TestCase):
    def test_what_is_left_out_and_why(self):
        import panes_bundle_agent as agent
        made = agent.rows(0, 1)
        self.assertEqual(made, agent.rows(0, 1))                    # the same seed, the same rows
        tools = PBT.schema()
        one = next(row for row in made if row["actions"] and row["actions"][0][0] == "close_pane")
        # a row equal to a request of the history is left out and counted
        kept, stats = PBT.agent_rows(0, 1, [], tools, exclude={agent.fold(one["query"].upper() + " ")}, log=lambda *a: None)
        self.assertEqual(stats["same_as_a_history_request"], 1)
        self.assertNotIn(one["query"], [row["query"] for row in kept])
        # no copied word of what is kept is one the bundle will know only as wording
        known, held = PBT.wording(kept, tools)
        _, none_held = PBT.wording([], tools)
        for row in kept:
            p = PBT.prepare(row, tools)
            for clause in p["clauses"]:
                lo = clause["range"][0]
                for key, (a, b) in clause["spans"].items():
                    if key not in clause["numbers"]:
                        self.assertFalse(set(p["words"][lo + a:lo + b + 1]) & known, row["query"])
        # a pane named by its number is that number, as a proposal gives it; a list stands alone
        for row in kept:
            for kind, args in row["actions"]:
                self.assertFalse(str(args.get("pane", "")).startswith("id:"), row["query"])
            if any(kind == "list" for kind, _ in row["actions"]):
                self.assertEqual(len(row["actions"]), 1, row["query"])
        self.assertGreater(stats["kept"], 0.8 * stats["generated"])


class Artifact(unittest.TestCase):
    def test_it_loads_and_names_itself(self):
        bundle = PB.Bundle.load(directory(self, packed()))
        self.assertEqual(bundle.label, "bundle by hand")
        self.assertEqual(bundle.read("move to the next tab")[0], [call("go_to_tab", tab="next")])

    def test_a_file_that_is_not_the_one_in_the_manifest_is_refused(self):
        with self.assertRaisesRegex(PB.BundleError, "does not match the digest"):
            PB.Bundle.load(directory(self, packed(), lie=True))

    def test_a_damaged_or_foreign_file_is_refused(self):
        with self.assertRaisesRegex(PB.BundleError, "not a panes bundle"):
            PB.Bundle.load(directory(self, b"something else"))
        raw = packed()
        size = int.from_bytes(raw[len(PB.MAGIC):len(PB.MAGIC) + 4], "little")
        # The header whole and the rest cut short, with a manifest that names
        # the cut file: refused when it is loaded, before any request.
        for kept in (0, 40, len(raw) - len(PB.MAGIC) - 4 - size - 6):
            cut = raw[:len(PB.MAGIC) + 4 + size + kept]
            with self.assertRaisesRegex(PB.BundleError, "outside the file"):
                PB.Bundle.load(directory(self, cut))

    def test_a_header_that_lacks_what_the_reader_goes_by_is_refused(self):
        raw = packed()
        start = len(PB.MAGIC) + 4
        size = int.from_bytes(raw[len(PB.MAGIC):start], "little")
        head = json.loads(raw[start:start + size])
        # value_words among them: without that table "make the left pane
        # wider" would be answered again, and nothing would say so
        for missing in ("known", "tools", "separators", "clauses_max", "copy_max", "readings", "nets", "members",
                        "names", "pairs", "value_words", "copied", "frame_runs", "frame_run", "closed_sets", "said"):
            encoded = json.dumps({key: value for key, value in head.items() if key != missing}).encode()
            damaged = PB.MAGIC + len(encoded).to_bytes(4, "little") + encoded + raw[start + size:]
            with self.assertRaises(PB.BundleError, msg=missing):
                PB.Bundle.load(directory(self, damaged))
        # runs of another length than the reader holds to
        encoded = json.dumps({**head, "frame_run": PB.RUN - 1}).encode()
        damaged = PB.MAGIC + len(encoded).to_bytes(4, "little") + encoded + raw[start + size:]
        with self.assertRaisesRegex(PB.BundleError, "runs of"):
            PB.Bundle.load(directory(self, damaged))
        old = PB.MAGIC.replace(b"6", b"4") + raw[len(PB.MAGIC):]
        with self.assertRaisesRegex(PB.BundleError, "not a panes bundle"):
            PB.Bundle.load(directory(self, old))
        # a header for no job the reader knows
        encoded = json.dumps({**head, "job": "weather"}).encode()
        damaged = PB.MAGIC + len(encoded).to_bytes(4, "little") + encoded + raw[start + size:]
        with self.assertRaisesRegex(PB.BundleError, "no job"):
            PB.Bundle.load(directory(self, damaged))

    def test_a_file_of_the_format_before_is_a_panes_bundle(self):
        raw = packed()
        start = len(PB.MAGIC) + 4
        size = int.from_bytes(raw[len(PB.MAGIC):start], "little")
        head = json.loads(raw[start:start + size])
        self.assertEqual((head["job"], PB.Bundle(raw).job), ("panes", "panes"))
        (magic, (schema, _)), = PB.OLDER.items()
        del head["job"]
        encoded = json.dumps({**head, "schema": schema}).encode()
        old = magic + len(encoded).to_bytes(4, "little") + encoded + raw[start + size:]
        self.assertEqual(PB.Bundle(old).job, "panes")
        # a v5 header that names a job is still a panes bundle's
        encoded = json.dumps({**head, "schema": schema, "job": "tmux"}).encode()
        self.assertEqual(PB.Bundle(magic + len(encoded).to_bytes(4, "little") + encoded + raw[start + size:]).job,
                         "panes")

    def test_one_member_is_no_bundle(self):
        # It answers when its members agree, and one agrees with itself about anything.
        with self.assertRaisesRegex(PB.BundleError, "1 member"):
            PB.Bundle(packed(members=1))
        with self.assertRaisesRegex(PB.BundleError, "1 member"):
            PB.Bundle.load(directory(self, packed(members=1)))
        with self.assertRaisesRegex(SystemExit, "at least 2 different seeds"):
            PBT.write(Path(tempfile.gettempdir()) / "kn-never-written", [], {}, [0], [], "one")
        with self.assertRaisesRegex(SystemExit, "at least 2 different seeds"):
            PBT.write(Path(tempfile.gettempdir()) / "kn-never-written", [], {}, [3, 3], [], "twice the same")

    def test_the_manifest_names_a_file_of_its_own_directory(self):
        # The second review: a path was followed, a pipe was waited on, and
        # the whole file was read before its size was looked at.
        raw = packed()
        elsewhere = directory(self, raw)
        for name in (str(elsewhere / PB.FILE), "../" + elsewhere.name + "/" + PB.FILE, "sub/" + PB.FILE, "", ".", "..",
                     7, None, ["bundle.bin"]):
            with self.assertRaisesRegex(PB.BundleError, "its own directory", msg=repr(name)):
                PB.Bundle.load(directory(self, raw, file=name))
        for size in (0, -1, PB.MAX_BYTES + 1, "12", None, True, 1.5):
            with self.assertRaisesRegex(PB.BundleError, "no size a bundle can have", msg=repr(size)):
                PB.Bundle.load(directory(self, raw, bytes=size))
        # another size than the manifest states: refused by its size, before a byte of it is read
        really, sizes = os.read, []

        def watched(handle, most):
            sizes.append(os.fstat(handle).st_size)
            return really(handle, most)
        with mock.patch.object(PB.os, "read", side_effect=watched), \
                self.assertRaisesRegex(PB.BundleError, "does not have the size"):
            PB.Bundle.load(directory(self, raw, bytes=len(raw) + 1))
        self.assertTrue(sizes and len(raw) not in sizes)        # the manifest was read, the file was not
        # a directory, and a pipe nobody writes to: refused at once, not waited on
        folder = directory(self, raw, file="inner")
        (folder / "inner").mkdir()
        with self.assertRaisesRegex(PB.BundleError, "not a regular file|unreadable"):
            PB.Bundle.load(folder)
        if hasattr(os, "mkfifo"):
            folder = directory(self, raw, file="pipe")
            os.mkfifo(folder / "pipe")
            with self.assertRaisesRegex(PB.BundleError, "not a regular file"):
                PB.Bundle.load(folder)
        # a symlink inside the directory to the real file is the real file
        folder = directory(self, raw, file="link")
        os.symlink(folder / PB.FILE, folder / "link")
        self.assertEqual(len(PB.Bundle.load(folder).members), 2)

    def test_the_manifest_is_read_as_carefully_as_the_file(self):
        # The third review: the manifest itself was read with no look at what
        # kind of file it is, and a pipe in its place held the request up.
        raw = packed()
        if hasattr(os, "mkfifo"):
            folder = directory(self, raw)
            (folder / "manifest.json").unlink()
            os.mkfifo(folder / "manifest.json")
            with self.assertRaisesRegex(PB.BundleError, "manifest is not a regular file"):
                PB.Bundle.load(folder)
        if os.path.exists("/dev/zero"):
            folder = directory(self, raw)
            (folder / "manifest.json").unlink()
            os.symlink("/dev/zero", folder / "manifest.json")
            with self.assertRaisesRegex(PB.BundleError, "manifest is not a regular file"):
                PB.Bundle.load(folder)
        # larger than a manifest can be: refused by its size, before a byte is read
        folder = directory(self, raw)
        (folder / "manifest.json").write_bytes(b" " * (PB.MANIFEST_MAX + 1))
        with mock.patch.object(PB.os, "read", side_effect=AssertionError("it was read")), \
                self.assertRaisesRegex(PB.BundleError, "manifest is larger than one can be"):
            PB.Bundle.load(folder)
        # not text, and nesting no parser follows: a manifest that cannot be read, not a crash
        for content in (b"\xff\xfe{}", b"[" * 100000):
            folder = directory(self, raw)
            (folder / "manifest.json").write_bytes(content)
            with self.assertRaisesRegex(PB.BundleError, "manifest is unreadable"):
                PB.Bundle.load(folder)

    def test_members_that_are_the_same_nets_are_one_member(self):
        # The third review: the same member twice loaded and answered, and
        # two that are the same agree about anything.
        with self.assertRaisesRegex(PB.BundleError, "the same nets"):
            PB.Bundle(packed(made=[(0, nets()), (1, nets())]))
        with self.assertRaisesRegex(PB.BundleError, "the same nets"):
            PB.Bundle(packed(made=[(0, nets()), (1, nets(lift=0.25)), (2, nets())]))
        self.assertEqual(len(PB.Bundle(packed(made=[(0, nets()), (1, nets(lift=0.25))])).members), 2)

    def test_nets_without_a_size_are_refused(self):
        raw = packed()
        start = len(PB.MAGIC) + 4
        size = int.from_bytes(raw[len(PB.MAGIC):start], "little")
        head = json.loads(raw[start:start + size])
        for key in ("dim", "hidden"):
            encoded = json.dumps({**head, key: 0}).encode()
            with self.assertRaisesRegex(PB.BundleError, "have no size", msg=key):
                PB.Bundle(PB.MAGIC + len(encoded).to_bytes(4, "little") + encoded + raw[start + size:])

    def test_nothing_wrong_with_the_file_stops_a_request(self):
        # A fault that shows only while a request is read: the bundle is
        # given up for this process and the request goes on to the model.
        class Faulty:
            label = "bundle faulty"

            def read(self, request, texts=None):
                raise KeyError("a table that is not there")
        saved = dict(PB._LOADED)
        self.addCleanup(lambda: (PB._LOADED.clear(), PB._LOADED.update(saved)))
        PB._LOADED["bundle"] = Faulty()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as said:
            self.assertIsNone(PB.read("move to the next tab"))
            self.assertIsNone(PB.read("move to the next tab"))
        self.assertEqual(said.getvalue().count("the panes bundle is not used"), 1)
        self.assertIsNone(PB._LOADED["bundle"])
        # and a file that cannot be loaded at all is no bundle, not an error
        PB._LOADED.clear()
        with mock.patch.object(PB.Bundle, "load", side_effect=OSError("gone")), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertIsNone(PB.read("move to the next tab"))

    def test_no_manifest_no_bundle(self):
        with self.assertRaises(PB.BundleError):
            PB.Bundle.load(Path(tempfile.gettempdir()) / "kn-no-such-bundle")

    def test_a_bundle_that_is_switched_on_and_missing_is_said_once(self):
        # Whoever reaches the reader has switched the bundle on. No
        # directory, an empty one, a manifest that is a directory: each is
        # said on stderr, once, and the model reads the request.
        empty = Path(tempfile.mkdtemp(prefix="kn-bundle-"))
        self.addCleanup(shutil.rmtree, empty, True)
        folded = Path(tempfile.mkdtemp(prefix="kn-bundle-"))
        self.addCleanup(shutil.rmtree, folded, True)
        (folded / "manifest.json").mkdir()
        saved = dict(PB._LOADED)
        self.addCleanup(lambda: (PB._LOADED.clear(), PB._LOADED.update(saved)))
        for folder in (empty / "nothing-here", empty, folded):
            PB._LOADED.clear()
            with mock.patch.object(PB, "DIRECTORY", folder), mock.patch("sys.stderr", new_callable=io.StringIO) as said:
                self.assertIsNone(PB.read("move to the next tab"))
                self.assertIsNone(PB.read("move to the next tab"))
                self.assertEqual(said.getvalue().count("the panes bundle is not used"), 1, folder)
                # and asked directly it says why and ends, with no traceback
                self.assertEqual(PB.main(["move to the next tab"]), 1)
                self.assertRegex(said.getvalue(), "panes_bundle: the bundle manifest is (unreadable|not a regular file)")

    def test_members_must_agree(self):
        agree = PB.Bundle.load(directory(self, packed(members=3)))
        self.assertEqual(len(agree.members), 3)
        self.assertEqual(agree.read("move to the next tab")[0], [call("go_to_tab", tab="next")])
        split = PB.Bundle.load(directory(self, packed(members=2, split=True)))
        self.assertEqual(split.read("move to the next tab"), ([], "the members do not agree"))

    def test_members_must_be_the_same_nets(self):
        other = nets()
        del other["span close_pane pane"]
        with self.assertRaises(ValueError):
            PBT.pack([(0, nets()), (1, other)], KNOWN, set(PAIRS), TOOLS, [], dim=2, hidden=4)

    def test_the_checked_in_bundle_is_the_one_its_manifest_names(self):
        if not (PB.DIRECTORY / "manifest.json").is_file():
            self.skipTest("no bundle is checked in")
        bundle = PB.Bundle.load()
        self.assertGreaterEqual(len(bundle.members), PB.MEMBERS_MIN)
        calls, _ = bundle.read("split right and run htop")
        self.assertEqual(calls, [call("open_pane", side="right", program="htop")])
        # The trainer ran the whole reader over every training row before it
        # wrote the file: every row it answers, it answers as the row is labelled.
        manifest = json.loads((PB.DIRECTORY / "manifest.json").read_text())
        self.assertEqual(sorted(manifest["code"]), sorted(("panes_bundle.py", "panes_bundle_train.py") + PBT.ROW_CODE))
        self.assertEqual(manifest["reads_its_rows"]["risky_and_not_labelled"], 0)
        self.assertGreater(manifest["reads_its_rows"]["as_labelled"], 0)
        self.assertEqual(manifest["reads_its_rows"]["as_labelled"], manifest["reads_its_rows"]["answered"])
        # the file is the one this reader and this trainer wrote
        for source in ("panes_bundle.py", "panes_bundle_train.py"):
            self.assertEqual(manifest["code"][source], hashlib.sha256((PBT.REPO / source).read_bytes()).hexdigest(), source)
        self.assertTrue(manifest["rows"]["library_sha256"])
        # every kind that is risky whatever its arguments has its target held to trained words
        self.assertEqual({kind for kind in bundle.tools if Action(kind, {}).risky}, set(PB.KNOWN_TARGET))
        for kind, key in PB.KNOWN_TARGET.items():
            self.assertIn([key, "ref"], [spec[:2] for spec in bundle.tools[kind]["args"]])


class TrainerRefusals(unittest.TestCase):
    """What the trainer will not take rows from. None of this needs PyTorch."""

    def test_a_current_gate_set_gives_no_rows_under_any_name(self):
        import jobs
        gate = PBT.REPO / jobs.JOBS["panes"].heldout
        self.assertTrue(gate.is_file())
        folder = Path(tempfile.mkdtemp(prefix="kn-bundle-"))
        self.addCleanup(shutil.rmtree, folder, True)
        link = folder / "rows.jsonl"
        os.symlink(gate, link)
        for spelling in (jobs.JOBS["panes"].heldout, str(gate), str(link)):
            with self.assertRaisesRegex(SystemExit, "current gate set"):
                PBT.refuse_gates([spelling])
            # the same refusal from the command line, before a row is read
            with mock.patch.object(PBT, "write", side_effect=AssertionError("a file was written")), \
                    self.assertRaisesRegex(SystemExit, "current gate set"):
                PBT.main(["--rows", spelling, "--out", str(folder / "out")])
        with self.assertRaisesRegex(SystemExit, "current gate set"):
            PBT.spent_rows(PBT.SPENT + (jobs.JOBS["panes"].heldout,))
        # every job's gate is refused, not only this job's
        for job in jobs.JOBS.values():
            with self.assertRaisesRegex(SystemExit, "current gate set"):
                PBT.refuse_gates([job.heldout])
        PBT.refuse_gates(PBT.SPENT)                         # the spent sets are not gates
        # a copy under another name is known by its digest
        copy = folder / "something-else.jsonl"
        shutil.copyfile(gate, copy)
        with self.assertRaisesRegex(SystemExit, "copy of a current gate set"):
            PBT.refuse_gates([str(copy)])
        with mock.patch.object(PBT, "write", side_effect=AssertionError("a file was written")), \
                self.assertRaisesRegex(SystemExit, "copy of a current gate set"):
            PBT.main(["--rows", str(copy), "--out", str(folder / "out")])

    def test_a_file_that_answers_a_training_row_riskily_is_not_written(self):
        # "shut the left pane" labelled as a tab change: the hand-made nets
        # close the pane, a risky action the row does not hold.
        raw = packed()
        rows = [{"query": "move to the next tab", "actions": [["go_to_tab", {"tab": "next"}]], "spans": []},
                {"query": "shut the left pane", "actions": [["go_to_tab", {"tab": "next"}]], "spans": []},
                {"query": "what time is it", "actions": [], "spans": []}]
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        with FakeKilix(desktop()):
            got = PBT.reads_its_rows(raw, rows)
        self.assertEqual(got, {"rows": 3, "answered": 2, "as_labelled": 1, "proposed_and_dropped": 0,
                               "risky_and_not_labelled": 1})
        with self.assertRaisesRegex(SystemExit, "risky action the row does not hold"):
            PBT.refuse_misread_rows(got)

    def test_a_file_that_answers_a_training_row_otherwise_than_labelled_is_not_written(self):
        # "move to the next tab" labelled as the previous one: nothing risky,
        # and still not what the row says (the second review: only a risky
        # misreading stopped a write).
        rows = [{"query": "move to the next tab", "actions": [["go_to_tab", {"tab": "previous"}]], "spans": []},
                {"query": "what time is it", "actions": [], "spans": []}]
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        with FakeKilix(desktop()):
            got = PBT.reads_its_rows(packed(), rows)
        self.assertEqual(got, {"rows": 2, "answered": 1, "as_labelled": 0, "proposed_and_dropped": 0,
                               "risky_and_not_labelled": 0})
        with self.assertRaisesRegex(SystemExit, "otherwise than the row is labelled"):
            PBT.refuse_misread_rows(got)
        PBT.refuse_misread_rows({**got, "as_labelled": 1})           # every answer as labelled: written

    def test_the_nets_are_checked_on_examples_from_all_over(self):
        self.assertEqual(PBT.spread(list(range(7))), list(range(7)))
        sample = PBT.spread(list(range(21525)))
        self.assertEqual((len(sample), sample[0]), (PBT.CHECKED, 0))
        self.assertGreater(sample[-1], 21000)                        # not the first 200

    def test_what_decides_the_rows_is_recorded(self):
        folder = Path(tempfile.mkdtemp(prefix="kn-bundle-"))
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "a").mkdir()
        (folder / "a" / "one.txt").write_text("one")
        before = PBT.tree_digest(folder)
        self.assertEqual(before, PBT.tree_digest(folder))
        (folder / "a" / "one.txt").write_text("two")
        self.assertNotEqual(before, PBT.tree_digest(folder))
        self.assertIsNone(PBT.library_commit(folder / "no-such-checkout"))
        self.assertEqual(PBT.ROW_CODE, ("actions.py", "tuning.py", "toolset.py", "panes_bundle_agent.py",
                                        "job_schemas.py", "job_rows.py", "pane_reads.py"))


class WhatTheReviewsFound(unittest.TestCase):
    """Requests the checked-in bundle answered and misread before it was held
    to whole training clauses (the reviews of 2026-11-17). Each is made of
    words and word pairs it knows. None may be answered."""

    # The first review: held to known words and pairs, it read these.
    FIRST = {
        # the caller's own tab was renamed; the request names another
        "rename the logs tab to api": PB.UNSHOWN,
        # "logs" was passed over: go to the previous tab
        "back to the logs tab": PB.UNSHOWN,
        # nothing asks for a layout: the panes were stacked. Since v5 "all the
        # others" reads as a list, which is answered only alone
        "switch to the htop pane and all the others": PB.ALONE,
        # any word between two clauses was taken for "and"
        "go to tab 2 or rename this tab to api": "clauses joined by a word that never joined two in training",
        "go to tab 2 nope rename this tab to api": "clauses joined by a word that never joined two in training",
        "close tab 2 or go to tab 1": "clauses joined by a word that never joined two in training",
        # three clauses were never trained; two of these were dropped
        "split right, close this pane, split below": None,
        "close tab 1 and close tab 2 and go to tab 3": "more clauses than any training row had",
        # a title was read as a position
        "go to the tab named two": PB.UNSHOWN,
        # another pane is named and the caller's own was resized
        "make the left pane wider": None,
        # one of two words of place passed over
        "take me to the top pane to the left": None,
        "switch to grid, go in rows": None,
        # a pane named by its title and by its place: one of the two passed over
        "just type df -h in the build pane at the bottom for me": None,
        # the name swallowed the clauses after it
        "retitle the tab logs, call the pane above logs, tab name: frontend": PB.LONG,
        # what to close is still never named by a word the bundle does not know
        "close the 3rd tab": "an untrained word names what to close or type into",
        # digits the nets take for a number and the reader cannot read as one
        "go to tab \uff13": "digits the reader does not read",
        "close tab \u0663": "digits the reader does not read",
        "make this pane wider by \u00b2": "digits the reader does not read",
    }
    # The second review: held to pairs of neighbours of its own tool's
    # clauses, it read these, and the route ran every one with no question.
    SECOND = (
        # 1.1 a number in the request became the target, in the shared checks
        "call this tab 2", "call this tab 1", "rename tab 2", "call this tab 2 and split right",
        "make this pane 3 rows taller",
        # 1.2 "it" after a move or an open acted where the request came from
        "go to the logs tab and rename it to api", "go to the left pane and rename it to api",
        "go to the left pane and make it wider", "go to tab 2 and make it wider", "split right and make it wider",
        "split right and rename it to logs", "go to the left pane and run make test",
        "go to tab 2 and rename this tab to api", "open a new tab and split right",
        # 1.3 a word of place in one clause, answered for by the other clause's call
        "make the left pane wider and go to the left pane", "split left and make the left pane wider",
        "maximize the left pane and make the left pane wider",
        # 1.4 a layout nobody named, 1.5 a place nobody named
        "rearrange the panes", "layout", "i'm screen sharing, it's too cramped", "four panes", "grid is too cramped",
        "bring my cursor back in", "go to it", "take me back one tab", "move one tab", "go to build",
        # 1.6 fragments and statements opened things
        "i want a quick check", "hi, direction doesn't matter", "i need one", "right please", "tab named work",
        # 1.7 a negation was passed over
        "not a tab", "not a new tab, thanks", "not a separate tab", "exchange this pane, not under them",
        # 1.8 a name that is not a name
        "rename this tab please", "rename it accordingly", "rename this tab to api!",
        # 1.9 a tab or a pane by the spelling of the new name
        "rename this one api", "rename this one left",
        # 1.10 a resize that names another pane by its title
        "widen the log pane", "make the log pane wider", "the log pane is too cramped, make it wider",
        # of the same kinds, found while these were fixed
        "run htop in the left pane and make it wider", "close tab 1 and go to tab 2",
        "rename this tab to api and go to the api tab", "open a new tab and go to the next tab",
        "arrange the panes in a grid and go to the pane above", "maximize this pane and rename it to logs",
    )
    # The third review: held to whole training clauses, it read these.
    THIRD = (
        # 1 a word it has never seen, beside the name, was part of the name
        "rename this tab to api thx", "rename this pane to api quickly", "rename this tab to uh api",
        "title this tab api pronto", "name this tab api asap", "set the tab title to api thx",
        # 2 a pane that is closed may be the last of its tab: the tabs after it are counted anew
        "close the notes pane and shift this tab to position 1", "close the notes pane and go to tab 2",
        "kill the notes pane and close tab 2", "close the notes pane, then move this tab to position 1",
        # 3 whether the tab shows one pane at full size was read before the first call ran
        "maximize the current pane and restore layout", "layout stacked and restore layout",
        "panes grid and full-screen this pane please", "restore layout and zoom this pane",
        # a move of a tab is made through the caller's pane: the focus went back where it came from
        "go to the left pane and move this tab to the left", "maximize left pane and move current tab left",
        # the pane the request is made from may be closed first, and the second call never sent
        "close tab 3 and new tab", "close this pane and go to tab 1", "close the build pane and arrange the rest in a grid",
        # a pane that was renamed or typed into, named again by the title it had
        "call the build pane api and close the build pane", "call the build pane api and select the build pane",
        "in the notes pane, run ls and close the notes pane",
    )
    # What is left of its first finding: one word the bundle has never seen,
    # standing alone where a name stands, is the name. The reader cannot
    # tell "asap" from "api" (panes-bundle/README.md, "What it still gets
    # wrong"). Held here so that nobody takes it for fixed.
    A_NEW_WORD_ALONE = {
        "rename the tab asap": [call("rename_tab", name="asap")],
        "call this pane asap": [call("rename_pane", name="asap")],
    }
    STILL_READ = {
        "go to tab two": [call("go_to_tab", tab="2")],
        "rename this tab to api": [call("rename_tab", name="api")],
        "go to the logs tab": [call("go_to_tab", tab="logs")],
        "move to the next tab": [call("go_to_tab", tab="next")],
        "take me to the pane to the left": [call("go_to_pane", pane="left")],
        "rename this tab to build logs": [call("rename_tab", name="build logs")],
        "make this pane wider": [call("resize_pane", direction="wider")],
        "close the build pane": [call("close_pane", pane="build")],
        # a number written with digits and an ordinal ending is that number, not a title
        "go to the 3rd tab": [call("go_to_tab", tab="3")],
        # two clauses, the second resolved by nothing the first changes
        "rename this tab to logs and split right": [call("rename_tab", name="logs"), call("open_pane", side="right")],
        "rename this tab to api then split right": [call("rename_tab", name="api"), call("open_pane", side="right")],
        "make this pane wider and go to the left pane": [call("resize_pane", direction="wider"),
                                                         call("go_to_pane", pane="left")],
        "split right and go to tab 2": [call("open_pane", side="right"), call("go_to_tab", tab="2")],
        "split right and rename the tab to api": [call("open_pane", side="right"), call("rename_tab", name="api")],
        "go to tab 2 and close tab 1": [call("go_to_tab", tab="2"), call("close_tab", tab="1")],
        # a pane on a side of the caller's is not the caller's own, and its tab stays
        "close the left pane and arrange the rest in a grid": [call("close_pane", pane="left"),
                                                               call("arrange_panes", layout="grid")],
        "close the left pane and go to tab 2": [call("close_pane", pane="left"), call("go_to_tab", tab="2")],
        # names written as one word, whatever marks they hold
        "rename this tab to api-server": [call("rename_tab", name="api-server")],
        "rename this tab to v1.2": [call("rename_tab", name="v1.2")],
        # a rename that says what it renames, after a clause about the other kind
        "maximize this pane and rename this tab to logs": [call("maximize_pane"), call("rename_tab", name="logs")],
        "go to the left pane and rename the tab logs": [call("go_to_pane", pane="left"), call("rename_tab", name="logs")],
    }
    # Read by v4, no longer by v5: its separator nets do not split these at
    # "and" (the agent rows' "open a new tab named X and run Y" is one clause),
    # and the whole is wording no tool was trained on. They go to the model.
    # Held so that the loss is seen (panes-bundle/README.md, "What v5 gave up").
    NO_LONGER_READ = ("rename this tab to api and split right", "close the next tab and open a new tab")

    def setUp(self):
        if not (PB.DIRECTORY / "manifest.json").is_file():
            self.skipTest("no bundle is checked in")
        self.bundle = PB.Bundle.load()

    def used(self, request):
        """The calls the route would run for the bundle, or None: the reader and the wiring's hold on it."""
        texts: list = []
        calls, _ = self.bundle.read(request, texts)
        return calls if calls and needle_cli.checks_agree(request, calls, texts) else None

    # A clause that is no training clause of its tool: the first reader's
    # reason, or the second reader's where the file switches it on for the
    # clause's kind and it declines too.
    NO_TRAINING_CLAUSE = (PB.UNSHOWN, PB.SECOND_RUN, PB.SECOND_SET, PB.SECOND_SAID, PB.SECOND_RISKY)

    def test_each_request_the_first_review_found_is_declined(self):
        for request, why in self.FIRST.items():
            calls, reason = self.bundle.read(request)
            self.assertEqual(calls, [], request)
            if why == PB.UNSHOWN:
                self.assertIn(reason, self.NO_TRAINING_CLAUSE, request)
            elif why:
                self.assertEqual(reason, why, request)

    def test_no_request_the_second_review_found_is_answered(self):
        for request in self.SECOND:
            self.assertIsNone(self.used(request), request)
        # the reader does read "call this tab 2", as a tab named 2; the checks
        # make it tab 2, and the wiring does not use what the checks changed
        self.assertEqual(self.bundle.read("call this tab 2")[0], [call("rename_tab", name="2")])

    def test_no_request_the_third_review_found_is_answered(self):
        for request in self.THIRD:
            self.assertIsNone(self.used(request), request)
        for request in self.THIRD[:6]:
            self.assertEqual(self.bundle.read(request), ([], PB.MIXED), request)
        # since v5 the separator nets do not split some of these at "and": the
        # whole is then one clause, with a pair or a frame no tool was trained on
        for request in self.THIRD[6:]:
            calls, reason = self.bundle.read(request)
            self.assertEqual(calls, [], request)
            self.assertIn(reason, (PB.LEANS, PB.PAIR, PB.UNSHOWN), request)
        self.assertEqual(self.bundle.read(self.THIRD[6]), ([], PB.LEANS))

    def test_a_new_word_alone_where_a_name_stands_is_still_the_name(self):
        for request, calls in self.A_NEW_WORD_ALONE.items():
            self.assertEqual(self.used(request), calls, request)

    def test_a_layout_kitty_shows_the_other_way_round_is_not_answered(self):
        # The third review, finding 5, checked in kitty's own source: its
        # vertical layout shows the panes one below another, and its
        # horizontal layout side by side. The tool's labels say the opposite.
        for request in ("arrange the panes one above another", "arrange the panes side by side"):
            self.assertEqual(self.bundle.read(request), ([], PB.TURNED), request)
        self.assertEqual(PB.CROSSED, {("arrange_panes", "layout"): ("vertical", "horizontal")})
        # while the tool's description still says so; once it is put right, so is this rule
        import actions
        self.assertIn("vertical is side by side", json.dumps(actions.TOOLS))
        # the other layouts are answered
        self.assertEqual(self.used("stack everything"), [call("arrange_panes", layout="stack")])

    def test_ordinary_requests_are_still_read(self):
        for request, calls in self.STILL_READ.items():
            self.assertEqual(self.bundle.read(request), (calls, "ok"), request)
            self.assertEqual(self.used(request), calls, request)

    def test_what_v5_no_longer_reads_goes_to_the_model(self):
        for request in self.NO_LONGER_READ:
            calls, reason = self.bundle.read(request)
            self.assertEqual(calls, [], request)
            self.assertIn(reason, self.NO_TRAINING_CLAUSE, request)

    def test_what_the_file_goes_by(self):
        self.assertEqual(self.bundle.clauses_max, 2)
        self.assertEqual(self.bundle.separators, {"and", "then", ",", ";"})
        self.assertEqual(self.bundle.copy_max["rename_tab"], {"name": 3, "tab": 1})
        for word in ("left", "right", "top", "bottom", "next", "previous", "grid"):
            self.assertIn(word, self.bundle.value_words)
        self.assertNotIn("current", self.bundle.value_words)
        # a rename of a tab names the tab, and a resize its pane, only by a number (an agent's "tab id 4",
        # "pane 412"), never by a copied title
        self.assertEqual(self.bundle.head["nets"]["arg rename_tab tab"]["outputs"], [PB.ABSENT, PB.COPY])
        for tool, mark in (("rename_tab", "tab"), ("resize_pane", "pane")):
            marks = {m for line in self.bundle.readings(tool) for m in re.findall(rf"<{mark}#?>", line)}
            self.assertEqual(marks, {f"<{mark}#>"}, tool)
        # what a tool acts on is read from its name (acts_on): every tool but the list is named for a tab or
        # for panes, and a list is only ever the request's one clause (ALONE)
        self.assertTrue(all(tool.endswith(("_tab", "_pane", "_panes")) for tool in self.bundle.tools if tool != PB.LIST))
        self.assertIn(PB.LIST, self.bundle.tools)
        self.assertEqual(set(PB.RENAMES), {tool for tool in self.bundle.tools if tool.startswith("rename_")})
        # every tool is in the table of what a call changes, or is one of the four that change nothing a later
        # call is resolved by but a title or the pane they name (changes), or the list, which changes nothing
        self.assertEqual(set(self.bundle.tools),
                         set(PB._CHANGES) | {"rename_tab", "rename_pane", "resize_pane", "run_in_pane", PB.LIST})
        self.assertTrue(set(PB._GOES_BY) <= set(self.bundle.tools))
        self.assertTrue(set(PB.NOT_THE_CALLERS) <= set(self.bundle.tools))
        # every argument that names something by a copied text is held to one new word, but a program and a
        # command, which are made of new words and wait for a yes
        copied = {key for tool in self.bundle.tools.values() for key, kind, _, _ in tool["args"] if kind in ("ref", "text")}
        self.assertEqual(copied - set(PB.ONE_NEW), {"program", "command"})
        # the second reader's runs are of the length this reader holds to
        self.assertEqual(self.bundle.head["frame_run"], PB.RUN)
        # "go to it" only ever followed a clause that opens a pane: it is a reading there and nowhere else
        go_to_it = "go to it".split()
        self.assertIn(PB.reading_line("open_pane", go_to_it, {"pane": "below"}), self.bundle.readings("go_to_pane"))
        self.assertNotIn(PB.reading_line("", go_to_it, {"pane": "below"}), self.bundle.readings("go_to_pane"))
        # names are made of words that were copied as names
        self.assertIn("logs", self.bundle.copied["name"])
        self.assertNotIn("please", self.bundle.copied["name"])
        self.assertIn("please", self.bundle.known)


class WhatV5Reads(unittest.TestCase):
    """What file format v5 added, read by the checked-in file: agent-style
    wording, ids, the list kind and the second reader. The requests are made
    up, or are walks of the second review every run of three of whose frames
    stands in a frame of the generated and agent-style rows alone."""

    # The kinds the standard of experiment 10 passed (panes-bundle/README.md, "The reader now")
    SECOND_KINDS = {"arrange_panes", PB.COMPOUND, "go_to_pane", "go_to_tab", "move_tab", "rename_tab"}
    IDS = {
        "rename tab id 4 to api": [call("rename_tab", tab="id:4", name="api")],
        "rename tab id 1 to x; make pane 2 wider by 5": [call("rename_tab", tab="id:1", name="x"),
                                                         call("resize_pane", pane="2", direction="wider", amount=5)],
        "close pane 412": [call("close_pane", pane="412")],
        "rename this tab to api.": [call("rename_tab", name="api")],
    }
    # A tab's place is not its id, and only a rename names a tab by its id in training.
    NOT_READ = ("rename tab 4 to api", "go to tab id 4", "close tab id 7")
    SECOND = {
        "shift the active tab to slot 1": [call("move_tab", position=1)],
        "move the focused tab to the left": [call("move_tab", direction="left")],
        "focus tab two?": [call("go_to_tab", tab="2")],
        "hop to the bottom": [call("go_to_pane", pane="below")],
        "switch to fat": [call("arrange_panes", layout="fat")],
    }
    # What the second reader answers and the labeller of the walks judged to
    # ask for nothing plainly, or to name what may be a tab or a pane
    # (panes-bundle/README.md, "What it still gets wrong"). The tuned model
    # misreads most of the same. Held here so that nobody takes it for fixed.
    SECOND_FRAGMENTS = {
        "a grid": [call("arrange_panes", layout="grid")],
        "over to the right": [call("go_to_pane", pane="right")],
        "could you switch to work": [call("go_to_tab", tab="work")],
        "name this one frontend": [call("rename_tab", name="frontend")],
        "the panes stacked?": [call("arrange_panes", layout="stack")],
    }

    def setUp(self):
        if not (PB.DIRECTORY / "manifest.json").is_file():
            self.skipTest("no bundle is checked in")
        self.bundle = PB.Bundle.load()

    def used(self, request):
        texts: list = []
        calls, _ = self.bundle.read(request, texts)
        return calls if calls and needle_cli.checks_agree(request, calls, texts) else None

    def test_the_second_reader_is_on_for_the_kinds_its_standard_passed(self):
        self.assertEqual(self.bundle.second_kinds, self.SECOND_KINDS)

    def test_ids(self):
        for request, calls in self.IDS.items():
            self.assertEqual(self.bundle.read(request), (calls, PB.OK), request)
            self.assertEqual(self.used(request), calls, request)
        for request in self.NOT_READ:
            self.assertEqual(self.bundle.read(request)[0], [], request)

    def test_a_list_stands_alone(self):
        self.assertEqual(self.bundle.read("list pane 412 and its title"),
                         ([call(PB.LIST, what="panes", about="412")], PB.OK))
        for request in ("list tabs; close tab 2", "list panes with their ids and close tab 2"):
            self.assertEqual(self.bundle.read(request), ([], PB.ALONE), request)

    def test_the_second_reader_answers_only_the_kinds_it_is_on_for(self):
        answered = {**self.SECOND, **self.SECOND_FRAGMENTS}
        for request, calls in answered.items():
            self.assertEqual(self.bundle.read(request), (calls, PB.OK_SECOND), request)
            self.assertEqual(self.used(request), calls, request)
        self.bundle.second_kinds = frozenset()
        for request in answered:
            self.assertEqual(self.bundle.read(request), ([], PB.UNSHOWN), request)

    def test_not_what_the_second_review_found(self):
        # Requests of the reviews that the second reader as first built, held
        # to pairs of neighbours, answered again (wrongly).
        for request in ("not a new tab, thanks", "i need one"):
            self.assertIsNone(self.used(request), request)
        # A clause it would answer alone still does not go by what the clause before it changes.
        self.assertEqual(self.bundle.read("go to tab 2 and shift the active tab to slot 1"), ([], PB.LEANS))


class WhatTheFourthReviewFound(unittest.TestCase):
    """The fourth review, of format v5 as first shipped (panes-bundle/README.md,
    "What the fourth review found"). The requests are the reviewer's."""

    # The second reader answered each of these: a clause in new wording, of a
    # kind its standard kept off, beside a second clause (`compound`).
    BESIDE = {
        "rename this tab to api and grow this pane lower": "resize_pane",    # resized it shorter
        "grow this pane lower and go to tab 2": "resize_pane",               # shorter
        "move this tab left; shrink this pane broader": "resize_pane",       # wider
        "maximize left pane again and go to tab 2": "maximize_pane",
        "label the pane to the left pane to the left api and go to tab 2": "rename_pane",
        "full-screen this pane to the normal pane layout and go to tab 2": "maximize_pane",
    }
    # A clause in new wording of a kind that is on is still answered beside another.
    STILL = {"arrange the panes tall? and go to tab 2": [call("arrange_panes", layout="tall"), call("go_to_tab", tab="2")]}
    # The tuner's own rows, each a training clause the first reader answered by its direction.
    OPPOSED = ("stretch this pane lower", "stretch this pane narrower", "stretch this pane shorter",
               "stretch this pane thinner")

    def setUp(self):
        if not (PB.DIRECTORY / "manifest.json").is_file():
            self.skipTest("no bundle is checked in")
        self.bundle = PB.Bundle.load()

    def test_beside_another_clause_a_kind_that_is_off_is_not_answered(self):
        for request, kind in self.BESIDE.items():
            self.assertNotIn(kind, self.bundle.second_kinds, request)
            self.assertEqual(self.bundle.read(request), ([], PB.UNSHOWN), request)
        for request, calls in self.STILL.items():
            self.assertEqual(self.bundle.read(request), (calls, PB.OK_SECOND), request)

    def test_it_is_the_clauses_kind_that_holds_it(self):
        # With the clause's own kind switched on by hand, the request is answered again.
        self.bundle.second_kinds = self.bundle.second_kinds | {"maximize_pane"}
        self.assertEqual(self.bundle.read("maximize left pane again and go to tab 2"),
                         ([call("maximize_pane", pane="left"), call("go_to_tab", tab="2")], PB.OK_SECOND))

    def test_a_resize_whose_verb_and_direction_pull_opposite_ways(self):
        for request in self.OPPOSED:
            self.assertEqual(self.bundle.read(request), ([], PB.OPPOSED), request)
        # whichever reader would answer it
        self.bundle.second_kinds = frozenset(self.bundle.tools) | {PB.COMPOUND}
        for request in ("grow this pane lower", "expand this pane lower by 5 cells", "shrink this pane broader",
                        "expand this pane a little narrower", "rename this tab to api and grow this pane lower"):
            self.assertEqual(self.bundle.read(request)[0], [], request)
        # and where they agree, as before
        self.assertEqual(self.bundle.read("stretch this pane taller"), ([call("resize_pane", direction="taller")], PB.OK))
        self.assertEqual(self.bundle.read("make this pane lower"), ([call("resize_pane", direction="shorter")], PB.OK))

    def test_a_list_about_one_pane_is_the_whole_list(self):
        # Held as it is: what a list is about is read and not used, and the
        # route shows the exact list reader's whole list (needle_cli.listed).
        self.assertEqual(self.bundle.read("describe pane 301"), ([call(PB.LIST, what="panes", about="301")], PB.OK))

    def test_the_manifests_kinds_are_not_under_the_files_digest(self):
        # Held as it is: the digest is the file's; the second reader's kinds
        # are the manifest's word, and a manifest that names every kind loads.
        # The risky hold does not depend on them.
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(PB.DIRECTORY / "bundle.bin", tmp)
            manifest = json.loads((PB.DIRECTORY / "manifest.json").read_text())
            manifest["second_reader"] = {"kinds": sorted(set(self.bundle.tools) | {PB.COMPOUND})}
            Path(tmp, "manifest.json").write_text(json.dumps(manifest))
            every = PB.Bundle.load(Path(tmp))
            self.assertEqual(every.second_kinds, frozenset(self.bundle.tools) | {PB.COMPOUND})
            for request in ("close the pane on the left now", "type ls in the left pane please"):
                calls, _ = every.read(request)
                self.assertFalse(any(Action(c["name"], c["arguments"]).risky for c in calls), request)


class Terminal(io.StringIO):
    def __init__(self, answers):
        super().__init__("".join(f"{answer}\n" for answer in answers))

    def isatty(self):
        return True


class Model:
    """Stands where the model stands and counts what reaches it."""
    label = "the model"

    def __init__(self, *calls):
        self.calls, self.prompts = list(calls), []

    def reset(self):
        pass

    def complete(self, text):
        self.prompts.append(text)
        return {"type": "call", "function_calls": self.calls}


class Route(unittest.TestCase):
    """The whole route on the tests' fake desktop, with a bundle standing before the model."""

    def standing_in_front(self):
        return PB.Bundle.load(directory(self, packed()))

    def setUp(self):
        saved = dict(PB._LOADED)
        PB._LOADED["bundle"] = self.standing_in_front()
        self.addCleanup(lambda: (PB._LOADED.clear(), PB._LOADED.update(saved)))
        self.switch("1")
        shutil.rmtree(history.directory(), ignore_errors=True)
        before = os.environ.pop("KILIX_NEEDLE_HISTORY", None)
        if before is not None:
            self.addCleanup(os.environ.__setitem__, "KILIX_NEEDLE_HISTORY", before)

    def switch(self, value):
        before = os.environ.get("KILIX_NEEDLE_BUNDLE")
        os.environ["KILIX_NEEDLE_BUNDLE"] = value
        self.addCleanup(os.environ.__setitem__, "KILIX_NEEDLE_BUNDLE", before or "0")

    def ask(self, prompt, *model_calls, answers=(), **modes):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        saved = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = Terminal(answers), io.StringIO()
        try:
            with FakeKilix(desktop()) as fake:
                model = Model(*model_calls)
                status = needle_cli.handle(model, prompt, out=sys.stdout, **modes)
                self.said = sys.stdout.getvalue()
                return status, fake.calls(), model
        finally:
            sys.stdin, sys.stdout = saved

    def entries(self):
        path = history.directory() / "requests.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class BeforeTheModel(Route):
    def test_known_ground_never_reaches_the_model(self):
        request = "move to the next tab"
        self.assertIsNone(panes_exact.admitted(request))            # not an exact command either
        status, calls, model = self.ask(request, call("close_tab", tab="current"))
        self.assertEqual((status, model.prompts), (0, []))
        self.assertEqual(len(calls), 1)                             # one kilix call: the tab change
        (entry,) = self.entries()
        self.assertEqual(entry["engine"], "bundle by hand")
        self.assertEqual(entry["calls"], [call("go_to_tab", tab="next")])
        self.assertEqual(entry["items"][0]["outcome"], "done")

    def test_what_it_declines_goes_to_the_model_as_before(self):
        status, calls, model = self.ask("move to the tab after this one", call("go_to_tab", tab="next"))
        self.assertEqual(model.prompts, ["move to the tab after this one"])
        self.assertEqual(self.entries()[0]["engine"], "the model")

    def test_switched_off_everything_goes_to_the_model(self):
        self.switch("0")
        _, _, model = self.ask("move to the next tab", call("go_to_tab", tab="next"))
        self.assertEqual(model.prompts, ["move to the next tab"])

    def test_its_calls_meet_the_same_checks_and_the_same_question(self):
        request = "shut the left pane"
        self.assertIsNone(panes_exact.admitted(request))
        self.assertEqual(PB.read(request), ([call("close_pane", pane="left")], "bundle by hand"))
        status, calls, model = self.ask(request, answers=["n"])
        self.assertEqual((model.prompts, calls), ([], []))          # asked, answered no: nothing ran
        self.assertEqual(status, 1)
        self.assertRegex(self.said, r"close pane 301 .*\? \[y/N\]")   # the question was put
        self.assertEqual(self.entries()[0]["engine"], "bundle by hand")
        # answered yes: the close runs, once, on the pane the question named
        status, calls, model = self.ask(request, answers=["y"])
        self.assertEqual((status, model.prompts), (0, []))
        self.assertEqual([args for args, _ in calls], [["close-window", "--match=id:301"]])
        self.assertIn("[y/N]", self.said)

    def test_a_yes_given_in_advance_counts_for_its_calls_as_for_the_model_s(self):
        request = "shut the left pane"
        # the same proposal from the model, then from the bundle: the same run, no question
        self.switch("0")
        _, by_model, model = self.ask(request, call("close_pane", pane="left"), assume_yes=True)
        self.assertEqual(model.prompts, [request])
        said_to_model = self.said
        self.switch("1")
        status, by_bundle, model = self.ask(request, assume_yes=True)
        self.assertEqual((status, model.prompts), (0, []))
        self.assertEqual(by_bundle, by_model)
        self.assertEqual(self.said, said_to_model)
        self.assertNotIn("[y/N]", self.said)

    def test_an_agent_gets_no_yes_for_the_bundle_s_risky_call(self):
        # An agent harness cannot answer a question: the close does not run.
        status, calls, model = self.ask("shut the left pane", agent=True)
        self.assertEqual((calls, model.prompts), ([], []))
        self.assertEqual(status, 1)

    def test_a_bundle_that_fails_while_reading_leaves_the_request_to_the_model(self):
        class Faulty:
            label = "bundle faulty"

            def read(self, request, texts=None):
                raise IndexError("a table row that is not in the file")
        PB._LOADED["bundle"] = Faulty()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as said:
            status, calls, model = self.ask("move to the next tab", call("go_to_tab", tab="next"))
            again = self.ask("move to the next tab", call("go_to_tab", tab="next"))
        self.assertEqual((status, model.prompts), (0, ["move to the next tab"]))
        self.assertEqual(len(calls), 1)                             # the model's tab change ran
        self.assertEqual(again[2].prompts, ["move to the next tab"])
        self.assertEqual(said.getvalue().count("the panes bundle is not used"), 1)
        self.assertEqual([entry["engine"] for entry in self.entries()], ["the model", "the model"])

    def test_nothing_in_front_of_the_model_stops_a_request(self):
        # The second review: with the switch on and the reader not importable,
        # the request ended in an error. Whatever fails before the model, the
        # model reads the request.
        request = "move to the next tab"
        with mock.patch.dict(sys.modules, {"panes_bundle": None}), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as said:
            status, calls, model = self.ask(request, call("go_to_tab", tab="next"))
        self.assertEqual((status, model.prompts, len(calls)), (0, [request], 1))
        self.assertRegex(said.getvalue(), r"the panes bundle is not used \(\w*Error: ")
        with mock.patch.object(needle_cli, "checks_agree", side_effect=RuntimeError("anything at all")), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as said:
            status, calls, model = self.ask(request, call("go_to_tab", tab="next"))
        self.assertEqual((status, model.prompts, len(calls)), (0, [request], 1))
        self.assertIn("the panes bundle is not used (RuntimeError: anything at all)", said.getvalue())
        # The third review: a pipe where the manifest should be held the request up.
        if hasattr(os, "mkfifo"):
            folder = directory(self, packed())
            (folder / "manifest.json").unlink()
            os.mkfifo(folder / "manifest.json")
            PB._LOADED.clear()
            with mock.patch.object(PB, "DIRECTORY", folder), mock.patch("sys.stderr", new_callable=io.StringIO) as said:
                status, calls, model = self.ask(request, call("go_to_tab", tab="next"))
            self.assertEqual((status, model.prompts, len(calls)), (0, [request], 1))
            self.assertIn("the panes bundle is not used (BundleError: the bundle manifest is not a regular file)",
                          said.getvalue())

    def test_more_than_one_line_is_never_read(self):
        self.assertIsNone(PB.read("move to the next tab\nshut the left pane"))

    def test_a_proposal_the_checks_refuse_steps_aside_for_the_model(self):
        # The request gives no ground for closing anything: the checks refuse
        # the proposal, so the model reads the request as if no bundle stood there.
        refused = [call("close_pane", pane="left")]
        with mock.patch.object(PB, "read", return_value=(refused, "bundle by hand")):
            _, calls, model = self.ask("move to the next tab", call("go_to_tab", tab="next"))
        self.assertEqual(model.prompts, ["move to the next tab"])
        (entry,) = self.entries()
        self.assertEqual((entry["engine"], entry["calls"]), ("the model", [call("go_to_tab", tab="next")]))

    def test_the_reader_says_which_clause_each_call_was_read_from(self):
        bundle, texts = PB.Bundle(packed(free_text=["rename_tab"])), ["stale"]
        calls, _ = bundle.read("Rename this tab to Build  logs and move to the next tab", texts)
        self.assertEqual(calls, [call("rename_tab", name="Build  logs"), call("go_to_tab", tab="next")])
        self.assertEqual(texts, ["Rename this tab to Build  logs", "move to the next tab"])
        # nothing proposed: the list is left as it was
        self.assertEqual(bundle.read("rename this tab", texts)[0], [])
        self.assertEqual(texts, ["Rename this tab to Build  logs", "move to the next tab"])

    def test_the_checks_must_read_each_clause_as_they_read_the_whole(self):
        # The checks bind a "tab 2" found anywhere to a rename. The bundle
        # read it for the other call, so its right reading would rename tab 2.
        calls = [call("rename_tab", name="api"), call("go_to_tab", tab="2")]
        request, texts = "rename this tab to api and go to tab 2", ["rename this tab to api", "go to tab 2"]
        self.assertEqual(needle_cli._admitted(request, calls)[0], ("rename_tab", {"name": "api", "tab": "2"}))
        self.assertFalse(needle_cli.checks_agree(request, calls, texts))
        with mock.patch.object(PB, "read", side_effect=lambda _request, given: (given.extend(texts), (calls, "bundle by hand"))[1]):
            _, ran, model = self.ask(request)
        self.assertEqual((ran, model.prompts), ([], [request]))        # nothing ran; the model was asked
        # one call, or two that the checks read alike either way, are used
        self.assertTrue(needle_cli.checks_agree("rename this tab to api", calls[:1], texts[:1]))
        both = [call("go_to_tab", tab="next"), call("rename_tab", name="api")]
        self.assertTrue(needle_cli.checks_agree("move to the next tab and rename this tab to api", both,
                                                ["move to the next tab", "rename this tab to api"]))
        # without the clauses there is nothing to hold two calls to
        self.assertFalse(needle_cli.checks_agree("move to the next tab and rename this tab to api", both, []))

    def test_the_checks_must_admit_what_the_bundle_read(self):
        # The second review, 1.1: the checks bind a number they find in the
        # request. "call this tab 2" became a rename of tab 2 and "3 rows
        # taller" a resize of pane 3, and a single admitted call was used as
        # it came back. The bundle read a name and an amount, no tab and no
        # pane: where the checks admit something else than it read, the
        # model reads the request.
        for request, calls in (("call this tab 2", [call("rename_tab", name="2")]),
                               ("make this pane 3 rows taller", [call("resize_pane", direction="taller", amount=3)]),
                               # a copied title that the checks take for a side
                               ("switch to bottom", [call("go_to_tab", tab="bottom")])):
            self.assertIsNotNone(needle_cli._admitted(request, calls), request)
            self.assertFalse(needle_cli.checks_agree(request, calls, [request]), request)
            reads = lambda _request, given, calls=calls, request=request: (given.append(request), (calls, "bundle by hand"))[1]
            with mock.patch.object(PB, "read", side_effect=reads):
                _, ran, model = self.ask(request)
            self.assertEqual((ran, model.prompts), ([], [request]), request)     # nothing ran; the model was asked
        self.assertEqual(needle_cli._admitted("call this tab 2", [call("rename_tab", name="2")]),
                         [("rename_tab", {"name": "2", "tab": "2"})])
        # What the checks add to any such call, and how they spell a title or
        # a pane's number, is no other reading.
        for request, calls in (("maximize this pane", [call("maximize_pane")]),
                               ("make this pane wider", [call("resize_pane", direction="wider")]),
                               ("make this pane wider by 5", [call("resize_pane", direction="wider", amount=5)]),
                               ("rename this pane to api", [call("rename_pane", name="api")]),
                               ("close the Build pane", [call("close_pane", pane="Build")]),
                               ("go to pane 70", [call("go_to_pane", pane="70")]),
                               ("go to tab 2", [call("go_to_tab", tab="2")]),
                               ("go to the left pane", [call("go_to_pane", pane="left")])):
            self.assertTrue(needle_cli.checks_agree(request, calls, [request]), request)
        as_read = needle_cli._as_read
        self.assertTrue(as_read(call("resize_pane", direction="wider"), "resize_pane", {"direction": "wider", "amount": 2}))
        self.assertFalse(as_read(call("resize_pane", direction="wider"), "resize_pane", {"direction": "wider", "amount": 3}))
        self.assertFalse(as_read(call("maximize_pane"), "maximize_pane", {"pane": "current", "restore": 0}))
        self.assertFalse(as_read(call("maximize_pane"), "maximize_pane", {"pane": "left", "restore": False}))
        self.assertFalse(as_read(call("go_to_tab", tab="2"), "go_to_tab", {}))                  # dropped
        self.assertFalse(as_read(call("go_to_tab", tab="2"), "close_tab", {"tab": "2"}))        # another tool
        self.assertFalse(as_read(call("go_to_tab", tab="2"), "go_to_tab", {"tab": "id:2"}))     # a tab's number is not an id
        self.assertFalse(as_read(call("close_pane", pane="the build"), "close_pane", {"pane": "name:build"}))
        # the table of what the checks add is what they add today
        self.assertEqual(needle_cli._ADDED_TO_ANY, {("maximize_pane", "pane"): "current", ("maximize_pane", "restore"): False,
                                                    ("rename_pane", "pane"): "current", ("resize_pane", "amount"): 2})

    def test_a_list_it_reads_is_the_exact_reader_s_list(self):
        listed = ([call("list", what="panes", about="300")], "bundle by hand")
        with mock.patch.object(PB, "read", return_value=listed):
            status, ran, model = self.ask("list pane 300 and its title")
        self.assertEqual((status, model.prompts), (0, []))
        self.assertEqual(ran, [])                                   # nothing but a snapshot is asked of kitty
        (entry,) = self.entries()
        self.assertEqual(entry["engine"], "bundle by hand")
        exact = self.ask("list panes")[0:1]
        self.assertEqual(exact, (0,))
        # the same items as the exact reader's list of the panes
        self.assertEqual([item["kind"] for item in entry["items"]], [item["kind"] for item in self.entries()[-1]["items"]])
        self.assertTrue(entry["items"] and all(item["kind"] == "list_panes" for item in entry["items"]))

    def test_a_list_beside_another_call_is_never_used(self):
        both = ([call("list", what="panes"), call("close_pane", pane="left")], "bundle by hand")
        with mock.patch.object(PB, "read", return_value=both):
            _, ran, model = self.ask("list the panes and close the left one")
        self.assertEqual((ran, model.prompts), ([], ["list the panes and close the left one"]))

    def test_the_second_reader_s_answers_are_recorded_as_its_own(self):
        second = ([call("go_to_tab", tab="next")], "bundle by hand (second reader)")
        with mock.patch.object(PB, "read", return_value=second):
            status, ran, model = self.ask("move to the next tab")
        self.assertEqual((status, model.prompts, len(ran)), (0, [], 1))
        self.assertEqual(self.entries()[0]["engine"], "bundle by hand (second reader)")

    def test_the_evaluation_scores_a_list_as_the_exact_reader_s(self):
        import evaluate
        listed = ([call("list", what="tabs")], "bundle by hand")
        route = evaluate.PanesRoute(Model(call("close_tab", tab="current")), lambda calls: calls, True)
        with mock.patch.object(PB, "read", return_value=listed):
            self.assertEqual(route.complete("show the tabs with their titles"), {"function_calls": []})
        self.assertEqual(route.asked, {"exact": 0, "bundle": 1, "model": 0})
        # a pane's number is its id and a tab's id stays one, as the checks admit them
        self.assertEqual(evaluate.expected([["close_pane", {"pane": "70"}], ["close_pane", {"pane": "id:71"}],
                                            ["rename_tab", {"tab": "id:4", "name": "x"}],
                                            ["go_to_tab", {"tab": "pane:9"}], ["go_to_tab", {"tab": "2"}],
                                            ["go_to_tab", {"tab": "logs"}]]),
                         [["close_pane", {"pane": "id:70"}], ["close_pane", {"pane": "id:71"}],
                          ["rename_tab", {"name": "x", "tab": "id:4"}], ["go_to_tab", {"tab": "pane:9"}],
                          ["go_to_tab", {"tab": "2"}], ["go_to_tab", {"tab": "name:logs"}]])

    def test_the_evaluation_can_score_the_whole_route(self):
        import evaluate
        cases = [{"request": "close this pane", "expect": [["close_pane", {"pane": "current"}]]},
                 {"request": "move to the next tab", "expect": [["go_to_tab", {"tab": "next"}]]},
                 {"request": "go back to the previous tab", "expect": [["go_to_tab", {"tab": "previous"}]]}]
        for bundle, asked in ((True, {"exact": 1, "bundle": 1, "model": 1}),
                              (False, {"exact": 1, "bundle": 0, "model": 2})):
            # a model that always says "previous": right for the last case alone
            route = evaluate.PanesRoute(Model(call("go_to_tab", tab="previous")), lambda calls: calls, bundle)
            totals = evaluate.score(route, cases)["totals"]
            self.assertEqual(route.asked, asked)
            self.assertEqual((totals["exact"], totals["unsafe"]), (3 if bundle else 2, 0))

    def test_unset_the_switch_follows_the_default(self):
        os.environ.pop("KILIX_NEEDLE_BUNDLE")
        for default in (False, True):
            with mock.patch.object(needle_cli, "BUNDLE_BY_DEFAULT", default):
                self.assertEqual(needle_cli.bundle_enabled(), default)
                self.assertEqual(needle_cli.bundle_proposal("move to the next tab") is not None, default)
        os.environ["KILIX_NEEDLE_BUNDLE"] = "0"
        with mock.patch.object(needle_cli, "BUNDLE_BY_DEFAULT", True):
            self.assertIsNone(needle_cli.bundle_proposal("move to the next tab"))

    def test_only_a_plain_yes_switches_the_bundle_on(self):
        # "off", "false", an empty value or a typo must never leave it on.
        with mock.patch.object(needle_cli, "BUNDLE_BY_DEFAULT", True):
            for value in ("0", "off", "OFF", "false", "no", "", " ", "2", "enabled?"):
                os.environ["KILIX_NEEDLE_BUNDLE"] = value
                self.assertFalse(needle_cli.bundle_enabled(), value)
                self.assertIsNone(needle_cli.bundle_proposal("move to the next tab"), value)
        with mock.patch.object(needle_cli, "BUNDLE_BY_DEFAULT", False):
            for value in ("1", "on", "On", "true", "yes", " 1 "):
                os.environ["KILIX_NEEDLE_BUNDLE"] = value
                self.assertTrue(needle_cli.bundle_enabled(), value)
        os.environ["KILIX_NEEDLE_BUNDLE"] = "0"

    def test_the_default_is_off_until_the_review_is_answered(self):
        self.assertIs(needle_cli.BUNDLE_BY_DEFAULT, False)

    def test_switched_off_the_bundle_is_not_even_loaded(self):
        os.environ["KILIX_NEEDLE_BUNDLE"] = "0"
        with mock.patch.object(PB, "default", side_effect=AssertionError("the bundle was loaded")):
            self.assertIsNone(needle_cli.bundle_proposal("move to the next tab"))


class TheCheckedInBundleBeforeTheModel(Route):
    """The file that ships, through the whole route (the second review: only
    a hand-made bundle ever went through it)."""

    def standing_in_front(self):
        if not (PB.DIRECTORY / "manifest.json").is_file():
            self.skipTest("no bundle is checked in")
        return PB.Bundle.load()

    def test_what_the_second_review_found_goes_to_the_model_and_nothing_runs(self):
        for request in WhatTheReviewsFound.SECOND:
            _, ran, model = self.ask(request)
            self.assertEqual((ran, model.prompts), ([], [request]), request)

    def test_what_the_third_review_found_goes_to_the_model_and_nothing_runs(self):
        for request in WhatTheReviewsFound.THIRD:
            _, ran, model = self.ask(request)
            self.assertEqual((ran, model.prompts), ([], [request]), request)

    def test_what_it_answers_runs_where_the_request_says(self):
        # The caller is pane 300 in tab 3 (id 30); pane 301 is to its left, tab 2 has the id 20.
        split = ["launch", "--type=window", "--match=window_id:300", "--source-window=id:300", "--cwd=current",
                 "--next-to=id:300", "--location=vsplit"]
        for request, sent in {
                "rename this tab to api": [["set-tab-title", "--match=id:30", "api"]],
                "make this pane wider": [["resize-window", "--match=id:300", "--axis=horizontal", "--increment=2"]],
                "go to the left pane and rename the tab logs": [["focus-window", "--match=id:301"],
                                                                ["set-tab-title", "--match=id:30", "logs"]],
                "split right and go to tab 2": [split, ["focus-tab", "--match=id:20"]],
                "rename this tab to logs and split right": [["set-tab-title", "--match=id:30", "logs"], split]}.items():
            status, ran, model = self.ask(request)
            self.assertEqual((status, model.prompts), (0, []), request)
            self.assertEqual([args for args, _ in ran], sent, request)
            self.assertEqual(self.entries()[-1]["engine"], PB.default().label, request)

    def test_a_close_it_reads_still_asks(self):
        status, ran, model = self.ask("close the build pane", answers=["n"])
        self.assertEqual((status, ran, model.prompts), (1, [], []))
        self.assertRegex(self.said, r"close pane 301 .*\? \[y/N\]")

    def test_what_the_second_reader_answers_is_recorded_as_its_own(self):
        # The caller's tab is the third; moved to the first place through the caller's pane.
        status, ran, model = self.ask("shift the active tab to slot 1")
        self.assertEqual((status, model.prompts), (0, []))
        backward = ["action", "--match=id:300", "move_tab_backward"]
        self.assertEqual([args for args, _ in ran], [["focus-window", "--match=id:300"], backward, backward])
        self.assertEqual(self.entries()[-1]["engine"], PB.default().label + " (second reader)")
        # and what the first reader answers is not
        self.ask("rename this tab to api")
        self.assertEqual(self.entries()[-1]["engine"], PB.default().label)

    def test_a_list_it_reads_is_the_exact_readers_list(self):
        status, ran, model = self.ask("list pane 412 and its title")
        self.assertEqual((status, ran, model.prompts), (0, [], []))
        _, exact_ran, _ = self.ask("list panes")
        self.assertEqual(self.entries()[-1]["engine"], "control")
        self.assertEqual(self.entries()[-2]["items"], self.entries()[-1]["items"])


if __name__ == "__main__":
    unittest.main()
