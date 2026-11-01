"""Windows scope-owner contracts with real local stores and synthetic sources."""
from contextlib import redirect_stdout
import hashlib
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/windows-proof')]
import collector_service
from collector_private import ensure_private_dir, read_private_json, write_private_json_new
from collector_web_meta import WebMetadata
from continuous_store import CollectorStore
from conversation_scope import alias_for
from web_data import WebDataError
from windows_web_manager import WindowsWebManager, atomic_private, scope_fingerprint
import web_runtime
from windows_web_server import make_windows_server


class Child:
    def __init__(self, store):
        self.store, self.code = store, None
    def poll(self):
        return self.code


class WindowsManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-windows-manager-')
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / 'synthetic-self_1234/db_storage'
        self.db.mkdir(parents=True)
        self.exe = self.root / 'Weixin.exe'; self.exe.write_bytes(b'SYNTHETIC_NOT_EXECUTED')
        self.data = self.root / 'collector-data'
        for path in (self.data, self.data / 'private', self.data / 'private/store'):
            path.mkdir(mode=0o700)
        chats = [{'alias': alias, 'chat_id': chat, 'expected_sender_id': 'synthetic-peer'}
                 for alias, chat in [('private_test','synthetic-peer'),('group_test','synthetic-group@chatroom')]]
        self.scope = {'schema_version': 1, 'session_id': 'a' * 32,
                      'db_root': str(self.db), 'exe_path': str(self.exe), 'config': {'conversations': chats},
                      'test_tokens': {alias: ['SYNTHETIC_' + alias.upper() + '_' + str(i) for i in range(3)] for alias in ['private_test','group_test']}}
        keys = {'schema_version': 1, 'db_root': str(self.db), 'exe_path': str(self.exe),
                'keys': {relative: {'enc_key': '01' * 32} for relative in collector_service.RELATIVES}}
        write_private_json_new(self.data / 'private/scope.json', self.scope)
        write_private_json_new(self.data / 'private/keys.json', keys)
        self.children = []
        self.seed(self.data)
        self.package = self.root / 'package'; self.package.mkdir()
        self.output = self.root / 'results'; self.output.mkdir()
        self.probe_result = {'state':'matched','matched':True,'reason':'synthetic_matched'}
        self.clock_value = 0
        self.managers = []
        self.choices = [{'chat_id': 'synthetic-peer', 'name': '合成私聊', 'type':'private'},
                        {'chat_id':'synthetic-other-peer','name':'合成私聊二','type':'private'},
                        {'chat_id':'synthetic-group@chatroom','name':'合成群聊','type':'group'},
                        {'chat_id':'synthetic-second-group@chatroom','name':'合成群聊二','type':'group'}]

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        for child in self.children:
            if child.code is None:
                child.store.close(); child.code = 0
        self.temp.cleanup()

    def seed(self, target):
        scope, _, _, _, _, binding = collector_service.prepare_session(target)
        store = CollectorStore(target / 'private/store', binding)
        if not store.resumed:
            cursors = {'message/message_0.db': {row['alias']:{'local_id':0,'present':True} for row in scope['config']['conversations']}}
            store.initialize(cursors, {})
            row = scope['config']['conversations'][0]
            event = {'source_key': hashlib.sha256((str(target)+row['alias']).encode()).hexdigest(),
                     'alias': row['alias'], 'source_local_id':1, 'sent_at':1793491200,
                     'sender_id':'synthetic-peer', 'body':'合成数据不含真实聊天', 'observed_at':'2026-11-01T00:00:00+00:00'}
            store.commit(cursors, cursors, [event], {})
        metadata = WebMetadata(target / 'private', store.binding_digest, '2026-11-01T00:00:00+00:00')
        metadata.ensure_catalog(scope['config'])
        metadata.status('collecting',store.stats()['total_messages'],0,store.resumed)
        store.close()

    def start_child(self, target, output):
        marker=read_private_json(self.data/'private/owned-collector.json')
        self.assertEqual(marker['state'],'starting')
        self.assertEqual(marker['output'],str(output))
        output.mkdir()
        self.seed(target)
        binding = collector_service.prepare_session(target)[-1]
        child = Child(CollectorStore(target / 'private/store', binding))
        self.children.append(child)
        collector_service.Heartbeat(output).emit('collecting',child.store.stats()['total_messages'],0,True)
        return child

    def stop_child(self, child, output):
        child.store.close(); child.code = 0
        return 0, True

    def manager(self, **options):
        defaults = dict(pid=123,creation_time=456,start_collector=self.start_child,
                        stop_collector=self.stop_child,catalog_loader=lambda _:[dict(row) for row in self.choices],
                        account_probe=lambda:dict(self.probe_result),clock=lambda:self.clock_value)
        defaults.update(options)
        manager = WindowsWebManager(self.data,self.package,self.output,**defaults)
        self.managers.append(manager)
        manager.start()
        return manager

    def ready_catalog(self, manager):
        before = manager.child
        manager.request_catalog(expected_generation=manager.scope_generation)
        manager.tick()
        self.assertTrue(manager.catalog_ready)
        self.assertIs(manager.child,before)
        return manager.list_conversations(expected_generation=manager.scope_generation)['conversations']

    def select(self, manager, count=4):
        rows = self.ready_catalog(manager)
        old = manager.scope_generation
        manager.select_many([row['id'] for row in rows[:count]],expected_generation=old)
        self.assertNotEqual(manager.scope_generation,old)
        with self.assertRaisesRegex(WebDataError,'scope_changed'):
            manager.messages('private_test',expected_generation=old)
        manager.tick()
        self.assertEqual(manager.stage,'running')
        return manager.active_scope_id

    def test_legacy_baseline_scope_keys_and_records_remain_unchanged(self):
        paths = [self.data/'private'/name for name in ('scope.json','keys.json')]
        before = {p:p.read_bytes() for p in paths}
        manager=self.manager()
        old_baseline=manager.child.store.snapshot()['baseline_completed_at']
        identifier=self.select(manager)
        self.assertNotEqual(identifier,'legacy')
        self.assertEqual(len(manager.conversations()['conversations']),4)
        manager.restore_profile('legacy',expected_generation=manager.scope_generation);manager.tick()
        self.assertEqual(manager.active_scope_id,'legacy')
        self.assertEqual(manager.child.store.snapshot()['baseline_completed_at'],old_baseline)
        self.assertEqual(len(manager.messages('private_test')['messages']),1)
        self.assertEqual(before,{p:p.read_bytes() for p in paths})

    def test_same_account_first_model_copy_but_no_ai_history_copy_or_overwrite(self):
        assistant=ensure_private_dir(self.data/'private/assistant',create=True)
        write_private_json_new(assistant/'provider.json',{'schema_version':1,'synthetic_config':True})
        write_private_json_new(assistant/'threads.json',{'synthetic_original_history':True})
        manager=self.manager();identifier=self.select(manager)
        target=manager._assistant_directory()
        self.assertTrue((target/'provider.json').exists())
        self.assertFalse((target/'threads.json').exists())
        atomic_private(target/'provider.json',{'schema_version':1,'synthetic_new_config':True})
        manager.restore_profile('legacy',expected_generation=manager.scope_generation);manager.tick()
        manager.restore_profile(identifier,expected_generation=manager.scope_generation);manager.tick()
        self.assertTrue(read_private_json(target/'provider.json')['synthetic_new_config'])
        self.assertTrue(read_private_json(assistant/'threads.json')['synthetic_original_history'])

    def test_active_pointer_restores_new_profile_without_legacy_changes(self):
        manager=self.manager();identifier=self.select(manager)
        manager.close()
        replacement=self.manager()
        self.assertEqual(replacement.active_scope_id,identifier)
        self.assertEqual(len(replacement.conversations()['conversations']),4)

    def test_account_mismatch_clears_domains_and_return_restores_original_checkpoint(self):
        manager=self.manager();self.ready_catalog(manager)
        before=manager.scope_generation
        self.probe_result={'state':'mismatch','matched':False,'reason':'synthetic_other_account'}
        self.clock_value=5;manager.tick()
        self.assertNotEqual(manager.scope_generation,before)
        self.assertTrue(manager.account_blocked)
        self.assertIsNone(manager.provider);self.assertIsNone(manager.assistant);self.assertIsNone(manager.child)
        self.assertEqual(manager.setup()['profiles'],[])
        self.assertIsNone(manager.setup()['active_scope'])
        self.assertEqual(manager.list_conversations()['conversations'],[])
        with self.assertRaises(WebDataError): manager.messages('private_test')
        self.probe_result={'state':'matched','matched':True,'reason':'synthetic_matched'}
        self.clock_value=10;manager.tick()
        self.assertFalse(manager.account_blocked)
        self.assertEqual(manager.stage,'running')
        self.assertEqual(len(manager.messages('private_test')['messages']),1)

    def test_unknown_account_never_starts_collection_or_reads_catalog(self):
        self.probe_result={'state':'unknown','matched':False,'reason':'synthetic_unavailable'}
        manager=self.manager(catalog_loader=lambda _:self.fail('unknown account must not enumerate sources'))
        self.assertEqual(manager.children_started,0)
        self.assertTrue(manager.account_blocked)
        with self.assertRaises(ValueError):manager.request_catalog(expected_generation=manager.scope_generation)

    def test_start_intent_is_durable_before_launch_and_failed_launch_does_not_erase_it(self):
        def uncertain_launch(*_):
            self.assertEqual(read_private_json(self.data/'private/owned-collector.json')['state'],'starting')
            raise OSError('synthetic_launch_failure')
        with self.assertRaises(OSError):self.manager(start_collector=uncertain_launch)
        self.assertEqual(read_private_json(self.data/'private/owned-collector.json')['state'],'starting')
        self.managers[-1].close()
        with self.assertRaises(Exception):self.manager()

    def test_running_uncertain_launch_blocks_same_owner_and_keeps_start_intent(self):
        manager=self.manager();rows=self.ready_catalog(manager);started=manager.children_started
        manager.start_collector=lambda *_:(_ for _ in ()).throw(OSError('synthetic_uncertain_launch'))
        manager.select_many([row['id'] for row in rows],expected_generation=manager.scope_generation)
        manager.tick()
        self.assertFalse(manager.cleanup_confirmed)
        self.assertTrue(manager.switching)
        self.assertEqual(read_private_json(self.data/'private/owned-collector.json')['state'],'starting')
        for _ in range(2):manager.tick()
        self.assertEqual(manager.children_started,started)
        with self.assertRaises(ValueError):manager.select_many([rows[0]['id']],expected_generation=manager.scope_generation)
        with self.assertRaises(ValueError):manager.restore_profile('legacy',expected_generation=manager.scope_generation)

    def test_mutation_forces_probe_even_inside_cached_interval(self):
        manager=self.manager();rows=self.ready_catalog(manager)
        self.probe_result={'state':'mismatch','matched':False,'reason':'synthetic_other'}
        with self.assertRaises(ValueError):manager.select_many([rows[0]['id']],expected_generation=manager.scope_generation)
        self.assertTrue(manager.account_blocked)
        self.assertEqual(manager.children_started,1)

    def test_picker_refresh_keeps_opaque_ids_and_selects_existing_scope(self):
        manager=self.manager();first=self.ready_catalog(manager);second=self.ready_catalog(manager)
        self.assertEqual(first,second)
        self.assertEqual(sum(row['selected'] for row in first),2)
        self.assertFalse(any('synthetic-peer'==row['id'] for row in first))

    def test_zero_fifty_one_unknown_and_duplicate_selections_do_not_switch(self):
        manager=self.manager();rows=self.ready_catalog(manager);generation=manager.scope_generation
        for value in ([],['unknown'],[rows[0]['id']]*2,[rows[0]['id']]*51):
            with self.assertRaises(ValueError):manager.select_many(value,expected_generation=generation)
            self.assertEqual(manager.scope_generation,generation)

    def test_fifty_groups_and_private_only_scopes_are_supported(self):
        self.choices=[{'chat_id':f'synthetic-group-{n}@chatroom','name':f'合成群{n}','type':'group'} for n in range(50)]
        manager=self.manager();self.select(manager,50)
        self.assertEqual(manager.setup()['active_scope']['group_count'],50)
        self.assertEqual(manager.setup()['active_scope']['private_count'],0)

    def test_tampered_profile_account_cannot_restore_or_import_configuration(self):
        manager=self.manager();identifier=self.select(manager)
        manager.restore_profile('legacy',expected_generation=manager.scope_generation);manager.tick()
        manifest=manager._profile_root(identifier)/'profile.json'
        value=read_private_json(manifest);value['account_identity']='0'*64;atomic_private(manifest,value)
        with self.assertRaises(ValueError):manager.restore_profile(identifier,expected_generation=manager.scope_generation)
        self.assertEqual(manager.active_scope_id,'legacy')

    def test_second_manager_cannot_own_same_legacy_directory(self):
        first=self.manager()
        with self.assertRaisesRegex(ValueError,'runtime_already_running'):self.manager()
        self.assertEqual(first.stage,'running')

    def test_stop_failure_keeps_child_handle_and_blocks_retry_until_confirmed(self):
        manager=self.manager();rows=self.ready_catalog(manager);child=manager.child
        original=manager.stop_collector
        for failure in ('unconfirmed','exception'):
            with self.subTest(failure=failure):
                before_children=manager.children_started
                manager.stop_collector=(lambda *_:(None,False)) if failure=='unconfirmed' else (lambda *_:(_ for _ in ()).throw(OSError('synthetic_stop_failure')))
                manager.select_many([row['id'] for row in rows],expected_generation=manager.scope_generation)
                manager.tick()
                self.assertIs(manager.child,child)
                self.assertFalse(manager.cleanup_confirmed)
                self.assertTrue(manager.switching)
                self.assertEqual(manager.children_started,before_children)
                with self.assertRaises(ValueError):manager.restore_profile('legacy',expected_generation=manager.scope_generation)
                manager.stop_collector=original
                manager._stop_active()
                self.assertTrue(manager.cleanup_confirmed)
                self.assertIsNone(manager.child)
                manager.switching=False
                manager._activate('legacy')
                child=manager.child
        self.assertEqual(manager.children_started,3)

    def test_unconfirmed_child_marker_blocks_reopening_after_owner_exits(self):
        manager=self.manager();child=manager.child;original=manager.stop_collector
        manager.stop_collector=lambda *_:(None,False)
        with self.assertRaises(ValueError):manager.close()
        self.assertIs(manager.child,child)
        with self.assertRaises(Exception):self.manager()
        manager.stop_collector=original
        manager.close()

    def test_exited_child_and_offline_view_do_not_claim_collecting(self):
        manager=self.manager();manager.child.code=2
        self.assertEqual(manager.status()['state'],'error')
        manager.close()
        offline=self.manager(read_only=True,pid=None,creation_time=None)
        self.assertEqual(offline.status()['state'],'stopped')

    def test_readonly_can_view_old_profiles_without_keys_or_source_catalog(self):
        manager=self.manager();identifier=self.select(manager);manager.close()
        with patch('windows_web_manager.prepare_session',side_effect=AssertionError('readonly must not prepare keys')):
            offline=self.manager(read_only=True,pid=None,creation_time=None,
                catalog_loader=lambda _:self.fail('readonly no source catalog'),
                account_probe=lambda:self.fail('readonly no process probe'))
            self.assertEqual(offline.active_scope_id,identifier)
            self.assertEqual(len(offline.conversations()['conversations']),4)
            self.assertEqual(offline.children_started,0)
            self.assertFalse(offline.setup()['checks']['reader_ready'])
            with self.assertRaises(ValueError):offline.request_catalog(expected_generation=offline.scope_generation)
            offline.restore_profile('legacy',expected_generation=offline.scope_generation);offline.tick()
            self.assertEqual(len(offline.messages('private_test')['messages']),1)

    def test_production_managed_runtime_owns_real_http_and_verified_child_argv(self):
        static=self.package/'web';static.mkdir()
        (static/'index.html').write_text('<!doctype html><title>Synthetic</title>')
        result_output=self.root/'managed-runtime-output'
        commands=[]
        def factory(data, package, output, **options):
            options['account_probe']=lambda:dict(self.probe_result)
            options['catalog_loader']=lambda _:[dict(row) for row in self.choices]
            manager=WindowsWebManager(data,package,output,**options)
            self.managers.append(manager)
            return manager
        def child(command,**options):
            commands.append(command)
            target=Path(command[command.index('--data-dir')+1]);output=Path(command[command.index('--output')+1])
            return self.start_child(target,output)
        with patch.object(web_runtime,'stop_collection',side_effect=self.stop_child),redirect_stdout(io.StringIO()):
            result=web_runtime.run(self.data,result_output,package=self.package,pid=123,creation_time=456,
                no_open=True,popen_factory=child,wait=lambda event:event.set(),manage_scopes=True,manager_factory=factory)
        self.assertEqual(result['status'],'通过')
        self.assertTrue(result['server_started']);self.assertTrue(result['server_closed'])
        self.assertTrue(result['collector_cleanup_confirmed'])
        self.assertIn('--verify-active-account',commands[0])
        self.assertNotIn(str(self.root),json.dumps(result))
        self.assertEqual(result['scope_limit'],50)

    def test_managed_runtime_unknown_account_result_reports_safe_diagnosis(self):
        static=self.package/'web';static.mkdir()
        (static/'index.html').write_text('<!doctype html><title>Synthetic</title>')
        def factory(data,package,output,**options):
            options['account_probe']=lambda:{'state':'unknown','matched':False,'reason':'windows_account_open_database_unavailable'}
            manager=WindowsWebManager(data,package,output,**options)
            self.managers.append(manager)
            return manager
        with redirect_stdout(io.StringIO()):
            result=web_runtime.run(self.data,self.root/'unknown-runtime',package=self.package,pid=123,creation_time=456,
                no_open=True,wait=lambda event:event.set(),manage_scopes=True,manager_factory=factory)
        self.assertFalse(result['account_verified'])
        self.assertEqual(result['account_probe_reason'],'windows_account_open_database_unavailable')
        self.assertEqual(result['manager_final_stage'],'error')
        self.assertEqual(result['manager_final_reason'],'windows_active_account_not_verified')
        self.assertEqual(result['collection_setup_status'],'未验证')
        self.assertEqual(result['saved_scope_count'],0)
        self.assertNotIn(str(self.root),json.dumps(result))
        self.assertFalse(result['collector_child_started'])

    def waiting_manager(self):
        self.process_result={'state':'not_running','pid':None,'creation_time':None,'reason':'windows_process_not_running'}
        self.probe_result={'state':'unknown','matched':False,'reason':'windows_account_open_database_unavailable'}
        return self.manager(wait_for_wechat=True,pid=None,creation_time=None,account_probe=None,
            process_detector=lambda:dict(self.process_result),
            account_probe_factory=lambda *_:lambda:dict(self.probe_result))

    def detect_process(self, manager, *, logged_in=False, pid=222, birth=333):
        self.process_result={'state':'ready','pid':pid,'creation_time':birth,'reason':'windows_process_candidate_ready'}
        self.probe_result=({'state':'matched','matched':True,'reason':'windows_account_open_database_matched'} if logged_in else
                           {'state':'unknown','matched':False,'reason':'windows_account_open_database_unavailable'})
        self.clock_value+=4
        manager.tick()

    def test_waiting_startup_then_login_and_start_without_restarting_runtime(self):
        paths=[self.data/'private'/name for name in ('scope.json','keys.json')]
        original={path:path.read_bytes() for path in paths}
        old_readonly=self.manager(read_only=True,pid=None,creation_time=None)
        self.assertEqual(old_readonly.setup()['connection']['state'],'explicit_read_only')
        old_readonly.close()
        manager=self.waiting_manager()
        offline=manager.setup()['connection']
        self.assertFalse(manager.read_only)
        self.assertEqual(offline['state'],'waiting_for_wechat')
        self.assertTrue(offline['can_read_saved'])
        self.assertFalse(offline['checks']['process_running'])
        self.assertFalse(offline['can_modify_scope'])
        self.assertEqual(manager.children_started,0)
        self.assertEqual(manager.status()['state'],'stopped')
        self.detect_process(manager,logged_in=False)
        unverified=manager.setup()['connection']
        self.assertTrue(unverified['checks']['process_running'])
        self.assertFalse(unverified['checks']['account_verified'])
        self.assertEqual(unverified['state'],'account_unverified')
        self.assertEqual(manager.children_started,0)
        self.assertEqual(manager.list_conversations()['conversations'],[])
        with self.assertRaises(ValueError):manager.request_catalog(expected_generation=manager.scope_generation)
        self.probe_result={'state':'matched','matched':True,'reason':'windows_account_open_database_matched'}
        manager.request_recheck(expected_generation=manager.scope_generation)
        self.assertEqual(manager.setup()['connection']['state'],'checking_account')
        manager.tick()
        ready=manager.setup()['connection']
        self.assertEqual(ready['state'],'connected')
        self.assertTrue(ready['can_start']);self.assertTrue(ready['can_modify_scope'])
        self.assertFalse(ready['checks']['collecting'])
        self.assertEqual(manager.children_started,0)
        generation=manager.scope_generation
        manager.request_start(expected_generation=generation);manager.tick()
        self.assertNotEqual(manager.scope_generation,generation)
        self.assertTrue(manager.setup()['connection']['checks']['collecting'])
        self.assertEqual(manager.children_started,1)
        self.assertEqual(len(manager.messages('private_test')['messages']),1)
        self.assertEqual(original,{path:path.read_bytes() for path in paths})

    def test_running_disconnect_and_new_process_restore_original_account_and_nonce(self):
        manager=self.waiting_manager();self.detect_process(manager,logged_in=True)
        manager.request_start(expected_generation=manager.scope_generation);manager.tick()
        baseline=manager.child.store.snapshot()['baseline_completed_at']
        generation=manager.scope_generation;first=manager.child
        self.process_result={'state':'not_running','pid':None,'creation_time':None,'reason':'windows_process_not_running'}
        self.clock_value+=4;manager.tick()
        self.assertEqual(first.poll(),0)
        self.assertFalse(manager.setup()['connection']['checks']['collecting'])
        self.assertEqual(manager.setup()['connection']['state'],'waiting_for_wechat')
        self.assertTrue(manager.setup()['connection']['can_read_saved'])
        self.assertNotEqual(manager.scope_generation,generation)
        self.detect_process(manager,logged_in=True,pid=444,birth=555)
        self.assertTrue(manager.setup()['connection']['checks']['collecting'])
        self.assertEqual((manager.pid,manager.creation_time),(444,555))
        self.assertEqual(manager.child.store.snapshot()['baseline_completed_at'],baseline)
        self.assertEqual(manager.children_started,2)
        with self.assertRaises(WebDataError):manager.messages('private_test',expected_generation=generation)

    def test_waiting_wrong_account_hides_saved_domain_and_never_starts(self):
        manager=self.waiting_manager()
        self.process_result={'state':'ready','pid':222,'creation_time':333,'reason':'windows_process_candidate_ready'}
        self.probe_result={'state':'mismatch','matched':False,'reason':'windows_account_changed_or_ambiguous'}
        self.clock_value+=4;manager.tick()
        connection=manager.setup()['connection']
        self.assertEqual(connection['state'],'wrong_account')
        self.assertFalse(connection['can_read_saved'])
        self.assertFalse(connection['can_start'])
        self.assertEqual(manager.setup()['profiles'],[])
        self.assertEqual(manager.children_started,0)
        blocked_generation=manager.scope_generation
        with self.assertRaises(ValueError):manager.restore_profile('legacy',expected_generation=blocked_generation)
        self.probe_result={'state':'matched','matched':True,'reason':'windows_account_open_database_matched'}
        self.clock_value+=4;manager.tick()
        self.assertNotEqual(manager.scope_generation,blocked_generation)
        with self.assertRaises(WebDataError):manager.messages('private_test',expected_generation=blocked_generation)

    def test_explicit_readonly_cannot_recheck_start_or_turn_into_waiting_mode(self):
        manager=self.manager(read_only=True,pid=None,creation_time=None,
            process_detector=lambda:self.fail('explicit readonly must not detect process'))
        connection=manager.setup()['connection']
        self.assertEqual(connection['state'],'explicit_read_only')
        self.assertEqual(connection['actions'],[])
        with self.assertRaises(ValueError):manager.request_recheck(expected_generation=manager.scope_generation)
        with self.assertRaises(ValueError):manager.request_start(expected_generation=manager.scope_generation)
        with self.assertRaisesRegex(ValueError,'explicit_read_only_conflict'):
            WindowsWebManager(self.data,self.package,self.output,read_only=True,wait_for_wechat=True)

    def test_waiting_http_recheck_and_start_require_scope_origin_and_login_proof(self):
        manager=self.waiting_manager();static=self.package/'web';static.mkdir()
        (static/'index.html').write_text('<!doctype html>synthetic')
        (static/'connect.html').write_text('<!doctype html>synthetic connect')
        server=make_windows_server(manager,static)
        worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);worker.start()
        try:
            def request(method,path,value=None,**extra):
                headers={'Cookie':'chatlens_session='+server.token,'X-ChatLens-Scope':manager.scope_generation,
                         'Origin':server.base_url,'Content-Type':'application/json'}
                headers.update(extra)
                connection=http.client.HTTPConnection(*server.server_address,timeout=2)
                connection.request(method,path,body=None if value is None else json.dumps(value),headers=headers)
                response=connection.getresponse();result=json.loads(response.read());status=response.status;connection.close()
                return status,result
            self.assertIn('/connect.html#token=',server.launch_url)
            self.assertEqual(request('POST','/api/setup/recheck',{},Origin='https://invalid.example')[0],403)
            self.assertEqual(request('POST','/api/setup/recheck',{},**{'X-ChatLens-Scope':'stale'})[0],409)
            self.assertEqual(request('POST','/api/setup/recheck',{'unexpected':True})[0],400)
            self.assertEqual(request('POST','/api/setup/start',{})[0],503)
            self.assertEqual(request('POST','/api/setup/recheck',{})[0],200)
            manager.tick()
            self.assertEqual(request('GET','/api/setup')[1]['connection']['state'],'waiting_for_wechat')
            self.detect_process(manager,logged_in=True)
            old=manager.scope_generation
            self.assertEqual(request('POST','/api/setup/start',{})[0],200)
            manager.tick()
            self.assertEqual(request('GET','/api/messages?conversation=private_test',**{'X-ChatLens-Scope':old})[0],409)
            self.assertEqual(request('GET','/api/messages?conversation=private_test')[0],200)
            self.assertTrue(request('GET','/api/setup')[1]['connection']['checks']['collecting'])
        finally:
            server.shutdown();server.server_close();worker.join(timeout=2)

    def test_production_waiting_runtime_starts_after_login_with_newly_detected_pid(self):
        static=self.package/'web';static.mkdir()
        (static/'index.html').write_text('<!doctype html>Synthetic')
        (static/'connect.html').write_text('<!doctype html>Synthetic connect')
        self.process_result={'state':'not_running','pid':None,'creation_time':None,'reason':'windows_process_not_running'}
        self.probe_result={'state':'unknown','matched':False,'reason':'windows_account_open_database_unavailable'}
        owner=[];commands=[];opened=[];steps=[]
        def factory(data,package,output,**options):
            options['process_detector']=lambda:dict(self.process_result)
            options['account_probe_factory']=lambda *_:lambda:dict(self.probe_result)
            options['clock']=lambda:self.clock_value
            manager=WindowsWebManager(data,package,output,**options)
            owner.append(manager);self.managers.append(manager)
            return manager
        def child(command,**options):
            commands.append(command)
            return self.start_child(Path(command[command.index('--data-dir')+1]),Path(command[command.index('--output')+1]))
        def progress(shutdown):
            manager=owner[0];steps.append(manager.setup()['connection']['state'])
            if len(steps)==1:
                self.assertEqual(steps[-1],'waiting_for_wechat')
                self.process_result={'state':'ready','pid':777,'creation_time':888,'reason':'windows_process_candidate_ready'}
                self.clock_value+=4
            elif len(steps)==2:
                self.assertEqual(steps[-1],'account_unverified')
                self.assertEqual(commands,[])
                self.probe_result={'state':'matched','matched':True,'reason':'windows_account_open_database_matched'}
                manager.request_recheck(expected_generation=manager.scope_generation)
            elif len(steps)==3:
                self.assertEqual(steps[-1],'connected')
                self.assertEqual(commands,[])
                manager.request_start(expected_generation=manager.scope_generation)
            else:
                self.assertTrue(manager.setup()['connection']['checks']['collecting'])
                shutdown.set()
        with patch.object(web_runtime,'stop_collection',side_effect=self.stop_child),redirect_stdout(io.StringIO()):
            result=web_runtime.run(self.data,self.root/'wait-runtime',package=self.package,
                no_open=False,open_browser=lambda url:opened.append(url) or True,popen_factory=child,
                wait=progress,manage_scopes=True,wait_for_wechat=True,manager_factory=factory)
        self.assertEqual(result['status'],'通过',result)
        self.assertTrue(result['wait_for_wechat'])
        self.assertFalse(result['read_only'])
        self.assertIn('/connect.html#token=',opened[0])
        self.assertEqual(commands[0][commands[0].index('--pid')+1],'777')
        self.assertEqual(commands[0][commands[0].index('--creation-time')+1],'888')
        self.assertEqual(len(commands),1)
        self.assertEqual(result['cloud_calls'],0)
        self.assertTrue(result['server_closed'])
        self.assertNotIn(str(self.root),json.dumps(result))
        self.assertTrue(result['reader_readiness_verified'])
        self.assertEqual(result['collector_stage_before_close'],'collecting')
        self.assertEqual(result['collection_setup_status'],'通过')

    def test_wait_runtime_final_report_requires_readiness_before_close(self):
        static=self.package/'web';static.mkdir()
        (static/'index.html').write_text('<!doctype html>Synthetic')
        (static/'connect.html').write_text('<!doctype html>Synthetic connect')
        for phase,expected in [('starting',False),('absent',False),('paused',True)]:
            with self.subTest(phase=phase):
                owners=[]
                def factory(data,package,output,**options):
                    options['process_detector']=lambda:{'state':'ready','pid':777,'creation_time':888,'reason':'windows_process_candidate_ready'}
                    options['account_probe_factory']=lambda *_:lambda:{'state':'matched','matched':True,'reason':'windows_account_open_database_matched'}
                    options['clock']=lambda:self.clock_value
                    manager=WindowsWebManager(data,package,output,**options)
                    owners.append(manager);self.managers.append(manager)
                    return manager
                def child(command,**options):
                    output=Path(command[command.index('--output')+1])
                    value=self.start_child(Path(command[command.index('--data-dir')+1]),output)
                    if phase=='absent':(output/'status.json').unlink()
                    else:collector_service.Heartbeat(output).emit(phase,1,0,True)
                    return value
                def progress(shutdown):
                    manager=owners[0]
                    if manager.child is None:manager.request_start(expected_generation=manager.scope_generation)
                    else:shutdown.set()
                with patch.object(web_runtime,'stop_collection',side_effect=self.stop_child),redirect_stdout(io.StringIO()):
                    result=web_runtime.run(self.data,self.root/('report-'+phase),package=self.package,
                        no_open=True,popen_factory=child,wait=progress,manage_scopes=True,wait_for_wechat=True,manager_factory=factory)
                self.assertEqual(result['status'],'通过',result)
                self.assertEqual(result['reader_readiness_verified'],expected)
                self.assertEqual(result['collector_stage_before_close'],'starting' if phase=='absent' else phase)
                self.assertEqual(result['collection_setup_status'],'通过' if expected else '未验证')

    def start_waiting_collection(self):
        manager=self.waiting_manager();self.detect_process(manager,logged_in=True)
        manager.request_start(expected_generation=manager.scope_generation);manager.tick()
        self.assertTrue(manager.setup()['connection']['checks']['collecting'])
        return manager

    def test_owned_starting_heartbeat_does_not_report_collecting_from_saved_metadata(self):
        manager=self.start_waiting_collection()
        collector_service.Heartbeat(manager.child_output).emit('starting',0)
        with manager.provider._open() as (_,checkpoint):binding=checkpoint['binding_digest']
        WebMetadata(manager.data_dir/'private',binding,'2026-11-01T00:00:00+00:00').status('stopped',1,0,True)
        connection=manager.setup()['connection']
        self.assertTrue(connection['collector_running'])
        self.assertEqual(connection['collector_stage'],'starting')
        self.assertFalse(connection['checks']['collecting'])
        self.assertFalse(connection['can_start'])
        self.assertEqual(manager.status()['state'],'starting')
        collector_service.Heartbeat(manager.child_output).emit('collecting',1,0,True)
        self.assertTrue(manager.setup()['connection']['checks']['collecting'])
        self.assertEqual(manager.status()['state'],'collecting')

    def test_owned_paused_heartbeat_cannot_start_a_second_collector(self):
        manager=self.start_waiting_collection();child=manager.child
        collector_service.Heartbeat(manager.child_output).emit('paused',1,0,True)
        connection=manager.setup()['connection']
        self.assertTrue(connection['collector_running'])
        self.assertEqual(connection['collector_stage'],'paused')
        self.assertFalse(connection['checks']['collecting'])
        self.assertFalse(connection['can_start'])
        with self.assertRaises(ValueError):manager.request_start(expected_generation=manager.scope_generation)
        self.assertIs(manager.child,child)
        self.assertEqual(manager.status()['state'],'paused')
        self.assertEqual(manager.children_started,1)

    def test_only_fresh_current_owned_heartbeat_establishes_collection_readiness(self):
        manager=self.start_waiting_collection()
        own=manager.child_output/'status.json';own.unlink()
        unrelated=self.output/'synthetic-old-run';unrelated.mkdir()
        collector_service.Heartbeat(unrelated).emit('collecting',99,0,True)
        self.assertEqual(manager.setup()['connection']['collector_stage'],'starting')
        self.assertFalse(manager.setup()['connection']['checks']['collecting'])
        collector_service.Heartbeat(manager.child_output).emit('collecting',1,0,True)
        document=json.loads(own.read_text());document['recorded_at_utc']='2000-01-01T00:00:00+00:00'
        collector_service.write_public(own,document)
        self.assertEqual(manager.setup()['connection']['collector_stage'],'stale')
        self.assertFalse(manager.setup()['connection']['checks']['collecting'])
        self.assertFalse(manager.setup()['connection']['can_start'])
        self.assertEqual(manager.status()['state'],'stale')
        document['recorded_at_utc']='2999-01-01T00:00:00+00:00';collector_service.write_public(own,document)
        self.assertEqual(manager.status()['state'],'stale')


if __name__=='__main__': unittest.main()
