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
        # A half-typed line is cut to the kill ring first (KN-R6-01).
        self.assertEqual(step.commands, ((("send-text", match, "--stdin"), b"\x05\x15"),
                                         (("send-text", match, "--stdin"), b"make test"),
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
        self.assertEqual(calls, [(["send-text", match, "--stdin"], b"\x05\x15"),
                                 (["send-text", match, "--stdin"], b"make test"),
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


class NameMatching(unittest.TestCase):
    """Review KN-04: names match exactly, or as a unique whole word of a title."""

    def tree(self):
        return kilix.Tree([{"id": 1, "is_active": True, "is_focused": True, "tabs": [
            tab(10, "logs", [window(100, "tail -f", "log", active=True)]),
            # The active pane is a third one: the requester's own pane is left
            # out of whole-word matching (KN-R5-01), and these cases are about
            # other panes' titles (mutant M69).
            tab(20, "work", [window(200, "pleb@host: ~/src/catalog (vim)", "vim"),
                             window(201, "make build", "bash"),
                             window(202, "shell", "bash", active=True)], active=True),
        ]}])

    def test_a_program_name_wins_over_a_title_substring_in_the_current_tab(self):
        self.assertEqual(self.tree().pane("name:log")["id"], 100)

    def test_path_and_host_fragments_of_a_title_are_not_names(self):
        for name in ("src", "host", "cat"):
            with self.subTest(name=name), self.assertRaises(kilix.KilixError):
                self.tree().pane(f"name:{name}")

    def test_a_unique_whole_word_of_a_title_still_names_its_pane(self):
        self.assertEqual(self.tree().pane("name:build")["id"], 201)

    def test_tabs_match_exactly_or_by_whole_word(self):
        self.assertEqual(self.tree().tab("name:logs")["id"], 10)
        with self.assertRaises(kilix.KilixError):
            self.tree().tab("name:log")


class TypedLength(unittest.TestCase):
    """Review KN-11: the MAX_TYPED cap had no test (reachable via `bridge`)."""

    def test_a_command_over_the_cap_is_never_typed(self):
        with self.assertRaisesRegex(kilix.KilixError, "too long"):
            resolve(Action("run_in_pane", {"pane": "name:build",
                                           "command": "x" * (kilix.MAX_TYPED + 1)}))
        step = resolve(Action("run_in_pane", {"pane": "name:build",
                                              "command": "x" * kilix.MAX_TYPED}))
        self.assertEqual(len(step.commands[1][1]), kilix.MAX_TYPED)


class ShellInFront(unittest.TestCase):
    """KN-09 / KN-R2-10: prompt marks are not enough; the state kilix reports
    out of band must agree."""

    def run_into(self, **changes):
        tree = copy.deepcopy(desktop())
        build = tree[0]["tabs"][2]["windows"][1]
        build.update(changes)
        return resolve(Action("run_in_pane", {"pane": "name:build", "command": "ls"}), tree=tree)

    def test_the_alternate_screen_or_a_foreground_program_refuses_typing(self):
        with self.assertRaisesRegex(kilix.KilixError, "not at a shell prompt"):
            self.run_into(in_alternate_screen=True)
        with self.assertRaisesRegex(kilix.KilixError, "not at a shell prompt"):
            self.run_into(foreground_processes=[{"cmdline": ["python3"], "pid": 1}])
        self.run_into(foreground_processes=[{"cmdline": ["-bash"], "pid": 1}])   # a login shell

    def test_an_exact_title_wins_over_a_whole_word_match(self):           # R17
        tree = kilix.Tree([{"id": 1, "is_active": True, "is_focused": True, "tabs": [
            tab(10, "a", [window(100, "notes", "bash", active=True)], active=True),
            tab(20, "b", [window(200, "old notes", "bash", active=True)])]}])
        self.assertEqual(tree.pane("name:notes")["id"], 100)


class ShellInFrontStrict(ShellInFront):
    def test_a_program_beside_the_shell_or_no_report_refuses(self):   # R3 K02, KN-R3-07
        with self.assertRaisesRegex(kilix.KilixError, "not at a shell prompt"):
            self.run_into(foreground_processes=[{"cmdline": ["bash"], "pid": 1},
                                                {"cmdline": ["python3"], "pid": 2}])
        with self.assertRaisesRegex(kilix.KilixError, "not at a shell prompt"):
            self.run_into(foreground_processes=[])


class AsciiTabNumbers(unittest.TestCase):
    def test_a_non_ascii_digit_is_not_a_tab_number(self):           # KN-R4-07, mutant M95
        tree = kilix.Tree(desktop())
        self.assertEqual(tree.tab("2")["id"], 20)
        with self.assertRaises(kilix.KilixError):
            tree.tab("٢")        # Arabic-Indic 2


class ReviewR5Resolution(unittest.TestCase):
    """KN-R5-01, -05 in resolution."""

    def self_titled(self):
        tree = copy.deepcopy(desktop())
        title = 'kn --yes "close the deploy tab"'
        tree[0]["tabs"][2]["title"] = title
        tree[0]["tabs"][2]["windows"][0]["title"] = title
        return tree

    def test_the_requesters_own_title_never_names_a_target(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        tree = kilix.Tree(self.self_titled())
        with self.assertRaises(kilix.KilixError):
            tree.tab("name:deploy")
        with self.assertRaises(kilix.KilixError):
            tree.pane("name:deploy")

    def test_a_whole_word_match_is_marked_fuzzy(self):
        tree = copy.deepcopy(desktop())
        tree[0]["tabs"][1]["windows"][1]["title"] = "make build"
        tree[0]["tabs"][2]["windows"][1]["title"] = "shell"
        step = resolve(Action("close_pane", {"pane": "name:build"}), tree=tree)
        self.assertTrue(step.fuzzy)
        self.assertFalse(resolve(Action("close_pane", {"pane": "name:notes"})).fuzzy)

    def test_a_close_never_wraps_round_the_tab_bar(self):
        os.environ["KITTY_WINDOW_ID"] = "300"      # the caller is in the last tab
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        with self.assertRaisesRegex(kilix.KilixError, "no next tab"):
            resolve(Action("close_tab", {"tab": "next"}))
        self.assertEqual(resolve(Action("go_to_tab", {"tab": "next"})).commands[0][0],
                         ("focus-tab", "--match=id:10"))


class ReviewR5Debatable(unittest.TestCase):
    def test_a_tab_titled_next_makes_the_keyword_ambiguous(self):
        tree = copy.deepcopy(desktop())
        tree[0]["tabs"][0]["title"] = "next"
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        with self.assertRaisesRegex(kilix.KilixError, "ambiguous"):
            kilix.Tree(tree).tab("next")

    def test_a_cased_title_wins_over_a_local_casefold_match(self):
        tree = copy.deepcopy(desktop())
        tree[0]["tabs"][1]["windows"][1]["title"] = "Build"
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        self.assertEqual(kilix.Tree(tree).pane("name:Build")["id"], 201)
        self.assertEqual(kilix.Tree(tree).pane("name:build")["id"], 301)


class ReviewR6Resolution(unittest.TestCase):
    """0.2.2 review R6's surviving mutants and findings, in resolution."""

    def setUp(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def tree(self, change=None):
        data = copy.deepcopy(desktop())
        if change:
            change(data)
        return kilix.Tree(data)

    def test_a_whole_word_tab_match_is_fuzzy(self):                      # F02
        def rename(d): d[0]["tabs"][1]["title"] = "deploy prod"
        step = kilix.resolve(Action("close_tab", {"tab": "name:deploy"}), self.tree(rename))
        self.assertTrue(step.fuzzy)

    def test_a_whole_word_typing_target_is_fuzzy(self):                   # F03
        def rename(d): d[0]["tabs"][2]["windows"][2]["title"] = "api server"
        step = kilix.resolve(Action("run_in_pane", {"pane": "name:api", "command": "make"}),
                             self.tree(rename))
        self.assertTrue(step.fuzzy)

    def test_every_keyword_titled_tab_is_ambiguous_in_any_case(self):     # K01, K02
        for title, ref in (("previous", "previous"), ("Last", "last"), ("NEXT", "next")):
            with self.subTest(title=title):
                def rename(d, title=title): d[0]["tabs"][0]["title"] = title
                with self.assertRaisesRegex(kilix.KilixError, "ambiguous"):
                    self.tree(rename).tab(ref)

    def test_no_wrap_for_a_close_either_way(self):                        # K03, KN-R6-02
        os.environ["KITTY_WINDOW_ID"] = "100"                            # tab 1, one pane
        with self.assertRaisesRegex(kilix.KilixError, "no previous tab"):
            kilix.resolve(Action("close_tab", {"tab": "previous"}), self.tree())
        os.environ["KITTY_WINDOW_ID"] = "300"                            # tab 3, first pane
        with self.assertRaisesRegex(kilix.KilixError, "no previous pane"):
            kilix.resolve(Action("close_pane", {"pane": "previous"}), self.tree())
        with self.assertRaisesRegex(kilix.KilixError, "no previous pane"):
            kilix.resolve(Action("run_in_pane", {"pane": "previous", "command": "ls"}), self.tree())
        self.assertEqual(kilix.resolve(Action("go_to_pane", {"pane": "previous"}), self.tree())
                         .commands[0][0], ("focus-window", "--match=id:302"))

    def test_a_lowercase_name_keeps_the_local_pane_first(self):          # N01
        def twin(d): d[0]["tabs"][1]["windows"][1]["title"] = "build"
        self.assertEqual(self.tree(twin).pane("name:build")["id"], 301)


class StillAtPrompt(unittest.TestCase):
    """R6 mutants R02-R04: the re-read before typing checks both signals and
    fails closed."""

    def setUp(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def check(self, **changes):
        from unittest import mock
        data = copy.deepcopy(desktop())
        data[0]["tabs"][2]["windows"][1].update(changes)
        with mock.patch.object(kilix, "snapshot", return_value=kilix.Tree(data)):
            return kilix.still_at_prompt(301)

    def test_both_signals_are_needed(self):
        self.assertTrue(self.check())
        self.assertFalse(self.check(at_prompt=False))                                    # R03
        self.assertFalse(self.check(foreground_processes=[{"cmdline": ["ssh", "h"], "pid": 1}]))   # R02

    def test_a_failed_read_is_not_ready(self):                                          # R04
        import needle_cli
        from unittest import mock
        with FakeKilix(desktop()) as fake, \
                mock.patch.object(kilix, "still_at_prompt", side_effect=kilix.KilixError("gone")):
            record = needle_cli.run_calls("run make in the build pane",
                                          [{"name": "run_in_pane",
                                            "arguments": {"pane": "build", "command": "make"}}],
                                          needle_cli.Options(assume_yes=True))
            self.assertEqual(fake.calls(), [])
        self.assertEqual(record["items"][0]["outcome"], "unresolved")
