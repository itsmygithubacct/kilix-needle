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
                self.assertEqual(panes_exact.admitted(request), [{"name": kind, "arguments": args}])

    def test_anything_more_is_left_to_the_model(self):
        for request in ("close the pane titled bench-target and tab 2", "don't close pane 70",
                        "close the pane titled bench-target if the build fails",
                        "close whichever pane", "close the pane titled", "run make in the build pane",
                        "my boss said close tab 2", "close tab 2 - no wait, never mind"):
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

    def test_other_requests_still_go_to_the_model(self):
        engine = mock.Mock()
        engine.complete.return_value = {"function_calls": []}
        with FakeKilix(desktop()):
            needle_cli.run_request(engine, "could you bring the notes pane forward",
                                   needle_cli.Options(dry_run=True))
        engine.complete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
