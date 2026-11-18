"""Training rows for the bundle of every job: requests the way coding agents
write them, each with the reading the bundle should give (job_schemas).

A template is wording with slots. `{slot}` takes a value from a pool: the
words it puts in the request and the arguments they stand for. `[a|b|c]`
takes one wording, `(x)` takes x or nothing. Every row records, for each
argument, the words it came from (spans), as the panes rows do: that is what
the nets for copied text learn from.

The names, paths, sessions and texts in the pools are made up here. None is
taken from a request of any eval set or suite, or from the desktop of the
needle-calls bench.

Which rows are kept (rows()):
- files, system, agents, apps: only rows the job's grammar reads, and reads
  as the row is labelled (the reading written as the job's call equals the
  grammar's). On these jobs a proposal is admitted only when it equals what
  the grammar reads, so a row the grammar does not read would teach the
  bundle an answer that is never used, and one it reads otherwise would teach
  it an answer that is refused.
- tmux, pty: rows whose call reads back through the job's grammar sentence
  (tmux_job.reads_back, pty_job.reads_back); logs: rows the logs tool's
  argument checks take; read_pane: rows whose arguments pane_reads takes.
  The wording need not be the grammar's.
"""
from __future__ import annotations

import collections
import itertools
import math
import random
import re

import job_schemas

# A number with "'s" after it ("pane 42's screen") is one word to the reader
# (panes_bundle.tokenize), and not a number: no template puts one there.

# ---- the template engine ----------------------------------------------------

_TOKEN = re.compile(r"\{(\w+)\}|\[([^\[\]]*)\]|\(([^()]*)\)|([^{\[(]+)")


def _expand(template: str, pools: dict, rng: random.Random):
    """(text, args, spans): one random expansion of a template."""
    def walk(text):
        out, args, spans = [], {}, []
        for slot, choice, optional, plain in _TOKEN.findall(text):
            if slot:
                surface, values = pools[slot](rng) if callable(pools[slot]) else rng.choice(pools[slot])
                out.append(surface)
                for key, value in values.items():
                    args[key] = value
                    spans.append((_span_surface(surface, key, value), value))
            elif choice:
                sub, a, s = walk(rng.choice(choice.split("|")))
                out.append(sub)
                args.update(a)
                spans.extend(s)
            elif optional:
                if rng.random() < 0.5:
                    sub, a, s = walk(optional)
                    out.append(sub)
                    args.update(a)
                    spans.extend(s)
            else:
                out.append(plain)
        return "".join(out), args, spans

    text, args, spans = walk(template)
    return " ".join(text.split()), args, spans


def _skeletons(template: str) -> list[str]:
    """Every wording of a template: each [a|b] and (x) resolved, the slots kept."""
    parts = []
    for slot, choice, optional, plain in _TOKEN.findall(template):
        if slot:
            parts.append(["{" + slot + "}"])
        elif choice:
            parts.append([s for alt in choice.split("|") for s in _skeletons(alt)])
        elif optional:
            parts.append([""] + _skeletons(optional))
        else:
            parts.append([plain])
    return ["".join(p) for p in itertools.product(*parts)]


def _choices(job: str, tool: str, pool_) -> list | None:
    """What a slot's pool gives that stays in the bundle's frame of the
    clause: the entries of a pool of closed values (each with its own words),
    the forms of a pool function that has them (the quote marks around a
    copied text), or None for a pool of copied values or numbers, which the
    frame holds only as marks."""
    if callable(pool_):
        forms = getattr(pool_, "forms", 0)
        return list(range(forms)) if forms else None
    if job == "panes":
        # the panes job's schema is the actions' own; of the kinds its rows
        # here hold, the list's "what" is a closed value (a read has numbers)
        import panes_bundle
        kinds = {"what": "enum"} if tool == panes_bundle.LIST else {}
    else:
        kinds = {arg["key"]: arg["kind"] for arg in job_schemas.SCHEMAS.get(job, {}).get(tool, {}).get("args", ())}
    if all(kinds.get(key) in ("enum", "flag") for _, values in pool_ for key in values):
        return list(pool_)
    return None


def _combos(job: str, tool: str, template: str, pools: dict, rng: random.Random, cap: int) -> list:
    """[(skeleton, {slot: choice})]: every wording of the template with every
    choice of its closed slots, or `cap` of them drawn at random when there
    are more."""
    plans = []
    for skeleton in _skeletons(template):
        dims = [(slot, _choices(job, tool, pools[slot])) for slot in re.findall(r"\{(\w+)\}", skeleton)]
        dims = [(slot, options) for slot, options in dims if options]
        plans.append((skeleton, dims, math.prod(len(options) for _, options in dims)))
    total = sum(size for _, _, size in plans)
    if total <= cap:
        return [(skeleton, dict(zip([slot for slot, _ in dims], pick)))
                for skeleton, dims, _ in plans
                for pick in itertools.product(*[options for _, options in dims])]
    out, seen = [], set()
    weights = [size for _, _, size in plans]
    while len(out) < cap:
        skeleton, dims, _ = rng.choices(plans, weights)[0]
        indexes = tuple(rng.randrange(len(options)) for _, options in dims)
        if (skeleton, indexes) in seen:
            continue
        seen.add((skeleton, indexes))
        out.append((skeleton, {slot: options[i] for (slot, options), i in zip(dims, indexes)}))
    return out


def _fill(skeleton: str, pools: dict, rng: random.Random, chosen: dict):
    """(text, args, spans): a skeleton with its closed slots as chosen and its
    other slots drawn at random."""
    out, args, spans = [], {}, []
    for slot, _, _, plain in _TOKEN.findall(skeleton):
        if not slot:
            out.append(plain)
            continue
        pool_ = pools[slot]
        if slot in chosen:
            surface, values = pool_(rng, form=chosen[slot]) if callable(pool_) else chosen[slot]
        else:
            surface, values = pool_(rng) if callable(pool_) else rng.choice(pool_)
        out.append(surface)
        for key, value in values.items():
            args[key] = value
            spans.append((_span_surface(surface, key, value), value))
    return " ".join("".join(out).split()), args, spans


# A request may end in a full stop, and a question in a question mark: the
# reader holds such a mark as part of the clause's wording.
QUESTION_WORDS = frozenset({"what", "what's", "whats", "which", "is", "are", "how", "any", "does", "do", "can",
                            "where", "who", "did", "has", "have", "was", "were", "will", "anything"})


def _endings(text: str, spans=()) -> list[str]:
    """The text as written, without its final mark, with a full stop, and,
    for a question, with a question mark. A mark that ends a copied value at
    the end of the text is the value's own ("tell claude: what is left to
    do?"), and no mark is put after a path or a quote: the reader would not
    split it from them."""
    if any(isinstance(surface, str) and surface[-1:] in (".", "?", "!") and text.endswith(surface)
           for surface, _ in spans):
        return [text]
    base = text[:-1].rstrip() if text[-1:] in ".?!" and text[-2:-1] not in "\"'`!?." else text
    words = base.split()
    if not words or "/" in words[-1] or words[-1][-1:] in "\"'`":
        return [text]
    out = [text, base, base + "."]
    if words[0].casefold().strip(",") in QUESTION_WORDS:
        out.append(base + "?")
    return list(dict.fromkeys(out))


def _span_surface(surface, key, value):
    """The words of a slot's surface that an argument came from: a copied
    value is itself; a value from a closed list is named by the surface."""
    if isinstance(value, str) and value in surface:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        for word in re.findall(r"[0-9]+", surface):
            if int(word) == value:
                return word
    return surface


def pool(*entries):
    """A pool: [(surface, {arg: value})]. A bare string is (s, {})."""
    return [(e, {}) if isinstance(e, str) else e for e in entries]


def copy(key, *values):
    """A pool of copied values: each value stands for itself."""
    return [(v, {key: v}) for v in values]


def number(key, low, high, common=()):
    """A pool function: a number in a range, often one of `common`."""
    def take(rng):
        n = rng.choice(common) if common and rng.random() < 0.6 else rng.randint(low, high)
        return str(n), {key: n}
    return take


# ---- made-up values ---------------------------------------------------------

HOME = "/home/pleb"
PROJECTS = ["orchard", "quill", "harbor-sync", "mosaic-db", "lantern", "pebble-ui", "atlas-cli", "nimbus",
            "kestrel", "sprocket", "fernweh", "dovetail", "copperline", "marble-api", "wren"]
FILES = ["README.md", "setup.py", "pyproject.toml", "Cargo.toml", "src/lib.rs", "src/main.rs", "main.go",
         "package.json", "docker-compose.yml", "config/app.yaml", "docs/usage.md", "CHANGELOG.md", "Makefile",
         "tests/test_core.py", "scripts/deploy.sh", "notes/todo.md", "src/server/routes.py", "app/models.py",
         "internal/http/handler.go", "web/index.html", "src/components/Button.tsx", "requirements.txt",
         "config/logging.ini", "migrations/0007_add_users.sql", "LICENSE", "Dockerfile", "src/util/strings.c",
         "include/config.h", "docs/architecture.md", "train.py", "eval/metrics.py", "data/schema.json"]
DIRS = [f"{HOME}/gpu_terminal/{p}" for p in PROJECTS] + [
    f"{HOME}/research/{d}" for d in ("papers", "drafts", "experiments", "reading")] + [
    f"{HOME}/Documents/{d}" for d in ("work", "receipts")] + [f"{HOME}/src/{p}" for p in PROJECTS[:6]] + [
    "/etc/nginx", "/var/log", "/srv/app", "/opt/tools", f"{HOME}/.config/kitty", f"{HOME}/scratch"]
SCOPES = ["here", "Downloads", "Documents", "research", "projects", "this project", "the current directory",
          "this directory", "."]
SYSTEM_FILES = ["/etc/hosts", "/etc/fstab", "/etc/nginx/nginx.conf", "/etc/ssh/sshd_config", "/var/log/syslog",
                "/var/log/nginx/access.log", "/etc/resolv.conf", "/etc/os-release", "/var/log/auth.log",
                "/etc/systemd/system/backup.service", "/var/log/postgresql/postgresql-16-main.log"]
# a word searched for without quotes
WORDS = ["TODO", "FIXME", "XXX", "HACK", "deprecated", "unwrap", "panic", "localhost", "DATABASE_URL",
         "max_retries", "segfault", "token", "license", "timeout", "NotImplementedError", "useEffect"]
SEARCH = ["TODO", "FIXME", "def main", "import torch", "connection refused", "timeout", "panic!", "SECRET_KEY",
          "max_retries", "fn parse_header", "class Config", "unwrap()", "deprecated", "localhost:8080",
          "assert_eq!", "useEffect", "DATABASE_URL", "retry_backoff", "func NewServer", "@pytest.mark.slow",
          "XXX", "HACK", "raise NotImplementedError", "console.log", "version =", "port: 8443",
          "OutOfMemoryError", "segfault", "def test_", "TODO(pleb)", "set -euo pipefail", "license", "token"]
NAMES = ["invoice", "report", "backup", "config", "draft", "notes", "2026-11", "screenshot", "budget",
         "resume", "export", "dump", "test_", "setup", "schema", "deploy", "photo", "minutes", "README", "q3"]
EXTENSIONS = ["pdf", "md", "py", "rs", "go", "json", "yaml", "toml", "log", "csv", "txt", "sh", "png", "zip"]
TYPE_WORDS = ["markdown", "python", "rust", "text", "shell", "javascript"]
SERVICES = ["nginx", "postgresql", "docker", "ssh", "cron", "cups", "redis", "mysql", "apache2", "bluetooth",
            "NetworkManager", "systemd-resolved", "ollama", "caddy", "grafana-server", "prometheus",
            "containerd", "libvirtd", "avahi-daemon", "smbd", "fail2ban", "ufw", "tailscaled", "snapd"]
USER_SERVICES = ["syncthing", "pipewire", "wireplumber", "gpg-agent", "dbus", "emacs", "mpd"]
TAGS = ["sshd", "kernel", "sudo", "CRON", "systemd", "kitty", "Xorg", "dockerd", "NetworkManager", "polkitd",
        "gdm-password", "rsyslogd", "earlyoom", "smartd", "ollama"]
PACKAGES = ["ripgrep", "jq", "curl", "git", "htop", "tmux", "python3", "nodejs", "gcc", "make", "fzf", "bat",
            "neovim", "ffmpeg", "sqlite3", "rsync", "zstd", "pandoc", "shellcheck", "golang-go", "cargo",
            "openssh-server", "docker-ce", "wget", "unzip", "tree", "ncdu", "btop"]
COMMANDS = ["rg", "jq", "curl", "fd", "nvim", "ffmpeg", "sqlite3", "rsync", "make", "gcc", "python3", "tree",
            "pandoc", "zstd", "htop", "tmux", "git", "wget"]
OWNED = ["/usr/bin/rg", "/usr/bin/jq", "/usr/lib/x86_64-linux-gnu/libssl.so.3", "/etc/nginx/nginx.conf",
         "/usr/bin/python3", "/bin/ls", "/usr/sbin/sshd", "/usr/bin/curl", "/etc/ssh/sshd_config",
         "/usr/share/zoneinfo/UTC", "/usr/bin/make", "/usr/lib/systemd/systemd", "/usr/bin/tmux"]
MOUNTS = ["/", "/home", "/var", "/tmp", "/boot", "/srv", "/mnt/data", "/home/pleb", "/var/lib/docker", "/opt"]
PROMPTS = ["fix the failing tests", "review the last commit", "add a --dry-run flag to the cli",
           "update the README with the new install steps", "find out why the build is slow",
           "write tests for the parser", "refactor the config loader", "bump the version to 2.4.0",
           "summarize what changed since yesterday", "port the retry logic to the new client",
           "check the CI logs and fix what broke", "rename the Widget class to Panel everywhere",
           "profile the startup path", "delete the dead code in utils", "draft release notes for v1.3",
           "make the linter pass", "investigate the flaky integration test", "add type hints to models.py"]
MESSAGES = ["rerun the failing test", "stop and summarize where you are", "commit your work",
            "rebase on main", "the build is green now", "use the staging database instead",
            "skip the slow tests for now", "what is left to do?", "push the branch when done",
            "please open a PR", "revert the last change", "run the full suite before committing",
            "the API key is in .env now", "ignore the lint warnings in vendor/", "go ahead"]
MODELS = ["opus", "sonnet", "o3", "gpt-5", "grok-4", "haiku", "gpt-5-codex", "qwen3-coder"]
TMUX_SESSIONS = ["work", "deploy", "api", "worker", "logs", "train", "notebook", "mon", "dev", "batch", "etl",
                 "bot", "proxy", "watch", "jobs"]
LOG_TITLES = ["auth rewrite", "nightly import", "release prep", "flaky test hunt", "docs pass", "cache bug"]
LOG_FILES = [f"{HOME}/logs/deploy.log", f"{HOME}/logs/cron-backup.log", "/var/log/nginx/error.log",
             f"{HOME}/gpu_terminal/quill/logs/app.log", f"{HOME}/gpu_terminal/orchard/server.log",
             "/srv/app/logs/worker.log", f"{HOME}/tmp/build-output.txt"]
SESSION_FILES = {"codex": [f"{HOME}/.codex/sessions/2026/11/{d:02d}/rollout-2026-11-{d:02d}T{h:02d}-{m:02d}-{s:02d}.jsonl"
                           for d, h, m, s in ((3, 9, 1, 12), (8, 14, 22, 5), (12, 18, 40, 51), (15, 7, 3, 9))],
                 "claude": [f"{HOME}/.claude/projects/{p}/{x}.jsonl"
                            for p, x in (("quill", "3e1a77c0"), ("orchard", "9b20f4d1"), ("lantern", "c47e8a02"))]}
EVENTS = ["ev-1a2b", "ev-77f0", "ev-03c9", "e512", "e7", "evt-9931", "ev-5d5d", "e1044"]


def _hex(rng, n=16):
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _target(rng):
    s = rng.choice(TMUX_SESSIONS)
    kind = rng.random()
    if kind < 0.45:
        t = s
    elif kind < 0.8:
        t = f"{s}:{rng.randint(0, 3)}.{rng.randint(0, 3)}"
    else:
        t = f"%{rng.randint(0, 40)}"
    return t, {"target": t}


def _pty_id(key):
    def take(rng):
        ident = _hex(rng)
        return ident, {key: ident}
    return take


def _journal_id(rng):
    ident = f"{_hex(rng)}.{rng.randint(1_760_000_000_000, 1_770_000_000_000)}" if rng.random() < 0.6 else _hex(rng)
    return ident, {"id": ident}


def _quoted(key, values, marks=('"', "'")):
    """A pool function: a copied value inside quotes (the value without them).
    Its forms are the quote marks: the frame holds them as words."""
    def take(rng, form=None):
        v = rng.choice(values)
        q = marks[form] if form is not None else rng.choice(marks)
        return f"{q}{v}{q}", {key: v}
    take.forms = len(marks)
    return take


def _abs_file(rng):
    d = rng.choice(DIRS[:25])
    f = rng.choice(FILES)
    p = f"{d}/{f}"
    return p, {"path": p}


def _rel_file(rng):
    f = rng.choice(FILES)
    return f, {"path": f}


def _any_abs_file(rng):
    """An absolute path to a file: in a project or a home directory, or a system file."""
    if rng.random() < 0.3:
        p = rng.choice(SYSTEM_FILES + LOG_FILES)
        return p, {"path": p}
    return _abs_file(rng)


def _rel_file_dot(rng):
    """A relative path, now and then written from "./"."""
    f = rng.choice(FILES)
    f = f"./{f}" if rng.random() < 0.15 else f
    return f, {"path": f}


def _scope(rng):
    if rng.random() < 0.55:
        s = rng.choice(SCOPES)
    else:
        s = rng.choice(DIRS)
    return s, {"scope": s}


# ---- the templates, job by job ---------------------------------------------

def _files_templates():
    common = {"scope": _scope, "qpath": _quoted("path", FILES), "qtext": _quoted("text", SEARCH),
              "qname": _quoted("name", NAMES), "ext": [(e, {"extension": e}) for e in EXTENSIONS + TYPE_WORDS],
              "days": number("days", 1, 60, (1, 2, 3, 7, 14, 30)),
              "today": pool(("today", {"modified": "today"}), ("yesterday", {"modified": "yesterday"}))}
    t = []
    # preview "rel/path" in SCOPE: the grammar's form, with agents' heads and tails.
    for head in ("preview", "Preview", "preview", "preview"):
        t.append(("preview_file", f"{head} {{qpath}} in {{scope}}", common))
    # find text "TEXT" in SCOPE
    t.append(("search_text", "[find|search|Find|Search] [text |]{qtext} in {scope}", common))
    t.append(("search_text", "find text {qtext} in {scope}", common))
    # find files named / starting with
    t.append(("find_files", "[find|search for] files named {qname} in {scope}( modified {today})",
              common))
    t.append(("find_files", "[find|search for] files named {qname} in {scope} modified last {days} days", common,
              {"modified": "days"}))
    t.append(("find_files", "[find|search for] files starting with {qname} in {scope}"
                            "( modified {today})", common, {"prefix": True}))
    t.append(("find_files", "[find|locate|look for] [files|the files|all files] [whose name starts with|"
                            "with names starting with|starting with] {qname} [in|under|inside|below] {scope}",
              common, {"prefix": True}))
    t.append(("find_files", "[find|locate|search for] [files|the files] [named|called|matching|containing] {qname} "
                            "[in|under|inside|within] {scope}( and return only the paths)", common))
    t.append(("find_files", "[find|show] {ext} files in {scope}( modified {today})", common))
    t.append(("find_files", "[find|show] the {ext} files in {scope}", common))
    t.append(("find_files", "[find|show] {ext} files in {scope} modified last {days} days", common,
              {"modified": "days"}))
    t.append(("find_files", "[find|show] files in {scope} modified {today}", common))
    t.append(("find_files", "[find|show] files in {scope} modified last {days} days", common,
              {"modified": "days"}))
    # list largest / recent files
    t.append(("list_files", "[show|list] [largest|large] files in {scope}", common, {"order": "size"}))
    t.append(("list_files", "[show|list] [recent|newest] files in {scope}", common, {"order": "modified"}))
    # Wordings of experiment 11's development half: the files tool's own
    # checks take a reading the grammar does not (job_routes.files_checks).
    more = {**common, "apath": _any_abs_file, "rpath": _rel_file_dot, "word": copy("text", *WORDS),
            "here": copy("scope", "here")}
    view = "[cat|show|show me|preview|read|view|print|display]"
    t.append(("preview_file", f"{view} [the file |the contents of |]{{apath}}", more))
    t.append(("preview_file", "[what's|what is] in {apath}(?)", more))
    t.append(("preview_file", f"{view} [the file |the contents of |]{{rpath}} {{here}}", more))
    t.append(("preview_file", f"{view} [the file |the contents of |]{{rpath}} in {{scope}}", more))
    t.append(("preview_file", f"{view} [the file |]{{qpath}} in {{scope}}", more))
    t.append(("search_text", "search {scope} for [the text |the string |the error message |]{qtext}", more))
    t.append(("search_text", "search {scope} for {word}", more))
    t.append(("search_text", "[grep|grep for|grep -rn|grep -r|look for|search for] {qtext} in {scope}", more))
    t.append(("search_text", "[grep for|look for|search for] {word} in {scope}", more))
    t.append(("search_text", "look through {scope} for {qtext}", more))
    t.append(("search_text", "search {here} for [the text |the string |the error message |]{qtext}", more))
    t.append(("find_files", "find [all |]{ext} files in {scope} modified in the last {days} days", more,
              {"modified": "days"}))
    t.append(("find_files", "find all {ext} files in {scope}( modified {today})", more))
    t.append(("find_files", "[any|are there any] {ext} files modified {today} in {scope}(?)", more))
    t.append(("find_files", "find {ext} files [named|called] {qname} in {scope}", more))
    t.append(("find_files", "find {ext} files [starting with|beginning with] {qname} in {scope}", more,
              {"prefix": True}))
    t.append(("find_files", "find files [beginning with|whose name begins with] {qname} in {scope}", more,
              {"prefix": True}))
    t.append(("list_files", "[list|show] [the |][most recent|recent|newest|latest|recently changed|recently modified] "
                            "files in {scope}(, newest first)", more, {"order": "modified"}))
    t.append(("list_files", "[list|show] [the |][largest|biggest|large] files in {scope}(, largest first)", more,
              {"order": "size"}))
    t.append(("list_files", "[largest|biggest] files in {scope}", more, {"order": "size"}))
    t.append(("list_files", "[recent|newest] files in {scope}", more, {"order": "modified"}))
    t.append(("list_files", "which files in {scope} [changed|were modified|were changed] most recently(?)", more,
              {"order": "modified"}))
    t.append(("list_files", "sort the files in {scope} by size(, largest first)", more, {"order": "size"}))
    t.append(("list_files", "sort the files in {scope} by [date|modification time](, newest first)", more,
              {"order": "modified"}))
    return t


def _system_templates():
    mem = pool(("memory", {"kind": "memory"}), ("ram", {"kind": "memory"}), ("RAM", {"kind": "memory"}),
               ("swap", {"kind": "memory"}))
    sort = pool(("memory", {"sort": "memory"}), ("ram", {"sort": "memory"}), ("cpu", {"sort": "cpu"}),
                ("CPU", {"sort": "cpu"}))
    c = {"mount": copy("path", *MOUNTS), "unit": copy("unit", *SERVICES), "uunit": copy("unit", *USER_SERVICES),
         "tag": copy("identifier", *TAGS), "pkg": copy("target", *PACKAGES), "cmd": copy("target", *COMMANDS),
         "owned": copy("target", *OWNED), "mem": mem, "sort": sort,
         "n": number("limit", 1, 50, (3, 5, 10, 15, 20)), "jn": number("limit", 1, 100, (10, 20, 30, 50, 100)),
         "since_n": number("since_n", 1, 72, (1, 2, 3, 5, 6, 10, 12, 15, 24, 30, 48)),
         "hours": pool(("hours", {"since": "hours"}), ("hour", {"since": "hour"})),
         "minutes": pool(("minutes", {"since": "minutes"}), ("minute", {"since": "minute"})),
         "daysu": pool(("days", {"since": "days"}), ("day", {"since": "day"})),
         "day": pool(("today", {"since": "today"}), ("yesterday", {"since": "yesterday"})),
         "date": [(d, {"since": "date", "since_date": d}) for d in ("2026-11-01", "2026-10-15", "2026-11-17",
                                                                     "2026-09-30", "2026-11-10")],
         "errs": pool(("errors", {"priority": "err"}), ("warnings", {"priority": "warning"})),
         "boot": pool(("this boot", {"boot": "current"}), ("the previous boot", {"boot": "previous"}),
                      ("all boots", {"boot": "any"}), ("the current boot", {"boot": "current"}))}
    t = [
        ("resources", "[show|check|] [memory|ram|swap][ usage| status|]", c, {"kind": "memory"}),
        ("resources", "how much [memory|ram] is [free|available]", c, {"kind": "memory"}),
        ("resources", "[show|check|] [cpu load|load average|load averages]", c, {"kind": "cpu"}),
        ("resources", "[show|check|] [system resources|resource usage|system load]", c, {"kind": "all"}),
        ("resources", "[show|check|] [disk space|disk usage|free space]", c, {"kind": "disk"}),
        ("resources", "[show|check|] [disk space|disk usage|free space] [on|for] {mount}", c, {"kind": "disk"}),
        ("resources", "how much [disk |free |]space is [left|free|available] on {mount}", c, {"kind": "disk"}),
        ("resources", "[show|check|get] [the |][memory|ram] usage[ please|]", c, {"kind": "memory"}),
        ("processes", "[show |list |][the |]top {n} processes by {sort}", c),
        ("processes", "[show |list |][the |]processes [by|using the most] {sort}", c),
        ("processes", "what[ is|'s] using [the most |all |my |all my ]{sort}", c),
        ("processes", "[which|what] process[es|] [is|are] using the most {sort}", c),
        ("processes", "[show|list|get] [the |][running |current |]processes [sorted|ranked|ordered] by {sort}( usage)",
         c),
        ("processes", "[show|list|get|find] [the |]top {n} process[es|] [by|sorted by|ranked by] {sort}( usage)", c),
        ("services", "[show|list] [failed|all] services", c,),
        ("services", "[show|list] failed services", c, {"state": "failed"}),
        ("services", "[show |check |][the |]{unit} service[ status|]", c),
        ("services", "is [the |]{unit}( service) [running|active|up|enabled]", c),
        ("services", "[check|show|get] [the |]status of [the |]{unit}( service)", c),
        ("services", "[show |check |][the |]user {uunit} service[ status|]", c, {"scope": "user"}),
        ("journal", "[show |read |list |][the |]journal", c),
        ("journal", "[show |read |list |][the |]{errs}", c),
        ("journal", "[show |read |][the |]{errs} from [the |]{unit} service", c),
        ("journal", "[show |read |][the |]journal from [the |]{unit} service", c),
        ("journal", "[show |read |][the |]{errs} from [the |]program {tag}", c),
        ("journal", "[show |read |][the |]{errs} since {since_n} {hours} ago", c),
        ("journal", "[show |read |][the |]{errs} since {since_n} {minutes} ago", c),
        ("journal", "[show |read |][the |]{errs} since {since_n} {daysu} ago", c),
        ("journal", "[show |read |][the |]{errs} since {day}", c),
        ("journal", "[show |read |][the |]{errs} since {date}", c),
        ("journal", "[show |read |][the |]{errs} from {boot}", c),
        ("journal", "[show |read |][the |]{errs} from [the |]{unit} service since {since_n} {hours} ago", c),
        ("journal", "[show |read |][the |]journal [limit|last] {jn}", c),
        ("journal", "[show |read |][the |]{errs} [limit|last] {jn}( entries)", c),
        ("packages", "is {pkg} installed", c, {"operation": "status"}),
        ("packages", "[show |check |][package |]{pkg} package status", c, {"operation": "status"}),
        ("packages", "[check whether|check if|see if|determine whether] [the |][package |]{pkg} is installed", c,
         {"operation": "status"}),
        ("packages", "[what|which] version of {pkg} is installed", c, {"operation": "status"}),
        ("packages", "[which|what] package [provides|owns] {owned}", c, {"operation": "owner"}),
        ("packages", "[which|what] package [provides|owns] [the |]{cmd} command", c, {"operation": "owner"}),
        ("packages", "[find |]which package [provides|owns|contains|installed] {owned}", c, {"operation": "owner"}),
        ("slow_machine", "why is [my |the |][machine|computer|system] [so |]slow", c),
        ("boot_failures", "what failed during [this|the current] boot", c),
    ]
    # Wordings of experiment 11's development half: the system normalizer's
    # checks of a proposal take a reading the grammar does not
    # (job_routes.system_checks).
    rss = pool(("memory", {"sort": "memory"}), ("ram", {"sort": "memory"}), ("cpu", {"sort": "cpu"}),
               ("RSS", {"sort": "memory"}))
    d = {**c, "rss": rss}
    t += [
        ("resources", "am I [running |][low on|short on|out of] {mem}(?)", d),
        ("resources", "am I [running |][low on|short on|out of] [disk|disk space|space](?)", d, {"kind": "disk"}),
        ("resources", "is the disk [full|almost full|nearly full|filling up](?)", d, {"kind": "disk"}),
        ("resources", "[show|check|report] [the |][current |][cpu|cpu load|cpu usage|load average]", d,
         {"kind": "cpu"}),
        ("resources", "[what's|what is] the [current |][cpu load|load average|cpu usage](?)", d, {"kind": "cpu"}),
        ("resources", "[show|check|report] [the |][memory|ram|memory usage|ram usage]", d, {"kind": "memory"}),
        ("resources", "[show|check|report] [the |][disk|disk usage|disk space]", d, {"kind": "disk"}),
        ("resources", "how much {mem} [is free|is available|is left|do I have]( left)(?)", d),
        ("resources", "how much {mem} does this [box|machine] have available(?)", d),
        ("resources", "[show|check|report] [all|overall|system] resources", d, {"kind": "all"}),
        ("resources", "[show |check |][the |]overall resource [use|usage]", d, {"kind": "all"}),
        ("resources", "[free space|disk space|disk usage] on {mount}(?)", d, {"kind": "disk"}),
        ("processes", "[list|show] the top {n} {rss} hogs", d),
        ("processes", "[list|show] the [heaviest|hungriest|biggest] processes by {rss}", d),
        ("processes", "sort [the |]processes by {rss}", d),
        ("processes", "which processes [hold|use] the most {rss}(?)", d),
        ("processes", "top {rss} processes(, limit {n})", d),
        ("processes", "[show|list] {n} processes [ordered|sorted] by {rss}", d),
        ("journal", "[show |read |][the |]{unit} service journal", d),
        ("journal", "[show |read |][the |]journal for {unit}(, last {jn} entries)", d),
        ("journal", "{tag} [log|logs|journal] since {date}", d),
        ("journal", "[show |read |][the |]journal {errs}", d),
        ("journal", "[show |read |][the |]{errs} in the journal", d),
        ("packages", "which package does {cmd} come from(?)", d, {"operation": "owner"}),
        ("packages", "which package [ships|installed|provides|owns] {owned}(?)", d, {"operation": "owner"}),
        ("services", "list all services( and their [state|status])", d, {"state": "all"}),
        ("services", "list [all |]user services", d, {"state": "all", "scope": "user"}),
        ("services", "list failed user services", d, {"state": "failed", "scope": "user"}),
        ("services", "is the {uunit} user service [running|active](?)", d, {"scope": "user"}),
        ("services", "is {uunit} running as a user service(?)", d, {"scope": "user"}),
        ("services", "which services [have failed|failed](?)", d, {"state": "failed"}),
    ]
    return t


def _agents_templates():
    agent = pool(("claude", {"agent": "claude"}), ("codex", {"agent": "codex"}), ("grok", {"agent": "grok"}),
                 ("qwen", {"agent": "qwen-omp"}), ("Claude", {"agent": "claude"}), ("Codex", {"agent": "codex"}))
    s_agent = pool(("claude", {"s_agent": "claude"}), ("codex", {"s_agent": "codex"}), ("grok", {"s_agent": "grok"}),
                   ("qwen", {"s_agent": "qwen-omp"}))
    dirs = PROJECTS + [f"{HOME}/gpu_terminal/{p}" for p in PROJECTS] + [f"{HOME}/research/drafts", "the api repo"]
    c = {"agent": agent, "s_agent": s_agent, "dir": copy("dir", *dirs), "s_dir": copy("s_dir", *dirs),
         "prompt": copy("prompt", *PROMPTS), "text": copy("text", *MESSAGES), "model": copy("model", *MODELS),
         "place": pool(("on the right", {"place": "right"}), ("to the right", {"place": "right"}),
                       ("on the left", {"place": "left"}), ("below", {"place": "down"}),
                       ("above", {"place": "up"}), ("in a split on the right", {"place": "right"})),
         "secs": number("timeout", 10, 3600, (30, 60, 120, 300, 600, 900)),
         "mins": number("timeout", 1, 60, (2, 5, 10, 15, 30))}
    return [
        ("agent", "[start|launch|open|spin up|run] {agent} in {dir}", c),
        ("agent", "[start|launch|open|spin up] {agent} in {dir}: {prompt}", c),
        ("agent", "[start|launch|open|spin up] {agent} in {dir} and have it {prompt}", c),
        ("agent", "[start|launch|open] {agent} in {dir} {place}", c),
        ("agent", "[start|launch|open] {agent} in {dir} {place}: {prompt}", c),
        ("agent", "[start|launch|open] {agent} in {dir} with model {model}", c),
        ("agent", "[start|launch|open] {agent} with model {model} in {dir}: {prompt}", c),
        ("agent", "[start|open] a new {agent} session in {dir}", c),
        ("agent", "spawn {agent} in {dir}", c),
        ("agent", "spawn {agent} in {dir}: {prompt}", c),
        ("agent", "[start|launch|open|spawn] {agent} {place} in {dir}", c),
        ("agent", "[start|launch|open|spawn] {agent} {place} in {dir}: {prompt}", c),
        ("agent", "[start|launch|open|spawn] {agent} in {dir} using model {model}", c),
        ("agent", "[start|launch|open|spawn] {agent} in {dir} using model {model}: {prompt}", c),
        ("wait", "wait [until|till|for] [the |]{s_agent}[ session|] in {s_dir} [is idle|is done|finishes|to finish]",
         c, {"for": "idle"}),
        ("wait", "wait [until|till] [the |]{s_agent}[ session|] in {s_dir} [needs input|asks something|is waiting for approval|asks for approval]",
         c, {"for": "waiting"}),
        ("wait", "[block|wait] until [the |]{s_agent}[ session|] in {s_dir} is idle, [max|up to|timeout] {secs} seconds",
         c, {"for": "idle"}),
        ("wait", "wait until [the |]{s_agent}[ session|] in {s_dir} is [idle|done], [max|up to|timeout] {mins} minutes",
         c, {"for": "idle", "minutes": True}),
        ("tell", "[tell|message|ask] [the |]{s_agent}[ session|] in {s_dir}: {text}", c),
        ("tell", "send [the |]{s_agent}[ session|] in {s_dir} the message: {text}", c),
        ("tell", "tell [the |]{s_agent}[ session|] in {s_dir} to {text}", c),
    ]


def _apps_templates():
    import apps
    names = [(name, {"app": app}) for app, aliases in apps.LAUNCH_NAMES.items() for name in aliases]
    sections = [(name, {"section": section}) for section, aliases in apps.SECTION_NAMES.items() for name in aliases]
    names += [(app, {"app": app}) for app in apps.LAUNCHABLE if (app, {"app": app}) not in names]
    c = {"app": names, "section": sections}
    return [
        ("launch", "start a game of {app}", c),
        ("launch", "play {app}", c),
        ("settings", "{section} settings[ please|]", c),
        ("settings", "open the {section} section of [the |]settings( screen)", c),
        ("settings", "[take me to|jump to|go to|open] the {section} [section|page|tab] of [the |]settings( screen)", c),
        ("settings", "open the settings screen for [the |]{section}", c),
        ("settings", "I want to change the {section} settings, open them", c),
        ("launch", "[open|launch|start|run|fire up|bring up] [the |]{app}", c),
        ("launch", "[open|launch|start] [the |]{app}( in a new tab)[ please|]", c),
        ("launch", "[can you |please |]open [the |]{app}", c),
        ("settings", "[open|show|go to|take me to] [the |]{section} settings", c),
        ("settings", "[open|show] [the |]settings [at|for|on] [the |]{section}( section)", c),
        ("settings", "[open|show] settings for [the |]{section}", c),
    ]


def _tmux_templates():
    c = {"target": _target, "name": copy("name", *TMUX_SESSIONS + ["scratch2", "build-7", "api_v2", "night"]),
         "new_name": copy("new_name", *("old", "ci-old", "api2", "archive", "done", "paused", "v2", "stage")),
         "sess": copy("target", *TMUX_SESSIONS), "cwd": _quoted("cwd", DIRS), "bcwd": copy("cwd", *DIRS),
         "lines": number("lines", 1, 2000, (10, 20, 30, 50, 100, 200, 500)),
         "pct": [(f"%{n}", {"target": f"%{n}"}) for n in range(41)]}
    return [
        ("tmux_list", "[list|show] [the |all |my |][tmux |]sessions[.|]", c),
        ("tmux_list", "[ls|tmux ls|what tmux sessions are there|which sessions are running|list tmux sessions please]", c),
        ("tmux_list", "[show me|give me|get] [the |][tmux |]sessions", c),
        ("tmux_list", "[show me|give me|get] [a|the] list of [tmux |]sessions", c),
        ("tmux_new", "new session {name}", c),
        ("tmux_new", "new session {name} in {cwd}", c),
        ("tmux_new", "[create|start|open|make] a [new |][tmux |]session [named|called] {name}", c),
        ("tmux_new", "[create|start|open|make] a [new |][tmux |]session [named|called] {name} in {bcwd}", c),
        ("tmux_new", "tmux new -s {name}", c),
        ("tmux_new", "create [tmux |]session {name}", c),
        ("tmux_new", "[create|start|new] [tmux |]session {name} in {bcwd}", c),
        ("tmux_read", "[read|show] [target |]{pct}", c),
        ("tmux_list", "[which|what] sessions [exist|are there|are running|are open](?)", c),
        ("tmux_list", "tmux list(-sessions)", c),
        ("tmux_list", "list [current|every|all|all the|the current] [tmux |]sessions( on the server)", c),
        ("tmux_list", "sessions?", c),
        ("tmux_list", "what sessions does this tmux server have(?)", c),
        ("tmux_read", "read the {sess} session", c),
        ("tmux_read", "read pane {pct}, last {lines} lines", c),
        ("tmux_read", "[tmux |]capture-pane -t {target}", c),
        ("tmux_read", "what does {target} show(?)", c),
        ("tmux_read", "read {target}", c),
        ("tmux_read", "read {target} last {lines} lines", c),
        ("tmux_read", "[show|capture|read|get] the last {lines} lines [of|from] {target}", c),
        ("tmux_read", "[show me|what's in|what is in|capture|dump] {target}[.|]", c),
        ("tmux_read", "[show|read|capture] [the |]output of {target}", c),
        ("tmux_read", "capture-pane -p -t {target}", c),
        ("tmux_rename", "rename session {sess} to {new_name}", c),
        ("tmux_rename", "rename [the |][tmux |]session {sess} [to|as] {new_name}", c),
        ("tmux_rename", "[call|name] [the |]session {sess} {new_name} [now|instead|]", c),
        ("tmux_rename", "tmux rename-session -t {sess} {new_name}", c),
    ]


def _pty_templates():
    c = {"id": _pty_id("id"), "jid": _journal_id, "pane": number("pane_id", 2, 999, ()),
         "lines": number("max_lines", 1, 1000, (10, 20, 30, 50, 100, 200))}
    return [
        ("pty_list", "[list|show] [all |the |my |][pty |persistent |broker |]sessions", c),
        ("pty_list", "[what|which] [pty |broker |]sessions [are there|exist|are running]", c),
        ("pty_journals", "[list|show] [all |the |][archived|saved] journals", c),
        ("pty_status", "[show|get|describe|inspect] [the |][status|state|details] of session {id}", c),
        ("pty_status", "[show|inspect] session {id}", c),
        ("pty_status", "what is session {id} [running|doing]", c),
        ("pty_pane", "[which|what] session is [behind|in|running in] [the |]pane {pane}", c),
        ("pty_pane", "[show|get] the session [of|for|behind] pane {pane}", c),
        ("pty_observe", "[show|read|get|tail] the last {lines} lines [of|from] session {id}", c),
        ("pty_observe", "last {lines} lines of [the output of |]session {id}", c),
        ("pty_journal", "[show|read|get] [the |][archived |]journal {jid}", c),
        ("pty_foreground", "show foreground processes of session {id}", c),
        ("pty_foreground", "what is running in the foreground of session {id}", c),
    ]


def _logs_templates():
    def title(rng):
        v = rng.choice(LOG_TITLES)
        q = rng.choice(('"', "'"))
        return f"{q}{v}{q}", {"session": v}
    c = {"pane": number("pane", 2, 999, ()), "title": title,
         "query": _quoted("query", SEARCH), "bquery": copy("query", "timeout", "panic", "OOM", "Traceback",
                                                           "EADDRINUSE", "denied", "retrying"),
         "event": copy("event", *EVENTS), "file": copy("file", *LOG_FILES),
         "codex_file": [(f, {"file": f, "provider": "codex"}) for f in SESSION_FILES["codex"]],
         "claude_file": [(f, {"file": f, "provider": "claude"}) for f in SESSION_FILES["claude"]],
         "limit": number("limit", 1, 100, (5, 10, 20, 30, 50))}
    return [
        ("logs_brief", "[give me |show |get |][a |the |]brief [of|for|on] [the session in |]pane {pane}", c),
        ("logs_brief", "[summarize|recap] the session in pane {pane}", c),
        ("logs_brief", "[give me |show |][a |the |]brief [of|for|on] the session [titled|named|called] {title}", c),
        ("logs_events", "[show|read|list|get] [the |][events|log|transcript|session log] [of|for|from] pane {pane}", c),
        ("logs_events", "[show|read|get] the last {limit} events [of|from] pane {pane}", c),
        ("logs_events", "[show|read] the [log|transcript|events] in pane {pane}", c),
        ("logs_events", "[show|read] the [log|events] of the session [titled|named|called] {title}", c),
        ("logs_events", "[read|show] the [raw |plain |]log file {file}", c, {"provider": "raw"}),
        ("logs_events", "[read|show] the codex session file {codex_file}", c),
        ("logs_events", "[read|show] the claude session file {claude_file}", c),
        ("logs_search", "search [the |]pane {pane}[ log|] for {query}", c),
        ("logs_search", "search pane {pane} for {bquery}", c),
        ("logs_search", "search the [log|logs|transcript|events] of pane {pane} for {query}", c),
        ("logs_search", "find {query} in the [log|transcript] of pane {pane}", c),
        ("logs_search", "search the [raw |]log file {file} for {query}", c, {"provider": "raw"}),
        ("logs_source", "[show|get|read] [me |][the |]source [of|for] event {event}", c),
        ("logs_events", "read events from the session [titled|named|called] {title}", c),
        ("logs_search", "search pane {pane}'s [session|log|transcript] for {query}", c),
        ("logs_search", "search [the |]session {title} for {query}", c),
        ("logs_brief", "summarize the session {title}", c),
        ("logs_events", "read the [plain-text|plain text|raw] log {file}", c, {"provider": "raw"}),
        ("logs_search", "[look for|search for|find] {query} in {file}", c, {"provider": "raw"}),
        ("logs_search", "search {file} for {query}", c, {"provider": "raw"}),
        ("logs_source", "[raw source|the raw source|the source] of {event}", c),
        ("logs_source", "show [the |]source of [event |]{event}", c),
    ]


def _read_pane_templates():
    unit = pool(("panes", {"what": "panes"}), ("tabs", {"what": "tabs"}), ("panes and tabs", {"what": "panes and tabs"}),
                ("tabs and panes", {"what": "panes and tabs"}))
    c = {"pane": number("pane", 2, 999, ()), "lines": number("lines", 1, 1000, (10, 20, 30, 40, 50, 60, 80, 100,
                                                                                 150, 200, 300, 500)), "unit": unit}
    return [
        ("read_pane", "[read|show|get|print|dump|capture|grab|fetch|view] pane {pane}", c),
        ("read_pane", "[read|show|get|capture] [the |][screen|text|output|contents] of pane {pane}", c),
        ("read_pane", "[read|show|get|print|capture] [the |][screen|text|output|contents] [of|in|from|on] pane {pane}", c),
        ("read_pane", "[read|show|get|print|capture] the last {lines} lines [of|in|from|on] pane {pane}", c),
        ("read_pane", "[show me|get me] the last {lines} lines [of|from] pane {pane}( output)", c),
        ("read_pane", "what[ is|'s] on pane {pane}( right now)", c),
        ("read_pane", "what does pane {pane} [show|say]", c),
        ("read_pane", "last {lines} lines of pane {pane}", c),
        ("read_pane", "[read|show|get] [the |]screen of pane {pane} to see if [the build finished|it is done|the tests passed]", c),
        ("read_pane", "[read|show] pane {pane}[.|, thanks|]", c),
        ("read_pane", "check [the |]output [of|in] pane {pane}", c),
        ("read_pane", "get the text of pane {pane}, last {lines} lines", c),
        ("read_pane", "get-text pane {pane}", c),
        ("read_pane", "[show|read] the bottom {lines} lines of pane {pane}", c),
        # wordings of experiment 11's development half
        ("read_pane", "last {lines} lines [of|from|in] pane {pane}[ please|]", c),
        ("read_pane", "[read|show|get|print|capture|fetch|grab|tail] pane {pane}, [last|only the last|the last] {lines} lines",
         c),
        ("read_pane", "[read|show|get|fetch|grab] the [last|most recent|latest] {lines} lines [of|in|from|out of] pane {pane}",
         c),
        ("read_pane", "tail -n {lines} pane {pane}", c),
        ("read_pane", "tail pane {pane}", c),
        ("read_pane", "[pull|fetch|get|grab|cat|show|read|scrape|capture|dump] the [screen|screen text|contents|output|"
                      "visible screen|whole visible screen|text] [of|from|in] pane {pane}", c),
        ("read_pane", "pane {pane} [contents|output|screen][ please|, please|]", c),
        ("read_pane", "pane {pane}: what's on [screen|the screen](?)", c),
        ("read_pane", "what's on [the |]screen in pane {pane}(?)", c),
        ("read_pane", "what's the latest output in pane {pane}(?)", c),
        ("read_pane", "[read|show|get] pane id {pane}", c),
        ("read_pane", "[read|show|get] pane #{pane}", c),
        ("read_pane", "[get|show|read|scrape|fetch] pane {pane}'s [screen|screen text|output|contents]", c),
        ("read_pane", "[read|show|get] pane {pane}'s last {lines} lines", c),
        ("read_pane", "show the tail of pane {pane}", c),
        ("read_pane", "show the tail of pane {pane}, {lines} lines", c),
        ("read_pane", "read pane {pane} without switching to it", c),
        # the bundle's list of panes and tabs
        ("list", "[list|show|show me|enumerate] [all |every |the |all the |open |all open |]{unit}", c),
        ("list", "[list|show|show me|enumerate] [all |the |]{unit} [right now|in kilix|in all tabs|across all tabs]", c),
        ("list", "what {unit} are [there|open]( [right now|in kilix|in kilix right now])(?)", c),
        ("list", "[list|show] [the |all |every |]{unit} and their titles", c),
        ("list", "list open tabs and the panes inside them", c, {"what": "panes and tabs"}),
        ("list", "list every pane and tab", c, {"what": "panes and tabs"}),
    ]


TEMPLATES = {"files": _files_templates, "system": _system_templates, "agents": _agents_templates,
             "apps": _apps_templates, "tmux": _tmux_templates, "pty": _pty_templates, "logs": _logs_templates,
             "panes": _read_pane_templates}


# ---- what a row is checked against -----------------------------------------

def _grammar_reads_at_all(job: str, request: str) -> bool:
    """Whether the files or system grammar reads the request at all."""
    if job == "files":
        import files_job
        try:
            files_job.parse(request)
        except ValueError:
            return False
        return True
    import system_job
    return bool(system_job.parse(request))


def _grammar_reads(job: str, request: str, calls: list) -> bool:
    """Whether the job's grammar reads the request as these job calls."""
    if job == "files":
        import files_job
        try:
            want = files_job.parse(request)
        except ValueError:
            return False
        return [{"name": a.kind, "arguments": a.args} for a in want] == calls
    if job == "system":
        import system_job
        want = system_job.parse(request)
        if not want:
            return False
        return system_job.interpret(request, calls) == want
    if job == "agents":
        import agents
        from actions import Refusal
        got = agents.interpret(request, calls)
        return bool(got) and not any(isinstance(a, Refusal) for a in got)
    if job == "apps":
        import apps
        from actions import Refusal
        got = apps.interpret(request, calls)
        return bool(got) and not any(isinstance(a, Refusal) for a in got) and \
            [{"name": a.kind, "arguments": dict(a.args)} for a in got] == calls
    raise ValueError(job)


def admitted(job: str, request: str, calls: list) -> bool:
    """Whether a row's reading is one its job takes, as rows() keeps it."""
    if job == "panes":
        import pane_reads
        import panes_bundle
        if len(calls) == 1 and calls[0]["name"] == panes_bundle.LIST:
            return calls[0]["arguments"].get("what") in ("panes", "tabs", "panes and tabs")
        return len(calls) == 1 and calls[0]["name"] == pane_reads.NAME and \
            pane_reads.check_args(calls[0]["arguments"]) is None
    got = job_schemas.to_job(job, calls)
    if got is None:
        return False
    if job in ("files", "system"):
        # Experiment 11, the owner's eighth answer: arm B admits a files or
        # system answer that equals the grammar's reading or that the job's
        # own checks of a proposal take (job_routes.files_checks,
        # job_routes.system_checks). A row is kept on stricter terms: where
        # the grammar reads the request, only as the grammar reads it, so
        # that no row teaches a reading the grammar contradicts.
        if _grammar_reads_at_all(job, request):
            return _grammar_reads(job, request, got)
        import job_routes
        if job == "files":
            return job_routes.files_checks(got) is not None
        return job_routes.system_checks(request, got) is not None
    if job in ("agents", "apps"):
        return _grammar_reads(job, request, got)
    if job == "tmux":
        import tmux_job
        return len(got) == 1 and tmux_job.reads_back(got[0])
    if job == "pty":
        import pty_job
        return len(got) == 1 and pty_job.reads_back(got[0])
    if job == "logs":
        import logs_words
        return len(got) == 1 and logs_words.check(got[0]) is None
    raise ValueError(job)


def fold(text: str) -> str:
    """A request as a history or eval request is compared with it: case,
    spaces and the marks at its end do not count."""
    return " ".join(text.casefold().split()).strip(" .!?")


def same(text: str) -> str:
    """A request as two rows are compared: case and spaces do not count, the
    marks at its end do (the reader's frame holds them)."""
    return " ".join(text.casefold().split())


# The most wordings one template gives (the apps' launch templates come
# nearest: every name of every app in every wording).
CAP = 4000


def rows(job: str, seed: int, per_template: int = 60, exclude: set | None = None, cap: int = CAP):
    """(rows, counts): {query, actions, spans, template} rows for one job.

    Each template gives every wording it has, with every choice of its slots
    whose words stay in the reader's frame (closed values, quote marks), or
    `cap` of them drawn at random when it has more; its copied values and
    numbers are drawn at random. A template with fewer wordings than
    `per_template` gives each one several times, with other values. Each row
    is also given without its final mark, with a full stop, and, for a
    question, with a question mark. Only rows its job takes are kept
    (admitted)."""
    rng = random.Random(f"{job}:{seed}")
    out, seen, counts = [], set(), collections.Counter()
    for index, entry in enumerate(TEMPLATES[job]()):
        tool, template, pools = entry[0], entry[1], entry[2]
        fixed = entry[3] if len(entry) > 3 else {}
        combos = _combos(job, tool, template, pools, rng, cap)
        counts["wordings"] += len(combos)
        reps = max(1, math.ceil(per_template / len(combos)))
        for skeleton, chosen in combos:
            for _ in range(reps):
                written, args, spans = _fill(skeleton, pools, rng, chosen)
                args = {**fixed, **args}
                for text in _endings(written, spans):
                    counts["generated"] += 1
                    if same(text) in seen:
                        counts["same_as_another_row"] += 1
                        continue
                    if exclude is not None and fold(text) in exclude:
                        counts["excluded"] += 1
                        continue
                    seen.add(same(text))
                    calls = [{"name": tool, "arguments": args}]
                    if not admitted(job, text, calls):
                        counts["not_admitted"] += 1
                        counts[f"not_admitted {index} {tool}"] += 1
                        continue
                    out.append({"query": text, "actions": [[tool, args]],
                                "spans": [[surface, value] for surface, value in spans],
                                "template": f"{job}:{index}"})
                    counts["kept"] += 1
    return out, dict(counts)
