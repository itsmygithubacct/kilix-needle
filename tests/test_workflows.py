"""Desktop readiness uses synthetic bytes and the pinned licence authority only."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support  # noqa: F401
import asset
import needle_cli
import state
import workflows


class Terminal(io.StringIO):
    def __init__(self, text=""):
        super().__init__(text)
        self.buffer = io.BytesIO()

    def isatty(self):
        return True


class WorkflowFixtures(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="kn-workflows-")
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        self.root = self.base / "content with spaces"
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {
            "GPU_TERMINAL_HOME": str(self.base / "state"),
            "KILIX_CONTENT_ROOT": str(self.root),
            "KILIX_LICENSE_RECEIPTS": str(self.base / "receipts"),
            "XDG_DATA_HOME": str(self.base / "legacy"),
        }).start()
        for key in ("KILIX_NEEDLE_SYSTEM_PROFILE", "KILIX_NEEDLE_LIBRARY", "KILIX_NEEDLE_ENGINE"):
            os.environ.pop(key, None)
        self.content, self.first_use, self.lic = asset._content()
        self.bytes = {"needle2": b"synthetic engine bytes", "needle2-runtime": b"synthetic runtime bytes"}
        self.specs = {
            aid: SimpleNamespace(asset_id=aid, version="fixture-v1", files=[
                SimpleNamespace(path="payload", bytes=len(data), sha256=hashlib.sha256(data).hexdigest())])
            for aid, data in self.bytes.items()
        }
        catalog = SimpleNamespace(require_asset=lambda aid: self.specs[aid])
        patch.object(self.content, "verified_packaged_catalog", return_value=catalog).start()
        patch.object(self.lic, "load_determined_records", return_value=object()).start()
        self.agreement = patch.object(self.first_use, "needs_agreement", return_value=False).start()

    def install_fixture(self, aid="needle2"):
        directory = self.root / "assets" / aid
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "payload").write_bytes(self.bytes[aid])
        return directory

    def select(self, job="panes", data=b"synthetic tuned weights"):
        weights = self.base / "selected weights.cact"
        weights.write_bytes(data)
        state.home().mkdir(parents=True, exist_ok=True)
        (state.home() / "model.json").write_text(json.dumps({
            "schema": "kilix-needle.selection/v2", "jobs": {job: {
                "weights": str(weights), "sha256": hashlib.sha256(data).hexdigest()}}}))
        return weights

    def test_missing_status_creates_nothing_and_dispatches_exact_json(self):
        before = list(self.base.iterdir())
        with patch.object(self.content, "Installer", side_effect=AssertionError("installer")), \
             patch.object(self.lic.ReceiptStore, "__init__", side_effect=AssertionError("write constructor")), \
             patch.object(asset, "install", side_effect=AssertionError("acquisition")), \
             patch.object(state, "place", side_effect=AssertionError("migration")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = needle_cli.main(["workflows", "status", "--json"])
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["schema"], "kilix.workflows/v1")
        self.assertFalse(result["ready"])
        self.assertEqual(result["required_assets"], ["needle2"])
        self.assertEqual(result["assets"][0]["state"], "missing")
        self.assertEqual(list(self.base.iterdir()), before)

    def test_ready_status_hashes_bytes_and_preserves_modes_and_mtime(self):
        directory = self.install_fixture()
        self.base.joinpath("receipts").mkdir(mode=0o755)
        before = {str(p): p.stat() for p in self.base.rglob("*")}
        with patch.object(asset, "load_verified", side_effect=AssertionError("engine image")):
            self.assertTrue(workflows.status()["ready"])
        after = {str(p): p.stat() for p in self.base.rglob("*")}
        self.assertEqual(set(before), set(after))
        for name, info in before.items():
            self.assertEqual((info.st_mode, info.st_mtime_ns), (after[name].st_mode, after[name].st_mtime_ns))
        (directory / "payload").write_bytes(b"X" * len(self.bytes["needle2"]))
        result = workflows.status()
        self.assertFalse(result["ready"])
        self.assertEqual(result["assets"][0]["state"], "corrupt")

    def test_missing_member_wrong_size_extra_file_and_fifo_refuse(self):
        directory = self.install_fixture()
        member = directory / "payload"
        for variant in ("missing", "size", "extra", "fifo"):
            with self.subTest(variant=variant):
                self.install_fixture()
                if variant == "missing":
                    member.unlink()
                elif variant == "size":
                    member.write_bytes(b"short")
                elif variant == "extra":
                    (directory / "extra").write_text("extra")
                else:
                    member.unlink()
                    os.mkfifo(member)
                self.assertEqual(workflows.status()["assets"][0]["state"], "corrupt")
                if member.exists():
                    member.unlink()
                (directory / "extra").unlink(missing_ok=True)

    def test_asset_selection_member_and_directory_symlinks_refuse(self):
        directory = self.install_fixture()
        (directory / "payload").unlink()
        source = self.base / "real-payload"
        source.write_bytes(self.bytes["needle2"])
        (directory / "payload").symlink_to(source)
        self.assertEqual(workflows.status()["assets"][0]["state"], "corrupt")
        (directory / "payload").unlink()
        self.install_fixture()
        (directory / "linked").symlink_to(self.base, target_is_directory=True)
        self.assertEqual(workflows.status()["assets"][0]["state"], "corrupt")
        (directory / "linked").unlink()
        moved = self.base / "moved"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        self.assertEqual(workflows.status()["assets"][0]["state"], "corrupt")

    def test_corrupt_asset_is_detected_even_without_acceptance(self):
        directory = self.install_fixture()
        self.agreement.return_value = True
        (directory / "payload").write_bytes(b"bad")
        self.assertEqual(workflows.status()["assets"][0]["state"], "corrupt")

    def test_license_not_inferred_from_installed_bytes(self):
        self.install_fixture()
        self.agreement.return_value = True
        self.assertEqual(workflows.status()["assets"][0]["state"], "agreement-required")

    def test_tuned_selection_requires_only_runtime_not_training(self):
        self.install_fixture()
        weights = self.select("agents")
        result = workflows.status()
        self.assertEqual(result["required_assets"], ["needle2", "needle2-runtime"])
        self.assertFalse(result["ready"])
        self.install_fixture("needle2-runtime")
        self.assertTrue(workflows.status()["ready"])
        weights.write_bytes(b"wrong")
        self.assertEqual(workflows.status()["models"][0]["state"], "invalid")

    def test_apps_and_files_selections_do_not_require_runtime(self):
        self.install_fixture()
        for job in ("apps", "files"):
            self.select(job)
            result = workflows.status()
            self.assertTrue(result["ready"])
            self.assertEqual(result["required_assets"], ["needle2"])

    def test_legacy_selection_and_invalid_selection_fail_closed(self):
        self.install_fixture()
        weights = self.select()
        selection = state.home() / "model.json"
        selection.write_text(json.dumps({"weights": str(weights), "sha256": hashlib.sha256(weights.read_bytes()).hexdigest()}))
        self.assertIn("needle2-runtime", workflows.status()["required_assets"])
        for value in ("not-json", json.dumps({"schema": "future"}), json.dumps({"schema": "kilix-needle.selection/v2", "jobs": {"panes": {"weights": "relative", "sha256": "0" * 64}}})):
            selection.write_text(value)
            self.assertFalse(workflows.status()["ready"])
            self.assertEqual(workflows.status()["models"][0]["state"], "invalid")

    def test_nonregular_oversized_and_symlink_model_metadata_refuse(self):
        self.install_fixture()
        self.select()
        selection = state.home() / "model.json"
        selection.unlink()
        os.mkfifo(selection)
        self.assertFalse(workflows.status()["ready"])
        selection.unlink()
        selection.write_bytes(b" " * 65_537)
        self.assertFalse(workflows.status()["ready"])
        selection.unlink()
        target = self.base / "real-model.json"
        target.write_text('{"schema":"kilix-needle.selection/v2","jobs":{}}')
        selection.symlink_to(target)
        self.assertFalse(workflows.status()["ready"])

    def test_profile_uses_own_verified_library_without_runtime_acquisition(self):
        self.install_fixture()
        directory = state._legacy("system-normalizer")
        config = {"schema": "kilix-needle-system-normalizer/v1", "role": "proposal-only"}
        data = {"weights": b"synthetic profile weights", "library": b"synthetic profile library"}
        for kind, value in data.items():
            digest = hashlib.sha256(value).hexdigest()
            path = directory / "objects" / digest / ("weights.cact" if kind == "weights" else "libneedle.so")
            path.parent.mkdir(parents=True)
            path.write_bytes(value)
            config[kind] = {"path": str(path), "sha256": digest, "size": len(value)}
        (directory / "profile.json").write_text(json.dumps(config))
        with patch.object(asset, "PINNED_LIB_SHA256", config["library"]["sha256"]), \
             patch.object(asset, "PINNED_LIB_BYTES", config["library"]["size"]), \
             patch.object(state, "place", side_effect=AssertionError("migration")):
            result = workflows.status()
            self.assertTrue(result["ready"])
            self.assertEqual(result["required_assets"], ["needle2"])
            Path(config["weights"]["path"]).write_bytes(b"wrong")
            self.assertFalse(workflows.status()["ready"])
        self.assertTrue(directory.exists())
        self.assertFalse((state.home() / "system-normalizer").exists())

    def test_profile_override_missing_or_relative_is_not_ready(self):
        self.install_fixture()
        for value in ("relative", str(self.base / "missing-profile")):
            os.environ["KILIX_NEEDLE_SYSTEM_PROFILE"] = value
            self.assertFalse(workflows.status()["ready"])

    def test_root_symlink_and_explicit_engine_never_report_ready_or_acquire(self):
        self.install_fixture()
        alias = self.base / "symlink-root"
        alias.symlink_to(self.root, target_is_directory=True)
        with patch.object(asset, "install", side_effect=AssertionError("install")):
            self.assertFalse(workflows.status(str(alias))["ready"])
            with self.assertRaisesRegex(workflows.WorkflowError, "real directory"):
                workflows.setup(str(alias), stdin=Terminal(), stdout=Terminal())
            os.environ["KILIX_NEEDLE_ENGINE"] = str(self.base / "missing-explicit-engine")
            self.assertFalse(workflows.status()["ready"])
            with self.assertRaisesRegex(workflows.WorkflowError, "unsupported"):
                workflows.setup(stdin=Terminal(), stdout=Terminal())
        self.assertTrue(alias.is_symlink())

    def test_explicit_pinned_library_verifies_without_catalog_runtime(self):
        self.install_fixture()
        self.select()
        library = self.base / "library.so"
        data = b"synthetic explicit library"
        library.write_bytes(data)
        os.environ["KILIX_NEEDLE_LIBRARY"] = str(library)
        with patch.object(asset, "PINNED_LIB_SHA256", hashlib.sha256(data).hexdigest()), \
             patch.object(asset, "PINNED_LIB_BYTES", len(data)):
            self.assertTrue(workflows.status()["ready"])
            self.assertEqual(workflows.status()["required_assets"], ["needle2"])
            library.write_bytes(b"wrong")
            self.assertFalse(workflows.status()["ready"])

    def test_existing_current_profile_directory_does_not_adopt_legacy_profile(self):
        self.install_fixture()
        legacy = state._legacy("system-normalizer")
        legacy.mkdir(parents=True)
        (legacy / "profile.json").write_text("bad-json")
        (state.home() / "system-normalizer").mkdir(parents=True)
        # Runtime will leave the blocked legacy directory untouched; it is not
        # selected and does not become the desktop's configured profile.
        self.assertTrue(workflows.status()["ready"])

    def test_catalog_refusal_and_oversized_output_fail_closed(self):
        with patch.object(self.content, "verified_packaged_catalog", side_effect=RuntimeError("catalog mismatch")):
            self.assertFalse(workflows.status()["ready"])
        with patch.object(workflows, "_model_status", return_value=([{"kind": "selection", "job": "panes", "state": "invalid", "error": "X" * 20_000}], False)):
            result = workflows.status()
        self.assertFalse(result["ready"])
        self.assertLessEqual(len(json.dumps(result).encode()), workflows.MAX_JSON)

    def test_setup_nonterminal_does_not_create_lock_or_call_status(self):
        with patch.object(workflows, "status", side_effect=AssertionError("status")):
            with self.assertRaisesRegex(workflows.WorkflowError, "terminal"):
                workflows.setup(stdin=io.StringIO(), stdout=io.StringIO())
        self.assertFalse(self.root.exists())

    def test_setup_reuses_acceptance_and_verifies_after_acquisition(self):
        installer = SimpleNamespace(ensure_upstream_asset=lambda spec, **kwargs: self.install_fixture(spec.asset_id))
        with patch.object(self.content, "Installer", return_value=installer) as factory, \
             patch.object(self.lic, "load_determined_texts", return_value=object()), \
             patch.object(asset, "install", side_effect=AssertionError("new acceptance")):
            self.assertTrue(workflows.setup(stdin=Terminal(), stdout=Terminal())["ready"])
        factory.assert_called_once_with(str(self.root))
        self.assertFalse((state.home() / "model.json").exists())

    def test_setup_requires_existing_typed_license_path_and_postcheck(self):
        self.agreement.return_value = True
        def licensed(*args, **kwargs):
            self.assertEqual(kwargs["asset_id"], "needle2")
            self.install_fixture()
            self.agreement.return_value = False
        with patch.object(asset, "install", side_effect=licensed) as acquire:
            self.assertTrue(workflows.setup(stdin=Terminal(), stdout=Terminal())["ready"])
        acquire.assert_called_once()
        (self.root / "assets" / "needle2" / "payload").unlink()
        (self.root / "assets" / "needle2").rmdir()
        with patch.object(self.content, "Installer") as installer, \
             patch.object(self.lic, "load_determined_texts", return_value=object()):
            with self.assertRaisesRegex(workflows.WorkflowError, "not installed"):
                workflows.setup(stdin=Terminal(), stdout=Terminal())

    def test_corrupt_or_custom_invalid_setup_refuses_before_any_acquisition(self):
        self.install_fixture().joinpath("payload").write_bytes(b"bad")
        with patch.object(asset, "install", side_effect=AssertionError("install")), \
             patch.object(self.content, "Installer", side_effect=AssertionError("installer")):
            with self.assertRaisesRegex(workflows.WorkflowError, "unchanged"):
                workflows.setup(stdin=Terminal(), stdout=Terminal())
            self.install_fixture()
            self.select().unlink()
            with self.assertRaisesRegex(workflows.WorkflowError, "unchanged"):
                workflows.setup(stdin=Terminal(), stdout=Terminal())

    def test_whole_session_lock_refuses_concurrent_process_before_status(self):
        self.root.mkdir()
        script = "import fcntl,os,sys; f=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.readline()"
        child = subprocess.Popen([sys.executable, "-B", "-c", script, str(self.root / ".needle-workflows-setup.lock")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with patch.object(workflows, "status", side_effect=AssertionError("status")), \
                 patch.object(asset, "install", side_effect=AssertionError("install")):
                with self.assertRaisesRegex(workflows.WorkflowError, "already running"):
                    workflows.setup(str(self.root / ".." / self.root.name), stdin=Terminal(), stdout=Terminal())
        finally:
            child.communicate("done\n", timeout=5)
        self.install_fixture()
        self.assertTrue(workflows.setup(stdin=Terminal(), stdout=Terminal())["ready"])

    def test_setup_lock_symlink_is_refused_without_touching_target(self):
        self.root.mkdir()
        target = self.base / "unrelated"
        target.write_text("unchanged")
        (self.root / ".needle-workflows-setup.lock").symlink_to(target)
        with patch.object(workflows, "status", side_effect=AssertionError("status")):
            with self.assertRaises(OSError):
                workflows.setup(stdin=Terminal(), stdout=Terminal())
        self.assertEqual(target.read_text(), "unchanged")


class AuthorityProbe(unittest.TestCase):
    def test_real_receipt_coverage_current_binding_and_nonblocking_junk(self):
        content, first_use, lic = asset._content()
        with tempfile.TemporaryDirectory(prefix="kn-workflows-authority-") as scratch, patch.dict(os.environ, {
            "GPU_TERMINAL_HOME": scratch + "/state", "KILIX_CONTENT_ROOT": scratch + "/content",
            "KILIX_LICENSE_RECEIPTS": scratch + "/receipts", "XDG_DATA_HOME": scratch + "/legacy", "KILIX_NEEDLE_SYSTEM_PROFILE": "",
        }):
            self.assertEqual(workflows.status()["assets"][0]["state"], "agreement-required")
            self.assertEqual(list(Path(scratch).iterdir()), [])
            spec = content.verified_packaged_catalog().require_asset("needle2")
            records = lic.load_determined_records()
            record = first_use.license_record_for(spec, records)
            agreement = lic.capture_agreement(record, lic.typed_agreement_line(record))
            receipt = lic.receipt_from_agreement(record, agreement, manifest_digest=spec.manifest_digest,
                                               release_digest="1" * 64, catalogue_digest="2" * 64)
            store = lic.ReceiptStore.shared()
            path = store.write(receipt)
            self.assertEqual(workflows.status()["assets"][0]["state"], "missing")
            path.write_text("not-json")
            self.assertFalse(workflows.status()["ready"])
            path.unlink()
            os.mkfifo(path)
            self.assertEqual(workflows.status()["assets"][0]["state"], "agreement-required")

    def test_wrong_typed_acceptance_never_enters_installer(self):
        content, _first_use, _lic = asset._content()
        with tempfile.TemporaryDirectory(prefix="kn-workflows-decline-") as scratch, patch.dict(os.environ, {
            "GPU_TERMINAL_HOME": scratch + "/state", "KILIX_CONTENT_ROOT": scratch + "/content",
            "KILIX_LICENSE_RECEIPTS": scratch + "/receipts", "XDG_DATA_HOME": scratch + "/legacy", "KILIX_NEEDLE_SYSTEM_PROFILE": "",
        }), patch.object(content, "Installer", side_effect=AssertionError("installer")):
            with self.assertRaisesRegex(asset.AssetError, "not accepted"):
                workflows.setup(stdin=Terminal("yes\n"), stdout=Terminal())
            self.assertEqual(list(Path(scratch + "/receipts").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
