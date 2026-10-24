#!/usr/bin/python3
"""Constrained clients using the real Needle CLI grammar and stdio MCP server.

RemoteController is dispatch-only. This entrypoint never establishes authority.
"""
import sys
import pty_cli
from kilix_pty_agent_client import RemoteController


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "pty":
        # Existing CLI performs structured/text parsing, validation and rendering.
        return pty_cli.main(["--json", *argv[1:]], controller=RemoteController())
    if argv in (["mcp", "--tools", "pty"], ["mcp", "--tools", "all"]):
        import mcp_server
        def unavailable(*args):
            raise ValueError("no model runtime is exposed by the PTY server")
        return mcp_server.serve(unavailable, tools=argv[2], pty_controller=RemoteController(),
                                discovery_only=argv[2] == "all")
    raise ValueError("use pty ARGS or mcp --tools pty|all; all is discovery-only")


if __name__ == "__main__":
    raise SystemExit(main())
