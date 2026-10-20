"""Windows desktop job: grammar, grounding, planning and SDK request sequences."""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import desktop_windows as backend  # noqa: E402
import mcp_server  # noqa: E402
import windows_cli  # noqa: E402
import windows_grammar as grammar  # noqa: E402
import windows_job as job  # noqa: E402


def row(hwnd, title, exe, pid=None, started="133000000000000000", rect=None, minimized=False):
    return {"title": title, "class": "C", "rect": rect or {"x": 10, "y": 20, "width": 800, "height": 600},
            "minimized": minimized, "visible": True, "enabled": True,
            "target": {"hwnd": str(hwnd), "pid": pid or hwnd % 60000 + 1, "started": started},
            "process_path": "C:\\Windows\\System32\\" + exe}


class FakeDesktop:
    """The SDK contract as Needle relies on it: exact targets, foreground input, unique selectors."""

    def __init__(self, rows=None, elements=None):
        self.rows = list(rows or [])
        self.elements = elements or {}
        self.foreground = None
        self.calls = []
        self.fail = {}
        self.launched = []
        self.where = "fake"

    def _target(self, target):
        for r in self.rows:
            if r["target"] == target:
                return r
        raise backend.WindowsDesktopError("stale target", "window_not_found")

    def __call__(self, operation, params=None, *, target=None, timeout=20):
        params = params or {}
        self.calls.append((operation, json.loads(json.dumps(params)), target))
        if operation in self.fail:
            code = self.fail[operation]
            raise backend.WindowsDesktopError(f"{operation} failed", code)
        if operation == "status":
            return {"available": True, "user": "PC\\me", "session": 1}
        if operation in ("windows", "find") and "selector" not in params:
            rows = [r for r in self.rows if backend.matches(r, {k: params[k] for k in ("app", "title") if k in params})
                    or (not params.get("app") and not params.get("title") and "pid" not in params)]
            if "pid" in params:
                rows = [r for r in self.rows if r["target"]["pid"] == params["pid"]]
            return {"windows": rows, "skipped": 0, "incomplete": False}
        if operation == "find":
            w = self._target(target)
            sel = params["selector"]
            found = [e for e in self.elements.get(w["target"]["hwnd"], [])
                     if all(e.get(k) == v for k, v in sel.items())]
            return {"target": target, "elements": found, "visited": 10, "incomplete": False, "truncated": False}
        if operation == "launch":
            exe = params["argv"][0]
            new = row(900000 + len(self.launched), "Untitled - Notepad" if exe == "notepad.exe" else "Calculator",
                      "notepad.exe" if exe == "notepad.exe" else "ApplicationFrameHost.exe")
            self.launched.append(params["argv"])
            self.rows.append(new)
            self.foreground = new["target"]["hwnd"]
            return {"pid": new["target"]["pid"], "started": new["target"]["started"],
                    "target": new["target"] if exe == "notepad.exe" else None, "window_found": exe == "notepad.exe"}
        if operation == "wait":
            return {"state": params["state"], "elapsed_ms": 5}
        w = self._target(target)
        hwnd = w["target"]["hwnd"]
        if operation == "focus":
            self.foreground = hwnd
            return {"target": target, "foreground": True}
        if operation in ("type", "key", "click", "scroll") and self.foreground != hwnd:
            raise backend.WindowsDesktopError("not foreground", "foreground_mismatch")
        if operation in ("invoke", "set-value"):
            sel = params["selector"]
            found = [e for e in self.elements.get(hwnd, []) if all(e.get(k) == v for k, v in sel.items())]
            if len(found) != 1:
                raise backend.WindowsDesktopError("selector", "element_not_found" if not found else "ambiguous_element")
        if operation == "close":
            self.rows.remove(w)
        if operation == "move":
            w["rect"] = {k: params[k] for k in ("x", "y", "width", "height")}
            return {"target": target, "rect": w["rect"]}
        return {"target": target}

    def ops(self):
        return [c[0] for c in self.calls]


def desktop(*rows, elements=None):
    fake = FakeDesktop(rows, elements)
    return backend.Windows(fake), fake


NOTEPAD = row(1001, "notes.txt - Notepad", "notepad.exe")
NOTEPAD2 = row(1002, "todo.txt - Notepad", "notepad.exe")
EXPLORER_BAR = row(65806, "", "explorer.exe")
DOCS = row(4063882, "Documents", "explorer.exe")


class Grammar(unittest.TestCase):
    CASES = {
        'open notepad then type "hello" then press ctrl+s': [
            ("launch", {"program": "notepad.exe"}),
            ("keyboard", {"action": "type", "text": "hello"}),
            ("keyboard", {"action": "press", "keys": "CTRL+S"})],
        'focus the "Untitled - Notepad" window': [("window", {"action": "focus", "title": "Untitled - Notepad"})],
        'click the "Save" button in notepad': [
            ("control", {"action": "invoke", "name": "Save", "control_type": "Button", "app": "notepad.exe"})],
        'set the "File name:" field to "C:\\notes\\a.txt" then press enter': [
            ("control", {"action": "set_value", "name": "File name:", "control_type": "Edit", "text": "C:\\notes\\a.txt"}),
            ("keyboard", {"action": "press", "keys": "ENTER"})],
        'wait for the "Save As" window to close for up to 10 seconds': [
            ("wait", {"for": "window", "state": "closed", "ms": 10000, "title": "Save As"})],
        "list windows": [("window", {"action": "list"})],
        "show the controls in notepad": [("control", {"action": "inspect", "app": "notepad.exe"})],
        "inspect calculator": [("control", {"action": "inspect", "app": "ApplicationFrameHost.exe", "title": "Calculator"})],
        "find notepad": [("window", {"action": "find", "app": "notepad.exe"})],
        "find notepad windows": [("window", {"action": "find", "app": "notepad.exe"})],
        'type "7" in calc': [("keyboard", {"action": "type", "text": "7", "app": "ApplicationFrameHost.exe", "title": "Calculator"})],
        'set the "File name:" field to "a.txt" in notepad': [
            ("control", {"action": "set_value", "name": "File name:", "control_type": "Edit", "text": "a.txt", "app": "notepad.exe"})],
        "minimize notepad and close it": [("window", {"action": "minimize", "app": "notepad.exe"}), ("window", {"action": "close"})],
        "move notepad to 100, 200": [("window", {"action": "move", "x": 100, "y": 200, "app": "notepad.exe"})],
        "resize notepad to 800x600": [("window", {"action": "resize", "width": 800, "height": 600, "app": "notepad.exe"})],
        "click at 10, 20 in notepad": [("pointer", {"action": "click", "x": 10, "y": 20, "button": "left", "app": "notepad.exe"})],
        "right-click 40, 50": [("pointer", {"action": "click", "x": 40, "y": 50, "button": "right"})],
        "double-click 5, 6 in notepad": [("pointer", {"action": "double_click", "x": 5, "y": 6, "button": "left", "app": "notepad.exe"})],
        "scroll down 5 steps in notepad": [("scroll", {"direction": "down", "steps": 5, "app": "notepad.exe"})],
        "scroll up": [("scroll", {"direction": "up", "steps": 3})],
        "wait 2 seconds": [("wait", {"for": "time", "ms": 2000})],
        "pause for 250 ms": [("wait", {"for": "time", "ms": 250})],
        "wait until the Save button is enabled": [
            ("wait", {"for": "control", "state": "enabled", "name": "Save", "control_type": "Button", "ms": 10000})],
        'launch notepad with "C:\\notes\\a.txt"': [("launch", {"program": "notepad.exe", "document": "C:\\notes\\a.txt"})],
        'open "C:\\Tools\\viewer.exe"': [("launch", {"program": "C:\\Tools\\viewer.exe"})],
        "open calculator and press 7": [("launch", {"program": "calc.exe"}), ("keyboard", {"action": "press", "keys": "7"})],
        'please type "a" into notepad': [("keyboard", {"action": "type", "text": "a", "app": "notepad.exe"})],
        'type "x" into the search box in "Documents"': [
            ("control", {"action": "set_value", "name": "search", "control_type": "Edit", "text": "x", "title": "Documents"})],
        'click the control with automation id "saveButton" in notepad': [
            ("control", {"action": "invoke", "automation_id": "saveButton", "app": "notepad.exe"})],
        'focus the notepad window titled "todo" then press f5': [
            ("window", {"action": "focus", "app": "notepad.exe", "title": "todo"}), ("keyboard", {"action": "press", "keys": "F5"})],
    }

    def test_requests_parse_to_exact_actions(self):
        for request, expected in self.CASES.items():
            with self.subTest(request=request):
                actions = grammar.parse(request)
                self.assertIsNotNone(actions, request)
                self.assertEqual([(a["name"], a["arguments"]) for a in actions], expected)

    def test_conditions_negation_and_repetition_refuse_the_whole_request(self):
        for request in ("don't close notepad", "close notepad if it is open", "repeat press enter",
                        "press enter every second", "type the password into notepad"):
            with self.subTest(request=request), self.assertRaises(job.WindowsActionError):
                grammar.parse(request)

    def test_misses_are_none_with_a_reason_when_one_exists(self):
        self.assertIsNone(grammar.parse("type hello"))  # unquoted text is never typed
        self.assertIsNone(grammar.parse("open the frobnicator"))
        reasons = []
        self.assertIsNone(grammar.parse("open powershell.exe", reasons))
        self.assertIn("interpreters", reasons[0])

    def test_interpreters_and_options_are_never_launched(self):
        for program in ("cmd.exe", "POWERSHELL.EXE", "C:\\Windows\\System32\\wscript.exe", "rundll32.exe", "pwsh.exe",
                        "hh.exe", "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\POWERS~1.EXE",
                        "python3.exe", "python3.12.exe", "pythonw3.exe", "pwsh7.exe", "Rscript.exe"):
            with self.subTest(program=program), self.assertRaises(job.WindowsActionError):
                job.normalize([{"name": "launch", "arguments": {"program": program}}])
        with self.assertRaises(job.WindowsActionError):
            job.normalize([{"name": "launch", "arguments": {"program": "notepad", "document": "/c calc"}}])
        with self.assertRaises(job.WindowsActionError):
            job.normalize([{"name": "launch", "arguments": {"program": "notepad.exe && calc.exe"}}])
        # Explorer would run a document with its file association.
        for program in ("explorer", "explorer.exe", "C:\\Windows\\Explorer.EXE"):
            with self.subTest(program=program), self.assertRaises(job.WindowsActionError):
                job.normalize([{"name": "launch", "arguments": {"program": program, "document": "C:\\x\\a.bat"}}])
        job.normalize([{"name": "launch", "arguments": {"program": "explorer"}}])

    def test_key_chords_use_sdk_names(self):
        self.assertEqual(job.key_chord("shift+ctrl+s"), "CTRL+SHIFT+S")
        self.assertEqual(job.key_chord("Return"), "ENTER")
        self.assertEqual(job.key_chord("alt f4"), "ALT+F4")
        self.assertEqual(job.key_chord("page down"), "PAGEDOWN")
        for bad in ("ctrl", "f13", "ctrl+ctrl+a", "a+b", "menu"):
            with self.subTest(bad=bad), self.assertRaises(job.WindowsActionError):
                job.key_chord(bad)
        # Start, Run and search launch whatever is typed next.
        for launcher in ("win+r", "windows x", "super s", "ctrl+esc", "ctrl+shift+esc"):
            with self.subTest(launcher=launcher), self.assertRaises(job.LauncherChordError):
                job.key_chord(launcher)
        reasons = []
        self.assertIsNone(grammar.parse('press win+r then type "cmd" then press enter', reasons))
        self.assertIn("Windows-key", reasons[0])

    def test_an_app_alias_keeps_its_window_title(self):
        # A packaged app's window belongs to ApplicationFrameHost; the alias title keeps it Calculator.
        actions = job.normalize([{"name": "keyboard", "arguments": {"action": "type", "text": "x", "app": "calculator"}}])
        self.assertEqual(actions[0]["arguments"]["title"], "Calculator")
        self.assertEqual(job.validate('type "x" in calculator', [{"name": "keyboard", "arguments": {
            "action": "type", "text": "x", "app": "calculator"}}])[0]["arguments"]["app"], "ApplicationFrameHost.exe")
        settings = row(196696, "Settings", "ApplicationFrameHost.exe")
        win, _fake = desktop(settings)
        with self.assertRaises(backend.WindowsDesktopError) as caught:
            win.plan(actions, win.snapshot())
        self.assertEqual(caught.exception.code, "window_not_found")

    def test_eight_steps_at_most(self):
        with self.assertRaises(job.WindowsActionError):
            grammar.parse("; ".join(["press enter"] * 9))
        self.assertEqual(len(grammar.parse("; ".join(["press enter"] * 8))), 8)


class Grounding(unittest.TestCase):
    """A model proposal must be grounded in its clause before it runs."""

    def check(self, request, calls):
        return job.validate(request, calls)

    def test_grounded_proposal_is_admitted(self):
        calls = [{"name": "control", "arguments": {"action": "invoke", "name": "Save", "control_type": "Button", "app": "notepad.exe"}}]
        self.assertEqual(self.check('click the "Save" button in notepad', calls)[0]["arguments"]["name"], "Save")

    def test_invented_or_changed_values_are_refused(self):
        bad = {
            'type "hello" in notepad': [{"name": "keyboard", "arguments": {"action": "type", "text": "hello!", "app": "notepad.exe"}}],
            "close notepad": [{"name": "window", "arguments": {"action": "close", "app": "calc.exe"}}],
            "move notepad to 100, 200": [{"name": "window", "arguments": {"action": "move", "x": 200, "y": 100, "app": "notepad.exe"}}],
            "focus notepad": [{"name": "window", "arguments": {"action": "close", "app": "notepad.exe"}}],
            "press ctrl+s": [{"name": "keyboard", "arguments": {"action": "press", "keys": "ctrl+shift+s"}}],
            'focus "A" then press enter': [{"name": "window", "arguments": {"action": "focus", "title": "A"}}],
            'click the "Save" button': [{"name": "control", "arguments": {"action": "invoke", "name": "Delete", "control_type": "Button"}}],
            "wait 2 seconds": [{"name": "wait", "arguments": {"for": "time", "ms": 3000}}],
            "open notepad": [{"name": "launch", "arguments": {"program": "mspaint.exe"}}],
            'focus "A" and "B"': [{"name": "window", "arguments": {"action": "focus", "title": "A"}}],
        }
        for request, calls in bad.items():
            with self.subTest(request=request), self.assertRaises(job.WindowsActionError):
                self.check(request, calls)

    def test_schema_bounds_and_unknown_fields(self):
        for calls in ([{"name": "window", "arguments": {"action": "focus", "shell": "x"}}],
                      [{"name": "pointer", "arguments": {"action": "click", "x": -1, "y": 2}}],
                      [{"name": "scroll", "arguments": {"direction": "down", "steps": 11}}],
                      [{"name": "wait", "arguments": {"for": "time", "ms": 10001}}],
                      [{"name": "control", "arguments": {"action": "invoke"}}],
                      [{"name": "control", "arguments": {"action": "find", "control_type": "Spaceship"}}],
                      [{"name": "keyboard", "arguments": {"action": "type", "text": "a\nb"}}],
                      [{"name": "window", "arguments": {"action": "list", "app": "notepad.exe"}}]):
            with self.subTest(calls=calls), self.assertRaises(job.WindowsActionError):
                job.normalize(calls)


class Planning(unittest.TestCase):
    def test_input_gets_an_explicit_focus_step_once(self):
        win, fake = desktop(NOTEPAD)
        actions = grammar.parse('type "a" in notepad then press enter then click at 5, 5')
        plan = win.plan(actions, win.snapshot())
        self.assertEqual([(p["name"], p["arguments"].get("action"), p.get("implied", False)) for p in plan],
                         [("window", "focus", True), ("keyboard", "type", False), ("keyboard", "press", False),
                          ("pointer", "click", False)])
        self.assertEqual(plan[1]["window"]["window"]["title"], "notes.txt - Notepad")

    def test_ambiguous_and_missing_windows_are_refused_with_candidates(self):
        win, _ = desktop(NOTEPAD, NOTEPAD2)
        with self.assertRaises(backend.WindowsDesktopError) as caught:
            win.plan(grammar.parse("close notepad"), win.snapshot())
        self.assertEqual(caught.exception.code, "ambiguous_target")
        self.assertIn("todo.txt - Notepad", str(caught.exception))
        plan = win.plan(grammar.parse('close the notepad window titled "todo"'), win.snapshot())
        self.assertEqual(plan[0]["window"]["identity"]["hwnd"], "1002")
        with self.assertRaises(backend.WindowsDesktopError) as caught:
            win.plan(grammar.parse("close paint"), win.snapshot())
        self.assertEqual(caught.exception.code, "window_not_found")

    def test_untitled_shell_windows_are_not_addressable_by_app(self):
        win, _ = desktop(EXPLORER_BAR, DOCS)
        plan = win.plan(grammar.parse("focus explorer"), win.snapshot())
        self.assertEqual(plan[0]["window"]["window"]["title"], "Documents")

    def test_unnamed_input_needs_a_window(self):
        win, _ = desktop(NOTEPAD)
        with self.assertRaises(backend.WindowsDesktopError):
            win.plan(grammar.parse('type "x"'), win.snapshot())

    def test_a_closed_window_is_not_reused(self):
        win, _ = desktop(NOTEPAD)
        with self.assertRaises(backend.WindowsDesktopError):
            win.plan(grammar.parse('close notepad then type "x"'), win.snapshot())

    def test_launch_defers_the_window_until_it_exists(self):
        win, fake = desktop()
        plan = win.plan(grammar.parse('open notepad then type "hi"'), win.snapshot())
        self.assertEqual(plan[2]["window"]["deferred"], "resolved after launch")
        self.assertEqual(fake.ops(), ["status", "windows"])

    def test_shell_and_launcher_windows_get_no_input(self):
        kilix = row(1246354, "Kilix", "kitty.exe")
        run = row(5005, "Run", "explorer.exe")
        for request, rows in (('type "x" in "Kilix"', [kilix]), ('click "OK" in "Run"', [run]),
                              ('type "x" into the search box in "Documents"', [DOCS]),
                              ('open explorer then type "cmd" then press enter', []),
                              ('scroll down in "Kilix"', [kilix])):
            with self.subTest(request=request):
                win, fake = desktop(*rows)
                with self.assertRaises(backend.WindowsDesktopError) as caught:
                    win.plan(grammar.parse(request), win.snapshot())
                self.assertEqual(caught.exception.code, "shell_window")
        # Managing those windows is still allowed.
        win, fake = desktop(kilix)
        win.plan(grammar.parse('minimize "Kilix"'), win.snapshot())


class Execution(unittest.TestCase):
    def run_request(self, request, fake_rows=(), elements=None, execute=True):
        fake = FakeDesktop(fake_rows, elements)
        record = windows_cli.run(request, execute=execute, backend=backend.Windows(fake))
        return record, fake

    def test_open_type_save_sends_exact_sdk_requests(self):
        record, fake = self.run_request('open notepad then type "hello" then press ctrl+s')
        self.assertEqual(record["status"], 0, record)
        self.assertEqual(fake.ops(), ["status", "windows", "launch", "focus", "type", "key"])
        launched = fake.calls[2]
        self.assertEqual(launched[1], {"argv": ["notepad.exe"]})
        target = fake.calls[4][2]
        self.assertEqual(fake.calls[3][2], target)
        self.assertEqual(fake.calls[4][1], {"text": "hello"})
        self.assertEqual(fake.calls[5][1], {"keys": ["CTRL", "S"]})
        self.assertTrue(all(r["application_completion_verified"] is False for r in record["results"]))
        self.assertTrue(record["results"][1]["implied"])

    def test_preview_sends_no_input(self):
        record, fake = self.run_request('type "x" in notepad', [NOTEPAD], execute=False)
        self.assertEqual(record["outcome"], "planned")
        self.assertEqual(fake.ops(), ["status", "windows"])

    def test_read_only_requests_run_without_consent(self):
        record, fake = self.run_request("show the controls in notepad", [NOTEPAD], execute=False)
        self.assertEqual(record["outcome"], "observed")
        self.assertEqual(fake.ops(), ["status", "windows", "inspect"])

    def test_control_names_resolve_case_insensitively_only_when_unique(self):
        elements = {"1001": [{"id": "1", "name": "Save", "control_type": "Button"},
                             {"id": "2", "name": "Cancel", "control_type": "Button"}]}
        record, fake = self.run_request("click the save button in notepad", [NOTEPAD], elements)
        self.assertEqual(record["status"], 0, record)
        self.assertEqual(fake.calls[-1][0], "invoke")
        self.assertEqual(fake.calls[-1][1]["selector"], {"name": "Save", "control_type": "Button"})
        self.assertEqual(record["results"][0]["result"], {"resolved_name": "Save"})
        elements["1001"].append({"id": "3", "name": "SAVE", "control_type": "Button"})
        record, fake = self.run_request("click the save button in notepad", [NOTEPAD], elements)
        self.assertEqual(record["results"][0]["error"]["code"], "ambiguous_element")
        self.assertNotIn("invoke", fake.ops())

    def test_set_value_uses_the_exact_selector_and_text(self):
        elements = {"1001": [{"id": "1", "name": "File name:", "control_type": "Edit"}]}
        record, fake = self.run_request('set the "File name:" field in notepad to "a.txt"', [NOTEPAD], elements)
        self.assertEqual(record["status"], 0, record)
        self.assertEqual(fake.calls[-1][:2], ("set-value", {"selector": {"name": "File name:", "control_type": "Edit"},
                                                            "depth": 32, "limit": 20000, "text": "a.txt"}))

    def test_first_failure_stops_and_is_classified(self):
        record, fake = self.run_request('type "x" in notepad then press enter', [NOTEPAD])
        self.assertEqual(record["status"], 0)
        fake = FakeDesktop([NOTEPAD])
        fake.fail["type"] = "foreground_mismatch"
        record = windows_cli.run('type "x" in notepad then press enter', execute=True, backend=backend.Windows(fake))
        self.assertEqual(record["outcome"], "partial")
        self.assertEqual(record["results"][-1]["status"], "blocked")
        self.assertNotIn("key", fake.ops())
        fake = FakeDesktop([NOTEPAD])
        fake.fail["type"] = "timeout"
        record = windows_cli.run('type "x" in notepad', execute=True, backend=backend.Windows(fake))
        self.assertEqual(record["results"][-1]["status"], "uncertain")
        self.assertEqual(record["outcome"], "uncertain")  # never "blocked" when input may have landed
        fake = FakeDesktop([NOTEPAD])
        fake.fail["close"] = "timeout"
        record = windows_cli.run('close notepad', execute=True, backend=backend.Windows(fake))
        self.assertEqual((record["outcome"], record["results"][0]["status"]), ("uncertain", "uncertain"))
        for code in ("elevated_target", "stale_target", "window_moved", "password_element"):  # refused before input
            fake = FakeDesktop([NOTEPAD])
            fake.fail["type"] = code
            record = windows_cli.run('type "x" in notepad', execute=True, backend=backend.Windows(fake))
            self.assertEqual(record["results"][-1]["status"], "blocked", code)

    def test_move_keeps_size_and_resize_keeps_position(self):
        record, fake = self.run_request("move notepad to 100, 200", [row(1001, "n - Notepad", "notepad.exe")])
        self.assertEqual(fake.calls[-1][1], {"x": 100, "y": 200, "width": 800, "height": 600})
        record, fake = self.run_request("resize notepad to 640x480", [row(1001, "n - Notepad", "notepad.exe")])
        self.assertEqual(fake.calls[-1][1], {"x": 10, "y": 20, "width": 640, "height": 480})

    def test_scroll_defaults_to_the_window_center(self):
        record, fake = self.run_request("scroll down 2 steps in notepad", [NOTEPAD])
        self.assertEqual(fake.calls[-1][:2], ("scroll", {"x": 400, "y": 300, "delta": -240}))

    def test_a_named_app_after_launch_is_the_launched_window(self):
        record, fake = self.run_request('open notepad then type "secret" in notepad', [NOTEPAD])
        self.assertEqual(record["status"], 0, record)
        typed = [c for c in fake.calls if c[0] == "type"][0]
        self.assertEqual(typed[2]["hwnd"], "900000")  # not the older notes.txt window 1001
        # A title still names the older window explicitly.
        record, fake = self.run_request('open notepad then type "x" in "notes.txt - Notepad"', [NOTEPAD])
        self.assertEqual([c for c in fake.calls if c[0] == "type"][0][2]["hwnd"], "1001")

    def test_a_wait_deferred_shell_window_is_refused_when_it_appears(self):
        fake = FakeDesktop([NOTEPAD])
        original = fake.__call__

        def calls(operation, params=None, **kw):
            if operation == "wait" and params.get("title") == "Open":
                fake.rows.append(row(6006, "Open", "explorer.exe"))  # a shell-hosted window
            return original(operation, params, **kw)
        record = windows_cli.run('wait for the "Open" window to appear then type "x" in it', execute=True,
                                 backend=backend.Windows(calls))
        self.assertEqual(record["results"][-1]["status"], "blocked", record)
        self.assertEqual(record["results"][-1]["error"]["code"], "shell_window")
        self.assertNotIn("type", fake.ops())

    def test_calculator_window_is_found_after_launch(self):
        record, fake = self.run_request("open calculator then press 7")
        self.assertEqual(record["status"], 0, record)
        self.assertEqual(fake.ops(), ["status", "windows", "launch", "find", "focus", "key"])
        self.assertEqual(fake.calls[3][1]["app"], "ApplicationFrameHost.exe")

    def test_dialog_after_close_is_bound_through_a_wait(self):
        dialog = row(2002, "Notepad", "notepad.exe", pid=1001 % 60000 + 1)
        elements = {"2002": [{"id": "9", "name": "Don't Save", "control_type": "Button"}]}
        fake = FakeDesktop([NOTEPAD], elements)
        original = fake.__call__

        def calls(operation, params=None, **kw):
            if operation == "close" and dialog not in fake.rows:
                fake.calls.append((operation, params, kw.get("target")))
                fake.rows.append(dialog)  # The dirty document prompts instead of closing.
                return {"target": kw.get("target")}
            return original(operation, params, **kw)
        win = backend.Windows(calls)
        request = 'close notepad then wait for the "Notepad" window to appear then click the "Don\'t Save" button in it'
        record = windows_cli.run(request, execute=True, backend=win)
        self.assertEqual(record["status"], 0, record)
        self.assertEqual(fake.ops(), ["status", "windows", "close", "find", "find", "invoke"])
        self.assertEqual(fake.calls[-1][2]["hwnd"], "2002")  # the exact title beat "notes.txt - Notepad"

    def test_wait_for_window_uses_filters_and_a_long_enough_timeout(self):
        dialog = row(3003, "Save As", "notepad.exe")
        record, fake = self.run_request('wait for the "Save As" window to close for up to 30 seconds', [NOTEPAD, dialog])
        self.assertEqual(record["status"], 0, record)
        op, params, target = fake.calls[-1]
        self.assertEqual((op, params["state"], target["hwnd"]), ("wait", "closed", "3003"))
        self.assertGreater(params["wait_ms"], 29000)
        record, fake = self.run_request('wait for the "Save As" window to appear for up to 5 seconds', [NOTEPAD])
        self.assertEqual(fake.calls[-2][:2], ("wait", {"state": "present", "wait_ms": fake.calls[-2][1]["wait_ms"],
                                                       "poll_ms": 100, "title": "Save As"}))
        self.assertEqual(record["results"][-1]["error"]["code"], "wait_timeout")  # the fake never shows it
        self.assertEqual(record["results"][-1]["status"], "failed")

    def test_parse_only_connects_to_nothing(self):
        record = windows_cli.run("close notepad", parse_only=True, backend=object())
        self.assertEqual(record["outcome"], "proposed")

    def test_grammar_mode_reports_the_refusal_reason(self):
        record = windows_cli.run("open cmd.exe", parse_only=True)
        self.assertIn("interpreters", record["note"])
        record = windows_cli.run("open the frobnicator", parse_only=True)
        self.assertIn("grammar", record["note"])


class Transport(unittest.TestCase):
    def fake_run(self, reply, code=0):
        calls = []

        def run(argv, input=None, **kwargs):
            calls.append((argv, json.loads(input)))
            return subprocess.CompletedProcess(argv, code, json.dumps(reply).encode(), b"")
        return run, calls

    def test_host_and_wsl_command_lines(self):
        run, calls = self.fake_run({"schema": "kilix.desktop/v1", "ok": True, "operation": "status", "result": {}})
        t = backend.Transport(host="thinktop-windows", kilix="/usr/bin/kilix", run=run)
        t("status")
        self.assertEqual(calls[0][0], ["/usr/bin/kilix", "windows", "--host", "thinktop-windows", "--json", "--request-json", "-"])
        self.assertEqual(calls[0][1]["schema"], "kilix.desktop/v1")
        with mock.patch.object(backend, "in_wsl", return_value=True):
            t = backend.Transport(kilix="/k", run=run)
        t("status")
        self.assertEqual(calls[1][0], ["/k", "desktop", "--json", "--request-json", "-"])
        with mock.patch.object(backend, "in_wsl", return_value=False), self.assertRaises(backend.WindowsDesktopError):
            backend.Transport(kilix="/k", run=run)
        for host in ("-oProxyCommand=x", "a b", "a;b", ""):
            with self.subTest(host=host), self.assertRaises(backend.WindowsDesktopError):
                backend.Transport(host=host, kilix="/k", run=run)

    def test_errors_and_malformed_replies(self):
        run, _ = self.fake_run({"schema": "kilix.desktop/v1", "ok": False, "operation": "focus",
                                "error": {"code": "foreground_mismatch", "message": "no"}}, 1)
        with self.assertRaises(backend.WindowsDesktopError) as caught:
            backend.Transport(host="h", kilix="/k", run=run)("focus")
        self.assertEqual(caught.exception.code, "foreground_mismatch")
        run, _ = self.fake_run({"schema": "other", "ok": True, "operation": "focus"})
        with self.assertRaises(backend.WindowsDesktopError) as caught:
            backend.Transport(host="h", kilix="/k", run=run)("focus")
        self.assertEqual(caught.exception.code, "transport_error")

        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)
        with self.assertRaises(backend.WindowsDesktopError) as caught:
            backend.Transport(host="h", kilix="/k", run=timeout)("type")
        self.assertEqual(caught.exception.code, "timeout")


class Surfaces(unittest.TestCase):
    def test_mcp_menu_is_opt_in_and_act_needs_consent(self):
        all_names = {t["name"] for t in mcp_server.Server(lambda *a: None)._tools}
        self.assertFalse(any(n.startswith("kilix_windows") for n in all_names))
        server = mcp_server.Server(lambda *a: None, tools="windows", windows_host="h")
        self.assertEqual({t["name"] for t in server._tools}, {"kilix_windows_read", "kilix_windows_plan", "kilix_windows_act"})
        reply = server.call_tool("kilix_windows_act", {"request": "close notepad", "confirm_risky": False})
        self.assertTrue(reply["isError"])
        self.assertEqual(reply["structuredContent"]["outcome"], "blocked")
        with self.assertRaises(ValueError):
            server.call_tool("kilix_windows_read", {"host": "elsewhere"})

    def test_mcp_routes_to_the_configured_host(self):
        server = mcp_server.Server(lambda *a: None, tools="windows", windows_host="thinktop-windows")
        with mock.patch.object(windows_cli, "run", return_value={"status": 0}) as run:
            server.call_tool("kilix_windows_plan", {"request": "close notepad"})
        self.assertEqual(run.call_args.kwargs["host"], "thinktop-windows")
        self.assertFalse(run.call_args.kwargs["execute"])

    def test_cli_parse_only_and_schema(self):
        out = io.StringIO()
        with redirect_stdout(out):
            status = windows_cli.main(["--parse-only", "--json", "close", "notepad"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())["actions"][0]["arguments"], {"action": "close", "app": "notepad.exe"})
        out = io.StringIO()
        with redirect_stdout(out):
            windows_cli.main(["--schema"])
        self.assertEqual(json.loads(out.getvalue())["schema"], job.VERSION)


if __name__ == "__main__":
    unittest.main()
