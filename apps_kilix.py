"""Carry out admitted apps-job actions through the kilix CLI.

A launch opens a new tab running `kilix app run ID`, `kilix games play ID` or a
host tool, never in the caller's pane (`kilix app run` replaces the process
that runs it). Settings changes run `kilix settings --set` or `kilix games
enable|disable`, which Kilix applies to its running instances itself.

Installing needs a person's own yes. So "ready" comes only from Kilix's own
readiness functions, called in a Python probe that installs nothing
(`content_app.application_spec` with `Installer.ready`, `games.game_ready`
with `games.game_enabled`), never from a kilix verb: review R12 found `kilix
app install dosbox` boots DOSBox and `kilix launcher` runs an installer. Apps
Kilix builds from system or custom sources, and the host tools, install or
update inside their own verbs, so they are never ready: every launch of one
waits for a person. The tab also turns off every install switch Kilix has.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess

import apps
import kilix

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


# Every per-component install switch Kilix reads (grep of the Kilix tree).
NO_INSTALL = {name: "0" for name in (
    "KILIX_APP_AUTO_INSTALL", "KILIX_PDF_AUTO_INSTALL", "KILIX_TUI_UTILS_AUTO_INSTALL",
    "KILIX_CHAWAN_AUTO_INSTALL", "KILIX_AMP_AUTO_INSTALL", "KILIX_CAP_AUTO_INSTALL",
    "KILIX_ICEWM_AUTO_INSTALL", "KILIX_LAND_DESKTOP_AUTO_INSTALL", "KILIX_LOOK_AUTO_INSTALL",
    "KILIX_MASK_AUTO_INSTALL", "KILIX_NVR_AUTO_INSTALL", "KILIX_RTSP_AUTO_INSTALL")}

# Runs Kilix's readiness functions and nothing else; exit 0 only when ready.
PROBE = """
import sys
home, kind, name = sys.argv[1:4]
sys.dont_write_bytecode = True
sys.path[:0] = [home + "/config", home + "/desktop"]
try:
    if kind == "game":
        import games
        ready = bool(games.game_enabled(name)) and bool(games.game_ready(name))
    else:
        import content_app
        from kilix_sdk import content
        from kilix_sdk._content_runtime import apps_root
        spec = content_app.application_spec(name)
        ready = (spec.source_type in ("git", "archive")
                 and bool(content.Installer(apps_root()).ready(spec)))
except Exception:
    ready = False
sys.exit(0 if ready else 1)
"""


def _ready(kind: str, name: str) -> bool:
    """Any failure reads as not ready, so the launch waits for a person."""
    home = _kilix_home()
    if home is None:
        return False
    try:
        done = subprocess.run(["python3", "-c", PROBE, str(home), kind, name],
                              env=dict(os.environ, **NO_INSTALL), capture_output=True,
                              timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _tab(title: str, command: list[str], *, install: bool, hold: bool = False) -> tuple:
    gates = {"KILIX_APP_AUTO_INSTALL": "1"} if install else NO_INSTALL
    argv = ["launch", "--type=tab", f"--tab-title={title}",
            *(f"--env={name}={value}" for name, value in gates.items())]
    if hold:
        argv.append("--hold")
    return tuple(argv + ["--", kilix.KILIX, *command])


def resolve(action: apps.Action) -> Step:
    kind, args = action.kind, action.args
    if kind == "launch":
        app = args["app"]
        if app in apps.HOST_TOOLS:
            # Their verbs run Kilix's tui-utils installer, or install when missing.
            command, title = _HOST_ARGV[app], _TITLES[app]
            ready, hold = False, app == "transcripts"
        elif app in apps.AVAILABILITY:
            # dosbox too: Kilix keeps it in Games, and `kilix app` hands it there.
            command, title, hold = ["games", "play", app], app, False
            ready = _ready("game", app)
        else:
            command, title, hold = ["app", "run", app], app, False
            ready = _ready("app", app)
        words = f"open {app} in a new tab" + ("" if ready else " (it may install first)")
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
