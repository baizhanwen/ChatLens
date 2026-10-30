"""Client-isolated, explicitly limited visible-window adapters.

No adapter decrypts a database or sends messages. Native text reading is opt-in.
Every event is an observed fragment, not a platform message: sender and original
sent time remain unknown. Consumers must preserve timestamp_quality and scope.
"""
import difflib
import hashlib
import json
import os
import platform as host_platform
import queue
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NATIVE_DIR=Path(os.environ.get('CHATLENS_NATIVE_DIR',str(ROOT/'scripts/native')))
VERSION = '0.1.0'
CAPABILITY_KEYS = ['history','current_conversation','background','multi_conversation','attachments','edits','recalls']

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def _native_command(client, mode='metadata', window_id=None, system=None):
    system = system or host_platform.system()
    if system == 'Darwin':
        probe_binary=NATIVE_DIR/'bin/macos-probe'
        cmd = [str(probe_binary),client] if probe_binary.exists() else ['swift', str(NATIVE_DIR/'macos_probe.swift'), client]
        if mode == 'capture': cmd.append('--capture')
        return cmd
    if system == 'Windows':
        cmd=['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(NATIVE_DIR/'windows_probe.ps1'),'-Platform',client]
        if mode == 'capture': cmd.append('-Capture')
        if window_id is not None: cmd += ['-WindowHandle',str(window_id)]
        return cmd
    raise RuntimeError('unsupported_host_os')

def native_probe(client, mode='metadata', window_id=None):
    p = subprocess.run(_native_command(client,mode,window_id),capture_output=True,text=True,timeout=45)
    if p.returncode: raise RuntimeError('native_probe_failed')
    return json.loads(p.stdout)

class FragmentTracker:
    """Overlap by visible occurrence, never collapse equal strings globally."""
    def __init__(self, cursor=0):
        self.previous=[]
        self.cursor=cursor
    def update(self, fragments):
        texts=[x.get('identity',x['text']) if isinstance(x,dict) else str(x) for x in fragments]
        if texts == self.previous: return []
        overlap=0
        for size in range(min(len(self.previous),len(texts)),0,-1):
            if self.previous[-size:]==texts[:size]:
                overlap=size
                break
        matcher=difflib.SequenceMatcher(a=self.previous,b=texts,autojunk=False)
        added=[]
        for tag,a0,a1,b0,b1 in ([('insert',0,0,overlap,len(texts))] if overlap else matcher.get_opcodes()):
            if tag in ('insert','replace'):
                for i in range(b0,b1):
                    self.cursor+=1
                    fragment=fragments[i] if isinstance(fragments[i],dict) else {'text':str(fragments[i]),'confidence':1.0}
                    added.append((self.cursor,i,fragment))
        self.previous=texts
        return added

class VisibleWindowAdapter:
    def __init__(self, client, system=None, reader=None):
        if client not in ('wechat','qq'): raise ValueError('unsupported_client')
        self.client=client
        self.system=system or host_platform.system()
        self.reader=reader or native_probe
        self._stop=threading.Event()
        self._thread=None
        self._events=queue.Queue(maxsize=10000)
        self._state={'state':'stopped','status':'未验证','adapter_version':VERSION,'platform':client,'os':self.system,'capture_mode':'visible_window','scope':'指定窗口可见文本片段；非完整消息、非全会话实时接入','last_success_at':None,'gaps':[],'failure_count':0,'cursor':0}
        self._tracker=FragmentTracker()
        self._last_probe=None
        self._options={}

    def probe(self):
        if self.system != host_platform.system() or self.system not in ('Darwin','Windows'):
            return {'status':'未验证','reason':'target_system_not_available','platform':self.client,'os':self.system,'capabilities':{k:{'status':'未验证','reason':'real_target_client_required'} for k in CAPABILITY_KEYS}}
        try:
            native=self.reader(self.client)
            self._last_probe=native
            reason=None
            if not native.get('running'): reason='client_not_running'
            elif native.get('screen_locked'): reason='screen_locked'
            elif self.system == 'Darwin' and not native.get('accessibility_trusted'): reason='accessibility_permission_missing'
            elif not native.get('windows'): reason='no_accessible_window'
            available = reason is None
            capabilities={k:{'status':'不支持','reason':'visible_fragments_only'} for k in CAPABILITY_KEYS}
            capabilities['current_conversation']={'status':'受限','reason':reason or 'sender_time_and_message_mapping_unverified'}
            return {'status':'受限','reason':reason or 'visible_window_only','can_read_fragments':available,'platform':self.client,'os':self.system,'native':native,'capabilities':capabilities}
        except Exception as e:
            return {'status':'受限','reason':str(e) if str(e) in ('unsupported_host_os','native_probe_failed') else type(e).__name__,'platform':self.client,'os':self.system,'capabilities':{k:{'status':'未验证','reason':'probe_failed'} for k in CAPABILITY_KEYS}}

    def start(self, account_id=None, conversation_id=None, window_index=None, window_id=None, capture_mode='accessibility', poll_seconds=2, acknowledged_scope=False, on_event=None, cursor=0, **kwargs):
        if not account_id or not conversation_id or not acknowledged_scope:
            raise ValueError('explicit_account_conversation_and_scope_acknowledgement_required')
        if window_index is None and window_id is None:
            raise ValueError('explicit_window_selection_required')
        if capture_mode not in ('accessibility','ocr'): raise ValueError('unsupported_capture_mode')
        if capture_mode == 'ocr' and self.system != 'Darwin':
            return self._pause('windows_ocr_not_implemented')
        if self._thread and self._thread.is_alive(): return self.health()
        self._options=dict(account_id=account_id,conversation_id=conversation_id,window_index=window_index,window_id=window_id,capture_mode=capture_mode,on_event=on_event)
        self._tracker=FragmentTracker(cursor)
        self._state.update(cursor=cursor, failure_count=0,capture_mode='ocr_visible_fragment' if capture_mode=='ocr' else 'accessibility_visible_fragment')
        self._stop.clear()
        self._thread=threading.Thread(target=self._run,args=(max(1,float(poll_seconds)),),daemon=True)
        self._state.update(state='running',status='受限')
        self._thread.start()
        return self.health()

    def _pause(self, reason):
        self._state.update(state='paused',status='受限',reason=reason)
        if not self._state['gaps'] or self._state['gaps'][-1]['reason'] != reason:
            self._state['gaps'].append({'at':utcnow(),'reason':reason})
        self._state['gaps']=self._state['gaps'][-100:]
        return self.health()

    def _read_fragments(self):
        o=self._options
        native=self.reader(self.client,'capture',o.get('window_id'))
        if not native.get('running'): raise RuntimeError('client_not_running')
        if native.get('screen_locked'): raise RuntimeError('screen_locked')
        # Current contract is conservative: background/hidden windows are gaps.
        if not native.get('frontmost',any(x.get('frontmost') for x in native.get('windows',[]))): raise RuntimeError('window_not_foreground')
        if o['capture_mode']=='ocr':
            if not native.get('screen_capture_permission'): raise RuntimeError('screen_capture_permission_missing')
            allowed={int(x['window_id']) for x in native.get('onscreen_windows',[])}
            if int(o.get('window_id') or 0) not in allowed: raise RuntimeError('selected_window_not_visible')
            return self._ocr(o['window_id'])
        if self.system=='Darwin' and not native.get('accessibility_trusted'): raise RuntimeError('accessibility_permission_missing')
        windows=native.get('windows',[])
        if self.system=='Windows' and o.get('window_id') is not None:
            windows=[w for w in windows if int(w.get('window_id',0))==int(o['window_id'])]
            index=0
        else: index=int(o.get('window_index') or 0)
        if index>=len(windows): raise RuntimeError('selected_window_unavailable')
        w=windows[index]
        if w.get('minimized'): raise RuntimeError('window_minimized')
        fragments=w.get('fragments',[])
        if not fragments: raise RuntimeError('no_readable_text')
        return [{'text':x,'confidence':1.0} if isinstance(x,str) else x for x in fragments]

    def _ocr(self, window_id):
        with tempfile.TemporaryDirectory(prefix='chatlens-ocr-') as d:
            os.chmod(d,0o700)
            p=Path(d)/'window.png'
            r=subprocess.run(['screencapture','-x','-o','-l',str(window_id),str(p)],capture_output=True,timeout=15)
            if r.returncode: raise RuntimeError('window_capture_failed')
            os.chmod(p,0o600)
            ocr_binary=NATIVE_DIR/'bin/macos-ocr'
            cmd=[str(ocr_binary),str(p),'--capture'] if ocr_binary.exists() else ['swift',str(NATIVE_DIR/'macos_ocr.swift'),str(p),'--capture']
            r=subprocess.run(cmd,capture_output=True,text=True,timeout=45)
            if r.returncode: raise RuntimeError('ocr_failed')
            return json.loads(r.stdout).get('fragments',[])

    def _run(self, poll_seconds):
        while not self._stop.is_set():
            old_previous=list(self._tracker.previous)
            old_cursor=self._tracker.cursor
            try:
                fragments=self._read_fragments()
                self._state.update(state='running',status='受限',failure_count=0,reason='visible_fragments_only')
                for cursor,index,fragment in self._tracker.update(fragments):
                    now=utcnow()
                    o=self._options
                    event={'id':None,'platform':self.client,'account_id':o['account_id'],'conversation_id':o['conversation_id'],'sender_id':'unknown','source_id':'visible:%s:%s:%s'%(o['account_id'],o['conversation_id'],cursor),'sent_at':now,'observed_at':now,'type':'visible_fragment','content':fragment['text'],'revision':1,'deleted':False,'capture_mode':self._state['capture_mode'],'metadata':{'timestamp_quality':'observed_only_not_original','sender_quality':'unknown','scope':'visible_window_fragment_not_complete_message','confidence':fragment.get('confidence',1.0),'visible_index':index,'adapter_version':VERSION,'original_timestamp':None}}
                    # External callback must atomically persist event+cursor. Cursor advances only on success.
                    if o.get('on_event'): o['on_event'](event,cursor)
                    if not o.get('on_event'): self._events.put_nowait(event)
                    self._state['cursor']=cursor
                    self._state['last_success_at']=now
            except Exception as e:
                self._tracker.previous=old_previous
                self._tracker.cursor=old_cursor
                # Never include exception strings from native tools; they may contain private text.
                known={'client_not_running','screen_locked','window_not_foreground','screen_capture_permission_missing','selected_window_not_visible','accessibility_permission_missing','selected_window_unavailable','window_minimized','no_readable_text','window_capture_failed','ocr_failed'}
                self._pause(str(e) if str(e) in known else type(e).__name__)
                self._state['failure_count']+=1
                if self._state['failure_count']>=3:
                    self._state['state']='restricted'
                    self._stop.set()
                    break
            self._stop.wait(poll_seconds)

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread(): self._thread.join(timeout=2)
        self._state['state']='stopped'
        return self.health()

    def health(self):
        return dict(self._state)

    def events(self):
        while True:
            try: yield self._events.get_nowait()
            except queue.Empty: return

class MacOSWeChatAdapter(VisibleWindowAdapter):
    def __init__(self, **kwargs): super().__init__('wechat','Darwin',**kwargs)
class MacOSQQAdapter(VisibleWindowAdapter):
    def __init__(self, **kwargs): super().__init__('qq','Darwin',**kwargs)
class WindowsWeChatAdapter(VisibleWindowAdapter):
    def __init__(self, **kwargs): super().__init__('wechat','Windows',**kwargs)
class WindowsQQAdapter(VisibleWindowAdapter):
    def __init__(self, **kwargs): super().__init__('qq','Windows',**kwargs)

ADAPTERS={'macos-wechat':MacOSWeChatAdapter,'macos-qq':MacOSQQAdapter,'windows-wechat':WindowsWeChatAdapter,'windows-qq':WindowsQQAdapter}

def create_adapter(name):
    if name not in ADAPTERS: raise ValueError('unknown_adapter')
    return ADAPTERS[name]()

class TriggerPolicy:
    """Thread-safe debounced events, bounded max-wait, and a strict cooldown."""
    def __init__(self, debounce_seconds=1, cooldown_seconds=10, clock=None, max_wait_seconds=5):
        self.clock=clock or time.monotonic
        self.debounce=max(0,float(debounce_seconds))
        self.cooldown=max(0,float(cooldown_seconds))
        self.max_wait=max(self.debounce,float(max_wait_seconds))
        self.first_trigger=None
        self.pending=False
        self.last_trigger=None
        self.last_capture=None
        self.reason=None
        self.trigger_count=0
        self._lock=threading.RLock()
    def trigger(self,reason):
        with self._lock:
            self.last_trigger=self.clock()
            if not self.pending:self.first_trigger=self.last_trigger
            self.pending=True
            self.reason=str(reason)[:80]
            self.trigger_count+=1
    def due_at(self):
        with self._lock:
            if not self.pending:return None
            stable_or_max=min(self.last_trigger+self.debounce,self.first_trigger+self.max_wait)
            return max(stable_or_max,(self.last_capture+self.cooldown) if self.last_capture is not None else float('-inf'))
    def ready(self):
        with self._lock:return self.pending and self.clock()>=self.due_at()
    def consume(self):
        with self._lock:
            if not self.ready():return False
            self.pending=False
            self.first_trigger=None
            self.last_capture=self.clock()
            return True

class NativeWindowWatcher:
    """Opt-in selected-window local pixel-change stream. Never persists frames."""
    def __init__(self,system,window_id,roi,on_event):
        self.system=system;self.window_id=window_id;self.roi=roi;self.on_event=on_event
        self.process=None;self.thread=None;self.closed=threading.Event()
    def start(self):
        if self.system!='Darwin':
            self.on_event({'status':'restricted','reason':'native_change_watcher_not_implemented_on_target'})
            return
        binary=NATIVE_DIR/'bin/macos-watch'
        if not binary.exists():
            self.on_event({'status':'restricted','reason':'native_change_watcher_not_built'})
            return
        self.process=subprocess.Popen([str(binary),'--watch',str(self.window_id),'--roi',','.join(str(x) for x in self.roi)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1)
        def read():
            try:
                for line in self.process.stdout:
                    if self.closed.is_set():break
                    try:
                        event=json.loads(line)
                        # Only a controlled metadata schema is consumed, never images/text.
                        safe={k:event[k] for k in ('status','reason','frames','changes','roi','image_saved','model_called') if k in event}
                        self.on_event(safe)
                    except (ValueError,TypeError):continue
            finally:
                if not self.closed.is_set():self.on_event({'status':'restricted','reason':'native_change_watcher_exited'})
        self.thread=threading.Thread(target=read,daemon=True);self.thread.start()
    def stop(self):
        self.closed.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:self.process.kill()
        if self.thread and self.thread is not threading.current_thread():self.thread.join(timeout=1)

class WindowVisionAdapter(VisibleWindowAdapter):
    """Main capture route: selected window image -> configured VLM -> source events.

    AX/UIA is used only to gate window state. OCR is a separately labeled fallback.
    No screenshot or VLM request is attempted without explicit start configuration.
    """
    def __init__(self, client, system=None, reader=None, extractor=None, capturer=None, watcher_factory=None, cropper=None, title_verifier=None):
        super().__init__(client,system,reader)
        self.extractor=extractor
        self.capturer=capturer
        self.cropper=cropper
        self.title_verifier=title_verifier
        self.watcher_factory=watcher_factory or NativeWindowWatcher
        self._watcher=None
        self._state.update(capture_mode='vision_window',scope='指定窗口视觉模型识别；可见内容受限，不保证完整历史、后台、多会话或撤回')
        self._last_image_hash=None
        self._wake=threading.Event()
        self._policy=TriggerPolicy()
        self._state.update(trigger_count=0,capture_count=0,model_calls=0,skipped_unchanged=0,last_trigger_reason=None,next_allowed_at=None,trigger_mode='event',periodic_fallback_seconds=0,content_change_observer='macOS本地ROI像素变化；需显式启用关注模式'
          ,watch_changes=False,monitor_state='disabled',monitor_frames=0,monitor_changes=0,scroll_idle_observer='未验证：仅交付trigger接口')

    def probe(self):
        result=super().probe()
        native=result.get('native',{})
        if self.system=='Darwin' and native.get('running'):
            reason='vision_model_and_explicit_window_required'
            if not native.get('screen_capture_permission'): reason='screen_capture_permission_missing'
            elif not native.get('onscreen_windows'): reason='no_visible_target_window'
            result.update(status='受限',reason=reason,can_read_fragments=False,capture_route='window_screenshot_to_vision_model',vision_available=False,window_candidates=native.get('onscreen_windows',[]))
        else:
            result['capture_route']='window_screenshot_to_vision_model'
            result['vision_available']=False
        return result

    def start(self, account_id=None, conversation_id=None, window_id=None, model_config=None, expected_conversation=None, acknowledged_scope=False, poll_seconds=3, on_event=None, checkpoint=None, debounce_seconds=1, cooldown_seconds=10, periodic_fallback_seconds=0, on_batch=None, watch_changes=False, watch_roi=None, max_wait_seconds=5, vision_crop=None, crop_acknowledged=False, **kwargs):
        if not account_id or not conversation_id or not acknowledged_scope or not expected_conversation:
            raise ValueError('explicit_account_conversation_label_and_scope_required')
        if not window_id: raise ValueError('explicit_window_selection_required')
        config=dict(model_config or {})
        config.setdefault('name',config.get('vision_name',config.get('model')))
        config.setdefault('url',config.get('vision_url',config.get('base_url',config.get('endpoint'))))
        config.setdefault('mode',config.get('vision_mode','disabled'))
        if not (config.get('name') or config.get('vision_name') or config.get('model')) or not (config.get('url') or config.get('vision_url') or config.get('base_url') or config.get('endpoint')):
            return self._pause('vision_model_not_configured')
        # Provider helper enforces cloud scope too; do not call it if not opted in.
        if config.get('mode',config.get('vision_mode'))=='cloud' and not (config.get('cloud_authorized') or config.get('vision_cloud_consent') or config.get('allow_image_upload')):
            return self._pause('vision_image_upload_not_authorized')
        if config.get('mode')=='cloud' and self.system!='Darwin':return self._pause('local_conversation_unconfirmed_before_upload')
        if not crop_acknowledged:return self._pause('vision_crop_not_confirmed')
        crop=list(vision_crop if vision_crop is not None else [0.25,0,0.75,0.85])
        if len(crop)!=4 or any(not isinstance(x,(int,float)) for x in crop) or crop[0]<0 or crop[1]<0 or crop[2]<=0 or crop[3]<=0 or crop[0]+crop[2]>1 or crop[1]+crop[3]>1:
            raise ValueError('invalid_normalized_vision_crop')
        if self.system!=host_platform.system(): return self._pause('target_system_not_available')
        if self._thread and self._thread.is_alive(): return self.health()
        checkpoint=checkpoint or {}
        self._options=dict(account_id=account_id,conversation_id=conversation_id,window_id=int(window_id),model_config=config,expected_conversation=expected_conversation,on_event=on_event,on_batch=on_batch,vision_crop=crop)
        self._tracker=FragmentTracker(int(checkpoint.get('cursor',0)))
        self._tracker.previous=list(checkpoint.get('previous',[]))
        self._state.update(cursor=self._tracker.cursor,failure_count=0,capture_mode='vision_window',state='running',status='受限',reason='visible_window_vision_unverified')
        self._stop.clear()
        self._policy=TriggerPolicy(debounce_seconds,cooldown_seconds,max_wait_seconds=max_wait_seconds)
        self._state['periodic_fallback_seconds']=max(0,float(periodic_fallback_seconds))
        self._state['watch_changes']=bool(watch_changes)
        roi=watch_roi if watch_roi is not None else [0.22,0.12,0.77,0.64]
        if not isinstance(roi,(list,tuple)) or len(roi)!=4 or any(not isinstance(x,(int,float)) for x in roi) or roi[0]<0 or roi[1]<0 or roi[2]<=0 or roi[3]<=0 or roi[0]+roi[2]>1 or roi[1]+roi[3]>1:
            raise ValueError('invalid_normalized_watch_roi')
        self._state['watch_roi']=list(roi)
        self._state['max_wait_seconds']=max_wait_seconds
        if watch_changes:
            def changed(event):
                if self._stop.is_set():return
                self._state['monitor_state']=event.get('status','unknown')
                self._state['monitor_frames']=event.get('frames',self._state['monitor_frames'])
                self._state['monitor_changes']=event.get('changes',self._state['monitor_changes'])
                if event.get('status')=='changed':self.trigger('content_changed')
                elif event.get('status') in ('restricted','paused'):
                    self._pause(event.get('reason','local_change_monitor_unavailable'))
            self._watcher=self.watcher_factory(self.system,int(window_id),roi,changed)
            self._watcher.start()
        self.trigger('manual_start')
        self._thread=threading.Thread(target=self._run_vision,args=(max(1,float(poll_seconds)),),daemon=True)
        self._thread.start()
        return self.health()

    def _capture_selected(self, native=None):
        native=native if native is not None else self.reader(self.client)
        self._validate_window(native)
        o=self._options
        if self.capturer: return self._crop_image(self.capturer(o['window_id']))
        with tempfile.TemporaryDirectory(prefix='chatlens-vision-') as d:
            os.chmod(d,0o700)
            p=Path(d)/'selected-window.png'
            if self.system=='Darwin':
                cmd=['screencapture','-x','-o','-l',str(o['window_id']),str(p)]
            else:
                cmd=['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(NATIVE_DIR/'windows_capture.ps1'),'-WindowHandle',str(o['window_id']),'-Output',str(p),'-Crop',','.join(str(x) for x in o['vision_crop'])]
            r=subprocess.run(cmd,capture_output=True,timeout=20)
            if r.returncode or not p.exists(): raise RuntimeError('window_capture_failed')
            os.chmod(p,0o600)
            return self._crop_image(p.read_bytes()) if self.system=='Darwin' else p.read_bytes()

    def _crop_image(self,data,roi=None):
        roi=self._options['vision_crop'] if roi is None else roi
        if self.cropper:return self.cropper(data,roi)
        binary=NATIVE_DIR/'bin/macos-crop'
        if not binary.exists():raise RuntimeError('native_crop_helper_not_built')
        result=subprocess.run([str(binary),'--roi',','.join(str(x) for x in roi)],input=data,capture_output=True,timeout=15)
        if result.returncode or not result.stdout:raise RuntimeError('image_crop_failed')
        return result.stdout

    def _verify_local_title(self,data,expected):
        """Fail-closed cloud gate: OCR only the top header, never match body text."""
        if self.system!='Darwin':return False
        import unicodedata
        def normalized(value):return ' '.join(unicodedata.normalize('NFKC',value).split())
        target=normalized(expected)
        if not target:return False
        binary=NATIVE_DIR/'bin/macos-ocr'
        if not binary.exists():return False
        try:
            # A separate conservative title-only crop avoids matching names in message body.
            # Layouts with titles outside this fixed header fail closed until adapted.
            header=self._crop_image(data,[0,0,1,0.10])
            with tempfile.TemporaryDirectory(prefix='chatlens-title-gate-') as folder:
                os.chmod(folder,0o700)
                image=Path(folder)/'title-only.png';image.write_bytes(header);os.chmod(image,0o600)
                result=subprocess.run([str(binary),str(image),'--capture'],capture_output=True,text=True,timeout=30)
                if result.returncode:return False
                items=json.loads(result.stdout).get('fragments',[])
            matches=[]
            for item in items:
                if not isinstance(item,dict) or not isinstance(item.get('text'),str):continue
                confidence=item.get('confidence',0)
                if isinstance(confidence,(float,int)) and not isinstance(confidence,bool) and confidence>=0.85 and normalized(item['text'])==target:
                    matches.append(item)
            return len(matches)==1
        except (OSError,ValueError,TypeError,subprocess.TimeoutExpired,RuntimeError):
            return False

    def _validate_window(self,native):
        o=self._options
        if not native.get('running'): raise RuntimeError('client_not_running')
        if native.get('screen_locked'): raise RuntimeError('screen_locked')
        if self.system=='Darwin':
            if not native.get('screen_capture_permission'): raise RuntimeError('screen_capture_permission_missing')
            if not native.get('frontmost'): raise RuntimeError('window_not_foreground')
            allowed=[int(x['window_id']) for x in native.get('onscreen_windows',[])]
            if o['window_id'] not in allowed: raise RuntimeError('selected_window_not_visible')
            if allowed[0]!=o['window_id']:raise RuntimeError('window_not_foreground')
        elif self.system=='Windows':
            windows=[w for w in native.get('windows',[]) if int(w.get('window_id',0))==o['window_id']]
            if not windows: raise RuntimeError('selected_window_unavailable')
            if windows[0].get('minimized'): raise RuntimeError('window_minimized')
            if not windows[0].get('frontmost'): raise RuntimeError('window_not_foreground')
        else: raise RuntimeError('unsupported_host_os')
    def trigger(self,reason='manual'):
        allowed={'manual','manual_start','foreground_transition','content_changed','scroll_stable','idle','periodic_fallback'}
        if reason not in allowed: raise ValueError('unsupported_trigger_reason')
        self._policy.trigger(reason)
        self._wake.set()
        self._state.update(trigger_count=self._policy.trigger_count,last_trigger_reason=reason)
        due=self._policy.due_at()
        self._state['next_allowed_at']=None if due is None else max(0,due-self._policy.clock())
        return self.health()

    def _run_vision(self,poll_seconds):
        was_foreground=None
        last_periodic=time.monotonic()
        while not self._stop.is_set():
            old_previous=list(self._tracker.previous)
            old_cursor=self._tracker.cursor
            try:
                native=self.reader(self.client)
                self._validate_window(native)
                if was_foreground is False: self.trigger('foreground_transition')
                was_foreground=True
                fallback=self._state['periodic_fallback_seconds']
                if fallback and time.monotonic()-last_periodic>=fallback:
                    self.trigger('periodic_fallback');last_periodic=time.monotonic()
                if self._state['failure_count']:
                    self._state.update(state='paused',status='受限',reason=self._state.get('last_error','read_failed'))
                else:
                    self._state.update(state='waiting',status='受限',reason='waiting_for_trigger')
                if not self._policy.consume():
                    due=self._policy.due_at()
                    delay=poll_seconds if due is None else max(1,min(poll_seconds,due-self._policy.clock()))
                    self._wake.wait(delay)
                    self._wake.clear()
                    continue
                self._state.update(next_allowed_at=None)
                if self._stop.is_set():return
                data=self._capture_selected(native)
                self._state['capture_count']+=1
                digest=hashlib.sha256(data).hexdigest()
                if digest!=self._last_image_hash:
                    local_title_verified=False
                    if self._options['model_config'].get('mode')=='cloud':
                        if self._stop.is_set():return
                        verify=self.title_verifier or self._verify_local_title
                        local_title_verified=verify(data,self._options['expected_conversation']) is True
                        if not local_title_verified:raise RuntimeError('local_conversation_unconfirmed_before_upload')
                    if self.extractor is None:
                        try: from .vision import extract_messages
                        except ImportError: from vision import extract_messages
                        extract=extract_messages
                    else: extract=self.extractor
                    now=utcnow();o=self._options
                    if self._stop.is_set():return
                    self._state['model_calls']+=1
                    result=extract(data,o['model_config'],{'expected_conversation':o['expected_conversation'],'account_id':o['account_id'],'conversation_id':o['conversation_id'],'observed_at':now,'crop_known':True,'vision_crop':o['vision_crop'],'local_title_verified':local_title_verified})
                    # stop/delete can finish while a synchronous model request is in flight.
                    # Discard every late result before validation or persistence.
                    if self._stop.is_set():return
                    if result.get('coverage',{}).get('conversation_matches') is not True: raise RuntimeError('conversation_identity_unconfirmed')
                    messages=result.get('messages',[])
                    if not isinstance(messages,list): raise RuntimeError('invalid_vision_output')
                    fragments=[]
                    for message in messages:
                        if not isinstance(message,dict) or not isinstance(message.get('content'),str): raise RuntimeError('invalid_vision_output')
                        fragments.append(dict(message,text=message['content'],identity=json.dumps([message.get('sender_id'),message.get('sent_at'),message.get('type'),message['content']],ensure_ascii=False)))
                    if self._tracker.previous and fragments:
                        identities=[m.get('identity',m['text']) for m in fragments]
                        if not any(x in self._tracker.previous for x in identities):
                            self._state['gaps'].append({'at':utcnow(),'reason':'possible_gap_no_visible_overlap'})
                            self._state['gaps']=self._state['gaps'][-100:]
                    outgoing=[]
                    for cursor,index,m in self._tracker.update(fragments):
                        sender=m.get('sender_id') or 'unknown'
                        sent=m.get('sent_at')
                        event={'platform':self.client,'account_id':o['account_id'],'conversation_id':o['conversation_id'],'sender_id':sender,'source_id':'vision:%s:%s:%s'%(o['account_id'],o['conversation_id'],cursor),'sent_at':sent or now,'observed_at':now,'type':m.get('type') or 'text','content':m['content'],'revision':1,'deleted':False,'capture_mode':'vision_window','metadata':{'timestamp_quality':'vision_read_unverified' if sent else 'observed_only_not_original','original_timestamp':sent,'sender_quality':'vision_read_unverified' if sender!='unknown' else 'unknown','scope':'visible_window_vision_only','confidence':m.get('confidence'),'uncertain':result.get('uncertain',[]),'adapter_version':VERSION,'image_persisted':False,'vision_crop':o['vision_crop'],'crop_confirmed':True,'edits_and_recalls':'unverified_not_inferred'}}
                        outgoing.append((event,cursor))
                    batch_checkpoint={'cursor':self._tracker.cursor,'previous':list(self._tracker.previous),'adapter_version':VERSION}
                    if self._stop.is_set():return
                    if o.get('on_batch'):
                        o['on_batch']([event for event,_ in outgoing],batch_checkpoint)
                    elif o.get('on_event'):
                        for event,cursor in outgoing:o['on_event'](event,cursor)
                    else:
                        for event,_ in outgoing:self._events.put_nowait(event)
                    self._state['cursor']=self._tracker.cursor
                    self._last_image_hash=digest
                    self._state['last_success_at']=now
                else:
                    self._state['skipped_unchanged']+=1
                self._state.update(state='waiting',status='受限',failure_count=0,reason='waiting_for_trigger',last_error=None)
            except Exception as e:
                self._tracker.previous=old_previous
                self._tracker.cursor=old_cursor
                known={'client_not_running','screen_locked','window_not_foreground','screen_capture_permission_missing','selected_window_not_visible','selected_window_unavailable','window_minimized','window_capture_failed','conversation_identity_unconfirmed','invalid_vision_output','unsupported_host_os','native_crop_helper_not_built','image_crop_failed','local_conversation_unconfirmed_before_upload'}
                reason=str(e) if str(e) in known else type(e).__name__
                self._pause(reason)
                self._state['last_error']=reason
                if reason in {'screen_locked','window_not_foreground','selected_window_not_visible','selected_window_unavailable','window_minimized','client_not_running'}:
                    was_foreground=False
                else:
                    self._state['failure_count']+=1
                    if self._state['failure_count']>=3:
                        self._state['state']='restricted';self._stop.set();break
            self._stop.wait(poll_seconds)
        if self._watcher:self._watcher.stop()

    def health(self):
        state=super().health()
        due=self._policy.due_at()
        remaining=None if due is None else max(0,due-self._policy.clock())
        state['next_allowed_at']=None if remaining is None else (datetime.now(timezone.utc)+timedelta(seconds=remaining)).isoformat()
        state['pending_trigger']=self._policy.pending
        return state

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._watcher:self._watcher.stop()
        return super().stop()

    def checkpoint(self):
        return {'cursor':self._state['cursor'],'previous':list(self._tracker.previous),'adapter_version':VERSION}

class MacOSWeChatVisionAdapter(WindowVisionAdapter):
    def __init__(self, **kwargs): super().__init__('wechat','Darwin',**kwargs)
class MacOSQQVisionAdapter(WindowVisionAdapter):
    def __init__(self, **kwargs): super().__init__('qq','Darwin',**kwargs)
class WindowsWeChatVisionAdapter(WindowVisionAdapter):
    def __init__(self, **kwargs): super().__init__('wechat','Windows',**kwargs)
class WindowsQQVisionAdapter(WindowVisionAdapter):
    def __init__(self, **kwargs): super().__init__('qq','Windows',**kwargs)

ADAPTERS.update({'macos-wechat-vision':MacOSWeChatVisionAdapter,'macos-qq-vision':MacOSQQVisionAdapter,'windows-wechat-vision':WindowsWeChatVisionAdapter,'windows-qq-vision':WindowsQQVisionAdapter})

def dock_window(client, window_id, bounds, system=None, runner=None):
    """Move/resize only a validated native client window. Never reparent/embed it."""
    import math
    if client not in ('wechat','qq'):raise ValueError('unsupported_client')
    if isinstance(window_id,bool) or not isinstance(window_id,(int,str)) or not str(window_id).isdigit() or int(window_id)<=0:raise ValueError('explicit_window_id_required')
    if not isinstance(bounds,dict) or any(k not in bounds or isinstance(bounds[k],bool) or not isinstance(bounds[k],(int,float)) or not math.isfinite(bounds[k]) for k in ('x','y','width','height')):raise ValueError('invalid_window_bounds')
    if not 200<=bounds['width']<=10000 or not 150<=bounds['height']<=10000:raise ValueError('invalid_window_bounds')
    system=system or host_platform.system();run=runner or subprocess.run
    if system=='Darwin':
        binary=NATIVE_DIR/'bin/macos-dock'
        if not binary.exists():return {'ok':False,'status':'受限','reason':'native_dock_helper_not_built','changed':False}
        cmd=[str(binary),'--platform',client,'--window-id',str(window_id)]
        for k in ('x','y','width','height'):cmd+=['--'+k,str(bounds[k])]
    elif system=='Windows':
        cmd=['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(NATIVE_DIR/'windows_dock.ps1'),'-Platform',client,'-WindowHandle',str(window_id)]
        for key,arg in [('x','X'),('y','Y'),('width','Width'),('height','Height')]:cmd+=['-'+arg,str(round(bounds[key]))]
    else:return {'ok':False,'status':'不支持','reason':'unsupported_host_os','changed':False}
    try:
        result=run(cmd,capture_output=True,text=True,timeout=15)
        data=json.loads(result.stdout)
        if not isinstance(data,dict) or not isinstance(data.get('ok'),bool):raise ValueError()
        allowed={'ok','status','reason','changed','window_id','platform','original_bounds','new_bounds','verification_scope','chat_content_read','messages_sent','accessibility_trusted'}
        return {k:v for k,v in data.items() if k in allowed}
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return {'ok':False,'status':'受限','reason':'native_dock_failed','changed':False}

def restore_dock(record, system=None, runner=None):
    if not isinstance(record,dict) or not record.get('original_bounds') or not record.get('platform') or not record.get('window_id'):
        return {'ok':False,'status':'受限','reason':'no_original_window_bounds','changed':False}
    return dock_window(record['platform'],record['window_id'],record['original_bounds'],system=system,runner=runner)
