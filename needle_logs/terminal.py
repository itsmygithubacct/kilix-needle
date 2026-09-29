"""Conservative raw terminal view: only committed plain lines are exported."""


def plain_lines(data: bytes):
    """Return (lines, gaps). Any terminal control makes replay unsupported.

    This deliberately does not claim to reconstruct cursor redraws or authority.
    """
    if any(byte < 32 and byte not in (9, 10) for byte in data) or 127 in data:
        return [], [{"code": "terminal_controls", "byte_start": 0, "byte_end": len(data),
                     "message": "terminal controls require replay; raw text omitted"}]
    lines = []
    offset = 0
    for raw in data.splitlines(keepends=True):
        end = offset + len(raw)
        if not raw.endswith(b"\n"):
            return lines, [{"code": "partial_eof", "byte_start": offset,
                            "byte_end": len(data), "message": "unfinished raw line; suffix omitted"}]
        try:
            text = raw[:-1].decode("utf-8")
        except UnicodeDecodeError:
            return lines, [{"code": "invalid_utf8", "byte_start": offset,
                            "byte_end": len(data), "message": "invalid UTF-8 raw line; suffix omitted"}]
        if any(0x7f <= ord(char) <= 0x9f or 0x202a <= ord(char) <= 0x202e or 0x2066 <= ord(char) <= 0x2069 for char in text):
            return lines, [{"code": "terminal_controls", "byte_start": offset,
                            "byte_end": len(data), "message": "Unicode terminal controls require replay; suffix omitted"}]
        lines.append((offset, end, text))
        offset = end
    return lines, []
