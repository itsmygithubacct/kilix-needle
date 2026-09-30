"""Canonical pane commands read without a model (token-cost finding 13)."""
import os
import unittest
from unittest import mock

from support import FakeKilix, desktop
import needle_cli
import panes_exact


class Forms(unittest.TestCase):
    def test_canonical_commands_become_calls(self):
        cases = {
            "Close the pane titled bench-target in this tab.": ("close_pane", {"pane": "bench-target"}),
            "close pane 70": ("close_pane", {"pane": "70"}),
            "close pane:70": ("close_pane", {"pane": "70"}),
            "please go to the logs pane": ("go_to_pane", {"pane": "logs"}),
            "focus pane 12": ("go_to_pane", {"pane": "12"}),
            "run 'make test' in the build pane": ("run_in_pane", {"pane": "build", "command": "make test"}),
            "type the command `touch /tmp/x` into pane bench-target and press Enter":
                ("run_in_pane", {"pane": "bench-target", "command": "touch /tmp/x"}),
            "close tab 2": ("close_tab", {"tab": "2"}),
            "go to tab 3, thanks": ("go_to_tab", {"tab": "3"}),
        }
        for request, (kind, args) in cases.items():
            with self.subTest(request=request):
                self.assertEqual(panes_exact.admitted(request)[0], [{"name": kind, "arguments": args}])

    def test_anything_more_is_left_to_the_model(self):
        for request in ("close the pane titled bench-target and tab 2", "don't close pane 70",
                        "close the pane titled bench-target if the build fails",
                        "close whichever pane", "close the pane titled", "run make tomorrow in the build pane",
                        "my boss said close tab 2", "close tab 2 - no wait, never mind"):
            with self.subTest(request=request):
                self.assertIsNone(panes_exact.admitted(request))


class LunaWordings(unittest.TestCase):
    """gpt-6-luna Needle rerun: the pane wordings that still reached the model."""
    def test_each_is_read_as_its_canonical_sentence(self):
        cmd = "touch /tmp/sent-1"
        cases = {
            'close the pane titled "bench-target" in this tab': "close the pane titled bench-target",
            f"In the pane titled bench-target in this tab, type `{cmd}` and press Enter.":
                f"run '{cmd}' in the pane titled bench-target",
            f'In the pane titled "bench-target" in this tab, type `{cmd}` at the shell prompt and press Enter.':
                f"run '{cmd}' in the pane titled bench-target",
            f"In the pane titled bench-target, type and run: {cmd}": f"run '{cmd}' in the pane titled bench-target",
            f"Type and press Enter: {cmd} in pane titled bench-target": f"run '{cmd}' in the pane titled bench-target",
            f"Run the command {cmd} in the pane titled bench-target in this tab.":
                f"run '{cmd}' in the pane titled bench-target",
            "Split right and run bash.": "split right",
            "Open a new pane directly to the right of the pane you are running in. It should run a shell.":
                "split right",
            "split right and run htop": "split right and run htop",
        }
        for request, sentence in cases.items():
            with self.subTest(request=request):
                self.assertEqual(panes_exact.admitted(request)[1], sentence)

    def test_an_unquoted_command_is_only_program_and_arguments(self):
        for request in ("Run touch tomorrow in pane bench-target", "run make if the tests pass in pane 2"):
            with self.subTest(request=request):
                self.assertIsNone(panes_exact.admitted(request))


class OpenPaneParts(unittest.TestCase):
    """A plain shell pane to one side, however an agent words it (luna-full A/B)."""

    def test_each_side_is_read_as_split(self):
        cases = {
            "open a new shell pane directly to the right of the pane I am running in": "split right",
            "split the pane I am running in to the right and open a shell in the new pane": "split right",
            "split this pane to the right": "split right",
            "Open a new terminal pane on the left side of my pane.": "split left",
            "open a pane below this one": "split below",
            "open up a new pane on the right": "split right",
        }
        for request, sentence in cases.items():
            with self.subTest(request=request):
                self.assertEqual(panes_exact.admitted(request)[1], sentence)

    def test_anything_more_is_left_to_the_model(self):
        for request in ("open up a new pane", "split the logs pane to the right", "open htop to the right",
                        "split right of pane 70", "open a pane to the right in tab 2", "do not split right",
                        "open a pane to the right or left", "open a shell to the right with vim",
                        "close the pane to the right", "move the pane to the right",
                        "open a pane to the right of the build pane", "open a pane up top"):
            with self.subTest(request=request):
                self.assertIsNone(panes_exact.admitted(request))


class Route(unittest.TestCase):
    def setUp(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def test_a_canonical_command_never_asks_the_model(self):
        engine = mock.Mock()
        with FakeKilix(desktop()):
            record = needle_cli.run_request(engine, "go to the notes pane", needle_cli.Options(dry_run=True))
        engine.complete.assert_not_called()
        self.assertEqual([(i["kind"], i["outcome"]) for i in record["items"]], [("go_to_pane", "would")])

    def test_an_agents_request_sent_to_the_panes_job_runs_as_the_agents_job(self):
        engine = mock.Mock()
        os.makedirs("/tmp/kn-agent-abc", exist_ok=True)
        with FakeKilix(desktop()):
            record = needle_cli.run_request(
                engine, "Start an interactive Codex coding-agent session in a new tab, working in the "
                        "directory /tmp/kn-agent-abc. Do not give it a task.", needle_cli.Options(dry_run=True))
        engine.complete.assert_not_called()
        self.assertEqual(record["job"], "agents")
        self.assertEqual([i["kind"] for i in record["items"]], ["agent"])

    def test_other_requests_still_go_to_the_model(self):
        engine = mock.Mock()
        engine.complete.return_value = {"function_calls": []}
        with FakeKilix(desktop()):
            needle_cli.run_request(engine, "could you bring the notes pane forward",
                                   needle_cli.Options(dry_run=True))
        engine.complete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
