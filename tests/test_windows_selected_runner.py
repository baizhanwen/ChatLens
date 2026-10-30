"""Synthetic fixtures/fake Windows only. Never open a real account or process."""
import contextlib
import hashlib
import hmac
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts/windows-proof'
if str(SCRIPTS) not in sys.path:sys.path.insert(0,str(SCRIPTS))
import windows_selected_runner as R
import windows_selected_engine as E
import windows_selected_native as N

KEYS=[bytes((n+i)%256 for i in range(32)) for n in (1,41,81,121)]
SALTS=[bytes((n+i)%256 for i in range(16)) for n in (1,21,41,61)]
RELS=('message/message_0.db','message/message_1.db','contact/contact.db')
BG={'probe_succeeded':True,'running':True,'frontmost':False,'screen_locked':False}


def page(index):
    raw=bytes(i%256 for i in range(4016))
    mk=hashlib.pbkdf2_hmac('sha512',KEYS[index],bytes(x^0x3a for x in SALTS[index]),2,32)
    return SALTS[index]+raw+hmac.new(mk,raw+struct.pack('<I',1),hashlib.sha512).digest()


class Memory:
    def __init__(self,*args):
        self.base=0x10000000;self.data=bytearray(0x8000);self.creation_time=123
        self.data[0x80:0x80+len(R.proof.MARKER)]=R.proof.MARKER
        for i in range(4):
            node=0x200+i*0x100;config=0x2000+i*0x100;address=0x5000+i*0x100
            struct.pack_into('<QQ',self.data,node+16,self.base+0x80,len(R.proof.MARKER))
            struct.pack_into('<Q',self.data,node+0x28,self.base+config)
            literal=b"x'"+KEYS[i].hex().encode()+SALTS[i].hex().encode()+b"'"
            encoded=bytes(x^R.proof.MASK[j%len(R.proof.MASK)] for j,x in enumerate(literal))
            struct.pack_into('<QQ',self.data,config+0x88+8,self.base+address,len(encoded))
            self.data[address:address+len(encoded)]=encoded
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def regions(self,include_image=False):return iter([(self.base,len(self.data))])
    def read(self,address,size):
        offset=address-self.base
        return bytes(self.data[offset:offset+size]) if 0<=offset<=len(self.data)-size else b''


class SelectedRunnerTests(unittest.TestCase):
    def test_three_selected_keys_authenticate_and_other_salt_is_not_kept(self):
        memory=Memory();stats={};budget=R.proof.Budget(memory)
        result=R.selected_keys(memory,{rel:page(i) for i,rel in enumerate(RELS)},KEYS[0],budget,stats)
        self.assertEqual(result,{rel:KEYS[i] for i,rel in enumerate(RELS)})
        self.assertEqual(stats['candidate_count'],2)
        self.assertNotIn(KEYS[3],result.values())

    def test_missing_key_and_budget_cannot_report_partial_success(self):
        memory=Memory();memory.data[0x5200:0x5200+99]=bytes(99)
        with self.assertRaisesRegex(R.RunError,'selected_database_keys_incomplete'):
            R.selected_keys(memory,{rel:page(i) for i,rel in enumerate(RELS)},KEYS[0],R.proof.Budget(memory),{})
        with self.assertRaisesRegex(R.proof.ProofError,'scan_byte_limit'):
            R.selected_keys(Memory(),{rel:page(i) for i,rel in enumerate(RELS)},KEYS[0],R.proof.Budget(Memory(),max_bytes=1),{})

    def test_synthetic_driver_smoke_checks_real_encryption_and_wal(self):
        try:import sqlcipher3.dbapi2 as driver
        except ImportError:self.skipTest('isolated SQLCipher driver required')
        with tempfile.TemporaryDirectory() as tmp:
            result=R.smoke(driver,Path(tmp))
            self.assertEqual(result['status'],'通过')
            self.assertTrue(result['wal_committed_visible'])
            self.assertTrue(result['wal_uncommitted_hidden'])
            self.assertTrue(result['readonly_write_denied'])
            self.assertNotEqual((Path(tmp)/'synthetic-driver-smoke.db').read_bytes()[:16],b'SQLite format 3\0')

    def test_unsafe_message_directory_is_refused_before_enumeration(self):
        class Directory:
            def __truediv__(self,name):return self
            def iterdir(self):raise AssertionError('must not enumerate a redirected directory')
        with patch.object(R.platform,'path_is_local',return_value=False):
            with self.assertRaisesRegex(R.RunError,'selected_message_directory_unsafe'):
                R.fixed_shards(Directory())

    def test_existing_output_is_not_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'existing';out.mkdir();(out/'user.txt').write_text('KEEP')
            argv=['runner','--key-file',str(Path(tmp)/'private/validated-message0-key.json'),'--pid','42','--creation-time','123','--output',str(out)]
            with patch.object(R.sys,'argv',argv),patch.object(R.sys,'platform','win32'),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(R.main(),2)
            self.assertEqual([p.name for p in out.iterdir()],['user.txt'])
            self.assertEqual((out/'user.txt').read_text(),'KEEP')

    def test_final_foreground_rejects_even_if_engine_summary_was_complete(self):
        try:import sqlcipher3.dbapi2
        except ImportError:self.skipTest('isolated SQLCipher driver required')
        for foreground in (False,True):
            with self.subTest(final_foreground=foreground),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);account=base/'synthetic-account/db_storage';(account/'message').mkdir(parents=True)
                for name in ['message_0.db','message_1.db']:(account/'message'/name).write_bytes(b'synthetic')
                key=base/'third/private/validated-message0-key.json';key.parent.mkdir(parents=True)
                key.write_text(json.dumps({'schema_version':1,'kind':'direct_sqlcipher_encryption_key','database':RELS[0],
                                          'db_root':str(account),'exe_path':str(base/'Weixin.exe'),
                                          'key_hex':KEYS[0].hex(),'salt_hex':SALTS[0].hex()}))
                output=base/'new-run'
                class Native:
                    def __init__(self,*a):self.calls=0
                    def __enter__(self):return self
                    def __exit__(self,*a):pass
                    def sample(self):
                        self.calls+=1
                        return dict(BG,frontmost=foreground if self.calls>=4 else False)
                class Poller:
                    baseline_completed_at='2026-01-01T00:00:00+00:00'
                    def __init__(self,*a):pass
                    def poll(self,callback):
                        callback();callback()
                        return [{'alias':alias,'token_index':index,'event_id_digest':'synthetic-%s-%s'%(alias,index),
                                 'body_exact_match':True,'expected_sender_match':True,'background_confirmed':True}
                                for alias in ['private_test','group_test'] for index in [1,2]]
                    def summary(self):return {'status':'通过','all_expected_tokens_observed':True,'unique_source_messages':4,'observed_events':4}
                    def close(self):pass
                def read_page(p):return page(0 if p.name=='message_0.db' else 1 if p.name=='message_1.db' else 2)
                def private_json(p,d):p.write_text(json.dumps(d))
                argv=['runner','--key-file',str(key),'--pid','42','--creation-time','123','--output',str(output)]
                stack=contextlib.ExitStack()
                with stack:
                    for obj,name,value in [
                        (R.sys,'argv',argv),(R.sys,'platform','win32'),(R.platform,'path_is_local',lambda *a:True),
                        (R.platform,'private_dir',lambda p:p.mkdir()),(R.platform,'private_json',private_json),
                        (R.platform,'read_page',read_page),(R.platform,'WindowsProcess',Memory),
                        (R,'smoke',lambda *a:{'status':'通过'}),(N,'NativeState',Native),
                        (N,'detect_shm_preconditions',lambda *a:{}),(E,'resolve',lambda *a:{'conversations':[]}),
                        (E,'Poller',Poller)]:stack.enter_context(patch.object(obj,name,value))
                    stack.enter_context(patch('builtins.input',side_effect=['SyntheticPrivate','SyntheticGroup','']))
                    stdout=stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    self.assertEqual(R.main(),2 if foreground else 0)
                public_text=(output/'result-to-send.json').read_text();public=json.loads(public_text)
                self.assertEqual(public['status'],'未验证' if foreground else '通过')
                self.assertEqual(public['business_read_status'],'未验证' if foreground else '通过')
                self.assertNotIn(str(base),public_text)
                for item in KEYS+SALTS:self.assertNotIn(item.hex(),public_text)
                self.assertIn('READY',stdout.getvalue())
                for item in KEYS:self.assertNotIn(item.hex(),stdout.getvalue())


if __name__=='__main__':unittest.main()
