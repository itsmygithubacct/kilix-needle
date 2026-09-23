"""The five-tool schema translates onto the ten internal actions, losslessly."""
import unittest

import support  # noqa: F401
from actions import LEGACY_TOOLS, Action, Refusal, interpret
import toolset


def call(tool, **arguments):
    return {"name": tool, "arguments": arguments}


class Translate(unittest.TestCase):
    def test_every_legacy_action_is_reachable(self):
        cases = [
            (call("open", kind="pane", side="right", program="htop", name="x"),
             call("open_pane", side="right", program="htop", name="x")),
            # A side on a tab passes through, for the checks to refuse: dropping it
            # silently hid a pane request misread as a tab (QAT run 3).
            (call("open", kind="tab", side="right", program="btop"),
             call("open_tab", side="right", program="btop")),
            (call("close", kind="pane", which="left"), call("close_pane", pane="left")),
            (call("close", kind="tab", which="2"), call("close_tab", tab="2")),
            (call("go_to", kind="pane", which="htop"), call("go_to_pane", pane="htop")),
            (call("go_to", kind="tab", which="next"), call("go_to_tab", tab="next")),
            (call("run_in_pane", pane="left", command="ls"), call("run_in_pane", pane="left", command="ls")),
        ]
        for five, ten in cases:
            self.assertEqual(toolset.to_actions([five]), [ten], five)
        got = toolset.to_actions([call("adjust", layout="grid", tab_name="api", resize="wider", amount=3)])
        self.assertEqual(got, [call("arrange_panes", layout="grid"), call("rename_tab", name="api"),
                               call("resize_pane", direction="wider", amount=3)])
        reached = {c["name"] for c in got} | {ten["name"] for _, ten in cases}
        self.assertEqual(reached, {t["name"] for t in LEGACY_TOOLS})

    def test_malformed_calls_reach_the_checks_and_are_refused(self):
        for bad in (call("open"), call("close", kind="window", which="x"), call("adjust"),
                    {"name": "open", "arguments": "pane"}, call("rm_rf")):
            results = interpret("close everything", toolset.to_actions([bad]))
            self.assertTrue(results and all(isinstance(r, Refusal) for r in results), bad)

    def test_the_checks_still_apply_after_translation(self):
        [result] = interpret("go to the left pane",
                             toolset.to_actions([call("close", kind="pane", which="left")]))
        self.assertIsInstance(result, Refusal)

    def test_five_tools_so_retrieval_never_drops_one(self):
        self.assertLessEqual(len(toolset.TOOLS), 5)


if __name__ == "__main__":
    unittest.main()


class Inverse(unittest.TestCase):
    def test_round_trip_for_every_action(self):
        actions = [["open_pane", {"side": "left", "program": "htop", "name": "x"}],
                   ["open_tab", {"program": "btop"}], ["close_pane", {"pane": "left"}],
                   ["close_tab", {"tab": "2"}], ["go_to_pane", {"pane": "htop"}],
                   ["go_to_tab", {"tab": "next"}], ["arrange_panes", {"layout": "grid"}],
                   ["rename_tab", {"name": "api"}], ["resize_pane", {"direction": "wider", "amount": 3}],
                   ["resize_pane", {"direction": "taller"}],
                   ["run_in_pane", {"pane": "left", "command": "ls"}]]
        back = toolset.to_actions(toolset.from_actions(actions))
        self.assertEqual([[c["name"], c["arguments"]] for c in back], actions)
