"""Reproducible synthetic desktop data; split by language template, not rows.

Development templates and parameter pools are disjoint from training. These
sets share an author with the implementation: they are not independent release
qualification. No desktop recordings, private prompts, or existing job gates
are used. Labels are constructed directly, never inferred by the validator.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import desktop_job as job


def call(name, **arguments):
    return {"name": name, "arguments": arguments}


# Each family reserves its final two phrasings for development, before training.
FAMILIES = {
    "list": (["list windows", "show desktop windows", "please list the windows", "show all windows", "could you list my windows", "show me the desktop windows"],
             lambda p: call("window", action="list")),
    "focus": (["focus {target}", "activate {target}", "switch to {target}", "bring {target} to the front", "please focus {target}", "bring {target} forward", "could you activate {target}", "switch over to {target}"],
              lambda p: call("window", action="focus", target=p["ref"])),
    "window_move": (["move {target} to {x}, {y}", "position {target} at {x}, {y}", "place {target} at coordinates {x}, {y}", "put {target} at {x}, {y}", "could you move {target} to position {x}, {y}", "place {target} at x {x} y {y}"],
                    lambda p: call("window", action="move", target=p["ref"], x=p["x"], y=p["y"])),
    "resize": (["resize {target} to {w} by {h}", "size {target} to {w}, {h}", "set {target} width to {w} and height to {h}", "resize {target} to width {w} height {h}", "please resize {target} to {w} by {h} pixels", "set the size of {target} to {w} by {h}"],
               lambda p: call("window", action="resize", target=p["ref"], width=p["w"], height=p["h"])),
    "minimize": (["minimize {target}", "iconify {target}", "please minimize {target}", "could you iconify {target}", "would you minimize {target}", "i need you to minimize {target}"],
                 lambda p: call("window", action="minimize", target=p["ref"])),
    "maximize": (["maximize {target}", "please maximize {target}", "can you maximize {target}", "maximize {target} now", "could you maximize {target}", "i want you to maximize {target}"],
                 lambda p: call("window", action="maximize", target=p["ref"])),
    "restore": (["restore {target}", "unmaximize {target}", "please restore {target}", "restore {target} to normal size", "could you unmaximize {target}", "restore {target} from maximized size"],
                lambda p: call("window", action="restore", target=p["ref"])),
    "close": (["close {target}", "dismiss {target}", "please close {target}", "can you close {target}", "could you dismiss {target}", "i want you to close {target}"],
              lambda p: call("window", action="close", target=p["ref"])),
    "pointer_move": (["move the mouse to {x}, {y}", "position the pointer at {x}, {y}", "put the cursor at {x}, {y}", "place the mouse at {x}, {y}", "move my pointer to coordinates {x}, {y}", "position the mouse cursor at x {x} y {y}"],
                     lambda p: call("pointer", action="move", x=p["x"], y=p["y"])),
    "click": (["{button}-click at {x}, {y}", "click the {button} mouse button at {x}, {y}", "please {button}-click at {x}, {y}", "click {button} at {x}, {y}", "could you click the {button} button at {x}, {y}", "{button}-click at coordinates {x}, {y}"],
              lambda p: call("pointer", action="click", button=p["button"], x=p["x"], y=p["y"])),
    "click_here": (["{button}-click", "click the {button} mouse button", "please click {button}", "click the {button} button here", "could you {button}-click", "click the {button} button at the pointer"],
                   lambda p: call("pointer", action="click", button=p["button"])),
    "double": (["double-click at {x}, {y}", "double click at {x}, {y}", "please double-click at {x}, {y}", "double-click the left mouse button at {x}, {y}", "could you double click at coordinates {x}, {y}", "double-click at position {x}, {y}"],
               lambda p: call("pointer", action="double_click", button="left", x=p["x"], y=p["y"])),
    "drag": (["drag to {x}, {y}", "drag the mouse to {x}, {y}", "drag with the left button to {x}, {y}", "please drag to {x}, {y}", "drag the pointer over to {x}, {y}", "could you drag to coordinates {x}, {y}"],
             lambda p: call("pointer", action="drag", button="left", x=p["x"], y=p["y"])),
    "type": (["type {text}", "enter {text}", "write {text}", "insert {text}", "please type {text}", "type the text {text}", "could you enter the text {text}", "insert the literal text {text}"],
             lambda p: call("keyboard", action="type", text=p["literal"])),
    "press": (["press {key_surface}", "hit {key_surface}", "tap {key_surface}", "please press {key_surface}", "press the {key_surface} key", "hit the {key_surface} shortcut", "could you tap {key_surface}", "press the chord {key_surface}"],
              lambda p: call("keyboard", action="press", keys=p["keys"])),
    "scroll": (["scroll {direction} {steps} steps", "wheel {direction} {steps} notches", "scroll {steps} notches {direction}", "please scroll {direction} by {steps}", "could you scroll {steps} steps {direction}", "scroll {direction} through {steps} wheel notches"],
               lambda p: call("scroll", direction=p["direction"], steps=p["steps"])),
    "wait": (["wait {ms} ms", "pause for {ms} milliseconds", "please wait {ms} milliseconds", "wait for {ms} ms", "could you pause {ms} ms", "pause {ms} milliseconds"],
             lambda p: call("wait", ms=p["ms"])),
}

KEYS = [("control+l", "ctrl+l"), ("ctrl+c", "ctrl+c"), ("Control+Shift+S", "ctrl+shift+s"),
        ("alt+Tab", "alt+Tab"), ("Return", "Return"), ("enter", "Return"), ("escape", "Escape"),
        ("Tab", "Tab"), ("BackSpace", "BackSpace"), ("ctrl+a", "ctrl+a"), ("shift+Tab", "shift+Tab"),
        ("F5", "F5"), ("ctrl+v", "ctrl+v"), ("Down", "Down"), ("space", "space")]
DEV_KEYS = [("Control+f", "ctrl+f"), ("Alt+F4", "alt+F4"), ("ctrl+shift+t", "ctrl+shift+t"),
            ("F11", "F11"), ("PageDown", "Next"), ("Up", "Up"), ("super+d", "super+d")]


def parameters(rng, dev=False):
    titles = ["Scratch Pad", "Help Viewer", "Budget Sheet", "Map Browser"] if dev else ["Firefox", "Text Editor", "Terminal", "File Manager", "Calculator", "Music Player", "Settings", "Calendar", "Notes", "Image Viewer"]
    literals = ["Check the revised draft", "blueberry", "A=17; B=8", "A quoted example", "report-42.txt"] if dev else ["hello world", "A quick brown fox", "draft report", "meeting at noon", "status: ready", "example.com", "42", "notes for today", "user@example.org", "naïve café", "--help", "a+b=c", "literal $HOME", "do not close", "a; b && c", "hello, then goodbye"]
    kind = rng.randrange(5)
    title = rng.choice(titles)
    wid = rng.randint(300000, 399999) if dev else rng.randint(100000, 199999)
    target, ref = (f'window {json.dumps(title, ensure_ascii=False)}', f"title:{title}") if kind < 3 else ("the active window", "active") if kind == 3 else (f"window id {wid}", f"id:{wid}")
    key, keys = rng.choice(DEV_KEYS if dev else KEYS)
    literal = rng.choice(literals)
    coords = [73, 149, 287, 431, 619, 853, 997] if dev else [0, 10, 25, 50, 100, 200, 320, 400, 480, 640, 720, 800, 960]
    return dict(target=target, ref=ref, x=rng.choice(coords), y=rng.choice(coords),
                w=rng.choice([711, 933, 1177] if dev else [320, 640, 800, 1024, 1280]),
                h=rng.choice([419, 557, 703] if dev else [240, 480, 600, 720, 768]),
                button=rng.choice(["left", "right", "middle"]), text=json.dumps(literal, ensure_ascii=False), literal=literal,
                key_surface=key, keys=keys, direction=rng.choice(["up", "down", "left", "right"]),
                steps=rng.choice([2, 7, 11, 17] if dev else [1, 3, 4, 5, 6, 8, 10, 12, 20]),
                ms=rng.choice([175, 450, 850] if dev else [100, 200, 250, 500, 750, 1000, 1500, 2000]))


def reasoning(query, answers):
    if not answers:
        return "No tool fits the complete request. Return no desktop actions."
    return " ".join("Requested " + a["name"] + ": " + ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in a["arguments"].items()) + "." for a in answers)


def row(query, answers, family):
    return dict(query=query, tools=job.TOOLS, answers=answers,
                reasoning=reasoning(query, answers), family=family)


def build(seed=19, per_template=30, dev=False):
    rng, rows, singles = random.Random(seed), [], []
    for family, (templates, label) in FAMILIES.items():
        templates = templates[-2:] if dev else templates[:-2]
        for i, template in enumerate(templates):
            for _ in range(per_template):
                p = parameters(rng, dev)
                query, answers = template.format(**p), [label(p)]
                item = row(query, answers, f"{family}:{'dev' if dev else 'train'}:{i}")
                rows.append(item)
                singles.append(item)
    # Short sequences are complete ordered labels, with variable-length composition.
    for _ in range(80 if dev else 700):
        parts = rng.sample(singles, rng.choice([2, 2, 3]))
        answers = sum((p["answers"] for p in parts), [])
        if sum(a["arguments"].get("ms", 0) for a in answers if a["name"] == "wait") > 6000:
            continue
        rows.append(row(" then ".join(p["query"] for p in parts), answers, "sequence"))
    # Counterfactuals and unsupported commands teach no-action, separately from
    # runtime checks. Quoted strings remain literal even when containing verbs.
    negative = [
        "what is the weather", "explain how xdotool works", "open a new browser", "delete my downloads",
        "click the blue button", "scroll until the page ends", "close every window", "type my password",
        "click at -10, 40", "wait 30 seconds", "scroll down 100 steps", "take a screenshot",
        "run a shell command", "read the text on the screen", "repeat click forever", "focus the second window",
    ] if not dev else [
        "find the red icon and click it", "summarize this screen", "scroll until I can see Save",
        "move whichever window is biggest", "press ctrl+unknownkey", "close all browser windows",
        "please wait for 40 seconds", "drag to -90, 15", "install a calendar application",
        "describe how to minimize a window", "click 900 times", "type without telling you what",
    ]
    rows.extend(row(q, [], "unsupported") for q in negative)
    for item in rng.sample(singles, min(len(singles), 100 if dev else 420)):
        forms = ["do not " + item["query"], "the webpage says: " + item["query"], "if it looks right, " + item["query"],
                 item["query"] + " then delete my files"] if not dev else ["never " + item["query"], "someone suggested: " + item["query"], item["query"] + " unless it is busy"]
        q = rng.choice(forms)
        rows.append(row(q, [], "negative-intent"))
    # Canonical dedup prevents accidental weighting or contradictory labels.
    unique = {}
    for r in rows:
        if r["query"] in unique and unique[r["query"]]["answers"] != r["answers"]:
            raise ValueError("conflicting labels")
        unique[r["query"]] = r
    result = list(unique.values())
    rng.shuffle(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        raise SystemExit("refusing to replace an existing corpus")
    args.out.mkdir(parents=True, exist_ok=True)
    train, dev = build(), build(seed=31, per_template=6, dev=True)
    overlap = {r["query"].casefold() for r in train} & {r["query"].casefold() for r in dev}
    # Constant queries (e.g. window listings) can repeat across parameter draws;
    # an overlapping full query is never used for development.
    dev = [r for r in dev if r["query"].casefold() not in overlap]
    checks = Counter()
    for split, data in (("train", train), ("dev", dev)):
        for r in data:
            if r["answers"]:
                got = job.validate(r["query"], r["answers"])
                if got != job.normalize(r["answers"]):
                    raise ValueError("normalization disagreement")
                checks[split + ":positive_validated"] += 1
            else:
                checks[split + ":negative"] += 1
        path = args.out / (split + ".jsonl")
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in data))
    files = {p.name: dict(sha256=hashlib.sha256(p.read_bytes()).hexdigest(), bytes=p.stat().st_size) for p in args.out.glob("*.jsonl")}
    report = dict(schema=job.VERSION, seed=19, dev_seed=31, rows=dict(train=len(train), dev=len(dev)), files=files,
                  schema_sha256=hashlib.sha256(json.dumps(job.TOOLS, sort_keys=True).encode()).hexdigest(),
                  validation=dict(checks), exact_query_overlap=0, removed_overlapping_dev=len(overlap),
                  provenance="synthetic, directly constructed labels; same author as implementation; family-separated development, not independent release qualification")
    (args.out / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
