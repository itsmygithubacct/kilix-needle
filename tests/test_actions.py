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

    def test_generated_phrasings_the_checks_once_refused(self):
        # Found by validating the tuning corpus against these checks.
        for prompt, item, want in (
                ("close the pane on top", call("close_pane", pane="above"), {"pane": "above"}),
                ("close the lower pane", call("close_pane", pane="below"), {"pane": "below"}),
                ("close tab two", call("close_tab", tab="two"), {"tab": "2"}),
                ("run uptime in the upper pane", call("run_in_pane", pane="upper", command="uptime"),
                 {"pane": "above", "command": "uptime"})):
            [result] = interpret(prompt, [item])
            self.assertIsInstance(result, Action, prompt)
            self.assertEqual(result.args, want, prompt)

    def test_a_value_must_be_whole_words_of_the_request(self):
        # tuning run 2: "stack the panes" -> go_to pane "a"
        [result] = interpret("stack the panes", [call("go_to_pane", pane="a")])
        self.assertIsInstance(result, Refusal)
        [result] = interpret("open a tab running top", [call("open_tab", program="to")])
        self.assertIsInstance(result, Refusal)
        [result] = interpret("go to tab 2 and run ls -la in the left pane",
                             [call("run_in_pane", pane="left", command="ls -la")])
        self.assertIsInstance(result, Action)

    def test_a_word_introduced_as_a_name_stays_a_name(self):
        # held-out v3: both were admitted as the wrong target
        [result] = interpret("close the pane running top", [call("close_pane", pane="top")])
        self.assertEqual(result, Action("close_pane", {"pane": "name:top"}))
        [result] = interpret("close the tab named two", [call("close_tab", tab="two")])
        self.assertEqual(result, Action("close_tab", {"tab": "name:two"}))
        [result] = interpret("close the top pane", [call("close_pane", pane="top")])
        self.assertEqual(result, Action("close_pane", {"pane": "above"}))
        [result] = interpret("close tab two", [call("close_tab", tab="two")])
        self.assertEqual(result, Action("close_tab", {"tab": "2"}))

    def test_the_introducing_word_is_not_part_of_the_name(self):
        [result] = interpret("close the pane running top", [call("close_pane", pane="running top")])
        self.assertEqual(result, Action("close_pane", {"pane": "name:top"}))
        [result] = interpret("go to the tab named logs", [call("go_to_tab", tab="named logs")])
        self.assertEqual(result, Action("go_to_tab", {"tab": "name:logs"}))

    def test_a_command_for_a_tab_is_not_typed_into_this_pane(self):
        # Only the pane/tab unit rule stops this one: the command span is right
        # and "current" needs no mention.
        [result] = interpret("run ls in tab 2", [call("run_in_pane", pane="current", command="ls")])
        self.assertIsInstance(result, Refusal)
        [result] = interpret("type make in the build tab",
                             [call("run_in_pane", pane="current", command="make")])
        self.assertIsInstance(result, Refusal)

    def test_a_truncated_command_is_refused(self):
        # engine (five-tool schema): "run make test in the right pane" -> command "test"
        [result] = interpret("run make test in the right pane",
                             [call("run_in_pane", pane="right", command="test")])
        self.assertIsInstance(result, Refusal)
        for prompt, command, pane in (("run make test in the right pane", "make test", "right"),
                                      ("type ls -la into the pane below", "ls -la", "below"),
                                      ("in the left pane run git log", "git log", "left"),
                                      ("run npm run dev in the notes pane", "npm run dev", "notes"),
                                      ("type uptime", "uptime", "current")):
            [result] = interpret(prompt, [call("run_in_pane", pane=pane, command=command)])
            self.assertIsInstance(result, Action, prompt)

    def test_a_filler_only_target_is_not_this_pane(self):
        # engine (five-tool schema): "type ls -la into the pane below" -> pane "pane", command "below"
        [result] = interpret("type ls -la into the pane below",
                             [call("run_in_pane", pane="pane", command="below")])
        self.assertIsInstance(result, Refusal)
        [result] = interpret("close the pane", [call("close_pane", pane="the pane")])
        self.assertIsInstance(result, Refusal)

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

    def test_a_tab_is_not_renamed_to_a_unit_word(self):
        [result] = interpret("make this pane wider by 10", [call("rename_tab", name="pane")])
        self.assertIsInstance(result, Refusal)

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


class BlindCorpusPhrasings(unittest.TestCase):
    """Phrasings for the four reversible actions, from kilix-ml's blind-authored
    corpus, that the checks refused; and the look-alikes that must stay refused."""

    def admit(self, prompt, tool, **arguments):
        [result] = interpret(prompt, [call(tool, **arguments)])
        self.assertIsInstance(result, Action, prompt)
        return result

    def refuse(self, prompt, tool, **arguments):
        [result] = interpret(prompt, [call(tool, **arguments)])
        self.assertIsInstance(result, Refusal, prompt)

    def test_rename_pane(self):
        for prompt in ("set this pane title to backend", "change the pane name to music",
                       "change this pane label to logs", "make this pane title read deploy",
                       "label this pane docs", "give the current pane the name api"):
            self.admit(prompt, "rename_pane", name=prompt.rsplit(" ", 1)[1])
        self.assertEqual(self.admit("set the title of the pane left to db", "rename_pane",
                                    pane="left", name="db").args, {"pane": "left", "name": "db"})
        self.refuse("set the tab title to logs", "rename_pane", name="logs")

    def test_swap_panes(self):
        for prompt, side in (("exchange this pane and the lower pane", "below"),
                             ("reverse the positions of this pane and the upper one", "above"),
                             ("trade places with the pane on the right", "right"),
                             ("swap with the left neighbor", "left"),
                             ("switch this pane with its neighbour below", "below")):
            self.admit(prompt, "swap_panes", side=side)
        for prompt in ("switch to the left pane", "swap this pane and close the left one",
                       "run swap in the left pane"):
            self.refuse(prompt, "swap_panes", side="left")

    def test_maximize_and_restore(self):
        for prompt in ("make this pane fill the window", "expand this pane to full size",
                       "show only the current pane"):
            self.admit(prompt, "maximize_pane")
        for prompt in ("restore the previous pane layout", "bring back the split view",
                       "return to the normal pane layout"):
            self.admit(prompt, "maximize_pane", restore=True)
        for prompt in ("zoom the tab", "make the tab fill the window",
                       "type fill the window in this pane"):
            self.refuse(prompt, "maximize_pane")


class NewActionGuards(unittest.TestCase):
    """Guards on the four reversible actions that no other test reached."""

    def test_a_tab_position_outside_one_to_nine_is_refused(self):
        for position in (0, 10, 12):
            [result] = interpret(f"move this tab to position {position}",
                                 [call("move_tab", position=position)])
            self.assertIsInstance(result, Refusal, position)
        [result] = interpret("move this tab to position 9", [call("move_tab", position=9)])
        self.assertIsInstance(result, Action)

    def test_a_swap_needs_a_pane(self):
        for prompt in ("swap left", "swap the files on the left", "exchange the left ones"):
            [result] = interpret(prompt, [call("swap_panes", side="left")])
            self.assertIsInstance(result, Refusal, prompt)


class ProgramStarts(unittest.TestCase):
    """A program is started as argv: it must be what the request asks to start.
    Each refused case is a measured base-model output."""

    def check(self, prompt, tool, **arguments):
        [result] = interpret(prompt, [call(tool, **arguments)])
        return result

    def test_misread_programs_are_refused(self):
        for prompt, tool, args in (
                ("split the screen", "open_tab", {"program": "screen"}),
                ("open the pod bay doors", "open_tab", {"program": "bay doors"}),
                ("type git status in the right pane", "open_pane", {"program": "type git"}),
                ("focus the left pane, then make it taller by 5", "open_pane", {"program": "left"}),
                ("switch to the next tab", "open_tab", {"program": "next"}),
                ("open a pane running watch and name it bottom", "open_pane", {"program": "bottom"}),
                ("run grep -rn pane src in the next pane", "open_pane", {"program": "grep -rn"}),
                ("new tab named echo", "open_tab", {"program": "echo"}),
                ("open a tab called run htop", "open_tab", {"program": "htop", "name": "run htop"})):
            self.assertIsInstance(self.check(prompt, tool, **args), Refusal, prompt)

    def test_open_starts_a_program_only_with_a_location(self):
        # "open X" alone does not say it is a program: the whole span is refused too.
        for prompt, program in (("open the pod bay doors", "the pod bay doors"),
                                ("open firefox", "firefox")):
            self.assertIsInstance(self.check(prompt, "open_tab", program=program), Refusal, prompt)
        self.assertIsInstance(self.check("open firefox in a new tab", "open_tab",
                                         program="firefox"), Action)

    def test_a_side_must_be_said_outside_the_program(self):
        self.assertIsInstance(self.check("open a pane running bottom", "open_pane",
                                         program="bottom", side="below"), Refusal)
        self.assertIsInstance(self.check("split the screen", "open_pane", side="below"), Refusal)

    def test_requested_programs_are_admitted(self):
        for prompt, tool, args in (
                ("open htop in a pane below", "open_pane", {"program": "htop", "side": "below"}),
                ("split right and run htop", "open_pane", {"program": "htop", "side": "right"}),
                ("open a new tab running btop", "open_tab", {"program": "btop"}),
                ("start ranger in a new tab", "open_tab", {"program": "ranger"}),
                ("open a tab with htop", "open_tab", {"program": "htop"}),
                ("open a pane running bottom", "open_pane", {"program": "bottom"}),
                ("open a tab called frontend running vim", "open_tab",
                 {"program": "vim", "name": "frontend"}),
                ("split right running htop -d 5 called mon", "open_pane",
                 {"program": "htop -d 5", "side": "right", "name": "mon"})):
            self.assertIsInstance(self.check(prompt, tool, **args), Action, prompt)


class SplitThenRun(unittest.TestCase):
    def test_split_and_run_is_one_pane(self):
        # tuned five-tool model: two opens for one request
        results = interpret("split right and run htop",
                            [call("open_pane", side="right"), call("open_pane", program="htop")])
        self.assertEqual(results, [Action("open_pane", {"side": "right", "program": "htop"})])

    def test_an_explicit_second_pane_stays_two(self):
        results = interpret("split right and open a pane running htop",
                            [call("open_pane", side="right"), call("open_pane", program="htop")])
        self.assertEqual(len(results), 2)
        results = interpret("split right, then split below",
                            [call("open_pane", side="right"), call("open_pane", side="below")])
        self.assertEqual(len(results), 2)


class SupplementPhrasings(unittest.TestCase):
    """From the independent supplement corpus: gerunds and "there"/"in it"."""

    def test_gerunds_are_verbs(self):
        [result] = interpret("would you mind closing the current tab",
                             [call("close_tab", tab="current")])
        self.assertEqual(result, Action("close_tab", {"tab": "current"}))
        [result] = interpret("would you mind typing ls -la in the upper pane",
                             [call("run_in_pane", pane="upper", command="ls -la")])
        self.assertEqual(result, Action("run_in_pane", {"pane": "above", "command": "ls -la"}))
        [result] = interpret("stop closing tabs", [call("close_tab", tab="current")])
        self.assertIsInstance(result, Refusal)

    def test_there_and_in_it_are_locations(self):
        # "there" is the new pane, which no action can name: typing into this
        # pane instead would be wrong, so it stays refused.
        [result] = interpret("split to the left and type cargo test there",
                             [call("run_in_pane", pane="current", command="cargo test")])
        self.assertIsInstance(result, Refusal)
        [result] = interpret("type cargo test in here",
                             [call("run_in_pane", pane="current", command="cargo test")])
        self.assertIsInstance(result, Action)
        [result] = interpret("open a tab titled logs and launch ranger in it",
                             [call("open_tab", name="logs", program="ranger")])
        self.assertEqual(result, Action("open_tab", {"name": "logs", "program": "ranger"}))

    def test_running_is_not_a_run_verb(self):
        [result] = interpret("go to the pane running top",
                             [call("run_in_pane", pane="current", command="top")])
        self.assertIsInstance(result, Refusal)


class TunedModelMisreads(unittest.TestCase):
    """Measured on the quantisation-aware tuned model (QAT run 3)."""

    def check(self, prompt, *calls):
        return interpret(prompt, list(calls))

    def test_a_name_must_be_said_as_a_name(self):
        [r] = self.check("split the window and start less",
                         call("open_pane", name="the window", program="less"))
        self.assertIsInstance(r, Refusal)
        for prompt, name in (("new tab for logs", "logs"), ("open a pane called notes", "notes"),
                             ("create a build tab", "build"), ("open a tab and name it api", "api")):
            [r] = self.check(prompt, call("open_tab" if "tab" in prompt else "open_pane", name=name))
            self.assertIsInstance(r, Action, prompt)

    def test_a_tab_is_opened_only_when_a_tab_is_asked_for(self):
        import toolset
        [r] = interpret("pop open a split above me with less in it", toolset.to_actions(
            [{"name": "open", "arguments": {"kind": "tab", "side": "above", "program": "less"}}]))
        self.assertIsInstance(r, Refusal)
        [r] = self.check("open a new split", call("open_tab"))
        self.assertIsInstance(r, Refusal)
        [r] = self.check("open htop in a new tab", call("open_pane", program="htop"))
        self.assertIsInstance(r, Refusal)

    def test_running_in_an_existing_pane_is_not_a_start(self):
        [r] = self.check("run ls -la in the right split",
                         call("open_pane", side="right", program="ls -la"))
        self.assertIsInstance(r, Refusal)
        [r] = self.check("open htop in a pane below", call("open_pane", side="below", program="htop"))
        self.assertIsInstance(r, Action)

    def test_an_implied_typed_command_must_look_like_one(self):
        [r] = self.check("type faster, I'm bored", call("run_in_pane", pane="current", command="faster"))
        self.assertIsInstance(r, Refusal)
        for prompt, command in (("type git status", "git status"), ("type ./build.sh", "./build.sh"),
                                ("type faster in this pane", "faster"),
                                ("type `faster` please", "faster")):
            [r] = self.check(prompt, call("run_in_pane", pane="current", command=command))
            self.assertIsInstance(r, Action, prompt)
