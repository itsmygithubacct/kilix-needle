#!/usr/bin/python3
"""Constrained supervisor PTY adapter; deployed only inside its actor sandbox.

The injected remote proxy can dispatch but cannot issue user authority. This
entrypoint is intentionally distinct from the ordinary unrestricted CLI/MCP.
"""
import argparse
import json
import sys

import pty_cli
from kilix_pty_agent_client import RemoteController


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=["pty"])
    parser.add_argument("--request-json", required=True)
    args = parser.parse_args(argv)
    import json_request
    request = json_request.loads(args.request_json)
    record = pty_cli.run(request, agent=True, controller=RemoteController())
    print(json.dumps(record, ensure_ascii=True, separators=(",", ":")))
    return record["status"]


if __name__ == "__main__":
    raise SystemExit(main())
