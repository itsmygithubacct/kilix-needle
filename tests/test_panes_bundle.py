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


def packed(*, pairs=PAIRS, free_text=(), members=1, split=False, value_words=None) -> bytes:
    """The bundle file. `split`: the second member finds no tool where the first does."""
    made = [(seed, nets(tool_says_none=split and seed == 1)) for seed in range(members)]
    return PBT.pack(made, KNOWN, set(pairs), TOOLS, list(free_text), dim=2, hidden=4, value_words=value_words)


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
                         ([], "a word that always named a value is not in the answer"))
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
        for kept in (40, len(raw) - len(PB.MAGIC) - 4 - size - 6):     # header whole, the rest cut short
            cut = raw[:len(PB.MAGIC) + 4 + size + kept]
            with self.assertRaisesRegex(PB.BundleError, "size its header states"):
                PB.Bundle.load(directory(self, cut)).read("move to the next tab")

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
        # every kind that is risky whatever its arguments has its target held to trained words
        self.assertEqual({kind for kind in bundle.tools if Action(kind, {}).risky}, set(PB.KNOWN_TARGET))
        for kind, key in PB.KNOWN_TARGET.items():
            self.assertIn([key, "ref"], [spec[:2] for spec in bundle.tools[kind]["args"]])


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

    def ask(self, prompt, *model_calls, answers=()):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        saved = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = Terminal(answers), io.StringIO()
        try:
            with FakeKilix(desktop()) as fake:
                model = Model(*model_calls)
                status = needle_cli.handle(model, prompt, out=sys.stdout)
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
        self.assertEqual(self.entries()[0]["engine"], "bundle by hand")

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
