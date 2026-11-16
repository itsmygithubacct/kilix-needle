#!/usr/bin/env python3
"""Build the deduplicated LFM2 command-model delivery from the published one.

Reads the extracted v9 delivery directory. Writes a new delivery directory, a
deterministic tar.gz with regular files only, and a JSON record. The model
file and every kept runtime file are byte-identical to v9; this is checked.

Each versioned llama.cpp library is kept once, under its ELF SONAME
(``lib*.so.0``). llama-server and every library reference those libraries by
SONAME only (DT_NEEDED), and the Kilix runtime copies manifest members into a
private directory and starts llama-server there, so the unversioned and fully
versioned copies were never opened. No symlink or hardlink is written.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

DUPLICATED = ('libggml-base', 'libggml', 'libllama-common', 'libllama', 'libmtmd')


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def elf_names(path):
    out = subprocess.run(['readelf', '-d', str(path)], check=True, capture_output=True, text=True).stdout
    needed, soname = [], None
    for line in out.splitlines():
        if '(NEEDED)' in line:
            needed.append(line.split('[', 1)[1].rstrip(']'))
        elif '(SONAME)' in line:
            soname = line.split('[', 1)[1].rstrip(']')
    return needed, soname


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True, help='extracted v9 delivery directory')
    parser.add_argument('--source-version', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--notice', type=Path, required=True, help='NOTICE.txt for the new delivery')
    parser.add_argument('--output', type=Path, required=True, help='directory for the new delivery and archive')
    args = parser.parse_args()

    source = args.source
    members = sorted(str(p.relative_to(source)) for p in source.rglob('*') if p.is_file())
    assert not any(p.is_symlink() for p in source.rglob('*')), 'source has symlinks'
    runtime = source / 'runtime'
    drop, keep_soname = set(), {}
    for stem in DUPLICATED:
        copies = sorted(p.name for p in runtime.glob(stem + '.so*'))
        soname = stem + '.so.0'
        assert soname in copies and len(copies) == 3, copies
        digests = {sha256(runtime / name) for name in copies}
        assert len(digests) == 1, f'{stem}: copies differ'
        assert elf_names(runtime / soname)[1] == soname, f'{stem}: SONAME is not {soname}'
        keep_soname[soname] = digests.pop()
        drop.update('runtime/' + name for name in copies if name != soname)
    kept = [name for name in members if name not in drop]
    # Every in-bundle DT_NEEDED name must still be a kept member.
    present = {Path(name).name for name in kept if name.startswith('runtime/')}
    dropped = {Path(name).name for name in drop}
    for name in kept:
        if not name.startswith('runtime/'):
            continue
        needed, _soname = elf_names(source / name)
        for dependency in needed:
            assert dependency not in dropped, f'{name} needs dropped {dependency}'
            if dependency.startswith(('libggml', 'libllama', 'libmtmd')):
                assert dependency in present, f'{name} needs missing {dependency}'

    root = args.output / args.version
    if root.exists():
        raise SystemExit(f'{root} exists')
    files = {}
    for name in kept:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == 'notices/NOTICE.txt':
            shutil.copyfile(args.notice, target)
        else:
            try:
                os.link(source / name, target)
            except OSError:
                shutil.copyfile(source / name, target)
        files[name] = {'bytes': target.stat().st_size, 'sha256': sha256(target)}
        if name != 'notices/NOTICE.txt':
            assert files[name]['sha256'] == sha256(source / name)

    archive = args.output / (args.version + '.tar.gz')
    with open(archive, 'xb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=6, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode='w', format=tarfile.USTAR_FORMAT) as tar:
                for name in kept:
                    path = root / name
                    info = tarfile.TarInfo(args.version + '/' + name)
                    info.size = path.stat().st_size
                    info.mode = 0o755 if name.startswith('runtime/') else 0o644
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    info.type = tarfile.REGTYPE
                    with open(path, 'rb') as stream:
                        tar.addfile(info, stream)
    # Read the archive back: regular files only, exact names, exact bytes.
    seen = {}
    with tarfile.open(archive, 'r:gz') as tar:
        for info in tar:
            assert info.isreg() and not info.islnk() and not info.issym(), info.name
            prefix = args.version + '/'
            assert info.name.startswith(prefix) and '..' not in Path(info.name).parts
            digest = hashlib.sha256()
            stream = tar.extractfile(info)
            while block := stream.read(1 << 20):
                digest.update(block)
            seen[info.name[len(prefix):]] = {'bytes': info.size, 'sha256': digest.hexdigest()}
    assert seen == files and list(seen) == kept

    record = {
        'archive': archive.name,
        'archive_bytes': archive.stat().st_size,
        'archive_sha256': sha256(archive),
        'version': args.version,
        'root': args.version,
        'source_version': args.source_version,
        'model_bytes': files['model.gguf']['bytes'],
        'model_sha256': files['model.gguf']['sha256'],
        'notice_bytes': files['notices/NOTICE.txt']['bytes'],
        'notice_sha256': files['notices/NOTICE.txt']['sha256'],
        'installed_bytes': sum(item['bytes'] for item in files.values()),
        'members': len(kept),
        'member_types': 'regular files only; no symlinks, hardlinks or directories',
        'dropped_members': sorted(drop),
        'dropped_bytes': sum((source / name).stat().st_size for name in drop),
        'kept_sonames': keep_soname,
        'files': files,
    }
    (args.output / (args.version + '.json')).write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({k: v for k, v in record.items() if k != 'files'}, indent=2))


if __name__ == '__main__':
    main()
