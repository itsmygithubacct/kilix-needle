"""Read the window inventory of one local Kilix instance on Linux."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import struct
import sys


def valid_pid(pid: object) -> bool:
    return type(pid) is int and 0 < pid <= 2**31 - 1


def listening_instances() -> list[int]:
    """Enumerate canonical Kilix listening addresses without connecting to them."""
    try:
        with open("/proc/net/unix", "rb") as table:
            data = table.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            return []
        listeners = []
        for line in data.splitlines():
            fields = line.split(maxsplit=7)
            if len(fields) != 8 or fields[3:6] != [b"00010000", b"0001", b"01"]:
                continue
            match = re.fullmatch(rb"@kilix-([1-9][0-9]{0,9})", fields[7])
            if match and valid_pid(int(match[1])):
                listeners.append(int(match[1]))
        return listeners
    except OSError:
        return []


def _windows(pid: int) -> tuple[list, int]:
    """Send only the kitty RC ls request, on the socket whose peer we checked."""
    prefix, end = b"\x1bP@kitty-cmd", b"\x1b\\"
    limit = 8 * 1024 * 1024
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(5)
        peer.connect(f"\0kilix-{pid}")
        peer_pid, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        process = Path("/proc") / str(pid)
        if peer_pid != pid or uid != os.geteuid():
            raise ValueError("unexpected Kilix instance")
        if (process / "exe").readlink().name.removesuffix(" (deleted)") != "kitty":
            raise ValueError("unexpected Kilix instance")
        started = int((process / "stat").read_bytes().rsplit(b") ", 1)[1].split()[19])
        # The documented standalone ls request carries no password or caller ID.
        peer.sendall(prefix + b'{"cmd":"ls","version":[0,14,2]}' + end)
        reply = bytearray()
        while not reply.endswith(end):
            chunk = peer.recv(65536)
            if not chunk or len(reply) + len(chunk) > limit:
                raise ValueError("incomplete or oversized window inventory")
            reply.extend(chunk)
    if not reply.startswith(prefix):
        raise ValueError("invalid window inventory")
    envelope = json.loads(reply[len(prefix):-len(end)])
    if envelope.get("ok") is not True:
        raise ValueError("window inventory refused")
    return json.loads(envelope["data"]), started


def main(pid: int, toolkit: str) -> None:
    # Run the same pane inspector as 'kilix panes list' in a bounded child.
    # A caller and broker status from the server's startup pane do not apply.
    sys.path.insert(0, toolkit)
    from kilix_tui import kitty_rc, pane_center
    os.environ.pop("KITTY_WINDOW_ID", None)
    os.environ.pop("KITTY_PTY_BROKER_EXECUTABLE", None)
    windows, started = _windows(pid)
    snapshot = pane_center.Inspector().snapshot(kitty_rc.parse(windows)).to_dict()
    snapshot.update(instance=pid, instance_start=started)
    print(json.dumps(snapshot))


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2])
