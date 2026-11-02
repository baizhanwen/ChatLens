"""Synthetic process/open-file metadata; never inspect real WeChat or keys."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import macos_active_account as active


def metadata(**changes):
    result = dict(bundle_id='com.tencent.xinWeChat', running=True, matching_applications=1,
                  process_id=12345, process_started_at=1700000000.125, version='4.1.15',
                  frontmost=True, screen_state_available=True, screen_locked=False)
    result.update(changes)
    return result


class ActiveAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-macos-active-')
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'synthetic-home'
        self.base = self.home / 'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files'
        self.base.mkdir(parents=True)
        self.probe = self.root / 'synthetic-probe-web'
        self.probe.write_bytes(b'SYNTHETIC_EXECUTABLE_NOT_RUN')
        self.probe.chmod(0o700)
        self.first = self.account('wxid_SYNTHETIC_ACTIVE')
        self.second = self.account('wxid_SYNTHETIC_INACTIVE')
        self.probe_documents = [metadata(), metadata()]
        self.listing = ('p12345\nn' + str(self.first / 'message/message_0.db') + '\n').encode()
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def account(self, name):
        root = self.base / name / 'db_storage'
        (root / 'message').mkdir(parents=True)
        (root / 'message/message_0.db').write_bytes(b'SYNTHETIC_BODY_AND_KEYS_MUST_NOT_BE_READ')
        return root

    def runner(self, command, **options):
        self.calls.append((command, options))
        if command == [str(self.probe), 'wechat']:
            value = self.probe_documents.pop(0)
            if callable(value):
                value = value()
            raw = value if isinstance(value, bytes) else json.dumps(value).encode()
            return subprocess.CompletedProcess(command, 0, stdout=raw)
        self.assertEqual(command, ['/usr/sbin/lsof', '-nP', '-p', '12345', '-Fn'])
        return subprocess.CompletedProcess(command, 0, stdout=self.listing)

    def detect(self, **options):
        with mock.patch.object(active, '_local_volume', return_value=True):
            return active.detect_active_account(self.probe, runner=options.pop('runner', self.runner),
                                                home=self.home, **options)

    def assert_error(self, code=None, **options):
        with self.assertRaises(active.ActiveAccountError) as failure:
            self.detect(**options)
        if code is not None:
            self.assertEqual(failure.exception.code, code)
        self.assertEqual(failure.exception.reason, failure.exception.code)
        self.assertRegex(str(failure.exception), '^macos_active_[a-z_]+$')
        for private in (str(self.root), 'SYNTHETIC_ACTIVE', 'SYNTHETIC_INACTIVE', 'BODY_AND_KEYS'):
            self.assertNotIn(private, str(failure.exception))
        return failure.exception

    def test_single_active_account_uses_open_files_without_scanning_or_reading_payload(self):
        self.listing += ('n' + str(self.first / 'message') + '\nn/dev/null\n').encode()
        with mock.patch.object(Path, 'iterdir', side_effect=AssertionError('must not enumerate accounts')), \
                mock.patch.object(Path, 'glob', side_effect=AssertionError('must not discover arbitrary databases')), \
                mock.patch.object(Path, 'open', side_effect=AssertionError('must not read any file payload')):
            account = self.detect()
        self.assertEqual(account.db_root, self.first)
        self.assertEqual(account.pid, 12345)
        self.assertEqual(account.started_at, 1700000000.125)
        self.assertEqual(account.client_version, '4.1.15')
        self.assertNotIn(str(self.root), repr(account))
        self.assertNotIn('SYNTHETIC_ACTIVE', repr(account))
        self.assertEqual(len(self.calls), 3)
        for _, options in self.calls:
            self.assertEqual(options, dict(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False))

    def test_multiple_open_files_in_same_root_are_one_account(self):
        contact = self.first / 'contact/contact.db'
        contact.parent.mkdir()
        contact.write_bytes(b'SYNTHETIC_CONTACT_NOT_READ')
        self.listing += ('n' + str(contact) + '\nn' + str(contact) + '\n').encode()
        self.assertEqual(self.detect().db_root, self.first)

    def test_two_active_roots_fail_closed_despite_one_expected_account(self):
        self.listing += ('n' + str(self.second / 'message/message_0.db') + '\n').encode()
        error = self.assert_error('macos_active_account_ambiguous')
        self.assertIs(error.wechat_running, True)

    def test_old_root_with_only_log_file_still_cannot_be_guessed_inactive(self):
        stale_log = self.first / 'synthetic-log-only'
        stale_log.write_bytes(b'SYNTHETIC_NO_CHAT_PAYLOAD')
        self.listing = ('p12345\nn' + str(stale_log) + '\nn' +
                        str(self.second / 'message/message_0.db') + '\n').encode()
        error = self.assert_error('macos_active_account_ambiguous')
        self.assertIs(error.wechat_running, True)

    def test_explicit_native_no_process_is_distinct_from_invalid_probe(self):
        self.probe_documents = [dict(platform='wechat', os='macOS', running=False, matching_applications=0)]
        error = self.assert_error('macos_active_process_not_running')
        self.assertIs(error.wechat_running, False)
        self.assertEqual(len(self.calls), 1)

    def test_multiple_native_processes_are_rejected_without_lsof(self):
        self.probe_documents = [metadata(matching_applications=2)]
        error = self.assert_error('macos_active_process_ambiguous')
        self.assertIs(error.wechat_running, True)
        self.assertEqual(len(self.calls), 1)

    def test_metadata_failure_only_reports_validated_running_and_fixed_category(self):
        for changes, issue, running in (
                ({'version': 'unknown'}, 'client_version', True),
                ({'process_started_at': None}, 'process_started_at', True),
                ({'process_id': None}, 'process_id', True),
                ({'frontmost': None}, 'frontmost_state', True),
                ({'screen_state_available': None}, 'screen_state', True),
                ({'bundle_id': 'SYNTHETIC_PRIVATE_BUNDLE'}, 'application_state', None),
                ({'matching_applications': True}, 'application_count', None)):
            with self.subTest(issue=issue):
                self.calls = []
                self.probe_documents = [metadata(**changes)]
                error = self.assert_error('macos_active_process_metadata_invalid')
                self.assertIs(error.wechat_running, running)
                self.assertEqual(error.metadata_issue, issue)
                self.assertEqual(len(self.calls), 1)

    def test_lsof_failure_retains_only_validated_running_fact(self):
        original = self.runner
        def runner(command, **options):
            if command[0] == '/usr/sbin/lsof':
                raise PermissionError('SYNTHETIC_PRIVATE_PATH')
            return original(command, **options)
        error = self.assert_error('macos_active_open_files_unavailable', runner=runner)
        self.assertIs(error.wechat_running, True)
        self.assertIsNone(error.metadata_issue)

    def test_no_regular_open_file_or_only_outside_fixed_scope_is_not_an_account(self):
        self.listing = ('p12345\nn' + str(self.first) + '\nn' + str(self.first / 'message') +
                        '\nn' + str(self.root / 'db_storage/message.db') + '\n').encode()
        self.assert_error('macos_active_account_not_identified')

    def test_source_root_must_occupy_fixed_account_db_storage_position(self):
        invalid = self.base / 'wxid_SYNTHETIC_ACTIVE/extra/db_storage/message.db'
        invalid.parent.mkdir(parents=True)
        invalid.write_bytes(b'SYNTHETIC_UNRELATED_FILE')
        self.listing = ('p12345\nn' + str(invalid) + '\n').encode()
        self.assert_error('macos_active_account_not_identified')

    def test_missing_or_deleted_open_file_never_falls_back_to_inactive_root(self):
        self.listing = ('p12345\nn' + str(self.first / 'message/message_0.db') + ' (deleted)\n').encode()
        self.assert_error('macos_active_path_unavailable')

    def test_process_fields_must_match_the_probed_pid(self):
        for prefix in ('p99999\n', '', 'p12345\np99999\n', 'p012345\n'):
            with self.subTest(prefix=prefix):
                self.probe_documents = [metadata(), metadata()]
                self.listing = (prefix + 'n' + str(self.first / 'message/message_0.db') + '\n').encode()
                self.assert_error('macos_active_open_file_process_mismatch')

    def test_process_id_start_time_or_version_change_during_lsof_rejected(self):
        for changes in ({'process_id': 54321}, {'process_started_at': 1700000001.125}, {'version': '4.1.16'}):
            with self.subTest(changes=changes):
                self.probe_documents = [metadata(), metadata(**changes)]
                self.assert_error('macos_active_process_changed')

    def test_multiple_missing_or_untrusted_process_metadata_rejected_before_lsof(self):
        cases = [dict(running=False), dict(matching_applications=True),
                 dict(process_id=True), dict(process_id=0), dict(process_id=2**31),
                 dict(process_started_at=True), dict(process_started_at=float('nan')),
                 dict(process_started_at=float('inf')), dict(process_started_at=-1),
                 dict(version='unknown'), dict(version=''), dict(bundle_id='untrusted.bundle'),
                 dict(frontmost=None), dict(screen_state_available=None), dict(screen_locked=None)]
        for changes in cases:
            with self.subTest(changes=changes):
                self.calls = []
                self.probe_documents = [metadata(**changes)]
                self.assert_error('macos_active_process_metadata_invalid')
                self.assertEqual(len(self.calls), 1)
        document = metadata(); del document['process_id']; document['pid'] = 12345
        self.probe_documents = [document]
        self.assert_error('macos_active_process_metadata_invalid')

    def test_frontmost_locked_or_unavailable_screen_state_does_not_guess_capture_readiness(self):
        # Detection exposes identity only. The collector separately gates reads
        # on reliable unlocked/background samples and must not infer that here.
        self.probe_documents = [metadata(screen_state_available=False, screen_locked=True),
                                metadata(screen_state_available=False, screen_locked=True)]
        self.assertEqual(self.detect().db_root, self.first)

    def test_duplicate_json_keys_malformed_or_oversized_probe_output_rejected(self):
        samples = [b'{"process_id":12345,"process_id":12345}', b'{', b'\xff',
                   b' ' * (active.MAX_PROBE_BYTES + 1)]
        for raw in samples:
            with self.subTest(size=len(raw)):
                self.probe_documents = [raw]
                self.assert_error()

    def test_lsof_failure_timeout_invalid_utf8_and_size_limits_are_sanitized(self):
        for issue in ('nonzero', 'timeout', 'encoding', 'size', 'line_count'):
            self.probe_documents = [metadata(), metadata()]
            original = self.runner
            def runner(command, **options):
                if command[0] != '/usr/sbin/lsof':
                    return original(command, **options)
                if issue == 'timeout':
                    raise subprocess.TimeoutExpired('SYNTHETIC_PRIVATE_PATH', 5)
                payload = {'encoding': b'\xff', 'size': b'x' * (active.MAX_LSOF_BYTES + 1),
                           'line_count': b'p12345\n' + b'n/dev/null\n' * 16385}.get(issue, b'SYNTHETIC_PRIVATE_PATH')
                return subprocess.CompletedProcess(command, 1 if issue == 'nonzero' else 0, stdout=payload)
            with self.subTest(issue=issue):
                self.assert_error(runner=runner)

    def test_traversal_control_characters_and_invalid_account_names_rejected(self):
        samples = [str(self.first / 'message') + '/../message/message_0.db',
                   str(self.first / 'message/message_0.db') + '\r',
                   str(self.base / 'invalid account/db_storage/message.db')]
        for name in samples:
            with self.subTest(sample_kind=samples.index(name)):
                self.probe_documents = [metadata(), metadata()]
                self.listing = ('p12345\nn' + name + '\n').encode()
                self.assert_error('macos_active_open_file_path_invalid')

    @unittest.skipIf(os.name == 'nt', 'POSIX link boundary')
    def test_symlink_account_and_file_and_hardlinked_database_rejected(self):
        redirected = self.base / 'wxid_SYNTHETIC_LINK'
        redirected.symlink_to(self.first.parent, target_is_directory=True)
        self.listing = ('p12345\nn' + str(redirected / 'db_storage/message/message_0.db') + '\n').encode()
        self.assert_error('macos_active_path_redirected')
        redirected.unlink()
        real = self.first / 'message/message_0.db'
        link = self.first / 'message/synthetic-link.db'
        link.symlink_to(real)
        self.probe_documents = [metadata(), metadata()]
        self.listing = ('p12345\nn' + str(link) + '\n').encode()
        self.assert_error('macos_active_path_redirected')
        link.unlink()
        os.link(real, link)
        self.probe_documents = [metadata(), metadata()]
        self.listing = ('p12345\nn' + str(real) + '\n').encode()
        self.assert_error('macos_active_file_unsafe')

    def test_nonlocal_volume_or_mount_inspection_failure_never_returns_account(self):
        with mock.patch.object(active, '_local_volume', return_value=False):
            with self.assertRaisesRegex(active.ActiveAccountError, 'macos_active_local_volume_required'):
                active.detect_active_account(self.probe, runner=self.runner, home=self.home)
        self.probe_documents = [metadata(), metadata()]
        with mock.patch.object(active, '_local_volume', side_effect=active.ActiveAccountError('macos_active_mount_metadata_unavailable')):
            with self.assertRaisesRegex(active.ActiveAccountError, 'macos_active_mount_metadata_unavailable'):
                active.detect_active_account(self.probe, runner=self.runner, home=self.home)

    def test_account_root_replacement_after_lsof_is_detected(self):
        def replace_root():
            self.first.rename(self.first.with_name('synthetic-displaced-store'))
            self.first.mkdir()
            return metadata()
        self.probe_documents = [metadata(), replace_root]
        self.assert_error('macos_active_account_root_changed')

    def test_probe_path_must_be_safe_regular_file(self):
        absent = self.root / 'absent-native-probe'
        with self.assertRaisesRegex(active.ActiveAccountError, 'macos_active_path_unavailable'):
            active.detect_active_account(absent, runner=self.runner, home=self.home)
        self.assertEqual(self.calls, [])

    def test_native_volume_helper_rejects_non_macos_instead_of_guessing_local(self):
        with mock.patch.object(active.sys, 'platform', 'synthetic-not-darwin'):
            with self.assertRaisesRegex(active.ActiveAccountError, 'macos_active_native_platform_required'):
                active._local_volume(self.root)


if __name__ == '__main__':
    unittest.main()
