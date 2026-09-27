"""Source selectors must not become instructions or silently choose a pane."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from needle_logs.resolve import ResolveError, resolve_source, check_binding


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"KILIX_TRANSCRIPT_DIR": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.broker = "1a" * 8
        (self.root / f"{self.broker}.log").write_text("hello\n")
        self.pane = {"pane_id": 23, "title": "review", "broker": {"session_id": self.broker}}
        self.tree = {"schema": "kilix.panes/v1", "panes": [self.pane]}

    def test_explicit_selection_reads_raw_identity(self):
        for selector in ("23", "review", self.broker, self.broker[:5]):
            with self.subTest(selector=selector):
                result = resolve_source(selector, tree=self.tree)
                self.assertEqual(result["path"], str(self.root / f"{self.broker}.log"))
                self.assertEqual(result["provider"], "raw")

    def test_ambiguous_titles_and_missing_ids_do_not_fallback(self):
        self.tree["panes"].append(self.pane | {"pane_id": 24})
        for selector, code in (("review", "ambiguous"), ("25", "not_found")):
            with self.assertRaises(ResolveError) as raised:
                resolve_source(selector, tree=self.tree)
            self.assertEqual(raised.exception.code, code)

    def test_changed_broker_refuses_binding(self):
        binding = resolve_source("23", tree=self.tree)
        with mock.patch("needle_logs.resolve.resolve_source", return_value=binding | {
                "expected_broker_id": "2b" * 8}):
            with self.assertRaisesRegex(ResolveError, "changed"):
                check_binding(binding)

    def test_provider_paths_cannot_escape_root(self):
        self.pane["coding_session"] = {"provider": "codex", "path": str(self.root / "secret")}
        with self.assertRaises(ResolveError) as raised:
            resolve_source("23", tree=self.tree)
        self.assertEqual(raised.exception.code, "source_outside_root")

    def test_raw_symlink_escape_is_rejected(self):
        link = self.root / f"{self.broker}.log"
        link.unlink()
        with tempfile.NamedTemporaryFile() as other:
            link.symlink_to(other.name)
            with self.assertRaises(ResolveError) as raised:
                resolve_source("23", tree=self.tree)
            self.assertEqual(raised.exception.code, "source_outside_root")

    def test_malformed_broker_cannot_construct_path(self):
        self.pane["broker"] = {"session_id": "../../secrets"}
        with self.assertRaises(ResolveError) as raised:
            resolve_source("23", tree=self.tree)
        self.assertEqual(raised.exception.code, "source_unavailable")


if __name__ == "__main__":
    unittest.main()
