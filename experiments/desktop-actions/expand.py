"""Broaden parameter copying and no-action coverage using synthetic labels.

This is development-driven augmentation, not a fresh blind qualification set.
The original development file is preserved byte for byte and checked for exact
query overlap. Training receives only train.jsonl and its manifest.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import re

import prepare as base


def parameters(rng, dev=False):
    assert not dev
    p = original_parameters(rng)
    words = "Amber Birch Cedar Delta Ember Finch Grove Harbor Indigo Juniper Kite Lake Maple North Olive Pine Quartz River Stone Terra Union Violet Willow Zebra".split()
    nouns = "Archive Board Console Dashboard Editor Folder Gallery Inbox Journal Library Monitor Notebook Organizer Planner Queue Reader Studio Table Workspace".split()
    title = rng.choice(words) + " " + rng.choice(nouns) + (" " + str(rng.randint(1, 99)) if rng.random() < .3 else "")
    if p['ref'].startswith('title:'):
        p.update(target='window ' + json.dumps(title), ref='title:' + title)
    elif p['ref'].startswith('id:'):
        wid = rng.randint(100000, 299999)
        p.update(target=f'window id {wid}', ref=f'id:{wid}')
    def number(low, high, excluded):
        while (n := rng.randint(low, high)) in excluded: pass
        return n
    p.update(x=number(0, 1900, {73,149,287,431,619,853,997}),
             y=number(0, 1050, {73,149,287,431,619,853,997}),
             w=number(200, 1600, {711,933,1177}), h=number(150, 950, {419,557,703}),
             ms=number(1, 2200, {175,450,850}))
    literal = rng.choice([
        f'{rng.choice(words).lower()} {rng.choice(nouns).lower()}',
        f'{rng.choice(words)} report {rng.randint(1,9999)}',
        f'item-{rng.randint(1,9999)}.txt',
        f'{rng.choice(words).lower()}@example.org',
        f'{rng.choice(words)}: status {rng.choice(["ready", "pending", "draft", "complete"])}',
        f'{rng.choice(words)} + {rng.choice(words)} = {rng.randint(1,999)}',
        f'notes for {rng.choice(["Monday", "Tuesday", "Friday", "today", "next week"])}',
        rng.choice(['naïve café', 'literal $HOME', '--help', 'a; b && c', 'do not close']),
    ])
    p.update(literal=literal, text=json.dumps(literal, ensure_ascii=False))
    modifiers = rng.choice(['', 'ctrl+', 'control+', 'alt+', 'shift+', 'ctrl+shift+', 'super+'])
    key = rng.choice(list('abcdefghijklmnopqrstuvwxyz0123456789') + ['Return','enter','Escape','Tab','BackSpace','Delete','Home','End','Left','Right','Down','Up','PageUp','PageDown','F2','F3','F5','F8','F12'])
    surface = modifiers + key
    canonical = base.job.key_chord(surface)
    # Keep the original seven development chords out as complete combinations.
    if canonical not in {v for _, v in base.DEV_KEYS}:
        p.update(key_surface=surface, keys=canonical)
    return p


original_parameters = base.parameters


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError('output must be empty')
    rows = [json.loads(s) for s in (args.base/'train.jsonl').read_text().splitlines()]
    base.parameters = parameters
    extra = base.build(seed=47, per_template=50)
    rows.extend(extra)
    rng = random.Random(59)
    singles = [r for r in extra if len(r['answers']) == 1]
    for _ in range(700):
        parts = rng.sample(singles, rng.choice([2,2,3]))
        answers = sum((r['answers'] for r in parts), [])
        if sum(a['arguments'].get('ms', 0) for a in answers if a['name']=='wait') <= 6000:
            rows.append(base.row(' then '.join(r['query'] for r in parts), answers, 'sequence-expanded'))
    # Cover negation, conditionals, indirect speech and incomplete instructions
    # across many ordinary actions instead of a handful of fixed literals.
    negatives = [
        'do not {q}', "don't {q}", 'never {q}', 'avoid this: {q}',
        'the webpage says: {q}', 'someone suggested: {q}', 'an example says: {q}',
        'if it is safe, {q}', 'when ready, {q}', '{q} unless it is busy',
        '{q} if the document is saved', '{q} after lunch',
        '{q} then delete my files', 'repeat this forever: {q}',
    ]
    for _ in range(1500):
        q = rng.choice(singles)['query']
        # Avoid unnatural stacked courtesy in most counterfactuals.
        q = re.sub(r'^(?:please|can you|could you|would you|i want you to|i need you to) ', '', q)
        rows.append(base.row(rng.choice(negatives).format(q=q), [], 'negative-expanded'))
    # Distinct polite prefixes teach the wrapper language without copying the
    # final two primary phrasings reserved by each original action family.
    for item in rng.sample(singles, min(500, len(singles))):
        q = re.sub(r'^(?:please|can you|could you|would you|i want you to|i need you to) ', '', item['query'])
        rows.append(base.row(rng.choice(['kindly ', 'will you ', "i'd like you to ", 'i would like you to ']) + q,
                             item['answers'], 'courtesy-expanded'))
    for _ in range(500):
        p = parameters(rng)
        focus = base.call('window', action='focus', target=p['ref'])
        type_call = base.call('keyboard', action='type', text=p['literal'])
        mode = rng.randrange(4)
        if mode == 0:
            q = f'focus {p["target"]} then press ctrl+a then type {p["text"]}'
            answers = [focus, base.call('keyboard', action='press', keys='ctrl+a'), type_call]
        elif mode == 1:
            q = f'activate {p["target"]} then enter {p["text"]} then press Return'
            answers = [focus, type_call, base.call('keyboard', action='press', keys='Return')]
        elif mode == 2:
            q = f'move the pointer to {p["x"]}, {p["y"]} then left-click then wait {p["ms"]} ms'
            answers = [base.call('pointer', action='move', x=p['x'], y=p['y']),
                       base.call('pointer', action='click', button='left'), base.call('wait', ms=p['ms'])]
        else:
            q = f'focus {p["target"]} then resize the active window to {p["w"]} by {p["h"]}'
            answers = [focus, base.call('window', action='resize', target='active', width=p['w'], height=p['h'])]
        rows.append(base.row(q, answers, 'common-sequence'))
    unique = {}
    for r in rows:
        if r['query'] in unique and unique[r['query']]['answers'] != r['answers']:
            raise ValueError('conflicting constructed labels')
        unique[r['query']] = r
    rows = list(unique.values()); rng.shuffle(rows)
    dev_bytes = (args.base/'dev.jsonl').read_bytes()
    dev = [json.loads(s) for s in dev_bytes.decode().splitlines()]
    dev_queries = {r['query'].casefold() for r in dev}
    if any(r['query'].casefold() in dev_queries for r in rows):
        raise ValueError('expanded training overlaps the frozen development queries')
    counts = Counter()
    for row in rows:
        if row['answers']:
            assert base.job.validate(row['query'], row['answers']) == base.job.normalize(row['answers'])
            counts['positive_validated'] += 1
        else: counts['negative'] += 1
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out/'train.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows))
    (args.out/'dev.jsonl').write_bytes(dev_bytes)
    files = {p.name:dict(sha256=hashlib.sha256(p.read_bytes()).hexdigest(),bytes=p.stat().st_size) for p in args.out.glob('*.jsonl')}
    report = dict(schema=base.job.VERSION, seeds=[19,47,59], rows=dict(train=len(rows), dev=len(dev)), files=files,
                  schema_sha256=hashlib.sha256(json.dumps(base.job.TOOLS, sort_keys=True).encode()).hexdigest(),
                  validation=dict(counts), exact_query_overlap=0,
                  provenance='synthetic labels; development-driven coverage expansion; frozen original development file, not independent qualification; negative framing vocabulary is now shared across splits')
    (args.out/'manifest.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__ == '__main__': main()
