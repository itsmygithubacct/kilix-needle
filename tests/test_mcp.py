"""The MCP stdio server: protocol shape, tools, and agent-mode behaviour."""
import io
import json
import os
import unittest
from unittest import mock

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
                         ["kilix_plan", "kilix_act", "kilix_apps_plan", "kilix_apps_act",
                          "kilix_agents_plan", "kilix_agents_act", "kilix_system_plan",
                          "kilix_system_read", "kilix_system_suggest", "kilix_logs_read",
                          "kilix_files_plan", "kilix_files_read"])
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
        # "close tab 2" is an exact pane command: the engine is never loaded
        self.assertFalse(engine.closed)
        with FakeKilix(desktop()):
            _, engine = converse([call(1, "kilix_plan", request="close whichever tab is second")],
                                 calls=[{"name": "close_tab", "arguments": {"tab": "2"}}])
        self.assertTrue(engine.closed)        # a model-route request loads it, and serve closes it

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


class TokenCost(unittest.TestCase):
    """Agents pay for every byte of tools/list and of each result (2026-09-29 route
    benchmark: 9,026 bytes of tool text and every record sent twice)."""

    def setUp(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def test_tool_list_stays_small(self):
        replies, _ = converse([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        tools = replies[0]["result"]["tools"]
        self.assertLessEqual(len(json.dumps(tools)), 6800)

        def described(node):
            if isinstance(node, dict):
                return sum(len(v) if k == "description" and isinstance(v, str) else described(v)
                           for k, v in node.items())
            return sum(map(described, node)) if isinstance(node, list) else 0
        self.assertLessEqual(described(tools), 2800)

    def test_result_is_one_compact_record_without_the_echoed_request(self):
        with FakeKilix(desktop()):
            replies, _ = converse([call(1, "kilix_plan", request="close tab 2")],
                                  calls=[{"name": "close_tab", "arguments": {"tab": "2"}}])
        result = replies[0]["result"]
        record = result["structuredContent"]
        self.assertNotIn("request", record)
        self.assertNotIn("note", record)                    # it was empty
        self.assertEqual(record["items"][0]["outcome"], "would")
        text = result["content"][0]["text"]
        self.assertEqual(json.loads(text), record)
        self.assertNotIn(", ", text)
        self.assertNotIn("close tab 2\"", text)

    def test_trim_keeps_everything_the_caller_does_not_already_have(self):
        record = {"request": "close tab 2", "status": 1, "note": "nothing runs without a yes",
                  "observation": None, "hint": "", "future_field": {"x": ""},
                  "items": [{"outcome": "refused", "reason": "not a plain instruction", "note": ""}]}
        kept = mcp_server._result(record, True, "close tab 2")["structuredContent"]
        self.assertEqual(kept, {"status": 1, "note": "nothing runs without a yes",
                                "observation": None, "future_field": {"x": ""},
                                "items": record["items"]})
        other = mcp_server._result(record, True, "close tab 3")["structuredContent"]
        self.assertEqual(other["request"], "close tab 2")   # not an echo: kept
        text = mcp_server._result(record, True, "close tab 2")["content"][0]["text"]
        self.assertIn("not a plain instruction", text)
        self.assertIn("refused", text)


class FirstCallRight(unittest.TestCase):
    """Luna route benchmark (2026-09-29): every kilix_logs_read of a plain file first failed on
    the missing provider, and agents called a _plan tool before its _act 67 times. Each is a
    whole extra model round trip (~14k tokens)."""

    def test_the_schema_and_the_error_both_name_the_provider_to_use(self):
        spec = next(t for t in mcp_server.TOOL_LIST if t["name"] == "kilix_logs_read")
        self.assertIn("raw", spec["inputSchema"]["properties"]["provider"]["description"])
        replies, _ = converse([call(1, "kilix_logs_read", file="/tmp/app.log",
                                    operation="search", query="x")])
        message = replies[0]["error"]["message"]
        self.assertIn("raw for a plain text log", message)
        self.assertIn("codex", message)

    def test_each_plan_tool_says_its_act_tool_runs_the_same_checks(self):
        tools = {t["name"]: t["description"] for t in mcp_server.TOOL_LIST}
        for plan, act in (("kilix_plan", "kilix_act"), ("kilix_apps_plan", "kilix_apps_act"),
                          ("kilix_agents_plan", "kilix_agents_act")):
            with self.subTest(plan=plan):
                self.assertIn(f"{act} runs the same checks", tools[plan])


class FuzzyUnderMcp(unittest.TestCase):
    """Review R8 mutant G01: a whole-word target is never acted on through MCP."""

    def setUp(self):
        # A known caller, so the guard under test is the fuzzy gate, not the
        # no-caller rule (which would refuse everything risky by itself).
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def test_confirm_risky_does_not_cover_a_whole_word_target(self):
        import copy
        tree = copy.deepcopy(desktop())
        tree[0]["tabs"][2]["windows"][2]["title"] = "api server"
        for request, calls in (("close the api pane", [{"name": "close_pane", "arguments": {"pane": "api"}}]),
                               ("run make in the api pane",
                                [{"name": "run_in_pane", "arguments": {"pane": "api", "command": "make"}}])):
            with self.subTest(request=request), FakeKilix(tree) as fake:
                converse([call(1, "kilix_act", request=request, confirm_risky=True)], calls=calls)
                self.assertEqual(fake.calls(), [])


class ElsewhereUnderMcp(unittest.TestCase):
    """KN-R9-01 through MCP: confirm_risky does not choose between two tabs' panes."""

    def setUp(self):
        os.environ["KITTY_WINDOW_ID"] = "300"
        self.addCleanup(os.environ.pop, "KITTY_WINDOW_ID", None)

    def test_confirm_risky_does_not_cover_a_name_in_two_tabs(self):
        import copy
        tree = copy.deepcopy(desktop())
        tree[0]["tabs"][1]["windows"][1]["title"] = "Build"
        with FakeKilix(tree) as fake:
            replies, _ = converse([call(1, "kilix_act", request="close the Build pane",
                                        confirm_risky=True)],
                                  calls=[{"name": "close_pane", "arguments": {"pane": "Build"}}])
            self.assertEqual(fake.calls(), [])
        item = replies[0]["result"]["structuredContent"]["items"][0]
        self.assertEqual(item["outcome"], "skipped")



class ExactNeedsNoEngine(unittest.TestCase):
    def test_an_exact_pane_command_on_the_cli_never_opens_the_runtime(self):
        # luna-full benchmark: loading the engine first made every exact panes
        # CLI request wait ~3 s, and agents gave up on the command.
        out = io.StringIO()
        with FakeKilix(desktop()), mock.patch.object(
                needle_cli, "open_runtime", side_effect=AssertionError("runtime opened")), \
                mock.patch("sys.stdout", out):
            status = needle_cli.main(["--json", "--dry-run", "close tab 2"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())["items"][0]["outcome"], "would")


if __name__ == "__main__":
    unittest.main()
