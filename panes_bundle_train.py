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
refuse one under any path before it is opened, and a copy of one under
another name by its digest, before a line of it is taken. The generator does
read every eval file, the current gate set among them, as the tuner does:
only to keep each of their requests out of the rows it makes.

Nothing is written unless the file, read as a request meets it, answers
every training row it answers as that row is labelled, and its nets score a
spread sample of their examples as PyTorch did. A bundle has at least two
members: it answers when they agree.

`--per-template N` asks the generator for more examples of each template than
the tuned model gets. Measured on four held-out sets: it does not help.

`--agent SEED` adds the agent-style rows of panes_bundle_agent.py (requests
the way coding agents write them: ids, "only", full stops, ";" between
clauses, the bundle's own list kind), `--agent-per-template` of each template.
Each one goes through the same checks as every training row, and one is
dropped, and counted, when it repeats another row, when the checks refuse it,
when its values cannot be placed in it, or when a word it copies is one the
other rows know only as wording (so that rule keeps its force).
`--exclude-history` also drops one equal (folded) to a request of the job's
history that went to a tuned model; the manifest records the counts, never a
request.

The file holds the second reader's ground beside the readings (runs of
neighbours of each tool's training frames, the closed values they had
together). `--second-kinds KIND ...` switches it on for those kinds in the
manifest: none unless named, and each one only after it has been measured
against the model (the standard of the experiment that chose it). Before
anything is written the file is also read with the second reader on for every
kind, and must answer its training rows as they are labelled that way too.

`--job JOB` trains the bundle of another job (job_schemas.py: its tools, made
for reading) on the rows of job_rows.py, made from `--job-rows SEED` with
`--job-per-template` examples of each template, into job-bundles/JOB/. For
the panes job `--job-rows SEED` adds the rows of job_rows.py (the reads of a
pane's text) to the others. Every row is one the job's own checks take as it
is labelled (job_rows.admitted), and so is every answer the file gives to its
own rows before it is written.

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
import re
import struct
import sys
import tempfile

import panes_bundle as PB

REPO = Path(__file__).resolve().parent
SEPARATORS = {"and", "then", ",", ";"}      # used only to label training rows
DIM, HIDDEN, EPOCHS, RATE = 8, 32, 40, 0.01
CHECKED = 200       # examples of each net, spread over all it has, scored by PyTorch and again by the reader
# What decides the rows, beside the generator's library: recorded in the manifest.
ROW_CODE = ("actions.py", "tuning.py", "toolset.py", "panes_bundle_agent.py", "job_schemas.py", "job_rows.py",
            "pane_reads.py")
# The job's eval sets that have gated and are no longer a gate: what --spent adds.
SPENT = ("evals/dev.jsonl", "evals/test.jsonl") + tuple(f"evals/heldout-v{n}.jsonl" for n in range(2, 15))
# The bundle's own kind (panes_bundle.LIST): what the list holds, and what the
# request names beside it ("list pane 412 and its title"), which the reader
# must account for and the list does not need.
LIST_ARGS = (("what", "enum", None, None), ("about", "ref", None, None))
LIST_WHAT = ("panes", "tabs", "panes and tabs")
# The bundle's other kind, a read of a pane's text (panes_bundle.READ): the
# pane's id, and how many of its last lines.
READ_ARGS = (("pane", "int", 1, 999_999_999), ("lines", "int", 1, 1000))
_ID = re.compile(r"(?:id|pane):[0-9]+")


def schema(job: str = "panes") -> dict:
    """Each tool's arguments as the bundle treats them: a value of a closed
    list (enum), a flag, a number in a range (int), a pane or tab (ref: a word
    of place or copied text) or free text. Another job's are job_schemas'."""
    if job != "panes":
        import job_schemas
        return job_schemas.schema(job)
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
    tools[PB.LIST] = {"args": [list(arg) for arg in LIST_ARGS], "required": ["what"]}
    tools[PB.READ] = {"args": [list(arg) for arg in READ_ARGS], "required": ["pane"]}
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
        library = tree_digest(staged)
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
             "exclude": sorted(data["exclude"]),
             # the generator's library as it was staged, supplements and all, and where it came from
             "library_sha256": library, "library_commit": library_commit(tuning.LIBRARY)}
    if data.get("cap_share"):
        examples, stats["capped"] = tuning.cap_share(examples, data["cap_share"], data["seed"])
    stats["kept"] = len(examples)
    return [{key: example[key] for key in ("query", "actions", "spans")} for example in examples], stats


def tree_digest(folder: Path) -> str:
    """One digest for every file under a folder: each one's path and bytes, in order."""
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(folder).rglob("*") if p.is_file()):
        digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def library_commit(library: Path) -> str | None:
    """The commit of the checkout the generator's library lies in, if it lies in one."""
    import subprocess
    try:
        done = subprocess.run(["git", "-C", str(library), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


def refuse_gates(paths) -> None:
    """A current gate set of any job is never read as rows. Under any
    spelling (a relative or absolute path, a symlink or a hard link) it is
    refused unopened. A copy under another name is known by its digest: the
    file and the gate sets are hashed, and it is refused before a line of it
    is taken."""
    import jobs

    def identity(path):
        real = os.path.realpath(path)
        try:
            info = os.stat(real)
            return real, (info.st_dev, info.st_ino)
        except OSError:
            return real, None

    def digest(path):
        try:
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            return None
    gates = [identity(REPO / job.heldout) for job in jobs.JOBS.values()]
    names = {real for real, _ in gates}
    inodes = {inode for _, inode in gates if inode}
    for path in paths:
        real, inode = identity(REPO / path)
        if real in names or inode in inodes:
            raise SystemExit(f"panes_bundle_train: {path} is a current gate set and is never read")
    contents = {digest(real) for real in names} - {None}
    for path in paths:
        if digest(REPO / path) in contents:
            raise SystemExit(f"panes_bundle_train: {path} is a copy of a current gate set and is never read")


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


def history_requests() -> tuple[set, int]:
    """The requests of this machine's panes history that reached a tuned
    model, folded (panes_bundle_agent.fold), and how many lines they are.
    Only the request of such a line is taken, and only to keep a row equal to
    it out of the agent rows: no line of the history is a row. Every other
    line, and every other part of these, is passed over."""
    import history
    import panes_bundle_agent as agent
    folder = history.directory()
    if not folder.is_dir():
        raise SystemExit(f"panes_bundle_train: there is no request history at {folder}")
    out, lines = set(), 0
    for path in sorted(folder.glob("requests*.jsonl")):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("job") != "panes" \
                        or not isinstance(entry.get("request"), str) \
                        or not str(entry.get("engine") or "").startswith("tuned"):
                    continue
                out.add(agent.fold(entry["request"]))
                lines += 1
    return out, lines


def wording(rows: list[dict], tools: dict) -> tuple[set, dict]:
    """The words of the rows that are not copied (what the bundle will know
    as wording), and for each argument the words copied for it as text."""
    known, held = set(), collections.defaultdict(set)
    for row in rows:
        p = prepare(row, tools)
        if p is None:
            continue
        copied = set()
        for clause in p["clauses"]:
            lo = clause["range"][0]
            for key, (a, b) in clause["spans"].items():
                copied.update(range(lo + a, lo + b + 1))
                if key not in clause["numbers"]:
                    held[key].update(p["words"][lo + a:lo + b + 1])
        known.update(w for i, w in enumerate(p["words"]) if i not in copied)
    return known, held


def agent_rows(seed: int, per_template: int, rows: list[dict], tools: dict, exclude: set | None = None,
               log=print) -> tuple[list[dict], dict]:
    """Agent-style rows (panes_bundle_agent) to add to `rows`, and what was
    left out and why. A row is left out when:
    - it is one of `rows` already, or equal (folded) to a request of
      `exclude` (this machine's history, history_requests);
    - the checks do not admit each of its actions for its request, as a
      generated row's (a list, the bundle's own, is the row's one action);
    - its clauses cannot be told apart, or a copied value not found;
    - a word copied for one of its arguments is one the bundle will know
      only as wording: the hold on such words (PB.PLAIN) is kept as strong
      as the other rows left it."""
    import panes_bundle_agent as agent
    from actions import Action, interpret
    reads = PB.READ in tools
    made = agent.rows(seed, per_template, reads=reads)
    out = collections.Counter(generated=len(made))
    if reads:
        out["left_out_as_reads"] = len(agent.rows(seed, per_template)) - len(made)
    present = {agent.fold(row["query"]) for row in rows}
    kept, why = [], collections.defaultdict(collections.Counter)
    for row in made:
        key = agent.fold(row["query"])
        actions = [[kind, dict(args)] for kind, args in row["actions"]]
        if key in present:
            reason = "same_as_another_row"
        elif exclude is not None and key in exclude:
            reason = "same_as_a_history_request"
        elif any(kind == PB.LIST for kind, _ in actions):
            reason = None if len(actions) == 1 and actions[0][1].get("what") in LIST_WHAT else "not_admitted"
        else:
            calls = [{"name": kind, "arguments": args} for kind, args in actions]
            admitted = [a for a in interpret(row["query"], calls) if isinstance(a, Action)] if calls else []
            reason = None if len(admitted) == len(actions) else "not_admitted"
        if reason is None and prepare(row, tools) is None:
            reason = "clauses_or_values_not_placed"
        if reason:
            out[reason] += 1
            why[row["template"]][reason] += 1
            continue
        kept.append({"query": row["query"], "actions": actions, "spans": row["spans"]})
    known, _ = wording(rows + kept, tools)
    _, held = wording(rows, tools)
    plain = []
    for row in kept:
        p = prepare(row, tools)
        if any(word in known and word not in held[key]
               for clause in p["clauses"] for key, (a, b) in clause["spans"].items() if key not in clause["numbers"]
               for word in p["words"][clause["range"][0] + a:clause["range"][0] + b + 1]):
            out["copied_word_known_as_wording"] += 1
            continue
        plain.append(row)
    out["kept"] = len(plain)
    for template, reasons in sorted(why.items()):
        log(f"  agent template {template}: {dict(reasons)}")
    return plain, dict(sorted(out.items()))


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
                # where "the tab named two" would be a title, and "tab id 4"
                # for the tab whose id is 4.
                if kind == "int" or (kind == "ref" and (str(value).isdigit() or _ID.fullmatch(str(value)))):
                    numbers.add(key)
            elif kind == "flag":
                labels[key] = "true" if value else PB.ABSENT
            else:
                labels[key] = str(value)
        out.append({"range": (lo, hi), "tool": tool, "labels": labels, "spans": spans, "numbers": numbers})
    separators = {i for a, b in bounds for i in range(a, b + 1)}
    return {"words": words, "clauses": out, "separators": separators}


def ground(prepared: list[dict], job: str = "panes") -> dict:
    """What the reader holds a request to, beside words and pairs: for each
    tool the readings of its training clauses (PB.reading_line: the tool of
    the clause before it, the clause with each copied text replaced by a
    mark for its argument, and its values from the closed lists), the words
    that joined two clauses, the largest number of clauses in a row, for each
    argument the most words copied for it, and for each argument name the
    words that were copied for it as text. A clause with an argument that
    could not be placed in the request has no reading: its copied words
    would stand in it as plain words."""
    readings, separators, most = collections.defaultdict(set), set(), 1
    longest, copied = collections.defaultdict(dict), collections.defaultdict(set)
    frame_runs, closed_sets = collections.defaultdict(set), collections.defaultdict(set)
    for p in prepared:
        most = max(most, len(p["clauses"]))
        separators.update(p["words"][i] for i in p["separators"])
        for index, clause in enumerate(p["clauses"]):
            lo, hi = clause["range"]
            for key, (a, b) in clause["spans"].items():
                longest[clause["tool"]][key] = max(longest[clause["tool"]].get(key, 0), b - a + 1)
                if key not in clause["numbers"]:
                    copied[key].update(p["words"][lo + a:lo + b + 1])
            if clause["tool"] == PB.NONE or any(label is None for label in clause["labels"].values()):
                continue
            spans = {key: (a, b, key in clause["numbers"]) for key, (a, b) in clause["spans"].items()}
            closed = {key: label for key, label in clause["labels"].items() if label not in (PB.ABSENT, PB.COPY)}
            before = p["clauses"][index - 1]["tool"] if index else ""
            frame = PB.frame_of(p["words"][lo:hi], spans)
            readings[clause["tool"]].add(PB.reading_line(before, frame, closed))
            # The second reader's: each run of neighbours of the frame with
            # the tool of the clause before, and the closed values together.
            frame_runs[clause["tool"]].update(f"{before}\t{run}" for run in PB.runs(frame))
            closed_sets[clause["tool"]].add("".join(f"\t{key}={closed[key]}" for key in sorted(closed)))
    return {"readings": {tool: sorted(lines) for tool, lines in readings.items()},
            "separators": sorted(separators), "clauses_max": most,
            "copy_max": {tool: dict(sorted(keys.items())) for tool, keys in sorted(longest.items())},
            "copied": {key: sorted(words) for key, words in sorted(copied.items())},
            "frame_runs": {tool: sorted(lines) for tool, lines in frame_runs.items()},
            "closed_sets": {tool: sorted(lines) for tool, lines in closed_sets.items()},
            "said": sorted(closed_values(job))}


def examples(rows: list[dict], tools: dict, job: str = "panes") -> tuple[dict, set, set, dict]:
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
                                "ground": ground(prepared, job)}


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


def closed_values(job: str = "panes") -> set:
    """Every value of a closed list: the tools' enumerated values (sides,
    layouts, directions) and the words of place, but not "current", which
    names the caller's own pane or tab and is as often left out. Another
    job's are those of its schema (job_schemas.closed_values)."""
    if job != "panes":
        import job_schemas
        return {str(value) for value in job_schemas.closed_values(job)}
    from actions import TOOLS
    values = {value for value in PB.CANON if value != "current"}
    for tool in TOOLS:
        for spec in tool["parameters"]["properties"].values():
            values.update(str(value) for value in spec.get("enum", ()))
    return values


def closed_words(rows: list[dict], job: str = "panes") -> dict:
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

    closed = closed_values(job)
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
        sample = spread(d[0]) if isinstance(d, tuple) else [word for fl, _, _ in spread(d) for word in fl]
        with torch.no_grad():
            checks[name] = [(features, net(net.ids([features]))[0].tolist()) for features in sample]
        log(f"  {name}: {len(d[1]) if isinstance(d, tuple) else len(d)} examples, "
            f"{len(net.names)} features, {len(members[name][2])} weights")
    return members, checks


def spread(examples: list) -> list:
    """CHECKED of them, taken evenly over the whole list (all, if there are fewer)."""
    return examples[::max(1, len(examples) // CHECKED)][:CHECKED]


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


def reads_its_rows(raw: bytes, rows: list[dict], second: bool = False, job: str = "panes") -> dict:
    """The file read as a request meets it (Bundle.read, then the checks as
    needle_cli.checks_agree holds a proposal to them), on every row it was
    trained on: how many it answers, how many of those as the row is
    labelled, and how many with a risky action the row does not hold. The
    nets are checked one by one in same_as_trained; this checks the reader
    that stands on them. A list is the route's own and meets no checks: it
    is as labelled when it lists what the row's list does.

    `second`: with the second reader switched on for every kind. A file is
    written whatever this finds; the manifest records it.

    A read of a pane meets pane_reads' checks of its arguments. Another job's
    answer meets that job's checks as job_rows.admitted holds a row to them,
    and is as labelled when its calls are the row's."""
    from actions import Action, interpret
    from evaluate import _norm, expected
    from needle_cli import checks_agree
    bundle, out = PB.Bundle(raw), collections.Counter(rows=len(rows))
    if second:
        bundle.second_kinds = frozenset(bundle.tools) | {PB.COMPOUND}
    for row in rows:
        texts: list = []
        calls, _ = bundle.read(row["query"], texts)
        if not calls:
            continue
        if job != "panes" or calls[0]["name"] == PB.READ:
            import job_rows
            if not job_rows.admitted(job, row["query"], calls):
                out["proposed_and_dropped"] += 1
                continue
            want = [[kind, dict(args)] for kind, args in row["actions"]]
            out["answered"] += 1
            out["as_labelled"] += [[c["name"], c["arguments"]] for c in calls] == want
            out["risky_and_not_labelled"] += any(PB.risky(c) and [c["name"], c["arguments"]] not in want
                                                 for c in calls)
            continue
        if calls[0]["name"] == PB.LIST:
            out["answered"] += 1
            out["as_labelled"] += [[c["name"], c["arguments"]["what"]] for c in calls] == \
                [[kind, args.get("what")] for kind, args in row["actions"]]
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


def refuse_misread_rows(on_rows: dict) -> None:
    """No file is written that answers one of its own training rows wrongly
    (reads_its_rows): with a risky action the row does not hold, or in any
    other way than the row is labelled."""
    if on_rows["risky_and_not_labelled"]:
        raise SystemExit(f"the file answers a training row with a risky action the row does not hold ({on_rows})")
    if on_rows["as_labelled"] != on_rows["answered"]:
        raise SystemExit(f"the file answers a training row otherwise than the row is labelled ({on_rows})")


def pack(members: list[tuple[int, dict]], known: set, pairs: set, tools: dict, free_text: list[str],
         dim: int = DIM, hidden: int = HIDDEN, value_words: dict | None = None,
         ground: dict | None = None, job: str = "panes") -> bytes:
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
    ground = {"readings": {}, "separators": sorted(SEPARATORS), "clauses_max": 2, "copy_max": {}, "copied": {},
              "frame_runs": {}, "closed_sets": {}, "said": [], **(ground or {})}
    head = {"schema": PB.SCHEMA, "job": job, "dim": dim, "hidden": hidden, "known": sorted(known), "tools": tools,
            "free_text": sorted(free_text), "value_words": value_words or {}, "nets": {}, "members": [],
            "separators": sorted(ground["separators"]), "clauses_max": int(ground["clauses_max"]),
            "copy_max": ground["copy_max"], "copied": {key: sorted(words) for key, words in ground["copied"].items()},
            "readings": {}, "frame_runs": {}, "frame_run": PB.RUN,
            "closed_sets": {tool: sorted(ground["closed_sets"].get(tool, ())) for tool in sorted(tools)},
            "said": sorted(ground["said"])}
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
    # Each tool's readings, after the weights: one a line.
    for tool in sorted(tools):
        lines = sorted(ground["readings"].get(tool, ()))
        if any("\n" in line for line in lines):
            raise ValueError(f"a reading of {tool} holds a line break")
        blob = "\n".join(lines).encode("utf-8")
        head["readings"][tool] = [add(blob), len(blob)]
    for tool in sorted(tools):
        lines = sorted(ground["frame_runs"].get(tool, ()))
        if any("\n" in line for line in lines):
            raise ValueError(f"a run of {tool}'s frames holds a line break")
        blob = "\n".join(lines).encode("utf-8")
        head["frame_runs"][tool] = [add(blob), len(blob)]
    encoded = json.dumps(head, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return PB.MAGIC + len(encoded).to_bytes(4, "little") + encoded + bytes(body)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(out: Path, rows: list[dict], row_stats: dict, seeds: list[int], free_text: list[str],
          name: str, log=print, second_kinds=(), job: str = "panes", misread_to: str | None = None) -> dict:
    if len(set(seeds)) != len(seeds) or len(seeds) < PB.MEMBERS_MIN:
        raise SystemExit(f"a bundle answers when its members agree: it takes at least {PB.MEMBERS_MIN} different seeds")
    tools = schema(job)
    if any(kind not in tools and kind != PB.COMPOUND for kind in second_kinds):
        raise SystemExit(f"the second reader cannot answer {sorted(set(second_kinds) - set(tools))}")
    data, known, pairs, used = examples(rows, tools, job)
    trained, checks = [], []
    for seed in seeds:
        log(f"member seed {seed}")
        nets, sampled = train_member(data, seed, log)
        trained.append((seed, nets))
        checks.append(sampled)
    always, closed = value_words(rows), closed_words(rows, job)
    ground_ = used.pop("ground")
    raw = pack(trained, known, pairs, tools, free_text, value_words={**closed, **always}, ground=ground_, job=job)
    same = same_as_trained(raw, checks)
    if any(member["another_output"] for member in same):
        raise SystemExit(f"the file does not read as it was trained ({same})")
    on_rows = reads_its_rows(raw, rows, job=job)
    log(f"on its own rows: {on_rows}")
    on_rows_second = reads_its_rows(raw, rows, second=True, job=job)
    log(f"on its own rows, with the second reader on for every kind: {on_rows_second}")
    if misread_to and any(n["as_labelled"] != n["answered"] or n["risky_and_not_labelled"]
                          for n in (on_rows, on_rows_second)):
        Path(misread_to).write_bytes(raw)
        log(f"the file that misreads its rows is kept in {misread_to}, to be looked into; it is no bundle")
    refuse_misread_rows(on_rows)
    refuse_misread_rows(on_rows_second)
    out.mkdir(parents=True, exist_ok=True)
    (out / PB.FILE).write_bytes(raw)
    import torch
    manifest = {
        "schema": PB.MANIFEST_SCHEMA, "job": job, "name": name, "file": PB.FILE,
        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
        "members": [{"seed": seed, "nets": len(nets),
                     "weights": sum(len(flat) for _, _, flat in nets.values()), "read_as_trained": check}
                    for (seed, nets), check in zip(trained, same)],
        "answers_when": "every member proposes the same calls and the checks admit every one of them as it was read",
        "weights_as": "little-endian half floats; each net was rounded to them before it was checked",
        "trainer": {"dim": DIM, "hidden": HIDDEN, "epochs": EPOCHS, "learning_rate": RATE,
                    "torch": torch.__version__, "threads": 1},
        "rows": {**row_stats, **used, "known_words": len(known), "known_pairs": len(pairs),
                 "value_words": len(always), "closed_words": len(closed),
                 "value_and_closed_words": len({**closed, **always}),
                 "readings": {tool: len(lines) for tool, lines in sorted(ground_["readings"].items())},
                 "copied_words": {key: len(words) for key, words in sorted(ground_["copied"].items())},
                 "separators": ground_["separators"], "clauses_max": ground_["clauses_max"],
                 "copy_max": ground_["copy_max"],
                 "sha256": hashlib.sha256("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n"
                                                  for r in rows).encode("utf-8")).hexdigest()},
        "free_text": sorted(free_text),
        "reads_its_rows": on_rows,
        "reads_its_rows_with_the_second_reader": on_rows_second,
        # The kinds the second reader answers (panes_bundle.Member.second),
        # each switched on only after it was measured against the model.
        "second_reader": {"kinds": sorted(second_kinds)},
        "code": {source: sha256_file(REPO / source)
                 for source in ("panes_bundle.py", "panes_bundle_train.py") + ROW_CODE},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", choices=PB.JOBS, default="panes")
    parser.add_argument("--out", help="where to write the bundle (the job's own directory if not given)")
    parser.add_argument("--job-rows", type=int, metavar="SEED",
                        help="rows of job_rows.py made from this seed: another job's rows (seed 0 if not given), "
                             "or added to the panes job's")
    parser.add_argument("--job-per-template", type=int, default=60)
    parser.add_argument("--seeds", type=int, nargs="+", default=[0, 1])
    parser.add_argument("--per-template", type=int, help="examples of each template (the tuner's own number if not given)")
    parser.add_argument("--free-text", nargs="*", default=[],
                        help="tools whose copied text may hold word pairs not seen in training")
    parser.add_argument("--rows", help="read rows from this file (query, actions, spans) and do not generate")
    parser.add_argument("--save-rows", help="also write the rows used to this file")
    parser.add_argument("--misread-to", help="if the file answers one of its rows otherwise than labelled, keep it "
                                             "in this file (to look into; nothing reads it as a bundle)")
    parser.add_argument("--spent", action="store_true",
                        help="add the cases of the job's spent eval sets (never a current gate set)")
    parser.add_argument("--agent", type=int, metavar="SEED",
                        help="add agent-style rows (panes_bundle_agent) made from this seed")
    parser.add_argument("--agent-per-template", type=int, default=30)
    parser.add_argument("--exclude-history", action="store_true",
                        help="leave out every agent row equal to a request of this machine's panes history "
                             "that reached a tuned model (counted; no line of it is read for anything else)")
    parser.add_argument("--second-kinds", nargs="*", default=[],
                        help="the kinds (tools, or 'compound') the second reader answers")
    parser.add_argument("--name", default="")
    args = parser.parse_args(argv)
    if args.exclude_history and args.agent is None and args.job_rows is None:
        parser.error("--exclude-history goes with --agent or --job-rows")
    if args.job != "panes" and (args.spent or args.agent is not None or args.exclude_history):
        parser.error("--spent, --agent and --exclude-history are the panes job's")
    out = Path(args.out) if args.out else PB.directory(args.job)
    if args.rows:
        refuse_gates([os.path.abspath(args.rows)])
        raw_rows = Path(args.rows).read_bytes()
        rows = [json.loads(line) for line in raw_rows.decode("utf-8").splitlines() if line.strip()]
        stats = {"source": args.rows, "source_sha256": hashlib.sha256(raw_rows).hexdigest()}
    elif args.job != "panes":
        rows, stats = [], {}
    else:
        rows, stats = build_rows(args.per_template)
    if args.spent:
        generated = {row["query"] for row in rows}
        more, stats["spent"] = spent_rows()
        rows += [row for row in more if row["query"] not in generated]
    if args.agent is not None:
        exclude, compared = history_requests() if args.exclude_history else (None, 0)
        more, made = agent_rows(args.agent, args.agent_per_template, rows, schema(), exclude)
        rows += more
        stats["agent"] = {**made, "seed": args.agent, "per_template": args.agent_per_template,
                          "history_excluded": args.exclude_history, "history_requests_compared": compared}
    if args.job != "panes" and not args.rows or args.job == "panes" and args.job_rows is not None:
        import job_rows
        import panes_bundle_agent as agent
        seed = args.job_rows if args.job_rows is not None else 0
        made, counts = job_rows.rows(args.job, seed, args.job_per_template)
        # Two rows are the same when they differ only in case and spaces: the
        # reader's frame holds a final mark ("list sessions." is not "list
        # sessions"). A history request is left out with or without one.
        present = {job_rows.same(row["query"]) for row in rows}
        exclude, compared = history_requests() if args.exclude_history else (set(), 0)
        kept = collections.Counter()
        for row in made:
            key = job_rows.same(row["query"])
            if key in present:
                kept["same_as_another_row"] += 1
            elif agent.fold(row["query"]) in exclude:
                kept["same_as_a_history_request"] += 1
            else:
                present.add(key)
                rows.append({k: row[k] for k in ("query", "actions", "spans")})
                kept["kept"] += 1
        stats["job_rows"] = {"seed": seed, "per_template": args.job_per_template, "cap": job_rows.CAP,
                             "made": {k: v for k, v in counts.items() if not k.startswith("not_admitted ")},
                             **dict(kept), "history_excluded": args.exclude_history,
                             "history_requests_compared": compared}
    if args.save_rows:
        with open(args.save_rows, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest = write(out, rows, stats, args.seeds, args.free_text, args.name,
                     second_kinds=args.second_kinds, job=args.job, misread_to=args.misread_to)
    print(json.dumps({k: manifest[k] for k in ("name", "members", "rows")}, indent=1)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
