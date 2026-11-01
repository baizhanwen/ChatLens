"""Large metadata directories and opaque paging, without real WeChat access."""
from pathlib import Path
import hashlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/windows-proof')]

from collector_private import read_private_json
from web_data import WebDataError
from windows_web_manager import CatalogRead, CatalogReadError, load_contact_catalog
from windows_catalog_paging import page_conversations, PAGE_SIZE


class CatalogPagingTests(unittest.TestCase):
    def manager(self, size=651):
        rows = [{'id': 'opaque-synthetic-%06d' % n, 'name': '合成会话%06d' % n,
                 'type': 'group' if n % 3 == 0 else 'private', 'selected': n in (0, 350, 650)}
                for n in range(size)]
        return SimpleNamespace(contacts=rows, catalog_ready=True, scope_generation='synthetic-domain-a',
                               _catalog_status=lambda: {'state': 'ready'})

    def test_all_pages_are_complete_disjoint_and_always_include_existing_selection_on_first_page(self):
        manager = self.manager()
        first = page_conversations(manager)
        self.assertEqual(len(first['conversations']), PAGE_SIZE)
        self.assertEqual(len(first['selected_conversations']), 3)
        self.assertTrue(first['has_more'])
        cursor, rows = first['next_cursor'], list(first['conversations'])
        self.assertRegex(cursor, r'^[A-Za-z0-9_-]{1,128}$')
        while cursor:
            result = page_conversations(manager, cursor=cursor)
            self.assertEqual(result['selected_conversations'], [])
            rows.extend(result['conversations'])
            cursor = result['next_cursor']
        self.assertEqual(len(rows), 651)
        self.assertEqual(len({row['id'] for row in rows}), 651)
        self.assertFalse(result['has_more'])
        self.assertEqual(manager.contacts[350]['name'], '合成会话000350')

    def test_query_pages_are_global_not_just_the_initial_two_hundred_rows(self):
        manager = self.manager(12001)
        result = page_conversations(manager, '012000')
        self.assertEqual([row['id'] for row in result['conversations']], ['opaque-synthetic-012000'])
        self.assertFalse(result['has_more'])
        self.assertEqual(len(result['selected_conversations']), 3)

    def test_cursors_reject_tampering_different_query_domain_or_catalog_revision(self):
        manager = self.manager()
        cursor = page_conversations(manager)['next_cursor']
        for invalid in ('x', cursor[:-2] + ('AB' if cursor[-2:] != 'AB' else 'CD'), '\0', 'x' * 129):
            with self.assertRaisesRegex(WebDataError, 'windows_scope_cursor_invalid'):
                page_conversations(manager, cursor=invalid)
        with self.assertRaisesRegex(WebDataError, 'windows_scope_cursor_invalid'):
            page_conversations(manager, '其他查询', cursor)
        manager.scope_generation = 'synthetic-domain-b'
        with self.assertRaisesRegex(WebDataError, 'windows_scope_cursor_invalid'):
            page_conversations(manager, cursor=cursor)
        manager.scope_generation = 'synthetic-domain-a'
        manager.contacts[-1]['name'] = '合成名称已更新'
        with self.assertRaisesRegex(WebDataError, 'windows_scope_cursor_invalid'):
            page_conversations(manager, cursor=cursor)

    def test_selected_changes_and_another_manager_invalidate_cursor_without_exposing_rows(self):
        manager = self.manager()
        cursor = page_conversations(manager)['next_cursor']
        other = self.manager()
        with self.assertRaisesRegex(WebDataError, 'windows_scope_cursor_invalid'):
            page_conversations(other, cursor=cursor)
        manager.contacts[-1]['selected'] = False
        with self.assertRaisesRegex(WebDataError, 'windows_scope_cursor_invalid'):
            page_conversations(manager, cursor=cursor)

    def test_not_ready_cannot_be_mistaken_for_ready_empty_or_seed_current_selection(self):
        manager = self.manager()
        manager.catalog_ready = False
        manager._catalog_status = lambda: {'state': 'loading'}
        result = page_conversations(manager)
        self.assertEqual(result['conversations'], [])
        self.assertEqual(result['selected_conversations'], [])
        self.assertEqual(result['catalog']['state'], 'loading')
        self.assertIsNone(result['next_cursor'])

    def test_more_than_fifty_selected_is_rejected_instead_of_silently_truncated(self):
        manager = self.manager()
        for row in manager.contacts[:51]:
            row['selected'] = True
        with self.assertRaisesRegex(WebDataError, 'windows_scope_selection_invalid'):
            page_conversations(manager)

    def test_search_first_blank_query_returns_only_existing_selection_until_a_name_is_entered(self):
        manager = self.manager(12001)
        manager.search_first = True
        manager.contacts[0]['chat_id'] = 'SYNTHETIC_PRIVATE_ID_NOT_FOR_API'
        result = page_conversations(manager)
        self.assertTrue(result['search_required'])
        self.assertEqual(result['conversations'], [])
        self.assertEqual(len(result['selected_conversations']), 3)
        self.assertFalse(result['has_more'])
        self.assertIsNone(result['next_cursor'])
        self.assertTrue(all('chat_id' not in row for row in result['selected_conversations']))
        found = page_conversations(manager, '012000')
        self.assertEqual(len(found['conversations']), 1)
        self.assertEqual(found['conversations'][0]['name'], '合成会话012000')
        self.assertEqual(len(found['selected_conversations']), 3)


class LargeCatalogSourceTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing isolated encrypted-source fixture, not its tests.
        from tests.test_windows_web_manager import WindowsManagerTests
        self.fixture = WindowsManagerTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def contact_connection(self, driver):
        from windows_selected_engine import _open
        keys = read_private_json(self.fixture.data / 'private/keys.json')
        key = keys['keys']['contact/contact.db']['enc_key']
        connection = driver.connect(str(self.fixture.db / 'contact/contact.db'))
        connection.execute('PRAGMA key = "x\'%s\'"' % key)
        connection.execute('PRAGMA cipher_compatibility=4')
        return connection

    def job(self):
        manager = self.fixture.manager(catalog_loader=load_contact_catalog)
        return CatalogRead(manager._source_documents(), manager.scope_generation, manager.account_identity)

    def test_real_encrypted_contact_book_over_ten_thousand_reaches_matching_contacts_at_the_end(self):
        driver = self.fixture.create_encrypted_catalog_sources(contact_count=12001)
        connection = self.contact_connection(driver)
        for row in self.fixture.choices:
            connection.execute('INSERT INTO contact VALUES (?,?,?,?)',
                               (row['chat_id'], row['name'], '合成昵称', 'SYNTHETIC_SECRET_NOT_QUERIED'))
        connection.commit()
        connection.close()
        job = self.job()
        result = load_contact_catalog(job)
        self.assertEqual({row['name'] for row in result}, {row['name'] for row in self.fixture.choices})
        self.assertEqual(job.snapshot()['contacts_seen'], 12005)
        self.assertEqual(job.snapshot()['conversations_found'], 4)
        self.assertEqual(job.snapshot()['phase'], 'complete')
        self.assertEqual(job.connections, set())

    def test_more_than_ten_thousand_valid_message_table_names_are_streamed_and_not_mislimited(self):
        self.fixture.create_encrypted_catalog_sources()
        job = self.job()
        rows = [('Msg_' + hashlib.md5(('synthetic-catalog-%d' % n).encode()).hexdigest(),)
                for n in range(12001)]
        rows += [('Msg_' + hashlib.md5(row['chat_id'].encode()).hexdigest(),) for row in self.fixture.choices]
        # These are explicit synthetic sqlite_master rows. Real encrypted
        # contacts/HMAC are used; no Windows process or message table is queried.
        class MetadataCursor:
            def __init__(self): self.offset = 0
            def fetchmany(self, count):
                batch = rows[self.offset:self.offset + count]
                self.offset += len(batch)
                return batch
        class MetadataConnection:
            def execute(self, sql):
                if 'sqlite_master' not in sql or 'message_content' in sql:
                    raise AssertionError('only synthetic schema metadata is allowed')
                return MetadataCursor()
            def close(self): pass
        original_open = job.open
        def open_metadata(driver, path, key):
            return MetadataConnection() if str(path).endswith('message_0.db') else original_open(driver, path, key)
        with patch.object(job, 'open', side_effect=open_metadata):
            result = load_contact_catalog(job)
        self.assertEqual(len(result), 4)
        self.assertGreater(job.snapshot()['tables_seen'], 12001)
        self.assertEqual(job.snapshot()['phase'], 'complete')

    def test_unsupported_msg_objects_and_duplicate_hashes_are_not_counted_as_valid_choices(self):
        self.fixture.create_encrypted_catalog_sources()
        job = self.job()
        expected = self.fixture.choices[0]
        digest = hashlib.md5(expected['chat_id'].encode()).hexdigest()
        rows = [('Msg_' + digest.upper(),), ('Msg_' + digest,), ('Msg_' + digest + '_extra',),
                ('Msg_not_a_chat',), ('Msg_' + 'f' * 33,), ('Msg_' + 'z' * 32,)]
        class Cursor:
            def __init__(self): self.done = False
            def fetchmany(self, _):
                if self.done: return []
                self.done = True
                return rows
        class Connection:
            def execute(self, sql): return Cursor()
            def close(self): pass
        original_open = job.open
        with patch.object(job, 'open', side_effect=lambda driver, path, key:
                          Connection() if str(path).endswith('message_0.db') else original_open(driver, path, key)):
            result = load_contact_catalog(job)
        self.assertEqual(sum(row['chat_id'] == expected['chat_id'] for row in result), 1)
        self.assertEqual(job.snapshot()['phase'], 'complete')

    def test_explicit_small_scan_guard_rejects_complete_index_without_publishing_partial_results(self):
        self.fixture.create_encrypted_catalog_sources(contact_count=101)
        job = self.job()
        with patch('windows_web_manager.CATALOG_SCAN_LIMIT', 100):
            with self.assertRaisesRegex(CatalogReadError, 'windows_scope_catalog_limit'):
                load_contact_catalog(job)
        self.assertEqual(job.snapshot()['contacts_seen'], 101)
        self.assertEqual(job.snapshot()['phase'], 'reading_contacts')
        self.assertIsNone(job.result)

    def test_actual_manager_retains_more_than_ten_thousand_choices_and_pages_opaque_ids(self):
        choices = [{'chat_id': 'synthetic-large-%05d' % n, 'name': '合成扩展%05d' % n,
                    'type': 'private'} for n in range(12001)]
        choices.extend(self.fixture.choices)
        manager = self.fixture.manager(catalog_loader=lambda _: choices)
        self.fixture.ready_catalog(manager)
        self.assertEqual(len(manager.contacts), 12005)
        first = manager.list_conversations(expected_generation=manager.scope_generation)
        self.assertEqual(len(first['conversations']), 200)
        self.assertEqual(len(first['selected_conversations']), 2)
        second = manager.list_conversations(cursor=first['next_cursor'], expected_generation=manager.scope_generation)
        self.assertEqual(len(second['conversations']), 200)
        self.assertFalse({row['id'] for row in first['conversations']}
                         & {row['id'] for row in second['conversations']})
        tail = manager.list_conversations('12000', expected_generation=manager.scope_generation)
        self.assertEqual([row['name'] for row in tail['conversations']], ['合成扩展12000'])
        self.assertEqual(len(tail['selected_conversations']), 2)
        self.assertTrue(all('chat_id' not in row for row in first['conversations']))
        with self.assertRaisesRegex(WebDataError, 'scope_changed'):
            manager.list_conversations(cursor=first['next_cursor'], expected_generation='stale-synthetic-scope')


if __name__ == '__main__':
    unittest.main()
