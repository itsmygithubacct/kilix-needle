"""Change model serialization while preserving every query and action label."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import desktop_codec
import desktop_cli
import desktop_job


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    if a.out.exists() and any(a.out.iterdir()): raise ValueError('output must be empty')
    a.out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((a.data/'manifest.json').read_text())
    fingerprints = {}
    for split in ('train','dev'):
        source = a.data/(split+'.jsonl')
        assert hashlib.sha256(source.read_bytes()).hexdigest() == manifest['files'][source.name]['sha256']
        rows = [json.loads(s) for s in source.read_text().splitlines()]
        canonical = [{'query':r['query'],'answers':r['answers']} for r in rows]
        fingerprints[split] = hashlib.sha256(json.dumps(canonical,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        for r in rows:
            original = r['answers']
            r['canonical_answers'] = original
            r['answers'] = [{'name':'desktop_plan','arguments':{'plan':json.dumps(original,separators=(',',':'),ensure_ascii=False)}}] if original else []
            r['tools'] = desktop_codec.PLAN_TOOLS
            assert desktop_codec.decode(r['answers']) == original
        (a.out/source.name).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    manifest.update(codec=desktop_codec.JSON_PLAN, schema_sha256=desktop_cli.schema_sha(),
                    action_schema_sha256=desktop_cli.schema_sha(desktop_codec.DIRECT),
                    canonical_query_label_sha256=fingerprints,
                    source_data_files=manifest['files'],
                    files={p.name:dict(sha256=hashlib.sha256(p.read_bytes()).hexdigest(),bytes=p.stat().st_size) for p in a.out.glob('*.jsonl')})
    manifest['provenance'] += '; serialization only: original queries, canonical references and ordering preserved'
    (a.out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(manifest,indent=2))


if __name__ == '__main__': main()
