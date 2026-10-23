"""Decode raw requests without discarding repeated object members."""
import json

MAX_DEPTH = 128


def check_depth(raw):
    """Bound text nesting before decoding, without building another object tree."""
    if not isinstance(raw, str):
        return  # Let json.loads report other invalid input types.
    depth = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError("JSON depth exceeds 128 levels")
        elif char in "]}":
            depth -= 1


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def loads(raw):
    if isinstance(raw, (bytes, bytearray)):
        # Decode once, just as json.loads does; apply the same bound to encodings.
        raw = raw.decode(json.detect_encoding(raw), "surrogatepass")
    check_depth(raw)
    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except RecursionError:
        raise ValueError("JSON depth exceeds decoder limit") from None
