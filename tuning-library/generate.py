#!/usr/bin/env python3
"""Generate kilix-needle training examples from this library's corpus.

    python3 generate.py [--seed N] [--per-template K] [--exclude FILE ...] [--out FILE]

Every example is {"query": ..., "actions": [[action, {args}], ...],
"spans": [[words, value], ...]} in kilix-needle's internal action vocabulary;
`spans` says which words of the query each bound value came from. kilix-needle turns that into the
schema its model sees. The output depends only on the corpus and the seed.

`--exclude` takes kilix-needle eval files (JSONL with a "request" field):
any generated query equal to an eval request, after case and whitespace
folding, is dropped, and the count is reported. Exact-match exclusion does
not remove paraphrases; the fine-tuning gate therefore uses a held-out set
written without sight of this corpus.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import re
import sys

ROOT = Path(__file__).resolve().parent
CORPUS = ROOT / "corpus"
_SLOT = re.compile(r"\{(\w+)\}")


def _fold(text: str) -> str:
    return " ".join(text.casefold().split())


def load_vocab(corpus: Path = CORPUS) -> dict:
    vocab = corpus / "vocab"

    def lines(name):
        return [line.strip() for line in (vocab / name).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]

    def table(name):
        return json.loads((vocab / name).read_text(encoding="utf-8"))

    return {"program": lines("programs.txt"), "name": lines("names.txt"),
            "command": lines("commands.txt"), "side": table("sides.json"),
            "layout": table("layouts.json"), "direction": table("directions.json"),
            "number": table("ordinals.json")}


def load_templates(corpus: Path = CORPUS) -> list[dict]:
    templates = []
    for path in sorted((corpus / "actions").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data["templates"]:
            actions = item.get("actions")
            if actions is None:
                actions = [[data["action"], item.get("args", {})]]
            templates.append({"text": item["text"], "actions": actions, "source": path.name})
    return templates


def _fill(template: dict, vocab: dict, rng: random.Random) -> dict:
    """One filling of a template: surface text plus canonical bindings."""
    bound, surface = {}, {}
    numbers = sorted(vocab["number"])
    for slot in dict.fromkeys(_SLOT.findall(template["text"])):
        if slot in ("side", "side_adj"):
            # "split {side}" takes an adverb ("on the left"); "the {side_adj} pane"
            # takes an adjective ("left", "top"). Both bind the same canonical side.
            if "side" not in bound:
                bound["side"] = rng.choice(sorted(vocab["side"]))
            forms = vocab["side"][bound["side"]]
            surface[slot] = rng.choice(forms["adverb" if slot == "side" else "adjective"])
        elif slot in ("layout", "direction"):
            canonical = rng.choice(sorted(vocab[slot]))
            bound[slot], surface[slot] = canonical, rng.choice(vocab[slot][canonical])
        elif slot in ("num", "num2", "ordinal"):
            key = "num" if slot == "ordinal" else slot
            if key not in bound:
                taken = {bound.get("num"), bound.get("num2")}
                bound[key] = rng.choice([n for n in numbers if n not in taken])
            forms = vocab["number"][bound[key]]
            surface[slot] = forms[2] if slot == "ordinal" else rng.choice(forms[:2])
        elif slot == "amount":
            value = rng.randint(1, 30)
            bound[slot], surface[slot] = value, str(value)
        elif slot in ("program", "name", "command"):
            value = rng.choice(vocab[slot])
            bound[slot], surface[slot] = value, value
        else:
            raise ValueError(f"{template['source']}: unknown slot {{{slot}}}")
    text = _SLOT.sub(lambda m: surface[m.group(1)], template["text"])

    def resolve(value):
        if isinstance(value, str) and value.startswith("$"):
            return bound[value[1:]]
        return value

    actions = [[kind, {key: resolve(value) for key, value in args.items()}]
               for kind, args in template["actions"]]
    # Which words of the query each bound value came from, so a consumer can
    # write the derivation Needle is trained to produce before its calls.
    spans = [[surface[slot], bound["side" if slot == "side_adj" else
                                    "num" if slot == "ordinal" else slot]]
             for slot in dict.fromkeys(_SLOT.findall(template["text"]))]
    return {"query": text, "actions": actions, "spans": spans}


def generate(seed: int = 0, per_template: int = 12, exclude: set[str] | None = None,
             corpus: Path = CORPUS) -> tuple[list[dict], int]:
    """Examples, and how many were dropped for matching an excluded request."""
    rng = random.Random(seed)
    vocab = load_vocab(corpus)
    seen, examples, excluded = set(), [], 0
    for template in load_templates(corpus):
        slots = _SLOT.findall(template["text"])
        attempts = per_template if slots else 1
        for _ in range(attempts * 3):
            if attempts == 0:
                break
            example = _fill(template, vocab, rng)
            key = _fold(example["query"])
            if key in seen:
                continue
            seen.add(key)
            if exclude and key in exclude:
                excluded += 1
                continue
            examples.append(example)
            attempts -= 1
    rng.shuffle(examples)
    return examples, excluded


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--per-template", type=int, default=12)
    parser.add_argument("--exclude", nargs="*", default=[], metavar="FILE")
    parser.add_argument("--out", default="-")
    args = parser.parse_args(argv)
    exclude = set()
    for path in args.exclude:
        with open(path, encoding="utf-8") as handle:
            exclude |= {_fold(json.loads(line)["request"]) for line in handle if line.strip()}
    examples, dropped = generate(args.seed, args.per_template, exclude)
    text = "".join(json.dumps(example, ensure_ascii=False) + "\n" for example in examples)
    if args.out == "-":
        sys.stdout.write(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
    print(f"{len(examples)} examples, {dropped} dropped as eval requests", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
