"""One request end to end: engine reply -> checks -> resolution -> kilix calls."""
import io
import os
import sys
import unittest

from support import FakeKilix, desktop
import needle_cli


class ScriptedEngine:
    def __init__(self, *calls):
        self.calls = list(calls)
        self.resets = 0
        self.prompts = []

    def reset(self):
        self.resets += 1

    def complete(self, text):
        self.prompts.append(text)
        return {"type": "call", "function_calls": self.calls}


class Terminal(io.StringIO):
    """stdin that claims to be a terminal and answers with scripted lines."""

    def __init__(self, answers, tty=True):
        super().__init__("".join(f"{answer}\n" for answer in answers))
        self.tty = tty

    def isatty(self):
        return self.tty


def call(tool, **arguments):
    return {"name": tool, "arguments": arguments}


class Handle(unittest.TestCase):
    def run_request(self, prompt, *calls, answers=(), tty=True, dry_run=False, yes=False):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        saved = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = Terminal(answers, tty), io.StringIO()
        try:
            with FakeKilix(desktop()) as fake:
                engine = ScriptedEngine(*calls)
                status = needle_cli.handle(engine, prompt, dry_run=dry_run, assume_yes=yes,
                                           out=sys.stdout)
                return status, fake.calls(), sys.stdout.getvalue(), engine
        finally:
            sys.stdin, sys.stdout = saved

    def test_safe_action_runs_without_asking(self):
        status, calls, out, engine = self.run_request("next tab", call("go_to_tab", tab="next"))
        self.assertEqual((status, calls), (0, [(["focus-tab", "--match=id:10"], None)]))
        self.assertNotIn("[y/N]", out)
        self.assertEqual(engine.resets, 1)  # each request starts a fresh session

    def test_risky_action_runs_only_on_yes(self):
        request = ("close this pane", call("close_pane", pane="this"))
        status, calls, out, _ = self.run_request(*request, answers=["n"])
        self.assertEqual((status, calls), (1, []))
        self.assertIn("close pane 300", out)
        status, calls, _, _ = self.run_request(*request, answers=["y"])
        self.assertEqual((status, calls), (0, [(["close-window", "--match=id:300"], None)]))

    def test_risky_action_never_runs_without_a_terminal(self):
        status, calls, out, _ = self.run_request(
            "close this pane", call("close_pane", pane="this"), answers=["y"], tty=False)
        self.assertEqual((status, calls), (1, []))
        self.assertIn("not run without a terminal", out)

    def test_a_partial_refusal_holds_the_rest_for_a_yes(self):
        # A safe action from a request that was partly misread is not run on its own.
        status, calls, out, _ = self.run_request(
            "go to tab 1 and close the htop pane",
            call("go_to_tab", tab="1"), call("close_tab", tab="htop"), answers=["n"])
        self.assertEqual(calls, [])
        self.assertIn("refused close_tab", out)
        self.assertIn("nothing runs without a yes", out)

    def test_yes_runs_a_risky_action_without_a_terminal(self):
        status, calls, out, _ = self.run_request(
            "close the left pane", call("close_pane", pane="left"), tty=False, yes=True)
        self.assertEqual((status, calls), (0, [(["close-window", "--match=id:301"], None)]))
        self.assertNotIn("[y/N]", out)

    def test_yes_never_closes_the_requesters_own_pane_or_tab(self):      # KN-R5-01
        for prompt, args in (("close this pane", ("close_pane", {"pane": "this"})),
                             ("close this tab", ("close_tab", {"tab": "this tab"}))):
            with self.subTest(prompt=prompt):
                status, calls, out, _ = self.run_request(
                    prompt, call(args[0], **args[1]), tty=False, yes=True)
                self.assertEqual((status, calls), (1, []))
                self.assertIn("this request was made from", out)
        # A person at a terminal may still say yes to it.
        status, calls, _, _ = self.run_request(
            "close this pane", call("close_pane", pane="this"), answers=["y"])
        self.assertEqual(calls, [(["close-window", "--match=id:300"], None)])

    def test_yes_covers_only_a_plain_instruction(self):
        # Review R2: "close tab 1 or tab 2" closed tab 2 on --yes. Not plain, so
        # it waits for a person, who sees the target named in the question.
        status, calls, out, _ = self.run_request(
            "close tab 1 or tab 2", call("close_tab", tab="2"), tty=False, yes=True)
        self.assertEqual((status, calls), (1, []))
        self.assertIn("not a plain instruction", out)
        status, calls, out, _ = self.run_request(
            "close tab 1 or tab 2", call("close_tab", tab="2"), answers=["y"])
        self.assertEqual(calls, [(["close-tab", "--match=id:20"], None)])
        self.assertIn("close tab 2 'work'", out)
        status, calls, _, _ = self.run_request(
            "close tab 2", call("close_tab", tab="2"), tty=False, yes=True)
        self.assertEqual((status, calls), (0, [(["close-tab", "--match=id:20"], None)]))

    def test_yes_never_overrides_a_refusal(self):
        # The refused call stays refused, and what survived still needs a person.
        status, calls, out, _ = self.run_request(
            "go to tab 1 and close the htop pane",
            call("go_to_tab", tab="1"), call("close_tab", tab="htop"), tty=False, yes=True)
        self.assertEqual((status, calls), (1, []))
        self.assertIn("refused close_tab", out)

    def test_yes_does_not_skip_resolution_checks(self):
        status, calls, out, _ = self.run_request(
            "run q in the htop pane", call("run_in_pane", pane="htop", command="q"),
            tty=False, yes=True)
        self.assertEqual((status, calls), (1, []))
        self.assertIn("not at a shell prompt", out)

    def test_dry_run_runs_nothing(self):
        status, calls, out, _ = self.run_request(
            "close tab 2", call("close_tab", tab="2"), dry_run=True)
        self.assertEqual(calls, [])
        self.assertIn("would close tab 2 'work'", out)

    def test_unresolvable_action_is_reported_and_others_continue(self):
        status, calls, out, _ = self.run_request(
            "go to the emacs pane and make it a grid",
            call("go_to_pane", pane="emacs"), call("arrange_panes", layout="grid"))
        self.assertEqual(status, 1)
        self.assertIn("no pane is called or running 'emacs'", out)
        self.assertEqual(calls, [(["goto-layout", "--match=id:30", "grid"], None)])

    def test_no_calls_and_bad_prompts(self):
        status, calls, out, _ = self.run_request("what's the weather")
        self.assertEqual((status, calls), (0, []))
        self.assertIn("no pane or tab action", out)
        status, calls, out, engine = self.run_request("ac\\dc", call("open_tab"))
        self.assertEqual((status, calls, engine.prompts), (1, [], []))


if __name__ == "__main__":
    unittest.main()


class InstallCommands(unittest.TestCase):
    """What the tool tells people to run must exist (measured: `kilix models`
    is not a command on this Kilix; it fell through to the bonsai store)."""

    def test_runtime_installs_the_engine_and_the_runtime_only(self):
        from unittest import mock
        import asset
        with mock.patch.object(asset, "install", return_value="/x") as install, \
                mock.patch.object(asset, "_installed_spec",
                                  side_effect=asset.AssetError("not installed")), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(needle_cli.main(["install", "--runtime"]), 0)
        self.assertEqual([c.kwargs["asset_id"] for c in install.call_args_list],
                         ["needle2", "needle2-runtime"])

    def test_an_installed_asset_is_not_asked_about_again(self):
        from unittest import mock
        import asset

        def installed(asset_id, root):
            if asset_id == "needle2":
                return None, "/assets/needle2"
            raise asset.AssetError("the needle2-runtime licence has not been accepted")
        out = io.StringIO()
        with mock.patch.object(asset, "_installed_spec", side_effect=installed), \
                mock.patch.object(asset, "install", return_value="/assets/rt") as install, \
                mock.patch("sys.stdout", out):
            self.assertEqual(needle_cli.main(["install", "--runtime"]), 0)
        self.assertEqual([c.kwargs["asset_id"] for c in install.call_args_list],
                         ["needle2-runtime"])
        self.assertIn("already installed: /assets/needle2", out.getvalue())

    def test_each_missing_asset_names_a_command_that_installs_it(self):
        from unittest import mock
        import asset

        class LicenseError(Exception):
            pass
        lic = mock.Mock(LicenseError=LicenseError)
        first_use = mock.Mock(needs_agreement=mock.Mock(return_value=True))
        content = mock.Mock(CatalogError=LookupError)
        for asset_id, command in (("needle2", "kilix-needle install"),
                                  ("needle2-runtime", "kilix-needle install --runtime"),
                                  ("needle2-train", "kilix-needle install --tuning")):
            with mock.patch.object(asset, "_content", return_value=(content, first_use, lic)):
                with self.assertRaises(asset.AssetError) as caught:
                    asset._installed_spec(asset_id, None)
            self.assertTrue(str(caught.exception).endswith(f"install it with: {command}"),
                            caught.exception)


class EngineLicenceGate(unittest.TestCase):
    """Review KN-11: the engine never starts without an accepted licence."""

    def test_an_unaccepted_licence_stops_the_engine_at_start(self):
        from unittest import mock
        import asset

        class LicenseError(Exception):
            pass
        spec = mock.Mock(files=[])
        content = mock.Mock(CatalogError=LookupError)
        content.verified_packaged_catalog.return_value.require_asset.return_value = spec
        first_use = mock.Mock(needs_agreement=mock.Mock(return_value=True))
        lic = mock.Mock(LicenseError=LicenseError)
        with mock.patch.object(asset, "_content", return_value=(content, first_use, lic)):
            with self.assertRaisesRegex(asset.AssetError, "has not been accepted"):
                asset.from_installed()
        content.Installer.assert_not_called()


class OneSnapshotPerRequest(unittest.TestCase):
    """Review KN-R4-01: Kilix renumbers tabs at once, so "close tab 2 and close
    tab 3" must close the tabs that were 2 and 3 when the request was made."""

    def test_later_numbers_mean_the_desktop_the_request_was_made_on(self):
        from unittest import mock
        import kilix
        from support import tab, window
        tabs = [tab(10 * n, name, [window(100 * n, name, "bash", active=(n == 1))], active=(n == 1))
                for n, name in ((1, "shell"), (2, "logs"), (3, "work"), (4, "docs"))]
        before = [{"id": 1, "is_active": True, "is_focused": True, "tabs": tabs}]
        after = [{"id": 1, "is_active": True, "is_focused": True,
                  "tabs": [t for t in tabs if t["title"] != "logs"]}]
        os.environ["KITTY_WINDOW_ID"] = "100"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        performed = []
        with mock.patch.object(kilix, "snapshot",
                               side_effect=[kilix.Tree(before), kilix.Tree(after)] * 2), \
                mock.patch.object(kilix, "perform", side_effect=performed.append):
            record = needle_cli.run_calls(
                "close tab 2 and close tab 3",
                [call("close_tab", tab="2"), call("close_tab", tab="3")],
                needle_cli.Options(assume_yes=True))
        closed = [s.commands[0][0] for s in performed]
        self.assertEqual(closed, [("close-tab", "--match=id:20"), ("close-tab", "--match=id:30")],
                         record)

    def test_an_action_that_did_not_resolve_holds_the_rest(self):
        # Review KN-R4-04: go_to_tab(5) did not resolve and the close ran anyway.
        from unittest import mock
        import kilix
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        # Both clauses are plain, so only the failed first action can hold the
        # second (mutant M91: without that, the close ran on --yes).
        with FakeKilix(desktop()) as fake:
            record = needle_cli.run_calls(
                "close tab 9 and close tab 2",
                [call("close_tab", tab="9"), call("close_tab", tab="2")],
                needle_cli.Options(assume_yes=True), confirm=lambda _q: False)
            self.assertEqual(fake.calls(), [])
        self.assertEqual([i["outcome"] for i in record["items"]], ["unresolved", "skipped"])
        self.assertIn("earlier action", record["items"][1]["reason"])


class ReviewR5Run(unittest.TestCase):
    """KN-R5-01 (fuzzy never waived), KN-R5-02 (prompt re-read), N04 (own-guard breaks)."""

    def setUp(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def test_a_fuzzy_target_waits_for_a_person(self):
        import copy
        tree = copy.deepcopy(desktop())
        tree[0]["tabs"][1]["windows"][1]["title"] = "make build"
        tree[0]["tabs"][2]["windows"][1]["title"] = "shell"
        with FakeKilix(tree) as fake:
            record = needle_cli.run_calls("close the build pane", [call("close_pane", pane="build")],
                                          needle_cli.Options(assume_yes=True), confirm=lambda _q: False)
            self.assertEqual(fake.calls(), [])
        self.assertIn("word of its title", record["items"][0]["reason"])

    def test_a_second_command_after_a_program_started_is_not_typed(self):
        from unittest import mock
        import copy, kilix
        before = desktop()
        after = copy.deepcopy(before)
        build = after[0]["tabs"][2]["windows"][1]
        build.update(at_prompt=False, foreground_processes=[{"cmdline": ["vim", "notes.txt"], "pid": 1}])
        sent = []
        with mock.patch.object(kilix, "snapshot",
                               side_effect=[kilix.Tree(before), kilix.Tree(before), kilix.Tree(after)]), \
                mock.patch.object(kilix, "perform", side_effect=sent.append):
            record = needle_cli.run_calls(
                "run vim notes.txt in the build pane and run make in the build pane",
                [call("run_in_pane", pane="build", command="vim notes.txt"),
                 call("run_in_pane", pane="build", command="make")],
                needle_cli.Options(assume_yes=True))
        self.assertEqual(len(sent), 1)
        self.assertEqual(record["items"][1]["outcome"], "unresolved")

    def test_an_own_pane_refusal_holds_the_rest_for_an_agent(self):    # N04
        with FakeKilix(desktop()) as fake:
            record = needle_cli.run_calls(
                "close this tab and close tab 1",
                [call("close_tab", tab="this tab"), call("close_tab", tab="1")],
                needle_cli.Options(assume_yes=True, agent=True))
            self.assertEqual(fake.calls(), [])
        self.assertEqual(record["items"][0]["outcome"], "refused")


class ReviewR6Run(unittest.TestCase):
    def setUp(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def test_a_pane_no_longer_at_a_prompt_holds_the_rest(self):          # R05
        from unittest import mock
        import copy, kilix
        before = desktop()
        after = copy.deepcopy(before)
        after[0]["tabs"][2]["windows"][1].update(
            at_prompt=False, foreground_processes=[{"cmdline": ["vim"], "pid": 1}])
        sent = []
        with mock.patch.object(kilix, "snapshot",
                               side_effect=[kilix.Tree(before), kilix.Tree(before), kilix.Tree(after)]), \
                mock.patch.object(kilix, "perform", side_effect=sent.append):
            record = needle_cli.run_calls(
                "run vim in the build pane and run make in the build pane and close tab 2",
                [call("run_in_pane", pane="build", command="vim"),
                 call("run_in_pane", pane="build", command="make"), call("close_tab", tab="2")],
                needle_cli.Options(assume_yes=True), confirm=lambda _q: False)
        self.assertEqual(len(sent), 1)
        self.assertEqual([i["outcome"] for i in record["items"]], ["done", "unresolved", "skipped"])


class FuzzyAtTheGate(unittest.TestCase):
    """R6 mutant F03 (still alive at R7): the gate, not resolve(), must hold a
    whole-word typing target."""

    def test_a_whole_word_typing_target_is_not_waived(self):
        import copy
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        tree = copy.deepcopy(desktop())
        tree[0]["tabs"][2]["windows"][2]["title"] = "api server"
        with FakeKilix(tree) as fake:
            record = needle_cli.run_calls("run make in the api pane",
                                          [call("run_in_pane", pane="api", command="make")],
                                          needle_cli.Options(assume_yes=True), confirm=lambda _q: False)
            self.assertEqual(fake.calls(), [])
        self.assertIn("word of its title", record["items"][0]["reason"])


class ReviewR8Case(unittest.TestCase):
    """KN-R8-01's rows, end to end with hand-written calls: the local pane."""

    def test_mixed_case_mentions_act_on_the_local_pane(self):
        import copy
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        tree = copy.deepcopy(desktop())
        tree[0]["tabs"][1]["windows"][1]["title"] = "Build"          # another tab's Build
        with FakeKilix(tree) as fake:
            needle_cli.run_calls("run make in the build pane and close the Build pane",
                                 [call("run_in_pane", pane="build", command="make"),
                                  call("close_pane", pane="Build")],
                                 needle_cli.Options(assume_yes=True), confirm=lambda _q: False)
            ids = [a for argv, _ in fake.calls() for a in argv if a.startswith("--match")]
        self.assertNotIn("--match=id:201", ids)
