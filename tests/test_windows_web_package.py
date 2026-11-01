"""Portable package privacy, completeness and upgrade launcher contracts.

Archives below contain tiny, explicitly synthetic runtime bytes. They never
execute Windows binaries or access a real user's collector-data.
"""
import ast
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_windows_web_package as package
import build_windows_collector_package as collector_package


def zip_bytes(entries):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w') as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return result.getvalue()


class WindowsWebPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / 'deliverables' / 'synthetic-upgrade.zip'
        for name in package.SOURCES:
            path = self.root / 'scripts/windows-proof' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'# Explicit synthetic public source\n')
        for name in package.AI_SOURCES:
            (self.root / 'scripts' / name).write_bytes(b'# Explicit synthetic AI source\n')
        for name in package.WEB_FILES:
            path = self.root / 'apps/web' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'Explicit synthetic web source\n')
        original_pth = (b'python314.zip\r\n.\r\n\r\n'
                        b'# Uncomment to run site.main() automatically\r\n#import site\r\n')
        runtime = zip_bytes({
            'python.exe': b'SYNTHETIC_NOT_AN_EXECUTABLE',
            'python314.dll': b'SYNTHETIC_DLL',
            'python314._pth': original_pth,
            'python314.zip': zip_bytes({'sqlite3/__init__.pyc': b'SYNTHETIC',
                                        'sqlite3/dbapi2.pyc': b'SYNTHETIC'}),
            '_sqlite3.pyd': b'SYNTHETIC', 'sqlite3.dll': b'SYNTHETIC',
            'LICENSE.txt': b'SYNTHETIC_RUNTIME_LICENSE',
        })
        wheel = zip_bytes({
            'sqlcipher3/_sqlite3.cp314-win_amd64.pyd': b'SYNTHETIC',
            'zstandard/backend_c.cp314-win_amd64.pyd': b'SYNTHETIC',
            'synthetic.dist-info/licenses/LICENSE': b'SYNTHETIC_WHEEL_LICENSE',
        })
        self.artifacts = []
        for name, kind, data in [('synthetic-runtime.zip', 'runtime', runtime),
                                 ('synthetic-wheel.whl', 'wheel', wheel)]:
            target = self.root / package.CACHE_DIRECTORY / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self.artifacts.append({'name': name, 'kind': kind, 'bytes': len(data),
                                   'sha256': hashlib.sha256(data).hexdigest(),
                                   'url': 'https://invalid.example/synthetic-do-not-download'})
        self.pins = mock.patch.object(package, 'ARTIFACTS', tuple(self.artifacts))
        self.pins.start()
        self.addCleanup(self.pins.stop)

    def test_archive_is_whitelisted_hashed_and_excludes_existing_private_data(self):
        private = self.root / 'collector-data/private'
        private.mkdir(parents=True)
        for name in ('scope.json', 'keys.json', 'collector.sqlite3', 'provider.json'):
            (private / name).write_bytes(b'SYNTHETIC_PRIVATE_DATA_MUST_NOT_SHIP')
        result = package.build(self.root, self.output)
        self.assertEqual(result['windows_execution_status'], '未验证')
        with zipfile.ZipFile(self.output) as archive:
            prefix = package.PREFIX + '/'
            entries = {name[len(prefix):]: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(entries.pop(package.MANIFEST))
        self.assertEqual(manifest['package_revision'], '20261101-r3')
        self.assertEqual(manifest['conversation_limits'], {'minimum': 1, 'maximum': 50})
        self.assertEqual(set(entries), {entry['name'] for entry in manifest['files']})
        for entry in manifest['files']:
            self.assertEqual(hashlib.sha256(entries[entry['name']]).hexdigest(), entry['sha256'])
            self.assertEqual(len(entries[entry['name']]), entry['bytes'])
        self.assertIn('windows_web_manager.py', entries)
        self.assertIn('windows_web_server.py', entries)
        self.assertIn('windows_account_probe.py', entries)
        self.assertIn('windows_process_detection.py', entries)
        self.assertTrue({'web/connect.html', 'web/connect.js', 'web/connect.css'} <= entries.keys())
        self.assertTrue(all(b'SYNTHETIC_PRIVATE_DATA_MUST_NOT_SHIP' not in data for data in entries.values()))
        self.assertFalse(any('private' in Path(name).parts or 'collector-data' in Path(name).parts
                             for name in entries))
        self.assertIn('runtime/LICENSE.txt', entries)
        self.assertIn('runtime/synthetic.dist-info/licenses/LICENSE', entries)
        self.assertEqual(entries['runtime/python314._pth'].count(b'..\r\n'), 1)
        self.assertIn(b'#import site', entries['runtime/python314._pth'])
        self.assertEqual((private / 'provider.json').read_bytes(), b'SYNTHETIC_PRIVATE_DATA_MUST_NOT_SHIP')

    def test_existing_archive_is_never_overwritten(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b'SYNTHETIC_EXISTING_RELEASE')
        with self.assertRaisesRegex(FileExistsError, 'output_already_exists'):
            package.build(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), b'SYNTHETIC_EXISTING_RELEASE')

    def test_offline_cache_missing_does_not_attempt_download(self):
        (self.root / package.CACHE_DIRECTORY / self.artifacts[0]['name']).unlink()
        with mock.patch.object(package, 'cached_artifact') as cache:
            with self.assertRaisesRegex(RuntimeError, 'reviewed_runtime_cache_required_for_offline_build'):
                package.build(self.root, self.output)
        cache.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_tampered_cache_fails_before_archive_creation(self):
        target = self.root / package.CACHE_DIRECTORY / self.artifacts[0]['name']
        data = target.read_bytes()
        target.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        with self.assertRaisesRegex(RuntimeError, 'cached_artifact_hash_mismatch'):
            package.build(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_manager_or_connect_source_cannot_make_partial_package(self):
        for relative in ('scripts/windows-proof/windows_web_manager.py', 'apps/web/connect.js'):
            target = self.root / relative
            contents = target.read_bytes()
            target.unlink()
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'invalid_public_source'):
                package.build(self.root, self.output)
            target.write_bytes(contents)
        self.assertFalse(self.output.exists())

    def test_production_manifest_closes_local_python_imports(self):
        source_names = set(package.SOURCES) | set(package.AI_SOURCES)
        shipped = {Path(name).stem for name in source_names if name.endswith('.py')}
        candidates = {file.stem for directory in (ROOT / 'scripts', ROOT / 'scripts/windows-proof')
                      for file in directory.glob('*.py')}
        for name in sorted(source_names):
            if not name.endswith('.py'):
                continue
            directory = ROOT / 'scripts' if name in package.AI_SOURCES else ROOT / 'scripts/windows-proof'
            path = directory / name
            self.assertTrue(path.is_file(), name)
            imports = set()
            for item in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                if isinstance(item, ast.Import):
                    imports.update(alias.name.split('.')[0] for alias in item.names)
                elif isinstance(item, ast.ImportFrom) and item.module:
                    imports.add(item.module.split('.')[0])
            self.assertFalse((imports & candidates) - shipped, (name, (imports & candidates) - shipped))

    def test_real_launcher_enables_management_and_requires_full_shared_surface(self):
        path = ROOT / 'scripts/windows-proof/Start-Web.ps1'
        data = path.read_bytes()
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        self.assertNotIn(b'\n', data.replace(b'\r\n', b''))
        text = data.decode('utf-8-sig')
        self.assertIn("'--manage-scopes'", text)
        self.assertIn("'--wait-for-wechat'", text)
        self.assertNotIn("'--read-only'", text)
        self.assertNotIn("Read-Host '输入候选编号", text)
        self.assertIn('窗口存在不代表已登录', text)
        self.assertIn('登录原账号后可在网页重新检测', text)
        for required in ('windows_web_manager.py', 'windows_web_server.py', 'windows_account_probe.py', 'windows_process_detection.py', 'conversation_scope.py',
                         'wechat_identity.py', 'web/connect.html', 'web/connect.js', 'web/connect.css'):
            self.assertIn("'" + required + "'", text)
        self.assertIn('collector-data 文件夹', text)
        self.assertIn('Assert-PrivateAcl $dataDirectory $true', text)
        self.assertIn('保留原会话、消息、检查点与模型配置', text)

    def test_collector_builder_includes_account_probe_and_all_local_imports(self):
        shipped = {Path(name).stem for name in collector_package.SOURCES if name.endswith('.py')}
        self.assertIn('windows_account_probe', shipped)
        directory = ROOT / 'scripts/windows-proof'
        local_modules = {file.stem for file in directory.glob('*.py')}
        for name in collector_package.SOURCES:
            if not name.endswith('.py'):
                continue
            imports = set()
            for item in ast.walk(ast.parse((directory / name).read_text(encoding='utf-8-sig'))):
                if isinstance(item, ast.Import):
                    imports.update(alias.name.split('.')[0] for alias in item.names)
                elif isinstance(item, ast.ImportFrom) and item.module:
                    imports.add(item.module.split('.')[0])
            self.assertFalse((imports & local_modules) - shipped,
                             (name, (imports & local_modules) - shipped))


if __name__ == '__main__':
    unittest.main()
