"""Ask backend selection shared by the CLI, workflow setup and model loaders."""
import json
import os
from pathlib import Path
import stat
import tempfile

import state

BACKENDS = ('lfm2', 'needle3', 'grammar')
DEFAULT = 'lfm2'
ASSETS = {'lfm2': 'lfm2-kilix-commands', 'needle3': 'needle3-kilix-commands'}
SCHEMA = 'kilix.ask.selection/v1'


def preference_path():
    return state.home() / 'ask.json'


def resolve(explicit=None):
    """Explicit option, environment, saved preference, then the release default.

    Reading a preference never creates state or loads a model. An explicit
    choice can also repair an invalid saved preference.
    """
    value = explicit or os.environ.get('KILIX_ASK_COMMANDS_BACKEND')
    if not value:
        try:
            fd = os.open(preference_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except FileNotFoundError:
            return DEFAULT
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                raise ValueError('Ask preference must be a small regular file')
            raw = os.read(fd, 4097)
            if len(raw) > 4096:
                raise ValueError('Ask preference is too large')
            record = json.loads(raw)
            if not isinstance(record, dict) or record.get('schema') != SCHEMA:
                raise ValueError('invalid Ask preference')
            value = record.get('backend')
        finally:
            os.close(fd)
    if value not in BACKENDS:
        raise ValueError('Ask model must be lfm2, needle3 or grammar')
    return value


def save(backend):
    """Atomically save an explicitly selected backend after readiness checks."""
    if backend not in BACKENDS:
        raise ValueError('Ask model must be lfm2, needle3 or grammar')
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
    if backend == 'lfm2':
        from commands_runtime import selection
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
    if backend == 'lfm2':
        from commands_runtime import require_platform as check
        check()
    elif backend == 'needle3':
        from compact_model import require_cached_platform
        require_cached_platform()
