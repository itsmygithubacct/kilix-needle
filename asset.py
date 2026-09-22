"""Find the Needle 2 engine and hold exactly the verified bytes.

kilix-needle never downloads and never accepts a licence. The engine is the
`needle2` asset in the Content catalog; `kilix models install needle2` shows
its licence, takes the typed agreement and installs it. This module only
admits what is already there: the catalog must verify against its pinned
digest, a receipt must cover the asset, and the installed engine must match
the catalog manifest's size and SHA-256.

The bytes are read once into a sealed memfd and hashed there, and the engine
is executed from that descriptor. What runs is what was verified, even if the
file on disk is replaced afterwards, and the installed file needs no execute
bit.

`--engine FILE` admits a local copy instead, for development. It is held to
the same pinned digest, so it can only ever be the same upstream bytes.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import stat
import sys

ASSET_ID = "needle2"
ENGINE_MEMBER = "needle"
# Cactus-Compute/needle2 at 32e9e3a93b205f786929697446ae669cf0a84579,
# linux-x86_64/needle. The catalog manifest carries the same pair; this copy
# exists only for --engine, which has no catalog to consult.
PINNED_SHA256 = "a2df5d661606957d2ff0e8abd4af31ee2953e759ce84e8e5921b6e8d13bd0e16"
PINNED_BYTES = 14_888_896
# libneedle.so from cactus_needle-2.0.4-py3-none-manylinux2014_x86_64.whl at the
# same revision: the runtime for tuned weights (see libengine.py).
PINNED_LIB_SHA256 = "9fa5386d3e3a8ee17914fb23643bc5f5c906b683fa33561e79c5445dd78bc389"
PINNED_LIB_BYTES = 14_315_600
_CONTENT_SRC = Path(__file__).resolve().parent / "third_party" / "kilix-content" / "src"


class AssetError(RuntimeError):
    """The engine is not installed, not covered, or not the verified bytes."""


class EngineImage:
    """A sealed in-memory copy of verified engine bytes."""

    def __init__(self, fd: int, sha256: str):
        self.fd = fd
        self.sha256 = sha256

    @property
    def path(self) -> str:
        return f"/proc/self/fd/{self.fd}"

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "EngineImage":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def load_verified(path: str | os.PathLike, sha256: str, size: int) -> EngineImage:
    """Read `path` once, refuse anything but the expected bytes, seal them."""
    try:
        # O_NONBLOCK: a named pipe in the engine's place must be refused, not waited on.
        source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as error:
        raise AssetError(f"cannot open the engine {os.fspath(path)!r}: {error.strerror}") from error
    try:
        info = os.fstat(source)
        if not stat.S_ISREG(info.st_mode):
            raise AssetError("the engine is not a regular file")
        if info.st_size != size:
            raise AssetError(f"the engine is {info.st_size} bytes, expected {size}")
        chunks, total = [], 0
        while chunk := os.read(source, 1 << 20):
            total += len(chunk)
            if total > size:
                raise AssetError("the engine grew while it was read")
            chunks.append(chunk)
    finally:
        os.close(source)
    data = b"".join(chunks)
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != size or digest != sha256:
        raise AssetError("the engine does not match its pinned SHA-256; it will not be run")
    image = os.memfd_create("kilix-needle-engine", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(image, view):]
        fcntl.fcntl(image, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW
                    | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        os.fchmod(image, 0o500)
    except OSError:
        os.close(image)
        raise
    return EngineImage(image, digest)


def from_file(path: str) -> EngineImage:
    return load_verified(path, PINNED_SHA256, PINNED_BYTES)


def library_from_file(path: str) -> EngineImage:
    """A local copy of the pinned libneedle.so, for development and tuning."""
    return load_verified(path, PINNED_LIB_SHA256, PINNED_LIB_BYTES)


def content_root(explicit: str | None = None) -> str:
    """The host installer root, as Kilix hands it to the apps it launches."""
    for value in (explicit, os.environ.get("KILIX_CONTENT_ROOT")):
        if value:
            if not os.path.isabs(value):
                raise AssetError("the content root must be an absolute path")
            return os.path.normpath(value)
    data = os.environ.get("KILIX_DATA_HOME")
    if data and os.path.isabs(data):
        return os.path.normpath(os.path.join(data, "desktop-apps"))
    raise AssetError("cannot find the Kilix content root; run inside Kilix or pass --root")


def _content():
    if _CONTENT_SRC.is_dir() and str(_CONTENT_SRC) not in sys.path:
        sys.path.insert(0, str(_CONTENT_SRC))
    try:
        import kilix_content
        from kilix_content import first_use
        import kilix_license
    except ImportError as error:
        raise AssetError("the Content component is missing; run "
                         "`git submodule update --init --recursive`") from error
    for owner, names in ((kilix_content, ("verified_packaged_catalog", "Installer", "CatalogError")),
                         (first_use, ("needs_agreement",)),
                         (kilix_license, ("ReceiptStore", "load_determined_records"))):
        missing = [name for name in names if not hasattr(owner, name)]
        if missing:
            raise AssetError(f"the pinned Content component lacks {', '.join(missing)}")
    return kilix_content, first_use, kilix_license


def _installed_spec(asset_id: str, root: str | None):
    """The verified catalog spec and install directory of a covered asset."""
    content, first_use, lic = _content()
    install_hint = f"install it with: kilix-needle install" if asset_id == ASSET_ID \
        else f"install it with: kilix models install {asset_id}"
    try:
        catalog = content.verified_packaged_catalog()
    except RuntimeError as error:
        raise AssetError(f"the Content catalog failed verification: {error}") from error
    try:
        spec = catalog.require_asset(asset_id)
    except content.CatalogError as error:
        raise AssetError(f"the pinned Content catalog has no {asset_id} asset") from error
    records = lic.load_determined_records()
    store = lic.ReceiptStore.shared()
    try:
        if first_use.needs_agreement(spec, records=records, store=store):
            raise AssetError(f"the {asset_id} licence has not been accepted; {install_hint}")
    except lic.LicenseError as error:
        raise AssetError(f"the licence receipt could not be checked: {error}") from error
    destination = content.Installer(content_root(root)).asset_destination(spec)
    if os.path.islink(destination) or not os.path.isdir(destination):
        raise AssetError(f"{asset_id} is not installed; {install_hint}")
    return spec, destination


def installed_member(asset_id: str, member: str, root: str | None = None) -> EngineImage:
    """One file of an installed, covered asset, verified and sealed in memory."""
    spec, destination = _installed_spec(asset_id, root)
    item = next((f for f in spec.files if f.path == member), None)
    if item is None:
        raise AssetError(f"the {asset_id} manifest has no {member!r} member")
    return load_verified(os.path.join(destination, member), item.sha256, item.bytes)


def installed_asset_dir(asset_id: str, root: str | None = None) -> str:
    """The directory of an installed, covered asset, every manifest file verified."""
    spec, destination = _installed_spec(asset_id, root)
    for item in spec.files:
        load_verified(os.path.join(destination, item.path), item.sha256, item.bytes).close()
    return destination


_WHEEL_LIBRARY = "needle/libneedle.so"


def library_from_wheel(wheel: EngineImage) -> EngineImage:
    """libneedle.so out of verified wheel bytes, held to its own pin.

    The needle2-runtime asset is the upstream wheel itself (kilix-content's
    archive mode needs one top-level directory; a wheel has two), so the
    catalog verifies the wheel and this verifies the library inside it.
    """
    import io
    import zipfile
    size = os.fstat(wheel.fd).st_size
    try:
        with zipfile.ZipFile(io.BytesIO(os.pread(wheel.fd, size, 0))) as archive:
            info = archive.getinfo(_WHEEL_LIBRARY)
            if info.file_size != PINNED_LIB_BYTES:
                raise AssetError("the wheel's libneedle.so is not the pinned size")
            data = archive.read(info)
    except (KeyError, zipfile.BadZipFile) as error:
        raise AssetError(f"the runtime wheel holds no usable {_WHEEL_LIBRARY}") from error
    if hashlib.sha256(data).hexdigest() != PINNED_LIB_SHA256:
        raise AssetError("the wheel's libneedle.so does not match its pinned SHA-256")
    image = os.memfd_create("kilix-needle-library", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(image, view):]
        fcntl.fcntl(image, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW
                    | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
    except OSError:
        os.close(image)
        raise
    return EngineImage(image, PINNED_LIB_SHA256)


def installed_library(root: str | None = None) -> EngineImage:
    """libneedle.so from the needle2-runtime asset: the runtime for tuned weights."""
    spec, _destination = _installed_spec("needle2-runtime", root)
    wheel = next((f.path for f in spec.files if f.path.endswith(".whl")), None)
    if wheel is None:
        raise AssetError("the needle2-runtime manifest has no wheel")
    with installed_member("needle2-runtime", wheel, root) as image:
        return library_from_wheel(image)


def from_installed(root: str | None = None) -> EngineImage:
    """Admit the installed needle2 asset, or say how to install it."""
    content, first_use, lic = _content()
    install_hint = f"install it with: kilix-needle install"
    try:
        catalog = content.verified_packaged_catalog()
    except RuntimeError as error:
        raise AssetError(f"the Content catalog failed verification: {error}") from error
    try:
        spec = catalog.require_asset(ASSET_ID)
    except content.CatalogError as error:
        raise AssetError(f"the pinned Content catalog has no {ASSET_ID} asset") from error
    records = lic.load_determined_records()
    store = lic.ReceiptStore.shared()
    try:
        if first_use.needs_agreement(spec, records=records, store=store):
            raise AssetError(f"the {ASSET_ID} licence has not been accepted; {install_hint}")
    except lic.LicenseError as error:
        raise AssetError(f"the licence receipt could not be checked: {error}") from error
    member = next((item for item in spec.files if item.path == ENGINE_MEMBER), None)
    if member is None:
        raise AssetError(f"the {ASSET_ID} manifest has no {ENGINE_MEMBER!r} member")
    destination = content.Installer(content_root(root)).asset_destination(spec)
    if os.path.islink(destination):
        raise AssetError(f"the installed {ASSET_ID} directory is a symlink; it will not be run")
    engine = os.path.join(destination, ENGINE_MEMBER)
    if not os.path.lexists(engine):
        raise AssetError(f"{ASSET_ID} is not installed; {install_hint}")
    return load_verified(engine, member.sha256, member.bytes)


# Categories Cc/Cf/Zl/Zp minus the two whitespace characters a licence may use:
# a carriage return on a consent screen can overwrite what was just read.
_SAFE_CONTROLS = "\n\t"
_MAX_SCREEN = 1024 * 1024


def _checked_screen(payload: bytes) -> bytes:
    import unicodedata
    if not payload or len(payload) > _MAX_SCREEN:
        raise AssetError("the licence screen is empty or too large")
    try:
        text = payload.decode("utf-8")
    except UnicodeError as error:
        raise AssetError("the licence screen is not UTF-8") from error
    if any(unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") and char not in _SAFE_CONTROLS
           for char in text):
        raise AssetError("the licence screen contains terminal controls; not shown")
    return payload


def install(root: str | None = None, *, supplied: str | None = None,
            stdin=None, stdout=None, asset_id: str = ASSET_ID) -> str:
    """First use: show the licence, take the typed agreement, install needle2.

    The screen, agreement, receipt and fetch are kilix-content's and its
    licence authority's; this only presents them. Consent is typed at a
    terminal and is never assumed: there is no flag that answers it. With
    `supplied`, the bytes are read from a directory instead of downloaded
    and nothing touches the network.
    """
    import tempfile
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if not (stdin.isatty() and stdout.isatty()):
        raise AssetError(f"{asset_id} is not installed, and its licence can only be "
                         f"accepted at a terminal: run `kilix-needle install`")
    content, first_use, lic = _content()
    try:
        spec = content.verified_packaged_catalog().require_asset(asset_id)
    except (RuntimeError, content.CatalogError) as error:
        raise AssetError(f"the Content catalog cannot offer {asset_id}: {error}") from error
    records = lic.load_determined_records()
    store = lic.ReceiptStore.shared()
    record = first_use.license_record_for(spec, records)
    with tempfile.TemporaryDirectory(prefix="kilix-needle-texts-") as scratch:
        texts = lic.load_determined_texts(Path(scratch) / "texts")
        screen = _checked_screen(first_use.present_asset(
            spec, record, texts, receipts=store, records=records, supplied=supplied))
        stdout.flush()
        stdout.buffer.write(screen)
        stdout.buffer.flush()
        expected = lic.typed_agreement_line(record)
        stdout.write(f"\nTo accept, type exactly:\n  {expected}\n> ")
        stdout.flush()
        typed = stdin.readline(512).strip()
        if typed != expected:
            raise AssetError("not accepted; nothing was written or downloaded")
        installer = content.Installer(content_root(root))
        try:
            first_use.install_with_agreement(
                spec, installer=installer, store=store, records=records, texts=texts,
                typed_text=typed, supplied=supplied,
                report=lambda message: print(f"  {message}", file=stdout))
        except lic.LicenseError as error:
            raise AssetError(f"the licence authority refused: {error}") from error
        return installer.asset_destination(spec)


TUNING_ASSETS = ("needle2-train", "needle2-runtime")


def missing_for_tuning(root: str | None = None) -> list[str]:
    """The tuning assets that are not installed and covered yet."""
    missing = []
    for asset_id in TUNING_ASSETS:
        try:
            _installed_spec(asset_id, root)
        except AssetError:
            missing.append(asset_id)
    return missing
