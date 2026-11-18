"""Reading another pane's screen, or its last lines, by pane id (pane_reads)."""
import io
import json
import os
import shutil
import sys
import unittest
from unittest import mock

from support import FakeKilix, desktop
import history
import kilix
import needle_cli
import pane_reads
import panes_bundle as PB


class Grammar(unittest.TestCase):
    def test_the_forms_it_reads(self):
        for request, args in (
                ("read pane 301", {"pane": 301}),
                ("Read pane 301.", {"pane": 301}),
                ("show pane 301's screen", {"pane": 301}),
                ("capture pane id 301", {"pane": 301}),
                ("show me the screen of pane 301", {"pane": 301}),
                ("read the text in pane 301", {"pane": 301}),
                ("get the output of pane 301", {"pane": 301}),
                ("show the last 40 lines of pane 301", {"pane": 301, "lines": 40}),
                ("read the last 5 lines in pane 301's output", {"pane": 301, "lines": 5}),
                ("what's on pane 301", {"pane": 301}),
                ("what is on pane 301's screen?", {"pane": 301}),
                ("please read pane 301, thanks", {"pane": 301}),
                ("can you show the last 1000 lines of pane 7", {"pane": 7, "lines": 1000})):
            self.assertEqual(pane_reads.parse(request), args, request)

    def test_what_it_does_not_read(self):
        for request in ("read pane", "read the build pane", "read this pane", "close pane 301",
                        "read pane 301 and close it", "read pane 301 then pane 302",
                        "show the last 0 lines of pane 301", "show the last 1001 lines of pane 301",
                        "read pane 0", "read pane 301\nrm -rf /", "tail pane 301",
                        "show the last lines of pane 301", "read pane -301", "",
                        "read pane " + "9" * 10, None, 301):
            self.assertIsNone(pane_reads.parse(request), request)

    def test_the_arguments_it_checks(self):
        self.assertIsNone(pane_reads.check_args({"pane": 3}))
        self.assertIsNone(pane_reads.check_args({"pane": 3, "lines": 1000}))
        for args in ({}, {"lines": 3}, {"pane": "3"}, {"pane": 3, "lines": 0}, {"pane": 3, "lines": 1001},
                     {"pane": True}, {"pane": 3, "extent": "all"}, {"pane": 0}, None, [3]):
            self.assertIsNotNone(pane_reads.check_args(args), args)

    def test_the_pane_must_be_in_the_tree(self):
        tree = kilix.Tree(desktop())
        self.assertIsNone(pane_reads.check({"pane": 301}, tree))
        self.assertIn("no pane 999", pane_reads.check({"pane": 999}, tree))

    def test_the_tool_is_not_the_model_s(self):
        import toolset
        self.assertNotIn(pane_reads.NAME, [tool["name"] for tool in toolset.TOOLS])


class Reading(unittest.TestCase):
    def test_the_screen_or_its_last_lines(self):
        asked = []

        def run(argv):
            asked.append(argv)
            return "one\ntwo\nthree\n\n   \n"

        self.assertEqual(pane_reads.read({"pane": 4}, run)["text"], "one\ntwo\nthree")
        self.assertEqual(asked[-1], ["get-text", "--match", "id:4", "--extent", "screen"])
        got = pane_reads.read({"pane": 4, "lines": 2}, run)
        self.assertEqual(asked[-1], ["get-text", "--match", "id:4", "--extent", "all"])
        self.assertEqual((got["text"], got["clipped"], got["untrusted"]), ("two\nthree", False, True))

    def test_a_long_screen_is_clipped_to_its_end(self):
        text = "x" * (pane_reads.MAX_BYTES + 10) + "\nlast"
        got = pane_reads.read({"pane": 4}, lambda argv: text)
        self.assertTrue(got["clipped"])
        self.assertTrue(got["text"].endswith("last"))
        self.assertLessEqual(len(got["text"].encode()), pane_reads.MAX_BYTES)


class Route(unittest.TestCase):
    """The panes route on the tests' fake desktop."""

    def setUp(self):
        shutil.rmtree(history.directory(), ignore_errors=True)
        before = os.environ.get("KILIX_NEEDLE_BUNDLE")
        os.environ["KILIX_NEEDLE_BUNDLE"] = "0"
        self.addCleanup(os.environ.__setitem__, "KILIX_NEEDLE_BUNDLE", before or "0")

    def ask(self, request, **modes):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)
        saved = sys.stdout
        sys.stdout = io.StringIO()
        model = mock.Mock()
        model.complete.side_effect = AssertionError("the model is not asked")
        try:
            with FakeKilix(desktop()) as fake:
                status = needle_cli.handle(model, request, out=sys.stdout, **modes)
                return status, fake.calls(), sys.stdout.getvalue()
        finally:
            sys.stdout = saved

    def entries(self):
        path = history.directory() / "requests.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_a_read_is_one_get_text_and_no_model(self):
        status, calls, _ = self.ask("show the last 20 lines of pane 301", agent=True)
        self.assertEqual(status, 0)
        self.assertEqual(calls, [(["get-text", "--match", "id:301", "--extent", "all"], None)])
        (entry,) = self.entries()
        self.assertEqual([item["kind"] for item in entry["items"]], ["read_pane"])

    def test_a_plan_reads_nothing(self):
        status, calls, _ = self.ask("read pane 301", dry_run=True, agent=True)
        self.assertEqual((status, calls), (0, []))

    def test_a_pane_that_is_not_there_is_refused(self):
        status, calls, said = self.ask("read pane 999", agent=True)
        self.assertEqual((status, calls), (1, []))
        self.assertIn("no pane 999", said)

    def test_the_bundle_s_read_is_checked_the_same(self):
        os.environ["KILIX_NEEDLE_BUNDLE"] = "1"
        for args, extent, status in (({"pane": 302, "lines": 5}, "all", 0), ({"pane": 999}, None, 1)):
            proposed = ([{"name": "read_pane", "arguments": args}], "bundle by hand")
            with mock.patch.object(PB, "read", return_value=proposed):
                got, calls, _ = self.ask("let me see what the notes pane says", agent=True)
            self.assertEqual(got, status)
            self.assertEqual(calls, [(["get-text", "--match", f"id:{args['pane']}", "--extent", extent], None)]
                             if extent else [])


if __name__ == "__main__":
    unittest.main()
