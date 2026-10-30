"""Loopback-only authenticated worker. Never logs message bodies or credentials."""
import argparse
import csv
import hashlib
import io
import json
import multiprocessing
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from .core import Store, VERSION, dumps, now, parse_text, normalize, opaque


def _analysis_child(connection,messages,kind,timezone,question,config):
    try:
        from .analysis import analyze
        result=analyze(messages,kind,timezone=timezone,question=question,model_config=config)
        connection.send({'ok':True,'result':result})
    except Exception as exc:
        safe=str(exc) if isinstance(exc,ValueError) and any(x in str(exc) for x in ('授权','引用','配置','回环','HTTPS','问题','时间','负责人')) else '分析失败，可重试；原文已保留'
        connection.send({'ok':False,'error':safe})
    finally:connection.close()


def bounded_analysis(messages,kind,timezone,question,config,timeout=45):
    """A hung provider/parser cannot monopolize the persistent analysis queue."""
    context=multiprocessing.get_context('spawn')
    receiver,sender=context.Pipe(duplex=False)
    process=context.Process(target=_analysis_child,args=(sender,messages,kind,timezone,question,config),daemon=True)
    process.start();sender.close()
    try:
        if not receiver.poll(max(0.1,min(120,float(timeout)))):raise TimeoutError('分析任务超时，可重试')
        payload=receiver.recv()
        if not payload.get('ok'):raise ValueError(payload.get('error','分析失败'))
        return payload['result']
    finally:
        receiver.close();process.join(timeout=.2)
        if process.is_alive():process.terminate();process.join(timeout=1)
        if process.is_alive():process.kill();process.join(timeout=1)

class Runtime:
    def __init__(self,store):
        self.store=store; self.closed=threading.Event();self.replay_stop=threading.Event();self.replay_thread=None
        self.replay={'running':False,'cursor':0,'total':0,'mode':'replay','demo':True}
        self.processor=threading.Thread(target=self.process_jobs,daemon=True);self.processor.start()
        self.last_error=None
        self.capture=None
        self.capture_options=None
        self.dock_record=None

    def dock(self,p):
        from .adapters import dock_window
        result=dock_window(p.get('platform','wechat'),p.get('window_id'),p.get('bounds'))
        if result.get('ok'):self.dock_record=result
        return result

    def undock(self,p=None):
        from .adapters import restore_dock
        if not self.dock_record:return {'ok':False,'status':'受限','changed':False,'reason':'no_original_window_bounds'}
        result=restore_dock(self.dock_record)
        if result.get('ok'):self.dock_record=None
        return result

    def capture_health(self):
        if not self.capture:return {'state':'stopped','status':'未验证','capture_mode':'vision_window','trigger_mode':'event','reason':'尚未启动指定窗口视觉采集','gaps':[]}
        state=self.capture.health()
        if self.capture_options and state.get('gaps'):
            cid=opaque(self.capture_options['platform'],self.capture_options['account_id'],self.capture_options['conversation_id'])
            with self.store.lock,self.store.conn:
                row=self.store.conn.execute('SELECT gaps FROM conversations WHERE id=?',(cid,)).fetchone()
                if row:
                    existing=json.loads(row['gaps']);merged=existing+[g for g in state['gaps'] if g not in existing]
                    if merged!=existing:self.store.conn.execute('UPDATE conversations SET gaps=? WHERE id=?',(dumps(merged[-1000:]),cid))
        return state

    def capture_probe(self,p):
        from .adapters import WindowVisionAdapter
        import platform
        target=p.get('platform','wechat')
        if target not in ('wechat','qq'):raise ValueError('platform 无效')
        adapter=WindowVisionAdapter(target)
        return adapter.probe()

    def start_capture(self,p):
        from .adapters import WindowVisionAdapter
        if self.store.settings()['paused']:raise ValueError('全局已暂停，请先恢复')
        s=self.store.settings()
        target=p.get('platform',s['vision_platform'])
        if target not in ('wechat','qq'):raise ValueError('platform 无效')
        account=p.get('account_id',s['vision_account_id']);conversation=p.get('conversation_id',s['vision_conversation_id'])
        expected=p.get('expected_conversation',s['vision_expected_conversation'])
        acknowledged=p.get('acknowledged_scope',s['vision_acknowledged_scope'])
        if not account or not conversation or not expected or not acknowledged:raise ValueError('需要明确账号、会话、可见标题和采集范围确认')
        mode=s['vision_mode'];cid=opaque(target,account,conversation)
        if mode=='disabled':raise ValueError('视觉模型未配置')
        if not s['vision_name'] or not s['vision_url']:raise ValueError('视觉模型URL或型号未配置')
        if not p.get('crop_acknowledged',s.get('vision_crop_acknowledged',False)):raise ValueError('请先完成本地裁剪预览并确认外发范围')
        if mode=='cloud' and not s['vision_cloud_consent']:raise ValueError('尚未明确授权选定窗口截图外发')
        if self.capture:self.capture.stop()
        checkpoint_id='vision:'+cid
        checkpoint=self.store.checkpoint(checkpoint_id)
        restored=dict(checkpoint['metadata']) if checkpoint else {}
        if checkpoint:restored['cursor']=checkpoint['cursor']
        config={'mode':mode,'url':s['vision_url'],'name':s['vision_name'],'cloud_authorized':mode=='cloud' and s['vision_cloud_consent']}
        self.store.ensure_capture_conversation(target,account,conversation,expected)
        self.capture=WindowVisionAdapter(target)
        # Every callback writes the normalized event and cursor in a single transaction.
        def persist_batch(events,checkpoint):
            if self.closed.is_set() or self.store.settings()['paused']:raise ValueError('全局已暂停或工作进程退出')
            messages=[normalize(event) for event in events]
            self.store.ingest(messages,checkpoint={'id':checkpoint_id,'cursor':checkpoint['cursor'],'adapter_version':'0.1.0','metadata':dict(checkpoint,conversation_id=cid)},require_whitelisted=True)
        self.capture_options=dict(platform=target,account_id=account,conversation_id=conversation,window_id=p.get('window_id',s['vision_window_id']),expected_conversation=expected,acknowledged_scope=acknowledged,
          watch_changes=bool(p.get('watch_changes',False)),watch_roi=p.get('watch_roi',[.22,.12,.77,.64]),max_wait_seconds=p.get('max_wait_seconds',5),
          vision_crop=p.get('vision_crop',s.get('vision_crop',[.25,0,.75,.85])),crop_acknowledged=bool(p.get('crop_acknowledged',s.get('vision_crop_acknowledged',False))))
        self.capture.start(**{k:v for k,v in self.capture_options.items() if k!='platform'},model_config=config,on_batch=persist_batch,checkpoint=restored,
          debounce_seconds=s['vision_debounce_seconds'],cooldown_seconds=s['vision_cooldown_seconds'],periodic_fallback_seconds=s['vision_periodic_fallback_seconds'])
        return self.capture_health()

    def capture_preview(self,p):
        import base64
        from .adapters import WindowVisionAdapter
        if not p.get('window_id'):raise ValueError('请先指定窗口ID')
        adapter=WindowVisionAdapter(p.get('platform','wechat'))
        crop=p.get('vision_crop',[.25,0,.75,.85])
        if not isinstance(crop,list) or len(crop)!=4 or not all(isinstance(x,(int,float)) for x in crop) or min(crop)<0 or crop[2]<=0 or crop[3]<=0 or crop[0]+crop[2]>1 or crop[1]+crop[3]>1:raise ValueError('裁剪坐标无效')
        adapter._options={'window_id':int(p['window_id']),'vision_crop':crop,'crop_acknowledged':True}
        deadline=time.monotonic()+min(15,max(0,float(p.get('wait_seconds',10))))
        while True:
            try:
                data=adapter._capture_selected()
                return {'data_url':'data:image/png;base64,'+base64.b64encode(data).decode(),'scope':'local_preview_only','vision_crop':crop}
            except RuntimeError as exc:
                if str(exc) not in ('window_not_foreground',) or time.monotonic()>=deadline:raise ValueError('本地预览受限，请确认目标窗口处于前台且已授权屏幕录制') from None
                time.sleep(.5)

    def stop_capture(self):
        if self.capture:
            self.capture.stop()
            if self.capture_options and self.capture._options:
                cid=opaque(self.capture_options['platform'],self.capture_options['account_id'],self.capture_options['conversation_id'])
                cp=self.capture.checkpoint()
                self.store.ingest([],checkpoint={'id':'vision:'+cid,'cursor':cp['cursor'],'metadata':dict(cp,conversation_id=cid)},schedule=False)
        state=self.capture_health()
        self.capture_options=None
        return state

    def process_jobs(self):
        while not self.closed.wait(.15):
            if self.store.settings()['paused']: continue
            job=self.store.next_job()
            if not job: continue
            try:
                p=job['payload'];s=self.store.settings();cid=job['conversation_id'];con=self.store.conversation(cid)
                messages=self.store.messages(cid,start=p.get('start'),end=p.get('end'),limit=max(1,min(1000,s.get('context_limit',300))),newest=True)['messages']
                # Keep the newest bounded input window, not full history on every event.
                messages=messages[-max(1,min(1000,s.get('context_limit',300))):]
                config={'mode':s['model_mode'],'url':s['model_url'],'name':s['model_name'],'allowed_conversation_ids':s['cloud_conversation_ids'],'cloud_authorized':cid in s['cloud_conversation_ids']}
                if config['mode']=='cloud' and not config['cloud_authorized']: raise ValueError('未明确授权该会话外发；请选择会话范围或切回规则模式')
                started=time.monotonic()
                result=bounded_analysis(messages,job['kind'],con['timezone'],p.get('question'),config,timeout=job.get('timeout',45))
                result['worker_elapsed_seconds']=round(time.monotonic()-started,4)
                self.store.save_analysis(job,messages,result)
            except Exception as exc:
                # Exception content from network/model may contain input; publish only controlled category.
                safe=str(exc) if isinstance(exc,ValueError) and ('授权' in str(exc) or '引用' in str(exc) or '配置' in str(exc)) else type(exc).__name__+'：分析失败，可重试；原文已保留'
                self.store.fail_job(job['id'],safe,job.get('lease_token'))

    def start_replay(self,p):
        if self.replay_thread and self.replay_thread.is_alive(): raise ValueError('已有回放运行，请先停止')
        valid,errors=parse_text(p.get('text',''),p.get('format','jsonl'))
        if errors: raise ValueError('回放输入存在无效行，请先预览修复')
        try:speed=float(p.get('speed',1))
        except (ValueError,TypeError):raise ValueError('speed 必须为正数')
        if not 0<speed<=10000:raise ValueError('speed 必须在 0..10000 事件/秒')
        messages=[m for _,m in valid]
        replay_id='replay:'+hashlib.sha256(p['text'].encode()).hexdigest()
        checkpoint=self.store.checkpoint(replay_id);cursor=checkpoint['cursor'] if checkpoint else 0
        if cursor>=len(messages):cursor=0
        self.replay_stop.clear();self.replay={'running':True,'cursor':cursor,'total':len(messages),'mode':'replay','demo':all(m.get('demo') for m in messages),'speed':speed,'checkpoint_id':replay_id}
        def replay_loop():
            try:
                for i in range(cursor,len(messages)):
                    if self.replay_stop.is_set() or self.closed.is_set():break
                    while self.store.settings()['paused']:
                        if self.replay_stop.wait(.1) or self.closed.is_set():return
                    m=messages[i];m['capture_mode']='replay';m['observed_at']=now()
                    self.store.ingest([m],checkpoint={'id':replay_id,'cursor':i+1,'metadata':{'total':len(messages)}})
                    self.replay['cursor']=i+1
                    if self.replay_stop.wait(1/speed):break
            except Exception as exc:self.replay['error']=type(exc).__name__
            finally:self.replay['running']=False
        self.replay_thread=threading.Thread(target=replay_loop,daemon=True);self.replay_thread.start()
        return dict(self.replay)

    def stop_replay(self):
        self.replay_stop.set()
        if self.replay_thread:self.replay_thread.join(timeout=2)
        return dict(self.replay)

    def state(self):
        with self.store.lock:
            row=self.store.conn.execute('SELECT MAX(observed_at),COUNT(*) FROM messages').fetchone()
            pending=self.store.conn.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('pending','running')").fetchone()[0]
        return {'paused':self.store.settings()['paused'],'mode':'replay' if self.replay.get('running') else 'vision_window' if self.capture else 'local','last_message_at':row[0],'message_count':row[1],'analysis_pending':pending,'replay':dict(self.replay),'capture':self.capture_health(),'settings':self.store.settings(),'data_dir':str(self.store.data_dir),'model_key_configured':bool(os.environ.get('CHATLENS_MODEL_API_KEY')),'vision_key_configured':bool(os.environ.get('CHATLENS_VISION_API_KEY')),'source_gaps':['导入及回放仅覆盖输入数据，不能代表全账号实时采集','视觉采集仅覆盖触发后取得的指定窗口画面；未见内容与消息变化事件不保证取得']}

    def export(self,p):
        kind=p.get('kind','daily');fmt=p.get('format','markdown');cid=p.get('conversation_id')
        if fmt not in ('json','markdown','csv'):raise ValueError('导出格式无效')
        messages=self.store.all_messages(cid,start=p.get('start'),end=p.get('end'))
        ids={m['id'] for m in messages}
        analyses=[a for a in self.store.analyses(cid) if a['status']=='valid' and set(a['input_ids']).issubset(ids)]
        tasks=[t for t in self.store.tasks()['tasks'] if (not cid or t['conversation_id']==cid) and set(t['evidence_ids']).issubset(ids)]
        if kind not in ('daily','weekly','todos','customer','messages'):raise ValueError('报告类型无效')
        if kind=='customer':analyses=[a for a in analyses if a['kind']=='customer']
        conversations=[c for c in self.store.conversations() if not cid or c['id']==cid]
        report={'title':'利明智耀.Chatlens '+kind,'brand':'利明智耀','generated_at':now(),'range':{'start':p.get('start'),'end':p.get('end'),'message_count':len(messages)},'mode':'local','limitations':['仅涵盖已取得的消息；不推断缺口内容','普通SQLite未加密；报告含原文，应由用户妥善保管'],'conversations':conversations,'messages':messages,'analyses':analyses,'tasks':tasks}
        if fmt=='json':content=json.dumps(report,ensure_ascii=False,indent=2);mime='application/json';ext='json'
        elif fmt=='csv':
            out=io.StringIO();rows=tasks if kind=='todos' else [dict(entry,conversation_id=a['conversation_id']) for a in analyses for entry in a['result'].get('items',[])] if kind=='customer' else messages
            fields=['id','conversation_id','content','owner','due_at','status','evidence_ids'] if kind=='todos' else ['conversation_id','category','claim','classification','evidence_ids'] if kind=='customer' else ['id','platform','account_id','conversation_id','sender_id','sent_at','content','revision','capture_mode']
            writer=csv.DictWriter(out,fieldnames=fields,extrasaction='ignore');writer.writeheader()
            for row in rows:
                safe={k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()}
                # Stop spreadsheet formulas in exported user-controlled text.
                for k,v in safe.items():
                    if isinstance(v,str) and v.startswith(('=','+','-','@')):safe[k]="'"+v
                writer.writerow(safe)
            content=out.getvalue();mime='text/csv';ext='csv'
        else:
            lines=['# '+report['title'],'','生成时间：'+report['generated_at'],'','范围：'+str(p.get('start') or '已取得起点')+' 至 '+str(p.get('end') or '已取得终点'),'','消息数：'+str(len(messages)),'','覆盖限制：仅涵盖已取得消息，缺口不推断。','']
            for c in conversations:
                lines+=['会话：'+c['name']+'；覆盖缺口：'+json.dumps(c['gaps'],ensure_ascii=False),'']
            if kind in ('todos','daily','weekly'):
                lines+=['## 待办','']
                for t in tasks:lines+=['- '+t['content']+' | 负责人：'+str(t['owner'] or '未明确')+' | 截止：'+str(t['due_at'] or '未明确')+' | 状态：'+t['status']+' | 证据：'+', '.join(t['evidence_ids'])]
            for a in analyses:
                lines+=['','## '+a['kind']+'（'+a['model']+'）','',json.dumps(a['result'],ensure_ascii=False,indent=2)]
            lines+=['','## 原文索引','']
            for m in messages:lines+=['- ['+m['id']+'] '+m['sent_at']+' '+m['sender_id']+'：'+m['content']]
            content='\n'.join(lines);mime='text/markdown';ext='md'
        return {'filename':'利明智耀.Chatlens-'+kind+'-'+time.strftime('%Y%m%d-%H%M%S')+'.'+ext,'content':content,'mime':mime}

    def close(self):
        self.closed.set();self.stop_replay();self.stop_capture();self.processor.join(timeout=3)
        if not self.processor.is_alive():self.store.close()

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,addr,runtime,token):
        super().__init__(addr,Handler);self.runtime=runtime;self.token=token

class Handler(BaseHTTPRequestHandler):
    server_version='ChatLens/'+VERSION
    def log_message(self,*args):pass
    def send_json(self,status,value):
        data=dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data)
    def auth(self):
        import hmac
        if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.token):
            self.send_json(401,{'error':'需要有效本地会话令牌'});return False
        # Browser-origin access is never allowed; Electron main process uses no Origin.
        if self.headers.get('Origin'):
            self.send_json(403,{'error':'禁止浏览器跨源请求'});return False
        return True
    def do_GET(self):
        if not self.auth():return
        try:
            u=urlsplit(self.path);p=u.path;q={k:v[0] for k,v in parse_qs(u.query).items()};rt=self.server.runtime;s=rt.store
            if p=='/api/health':r={'ok':True,'version':VERSION}
            elif p=='/api/state':r=rt.state()
            elif p=='/api/conversations':r={'conversations':s.conversations()}
            elif p=='/api/messages':r=s.messages(**{k:v for k,v in q.items() if k in ('conversation_id','q','sender','start','end','limit','offset')})
            elif p=='/api/messages/context':r=s.context(q.get('id'),q.get('radius',5))
            elif p=='/api/analyses':r={'analyses':s.analyses(q.get('conversation_id'))}
            elif p=='/api/tasks':r=s.tasks()
            elif p=='/api/capture/health':r=rt.capture_health()
            elif p=='/api/capture/windows':r=rt.capture_probe(q)
            elif p=='/api/capabilities':
                path=Path(os.environ.get('CHATLENS_COMPATIBILITY_PATH',str(Path(__file__).resolve().parents[2]/'docs'/'compatibility.json')))
                r={'capabilities':json.loads(path.read_text()) if path.exists() else {'status':'未验证','reason':'接入能力探针尚未完成'}}
            else:self.send_json(404,{'error':'接口不存在'});return
            self.send_json(200,r)
        except (ValueError,TypeError,KeyError) as exc:self.send_json(400,{'error':str(exc)})
        except Exception:self.send_json(500,{'error':'本地工作进程内部错误，原文未写入日志'})
    def do_POST(self):
        if not self.auth():return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<0 or length>64*1024*1024:self.send_json(413,{'error':'请求超过64MB限制，请分批导入'});return
            if not self.headers.get('Content-Type','').startswith('application/json'):self.send_json(415,{'error':'需要application/json'});return
            payload=json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(payload,dict):raise ValueError('请求必须为JSON对象')
            p=urlsplit(self.path).path;rt=self.server.runtime;s=rt.store
            if p=='/api/import/preview':
                valid,errors=parse_text(payload.get('text',''),payload.get('format','jsonl'));r={'valid_count':len(valid),'errors':errors,'sample':[m for _,m in valid[:10]]}
            elif p=='/api/import':r=s.import_text(payload.get('text',''),payload.get('format','jsonl'))
            elif p=='/api/replay':r=rt.start_replay(payload)
            elif p=='/api/replay/stop':r=rt.stop_replay()
            elif p=='/api/capture/probe':r=rt.capture_probe(payload)
            elif p=='/api/capture/start':r=rt.start_capture(payload)
            elif p=='/api/capture/preview':r=rt.capture_preview(payload)
            elif p=='/api/capture/dock':r=rt.dock(payload)
            elif p=='/api/capture/undock':r=rt.undock(payload)
            elif p=='/api/capture/stop':r=rt.stop_capture()
            elif p=='/api/capture/trigger':
                if not rt.capture:raise ValueError('尚未启动指定窗口采集')
                if s.settings()['paused']:raise ValueError('全局已暂停')
                r=rt.capture.trigger(payload.get('reason','manual'))
            elif p=='/api/analysis':
                args={k:payload[k] for k in ('question','start','end') if payload.get(k)}
                r={'job_id':s.enqueue(payload.get('conversation_id'),payload.get('kind','summary'),args),'status':'pending'}
            elif p=='/api/analysis/retry':r=s.retry_job(payload.get('job_id'))
            elif p=='/api/analysis/correct':r=s.correct_analysis(payload.get('id'),payload.get('result'))
            elif p=='/api/tasks/update':r=s.update_task(payload)
            elif p=='/api/settings':
                # Revoking cloud/crop consent must terminate the old capture configuration.
                if any(k.startswith('vision_') for k in payload):rt.stop_capture()
                r={'settings':s.update_settings(payload)}
            elif p in ('/api/pause','/api/resume'):
                if p.endswith('/pause'):rt.stop_capture()
                r={'paused':s.update_settings({'paused':p.endswith('/pause')})['paused']}
            elif p=='/api/conversations/update':
                r=s.update_conversation(payload)
                if payload.get('whitelisted') is False and rt.capture_options:
                    cid=opaque(rt.capture_options['platform'],rt.capture_options['account_id'],rt.capture_options['conversation_id'])
                    if cid==payload.get('id'):rt.stop_capture()
            elif p=='/api/conversations/delete':
                rt.stop_replay();rt.stop_capture();r=s.delete_conversation(payload.get('id'))
            elif p=='/api/export':r=rt.export(payload)
            else:self.send_json(404,{'error':'接口不存在'});return
            self.send_json(200,r)
        except (ValueError,TypeError,KeyError) as exc:self.send_json(400,{'error':str(exc)})
        except Exception:self.send_json(500,{'error':'本地工作进程内部错误，原文未写入日志'})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=0);parser.add_argument('--data-dir',default=os.environ.get('CHATLENS_DATA_DIR',str(Path.home()/'.chatlens')));args=parser.parse_args()
    token=os.environ.get('CHATLENS_TOKEN')
    if not token or len(token)<24:raise SystemExit('CHATLENS_TOKEN 必须为至少24字符的随机会话令牌')
    rt=Runtime(Store(args.data_dir));server=Server(('127.0.0.1',args.port),rt,token)
    def stop(*unused):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    print(dumps({'ready':True,'port':server.server_port,'version':VERSION}),flush=True)
    try:server.serve_forever(poll_interval=.2)
    finally:server.server_close();rt.close()

if __name__=='__main__':main()
