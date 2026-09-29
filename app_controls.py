"""Exact, model-independent apps controls. One complete request, one typed action.

These are deliberately separate from the five tools the selected apps model
was trained on. No model-produced argument or shell command enters this route.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re


@dataclass(frozen=True)
class Control:
    family: str
    operation: str
    target: str = ""
    value: str | int | bool | None = None

    def __post_init__(self):
        operations = {
            "audio": {"devices", "status", "volume", "adjust", "mute", "default"},
            "media": {"status", "play", "pause", "stop", "next", "previous", "volume", "seek", "shuffle", "repeat"},
            "voice": {"status", "speak", "read", "stop", "stop-speech", "stop-dictation", "dictate"},
            "voice_setting": {"set"}, "font": {"status", "set", "larger", "smaller", "reset"},
            "system": {"status"},
        }
        if self.operation not in operations.get(self.family, set()):
            raise ValueError("unknown control operation")
        if self.family == "audio" and self.operation != "devices" and self.target not in {"sink", "source"}:
            raise ValueError("audio needs an input or output target")
        if self.family == "system" and self.target not in {"system", "cpu", "memory", "disks", "network"}:
            raise ValueError("unknown system query")
        bounds = {
            ("audio", "volume"): (0, 100), ("audio", "adjust"): (-100, 100),
            ("media", "volume"): (0, 100), ("media", "seek"): (0, 86400),
            ("media", "repeat"): (0, 2), ("voice", "dictate"): (1, 120),
            ("font", "set"): (4, 110), ("font", "larger"): (1, 20), ("font", "smaller"): (1, 20),
        }
        if limits := bounds.get((self.family, self.operation)):
            if type(self.value) is not int or not limits[0] <= self.value <= limits[1]:
                raise ValueError("control value outside allowed bounds")
        elif (self.family, self.operation) in {("audio", "mute"), ("media", "shuffle")}:
            if type(self.value) is not bool:
                raise ValueError("control needs an explicit boolean")
        elif (self.family, self.operation) in {("audio", "default"), ("voice", "speak")}:
            if not isinstance(self.value, str) or not self.value.strip() or len(self.value) > 2048:
                raise ValueError("control needs nonempty literal text")
        elif self.family == "voice" and self.operation == "read":
            if self.value not in {"screen", "selection", "scrollback"}:
                raise ValueError("unknown read extent")
        elif self.family == "voice_setting":
            choices = {"wpm": {"120", "150", "170", "200", "240"},
                       "read_extent": {"screen", "selection", "scrollback"},
                       "spoken_punctuation": {"on", "off"}, "stt_seconds": {"15", "30", "60", "120"},
                       "stt_silence": {"500", "900", "1500"}, "stt_submit": {"never", "confirm"}}
            if self.value not in choices.get(self.target, set()):
                raise ValueError("unknown voice setting or value")
        elif self.value is not None:
            raise ValueError("unexpected control value")

    @property
    def mutates(self) -> bool:
        return self.operation not in {"status", "devices"}


_OUTPUT = r"(?:system|speaker|speakers|output)"
_INPUT = r"(?:mic|microphone|input)"
_AUDIO = rf"(?:{_OUTPUT}|{_INPUT})"
_LITERAL = r'"([^"\x00-\x1f\x7f-\x9f]+)"'


def parse(request: str) -> Control | None:
    """Abstain on questions about actions, compounds, qualifiers and bad bounds.

    Quoted device names and spoken text retain their exact case and punctuation.
    Courtesy is limited to an optional leading 'please' and final period.
    """
    if not isinstance(request, str) or len(request) > 2048 or re.search(
            r"[\x00-\x1f\x7f-\x9f]", request):
        return None
    text = re.sub(r"^please +", "", request.strip(), flags=re.I).removesuffix(".")

    def match(pattern):
        return re.fullmatch(pattern, text, flags=re.I | re.ASCII)

    def target(word):
        return "source" if re.fullmatch(_INPUT, word, re.I) else "sink"

    if match(r"(?:show|list) audio devices"):
        return Control("audio", "devices")
    if m := match(rf"(?:show|check) (?:the )?({_AUDIO}) volume"):
        return Control("audio", "status", target(m[1]))
    if m := match(rf"(?:show|check) (?:the )?((?:speaker|speakers|output)|{_INPUT}) status"):
        return Control("audio", "status", target(m[1]))
    if m := match(rf"(?:mute|unmute) (?:the |my )?({_AUDIO})(?: audio)?"):
        return Control("audio", "mute", target(m[1]), text.lower().startswith("mute"))
    if m := match(rf"set (?:the )?({_AUDIO}) volume to (\d{{1,3}})(?:%| percent)?"):
        if int(m[2]) <= 100:
            return Control("audio", "volume", target(m[1]), int(m[2]))
    if m := match(rf"(increase|decrease) (?:the )?({_AUDIO}) volume by (\d{{1,3}})(?:%| percentage points)"):
        if 1 <= int(m[3]) <= 100:
            return Control("audio", "adjust", target(m[2]),
                           int(m[3]) * (1 if m[1].lower() == "increase" else -1))
    if m := match(rf"set (?:the )?default (output|input) to {_LITERAL}"):
        return Control("audio", "default", target(m[1]), m[2])

    if match(r"(?:show|check) (?:music|player) status"):
        return Control("media", "status")
    if m := match(r"(play|resume|pause|stop) (?:the )?music"):
        return Control("media", "play" if m[1].lower() == "resume" else m[1].lower())
    if m := match(r"(?:play )?(next|previous) track"):
        return Control("media", m[1].lower())
    if m := match(r"set (?:music|player) volume to (\d{1,3})(?:%| percent)?"):
        if int(m[1]) <= 100:
            return Control("media", "volume", value=int(m[1]))
    if m := match(r"seek music to (\d{1,6}) seconds"):
        if int(m[1]) <= 86400:
            return Control("media", "seek", value=int(m[1]))
    if m := match(r"(?:set |turn )?(?:music )?shuffle (on|off)"):
        return Control("media", "shuffle", value=m[1].lower() == "on")
    if m := match(r"set (?:music )?repeat to (off|all|track)"):
        return Control("media", "repeat", value={"off": 0, "all": 1, "track": 2}[m[1].lower()])

    if match(r"(?:show|check) voice status"):
        return Control("voice", "status")
    if m := match(rf"(?:speak|say) {_LITERAL}"):
        if m[1].strip():
            return Control("voice", "speak", value=m[1])
    if m := match(r"read (?:the |this )?(selection|screen|scrollback|pane) aloud"):
        return Control("voice", "read", value="screen" if m[1].lower() == "pane" else m[1].lower())
    if m := match(r"stop (speaking|speech|dictation|voice)"):
        return Control("voice", {"speaking": "stop-speech", "speech": "stop-speech",
                                  "dictation": "stop-dictation", "voice": "stop"}[m[1].lower()])
    if m := match(r"(?:start )?dictation for (\d{1,3}) seconds"):
        if 1 <= int(m[1]) <= 120:
            return Control("voice", "dictate", value=int(m[1]))

    settings = (
        (r"set speech rate to (120|150|170|200|240)(?: wpm| words per minute)?", "wpm"),
        (r"set read extent to (screen|scrollback|selection)", "read_extent"),
        (r"(?:set |turn )spoken punctuation (on|off)", "spoken_punctuation"),
        (r"set dictation duration to (15|30|60|120) seconds", "stt_seconds"),
        (r"set dictation silence to (500|900|1500) milliseconds", "stt_silence"),
        (r"set dictation submit to (never|confirm)", "stt_submit"),
    )
    for pattern, key in settings:
        if m := match(pattern):
            return Control("voice_setting", "set", key, m[1].lower())

    # The terminal's text size, however agents name it ("set Kilix's terminal text
    # size to 14 points", "change the terminal font size to 14 pt"; gpt-6-luna
    # apps benchmark: the CLI got 0/12 on "set text size to N" before).
    size = (r"(?:the |kilix'?s? |kilix terminal'?s? )?(?:terminal'?s? )?(?:(?:text|font) size"
            r"(?: or (?:text|font) size)?|(?:text|font)size)")
    if match(rf"(?:show|check|get|what is) {size}"):
        return Control("font", "status")
    if m := match(rf"(?:set|change) {size} to (\d{{1,3}})(?: ?(?:points|point|pts|pt|px))?"):
        if 4 <= int(m[1]) <= 110:
            return Control("font", "set", value=int(m[1]))
    if m := match(r"make (?:the )?(?:terminal )?text (larger|bigger|smaller)(?: by (\d{1,2}) points)?"):
        if m[2] is None or 1 <= int(m[2]) <= 20:
            return Control("font", {"bigger": "larger"}.get(m[1].lower(), m[1].lower()), value=int(m[2] or 2))
    if match(rf"reset {size}"):
        return Control("font", "reset")
    if m := match(r"(?:show|check) (system|cpu|memory|disk|network) (?:status|usage)"):
        return Control("system", "status", {"disk": "disks"}.get(m[1].lower(), m[1].lower()))
    if match(r"how much (?:ram|memory) is (?:free|available)\??"):
        return Control("system", "status", "memory")
    return None


def run(control: Control, request: str, options, confirm) -> dict:
    """Share CLI/MCP confirmation and record semantics without loading a model."""
    import app_controls_backend as backend
    record = {"request": request, "status": 0, "note": "", "items": []}
    item = {"kind": control.family, "args": asdict(control),
            "summary": f"{control.family}: {control.operation}"}
    record["items"].append(item)
    attempted = False
    try:
        step = backend.prepare(control, under_overlay=options.under_overlay)
        item["summary"] = step.summary
        item["plan"] = step.public()
        if options.dry_run:
            item["outcome"] = "would"
        elif control.mutates and not options.assume_yes and (
                options.agent or not confirm(f"  {step.summary}? [y/N] ")):
            item.update(outcome="skipped", reason="needs a person's yes (--yes or confirm_risky)")
            record["status"] = 1
        else:
            attempted = control.mutates
            item["result"] = backend.perform(step)
            item["outcome"] = "done"
    except (backend.ControlError, OSError) as error:
        item.update(outcome="failed" if attempted else "unresolved", reason=str(error))
        if attempted:
            item["reason"] += "; state may have changed; inspect before retrying"
        record["status"] = 1
    return record
