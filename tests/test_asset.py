"""Admitting engine bytes: exact digest and size, one read, sealed, executable."""
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import support  # noqa: F401
import asset


def digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class LoadVerified(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-")
        self.addCleanup(self.dir.cleanup)
        # A real ELF, so the sealed image can be executed like the engine is.
        self.binary = os.path.join(self.dir.name, "engine")
        shutil.copyfile(shutil.which("true"), self.binary)
        os.chmod(self.binary, 0o600)  # no execute bit: the installed asset has none
        self.sha, self.size = digest(self.binary), os.path.getsize(self.binary)

    def test_admitted_bytes_run_from_the_sealed_image(self):
        with asset.load_verified(self.binary, self.sha, self.size) as image:
            self.assertEqual(image.sha256, self.sha)
            done = subprocess.run([image.path], pass_fds=(image.fd,))
            self.assertEqual(done.returncode, 0)
            with self.assertRaises(OSError):
                os.write(image.fd, b"x")
            os.unlink(self.binary)  # the file on disk no longer matters
            self.assertEqual(subprocess.run([image.path], pass_fds=(image.fd,)).returncode, 0)

    def test_wrong_digest_is_refused(self):
        with self.assertRaisesRegex(asset.AssetError, "does not match"):
            asset.load_verified(self.binary, "0" * 64, self.size)

    def test_same_size_different_bytes_is_refused(self):
        with open(self.binary, "r+b") as handle:
            handle.seek(self.size // 2)
            byte = handle.read(1)
            handle.seek(self.size // 2)
            handle.write(bytes([byte[0] ^ 1]))
        self.assertEqual(os.path.getsize(self.binary), self.size)
        with self.assertRaisesRegex(asset.AssetError, "does not match"):
            asset.load_verified(self.binary, self.sha, self.size)

    def test_wrong_size_is_refused_before_reading(self):
        with self.assertRaisesRegex(asset.AssetError, "bytes, expected"):
            asset.load_verified(self.binary, self.sha, self.size + 1)

    def test_symlink_is_refused(self):
        link = os.path.join(self.dir.name, "link")
        os.symlink(self.binary, link)
        with self.assertRaisesRegex(asset.AssetError, "cannot open"):
            asset.load_verified(link, self.sha, self.size)

    def test_named_pipe_is_refused_without_blocking(self):
        fifo = os.path.join(self.dir.name, "fifo")
        os.mkfifo(fifo)
        with self.assertRaisesRegex(asset.AssetError, "not a regular file"):
            asset.load_verified(fifo, self.sha, self.size)

    def test_directory_is_refused(self):
        with self.assertRaises(asset.AssetError):
            asset.load_verified(self.dir.name, self.sha, self.size)

    def test_override_is_held_to_the_upstream_pin(self):
        with self.assertRaises(asset.AssetError):
            asset.from_file(self.binary)


class ContentRoot(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in ("KILIX_CONTENT_ROOT", "KILIX_DATA_HOME")}

    def tearDown(self):
        for key, value in self.saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    def test_precedence(self):
        os.environ["KILIX_DATA_HOME"] = "/srv/kilix-data"
        self.assertEqual(asset.content_root(), "/srv/kilix-data/desktop-apps")
        os.environ["KILIX_CONTENT_ROOT"] = "/content//root/"
        self.assertEqual(asset.content_root(), "/content/root")
        self.assertEqual(asset.content_root("/explicit"), "/explicit")

    def test_relative_or_missing_is_refused(self):
        with self.assertRaisesRegex(asset.AssetError, "cannot find"):
            asset.content_root()
        with self.assertRaisesRegex(asset.AssetError, "absolute"):
            asset.content_root("relative/root")


if __name__ == "__main__":
    unittest.main()


class CatalogAgreement(unittest.TestCase):
    """The pinned Content component lists needle2, and --engine's pin is its pin."""

    def test_the_local_pin_is_the_catalog_manifest(self):
        content, _first_use, _lic = asset._content()
        spec = content.verified_packaged_catalog().require_asset(asset.ASSET_ID)
        member = next(item for item in spec.files if item.path == asset.ENGINE_MEMBER)
        self.assertEqual((member.sha256, member.bytes), (asset.PINNED_SHA256, asset.PINNED_BYTES))
        self.assertEqual(spec.licenses[0].license_id, "apache-2.0")


class Wheel(unittest.TestCase):
    """libneedle.so is taken out of the verified wheel and held to its own pin."""

    WHEEL = os.path.expanduser("~/research/refs/needle2/hf/python/"
                               "cactus_needle-2.0.4-py3-none-manylinux2014_x86_64.whl")

    def image_of(self, data):
        directory = tempfile.TemporaryDirectory(prefix="kn-")
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "w.whl")
        with open(path, "wb") as handle:
            handle.write(data)
        image = asset.load_verified(path, hashlib.sha256(data).hexdigest(), len(data))
        self.addCleanup(image.close)
        return image

    def zipped(self, members):
        import io
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        return buffer.getvalue()

    @unittest.skipUnless(os.path.exists(WHEEL), "needs the reference wheel")
    def test_the_pinned_wheel_yields_the_pinned_library(self):
        with open(self.WHEEL, "rb") as handle:
            wheel = self.image_of(handle.read())
        with asset.library_from_wheel(wheel) as library:
            self.assertEqual(library.sha256, asset.PINNED_LIB_SHA256)
            self.assertEqual(os.fstat(library.fd).st_size, asset.PINNED_LIB_BYTES)

    def test_a_wheel_without_the_library_is_refused(self):
        with self.assertRaisesRegex(asset.AssetError, "holds no usable"):
            asset.library_from_wheel(self.image_of(self.zipped({"needle/other.so": b"x"})))

    def test_a_substituted_library_is_refused(self):
        with self.assertRaisesRegex(asset.AssetError, "pinned"):
            asset.library_from_wheel(self.image_of(self.zipped({"needle/libneedle.so": b"x"})))
