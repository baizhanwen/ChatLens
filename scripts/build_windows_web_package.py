#!/usr/bin/env python3
"""Build the offline Windows Web/collector package from fixed public inputs."""
import argparse
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import zipfile

from build_windows_selected_package import ARTIFACTS, CACHE_DIRECTORY, cached_artifact, digest, unpack_payload


PREFIX = 'ChatLens-Windows-Web'
MANIFEST = 'WEB-MANIFEST.json'
SOURCES = (
    'Start-Web.ps1', 'Start-ChatLens.cmd', 'README-WEB.txt',
    'web_runtime.py', 'web_server.py', 'web_data.py', 'collector_web_meta.py',
    'collector_service.py', 'collector_supervisor.py', 'collector_private.py',
    'continuous_reader.py', 'continuous_store.py', 'windows_selected_engine.py',
    'windows_selected_native.py', 'windows_selected_supervisor.py',
    'windows_keyproof_core.py', 'windows_keyproof_platform.py',
    'THIRD-PARTY-SELECTED-NOTICES.txt',
)
WEB_FILES = ('index.html', 'app.js', 'styles.css', 'assets/chatlens-mark.svg')
AI_SOURCES = ('ai_knowledge.py', 'ai_provider.py', 'ai_service.py', 'ai_web_server.py')


def _public_bytes(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024**2:
        raise RuntimeError('invalid_public_source')
    return path.read_bytes()


def build(root, output):
    root, output = Path(root), Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError('output_already_exists')
    files = {name: _public_bytes(root / 'scripts/windows-proof' / name) for name in SOURCES}
    for name in AI_SOURCES:
        files[name] = _public_bytes(root / 'scripts' / name)
    for name in WEB_FILES:
        files['web/' + name] = _public_bytes(root / 'apps/web' / name)
    # Release assembly is offline: a missing reviewed input is a build failure.
    for artifact in ARTIFACTS:
        if not (root / CACHE_DIRECTORY / artifact['name']).is_file():
            raise RuntimeError('reviewed_runtime_cache_required_for_offline_build')
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
    for required in ('runtime/python.exe', 'runtime/python314.dll', 'runtime/python314.zip',
                     'runtime/_sqlite3.pyd', 'runtime/sqlite3.dll',
                     'runtime/sqlcipher3/_sqlite3.cp314-win_amd64.pyd',
                     'runtime/zstandard/backend_c.cp314-win_amd64.pyd'):
        if required not in files:
            raise RuntimeError('required_binary_payload_missing')
    with zipfile.ZipFile(io.BytesIO(files['runtime/python314.zip'])) as stdlib:
        if not {'sqlite3/__init__.pyc', 'sqlite3/dbapi2.pyc'}.issubset(stdlib.namelist()):
            raise RuntimeError('required_stdlib_sqlite3_package_missing')
    manifest = {
        'schema_version': 1, 'kind': 'chatlens_windows_local_web_package',
        'built_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'local_web_view_of_two_previously_selected_conversations_and_owned_collector',
        'windows_execution_status': '未验证', 'web_live_end_to_end_status': '未验证',
        'chat_messages_read_during_build': 0,
        'runtime': {'version': '3.14.10', 'architecture': 'x64',
                    'pth_modification': 'Add .. for package-local modules; site remains disabled'},
        'data_handling': 'User chooses existing collector-data locally; private data is not copied into this package. HTTP binds loopback only.',
        'upstream_artifacts': list(ARTIFACTS),
        'verification': 'fixed_hashes_and_archive_contents_only; no_native_windows_or_live_web_claim',
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
    return {'package_name': output.name, 'bytes': output.stat().st_size,
            'sha256': digest(output.read_bytes()), 'files': len(files),
            'windows_execution_status': '未验证', 'web_live_end_to_end_status': '未验证'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output or root / 'artifacts/deliverables/windows-check/ChatLens-Windows-Web-20261101.zip'
    print(json.dumps(build(root, output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
