"""Shared fixtures. Importing this cuts every route to the live Kilix instance."""
import os
import stat
import tempfile

# No test may reach the real desktop: without the socket address `kilix @`
# has nothing to connect to, and the module under test runs a recorder.
for name in ("KITTY_WINDOW_ID", "KILIX_CONTENT_ROOT"):
    os.environ.pop(name, None)
# An address nothing listens on: set, so no Kilix ancestor is looked for, and
# dead, so a stray real `kilix @` fails instead of reaching the desktop.
os.environ["KITTY_LISTEN_ON"] = "unix:@kilix-needle-test-no-such-socket"

# No test may reach the live store either: every state and content location
# is a scratch directory that lives as long as the test process. Review R3
# (KN-R3-04): in_use() reached the real licence-receipt store through
# KILIX_DATA_HOME, and passed only where needle2 happened to be accepted.
_SANDBOX = tempfile.TemporaryDirectory(prefix="kn-state-")
for _name in ("GPU_TERMINAL_HOME", "KILIX_DATA_HOME", "KILIX_CONFIG_HOME", "KILIX_STORAGE_HOME",
              "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
    os.environ[_name] = os.path.join(_SANDBOX.name, _name.lower())
for _name in ("KILIX_NEEDLE_LIBRARY", "KILIX_NEEDLE_ENGINE", "KILIX_ML_HOME"):
    os.environ.pop(_name, None)

import kilix  # noqa: E402

RECORDER = """#!/bin/sh
dir="$KILIX_NEEDLE_TEST_CALLS"
n=$(ls "$dir" | wc -l)
n=$(printf '%04d' "$n")
if [ "$2" = "ls" ]; then cat "$KILIX_NEEDLE_TEST_LS"; exit 0; fi
if [ "$2" = "send-text" ]; then cat > "$dir/$n.stdin"; fi
printf '%s\\0' "$@" > "$dir/$n.argv"
exit "${KILIX_NEEDLE_TEST_STATUS:-0}"
"""


class FakeKilix:
    """A `kilix` on disk that records each call and serves a fixed `ls`."""

    def __init__(self, tree):
        import json
        self._dir = tempfile.TemporaryDirectory(prefix="kn-")
        self.calls_dir = os.path.join(self._dir.name, "calls")
        os.mkdir(self.calls_dir)
        self.ls = os.path.join(self._dir.name, "ls.json")
        self.binary = os.path.join(self._dir.name, "kilix")
        with open(self.binary, "w", encoding="ascii") as handle:
            handle.write(RECORDER)
        os.chmod(self.binary, stat.S_IRWXU)
        with open(self.ls, "w", encoding="utf-8") as handle:
            json.dump(tree, handle)

    def __enter__(self):
        self._saved = kilix.KILIX
        kilix.KILIX = self.binary
        os.environ["KILIX_NEEDLE_TEST_CALLS"] = self.calls_dir
        os.environ["KILIX_NEEDLE_TEST_LS"] = self.ls
        return self

    def __exit__(self, *_exc):
        kilix.KILIX = self._saved
        os.environ.pop("KILIX_NEEDLE_TEST_STATUS", None)
        self._dir.cleanup()

    def calls(self):
        """[(argv after `@`, stdin bytes or None)] in order, excluding `ls`."""
        out = []
        for name in sorted(os.listdir(self.calls_dir)):
            if not name.endswith(".argv"):
                continue
            with open(os.path.join(self.calls_dir, name), "rb") as handle:
                argv = [part.decode() for part in handle.read().split(b"\0")[:-1]]
            stdin_path = os.path.join(self.calls_dir, name[:-5] + ".stdin")
            data = None
            if os.path.exists(stdin_path):
                with open(stdin_path, "rb") as handle:
                    data = handle.read()
            out.append((argv[1:], data))
        return out


def window(wid, title="bash", program="bash", *, active=False, neighbors=None,
           at_prompt=True, broker=None):
    env = {"KITTY_PTY_BROKER_SESSION": broker} if broker else {}
    return {"id": wid, "title": title, "is_active": active, "is_focused": active,
            "neighbors": neighbors or {}, "at_prompt": at_prompt, "env": env,
            # pids above pid_max, so ancestry can never match a real process
            "pid": 5_000_000 + wid,
            "foreground_processes": [{"cmdline": [f"/usr/bin/{program}"], "pid": 5_000_000 + wid}]}


def group(wid):
    """Kitty's window group id for a window: distinct from the window id."""
    return 9000 + wid


def tab(tid, title, windows, *, active=False, layouts=("splits", "stack", "tall", "grid")):
    # As in a real `kilix @ ls`: one group per window, and neighbours name groups.
    return {"id": tid, "title": title, "is_active": active, "is_focused": active,
            "enabled_layouts": list(layouts), "windows": windows,
            "groups": [{"id": group(w["id"]), "windows": [w["id"]]} for w in windows]}


def desktop():
    """Three tabs; the user is looking at tab 2; the caller runs in tab 3."""
    return [{"id": 1, "is_active": True, "is_focused": True, "tabs": [
        tab(10, "logs", [window(100, "tail", "tail", active=True)]),
        tab(20, "work", [
            window(200, "editor", "vim", active=True, neighbors={"right": [group(201)]},
                   at_prompt=False),
            window(201, "htop", "htop", neighbors={"left": [group(200)]}, at_prompt=False),
        ], active=True),
        tab(30, "shell", [
            window(300, "needle", "python3", active=True, neighbors={"left": [group(301)]}),
            window(301, "build", "bash", neighbors={"right": [group(300)]}, broker="ab" * 8),
            window(302, "notes", "bash"),
        ]),
    ]}]
