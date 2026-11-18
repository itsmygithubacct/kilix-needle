#!/usr/bin/env python3
"""Agent-style training rows for the panes bundle (panes_bundle_train.py --agent).

    python3 -B panes_bundle_agent.py OUT.jsonl [--seed N] [--per-template N]

The tuner's generator writes requests as people type them. Agents write
otherwise: they name panes and tabs by the ids a list gave them ("close pane
412"), end with a full stop or "only", give long commands and programs ("open
a new tab named api-v2 running python3 -m http.server 8000"), join two
requests with ";" as the tool's own example does ("rename tab id 1 to x; make
pane 2 wider by 5"), and ask for lists with words the exact list reader does
not take ("list pane 412 and its title"). These templates were written by
hand in those kinds of wording after the request history of one machine was
read for its wording only (2026-11-18). No line of it is a template or a row:
the trainer drops a row equal to one of its lines (--exclude-history) and
counts it.

Each row is {"query", "actions", "spans", "template"}, as the generator's
rows are: `spans` are [surface, value] pairs, the words of the request each
copied value came from. A pane named by its number has that number as its
value, as a proposal gives it; the checks admit it as the pane's id. A tab
named by "tab id N" is `id:N` (the reader reads a number after "id" that
way); "tab N" is the tab's place, N. `list` is the bundle's own kind: the
route answers it with the list the exact reader gives (needle_cli), and its
`about` holds what the request names beside it (a pane's number, a title),
which the list does not need and the reader must account for.

Names, commands and programs are made of words that are not the bundle's
wording. The trainer drops any row with a copied word that it knows only as
wording, so that the hold on such words (panes_bundle, PLAIN) keeps its force.

Standard library only. The same seed gives the same rows.
"""
from __future__ import annotations

import argparse
import json
import random
import sys

# Pieces of names: technical words that the bundle's wording does not use.
_NAME_WORDS = ("infra", "etl", "auth", "billing", "search", "cache", "gateway", "worker",
               "ingest", "scheduler", "notebook", "bench",
               "eval", "trainer", "proxy", "staging", "canary", "sandbox", "vm",
               "lint", "migrate", "backup", "sync", "relay", "watcher", "tracer", "profiler", "dash",
               "report", "preview", "capture", "render", "indexer", "crawler", "mailer", "router",
               "vault", "ledger", "sensor", "uploader", "exporter", "importer", "replica", "shard", "spool",
               "kiosk", "beacon", "atlas", "orbit", "nimbus", "quartz", "falcon", "otter", "maple", "cedar")
_SUFFIXES = ("tests", "smoke", "nightly", "v2", "v3", "0.3.1", "1.4.0", "rc2", "east",
             "west", "ui", "cli", "box", "lab", "01", "02")
_PROGRAMS = ("htop", "btop", "nvim", "less", "redis-cli", "psql", "sqlite3", "ipython", "lazygit", "k9s",
             "ncdu", "mc", "ranger", "glances", "bpytop", "tig", "mutt", "weechat", "irssi", "cmus")
_HOSTS = ("build-box", "gpu-01", "gpu-02", "staging-db", "edge-03", "lab-pc", "nas", "bastion", "pi4", "rack7")
_DIRS = ("/home/user/src/app", "/home/user/projects/etl", "/srv/data", "/opt/tools", "/var/tmp/run-17",
         "~/work/infra", "~/src/web-ui", "/mnt/scratch/jobs", "/home/user/research/bench", "./scripts")
_FILES = ("serve.py", "sync.py", "train.py", "launch.sh", "check.sh", "main.go", "index.js",
          "player.py", "worker.py", "bench.py", "ingest.py", "deploy.sh")
# For the rows that ask for nothing an action does: their own words.
_NONE_DIRS = ("/data/archive", "/tmp/scratch-area", "~/notes", "/var/cache/builds", "~/Downloads")
_NONE_NAMES = ("scratchpad", "misc", "playground", "inbox")
_FLAGS = ("--verbose", "--watch", "-x", "--release", "--dry-run", "-q", "--follow",
          "--no-cache", "--json", "--color=always", "--fast", "--once", "-vv")


class Values:
    """Copied texts and numbers, drawn from one seeded generator."""

    def __init__(self, rng: random.Random):
        self.rng = rng

    def pane_id(self) -> str:
        r = self.rng.random()
        low, high = (2, 9) if r < 0.08 else (10, 99) if r < 0.35 else (100, 999) if r < 0.85 else (1000, 1999)
        return str(self.rng.randint(low, high))

    def tab_place(self) -> str:
        return str(self.rng.randint(1, 9))

    def tab_id(self) -> str:
        return str(self.rng.randint(1, 240))

    def amount(self) -> int:
        return self.rng.choice((1, 2, 3, 4, 5, 5, 8, 10, 10, 12, 15, 20, 20, 25, 30, 40, 50))

    def place(self) -> int:
        return self.rng.randint(1, 9)

    def word(self) -> str:
        return self.rng.choice(_NAME_WORDS)

    def name(self) -> str:
        """Mostly one word, as agents name things ("api-v2"); now and then
        two or three."""
        rng = self.rng
        r = rng.random()
        if r < 0.75:
            return rng.choice((self.word(), f"{self.word()}-{rng.choice(_SUFFIXES)}",
                               f"{self.word()}{rng.randint(1, 9)}", f"{self.word()}-{self.word()}",
                               f"{self.word()}_{rng.choice(_SUFFIXES)}"))
        if r < 0.93:
            return f"{self.word()} {rng.choice(_SUFFIXES)}"
        return f"{self.word()} {rng.choice(_SUFFIXES)} {rng.choice(_SUFFIXES)}"

    def path(self) -> str:
        return f"{self.rng.choice(_DIRS)}/{self.rng.choice(_FILES)}"

    def program(self, most: int = 9) -> str:
        """A program to start: from one word to a command with paths and flags."""
        rng = self.rng
        forms = [
            lambda: rng.choice(_PROGRAMS),
            lambda: rng.choice(_PROGRAMS),
            lambda: self.path(),
            lambda: f"python3 {self.path()}",
            lambda: f"/usr/bin/python3 {self.path()} --once",
            lambda: f"ssh {rng.choice(('-tt', '-t'))} {rng.choice(_HOSTS)}",
            lambda: f"python3 -m http.server {rng.choice((8001, 8765, 9001, 5173))}",
            lambda: f"journalctl {rng.choice(('-f', '--follow'))} -u {self.word()}.service",
            lambda: f"bash {self.path()} {rng.choice(_FLAGS)}",
            lambda: f"python3 {self.path()} --config {self.word()}.toml {rng.choice(_FLAGS)}",
            lambda: f"mpv {rng.choice(_DIRS)}/{self.word()}-{rng.randint(1, 20)}.mp4",
            lambda: f"watch -n {rng.choice((15, 30, 45))} nvidia-smi",
        ]
        while True:
            text = rng.choice(forms)()
            if len(text.split()) <= most:
                return text

    def command(self, most: int = 9) -> str:
        """A command to type at a shell prompt."""
        rng = self.rng
        forms = [
            lambda: self.program(most),
            lambda: f"{rng.choice(('pytest', 'ruff format', 'go vet', 'cargo clippy'))} {rng.choice(_FLAGS)}",
            lambda: f"git {rng.choice(('fetch --all', 'log --oneline -5', 'stash show', 'remote -v'))}",
            lambda: f"cd {rng.choice(_DIRS)} && {rng.choice(('uv sync', 'cargo fetch', 'go mod tidy'))}",
            lambda: f"export {self.word().upper()}_HOME={rng.choice(_DIRS)}",
            lambda: f"tail -n {rng.choice((30, 60, 200))} {rng.choice(_DIRS)}/{self.word()}.log",
            lambda: f"ls -la {rng.choice(_DIRS)}",
            lambda: f"curl -s localhost:{rng.choice((8001, 8765, 3001))}/{self.word()} | jq -r .{self.word()}",
            lambda: f"echo {self.word()}-{rng.choice(('ready', 'ok', 'up'))}",
            lambda: f"source {rng.choice(_DIRS)}/.venv/bin/activate",
            lambda: f"du -sh {rng.choice(_DIRS)}",
            lambda: f"df -h",
            lambda: f"nvidia-smi",
        ]
        while True:
            text = rng.choice(forms)()
            if len(text.split()) <= most:
                return text

    def directory(self) -> str:
        return self.rng.choice(_DIRS)


def _row(query: str, actions: list, spans: list) -> dict:
    return {"query": query, "actions": actions, "spans": spans}


def _ending(rng: random.Random, words: str, *, only: bool = False) -> str:
    """The request as agents end it: as it is, with a full stop, or with
    "only". Not a full stop after a path: the reader keeps a full stop that
    ends a path as part of it."""
    r = rng.random()
    if only and r < 0.25:
        return words + " only."
    if only and r < 0.35:
        return words + " only"
    if r < 0.65 and "/" not in words.split()[-1]:
        return words + "."
    return words


def _cap(rng: random.Random, text: str) -> str:
    return text[:1].upper() + text[1:] if rng.random() < 0.5 else text


SIDES = (("right", "right"), ("left", "left"), ("below", "below"), ("above", "above"))
SIDE_WORDS = SIDES + (("down", "below"), ("up", "above"))
DIRECTIONS = ("wider", "narrower", "taller", "shorter")


# A list of one pane, and what no action does (templates "list one N" and
# "none N"). With a read of a pane among the kinds (bundle format v6), the
# frames of READ_FRAMES ask for a read: rows(reads=True) leaves their rows
# out, and every other row is the one rows() makes without it.
LIST_ONE = ("list pane {p}", "show pane {p}", "list pane {p} and its title", "show pane {p} and its title",
            "list pane {p} and its current command", "describe pane {p}",
            "list pane {p} with its title and tab", "list all panes including pane {p}")
NONE = ("open a new tab in {d}", "open a new tab titled {n} in {d}", "open a new tab in {d} running htop",
        "open a new tab titled {n} in {d} and run htop", "open a pane to the right in {d}",
        "split pane {p} to the right", "split pane {p} down", "split pane {p} vertically",
        "show the screen text of pane {p}", "read the output of pane {p}",
        "show the last lines of pane {p}", "what is pane {p} doing", "is pane {p} idle")
READ_FRAMES = ("show pane {p}", "show the screen text of pane {p}", "read the output of pane {p}",
               "show the last lines of pane {p}")
READ_TEMPLATES = frozenset([f"list one {LIST_ONE.index(f)}" for f in READ_FRAMES if f in LIST_ONE] +
                           [f"none {NONE.index(f)}" for f in READ_FRAMES if f in NONE])


def templates(v: Values) -> dict:
    """{template name: a function that makes one row}."""
    rng = v.rng
    t = {}

    def add(name):
        def wrap(fn):
            t[name] = fn
            return fn
        return wrap

    def by_pane(name, frames, make, *, only=False):
        for i, frame in enumerate(frames):
            @add(f"{name} {i}")
            def _(frame=frame):
                p = v.pane_id()
                words, actions, spans = make(frame, p)
                return _row(_cap(rng, _ending(rng, words, only=only)), actions, [[p, p]] + spans)

    # ---- a pane named by its number
    by_pane("close_pane", ("close pane {p}", "kill pane {p}", "close window {p}", "quit pane {p}", "exit pane {p}",
                           "close the pane {p}"),
            lambda f, p: (f.format(p=p), [["close_pane", {"pane": p}]], []), only=True)
    by_pane("go_to_pane", ("go to pane {p}", "focus pane {p}", "switch to pane {p}", "focus pane id {p}",
                           "move focus to pane {p}", "jump to pane {p}", "select pane {p}", "go to window {p}"),
            lambda f, p: (f.format(p=p), [["go_to_pane", {"pane": p}]], []))
    by_pane("maximize_pane", ("maximize pane {p}", "zoom pane {p}"),
            lambda f, p: (f.format(p=p), [["maximize_pane", {"pane": p}]], []))

    def rename(f, p):
        n = v.name()
        return f.format(p=p, n=n), [["rename_pane", {"pane": p, "name": n}]], [[n, n]]
    by_pane("rename_pane id", ("rename pane {p} to {n}", "set the title of pane {p} to {n}", "title pane {p} {n}",
                               "change the title of pane {p} to {n}", "rename window {p} to {n}"), rename)

    def typing(f, p):
        c = v.command()
        return f.format(p=p, c=c), [["run_in_pane", {"pane": p, "command": c}]], [[c, c]]
    by_pane("run_in_pane id", ("type {c} in pane {p}", "run {c} in pane {p}", "type {c} into pane {p}",
                               "execute {c} in pane {p}", "run {c} in window {p}", "type {c} in pane id {p}",
                               "run the command {c} in pane {p}", "type the command {c} into pane {p}",
                               "run '{c}' in pane {p}", "type '{c}' in pane {p}", "run \"{c}\" in pane {p}",
                               "type \"{c}\" into pane {p}"), typing)

    def resize(f, p):
        d, a = rng.choice(DIRECTIONS), v.amount()
        args = {"pane": p, "direction": d}
        spans = []
        if "{a}" in f:
            args["amount"] = a
            spans.append([str(a), a])
        return f.format(p=p, d=d, a=a), [["resize_pane", args]], spans
    by_pane("resize_pane id", ("make pane {p} {d} by {a}", "make pane {p} {d}", "make pane id {p} {d} by {a}"), resize)

    # ---- a tab named by its place or its id
    for i, frame in enumerate(("close tab {t}", "kill tab {t}")):
        @add(f"close_tab {i}")
        def _(frame=frame):
            n = v.tab_place()
            return _row(_cap(rng, _ending(rng, frame.format(t=n), only=True)), [["close_tab", {"tab": n}]], [[n, n]])
    for i, frame in enumerate(("go to tab {t}", "focus tab {t}", "switch to tab {t}", "activate tab {t}",
                               "jump to tab {t}")):
        @add(f"go_to_tab {i}")
        def _(frame=frame):
            n = v.tab_place()
            return _row(_cap(rng, _ending(rng, frame.format(t=n))), [["go_to_tab", {"tab": n}]], [[n, n]])
    for i, frame in enumerate(("rename tab {t} to {n}", "set the title of tab {t} to {n}",
                               "change the title of tab {t} to {n}")):
        @add(f"rename_tab place {i}")
        def _(frame=frame):
            k, n = v.tab_place(), v.name()
            return _row(_cap(rng, _ending(rng, frame.format(t=k, n=n))),
                        [["rename_tab", {"tab": k, "name": n}]], [[k, k], [n, n]])
    for i, frame in enumerate(("rename tab id {t} to {n}", "set the title of tab id {t} to {n}",
                               "change the title of tab id {t} to {n}")):
        @add(f"rename_tab id {i}")
        def _(frame=frame):
            k, n = v.tab_id(), v.name()
            return _row(_cap(rng, _ending(rng, frame.format(t=k, n=n))),
                        [["rename_tab", {"tab": f"id:{k}", "name": n}]], [[k, f"id:{k}"], [n, n]])

    # ---- the caller's own pane or tab
    for i, frame in enumerate(("rename this tab to {n}", "rename the current tab to {n}", "set this tab's title to {n}",
                               "title this tab {n}")):
        @add(f"rename_tab current {i}")
        def _(frame=frame):
            n = v.name()
            return _row(_cap(rng, _ending(rng, frame.format(n=n))), [["rename_tab", {"name": n}]], [[n, n]])
    for i, frame in enumerate(("rename this pane to {n}", "set this pane's title to {n}", "title the current pane {n}")):
        @add(f"rename_pane current {i}")
        def _(frame=frame):
            n = v.name()
            return _row(_cap(rng, _ending(rng, frame.format(n=n))), [["rename_pane", {"name": n}]], [[n, n]])
    for i, frame in enumerate(("make this pane {d} by {a}", "make the current pane {d} by {a}")):
        @add(f"resize_pane current {i}")
        def _(frame=frame):
            d, a = rng.choice(DIRECTIONS), v.amount()
            return _row(_cap(rng, _ending(rng, frame.format(d=d, a=a))),
                        [["resize_pane", {"direction": d, "amount": a}]], [[str(a), a]])
    for i, frame in enumerate(("move this tab to position {k}", "move the current tab to position {k}")):
        @add(f"move_tab {i}")
        def _(frame=frame):
            k = v.place()
            return _row(_cap(rng, _ending(rng, frame.format(k=k))), [["move_tab", {"position": k}]], [[str(k), k]])

    # ---- open a tab or a pane
    for i, frame in enumerate(("open a new tab running {g}", "open a tab running {g}", "new tab running {g}",
                               "open a new tab and run {g}", "start {g} in a new tab", "launch {g} in a new tab",
                               "open a new tab with {g}")):
        @add(f"open_tab program {i}")
        def _(frame=frame):
            g = v.program()
            return _row(_cap(rng, _ending(rng, frame.format(g=g))), [["open_tab", {"program": g}]], [[g, g]])
    for i, frame in enumerate(("open a new tab named {n} running {g}", "open a new tab titled {n} running {g}",
                               "open a tab named {n} running {g}", "open a tab titled {n} running {g}",
                               "open a new tab called {n} running {g}", "new tab named {n} running {g}",
                               "open a new tab named {n} and run {g}", "open a new tab titled {n} and run {g}")):
        @add(f"open_tab named program {i}")
        def _(frame=frame):
            n, g = v.name(), v.program()
            return _row(_cap(rng, _ending(rng, frame.format(n=n, g=g))),
                        [["open_tab", {"name": n, "program": g}]], [[n, n], [g, g]])
    for i, frame in enumerate(("open a new tab named {n}", "open a new tab titled {n}", "open a tab named {n}",
                               "new tab named {n}", "open a new tab called {n}")):
        @add(f"open_tab named {i}")
        def _(frame=frame):
            n = v.name()
            return _row(_cap(rng, _ending(rng, frame.format(n=n))), [["open_tab", {"name": n}]], [[n, n]])
    for i, frame in enumerate(("split the screen to the {s}", "split the screen {s}", "split the window to the {s}",
                               "open a new pane to the {s}", "open a pane on the {s}", "split {s}")):
        @add(f"open_pane side {i}")
        def _(frame=frame):
            word, side = rng.choice(SIDES if "the {s}" in frame else SIDE_WORDS)
            return _row(_cap(rng, _ending(rng, frame.format(s=word))), [["open_pane", {"side": side}]], [])
    for i, frame in enumerate(("open pane {s} running {g}", "open a new pane {s} running {g}", "split {s} and run {g}",
                               "open a pane to the {s} running {g}", "split {s} and start {g}",
                               "split the screen to the {s} and run {g}")):
        @add(f"open_pane program {i}")
        def _(frame=frame):
            word, side = rng.choice(SIDES if "the {s}" in frame else SIDE_WORDS)
            g = v.program(8)
            return _row(_cap(rng, _ending(rng, frame.format(s=word, g=g))),
                        [["open_pane", {"side": side, "program": g}]], [[g, g]])
    for i, frame in enumerate(("open a new pane to the {s} named {n}",)):
        @add(f"open_pane named {i}")
        def _(frame=frame):
            word, side = rng.choice(SIDES)
            n = v.name()
            return _row(_cap(rng, _ending(rng, frame.format(s=word, n=n))),
                        [["open_pane", {"side": side, "name": n}]], [[n, n]])

    # ---- lists
    pane_heads = ("list panes", "list all panes", "list the panes", "list the current panes", "list all kilix panes",
                  "list the open panes", "kilix pane list", "pane list", "show all panes", "show the panes",
                  "show me the panes", "get the pane list", "enumerate the panes", "list every pane")
    tab_heads = ("list tabs", "list the tabs", "list all tabs", "show the tabs", "show all tabs", "kilix tab list",
                 "list the open tabs", "enumerate the tabs")
    both_heads = ("list panes and tabs", "list tabs and panes", "list all panes and tabs", "list the current tabs and panes",
                  "list every pane in every tab", "show all panes and tabs", "list all kilix tabs and panes")
    pane_more = ("with their ids", "with ids and titles", "with their titles", "with pane ids", "including their ids",
                 "and their titles", "with titles and commands", "with their current commands", "with exact pane ids",
                 "and which one is focused", "with the tab each one is in", "include ids and titles",
                 "with their ids and titles", "and show their ids")
    tab_more = ("with their ids", "with ids and titles", "with their titles", "and their titles", "including their ids")
    for heads, more, what in ((pane_heads, pane_more, "panes"), (tab_heads, tab_more, "tabs"),
                              (both_heads, pane_more, "panes and tabs")):
        @add(f"list {what}")
        def _(heads=heads, more=more, what=what):
            words = rng.choice(heads)
            if rng.random() < 0.6:
                words += (", " if rng.random() < 0.3 else " ") + rng.choice(more)
            if rng.random() < 0.1:
                words += ". read only"
            return _row(_cap(rng, _ending(rng, words)), [["list", {"what": what}]], [])
    for i, frame in enumerate(LIST_ONE):
        @add(f"list one {i}")
        def _(frame=frame):
            p = v.pane_id()
            return _row(_cap(rng, _ending(rng, frame.format(p=p))), [["list", {"what": "panes", "about": p}]], [[p, p]])
    for i, frame in enumerate(("list panes whose title is {n}", "list the panes titled {n}", "list panes named {n}")):
        @add(f"list titled {i}")
        def _(frame=frame):
            n = f"{v.word()}-{rng.choice(_SUFFIXES)}"
            return _row(_cap(rng, _ending(rng, frame.format(n=n))), [["list", {"what": "panes", "about": n}]], [[n, n]])

    # ---- two clauses, joined as the tool's own example joins them. A close
    # and a list stand alone: the reader answers neither beside another clause.
    singles = sorted(name for name in t if not name.startswith(("list", "close")))
    for joiner in ("; ", ", then ", " and then ", " and "):
        @add(f"compound {joiner.strip()}")
        def _(joiner=joiner):
            while True:
                first, second = t[rng.choice(singles)](), t[rng.choice(singles)]()
                a, b = first["query"].rstrip(".").rstrip(), second["query"].rstrip(".").rstrip()
                if a.lower().endswith(" only") or b.lower().endswith(" only"):
                    continue
                b = b[:1].lower() + b[1:]
                end = "" if "/" in b.split()[-1] else rng.choice(("", "."))
                return _row(a + joiner + b + end, first["actions"] + second["actions"], first["spans"] + second["spans"])

    # ---- what no action does: a working directory, a split of another pane,
    # a pane's screen or state. Every word of these is wording to the bundle,
    # so their directories, names and numbers are their own: a word copied
    # elsewhere that it knew as wording would be a copied word made of
    # wording, and those rows are left out (panes_bundle_train.agent_rows).
    for i, frame in enumerate(NONE):
        @add(f"none {i}")
        def _(frame=frame):
            text = frame.format(d=rng.choice(_NONE_DIRS), n=rng.choice(_NONE_NAMES), p=str(rng.randint(2000, 2999)))
            return _row(_cap(rng, _ending(rng, text)), [], [])
    return t


def fold(text: str) -> str:
    """A request as the trainer compares it with another: case, spaces and
    the marks around it do not count."""
    return " ".join(str(text).casefold().split()).strip(" .!?,;:'\"")


def rows(seed: int = 0, per_template: int = 30, reads: bool = False) -> list[dict]:
    """per_template rows of each template (three times as many lists and
    pairs of clauses), in a fixed order; no two the same. `reads`: leave
    out the rows of READ_TEMPLATES, which a read of a pane answers."""
    rng = random.Random(seed)
    made = templates(Values(rng))
    out, seen = [], set()
    for name in sorted(made):
        want = per_template * (3 if name.startswith(("list", "compound")) else 1)
        got = tries = 0
        while got < want and tries < want * 20:
            tries += 1
            row = made[name]()
            key = fold(row["query"])
            if key in seen:
                continue
            seen.add(key)
            row["template"] = name
            out.append(row)
            got += 1
    return [row for row in out if row["template"] not in READ_TEMPLATES] if reads else out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--per-template", type=int, default=30)
    args = parser.parse_args(argv)
    made = rows(args.seed, args.per_template)
    with open(args.out, "w", encoding="utf-8") as handle:
        for row in made:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(len(made), "rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
