#!/usr/bin/env python3
"""Build an offline, public-only Windows KeyProof archive with pinned PSF runtime."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import urllib.request
import zipfile

RUNTIME_URL = 'https://www.python.org/ftp/python/3.15.2/python-3.15.2-embed-amd64.zip'
RUNTIME_SHA256 = 'd012f3aa29cf07436dae8aea1fec0fd195c2612a0b059b1f915ae0afb9ad1f62'
SOURCES = ('Verify-Key.ps1', 'Start-KeyProof.cmd', 'windows_keyproof_core.py',
           'windows_keyproof_platform.py', 'README-KEYPROOF.txt', 'THIRD-PARTY-NOTICES.txt')
PREFIX = 'ChatLens-Windows-KeyProof'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build(root, output):
    cache = root / '.build/windows-keyproof-runtime/python-3.15.2-embed-amd64.zip'
    if cache.exists():
        raw = cache.read_bytes()
    else:
        with urllib.request.urlopen(RUNTIME_URL, timeout=60) as reply:
            raw = reply.read(20 * 1024**2 + 1)
        if digest(raw) != RUNTIME_SHA256:
            raise RuntimeError('official_runtime_hash_mismatch')
        cache.parent.mkdir(parents=True, exist_ok=True)
        with cache.open('xb') as target:
            target.write(raw)
    if digest(raw) != RUNTIME_SHA256:
        raise RuntimeError('cached_runtime_hash_mismatch')
    files = {}
    for name in SOURCES:
        source = root / 'scripts/windows-proof' / name
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 256 * 1024:
            raise RuntimeError('invalid_public_source')
        files[name] = source.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as runtime:
        if runtime.testzip() is not None or len(runtime.namelist()) != 39:
            raise RuntimeError('unexpected_official_runtime_archive')
        for name in runtime.namelist():
            # Official embeddable package contains only flat runtime files.
            if PurePosixPath(name).name != name or '\\' in name:
                raise RuntimeError('unexpected_runtime_member')
            files['runtime/' + name] = runtime.read(name)
    original_pth = files['runtime/python315._pth']
    if original_pth != b'python315.zip\r\n.\r\n\r\n# Uncomment to run site.main() automatically\r\n#import site\r\n':
        raise RuntimeError('unexpected_runtime_pth')
    files['runtime/python315._pth'] = original_pth.replace(b'.\r\n', b'.\r\n..\r\n', 1)
    manifest = {
        'schema_version': 1, 'built_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'selected_windows_process_and_one_encrypted_first_page_key_authentication_only',
        'windows_execution_status': '未验证', 'business_read_status': '未验证',
        'runtime': {'version': '3.15.2', 'architecture': 'x64', 'source': RUNTIME_URL,
                    'upstream_sha256': RUNTIME_SHA256,
                    'verification': 'official_release_sha256_matches_download; signature_trust_chain_not_verified',
                    'pth_modification': 'Add .. for package-local modules; site remains disabled'},
        'files': [{'name': name, 'sha256': digest(data), 'bytes': len(data)}
                  for name, data in sorted(files.items())],
    }
    files['KEYPROOF-MANIFEST.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2)+'\n').encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(PREFIX + '/' + name, data)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(files):
            raise RuntimeError('package_integrity_failed')
    return {'output': str(output), 'bytes': output.stat().st_size,
            'sha256': digest(output.read_bytes()), 'files': len(files),
            'windows_execution_status': '未验证'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output or root/'artifacts/deliverables/windows-check/ChatLens-Windows-KeyProof-20261029.zip'
    print(json.dumps(build(root, output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
