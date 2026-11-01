#!/usr/bin/env python3
"""Build an offline Windows collector package from public, pinned inputs only."""
import argparse
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import zipfile

from build_windows_selected_package import ARTIFACTS, cached_artifact, digest, unpack_payload


PREFIX = 'ChatLens-Windows-Collector'
MANIFEST = 'COLLECTOR-MANIFEST.json'
SOURCES = (
    'Verify-Collector.ps1', 'Start-Collector.cmd', 'README-COLLECTOR.txt',
    'collector_service.py', 'collector_supervisor.py', 'collector_private.py', 'windows_account_probe.py',
    'continuous_reader.py', 'continuous_store.py', 'conversation_scope.py', 'wechat_identity.py', 'collector_web_meta.py', 'windows_selected_engine.py',
    'windows_selected_native.py', 'windows_selected_supervisor.py',
    'windows_keyproof_core.py', 'windows_keyproof_platform.py',
    'THIRD-PARTY-SELECTED-NOTICES.txt',
)


def build(root, output):
    root, output = Path(root), Path(output)
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
                     'runtime/_sqlite3.pyd', 'runtime/sqlite3.dll', 'runtime/python314.zip',
                     'runtime/sqlcipher3/_sqlite3.cp314-win_amd64.pyd',
                     'runtime/zstandard/backend_c.cp314-win_amd64.pyd'):
        if required not in files:
            raise RuntimeError('required_binary_payload_missing')
    with zipfile.ZipFile(io.BytesIO(files['runtime/python314.zip'])) as stdlib:
        if not {'sqlite3/__init__.pyc', 'sqlite3/dbapi2.pyc'}.issubset(stdlib.namelist()):
            raise RuntimeError('required_stdlib_sqlite3_package_missing')
    manifest = {
        'schema_version': 1, 'kind': 'chatlens_windows_collector_package',
        'built_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'continuous_new_text_of_two_previously_selected_conversations_with_local_restart_checkpoint',
        'windows_execution_status': '未验证', 'business_read_status': '未验证',
        'restart_recovery_status': '未验证', 'chat_messages_read': 0,
        'runtime': {'version': '3.14.10', 'architecture': 'x64',
                    'pth_modification': 'Add .. for package-local modules; site remains disabled'},
        'private_data': 'New text bodies and checkpoints stay in collector-data/private/store/collector.sqlite3; local SQLite is not encrypted.',
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
            'restart_recovery_status': '未验证', 'chat_messages_read': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output or root / 'artifacts/deliverables/windows-check/ChatLens-Windows-Collector-20261101.zip'
    print(json.dumps(build(root, output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
