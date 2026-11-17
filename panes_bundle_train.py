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
(router-model/README.md). A current gate set (jobs.py) is refused before it
is opened. A bundle trained this way must not be scored on those sets: the
manifest lists them under rows.spent.

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
            out.append({"range": (lo, hi), "tool": PB.NONE, "labels": {}, "spans": {}})
            continue
        tool, args = row["actions"][c]
        if tool not in tools:
            return None
        labels, spans = {}, {}
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
            elif kind == "flag":
                labels[key] = "true" if value else PB.ABSENT
            else:
                labels[key] = str(value)
        out.append({"range": (lo, hi), "tool": tool, "labels": labels, "spans": spans})
    separators = {i for a, b in bounds for i in range(a, b + 1)}
    return {"words": words, "clauses": out, "separators": separators}


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
    return data, known, pairs, {"rows": len(rows), "rows_used": len(prepared), "rows_skipped": skipped}


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


def pack(members: list[tuple[int, dict]], known: set, pairs: set, tools: dict, free_text: list[str],
         dim: int = DIM, hidden: int = HIDDEN, value_words: dict | None = None) -> bytes:
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
    head = {"schema": PB.SCHEMA, "dim": dim, "hidden": hidden, "known": sorted(known), "tools": tools,
            "free_text": sorted(free_text), "value_words": value_words or {}, "nets": {}, "members": []}
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
    always = value_words(rows)
    raw = pack(trained, known, pairs, tools, free_text, value_words=always)
    same = same_as_trained(raw, checks)
    if any(member["another_output"] for member in same):
        raise SystemExit(f"the file does not read as it was trained ({same})")
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
                 "value_words": len(always),
                 "sha256": hashlib.sha256("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n"
                                                  for r in rows).encode("utf-8")).hexdigest()},
        "free_text": sorted(free_text),
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
        with open(args.rows, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        stats = {"source": args.rows}
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
