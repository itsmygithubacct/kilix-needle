"""Resolution against a synthetic `kilix @ ls`, and the argv that runs."""
import copy
import os
import unittest

from support import PROMPT, FakeKilix, desktop, tab, verified, window
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
    def test_prompt_marks_and_broker_identity_leave_shell_input_unverified(self):
        for pane, wid in (("left", 301), ("name:notes", 302), ("name:editor", 200)):
            with self.subTest(pane=pane), self.assertRaisesRegex(
                    kilix.ShellInputUnverified, f"pane {wid} .*manually"):
                resolve(Action("run_in_pane", {"pane": pane, "command": "pwd"}))

    def test_unmarked_shells_and_pane_metadata_cannot_authorize_submission(self):
        for shell in ("bash", "-dash", "sh", "zsh", "fish"):
            for at_prompt in (False, True, None):
                tree = desktop()
                window = tree[0]["tabs"][2]["windows"][1]
                window.update(at_prompt=at_prompt, foreground_processes=[{"cmdline": [shell]}],
                              user_vars={"shell_input_state": "verified"},
                              env={"SHELL_INPUT_STATE": "verified"})
                with self.subTest(shell=shell, mark=at_prompt), self.assertRaises(
                        kilix.ShellInputUnverified):
                    resolve(Action("run_in_pane", {"pane": "left", "command": "pwd"}), tree=tree)


def proven(tree=None, wid=301, proof=None):
    """The desktop with one pane reporting a verifiable prompt."""
    tree = tree or desktop()
    for t in tree[0]["tabs"]:
        for w in t["windows"]:
            if w["id"] == wid:
                w["shell_input"] = verified() if proof is None else proof
    return tree


class VerifiedSubmission(unittest.TestCase):
    """The engine's shell_input report is the only signal that permits a run."""

    def test_a_verified_prompt_resolves_to_one_guarded_submission(self):
        for pane, wid in (("left", 301), ("name:notes", 302)):
            with self.subTest(pane=pane):
                step = resolve(Action("run_in_pane", {"pane": pane, "command": "make 'a b' $X"}),
                               tree=proven(wid=wid))
                self.assertEqual(step.commands, (((
                    "shell-submit", f"--match=id:{wid}", f"--expect-prompt={PROMPT}"),
                    b"make 'a b' $X"),))
                self.assertIn("verified empty shell prompt", step.summary)
                self.assertEqual((step.verification["target_id"], step.verification["prompt_id"]),
                                 (wid, PROMPT))

    def test_an_unverifiable_report_is_refused_with_its_reason(self):
        for proof, words in (
                ({"verifiable": False, "code": "input", "reason": "input reached the pane after its prompt appeared"},
                 "input reached the pane"),
                ({"verifiable": False, "code": "unavailable", "reason": "no Kilix shell integration"},
                 "no Kilix shell integration"),
                ({"verifiable": True}, "reports no shell editor state"),
                ({"verifiable": True, "prompt_id": "../x"}, "reports no shell editor state"),
                ({"verifiable": "yes", "prompt_id": PROMPT}, "reports no shell editor state"),
                ("verified", "reports no shell editor state")):
            with self.subTest(proof=proof), self.assertRaisesRegex(
                    kilix.ShellInputUnverified, f"pane 301 .*{words}.*manually") as cm:
                resolve(Action("run_in_pane", {"pane": "left", "command": "pwd"}), tree=proven(proof=proof))
            if isinstance(proof, dict) and proof.get("code"):
                self.assertEqual(cm.exception.cause, proof["code"])

    def test_a_command_must_be_one_line(self):
        for command in ("pwd\nrm -rf x", "pwd\r", "echo \x1b[2J", "a\x7f"):
            with self.subTest(command=command), self.assertRaisesRegex(kilix.KilixError, "one shell command line"):
                resolve(Action("run_in_pane", {"pane": "left", "command": command}), tree=proven())

    def step(self, tree=None):
        return resolve(Action("run_in_pane", {"pane": "left", "command": "make test"}), tree=tree or proven())

    def test_submission_sends_the_command_on_stdin_and_reads_the_receipt(self):
        step = self.step()
        with FakeKilix(proven()) as fake, Caller(300):
            fake.answer(stdout=b'{"status": "submitted", "prompt_id": "%s", "window_id": 301}\n' % PROMPT.encode())
            receipt = kilix.perform(step)
            self.assertEqual(fake.calls(), [(["shell-submit", "--match=id:301", f"--expect-prompt={PROMPT}"],
                                             b"make test")])
        self.assertEqual((receipt["submitted"], receipt["target_id"], receipt["prompt_id"],
                          receipt["completion_verified"]), (True, 301, PROMPT, False))

    def test_a_changed_pane_is_refused_before_any_input(self):
        step = self.step()
        for change in ("pid", "closed", "broker"):
            tree = proven()
            build = tree[0]["tabs"][2]["windows"][1]
            if change == "pid":
                build["pid"] += 1
            elif change == "broker":
                build["env"]["KITTY_PTY_BROKER_SESSION"] = "cd" * 8
            else:
                tree[0]["tabs"][2]["windows"].remove(build)
            with self.subTest(change=change), FakeKilix(tree) as fake, Caller(300):
                with self.assertRaisesRegex(kilix.ShellInputUnverified, "changed after the request was planned") as cm:
                    kilix.perform(step)
                self.assertEqual((fake.calls(), cm.exception.cause), ([], "stale"))

    def test_engine_and_shell_refusals_keep_their_codes(self):
        step = self.step()
        for code, reason in (("stale", "the prompt changed after the request was planned"),
                             ("input", "input reached the pane after its prompt appeared"),
                             ("draft", "the shell editor already holds input; it was left untouched"),
                             ("busy", "the shell that reported the prompt is not in the foreground")):
            with self.subTest(code=code), FakeKilix(proven()) as fake, Caller(300):
                fake.answer(stderr=f"Error: {code}: {reason}\n".encode(), status=1)
                with self.assertRaisesRegex(kilix.ShellInputUnverified, f"pane 301 .*{reason}.*manually") as cm:
                    kilix.perform(step)
                self.assertEqual(cm.exception.cause, code)

    def test_an_engine_without_guarded_submission_is_a_refusal(self):
        with FakeKilix(proven()) as fake, Caller(300):
            fake.answer(stderr=b"Error: Unknown subcommand: shell-submit\n", status=1)
            with self.assertRaises(kilix.ShellInputUnverified) as cm:
                kilix.perform(self.step())
        self.assertEqual(cm.exception.cause, "unavailable")

    def test_an_unconfirmed_submission_is_never_reported_as_refused(self):
        for stdout, stderr, status in ((b"", b"Error: unconfirmed: the shell claimed the command\n", 1),
                                       (b"not json", b"", 0),
                                       (b'{"status": "submitted", "prompt_id": "other", "window_id": 301}', b"", 0),
                                       (b'{"status": "submitted", "prompt_id": "%s", "window_id": 302}'
                                        % PROMPT.encode(), b"", 0)):
            with self.subTest(stdout=stdout, stderr=stderr), FakeKilix(proven()) as fake, Caller(300):
                fake.answer(stdout=stdout, stderr=stderr, status=status)
                with self.assertRaises(kilix.ShellInputUnconfirmed):
                    kilix.perform(self.step())


class Perform(unittest.TestCase):
    def test_previously_resolved_typing_commands_cannot_reach_the_terminal(self):
        action = Action("run_in_pane", {"pane": "left", "command": "pwd"})
        old_step = kilix.Step(action, "run pwd in the build pane",
                              ((("send-text", "--match=id:301", "--stdin"), b"pwd\r"),))
        with FakeKilix(desktop()) as fake:
            with self.assertRaises(kilix.ShellInputUnverified):
                kilix.perform(old_step)
            self.assertEqual(fake.calls(), [])

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
        with self.assertRaises(kilix.ShellInputUnverified):
            resolve(Action("run_in_pane", {"pane": "name:build", "command": "x" * kilix.MAX_TYPED}))


class ExactTitle(unittest.TestCase):
    def test_an_exact_title_wins_over_a_whole_word_match(self):           # R17
        tree = kilix.Tree([{"id": 1, "is_active": True, "is_focused": True, "tabs": [
            tab(10, "a", [window(100, "notes", "bash", active=True)], active=True),
            tab(20, "b", [window(200, "old notes", "bash", active=True)])]}])
        self.assertEqual(tree.pane("name:notes")["id"], 100)


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

    def test_names_match_without_case_the_current_tab_first(self):   # KN-R8-01
        tree = copy.deepcopy(desktop())
        tree[0]["tabs"][1]["windows"][1]["title"] = "Build"
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        for ref in ("name:build", "name:Build", "name:BUILD"):
            with self.subTest(ref=ref):
                self.assertEqual(kilix.Tree(tree).pane(ref)["id"], 301)


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
        tree = self.tree(rename)
        with self.assertRaisesRegex(kilix.ShellInputUnverified, "pane 302"):
            kilix.resolve(Action("run_in_pane", {"pane": "name:api", "command": "make"}), tree)
        self.assertTrue(tree.fuzzy)

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


class ReviewR9Resolution(unittest.TestCase):
    """0.2.2 review R9: KN-R9-01, KN-R9-02 and the case mutants K03, K04, K06."""

    def setUp(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def tree(self, change=None):
        data = copy.deepcopy(desktop())
        if change:
            change(data)
        return kilix.Tree(data)

    def test_a_pane_called_by_a_keyword_makes_it_ambiguous(self):       # KN-R9-02
        for where, field, value, ref in (
                ((2, 1), "title", "next", "next"),         # this tab
                ((1, 1), "title", "Previous", "previous"),  # another tab, any case
                ((2, 2), "program", "next", "next"),        # a program called next
                ((2, 2), "title", "left", "left")):         # a neighbour word too
            with self.subTest(where=where, field=field, value=value):
                def rename(d, where=where, field=field, value=value):
                    w = d[0]["tabs"][where[0]]["windows"][where[1]]
                    if field == "title":
                        w["title"] = value
                    else:
                        w["foreground_processes"][0]["cmdline"] = [value]
                with self.assertRaisesRegex(kilix.KilixError, "ambiguous"):
                    self.tree(rename).pane(ref)
        self.assertEqual(self.tree().pane("previous")["id"], 302)   # control: no such pane

    def test_a_name_also_in_another_tab_is_marked(self):                 # KN-R9-01
        def twin(d): d[0]["tabs"][1]["windows"][1]["title"] = "Build"
        for ref in ("name:build", "name:Build"):
            with self.subTest(ref=ref):
                step = kilix.resolve(Action("close_pane", {"pane": ref}), self.tree(twin))
                self.assertEqual(step.closes, frozenset({301}))
                self.assertTrue(step.elsewhere)
                self.assertFalse(step.fuzzy)
        # Controls: a name only here, and a name only in another tab.
        self.assertFalse(kilix.resolve(Action("close_pane", {"pane": "name:build"}),
                                       self.tree()).elsewhere)
        step = kilix.resolve(Action("close_pane", {"pane": "name:htop"}), self.tree())
        self.assertEqual((step.closes, step.elsewhere), (frozenset({201}), False))

    def test_a_pane_title_matches_without_case(self):                    # K03
        def cap(d): d[0]["tabs"][2]["windows"][1]["title"] = "Build"
        step = kilix.resolve(Action("close_pane", {"pane": "name:build"}), self.tree(cap))
        self.assertEqual((step.closes, step.fuzzy, step.elsewhere), (frozenset({301}), False, False))

    def test_a_program_matches_without_case(self):                       # K04
        def cap(d): d[0]["tabs"][2]["windows"][2]["foreground_processes"][0]["cmdline"] = ["/usr/bin/Rsync"]
        step = kilix.resolve(Action("close_pane", {"pane": "name:rsync"}), self.tree(cap))
        self.assertEqual((step.closes, step.fuzzy), (frozenset({302}), False))

    def test_tab_titles_match_without_case(self):                        # K06
        def twins(d): d[0]["tabs"][0]["title"], d[0]["tabs"][1]["title"] = "build", "Build"
        with self.assertRaisesRegex(kilix.KilixError, "matches several tabs"):
            self.tree(twins).tab("name:build")
        def cap(d): d[0]["tabs"][0]["title"] = "Logs"
        tree = self.tree(cap)
        self.assertEqual(tree.tab("name:logs")["id"], 10)
        self.assertFalse(tree.fuzzy)


if __name__ == "__main__":
    unittest.main()
