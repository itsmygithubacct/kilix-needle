"""Decode LFM3's native tool syntax without executing model-generated code."""
import re

from commands_runtime import CommandModelError

_CALL = re.compile(r'<tool_call>\s*<function=([A-Za-z_][A-Za-z_0-9]*)>(.*?)</function>\s*</tool_call>', re.S)
_ARG = re.compile(r'<parameter=([A-Za-z_][A-Za-z_0-9]*)>\n?(.*?)\n?</parameter>', re.S)


def parse_calls(text):
    if not isinstance(text, str) or len(text.encode()) > 32768:
        raise CommandModelError('invalid or oversized model output')
    text = text.strip()
    if text.endswith('<|im_end|>'):
        text = text[:-len('<|im_end|>')].rstrip()
    # A template can open the thinking section before generation begins.
    if '</think>' in text:
        text = text.split('</think>', 1)[1].strip()
    if '<think>' in text:
        raise CommandModelError('unfinished reasoning output')
    calls = []
    remaining = text
    while remaining.startswith('<tool_call>'):
        match = _CALL.match(remaining)
        if match is None or len(calls) >= 4:
            raise CommandModelError('invalid native LFM3 tool call')
        arguments = {}
        body = match[2].strip()
        while body:
            parameter = _ARG.match(body)
            if parameter is None or parameter[1] in arguments:
                raise CommandModelError('invalid or duplicate LFM3 argument')
            value = parameter[2]
            if any(tag in value for tag in ('<parameter', '</parameter', '<tool_call', '</tool_call', '<function', '</function')):
                raise CommandModelError('nested LFM3 tool syntax')
            arguments[parameter[1]] = value
            body = body[parameter.end():].strip()
        calls.append({'name': match[1], 'arguments': arguments})
        remaining = remaining[match.end():].strip()
    if remaining and (calls or any(tag in remaining for tag in ('<tool', '</tool', '<function', '<parameter', '<|tool'))):
        raise CommandModelError('mixed or incomplete LFM3 tool output')
    # A complete natural-language response with no tool syntax is abstention.
    return calls
