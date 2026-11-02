#!/usr/bin/env python3
"""Assemble the macOS Web test package from fixed public, offline inputs.

No source database, private workspace, account, model key or installed virtual
environment is opened. Upstream archives are checked before any payload is used.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import stat
import tarfile
import zipfile


PREFIX = 'ChatLens-macOS-arm64-Web'
PACKAGE_REVISION = '20261102-r1'
MANIFEST = 'MACOS-MANIFEST.json'
CACHE_DIRECTORY = '.build/macos-web-runtime'
TEMPLATES = 'scripts/macos-web-package'
ARTIFACTS = (
    {'kind': 'runtime', 'name': 'cpython-3.12.14+20260814-aarch64-apple-darwin-pgo+lto-full.tar.zst',
     'bytes': 54796258, 'sha256': '1f6e3adb3467ba77dd7930f91af8960a7cbb1586715efc313af8dd478a8911ec',
     'url': 'https://github.com/astral-sh/python-build-standalone/releases/download/20260814/cpython-3.12.14%2B20260814-aarch64-apple-darwin-pgo%2Blto-full.tar.zst'},
    {'kind': 'wheel', 'distribution': 'sqlcipher3_wheels', 'package': 'sqlcipher3',
     'name': 'sqlcipher3_wheels-0.5.7-cp312-cp312-macosx_11_0_arm64.whl', 'bytes': 3163917,
     'sha256': '75636e5eea2669cc0d31d39c639dc69769824020456ba7b181a0c4836b221952',
     'url': 'https://files.pythonhosted.org/packages/7d/f9/981464b4f8144f8ce5b6ffa80429b24fd342ce1991a48bc9fdc60c438469/sqlcipher3_wheels-0.5.7-cp312-cp312-macosx_11_0_arm64.whl'},
    {'kind': 'wheel', 'distribution': 'zstandard', 'package': 'zstandard',
     'name': 'zstandard-0.25.0-cp312-cp312-macosx_11_0_arm64.whl', 'bytes': 640436,
     'sha256': '913cbd31a400febff93b564a23e17c3ed2d56c064006f54efec210d586171c00',
     'url': 'https://files.pythonhosted.org/packages/aa/1c/d920d64b22f8dd028a8b90e2d756e431a5d86194caa78e3819c7bf53b4b3/zstandard-0.25.0-cp312-cp312-macosx_11_0_arm64.whl'},
)
MAC_SOURCES = (
    'macos_web.py', 'macos_web_server.py', 'macos_collector.py', 'macos_active_account.py',
    'macos_reader_setup.py', 'macos_capture_keys.py', 'prepare_wechat_read_test.py',
    'ai_knowledge.py', 'ai_provider.py', 'ai_query.py', 'ai_statistics.py', 'ai_service.py', 'ai_web_server.py',
)
SHARED_SOURCES = (
    'collector_private.py', 'collector_web_meta.py', 'continuous_reader.py', 'continuous_store.py',
    'conversation_scope.py', 'scope_history.py', 'wechat_identity.py', 'windows_selected_engine.py',
    'windows_keyproof_platform.py', 'web_data.py', 'web_server.py',
)
WEB_FILES = ('index.html', 'app.js', 'styles.css', 'connect.html', 'connect.js', 'connect.css',
             'assets/chatlens-mark.svg')
TEMPLATE_FILES = ('Start-ChatLens.command', 'Verify-Package.command', 'README-macOS.txt',
                  'THIRD-PARTY-MACOS-NOTICES.txt', 'licenses/LICENSE.sqlcipher.txt',
                  'licenses/LICENSE.openssl.txt', 'licenses/LICENSE.zstd.txt')
EXECUTABLES = {'Start-ChatLens.command', 'Verify-Package.command',
               '.build/wechat-read-venv/bin/python', 'runtime/bin/python3.12',
               'scripts/native/bin/macos-probe-web'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _name(name):
    value = PurePosixPath(name)
    if (not isinstance(name, str) or not name or value.is_absolute() or '\\' in name
            or '..' in value.parts or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise RuntimeError('package_member_path_invalid')
    return value


def _regular(root, relative, limit=1024 * 1024):
    relative = _name(relative)
    target = Path(root)
    for part in relative.parts:
        target /= part
        if target.is_symlink():
            raise RuntimeError('public_input_redirected')
    info = target.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= limit:
        raise RuntimeError('public_input_invalid')
    return target.read_bytes()


def cached_artifact(root, artifact):
    try:
        raw = _regular(root, CACHE_DIRECTORY + '/' + artifact['name'], 128 * 1024**2)
    except FileNotFoundError:
        raise RuntimeError('reviewed_macos_runtime_cache_required') from None
    if len(raw) != artifact['bytes'] or digest(raw) != artifact['sha256']:
        raise RuntimeError('macos_cached_artifact_hash_mismatch')
    return raw


def _put(files, name, data):
    _name(name)
    if name.casefold() in {key.casefold() for key in files}:
        raise RuntimeError('package_member_collision')
    files[name] = data


def runtime_members(archive):
    """Read only installed-runtime and full upstream licenses, never extract."""
    files, total, count = {}, 0, 0
    excluded = {'site-packages', '__pycache__', 'test', 'tests', 'idlelib', 'ensurepip', 'turtledemo', 'pkgconfig'}
    for member in archive:
        count += 1
        if count > 25000:
            raise RuntimeError('runtime_member_limit')
        _name(member.name)
        target = None
        if member.name.startswith('python/licenses/'):
            target = 'licenses/python-build-standalone/' + member.name[len('python/licenses/'):]
        elif member.name == 'python/PYTHON.json':
            target = 'licenses/python-build-standalone/PYTHON.json'
        elif member.name.startswith('python/install/'):
            relative = member.name[len('python/install/'):]
            parts = PurePosixPath(relative).parts
            if relative == 'bin/python3.12' or relative.startswith('lib/'):
                if not any(part in excluded or part.startswith('config-') for part in parts):
                    if not relative.endswith(('.pyc', '.pyo', '.a', '.pc')):
                        target = 'runtime/' + relative
        if target is None or member.isdir():
            continue
        if not member.isfile():
            # bin/python aliases are deliberately outside the selected subset.
            raise RuntimeError('runtime_selected_link_refused')
        if not 0 <= member.size <= 64 * 1024**2:
            raise RuntimeError('runtime_file_size_invalid')
        total += member.size
        if total > 256 * 1024**2:
            raise RuntimeError('runtime_payload_size_limit')
        source = archive.extractfile(member)
        if source is None:
            raise RuntimeError('runtime_member_unavailable')
        raw = source.read(member.size + 1)
        if len(raw) != member.size:
            raise RuntimeError('runtime_member_size_mismatch')
        _put(files, target, raw)
    required = {'runtime/bin/python3.12', 'runtime/lib/python3.12/os.py',
                'runtime/lib/python3.12/encodings/__init__.py',
                'runtime/lib/python3.12/LICENSE.txt',
                'licenses/python-build-standalone/LICENSE.cpython.txt',
                'licenses/python-build-standalone/LICENSE.openssl-3.txt',
                'licenses/python-build-standalone/PYTHON.json'}
    if not required <= files.keys():
        raise RuntimeError('runtime_required_files_missing')
    metadata = json.loads(files['licenses/python-build-standalone/PYTHON.json'])
    if metadata.get('python_version') != '3.12.14' or metadata.get('target_triple') != 'aarch64-apple-darwin':
        raise RuntimeError('runtime_build_metadata_invalid')
    return files


def unpack_runtime(data):
    import zstandard
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as source:
        with tarfile.open(fileobj=source, mode='r|') as archive:
            return runtime_members(archive)


def unpack_wheel(data, artifact):
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) > 512:
            raise RuntimeError('wheel_member_limit')
        for member in archive.infolist():
            name = member.filename
            parts = _name(name).parts
            if member.is_dir():
                continue
            if stat.S_ISLNK(member.external_attr >> 16):
                raise RuntimeError('wheel_link_refused')
            if (not parts or not (parts[0] == artifact['package']
                    or parts[0].startswith(artifact['distribution'] + '-') and parts[0].endswith('.dist-info'))):
                raise RuntimeError('wheel_unexpected_member')
            if member.file_size > 32 * 1024**2 or name.endswith(('.pyc', '.pyo', '.pth')):
                raise RuntimeError('wheel_member_invalid')
            if name.endswith('/RECORD'):
                continue
            _put(files, 'runtime/lib/python3.12/site-packages/' + name, archive.read(member))
    if not any('/licenses/LICENSE' in name for name in files):
        raise RuntimeError('wheel_license_missing')
    return files


def build(root, output):
    root, output = Path(root), Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError('output_already_exists')
    files = {}
    for name in MAC_SOURCES:
        _put(files, 'scripts/' + name, _regular(root, 'scripts/' + name))
    for name in SHARED_SOURCES:
        _put(files, 'scripts/windows-proof/' + name, _regular(root, 'scripts/windows-proof/' + name))
    for name in WEB_FILES:
        _put(files, 'apps/web/' + name, _regular(root, 'apps/web/' + name))
    for name in TEMPLATE_FILES:
        _put(files, name, _regular(root, TEMPLATES + '/' + name))
    _put(files, 'scripts/macos_package_check.py', _regular(root, TEMPLATES + '/macos_package_check.py'))
    _put(files, '.build/wechat-read-venv/bin/python', _regular(root, TEMPLATES + '/python-bridge'))
    _put(files, 'scripts/native/macos_probe.swift', _regular(root, 'scripts/native/macos_probe.swift'))
    _put(files, 'scripts/native/bin/macos-probe-web', _regular(root, 'scripts/native/bin/macos-probe-web'))
    # Every input is checked before opening the destination archive.
    payloads = [(artifact, cached_artifact(root, artifact)) for artifact in ARTIFACTS]
    for artifact, raw in payloads:
        payload = unpack_runtime(raw) if artifact['kind'] == 'runtime' else unpack_wheel(raw, artifact)
        for name, data in payload.items():
            _put(files, name, data)
    required = {'runtime/lib/python3.12/site-packages/sqlcipher3/_sqlite3.cpython-312-darwin.so',
                'runtime/lib/python3.12/site-packages/zstandard/backend_c.cpython-312-darwin.so'}
    if not required <= files.keys():
        raise RuntimeError('required_macos_extension_missing')
    for name in ('runtime/bin/python3.12', 'scripts/native/bin/macos-probe-web', *sorted(required)):
        raw = files[name]
        if len(raw) < 8 or raw[:8] != b'\xcf\xfa\xed\xfe\x0c\x00\x00\x01':
            raise RuntimeError('macos_arm64_binary_required')
    manifest = {'schema_version': 1, 'kind': 'chatlens_macos_arm64_local_web_package',
        'package_revision': PACKAGE_REVISION, 'built_at_utc': datetime.now(timezone.utc).isoformat(),
        'platform': 'macOS', 'architecture': 'arm64', 'supported_test_scope': {'macos_major': 27, 'wechat': '4.1.11'},
        'startup': 'Start-ChatLens.command', 'package_check': 'Verify-Package.command',
        'workspace': '~/Library/Application Support/ChatLens/macOS-Web',
        'data_handling': 'No private data shipped. Account- and scope-isolated persistent user workspace; development workspace is not migrated automatically. Mac and Windows data remain separate.',
        'scope_history_continuity': 'Retained conversations reuse same-account already-collected text and the previous active checkpoint. Added conversations start at their new source MAX. Existing targets merge saved text without replacing message rowids or checkpoint; source profiles and AI histories remain independent.',
        'runtime': {'python': '3.12.14', 'sqlcipher3_wheels': '0.5.7', 'sqlcipher': '4.12.0 community',
                    'sqlite': '3.51.1', 'openssl_in_sqlcipher_wheel': '3.6.0', 'zstandard': '0.25.0'},
        'compatibility_bridge': '.build/wechat-read-venv/bin/python is a relative shell bridge to the bundled standalone runtime; no local virtual environment or pyvenv.cfg is copied.',
        'first_use_requirements': ['Apple Command Line Tools', 'Apple system Python with LLDB module', 'User-granted Terminal access to the selected WeChat account data'],
        'first_use': 'Current account detected from process/open-file metadata; initialization and conversation selection are user-confirmed. Only newly enabled selected text is collected.',
        'native_probe_minimum_macos': '27.0', 'notarized': False,
        'other_machine_installation_status': '未验证', 'terminal_gatekeeper_status': '未验证',
        'native_collection_from_extracted_package_status': '未验证',
        'messages_read_during_build': 0, 'model_called_during_build': False,
        'upstream_artifacts': list(ARTIFACTS),
        'files': [{'name': name, 'sha256': digest(raw), 'bytes': len(raw),
                   'mode': '0755' if name in EXECUTABLES or name.endswith(('.so', '.dylib')) else '0644'}
                  for name, raw in sorted(files.items())]}
    files[MANIFEST] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(PREFIX + '/' + name, date_time=(2026, 11, 2, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if name in EXECUTABLES or name.endswith(('.so', '.dylib')) else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, raw)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(files):
            raise RuntimeError('macos_package_integrity_failed')
    return {'package_name': output.name, 'bytes': output.stat().st_size, 'sha256': digest(output.read_bytes()),
            'files': len(files), 'other_machine_status': '未验证', 'real_wechat_read_during_build': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output or root / f'artifacts/deliverables/macos-web/ChatLens-macOS-arm64-Web-{PACKAGE_REVISION}.zip'
    print(json.dumps(build(root, output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
