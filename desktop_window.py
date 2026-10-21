"""Small X11 window-message helper, run under the backend's subprocess deadline.

Graceful close uses ICCCM WM_DELETE_WINDOW only when the client advertises it.
Maximize/restore use the EWMH _NET_WM_STATE message. No kill/destroy fallback.
Imports stay lazy so parsing and other desktop operations need no Python Xlib.
"""
from __future__ import annotations

import argparse
import json
import sys


def request(connection, operation, wid, *, execute=False):
    from Xlib import X, Xatom, protocol

    if operation not in ("close", "maximize", "restore") or not 0 < wid <= 0xffffffff:
        raise ValueError("invalid window operation")
    root = connection.screen().root
    window = connection.create_resource_object("window", wid)
    window.get_attributes()  # Refuse a vanished resource, including in preview.

    def atom(name):
        value = connection.intern_atom(name, only_if_exists=True)
        if not value:
            raise ValueError("window protocol is unavailable: " + name)
        return value

    if operation == "close":
        message = atom("WM_PROTOCOLS")
        delete = atom("WM_DELETE_WINDOW")
        protocols = window.get_full_property(message, Xatom.ATOM)
        if protocols is None or protocols.format != 32 or delete not in protocols.value:
            raise ValueError("window does not support graceful close")
        recipient, mask = window, 0
        data = [delete, X.CurrentTime, 0, 0, 0]
    else:
        message = atom("_NET_WM_STATE")
        vertical = atom("_NET_WM_STATE_MAXIMIZED_VERT")
        horizontal = atom("_NET_WM_STATE_MAXIMIZED_HORZ")
        supported = root.get_full_property(atom("_NET_SUPPORTED"), Xatom.ATOM)
        if supported is None or supported.format != 32 or not {message, vertical, horizontal} <= set(supported.value):
            raise ValueError("window manager does not support maximization")
        recipient, mask = root, X.SubstructureRedirectMask | X.SubstructureNotifyMask
        data = [int(operation == "maximize"), vertical, horizontal, 2, 0]
    if execute:
        event = protocol.event.ClientMessage(window=wid, client_type=message, data=(32, data))
        recipient.send_event(event, event_mask=mask, propagate=False)
        connection.sync()


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("operation", choices=("close", "maximize", "restore"))
    parser.add_argument("window", type=int)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    connection = None
    try:
        from Xlib import display
        connection = display.Display()
        errors = []
        connection.set_error_handler(lambda error, _request: errors.append(error))
        request(connection, args.operation, args.window, execute=args.execute)
        connection.sync()
        if errors:
            raise ValueError("X11 rejected the window request")
        print(json.dumps({"ok": True}))
        return 0
    except ImportError:
        print(json.dumps({"ok": False, "error": "install python3-xlib for window close/maximize/restore"}))
        return 1
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)[:256]}))
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    sys.exit(main())
