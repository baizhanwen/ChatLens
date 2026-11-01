"""Synthetic launcher lifecycle; no real WeChat, credentials, or processes."""
from contextlib import redirect_stdout
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import collector_service
from collector_private import read_private_json, write_private_json_new
from continuous_store import CollectorStore
import web_data
import web_runtime as runtime
from web_server import make_server


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class SyntheticChild:
    def __init__(self, output, *, cooperative=True, returncode=None):
        self.output = Path(output)
        self.cooperative = cooperative
        self.returncode = returncode
        self.terminate = mock.Mock(side_effect=AssertionError('runtime must not terminate a supervisor'))
        self.kill = mock.Mock(side_effect=AssertionError('runtime must not kill a supervisor'))

    def poll(self):
        if self.returncode is None and self.cooperative and (self.output / 'supervisor-stop.request').exists():
            self.returncode = 0
        return self.returncode


class WebRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-web-runtime-')
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / 'collector-data'
        self.private = self.data / 'private'
        self.directory = self.private / 'store'
        for path in (self.data, self.private, self.directory):
            path.mkdir(mode=0o700)
        self.store = CollectorStore(self.directory, {'synthetic_runtime_only': True})
        self.store.initialize({}, {})
        self.body = 'SYNTHETIC_PRIVATE_BODY_NOT_FOR_PUBLIC_REPORT'
        self.name = 'SYNTHETIC_PRIVATE_CONTACT_NAME'
        self.store.commit({}, {}, [{
            'source_key': hashlib.sha256(b'synthetic-runtime-event').hexdigest(),
            'alias': 'private_test', 'source_local_id': 1, 'sent_at': 1700000001,
            'sender_id': 'SYNTHETIC_ACCOUNT_ID', 'body': self.body,
            'observed_at': '2026-11-01T00:00:00+00:00',
        }], {})
        write_private_json_new(self.private / 'catalog.json', {
            'schema_version': 1, 'binding_digest': self.store.binding_digest,
            'conversations': [
                {'id': 'private_test', 'name': self.name, 'kind': 'private',
                 'sender_names': {'SYNTHETIC_ACCOUNT_ID': self.name}},
                {'id': 'group_test', 'name': 'SYNTHETIC_GROUP_NAME', 'kind': 'group', 'sender_names': {}},
            ],
        })
        self.package = self.root / 'synthetic-portable-package'
        self.static = self.package / 'web'
        self.static.mkdir(parents=True)
        (self.static / 'index.html').write_bytes(b'<!doctype html><title>Synthetic ChatLens</title>')
        self.output = self.root / 'synthetic-web-result'
        self.servers, self.children, self.launches = [], [], []
        self.stdout = io.StringIO()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def server_factory(self, data, static_dir, **options):
        server = make_server(data, static_dir, **options)
        self.servers.append(server)
        return server

    def child_factory(self, command, **options):
        output = Path(command[command.index('--output') + 1])
        output.mkdir()  # Simulate the independently owned supervisor.
        child = SyntheticChild(output)
        self.children.append(child)
        self.launches.append((command, options))
        return child

    def run_local(self, **options):
        arguments = dict(package=self.package, read_only=True, no_open=True,
                         wait=lambda event: event.set(), server_factory=self.server_factory)
        arguments.update(options)
        with redirect_stdout(self.stdout):
            return runtime.run(self.data, self.output, **arguments)

    def checkpoint(self):
        return tuple(self.store._connection.execute(
            'SELECT binding_digest,initialized,cursors,epochs,baseline_completed_at,last_commit_at,commits '
            'FROM checkpoint WHERE id=1').fetchone())

    def assert_redacted(self, result):
        saved = (self.output / 'result-to-send.json').read_text(encoding='utf-8')
        self.assertEqual(json.loads(saved), result)
        for value in (self.body, self.name, 'SYNTHETIC_GROUP_NAME', 'SYNTHETIC_ACCOUNT_ID',
                      'SYNTHETIC_KEYS_NEVER_READ', str(self.root), '#token=', 'chatlens_session'):
            self.assertNotIn(value, saved)
        for server in self.servers:
            self.assertNotIn(server.token, saved)
            self.assertEqual(server.socket.fileno(), -1)
        self.assertEqual(result['web_live_end_to_end_status'], '未验证')
        self.assertEqual(result['cloud_calls'], 0)
        self.assertEqual(result['messages_sent_by_tool'], 0)
        self.assertFalse(result['private_data_copied'])
        self.assertFalse(result['source_database_records_modified'])

    def test_read_only_http_reads_saved_message_without_keys_source_or_baseline_changes(self):
        source = self.root / 'SYNTHETIC_SOURCE_DB'
        source.mkdir()
        source_file = source / 'synthetic.db'
        source_file.write_bytes(b'SYNTHETIC_SOURCE_MUST_STAY_UNREAD')
        write_private_json_new(self.private / 'keys.json', {'keys': 'SYNTHETIC_KEYS_NEVER_READ'})
        write_private_json_new(self.private / 'scope.json', {'db_root': str(source), 'synthetic_only': True})
        before = self.checkpoint()
        protected = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                     for path in (source_file, self.private / 'keys.json', self.private / 'scope.json')}
        metadata_reads = []
        def only_web_metadata(path, **options):
            self.assertIn(Path(path).name, ('catalog.json', 'web-status.json'))
            metadata_reads.append(Path(path).name)
            return read_private_json(path, **options)
        def inspect_http(shutdown):
            server = self.servers[0]
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            connection.request('GET', '/api/session', headers={'Authorization': 'Bearer ' + server.token})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            cookie = response.getheader('Set-Cookie').split(';', 1)[0]
            response.read(); connection.close()
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            connection.request('GET', '/api/messages?conversation=private_test', headers={'Cookie': cookie})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            document = json.loads(response.read())
            self.assertEqual([row['body'] for row in document['messages']], [self.body])
            self.assertEqual(document['messages'][0]['sender'], self.name)
            connection.close()
            shutdown.set()
        forbidden = mock.Mock(side_effect=AssertionError('readonly must not start or prepare collection'))
        with mock.patch.object(web_data, 'read_private_json', side_effect=only_web_metadata), \
                mock.patch.object(collector_service, 'prepare_session', forbidden), \
                mock.patch.object(runtime, 'owned_command', forbidden):
            result = self.run_local(wait=inspect_http, popen_factory=forbidden)
        forbidden.assert_not_called()
        self.assertIn('catalog.json', metadata_reads)
        self.assertEqual(self.checkpoint(), before)
        for path, (content, modified) in protected.items():
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mtime_ns, modified)
        self.assertEqual(result['status'], '通过')
        self.assertTrue(result['read_only'])
        self.assertFalse(result['collector_child_started'])
        self.assertTrue(result['server_closed'])
        self.assertFalse((self.output / 'collector-run').exists())
        self.assert_redacted(result)

    def test_collecting_launch_uses_fixed_argv_and_own_supervisor_stop_file(self):
        unrelated = self.root / 'unrelated-collector-run'
        unrelated.mkdir()
        sentinel = unrelated / 'supervisor-stop.request'
        sentinel.write_bytes(b'SYNTHETIC_EXISTING_UNRELATED_REQUEST')
        result = self.run_local(read_only=False, pid=12345, creation_time=123456789,
                                popen_factory=self.child_factory)
        command, options = self.launches[0]
        self.assertEqual(command, [str(self.package / 'runtime/python.exe'), '-I', '-S', '-B', '-X', 'utf8',
                                  str(self.package / 'collector_supervisor.py'), '--data-dir', str(self.data),
                                  '--output', str(self.output / 'collector-run'), '--pid', '12345',
                                  '--creation-time', '123456789', '--web-metadata'])
        self.assertFalse(options['shell'])
        self.assertEqual(options['cwd'], str(self.package))
        self.assertIsNone(options['stdin'])
        self.assertIsNone(options['stdout'])
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        if os.name == 'nt':
            self.assertEqual(options['creationflags'], subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertTrue(options['start_new_session'])
        own = self.output / 'collector-run'
        self.assertTrue((own / 'supervisor-stop.request').is_file())
        self.assertFalse((own / 'stop.request').exists())
        self.assertEqual(sentinel.read_bytes(), b'SYNTHETIC_EXISTING_UNRELATED_REQUEST')
        self.children[0].terminate.assert_not_called()
        self.children[0].kill.assert_not_called()
        self.assertTrue(result['collector_cleanup_confirmed'])
        self.assertEqual(result['collector_child_exit_code'], 0)
        self.assert_redacted(result)

    def test_ctrl_c_normally_closes_web_and_requests_own_collection_stop(self):
        def interrupt(_):
            raise KeyboardInterrupt
        result = self.run_local(read_only=False, pid=12, creation_time=34,
                                popen_factory=self.child_factory, wait=interrupt)
        self.assertEqual(result['reason'], 'local_web_normal_stop')
        self.assertTrue(result['server_closed'])
        self.assertTrue(result['collector_cleanup_confirmed'])
        self.assertEqual(result['collector_child_exit_code'], 0)
        self.assert_redacted(result)

    def test_existing_output_and_symlink_output_are_never_overwritten(self):
        self.output.mkdir()
        sentinel = self.output / 'result-to-send.json'
        sentinel.write_bytes(b'SYNTHETIC_EXISTING_REPORT')
        forbidden = mock.Mock(side_effect=AssertionError('must fail before any server or process starts'))
        with self.assertRaisesRegex(ValueError, 'web_output_already_exists'):
            self.run_local(server_factory=forbidden, popen_factory=forbidden)
        forbidden.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b'SYNTHETIC_EXISTING_REPORT')
        if os.name != 'nt':
            link = self.root / 'synthetic-output-link'
            link.symlink_to(self.output, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'web_output_already_exists'):
                runtime.run(self.data, link, package=self.package, read_only=True, server_factory=forbidden)
            self.assertEqual(sentinel.read_bytes(), b'SYNTHETIC_EXISTING_REPORT')

    def test_invalid_or_conflicting_process_identity_cannot_start_collection(self):
        cases = ({}, {'pid': 12}, {'creation_time': 34}, {'pid': True, 'creation_time': 34},
                 {'pid': 0, 'creation_time': 34}, {'pid': 12, 'creation_time': -1},
                 {'read_only': True, 'pid': 12}, {'read_only': True, 'creation_time': 34},
                 {'read_only': True, 'pid': 12, 'creation_time': 34})
        forbidden = mock.Mock(side_effect=AssertionError('invalid identity must not start'))
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                parameters = dict(options)
                self.run_local(read_only=parameters.pop('read_only', False),
                               server_factory=forbidden, popen_factory=forbidden, **parameters)
        forbidden.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_missing_store_does_not_create_data_or_initial_baseline(self):
        self.store.close()
        database = self.directory / 'collector.sqlite3'
        database.unlink()
        forbidden = mock.Mock(side_effect=AssertionError('missing state must not start'))
        with self.assertRaises(web_data.WebDataError):
            self.run_local(server_factory=forbidden, popen_factory=forbidden)
        forbidden.assert_not_called()
        self.assertFalse(database.exists())
        self.assertFalse(self.output.exists())

    def test_server_creation_failure_writes_redacted_failure_without_starting_child(self):
        def failed_server(*_, **__):
            raise OSError('SYNTHETIC_PRIVATE_PATH_TOKEN_AND_KEY')
        forbidden = mock.Mock(side_effect=AssertionError('no collector when HTTP setup fails'))
        result = self.run_local(server_factory=failed_server, popen_factory=forbidden)
        forbidden.assert_not_called()
        self.assertEqual(result['status'], '失败')
        self.assertFalse(result['server_started'])
        self.assertFalse(result['collector_child_started'])
        self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(result))
        self.assert_redacted(result)

    def test_thread_start_failure_closes_socket_without_shutdown_deadlock_or_join_error(self):
        with mock.patch.object(threading.Thread, 'start', side_effect=RuntimeError('SYNTHETIC_THREAD_START_FAILURE')):
            result = self.run_local()
        self.assertEqual(result['status'], '失败')
        self.assertFalse(result['server_started'])
        self.assertTrue(result['server_closed'])
        self.assert_redacted(result)

    def test_child_launch_failure_keeps_report_and_closes_http(self):
        def failed_child(*_, **__):
            raise OSError('SYNTHETIC_PRIVATE_EXECUTABLE_PATH')
        result = self.run_local(read_only=False, pid=12, creation_time=34, popen_factory=failed_child)
        self.assertEqual(result['status'], '失败')
        self.assertTrue(result['server_started'])
        self.assertTrue(result['server_closed'])
        self.assertFalse(result['collector_child_started'])
        self.assert_redacted(result)

    def test_unexpected_server_worker_exit_is_failure_without_shutdown_deadlock(self):
        worker_threads = []
        original_thread = threading.Thread
        closed = []
        class EarlyExitServer:
            base_url = 'http://127.0.0.1:1'
            launch_url = base_url + '/#token=SYNTHETIC_SESSION_TOKEN'
            def serve_forever(self, **_):
                return
            def shutdown(self):
                raise AssertionError('a dead worker must not enter shutdown wait')
            def server_close(self):
                closed.append(True)
        def threads(*arguments, **options):
            worker = original_thread(*arguments, **options)
            worker_threads.append(worker)
            return worker
        def browser(_):
            worker_threads[0].join(timeout=2)
            self.assertFalse(worker_threads[0].is_alive())
            return True
        with mock.patch.object(runtime.threading, 'Thread', side_effect=threads):
            result = self.run_local(server_factory=lambda *_, **__: EarlyExitServer(),
                                    no_open=False, open_browser=browser)
        self.assertEqual(result['status'], '失败')
        self.assertTrue(result['server_closed'])
        self.assertEqual(closed, [True])
        self.assertNotIn('SYNTHETIC_SESSION_TOKEN', json.dumps(result))
        self.assert_redacted(result)

    def test_browser_fallback_url_is_local_console_only_not_report(self):
        opened = []
        def browser(url):
            opened.append(url)
            return False
        result = self.run_local(no_open=False, open_browser=browser)
        self.assertEqual(opened, [self.servers[0].launch_url])
        self.assertIn(self.servers[0].launch_url, self.stdout.getvalue())
        self.assertTrue(result['browser_open_requested'])
        self.assert_redacted(result)

    def test_failed_collector_keeps_saved_messages_available_until_user_closes_page(self):
        def finished_child(command, **options):
            child = self.child_factory(command, **options)
            child.returncode = 2
            return child
        observed = []
        def observe(shutdown):
            observed.append(self.servers[0].data.messages('private_test')['messages'][0]['body'])
            shutdown.set()
        result = self.run_local(read_only=False, pid=12, creation_time=34,
                                popen_factory=finished_child, wait=observe)
        self.assertEqual(observed, [self.body])
        self.assertEqual(result['collector_child_exit_code'], 2)
        self.assertEqual(result['status'], '失败')
        self.assertEqual(result['reason'], 'web_collector_child_failed')
        self.assertFalse((self.output / 'collector-run' / 'supervisor-stop.request').exists())
        self.assert_redacted(result)

    def test_unconfirmed_owned_cleanup_is_failure_and_never_kills_other_processes(self):
        with mock.patch.object(runtime, 'stop_collection', return_value=(None, False)) as stop:
            result = self.run_local(read_only=False, pid=12, creation_time=34,
                                    popen_factory=self.child_factory)
        stop.assert_called_once_with(self.children[0], self.output / 'collector-run')
        self.assertEqual(result['reason'], 'web_collector_stop_not_confirmed')
        self.assertEqual(result['status'], '失败')
        self.assertFalse(result['collector_cleanup_confirmed'])
        self.children[0].terminate.assert_not_called()
        self.children[0].kill.assert_not_called()
        self.assertTrue(result['server_closed'])
        self.assert_redacted(result)

    def test_owned_stop_waits_for_directory_and_has_bounded_non_killing_timeout(self):
        owned = self.root / 'synthetic-late-supervisor'
        child, clock = SyntheticChild(owned, cooperative=False), Clock()
        def progress(seconds):
            clock.sleep(seconds)
            if clock.value >= .2 and not owned.exists():
                owned.mkdir()
        before = runtime.signal.getsignal(runtime.signal.SIGINT)
        result = runtime.stop_collection(child, owned, grace=.5, clock=clock.now, sleep=progress)
        self.assertEqual(result, (None, False))
        self.assertLess(clock.value, .7)
        self.assertTrue((owned / 'supervisor-stop.request').is_file())
        self.assertFalse((owned / 'stop.request').exists())
        self.assertEqual(runtime.signal.getsignal(runtime.signal.SIGINT), before)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_owned_stop_does_not_touch_output_after_child_already_exited(self):
        absent = self.root / 'synthetic-no-longer-running'
        child = SyntheticChild(absent, returncode=7)
        self.assertEqual(runtime.stop_collection(child, absent), (7, True))
        self.assertFalse(absent.exists())
        self.assertEqual(runtime.stop_collection(None, absent), (None, True))


if __name__ == '__main__':
    unittest.main()
