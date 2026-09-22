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
            "close this pane", call("close_pane", pane="this"), tty=False, yes=True)
        self.assertEqual((status, calls), (0, [(["close-window", "--match=id:300"], None)]))
        self.assertNotIn("[y/N]", out)

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
