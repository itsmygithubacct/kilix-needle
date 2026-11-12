"""Source selectors must not become instructions or silently choose a pane."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from needle_logs.resolve import ResolveError, resolve_source, check_binding
from needle_logs.sources import SourceError, read_source


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

    def test_missing_claude_path_uses_exact_unique_session_filename(self):
        import json
        session = "12345678-1234-1234-1234-123456789abc"
        project = self.root / "projects" / "synthetic-project"
        project.mkdir(parents=True)
        path = project / (session + ".jsonl")
        path.write_text(json.dumps({"type": "user", "sessionId": session,
                                   "message": {"role": "user", "content": "Read this."}}) + "\n")
        self.pane["coding_session"] = {"provider": "claude", "session_id": session, "path": None}
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)}):
            binding = resolve_source("23", tree=self.tree)
            self.assertEqual(binding["path"], str(path))
            self.assertEqual(binding["provider"], "claude")
            result = read_source(binding["path"], binding["provider"], binding=binding)
            self.assertEqual(result["source"]["session_id"], session)
            duplicate = project.parent / "another-project"
            duplicate.mkdir()
            (duplicate / path.name).write_bytes(path.read_bytes())
            with self.assertRaises(ResolveError) as raised:
                resolve_source("23", tree=self.tree)
            self.assertEqual(raised.exception.code, "ambiguous_source")

    def test_session_lookup_is_bounded_and_does_not_accept_path_syntax(self):
        from needle_logs.resolve import _claude_session_path
        project = self.root / "projects" / "synthetic-project"
        project.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)}):
            self.assertEqual(_claude_session_path("../../escape"), "")
            with mock.patch("needle_logs.resolve.time.monotonic", side_effect=[0, 3]):
                with self.assertRaises(ResolveError) as raised:
                    _claude_session_path("12345678-1234-1234-1234-123456789abc")
            self.assertEqual(raised.exception.code, "source_lookup_limit")

    def _homes(self):
        return mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex"),
                                            "CLAUDE_CONFIG_DIR": str(self.root / "other-provider"),
                                            "GROK_HOME": str(self.root / "grok")})

    def _rollout(self, thread, folder="sessions/2026/11/12"):
        import json
        directory = self.root / "codex" / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-2026-11-12T01-02-03-{thread}.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n"
                        + json.dumps({"type": "response_item", "payload": {
                            "type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": "Read this."}]}}) + "\n")
        return path

    def test_thread_id_reads_without_any_pane_listing(self):
        # Behind a shared Codex app-server the pane list is refused. A full
        # session ID names one recording, so it must not need the pane list.
        from needle_logs.cli import read
        thread = "01a1cf05-b66a-7992-806c-e1f87b5e5881"
        path = self._rollout(thread)
        refused = ResolveError("caller_context_unverified", "unverified")
        with self._homes(), \
                mock.patch("needle_logs.resolve.snapshot", side_effect=refused) as listing:
            for selector in (thread, thread.upper(), f" {thread} "):
                with self.subTest(selector=selector):
                    binding = resolve_source(selector)
                    self.assertEqual((binding["path"], binding["provider"], binding["pane_id"],
                                      binding["expected_session_id"]),
                                     (str(path), "codex", None, thread))
            result = read({"operation": "brief", "session": thread,
                           "cache_path": str(self.root / "cache" / "index.sqlite3")})
            listing.assert_not_called()
        self.assertEqual((result["status"], result["errors"]), ("ok", []))
        self.assertEqual([event["kind"] for event in result["events"]], ["request"])

    def test_thread_id_lookup_covers_archives_and_refuses_duplicates(self):
        thread = "01a1cf05-b66a-7992-806c-e1f87b5e5882"
        archived = self._rollout(thread, "archived_sessions")
        with self._homes():
            self.assertEqual(resolve_source(thread)["path"], str(archived))
            self._rollout(thread)
            with self.assertRaises(ResolveError) as raised:
                resolve_source(thread)
            self.assertEqual(raised.exception.code, "ambiguous_source")

    def test_unrecorded_session_id_still_selects_a_live_pane(self):
        session = "01a1cf05-b66a-7992-806c-e1f87b5e5883"
        self.pane["coding_session"] = {"provider": "grok", "session_id": session}
        with self._homes():
            binding = resolve_source(session, tree=self.tree)
            self.assertEqual((binding["pane_id"], binding["provider"]), (23, "raw"))
            with self.assertRaises(ResolveError) as raised:
                resolve_source(session.replace("5883", "5884"), tree=self.tree)
            self.assertEqual(raised.exception.code, "not_found")

    def test_session_id_claimed_by_both_providers_is_refused(self):
        session = "01a1cf05-b66a-7992-806c-e1f87b5e5886"
        self._rollout(session)
        project = self.root / "other-provider" / "projects" / "synthetic-project"
        project.mkdir(parents=True)
        (project / (session + ".jsonl")).write_text("{}\n")
        with self._homes():
            with self.assertRaises(ResolveError) as raised:
                resolve_source(session)
        self.assertEqual(raised.exception.code, "ambiguous_source")

    def test_replaced_rollout_refuses_thread_binding(self):
        thread = "01a1cf05-b66a-7992-806c-e1f87b5e5885"
        path = self._rollout(thread)
        with self._homes():
            binding = resolve_source(thread)
            check_binding(binding)
            replacement = path.with_suffix(".new")
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
            with self.assertRaisesRegex(ResolveError, "changed"):
                check_binding(binding)

    def _grok_session(self, session, folder="%2Fwork"):
        import json
        directory = self.root / "grok" / "sessions" / folder / session
        directory.mkdir(parents=True, exist_ok=True)
        updates = directory / "updates.jsonl"
        updates.write_text(json.dumps({"timestamp": 1794300000, "method": "session/update", "params": {
            "sessionId": session, "update": {"sessionUpdate": "user_message_chunk",
                                             "content": {"type": "text", "text": "Read this."}}}}) + "\n")
        (directory / "events.jsonl").write_text("{}\n")
        return updates

    def test_grok_session_reads_by_id_folder_sibling_or_pane(self):
        from needle_logs.cli import read
        session = "01a1cf05-b66a-7992-806c-e1f87b5e5887"
        updates = self._grok_session(session)
        cache = str(self.root / "cache" / "index.sqlite3")
        refused = ResolveError("caller_context_unverified", "unverified")
        with self._homes(), mock.patch("needle_logs.resolve.snapshot", side_effect=refused) as listing:
            binding = resolve_source(session)
            self.assertEqual((binding["path"], binding["provider"], binding["pane_id"]),
                             (str(updates), "grok", None))
            result = read({"operation": "brief", "session": session, "cache_path": cache})
            listing.assert_not_called()
            self.assertEqual((result["status"], result["errors"]), ("ok", []))
            self.assertEqual([event["kind"] for event in result["events"]], ["request"])
            # The session folder and its other files name the same conversation.
            for target in (updates.parent, updates.parent / "events.jsonl"):
                result = read({"operation": "brief", "file": str(target), "provider": "grok",
                               "cache_path": cache})
                self.assertEqual((result["status"], result["source"]["path"]), ("ok", str(updates)))
            # Kilix lists a live Grok pane by session ID, without a path.
            self.pane["coding_session"] = {"provider": "grok", "session_id": session}
            binding = resolve_source("23", tree=self.tree)
            self.assertEqual((binding["provider"], binding["path"], binding["pane_id"]),
                             ("grok", str(updates), 23))
            self._grok_session(session, folder="%2Fother")
            with self.assertRaises(ResolveError) as raised:
                resolve_source(session)
            self.assertEqual(raised.exception.code, "ambiguous_source")

    def test_discovered_file_swap_cannot_read_outside_root(self):
        candidate = self.root / f"{self.broker}.log"
        outside = self.root.parent / f"{self.root.name}-outside-synthetic.log"
        outside.write_bytes(b"OUTSIDE ROOT SYNTHETIC\n")
        self.addCleanup(outside.unlink)
        parked = self.root / "parked.log"
        binding = resolve_source("23", tree=self.tree)
        real_open = os.open
        swapped = [False]
        def race(path, flags, *args, **kwargs):
            if path == candidate.name and kwargs.get("dir_fd") is not None and not swapped[0]:
                swapped[0] = True
                candidate.rename(parked)
                candidate.symlink_to(outside)
            return real_open(path, flags, *args, **kwargs)
        try:
            with mock.patch("needle_logs.sources.os.open", side_effect=race):
                with self.assertRaises(SourceError) as raised:
                    read_source(binding["path"], binding["provider"], binding=binding)
            self.assertEqual(raised.exception.code, "source_changed")
            self.assertTrue(swapped[0])
        finally:
            if candidate.is_symlink():
                candidate.unlink()
            if parked.exists():
                parked.rename(candidate)
        with mock.patch("needle_logs.resolve.snapshot", return_value=self.tree):
            check_binding(binding)

    def test_cli_passes_discovered_root_binding(self):
        from needle_logs.cli import read
        candidate = self.root / f"{self.broker}.log"
        outside = self.root.parent / f"{self.root.name}-outside-cli-synthetic.log"
        outside.write_bytes(b"OUTSIDE ROOT SYNTHETIC\n")
        self.addCleanup(outside.unlink)
        parked = self.root / "parked-cli.log"
        real_open = os.open
        swapped = [False]
        def race(path, flags, *args, **kwargs):
            if path == candidate.name and kwargs.get("dir_fd") is not None and not swapped[0]:
                swapped[0] = True
                candidate.rename(parked)
                candidate.symlink_to(outside)
            return real_open(path, flags, *args, **kwargs)
        try:
            with mock.patch("needle_logs.resolve.snapshot", return_value=self.tree), \
                 mock.patch("needle_logs.sources.os.open", side_effect=race):
                result = read({"operation": "events", "session": "23",
                               "cache_path": str(self.root / "cache" / "index.sqlite3")})
            self.assertTrue(swapped[0])
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["errors"][0]["code"], "source_changed")
        finally:
            if candidate.is_symlink():
                candidate.unlink()
            if parked.exists():
                parked.rename(candidate)


if __name__ == "__main__":
    unittest.main()
