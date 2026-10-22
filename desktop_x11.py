"""Bounded xdotool backend. No shell, command chains, or model-owned argv.

Requires an EWMH window manager. Close sends WM_DELETE_WINDOW, never destroys
the client. X11 input is inherently racy: a focus check and an injected event
are separate server operations. Receipts do not prove application completion.
"""
from __future__ import annotations
import os
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from desktop_job import DesktopError, normalize


class X11:
    def __init__(self, display=None, xdotool=None):
        if display is None and os.environ.get("XDG_SESSION_TYPE") == "wayland":
            raise DesktopError("native Wayland is not supported; supply an explicit X11 display for an X11 session")
        self.display = display or os.environ.get("DISPLAY")
        if not self.display or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", self.display):
            raise DesktopError("set DISPLAY or pass an explicit X11 --display")
        self.xdotool = xdotool or os.environ.get("KILIX_NEEDLE_XDOTOOL") or shutil.which("xdotool")
        self.xprop = shutil.which("xprop")
        if not self.xdotool or not os.path.isabs(self.xdotool) or not os.access(self.xdotool, os.X_OK) or not self.xprop:
            raise DesktopError("xdotool and xprop must be installed (xdotool override must be an absolute executable)")
        self.env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "DISPLAY": self.display}
        if os.environ.get("XAUTHORITY"):
            self.env["XAUTHORITY"] = os.environ["XAUTHORITY"]
        elif os.environ.get("HOME"):
            self.env["HOME"] = os.environ["HOME"]
        self.deadline = time.monotonic() + 30

    def _run(self, argv, *, text=None, allow_failure=False, command_timeout=5):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DesktopError("desktop operation exceeded its 30-second limit")
        try:
            proc = subprocess.run(argv, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=min(command_timeout, remaining), env=self.env, check=False)
        except subprocess.TimeoutExpired as e:
            raise DesktopError("desktop command timed out; its effect may be uncertain") from e
        except OSError as e:
            raise DesktopError("desktop command could not start") from e
        if len(proc.stdout.encode()) > 65536 or len(proc.stderr.encode()) > 65536:
            raise DesktopError("desktop command output exceeded its limit")
        if proc.returncode and not allow_failure:
            raise DesktopError(f"{os.path.basename(argv[0])} {argv[1]} failed (status {proc.returncode})")
        return proc.stdout

    def xdo(self, *args, text=None, allow_failure=False, command_timeout=5):
        return self._run([self.xdotool, *map(str, args)], text=text, allow_failure=allow_failure,
                         command_timeout=command_timeout)

    def properties(self, *args):
        return self._run([self.xprop, *map(str, args)])

    def window_message(self, operation, wid, *, execute=False):
        argv = [sys.executable, "-B", str(Path(__file__).with_name("desktop_window.py")), operation, str(wid)]
        if execute:
            argv.append("--execute")
        raw = self._run(argv, allow_failure=True)
        try:
            reply = json.loads(raw)
        except ValueError as error:
            raise DesktopError("window helper returned no valid receipt") from error
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            note = reply.get("error", "window request failed") if isinstance(reply, dict) else "window request failed"
            raise DesktopError(str(note))

    def active(self):
        value = self.properties("-root", "_NET_ACTIVE_WINDOW")
        match = re.search(r"# (0x[0-9a-fA-F]+)", value)
        return int(match[1], 16) if match and int(match[1], 16) else None

    def geometry(self, wid):
        raw = self.xdo("getwindowgeometry", "--shell", wid)
        fields = dict(re.findall(r"^(X|Y|WIDTH|HEIGHT)=(-?\d+)$", raw, re.M))
        if len(fields) != 4:
            raise DesktopError("could not read complete window geometry")
        return {k.lower(): int(v) for k, v in fields.items()}

    def identity(self, wid):
        title = self.xdo("getwindowname", wid).rstrip("\n")
        if len(title.encode()) > 1024:
            raise DesktopError("window title exceeded its limit")
        props = self.properties("-id", wid, "_NET_WM_PID", "WM_CLASS")
        pid = re.search(r"_NET_WM_PID\([^)]*\) = (\d+)", props)
        klass = re.search(r"WM_CLASS\([^)]*\) = (.*)", props)
        return dict(id=wid, title=title, pid=int(pid[1]) if pid else None, wm_class=klass[1] if klass else None)

    def snapshot(self):
        self.deadline = time.monotonic() + 30
        root = self.properties("-root", "_NET_CLIENT_LIST_STACKING")
        if "#" not in root:
            raise DesktopError("this X11 display needs an EWMH window manager")
        ids = list(dict.fromkeys(int(s, 16) for s in re.findall(r"0x[0-9a-fA-F]+", root)))
        if len(ids) > 100:
            raise DesktopError("more than 100 managed windows; narrow the desktop session")
        geometry = self.xdo("getdisplaygeometry").split()
        if len(geometry) != 2 or not all(n.isdigit() for n in geometry):
            raise DesktopError("could not read the screen dimensions")
        windows = []
        for wid in ids:
            # A disappearing window makes this snapshot incomplete; do not hide it.
            windows.append({**self.identity(wid), "geometry": self.geometry(wid)})
        return dict(display=self.display, width=int(geometry[0]), height=int(geometry[1]),
                    active=self.active(), windows=windows, observed_unix=time.time())

    @staticmethod
    def resolve(target, snapshot, active):
        if target == "active":
            matches = [w for w in snapshot["windows"] if w["id"] == active]
        elif target.startswith("id:"):
            matches = [w for w in snapshot["windows"] if w["id"] == int(target[3:])]
        else:
            matches = [w for w in snapshot["windows"] if w["title"] == target[6:]]
        if len(matches) != 1:
            raise DesktopError("window target is missing or ambiguous; use an exact title or ID from desktop list")
        return {k: matches[0][k] for k in ("id", "title", "pid", "wm_class")}

    def plan(self, calls, snapshot):
        actions = normalize(calls)
        active, closed, result = snapshot["active"], set(), []
        for call in actions:
            name, args = call["name"], call["arguments"]
            item = dict(name=name, arguments=args)
            if "x" in args and name == "pointer" and not (args["x"] < snapshot["width"] and args["y"] < snapshot["height"]):
                raise DesktopError("pointer coordinates are outside this screen")
            if name == "window" and args["action"] != "list":
                target = self.resolve(args["target"], snapshot, active)
                if target["id"] in closed:
                    raise DesktopError("a sequence cannot reuse a window after closing it")
                item["target"] = target
                if args["action"] == "focus":
                    active = target["id"]
                elif args["action"] in ("close", "minimize") and target["id"] == active:
                    active = None
                if args["action"] == "close":
                    closed.add(target["id"])
            elif name == "keyboard":
                item["target"] = self.resolve("active", snapshot, active)
                if item["target"]["id"] in closed:
                    raise DesktopError("the typing target was closed earlier in the sequence")
            result.append(item)
        # Preflight every window protocol before any action in a batch executes.
        # The helper works with Debian's xdotool and never substitutes a kill.
        for item in result:
            op = item["arguments"].get("action")
            if item["name"] == "window" and op in ("close", "maximize", "restore"):
                self.window_message(op, item["target"]["id"])
        return result

    def _recheck(self, expected):
        current = self.identity(expected["id"])
        # PID/class identify the client when available. Without PID, preserve
        # the title too. X11 IDs have no generation counter; reuse remains a limit.
        keys = ("id", "pid", "wm_class") if expected["pid"] else ("id", "pid", "wm_class", "title")
        if any(current[k] != expected[k] for k in keys):
            raise DesktopError("window identity changed; obtain a new plan")

    def execute(self, plan):
        self.deadline = time.monotonic() + 30
        results = []
        for item in plan:
            name, args, target = item["name"], item["arguments"], item.get("target")
            receipt = {"name": name, "arguments": args, "status": "pending", "application_completion_verified": False}
            results.append(receipt)
            attempted = False
            try:
                if target:
                    self._recheck(target)
                    receipt["window_id"] = target["id"]
                if name == "window":
                    op = args["action"]
                    if op == "list":
                        receipt.update(status="observed", observation=self.snapshot())
                        continue
                    wid = target["id"]
                    attempted = True
                    if op == "focus":
                        self.xdo("windowactivate", "--sync", wid)
                        if self.active() != wid:
                            raise DesktopError("requested focus was not observed")
                        receipt["focus_verified"] = True
                    elif op == "move":
                        self.xdo("windowmove", "--sync", wid, args["x"], args["y"])
                        receipt["observed_geometry"] = self.geometry(wid)
                    elif op == "resize":
                        self.xdo("windowsize", "--sync", wid, args["width"], args["height"])
                        receipt["observed_geometry"] = self.geometry(wid)
                    elif op == "minimize":
                        self.xdo("windowminimize", "--sync", wid)
                    elif op in ("maximize", "restore", "close"):
                        self.window_message(op, wid, execute=True)
                elif name == "keyboard":
                    if self.active() != target["id"]:
                        raise DesktopError("focus changed; no keyboard event sent")
                    attempted = True
                    if args["action"] == "type":
                        # stdin makes leading dashes and command-like text literal.
                        # Temporary Unicode key mappings need time to reach the
                        # client before xdotool restores them, especially under
                        # CPU load. Keep the whole operation within its deadline.
                        unicode_text = not args["text"].isascii()
                        self.xdo("type", "--clearmodifiers", "--delay", "40" if unicode_text else "12",
                                 "--file", "-", text=args["text"], command_timeout=25 if unicode_text else 8)
                    else:
                        self.xdo("key", "--clearmodifiers", args["keys"])
                elif name == "pointer":
                    op = args["action"]
                    button = {"left": 1, "middle": 2, "right": 3}.get(args.get("button"))
                    attempted = True
                    if op == "drag":
                        try:
                            self.xdo("mousedown", button)
                            self.xdo("mousemove", "--sync", args["x"], args["y"])
                        finally:
                            # Even a timeout must leave enough time to release
                            # the button this drag attempted to press.
                            deadline = self.deadline
                            self.deadline = time.monotonic() + 3
                            try:
                                self.xdo("mouseup", button)
                            finally:
                                self.deadline = deadline
                    else:
                        if "x" in args:
                            self.xdo("mousemove", "--sync", args["x"], args["y"])
                        if op != "move":
                            self.xdo("click", "--clearmodifiers", "--repeat", 2 if op == "double_click" else 1, "--delay", 100, button)
                elif name == "scroll":
                    attempted = True
                    self.xdo("click", "--clearmodifiers", "--repeat", args["steps"], "--delay", 30,
                             {"up": 4, "down": 5, "left": 6, "right": 7}[args["direction"]])
                elif name == "wait":
                    duration = args["ms"] / 1000
                    if duration > self.deadline - time.monotonic():
                        raise DesktopError("pause would exceed the desktop operation deadline")
                    time.sleep(duration)
                    receipt["status"] = "waited"
                    continue
                receipt["status"] = "submitted"
            except (DesktopError, OSError) as error:
                receipt.update(status="uncertain" if attempted else "blocked", error=str(error))
                return dict(status=1, outcome="partial" if len(results) > 1 or attempted else "blocked", results=results,
                            note="stopped at the first failure; observe the desktop before retrying")
        return dict(status=0, outcome="submitted", results=results)
