"""Needle through `libneedle.so`, for weights the `needle` binary cannot load.

The standalone binary carries its weights and has no way to take others, so a
fine-tuned `.cact` runs through the upstream shared library instead: a child
process loads the library from its sealed descriptor with ctypes, hands the
weights to `needle_load`, and answers JSON lines on stdin/stdout. There is no
port, so nothing else on the machine can reach it.

Generation 2 remains the default. The isolated generation-3 pilot explicitly
passes generation=3 and verified weights, uses the six-argument completion
API, and reports confidence as unavailable for its local LoRA exports.

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
import select
import subprocess
import sys
import threading
import time

_BUFFER = 64 * 1024
# Upstream's default is 256 new tokens. A call carrying a long absolute path twice
# (command and pane) ran past it: "tool call truncated: token budget exhausted",
# 17 times in the gpt-6-luna route benchmark. Generation stops at the end of the
# call, so a higher cap costs time only where the answer was being cut off.
_MAX_NEW_TOKENS = 768
_WORKER = os.path.abspath(__file__)


class LibEngineError(RuntimeError):
    """The library worker could not start or answer."""


class LibEngine:
    """Same surface as engine.Engine: start, complete, reset, close."""

    def __init__(self, library, tools: list[dict], weights=None, *, timeout: float = 120.0,
                 generation: int = 2, backend: str = "native"):
        if generation not in (2, 3):
            raise ValueError("the library generation must be explicitly 2 or 3")
        if generation == 3 and weights is None:
            raise ValueError("generation 3 requires verified explicit weights")
        if backend not in ("native", "rust3") or (backend == "rust3" and generation != 3):
            raise ValueError("unsupported library backend/generation")
        self.library, self.weights, self.tools, self.timeout = library, weights, tools, timeout
        self.generation = generation
        self.backend = backend
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def __enter__(self) -> "LibEngine":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def start(self) -> None:
        if self._process is not None:
            raise LibEngineError("the Needle library worker is already started")
        fds = [self.library.fd] + ([self.weights.fd] if self.weights is not None else [])
        argv = [sys.executable, "-I", "-B", _WORKER, "--worker", str(self.library.fd),
                str(self.weights.fd) if self.weights is not None else "-",
                str(self.generation), str(os.getpid()), self.backend]
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "NEEDLE_TELEMETRY": "0",
               "DO_NOT_TRACK": "1", "CUDA_VISIBLE_DEVICES": "", "HIP_VISIBLE_DEVICES": ""}
        self._process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, env=env, pass_fds=fds,
                                         text=True, encoding="utf-8")
        try:
            reply = self._ask({"op": "init", "tools": self.tools})
            if not reply.get("ok"):
                raise LibEngineError(reply.get("error") or "the Needle library would not start")
        except BaseException:
            self.close()
            raise

    def _ask(self, message: dict) -> dict:
        with self._lock:
            return self._ask_locked(message)

    def _ask_locked(self, message: dict) -> dict:
        process = self._process
        if process is None or process.poll() is not None:
            raise LibEngineError("the Needle library worker is not running")
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()
            # Bound the entire response, including a worker that writes only
            # part of a line. A ready pipe alone does not bound readline().
            deadline = time.monotonic() + self.timeout
            chunks = bytearray()
            while b"\n" not in chunks:
                remaining = deadline - time.monotonic()
                ready = select.select([process.stdout], [], [], max(0, remaining))[0]
                if remaining <= 0 or not ready:
                    raise LibEngineError(f"the Needle library did not answer in {self.timeout:g} s")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise LibEngineError("the Needle library worker exited")
                chunks.extend(chunk)
                if len(chunks) > 1024 * 1024:
                    raise LibEngineError("the Needle library response is too large")
            line = chunks.decode("utf-8")
        except LibEngineError:
            self.close()
            raise
        except UnicodeError as error:
            self.close()
            raise LibEngineError("the Needle library response is not UTF-8") from error
        except OSError as error:
            self.close()
            raise LibEngineError(f"the Needle library worker failed: {error}") from error
        if not line:
            raise LibEngineError("the Needle library worker exited")
        try:
            value = json.loads(line)
        except ValueError as error:
            self.close()
            raise LibEngineError("the Needle library worker answered with malformed JSON") from error
        if not isinstance(value, dict):
            self.close()
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
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            finally:
                process.stdout.close()


def _worker(library_fd: int, weights_fd: int | None, generation: int, parent_pid: int,
            backend: str = "native") -> int:
    import ctypes
    import signal
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        return 1
    out = sys.stdout

    def say(value):
        out.write(json.dumps(value, ensure_ascii=False) + "\n")
        out.flush()

    lib = ctypes.CDLL(f"/proc/self/fd/{library_fd}")
    if backend == "rust3":
        return _rust_worker(lib, weights_fd, say)
    lib.needle_load.argtypes = [ctypes.c_char_p, ctypes.c_ulonglong]
    lib.needle_load.restype = ctypes.c_int
    lib.needle_init.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.needle_init.restype = ctypes.c_int
    audio_types = [ctypes.POINTER(ctypes.c_float), ctypes.c_int] if generation == 3 else []
    lib.needle_complete.argtypes = [ctypes.c_char_p, *audio_types, ctypes.c_int,
                                   ctypes.c_char_p, ctypes.c_int]
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
            audio_args = (None, 0) if generation == 3 else ()
            limit = 512 if generation == 3 else _MAX_NEW_TOKENS
            written = lib.needle_complete(message["input"].encode("utf-8"), *audio_args, limit,
                                          buffer, _BUFFER)
            if written < 0:
                say({"type": "error", "error": f"needle_complete {written}"})
            else:
                reply = json.loads(buffer.value.decode("utf-8"))
                # The generation-3 pilot uses local LoRA exports, whose
                # confidence head is removed. Never expose decode confidence
                # as a calibrated probability. Withheld calls stay withheld.
                if generation == 3:
                    reply["confidence"] = None
                say(reply)
        elif op == "reset":
            lib.needle_reset()
            say({"ok": True})
        else:
            say({"type": "error", "error": f"unknown op {op!r}"})
    return 0


def _rust_reply(text: str, stop: int, positions: int) -> dict:
    """Preserve raw proposals; incomplete or ambiguous payloads are errors."""
    reply = {"confidence": None, "raw_text": text, "stop": stop, "positions": positions}
    try:
        if stop not in (0, 1):
            raise ValueError("completion exhausted its token or context budget")
        opening, closing = "<tool_call>", "</tool_call>"
        if text.count(opening) != 1 or text.count(closing) != 1:
            raise ValueError("expected one complete tool-call payload")
        before, payload = text.split(opening)
        payload, after = payload.split(closing)
        if after.strip() or before.count("<think>") != 1 or before.count("</think>") != 1:
            raise ValueError("unexpected text outside the completion structure")
        if not before.startswith("<think>") or before.split("</think>", 1)[1].strip():
            raise ValueError("malformed reasoning boundary")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result
        calls = json.loads(payload, object_pairs_hook=unique)
        if not isinstance(calls, list):
            raise ValueError("tool-call payload must be a list")
        for call in calls:
            if (not isinstance(call, dict) or set(call) != {"name", "arguments"}
                    or not isinstance(call["name"], str) or not isinstance(call["arguments"], dict)):
                raise ValueError("malformed tool call")
        reply.update(type="call", function_calls=calls)
    except (ValueError, TypeError) as exc:
        reply.update(type="error", error=str(exc))
    return reply


def _rust_worker(lib, weights_fd, say):
    """Cached CPU ABI, with independent state on every request and no ports."""
    import ctypes as c
    lib.needle_v3_cached_load.argtypes = [c.c_char_p, c.c_char_p]
    lib.needle_v3_cached_load.restype = c.c_void_p
    lib.needle_v3_cached_complete.argtypes = [c.c_void_p, c.c_char_p, c.c_size_t,
        c.c_bool, c.c_bool, c.POINTER(c.c_uint32), c.POINTER(c.c_size_t)]
    lib.needle_v3_cached_complete.restype = c.c_void_p
    lib.needle_v3_cached_free.argtypes = [c.c_void_p]
    lib.needle_v3_cached_free.restype = None
    lib.needle_free_str.argtypes = [c.c_void_p]
    lib.needle_free_str.restype = None
    lib.needle_last_error.argtypes = []
    lib.needle_last_error.restype = c.c_char_p
    handle = None
    try:
        for line in sys.stdin:
            message = json.loads(line)
            op = message.get("op")
            if op == "init":
                if handle is not None or weights_fd is None:
                    say({"ok": False, "error": "cached runtime requires one explicit weight initialization"})
                    return 1
                tools = json.dumps(message["tools"], ensure_ascii=False, separators=(",", ":")).encode()
                handle = lib.needle_v3_cached_load(f"/proc/self/fd/{weights_fd}".encode(), tools)
                say({"ok": True} if handle else {"ok": False, "error":
                    (lib.needle_last_error() or b"cached model load failed").decode(errors="replace")})
                if not handle:
                    return 1
            elif op == "reset":
                # generate clones an immutable prefix, including all history.
                say({"ok": True})
            elif op == "complete" and handle:
                query = message["input"]
                if not isinstance(query, str) or "\0" in query or len(query.encode()) > 16384:
                    say({"type": "error", "error": "invalid or oversized request"})
                    continue
                stop, positions = c.c_uint32(99), c.c_size_t()
                pointer = lib.needle_v3_cached_complete(handle, query.encode(), 512, True, True,
                    c.byref(stop), c.byref(positions))
                if not pointer:
                    say({"type": "error", "error": (lib.needle_last_error() or b"completion failed").decode(errors="replace")})
                    continue
                try:
                    text = c.string_at(pointer).decode("utf-8")
                    say(_rust_reply(text, stop.value, positions.value))
                finally:
                    lib.needle_free_str(pointer)
            else:
                say({"type": "error", "error": "cached runtime is not initialized or operation is invalid"})
    finally:
        if handle:
            lib.needle_v3_cached_free(handle)
    return 0


if __name__ == "__main__" and len(sys.argv) == 7 and sys.argv[1] == "--worker":
    sys.exit(_worker(int(sys.argv[2]), None if sys.argv[3] == "-" else int(sys.argv[3]),
                     int(sys.argv[4]), int(sys.argv[5]), sys.argv[6]))
