"""macOS package privacy, offline inputs and relative-launcher contracts.

Synthetic archives below never execute an application, access WeChat, install
dependencies, or use any existing private workspace.
"""
import ast
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_macos_web_package as package

MACHO = b'\xcf\xfa\xed\xfe\x0c\x00\x00\x01SYNTHETIC_NOT_EXECUTABLE'


def zip_bytes(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name, raw in entries.items():
            archive.writestr(name, raw)
    return stream.getvalue()


class MacWebPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-mac-package-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / 'deliverables/synthetic-package.zip'
        for name in package.MAC_SOURCES:
            self.write('scripts/' + name)
        for name in package.SHARED_SOURCES:
            self.write('scripts/windows-proof/' + name)
        for name in package.WEB_FILES:
            self.write('apps/web/' + name)
        for name in (*package.TEMPLATE_FILES, 'macos_package_check.py', 'python-bridge'):
            self.write(package.TEMPLATES + '/' + name)
        self.write('scripts/native/macos_probe.swift')
        self.write('scripts/native/bin/macos-probe-web', MACHO)
        self.runtime = {'runtime/bin/python3.12': MACHO,
                        'runtime/lib/python3.12/LICENSE.txt': b'SYNTHETIC_PSF_LICENSE',
                        'licenses/python-build-standalone/LICENSE.cpython.txt': b'SYNTHETIC_FULL_LICENSE'}
        wheel1 = zip_bytes({'sqlcipher3/_sqlite3.cpython-312-darwin.so': MACHO,
                            'sqlcipher3_wheels-0.5.7.dist-info/licenses/LICENSE': b'SYNTHETIC_WRAPPER_LICENSE'})
        wheel2 = zip_bytes({'zstandard/backend_c.cpython-312-darwin.so': MACHO,
                            'zstandard-0.25.0.dist-info/licenses/LICENSE': b'SYNTHETIC_ZSTD_LICENSE'})
        self.artifacts = []
        for kind, name, distribution, pkg, raw in (
                ('runtime', 'synthetic-runtime.zst', None, None, b'SYNTHETIC_PINNED_RUNTIME'),
                ('wheel', 'synthetic-sqlcipher.whl', 'sqlcipher3_wheels', 'sqlcipher3', wheel1),
                ('wheel', 'synthetic-zstd.whl', 'zstandard', 'zstandard', wheel2)):
            self.write(package.CACHE_DIRECTORY + '/' + name, raw)
            self.artifacts.append({'kind': kind, 'name': name, 'distribution': distribution,
                'package': pkg, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                'url': 'https://invalid.example/synthetic-not-downloaded'})
        self.pins = mock.patch.object(package, 'ARTIFACTS', tuple(self.artifacts))
        self.pins.start(); self.addCleanup(self.pins.stop)
        self.decoder = mock.patch.object(package, 'unpack_runtime', return_value=self.runtime)
        self.decode_runtime = self.decoder.start(); self.addCleanup(self.decoder.stop)

    def write(self, name, raw=b'# SYNTHETIC_PUBLIC_SOURCE\n'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def test_whitelist_excludes_existing_private_data_venv_and_user_documents(self):
        marker = b'SYNTHETIC_PRIVATE_MUST_NOT_SHIP'
        for name in ('artifacts/private/macos-web/keys.json', 'collector-data/private/store.sqlite3',
                     '.build/wechat-read-venv/pyvenv.cfg', '.env', 'user-material.pdf'):
            self.write(name, marker)
        result = package.build(self.root, self.output)
        self.assertEqual(result['other_machine_status'], '未验证')
        with zipfile.ZipFile(self.output) as archive:
            prefix = package.PREFIX + '/'
            entries = {name[len(prefix):]: archive.read(name) for name in archive.namelist()}
            modes = {name[len(prefix):]: archive.getinfo(name).external_attr >> 16 for name in archive.namelist()}
        manifest = json.loads(entries.pop(package.MANIFEST))
        self.assertEqual(set(entries), {row['name'] for row in manifest['files']})
        self.assertEqual(manifest['workspace'], '~/Library/Application Support/ChatLens/macOS-Web')
        self.assertFalse(manifest['notarized'])
        self.assertEqual(manifest['messages_read_during_build'], 0)
        for row in manifest['files']:
            self.assertEqual(package.digest(entries[row['name']]), row['sha256'])
            self.assertEqual(len(entries[row['name']]), row['bytes'])
        self.assertTrue(all(marker not in raw for raw in entries.values()))
        self.assertFalse(any('private' in Path(name).parts or name.endswith('pyvenv.cfg') for name in entries))
        for name in package.EXECUTABLES:
            self.assertEqual(stat.S_IMODE(modes[name]), 0o755)
        self.assertTrue(stat.S_ISREG(modes['runtime/bin/python3.12']))

    def test_existing_archive_is_never_overwritten(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b'SYNTHETIC_OLD_RELEASE')
        with self.assertRaisesRegex(FileExistsError, 'output_already_exists'):
            package.build(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), b'SYNTHETIC_OLD_RELEASE')

    def test_cache_missing_or_tampered_fails_before_payload_use(self):
        path = self.root / package.CACHE_DIRECTORY / self.artifacts[0]['name']
        path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'reviewed_macos_runtime_cache_required'):
            package.build(self.root, self.output)
        self.decode_runtime.assert_not_called()
        path.write_bytes(b'SYNTHETIC_TAMPERED_BYTES')
        with self.assertRaisesRegex(RuntimeError, 'macos_cached_artifact_hash_mismatch'):
            package.build(self.root, self.output)
        self.assertFalse(self.output.exists())

    @unittest.skipIf(sys.platform == 'win32', 'POSIX source-file link boundary')
    def test_public_source_symlink_is_not_followed(self):
        path = self.root / 'apps/web/app.js'
        path.unlink()
        path.symlink_to(self.write('synthetic-private-file', b'SYNTHETIC_PRIVATE'))
        with self.assertRaisesRegex(RuntimeError, 'public_input_redirected'):
            package.build(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_wrong_architecture_payload_is_rejected(self):
        self.runtime['runtime/bin/python3.12'] = b'SYNTHETIC_X64_NOT_ARM64'
        with self.assertRaisesRegex(RuntimeError, 'macos_arm64_binary_required'):
            package.build(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_wheel_traversal_extra_package_and_startup_hook_are_refused(self):
        artifact = self.artifacts[1]
        for name in ('../escape', '/absolute', 'another_package/evil.py', 'sqlcipher3/startup.pth'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                package.unpack_wheel(zip_bytes({name: b'SYNTHETIC'}), artifact)

    def test_runtime_tar_traversal_and_selected_symlinks_are_refused(self):
        for name, link in (('../escape', False), ('python/install/lib/python3.12/os.py', True)):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w') as archive:
                info = tarfile.TarInfo(name)
                if link:
                    info.type = tarfile.SYMTYPE; info.linkname = '/synthetic/private'
                    archive.addfile(info)
                else:
                    info.size = 9; archive.addfile(info, io.BytesIO(b'SYNTHETIC'))
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode='r:') as archive, self.assertRaises(RuntimeError):
                package.runtime_members(archive)

    def test_production_import_closure_contains_every_local_dependency(self):
        shipped = {Path(name).stem for name in (*package.MAC_SOURCES, *package.SHARED_SOURCES)}
        candidates = {p.stem for directory in (ROOT / 'scripts', ROOT / 'scripts/windows-proof') for p in directory.glob('*.py')}
        for name in (*package.MAC_SOURCES, *package.SHARED_SOURCES):
            path = ROOT / 'scripts' / ('windows-proof' if name in package.SHARED_SOURCES else '') / name
            imported = set()
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import): imported.update(row.name.split('.')[0] for row in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module: imported.add(node.module.split('.')[0])
            self.assertFalse((imported & candidates) - shipped, (name, imported - shipped))

    def test_launchers_preserve_fixed_user_workspace_and_run_isolated_verifier(self):
        templates = ROOT / package.TEMPLATES
        launcher = (templates / 'Start-ChatLens.command').read_text()
        verifier = (templates / 'Verify-Package.command').read_text()
        bridge = (templates / 'python-bridge').read_text()
        self.assertIn('--workspace "$HOME/Library/Application Support/ChatLens/macOS-Web"', launcher)
        self.assertIn('27.*', launcher)
        self.assertIn('-I -S -B', launcher)
        self.assertIn('-I -S -B', verifier)
        self.assertIn('runtime/bin/python3.12', bridge)
        self.assertIn('-I -B "$@"', bridge)
        self.assertIn('PYTHONDONTWRITEBYTECODE=1', launcher)
        self.assertIn('PYTHONNOUSERSITE=1', launcher)
        self.assertIn('unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE', launcher)
        self.assertNotIn('/Users/', launcher + verifier + bridge)
        for forbidden in ('pip install', 'xattr', 'spctl', 'sudo ', 'brew install'):
            self.assertNotIn(forbidden, launcher + verifier + bridge)

    def test_checker_rejects_unlisted_startup_files_before_importing_runtime_packages(self):
        path = ROOT / package.TEMPLATES / 'macos_package_check.py'
        spec = importlib.util.spec_from_file_location('synthetic_macos_package_checker', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        root = self.root / 'synthetic-extracted-package'
        root.mkdir()
        (root / 'MACOS-MANIFEST.json').write_text(json.dumps({'kind': 'chatlens_macos_arm64_local_web_package', 'files': []}))
        (root / 'sitecustomize.py').write_bytes(b'SYNTHETIC_MUST_NOT_EXECUTE')
        with self.assertRaises(ValueError):
            module.check(root)


if __name__ == '__main__':
    unittest.main()
