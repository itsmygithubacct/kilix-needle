"""Carry out admitted apps-job actions through the kilix CLI.

A launch opens a new tab running `kilix app run ID`, `kilix games play ID` or a
host tool, never in the caller's pane (`kilix app run` replaces the process
that runs it). Catalog apps install on first use unless told not to, so every
launch runs with KILIX_APP_AUTO_INSTALL=0 unless a person agreed to the
install. Settings changes run `kilix settings --set` or `kilix games enable|
disable`, which Kilix applies to its running instances itself.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess

import apps
import kilix

_TUI_UTILS = {"launcher": "kilix-launcher", "temps": "kilix-temps", "memory": "kilix-memory",
              "mixer": "kilix-volume"}
_HOST_ARGV = {"launcher": ["launcher"], "temps": ["temps"], "memory": ["memory"],
              "mixer": ["volume"], "transcripts": ["transcript", "list"]}
_TITLES = {"launcher": "Launcher", "temps": "Temperatures", "memory": "Memory",
           "mixer": "Volume mixer", "transcripts": "Transcripts"}


@dataclass(frozen=True)
class Step:
    action: apps.Action
    summary: str
    tab: tuple | None = None      # `kilix @ launch` argv for a new tab, or
    cli: tuple | None = None      # a kilix subcommand that returns
    ready: bool = True            # False: the launch would install first
    install_tab: tuple | None = None   # the same tab, with installing allowed


def _kilix_home() -> Path | None:
    found = shutil.which(kilix.KILIX)
    return Path(os.path.realpath(found)).parent if found else None


def _app_ready(content_id: str) -> bool:
    """`kilix app install ID` with installing refused only reports: 0 when ready."""
    env = dict(os.environ, KILIX_APP_AUTO_INSTALL="0", KILIX_PDF_AUTO_INSTALL="0")
    try:
        done = subprocess.run([kilix.KILIX, "app", "install", content_id], env=env,
                              capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _game_ready(game: str) -> bool:
    """The desktops' own readiness check; any failure reads as not ready, so the
    launch waits for a person rather than installing unasked."""
    home = _kilix_home()
    if home is None or not (home / "desktop" / "games.py").is_file():
        return False
    probe = ("import sys; sys.path[:0] = [sys.argv[1] + '/config', sys.argv[1] + '/desktop']; "
             "import games; sys.exit(0 if games.game_ready(sys.argv[2]) else 1)")
    try:
        done = subprocess.run(["python3", "-c", probe, str(home), game], capture_output=True,
                              timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _tool_ready(tool: str) -> bool:
    binary = _TUI_UTILS.get(tool)
    if binary is None:
        return True
    prefix = Path(os.environ.get("KILIX_TUI_UTILS_PREFIX") or Path.home() / ".local")
    return os.access(prefix / "bin" / binary, os.X_OK)


def _tab(title: str, command: list[str], *, install: bool, hold: bool = False) -> tuple:
    argv = ["launch", "--type=tab", f"--tab-title={title}",
            f"--env=KILIX_APP_AUTO_INSTALL={'1' if install else '0'}"]
    if hold:
        argv.append("--hold")
    return tuple(argv + ["--", "kilix", *command])


def resolve(action: apps.Action) -> Step:
    kind, args = action.kind, action.args
    if kind == "launch":
        app = args["app"]
        if app in apps.HOST_TOOLS:
            command, title = _HOST_ARGV[app], _TITLES[app]
            ready = _tool_ready(app)
            hold = app == "transcripts"
        elif app in apps.GAMES:
            command, title, hold = ["games", "play", app], app, False
            ready = _game_ready(app)
        else:
            command, title, hold = ["app", "run", app], app, False
            ready = _app_ready(app)
        words = f"open {app} in a new tab" + ("" if ready else " (it installs first)")
        return Step(action, words, tab=_tab(title, command, install=False, hold=hold), ready=ready,
                    install_tab=None if ready else _tab(title, command, install=True, hold=hold))
    if kind == "show":
        state = "on" if args["on"] else "off"
        return Step(action, f"{'show' if args['on'] else 'hide'} {args['item'].replace('_', ' ')}",
                    cli=("settings", "--set", f"{args['item']}={state}"))
    if kind == "pane_stat":
        return Step(action, f"set pane {args['stat']} to {args['mode']}",
                    cli=("settings", "--set", f"pane_{args['stat']}={args['mode']}"))
    if kind == "game":
        verb = "enable" if args["available"] else "disable"
        return Step(action, f"{verb} {args['game']} in the games list",
                    cli=("games", verb, args["game"]))
    return Step(action, f"open the {args['section']} settings in a new tab",
                tab=_tab("Settings", ["settings", "--section", args["section"]], install=False))


def perform(step: Step, *, install: bool = False) -> None:
    if step.tab is not None:
        kilix._run(list(step.install_tab if install and step.install_tab else step.tab))
        return
    try:
        done = subprocess.run([kilix.KILIX, *step.cli], capture_output=True, timeout=30,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise kilix.KilixError(f"kilix {step.cli[0]} failed: {error}") from error
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip() or f"status {done.returncode}"
        raise kilix.KilixError(f"kilix {' '.join(step.cli[:2])}: {detail}")
