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
KNOWN = {"move", "to", "the", "next", "tab", "this", "position", "rename", "shut", "pane", "left", "and", "htop"}
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
          "copied": {"pane": ["htop"]}}
RIGHT, UP, LEFT, DOWN = [4.0, 0.0], [0.0, 4.0], [-4.0, 0.0], [0.0, -4.0]   # one hidden unit each


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
    flat += [(biases or {}).get(output, 0.0) for output in outputs]
    return names, list(outputs), flat


def nets(tool_says_none=False) -> dict:
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
        # a name is any word the nets do not know, or "left"
        "arg rename_tab name": net([PB.ABSENT, PB.COPY], {"u=<x>": RIGHT, "u=left": RIGHT}, {PB.COPY: first},
                                   {PB.ABSENT: 1.0}),
        "span rename_tab name": net(["first", "last"], {"w+0=<x>": RIGHT, "w+0=left": RIGHT, "w+1=</s>": UP},
                                    {"first": first, "last": [1.0, 1.0, 0.0, 0.0]}),
        "arg close_pane pane": net([PB.ABSENT, "left", PB.COPY], {"u=left": RIGHT, "u=<x>": UP, "u=htop": UP},
                                   {"left": first, PB.COPY: [0.0, 1.0, 0.0, 0.0]}, {PB.ABSENT: 1.0}),
        "span close_pane pane": net(["first", "last"], {"w+0=<x>": RIGHT, "w+0=htop": RIGHT},
                                    {"first": first, "last": first}),
    }


def packed(*, pairs=PAIRS, free_text=(), members=2, split=False, value_words=None, ground=GROUND,
           made=None) -> bytes:
    """The bundle file. `split`: the second member finds no tool where the first does."""
    made = made or [(seed, nets(tool_says_none=split and seed == 1)) for seed in range(members)]
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
                ({"pane", "tab"}, [call("go_to_tab", tab="2"), call("go_to_pane", pane="logs")]),
                ({"pane"}, [call("go_to_pane", pane="left"), call("resize_pane", pane="left", direction="wider")]),
                ({"pane", "sides"}, [call("open_pane", side="right"), call("close_pane", pane="left"),
                                     call("maximize_pane", pane="left")]),
                ({"pane", "sides", "tab"}, [call("close_pane", pane="current"), call("maximize_pane", pane="70")]),
                ({"pane", "tab", "order"}, [call("open_tab"), call("close_tab", tab="2"), call("close_tab", tab="current")]),
                ({"sides"}, [call("arrange_panes", layout="grid"), call("swap_panes", side="left"), call("maximize_pane")]),
                ({"order"}, [call("move_tab", direction="left")]),
                ({"title api"}, [call("rename_tab", name="API"), call("rename_pane", name="api")]),
                ({"title build", "title logs"}, [call("rename_tab", name="Build  logs")]),
                ({"pane", "tab", "order", "title logs"}, [call("open_tab", name="logs")]),
                # a program that is started and a command that is typed are what the pane is called then
                ({"pane", "sides", "title htop"}, [call("open_pane", side="right", program="htop")]),
                ({"title make", "title test"}, [call("run_in_pane", pane="current", command="make test")]),
                (set(), [call("resize_pane", direction="wider")])):
            for one in calls:
                self.assertEqual(PB.changes(one), changed, one)
        # ... and what a call is resolved by
        for needed, refs, calls in (
                ({"tab"}, ["tab"], [call("rename_tab", name="api"), call("close_tab", tab="current")]),
                ({"pane"}, ["pane"], [call("resize_pane", direction="wider"), call("maximize_pane"),
                                      call("rename_pane", name="api"), call("close_pane", pane="current")]),
                ({"pane"}, [], [call("open_pane", side="right")]),
                ({"pane", "sides"}, ["pane"], [call("go_to_pane", pane="left"), call("go_to_pane", pane="next")]),
                ({"pane", "sides"}, [], [call("swap_panes", side="left")]),
                ({"tab", "order"}, ["tab"], [call("go_to_tab", tab="next"), call("go_to_tab", tab="last")]),
                ({"tab", "order"}, [], [call("move_tab", direction="left")]),
                ({"tab"}, [], [call("arrange_panes", layout="grid")]),
                ({"order"}, ["tab"], [call("go_to_tab", tab="2")]),
                ({"title logs"}, ["tab"], [call("go_to_tab", tab="Logs")]),
                # a pane with a title is looked for in the caller's tab first
                ({"title build", "title logs", "tab"}, ["pane"], [call("close_pane", pane="build logs")]),
                (set(), ["pane"], [call("close_pane", pane="70")]),
                (set(), [], [call("open_tab")])):
            for one in calls:
                self.assertEqual(PB.goes_by(one, refs), needed, one)

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
        # a word new to the bundle is what a name is made of
        self.assertEqual(free.read("rename this tab to Q3")[0], [call("rename_tab", name="Q3")])
        # copied as a pane's title is not copied as a name
        self.assertEqual(self.member.read("shut the htop pane")[0], [call("close_pane", pane="htop")])
        self.assertEqual(PB.Bundle(packed(ground={**GROUND, "copied": {"name": ["htop"]}})).read("shut the htop pane"),
                         ([], PB.PLAIN))

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

    def test_new_text_inside_a_copy_only_for_a_tool_listed_for_it(self):
        request = "rename this tab to build logs"
        self.assertEqual(self.member.read(request), ([], "an untrained word pair"))
        free = PB.Bundle(packed(free_text=["rename_tab"]))
        self.assertEqual(free.read(request)[0], [call("rename_tab", name="build logs")])
        # and never for a pair outside the copied text
        self.assertEqual(free.read("rename this next tab to build logs")[0], [])


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
                        "names", "pairs", "value_words", "copied"):
            encoded = json.dumps({key: value for key, value in head.items() if key != missing}).encode()
            damaged = PB.MAGIC + len(encoded).to_bytes(4, "little") + encoded + raw[start + size:]
            with self.assertRaises(PB.BundleError, msg=missing):
                PB.Bundle.load(directory(self, damaged))
        old = PB.MAGIC.replace(b"4", b"3") + raw[len(PB.MAGIC):]
        with self.assertRaisesRegex(PB.BundleError, "not a panes bundle"):
            PB.Bundle.load(directory(self, old))

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
        # another size than the manifest states: refused by its size, before a byte is read
        with mock.patch.object(PB.os, "read", side_effect=AssertionError("the file was read")), \
                self.assertRaisesRegex(PB.BundleError, "does not have the size"):
            PB.Bundle.load(directory(self, raw, bytes=len(raw) + 1))
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
                self.assertIn("panes_bundle: the bundle manifest is unreadable", said.getvalue())

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
        self.assertEqual(PBT.ROW_CODE, ("actions.py", "tuning.py", "toolset.py"))


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
        # nothing asks for a layout: the panes were stacked
        "switch to the htop pane and all the others": PB.UNSHOWN,
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
    STILL_READ = {
        "go to tab two": [call("go_to_tab", tab="2")],
        "rename this tab to api": [call("rename_tab", name="api")],
        "go to the logs tab": [call("go_to_tab", tab="logs")],
        "move to the next tab": [call("go_to_tab", tab="next")],
        "take me to the pane to the left": [call("go_to_pane", pane="left")],
        "arrange the panes one above another": [call("arrange_panes", layout="horizontal")],
        "rename this tab to build logs": [call("rename_tab", name="build logs")],
        "make this pane wider": [call("resize_pane", direction="wider")],
        "close the build pane": [call("close_pane", pane="build")],
        # a number written with digits and an ordinal ending is that number, not a title
        "go to the 3rd tab": [call("go_to_tab", tab="3")],
        # two clauses, the second resolved by nothing the first changes
        "rename this tab to api and split right": [call("rename_tab", name="api"), call("open_pane", side="right")],
        "make this pane wider and go to the left pane": [call("resize_pane", direction="wider"),
                                                         call("go_to_pane", pane="left")],
        "split right and go to tab 2": [call("open_pane", side="right"), call("go_to_tab", tab="2")],
        "split right and rename the tab to api": [call("open_pane", side="right"), call("rename_tab", name="api")],
        "go to tab 2 and close tab 1": [call("go_to_tab", tab="2"), call("close_tab", tab="1")],
        "close the build pane and arrange the rest in a grid": [call("close_pane", pane="build"),
                                                                call("arrange_panes", layout="grid")],
        # a rename that says what it renames, after a clause about the other kind
        "maximize this pane and rename this tab to logs": [call("maximize_pane"), call("rename_tab", name="logs")],
        "go to the left pane and rename the tab logs": [call("go_to_pane", pane="left"), call("rename_tab", name="logs")],
    }

    def setUp(self):
        if not (PB.DIRECTORY / "manifest.json").is_file():
            self.skipTest("no bundle is checked in")
        self.bundle = PB.Bundle.load()

    def used(self, request):
        """The calls the route would run for the bundle, or None: the reader and the wiring's hold on it."""
        texts: list = []
        calls, _ = self.bundle.read(request, texts)
        return calls if calls and needle_cli.checks_agree(request, calls, texts) else None

    def test_each_request_the_first_review_found_is_declined(self):
        for request, why in self.FIRST.items():
            calls, reason = self.bundle.read(request)
            self.assertEqual(calls, [], request)
            if why:
                self.assertEqual(reason, why, request)

    def test_no_request_the_second_review_found_is_answered(self):
        for request in self.SECOND:
            self.assertIsNone(self.used(request), request)
        # the reader does read "call this tab 2", as a tab named 2; the checks
        # make it tab 2, and the wiring does not use what the checks changed
        self.assertEqual(self.bundle.read("call this tab 2")[0], [call("rename_tab", name="2")])

    def test_ordinary_requests_are_still_read(self):
        for request, calls in self.STILL_READ.items():
            self.assertEqual(self.bundle.read(request), (calls, "ok"), request)
            self.assertEqual(self.used(request), calls, request)

    def test_what_the_file_goes_by(self):
        self.assertEqual(self.bundle.clauses_max, 2)
        self.assertEqual(self.bundle.separators, {"and", "then", ","})
        self.assertEqual(self.bundle.copy_max["rename_tab"], {"name": 2})
        for word in ("left", "right", "top", "bottom", "next", "previous", "grid"):
            self.assertIn(word, self.bundle.value_words)
        self.assertNotIn("current", self.bundle.value_words)
        # no rename of a tab in training named the tab, and no resize the pane
        self.assertEqual(self.bundle.head["nets"]["arg rename_tab tab"]["outputs"], [PB.ABSENT])
        self.assertFalse(any("<tab" in line for line in self.bundle.readings("rename_tab")))
        self.assertFalse(any("<pane" in line for line in self.bundle.readings("resize_pane")))
        # what a tool acts on is read from its name (acts_on): every tool is named for a tab or for panes
        self.assertTrue(all(tool.endswith(("_tab", "_pane", "_panes")) for tool in self.bundle.tools))
        self.assertEqual(set(PB.RENAMES), {tool for tool in self.bundle.tools if tool.startswith("rename_")})
        # every tool is in the table of what a call changes, or is one of the four that change nothing a later
        # call is resolved by but a title or the pane they name (changes)
        self.assertEqual(set(self.bundle.tools), set(PB._CHANGES) | {"rename_tab", "rename_pane", "resize_pane", "run_in_pane"})
        self.assertTrue(set(PB._GOES_BY) <= set(self.bundle.tools))
        # "go to it" only ever followed a clause that opens a pane: it is a reading there and nowhere else
        go_to_it = "go to it".split()
        self.assertIn(PB.reading_line("open_pane", go_to_it, {"pane": "below"}), self.bundle.readings("go_to_pane"))
        self.assertNotIn(PB.reading_line("", go_to_it, {"pane": "below"}), self.bundle.readings("go_to_pane"))
        # names are made of words that were copied as names
        self.assertIn("logs", self.bundle.copied["name"])
        self.assertNotIn("please", self.bundle.copied["name"])
        self.assertIn("please", self.bundle.known)


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
                "rename this tab to api and split right": [["set-tab-title", "--match=id:30", "api"], split]}.items():
            status, ran, model = self.ask(request)
            self.assertEqual((status, model.prompts), (0, []), request)
            self.assertEqual([args for args, _ in ran], sent, request)
            self.assertEqual(self.entries()[-1]["engine"], PB.default().label, request)

    def test_a_close_it_reads_still_asks(self):
        status, ran, model = self.ask("close the build pane", answers=["n"])
        self.assertEqual((status, ran, model.prompts), (1, [], []))
        self.assertRegex(self.said, r"close pane 301 .*\? \[y/N\]")


if __name__ == "__main__":
    unittest.main()
