"""Deterministic proposals for the bounded Windows desktop language.

Parsing is all-or-nothing and the result still passes the same grounding
validator as a model proposal. A miss is None, never a shortened plan.

  open notepad then type "hello" then press ctrl+s
  focus the "Untitled - Notepad" window
  click the "Save" button in notepad
  set the "File name:" field to "notes.txt" then press enter
  wait for the "Save As" window to close for up to 10 seconds
"""
from __future__ import annotations

from decimal import Decimal
import re

import windows_job as job

_LIT = r"LITERAL\d+"
_UNIT = r"(?:ms|milliseconds?|s|secs?|seconds?)"
_CURRENT = re.compile(r"(?:it|this|that|them|(?:the|this|that|the same)\s+(?:window|app|application|program))", re.I)
_NOUNS = sorted(job.CONTROL_TYPES, key=len, reverse=True)
_NOUN = r"(?:" + "|".join(re.escape(n) for n in _NOUNS) + r")"
_STATES = {"appear": "present", "appears": "present", "exist": "present", "exists": "present",
           "present": "present", "show up": "present", "shows up": "present", "open": "present",
           "visible": "visible", "enabled": "enabled", "close": "closed", "closes": "closed",
           "closed": "closed", "disappear": "closed", "disappears": "closed", "gone": "closed"}


def _alias(words):
    words = re.sub(r"\s+", " ", words.strip().lower())
    words = re.sub(r"^the ", "", words)
    words = re.sub(r" (?:window|app|application|program)s?$", "", words)
    return words if words in job.APPS else None


def _window(fragment, lit):
    """Window reference -> {} (the sequence's window), {app?, title?}, or None."""
    text = re.sub(r"\s+", " ", (fragment or "").strip())
    if not text or _CURRENT.fullmatch(text):
        return {}
    m = re.fullmatch(r"(?:the )?(?:window )?(?:(?:titled|named|called|with (?:the )?title) )?(" + _LIT + r")(?: window)?", text, re.I)
    if m:
        value = lit(m[1])
        return {"app": value} if re.search(r"\.exe$", value, re.I) else {"title": value}
    m = re.fullmatch(r"(.+?) (?:window )?(?:titled|named|called|with (?:the )?title) (" + _LIT + r")", text, re.I)
    if m:
        base = _window(m[1], lit)
        if base is None or "title" in base or not base:
            return None
        return {**base, "title": lit(m[2])}
    alias = _alias(text)
    if alias:
        _program, owner, title = job.APPS[alias]
        return {"app": owner, **({"title": title} if title else {})}
    m = re.fullmatch(r"(?:the )?([\w.-]+\.exe)(?: window| app| application| program)?", text, re.I)
    if m:
        return {"app": m[1]}
    return None


def _control(fragment, lit):
    """Control reference [in WINDOW] -> (selector, window fragment) or None."""
    text = re.sub(r"\s+", " ", fragment.strip())
    win = None
    m = re.fullmatch(r"(.+?) (?:in|on|of|inside) (.+)", text, re.I)
    if m and _window(m[2], lit) is not None:
        text, win = m[1], m[2]
    text = re.sub(r"^(?:the|a|an) ", "", text, flags=re.I)
    selector = None
    m = re.fullmatch(r"(?:(" + _NOUN + r"|control|element) )?with (?:the )?automation[ -]?id (" + _LIT + r"|[\w.-]+)", text, re.I)
    if m:
        value = lit(m[2]) if re.fullmatch(_LIT, m[2]) else m[2]
        selector = {"automation_id": value}
        if m[1] and m[1].lower() not in ("control", "element"):
            selector["control_type"] = job.CONTROL_TYPES[m[1].lower()]
    if selector is None:
        m = re.fullmatch(r"(" + _LIT + r")(?: (" + _NOUN + r"))?", text, re.I) or \
            re.fullmatch(r"(" + _NOUN + r") (?:(?:named|called|labell?ed|titled) )?(" + _LIT + r")", text, re.I)
        if m:
            name, noun = (m[1], m[2]) if re.fullmatch(_LIT, m[1]) else (m[2], m[1])
            selector = {"name": lit(name)}
            if noun:
                selector["control_type"] = job.CONTROL_TYPES[noun.lower()]
    if selector is None:
        m = re.fullmatch(r"([A-Za-z0-9][\w'&:-]*(?: [A-Za-z0-9][\w'&:-]*){0,4}) (" + _NOUN + r")", text, re.I)
        if m and not re.search(_LIT, m[1]):
            selector = {"name": m[1], "control_type": job.CONTROL_TYPES[m[2].lower()]}
    return (selector, win) if selector else None


def _with_window(args, fragment, lit):
    target = _window(fragment, lit)
    if target is None:
        return None
    return {**args, **target}


def _ms(number, unit):
    value = Decimal(number) * (1 if unit.lower().startswith("m") else 1000)
    return int(value) if value == int(value) else None


def _clause(clause, literals):
    def lit(token):
        return literals[int(token[7:])]
    c = clause.strip()
    m = re.fullmatch(r"(?:open|launch|start|run) (?:the )?(.+?)(?: (?:with|and open|to open|for) (?:the )?(?:file )?(" + _LIT + r"))?", c, re.I)
    if m:
        target = m[1]
        if re.fullmatch(_LIT, target):
            program = lit(target)
        elif _alias(target):
            program = _alias(target)
        elif re.fullmatch(r"[\w.-]+\.exe(?: (?:app|application|program))?", target, re.I):
            program = target.split()[0]
        else:
            return None
        args = {"program": program}
        if m[2]:
            args["document"] = lit(m[2])
        return {"name": "launch", "arguments": args}
    if re.fullmatch(r"(?:list|show)(?: me)?(?: all)?(?: (?:the|my))?(?: open)? windows", c, re.I):
        return {"name": "window", "arguments": {"action": "list"}}
    m = re.fullmatch(r"(?:list|show|inspect)(?: me)?(?: all)?(?: the)?(?: (?:controls|elements|ui elements|ui))?(?: (?:in|of|on|for) (.+))?", c, re.I)
    if m and (m[1] or re.search(r"\b(?:controls|elements|ui)\b", c, re.I)):
        args = _with_window({"action": "inspect"}, m[1], lit)
        return {"name": "control", "arguments": args} if args is not None else None
    m = re.fullmatch(r"inspect (.+)", c, re.I)
    if m:
        args = _with_window({"action": "inspect"}, m[1], lit)
        return {"name": "control", "arguments": args} if args else None
    m = re.fullmatch(r"(?:find|locate|look for) (.+)", c, re.I)
    if m:
        target = _window(m[1], lit)
        if target:
            return {"name": "window", "arguments": {"action": "find", **target}}
        control = _control(m[1], lit)
        if control:
            args = _with_window({"action": "find", **control[0]}, control[1], lit)
            return {"name": "control", "arguments": args} if args is not None else None
        return None
    m = re.fullmatch(r"(?:focus(?: on)?|activate|switch to|bring up) (.+)", c, re.I) or \
        re.fullmatch(r"bring (.+?) to (?:the )?front", c, re.I)
    if m:
        args = _with_window({"action": "focus"}, m[1], lit)
        return {"name": "window", "arguments": args} if args is not None else None
    m = re.fullmatch(r"(minimi[sz]e|maximi[sz]e|restore|close) (.+)", c, re.I)
    if m:
        action = {"minimise": "minimize", "maximise": "maximize"}.get(m[1].lower(), m[1].lower())
        args = _with_window({"action": action}, m[2], lit)
        return {"name": "window", "arguments": args} if args is not None else None
    m = re.fullmatch(r"move (.+?) to (?:position )?(-?\d+)\s*,?\s*(-?\d+)(?: pixels)?", c, re.I)
    if m:
        args = _with_window({"action": "move", "x": int(m[2]), "y": int(m[3])}, m[1], lit)
        return {"name": "window", "arguments": args} if args is not None else None
    m = re.fullmatch(r"resize (.+?) to (\d+)\s*(?:x|×|by|,)\s*(\d+)(?: pixels)?", c, re.I)
    if m:
        args = _with_window({"action": "resize", "width": int(m[2]), "height": int(m[3])}, m[1], lit)
        return {"name": "window", "arguments": args} if args is not None else None
    m = re.fullmatch(r"(?:type|enter|write|insert) (?:the )?(?:text )?(" + _LIT + r")(?: (?:in|into|on) (.+))?", c, re.I)
    if m:
        text = lit(m[1])
        if m[2]:
            target = _window(m[2], lit)
            if target is not None:
                return {"name": "keyboard", "arguments": {"action": "type", "text": text, **target}}
            control = _control(m[2], lit)
            if control:
                args = _with_window({"action": "set_value", **control[0], "text": text}, control[1], lit)
                return {"name": "control", "arguments": args} if args is not None else None
            return None
        return {"name": "keyboard", "arguments": {"action": "type", "text": text}}
    m = re.fullmatch(r"(?:set|fill(?: in)?) (.+?) (?:to|with) (" + _LIT + r")(?: (?:in|on) (.+))?", c, re.I)
    if m:
        control = _control(m[1], lit)
        if not control or control[1] and m[3]:
            return None
        args = _with_window({"action": "set_value", **control[0], "text": lit(m[2])}, control[1] or m[3], lit)
        return {"name": "control", "arguments": args} if args is not None else None
    m = re.fullmatch(r"(?:press|hit|tap) (.+?)(?: (?:in|on) (.+))?", c, re.I)
    if m:
        keys = re.sub(r"\b(?:the|key|keys|chord|shortcut|together)\b", "", m[1], flags=re.I).strip()
        if re.fullmatch(_LIT, keys):
            keys = lit(keys)
        try:
            chord = job.key_chord(keys)
        except job.LauncherChordError:
            raise
        except job.WindowsActionError:
            chord = None
        if chord:
            args = _with_window({"action": "press", "keys": chord}, m[2], lit)
            return {"name": "keyboard", "arguments": args} if args is not None else None
    m = re.fullmatch(r"(double[ -]click|right[ -]click|click)(?: on)? (.+)", c, re.I)
    if m:
        verb, rest = m[1].lower(), m[2]
        point = re.fullmatch(r"(?:at )?(?:position )?(\d+)\s*,?\s*(\d+)(?: (?:in|on|of) (.+))?", rest, re.I)
        if point:
            args = {"action": "double_click" if verb.startswith("double") else "click",
                    "x": int(point[1]), "y": int(point[2]), "button": "right" if verb.startswith("right") else "left"}
            args = _with_window(args, point[3], lit)
            return {"name": "pointer", "arguments": args} if args is not None else None
        if verb != "click":
            return None
    m = re.fullmatch(r"(?:click(?: on)?|press|invoke|hit|tap) (.+)", c, re.I)
    if m:
        control = _control(m[1], lit)
        if not control:
            return None
        args = _with_window({"action": "invoke", **control[0]}, control[1], lit)
        return {"name": "control", "arguments": args} if args is not None else None
    m = re.fullmatch(r"scroll (up|down)(?: (?:by )?(\d+)(?: (?:steps?|times|notches?|clicks?|lines?))?)?(?: (?:in|on) (.+))?", c, re.I)
    if m:
        args = {"direction": m[1].lower(), "steps": int(m[2]) if m[2] else 3}
        args = _with_window(args, m[3], lit)
        return {"name": "scroll", "arguments": args} if args is not None else None
    m = re.fullmatch(r"(?:wait|pause)(?: for)? (\d+(?:\.\d+)?) ?(" + _UNIT + r")", c, re.I)
    if m:
        ms = _ms(m[1], m[2])
        return {"name": "wait", "arguments": {"for": "time", "ms": ms}} if ms else None
    states = "|".join(sorted((re.escape(s) for s in _STATES), key=len, reverse=True))
    m = re.fullmatch(r"wait (?:for|until) (.+?) (?:to )?(?:is |be |becomes? |to be )?(" + states + r")"
                     r"(?: (?:for )?(?:up to|at most) (\d+(?:\.\d+)?) ?(" + _UNIT + r"))?", c, re.I)
    if m:
        state = _STATES[m[2].lower()]
        args = {"state": state}
        if m[3]:
            ms = _ms(m[3], m[4])
            if not ms:
                return None
            args["ms"] = ms
        target = _window(m[1], lit)
        if target is not None:
            return {"name": "wait", "arguments": {"for": "window", **args, **target}}
        control = _control(m[1], lit)
        if control:
            args = _with_window({"for": "control", **args, **control[0]}, control[1], lit)
            return {"name": "wait", "arguments": args} if args is not None else None
    return None


def parse(request, reasons=None):
    """Validated actions, or None. A refusal of parsed calls is appended to reasons."""
    # Eligibility failures (conditions, negation, too many steps) are terminal.
    clauses = job.request_clauses(request)
    try:
        calls = [_clause(clause, literals) for clause, literals in clauses]
        if any(call is None for call in calls):
            return None
        return job.validate(request, calls)
    except job.WindowsActionError as error:
        if reasons is not None:
            reasons.append(str(error))
        return None
