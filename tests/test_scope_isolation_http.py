"""Real Mac manager + SQLite + HTTP scope guards over temporary synthetic data."""
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_multi_fixture import SyntheticRuntime


class ScopeHttpTests(unittest.TestCase):
    def setUp(self):
        self.runtime = SyntheticRuntime()
        self.assertEqual(self.runtime.request('GET', '/api/session', generation=None, bootstrap=True)[0], 200)

    def tearDown(self):
        self.runtime.close()

    def select(self, choices=None):
        self.runtime.populate()
        choices = choices or ['choice_000', 'choice_001', 'choice_002', 'choice_035', 'choice_036']
        old = self.runtime.manager.scope_generation
        code, value = self.runtime.request('POST', '/api/setup/select', {'conversation_ids': choices})
        self.assertEqual(code, 200, value)
        self.assertNotEqual(value['scope_generation'], old)
        self.runtime.tick()
        self.assertEqual(self.runtime.manager.stage, 'running')
        return self.runtime.manager.active_scope_id

    def configuration(self):
        code, config = self.runtime.request('POST', '/api/ai/config',
            {'base_url': 'http://127.0.0.1:9/v1', 'model': 'explicit-synthetic-model', 'key_action': 'delete'})
        self.assertEqual(code, 200, config)
        return config['target_fingerprint']

    def question(self, question='今天有多少条消息', **extra):
        aliases = [row['alias'] for row in self.runtime.manager.active_config['conversations']]
        code, job = self.runtime.request('POST', '/api/ai/questions',
            {'question': question, 'scope': aliases, 'time_range': 'all', **extra})
        self.assertEqual(code, 200, job)
        service = self.runtime.manager.assistant
        service.jobs[job['job_id']]['worker'].join(timeout=3)
        return self.runtime.request('GET', '/api/ai/jobs/' + job['job_id'])[1]

    def test_session_discovery_and_old_nonce_cannot_target_same_alias_same_model_new_profile(self):
        self.assertEqual(self.runtime.request('GET', '/api/setup', generation=None)[0], 200)
        self.assertEqual(self.runtime.request('GET', '/api/conversations', generation=None)[0], 409)
        self.select()
        fingerprint = self.configuration()
        previous = self.question()
        self.assertEqual(previous['statistics']['message_count'], 15)
        self.assertEqual(self.runtime.provider.calls, [])
        old = self.runtime.manager.scope_generation
        common = self.runtime.manager.active_config['conversations'][0]['alias']
        self.select(['choice_000', 'choice_001'])
        self.assertIn(common, [row['alias'] for row in self.runtime.manager.active_config['conversations']])
        self.assertEqual(self.runtime.request('GET', '/api/ai/config')[1]['target_fingerprint'], fingerprint)
        routes = ['/api/conversations', '/api/status', '/api/messages?conversation=' + common + '&after=0',
                  '/api/ai/config', '/api/ai/threads', '/api/ai/jobs/' + previous['id'],
                  '/api/ai/evidence?conversation_id=' + common + '&message_id=1']
        for route in routes:
            code, value = self.runtime.request('GET', route, generation=old)
            self.assertEqual(code, 409, route)
            self.assertEqual(value['error']['code'], 'scope_changed')
        code, value = self.runtime.request('POST', '/api/ai/questions', {
            'question': '分析沟通重点', 'scope': [common], 'time_range': 'all',
            'target_fingerprint': fingerprint}, generation=old)
        self.assertEqual(code, 409)
        self.assertEqual(value['error']['code'], 'scope_changed')
        self.assertEqual(self.runtime.request('GET', '/api/ai/threads')[1]['threads'], [])
        self.assertEqual(self.runtime.request('GET', '/api/ai/threads/' + previous['thread_id'])[0], 404)
        self.assertEqual(self.runtime.request('GET', '/api/session', generation=None)[0], 200)

    def test_selection_limits_same_kind_and_catalog_reload_keep_actual_collector_running(self):
        original = self.runtime.manager.scope_generation
        for choices in ([], ['choice_000'] * 2, ['choice_%03d' % i for i in range(51)]):
            self.assertEqual(self.runtime.request('POST', '/api/setup/select',
                {'conversation_ids': choices})[0], 400)
            self.assertEqual(self.runtime.manager.scope_generation, original)
        self.select(['choice_%03d' % i for i in range(50)])
        self.assertEqual(len(self.runtime.request('GET', '/api/conversations')[1]['conversations']), 50)
        self.assertEqual(self.runtime.request('GET', '/api/status')[1]['total_messages'], 150)
        collector = self.runtime.manager.collector
        before = collector.polls
        self.runtime.command({'command': 'queue_message'})
        self.assertEqual(self.runtime.request('POST', '/api/setup/catalog', {})[0], 200)
        self.runtime.tick()
        self.assertIs(self.runtime.manager.collector, collector)
        self.assertFalse(collector.closed)
        self.assertGreater(collector.polls, before)
        self.assertEqual(self.runtime.request('GET', '/api/status')[1]['total_messages'], 151)
        self.assertEqual(len(self.runtime.request('GET', '/api/setup/conversations')[1]['conversations']), 60)
        self.select(['choice_035', 'choice_036'])
        self.assertTrue(all(row['type'] == 'group' for row in
                            self.runtime.request('GET', '/api/conversations')[1]['conversations']))
        self.select(['choice_000', 'choice_001', 'choice_002'])
        self.assertTrue(all(row['type'] == 'private' for row in
                            self.runtime.request('GET', '/api/conversations')[1]['conversations']))

    def test_base_data_checks_generation_inside_getter_after_preliminary_guard(self):
        self.select()
        manager = self.runtime.manager
        before, release = threading.Event(), threading.Event()
        original = manager.public_scope_context
        paused = False
        def context():
            nonlocal paused
            result = original()
            if not paused and threading.current_thread() is not threading.main_thread():
                paused = True
                before.set()
                release.wait(3)
            return result
        old = manager.scope_generation
        result = []
        with patch.object(manager, 'public_scope_context', side_effect=context), \
                patch.object(manager.provider, 'messages', wraps=manager.provider.messages) as read:
            request = threading.Thread(target=lambda: result.append(self.runtime.request(
                'GET', '/api/messages', generation=old)))
            request.start()
            try:
                self.assertTrue(before.wait(2))
                self.runtime.populate()
                manager.select_many(['choice_000'], expected_generation=old)
            finally:
                release.set(); request.join(timeout=3)
            self.assertEqual(result[0][0], 409)
            self.assertEqual(result[0][1]['error']['code'], 'scope_changed')
            read.assert_not_called()

    def test_ai_dispatch_pins_generation_under_manager_lock_through_service_acquisition(self):
        self.select()
        manager = self.runtime.manager
        self.runtime.populate()
        old = manager.scope_generation
        original = manager.ai_service
        acquired, release, selected = threading.Event(), threading.Event(), threading.Event()
        observed, results = [], []
        def getter(**kwargs):
            observed.append(kwargs.get('expected_generation'))
            service = original(**kwargs)
            acquired.set()
            release.wait(3)
            return service
        aliases = [row['alias'] for row in manager.active_config['conversations']]
        with patch.object(manager, 'ai_service', side_effect=getter):
            request = threading.Thread(target=lambda: results.append(self.runtime.request('POST', '/api/ai/plan',
                {'question': '今天消息多少条', 'scope': aliases, 'time_range': 'all'}, generation=old)))
            request.start()
            self.assertTrue(acquired.wait(2))
            def switch():
                manager.select_many(['choice_000'], expected_generation=old)
                selected.set()
            switching = threading.Thread(target=switch)
            switching.start()
            try:
                self.assertFalse(selected.wait(.05), 'Profile selection must wait for the guarded AI dispatch.')
            finally:
                release.set(); request.join(timeout=3); switching.join(timeout=3)
        self.assertEqual(observed, [old])
        self.assertEqual(results[0][0], 200)
        self.assertTrue(selected.is_set())
        self.assertNotEqual(manager.scope_generation, old)
        self.assertEqual(self.runtime.request('GET', '/api/ai/config', generation=old)[0], 409)

    def test_real_checkpoints_history_and_late_model_result_stay_in_their_profile(self):
        first = self.select()
        answer = self.question()
        self.assertEqual(answer['response_kind'], 'statistics')
        initial = self.runtime.snapshot()
        fingerprint = self.configuration()
        self.runtime.provider.slow = True
        self.runtime.provider.release.clear()
        aliases = [row['alias'] for row in self.runtime.manager.active_config['conversations']]
        code, launched = self.runtime.request('POST', '/api/ai/questions', {'question': '分析沟通重点',
            'scope': aliases, 'time_range': 'all', 'target_fingerprint': fingerprint})
        self.assertEqual(code, 200)
        self.assertTrue(self.runtime.provider.entered.wait(2))
        old_service = self.runtime.manager.assistant
        self.select(['choice_000', 'choice_035'])
        self.assertEqual(self.runtime.request('GET', '/api/ai/threads')[1]['threads'], [])
        self.runtime.provider.release.set()
        old_service.jobs[launched['job_id']]['worker'].join(timeout=3)
        self.assertEqual(old_service.get_job(launched['job_id'])['stage'], 'cancelled')
        self.assertEqual(self.runtime.request('GET', '/api/ai/threads')[1]['threads'], [])
        self.assertEqual(self.runtime.snapshot(first), initial)
        code, restored = self.runtime.request('POST', '/api/setup/profile', {'profile_id': first})
        self.assertEqual(code, 200, restored)
        self.runtime.tick()
        self.assertEqual(self.runtime.snapshot(), initial)
        history = self.runtime.request('GET', '/api/ai/threads/' + answer['thread_id'])
        self.assertEqual(history[0], 200)
        self.assertEqual(history[1]['messages'][-1]['response_kind'], 'statistics')
        late = self.runtime.request('GET', '/api/ai/threads/' + launched['thread_id'])
        self.assertEqual([row['role'] for row in late[1]['messages']], ['user'])

    def test_another_account_cannot_see_profiles_model_or_messages_even_with_same_chat_aliases(self):
        self.select(['choice_000', 'choice_001'])
        self.configuration()
        old = self.runtime.manager.scope_generation
        first_scope = self.runtime.manager.active_scope_id
        self.runtime.switch_account()
        setup = self.runtime.request('GET', '/api/setup', generation=None)[1]
        self.assertEqual(setup['profiles'], [])
        self.assertEqual(setup['stage'], 'needs_initialization')
        self.assertEqual(self.runtime.request('GET', '/api/ai/config', generation=old)[0], 409)
        self.runtime.make_ready()
        self.select(['choice_000', 'choice_001'])
        self.assertEqual(self.runtime.manager.active_scope_id, first_scope)
        self.assertFalse(self.runtime.request('GET', '/api/ai/config')[1]['configured'])
        self.assertEqual(self.runtime.request('GET', '/api/ai/threads')[1]['threads'], [])
        messages = self.runtime.request('GET', '/api/messages')[1]['messages']
        self.assertTrue(all('合成账号1' in row['body'] for row in messages))
        self.assertFalse(any('合成账号0' in row['body'] for row in messages))

    def test_running_account_a_b_a_uses_owner_detection_and_preserves_original_sqlite_and_ai(self):
        first = self.select(['choice_000', 'choice_001'])
        answer = self.question()
        fingerprint = self.configuration()
        checkpoint = self.runtime.snapshot()
        manager = self.runtime.manager
        first_collector = manager.collector
        nonce_a = manager.scope_generation
        self.runtime.switch_account()
        self.assertIs(self.runtime.manager, manager)
        self.assertTrue(first_collector.closed)
        self.assertEqual(manager.setup()['profiles'], [])
        self.assertIsNone(manager.provider)
        self.assertIsNone(manager.assistant)
        for route in ('/api/messages', '/api/status', '/api/ai/config', '/api/ai/threads'):
            self.assertEqual(self.runtime.request('GET', route, generation=nonce_a)[0], 409)
        self.runtime.make_ready()
        self.select(['choice_000', 'choice_001'])
        nonce_b = manager.scope_generation
        self.assertEqual(manager.active_scope_id, first)
        self.assertFalse(self.runtime.request('GET', '/api/ai/config')[1]['configured'])
        second_collector = manager.collector
        self.runtime.switch_account()
        self.assertIs(self.runtime.manager, manager)
        self.assertTrue(second_collector.closed)
        self.runtime.tick()
        self.assertEqual(manager.stage, 'running')
        self.assertEqual(manager.active_scope_id, first)
        self.assertEqual(self.runtime.snapshot(), checkpoint)
        self.assertEqual(self.runtime.request('GET', '/api/ai/config')[1]['target_fingerprint'], fingerprint)
        self.assertEqual(self.runtime.request('GET', '/api/ai/threads/' + answer['thread_id'])[0], 200)
        self.assertNotIn(manager.scope_generation, (nonce_a, nonce_b))
        for old in (nonce_a, nonce_b):
            for route in ('/api/conversations', '/api/messages', '/api/ai/threads'):
                self.assertEqual(self.runtime.request('GET', route, generation=old)[0], 409)


if __name__ == '__main__':
    unittest.main()
