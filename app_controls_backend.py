"""Checked adapters for existing Kilix/system control surfaces; never a shell.

prepare() is read-only. perform() never retries a mutation. Audio device names
and indexes are pinned while the user confirms; defaults are not re-resolved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess

from app_controls import Control
import kilix


class ControlError(kilix.KilixError):
    pass


@dataclass(frozen=True)
class Step:
    control: Control
    summary: str
    argv: tuple[str, ...] = ()
    data: str | None = None
    device: dict = field(default_factory=dict)
    socket_path: str = ""
    payload: dict = field(default_factory=dict)
    timeout: int = 15

    def public(self):
        result = {}
        if self.argv:
            result["argv"] = list(self.argv)
        if self.device:
            result["device"] = self.device
        if self.payload:
            result.update(socket=self.socket_path, message=self.payload)
        return result


def command(argv, *, data=None, timeout=15):
    try:
        done = subprocess.run(list(argv), input=data, text=True, errors="replace", capture_output=True,
                              stdin=subprocess.DEVNULL if data is None else None,
                              timeout=timeout, check=False,
                              env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ControlError(f"{argv[0]} unavailable or timed out: {error}") from error
    if done.returncode:
        raise ControlError(f"{argv[0]} failed: {done.stderr.strip()[:2000] or done.returncode}")
    if len(done.stdout) > 1024 * 1024:
        raise ControlError(f"{argv[0]} returned too much data")
    return done.stdout.strip()


def decode(text):
    try:
        return json.loads(text)
    except (ValueError, UnicodeError) as error:
        raise ControlError("backend returned malformed JSON") from error


def devices(kind):
    rows = decode(command(("pactl", "--format=json", "list", kind + "s")))
    if not isinstance(rows, list):
        raise ControlError("pactl returned an invalid device list")
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or \
                not row["name"] or row["name"].startswith("-") or \
                type(row.get("index")) is not int:
            raise ControlError("pactl returned an invalid device identity")
        if kind == "source" and (row["name"].endswith(".monitor") or
                row.get("monitor_of_sink") not in (None, 4294967295, "4294967295")):
            continue
        result.append(row)
    return result


def levels(row):
    volume = row.get("volume")
    if not isinstance(volume, dict) or not volume:
        raise ControlError("device has no channel volume information")
    values = [v.get("value") if isinstance(v, dict) else None for v in volume.values()]
    if any(type(v) is not int or v < 0 for v in values):
        raise ControlError("device has invalid channel volume information")
    return values


def audio_state(row):
    if type(row.get("mute")) is not bool:
        raise ControlError("device has no mute information")
    values = levels(row)
    return {"name": row["name"], "description": row.get("description", ""),
            "index": row["index"], "muted": row["mute"],
            "volume_percent": [round(v * 100 / 65536, 2) for v in values]}


def resolve_device(c):
    rows = devices(c.target)
    name = c.value if c.operation == "default" else command(("pactl", "get-default-" + c.target))
    matches = [r for r in rows if r["name"] == name]
    if not matches and c.operation == "default":
        matches = [r for r in rows if r.get("description") == name]
    if len(matches) != 1:
        raise ControlError("audio device missing or ambiguous; use 'list audio devices' and an exact name")
    return matches[0]


def amp_path():
    return os.environ.get("KILIX_AMP_SOCKET") or str(
        Path(os.environ["XDG_RUNTIME_DIR"]) / "kilix-amp.sock" if os.environ.get("XDG_RUNTIME_DIR")
        else Path.home() / ".local/gpu_terminal/kilix/session/kilix-amp.sock")


def voice_path():
    home = os.environ.get("GPU_TERMINAL_HOME", str(Path.home() / ".local/gpu_terminal"))
    storage = os.environ.get("KILIX_STORAGE_HOME", str(Path(home) / "kilix"))
    session = os.environ.get("KILIX_SESSION_HOME", str(Path(storage) / "session"))
    return str(Path(session) / "voice/control.sock")


def connect(path, kind):
    """Require a real, same-user socket and check the connected peer on Linux."""
    try:
        info = os.lstat(path)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ControlError("control endpoint is not a same-user socket")
        sock = socket.socket(socket.AF_UNIX, kind)
        try:
            sock.settimeout(5)
            sock.connect(path)
            if hasattr(socket, "SO_PEERCRED"):
                _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    raise ControlError("control socket peer belongs to another user")
            return sock
        except BaseException:
            sock.close()
            raise
    except OSError as error:
        raise ControlError(f"control socket unavailable: {error}") from error


def exchange(sock, payload, *, packet=False):
    sock.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
    data = bytearray()
    while len(data) <= 65536:
        chunk = sock.recv(65537 - len(data))
        if not chunk:
            raise ControlError("control socket closed without a complete reply")
        data.extend(chunk)
        if packet or b"\n" in data:
            break
    if len(data) > 65536 or (not packet and not data.endswith(b"\n")):
        raise ControlError("oversized or incomplete control reply")
    reply = decode(data)
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        detail = reply.get("error", "invalid reply") if isinstance(reply, dict) else "invalid reply"
        raise ControlError(f"control request rejected: {str(detail)[:1000]}")
    if not packet and (type(reply.get("protocol")) is not int or reply["protocol"] != 1):
        raise ControlError("unsupported Kilix-Amp protocol (expected 1)")
    return reply


def media_request(path, payload):
    try:
        with connect(path, socket.SOCK_STREAM) as sock:
            state = exchange(sock, {"protocol": 1, "cmd": "state"})
            return state if payload["cmd"] == "state" else exchange(sock, payload)
    except OSError as error:
        raise ControlError(f"Kilix-Amp request failed: {error}") from error


def prepare(c: Control, *, under_overlay=False) -> Step:
    if c.family == "audio":
        if c.operation == "devices":
            return Step(c, "list output devices and non-monitor microphone sources")
        row = resolve_device(c)
        identity = {"name": row["name"], "index": row["index"]}
        summary = f"{c.operation} {c.target} {row.get('description') or row['name']!r} ({row['name']!r})"
        argv = ()
        if c.operation in {"volume", "adjust"}:
            current = levels(row)
            wanted = ([round(int(c.value) * 65536 / 100)] * len(current) if c.operation == "volume"
                      else [max(0, min(65536, v + round(int(c.value) * 65536 / 100))) for v in current])
            argv = ("pactl", "set-" + c.target + "-volume", row["name"], *map(str, wanted))
            identity["channels"] = list(row["volume"])
            summary += " to channel levels " + ", ".join(f"{v * 100 / 65536:.1f}%" for v in wanted)
        elif c.operation == "mute":
            argv = ("pactl", "set-" + c.target + "-mute", row["name"], "1" if c.value else "0")
            summary = ("mute " if c.value else "unmute ") + summary.removeprefix("mute ")
        elif c.operation == "default":
            argv = ("pactl", "set-default-" + c.target, row["name"])
            summary += "; existing streams are not moved"
        elif c.operation != "status":
            raise ControlError("unsupported audio operation")
        return Step(c, summary, argv, device=identity)
    if c.family == "media":
        op = "state" if c.operation == "status" else c.operation
        payload = {"protocol": 1, "cmd": op}
        if op in {"volume", "seek", "shuffle", "repeat"}:
            payload[{"volume": "level", "seek": "pos", "shuffle": "on", "repeat": "mode"}[op]] = c.value
        path = amp_path()
        # Readiness/protocol check is read-only, including for --dry-run.
        media_request(path, {"protocol": 1, "cmd": "state"})
        return Step(c, f"music: {op}" + (f" {c.value}" if c.value is not None else ""),
                    socket_path=path, payload=payload)
    if c.family == "voice_setting":
        return Step(c, f"set voice {c.target} to {c.value}",
                    (kilix.KILIX, "settings", "--set", f"{c.target}={c.value}", "--print"))
    if c.family == "font":
        op = "show" if c.operation == "status" else c.operation
        argv = (kilix.KILIX, "screen-size", op)
        if c.value is not None:
            argv += (str(c.value),)
        return Step(c, f"{op} saved terminal font size" + (f" {c.value} points" if c.value else ""), argv)
    if c.family == "system":
        return Step(c, f"show {c.target} status", ("kilix-system", "--json", "--top", "5"))
    if c.family == "voice":
        if c.operation in {"stop-speech", "stop-dictation"}:
            return Step(c, c.operation.replace("-", " "), socket_path=voice_path(),
                        payload={"op": c.operation})
        if c.operation in {"status", "stop"}:
            return Step(c, "show voice status" if c.operation == "status" else "stop speech and dictation",
                        (kilix.KILIX, "voice", c.operation))
        if c.operation == "speak":
            return Step(c, f"speak {c.value!r}", (kilix.KILIX, "speak", "-"), data=str(c.value))
        if c.operation == "read":
            try:
                tree = kilix.snapshot(under_overlay=under_overlay)
                pane = tree.active_pane if tree.caller is not None else None
            except kilix.KilixError as error:
                raise ControlError(str(error)) from error
            if pane is None:
                raise ControlError("cannot identify the calling pane")
            return Step(c, f"read {c.value} of pane {pane['id']} aloud",
                        (kilix.KILIX, "speak", "--pane", str(pane["id"]), "--extent", str(c.value)))
        if c.operation == "dictate":
            return Step(c, f"record dictation for up to {c.value} seconds; return transcript without typing",
                        (kilix.KILIX, "dictate", "--seconds", str(c.value)), timeout=int(c.value) + 25)
    raise ControlError("unsupported control")


_SETTING_KEYS = {
    "wpm": "KILIX_VOICE_TTS_RATE", "read_extent": "KILIX_VOICE_TTS_EXTENT",
    "spoken_punctuation": "KILIX_VOICE_STT_PUNCTUATION", "stt_seconds": "KILIX_VOICE_STT_MAX_SECONDS",
    "stt_silence": "KILIX_VOICE_STT_SILENCE_MS", "stt_submit": "KILIX_VOICE_STT_SUBMIT",
}


def perform(step: Step):
    c = step.control
    if c.family == "audio":
        if c.operation == "devices":
            return {"outputs": [audio_state(r) for r in devices("sink")],
                    "inputs": [audio_state(r) for r in devices("source")]}
        def current():
            rows = [r for r in devices(c.target) if r["name"] == step.device["name"] and
                    r["index"] == step.device["index"]]
            if len(rows) != 1:
                raise ControlError("planned audio device disappeared or changed identity")
            return rows[0]
        row = current()
        if "channels" in step.device and list(row.get("volume", {})) != step.device["channels"]:
            raise ControlError("planned audio device changed channel layout")
        if step.argv:
            command(step.argv)
            row = current()
            if c.operation == "mute" and row.get("mute") is not c.value:
                raise ControlError("mute readback differs from requested state")
            if c.operation in {"volume", "adjust"}:
                actual = levels(row)
                if list(row["volume"]) != step.device["channels"] or len(actual) != len(step.argv[3:]) or any(
                        abs(a - int(b)) > 2 for a, b in zip(actual, step.argv[3:])):
                    raise ControlError("volume readback differs from requested state")
            if c.operation == "default" and command(("pactl", "get-default-" + c.target)) != row["name"]:
                raise ControlError("default device readback differs from requested state")
        return audio_state(row)
    if c.family == "media":
        reply = media_request(step.socket_path, step.payload)
        if c.operation in {"volume", "shuffle", "repeat"} and (
                type(reply.get(c.operation)) is not type(c.value) or reply[c.operation] != c.value):
            raise ControlError("music setting readback differs from requested state")
        return reply
    if c.family == "voice" and step.payload:
        try:
            with connect(step.socket_path, socket.SOCK_SEQPACKET) as sock:
                return exchange(sock, step.payload, packet=True)
        except OSError as error:
            raise ControlError(f"voice request failed: {error}") from error
    output = command(step.argv, data=step.data, timeout=step.timeout)
    if c.family == "system":
        result = decode(output)
        if not isinstance(result, dict) or type(result.get("schema_version")) is not int or result["schema_version"] != 1:
            raise ControlError("unsupported kilix-system JSON schema (expected 1)")
        if c.target == "system":
            return result
        if c.target not in result:
            raise ControlError(f"kilix-system omitted {c.target}")
        return {c.target: result[c.target]}
    if c.family == "voice_setting":
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        key = _SETTING_KEYS[c.target]
        if values.get(key) != str(c.value):
            raise ControlError("voice setting readback differs from requested value")
        return {key: values[key]}
    if c.family == "font":
        saved = command((kilix.KILIX, "screen-size", "show")) if c.mutates else output
        match = re.fullmatch(r"font_size (\d+(?:\.\d+)?)", saved)
        if not match or not 4 <= float(match[1]) <= 110:
            raise ControlError("invalid saved font size readback")
        if c.operation == "set" and float(match[1]) != c.value:
            raise ControlError("font size readback differs from requested state")
        return {"saved_font_size_points": float(match[1]), "note": "live resize is best effort"}
    return {"transcript" if c.operation == "dictate" else "response": output}
