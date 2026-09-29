"""kilix-needle's one state directory: $GPU_TERMINAL_HOME/kilix-apps/kilix-needle.

Model selection, tuning runs, request history, the system normalizer profile and the
logs index all live under it. The normalizer profile and the logs index used to live
in $XDG_DATA_HOME/kilix-needle; `place` moves them over the first time they are used.
~/.config/kilix-needle/dirs.json is configuration a person edits, not state, and stays.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

_PARTS = ("kilix-apps", "kilix-needle")


def home() -> Path:
    root = Path(os.environ.get("GPU_TERMINAL_HOME") or Path.home() / ".local" / "gpu_terminal")
    return root.joinpath(*_PARTS)


def _legacy(name: str) -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "kilix-needle" / name


def place(name: str, moved=None) -> Path:
    """The state subdirectory `name`, adopting the old data-home copy if there is one.

    Only a real directory the caller owns is moved, and only when the new place is
    empty; anything else is left where it is for a person to look at. `moved(old, new)`
    runs after a move, for state that records its own location.
    """
    target = home() / name
    old = _legacy(name)
    try:
        info = old.lstat()
    except FileNotFoundError:
        return target
    if (os.path.lexists(target) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()):
        return target
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.rename(old, target)
    except FileNotFoundError:
        return target          # another process adopted it first
    if moved is not None:
        moved(old, target)
    try:
        old.parent.rmdir()     # the old kilix-needle directory, once nothing is left in it
    except OSError:
        pass
    return target
