"""A private Needle 2 engine process and its loopback client.

The engine is the upstream `needle` binary run with `--serve`. It listens on
127.0.0.1 only, so the port is chosen here, and every request checks that the
listening socket belongs to the child this module started: a local process
that won the port first would otherwise be trusted to choose kilix actions.

The upstream server parses requests by hand. Measured against the pinned
binary: it reads `{"input":"..."}` only in compact form (`{"input": "..."}`
with a space is an empty request, answered with no calls), it does not decode
`\\uXXXX` escapes, and it splits text at a backslash. Requests are therefore
compact raw UTF-8, and `check_prompt` refuses text the server would mangle.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import unicodedata

MAX_PROMPT = 400
MAX_RESPONSE = 1024 * 1024


class EngineError(RuntimeError):
    """The engine could not be started or did not answer usefully."""


def check_prompt(text: str) -> str:
    """Return the prompt stripped, or raise ValueError naming the problem."""
    text = text.strip()
    if not text:
        raise ValueError("the request is empty")
    if len(text.encode("utf-8")) > MAX_PROMPT:
        raise ValueError(f"the request is longer than {MAX_PROMPT} bytes")
    if "\\" in text:
        raise ValueError("the engine cannot read a backslash; rephrase without one")
    if any(unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") for char in text):
        raise ValueError("the request contains control characters")
    return text


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _listening_inodes(port: int) -> set[str]:
    """Inodes of sockets listening on 127.0.0.1:port, from /proc/net/tcp."""
    wanted = f"0100007F:{port:04X}"
    inodes = set()
    try:
        with open("/proc/net/tcp", encoding="ascii") as table:
            next(table, None)
            for line in table:
                fields = line.split()
                # local_address, state 0A = LISTEN, inode
                if len(fields) > 9 and fields[1] == wanted and fields[3] == "0A":
                    inodes.add(fields[9])
    except OSError:
        pass
    return inodes


def _owns_listener(pid: int, port: int) -> bool:
    inodes = _listening_inodes(port)
    if not inodes:
        return False
    try:
        descriptors = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return False
    for name in descriptors:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{name}")
        except OSError:
            continue
        if target.startswith("socket:[") and target[8:-1] in inodes:
            return True
    return False


class Engine:
    """One engine process serving one tool set, used as a context manager."""

    def __init__(self, image, tools: list[dict], *, timeout: float = 30.0,
                 startup: float = 10.0):
        self.image = image
        self.tools = tools
        self.timeout = timeout
        self.startup = startup
        self.port = 0
        self._process: subprocess.Popen | None = None
        self._scratch: tempfile.TemporaryDirectory | None = None

    def __enter__(self) -> "Engine":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def start(self) -> None:
        self._scratch = tempfile.TemporaryDirectory(prefix="kilix-needle-")
        tools_path = os.path.join(self._scratch.name, "tools.json")
        with open(tools_path, "w", encoding="utf-8") as handle:
            json.dump(self.tools, handle, ensure_ascii=False)
        self.port = _free_port()
        # A minimal environment: no NEEDLE_DEBUG (it writes logits to /tmp) and
        # no tuning variables inherited from the caller's shell.
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        try:
            # The verified bytes run from their sealed descriptor, never from a path
            # that could be replaced between the check and the exec.
            self._process = subprocess.Popen(
                [self.image.path, "--tools", tools_path, "--serve", "--port", str(self.port)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env, cwd=self._scratch.name,
                pass_fds=(self.image.fd,))
        except OSError as error:
            self.close()
            raise EngineError(f"cannot start the Needle engine: {error.strerror}") from error
        deadline = time.monotonic() + self.startup
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                code = self._process.returncode
                self.close()
                raise EngineError(f"the Needle engine exited during startup (status {code})")
            if _owns_listener(self._process.pid, self.port):
                return
            time.sleep(0.05)
        self.close()
        raise EngineError("the Needle engine did not start listening in time")

    def _post(self, path: str, body: bytes) -> dict:
        process = self._process
        if process is None or process.poll() is not None:
            raise EngineError("the Needle engine is not running")
        if not _owns_listener(process.pid, self.port):
            raise EngineError("the engine port is no longer held by the engine process")
        request = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                   "Connection: close\r\n\r\n").encode("ascii") + body
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout) as conn:
                conn.sendall(request)
                conn.shutdown(socket.SHUT_WR)
                data = b""
                while chunk := conn.recv(65536):
                    data += chunk
                    if len(data) > MAX_RESPONSE:
                        raise EngineError("the engine response is too large")
        except OSError as error:
            raise EngineError(f"cannot reach the Needle engine: {error}") from error
        head, _, payload = data.partition(b"\r\n\r\n")
        if not head.startswith(b"HTTP/1.1 200") and not head.startswith(b"HTTP/1.0 200"):
            raise EngineError("the engine answered with an error status")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise EngineError("the engine answered with malformed JSON") from error
        if not isinstance(value, dict):
            raise EngineError("the engine answered with an unexpected shape")
        return value

    def complete(self, text: str) -> dict:
        """One turn: the prompt, or a tool result fed back as its JSON text."""
        body = json.dumps({"input": text}, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        return self._post("/complete", body)

    def reset(self) -> None:
        self._post("/reset", b"{}")

    def close(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if self._scratch is not None:
            self._scratch.cleanup()
            self._scratch = None
