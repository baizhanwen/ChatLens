import importlib.util
import platform
import time
import unittest
from pathlib import Path
from unittest.mock import patch

path=Path(__file__).resolve().parents[1]/'services/worker/adapters.py'
spec=importlib.util.spec_from_file_location('chatlens_adapters',path)
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)

class AdapterTests(unittest.TestCase):
    def test_occurrences_are_not_content_deduped(self):
        tracker=a.FragmentTracker()
        first=tracker.update(['你好','你好','明天交付'])
        self.assertEqual([x[0] for x in first],[1,2,3])
        self.assertEqual(tracker.update(['你好','你好','明天交付']),[])
        self.assertEqual([x[2]['text'] for x in tracker.update(['明天交付','你好','你好'])],['你好','你好'])
    def test_vision_identity_separates_sender(self):
        tracker=a.FragmentTracker()
        self.assertEqual(len(tracker.update([{'text':'好','identity':'alice:好'}])),1)
        self.assertEqual(len(tracker.update([{'text':'好','identity':'bob:好'}])),1)
    def test_four_isolated_factories(self):
        for name in ['macos-wechat','macos-qq','windows-wechat','windows-qq','macos-wechat-vision','macos-qq-vision','windows-wechat-vision','windows-qq-vision']:
            self.assertEqual(a.create_adapter(name).health()['status'],'未验证')
        with self.assertRaises(ValueError): a.create_adapter('fake')
    def test_unavailable_os_not_fake_success(self):
        other='Windows' if platform.system()!='Windows' else 'Darwin'
        result=a.VisibleWindowAdapter('qq',other).probe()
        self.assertEqual(result['status'],'未验证')
        self.assertEqual(result['reason'],'target_system_not_available')
    def test_permission_probe_limited(self):
        result=a.VisibleWindowAdapter('wechat',reader=lambda *_:{'running':True,'accessibility_trusted':False}).probe()
        self.assertEqual(result['status'],'受限')
        self.assertEqual(result['capabilities']['history']['status'],'不支持')
    def test_explicit_scope_required(self):
        adapter=a.WindowVisionAdapter('wechat')
        with self.assertRaises(ValueError): adapter.start()
        with self.assertRaises(ValueError): adapter.start(account_id='a',conversation_id='c',expected_conversation='测试',acknowledged_scope=True,crop_acknowledged=True)
    def test_missing_model_cannot_succeed(self):
        adapter=a.WindowVisionAdapter('wechat')
        result=adapter.start(account_id='a',conversation_id='c',window_id=1,expected_conversation='测试',acknowledged_scope=True,crop_acknowledged=True)
        self.assertEqual(result['reason'],'vision_model_not_configured')
        self.assertEqual(result['state'],'paused')
    def test_cloud_image_requires_separate_authorization(self):
        adapter=a.WindowVisionAdapter('wechat')
        result=adapter.start(account_id='a',conversation_id='c',window_id=1,expected_conversation='测试',acknowledged_scope=True,crop_acknowledged=True,model_config={'mode':'cloud','model':'vision','base_url':'https://invalid'})
        self.assertEqual(result['reason'],'vision_image_upload_not_authorized')
    def test_vision_mock_preserves_unknown_identity_time_and_stops(self):
        def reader(*_):
            return {'running':True,'frontmost':True,'screen_capture_permission':True,'onscreen_windows':[{'window_id':1}],'windows':[{'window_id':1,'frontmost':True}]}
        def extract(data,config,context):
            self.assertEqual(data,b'fake synthetic image')
            return {'messages':[{'content':'合成内容','sender_id':None,'sent_at':None}],'coverage':{'conversation_matches':True},'uncertain':['synthetic']}
        adapter=a.WindowVisionAdapter('wechat',reader=reader,extractor=extract,capturer=lambda _:b'fake synthetic image',cropper=lambda data,roi:data)
        adapter.start(account_id='a',conversation_id='c',window_id=1,expected_conversation='测试',acknowledged_scope=True,crop_acknowledged=True,model_config={'mode':'local','model':'mock','base_url':'http://127.0.0.1'},debounce_seconds=0,cooldown_seconds=0)
        deadline=time.time()+2
        while not adapter.health()['last_success_at'] and time.time()<deadline: time.sleep(.02)
        events=list(adapter.events());adapter.stop()
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['sender_id'],'unknown')
        self.assertEqual(events[0]['metadata']['timestamp_quality'],'observed_only_not_original')
        self.assertEqual(events[0]['capture_mode'],'vision_window')
        self.assertEqual(adapter.health()['status'],'受限')
    def test_conversation_mismatch_creates_gap_not_message(self):
        def reader(*_): return {'running':True,'frontmost':True,'screen_capture_permission':True,'onscreen_windows':[{'window_id':1}],'windows':[{'window_id':1,'frontmost':True}]}
        adapter=a.WindowVisionAdapter('qq',reader=reader,extractor=lambda *_:{'messages':[{'content':'wrong chat'}],'coverage':{'conversation_matches':False}},capturer=lambda _:b'fake',cropper=lambda data,roi:data)
        adapter.start(account_id='a',conversation_id='c',window_id=1,expected_conversation='测试',acknowledged_scope=True,crop_acknowledged=True,model_config={'mode':'local','model':'mock','base_url':'http://127.0.0.1'},debounce_seconds=0,cooldown_seconds=0)
        deadline=time.time()+2
        while not adapter.health()['gaps'] and time.time()<deadline: time.sleep(.02)
        adapter.stop()
        self.assertEqual(list(adapter.events()),[])
        self.assertEqual(adapter.health()['gaps'][0]['reason'],'conversation_identity_unconfirmed')
    def test_lock_pauses_before_image_capture(self):
        adapter=a.WindowVisionAdapter('qq',reader=lambda *_:{'running':True,'screen_locked':True},capturer=lambda _:self.fail('must not capture locked screen'))
        adapter._options={'window_id':1}
        with self.assertRaisesRegex(RuntimeError,'screen_locked'): adapter._capture_selected()
    def test_checkpoint_occurrences_resume(self):
        tracker=a.FragmentTracker(20);tracker.previous=['old']
        self.assertEqual(tracker.update(['old','new'])[0][0],21)

    def test_trigger_debounce_cooldown_mock_clock(self):
        clock=[0.0]
        policy=a.TriggerPolicy(1,10,clock=lambda:clock[0])
        self.assertFalse(policy.ready())
        policy.trigger('manual')
        clock[0]=.5
        policy.trigger('content_changed')
        clock[0]=1.4
        self.assertFalse(policy.consume())
        clock[0]=1.5
        self.assertTrue(policy.consume())
        policy.trigger('manual')
        clock[0]=10
        self.assertFalse(policy.consume())
        clock[0]=11.5
        self.assertTrue(policy.consume())
        self.assertFalse(policy.consume())
        self.assertEqual(policy.trigger_count,3)
    def test_event_mode_no_periodic_capture_and_hash_skips_model(self):
        reader=lambda *_:{'running':True,'frontmost':True,'screen_capture_permission':True,'onscreen_windows':[{'window_id':1}],'windows':[{'window_id':1,'frontmost':True}]}
        adapter=a.WindowVisionAdapter('wechat',reader=reader,extractor=lambda *_:{'messages':[],'coverage':{'conversation_matches':True}},capturer=lambda _:b'identical fake image',cropper=lambda data,roi:data)
        adapter.start(account_id='a',conversation_id='c',window_id=1,expected_conversation='测试',acknowledged_scope=True,crop_acknowledged=True,model_config={'mode':'local','model':'mock','base_url':'http://127.0.0.1'},debounce_seconds=0,cooldown_seconds=0,poll_seconds=1)
        deadline=time.time()+2
        while adapter.health()['capture_count']<1 and time.time()<deadline:time.sleep(.01)
        time.sleep(1.1)
        self.assertEqual(adapter.health()['capture_count'],1)
        adapter.trigger('manual')
        deadline=time.time()+2
        while adapter.health()['capture_count']<2 and time.time()<deadline:time.sleep(.01)
        adapter.stop()
        self.assertEqual(adapter.health()['capture_count'],2)
        self.assertEqual(adapter.health()['model_calls'],1)
        self.assertEqual(adapter.health()['skipped_unchanged'],1)
        self.assertEqual(adapter.health()['periodic_fallback_seconds'],0)

    def test_continuous_change_max_wait_prevents_starvation(self):
        clock=[0.0]
        policy=a.TriggerPolicy(2,0,clock=lambda:clock[0],max_wait_seconds=5)
        policy.trigger('content_changed')
        for i in range(1,10):
            clock[0]=i*.5
            policy.trigger('content_changed')
            self.assertFalse(policy.ready())
        clock[0]=5
        policy.trigger('content_changed')
        self.assertTrue(policy.consume())
    def test_opt_in_watcher_triggers_and_stop_tears_down(self):
        created=[]
        class FakeWatcher:
            def __init__(self,system,window_id,roi,on_event):
                self.callback=on_event;self.stopped=False;created.append(self)
            def start(self):self.callback({'status':'changed','frames':2,'changes':1})
            def stop(self):self.stopped=True
        reader=lambda *_:{'running':True,'frontmost':True,'screen_capture_permission':True,'onscreen_windows':[{'window_id':1}],'windows':[{'window_id':1,'frontmost':True}]}
        adapter=a.WindowVisionAdapter('wechat',reader=reader,extractor=lambda *_:{'messages':[],'coverage':{'conversation_matches':True}},capturer=lambda _:b'synthetic',watcher_factory=FakeWatcher,cropper=lambda data,roi:data)
        adapter.start(account_id='a',conversation_id='c',window_id=1,expected_conversation='测试',acknowledged_scope=True,crop_acknowledged=True,model_config={'mode':'local','name':'mock','url':'http://127.0.0.1'},watch_changes=True,debounce_seconds=0,cooldown_seconds=0)
        adapter.stop()
        self.assertTrue(created[0].stopped)
        self.assertEqual(adapter.health()['monitor_changes'],1)
        self.assertTrue(adapter.health()['watch_changes'])
        self.assertGreaterEqual(adapter.health()['trigger_count'],2)
    def test_vision_batch_rollback_and_checkpoint_resume(self):
        reader=lambda *_:{'running':True,'frontmost':True,'screen_capture_permission':True,'onscreen_windows':[{'window_id':1}],'windows':[{'window_id':1,'frontmost':True}]}
        extraction=lambda *_:{'messages':[{'content':'合成重复'},{'content':'合成重复'}],'coverage':{'conversation_matches':True}}
        committed=[];attempts=[]
        def persist(events,checkpoint):
            attempts.append([m['source_id'] for m in events])
            if len(attempts)==1:raise RuntimeError('synthetic transaction failure')
            committed.append((events,checkpoint))
        adapter=a.WindowVisionAdapter('wechat',reader=reader,extractor=extraction,capturer=lambda _:b'synthetic batch',cropper=lambda data,roi:data)
        options=dict(account_id='a',conversation_id='c',window_id=1,expected_conversation='测试',acknowledged_scope=True,crop_acknowledged=True,model_config={'mode':'local','name':'mock','url':'http://127.0.0.1'},debounce_seconds=0,cooldown_seconds=0,poll_seconds=1)
        adapter.start(**options,on_batch=persist)
        deadline=time.time()+2
        while not attempts and time.time()<deadline:time.sleep(.01)
        self.assertEqual(adapter.health()['cursor'],0)
        adapter.trigger('manual')
        deadline=time.time()+2
        while not committed and time.time()<deadline:time.sleep(.01)
        adapter.stop()
        self.assertEqual(attempts[0],attempts[1])
        self.assertEqual(committed[0][1]['cursor'],2)
        self.assertEqual(len(committed[0][1]['previous']),2)
        restored=[]
        second=a.WindowVisionAdapter('wechat',reader=reader,extractor=extraction,capturer=lambda _:b'synthetic batch',cropper=lambda data,roi:data)
        second.start(**options,checkpoint=committed[0][1],on_batch=lambda ev,cp:restored.append((ev,cp)))
        deadline=time.time()+2
        while not restored and time.time()<deadline:time.sleep(.01)
        second.stop()
        self.assertEqual(restored[0][0],[])
        self.assertEqual(restored[0][1]['cursor'],2)

    def test_crop_must_be_explicitly_confirmed(self):
        adapter=a.WindowVisionAdapter('wechat')
        result=adapter.start(account_id='a',conversation_id='c',window_id=1,expected_conversation='测试',acknowledged_scope=True,model_config={'mode':'local','name':'mock','url':'http://127.0.0.1'})
        self.assertEqual(result['reason'],'vision_crop_not_confirmed')
        self.assertEqual(result['state'],'paused')
    def test_cropped_bytes_only_reach_model(self):
        seen=[]
        reader=lambda *_:{'running':True,'frontmost':True,'screen_capture_permission':True,'onscreen_windows':[{'window_id':1}],'windows':[{'window_id':1,'frontmost':True}]}
        def cropper(raw,roi):
            self.assertEqual(raw,b'full synthetic window with sidebars')
            self.assertEqual(roi,[.3,0,.7,.8])
            return b'cropped synthetic title and messages'
        def extractor(data,config,context):
            seen.append(data)
            self.assertTrue(context['crop_known'])
            self.assertEqual(context['vision_crop'],[.3,0,.7,.8])
            return {'messages':[],'coverage':{'conversation_matches':True}}
        adapter=a.WindowVisionAdapter('wechat',reader=reader,extractor=extractor,capturer=lambda _:b'full synthetic window with sidebars',cropper=cropper)
        adapter.start(account_id='a',conversation_id='c',window_id=1,expected_conversation='测试',acknowledged_scope=True,crop_acknowledged=True,vision_crop=[.3,0,.7,.8],model_config={'mode':'local','name':'mock','url':'http://127.0.0.1'},debounce_seconds=0,cooldown_seconds=0)
        deadline=time.time()+2
        while not seen and time.time()<deadline:time.sleep(.01)
        adapter.stop()
        self.assertEqual(seen,[b'cropped synthetic title and messages'])
    @unittest.skipUnless(platform.system()=='Darwin' and (a.NATIVE_DIR/'bin/macos-crop').exists(),'native macOS crop helper unavailable')
    def test_native_crop_synthetic_pixels(self):
        import subprocess,json
        result=subprocess.run([str(a.NATIVE_DIR/'bin/macos-crop'),'--self-test'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0)
        data=json.loads(result.stdout)
        self.assertEqual(data['status'],'passed')
        self.assertTrue(data['left_private_region_removed'])

    def test_dock_rejects_ambiguous_client_and_invalid_bounds(self):
        with self.assertRaises(ValueError):a.dock_window('other',1,{'x':0,'y':0,'width':600,'height':700})
        with self.assertRaises(ValueError):a.dock_window('wechat',0,{'x':0,'y':0,'width':600,'height':700})
        with self.assertRaises(ValueError):a.dock_window('wechat',1,{'x':0,'y':0,'width':True,'height':700})
    def test_dock_windows_command_is_position_only(self):
        calls=[]
        class Result:stdout='{"ok":true,"status":"真实验证通过","platform":"qq","window_id":22,"original_bounds":{"x":1,"y":2,"width":600,"height":700},"new_bounds":{"x":20,"y":30,"width":650,"height":750},"private_text":"must not escape"}'
        def run(cmd,**kwargs):calls.append(cmd);return Result()
        result=a.dock_window('qq',22,{'x':20,'y':30,'width':650,'height':750},system='Windows',runner=run)
        self.assertTrue(result['ok'])
        self.assertNotIn('private_text',result)
        self.assertIn('windows_dock.ps1',calls[0][calls[0].index('-File')+1])
        self.assertNotIn('SetParent',' '.join(calls[0]))
    def test_restore_uses_saved_bounds(self):
        calls=[]
        class Result:stdout='{"ok":true,"changed":true}'
        result=a.restore_dock({'platform':'wechat','window_id':12,'original_bounds':{'x':1,'y':2,'width':600,'height':700}},system='Windows',runner=lambda cmd,**kw:(calls.append(cmd) or Result()))
        self.assertTrue(result['ok'])
        self.assertEqual(calls[0][calls[0].index('-X')+1],'1')
        self.assertEqual(calls[0][calls[0].index('-Width')+1],'600')
        self.assertEqual(a.restore_dock({})['reason'],'no_original_window_bounds')
    def test_dock_error_output_is_controlled(self):
        class Result:stdout='not json synthetic private text'
        result=a.dock_window('wechat',1,{'x':0,'y':0,'width':600,'height':700},system='Windows',runner=lambda *args,**kwargs:Result())
        self.assertEqual(result,{'ok':False,'status':'受限','reason':'native_dock_failed','changed':False})
    @unittest.skipUnless(platform.system()=='Darwin' and (a.NATIVE_DIR/'bin/macos-dock').exists(),'native macOS dock helper unavailable')
    def test_native_dock_preflight_does_not_move_window(self):
        import subprocess,json
        result=subprocess.run([str(a.NATIVE_DIR/'bin/macos-dock'),'--platform','wechat','--preflight'],capture_output=True,text=True,timeout=10)
        data=json.loads(result.stdout)
        self.assertFalse(data['ok'])
        self.assertFalse(data['changed'])

    def test_slow_extractor_result_after_stop_never_persists(self):
        import threading
        started=threading.Event();release=threading.Event();persisted=[]
        reader=lambda *_:{'running':True,'frontmost':True,'screen_capture_permission':True,'onscreen_windows':[{'window_id':1}],'windows':[{'window_id':1,'frontmost':True}]}
        def slow(*_):
            started.set();release.wait(8)
            return {'messages':[{'content':'late synthetic result'}],'coverage':{'conversation_matches':True}}
        adapter=a.WindowVisionAdapter('wechat',reader=reader,extractor=slow,capturer=lambda _:b'synthetic image',cropper=lambda data,roi:data)
        try:
            adapter.start(account_id='a',conversation_id='c',window_id=1,expected_conversation='测试',acknowledged_scope=True,crop_acknowledged=True,model_config={'mode':'local','name':'mock','url':'http://127.0.0.1'},debounce_seconds=0,cooldown_seconds=0,on_batch=lambda ev,cp:persisted.append((ev,cp)))
            self.assertTrue(started.wait(2))
            adapter.stop()
            self.assertEqual(adapter.health()['state'],'stopped')
            release.set();adapter._thread.join(timeout=2)
            self.assertFalse(adapter._thread.is_alive())
            self.assertEqual(persisted,[])
            self.assertEqual(list(adapter.events()),[])
            self.assertEqual(adapter.health()['state'],'stopped')
        finally:
            release.set();adapter.stop()

    def test_cloud_local_title_gate_blocks_before_model_upload(self):
        seen=[]
        reader=lambda *_:{'running':True,'frontmost':True,'screen_capture_permission':True,'onscreen_windows':[{'window_id':1}],'windows':[{'window_id':1,'frontmost':True}]}
        def verify(data,expected):
            self.assertEqual(data,b'cropped synthetic window')
            self.assertEqual(expected,'授权测试会话')
            seen.append('verified');return False
        adapter=a.WindowVisionAdapter('wechat',reader=reader,extractor=lambda *_:self.fail('must not upload an unconfirmed conversation'),capturer=lambda _:b'cropped synthetic window',cropper=lambda data,roi:data,title_verifier=verify)
        adapter.start(account_id='a',conversation_id='c',window_id=1,expected_conversation='授权测试会话',acknowledged_scope=True,crop_acknowledged=True,model_config={'mode':'cloud','name':'mock','url':'https://synthetic.invalid','cloud_authorized':True},debounce_seconds=0,cooldown_seconds=0)
        deadline=time.time()+2
        while not adapter.health()['gaps'] and time.time()<deadline:time.sleep(.01)
        adapter.stop()
        self.assertEqual(seen,['verified'])
        self.assertEqual(adapter.health()['model_calls'],0)
        self.assertEqual(adapter.health()['gaps'][0]['reason'],'local_conversation_unconfirmed_before_upload')
        self.assertEqual(list(adapter.events()),[])
    def test_cloud_only_receives_local_title_verified_context(self):
        contexts=[]
        reader=lambda *_:{'running':True,'frontmost':True,'screen_capture_permission':True,'onscreen_windows':[{'window_id':1}],'windows':[{'window_id':1,'frontmost':True}]}
        def extract(data,config,context):
            contexts.append(context)
            return {'messages':[],'coverage':{'conversation_matches':True}}
        adapter=a.WindowVisionAdapter('wechat',reader=reader,extractor=extract,capturer=lambda _:b'cropped synthetic window',cropper=lambda data,roi:data,title_verifier=lambda *_:True)
        adapter.start(account_id='a',conversation_id='c',window_id=1,expected_conversation='授权测试会话',acknowledged_scope=True,crop_acknowledged=True,model_config={'mode':'cloud','name':'mock','url':'https://synthetic.invalid','cloud_authorized':True},debounce_seconds=0,cooldown_seconds=0)
        deadline=time.time()+2
        while not contexts and time.time()<deadline:time.sleep(.01)
        adapter.stop()
        self.assertTrue(contexts[0]['local_title_verified'])
        self.assertEqual(adapter.health()['model_calls'],1)

if __name__=='__main__': unittest.main()
