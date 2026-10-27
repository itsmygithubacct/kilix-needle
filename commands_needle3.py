"""The pinned, installed Needle 3 alternate for Ask; never installs on use."""
from contextlib import contextmanager
import json
from pathlib import Path
import re

import asset
import compact_model

ROOT = Path(__file__).resolve().parent
ASSET_ID = 'needle3-kilix-commands'


def selection():
    path = ROOT / 'needle3-model.json'
    if path.stat().st_size > 65536:
        raise ValueError('Needle3 selection is too large')
    chosen = json.loads(path.read_text())
    required = {'candidate.json', 'weights.cact', 'libneedle3-cached.so', 'tools.json'}
    if (not isinstance(chosen, dict) or chosen.get('schema') != 'kilix.needle3.model/v1'
            or not isinstance(chosen.get('files'), dict)
            or not isinstance(chosen.get('version'), str)
            or not re.fullmatch(r'needle3-kilix-commands-[0-9]{8}-v[1-9][0-9]*', chosen['version'])
            or not required <= chosen['files'].keys()):
        raise ValueError('invalid Needle3 release selection')
    for name, item in chosen['files'].items():
        if (not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
                or not isinstance(item, dict) or type(item.get('bytes')) is not int
                or item['bytes'] <= 0 or not isinstance(item.get('sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise ValueError('invalid Needle3 release member')
    return chosen


@contextmanager
def open_runtime(root=None):
    chosen = selection()
    spec, destination = asset._installed_spec(ASSET_ID, root)
    import commands_selection
    commands_selection.verify_catalog('needle3', spec, chosen)
    with compact_model.open_runtime(Path(destination) / 'candidate.json',
                                    expected=chosen['files']['candidate.json']) as runtime:
        yield runtime
