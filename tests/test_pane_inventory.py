"""Inventory requests never load a model or execute pane mutations."""
import unittest
from unittest.mock import patch

from support import FakeKilix, desktop
import actions
import kilix
import mcp_server
import router


class Inventory(unittest.TestCase):
    def test_inventory_is_claimed_by_the_routed_panes_job(self):
        for request in ('panes', 'tabs', 'list panes', 'show me all tabs',
                        'Please list Kilix panes and tabs.'):
            with self.subTest(request=request), FakeKilix(desktop()) as fake:
                self.assertTrue(router._claims_panes(request))
                server = mcp_server.Server(lambda *args: self.fail('inventory loaded the model'))
                result = server.call_tool('kilix_ask_plan', {'request': request})
                record = result['structuredContent']
                self.assertEqual(record['status'], 0, record)
                self.assertEqual(record['route']['job'], 'panes')
                self.assertTrue(record['items'])
                # Routing also reads the app catalog; it must never send input.
                self.assertEqual(fake.calls(), [(['list'], None)])

    def test_inventory_bypasses_model_and_mutations(self):
        for tool in ("kilix_act", "kilix_plan"):
            for request in ("list panes", "list tabs", "show me all panes",
                            "Please list Kilix panes and tabs.", "get existing tabs",
                            "panes", "tabs", "Kilix panes", "PANES?", "all panes",
                            "panes and tabs", "tabs and panes", "current panes",
                            "the existing tabs please", "list open panes"):
                with self.subTest(tool=tool, request=request), FakeKilix(desktop()) as fake:
                    def forbidden(*args):
                        self.fail("inventory must not load the model")
                    server = mcp_server.Server(forbidden)
                    result = server.call_tool(tool, {"request": request})
                    self.assertFalse(result["isError"])
                    items = result["structuredContent"]["items"]
                    self.assertTrue(items)
                    self.assertTrue(all(i["kind"].startswith("list_") for i in items))
                    self.assertEqual(fake.calls(), [])
                    expected = desktop()[0]["tabs"]
                    if "panes" in request.lower():
                        self.assertEqual({i["pane_id"] for i in items if "pane_id" in i},
                                         {w["id"] for t in expected for w in t["windows"]})

    def test_snapshot_failure_cannot_fall_back_to_model(self):
        for request in ("list panes", "panes", "tabs"):
            with self.subTest(request=request), patch(
                    "kilix.snapshot", side_effect=kilix.KilixError("unavailable")):
                server = mcp_server.Server(lambda: self.fail("model fallback"))
                result = server.call_tool("kilix_act", {"request": request})
                self.assertEqual(result["structuredContent"]["status"], 1)
                self.assertEqual(result["structuredContent"]["note"], "unavailable")

    def test_inspection_cannot_admit_model_creation(self):
        for request in ("list panes", "show tabs", "inspect the panes", "find pane 128",
                        "panes", "tabs", "Kilix panes", "panes and tabs"):
            for kind in ("open_pane", "open_tab"):
                with self.subTest(request=request, kind=kind):
                    result = actions.interpret(request, [{"name": kind, "arguments": {}}])
                    self.assertTrue(result)
                    self.assertTrue(all(isinstance(item, actions.Refusal) for item in result))

    def test_explicit_creation_still_admitted(self):
        for request, kind in (("open a pane", "open_pane"), ("open a tab", "open_tab"),
                              ("open panes", "open_pane"), ("open tabs", "open_tab")):
            result = actions.interpret(request, [{"name": kind, "arguments": {}}])
            self.assertEqual(result, [actions.Action(kind, {})])

    def test_inventory_cannot_swallow_creation_or_compound_requests(self):
        for request in ("open panes", "open tabs", "new tabs", "create panes",
                        "list panes and open a tab", "show panes then close pane 128"):
            with self.subTest(request=request):
                self.assertIsNone(actions.inventory_unit(request))
