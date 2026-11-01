"""Failure paths for scope metadata and closing/reopening an AI workspace."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ai_service as ai_fixtures
import test_web_data as data_fixtures
import macos_collector
from conversation_scope import alias_for
from web_data import WebDataError


class ScopeSafetyTests(unittest.TestCase):
    def test_required_scope_catalog_creation_failure_cannot_publish_a_web_collector(self):
        collector = object.__new__(macos_collector.MacCollector)
        collector.session = SimpleNamespace(config={'conversations': []})
        collector.metadata = SimpleNamespace(ensure_catalog=Mock(side_effect=OSError('SYNTHETIC_METADATA_WRITE_DENIED')),
                                             refresh_catalog=Mock(), failed=False)
        with self.assertRaises(OSError):
            collector._catalog()
        collector.metadata.refresh_catalog.assert_not_called()

    def test_missing_dynamic_catalog_fails_closed_instead_of_reporting_two_empty_legacy_conversations(self):
        fixture = data_fixtures.WebDataTests()
        # Fixture setup does not invoke the named test; use the common setup only.
        fixture.setUp();self.addCleanup(fixture.tearDown)
        alias = alias_for('synthetic_dynamic_contact')
        fixture.store.commit({}, {}, [data_fixtures.event(1, alias=alias)], {})
        for operation in (fixture.reader.conversations, fixture.reader.status, lambda: fixture.reader.messages(alias)):
            with self.subTest(operation=operation):
                with self.assertRaises(WebDataError) as caught:
                    operation()
                self.assertEqual(caught.exception.code, 'web_catalog_scope_unavailable')

    def test_closed_old_worker_cannot_overwrite_new_turn_after_same_profile_reopens(self):
        fixture = ai_fixtures.AiAssistantTests(methodName='test_empty_evidence_finishes_as_system_result_without_model_call')
        fixture.setUp();self.addCleanup(fixture.tearDown)
        fixture.add('合成报价待确认。')
        fixture.provider.release.clear()
        launched = fixture.assistant.ask(fixture.question(question='分析报价'))
        self.assertTrue(fixture.provider.entered.wait(2))
        old_worker = fixture.assistant.jobs[launched['job_id']]['worker']
        fixture.assistant.close()
        self.assertTrue(old_worker.is_alive())
        replacement = ai_fixtures.service.AiAssistant(fixture.data, fixture.ai_dir, provider=fixture.provider)
        fixture.assistants.append(replacement)
        latest = replacement.ask({'question': '一共有多少条消息', 'scope': ['private_test'], 'time_range': 'all',
                                  'thread_id': launched['thread_id']})
        replacement.jobs[latest['job_id']]['worker'].join(2)
        self.assertEqual(replacement.get_job(latest['job_id'])['response_kind'], 'statistics')
        before = replacement.get_thread(launched['thread_id'])
        self.assertEqual(len(before['messages']), 3)
        fixture.provider.release.set();old_worker.join(3)
        self.assertFalse(old_worker.is_alive())
        self.assertEqual(replacement.get_thread(launched['thread_id']), before)


if __name__ == '__main__':
    unittest.main()
