#!/usr/bin/env python3
"""Train the panes bundle that panes_bundle.py reads, and write panes-bundle/.

    uv run --with torch==2.6.0 panes_bundle_train.py --out panes-bundle --seeds 0 1

The rows are the tuner's own. They come from kilix-ml's kilix_panes pack with
kilix-needle's supplement templates, generated and filtered as
tuning.build_data does for the tuned model: every labelled action admitted by
the checks, no request of any eval set, no one action above its share. Each
row keeps the generator's record of which words an argument came from, which
is what the nets for copied text learn from.

`--spent` adds the cases of the job's spent eval sets (SPENT: dev, test and
the held-out sets that have gated), as the router is trained on them
(router-model/README.md). A bundle trained this way must not be scored on
those sets: the manifest lists them under rows.spent.

A current gate set (jobs.py) is never trained on. `--spent` and `--rows`
refuse one before it is opened. The generator does read every eval file,
the current gate set among them, as the tuner does: only to keep each of
their requests out of the rows it makes.

`--per-template N` asks the generator for more examples of each template than
the tuned model gets. Measured on four held-out sets: it does not help.

Training needs PyTorch (a CPU is enough: minutes). Reading needs nothing.
"""
from __future__ import annotations

import argparse
import array
import collections
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile

import panes_bundle as PB

REPO = Path(__file__).resolve().parent
SEPARATORS = {"and", "then", ",", ";"}      # used only to label training rows
DIM, HIDDEN, EPOCHS, RATE = 8, 32, 40, 0.01
CHECKED = 200       # examples of each net scored by PyTorch and again by the reader
# The job's eval sets that have gated and are no longer a gate: what --spent adds.
SPENT = ("evals/dev.jsonl", "evals/test.jsonl") + tuple(f"evals/heldout-v{n}.jsonl" for n in range(2, 15))


def schema() -> dict:
    """Each tool's arguments as the bundle treats them: a value of a closed
    list (enum), a flag, a number in a range (int), a pane or tab (ref: a word
    of place or copied text) or free text."""
    from actions import TOOLS
    tools = {}
    for tool in TOOLS:
        args = []
        for key, spec in tool["parameters"]["properties"].items():
            if "enum" in spec:
                args.append([key, "enum", None, None])
            elif spec.get("type") == "boolean":
                args.append([key, "flag", None, None])
            elif spec.get("type") == "integer":
                args.append([key, "int", spec.get("minimum", 0), spec.get("maximum", 10 ** 6)])
            else:
                args.append([key, "ref" if key in ("pane", "tab") else "text", None, None])
        tools[tool["name"]] = {"args": args, "required": list(tool["parameters"].get("required", []))}
    return tools


def build_rows(per_template: int | None = None, seed: int | None = None) -> tuple[list[dict], dict]:
    """The tuned model's training rows, with actions and spans kept."""
    import tuning
    import toolset
    from actions import Action, interpret
    manifest = tuning.recipe(tuning.load_manifest())
    data = dict(manifest["data"])
    if per_template is not None:
        data["per_template"] = per_template
    if seed is not None:
        data["seed"] = seed
    with tempfile.TemporaryDirectory(prefix="kn-bundle-") as scratch:
        staged = Path(scratch) / "library"
        added = tuning.stage_library(tuning.LIBRARY, data.get("supplements", []), staged)
        corpus = tuning.load_generator(staged)
        exclude = set()
        for rel in data["exclude"]:
            with open(REPO / rel, encoding="utf-8") as handle:
                exclude |= {corpus._fold(json.loads(line)["request"]) for line in handle if line.strip()}
        rows, dropped = corpus.generate(data["seed"], data["per_template"], exclude)
    examples, inconsistent, unsupported = [], 0, 0
    for row in rows:
        row["actions"] = [[kind, tuning._typed(kind, args)] for kind, args in row["actions"]]
        calls = [{"name": kind, "arguments": args} for kind, args in row["actions"]]
        admitted = [a for a in interpret(row["query"], calls) if isinstance(a, Action)]
        if len(admitted) != len(row["actions"]):
            inconsistent += 1
            continue
        try:
            answers = toolset.from_actions(row["actions"])
        except ValueError:
            unsupported += 1
            continue
        examples.append({"query": row["query"], "actions": row["actions"],
                         "spans": row.get("spans", []), "answers": answers})
    stats = {"generated": len(rows), "eval_matches_dropped": dropped,
             "inconsistent_dropped": inconsistent, "unsupported_dropped": unsupported,
             "supplement_files": added, "seed": data["seed"], "per_template": data["per_template"],
             "exclude": sorted(data["exclude"])}
    if data.get("cap_share"):
        examples, stats["capped"] = tuning.cap_share(examples, data["cap_share"], data["seed"])
    stats["kept"] = len(examples)
    return [{key: example[key] for key in ("query", "actions", "spans")} for example in examples], stats


def refuse_gates(paths) -> None:
    """A current gate set of any job is never read, under any spelling (a
    relative or absolute path, a symlink or a hard link): refused unopened."""
    import jobs

    def identity(path):
        real = os.path.realpath(path)
        try:
            info = os.stat(real)
            return real, (info.st_dev, info.st_ino)
        except OSError:
            return real, None
    gates = [identity(REPO / job.heldout) for job in jobs.JOBS.values()]
    names = {real for real, _ in gates}
    inodes = {inode for _, inode in gates if inode}
    for path in paths:
        real, inode = identity(REPO / path)
        if real in names or inode in inodes:
            raise SystemExit(f"panes_bundle_train: {path} is a current gate set and is never read")


def spent_rows(paths=SPENT) -> tuple[list[dict], dict]:
    """The cases of spent eval sets as rows: the request, its expected
    actions, and each text value the request holds word for word (the place
    its copy net learns). A case whose expected actions the checks do not
    admit as written is left out, as a generated row would be."""
    from actions import Action, interpret
    refuse_gates(paths)
    rows, seen = [], set()
    stats = {"files": [], "cases": 0, "repeated": 0, "inconsistent_dropped": 0}
    for rel in paths:
        raw = (REPO / rel).read_bytes()
        kept = 0
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            stats["cases"] += 1
            request, actions = case["request"], [[kind, dict(args)] for kind, args in case["expect"]]
            if request in seen:
                stats["repeated"] += 1
                continue
            calls = [{"name": kind, "arguments": args} for kind, args in actions]
            admitted = [a for a in interpret(request, calls) if isinstance(a, Action)] if calls else []
            if len(admitted) != len(actions):
                stats["inconsistent_dropped"] += 1
                continue
            words = [t[0].lower() for t in PB.tokenize(request)]
            spans = []
            for _, args in actions:
                for value in args.values():
                    if isinstance(value, str) and find_words(words, [t[0].lower() for t in PB.tokenize(value)]) >= 0:
                        spans.append([value, value])
            seen.add(request)
            rows.append({"query": request, "actions": actions, "spans": spans})
            kept += 1
        stats["files"].append({"path": rel, "sha256": hashlib.sha256(raw).hexdigest(), "kept": kept})
    stats["kept"] = len(rows)
    return rows, stats


def find_words(words, sub, start=0):
    for i in range(start, len(words) - len(sub) + 1):
        if words[i:i + len(sub)] == sub:
            return i
    return -1


def assign_spans(row):
    """For every action, {argument: the words it came from, or None}."""
    spans = [(str(s), str(v)) for s, v in row["spans"]]
    used, out = set(), []
    for _, args in row["actions"]:
        got = {}
        for key, value in args.items():
            got[key] = None
            for j, (surface, v) in enumerate(spans):
                if j not in used and v == str(value):
                    used.add(j)
                    got[key] = surface
                    break
        out.append(got)
    return out


def prepare(row: dict, tools: dict):
    """One row -> words, clause boundaries, and for each clause its tool, the
    label of every argument and the copied spans. None when the boundary
    between two actions cannot be told from what the generator recorded."""
    toks = PB.tokenize(row["query"])
    words = [t[0].lower() for t in toks]
    kinds = {(tool, key): kind for tool, spec in tools.items() for key, kind, _, _ in spec["args"]}
    located, cursor = [], 0
    for (tool, args), got in zip(row["actions"], assign_spans(row)):
        places = {}
        for key, value in args.items():
            surface = got[key]
            sub = [t[0].lower() for t in PB.tokenize(surface)] if surface is not None else None
            if sub is None and kinds.get((tool, key)) == "int":     # a number the generator did not record
                hits = [i for i, w in enumerate(words) if PB.as_int(w) == value]
                sub = [words[hits[0]]] if len(hits) == 1 else None
            if sub:
                at = find_words(words, sub, cursor)
                if at < 0:
                    at = find_words(words, sub)
                if at >= 0:
                    places[key] = (at, at + len(sub) - 1)
                    cursor = at + len(sub)
        located.append(places)
    n = len(row["actions"])
    if n <= 1:
        bounds = []
    elif n == 2:
        after = max([b for _, b in located[0].values()], default=-1)
        before = min([a for a, _ in located[1].values()], default=len(words))
        runs, i = [], 0
        while i < len(words):
            if words[i] in SEPARATORS:
                j = i
                while j + 1 < len(words) and words[j + 1] in SEPARATORS:
                    j += 1
                if i > after and j < before and i > 0 and j < len(words) - 1:
                    runs.append((i, j))
                i = j + 1
            else:
                i += 1
        if not runs:
            return None
        if len(runs) > 1:
            if located[1]:
                runs = runs[-1:]
            elif located[0]:
                runs = runs[:1]
            else:
                return None
        bounds = runs
    else:
        return None
    clauses, start = [], 0
    for a, b in bounds + [(len(words), len(words))]:
        clauses.append((start, a))
        start = b + 1
    out = []
    for c, (lo, hi) in enumerate(clauses):
        if n == 0:
            out.append({"range": (lo, hi), "tool": PB.NONE, "labels": {}, "spans": {}, "numbers": set()})
            continue
        tool, args = row["actions"][c]
        if tool not in tools:
            return None
        labels, spans, numbers = {}, {}, set()
        for key, kind, _, _ in tools[tool]["args"]:
            if key not in args:
                labels[key] = PB.ABSENT
                continue
            value, place = args[key], located[c].get(key)
            inside = place is not None and lo <= place[0] and place[1] < hi
            if kind in ("text", "int") or (kind == "ref" and str(value) not in PB.CANON):
                if not inside:
                    labels[key] = None           # cannot be supervised; leave this argument out
                    continue
                labels[key] = PB.COPY
                spans[key] = (place[0] - lo, place[1] - lo)
                # The copied words stand for a number: "tab two" for tab 2,
                # where "the tab named two" would be a title.
                if kind == "int" or (kind == "ref" and str(value).isdigit()):
                    numbers.add(key)
            elif kind == "flag":
                labels[key] = "true" if value else PB.ABSENT
            else:
                labels[key] = str(value)
        out.append({"range": (lo, hi), "tool": tool, "labels": labels, "spans": spans, "numbers": numbers})
    separators = {i for a, b in bounds for i in range(a, b + 1)}
    return {"words": words, "clauses": out, "separators": separators}


def ground(prepared: list[dict]) -> dict:
    """What the reader holds a request to, beside words and pairs: for each
    tool the frames of its training clauses (PB.frame_of: the clause with
    each copied text replaced by a mark for its argument), the words that
    joined two clauses, the largest number of clauses in a row, and for each
    argument the most words copied for it. A clause with an argument that
    could not be placed in the request has no frame: its copied words would
    stand in it as plain words."""
    frames, separators, most = collections.defaultdict(set), set(), 1
    longest = collections.defaultdict(dict)
    for p in prepared:
        most = max(most, len(p["clauses"]))
        separators.update(p["words"][i] for i in p["separators"])
        for clause in p["clauses"]:
            for key, (a, b) in clause["spans"].items():
                longest[clause["tool"]][key] = max(longest[clause["tool"]].get(key, 0), b - a + 1)
            if clause["tool"] == PB.NONE or any(label is None for label in clause["labels"].values()):
                continue
            lo, hi = clause["range"]
            spans = {key: (a, b, key in clause["numbers"]) for key, (a, b) in clause["spans"].items()}
            frames[clause["tool"]].update(PB.frame_pairs(PB.frame_of(p["words"][lo:hi], spans)))
    return {"frames": {tool: sorted(pairs) for tool, pairs in frames.items()},
            "separators": sorted(separators), "clauses_max": most,
            "copy_max": {tool: dict(sorted(keys.items())) for tool, keys in sorted(longest.items())}}


def examples(rows: list[dict], tools: dict) -> tuple[dict, set, set, dict]:
    """Training examples for every net, the known words and the known pairs."""
    prepared, skipped = [], 0
    for row in rows:
        p = prepare(row, tools)
        if p is None:
            skipped += 1
        else:
            prepared.append(p)
    known = set()
    for p in prepared:
        copied = set()
        for clause in p["clauses"]:
            lo = clause["range"][0]
            for a, b in clause["spans"].values():
                copied.update(range(lo + a, lo + b + 1))
        known.update(w for i, w in enumerate(p["words"]) if i not in copied)
    data, pairs = {"seg": ([], []), "tool": ([], [])}, set()
    for p in prepared:
        forms = PB.forms_of(p["words"], known)
        for i in range(len(forms)):
            data["seg"][0].append(PB.word_features(forms, i))
            data["seg"][1].append("sep" if i in p["separators"] else "word")
        for index, clause in enumerate(p["clauses"]):
            lo, hi = clause["range"]
            mine = forms[lo:hi]
            padded = ["<s>"] + mine + ["</s>"]
            pairs.update(a + "_" + b for a, b in zip(padded, padded[1:]))
            context = [w for j, w in enumerate(forms) if not lo <= j < hi and j not in p["separators"]]
            features = PB.clause_features(mine, context, index, len(p["clauses"]))
            data["tool"][0].append(features)
            data["tool"][1].append(clause["tool"])
            for key, label in clause["labels"].items():
                if label is None:
                    continue
                name = f"arg {clause['tool']} {key}"
                data.setdefault(name, ([], []))
                data[name][0].append(features)
                data[name][1].append(label)
                if label == PB.COPY:
                    a, b = clause["spans"][key]
                    data.setdefault(f"span {clause['tool']} {key}", []).append(
                        ([PB.word_features(mine, i) for i in range(len(mine))], a, b))
    return data, known, pairs, {"rows": len(rows), "rows_used": len(prepared), "rows_skipped": skipped,
                                "ground": ground(prepared)}


def value_words(rows: list[dict]) -> dict:
    """{word: the values it is another word for}, for every word that named
    a value of the answer in each training row that asks for an action and
    holds the word. The reader does not answer a request that holds such a
    word and leaves it out of the answer."""
    def words(text):
        return [t[0].lower() for t in PB.tokenize(str(text))]

    canon = collections.defaultdict(set)
    for row in rows:
        for surface, bound in row["spans"]:
            for word in words(surface):
                canon[word].add(str(bound))
    seen, named = collections.Counter(), collections.Counter()
    for row in rows:
        if not row["actions"]:
            continue
        values = [value for _, args in row["actions"] for value in args.values()]
        for word in set(words(row["query"])):
            seen[word] += 1
            named[word] += PB.stands_for(word, values, canon.get(word, ()))
    return {word: sorted(canon.get(word, ())) for word in sorted(seen) if named[word] == seen[word]}


def closed_values() -> set:
    """Every value of a closed list: the tools' enumerated values (sides,
    layouts, directions) and the words of place, but not "current", which
    names the caller's own pane or tab and is as often left out."""
    from actions import TOOLS
    values = {value for value in PB.CANON if value != "current"}
    for tool in TOOLS:
        for spec in tool["parameters"]["properties"].values():
            values.update(str(value) for value in spec.get("enum", ()))
    return values


def closed_words(rows: list[dict]) -> dict:
    """{word: the values it is another word for}, for every word that alone
    named a value of a closed list in some training row ("top" for above),
    and for each such value itself. value_words holds the words that named a
    value every time; these did not ("make the left pane bigger" is labelled
    as a resize of the caller's pane), and where one names nothing in the
    answer the reader leaves the request to the model. The values listed for
    a word are those of every phrase that held it, so "above" in "one above
    another" is in an answer that arranges the panes in rows."""
    def words(text):
        return [t[0].lower() for t in PB.tokenize(str(text))]

    closed = closed_values()
    canon, alone = collections.defaultdict(set), set(closed)
    for row in rows:
        for surface, bound in row["spans"]:
            held = words(surface)
            for word in held:
                canon[word].add(str(bound))
            if len(held) == 1 and str(bound) in closed:
                alone.add(held[0])
    return {word: sorted(canon.get(word, ())) for word in sorted(alone)}


def train_member(data: dict, seed: int, log=print) -> dict:
    """{net name: (feature names, outputs, flat weights in the file's order)}."""
    import torch
    torch.set_num_threads(1)

    class Net(torch.nn.Module):
        def __init__(self, features, outputs, net_seed):
            super().__init__()
            self.names = sorted(features)
            self.index = {f: i + 1 for i, f in enumerate(self.names)}
            self.outputs = list(outputs)
            g = torch.Generator().manual_seed(net_seed)

            def uniform(a, b):
                return torch.nn.Parameter((torch.rand((a, b), generator=g) * 2 - 1) / math.sqrt(a))

            self.emb = torch.nn.Parameter(torch.randn((len(self.names) + 1, DIM), generator=g) * 0.3)
            self.w1, self.b1 = uniform(DIM, HIDDEN), torch.nn.Parameter(torch.zeros(HIDDEN))
            self.w2 = uniform(HIDDEN, len(self.outputs))
            self.b2 = torch.nn.Parameter(torch.zeros(len(self.outputs)))

        def ids(self, feature_lists, width=None):
            rows = [[self.index[f] for f in fl if f in self.index] for fl in feature_lists]
            width = width or max(1, max((len(r) for r in rows), default=1))
            out = torch.zeros((len(rows), width), dtype=torch.long)
            for i, r in enumerate(rows):
                out[i, :len(r)] = torch.tensor(r, dtype=torch.long)
            return out

        def forward(self, ids):
            mask = (ids > 0).unsqueeze(-1)
            x = (self.emb[ids] * mask).sum(-2) / mask.sum(-2).clamp(min=1).sqrt()
            return torch.relu(x @ self.w1 + self.b1) @ self.w2 + self.b2

        def flat(self):
            parts = [self.emb, self.w1.t(), self.b1, self.w2.t(), self.b2]
            return torch.cat([p.detach().reshape(-1) for p in parts]).to(torch.float32).tolist()

    cross = torch.nn.functional.cross_entropy
    members, checks, net_seed = {}, {}, seed * 1000
    for name, d in sorted(data.items()):
        net_seed += 1
        g = torch.Generator().manual_seed(net_seed)
        if isinstance(d, tuple):
            outputs = sorted(set(d[1]))
            net = Net({f for fl in d[0] for f in fl}, outputs, net_seed)
            if len(outputs) > 1:
                ids = net.ids(d[0])
                y = torch.tensor([outputs.index(v) for v in d[1]])
                opt = torch.optim.Adam(net.parameters(), lr=RATE)
                for _ in range(EPOCHS):
                    order = torch.randperm(len(y), generator=g)
                    for s in range(0, len(y), 64):
                        sel = order[s:s + 64]
                        loss = cross(net(ids[sel]), y[sel])
                        opt.zero_grad()
                        loss.backward()
                        opt.step()
        else:
            net = Net({f for fl, _, _ in d for word in fl for f in word}, ["first", "last"], net_seed)
            longest = max(len(fl) for fl, _, _ in d)
            width = max(len(word) for fl, _, _ in d for word in fl)
            ids = torch.zeros((len(d), longest, width), dtype=torch.long)
            here = torch.zeros((len(d), longest), dtype=torch.bool)
            for i, (fl, _, _) in enumerate(d):
                ids[i, :len(fl)] = net.ids(fl, width)
                here[i, :len(fl)] = True
            first = torch.tensor([a for _, a, _ in d])
            last = torch.tensor([b for _, _, b in d])
            opt = torch.optim.Adam(net.parameters(), lr=RATE)
            for _ in range(EPOCHS):
                order = torch.randperm(len(d), generator=g)
                for s in range(0, len(d), 32):
                    sel = order[s:s + 32]
                    out = net(ids[sel]).masked_fill(~here[sel].unsqueeze(-1), float("-inf"))
                    loss = cross(out[..., 0], first[sel]) + cross(out[..., 1], last[sel])
                    opt.zero_grad()
                    loss.backward()
                    opt.step()
        with torch.no_grad():       # the file holds half floats: the net is what the file will hold
            for parameter in net.parameters():
                parameter.copy_(parameter.half().float())
        members[name] = (net.names, net.outputs, net.flat())
        sample = d[0][:CHECKED] if isinstance(d, tuple) else [word for fl, _, _ in d[:CHECKED] for word in fl]
        with torch.no_grad():
            checks[name] = [(features, net(net.ids([features]))[0].tolist()) for features in sample]
        log(f"  {name}: {len(d[1]) if isinstance(d, tuple) else len(d)} examples, "
            f"{len(net.names)} features, {len(members[name][2])} weights")
    return members, checks


def same_as_trained(raw: bytes, checks: list[dict]) -> list[dict]:
    """The file read by panes_bundle against what PyTorch scored after
    training, member by member: how many sampled examples pick another
    output, and the largest difference between two scores."""
    bundle, out = PB.Bundle(raw), []
    for member, sampled in zip(bundle.members, checks):
        differ, largest, examples = 0, 0.0, 0
        for name, sample in sampled.items():
            net = member.net(name)
            for features, trained in sample:
                got = net.scores(features)
                examples += 1
                largest = max(largest, max(abs(a - b) for a, b in zip(got, trained)))
                differ += max(range(len(got)), key=got.__getitem__) != max(range(len(trained)), key=trained.__getitem__)
        out.append({"examples": examples, "another_output": differ, "largest_score_difference": largest})
    return out


def reads_its_rows(raw: bytes, rows: list[dict]) -> dict:
    """The file read as a request meets it (Bundle.read, then the checks as
    needle_cli.checks_agree holds a proposal to them), on every row it was
    trained on: how many it answers, how many of those as the row is
    labelled, and how many with a risky action the row does not hold. The
    nets are checked one by one in same_as_trained; this checks the reader
    that stands on them."""
    from actions import Action, interpret
    from evaluate import _norm, expected
    from needle_cli import checks_agree
    bundle, out = PB.Bundle(raw), collections.Counter(rows=len(rows))
    for row in rows:
        texts: list = []
        calls, _ = bundle.read(row["query"], texts)
        if not calls:
            continue
        if not checks_agree(row["query"], calls, texts):
            out["proposed_and_dropped"] += 1
            continue
        results = interpret(row["query"], calls)
        got, want = [_norm(r.kind, r.args) for r in results], expected(row["actions"])
        out["answered"] += 1
        out["as_labelled"] += got == want
        out["risky_and_not_labelled"] += any(Action(kind, args).risky and [kind, args] not in want for kind, args in got)
    return {key: out[key] for key in ("rows", "answered", "as_labelled", "proposed_and_dropped", "risky_and_not_labelled")}


def pack(members: list[tuple[int, dict]], known: set, pairs: set, tools: dict, free_text: list[str],
         dim: int = DIM, hidden: int = HIDDEN, value_words: dict | None = None,
         ground: dict | None = None) -> bytes:
    """The bundle as one file (the layout is described in panes_bundle).

    members: [(seed, {net: (feature names, outputs, flat weights)})]. All
    members have the same nets over the same features; they differ in the
    seed alone. A net's flat weights are its table (the row of no feature
    first, then one row for each feature name in order), then the two layers."""
    first = members[0][1]
    for _, nets in members[1:]:
        if {key: (net[0], net[1]) for key, net in nets.items()} != {key: (net[0], net[1]) for key, net in first.items()}:
            raise ValueError("the members do not have the same nets over the same features")
    names = sorted({name for feature_names, _, _ in first.values() for name in feature_names})
    number = {name: n for n, name in enumerate(names)}
    ground = ground or {"frames": {}, "separators": sorted(SEPARATORS), "clauses_max": 2, "copy_max": {}}
    head = {"schema": PB.SCHEMA, "dim": dim, "hidden": hidden, "known": sorted(known), "tools": tools,
            "free_text": sorted(free_text), "value_words": value_words or {}, "nets": {}, "members": [],
            "separators": sorted(ground["separators"]), "clauses_max": int(ground["clauses_max"]),
            "copy_max": ground["copy_max"], "frames": {}}
    body = bytearray()

    def add(blob: bytes) -> int:
        at = len(body)
        body.extend(blob)
        return at

    blob = "\n".join(names).encode("utf-8")
    head["names"] = [add(blob), len(blob)]
    blob = "\n".join(sorted(pairs)).encode("utf-8")
    head["pairs"] = [add(blob), len(blob)]
    order = {}
    for key, (feature_names, outputs, _) in sorted(first.items()):
        order[key] = sorted(range(len(feature_names)), key=lambda i: number[feature_names[i]])
        ids = array.array("I", [number[feature_names[i]] for i in order[key]])
        if sys.byteorder != "little":
            ids.byteswap()
        head["nets"][key] = {"outputs": outputs, "ids": [add(ids.tobytes()), len(ids)]}
    for seed, nets in members:
        entry = {"seed": seed, "weights": {}}
        for key, (feature_names, _, flat) in sorted(nets.items()):
            rows = (len(feature_names) + 1) * dim
            values = list(flat[:dim])
            for i in order[key]:                 # the table's rows in the order of the feature numbers
                values.extend(flat[(i + 1) * dim:(i + 2) * dim])
            values.extend(flat[rows:])
            entry["weights"][key] = add(struct.pack(f"<{len(values)}e", *values))
        head["members"].append(entry)
    # Each tool's frames, after the weights: one pair a line.
    for tool in sorted(tools):
        blob = "\n".join(sorted(ground["frames"].get(tool, ()))).encode("utf-8")
        head["frames"][tool] = [add(blob), len(blob)]
    encoded = json.dumps(head, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return PB.MAGIC + len(encoded).to_bytes(4, "little") + encoded + bytes(body)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(out: Path, rows: list[dict], row_stats: dict, seeds: list[int], free_text: list[str],
          name: str, log=print) -> dict:
    tools = schema()
    data, known, pairs, used = examples(rows, tools)
    trained, checks = [], []
    for seed in seeds:
        log(f"member seed {seed}")
        nets, sampled = train_member(data, seed, log)
        trained.append((seed, nets))
        checks.append(sampled)
    always, closed = value_words(rows), closed_words(rows)
    ground_ = used.pop("ground")
    raw = pack(trained, known, pairs, tools, free_text, value_words={**closed, **always}, ground=ground_)
    same = same_as_trained(raw, checks)
    if any(member["another_output"] for member in same):
        raise SystemExit(f"the file does not read as it was trained ({same})")
    on_rows = reads_its_rows(raw, rows)
    log(f"on its own rows: {on_rows}")
    if on_rows["risky_and_not_labelled"]:
        raise SystemExit(f"the file answers a training row with a risky action the row does not hold ({on_rows})")
    out.mkdir(parents=True, exist_ok=True)
    (out / PB.FILE).write_bytes(raw)
    import torch
    manifest = {
        "schema": PB.MANIFEST_SCHEMA, "name": name, "file": PB.FILE,
        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
        "members": [{"seed": seed, "nets": len(nets),
                     "weights": sum(len(flat) for _, _, flat in nets.values()), "read_as_trained": check}
                    for (seed, nets), check in zip(trained, same)],
        "answers_when": "every member proposes the same calls and the checks admit every one of them",
        "weights_as": "little-endian half floats; each net was rounded to them before it was checked",
        "trainer": {"dim": DIM, "hidden": HIDDEN, "epochs": EPOCHS, "learning_rate": RATE,
                    "torch": torch.__version__, "threads": 1},
        "rows": {**row_stats, **used, "known_words": len(known), "known_pairs": len(pairs),
                 "value_words": len(always), "closed_words": len(closed),
                 "value_and_closed_words": len({**closed, **always}),
                 "frames": {tool: len(pairs_) for tool, pairs_ in sorted(ground_["frames"].items())},
                 "separators": ground_["separators"], "clauses_max": ground_["clauses_max"],
                 "copy_max": ground_["copy_max"],
                 "sha256": hashlib.sha256("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n"
                                                  for r in rows).encode("utf-8")).hexdigest()},
        "free_text": sorted(free_text),
        "reads_its_rows": on_rows,
        "code": {"panes_bundle.py": sha256_file(REPO / "panes_bundle.py"),
                 "panes_bundle_train.py": sha256_file(REPO / "panes_bundle_train.py")},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(PB.DIRECTORY))
    parser.add_argument("--seeds", type=int, nargs="+", default=[0, 1])
    parser.add_argument("--per-template", type=int, help="examples of each template (the tuner's own number if not given)")
    parser.add_argument("--free-text", nargs="*", default=[],
                        help="tools whose copied text may hold word pairs not seen in training")
    parser.add_argument("--rows", help="read rows from this file (query, actions, spans) and do not generate")
    parser.add_argument("--save-rows", help="also write the rows used to this file")
    parser.add_argument("--spent", action="store_true",
                        help="add the cases of the job's spent eval sets (never a current gate set)")
    parser.add_argument("--name", default="")
    args = parser.parse_args(argv)
    if args.rows:
        refuse_gates([os.path.abspath(args.rows)])
        raw_rows = Path(args.rows).read_bytes()
        rows = [json.loads(line) for line in raw_rows.decode("utf-8").splitlines() if line.strip()]
        stats = {"source": args.rows, "source_sha256": hashlib.sha256(raw_rows).hexdigest()}
    else:
        rows, stats = build_rows(args.per_template)
    if args.spent:
        generated = {row["query"] for row in rows}
        more, stats["spent"] = spent_rows()
        rows += [row for row in more if row["query"] not in generated]
    if args.save_rows:
        with open(args.save_rows, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest = write(Path(args.out), rows, stats, args.seeds, args.free_text, args.name)
    print(json.dumps({k: manifest[k] for k in ("name", "members", "rows")}, indent=1)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
