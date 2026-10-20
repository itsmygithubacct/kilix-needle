"""Windows desktop backend over the Kilix desktop SDK protocol (kilix.desktop/v1).

Each step is one JSON request on the stdin of a fixed command: `kilix desktop`
inside WSL, or `kilix windows --host HOST` from Linux over the user's own SSH
configuration. Needle resolves every window to one exact identity (HWND, PID
and process start time) before acting; the native worker rechecks it. Input
needs the target in the foreground, so typing, keys, clicks and scrolls are
preceded by an explicit focus step that the plan shows. Nothing is retried.
"""
from __future__ import annotations

import json
import math
import ntpath
import os
import re
import shutil
import subprocess
import time

import windows_job as job

SCHEMA = "kilix.desktop/v1"
SEQUENCE_SECONDS = 180
MAX_RESPONSE = 48 * 1024 * 1024
# Native refusals raised before any mutation; anything else on a mutating
# step may have taken effect and needs observation before another attempt.
_REFUSALS = frozenset("""invalid_request window_not_found ambiguous_target search_incomplete foreground_mismatch
element_not_found ambiguous_element element_disabled element_offscreen accessibility_incomplete
invalid_window_state input_busy stale_element desktop_unavailable access_denied unavailable locked
point_outside_window target_mismatch wait_timeout stale_target elevated_target window_moved window_occluded
invalid_coordinates cursor_clipped unsupported_pattern read_only_element password_element desktop_busy
session_mismatch previous_operation_incomplete dpi_unavailable invalid_path shell_window""".split())
_INPUT = frozenset(["keyboard", "pointer", "scroll", "control.invoke", "control.set_value"])


class WindowsDesktopError(job.WindowsActionError):
    def __init__(self, message, code="refused"):
        super().__init__(message)
        self.code = code


def in_wsl():
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/sys/kernel/osrelease", encoding="ascii", errors="replace") as source:
            return "microsoft" in source.read().lower()
    except OSError:
        return False


class Transport:
    """Send one kilix.desktop/v1 request and return its validated envelope."""

    def __init__(self, *, host=None, windows_root=None, kilix=None, run=None):
        kilix = kilix or os.environ.get("KILIX_NEEDLE_KILIX") or shutil.which("kilix")
        if not kilix:
            raise WindowsDesktopError("the kilix command was not found; set KILIX_NEEDLE_KILIX", "unavailable")
        if host is not None:
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]{0,252}", host):
                raise WindowsDesktopError("use an SSH host or configured alias, without options", "invalid_request")
            prefix = [kilix, "windows", "--host", host]
            if windows_root is not None:
                if "\0" in windows_root or len(windows_root) > 4096:
                    raise WindowsDesktopError("invalid Windows package root", "invalid_request")
                prefix += ["--windows-root", windows_root]
        else:
            if windows_root is not None:
                raise WindowsDesktopError("--windows-root applies only with --host", "invalid_request")
            if not in_wsl():
                raise WindowsDesktopError("outside WSL, select a Windows machine with --host SSH_ALIAS", "unavailable")
            prefix = [kilix, "desktop"]
        self.argv = [*prefix, "--json", "--request-json", "-"]
        self.where = host or "wsl"
        self.run = run or subprocess.run

    def __call__(self, operation, params=None, *, target=None, timeout=20):
        request = {"schema": SCHEMA, "operation": operation, "params": params or {}, "timeout": timeout}
        if target is not None:
            request["target"] = target
        try:
            proc = self.run(self.argv, input=json.dumps(request, ensure_ascii=True).encode(),
                            capture_output=True, timeout=timeout + 45, check=False)
        except subprocess.TimeoutExpired as error:
            raise WindowsDesktopError("the desktop request timed out; it may have run, so observe before retrying", "timeout") from error
        except OSError as error:
            raise WindowsDesktopError(f"could not start kilix: {error}", "transport_error") from error
        stdout = proc.stdout if isinstance(proc.stdout, bytes) else proc.stdout.encode()
        if len(stdout) > MAX_RESPONSE:
            raise WindowsDesktopError("desktop response exceeded its limit", "transport_error")
        try:
            reply = json.loads(stdout.decode("utf-8").lstrip("﻿"))
        except (UnicodeDecodeError, ValueError):
            stderr = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else proc.stderr or ""
            raise WindowsDesktopError("kilix returned no desktop response (exit %d)%s" % (
                proc.returncode, ": " + stderr.strip()[-400:] if stderr.strip() else ""), "transport_error") from None
        if (not isinstance(reply, dict) or reply.get("schema") != SCHEMA or reply.get("operation") != operation
                or type(reply.get("ok")) is not bool):
            raise WindowsDesktopError("kilix returned an invalid desktop response", "transport_error")
        if not reply["ok"]:
            error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
            raise WindowsDesktopError(str(error.get("message") or "the desktop refused the request"),
                                      str(error.get("code") or "refused"))
        return reply.get("result")


def _app_of(row):
    path = row.get("process_path") or ""
    return ntpath.basename(path)


def matches(row, selector):
    """Kilix SDK window filters: app exact basename/path, title substring, both ignoring case."""
    app, title = selector.get("app"), selector.get("title")
    if app is not None:
        path = (row.get("process_path") or "").replace("/", "\\").lower()
        wanted = app.replace("/", "\\").lower()
        if (path if "\\" in wanted else ntpath.basename(path)) != wanted:
            return False
    if title is not None and title.lower() not in (row.get("title") or "").lower():
        return False
    if app is not None and title is None and not (row.get("title") or "").strip():
        return False  # Untitled shell and helper windows are not addressable by app alone.
    return True


def describe(row):
    return {"title": row.get("title"), "app": _app_of(row), "hwnd": row["target"]["hwnd"],
            "pid": row["target"]["pid"], "minimized": row.get("minimized")}


def _is_input(item):
    return item["name"] in _INPUT or f'{item["name"]}.{item["arguments"].get("action")}' in _INPUT


def _refuse_shell(window):
    """Typing into a console, terminal, Explorer, Start or Run executes it."""
    app = ntpath.basename(window.get("app") or "").lower()
    title = (window.get("title") or "").strip().lower()
    if app in job.SHELL_APPS or title in job.SHELL_TITLES:
        raise WindowsDesktopError(f'"{window.get("title") or app}" runs what is typed into it; '
                                  "Needle does not type, click or scroll there", "shell_window")


def _covers(selector, deferred):
    """A named window that is the one this request launched or waited for."""
    named = {k: v.lower() for k, v in selector.items()}
    earlier = {k: v.lower() for k, v in deferred.items()}
    return named == earlier or ("app" in named and "title" not in named and named["app"] == earlier.get("app"))


def _selector(args):
    return {key: args[key] for key in ("app", "title") if key in args}


def _launch_selector(args):
    for program, owner, title in job.APPS.values():
        if program.lower() == args["program"].lower():
            return {"app": owner, **({"title": title} if title else {})}
    return {"app": ntpath.basename(args["program"])}


class Windows:
    def __init__(self, transport=None, *, host=None, windows_root=None, kilix=None):
        self.call = transport or Transport(host=host, windows_root=windows_root, kilix=kilix)
        self.where = getattr(self.call, "where", "custom")
        self.deadline = time.monotonic() + SEQUENCE_SECONDS

    def _request(self, operation, params=None, *, target=None, timeout=20):
        remaining = self.deadline - time.monotonic()
        if remaining < 2:
            raise WindowsDesktopError("the action sequence exceeded its time limit", "timeout")
        return self.call(operation, params, target=target, timeout=max(1, min(timeout, 60, int(remaining) - 1)))

    def snapshot(self):
        self.deadline = time.monotonic() + SEQUENCE_SECONDS
        status = self._request("status")
        if not isinstance(status, dict):
            raise WindowsDesktopError("desktop status was malformed", "transport_error")
        if not status.get("available"):
            reason = status.get("reason") or ("locked" if status.get("locked") else "not interactive")
            raise WindowsDesktopError(f"the Windows desktop is unavailable ({reason}); sign in and unlock it", "desktop_unavailable")
        listing = self._request("windows")
        rows = listing.get("windows") if isinstance(listing, dict) else listing
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("target"), dict) for r in rows):
            raise WindowsDesktopError("window listing was malformed", "transport_error")
        return {"where": self.where, "user": status.get("user"), "session": status.get("session"),
                "windows": rows, "incomplete": bool(isinstance(listing, dict) and (listing.get("incomplete") or listing.get("skipped"))),
                "observed_unix": time.time()}

    @staticmethod
    def resolve(selector, rows):
        found = [row for row in rows if matches(row, selector)]
        if len(found) > 1 and "title" in selector:
            # A dialog titled "Notepad" beside "notes - Notepad": one exact title wins.
            exact = [row for row in found if (row.get("title") or "").lower() == selector["title"].lower()]
            found = exact if len(exact) == 1 else found
        if len(found) == 1:
            return found[0]
        wanted = " ".join(f'{k} "{v}"' for k, v in selector.items())
        if not found:
            raise WindowsDesktopError(f"no window matches {wanted}; list windows to see titles", "window_not_found")
        options = "; ".join(f'"{r.get("title")}" ({_app_of(r)}, hwnd {r["target"]["hwnd"]})' for r in found[:6])
        raise WindowsDesktopError(f"{len(found)} windows match {wanted}: {options}. Name the title too", "ambiguous_target")

    def plan(self, actions, snapshot):
        actions = job.normalize(actions)
        rows = snapshot["windows"]
        result, current, focused, closed, deferred = [], None, None, set(), []

        def bind(args, required=True):
            nonlocal current
            selector = _selector(args)
            # "open notepad then type ... in notepad" means the new window, not an older one.
            earlier = next((w for w in reversed(deferred) if _covers(selector, w["selector"])), None) if selector else None
            if earlier is not None:
                window = earlier
            elif selector:
                row = self.resolve(selector, rows)
                window = {"selector": selector, "identity": row["target"], "window": describe(row)}
            elif current is not None:
                window = current
            elif required:
                raise WindowsDesktopError("name the window (for example: in notepad) or open/focus it earlier in the request")
            else:
                return None
            key = window.get("identity", {}).get("hwnd") or ("deferred", window.get("launch"), window.get("wait"))
            if key in closed:
                raise WindowsDesktopError("a sequence cannot use a window after closing it")
            current = window
            return window, key

        for index, call in enumerate(actions):
            name, args = call["name"], call["arguments"]
            item = {"name": name, "arguments": args}
            action = args.get("action")
            if name == "launch":
                # A new window usually, but not always, gets the foreground: focus it explicitly.
                current = {"selector": _launch_selector(args), "launch": index, "deferred": "resolved after launch"}
                deferred.append(current)
            elif name == "window" and action == "list":
                pass
            elif name == "window" and action == "find":
                item["matches"] = [describe(r) for r in rows if matches(r, _selector(args))]
            elif name == "wait" and args["for"] == "time":
                pass
            elif name == "wait" and args["for"] == "window" and _selector(args):
                item["selector"] = _selector(args)
                present = [describe(r) for r in rows if matches(r, item["selector"])]
                item["observed_now"] = present
                if args["state"] != "closed":
                    # "wait for the Save As window to appear then ... in it"
                    current = {"selector": item["selector"], "wait": index, "deferred": "resolved after the wait"}
                    item["_binds"] = current
                    deferred.append(current)
            else:
                window, key = bind(args)
                if _is_input(item):
                    _refuse_shell(window.get("window") or window["selector"])
                item["window"] = {k: v for k, v in window.items() if k not in ("launch", "wait")}
                item["_window"] = window
                if name in ("keyboard", "pointer", "scroll") and focused != key:
                    result.append({"name": "window", "arguments": {"action": "focus"}, "implied": True,
                                   "window": item["window"], "_window": window})
                    focused = key
                if name == "window" and action == "focus":
                    focused = key
                elif name == "window" and action in ("minimize", "close") and focused == key:
                    focused = None
                if name == "window" and action == "close":
                    closed.add(key)
            result.append(item)
        return result

    # Execution ---------------------------------------------------------------

    def _identity(self, item, launches):
        window = item["_window"]
        if "identity" in window:
            return window["identity"]
        if "launch" in window:
            launched = launches.get(window["launch"])
            if launched and launched.get("target"):
                window["identity"] = launched["target"]
                return window["identity"]
            # Applications such as Calculator hand their window to another process.
            row = self._wait_window(window["selector"], "visible", 9000)
        else:
            listing = self._request("find", dict(window["selector"]))
            row = self.resolve(window["selector"], listing.get("windows") if isinstance(listing, dict) else [])
        window.update(identity=row["target"], window=describe(row))
        return window["identity"]

    def _wait_window(self, selector, state, ms):
        """Wait by selector with Needle's disambiguation; the native wait refuses
        any substring ambiguity, so it is used only while nothing matches."""
        end = time.monotonic() + ms / 1000

        def remaining():
            return max(0, int((end - time.monotonic()) * 1000))
        while True:
            listing = self._request("find", dict(selector))
            rows = [r for r in (listing.get("windows") if isinstance(listing, dict) else []) if matches(r, selector)]
            if rows:
                row = self.resolve(selector, rows)
                ready = {"present": True, "visible": row.get("visible") and not row.get("minimized"),
                         "enabled": row.get("visible") and not row.get("minimized") and row.get("enabled"),
                         "closed": False}[state]
                if not ready:
                    if remaining() < 100:
                        raise WindowsDesktopError(f"the window did not become {state} in time", "wait_timeout")
                    self._request("wait", {"state": state, "wait_ms": remaining(), "poll_ms": 100},
                                  target=row["target"], timeout=min(60, math.ceil(remaining() / 1000) + 2))
                return None if state == "closed" else row
            if state == "closed":
                return None
            if remaining() < 100:
                raise WindowsDesktopError(f"no matching window became {state} in time", "wait_timeout")
            try:
                self._request("wait", {"state": "present", "wait_ms": remaining(), "poll_ms": 100, **selector},
                              timeout=min(60, math.ceil(remaining() / 1000) + 2))
            except WindowsDesktopError as error:
                if error.code != "ambiguous_target":
                    raise
            time.sleep(0.2)  # the native filter also counts untitled windows that Needle skips

    def _row(self, identity):
        listing = self._request("windows", {"pid": identity["pid"]})
        rows = listing.get("windows") if isinstance(listing, dict) else listing
        for row in rows or []:
            if row.get("target") == identity:
                return row
        raise WindowsDesktopError("the window is no longer visible", "window_not_found")

    def _element(self, identity, args):
        """Exact selector for a control; a unique case-insensitive name match is accepted."""
        selector = {k: args[k] for k in ("name", "control_type", "automation_id") if k in args}
        found = self._request("find", {"selector": selector, "depth": 32, "limit": 2000}, target=identity)
        elements = _elements(found)
        if len(elements) == 1:
            return selector, None
        if not elements and "name" in selector and "control_type" in selector:
            broad = self._request("find", {"selector": {"control_type": selector["control_type"]}, "depth": 32, "limit": 2000},
                                  target=identity)
            rows = _elements(broad)
            same = [e for e in rows if isinstance(e.get("name"), str) and e["name"].lower() == selector["name"].lower()]
            if len(same) == 1:
                return {**selector, "name": same[0]["name"]}, same[0]["name"]
            if len(same) > 1:
                raise WindowsDesktopError(f'{len(same)} {selector["control_type"]} controls are named "{selector["name"]}"', "ambiguous_element")
            if isinstance(broad, dict) and (broad.get("incomplete") or broad.get("truncated")):
                raise WindowsDesktopError("the control search was incomplete; inspect the window", "accessibility_incomplete")
        if len(elements) > 1:
            raise WindowsDesktopError(f"{len(elements)} controls match; add the control type or automation id", "ambiguous_element")
        if isinstance(found, dict) and (found.get("incomplete") or found.get("truncated")):
            raise WindowsDesktopError("the control search was incomplete; inspect the window", "accessibility_incomplete")
        raise WindowsDesktopError("no control matches; inspect the window to see its control names", "element_not_found")

    def _step(self, item, launches):
        name, args = item["name"], item["arguments"]
        action = args.get("action")
        if name == "launch":
            params = {"argv": [args["program"], *([args["document"]] if "document" in args else [])]}
            result = self._request("launch", params, timeout=15)
            return "started", {"pid": result.get("pid"), "window_found": result.get("window_found"),
                               "target": result.get("target"), "exited": result.get("exited")}, result
        if name == "window" and action == "list":
            listing = self._request("windows")
            rows = listing.get("windows") if isinstance(listing, dict) else listing
            return "observed", {"windows": [describe(r) for r in rows if (r.get("title") or "").strip()]}, None
        if name == "window" and action == "find":
            listing = self._request("find", _selector(args))
            rows = listing.get("windows") if isinstance(listing, dict) else []
            return "observed", {"windows": [describe(r) for r in rows if matches(r, _selector(args))]}, None
        if name == "wait" and args["for"] == "time":
            if args["ms"] / 1000 > self.deadline - time.monotonic() - 1:
                raise WindowsDesktopError("the pause would exceed the sequence time limit", "timeout")
            time.sleep(args["ms"] / 1000)
            return "waited", None, None
        if name == "wait" and "selector" in item:
            found = self._wait_window(item["selector"], args["state"], args["ms"])
            if found is not None and "_binds" in item:
                item["_binds"].update(identity=found["target"], window=describe(found))
            return "confirmed", {"state": args["state"], **({"window": describe(found)} if found else {})}, None
        identity = self._identity(item, launches)
        if _is_input(item) and "window" in item["_window"]:
            _refuse_shell(item["_window"]["window"])  # a deferred window, known only now
        if name == "window":
            if action in ("move", "resize"):
                rect = self._row(identity)["rect"]
                params = {"x": args.get("x", rect["x"]), "y": args.get("y", rect["y"]),
                          "width": args.get("width", rect["width"]), "height": args.get("height", rect["height"])}
                result = self._request("move", params, target=identity)
                return "submitted", {"rect": result.get("rect")} if isinstance(result, dict) else None, result
            result = self._request(action, {}, target=identity)
            return "submitted", {"foreground": True} if action == "focus" else None, result
        if name == "control":
            if action == "inspect":
                result = self._request("inspect", {"depth": 4, "limit": 300}, target=identity)
                return "observed", result, None
            if action == "find":
                selector = {k: args[k] for k in ("name", "control_type", "automation_id") if k in args}
                result = self._request("find", {"selector": selector, "depth": 32, "limit": 2000}, target=identity)
                return "observed", result, None
            selector, resolved = self._element(identity, args)
            params = {"selector": selector, "depth": 32, "limit": 20000}
            if action == "set_value":
                params["text"] = args["text"]
            result = self._request("invoke" if action == "invoke" else "set-value", params, target=identity)
            return "submitted", {"resolved_name": resolved} if resolved else None, result
        if name == "keyboard":
            if action == "type":
                result = self._request("type", {"text": args["text"]}, target=identity, timeout=30)
            else:
                result = self._request("key", {"keys": args["keys"].split("+")}, target=identity)
            return "submitted", None, result
        if name == "pointer":
            params = {"x": args["x"], "y": args["y"], "button": args["button"],
                      "count": 2 if action == "double_click" else 1}
            return "submitted", None, self._request("click", params, target=identity)
        if name == "scroll":
            if "x" in args:
                x, y = args["x"], args["y"]
            else:
                rect = self._row(identity)["rect"]
                x, y = rect["width"] // 2, rect["height"] // 2
            delta = 120 * args["steps"] * (1 if args["direction"] == "up" else -1)
            return "submitted", {"x": x, "y": y, "delta": delta}, self._request("scroll", {"x": x, "y": y, "delta": delta}, target=identity)
        if name == "wait":
            if args["for"] == "window":
                params = {"state": args["state"], "wait_ms": args["ms"], "poll_ms": 100}
            else:
                params = {"state": args["state"], "wait_ms": args["ms"], "poll_ms": 100, "depth": 32, "limit": 2000,
                          "selector": {k: args[k] for k in ("name", "control_type", "automation_id") if k in args}}
            result = self._request("wait", params, target=identity, timeout=min(60, math.ceil(args["ms"] / 1000) + 2))
            return "confirmed", {"state": args["state"]}, result
        raise WindowsDesktopError(f"unsupported action {name}")

    def execute(self, plan, progress=None):
        self.deadline = time.monotonic() + SEQUENCE_SECONDS
        results, launches = [], {}
        for index, item in enumerate(plan):
            receipt = {"name": item["name"], "arguments": item["arguments"], "status": "pending",
                       "application_completion_verified": False}
            if item.get("implied"):
                receipt["implied"] = True
            results.append(receipt)
            mutating = (item["name"], item["arguments"].get("action")) not in job.READ_ONLY and item["name"] != "wait"
            started = time.monotonic()
            if progress:
                progress(index, len(plan), item)
            try:
                status, observed, raw = self._step(item, launches)
                receipt["status"] = status
                if observed is not None:
                    receipt["observation" if status == "observed" else "result"] = observed
                if item["name"] == "launch":
                    launches[_launch_index(plan, item)] = raw
                if "_window" in item and "identity" in item["_window"]:
                    receipt["window"] = item["_window"]["identity"]
            except WindowsDesktopError as error:
                uncertain = mutating and error.code not in _REFUSALS
                status = "uncertain" if uncertain else "failed" if error.code == "wait_timeout" else "blocked"
                receipt.update(status=status, error={"code": error.code, "message": str(error)})
                receipt["elapsed_ms"] = round((time.monotonic() - started) * 1000)
                # An unknown effect outranks everything: the caller must look before acting again.
                return dict(status=1, outcome="uncertain" if uncertain else "partial" if index else status,
                            results=results, note="stopped at the first failure; observe the desktop before retrying")
            receipt["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        return dict(status=0, outcome="submitted" if any(r["status"] == "submitted" or r["status"] == "started" for r in results)
                    else "observed" if all(r["status"] == "observed" for r in results) else "completed", results=results)


def _elements(found):
    rows = found.get("elements") if isinstance(found, dict) else None
    if not isinstance(rows, list):
        raise WindowsDesktopError("control search response was malformed", "transport_error")
    return rows


def _launch_index(plan, item):
    """The launch's position among the original actions, as recorded by plan()."""
    position = 0
    for entry in plan:
        if entry is item:
            return position
        if not entry.get("implied"):
            position += 1
    raise WindowsDesktopError("launch step is missing from its plan")


def public_plan(plan):
    return [{k: v for k, v in item.items() if not k.startswith("_")} for item in plan]
