"""Resolution against a synthetic `kilix @ ls`, and the argv that runs."""
import copy
import os
import unittest

from support import FakeKilix, desktop, tab, window
from actions import Action
import kilix


class Caller:
    """Set KITTY_WINDOW_ID for the duration, as Kilix does for a pane."""

    def __init__(self, wid):
        self.wid = wid

    def __enter__(self):
        if self.wid is not None:
            os.environ["KITTY_WINDOW_ID"] = str(self.wid)
        return self

    def __exit__(self, *_exc):
        os.environ.pop("KITTY_WINDOW_ID", None)


def resolve(action, *, caller=300, tree=None):
    with Caller(caller):
        return kilix.resolve(action, kilix.Tree(tree or desktop()))


class CurrentMeansTheCaller(unittest.TestCase):
    def test_this_pane_is_where_needle_runs_not_the_viewed_tab(self):
        # Measured on the live desktop: run from a background tab, the first
        # version offered to close the pane the user was looking at.
        step = resolve(Action("close_pane", {"pane": "current"}), caller=300)
        self.assertEqual(step.commands, ((("close-window", "--match=id:300"), None),))

    def test_this_tab_is_the_callers_tab(self):
        step = resolve(Action("close_tab", {"tab": "current"}), caller=300)
        self.assertEqual(step.commands, ((("close-tab", "--match=id:30"), None),))

    def test_outside_a_pane_it_is_the_focused_one(self):
        step = resolve(Action("close_pane", {"pane": "current"}), caller=None)
        self.assertEqual(step.commands[0][0], ("close-window", "--match=id:200"))

    def test_relative_tabs_count_from_the_callers_tab(self):
        step = resolve(Action("go_to_tab", {"tab": "next"}), caller=300)
        self.assertEqual(step.commands[0][0], ("focus-tab", "--match=id:10"))  # wraps
        step = resolve(Action("go_to_tab", {"tab": "previous"}), caller=300)
        self.assertEqual(step.commands[0][0], ("focus-tab", "--match=id:20"))


class References(unittest.TestCase):
    def test_neighbours(self):
        step = resolve(Action("go_to_pane", {"pane": "left"}), caller=300)
        self.assertEqual(step.commands[0][0], ("focus-window", "--match=id:301"))
        with self.assertRaisesRegex(kilix.KilixError, "no pane right"):
            resolve(Action("go_to_pane", {"pane": "right"}), caller=300)

    def test_neighbours_are_group_ids_not_window_ids(self):
        # A window id where kitty puts a group id must not resolve: that is the
        # shape of the bug the live run found.
        tree = copy.deepcopy(desktop())
        tree[0]["tabs"][2]["windows"][0]["neighbors"] = {"left": [301]}
        with self.assertRaisesRegex(kilix.KilixError, "not listed"):
            resolve(Action("go_to_pane", {"pane": "left"}), tree=tree)

    def test_the_visible_window_of_a_group_is_its_last(self):
        tree = copy.deepcopy(desktop())
        tree[0]["tabs"][2]["groups"][1]["windows"] = [301, 302]   # 302 overlays 301
        step = resolve(Action("go_to_pane", {"pane": "left"}), tree=tree)
        self.assertEqual(step.commands[0][0], ("focus-window", "--match=id:302"))

    def test_two_neighbours_on_one_side_is_ambiguous(self):
        tree = copy.deepcopy(desktop())
        tree[0]["tabs"][2]["windows"][0]["neighbors"] = {"left": [9301, 9302]}
        with self.assertRaisesRegex(kilix.KilixError, "more than one"):
            resolve(Action("go_to_pane", {"pane": "left"}), tree=tree)

    def test_names_prefer_the_callers_tab_then_search_all(self):
        step = resolve(Action("go_to_pane", {"pane": "name:notes"}))
        self.assertEqual(step.commands[0][0], ("focus-window", "--match=id:302"))
        step = resolve(Action("go_to_pane", {"pane": "name:htop"}))
        self.assertEqual(step.commands[0][0], ("focus-window", "--match=id:201"))

    def test_ambiguous_and_missing_names_are_refused_with_candidates(self):
        with self.assertRaisesRegex(kilix.KilixError, "several panes: pane 301 .* pane 302"):
            resolve(Action("close_pane", {"pane": "name:bash"}))
        with self.assertRaisesRegex(kilix.KilixError, "no pane is called or running 'emacs'"):
            resolve(Action("close_pane", {"pane": "name:emacs"}))

    def test_tab_numbers_and_names(self):
        step = resolve(Action("close_tab", {"tab": "2"}))
        self.assertEqual(step.commands[0][0], ("close-tab", "--match=id:20"))
        self.assertIn("tab 2 'work' (2 panes)", step.summary)
        step = resolve(Action("go_to_tab", {"tab": "name:logs"}))
        self.assertEqual(step.commands[0][0], ("focus-tab", "--match=id:10"))
        with self.assertRaisesRegex(kilix.KilixError, "no tab 9; there are 3"):
            resolve(Action("close_tab", {"tab": "9"}))


class Commands(unittest.TestCase):
    def test_open_pane_is_anchored_to_the_caller(self):
        step = resolve(Action("open_pane", {"side": "left", "name": "notes",
                                            "program": "htop -d 5"}))
        # The tab is matched and the directory sourced from the anchor pane: a live
        # run without them opened the pane in the user's active tab instead.
        self.assertEqual(step.commands[0][0], (
            "launch", "--type=window", "--match=window_id:300", "--source-window=id:300",
            "--cwd=current", "--next-to=id:300",
            "--location=vsplit-before", "--title=notes", "--", "htop", "-d", "5"))

    def test_new_tab_takes_the_callers_directory(self):
        step = resolve(Action("open_tab", {}))
        self.assertEqual(step.commands[0][0], ("launch", "--type=tab",
                                               "--source-window=id:300", "--cwd=current"))

    def test_program_is_argv_never_a_shell(self):
        step = resolve(Action("open_tab", {"program": "echo $HOME; rm -rf x"}))
        argv = step.commands[0][0]
        self.assertEqual(argv[argv.index("--") + 1:], ("echo", "$HOME;", "rm", "-rf", "x"))
        with self.assertRaisesRegex(kilix.KilixError, "cannot split"):
            resolve(Action("open_tab", {"program": "echo 'unclosed"}))

    def test_layout_must_be_enabled_in_the_tab(self):
        step = resolve(Action("arrange_panes", {"layout": "grid"}))
        self.assertEqual(step.commands[0][0], ("goto-layout", "--match=id:30", "grid"))
        with self.assertRaisesRegex(kilix.KilixError, "fat layout is not enabled.*splits"):
            resolve(Action("arrange_panes", {"layout": "fat"}))

    def test_resize_sign_and_axis(self):
        step = resolve(Action("resize_pane", {"direction": "shorter", "amount": 4}))
        self.assertEqual(step.commands[0][0], ("resize-window", "--match=id:300",
                                               "--axis=vertical", "--increment=-4"))

    def test_rename_targets_the_callers_tab(self):
        step = resolve(Action("rename_tab", {"name": "build"}))
        self.assertEqual(step.commands[0][0], ("set-tab-title", "--match=id:30", "build"))


class Typing(unittest.TestCase):
    def test_broker_pane_is_matched_by_session_text_then_enter(self):
        step = resolve(Action("run_in_pane", {"pane": "left", "command": "make test"}))
        match = "--match=env:KITTY_PTY_BROKER_SESSION=" + "ab" * 8
        self.assertEqual(step.commands, ((("send-text", match, "--stdin"), b"make test"),
                                         (("send-text", match, "--stdin"), b"\r")))

    def test_plain_pane_is_matched_by_id(self):
        step = resolve(Action("run_in_pane", {"pane": "name:notes", "command": "ls"}))
        self.assertEqual(step.commands[0][0], ("send-text", "--match=id:302", "--stdin"))

    def test_never_types_into_a_pane_not_at_a_prompt(self):
        for pane in ("name:htop", "name:editor"):
            with self.assertRaisesRegex(kilix.KilixError, "not at a shell prompt"):
                resolve(Action("run_in_pane", {"pane": pane, "command": "q"}))

    def test_malformed_broker_session_falls_back_to_id(self):
        tree = copy.deepcopy(desktop())
        tree[0]["tabs"][2]["windows"][1]["env"]["KITTY_PTY_BROKER_SESSION"] = "x|id:1"
        step = resolve(Action("run_in_pane", {"pane": "left", "command": "ls"}), tree=tree)
        self.assertEqual(step.commands[0][0], ("send-text", "--match=id:301", "--stdin"))


class Perform(unittest.TestCase):
    def test_runs_exactly_the_resolved_argv_and_bytes(self):
        with FakeKilix(desktop()) as fake, Caller(300):
            step = kilix.resolve(Action("run_in_pane", {"pane": "left", "command": "make test"}),
                                 kilix.snapshot())
            kilix.perform(step)
            calls = fake.calls()
        match = "--match=env:KITTY_PTY_BROKER_SESSION=" + "ab" * 8
        self.assertEqual(calls, [(["send-text", match, "--stdin"], b"make test"),
                                 (["send-text", match, "--stdin"], b"\r")])

    def test_a_failing_command_is_reported(self):
        with FakeKilix(desktop()) as fake, Caller(300):
            os.environ["KILIX_NEEDLE_TEST_STATUS"] = "1"
            step = kilix.resolve(Action("go_to_tab", {"tab": "1"}), kilix.snapshot())
            with self.assertRaises(kilix.KilixError):
                kilix.perform(step)
            self.assertEqual(len(fake.calls()), 1)

    def test_empty_desktop_is_refused(self):
        with self.assertRaises(kilix.KilixError):
            kilix.Tree([])
        with self.assertRaises(kilix.KilixError):
            kilix.Tree([{"id": 1, "tabs": [tab(1, "x", [])]}])


if __name__ == "__main__":
    unittest.main()
