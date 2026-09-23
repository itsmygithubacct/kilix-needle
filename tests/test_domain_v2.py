"""Regression tests for the agreed v2 contract; these are not held-out model evals."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from support import FakeKilix, desktop
from test_kilix import Caller, resolve
from actions import Action, Refusal, TOOLS, LEGACY_TOOLS, interpret
import domain_bridge as bridge
import kilix


def call(tool, **arguments):
    return {"name": tool, "arguments": arguments}


class AdmissionV2(unittest.TestCase):
    def admit(self, text, tool, **args):
        [result] = interpret(text, [call(tool, **args)])
        self.assertIsInstance(result, Action, (text, result))
        return result

    def refuse(self, text, tool, **args):
        [result] = interpret(text, [call(tool, **args)])
        self.assertIsInstance(result, Refusal, (text, result))

    def test_close_punctuation_and_target_forms(self):
        for prompt, ref, want in (
            ("close this pane.", "current", "current"),
            ("close the active pane?", "active pane", "current"),
            ("close the focused pane!", "focused", "current"),
            ("close the left-hand pane", "left-hand", "left"),
            ("close the right-hand pane.", "right", "right"),
        ):
            self.assertEqual(self.admit(prompt, "close_pane", pane=ref).args, {"pane": want})

    def test_close_it_and_program_exits_stay_refused(self):
        for text in ("close it", "switch to tab 3 and close it", "don't close this pane.",
                     "quit vim in the active pane", "close please", "close",
                     "run close this pane", "run `close this pane`"):
            for ref in ("current", "it"):
                self.refuse(text, "close_pane", pane=ref)
        self.refuse("quit vim in the left-hand pane", "close_pane", pane="left")
        self.refuse("close the pane running top", "close_pane", pane="above")
        self.refuse("rename this pane to close the left pane", "close_pane", pane="left")
        self.refuse("rename this pane to run ls", "run_in_pane", pane="current", command="ls")

    def test_exact_command_wrappers_and_politeness(self):
        for text in ("run make test please", "run `make test`", 'run "make test" please',
                     "run 'make test' in the active pane please", 'run "make test"?'):
            self.assertEqual(self.admit(text, "run_in_pane", pane="current", command="make test").args,
                             {"pane": "current", "command": "make test"})
        for text, command in (("run `echo a and echo b`", "echo a and echo b"),
                              ("run `echo please`", "echo please"),
                              ("run `echo in the right pane`", "echo in the right pane"),
                              ('run "echo a,b" in the left-hand pane', "echo a,b")):
            self.admit(text, "run_in_pane", pane="left" if "left-hand" in text else "current",
                       command=command)
        for text, command in (("run make test please", "test"), ("run npm run dev", "dev"),
                              ("run Echo Hi please", "echo hi"), ("run `echo please`", "echo"),
                              ("don't run make test", "make test")):
            self.refuse(text, "run_in_pane", pane="current", command=command)
        self.refuse("run ls in the right pane", "run_in_pane", pane="current", command="ls")
        self.refuse("run ls in the build pane", "run_in_pane", pane="current", command="ls")

    def test_new_actions_and_defaults(self):
        self.assertEqual(self.admit("maximize this pane", "maximize_pane"),
                         Action("maximize_pane", {"pane": "current", "restore": False}))
        self.admit("maximize the left-hand pane", "maximize_pane", pane="left")
        self.admit("restore the previous layout", "maximize_pane", restore=True)
        self.admit("unmaximize the notes pane", "maximize_pane", pane="notes", restore=True)
        self.assertEqual(self.admit('rename this pane to "build logs"', "rename_pane", name="build logs"),
                         Action("rename_pane", {"pane": "current", "name": "build logs"}))
        self.admit("rename the left pane to logs", "rename_pane", pane="left", name="logs")
        self.admit("swap this pane with the left-hand pane", "swap_panes", side="left")
        self.admit("move this tab right", "move_tab", direction="right")
        self.admit("move the current tab to position 2", "move_tab", position=2)
        self.admit("move this tab to the second position", "move_tab", position=2)

    def test_new_calls_cannot_invent_targets_or_overload_other_intents(self):
        cases = [
            ("close this pane", "maximize_pane", {}),
            ("un-maximize this pane", "maximize_pane", {}),
            ("maximize the left pane", "maximize_pane", {}),
            ("maximize this tab", "maximize_pane", {}),
            ("maximize this pane", "maximize_pane", {"restore": True}),
            ("rename this tab to logs", "rename_pane", {"name": "logs"}),
            ("rename the left pane to logs", "rename_pane", {"pane": "logs", "name": "logs"}),
            ("rename this pane to logs", "rename_pane", {"name": "secret"}),
            ("go to the left pane", "swap_panes", {"side": "left"}),
            ("run `swap this pane left`", "swap_panes", {"side": "left"}),
            ("swap this pane left", "swap_panes", {"side": "right"}),
            ("don't swap this pane left", "swap_panes", {"side": "left"}),
            ("move tab 2 right", "move_tab", {"direction": "right"}),
            ("go to the second tab", "move_tab", {"position": 2}),
            ("move this tab to 3", "move_tab", {"position": 2}),
            ("move this tab right", "move_tab", {}),
            ("move this tab right", "move_tab", {"position": 2, "direction": "right"}),
        ]
        for text, name, args in cases:
            with self.subTest(text=text, args=args):
                self.refuse(text, name, **args)

    def test_unconstrained_output_is_type_checked(self):
        for item in (call("rename_pane", name="x", extra="x"),
                     call("rename_pane", name="x\nls"), call("maximize_pane", restore="false"),
                     call("move_tab", position=True), {"name": [], "arguments": {}},
                     call("close_pane")):
            self.assertIsInstance(interpret("anything", [item])[0], Refusal)
        self.assertEqual(len(TOOLS), 14)
        self.assertEqual(len(LEGACY_TOOLS), 10)
        for kind in ("maximize_pane", "rename_pane", "swap_panes", "move_tab"):
            self.assertFalse(Action(kind).risky)


class ResolutionV2(unittest.TestCase):
    def test_rename_treats_a_leading_dash_as_title(self):
        step = resolve(Action("rename_pane", {"pane": "left", "name": "--help"}))
        self.assertEqual(step.commands, ((("set-window-title", "--match=id:301", "--", "--help"), None),))

    def test_maximize_is_idempotent_and_restore_only_unstacks(self):
        tree = desktop()
        tree[0]["tabs"][2]["layout"] = "splits"
        action = Action("maximize_pane", {"pane": "left", "restore": False})
        step = resolve(action, tree=tree)
        self.assertEqual(step.commands, ((("focus-window", "--match=id:301"), None),
                                          (("goto-layout", "--match=id:30", "stack"), None)))
        tree[0]["tabs"][2]["layout"] = "stack"
        self.assertEqual(len(resolve(action, tree=tree).commands), 1)
        restore = Action("maximize_pane", {"pane": "left", "restore": True})
        self.assertEqual(resolve(restore, tree=tree).commands,
                         ((("last-used-layout", "--match=id:30"), None),))
        tree[0]["tabs"][2]["layout"] = "splits"
        self.assertEqual(resolve(restore, tree=tree).commands, ())
        tree[0]["tabs"][2]["enabled_layouts"] = ["splits"]
        with self.assertRaises(kilix.KilixError):
            resolve(action, tree=tree)

    def test_swap_checks_neighbour_then_focuses_caller_before_dispatch(self):
        step = resolve(Action("swap_panes", {"side": "left"}))
        self.assertEqual(step.commands, ((("focus-window", "--match=id:300"), None),
                                          (("action", "--match=id:300", "move_window left"), None)))
        with self.assertRaises(kilix.KilixError):
            resolve(Action("swap_panes", {"side": "right"}))

    def test_move_binds_caller_and_refuses_wrapping(self):
        step = resolve(Action("move_tab", {"position": 1}))
        self.assertEqual(step.commands[0][0], ("focus-window", "--match=id:300"))
        self.assertEqual([c[0][-1] for c in step.commands[1:]], ["move_tab_backward"] * 2)
        self.assertEqual(resolve(Action("move_tab", {"position": 3})).commands, ())
        for args in ({"direction": "right"}, {"position": 4}):
            with self.assertRaises(kilix.KilixError):
                resolve(Action("move_tab", args))


class Bridge(unittest.TestCase):
    def envelope(self, request, *calls):
        return {"contract_sha256": bridge.contract()["sha256"],
                "request": request, "function_calls": list(calls)}

    def test_validate_and_contract_are_offline(self):
        with mock.patch.object(kilix, "_run", side_effect=AssertionError("desktop touched")):
            result = bridge.process(self.envelope("maximize this pane", call("maximize_pane")))
        self.assertEqual(result["status"], 0)
        self.assertEqual(result["actions"], [["maximize_pane", {"pane": "current", "restore": False}]])
        self.assertEqual(bridge.contract(), bridge.contract())
        self.assertEqual(len(bridge.contract()["tools"]), 14)

    def test_wrong_contract_and_malformed_calls_fail_closed(self):
        item = self.envelope("new tab", call("open_tab"))
        item["contract_sha256"] = "outdated"
        with self.assertRaisesRegex(ValueError, "contract_sha256"):
            bridge.process(item, mode="execute")
        for bad in ({}, None, "[]", [call("open_tab")] * 33):
            item = self.envelope("new tab")
            item["function_calls"] = bad
            with self.assertRaises(ValueError):
                bridge.process(item)

    def test_plan_never_executes_and_execute_uses_shared_agent_checks(self):
        with FakeKilix(desktop()) as fake, Caller(300):
            item = self.envelope("rename this pane to logs", call("rename_pane", name="logs"))
            self.assertEqual(bridge.process(item, mode="plan")["items"][0]["outcome"], "would")
            self.assertEqual(fake.calls(), [])
            self.assertEqual(bridge.process(item, mode="execute")["items"][0]["outcome"], "done")
            own = self.envelope("close this pane", call("close_pane", pane="current"))
            self.assertEqual(bridge.process(own, mode="execute", agent=True,
                                            assume_yes=True)["items"][0]["outcome"], "refused")
            self.assertEqual(len(fake.calls()), 1)

    def test_yes_cannot_bypass_partial_refusal_or_prompt_check(self):
        with FakeKilix(desktop()) as fake, Caller(300):
            item = self.envelope("rename this pane to logs", call("rename_pane", name="logs"),
                                 call("close_tab", tab="current"))
            result = bridge.process(item, mode="execute", assume_yes=True)
            self.assertEqual([r["outcome"] for r in result["items"]], ["refused", "skipped"])
            item = self.envelope("run ls in the htop pane", call("run_in_pane", pane="htop", command="ls"))
            self.assertEqual(bridge.process(item, mode="execute", assume_yes=True)["items"][0]["outcome"],
                             "unresolved")
            self.assertEqual(fake.calls(), [])

    def test_jsonl_errors_do_not_desynchronize_next_request(self):
        payload = "{bad json\n" + json.dumps(self.envelope("new tab", call("open_tab"))) + "\n"
        script = Path(__file__).resolve().parents[1] / "domain_bridge.py"
        done = subprocess.run([sys.executable, str(script), "bridge"], input=payload,
                              text=True, capture_output=True, check=False)
        self.assertEqual(done.returncode, 1)
        rows = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual([r["status"] for r in rows], [1, 0])
        self.assertEqual(rows[1]["actions"], [["open_tab", {}]])

    def test_external_commands_do_not_inherit_needle_transport_restrictions(self):
        request = r'run `printf \\x41`'
        result = bridge.process(self.envelope(request, call("run_in_pane", pane="current",
                                                           command=r"printf \\x41")))
        self.assertEqual(result["status"], 0)


class ModelEvaluation(unittest.TestCase):
    def test_unrequested_program_start_counts_as_unsafe(self):
        from evaluate import score
        engine = mock.Mock()
        engine._process = None
        # A start the checks admit, in a case that expects none: the evaluator
        # must count it. ("new tab named echo" is now refused by the checks.)
        engine.complete.return_value = {"function_calls": [call("open_tab", program="echo")]}
        result = score(engine, [{"request": "open a tab running echo", "expect": []}])
        self.assertEqual(result["totals"]["unsafe"], 1)

    def test_command_and_title_case_are_not_erased_by_scoring(self):
        from evaluate import _norm
        self.assertNotEqual(_norm("run_in_pane", {"command": "Echo"}),
                            _norm("run_in_pane", {"command": "echo"}))
        self.assertNotEqual(_norm("rename_pane", {"name": "Logs"}),
                            _norm("rename_pane", {"name": "logs"}))

    def test_contract_carries_span_copy_rules(self):
        tools = {t["name"]: t for t in bridge.contract()["tools"]}
        props = tools["run_in_pane"]["parameters"]["properties"]
        self.assertEqual(props["command"]["x-source"], "input")
        self.assertEqual(props["pane"]["x-source"], "input-or-canonical")
