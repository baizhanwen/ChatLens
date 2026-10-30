"""Temporary fictional progress files only; no Windows API or user data."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

DIRECTORY = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
BASE_SPEC = importlib.util.spec_from_file_location('windows_keyproof_platform', DIRECTORY / 'windows_keyproof_platform.py')
BASE = importlib.util.module_from_spec(BASE_SPEC)
BASE_SPEC.loader.exec_module(BASE)
SPEC = importlib.util.spec_from_file_location('selected_progress_test_target', DIRECTORY / 'windows_selected_progress.py')
N = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {'windows_keyproof_platform': BASE}):
    SPEC.loader.exec_module(N)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / 'synthetic-run'
        self.path = self.root / '.synthetic-run-0123456789abcdef.progress.json'
        self.initial = {'schema_version': 1, 'kind': N.KIND, 'stage': 'startup'}
        self.path.write_text(json.dumps(self.initial), encoding='utf-8')
        self.lines = []
        self.local_patch = patch.object(N, 'path_is_local', return_value=True)
        self.local = self.local_patch.start()
        self.addCleanup(self.local_patch.stop)

    def sink(self, message, *, flush):
        self.lines.append((message, flush))

    def progress(self):
        return N.PhaseProgress(self.path, self.sink, output_dir=self.output)

    def test_none_path_only_prints_all_fixed_stages_without_platform_calls(self):
        self.local.side_effect = AssertionError('None path must not use native path API')
        progress = N.PhaseProgress(None, self.sink, output_dir='unvalidated-and-unused')
        for stage in N.STAGES:
            progress.set(stage)
        self.assertEqual(len(self.lines), 23)
        self.assertTrue(all(line.startswith('[阶段] ') and flush is True for line, flush in self.lines))
        for stage in ('waiting_private_name', 'waiting_group_name', 'waiting_start'):
            self.assertIn('等待', N.STAGES[stage])
        self.local.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), self.initial)

    def test_atomic_payload_whitelist_kind_booleans_and_same_directory(self):
        progress = self.progress()
        original = os.replace
        moves = []
        def replace(source, destination):
            self.assertEqual(Path(source).parent, self.root)
            self.assertEqual(Path(destination), self.path)
            self.assertNotEqual(Path(source), self.path)
            self.assertEqual(json.loads(self.path.read_text()), self.initial)
            moves.append((source, destination))
            original(source, destination)
        streams = [SimpleNamespace(isatty=lambda: True), SimpleNamespace(isatty=lambda: False),
                   SimpleNamespace(isatty=lambda: (_ for _ in ()).throw(OSError('SYNTHETIC_PRIVATE_ERROR')))]
        with patch.object(N.os, 'replace', side_effect=replace), patch.object(N.sys, 'stdin', streams[0]), \
                patch.object(N.sys, 'stdout', streams[1]), patch.object(N.sys, 'stderr', streams[2]):
            progress.set('read_message0')
        value = json.loads(self.path.read_text())
        self.assertEqual(set(value), N.FIELDS)
        self.assertEqual(value['schema_version'], 1)
        self.assertEqual(value['kind'], N.KIND)
        self.assertEqual(value['stage'], 'read_message0')
        self.assertEqual([value[k] for k in ('stdin_tty', 'stdout_tty', 'stderr_tty')], [True, False, False])
        self.assertTrue(all(type(value[k]) is bool for k in ('stdin_tty', 'stdout_tty', 'stderr_tty')))
        self.assertEqual(len(moves), 1)
        self.assertEqual(self.lines, [('[阶段] ' + N.STAGES['read_message0'], True)])
        self.assertNotIn(str(self.root), self.path.read_text())
        self.assertNotIn('SYNTHETIC_PRIVATE_ERROR', self.path.read_text())

    def test_repeated_stage_does_not_print_or_replace(self):
        progress = self.progress()
        progress.set('startup')
        before = self.path.read_bytes()
        with patch.object(N.os, 'replace', side_effect=AssertionError('duplicate stage wrote')):
            progress.set('startup')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.lines), 1)
        progress.set('collecting')
        self.assertEqual(json.loads(self.path.read_text())['stage'], 'collecting')

    def test_invalid_stage_is_static_and_does_not_write(self):
        progress = self.progress()
        before = self.path.read_bytes()
        for stage in ('SYNTHETIC_PRIVATE_NAME', {}, True, None):
            with self.assertRaises(N.ProgressError) as caught:
                progress.set(stage)
            self.assertEqual(caught.exception.code, 'invalid_progress_stage')
            self.assertNotIn('SYNTHETIC_PRIVATE_NAME', str(caught.exception))
        self.assertEqual(self.lines, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_binding_refuses_arbitrary_existing_config_bad_parent_and_bad_nonce(self):
        arbitrary = self.root / 'config.json'
        arbitrary.write_text(json.dumps(self.initial))
        before = arbitrary.read_bytes()
        for path, output in [(arbitrary, self.output), (self.path, self.root / 'different-run'),
                             (self.path, self.root / 'another' / 'synthetic-run'),
                             (self.root / '.synthetic-run-nohex.progress.json', self.output)]:
            with self.assertRaisesRegex(N.ProgressError, '^progress_path_binding_invalid$'):
                N.PhaseProgress(path, self.sink, output_dir=output)
        with self.assertRaisesRegex(N.ProgressError, '^progress_output_binding_required$'):
            N.PhaseProgress(self.path, self.sink)
        self.assertEqual(arbitrary.read_bytes(), before)

    def test_local_regular_exclusive_file_required(self):
        with patch.object(N, 'path_is_local', return_value=False):
            with self.assertRaisesRegex(N.ProgressError, '^progress_path_binding_invalid$'):
                self.progress()
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(N.ProgressError):
            self.progress()
        self.path.rmdir()
        target = self.root / 'synthetic-original.json'
        target.write_text(json.dumps(self.initial))
        self.path.symlink_to(target)
        with self.assertRaisesRegex(N.ProgressError, '^progress_file_identity_invalid$'):
            self.progress()
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaisesRegex(N.ProgressError, '^progress_file_identity_invalid$'):
            self.progress()
        self.assertEqual(target.read_text(), json.dumps(self.initial))

    def test_marker_startup_schema_size_and_extra_fields_are_checked(self):
        for value in ({'schema_version': 1, 'stage': 'startup'},
                      dict(self.initial, kind='other'), dict(self.initial, schema_version=True),
                      dict(self.initial, stage='collecting'),
                      dict(self.initial, stdin_tty='SYNTHETIC_PRIVATE_VALUE'),
                      dict(self.initial, recorded_at_utc='x' * 65),
                      dict(self.initial, key='SYNTHETIC_PRIVATE_VALUE')):
            self.path.write_text(json.dumps(value))
            with self.assertRaises(N.ProgressError) as caught:
                self.progress()
            self.assertNotIn('SYNTHETIC_PRIVATE_VALUE', str(caught.exception))
        self.path.write_text(' ' * (N.MAX_BYTES + 1))
        with self.assertRaises(N.ProgressError):
            self.progress()

    def test_replaced_destination_is_not_overwritten(self):
        progress = self.progress()
        substitute = self.root / 'synthetic-replacement.json'
        substitute.write_text(json.dumps(self.initial))
        os.replace(substitute, self.path)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(N.ProgressError, '^progress_file_identity_invalid$'):
            progress.set('memory_markers')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.lines, [])

    def test_failed_replace_keeps_owned_file_cleans_own_temp_and_redacts_native_error(self):
        progress = self.progress()
        before = self.path.read_bytes()
        with patch.object(N.os, 'replace', side_effect=PermissionError('SYNTHETIC_PRIVATE_PATH')):
            with self.assertRaises(N.ProgressError) as caught:
                progress.set('saving_keys')
        self.assertEqual(caught.exception.code, 'progress_write_failed')
        self.assertNotIn('SYNTHETIC_PRIVATE_PATH', str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])
        self.assertEqual(self.lines, [])

    def test_fixed_enum_has_only_expected_codes(self):
        expected = '''startup private_setup config_validation driver_smoke file_preflight
read_message0 read_message1 read_contact cached_key_check process_identity memory_markers
memory_references saving_keys checking_wal waiting_private_name waiting_group_name contact_lookup
saving_config native_precheck message_baseline waiting_start collecting finalizing'''.split()
        self.assertEqual(set(N.STAGES), set(expected))


if __name__ == '__main__':
    unittest.main()
