"""Private initializer state machine with fake children and synthetic pages."""
import contextlib
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import plistlib
import signal
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import macos_capture_keys as bridge
import macos_reader_setup as setup
import prepare_wechat_read_test as preparation
from collector_private import read_private_json, write_private_json_new


def synthetic_page(key, number):
    page = bytearray(4096)
    page[:16] = bytes([number]) * 16
    page[16:64] = b'SYNTHETIC_ENCRYPTED_PAGE_HEADER_ONLY'.ljust(48, b'.')
    salt = bytes(value ^ 0x3a for value in page[:16])
    authentication_key = hashlib.pbkdf2_hmac('sha512', bytes.fromhex(key), salt, 2, 32)
    page[4032:] = hmac.new(authentication_key, page[16:4032] + (1).to_bytes(4, 'little'), hashlib.sha512).digest()
    return page


class FakeChild:
    def __init__(self, number):
        self.pid, self.returncode, self.waits = 70000 + number, None, []
        self.timeouts = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.timeouts:
            self.timeouts -= 1
            raise subprocess.TimeoutExpired('synthetic-owned-helper', timeout)
        self.returncode = 0
        return 0


class ReaderInitializerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-reader-initializer-')
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'project'
        self.app = self.root / 'Synthetic-WeChat.app'
        self.app.mkdir()
        self.python = self.project / '.build/wechat-read-venv/bin/python'
        for path in (self.python, self.project / 'scripts/prepare_wechat_read_test.py',
                     self.project / 'scripts/macos_capture_keys.py'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'SYNTHETIC_HELPER_NEVER_EXECUTED')
        self.account_root = self.root / 'synthetic-account/db_storage'
        self.account_root.mkdir(parents=True)
        self.keys = {'message/message_0.db': '45' * 32, 'message/message_1.db': '57' * 32,
                     'contact/contact.db': '82' * 32}
        for number, (relative, key) in enumerate(self.keys.items(), 1):
            path = self.account_root / relative
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(synthetic_page(key, number))
        self.account = SimpleNamespace(db_root=self.account_root, pid=43210, started_at=1800000000.0, client_version='4.1.0')
        self.private = self.root / 'private-account'
        self.calls, self.children, self.signals, self.probes = [], [], [], []
        self.tick, self.pgrep = 100, 0
        self.system_patch = patch.object(setup, 'SYSTEM_PYTHON', self.python)
        self.system_patch.start()
        self.addCleanup(self.system_patch.stop)
        self.initializer = setup.ReaderInitializer(self.account, self.private, project_root=self.project,
            app_path=self.app, process_factory=self.factory, runner=self.probe, clock=lambda: self.tick,
            signal_group=lambda pid, signum: self.signals.append((pid, signum)))

    def tearDown(self):
        self.initializer.close()
        self.tmp.cleanup()

    def factory(self, command, **options):
        self.calls.append((command, options))
        child = FakeChild(len(self.calls))
        self.children.append(child)
        return child

    def probe(self, command, **options):
        self.probes.append((command, options))
        return SimpleNamespace(returncode=self.pgrep)

    def prepared(self):
        self.initializer.start()
        self.children[0].returncode = 0
        self.initializer.step()

    def capturing(self):
        self.prepared()
        self.pgrep = 1
        self.initializer.step()

    def key_document(self, values=None):
        document = {'keys': {relative: {'enc_key': key} for relative, key in (values or self.keys).items()}}
        target = self.initializer._state_dir / 'keys.json'
        write_private_json_new(target, document)
        return target

    def test_prepare_starts_one_fixed_private_helper_without_quitting_original(self):
        self.initializer.start()
        self.initializer.start()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.initializer.state, 'working')
        self.assertEqual(self.initializer.action, 'none')
        self.assertEqual(self.initializer.phase, 'preparing_copy')
        self.assertIsNone(self.initializer.key_file)
        command, options = self.calls[0]
        self.assertEqual(command[:3], [str(self.python), str(self.project / 'scripts/prepare_wechat_read_test.py'), 'prepare'])
        self.assertIn('message_and_contact', command)
        self.assertEqual(command[command.index('--db-root') + 1], str(self.account_root))
        self.assertEqual(command[command.index('--app') + 1], str(self.app))
        self.assertEqual(options['stdout'], subprocess.DEVNULL)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertTrue(options['start_new_session'])
        self.assertFalse(options['shell'])
        self.assertEqual(self.signals, [])
        self.assertEqual(self.probes, [])
        self.tick += 12
        self.assertEqual(self.initializer.elapsed_seconds, 12)
        state_file = self.initializer._state_dir / 'initializer-status.json'
        self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)
        state = read_private_json(state_file)
        self.assertEqual(state['state'], 'working')
        self.assertNotIn(str(self.account_root), json.dumps(state))
        self.assertTrue(all(key not in json.dumps(state) for key in self.keys.values()))

    def test_prepare_then_explicit_original_exit_then_private_capture_then_ready(self):
        self.prepared()
        self.assertEqual(self.initializer.state, 'needs_user_action')
        self.assertEqual(self.initializer.action, 'exit_wechat')
        self.initializer.step()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.probes[0][0], ['/usr/bin/pgrep', '-x', 'WeChat'])
        self.assertEqual(self.signals, [])
        self.pgrep = 1
        self.initializer.step()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1][0][:3], [str(self.python), str(self.project / 'scripts/macos_capture_keys.py'), 'capture'])
        self.assertEqual(self.initializer.action, 'confirm_login')
        self.assertEqual(self.initializer.phase, 'waiting_for_login')
        self.assertIsNone(self.initializer.key_file)
        target = self.key_document()
        self.children[1].returncode = 0
        self.initializer.step()
        self.assertEqual(self.initializer.state, 'ready')
        self.assertEqual(self.initializer.action, 'none')
        self.assertEqual(self.initializer.key_file, target)
        self.initializer.close()
        self.assertEqual(self.initializer.key_file, target)
        self.assertEqual(self.signals, [])

    def test_zero_capture_exit_is_insufficient_with_missing_or_invalid_keys(self):
        self.capturing()
        values = dict(self.keys)
        values.pop('contact/contact.db')
        self.key_document(values)
        self.children[1].returncode = 0
        self.initializer.step()
        self.assertEqual(self.initializer.state, 'error')
        self.assertEqual(self.initializer.reason, 'macos_reader_key_validation_failed')
        self.assertIsNone(self.initializer.key_file)

    def test_wrong_key_hmac_never_becomes_ready(self):
        self.capturing()
        values = dict(self.keys)
        values['message/message_1.db'] = '00' * 32
        self.key_document(values)
        self.children[1].returncode = 0
        self.initializer.step()
        self.assertEqual(self.initializer.reason, 'macos_reader_key_validation_failed')
        self.assertIsNone(self.initializer.key_file)

    def test_prepare_failure_uses_only_fixed_public_reason(self):
        self.initializer.start()
        self.children[0].returncode = 2
        self.initializer.step()
        self.assertEqual(self.initializer.reason, 'macos_reader_prepare_failed')
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.initializer.key_file)

    def test_capture_failure_is_not_mistaken_for_success_even_if_partial_keys_exist(self):
        self.capturing()
        self.key_document()
        self.children[1].returncode = 2
        self.initializer.step()
        self.assertEqual(self.initializer.state, 'error')
        self.assertEqual(self.initializer.reason, 'macos_reader_capture_failed')
        self.assertIsNone(self.initializer.key_file)

    def test_unknown_original_process_state_cannot_start_capture(self):
        self.prepared()
        self.pgrep = 2
        self.initializer.step()
        self.assertEqual(self.initializer.state, 'error')
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.initializer.key_file)

    def test_cancel_targets_only_owned_group_and_escalates_after_bounded_waits(self):
        self.capturing()
        child = self.children[1]
        child.timeouts = 2
        self.initializer.close()
        self.assertEqual(self.signals, [(child.pid, signal.SIGINT), (child.pid, signal.SIGTERM), (child.pid, signal.SIGKILL)])
        self.assertEqual(child.waits, [10, 3, 2])
        self.assertNotIn(self.account.pid, [pid for pid, _ in self.signals])
        self.assertEqual(self.initializer.reason, 'macos_reader_initialization_cancelled')
        self.assertIsNone(self.initializer.key_file)

    def test_unconfirmed_cleanup_is_not_reported_as_success(self):
        self.initializer.start()
        self.children[0].timeouts = 3
        self.initializer.close()
        self.assertEqual(self.initializer.reason, 'macos_reader_owned_helper_cleanup_unconfirmed')

    def test_deadline_stops_own_child(self):
        self.initializer.start()
        self.tick += 601
        self.initializer.step()
        self.assertEqual(self.initializer.reason, 'macos_reader_helper_deadline')
        self.assertEqual(self.signals[0], (self.children[0].pid, signal.SIGINT))

    def test_root_replacement_refuses_capture_without_loading_another_account(self):
        self.prepared()
        held = self.account_root.with_name('synthetic-held-db-storage')
        self.account_root.rename(held)
        self.account_root.mkdir()
        self.pgrep = 1
        self.initializer.step()
        self.assertEqual(self.initializer.state, 'error')
        self.assertEqual(self.initializer.reason, 'macos_reader_initialization_failed')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.probes, [])
        self.assertIsNone(self.initializer.key_file)

    def test_existing_initialization_is_preserved_and_private_paths_are_unique(self):
        self.private.mkdir(mode=0o700)
        old = self.private / 'reader-init-existing'
        old.mkdir(mode=0o700)
        write_private_json_new(old / 'keys.json', {'keys': {'SYNTHETIC_OLD': 'MUST_REMAIN'}})
        before = (old / 'keys.json').read_bytes()
        self.initializer.start()
        self.assertNotEqual(self.initializer._state_dir, old)
        self.assertEqual((old / 'keys.json').read_bytes(), before)

    def dispatcher_fixture(self):
        dispatcher = self.root / 'synthetic-system-dispatcher'
        dispatcher.write_bytes(b'SYNTHETIC_APPLE_DISPATCHER_NOT_EXECUTED')
        dispatcher.chmod(0o755)
        os.link(dispatcher, self.root / 'synthetic-dispatcher-hardlink')
        info = list(dispatcher.lstat())
        self.assertGreater(info[3], 1)
        info[4] = 0  # Simulate the fixed system binary's root ownership only.
        return dispatcher, os.stat_result(info)

    def dispatcher_patches(self, info, *, executable=True):
        fixed = Path('/usr/bin/python3')
        original_lstat, original_access = Path.lstat, os.access
        original_validate = setup.validate_local_path
        def lstat(path, *arguments, **options):
            return info if path == fixed else original_lstat(path, *arguments, **options)
        def access(path, mode, *arguments, **options):
            return executable if Path(path) == fixed else original_access(path, mode, *arguments, **options)
        def validate(path, require_dir=False):
            # All state/helper paths retain their real temporary-file checks.
            # The fixed system parent's metadata is entirely synthetic here.
            return path if Path(path) == fixed.parent and require_dir else original_validate(path, require_dir)
        return (patch.object(setup, 'SYSTEM_PYTHON', fixed), patch.object(Path, 'lstat', lstat),
                patch.object(setup.os, 'access', access), patch.object(setup, 'validate_local_path', validate))

    def test_fixed_root_owned_hardlinked_system_dispatcher_can_start_capture(self):
        self.prepared()
        _, info = self.dispatcher_fixture()
        self.pgrep = 1
        with contextlib.ExitStack() as stack:
            for boundary in self.dispatcher_patches(info):
                stack.enter_context(boundary)
            self.initializer.step()
        self.assertEqual(self.initializer.state, 'needs_user_action')
        self.assertEqual(self.initializer.action, 'confirm_login')
        self.assertEqual(self.calls[1][0][0], '/usr/bin/python3')
        self.assertEqual(len(self.calls), 2)
        self.assertIsNone(self.initializer.key_file)

    def test_system_dispatcher_exception_preserves_owner_mode_and_private_hardlink_rules(self):
        self.prepared()
        dispatcher, good = self.dispatcher_fixture()
        for change, executable in [({'owner': 1234}, True), ({'mode': stat.S_IFREG | 0o775}, True),
                                   ({'mode': stat.S_IFLNK | 0o755}, True), ({}, False)]:
            with self.subTest(change=change, executable=executable):
                values = list(good)
                values[4] = change.get('owner', values[4])
                values[0] = change.get('mode', values[0])
                with contextlib.ExitStack() as stack:
                    for boundary in self.dispatcher_patches(os.stat_result(values), executable=executable):
                        stack.enter_context(boundary)
                    with self.assertRaisesRegex(ValueError, 'macos_reader_system_python_unsafe'):
                        self.initializer._launch('capture')
        self.assertEqual(len(self.calls), 1)
        # The exception is scoped to the fixed system executable; ordinary
        # interpreter/helper/private paths must still reject hard links.
        with patch.object(setup, 'SYSTEM_PYTHON', dispatcher):
            with self.assertRaises(Exception):
                self.initializer._launch('capture')
        self.assertEqual(len(self.calls), 1)


class CurrentApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-current-application-')
        self.root = Path(self.tmp.name).resolve()
        self.current = self.make_app(self.root / 'Desktop/Current WeChat.app', '4.1.0')
        self.installed = self.make_app(self.root / 'Applications/WeChat.app', '4.2.0')
        self.account = SimpleNamespace(pid=98765, client_version='4.1.0')
        self.output = (str(self.current / 'Contents/MacOS/WeChat') + '\n').encode()
        self.returncode, self.calls = 0, []

    def tearDown(self):
        self.tmp.cleanup()

    def make_app(self, app, version, bundle='com.tencent.xinWeChat'):
        executable = app / 'Contents/MacOS/WeChat'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'SYNTHETIC_WECHAT_NEVER_EXECUTED')
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(
            {'CFBundleIdentifier': bundle, 'CFBundleShortVersionString': version}))
        return app

    def fake_ps(self, command, **options):
        self.calls.append((command, options))
        return SimpleNamespace(returncode=self.returncode, stdout=self.output)

    def test_exact_detected_process_selects_its_app_instead_of_other_installed_version(self):
        before = (self.installed / 'Contents/Info.plist').read_bytes()
        self.assertEqual(setup.current_application(self.account, run=self.fake_ps), self.current)
        command, options = self.calls[0]
        self.assertEqual(command, ['/bin/ps', '-p', '98765', '-o', 'comm='])
        self.assertEqual(options['timeout'], 3)
        self.assertFalse(options['shell'])
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertEqual((self.installed / 'Contents/Info.plist').read_bytes(), before)

    def test_bundle_or_version_mismatch_never_falls_back_to_an_installed_copy(self):
        target = self.current / 'Contents/Info.plist'
        for bundle, version in [('com.tencent.xinWeChat', '4.2.0'), ('synthetic.other.app', '4.1.0')]:
            target.write_bytes(plistlib.dumps({'CFBundleIdentifier': bundle, 'CFBundleShortVersionString': version}))
            with self.assertRaisesRegex(ValueError, '^macos_running_application_version_mismatch$'):
                setup.current_application(self.account, run=self.fake_ps)
        self.assertEqual(len(self.calls), 2)

    def test_nonzero_ps_invalid_path_multiple_results_and_redirects_fail_closed(self):
        self.returncode = 1
        with self.assertRaisesRegex(ValueError, '^macos_running_application_unavailable$'):
            setup.current_application(self.account, run=self.fake_ps)
        self.returncode = 0
        for output in (b'', b'/not/the/application\n', b'x' * 4097,
                       self.output + str(self.installed / 'Contents/MacOS/WeChat').encode(),
                       b'relative/WeChat.app/Contents/MacOS/WeChat'):
            self.output = output
            with self.assertRaises(Exception):
                setup.current_application(self.account, run=self.fake_ps)
        executable = self.current / 'Contents/MacOS/WeChat'
        executable.unlink()
        executable.symlink_to(self.installed / 'Contents/MacOS/WeChat')
        self.output = str(executable).encode()
        with self.assertRaises(Exception):
            setup.current_application(self.account, run=self.fake_ps)


class CaptureBridgeTests(unittest.TestCase):
    def test_lldb_discovery_is_fixed_bounded_and_wrapper_calls_capture_only(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-lldb-path-') as directory:
            path = Path(directory).resolve()
            calls = []
            def runner(command, **options):
                calls.append((command, options))
                return SimpleNamespace(returncode=0, stdout=(str(path) + '\n').encode())
            self.assertEqual(bridge.lldb_python_path(runner), str(path))
            self.assertEqual(calls[0][0], ['/usr/bin/xcrun', 'lldb', '-P'])
            self.assertEqual(calls[0][1]['stderr'], subprocess.DEVNULL)
            with patch.object(sys, 'argv', ['macos_capture_keys.py', 'capture']), patch.object(sys, 'path', list(sys.path)), \
                    patch.object(bridge, 'lldb_python_path', return_value=str(path)), \
                    patch.object(preparation, 'main', return_value=0) as capture:
                self.assertEqual(bridge.main(), 0)
                capture.assert_called_once_with()
            with patch.object(sys, 'argv', ['macos_capture_keys.py', 'prepare']), \
                    patch.object(preparation, 'main') as capture:
                self.assertEqual(bridge.main(), 2)
                capture.assert_not_called()

    def test_lldb_error_details_are_not_printed(self):
        stdout = io.StringIO()
        with patch.object(sys, 'argv', ['macos_capture_keys.py', 'capture']), \
                patch.object(bridge, 'lldb_python_path', side_effect=RuntimeError('SYNTHETIC_PRIVATE_KEY_PATH')), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(bridge.main(), 2)
        self.assertEqual(stdout.getvalue(), 'reason=macos_capture_wrapper_failed\n')


if __name__ == '__main__':
    unittest.main()
