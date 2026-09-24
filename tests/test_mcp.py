"""The MCP stdio server: protocol shape, tools, and agent-mode behaviour."""
import io
import json
import os
import unittest

from support import FakeKilix, desktop
import mcp_server
import needle_cli


class FakeImage:
    fd, path = -1, "/nonexistent"

    def close(self):
        pass


class ScriptedEngine:
    def __init__(self, calls):
        self.calls = calls
        self.closed = False

    def start(self):
        pass

    def reset(self):
        pass

    def complete(self, _text):
        return {"function_calls": self.calls}

    def close(self):
        self.closed = True


def converse(messages, calls=(), fail=None):
    """Feed newline-delimited messages to serve(); return the replies."""
    engine = ScriptedEngine(list(calls))

    def factory():
        if fail:
            raise mcp_server.asset.AssetError(fail)
        return needle_cli.Runtime(engine, [FakeImage()])
    out = io.StringIO()
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages) + "not json\n")
    mcp_server.serve(factory, stdin=stdin, stdout=out)
    return [json.loads(line) for line in out.getvalue().splitlines()], engine


def call(ident, name, **arguments):
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


class Protocol(unittest.TestCase):
    def test_initialize_list_and_notifications(self):
        replies, _ = converse([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "nope"}])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual([t["name"] for t in replies[1]["result"]["tools"]],
                         ["kilix_plan", "kilix_act"])
        self.assertEqual(replies[2]["error"]["code"], -32601)
        self.assertEqual(replies[3]["error"]["code"], -32700)   # the "not json" line
        self.assertEqual(len(replies), 4)                       # the notification got no reply

    def test_unknown_protocol_gets_the_default(self):
        replies, _ = converse([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "1999-01-01"}}])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")

    def test_bad_arguments_are_invalid_params(self):
        replies, _ = converse([call(1, "kilix_act", request=5),
                               call(2, "kilix_act", request="x", confirm_risky="yes"),
                               call(3, "rm_rf")])
        self.assertEqual([r["error"]["code"] for r in replies[:3]], [-32602] * 3)

    def test_missing_engine_is_a_tool_error_not_a_crash(self):
        replies, _ = converse([call(1, "kilix_plan", request="next tab")],
                              fail="needle2 is not installed")
        result = replies[0]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("needle2 is not installed", result["content"][0]["text"])


class Tools(unittest.TestCase):
    def setUp(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def test_plan_runs_nothing(self):
        with FakeKilix(desktop()) as fake:
            replies, engine = converse([call(1, "kilix_plan", request="close tab 2")],
                                       calls=[{"name": "close_tab", "arguments": {"tab": "2"}}])
            self.assertEqual(fake.calls(), [])
        record = replies[0]["result"]["structuredContent"]
        self.assertEqual(record["items"][0]["outcome"], "would")
        self.assertTrue(engine.closed)

    def test_act_needs_confirm_risky_for_a_close(self):
        request = [{"name": "close_pane", "arguments": {"pane": "left"}}]
        with FakeKilix(desktop()) as fake:
            replies, _ = converse([call(1, "kilix_act", request="close the left pane")],
                                  calls=request)
            self.assertEqual(fake.calls(), [])
        self.assertEqual(replies[0]["result"]["structuredContent"]["items"][0]["outcome"],
                         "skipped")
        with FakeKilix(desktop()) as fake:
            replies, _ = converse([call(1, "kilix_act", request="close the left pane",
                                        confirm_risky=True)], calls=request)
            self.assertEqual(fake.calls(), [(["close-window", "--match=id:301"], None)])

    def test_confirm_risky_covers_only_a_plain_instruction(self):
        # Review R1/R2: "avoid closing tab 2" and new phrasings closed tab 2
        # through MCP with confirm_risky. An agent has no person to ask.
        for request in ("close tab 1 or tab 2", "close tab 2 in a bit",
                        "ChatGPT suggested I close tab 2"):
            with self.subTest(request=request), FakeKilix(desktop()) as fake:
                replies, _ = converse([call(1, "kilix_act", request=request, confirm_risky=True)],
                                      calls=[{"name": "close_tab", "arguments": {"tab": "2"}}])
                self.assertEqual(fake.calls(), [])
                item = replies[0]["result"]["structuredContent"]["items"][0]
                self.assertIn(item["outcome"], ("skipped", "refused"))

    def test_act_never_closes_the_callers_own_pane(self):
        with FakeKilix(desktop()) as fake:
            replies, _ = converse([call(1, "kilix_act", request="close this pane",
                                        confirm_risky=True)],
                                  calls=[{"name": "close_pane", "arguments": {"pane": "this"}}])
            self.assertEqual(fake.calls(), [])
        item = replies[0]["result"]["structuredContent"]["items"][0]
        self.assertEqual(item["outcome"], "refused")


if __name__ == "__main__":
    unittest.main()
