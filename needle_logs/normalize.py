"""Lossless record identity and JSON pointer helpers."""
import hashlib
import json


def stable_id(prefix, *parts):
    data = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return prefix + hashlib.sha256(data).hexdigest()[:24]


def pointer_value(value, pointer):
    """Resolve an RFC 6901 pointer for replay verification."""
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("invalid JSON pointer")
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def display_text(value):
    """Escape terminal controls for human output; evidence keeps original text."""
    escaped = []
    for char in value:
        point = ord(char)
        if char in "\n\t":
            escaped.append(char)
        elif point < 32 or 0x7f <= point <= 0x9f or point in range(0x202a, 0x202f) or point in range(0x2066, 0x206a):
            escaped.append(f"\\u{point:04x}")
        else:
            escaped.append(char)
    return "".join(escaped)
