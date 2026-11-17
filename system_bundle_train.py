"""Train the system bundle (system_bundle.py) from system-bundle/training.jsonl.

    python3 system_bundle_train.py                    # write system-bundle/bundle.json + manifest
    python3 system_bundle_train.py --exclude heldout-v1 --out DIR --evaluate evals/system/heldout-v1.jsonl

Deterministic: the same rows and seed give the same bytes. Every net is an
averaged perceptron over sparse word features; each member is trained on its
own sample of the rows (85%) in its own order, so members disagree where the
rows are thin. The rows are the spent system sets (never held-out v2);
system-bundle/README.md says where each comes from.

A row is used only where code reproduces its label: the clauses must split so
that each grounds its call, and the time, path, number and copied name the
label states must be the ones code and the copy candidates find. Rows that do
not align are counted and left out, never forced.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from itertools import combinations
import hashlib
import json
from pathlib import Path
import random
import struct
import sys

import system_bundle as sb
import system_job

TRAINING = sb.DIRECTORY / "training.jsonl"
MEMBERS = 3
SAMPLE = 0.85
EPOCHS = 12
SEED = 20261117
VALUE_WORD_MIN = 3
TOOL_WORD_MIN = 5

_CUES = {
    "resources": r"memory|ram|swap|mem|cpu|load|processor|disk|space|storage|filesystem|drive|resources?|usage|system",
    "processes": r"process(?:es)?|procs?|programs?|apps?|top|hogs?|hogging|using|eating|consuming|heaviest|biggest",
    "services": r"services?|units?|failed|daemons?|running|status|loaded|active",
    "journal": r"logs?|logged|journal|errors?|warnings?|messages|events|entries",
    "packages": r"packages?|installed|provides|owns|owner|belongs",
}


def load_rows(path: Path, exclude=()) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("source") not in exclude:
                rows.append(row)
    return rows


def gold_calls(row) -> list:
    return [[kind, system_job.normalize(kind, args)] for kind, args in row["expect"]]


def _matches(word: str, value: str) -> bool:
    return word == value or word + ".service" == value or word.lower() == value


def _grounds(words, call) -> bool:
    import re
    kind, args = call
    low = " ".join(w.lower() for w in words)
    for key in ("unit", "identifier", "target"):
        if key in args and not any(_matches(w, args[key]) for w in words):
            return False
    if args.get("path", "/") != "/" and args["path"] not in words:
        return False
    if "since" in args and sb.read_since(words)[0] != args["since"]:
        return False
    return bool(re.search(r"\b(?:" + _CUES[kind] + r")\b", low)) or any(
        k in args for k in ("unit", "identifier", "target"))


def align(row, vocab_all):
    """[(words, call or None, index, count, previous)] and the joiner decisions, or None."""
    words = sb.tokenize(row["request"])
    if not words:
        return None
    forms = [sb.form(w, vocab_all) for w in words]
    joins = sb.joiner_positions(forms)
    calls = gold_calls(row)
    if not calls:
        return [(words, None, 0, 1, sb.NONE)], [], words, forms
    k = len(calls)
    options = []
    for cuts in combinations(joins, k - 1):
        spans = sb.split(words, forms, list(cuts))
        if all(_grounds(words[a:b], call) for (a, b), call in zip(spans, calls)):
            options.append(list(cuts))
    if len(options) != 1:
        return None
    cuts = options[0]
    spans = sb.split(words, forms, cuts)
    clauses, previous = [], sb.NONE
    for index, ((a, b), call) in enumerate(zip(spans, calls)):
        clauses.append((words[a:b], call, index, len(spans), previous))
        previous = call[0]
    decisions = [(j, "cut" if j in cuts else "keep") for j in joins]
    return clauses, decisions, words, forms


def gold_clause(words, forms, call):
    """The label's answers fed through the shared reader: (args, marks, presence, pick) or None."""
    tool, args = call
    defaults = system_job.DEFAULTS[tool]
    presence, picked = {}, {}

    def closed(arg):
        return args[arg]

    def present(arg):
        if arg == "since":
            value = "since" in args
        elif arg == "path":
            value = args.get("path", "/") != "/"
        elif arg == "limit":
            value = sb.read_number(words, set()) == args["limit"] or args["limit"] != defaults["limit"]
            # the limit is given when a number in the clause states it
            value = args["limit"] != defaults["limit"] or any(
                sb.number(w) == args["limit"] for w in words)
        else:
            value = arg in args
        presence[arg] = value
        return value

    def pick(arg, candidates):
        hits = [i for i in candidates if _matches(words[i], args[arg])]
        if len(hits) != 1:
            return None
        picked[arg] = (hits[0], candidates)
        return hits[0]

    got, marks = sb.read_clause(tool, words, forms, closed, present, pick)
    if got is None:
        return None
    normal = system_job.normalize(tool, got)
    if normal != args:
        return None
    return got, marks, presence, picked


class Perceptron:
    """Averaged multiclass perceptron over sparse features."""

    def __init__(self, classes):
        self.classes = list(classes)
        self.w = defaultdict(lambda: [0.0] * len(self.classes))
        self.total = defaultdict(lambda: [0.0] * len(self.classes))
        self.stamp = defaultdict(lambda: [0] * len(self.classes))
        self.t = 0

    def _score(self, features):
        s = [0.0] * len(self.classes)
        for f in features:
            if f in self.w:
                row = self.w[f]
                for c in range(len(s)):
                    s[c] += row[c]
        return s

    def _bump(self, f, c, delta):
        row, total, stamp = self.w[f], self.total[f], self.stamp[f]
        total[c] += (self.t - stamp[c]) * row[c]
        stamp[c] = self.t
        row[c] += delta

    def update(self, features, gold: int):
        self.t += 1
        s = self._score(features)
        guess = max(range(len(s)), key=lambda c: (s[c], -c))
        if guess != gold:
            for f in features:
                self._bump(f, gold, 1.0)
                self._bump(f, guess, -1.0)

    def averaged(self, features_order) -> list[list[float]]:
        out = []
        for f in features_order:
            if f not in self.w:
                out.append([0.0] * len(self.classes))
                continue
            row, total, stamp = self.w[f], self.total[f], self.stamp[f]
            out.append([(total[c] + (self.t - stamp[c]) * row[c]) / max(1, self.t)
                        for c in range(len(self.classes))])
        return out


class Ranker(Perceptron):
    """Averaged ranking perceptron: one score per candidate word."""

    def __init__(self):
        super().__init__(["score"])

    def update_rank(self, candidates: list, gold: int):
        self.t += 1
        scores = [self._score(f)[0] for f in candidates]
        guess = max(range(len(scores)), key=lambda i: (scores[i], -i))
        if guess != gold:
            for f in candidates[gold]:
                self._bump(f, 0, 1.0)
            for f in candidates[guess]:
                self._bump(f, 0, -1.0)


def _hex(matrix) -> str:
    flat = [max(-60000.0, min(60000.0, v)) for row in matrix for v in row]
    return struct.pack(f"<{len(flat)}e", *flat).hex() if flat else ""


def build(rows, seed=SEED, members=MEMBERS, log=print) -> dict:
    vocab_all = _Everything()
    stats = Counter()
    examples = defaultdict(list)          # net -> [(features, class)] ; copy nets -> [(cand feats, gold)]
    vocab, frames, joins = set(), defaultdict(set), set()
    max_clauses, max_words = 1, 1
    aligned = []
    for r, row in enumerate(rows):
        got = align(row, vocab_all)
        if got is None:
            stats["rows not aligned"] += 1
            continue
        clauses, decisions, words, forms = got
        ok = []
        for cw, call, index, count, previous in clauses:
            cf = [sb.form(w, vocab_all) for w in cw]
            if call is None:
                ok.append((cw, None, index, count, previous, None))
                continue
            gold = gold_clause(cw, cf, call)
            if gold is None:
                stats["clauses not reproduced"] += 1
                ok = None
                break
            ok.append((cw, call, index, count, previous, gold))
        if ok is None:
            stats["rows not aligned"] += 1
            continue
        aligned.append((r, ok, decisions, words))
        for cw, call, _i, _c, _p, gold in ok:
            if call is None:
                continue
            marks = gold[1]
            vocab.update(w.lower() for i, w in enumerate(cw) if i not in marks or marks[i] in ("<since>", "<num>"))
    vocab = {w for w in vocab if sb.number(w) is None and not w.startswith("/") and not sb._DATE.fullmatch(w)}
    stats["rows aligned"] = len(aligned)

    tool_counts, value_counts = defaultdict(Counter), defaultdict(lambda: defaultdict(Counter))
    for r, ok, decisions, words in aligned:
        forms = [sb.form(w, vocab) for w in words]
        for j, decision in decisions:
            joins.update(sb.join_pairs(forms, j))
            examples["seg"].append((r, sb.joiner_features(forms, j), decision))
        max_clauses = max(max_clauses, len(ok))
        for cw, call, index, count, previous, gold in ok:
            cf = [sb.form(w, vocab) for w in cw]
            max_words = max(max_words, len(cw))
            features = sb.clause_features(cf, index, count, previous)
            tool = call[0] if call else sb.NONE
            examples["tool"].append((r, features, tool))
            if call is None:
                continue
            args, marks, presence, picked = gold
            frames[tool].update(sb.frame_pairs(cf, marks))
            plain = {f for i, f in enumerate(cf) if i not in marks} - sb.MARKS
            for w in plain:
                tool_counts[w][tool] += 1
            for arg in sb.CLOSED[tool]:
                examples[f"{tool}.{arg}"].append((r, features, call[1][arg]))
                for w in plain:
                    value_counts[tool][arg][(w, call[1][arg])] += 1
            for arg in sb.PRESENCE[tool]:
                examples[f"{tool}.{arg}"].append((r, features, "present" if presence.get(arg) else "absent"))
            for arg, (gold_i, candidates) in picked.items():
                feats = [sb.token_features(cw, cf, i) for i in candidates]
                examples[f"{tool}.{arg}@"].append((r, feats, candidates.index(gold_i)))

    tool_words = {}
    for w, counts in tool_counts.items():
        if len(counts) == 1:
            (tool, n), = counts.items()
            if n >= TOOL_WORD_MIN:
                tool_words[w] = tool
    value_words = defaultdict(lambda: defaultdict(dict))
    for tool, per_arg in value_counts.items():
        for arg, counts in per_arg.items():
            by_word = defaultdict(Counter)
            for (w, v), n in counts.items():
                by_word[w][v] += n
            for w, vs in by_word.items():
                if len(vs) == 1:
                    (v, n), = vs.items()
                    if n >= VALUE_WORD_MIN and len(sb.CLOSED[tool][arg]) > 1:
                        value_words[tool][arg][w] = v

    nets, weights = {}, [dict() for _ in range(members)]
    for name in sorted(examples):
        rows_ = examples[name]
        copy = name.endswith("@")
        if copy:
            features = sorted({f for _r, cands, _g in rows_ for c in cands for f in c})
            classes = ["score"]
        else:
            features = sorted({f for _r, feats, _y in rows_ for f in feats})
            classes = _classes(name, rows_)
        nets[name] = {"classes": classes, "features": features}
        for m in range(members):
            rng = random.Random(f"{seed}/{m}/{name}")
            picked_rows = [x for x in rows_ if random.Random(f"{seed}/{m}/row/{x[0]}").random() < SAMPLE] \
                if members > 1 else list(rows_)
            model = Ranker() if copy else Perceptron(classes)
            for _epoch in range(EPOCHS):
                order = list(picked_rows)
                rng.shuffle(order)
                for x in order:
                    if copy:
                        model.update_rank(x[1], x[2])
                    else:
                        model.update(x[1], classes.index(x[2]))
            weights[m][name] = _hex(model.averaged(features))
        stats[f"examples {name}"] = len(rows_)
    log(" ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    return {"schema": sb.SCHEMA, "seed": seed, "members": members,
            "vocab": sorted(vocab), "frames": {t: sorted(p) for t, p in sorted(frames.items())},
            "joins": sorted(joins), "value_words": {t: {a: dict(sorted(w.items())) for a, w in sorted(v.items())}
                                                    for t, v in sorted(value_words.items())},
            "tool_words": dict(sorted(tool_words.items())), "max_clauses": max_clauses,
            "max_words": max_words, "nets": nets, "weights": weights}


def _classes(name, rows_):
    if name == "seg":
        return ["cut", "keep"]
    if name == "tool":
        return list(sb.TOOLS) + [sb.NONE]
    tool, arg = name.split(".")
    if arg in sb.CLOSED.get(tool, {}):
        return list(sb.CLOSED[tool][arg])
    return ["absent", "present"]


class _Everything:
    """A vocabulary that knows every word (for splitting before the vocabulary exists)."""

    def __contains__(self, _word):
        return True


def write(data: dict, directory: Path, rows_path: Path) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    digest = hashlib.sha256(body).hexdigest()
    (directory / sb.FILE).write_bytes(body)
    manifest = {"schema": "kilix-needle.system-bundle-manifest/v1", "sha256": digest, "bytes": len(body),
                "seed": data["seed"], "members": data["members"],
                "training_sha256": hashlib.sha256(rows_path.read_bytes()).hexdigest(),
                "trainer": "system_bundle_train.py", "epochs": EPOCHS, "sample": SAMPLE}
    (directory / sb.MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="ascii")
    return digest


def evaluate(bundle, cases) -> dict:
    import system_normalize
    totals, failures = Counter(), []
    for case in cases:
        want = [[k, system_job.normalize(k, a)] for k, a in case["expect"]]
        plan = system_normalize.plan(case["request"], None, bundle=bundle)
        got = plan["actions"] if plan["decision"] == "read" else []
        totals["cases"] += 1
        totals["exact"] += got == want
        if plan["path"] == "grammar":
            totals["grammar_exact"] += got == want
            totals["grammar_unsafe"] += any(a not in want for a in got)
        if plan["path"] == "bundle":
            totals["bundle_reads"] += 1
            totals["bundle_exact"] += got == want
            if got != want:
                totals["bundle_differs"] += 1
                failures.append({"request": case["request"], "got": got, "want": want})
    return {"totals": dict(totals), "failures": failures}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", default=str(TRAINING))
    parser.add_argument("--out", default=str(sb.DIRECTORY))
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--exclude", action="append", default=[], help="leave out rows of this source")
    parser.add_argument("--evaluate", metavar="CASES", help="score grammar + bundle on a set afterwards")
    args = parser.parse_args(argv)
    rows_path = Path(args.rows)
    rows = load_rows(rows_path, exclude=set(args.exclude))
    data = build(rows, seed=args.seed)
    digest = write(data, Path(args.out), rows_path)
    print(f"bundle {digest} ({len(rows)} rows)")
    if args.evaluate:
        bundle = sb.load(Path(args.out))
        cases = [json.loads(l) for l in Path(args.evaluate).read_text().splitlines() if l.strip()]
        result = evaluate(bundle, cases)
        print(json.dumps(result["totals"]))
        for f in result["failures"]:
            print("DIFFERS", json.dumps(f))
    return 0


if __name__ == "__main__":
    sys.exit(main())
