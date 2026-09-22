"""The checks between Needle's calls and anything that runs.

The first class replays calls the pinned engine actually produced; each was
wrong in a way that would have closed, typed or started something unasked.
"""
import unittest

import support  # noqa: F401
from actions import TOOLS, Action, Refusal, interpret


def call(tool, **arguments):
    return {"name": tool, "arguments": arguments}


class MeasuredMisreadings(unittest.TestCase):
    def test_go_to_read_as_close_is_refused(self):
        # engine: "go to the left pane" -> close_pane(left), confidence 0.41
        [result] = interpret("go to the left pane", [call("close_pane", pane="left")])
        self.assertIsInstance(result, Refusal)
        self.assertEqual(result.reason, "the request does not ask to close anything")

    def test_second_close_from_a_go_to_clause_is_refused(self):
        # engine: two close_tab calls at confidence 0.99
        first, second = interpret("close tab 2 and go to tab 1",
                                  [call("close_tab", tab="2"), call("close_tab", tab="1")])
        self.assertEqual(first, Action("close_tab", {"tab": "2"}))
        self.assertIsInstance(second, Refusal)

    def test_pane_close_read_as_tab_close_is_refused(self):
        # engine: "close the htop pane" -> close_tab(htop); tabs take their
        # active pane's title, so a tab called htop can exist
        [result] = interpret("close the htop pane", [call("close_tab", tab="htop")])
        self.assertIsInstance(result, Refusal)

    def test_garbage_argument_is_refused(self):
        # engine: "next tab" -> close_tab(",null,"), confidence 0.00
        [result] = interpret("next tab", [call("close_tab", tab=",null,")])
        self.assertIsInstance(result, Refusal)

    def test_invented_program_is_refused(self):
        # engine: "split the screen" -> open_pane(program=htop)
        [result] = interpret("split the screen",
                             [call("open_pane", side="below", program="htop")])
        self.assertIsInstance(result, Refusal)
        self.assertIn("not in the request", result.reason)

    def test_misfiled_command_word_is_refused(self):
        # engine: "run make test in the left pane" -> command "run"
        [result] = interpret("run make test in the left pane",
                             [call("run_in_pane", pane="left", command="run")])
        self.assertIsInstance(result, Refusal)
        self.assertIn("only the verb", result.reason)

    def test_command_text_does_not_name_a_pane(self):
        # engine: "split right and run htop" -> run_in_pane(pane=htop, command=htop)
        results = interpret("split right and run htop",
                            [call("open_pane", side="right"),
                             call("run_in_pane", pane="htop", command="htop")])
        self.assertEqual(results[0], Action("open_pane", {"side": "right"}))
        self.assertIsInstance(results[1], Refusal)
        [result] = interpret("run htop in the htop pane",
                             [call("run_in_pane", pane="htop", command="htop")])
        self.assertEqual(result, Action("run_in_pane", {"pane": "name:htop", "command": "htop"}))

    def test_a_bare_program_name_after_quit_is_the_program(self):
        # engine (held-out test set): "quit vim in the right pane" -> close_pane(vim)
        for prompt, item in (("quit vim in the right pane", call("close_pane", pane="vim")),
                             ("close htop", call("close_pane", pane="htop")),
                             ("kill server", call("close_tab", tab="server"))):
            [result] = interpret(prompt, [item])
            self.assertIsInstance(result, Refusal, prompt)
        for prompt, item in (("close the vim pane", call("close_pane", pane="vim")),
                             ("close the pane running vim", call("close_pane", pane="vim")),
                             ("kill the server tab", call("close_tab", tab="server")),
                             ("close the tab called logs", call("close_tab", tab="logs"))):
            [result] = interpret(prompt, [item])
            self.assertIsInstance(result, Action, prompt)

    def test_a_program_named_like_a_side_is_not_a_side(self):
        # engine (fresh bait): "kill top in the left pane" -> close_pane(top)
        [result] = interpret("kill top in the left pane", [call("close_pane", pane="top")])
        self.assertIsInstance(result, Refusal)
        for prompt, item in (("close the top pane", call("close_pane", pane="top")),
                             ("close the pane at the top", call("close_pane", pane="top")),
                             ("close the pane above", call("close_pane", pane="above")),
                             ("close tab number 3", call("close_tab", tab="3"))):
            [result] = interpret(prompt, [item])
            self.assertIsInstance(result, Action, prompt)

    def test_exiting_a_program_in_a_pane_is_not_closing_the_pane(self):
        # engine: "exit vim in the left pane" -> close_pane(left)
        [result] = interpret("exit vim in the left pane", [call("close_pane", pane="left")])
        self.assertIsInstance(result, Refusal)
        [result] = interpret("quit vim", [call("close_pane", pane="current")])
        self.assertIsInstance(result, Refusal)


class Admission(unittest.TestCase):
    def test_close_needs_a_closing_verb(self):
        for prompt in ("switch to the htop pane", "focus the pane above"):
            [result] = interpret(prompt, [call("close_pane", pane="current")])
            self.assertIsInstance(result, Refusal, prompt)

    def test_close_verbs(self):
        for verb in ("close", "kill", "quit", "exit", "shut"):
            [result] = interpret(f"{verb} this pane", [call("close_pane", pane="this")])
            self.assertEqual(result, Action("close_pane", {"pane": "current"}), verb)

    def test_the_pane_or_tab_is_the_object_of_the_close(self):
        for prompt, item in (("close this", call("close_pane", pane="current")),
                             ("kill the pane on the right", call("close_pane", pane="right")),
                             ("close the htop pane", call("close_pane", pane="htop")),
                             ("close the third tab", call("close_tab", tab="third"))):
            [result] = interpret(prompt, [item])
            self.assertIsInstance(result, Action, prompt)
        for prompt, item in (("close the file in the left pane", call("close_pane", pane="left")),
                             ("quit the job at tab 2", call("close_tab", tab="2")),
                             ("close tab 2", call("close_tab", tab="3"))):
            [result] = interpret(prompt, [item])
            self.assertIsInstance(result, Refusal, prompt)

    def test_close_pane_from_a_clause_naming_only_a_tab_is_refused(self):
        [result] = interpret("close the logs tab", [call("close_pane", pane="logs")])
        self.assertIsInstance(result, Refusal)

    def test_close_tab_needs_the_word_tab(self):
        [result] = interpret("close this", [call("close_tab", tab="current")])
        self.assertIsInstance(result, Refusal)
        [result] = interpret("close this tab", [call("close_tab", tab="this tab")])
        self.assertEqual(result, Action("close_tab", {"tab": "current"}))

    def test_run_needs_a_run_verb_and_its_target_in_the_same_clause(self):
        [result] = interpret("git status in the right pane",
                             [call("run_in_pane", pane="right", command="git status")])
        self.assertEqual(result.reason, "the request does not ask to run or type a command")
        [result] = interpret("go to the right pane and run ls",
                             [call("run_in_pane", pane="right", command="ls")])
        self.assertIsInstance(result, Refusal)
        [result] = interpret("type git status in the right pane",
                             [call("run_in_pane", pane="right", command="git status")])
        self.assertEqual(result, Action("run_in_pane",
                                        {"pane": "right", "command": "git status"}))

    def test_every_free_text_value_must_be_in_the_request(self):
        cases = [
            ("new tab", call("open_tab", name="logs")),
            ("rename this tab", call("rename_tab", name="build")),
            ("go to the logs tab", call("go_to_tab", tab="server")),
            ("run tests in the left pane", call("run_in_pane", pane="left", command="make")),
            ("make it wider", call("resize_pane", direction="wider", amount=10)),
        ]
        for prompt, item in cases:
            [result] = interpret(prompt, [item])
            self.assertIsInstance(result, Refusal, prompt)

    def test_unknown_tools_and_shapes_are_refused(self):
        results = interpret("anything", [call("delete_everything"), {"name": "open_tab"},
                                         "open_tab", call("arrange_panes", layout="spiral")])
        self.assertTrue(all(isinstance(item, Refusal) for item in results))
        self.assertEqual(interpret("anything", None), [])


class Normalisation(unittest.TestCase):
    def check(self, prompt, raw, expected):
        [result] = interpret(prompt, [raw])
        self.assertEqual(result, expected)

    def test_targets(self):
        self.check("switch to tab 3", call("go_to_tab", tab="tab 3"),
                   Action("go_to_tab", {"tab": "3"}))
        self.check("go to the third tab", call("go_to_tab", tab="third"),
                   Action("go_to_tab", {"tab": "3"}))
        self.check("next tab", call("go_to_tab", tab="next"),
                   Action("go_to_tab", {"tab": "next"}))
        self.check("go back a tab", call("go_to_tab", tab="back"),
                   Action("go_to_tab", {"tab": "previous"}))
        self.check("go to the pane on the right", call("go_to_pane", pane="the pane on the right"),
                   Action("go_to_pane", {"pane": "right"}))
        self.check("go up", call("go_to_pane", pane="up"),
                   Action("go_to_pane", {"pane": "above"}))
        self.check("switch to the htop pane", call("go_to_pane", pane="htop pane"),
                   Action("go_to_pane", {"pane": "name:htop"}))

    def test_program_loses_a_leading_verb(self):
        self.check("open a new tab running btop", call("open_tab", program="running btop"),
                   Action("open_tab", {"program": "btop"}))

    def test_empty_values_are_dropped(self):
        self.check("new tab", call("open_tab", name=""), Action("open_tab", {}))

    def test_a_name_that_only_repeats_the_unit_is_dropped(self):
        # engine: "new tab" -> open_tab(name="new tab")
        self.check("new tab", call("open_tab", name="new tab"), Action("open_tab", {}))
        self.check("open a new pane", call("open_pane", name="new pane"), Action("open_pane", {}))

    def test_resize_default_and_bounds(self):
        self.check("make the pane taller", call("resize_pane", direction="taller"),
                   Action("resize_pane", {"direction": "taller", "amount": 2}))
        [result] = interpret("make it wider by 99", [call("resize_pane", direction="wider",
                                                          amount=99)])
        self.assertIsInstance(result, Refusal)
        [result] = interpret("make it wider by 1", [call("resize_pane", direction="wider",
                                                         amount=True)])
        self.assertIsInstance(result, Refusal)


class Risk(unittest.TestCase):
    def test_what_waits_for_a_yes(self):
        self.assertTrue(Action("close_pane", {"pane": "current"}).risky)
        self.assertTrue(Action("close_tab", {"tab": "2"}).risky)
        self.assertTrue(Action("run_in_pane", {"pane": "left", "command": "ls"}).risky)
        self.assertTrue(Action("open_pane", {"program": "htop"}).risky)
        self.assertTrue(Action("open_tab", {"program": "btop"}).risky)
        for safe in (Action("open_pane", {"side": "right"}), Action("open_tab", {"name": "x"}),
                     Action("go_to_tab", {"tab": "next"}), Action("go_to_pane", {"pane": "left"}),
                     Action("arrange_panes", {"layout": "grid"}),
                     Action("rename_tab", {"name": "x"}),
                     Action("resize_pane", {"direction": "wider", "amount": 2})):
            self.assertFalse(safe.risky, safe)

    def test_tool_set_matches_admission(self):
        from actions import TOOL_NAMES
        self.assertEqual(TOOL_NAMES, {tool["name"] for tool in TOOLS})
        for tool in TOOLS:
            self.assertEqual(tool["parameters"]["type"], "object")


if __name__ == "__main__":
    unittest.main()
