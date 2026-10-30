#!/usr/bin/env python3
"""Build a pinned, offline Windows selected-conversation proof package."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import urllib.request
import zipfile


PREFIX = 'ChatLens-Windows-SelectedTest'
MANIFEST = 'SELECTED-MANIFEST.json'
CACHE_DIRECTORY = '.build/windows-selected-runtime'
SOURCES = (
    'Verify-SelectedTest.ps1', 'Start-SelectedTest.cmd',
    'windows_selected_engine.py', 'windows_selected_runner.py',
    'windows_selected_native.py', 'windows_keyproof_core.py',
    'windows_keyproof_platform.py', 'README-SELECTED.txt',
    'THIRD-PARTY-SELECTED-NOTICES.txt',
)
ARTIFACTS = (
    {
        'name': 'python-3.14.10-embed-amd64.zip', 'kind': 'runtime',
        'version': '3.14.10', 'bytes': 12619664,
        'url': 'https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip',
        'sha256': '90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159',
    },
    {
        'name': 'sqlcipher3-0.6.4-cp314-cp314-win_amd64.whl', 'kind': 'wheel',
        'version': '0.6.4', 'bytes': 2626446,
        'url': 'https://files.pythonhosted.org/packages/f1/eb/b243c4233f5487a1acf1810ac53bde439c3f11b606a4641328adc43d41b8/sqlcipher3-0.6.4-cp314-cp314-win_amd64.whl',
        'sha256': '33a4bb79d5bffbdcc1443ff7fd10556ad781fd4986a710c0c577b817b06e11ac',
    },
    {
        'name': 'zstandard-0.25.0-cp314-cp314-win_amd64.whl', 'kind': 'wheel',
        'version': '0.25.0', 'bytes': 516118,
        'url': 'https://files.pythonhosted.org/packages/3a/43/241f9615bcf8ba8903b3f0432da069e857fc4fd1783bd26183db53c4804b/zstandard-0.25.0-cp314-cp314-win_amd64.whl',
        'sha256': 'c19bcdd826e95671065f8692b5a4aa95c52dc7a02a4c5a0cac46deb879a017a2',
    },
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def cached_artifact(root, artifact):
    path = root / CACHE_DIRECTORY / artifact['name']
    if path.is_symlink():
        raise RuntimeError('cache_symlink_refused')
    if path.exists():
        if not path.is_file() or path.stat().st_size != artifact['bytes']:
            raise RuntimeError('cached_artifact_size_mismatch')
        data = path.read_bytes()
    else:
        with urllib.request.urlopen(artifact['url'], timeout=60) as reply:
            data = reply.read(artifact['bytes'] + 1)
        if len(data) != artifact['bytes'] or digest(data) != artifact['sha256']:
            raise RuntimeError('downloaded_artifact_integrity_failed')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as target:
            target.write(data)
    if digest(data) != artifact['sha256']:
        raise RuntimeError('cached_artifact_hash_mismatch')
    return data


def unpack_payload(data, flat=False):
    result = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) > 256 or archive.testzip() is not None:
            raise RuntimeError('invalid_upstream_archive')
        for entry in archive.infolist():
            name = entry.filename
            if entry.is_dir():
                continue
            parts = PurePosixPath(name).parts
            if (not re.fullmatch(r'[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*', name)
                    or any(part in ('.', '..') for part in parts)
                    or (flat and len(parts) != 1)
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000
                    or name.lower() in {key.lower() for key in result}
                    or entry.file_size > 32 * 1024**2):
                raise RuntimeError('unexpected_upstream_archive_member')
            result[name] = archive.read(entry)
    return result


def build(root, output):
    if output.exists() or output.is_symlink():
        raise FileExistsError('output_already_exists')
    files = {}
    for name in SOURCES:
        source = root / 'scripts/windows-proof' / name
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 1024**2:
            raise RuntimeError('invalid_public_source')
        files[name] = source.read_bytes()
    for artifact in ARTIFACTS:
        payload = unpack_payload(cached_artifact(root, artifact), artifact['kind'] == 'runtime')
        for name, data in payload.items():
            target = 'runtime/' + name
            if target.lower() in {key.lower() for key in files}:
                raise RuntimeError('upstream_payload_collision')
            files[target] = data
    original_pth = b'python314.zip\r\n.\r\n\r\n# Uncomment to run site.main() automatically\r\n#import site\r\n'
    if files.get('runtime/python314._pth') != original_pth:
        raise RuntimeError('unexpected_runtime_pth')
    files['runtime/python314._pth'] = original_pth.replace(b'.\r\n', b'.\r\n..\r\n', 1)
    for required in ('runtime/python.exe', 'runtime/python314.dll',
                     'runtime/sqlcipher3/_sqlite3.cp314-win_amd64.pyd',
                     'runtime/zstandard/backend_c.cp314-win_amd64.pyd'):
        if required not in files:
            raise RuntimeError('required_binary_payload_missing')
    manifest = {
        'schema_version': 1,
        'built_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'two_user_selected_conversations_four_controlled_new_text_messages_only',
        'windows_execution_status': '未验证', 'business_read_status': '未验证',
        'chat_messages_read': 0,
        'runtime': {'version': '3.14.10', 'architecture': 'x64',
                    'pth_modification': 'Add .. for package-local modules; site remains disabled'},
        'upstream_artifacts': list(ARTIFACTS),
        'verification': 'pinned_sha256_and_archive_contents; no_signature_trust_chain_or_windows_execution_claim',
        'files': [{'name': name, 'sha256': digest(data), 'bytes': len(data)}
                  for name, data in sorted(files.items())],
    }
    files[MANIFEST] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(PREFIX + '/' + name, data)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(files):
            raise RuntimeError('package_integrity_failed')
    return {'output': str(output), 'bytes': output.stat().st_size,
            'sha256': digest(output.read_bytes()), 'files': len(files),
            'windows_execution_status': '未验证', 'business_read_status': '未验证',
            'chat_messages_read': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output or root / 'artifacts/deliverables/windows-check/ChatLens-Windows-SelectedTest-20261030.zip'
    print(json.dumps(build(root, output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
