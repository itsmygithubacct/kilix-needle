"""Changes to the machine: four tools, read by checks that consume the whole request.

The admin job controls one systemd service, signals one process, installs or
removes packages, and refreshes or applies package updates (evals/admin/SPEC.md).
Every one of those is a change, so the reading here is strict on purpose:

- `parse` reads the whole request into actions or into nothing. Every word must
  be a verb it knows, a filler it knows, or the target. A word it does not know
  ends the reading with a reason, never with a guess.
- A target is a name the request spells out: a unit, a package, a program or a
  PID. "it", "that service", "whatever uses the memory" and "all services" are
  not names.
- Negation, alternatives, conditions, schedules, reported or hypothetical
  wishes, questions, other machines and shell syntax refuse the whole request.
- `interpret` admits a proposer's calls (a bundle's or a model's) only when they
  equal that reading, in order. A proposer never decides what runs.

`policy` says which actions an agent may confirm in advance and which need a
person at a terminal, and refuses outright the ones that would cut a session or
a remote login (protected units and programs, PID 1). This module only reads
text; admin_exec.py runs things.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from actions import Refusal


def _enum(*values):
    return {"type": "string", "enum": list(values)}


SERVICE_OPS = ("start", "stop", "restart", "reload", "enable", "disable")
SIGNALS = ("term", "kill", "hup")
PACKAGE_OPS = ("install", "remove", "purge")
UPDATE_OPS = ("refresh", "upgrade")

TOOLS = [
    {"name": "service", "description": "Start, stop, restart, reload, enable or disable one systemd service",
     "parameters": {"type": "object", "properties": {
         "operation": _enum(*SERVICE_OPS),
         "unit": {"type": "string", "description": "one exact service name, e.g. nginx"},
         "scope": _enum("system", "user")},
         "required": ["operation", "unit"], "additionalProperties": False}},
    {"name": "process", "description": "Send a signal to one process, by PID or by exact program name",
     "parameters": {"type": "object", "properties": {
         "signal": _enum(*SIGNALS),
         "pid": {"type": "integer", "minimum": 2, "maximum": 4194304},
         "name": {"type": "string", "description": "one exact program name, e.g. firefox"}},
         "required": [], "additionalProperties": False}},
    {"name": "packages", "description": "Install, remove or purge one Debian package",
     "parameters": {"type": "object", "properties": {
         "operation": _enum(*PACKAGE_OPS),
         "package": {"type": "string", "description": "one exact Debian package name"}},
         "required": ["operation", "package"], "additionalProperties": False}},
    {"name": "updates", "description": "Refresh the package lists (apt update) or upgrade installed packages (apt upgrade)",
     "parameters": {"type": "object", "properties": {"operation": _enum(*UPDATE_OPS)},
                    "required": ["operation"], "additionalProperties": False}},
]
DEFAULTS = {"service": {"scope": "system"}, "process": {"signal": "term"}, "packages": {}, "updates": {}}
MAX_ACTIONS = 5
MAX_REQUEST = 1024

UNIT = r"[A-Za-z0-9][A-Za-z0-9_.@:+-]{0,126}"
PACKAGE = r"[a-z0-9][a-z0-9+.-]{1,126}"
PROGRAM = r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}"
PID = r"[1-9][0-9]{0,6}"

# Words that look like a name but never name one thing.
GENERIC = frozenset("""
    a an the my our your their his her its this that these those it them they one ones
    all any every everything anything something whatever each both other others
    service services unit units daemon daemons process processes program programs app apps
    application applications package packages pid pids server servers web webserver database db
    system machine computer box host everything stuff thing things same rest
    again too also now already still finally asap urgent urgently quick quickly please pls thanks
    frozen freezing stuck hung hanging broken crashed crashing dead down slow lagging laggy
    restarted started stopped reloaded enabled disabled installed removed uninstalled purged
    killed terminated ended closed quit bounced upgraded updated refreshed
    """.split())


@dataclass(frozen=True)
class Action:
    kind: str
    args: dict


class AdminError(ValueError):
    """A call that does not fit the four tools."""


def normalize(kind, args):
    """Strict schema check, shared by the checks and the runner. Units lose `.service`."""
    spec = next((t["parameters"] for t in TOOLS if t["name"] == kind), None)
    if spec is None or not isinstance(args, dict) or set(args) - set(spec["properties"]):
        raise AdminError("unknown admin tool or argument")
    if any(key not in args for key in spec["required"]):
        raise AdminError("missing required admin argument")
    result = {**DEFAULTS[kind], **args}
    for key, value in result.items():
        prop = spec["properties"][key]
        if prop["type"] == "integer":
            if type(value) is not int or not prop["minimum"] <= value <= prop["maximum"]:
                raise AdminError("process ID is out of range")
        elif not isinstance(value, str) or not value or len(value) > 128:
            raise AdminError("admin argument must be a bounded string")
        if "enum" in prop and value not in prop["enum"]:
            raise AdminError("unsupported admin argument")
    if kind == "service":
        unit = result["unit"]
        if unit.endswith(".service"):
            unit = unit[:-len(".service")]
        if not re.fullmatch(UNIT, unit) or unit.endswith(".") or unit.lower() in GENERIC:
            raise AdminError("name one exact service")
        result["unit"] = unit
    if kind == "process":
        if ("pid" in result) == ("name" in result):
            raise AdminError("give one process ID or one exact program name")
        if "name" in result and (not re.fullmatch(PROGRAM, result["name"])
                                 or result["name"].lower() in GENERIC or result["name"].isdigit()):
            raise AdminError("name one exact program")
    if kind == "packages":
        if not re.fullmatch(PACKAGE, result["package"]) or result["package"] in GENERIC:
            raise AdminError("name one exact Debian package")
    return result


def action(kind, **args):
    return Action(kind, normalize(kind, args))


# ---------------------------------------------------------------- policy

# Stopping or disabling these can cut a remote login or the network.
_KEEP_RUNNING = frozenset({"ssh", "sshd", "NetworkManager", "networking", "systemd-networkd",
                           "systemd-resolved", "wpa_supplicant"})
# Any change to these ends the desktop session or the machine's bookkeeping.
_UNTOUCHABLE_UNITS = frozenset({"dbus", "dbus-broker", "systemd-logind", "systemd-journald",
                                "systemd-udevd", "gdm", "gdm3", "lightdm", "sddm",
                                "display-manager", "polkit", "init"})
_UNTOUCHABLE_PREFIXES = ("getty@", "systemd-", "user@", "user-runtime-dir@")
_UNTOUCHABLE_PROGRAMS = frozenset({"systemd", "init", "sshd", "dbus-daemon", "dbus-broker",
                                   "Xorg", "Xwayland", "gnome-shell", "kwin_x11", "kwin_wayland",
                                   "systemd-logind", "systemd-journald", "polkitd", "login", "sudo",
                                   "kitty", "kilix", "kitty-pty-broker"})


def refused(act: Action) -> str | None:
    """Why this action is never run, or None."""
    a = act.args
    if act.kind == "service":
        unit = a["unit"]
        if unit in _UNTOUCHABLE_UNITS or (a["scope"] == "system" and unit.startswith(_UNTOUCHABLE_PREFIXES)):
            return f"{unit} keeps the session or the system running; it is not changed from here"
        if a["operation"] in ("stop", "disable") and unit in _KEEP_RUNNING:
            return f"stopping or disabling {unit} can cut remote access or the network"
    if act.kind == "process":
        if a.get("name") in _UNTOUCHABLE_PROGRAMS:
            return f"{a['name']} keeps the session running; it is not signalled from here"
    return None


def needs_person(act: Action) -> str | None:
    """Why only a person at a terminal may confirm this action, or None when an
    agent's advance yes (--yes, MCP confirm) is enough."""
    a = act.args
    if act.kind == "service" and a["operation"] in ("stop", "disable"):
        return f"{a['operation']} takes a service down"
    if act.kind == "process":
        if a["signal"] == "kill":
            return "a forced kill gives the program no chance to clean up"
        if "name" in a:
            return "a program name can match several processes"
    if act.kind == "packages" and a["operation"] in ("remove", "purge"):
        return "removing packages can take others with them"
    if act.kind == "updates" and a["operation"] == "upgrade":
        return "an upgrade changes many packages at once"
    return None


# ---------------------------------------------------------------- reading

_SHELL = re.compile(r"[;|&<>`$\\{}\[\]*]|\$\(")


def _words(pattern: str):
    """A word pattern whose edges are not inside a name: '-', '.', '@' and '+' join a name."""
    return re.compile(r"(?<![\w@+.-])(?:" + pattern + r")(?![\w@+-]|\.\w)")

_NEGATION = _words(r"(?:don'?t|do not|dont|doesn'?t|does not|didn'?t|never|not|no need|no longer|"
                       r"without|cancel|never ?mind|forget (?:it|that|about)|scratch that|actually|"
                       r"instead|but|except|unless|nope|wait)")
_CONDITION = _words(r"(?:if|when|whenever|once|after|before|until|till|while|in case|as soon as|"
                        r"tomorrow|tonight|later|soon|eventually|midnight|noon|every|daily|weekly|hourly|"
                        r"monthly|schedule[ds]?|scheduled|o'?clock|at \d|in \d+ ?(?:s|secs?|seconds?|m|mins?|"
                        r"minutes?|h|hrs?|hours?|days?|weeks?))")
_ALTERNATIVE = _words(r"(?:or|either|maybe|perhaps|possibly)")
_HYPOTHETICAL = _words(r"(?:should|shall|would|might|may|could|can|wants?|wanted|wanting|asked|asks|"
                           r"said|says|told|tells|suggest\w*|recommend\w*|probably|how to|is it|"
                           r"what happens|supposed|need to know|wonder\w*|thinking|planning|plan to|"
                           r"going to|gonna|let me|i'?ll|i will|we'?ll|we will)")
_QUESTION = re.compile(r"^(?:is|are|was|were|does|do|did|has|have|had|what|what's|whats|which|who|why|"
                       r"how|where|whether|am i|can i|could i)\b")
_REMOTE = _words(r"(?:on|at|in|from|to) (?:the |my |our |a |that |this )?(?:other |remote )?"
                     r"(?:server|host|box|vm|container|cluster|pod|node|prod|production|staging|"
                     r"web\d+|db\d+|remote)|ssh (?:into|to|onto)|remotely")
_OUT_OF_SCOPE = _words(
    r"(?:reboot|rebooting|restart (?:the |my |this )?(?:machine|computer|system|server|pc|laptop|box|os)|"
    r"shut ?down|shutdown|power ?off|poweroff|halt|suspend|hibernate|log ?out|logout|log off|"
    r"mount|unmount|umount|format|edit|modify|change|configure|chmod|chown|useradd|adduser|userdel|"
    r"deluser|usermod|passwd|passwords?|groups?|firewall|ufw|iptables|nft|cron ?jobs?|cronjobs?|crontabs?|"
    r"modprobe|rmmod|"
    r"insmod|dist-upgrade|full-upgrade|autoremove|autoclean|clean|cleanup|clear|delete|rm|move|copy|"
    r"rename|create|write|downgrade|hold|unhold|mask|unmask|snap|flatpak|pip|pip3|npm|cargo|gem|"
    r"kubectl|reinstall|setup|set up|ppa|add-apt-repository|sources\.list|logs?|journal|status|"
    r"show|list|check (?:if|whether|that)|tell me|why|containers?|images?|https?|deb|dpkg|"
    r"a repo|a repository|the repo|repositories|repository)")
_CAPITAL_QUESTION = re.compile(r"\?\s*\S")      # a '?' with words after it

_PREFIX = re.compile(r"^(?:(?:hey|hi|ok|okay|so|um|uh|yo|alright|right)\b[,.!]?\s+)*"
                     r"(?:(?:please|pls|plz|kindly|go ahead and|just|quickly|now|real quick|"
                     r"can you|could you|would you|will you|can u|could u|would you mind|"
                     r"i need you to|i want you to|i'?d like you to|need you to|time to)\b[,.]?\s+)*",
                     re.I)
_SUFFIX = re.compile(r"(?:[,\s]+(?:please|pls|plz|thanks|thank you|thx|ty|for me|right now|now|asap|"
                     r"quickly|real quick|again|immediately|for me please|if you can|cheers|"
                     r"(?:on|for) (?:this|the local) (?:machine|box|computer|host|laptop|pc)|locally|here))+"
                     r"[\s.!?]*$|[\s.!?]+$", re.I)
_FRAMING = re.compile(r"(?:^|\s)(?:on (?:this|the local) (?:machine|box|computer|host|laptop|pc|system)|"
                      r"locally|here)(?=[\s,]|$),?", re.I)
_PURGE = re.compile(r"\b(?:remove|uninstall)\s+(\S+)\s+(?:and|with|including|plus|along with)\s+"
                    r"(?:all\s+)?(?:its|it's|their|the|his|her)?\s*(?:config(?:uration)?s?(?:\s+files)?|"
                    r"settings|conf files)\b|\bcompletely\s+(?:remove|uninstall)\s+(\S+)|"
                    r"\b(?:remove|uninstall)\s+(\S+)\s+completely\b", re.I)
_SPLIT = re.compile(r"\s*,\s*(?:and\s+)?(?:then\s+)?|\s+and\s+then\s+|\s+then\s+|\s+and\s+(?:also\s+)?|"
                    r"\s+(?:also|plus)\s+")
_SPLIT_KEEP = re.compile("(" + _SPLIT.pattern + ")")

_SERVICE_VERBS = {"start": "start", "stop": "stop", "restart": "restart", "bounce": "restart",
                  "reload": "reload", "enable": "enable", "disable": "disable"}
_PROCESS_VERBS = {"kill", "terminate", "end", "close", "quit", "pkill", "killall"}
_PACKAGE_VERBS = {"install": "install", "remove": "remove", "uninstall": "remove", "purge": "purge"}
_FILLER = frozenset("the a an my named called service unit daemon".split())
_PROCESS_FILLER = frozenset("the a an my named called process program app application pid with".split())
_PACKAGE_FILLER = frozenset("the a an named called package packages".split())
_SIGNAL_WORDS = {"sigkill": "kill", "sigterm": "term", "sighup": "hup"}
_KILL_OPTIONS = {"-9": "kill", "-term": "term", "-hup": "hup"}      # SPEC's typed kill form
_SEND_SIGNALS = {"sigkill": "kill", "kill": "kill", "sigterm": "term", "term": "term",
                 "sighup": "hup", "hup": "hup"}

_REFRESH = re.compile(r"(?:update|refresh|reload|resync|sync) (?:the )?(?:apt|package|packages|apt package)?\s*"
                      r"(?:lists?|index|indexes|sources|cache|database|metadata|repos)|"
                      r"(?:update|refresh) apt|check for (?:new )?updates|"
                      r"(?:sudo )?apt(?:-get)? update")
_UPGRADE = re.compile(r"upgrade|upgrade (?:all )?(?:the |my )?(?:installed )?(?:packages|software)|"
                      r"upgrade (?:everything|the system|the os|my system)|"
                      r"(?:install|apply) (?:all )?(?:the |any )?(?:available |pending |outstanding )?"
                      r"(?:updates|upgrades|package updates|system updates)|"
                      r"update (?:all )?(?:the |my )?(?:installed )?(?:packages|software)|"
                      r"update (?:everything|the system|my system|the os)|"
                      r"(?:sudo )?apt(?:-get)? upgrade")


_TYPOS = {"teh": "the", "pleas": "please", "plese": "please", "pleae": "please", "cna": "can",
          "coudl": "could", "yuo": "you", "thnaks": "thanks", "thansk": "thanks", "thnx": "thanks",
          "servcie": "service", "sevice": "service", "proccess": "process", "porcess": "process",
          "pacakge": "package", "packge": "package"}
_PARTICIPLES = {"restarted": "restart", "started": "start", "stopped": "stop", "reloaded": "reload",
                "enabled": "enable", "disabled": "disable", "installed": "install", "removed": "remove",
                "uninstalled": "uninstall", "purged": "purge", "killed": "kill", "terminated": "terminate"}
_REWRITES = (
    # get|have NAME restarted -> restart NAME
    (re.compile(r"^(?:get|have) (\S+(?: (?:user )?service)?) (" + "|".join(_PARTICIPLES) + r")$", re.I),
     lambda m: f"{_PARTICIPLES[m.group(2).lower()]} {m.group(1)}"),
    # give NAME a [quick|fresh] restart|reload|bounce
    (re.compile(r"^give (?:the )?(\S+(?: service)?) a (?:quick |fresh |good )?(restart|reload|bounce)$", re.I),
     lambda m: f"{m.group(2)} {m.group(1)}"),
    # make|have|set NAME (to) start|come up|run at|on boot -> enable NAME
    (re.compile(r"^(?:make|have|set) (\S+(?: service)?) (?:to )?(?:start|come up|run|launch)(?: automatically)? "
                r"(?:at|on) (?:boot|startup)$", re.I), lambda m: f"enable {m.group(1)}"),
    # enable|disable NAME at|on boot|startup
    (re.compile(r"^(enable|disable) (\S+(?: (?:user )?service)?) (?:at|on) (?:boot|startup)$", re.I),
     lambda m: f"{m.group(1)} {m.group(2)}"),
    # hang up [process|pid] N
    (re.compile(r"^hang up (?:process |pid )?(\d+)$", re.I), lambda m: f"send hup to process {m.group(1)}"),
    # SIGKILL|SIGTERM|SIGHUP [process|pid] N
    (re.compile(r"^(sigkill|sigterm|sighup) (?:process |pid )?(\d+)$", re.I),
     lambda m: f"send {m.group(1)} to process {m.group(2)}"),
)
_RE_REFRESH = re.compile(r"(?:re-?read|pull|fetch|download|get) (?:the )?(?:fresh |new |latest |the latest )?"
                         r"(?:apt |package )(?:lists?|index|indexes)(?: from the mirrors)?", re.I)


def _rewrite(clause: str) -> str:
    clause = re.sub(r"^start up\b", "start", clause, flags=re.I)
    for pattern, replace in _REWRITES:
        m = pattern.fullmatch(clause)
        if m:
            return replace(m)
    return clause


def _strip(word: str) -> str:
    return word.strip("\"'").rstrip(".,!")


def _service_segment(words, low):
    """VERB [the] [my] [user] NAME [.service] [user] [service|unit|daemon] [for|as [my|the] user]."""
    scope = "system"
    keep = []
    i = 0
    while i < len(words):
        w = low[i]
        if w in ("for", "as") and (low[i + 1:i + 2] == ["user"]
                                   or low[i + 1:i + 3] in (["my", "user"], ["the", "user"], ["a", "user"])):
            scope = "user"
            i += 2 if low[i + 1] == "user" else 3
            continue
        if w in ("user", "--user", "user-level"):
            scope = "user"
        elif w not in _FILLER:
            keep.append(words[i])
        i += 1
    return keep, scope


def _segment(text: str):
    """One clause: ("service"|"process"|"packages"|"updates"|"verb"|"names", payload) or (None, reason)."""
    text = _rewrite(text)
    low_text = text.lower()
    if _REFRESH.fullmatch(low_text) or _RE_REFRESH.fullmatch(low_text):
        return "updates", [("refresh", None)]
    if _UPGRADE.fullmatch(low_text):
        return "updates", [("upgrade", None)]
    text = re.sub(r"\b(force|hard)[- ]kill\b", r"\1 kill", text, flags=re.I)
    words = [_strip(w) for w in text.split()]
    words = [w for w in words if w]
    low = [w.lower() for w in words]
    if not words:
        return None, "an empty clause"
    if low[0] == "sudo":
        words, low = words[1:], low[1:]
        if not words:
            return None, "an empty clause"
    # systemctl [--user] VERB NAME...
    if low[0] == "systemctl":
        rest, rlow = words[1:], low[1:]
        scope = "system"
        if rlow[:1] == ["--user"]:
            scope, rest, rlow = "user", rest[1:], rlow[1:]
        if not rest or rlow[0] not in SERVICE_OPS or len(rest) != 2:
            return None, "systemctl takes one of start, stop, restart, reload, enable, disable and one name"
        return "service", [(rlow[0], rest[1], scope)]
    # apt / apt-get
    if low[0] in ("apt", "apt-get"):
        if len(low) >= 3 and low[1] in PACKAGE_OPS:
            return "packages", [(low[1], name) for name in words[2:]]
        return None, "apt takes update, upgrade, or install, remove or purge with package names"
    # kill [-SIG] PID | kill -s SIG PID | pkill/killall [-SIG] NAME
    signal = "term"
    verb_at = None
    for i, w in enumerate(low):
        if w in _SERVICE_VERBS or w in _PROCESS_VERBS or w in _PACKAGE_VERBS or w == "send":
            verb_at = i
            break
    if verb_at is None:
        if low[0] in ("update", "upgrade"):
            return None, ("updates apply to every package: say 'update the package lists' or "
                          "'upgrade packages'; one package alone is not upgraded from here")
        # No verb: a list continuing the previous clause ("install curl, jq and htop").
        names = [w for w, lw in zip(words, low) if lw not in _FILLER | _PACKAGE_FILLER]
        if len(names) == 1:
            return "names", names
        return None, ("words the checks do not read: a list item is one name"
                      if names else "a clause without a verb")
    head = low[:verb_at]
    verb = low[verb_at]
    tail, tlow = words[verb_at + 1:], low[verb_at + 1:]
    if verb in _SERVICE_VERBS and head == []:
        if tlow[:1] in (["process"], ["pid"]) or (len(tlow) == 1 and tlow[0].isdigit()):
            # "stop process 4312": a process, by the SPEC.
            if verb != "stop":
                return None, f"'{verb}' acts on services; name a service"
            verb = "kill"
        else:
            keep, scope = _service_segment(tail, tlow)
            if not keep:
                return "verb", [_SERVICE_VERBS[verb]]
            if len(keep) > 1:
                return None, "one service per clause; join several with 'and'"
            return "service", [(_SERVICE_VERBS[verb], keep[0], scope)]
    if verb in _PACKAGE_VERBS and head == []:
        names = [w for w, lw in zip(tail, tlow) if lw not in _PACKAGE_FILLER]
        if not names:
            return None, "name the package"
        if len(names) > 1:
            return None, "one package per clause; join several with 'and' or commas"
        return "packages", [(_PACKAGE_VERBS[verb], names[0])]
    if verb == "send":
        # send [a|the] [SIG]HUP|TERM|KILL [signal] to [process|pid] TARGET
        rest = [w for w in tlow if w not in ("a", "the", "signal")]
        if len(rest) >= 3 and rest[0] in _SEND_SIGNALS and rest[1] == "to":
            signal = _SEND_SIGNALS[rest[0]]
            targets = [w for w, lw in zip(tail, tlow) if lw not in ("a", "the", "signal", rest[0], "to")
                       and lw not in _PROCESS_FILLER]
            if len(targets) != 1:
                return None, "send a signal to one process; join several with 'and'"
            return "process", [(signal, targets[0])]
        return None, "send takes a signal (hup, term or kill) and a process"
    if verb in _PROCESS_VERBS:
        if head not in ([], ["force"], ["hard"], ["force", "-"]):
            return None, f"unexpected words before '{verb}'"
        if head in (["force"], ["hard"]):
            signal = "kill"
        targets, option = [], False
        for w, lw in zip(tail, tlow):
            if lw.startswith("-"):
                if verb != "kill" or lw not in _KILL_OPTIONS or option or targets:
                    return None, "options are read only in 'kill -9|-HUP|-TERM PID'"
                signal, option = _KILL_OPTIONS[lw], True
            elif lw in _SIGNAL_WORDS and verb == "kill":
                signal = _SIGNAL_WORDS[lw]
            elif lw in _PROCESS_FILLER:
                continue
            else:
                targets.append(w)
        if option and (len(targets) != 1 or not targets[0].isdigit()):
            return None, "options are read only in 'kill -9|-HUP|-TERM PID'"
        if verb in ("pkill", "killall") and any(t.isdigit() for t in targets):
            return None, f"{verb} takes a program name"
        if len(targets) > 1:
            return None, "one process per clause; join several with 'and'"
        return "process", [(signal, t) for t in targets]
    return None, f"unexpected words before '{verb}'"


def _actions_for(kind, payload):
    out = []
    if kind == "service":
        for op, name, scope in payload:
            out.append(action("service", operation=op, unit=name, scope=scope))
    elif kind == "process":
        if not payload:
            raise AdminError("name the process")
        for signal, target in payload:
            if re.fullmatch(PID, target):
                out.append(action("process", signal=signal, pid=int(target)))
            else:
                out.append(action("process", signal=signal, name=target))
    elif kind == "packages":
        for op, name in payload:
            if not re.fullmatch(PACKAGE, name.lower()):
                raise AdminError(f"'{name}' is not a Debian package name")
            out.append(action("packages", operation=op, package=name.lower()))
    elif kind == "updates":
        for op, _ in payload:
            out.append(action("updates", operation=op))
    return out


def _prepare(request):
    """The request with framing removed, or (None, reason)."""
    if not isinstance(request, str):
        return None, "the request is not text"
    if not request.strip():
        return None, "the request is empty"
    if len(request) > MAX_REQUEST:
        return None, "the request is too long"
    if any(ord(c) < 32 or ord(c) == 127 for c in request):
        return None, "control characters and line breaks are not accepted"
    text = " ".join(request.split())
    for a, b in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"')):
        text = text.replace(a, b)
    text = re.sub(r"[A-Za-z]+", lambda m: _TYPOS.get(m.group(0).lower(), m.group(0)), text)
    if _SHELL.search(text):
        return None, "shell syntax and wildcards are not accepted; name one action"
    if _CAPITAL_QUESTION.search(text):
        return None, "a question in the middle of a request"
    if "/" in text or re.search(r"\.deb(?![\w-])", text, re.I):
        return None, "files, paths and URLs are not installed or changed from here"
    text = _PREFIX.sub("", text)
    text = _FRAMING.sub(" ", text)
    text = " ".join(text.split())
    text = _SUFFIX.sub("", text)
    text = text.strip(" ,")
    if not text:
        return None, "the request names no action"
    low = text.lower()
    for pattern, reason in ((_NEGATION, "a negated, cancelled or excepted request runs nothing"),
                            (_CONDITION, "a conditional or scheduled request runs nothing"),
                            (_ALTERNATIVE, "a choice between actions runs nothing; name one"),
                            (_HYPOTHETICAL, "a reported, planned or hypothetical wish runs nothing"),
                            (_REMOTE, "only this machine is changed"),
                            (_OUT_OF_SCOPE, "outside the four admin tools (service, process, packages, updates)")):
        if pattern.search(low):
            return None, reason
    if _QUESTION.search(low):
        return None, "a question is not an action; reading is the system job"
    return text, None


def parse_explain(request):
    """(actions, None) for a complete reading, or (None, reason)."""
    text, reason = _prepare(request)
    if text is None:
        return None, reason
    text = _PURGE.sub(lambda m: "purge " + (m.group(1) or m.group(2) or m.group(3)), text)
    parts = _SPLIT_KEEP.split(text)
    segments, joins = parts[0::2], [""] + parts[1::2]
    if not segments or any(not seg.strip() for seg in segments):
        return None, "an empty clause"
    try:
        readings = []
        open_list = False       # a name joined by a bare comma, waiting for "and NAME"
        for segment, join in zip(segments, joins):
            kind, payload = _segment(segment)
            if kind is None:
                return None, payload
            if kind == "names":
                open_list = not re.search(r"\b(?:and|then|also|plus)\b", join)
            elif open_list:
                return None, "a list joined by commas ends with 'and NAME'"
            readings.append((kind, payload))
        if open_list:
            return None, "a list joined by commas ends with 'and NAME'"
        actions = []
        pending_verbs = []      # "enable and start nginx": verbs waiting for a name
        last = None             # (kind, payload) the next name list continues
        for kind, payload in readings:
            if kind == "verb":
                pending_verbs.extend(payload)
                last = None
                continue
            if kind == "names":
                if last is None or last[0] not in ("service", "process", "packages"):
                    return None, "a list of names without a verb"
                lkind, lpayload = last
                template = lpayload[-1]
                if lkind == "service":
                    payload = [(template[0], n, template[2]) for n in payload]
                elif lkind == "process":
                    payload = [(template[0], n) for n in payload]
                else:
                    payload = [(template[0], n) for n in payload]
                kind = lkind
            if pending_verbs:
                if kind != "service" or len(payload) != 1:
                    return None, "a verb without a name"
                _, name, scope = payload[0]
                for op in pending_verbs:
                    actions.extend(_actions_for("service", [(op, name, scope)]))
                pending_verbs = []
            actions.extend(_actions_for(kind, payload))
            last = (kind, payload)
        if pending_verbs:
            return None, "a verb without a name"
    except AdminError as error:
        return None, str(error)
    if not actions:
        return None, "the request names no action"
    if len(actions) > MAX_ACTIONS:
        return None, f"more than {MAX_ACTIONS} actions in one request"
    if len(set((a.kind, tuple(sorted(a.args.items()))) for a in actions)) != len(actions):
        return None, "the same action twice"
    return actions, None


def parse(request):
    return parse_explain(request)[0]


def hard_refusal(request) -> str | None:
    """Why the whole request refuses before any word is read as an action, or None.

    A request refused here (negated, conditional, alternative, hypothetical,
    another machine, shell syntax, outside the four tools) is never given to a
    proposer: a model must not turn "don't restart nginx" into a restart."""
    return _prepare(request)[1]


def _literal(request: str, value: str, *, fold: bool) -> bool:
    flags = re.I if fold else 0
    return bool(re.search(r"(?<![A-Za-z0-9_.@:+-])" + re.escape(value) + r"(?![A-Za-z0-9_@:+-])",
                          request, flags))


def grounded(request, actions) -> str | None:
    """Why a proposer's actions are not grounded in the request, or None.

    Every target must be written in the request as it is in the action: the
    unit, the program, the package, the PID. A proposal that passes is still
    unverified: only a person at a terminal may confirm it."""
    if not actions or len(actions) > MAX_ACTIONS:
        return "no action, or too many"
    for act in actions:
        a = act.args
        if act.kind == "service" and not (_literal(request, a["unit"], fold=False)
                                          or _literal(request, a["unit"] + ".service", fold=False)):
            return f"the service {a['unit']} is not named in the request"
        if act.kind == "process":
            target = str(a["pid"]) if "pid" in a else a["name"]
            if not _literal(request, target, fold=False):
                return f"the process {target} is not named in the request"
        if act.kind == "packages" and not _literal(request, a["package"], fold=True):
            return f"the package {a['package']} is not named in the request"
    return None


def interpret(request, calls):
    """Admit a proposer's calls only when they are the complete reading, in order."""
    wanted, reason = parse_explain(request)
    if wanted is None:
        return [Refusal("admin", reason)]
    if not isinstance(calls, list) or len(calls) != len(wanted):
        return [Refusal("admin", "the proposal does not hold every requested action exactly once")]
    observed = []
    try:
        for call in calls:
            if not isinstance(call, dict):
                raise AdminError("malformed admin call")
            observed.append(Action(call.get("name"), normalize(call.get("name"), call.get("arguments"))))
    except (AdminError, TypeError) as error:
        return [Refusal("admin", str(error))]
    if observed != wanted:
        return [Refusal("admin", "the proposal's action, target or order differs from the request")]
    return wanted


# ---------------------------------------------------------------- the accepted form

def sentence(actions) -> str | None:
    """The request this module reads as exactly these actions (offered back to a
    caller whose wording was not read)."""
    parts = []
    for act in actions:
        a = act.args
        if act.kind == "service":
            user = " user" if a["scope"] == "user" else ""
            parts.append(f"{a['operation']} the {a['unit']}{user} service")
        elif act.kind == "process":
            verb = {"term": "kill", "kill": "force kill", "hup": "send hup to"}[a["signal"]]
            parts.append(f"{verb} process {a['pid']}" if "pid" in a else f"{verb} {a['name']}")
        elif act.kind == "packages":
            parts.append(f"{a['operation']} {a['package']}")
        elif act.kind == "updates":
            parts.append("update the package lists" if a["operation"] == "refresh" else "upgrade packages")
        else:
            return None
    text = " and then ".join(parts)
    return text if parse(text) == list(actions) else None


FORMS = ('"restart the nginx service", "enable and start docker", "restart the pipewire user service", '
         '"kill process 4312", "force kill firefox", "send hup to process 812", "install curl and jq", '
         '"purge vim", "update the package lists", "upgrade packages"')


class Baseline:
    """The reading as an engine reply; never presented as model inference."""
    label = "admin request checks"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def reset(self):
        pass

    def complete(self, request):
        return {"function_calls": [{"name": a.kind, "arguments": a.args} for a in parse(request) or []]}
