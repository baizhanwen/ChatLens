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
PACKAGE_REVISION = '20261101-r6'
SOURCES = (
    'Start-Web.ps1', 'Start-ChatLens.cmd', 'README-WEB.txt',
    'web_runtime.py', 'web_server.py', 'windows_web_manager.py', 'windows_web_server.py',
    'windows_account_probe.py', 'windows_process_detection.py', 'windows_mapped_files.py',
    'web_data.py', 'collector_web_meta.py',
    'collector_service.py', 'collector_supervisor.py', 'collector_private.py',
    'continuous_reader.py', 'continuous_store.py', 'conversation_scope.py', 'wechat_identity.py', 'windows_selected_engine.py',
    'windows_selected_native.py', 'windows_selected_supervisor.py',
    'windows_keyproof_core.py', 'windows_keyproof_platform.py',
    'THIRD-PARTY-SELECTED-NOTICES.txt',
)
WEB_FILES = ('index.html', 'app.js', 'styles.css', 'connect.html', 'connect.js', 'connect.css',
             'assets/chatlens-mark.svg')
AI_SOURCES = ('ai_knowledge.py', 'ai_provider.py', 'ai_query.py', 'ai_statistics.py', 'ai_service.py', 'ai_web_server.py')


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
        'package_revision': PACKAGE_REVISION,
        'built_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'explicit_1_to_50_conversation_profiles_with_received_sent_unknown_statistics',
        'conversation_limits': {'minimum': 1, 'maximum': 50},
        'startup': 'Interactive readiness guide with retryable Windows process and current-account verification; explicit collection start; no automatic read-only fallback.',
        'process_selection': 'After exact executable, supported version, x64 architecture and creation-time checks, prefer exactly one visible, unowned main-window candidate (IsWindowVisible and GW_OWNER == NULL), matching .NET MainWindowHandle filtering without reading titles. Multiple valid main-window candidates remain ambiguous. Only when no valid main-window candidates exist may a unique process-tree root be selected as a hidden/tray candidate; account-reading verification is still required.',
        'account_process_family': {
            'windows_execution_status': '未验证',
            'maximum_members': 16, 'verification_passes': 2,
            'shared_enumeration_budget_seconds': 3, 'shared_region_limit': 32768, 'child_timeout_seconds': 6,
            'scope': 'One connected family of the original executable only; every member must retain exact executable, x64 architecture, supported version, creation time and parent/child identity before and after capture.',
            'evidence': 'Two complete query-only mapped-file metadata passes across every validated member. Data-file mappings are distinguished from pagefile-backed sections before resolving names. Each complete pass must positively match the original account root; any foreign root yields mismatch, and incomplete, empty or unstable evidence yields unknown.',
            'main_pid_usage': 'The selected main PID remains the collector foreground/background identity; a helper PID never replaces it.',
            'native_route': 'Documented VirtualQueryEx, QueryVirtualMemoryInformation and K32GetMappedFileNameW metadata queries with query-only process access; no PSS dependency, DuplicateHandle, memory-page or database-content read, key reinitialization or new runtime dependency for account verification.',
            'compatibility_scope': 'Required metadata APIs must be present at runtime; QueryVirtualMemoryInformation requires Windows 10 version 1607 or later. The WeChat executable remains limited to the validated 4.1.15.56 x64 version. Other Windows builds, WeChat versions and machines require separate native acceptance.',
        },
        'connection_diagnostics': 'User-requested export of cached fixed states, bounded counters and readiness booleans only. Export does not initiate another process/account scan or cloud request and contains no PID, private paths, account/contact identifiers, names, messages, keys, configuration or session credentials.',
        'account_readiness': 'Pending until the original local account and reader configuration are verified; process/window presence is not login or network-online proof.',
        'collector_readiness': 'Only a fresh heartbeat from the currently owned collector establishes actual initialization, collecting, paused or stale state; saved data and a launched process do not establish collection readiness.',
        'native_windows_sdk_execution_status': '未验证',
        'isolation': 'Account-bound profiles with independent store, checkpoint and AI history; stale scope requests are rejected.',
        'upgrade_handling': 'Reuse the selected existing collector-data in place. Preserve the legacy store, baseline, assistant and configuration. New profiles use separate private directories.',
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
    output = args.output or root / f'artifacts/deliverables/windows-check/ChatLens-Windows-Web-{PACKAGE_REVISION}.zip'
    print(json.dumps(build(root, output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
