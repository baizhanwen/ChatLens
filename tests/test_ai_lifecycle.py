"""Synthetic shutdown races; no real account, credentials, provider or source DB."""
from contextlib import contextmanager
import hashlib
import http.client
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
for directory in (TESTS, SCRIPTS, SCRIPTS / 'windows-proof'):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
import ai_service
from ai_web_server import attach_ai_routes
from web_server import make_server
import test_macos_web_manager as mac_fixtures
import test_web_runtime as runtime_fixtures
import test_ai_service as service_fixtures


class FakeAssistant:
    model_request_attempts = cloud_request_attempts = 0

    def __init__(self, *_):
        self.closed = False

    def status(self):
        return {'synthetic_only': True, 'closed': self.closed}

    def close(self):
        self.closed = True


class ObservedLock:
    """Observe the runtime owner's close attempt without changing lock behavior."""
    def __init__(self, lock, owner, attempted):
        self.lock, self.owner, self.attempted = lock, owner, attempted

    def __enter__(self):
        if threading.get_ident() == self.owner:
            self.attempted.set()
        return self.lock.__enter__()

    def __exit__(self, *arguments):
        return self.lock.__exit__(*arguments)


class AiLifecycleTests(unittest.TestCase):
    def mac(self):
        fixture = mac_fixtures.MacWebManagerTests(methodName='test_start_only_identifies_current_account_and_never_collects_without_scope')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.ready_to_select()
        fixture.manager.provider = SimpleNamespace(data_dir=fixture.manager.data_dir)
        return fixture

    def windows(self):
        fixture = runtime_fixtures.WebRuntimeTests(methodName='test_read_only_http_reads_saved_message_without_keys_source_or_baseline_changes')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def service(self):
        fixture = service_fixtures.AiAssistantTests(methodName='test_empty_evidence_finishes_as_system_result_without_model_call')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    @contextmanager
    def serving(self, server):
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        worker.start()
        try:
            yield
        finally:
            server.shutdown(); worker.join(timeout=2); server.server_close()

    def status_request(self, server):
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            headers = {'Cookie': 'chatlens_session=' + server.token}
            if callable(getattr(server.data, 'public_scope_context', None)):
                headers['X-ChatLens-Scope'] = server.data.public_scope_context()['scope_generation']
            connection.request('GET', '/api/ai/status', headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_mac_close_first_prevents_initial_assistant_construction_through_http(self):
        fixture = self.mac()
        static = fixture.project / 'synthetic-static'
        static.mkdir()
        server = make_server(fixture.manager, static)
        attach_ai_routes(server, fixture.manager.ai_service)
        constructor = mock.Mock(side_effect=AssertionError('closing manager cannot create an AI service'))
        with mock.patch.object(ai_service, 'AiAssistant', constructor), self.serving(server):
            fixture.manager.close()
            status, document = self.status_request(server)
        self.assertEqual(status, 503)
        self.assertEqual(document['error']['code'], 'ai_service_stopped')
        constructor.assert_not_called()
        self.assertIsNone(fixture.manager.assistant)

    def test_mac_close_waits_for_existing_construction_then_closes_its_result(self):
        fixture = self.mac()
        entered, release, closing, closed = (threading.Event() for _ in range(4))
        instances, failures = [], []
        class Blocked(FakeAssistant):
            def __init__(self, *arguments):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('SYNTHETIC_CONSTRUCTOR_GATE_TIMEOUT')
                super().__init__(*arguments)
                instances.append(self)
        def obtain():
            try:
                fixture.manager.ai_service()
            except Exception as error:
                failures.append(type(error).__name__)
        def close():
            closing.set()
            fixture.manager.close()
            closed.set()
        with mock.patch.object(ai_service, 'AiAssistant', Blocked):
            constructor = threading.Thread(target=obtain, daemon=True)
            closer = threading.Thread(target=close, daemon=True)
            constructor.start()
            try:
                self.assertTrue(entered.wait(1))
                closer.start()
                self.assertTrue(closing.wait(1))
                self.assertFalse(closed.wait(.05))
                release.set()
                constructor.join(timeout=1); closer.join(timeout=1)
                self.assertTrue(closed.is_set())
                self.assertEqual(failures, [])
                self.assertEqual(len(instances), 1)
                self.assertTrue(instances[0].closed)
                with self.assertRaises(ai_service.AiError) as caught:
                    fixture.manager.ai_service()
                self.assertEqual(caught.exception.code, 'ai_service_stopped')
            finally:
                release.set()
                constructor.join(timeout=2)
                if closer.ident is not None:
                    closer.join(timeout=2)

    def test_windows_closing_flag_blocks_initial_construction_via_real_ai_route(self):
        fixture = self.windows()
        data = SimpleNamespace(data_dir=fixture.data, private=fixture.private)
        server = make_server(data, fixture.static)
        runtime_fixtures.runtime.enable_assistant(server, data)
        constructor = mock.Mock(side_effect=AssertionError('closing runtime cannot create an AI service'))
        with mock.patch.object(ai_service, 'AiAssistant', constructor), self.serving(server):
            with server.assistant_lock:
                server.assistant_closing = True
            status, document = self.status_request(server)
        self.assertEqual(status, 503)
        self.assertEqual(document['error']['code'], 'ai_service_stopped')
        constructor.assert_not_called()

    def test_windows_runtime_closes_assistant_that_was_constructing_during_shutdown(self):
        fixture = self.windows()
        entered, release, closing_attempt = threading.Event(), threading.Event(), threading.Event()
        instances, requests, helpers = [], [], []
        owner = threading.get_ident()
        class Blocked(FakeAssistant):
            def __init__(self, *arguments):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('SYNTHETIC_CONSTRUCTOR_GATE_TIMEOUT')
                super().__init__(*arguments)
                instances.append(self)
        def wait(shutdown):
            server = fixture.servers[0]
            server.assistant_lock = ObservedLock(server.assistant_lock, owner, closing_attempt)
            def request():
                try:
                    requests.append(self.status_request(server))
                except OSError:
                    # The runtime may close HTTP after it has closed the newly
                    # created service. It must still close that service.
                    pass
            requester = threading.Thread(target=request, daemon=True)
            requester.start(); helpers.append(requester)
            self.assertTrue(entered.wait(1))
            def unblock():
                closing_attempt.wait(1)
                release.set()
            releaser = threading.Thread(target=unblock, daemon=True)
            releaser.start(); helpers.append(releaser)
            shutdown.set()
        try:
            with mock.patch.object(ai_service, 'AiAssistant', Blocked):
                result = fixture.run_local(wait=wait)
            for helper in helpers:
                helper.join(timeout=2)
            self.assertTrue(closing_attempt.is_set())
            self.assertEqual(len(instances), 1)
            self.assertTrue(instances[0].closed)
            self.assertTrue(fixture.servers[0].assistant_closing)
            self.assertTrue(result['server_closed'])
            self.assertEqual(result['model_request_attempts'], 0)
        finally:
            release.set()
            for helper in helpers:
                helper.join(timeout=2)

    def test_service_close_rejects_new_mutations_and_drops_late_provider_answer(self):
        fixture = self.service()
        fixture.add('合成客户约定周五交付。')
        fixture.provider.release.clear()
        launched = fixture.assistant.ask(fixture.question())
        worker = fixture.assistant.jobs[launched['job_id']]['worker']
        self.assertTrue(fixture.provider.entered.wait(1))
        configuration = fixture.ai_dir / 'provider.json'
        before = configuration.read_bytes()
        try:
            fixture.assistant.close()
            actions = [fixture.assistant.new_thread,
                       lambda: fixture.assistant.ask(fixture.question()),
                       lambda: fixture.assistant.test({'target_fingerprint': fixture.assistant.config()['target_fingerprint']}),
                       lambda: fixture.assistant.update_config({'base_url': 'https://another.invalid/v1', 'model': 'synthetic'})]
            for action in actions:
                with self.assertRaises(ai_service.AiError) as caught:
                    action()
                self.assertEqual(caught.exception.code, 'ai_service_stopped')
            self.assertEqual(configuration.read_bytes(), before)
            fixture.provider.release.set()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            result = fixture.assistant.get_job(launched['job_id'])
            self.assertEqual(result['stage'], 'cancelled')
            history = fixture.assistant.get_thread(launched['thread_id'])
            self.assertFalse(any(row['role'] == 'assistant' for row in history['messages']))
            public = json.dumps([result, history], ensure_ascii=False)
            self.assertNotIn(service_fixtures.SYNTHETIC_KEY, public)
        finally:
            fixture.provider.release.set(); worker.join(timeout=2)

    def test_ai_question_and_shutdown_leave_original_collector_bytes_and_checkpoint_unchanged(self):
        fixture = self.service()
        fixture.add('合成客户要求确认交付日期。')
        checkpoint = fixture.store.snapshot()
        database = fixture.store_dir / 'collector.sqlite3'
        digest = hashlib.sha256(database.read_bytes()).hexdigest()
        result = fixture.ask_done()
        self.assertEqual(result['stage'], 'done')
        fixture.assistant.close()
        self.assertEqual(fixture.store.snapshot(), checkpoint)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), digest)
        self.assertEqual(fixture.provider.calls[0]['kind'], 'chat')


if __name__ == '__main__':
    unittest.main()
