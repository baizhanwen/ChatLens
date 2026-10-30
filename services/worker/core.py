"""Transactional local message/event store. Uses only the Python standard library."""
import csv
import hashlib
import io
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

VERSION = '0.1.0'
KINDS = {'summary', 'todos', 'customer', 'communication', 'qa', 'daily', 'weekly'}

def now():
    return datetime.now(timezone.utc).isoformat()

def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

def iso(value, field='sent_at'):
    if not isinstance(value, str):
        raise ValueError('%s 必须为带时区的 ISO8601 字符串' % field)
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('%s 不是有效 ISO8601 时间' % field)
    if dt.tzinfo is None:
        raise ValueError('%s 必须包含时区' % field)
    return dt.astimezone(timezone.utc).isoformat()

def boolean(value):
    if isinstance(value, str):
        if value.lower() in ('true', '1', 'yes'): return True
        if value.lower() in ('false', '0', 'no', ''): return False
        raise ValueError('布尔值无效')
    if isinstance(value,bool):return value
    if isinstance(value,int) and value in (0,1):return bool(value)
    raise ValueError('布尔值必须为true/false或0/1')

def opaque(platform, account, conversation):
    return hashlib.sha256(dumps([platform, account, conversation]).encode()).hexdigest()[:32]

def normalize(raw):
    if not isinstance(raw, dict): raise ValueError('每行必须是 JSON 对象')
    required = ['platform', 'account_id', 'conversation_id', 'sender_id', 'source_id', 'sent_at', 'type', 'content']
    for key in required:
        if key not in raw or not isinstance(raw[key], str) or (not raw[key] and key != 'content'):
            raise ValueError('缺少或无效字段: ' + key)
    if raw['platform'] not in ('wechat', 'qq'): raise ValueError('platform 仅支持 wechat/qq')
    if len(raw['content']) > 1000000: raise ValueError('content 超过 1MB 限制')
    for optional in ('source_conversation_id','conversation_name','timezone','reference_id','attachment_name'):
        if optional in raw and raw[optional] is not None and not isinstance(raw[optional],str): raise ValueError(optional+' 必须为字符串')
    if 'source_conversation_id' in raw and not raw['source_conversation_id']: raise ValueError('source_conversation_id 不可为空')
    if raw.get('timezone'):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try: ZoneInfo(raw['timezone'])
        except (ValueError,ZoneInfoNotFoundError): raise ValueError('timezone 必须为有效IANA时区')
    if 'metadata' in raw and not isinstance(raw['metadata'],dict): raise ValueError('metadata 必须为对象')
    if raw.get('conversation_type') not in (None,'direct','group','unknown'): raise ValueError('conversation_type 无效')
    result = dict(raw)
    result['source_conversation_id'] = raw.get('source_conversation_id', raw['conversation_id'])
    result['conversation_id'] = opaque(raw['platform'], raw['account_id'], result['source_conversation_id'])
    result['id'] = hashlib.sha256(dumps([raw['platform'], raw['account_id'], result['source_conversation_id'], raw['source_id']]).encode()).hexdigest()
    result['sent_at'] = iso(raw['sent_at'])
    result['observed_at'] = iso(raw.get('observed_at') or now(), 'observed_at')
    try:
        rev=raw.get('revision',0)
        if rev is None or rev=='':rev=0
        if isinstance(rev,bool) or isinstance(rev,float) and not rev.is_integer(): raise ValueError()
        result['revision'] = int(rev)
    except (ValueError, TypeError): raise ValueError('revision 必须为非负整数')
    if result['revision'] < 0: raise ValueError('revision 必须为非负整数')
    result['deleted'] = boolean(raw.get('deleted', False))
    result['demo'] = boolean(raw.get('demo', False))
    result['capture_mode'] = raw.get('capture_mode') or 'import'
    if result['capture_mode'] not in ('import', 'replay', 'accessibility', 'ocr', 'local_source', 'uia', 'vision_window', 'accessibility_visible_fragment', 'ocr_visible_fragment'):
        raise ValueError('capture_mode 无效')
    if raw.get('confidence') is not None:
        result['confidence'] = float(raw['confidence'])
        if not 0 <= result['confidence'] <= 1: raise ValueError('confidence 必须在 0..1')
    supported = {'text', 'quote', 'attachment', 'image', 'audio', 'video', 'system', 'unknown', 'visible_fragment'}
    if result['type'] not in supported:
        result['unsupported_type'] = result['type']
        result['type'] = 'unknown'
        result['unsupported_reason'] = '消息类型暂不支持，保留原始内容及类型'
    if result['type'] in ('image', 'audio', 'video'):
        result.setdefault('unsupported_reason', '仅保存元信息；未执行图片识别或语音转写')
    return result

def parse_text(text, fmt='jsonl'):
    if not isinstance(text, str): raise ValueError('text 必须为 UTF-8 文本')
    text = text.lstrip('\ufeff')
    valid, errors = [], []
    if fmt == 'jsonl':
        rows = [(i, line) for i, line in enumerate(text.splitlines(), 1) if line.strip()]
        for line, value in rows:
            try: valid.append((line, normalize(json.loads(value))))
            except (ValueError, TypeError, OverflowError) as exc: errors.append({'line': line, 'error': str(exc)})
    elif fmt == 'csv':
        try:
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                line = reader.line_num
                try: valid.append((line, normalize(row)))
                except (ValueError, TypeError, OverflowError) as exc: errors.append({'line': line, 'error': str(exc)})
        except csv.Error as exc: errors.append({'line': 1, 'error': str(exc)})
    else: raise ValueError('format 必须为 jsonl/csv')
    return valid, errors

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS conversations(
 id TEXT PRIMARY KEY,platform TEXT NOT NULL,account_id TEXT NOT NULL,source_conversation_id TEXT NOT NULL,
 name TEXT NOT NULL,timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',whitelisted INTEGER NOT NULL DEFAULT 0,
 coverage_start TEXT,coverage_end TEXT,gaps TEXT NOT NULL DEFAULT '[]',demo INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(
 id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL,sender_id TEXT NOT NULL,sent_at TEXT NOT NULL,observed_at TEXT NOT NULL,
 type TEXT NOT NULL,content TEXT NOT NULL,revision INTEGER NOT NULL,deleted INTEGER NOT NULL,capture_mode TEXT NOT NULL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_order ON messages(conversation_id,sent_at,id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(id UNINDEXED,content, tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages WHEN new.deleted=0 BEGIN INSERT INTO messages_fts(id,content) VALUES(new.id,new.content); END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN DELETE FROM messages_fts WHERE id=old.id; END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN DELETE FROM messages_fts WHERE id=old.id; INSERT INTO messages_fts(id,content) SELECT new.id,new.content WHERE new.deleted=0; END;
CREATE TABLE IF NOT EXISTS analyses(
 id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,kind TEXT NOT NULL,
 status TEXT NOT NULL,result TEXT NOT NULL,input_ids TEXT NOT NULL,input_version TEXT NOT NULL,generated_at TEXT NOT NULL,
 model TEXT NOT NULL,prompt_version TEXT NOT NULL,range_start TEXT,range_end TEXT,user_corrected INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,kind TEXT NOT NULL,
 state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 available_at REAL NOT NULL,payload TEXT NOT NULL,timeout INTEGER NOT NULL DEFAULT 45,owner_pid INTEGER,owner_instance TEXT,lease_token TEXT);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(state,available_at);
CREATE TABLE IF NOT EXISTS todos(
 id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,analysis_id TEXT REFERENCES analyses(id) ON DELETE CASCADE,
 content TEXT NOT NULL,owner TEXT,due_at TEXT,status TEXT NOT NULL,evidence_ids TEXT NOT NULL,user_corrected INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints(id TEXT PRIMARY KEY,cursor INTEGER NOT NULL,last_success TEXT NOT NULL,adapter_version TEXT NOT NULL,metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
'''

class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        self.path = self.data_dir / 'chatlens.sqlite3'
        self.lock = threading.RLock()
        self.instance = uuid.uuid4().hex
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.execute('PRAGMA secure_delete=ON')
        self.conn.executescript(SCHEMA)
        columns={r['name'] for r in self.conn.execute('PRAGMA table_info(jobs)')}
        for column,datatype in (('owner_pid','INTEGER'),('owner_instance','TEXT'),('lease_token','TEXT')):
            if column not in columns:self.conn.execute('ALTER TABLE jobs ADD COLUMN '+column+' '+datatype)
        self.conn.execute('INSERT OR IGNORE INTO schema_version VALUES(2)')
        for row in self.conn.execute("SELECT id,owner_pid FROM jobs WHERE state='running'").fetchall():
            alive=False
            if row['owner_pid']:
                try:os.kill(row['owner_pid'],0);alive=True
                except ProcessLookupError:pass
                except PermissionError:alive=True
            if not alive:self.conn.execute("UPDATE jobs SET state='pending',error='工作进程中断后恢复',updated_at=?,lease_token=NULL,owner_pid=NULL,owner_instance=NULL WHERE id=?",(now(),row['id']))
        self.conn.commit()
        os.chmod(self.path, 0o600)
        for path in self.data_dir.glob('chatlens.sqlite3-*'):
            os.chmod(path, 0o600)
        self.apply_retention()

    def close(self):
        with self.lock:
            with self.conn:self.conn.execute("UPDATE jobs SET state='pending',owner_pid=NULL,owner_instance=NULL,lease_token=NULL,updated_at=? WHERE state='running' AND owner_instance=?",(now(),self.instance))
            self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.conn.close()

    def settings(self):
        result = {'model_mode': 'rules', 'model_url': '', 'model_name': '', 'cloud_conversation_ids': [], 'retention_days': 0, 'analysis_window_seconds': 2, 'context_limit': 300, 'paused': False, 'vision_mode':'disabled', 'vision_url':'', 'vision_name':'', 'vision_cloud_consent':False, 'vision_window_id':'', 'vision_platform':'wechat', 'vision_account_id':'', 'vision_conversation_id':'', 'vision_interval':5, 'vision_expected_conversation':'', 'vision_debounce_seconds':1.2, 'vision_cooldown_seconds':5, 'vision_periodic_fallback_seconds':0, 'vision_acknowledged_scope':False, 'vision_crop':[.25,0,.75,.85], 'vision_crop_acknowledged':False}
        with self.lock:
            for row in self.conn.execute('SELECT * FROM settings'): result[row['key']] = json.loads(row['value'])
        return result

    def update_settings(self, payload):
        allowed = {'model_mode','model_url','model_name','cloud_conversation_ids','retention_days','analysis_window_seconds','context_limit','paused','vision_mode','vision_url','vision_name','vision_cloud_consent','vision_window_id','vision_platform','vision_account_id','vision_conversation_id','vision_interval','vision_expected_conversation','vision_debounce_seconds','vision_cooldown_seconds','vision_periodic_fallback_seconds','vision_acknowledged_scope','vision_crop','vision_crop_acknowledged'}
        if any(key not in allowed for key in payload): raise ValueError('设置中包含不允许的字段；密钥只能从工作进程环境提供')
        if payload.get('model_mode', 'rules') not in ('rules', 'local', 'cloud'): raise ValueError('model_mode 无效')
        for key in ('retention_days', 'analysis_window_seconds', 'context_limit'):
            if key in payload:
                payload[key] = int(payload[key])
                if payload[key] < 0 or payload[key] > 100000: raise ValueError(key + ' 超出范围')
        if 'cloud_conversation_ids' in payload and (not isinstance(payload['cloud_conversation_ids'], list) or not all(isinstance(x,str) for x in payload['cloud_conversation_ids'])): raise ValueError('cloud_conversation_ids 必须为会话 ID 列表')
        if payload.get('vision_mode', 'disabled') not in ('disabled','local','cloud'): raise ValueError('vision_mode 无效')
        if 'vision_interval' in payload:
            payload['vision_interval'] = max(2,min(300,float(payload['vision_interval'])))
        for bkey in ('vision_cloud_consent','vision_acknowledged_scope','vision_crop_acknowledged','paused'):
            if bkey in payload: payload[bkey] = boolean(payload[bkey])
        for time_key in ('vision_debounce_seconds','vision_cooldown_seconds','vision_periodic_fallback_seconds'):
            if time_key in payload:
                payload[time_key]=float(payload[time_key])
                if not 0<=payload[time_key]<=3600: raise ValueError(time_key+' 必须在0..3600秒')
        if 'vision_crop' in payload:
            crop=payload['vision_crop']
            if not isinstance(crop,list) or len(crop)!=4 or any(not isinstance(x,(int,float)) or isinstance(x,bool) or not 0<=x<=1 for x in crop):raise ValueError('vision_crop 必须为0..1范围[x,y,width,height]数组')
            if crop[2]<=0 or crop[3]<=0 or crop[0]+crop[2]>1.000001 or crop[1]+crop[3]>1.000001:raise ValueError('vision_crop 超出窗口边界或面积为零')
        for string_key in ('model_url','model_name','vision_url','vision_name','vision_expected_conversation','vision_account_id','vision_conversation_id'):
            if string_key in payload and not isinstance(payload[string_key],str):raise ValueError(string_key+' 必须为字符串')
        if 'vision_platform' in payload and payload['vision_platform'] not in ('wechat','qq'): raise ValueError('vision_platform 无效')
        for url_key in ('model_url','vision_url'):
            if url_key not in payload: continue
            from urllib.parse import urlsplit
            u = urlsplit(payload[url_key])
            if payload[url_key] and (u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment): raise ValueError(url_key + ' 必须为不含凭据、查询参数和片段的 HTTP(S) 地址')
        with self.lock, self.conn:
            for key, value in payload.items(): self.conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key,dumps(value)))
        if 'retention_days' in payload: self.apply_retention()
        return self.settings()

    def apply_retention(self):
        days=self.settings()['retention_days']
        if not days: return {'deleted_messages':0}
        cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
        with self.lock,self.conn:
            cids=[r[0] for r in self.conn.execute('SELECT DISTINCT conversation_id FROM messages WHERE sent_at<?',(cutoff,))]
            count=self.conn.execute('SELECT COUNT(*) FROM messages WHERE sent_at<?',(cutoff,)).fetchone()[0]
            for cid in cids:
                # Remove all derived material for affected ranges; regeneration requires current evidence.
                self.conn.execute('DELETE FROM analyses WHERE conversation_id=?',(cid,))
                self.conn.execute('DELETE FROM todos WHERE conversation_id=?',(cid,))
                self.conn.execute('DELETE FROM jobs WHERE conversation_id=?',(cid,))
                self._delete_capture_checkpoints(cid)
            self.conn.execute('DELETE FROM messages WHERE sent_at<?',(cutoff,))
            self.conn.execute("DELETE FROM checkpoints WHERE id LIKE 'replay:%'")
            self.conn.execute('DELETE FROM conversations WHERE NOT EXISTS(SELECT 1 FROM messages WHERE messages.conversation_id=conversations.id)')
            for cid in cids:
                self.conn.execute('UPDATE conversations SET coverage_start=(SELECT MIN(sent_at) FROM messages WHERE conversation_id=?),coverage_end=(SELECT MAX(sent_at) FROM messages WHERE conversation_id=?) WHERE id=?',(cid,cid,cid))
        if count:
            with self.lock:
                self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)');self.conn.execute('VACUUM')
        return {'deleted_messages':count}

    def _conversation(self, m):
        self.conn.execute('INSERT OR IGNORE INTO conversations(id,platform,account_id,source_conversation_id,name,timezone,demo) VALUES(?,?,?,?,?,?,?)', (m['conversation_id'],m['platform'],m['account_id'],m['source_conversation_id'],m.get('conversation_name') or m['source_conversation_id'],m.get('timezone') or 'Asia/Shanghai',int(m.get('demo',False))))
        self.conn.execute('UPDATE conversations SET coverage_start=CASE WHEN coverage_start IS NULL OR coverage_start>? THEN ? ELSE coverage_start END,coverage_end=CASE WHEN coverage_end IS NULL OR coverage_end<? THEN ? ELSE coverage_end END WHERE id=?', (m['sent_at'],m['sent_at'],m['sent_at'],m['sent_at'],m['conversation_id']))

    def _enqueue(self, conversation_id, kind='summary', payload=None, immediate=False):
        payload = payload or {}
        existing = self.conn.execute("SELECT id FROM jobs WHERE conversation_id=? AND kind=? AND state='pending' AND payload=?",(conversation_id,kind,dumps(payload))).fetchone()
        if existing:
            if immediate: self.conn.execute('UPDATE jobs SET available_at=?,updated_at=? WHERE id=?',(time.time(),now(),existing['id']))
            return existing['id']
        jid = uuid.uuid4().hex
        available = time.time() + (0 if immediate else self.settings()['analysis_window_seconds'])
        self.conn.execute('INSERT INTO jobs(id,conversation_id,kind,state,created_at,updated_at,available_at,payload) VALUES(?,?,?,?,?,?,?,?)',(jid,conversation_id,kind,'pending',now(),now(),available,dumps(payload)))
        return jid

    def ingest(self, messages, checkpoint=None, schedule=True, require_whitelisted=False):
        counts = {'inserted':0,'updated':0,'unchanged':0}
        affected = set()
        with self.lock, self.conn:
            if not self.conn.in_transaction:self.conn.execute('BEGIN IMMEDIATE')
            if require_whitelisted:
                for cid in {m['conversation_id'] for m in messages}:
                    binding=self.conn.execute('SELECT whitelisted FROM conversations WHERE id=?',(cid,)).fetchone()
                    if not binding or not binding['whitelisted']:raise ValueError('会话采集白名单已关闭')
            for m in messages:
                self._conversation(m)
                old = self.conn.execute('SELECT * FROM messages WHERE id=?', (m['id'],)).fetchone()
                if old:
                    comparable = ('content','revision','deleted','sender_id','sent_at','type')
                    if m['revision'] < old['revision'] or all(m[k] == (bool(old[k]) if k == 'deleted' else old[k]) for k in comparable):
                        counts['unchanged'] += 1; continue
                    if m['revision'] == old['revision']:
                        # Conflicting observations at the same source revision cannot overwrite authoritative data.
                        counts['unchanged'] += 1; continue
                    self.conn.execute('UPDATE messages SET source_id=?,sender_id=?,sent_at=?,observed_at=?,type=?,content=?,revision=?,deleted=?,capture_mode=?,payload=? WHERE id=?',(m['source_id'],m['sender_id'],m['sent_at'],m['observed_at'],m['type'],m['content'],m['revision'],int(m['deleted']),m['capture_mode'],dumps(m),m['id']))
                    self.conn.execute("UPDATE analyses SET status='stale' WHERE conversation_id=?",(m['conversation_id'],))
                    # Candidate tasks are invalidated too; confirmed corrections remain visible as stale.
                    self.conn.execute("UPDATE todos SET status='stale',updated_at=? WHERE conversation_id=? AND status NOT IN ('done','dismissed')",(now(),m['conversation_id']))
                    counts['updated'] += 1
                else:
                    self.conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(m['id'],m['conversation_id'],m['source_id'],m['sender_id'],m['sent_at'],m['observed_at'],m['type'],m['content'],m['revision'],int(m['deleted']),m['capture_mode'],dumps(m)))
                    counts['inserted'] += 1
                affected.add(m['conversation_id'])
            if checkpoint:
                self.conn.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?)',(checkpoint['id'],checkpoint['cursor'],now(),checkpoint.get('adapter_version',VERSION),dumps(checkpoint.get('metadata',{}))))
            if schedule:
                for cid in affected:
                    for kind in ('summary','todos','customer','communication'): self._enqueue(cid,kind)
        return counts

    def import_text(self, text, fmt='jsonl'):
        valid, errors = parse_text(text,fmt)
        result = self.ingest([m for _,m in valid])
        result['errors'] = errors
        return result

    def conversations(self):
        with self.lock:
            rows = self.conn.execute('SELECT c.*,COUNT(m.id) AS message_count FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id GROUP BY c.id ORDER BY c.coverage_end DESC,c.id').fetchall()
            result=[]
            for row in rows:
                d=dict(row); d['gaps']=json.loads(d['gaps']); d['whitelisted']=bool(d['whitelisted']); d['demo']=bool(d['demo']); result.append(d)
            return result

    def conversation(self, cid):
        with self.lock:
            row=self.conn.execute('SELECT * FROM conversations WHERE id=?',(cid,)).fetchone()
            if not row: raise ValueError('会话不存在')
            return dict(row)

    def ensure_capture_conversation(self,platform,account_id,source_conversation_id,name,timezone='Asia/Shanghai'):
        from zoneinfo import ZoneInfo
        if platform not in ('wechat','qq') or not all(isinstance(x,str) and x for x in (account_id,source_conversation_id,name)):raise ValueError('采集绑定参数无效')
        ZoneInfo(timezone)
        cid=opaque(platform,account_id,source_conversation_id)
        with self.lock,self.conn:
            self.conn.execute('INSERT OR IGNORE INTO conversations(id,platform,account_id,source_conversation_id,name,timezone,whitelisted) VALUES(?,?,?,?,?,?,1)',(cid,platform,account_id,source_conversation_id,name,timezone))
            self.conn.execute('UPDATE conversations SET whitelisted=1,name=? WHERE id=?',(name,cid))
        return cid

    def update_conversation(self,payload):
        cid=payload.get('id'); self.conversation(cid)
        allowed={'whitelisted','name','timezone'}
        from zoneinfo import ZoneInfo
        if 'timezone' in payload: ZoneInfo(payload['timezone'])
        with self.lock,self.conn:
            for key in allowed:
                if key in payload: self.conn.execute('UPDATE conversations SET '+key+'=? WHERE id=?',(int(boolean(payload[key])) if key=='whitelisted' else str(payload[key]),cid))
        return {'ok':True}

    def messages(self, conversation_id=None,q=None,sender=None,start=None,end=None,limit=100,offset=0,include_deleted=False,newest=False):
        limit=max(1,min(int(limit),10000)); offset=max(0,int(offset)); params=[]; where=[]
        if conversation_id: where.append('conversation_id=?'); params.append(conversation_id)
        if not include_deleted: where.append('deleted=0')
        if q: where.append("content LIKE ? ESCAPE '\\'"); params.append('%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        if sender: where.append('sender_id=?'); params.append(sender)
        if start: where.append('sent_at>=?'); params.append(iso(start,'start'))
        if end: where.append('sent_at<=?'); params.append(iso(end,'end'))
        clause=' WHERE '+' AND '.join(where) if where else ''
        with self.lock:
            total=self.conn.execute('SELECT COUNT(*) FROM messages'+clause,params).fetchone()[0]
            order=' ORDER BY sent_at DESC,id DESC' if newest else ' ORDER BY sent_at,id'
            rows=self.conn.execute('SELECT payload FROM messages'+clause+order+' LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
            if newest: rows=list(reversed(rows))
        return {'messages':[json.loads(r['payload']) for r in rows],'total':total,'limit':limit,'offset':offset}

    def all_messages(self,conversation_id=None,q=None,sender=None,start=None,end=None,include_deleted=False):
        # A single lock makes an export consistent while page boundaries are read.
        result=[];offset=0
        with self.lock:
            while True:
                page=self.messages(conversation_id,q,sender,start,end,limit=10000,offset=offset,include_deleted=include_deleted)
                result.extend(page['messages']);offset+=len(page['messages'])
                if offset>=page['total'] or not page['messages']:break
        return result

    def context(self,mid,radius=5):
        radius=max(0,min(int(radius),50))
        with self.lock:
            row=self.conn.execute('SELECT conversation_id,sent_at,id,deleted FROM messages WHERE id=?',(mid,)).fetchone()
            if not row or row['deleted']: raise ValueError('原文不存在、已删除或已撤回；引用已失效')
            before=self.conn.execute('SELECT payload FROM messages WHERE conversation_id=? AND deleted=0 AND (sent_at<? OR (sent_at=? AND id<?)) ORDER BY sent_at DESC,id DESC LIMIT ?',(row['conversation_id'],row['sent_at'],row['sent_at'],mid,radius)).fetchall()
            after=self.conn.execute('SELECT payload FROM messages WHERE conversation_id=? AND deleted=0 AND (sent_at>? OR (sent_at=? AND id>=?)) ORDER BY sent_at,id LIMIT ?',(row['conversation_id'],row['sent_at'],row['sent_at'],mid,radius+1)).fetchall()
        return {'messages':[json.loads(r['payload']) for r in list(reversed(before))+list(after)]}

    def enqueue(self,cid,kind,payload=None):
        if kind not in KINDS: raise ValueError('分析类型无效')
        self.conversation(cid)
        with self.lock,self.conn: return self._enqueue(cid,kind,payload,immediate=True)

    def next_job(self):
        with self.lock,self.conn:
            self._expire_jobs()
            job=self.conn.execute("SELECT * FROM jobs WHERE state='pending' AND available_at<=? ORDER BY available_at,created_at LIMIT 1",(time.time(),)).fetchone()
            if not job: return None
            claimed_at=now();lease_token=uuid.uuid4().hex
            claim=self.conn.execute("UPDATE jobs SET state='running',attempts=attempts+1,updated_at=?,owner_pid=?,owner_instance=?,lease_token=? WHERE id=? AND state='pending'",(claimed_at,os.getpid(),self.instance,lease_token,job['id']))
            if not claim.rowcount:return None
            result=dict(job); result['payload']=json.loads(result['payload']);result['attempts']+=1;result['state']='running';result['updated_at']=claimed_at;result['lease_token']=lease_token;return result

    def _expire_jobs(self):
        for row in self.conn.execute("SELECT id,attempts,timeout,updated_at FROM jobs WHERE state='running'").fetchall():
            if datetime.fromisoformat(row['updated_at']).timestamp()+row['timeout'] < time.time():
                state='failed' if row['attempts']>=3 else 'pending'
                self.conn.execute("UPDATE jobs SET state=?,error=?,updated_at=?,available_at=?,lease_token=NULL,owner_pid=NULL,owner_instance=NULL WHERE id=? AND state='running'",(state,'分析任务超时；允许重试且拒绝迟到结果',now(),time.time()+1,row['id']))

    def fail_job(self,jid,error,lease_token=None):
        with self.lock,self.conn:
            job=self.conn.execute('SELECT attempts,lease_token FROM jobs WHERE id=?',(jid,)).fetchone()
            if not job or lease_token is not None and job['lease_token']!=lease_token:return
            attempts=job['attempts']; state='failed' if attempts>=3 else 'pending'
            self.conn.execute('UPDATE jobs SET state=?,error=?,updated_at=?,available_at=?,lease_token=NULL,owner_pid=NULL,owner_instance=NULL WHERE id=?',(state,str(error)[:400],now(),time.time()+min(30,2**attempts),jid))

    def retry_job(self,jid):
        with self.lock,self.conn:
            cur=self.conn.execute("UPDATE jobs SET state='pending',attempts=0,error=NULL,available_at=?,updated_at=?,lease_token=NULL,owner_pid=NULL,owner_instance=NULL WHERE id=?",(time.time(),now(),jid))
            if not cur.rowcount: raise ValueError('任务不存在')
        return {'job_id':jid,'status':'pending'}

    def save_analysis(self,job,messages,result):
        ids=[m['id'] for m in messages]; valid=set(ids)
        def validate_refs(value):
            if isinstance(value,dict):
                for key,val in value.items():
                    if key in ('evidence_ids','input_ids') and (not isinstance(val,list) or not all(isinstance(x,str) and x in valid for x in val)): raise ValueError('模型返回了输入范围外的引用')
                    validate_refs(val)
            elif isinstance(value,list):
                for x in value: validate_refs(x)
        if not isinstance(result,dict) or not isinstance(result.get('items'),list):raise ValueError('分析输出必须包含items列表')
        for item in result['items']:
            if not isinstance(item,dict) or not isinstance(item.get('evidence_ids'),list) or not item['evidence_ids']:raise ValueError('每项分析结论必须关联原文引用')
        validate_refs(result)
        aid=job['id']; cid=job['conversation_id']; version=hashlib.sha256(dumps([(m['id'],m['revision'],m['deleted']) for m in messages]).encode()).hexdigest()
        with self.lock,self.conn:
            lease=self.conn.execute('SELECT state,attempts,lease_token FROM jobs WHERE id=?',(job['id'],)).fetchone()
            if not lease:return None
            if 'attempts' in job and (lease['state']!='running' or lease['attempts']!=job['attempts'] or job.get('lease_token')!=lease['lease_token']):return None
            # Edits during model execution invalidate the result instead of publishing outdated claims.
            current_by_id={}
            for offset in range(0,len(ids),500):
                batch=ids[offset:offset+500]
                if batch:
                    for row in self.conn.execute('SELECT id,revision,deleted FROM messages WHERE id IN ('+','.join('?' for _ in batch)+')',batch):current_by_id[row['id']]=dict(row)
            stale=any(m['id'] not in current_by_id or current_by_id[m['id']]['deleted'] or current_by_id[m['id']]['revision']!=m['revision'] for m in messages)
            status='stale' if stale else 'valid'
            self.conn.execute('INSERT OR REPLACE INTO analyses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(aid,cid,job['kind'],status,dumps(result),dumps(ids),version,now(),str(result.get('mode') or result.get('model') or 'rules'),str(result.get('prompt_version') or 'v1'),job['payload'].get('start'),job['payload'].get('end'),0))
            self.conn.execute("UPDATE jobs SET state='done',error=NULL,updated_at=? WHERE id=?",(now(),job['id']))
            if job['kind']=='todos' and not stale:
                self.conn.execute("DELETE FROM todos WHERE conversation_id=? AND status='stale' AND user_corrected=0",(cid,))
                items=result.get('items',result.get('todos',[]))
                for item in items:
                    if not isinstance(item,dict): continue
                    content=item.get('content') or item.get('task') or item.get('title') or item.get('text')
                    if not content: continue
                    evidence=item.get('evidence_ids') or []
                    if not evidence: continue
                    tid=hashlib.sha256(dumps([cid,content,sorted(evidence)]).encode()).hexdigest()
                    self.conn.execute('INSERT INTO todos VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET analysis_id=excluded.analysis_id,evidence_ids=excluded.evidence_ids,status=CASE WHEN todos.user_corrected=1 THEN todos.status ELSE excluded.status END,updated_at=excluded.updated_at',(tid,cid,aid,str(content),item.get('owner'),item.get('due_at') or item.get('deadline'),'candidate',dumps(evidence),0,now()))
        return aid

    def analyses(self,cid=None):
        with self.lock:
            rows=self.conn.execute('SELECT * FROM analyses'+(' WHERE conversation_id=?' if cid else '')+' ORDER BY generated_at DESC',([cid] if cid else [])).fetchall()
            result=[]
            for row in rows:
                d=dict(row);d['result']=json.loads(d['result']);d['input_ids']=json.loads(d['input_ids']);result.append(d)
            return result

    def tasks(self):
        with self.lock:
            tasks=[]
            for row in self.conn.execute('SELECT * FROM todos ORDER BY due_at IS NULL,due_at,id'):
                d=dict(row);d['evidence_ids']=json.loads(d['evidence_ids']);
                analysis=self.conn.execute('SELECT status FROM analyses WHERE id=?',(d['analysis_id'],)).fetchone()
                d['evidence_status']='valid' if analysis and analysis['status']=='valid' and d['status']!='stale' else 'invalid'
                tasks.append(d)
            jobs=[]
            for row in self.conn.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200'):
                d=dict(row);d['payload']=json.loads(d['payload']);jobs.append(d)
            return {'tasks':tasks,'jobs':jobs}

    def update_task(self,payload):
        tid=payload.get('id')
        for key in ('content','owner','due_at'):
            if key in payload and payload[key] is not None and not isinstance(payload[key],str):raise ValueError(key+' 必须为字符串或空值')
        if 'content' in payload and not payload['content']:raise ValueError('待办内容不可为空')
        if 'status' in payload and payload['status'] not in ('candidate','confirmed','done','dismissed'): raise ValueError('待办状态无效')
        if payload.get('due_at'):
            value=payload['due_at']
            if isinstance(value,str) and len(value)==10:
                try: datetime.strptime(value,'%Y-%m-%d')
                except ValueError: raise ValueError('due_at 日期无效')
            else: iso(value,'due_at')
        with self.lock,self.conn:
            if not self.conn.execute('SELECT id FROM todos WHERE id=?',(tid,)).fetchone(): raise ValueError('待办不存在')
            for key in ('content','owner','due_at','status'):
                if key in payload: self.conn.execute('UPDATE todos SET '+key+'=?,user_corrected=1,updated_at=? WHERE id=?',(payload[key],now(),tid))
            row=dict(self.conn.execute('SELECT * FROM todos WHERE id=?',(tid,)).fetchone());row['evidence_ids']=json.loads(row['evidence_ids']);return {'task':row}

    def correct_analysis(self,aid,result):
        if not isinstance(result,dict) or not isinstance(result.get('items'),list):raise ValueError('修正结果必须包含items列表')
        for item in result['items']:
            if not isinstance(item,dict) or not isinstance(item.get('evidence_ids'),list) or not item['evidence_ids']:raise ValueError('每项修正结论必须关联原文')
        with self.lock,self.conn:
            row=self.conn.execute('SELECT * FROM analyses WHERE id=?',(aid,)).fetchone()
            if not row: raise ValueError('分析不存在')
            valid=set(json.loads(row['input_ids']))
            def check(obj):
                if isinstance(obj,dict):
                    for k,v in obj.items():
                        if k=='evidence_ids' and (not isinstance(v,list) or not set(v).issubset(valid)):raise ValueError('修正包含无效引用')
                        check(v)
                if isinstance(obj,list):
                    for v in obj:check(v)
            check(result)
            self.conn.execute('UPDATE analyses SET result=?,user_corrected=1 WHERE id=?',(dumps(result),aid))
        return {'ok':True}

    def checkpoint(self,cid):
        with self.lock:
            row=self.conn.execute('SELECT * FROM checkpoints WHERE id=?',(cid,)).fetchone()
            if not row:return None
            result=dict(row)
            result['metadata']=json.loads(result['metadata'])
            return result

    def _delete_capture_checkpoints(self,cid):
        self.conn.execute('DELETE FROM checkpoints WHERE id=?',('vision:'+cid,))
        for row in self.conn.execute('SELECT id,metadata FROM checkpoints').fetchall():
            try:belongs=json.loads(row['metadata']).get('conversation_id')==cid
            except (ValueError,TypeError,AttributeError):belongs=False
            if belongs:self.conn.execute('DELETE FROM checkpoints WHERE id=?',(row['id'],))

    def delete_conversation(self,cid):
        with self.lock,self.conn:
            count=self.conn.execute('SELECT COUNT(*) FROM messages WHERE conversation_id=?',(cid,)).fetchone()[0]
            self._delete_capture_checkpoints(cid)
            self.conn.execute('DELETE FROM conversations WHERE id=?',(cid,))
            # Replay checkpoints can contain removed message input; discard all replay resume metadata.
            self.conn.execute("DELETE FROM checkpoints WHERE id LIKE 'replay:%'")
            self.conn.execute("UPDATE settings SET value=? WHERE key='cloud_conversation_ids'",(dumps([x for x in self.settings()['cloud_conversation_ids'] if x!=cid]),))
        with self.lock:
            self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.conn.execute('VACUUM')
        return {'ok':True,'deleted_messages':count}
