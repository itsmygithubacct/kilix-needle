"""Actual raw ingress bodies with inert routing; no server or action startup."""
import argparse
import ast
import contextlib
import importlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import pty_job

ROOT = Path(__file__).resolve().parents[1]
RAW = '{"operation":"end","selector":{"id_prefix":"group."},"selector":{"id":"group.b"},"operation_id":"duplicate-selector"}'
DUPLICATES = [RAW,
    '{"operation":"end","operation":"end","selector":{"id":"group.b"},"operation_id":"key"}',
    '{"operation":"resolve","operation":"end","selector":{"id":"group.b"},"operation_id":"key"}',
    '{"operation":"end","selector":{"id":"group.b"},"selector":{"id":"group.b"},"operation_id":"key"}',
    '{"operation":"end","selector":{"id":"group.b"},"operation_id":"key","operation_id":"key"}',
    '{"operation":"end","selector":{"id":"group.b"},"operation_id":"old","operation_id":"new"}',
    '{"operation":"end","selector":{"id":"group.b","id":"group.b"},"operation_id":"key"}',
    '{"operation":"end","selector":{"id":"group.a","id":"group.b"},"operation_id":"key"}',
    '{"operation":"resolve","selector":{"id_prefix":"group.","id_prefix":"group."}}',
    '{"operation":"resolve","selector":{"id_prefix":"group.","id_prefix":"other."}}',
    '{"operation":"resolve","selector":{"startup_words":["compile"],"startup_words":["compile"]}}',
    '{"operation":"resolve","selector":{"startup_words":["compile"],"startup_words":["other"]}}',
    '{"operation":"end","selector":{"id":"group.b","attached":false,"attached":false},"operation_id":"key"}',
    '{"operation":"end","selector":{"id":"group.b","attached":false,"attached":true},"operation_id":"key"}',
    '{"operation":"end","selector":{"id":"group.b","attached":false,"att\\u0061ched":true},"operation_id":"key"}',
    '{"operation":"list","extra":[{"nested":{"x":1,"x":2}}]}']


def ingress(name, function, namespace):
    """Compile the actual function, excluding operational module imports/setup."""
    path = ROOT / name
    tree = ast.parse(path.read_bytes(), filename=str(path))
    namespace = {"__doc__": ast.get_docstring(tree), "__name__": "inert_ingress", **namespace}
    if (ROOT / "json_request.py").exists():
        namespace["json_request"] = importlib.import_module("json_request")
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function]
    assert len(body) == 1
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    return namespace[function]


class RawIngress(unittest.TestCase):
    def cli(self, raw, *, stdin=False, options=()):
        calls = []
        def counter(request, **kwargs):
            calls.append((request, kwargs))
            return {"job": "pty", "status": 0, "inert": True}
        main = ingress("pty_cli.py", "main", {"argparse": argparse, "json": json, "sys": sys,
                                              "pty_job": pty_job, "run": counter, "render": json.dumps})
        output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(raw if stdin else "")), contextlib.redirect_stdout(output):
            code = main(["--json", "--request-json", "-" if stdin else raw, *options])
        return code, json.loads(output.getvalue()), calls

    def mcp(self, lines):
        calls, closed = [], []
        class InertServer:
            def __init__(self, factory, **kwargs):
                self.options = kwargs
            def handle(self, message):
                calls.append(message)
                if message.get("id") is None:
                    return None
                return {"jsonrpc": "2.0", "id": message["id"], "result": {"inert": True}}
            def close(self):
                closed.append(True)
        serve = ingress("mcp_server.py", "serve", {"json": json, "sys": sys, "Server": InertServer})
        output = io.StringIO()
        def forbidden_factory(*args, **kwargs):
            raise AssertionError("runtime factory must never activate")
        code = serve(forbidden_factory, io.StringIO(lines), output, tools="pty")
        self.assertEqual(closed, [True])
        return code, [json.loads(line) for line in output.getvalue().splitlines()], calls

    def test_cli_inline_recursive_equal_and_conflicting_duplicates_never_route(self):
        for raw in DUPLICATES:
            with self.subTest(raw=raw):
                code, record, calls = self.cli(raw, options=("--yes", "--agent"))
                self.assertEqual((code, record["status"], calls), (2, 2, []))
                self.assertIn("duplicate", record["note"])
                self.assertIn("hint", record)

    def test_cli_stdin_recursive_equal_and_conflicting_duplicates_never_route(self):
        for raw in DUPLICATES:
            with self.subTest(raw=raw):
                code, record, calls = self.cli(raw, stdin=True, options=("--yes", "--agent"))
                self.assertEqual((code, record["status"], calls), (2, 2, []))

    def test_cli_valid_objects_preserve_payload_flags_and_existing_error_envelope(self):
        for selector in ({"id": "group.b"}, {"id_prefix": "group.", "attached": False},
                         {"startup_words": ["compile", "test"], "attached": True}):
            request = {"operation": "end", "selector": selector, "operation_id": "key"}
            for stdin in (False, True):
                with self.subTest(selector=selector, stdin=stdin):
                    code, record, calls = self.cli(json.dumps(request), stdin=stdin,
                                                   options=("--dry-run", "--yes", "--agent", "--timeout-seconds", "0.4"))
                    self.assertEqual((code, record["status"], len(calls)), (0, 0, 1))
                    value, flags = calls[0]
                    self.assertEqual(pty_job.structured(value), request)
                    self.assertEqual((flags["dry_run"], flags["assume_yes"], flags["agent"], flags["timeout_seconds"]),
                                     (True, True, True, 0.4))
        for raw in ('{"operation":', 'x' * 4097):
            code, record, calls = self.cli(raw)
            self.assertEqual((code, record["status"], calls), (2, 2, []))
        code, _, calls = self.cli('{"operation":"list"}')
        self.assertEqual((code, len(calls), calls[0][1]["assume_yes"]), (0, 1, False))

    def test_raw_mcp_duplicate_request_members_never_reach_inert_handler(self):
        for raw in DUPLICATES:
            line = '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"kilix_pty_act","arguments":{"request":' + raw + ',"confirm_risky":true}}}\n'
            with self.subTest(raw=raw):
                code, replies, calls = self.mcp(line)
                self.assertEqual((code, calls), (0, []))
                self.assertEqual(replies, [{"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}])

    def test_raw_mcp_duplicate_envelope_consent_and_array_members_never_route(self):
        lines = ['{"id":1,"method":"ping","method":"ping"}',
                 '{"id":1,"id":2,"method":"ping"}',
                 '{"id":1,"method":"ping","method":"tools/call"}',
                 '{"id":1,"method":"tools/call","params":{},"params":{}}',
                 '{"id":1,"method":"tools/call","params":{"name":"kilix_pty_read","name":"kilix_pty_act"}}',
                 '{"id":1,"method":"tools/call","params":{"arguments":{"confirm_risky":true,"confirm_risky":true}}}',
                 '{"id":1,"method":"tools/call","params":{"arguments":{"confirm_risky":false,"confirm_risky":true}}}',
                 '{"id":1,"method":"ping","extra":[{"x":1,"x":1}]}']
        for line in lines:
            with self.subTest(line=line):
                _, replies, calls = self.mcp(line + "\n")
                self.assertFalse(calls)
                self.assertEqual(replies[0]["error"]["code"], -32700)

    def test_raw_mcp_valid_pty_and_other_messages_keep_transport_behavior(self):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}]
        for i, name in enumerate(("kilix_pty_read", "kilix_pty_plan", "kilix_pty_act", "kilix_apps_plan"), 3):
            arguments = {"request": {"operation": "resolve", "selector": {"id": "group.b"}}}
            if name == "kilix_pty_act":
                arguments["confirm_risky"] = True
            messages.append({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        _, replies, calls = self.mcp("\n" + "\n".join(json.dumps(v) for v in messages) + "\n[]\n{\n")
        self.assertEqual(calls, messages)
        self.assertEqual(len(replies), len(messages) + 1)
        self.assertEqual(replies[-2]["error"]["code"], -32600)
        self.assertEqual(replies[-1]["error"]["code"], -32700)

    def test_deep_raw_cli_inline_and_stdin_refuse_without_routing(self):
        # Bounded bytes. Decoder recursion limits vary between Python versions.
        depth = 1500
        raw = "[" * depth + "0" + "]" * depth
        self.assertLess(len(raw), 4096)
        for stdin in (False, True):
            with self.subTest(stdin=stdin):
                code, record, calls = self.cli(raw, stdin=stdin, options=("--yes", "--agent"))
                self.assertEqual((code, record["status"], calls), (2, 2, []))
                self.assertIn("depth", record["note"])

    def test_deep_raw_mcp_refuses_then_handles_valid_next_line_in_memory(self):
        raw = "[" * 1500 + "0" + "]" * 1500
        valid = {"jsonrpc": "2.0", "id": 81, "method": "ping"}
        code, replies, calls = self.mcp(raw + "\n" + json.dumps(valid) + "\n")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [valid])
        self.assertEqual(replies, [
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
            {"jsonrpc": "2.0", "id": 81, "result": {"inert": True}}])

    def test_raw_reader_depth_controls_duplicates_and_unrelated_faults(self):
        import json_request
        for depth in (1, 32, 127, 128):
            raw = "[" * depth + "0" + "]" * depth
            self.assertEqual(json_request.loads(raw), json.loads(raw))
            duplicate = "[" * (depth - 1) + '{"x":1,"x":2}' + "]" * (depth - 1)
            with self.subTest(depth=depth), self.assertRaisesRegex(ValueError, "duplicate"):
                json_request.loads(duplicate)
        for raw in ('{"x":' * 129 + "0" + "}" * 129,
                    "[" * 129 + "0" + "]" * 129):
            for value in (raw, raw.encode(), raw.encode("utf-16"), bytearray(raw.encode())):
                with self.subTest(encoding=type(value).__name__), self.assertRaisesRegex(ValueError, "depth"):
                    json_request.loads(value)
        self.assertEqual(json_request.loads('{"x":"[[[\\\"}{{{"}'), {"x": '[[["}{{{'})
        with mock.patch.object(json_request.json, "loads", side_effect=RecursionError("decoder depth")):
            with self.assertRaisesRegex(ValueError, "depth"):
                json_request.loads("{}")
        for fault in (RuntimeError("unrelated decoder fault"), MemoryError("allocation fault")):
            with mock.patch.object(json_request.json, "loads", side_effect=fault):
                with self.assertRaises(type(fault)):
                    json_request.loads("{}")

    def test_decoder_recursion_is_a_value_refusal(self):
        import json_request
        with mock.patch.object(json_request.json, "loads", side_effect=RecursionError("decoder depth")):
            try:
                json_request.loads("{}")
            except ValueError:
                kind = "refused"
            except RecursionError:
                kind = "uncaught_recursion"
            else:
                kind = "decoded"
        self.assertEqual(kind, "refused")
