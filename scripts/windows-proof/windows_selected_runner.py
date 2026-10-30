"""Finite Windows selected-chat test. No model, messages sent or raw text export."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path
import re
import secrets
import struct
import sys
import time

import windows_keyproof_core as proof
import windows_keyproof_platform as platform


class RunError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def now():
    return datetime.now(timezone.utc).isoformat()


def visible_input(prompt):
    # CPython's console readline can send input(prompt) to C stderr.
    # Keep the UI visible when the launcher suppresses private diagnostics.
    print(prompt,flush=True)
    return input()


def bounded_regions(source, budget, image=False):
    rows = []
    for row in source.regions(include_image=image):
        budget.check()
        rows.append(row)
        if len(rows) > 100000:
            raise RunError('scan_region_limit')
    return rows


def fixed_shards(root):
    directory=root/'message'
    if not platform.path_is_local(directory,True):
        raise RunError('selected_message_directory_unsafe')
    entries=list(itertools.islice(directory.iterdir(),65))
    if len(entries)>64:
        raise RunError('message_directory_entry_limit')
    actual={p.name for p in entries if re.fullmatch(r'message_[0-9]+\.db',p.name)}
    if actual!={'message_0.db','message_1.db'}:
        raise RunError('expected_two_message_shards_changed')


def selected_keys(source, pages, cached, budget, stats, progress=None):
    """Authenticate literals only against these three explicitly selected pages."""
    found = {}
    for relative, page in pages.items():
        if proof.page_authenticates(cached, page):
            found[relative] = cached
    if len(found) == len(pages):
        return found
    if progress is not None:
        progress('memory_markers')
    regions = bounded_regions(source, budget, True)
    stats['marker_search_region_count'] = len(regions)
    markers = set()
    for base, data in proof.chunks(regions, budget, len(proof.MARKER)-1):
        pos = data.find(proof.MARKER)
        while pos >= 0:
            budget.check()
            markers.add(base + pos)
            if len(markers) > 128:
                raise RunError('scan_marker_limit')
            pos = data.find(proof.MARKER, pos+1)
    stats['marker_count'] = len(markers)
    if not markers:
        raise RunError('selected_database_keys_incomplete')
    patterns = [struct.pack('<QQ', address, len(proof.MARKER)) for address in sorted(markers)]
    if progress is not None:
        progress('memory_references')
    regions = bounded_regions(source, budget)
    stats['reference_search_region_count'] = len(regions)
    refs, configs, candidates = set(), set(), set()
    for base, data in proof.chunks(regions, budget, 128):
        for pattern in patterns:
            budget.check()
            pos = data.find(pattern)
            while pos >= 0:
                budget.check()
                ref = base + pos
                pos = data.find(pattern, pos+1)
                if ref in refs:
                    continue
                refs.add(ref)
                if len(refs) > 4096:
                    raise RunError('scan_reference_limit')
                node = budget.read(ref-16, 0x50)
                if len(node) != 0x50 or proof.u64(node,16) not in markers or proof.u64(node,24) != len(proof.MARKER):
                    continue
                pointer = proof.u64(node,0x28)
                if not 0x10000 <= pointer < proof.MAX_ADDRESS-0xb0 or pointer in configs:
                    continue
                configs.add(pointer)
                obj = budget.read(pointer+0x88,0x28)
                if len(obj) != 0x28:
                    continue
                address,size = proof.u64(obj,8),proof.u64(obj,16)
                if not 0 < size <= 1024 or not 0x10000 <= address < proof.MAX_ADDRESS-size:
                    continue
                blob = budget.read(address,size)
                if len(blob) != size:
                    continue
                decoded = bytes(v ^ proof.MASK[i%len(proof.MASK)] for i,v in enumerate(blob))
                for match in proof.LITERAL.finditer(decoded):
                    salt = bytes.fromhex(match.group(2).decode('ascii'))
                    if salt not in {page[:16] for relative,page in pages.items() if relative not in found}:
                        continue
                    key = bytes.fromhex(match.group(1).decode('ascii'))
                    if (key,salt) in candidates:
                        continue
                    candidates.add((key,salt))
                    if len(candidates) > 2048:
                        raise RunError('scan_candidate_limit')
                    for relative,page in pages.items():
                        if relative not in found and page[:16] == salt and proof.page_authenticates(key,page):
                            found[relative] = key
                    if len(found) == len(pages):
                        stats.update(reference_count=len(refs),config_count=len(configs),candidate_count=len(candidates))
                        return found
    raise RunError('selected_database_keys_incomplete')


def smoke(driver, private):
    """Create a brand-new synthetic encrypted DB; never use the account key."""
    import zstandard
    test_key = secrets.token_hex(32)
    path = private/'synthetic-driver-smoke.db'
    if path.exists():
        raise RunError('synthetic_smoke_path_exists')
    writer = reader = None
    try:
        writer = driver.connect(str(path), timeout=2)
        writer.execute('PRAGMA key = "x\'%s\'"' % test_key)
        writer.execute('PRAGMA cipher_compatibility=4')
        writer.execute('PRAGMA cipher_page_size=4096')
        version = writer.execute('PRAGMA cipher_version').fetchone()
        if not version or not re.fullmatch(r'[0-9.]+(?:[ a-zA-Z0-9_-]{0,64})?',version[0]):
            raise RunError('sqlcipher_driver_version_unavailable')
        writer.execute('CREATE TABLE synthetic_only (id INTEGER PRIMARY KEY,value TEXT)')
        writer.execute("INSERT INTO synthetic_only VALUES(1,'SYNTHETIC_COMMITTED')")
        writer.commit()
        if writer.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
            raise RunError('synthetic_wal_not_enabled')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        reader = driver.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
        reader.execute('PRAGMA key = "x\'%s\'"' % test_key)
        reader.execute('PRAGMA cipher_compatibility=4')
        reader.execute('PRAGMA query_only=ON')
        if reader.execute('SELECT count(*) FROM synthetic_only').fetchone()[0] != 1:
            raise RunError('synthetic_read_failed')
        writer.execute("INSERT INTO synthetic_only VALUES(2,'SYNTHETIC_WAL_ONLY')")
        if reader.execute('SELECT count(*) FROM synthetic_only').fetchone()[0] != 1:
            raise RunError('synthetic_uncommitted_wal_visible')
        writer.commit()
        if reader.execute('SELECT count(*) FROM synthetic_only').fetchone()[0] != 2:
            raise RunError('synthetic_committed_wal_invisible')
        denied = False
        try:
            reader.execute("INSERT INTO synthetic_only VALUES(3,'FORBIDDEN')")
        except driver.DatabaseError:
            denied = True
        reader.rollback()
        if not denied:
            raise RunError('synthetic_readonly_write_not_denied')
        raw = b'EXPLICIT_SYNTHETIC_ZSTD_FRAME'
        encoded = zstandard.ZstdCompressor().compress(raw)
        if zstandard.ZstdDecompressor().decompress(encoded,max_output_size=1024) != raw:
            raise RunError('synthetic_compression_smoke_failed')
        return {'status':'通过','cipher_version':version[0],'sqlite_version':str(driver.sqlite_version),
                'wal_committed_visible':True,'wal_uncommitted_hidden':True,'readonly_write_denied':True,
                'compression_verified':True,'scope':'new_synthetic_private_fixture_only'}
    finally:
        if reader is not None:
            reader.close()
        if writer is not None:
            writer.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file',type=Path,required=True)
    parser.add_argument('--pid',type=int,required=True)
    parser.add_argument('--creation-time',type=int,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--progress-file',type=Path)
    args=parser.parse_args()
    report={'schema_version':1,'stage':'selected_live','recorded_at_utc':now(),
            'scope':'two_user_selected_conversations_four_controlled_new_texts_only',
            'status':'未验证','reason':'not_started','business_read_status':'未验证',
            'python_version':sys.version.split()[0],'target_client_version':'4.1.15.56',
            'key_authentication':{},'driver_smoke':{'status':'未验证'},'contact_resolution':{'status':'未验证'},
            'baseline_completed_at':None,'new_observations':[],'summary':None,
            'native_state_before':None,'native_state_after':None,
            'scan_stats':{},'messages_sent_by_tool':0,'cloud_calls':0,
            'private_acl_verified':False,'selected_keys_saved':False,'poll_retries':0,
            'restart_recovery_status':'未验证','strict_end_to_end_latency_measured':False,
            'source_database_records_modified':False,'source_process_modified':False,
            'source_shm_may_receive_normal_sqlite_lock_metadata':True}
    owned=False; saved=False; budget=None; poller=None; progress=None
    def phase(stage):
        report['last_stage']=stage
        if progress is not None:
            progress.set(stage)
    try:
        from windows_selected_progress import PhaseProgress
        from windows_selected_engine import resolve,Poller
        from windows_selected_native import NativeState,detect_shm_preconditions
        progress=PhaseProgress(args.progress_file,output_dir=args.output)
        phase('startup')
        if sys.platform != 'win32' or struct.calcsize('P') != 8:
            raise RunError('windows_x64_runtime_required')
        if args.output.exists() or not platform.path_is_local(args.output.parent,True):
            raise RunError('new_local_output_required')
        phase('private_setup')
        args.output.mkdir();owned=True
        private=args.output/'private';platform.private_dir(private)
        report['private_acl_verified']=True
        phase('config_validation')
        if not platform.path_is_local(args.key_file) or args.key_file.name!='validated-message0-key.json' or args.key_file.parent.name!='private' or args.key_file.stat().st_size>16384:
            raise RunError('selected_private_message0_key_required')
        document=json.loads(args.key_file.read_text(encoding='utf-8-sig'))
        if document.get('schema_version')!=1 or document.get('kind')!='direct_sqlcipher_encryption_key' or document.get('database')!='message/message_0.db':
            raise RunError('selected_key_document_invalid')
        value=document.get('key_hex');salt=document.get('salt_hex')
        if not isinstance(value,str) or not re.fullmatch(r'[0-9a-fA-F]{64}',value) or not isinstance(salt,str) or not re.fullmatch(r'[0-9a-fA-F]{32}',salt):
            raise RunError('selected_key_encoding_invalid')
        root=Path(document.get('db_root') or '')
        exe=Path(document.get('exe_path') or '')
        if not platform.path_is_local(root,True) or root.name.lower()!='db_storage' or not platform.path_is_local(exe):
            raise RunError('selected_account_paths_invalid')
        phase('driver_smoke')
        import sqlcipher3.dbapi2 as driver
        print('正在包内私有目录验证虚构加密数据库、WAL 和只读驱动...',flush=True)
        report['driver_smoke']=smoke(driver,private)
        print('虚构驱动验证通过。下面只初始化所选账号的两个消息库及联系人库。',flush=True)
        relatives=('message/message_0.db','message/message_1.db','contact/contact.db')
        pages={}
        # Refuse unexpected extra shards rather than silently claiming complete coverage.
        phase('file_preflight')
        fixed_shards(root)
        for relative,stage in zip(relatives,('read_message0','read_message1','read_contact')):
            phase(stage)
            path=root/relative
            if not platform.path_is_local(path):
                raise RunError('selected_database_path_unavailable')
            pages[relative]=platform.read_page(path)
        phase('cached_key_check')
        if pages[relatives[0]][:16].hex()!=salt.lower() or not proof.page_authenticates(bytes.fromhex(value),pages[relatives[0]]):
            raise RunError('saved_message0_key_or_salt_changed')
        phase('process_identity')
        with platform.WindowsProcess(args.pid,str(exe),args.creation_time) as source:
            budget=proof.Budget(source)
            keys=selected_keys(source,pages,bytes.fromhex(value),budget,report['scan_stats'],phase)
        for relative in relatives:
            report['key_authentication'][relative]=proof.page_authenticates(keys[relative],pages[relative])
        phase('saving_keys')
        platform.private_json(private/'selected-keys.json',{'schema_version':1,'db_root':str(root),'exe_path':str(exe),
                             'keys':{relative:{'enc_key':key.hex()} for relative,key in keys.items()}})
        report['selected_keys_saved']=True
        # The contact connection also participates in normal WAL reads.
        phase('checking_wal')
        report['wal_preconditions']=detect_shm_preconditions(root,list(relatives))
        phase('waiting_private_name')
        private_name=visible_input('输入本轮私聊的准确名称（仅留本机），然后按回车：').strip()
        phase('waiting_group_name')
        group_name=visible_input('输入本轮群聊的准确名称（仅留本机），然后按回车：').strip()
        for name in (private_name,group_name):
            if not name or len(name.encode('utf-8'))>2048 or '\0' in name:
                raise RunError('selected_contact_name_invalid')
        key_hex={relative:key.hex() for relative,key in keys.items()}
        phase('contact_lookup')
        config=resolve(driver,root,key_hex,private_name,group_name)
        report['contact_resolution']={'status':'通过','private_test_matches':1,'group_test_matches':1}
        nonce=secrets.token_hex(4).upper()
        tokens={alias:['%s-%s-%d-%s'%(prefix,nonce,i,secrets.token_hex(3).upper()) for i in (1,2)]
                for alias,prefix in [('private_test','CLWP'),('group_test','CLWG')]}
        phase('saving_config')
        platform.private_json(private/'selected-config.json',{'db_root':str(root),'config':config,'tokens':tokens,'run_nonce':nonce})
        phase('native_precheck')
        with NativeState(args.pid,str(exe),args.creation_time) as native:
            state=native.sample();report['native_state_before']=state
            if not (state.get('probe_succeeded') is True and state.get('running') is True and state.get('frontmost') is False and state.get('screen_locked') is False):
                raise RunError('wechat_must_be_running_unlocked_and_in_background')
            phase('message_baseline')
            poller=Poller(driver,root,key_hex,config,tokens)
            report['baseline_completed_at']=getattr(poller,'baseline_completed_at',now())
            phase('waiting_start')
            print('\nREADY：已就绪，旧消息基线已建立；仅监听你选定的私聊和群。',flush=True)
            print('请让同一位受控发送人按顺序各发一次以下四条，每条单独发送：',flush=True)
            print('1. 私聊：'+tokens['private_test'][0],flush=True)
            print('2. 群聊：'+tokens['group_test'][0],flush=True)
            print('3. 私聊：'+tokens['private_test'][1],flush=True)
            print('4. 群聊：'+tokens['group_test'][1],flush=True)
            print('请保持电脑微信在后台，不在电脑打开这两个会话；等待最多五分钟。',flush=True)
            visible_input('准备好后按回车开始计时，再让同一位测试朋友发送上述四条：')
            phase('collecting')
            deadline=time.monotonic()+300
            retry_count=0
            while time.monotonic()<deadline:
                fixed_shards(root)
                try:
                    observations=poller.poll(native.sample)
                    retry_count=0
                except Exception as error:
                    if getattr(error,'code',None)=='selected_poll_or_native_state_failed' and retry_count<3:
                        retry_count+=1
                        report['poll_retries']+=1
                        time.sleep(0.5)
                        continue
                    raise
                for row in observations:
                    report['new_observations'].append(row)
                    print('已取得测试事件：%s #%s'%(row['alias'],row['token_index']),flush=True)
                summary=poller.summary()
                if summary.get('all_expected_tokens_observed') is True or len(report['new_observations'])>=4:
                    break
                time.sleep(0.5)
            report['summary']=poller.summary()
            report['native_state_after']=native.sample()
            summary=report['summary']
            final_state=report['native_state_after']
            final_background=(final_state.get('probe_succeeded') is True and final_state.get('running') is True
                              and final_state.get('frontmost') is False and final_state.get('screen_locked') is False)
            if (summary.get('status')=='通过' and summary.get('all_expected_tokens_observed') is True
                and summary.get('unique_source_messages')==4 and summary.get('observed_events')==4 and final_background):
                report.update(status='通过',business_read_status='通过',reason='four_selected_controlled_new_texts_verified')
            else:
                report.update(status='未验证',reason='controlled_sample_incomplete_or_mismatched')
    except (Exception,KeyboardInterrupt) as error:
        code=getattr(error,'code',None)
        if code is None and isinstance(error,proof.ProofError):
            code=str(error)
        if not isinstance(code,str) or not re.fullmatch(r'[a-z0-9_]{1,100}',code):
            code='selected_test_interrupted' if isinstance(error,KeyboardInterrupt) else 'selected_test_operation_failed'
        report['status']='待外部条件' if ('incomplete' in code or 'unavailable' in code or 'changed' in code or 'background' in code or 'scan_' in code) else '失败'
        report['reason']=code
    finally:
        if progress is not None:
            try:progress.set('finalizing')
            except Exception:report['progress_finalization_failed']=True
        if poller is not None:
            try:poller.close()
            except Exception:pass
        if budget is not None:
            report['scan_stats'].update(read_calls=budget.calls,read_request_bytes=budget.requested,read_returned_bytes=budget.received)
        report['finished_at_utc']=now()
        if owned:
            try:
                with (args.output/'result-to-send.json').open('x',encoding='utf-8') as target:
                    json.dump(report,target,ensure_ascii=False,indent=2)
                saved=True
                print('\n测试已结束。请只回传本轮 result-to-send.json；private 留在本机。',flush=True)
                print('状态：%s；原因：%s'%(report['status'],report['reason']),flush=True)
            except OSError:
                print('结果未能保存，请只反馈此固定状态，不发送 private。',flush=True)
    return 0 if saved and report['status']=='通过' else 2


if __name__=='__main__':
    raise SystemExit(main())
