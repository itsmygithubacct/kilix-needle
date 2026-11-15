"""Ask backend selection shared by the CLI, workflow setup and model loaders."""
import json
import os
from pathlib import Path
import stat
import tempfile

import state

BACKENDS = ('lfm2', 'lfm3', 'needle3', 'grammar')
# The release command model. It is the default only on a machine that has it:
# nothing here downloads it, and without it Ask reads requests with the grammar.
DEFAULT = 'lfm2'
FALLBACK = 'grammar'
# Where a choice came from, most specific first.
SOURCES = ('option', 'environment', 'saved', 'installed', 'not-installed')
ASSETS = {'lfm3': 'lfm3-kilix-commands', 'lfm2': 'lfm2-kilix-commands', 'needle3': 'needle3-kilix-commands'}
SCHEMA = 'kilix.ask.selection/v1'
REPAIR = ('choose a model for this request with --commands-backend (kilix ask --model) '
          'lfm2, lfm3, needle3 or grammar, or save one with '
          '`kilix-needle workflows select --model lfm2|lfm3|needle3|grammar`')


class PreferenceError(ValueError):
    """The saved Ask preference cannot be used; it was left as it is."""


def preference_path():
    return state.home() / 'ask.json'


def _saved(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ValueError('Ask preference must be a small regular file')
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise ValueError('Ask preference is too large')
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get('schema') != SCHEMA \
                or record.get('backend') not in BACKENDS:
            raise ValueError('invalid Ask preference')
        return record['backend']
    finally:
        os.close(fd)


def installed(backend=DEFAULT, root=None):
    """Whether this release's model for ``backend`` is installed and covered.

    Read-only and cheap: the pinned catalog asset, a platform its runtime
    supports, its install directory with every member present at its manifest
    size, and a covering receipt. No file
    is hashed here (loading the model verifies every byte), nothing is created,
    and any doubt is "no", so an unreadable install never selects a model.
    """
    if backend not in ASSETS:
        return False
    try:
        import asset
        import workflows
        content, first_use, lic = asset._content()
        spec = content.verified_packaged_catalog().require_asset(ASSETS[backend])
        verify_catalog(backend, spec)
        require_platform(backend)
        destination = workflows._content_root(root) / 'assets' / spec.install_name
        if destination.is_symlink() or not destination.is_dir():
            return False
        for item in spec.files:
            info = os.lstat(destination / item.path)
            if not stat.S_ISREG(info.st_mode) or info.st_size != item.bytes:
                return False
        return not first_use.needs_agreement(
            spec, records=lic.load_determined_records(), store=workflows._readonly_store(lic))
    except Exception:       # noqa: BLE001 - every failure means "not installed"
        return False


def choice(explicit=None, root=None):
    """The backend and where it came from: one of ``SOURCES``.

    Explicit option, environment, saved preference, then the release model when
    it is installed, otherwise the grammar. Reading never creates state, loads
    a model or downloads one. An explicit choice can also repair an invalid
    saved preference, which is refused with that guidance rather than rewritten.
    """
    source = 'option' if explicit else 'environment'
    value = explicit or os.environ.get('KILIX_ASK_COMMANDS_BACKEND')
    if not value:
        path = preference_path()
        try:
            return _saved(path), 'saved'
        except FileNotFoundError:
            if installed(DEFAULT, root):
                return DEFAULT, 'installed'
            return FALLBACK, 'not-installed'
        except (OSError, ValueError) as error:
            raise PreferenceError(f'unusable Ask preference {path}: {error}; {REPAIR}') from error
    if value not in BACKENDS:
        raise ValueError('Ask model must be lfm2, lfm3, needle3 or grammar')
    return value, source


def resolve(explicit=None, root=None):
    """The backend ``choice`` selects."""
    return choice(explicit, root)[0]


def save(backend):
    """Atomically save an explicitly selected backend after readiness checks."""
    if backend not in BACKENDS:
        raise ValueError('Ask model must be lfm2, lfm3, needle3 or grammar')
    path = preference_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise ValueError('Ask preferences need a real state directory')
    fd, temporary = tempfile.mkstemp(prefix='.ask-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'schema': SCHEMA, 'backend': backend}, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def release(backend):
    if backend in ('lfm2', 'lfm3'):
        from commands_runtime import selection
        return selection(backend=backend)
    elif backend == 'needle3':
        from commands_needle3 import selection
    else:
        raise ValueError('grammar does not have a model asset')
    return selection()


def verify_catalog(backend, spec, chosen=None):
    """Use the same release/catalog binding for readiness and actual loading."""
    chosen = release(backend) if chosen is None else chosen
    files = {item.path: {'bytes': item.bytes, 'sha256': item.sha256} for item in spec.files}
    if spec.version != chosen.get('version') or any(files.get(name) != item
                                                   for name, item in chosen['files'].items()):
        raise ValueError(f'catalog and {backend} model pins differ')
    return chosen


def require_platform(backend):
    if backend in ('lfm2', 'lfm3'):
        from commands_runtime import require_platform as check
        check()
    elif backend == 'needle3':
        from compact_model import require_cached_platform
        require_cached_platform()
