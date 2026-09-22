"""Needle through `libneedle.so`, for weights the `needle` binary cannot load.

The standalone binary carries its weights and has no way to take others, so a
fine-tuned `.cact` runs through the upstream shared library instead: a child
process loads the library from its sealed descriptor with ctypes, hands the
weights to `needle_load`, and answers JSON lines on stdin/stdout. There is no
port, so nothing else on the machine can reach it.

Measured on the pinned library: `needle_load` returns 0 on success, and on
bytes it cannot use returns -1 *and keeps its built-in base weights*. The
worker therefore refuses to start unless the load returned 0, so a tuned
model can never be silently replaced by the base one.

    parent:  {"op": "complete", "input": "..."}   worker: the engine's JSON
             {"op": "reset"}                              {"ok": true}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_BUFFER = 64 * 1024
_WORKER = os.path.abspath(__file__)


class LibEngineError(RuntimeError):
    """The library worker could not start or answer."""


class LibEngine:
    """Same surface as engine.Engine: start, complete, reset, close."""

    def __init__(self, library, tools: list[dict], weights=None, *, timeout: float = 120.0):
        self.library, self.weights, self.tools, self.timeout = library, weights, tools, timeout
        self._process: subprocess.Popen | None = None

    def __enter__(self) -> "LibEngine":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def start(self) -> None:
        fds = [self.library.fd] + ([self.weights.fd] if self.weights is not None else [])
        argv = [sys.executable, "-I", "-B", _WORKER, "--worker", str(self.library.fd),
                str(self.weights.fd) if self.weights is not None else "-"]
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        self._process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, env=env, pass_fds=fds,
                                         text=True, encoding="utf-8")
        reply = self._ask({"op": "init", "tools": self.tools})
        if not reply.get("ok"):
            self.close()
            raise LibEngineError(reply.get("error") or "the Needle library would not start")

    def _ask(self, message: dict) -> dict:
        process = self._process
        if process is None or process.poll() is not None:
            raise LibEngineError("the Needle library worker is not running")
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
        except OSError as error:
            raise LibEngineError(f"the Needle library worker failed: {error}") from error
        if not line:
            raise LibEngineError("the Needle library worker exited")
        try:
            value = json.loads(line)
        except ValueError as error:
            raise LibEngineError("the Needle library worker answered with malformed JSON") from error
        if not isinstance(value, dict):
            raise LibEngineError("the Needle library worker answered with an unexpected shape")
        return value

    def complete(self, text: str) -> dict:
        return self._ask({"op": "complete", "input": text})

    def reset(self) -> None:
        self._ask({"op": "reset"})

    def close(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _worker(library_fd: int, weights_fd: int | None) -> int:
    import ctypes
    out = sys.stdout

    def say(value):
        out.write(json.dumps(value, ensure_ascii=False) + "\n")
        out.flush()

    lib = ctypes.CDLL(f"/proc/self/fd/{library_fd}")
    lib.needle_load.argtypes = [ctypes.c_char_p, ctypes.c_ulonglong]
    lib.needle_load.restype = ctypes.c_int
    lib.needle_init.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.needle_init.restype = ctypes.c_int
    lib.needle_complete.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.needle_complete.restype = ctypes.c_int
    lib.needle_reset.argtypes = []
    buffer = ctypes.create_string_buffer(_BUFFER)
    for line in sys.stdin:
        message = json.loads(line)
        op = message.get("op")
        if op == "init":
            if weights_fd is not None:
                size = os.fstat(weights_fd).st_size
                data = os.pread(weights_fd, size, 0)
                if lib.needle_load(data, len(data)) != 0:
                    say({"ok": False, "error": "the tuned weights were not accepted; "
                                               "refusing to fall back to the base model"})
                    return 1
            tools = json.dumps(message["tools"], ensure_ascii=False).encode("utf-8")
            # needle_init reports success with a positive value (measured: 1 with ten
            # tools); needle_load, above, with exactly 0.
            status = lib.needle_init(None, tools, None)
            say({"ok": True} if status > 0 else {"ok": False, "error": f"needle_init {status}"})
        elif op == "complete":
            written = lib.needle_complete(message["input"].encode("utf-8"), 256, buffer, _BUFFER)
            if written < 0:
                say({"type": "error", "error": f"needle_complete {written}"})
            else:
                say(json.loads(buffer.value.decode("utf-8")))
        elif op == "reset":
            lib.needle_reset()
            say({"ok": True})
        else:
            say({"type": "error", "error": f"unknown op {op!r}"})
    return 0


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--worker":
    sys.exit(_worker(int(sys.argv[2]), None if sys.argv[3] == "-" else int(sys.argv[3])))
