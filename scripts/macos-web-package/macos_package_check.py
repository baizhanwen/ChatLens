#!/usr/bin/env python3
"""Check public package hashes and an in-memory runtime; never inspect WeChat."""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import sys


def check(root):
    root = Path(root)
    manifest = json.loads((root / 'MACOS-MANIFEST.json').read_text())
    if manifest.get('kind') != 'chatlens_macos_arm64_local_web_package':
        raise ValueError
    names = set()
    for row in manifest['files']:
        name = row['name']
        relative = Path(name)
        if (not isinstance(name, str) or relative.is_absolute() or '..' in relative.parts
                or name in names or any(ord(c) < 32 for c in name)):
            raise ValueError
        names.add(name)
        path = root
        for component in relative.parts:
            path /= component
            if path.is_symlink():
                raise ValueError
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != row['bytes']:
            raise ValueError
        if hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError
        if path.is_file() and path.name != '.DS_Store':
            actual.add(path.relative_to(root).as_posix())
    if actual != names | {'MACOS-MANIFEST.json'}:
        raise ValueError
    if sys.version_info[:3] != (3, 12, 14) or sys.platform != 'darwin':
        raise ValueError
    # The checker runs with -I -S. Add only the hashed wheel directory after
    # rejecting extra files, so .pth/sitecustomize cannot precede verification.
    sys.path.insert(0, str(root / 'runtime/lib/python3.12/site-packages'))
    from sqlcipher3 import dbapi2 as sqlcipher
    import zstandard
    connection = sqlcipher.connect(':memory:')
    cipher = connection.execute('PRAGMA cipher_version').fetchone()[0]
    connection.close()
    if cipher != '4.12.0 community' or zstandard.__version__ != '0.25.0':
        raise ValueError
    sample = b'ChatLens explicit synthetic in-memory package check'
    if zstandard.ZstdDecompressor().decompress(zstandard.ZstdCompressor().compress(sample)) != sample:
        raise ValueError
    return {'status': 'passed', 'public_files_checked': len(names),
            'python': '3.12.14', 'sqlcipher': cipher, 'zstandard': zstandard.__version__,
            'wechat_or_private_workspace_accessed': False, 'network_requested': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', required=True)
    parser.parse_args()
    try:
        print(json.dumps(check(Path(__file__).resolve().parents[1]), ensure_ascii=False))
        return 0
    except Exception:
        print('reason=macos_package_check_failed', flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
