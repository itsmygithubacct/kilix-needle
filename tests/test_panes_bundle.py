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
         "rename_tab": {"args": [["name", "text", None, None]], "required": ["name"]}}
KNOWN = {"move", "to", "the", "next", "tab", "this", "position", "rename", "shut", "pane", "left", "and", "htop"}
PAIRS = {"<s>_move", "move_to", "to_the", "the_next", "next_tab", "tab_</s>",          # move to the next tab
         "move_this", "this_tab", "tab_to", "to_position", "position_<num>", "<num>_</s>",
         "<s>_rename", "rename_this", "to_<x>", "<x>_</s>",                            # rename this tab to X
         "<s>_shut", "shut_the", "the_left", "left_pane", "pane_</s>", "the_<x>", "<x>_pane",
         "the_htop", "htop_pane"}                                                      # shut the htop pane
# Each tool's clauses as their answers read them: copied text is a mark for its argument.
FRAMES = {"go_to_tab": ["<s> move", "move to", "to the", "the next", "next tab", "tab </s>"],
          "move_tab": ["<s> move", "move this", "this tab", "tab to", "to position", "position <position#>",
                       "<position#> </s>"],
          "rename_tab": ["<s> rename", "rename this", "this tab", "tab to", "to <name>", "<name> </s>"],
          "close_pane": ["<s> shut", "shut the", "the left", "left pane", "pane </s>", "the <pane>", "<pane> pane"]}
GROUND = {"frames": FRAMES, "separators": ["and"], "clauses_max": 2,
          "copy_max": {"move_tab": {"position": 1}, "rename_tab": {"name": 2}, "close_pane": {"pane": 1}}}
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
        "arg rename_tab name": net([PB.ABSENT, PB.COPY], {"u=<x>": RIGHT}, {PB.COPY: first}, {PB.ABSENT: 1.0}),
        "span rename_tab name": net(["first", "last"], {"w+0=<x>": RIGHT, "w+1=</s>": UP},
                                    {"first": first, "last": [1.0, 1.0, 0.0, 0.0]}),
        "arg close_pane pane": net([PB.ABSENT, "left", PB.COPY], {"u=left": RIGHT, "u=<x>": UP, "u=htop": UP},
                                   {"left": first, PB.COPY: [0.0, 1.0, 0.0, 0.0]}, {PB.ABSENT: 1.0}),
        "span close_pane pane": net(["first", "last"], {"w+0=<x>": RIGHT, "w+0=htop": RIGHT},
                                    {"first": first, "last": first}),
    }


def packed(*, pairs=PAIRS, free_text=(), members=1, split=False, value_words=None, ground=GROUND,
           made=None) -> bytes:
    """The bundle file. `split`: the second member finds no tool where the first does."""
    made = made or [(seed, nets(tool_says_none=split and seed == 1)) for seed in range(members)]
    return PBT.pack(made, KNOWN, set(pairs), TOOLS, list(free_text), dim=2, hidden=4, value_words=value_words,
                    ground=ground)


def directory(case, raw, *, lie=False):
    folder = Path(tempfile.mkdtemp(prefix="kn-bundle-"))
    case.addCleanup(shutil.rmtree, folder, True)
    (folder / PB.FILE).write_bytes(raw)
    (folder / "manifest.json").write_text(json.dumps(
        {"schema": PB.MANIFEST_SCHEMA, "name": "by hand", "file": PB.FILE, "bytes": len(raw),
         "sha256": hashlib.sha256(raw + (b"x" if lie else b"")).hexdigest()}))
    return folder


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
        self.assertEqual(PB.Bundle(packed(value_words={"to": []})).read(request),
                         ([], "a word that names a value is not in the answer"))
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
        calls, reason = self.member.read("move to the next tab and rename this tab to api")
        self.assertEqual((calls, reason), ([call("go_to_tab", tab="next"), call("rename_tab", name="api")], "ok"))

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
        self.assertEqual(self.member.read(request), ([], "wording this tool was not trained on"))
        loose = PB.Bundle(packed(ground={**GROUND, "frames": {**FRAMES, "go_to_tab": FRAMES["go_to_tab"] + [
            "<s> shut", "shut the"]}}))
        self.assertEqual(loose.read(request)[0], [call("go_to_tab", tab="next")])     # what the rule declines

    def test_a_clause_is_framed_as_its_answer_reads_it(self):
        self.assertEqual(PB.frame_of(["go", "to", "tab", "two"], {"tab": (3, 3, True)}), ["go", "to", "tab", "<tab#>"])
        self.assertEqual(PB.frame_of(["the", "tab", "named", "two"], {"tab": (3, 3, False)}),
                         ["the", "tab", "named", "<tab>"])
        # copied text of any length is one mark
        self.assertEqual(PB.frame_of(["rename", "it", "to", "build", "logs", "now"], {"name": (3, 4, False)}),
                         ["rename", "it", "to", "<name>", "now"])
        self.assertEqual(PB.frame_pairs(["shut", "<pane>"]), ["<s> shut", "shut <pane>", "<pane> </s>"])
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
        no_number = {**FRAMES, "move_tab": [pair.replace("<position#>", "<position>") for pair in FRAMES["move_tab"]]}
        self.assertEqual(PB.Bundle(packed(ground={**GROUND, "frames": no_number})).read("move this tab to position 3"),
                         ([], "wording this tool was not trained on"))

    def test_only_a_word_that_joined_two_clauses_in_training_joins_two(self):
        request = "move to the next tab and rename this tab to api"
        self.assertEqual(len(self.member.read(request)[0]), 2)
        # the same net, the same sentence: "and" never joined two clauses in this one's training
        other = PB.Bundle(packed(ground={**GROUND, "separators": ["then"]}))
        self.assertEqual(other.read(request), ([], "clauses joined by a word that never joined two in training"))

    def test_no_more_clauses_than_a_training_row_had(self):
        two = "move to the next tab and rename this tab to api"
        self.assertEqual(self.member.read(two + " and move to the next tab"),
                         ([], "more clauses than any training row had"))
        one = PB.Bundle(packed(ground={**GROUND, "clauses_max": 1}))
        self.assertEqual(one.read(two), ([], "more clauses than any training row had"))
        self.assertEqual(one.read("move to the next tab")[0], [call("go_to_tab", tab="next")])

    def test_the_trainer_finds_each_tool_s_frames(self):
        tools = {"go_to_tab": {"args": [["tab", "ref", None, None]], "required": ["tab"]},
                 "rename_tab": {"args": [["name", "text", None, None], ["tab", "ref", None, None]], "required": ["name"]}}
        rows = [{"query": "go to tab two", "actions": [["go_to_tab", {"tab": "2"}]], "spans": [["two", "2"]]},
                {"query": "go to the tab named logs", "actions": [["go_to_tab", {"tab": "logs"}]],
                 "spans": [["logs", "logs"]]},
                {"query": "go to tab 3, then rename this tab to build logs",
                 "actions": [["go_to_tab", {"tab": "3"}], ["rename_tab", {"name": "build logs"}]],
                 "spans": [["3", "3"], ["build logs", "build logs"]]},
                # the title is not in the request: this clause has no frame
                {"query": "go to my usual tab", "actions": [["go_to_tab", {"tab": "logs"}]], "spans": []}]
        ground = PBT.ground([PBT.prepare(row, tools) for row in rows])
        self.assertEqual(ground["frames"]["go_to_tab"],
                         sorted(["<s> go", "go to", "to tab", "tab <tab#>", "<tab#> </s>",
                                 "to the", "the tab", "tab named", "named <tab>", "<tab> </s>"]))
        self.assertEqual(ground["frames"]["rename_tab"],
                         sorted(["<s> rename", "rename this", "this tab", "tab to", "to <name>", "<name> </s>"]))
        self.assertEqual((ground["separators"], ground["clauses_max"]), ([",", "then"], 2))
        self.assertEqual(ground["copy_max"], {"go_to_tab": {"tab": 1}, "rename_tab": {"name": 2}})

    def test_a_copy_is_no_longer_than_any_in_training(self):
        free = PB.Bundle(packed(free_text=["rename_tab"]))
        self.assertEqual(free.read("rename this tab to build logs")[0], [call("rename_tab", name="build logs")])
        # three words where two were the most: the rest of a sentence, not a title
        self.assertEqual(free.read("rename this tab to build logs now"),
                         ([], "a copied text longer than any in training"))
        one = PB.Bundle(packed(free_text=["rename_tab"], ground={**GROUND, "copy_max": {"rename_tab": {"name": 1}}}))
        self.assertEqual(one.read("rename this tab to build logs"), ([], "a copied text longer than any in training"))
        self.assertEqual(one.read("rename this tab to api")[0], [call("rename_tab", name="api")])

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
        self.assertEqual(loose.read("move to the next tab quickly"), ([], "an untrained word"))

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
        for missing in ("known", "tools", "separators", "clauses_max", "copy_max", "frames", "nets", "members",
                        "names", "pairs"):
            encoded = json.dumps({key: value for key, value in head.items() if key != missing}).encode()
            damaged = PB.MAGIC + len(encoded).to_bytes(4, "little") + encoded + raw[start + size:]
            with self.assertRaises(PB.BundleError, msg=missing):
                PB.Bundle.load(directory(self, damaged))
        old = PB.MAGIC.replace(b"3", b"2") + raw[len(PB.MAGIC):]
        with self.assertRaisesRegex(PB.BundleError, "not a panes bundle"):
            PB.Bundle.load(directory(self, old))

    def test_nothing_wrong_with_the_file_stops_a_request(self):
        # A fault that shows only while a request is read: the bundle is
        # given up for this process and the request goes on to the model.
        class Faulty:
            label = "bundle faulty"

            def read(self, request):
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

    def test_members_must_agree(self):
        agree = PB.Bundle.load(directory(self, packed(members=2)))
        self.assertEqual(len(agree.members), 2)
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
        self.assertTrue(bundle.members)
        calls, _ = bundle.read("split right and run htop")
        self.assertEqual(calls, [call("open_pane", side="right", program="htop")])
        # The trainer ran the whole reader over every training row before it
        # wrote the file: no risky call that a row's label does not hold.
        manifest = json.loads((PB.DIRECTORY / "manifest.json").read_text())
        self.assertEqual(sorted(manifest["code"]), ["panes_bundle.py", "panes_bundle_train.py"])
        self.assertEqual(manifest["reads_its_rows"]["risky_and_not_labelled"], 0)
        self.assertGreater(manifest["reads_its_rows"]["as_labelled"], 0)
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


class WhatTheReviewFound(unittest.TestCase):
    """Requests the bundle answered and misread before it held a clause to
    its own tool's wording (review of 2026-11-17). Each is made of words and
    word pairs the bundle knows. It must decline every one."""

    MISREAD = {
        # the caller's own tab was renamed; the request names another
        "rename the logs tab to api": "wording this tool was not trained on",
        # "logs" was passed over: go to the previous tab
        "back to the logs tab": "wording this tool was not trained on",
        # nothing asks for a layout: the panes were stacked
        "switch to the htop pane and all the others": "wording this tool was not trained on",
        # any word between two clauses was taken for "and"
        "go to tab 2 or rename this tab to api": "clauses joined by a word that never joined two in training",
        "go to tab 2 nope rename this tab to api": "clauses joined by a word that never joined two in training",
        "close tab 2 or go to tab 1": "clauses joined by a word that never joined two in training",
        # three clauses were never trained; two of these were dropped
        "split right, close this pane, split below": None,
        "close tab 1 and close tab 2 and go to tab 3": "more clauses than any training row had",
        # a title was read as a position
        "go to the tab named two": "wording this tool was not trained on",
        # another pane is named and the caller's own was resized
        "make the left pane wider": "a word that names a value is not in the answer",
        # found by this change's own probes: one of two words of place passed over
        "take me to the top pane to the left": "a word that names a value is not in the answer",
        "switch to grid, go in rows": "a word that names a value is not in the answer",
        # a pane named by its title and by its place: one of the two passed over
        "just type df -h in the build pane at the bottom for me": None,
        # the name swallowed the clauses after it
        "retitle the tab logs, call the pane above logs, tab name: frontend": "a copied text longer than any in training",
        # what to close is still never named by a word the bundle does not know
        "close the 3rd tab": "an untrained word names what to close or type into",
        # digits the nets take for a number and the reader cannot read as one
        "go to tab \uff13": "digits the reader does not read",
        "close tab \u0663": "digits the reader does not read",
        "make this pane wider by \u00b2": "digits the reader does not read",
    }
    STILL_READ = {
        "go to tab two": [call("go_to_tab", tab="2")],
        "rename this tab to api": [call("rename_tab", name="api")],
        "go to tab 2 and rename this tab to api": [call("go_to_tab", tab="2"), call("rename_tab", name="api")],
        "go to the logs tab": [call("go_to_tab", tab="logs")],
        "move to the next tab": [call("go_to_tab", tab="next")],
        "take me to the pane to the left": [call("go_to_pane", pane="left")],
        "arrange the panes one above another": [call("arrange_panes", layout="horizontal")],
        "rename this tab to build logs": [call("rename_tab", name="build logs")],
        # a number written with digits and an ordinal ending is that number, not a title
        "go to the 3rd tab": [call("go_to_tab", tab="3")],
        "move this tab to the 3rd position": [call("move_tab", position=3)],
    }

    def setUp(self):
        if not (PB.DIRECTORY / "manifest.json").is_file():
            self.skipTest("no bundle is checked in")
        self.bundle = PB.Bundle.load()

    def test_each_misread_request_is_declined(self):
        for request, why in self.MISREAD.items():
            calls, reason = self.bundle.read(request)
            self.assertEqual(calls, [], request)
            if why:
                self.assertEqual(reason, why, request)

    def test_ordinary_requests_are_still_read(self):
        for request, calls in self.STILL_READ.items():
            self.assertEqual(self.bundle.read(request), (calls, "ok"), request)

    def test_what_the_file_goes_by(self):
        self.assertEqual(self.bundle.clauses_max, 2)
        self.assertEqual(self.bundle.separators, {"and", "then", ","})
        self.assertEqual(self.bundle.copy_max["rename_tab"], {"name": 2})
        for word in ("left", "right", "top", "bottom", "next", "previous", "grid"):
            self.assertIn(word, self.bundle.value_words)
        self.assertNotIn("current", self.bundle.value_words)
        # no rename of a tab in training named the tab, and no resize the pane
        self.assertEqual(self.bundle.head["nets"]["arg rename_tab tab"]["outputs"], [PB.ABSENT])
        self.assertFalse(any("<tab" in pair for pair in self.bundle.frames("rename_tab")))


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


class BeforeTheModel(unittest.TestCase):
    def setUp(self):
        folder = directory(self, packed())
        saved = dict(PB._LOADED)
        PB._LOADED["bundle"] = PB.Bundle.load(folder)
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

            def read(self, request):
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


if __name__ == "__main__":
    unittest.main()
