"""Synthetic SDK handle tables and subprocess boundaries; no Windows access."""
import ctypes as C
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

MODULES = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))
import windows_account_probe as probe
from windows_process_detection import validate_process_family

ROOT = r'C:\Users\synthetic\wxid_fixture_0123\db_storage'
OTHER = r'C:\Users\synthetic\wxid_other_5678\db_storage'


class Snapshot:
    def __init__(self, paths=(), failure=None):
        self.paths, self.failure, self.index = list(paths), failure, 0
        self.calls, self.buffers = [], []

    def PssCaptureSnapshot(self, process, flags, context, target):
        self.calls.append(('capture', process, flags, context))
        if self.failure == 'capture':
            return 5
        C.cast(target, C.POINTER(C.c_void_p))[0] = C.c_void_p(7)
        return 0

    def PssWalkMarkerCreate(self, allocator, target):
        self.calls.append(('marker', allocator))
        if self.failure == 'marker':
            return 5
        C.cast(target, C.POINTER(C.c_void_p))[0] = C.c_void_p(8)
        return 0

    def PssWalkSnapshot(self, snapshot, information, marker, target, size):
        self.calls.append(('walk', information, size))
        if self.failure == 'walk':
            return 24
        if self.index == len(self.paths):
            return 259
        kind, path = self.paths[self.index]
        self.index += 1
        entry = C.cast(target, C.POINTER(probe.HandleEntry)).contents
        typename, name = C.create_unicode_buffer(kind), C.create_unicode_buffer(path)
        self.buffers.extend([typename, name])
        entry.Flags = 2
        entry.TypeNameLength, entry.TypeName = len(kind) * 2, C.addressof(typename)
        entry.ObjectNameLength, entry.ObjectName = len(path) * 2, C.addressof(name)
        return 0

    def PssWalkMarkerFree(self, marker):
        self.calls.append(('free_marker', marker.value))

    def PssFreeSnapshot(self, process, snapshot):
        self.calls.append(('free_snapshot', process, snapshot.value))


class WindowsAccountProbeTests(unittest.TestCase):
    def family_probe(self, paths_by_pid, *, families=None, denied=(), capture=None, fail_member=None,
                     reused_member=None, wrong_image=None, clock=None):
        before=[{'pid':12,'parent_pid':8,'creation_time':34},
                {'pid':13,'parent_pid':12,'creation_time':35}]
        families = families if families is not None else [before,before]
        evidence={'family_calls':[],'opened':[],'closed':[],'queries':{},'times':{}}
        executable=r'C:\synthetic\Weixin.exe'
        class FamilySnapshot(Snapshot):
            def PssCaptureSnapshot(self,process,*arguments):
                self.current=process
                self.paths=list(paths_by_pid.get(process,()))
                self.index=0
                if capture is not None:capture(process)
                return super().PssCaptureSnapshot(process,*arguments)
            def PssWalkSnapshot(self,*arguments):
                if self.current==fail_member:return 24
                return super().PssWalkSnapshot(*arguments)
        api=FamilySnapshot()
        api.GetLogicalDrives=lambda:0
        def opened(access,inherit,pid):
            evidence['opened'].append((access,inherit,pid))
            return None if pid in denied else pid
        def image(pid,flags,buffer,size):
            evidence['queries'][pid]=evidence['queries'].get(pid,0)+1
            buffer.value=(r'C:\unrelated\Weixin.exe' if pid==wrong_image and evidence['queries'][pid]>1 else executable)
            C.cast(size,C.POINTER(C.c_uint32)).contents.value=len(buffer.value)
            return 1
        def created(pid,timestamp,*unused):
            evidence['times'][pid]=evidence['times'].get(pid,0)+1
            birth={12:34,13:35}.get(pid,36)
            if pid==reused_member and evidence['times'][pid]>1:birth+=100
            C.cast(timestamp,C.POINTER(probe.FILETIME)).contents.low=birth
            return 1
        def family(exe,pid,birth,**options):
            evidence['family_calls'].append((exe,pid,birth,options['deadline']))
            if callable(families):rows=families(len(evidence['family_calls']))
            else:rows=families[min(len(evidence['family_calls'])-1,len(families)-1)]
            return validate_process_family(rows,pid,birth)
        native=SimpleNamespace(OpenProcess=opened,CloseHandle=lambda pid:evidence['closed'].append(pid) or 1,
            QueryFullProcessImageNameW=image,GetProcessTimes=created,GetCurrentProcess=lambda:99)
        with patch.object(probe,'_api',return_value=native),patch.object(probe,'_SnapshotAPI',return_value=api), \
             patch.object(probe,'path_is_local',return_value=True),patch.object(probe,'verified_process_family',side_effect=family):
            result=probe.probe_direct(12,34,executable,ROOT,diagnostics={},clock=clock or (lambda:0))
        return result,api,evidence

    def direct(self, api, *, diagnostics=None):
        executable = r'C:\synthetic\Weixin.exe'
        def image(handle, flags, buffer, size):
            buffer.value = executable
            C.cast(size,C.POINTER(C.c_uint32)).contents.value = len(executable)
            return 1
        def created(handle, timestamp, *unused):
            C.cast(timestamp,C.POINTER(probe.FILETIME)).contents.low = 34
            return 1
        native=SimpleNamespace(OpenProcess=lambda *_:42,CloseHandle=lambda _:1,
            QueryFullProcessImageNameW=image,GetProcessTimes=created,GetCurrentProcess=lambda:99)
        api.GetLogicalDrives=lambda:0
        with patch.object(probe,'_api',return_value=native),patch.object(probe,'_SnapshotAPI',return_value=api), \
             patch.object(probe,'path_is_local',return_value=True), \
             patch.object(probe,'verified_process_family',return_value=[{'pid':12,'parent_pid':8,'creation_time':34}]):
            return probe.probe_direct(12,34,executable,ROOT,diagnostics=diagnostics)

    def test_only_unique_matching_root_is_accepted_and_paths_are_never_returned(self):
        paths = [ROOT + r'\message\message_0.db', ROOT + r'\contact\contact.db-wal']
        result = probe.classify_paths(paths, ROOT.upper())
        self.assertTrue(result['matched'])
        self.assertNotIn('Users', json.dumps(result))
        for actual in ([OTHER + r'\message\message_0.db'], paths + [OTHER + r'\message\message_1.db']):
            self.assertEqual(probe.classify_paths(actual, ROOT)['state'], 'mismatch')

    def test_unresolved_device_empty_or_malformed_metadata_never_matches(self):
        for paths in ([], ['synthetic-non-db'], [None], [ROOT + '\0'], [r'\Device\Unmapped\db_storage\message\message_0.db']):
            self.assertEqual(probe.classify_paths(paths, ROOT)['state'], 'unknown')
        self.assertEqual(probe.classify_paths([ROOT + r'\message\message_0.db'], 'relative/db_storage')['state'], 'unknown')

    def test_native_device_and_long_path_mapping_does_not_confuse_prefixes(self):
        device = r'\Device\HarddiskVolume3'
        path = device + ROOT[2:] + r'\message\message_0.db-shm'
        self.assertTrue(probe.classify_paths([path], ROOT, {device: 'C:'})['matched'])
        self.assertTrue(probe.classify_paths(['\\\\?\\' + ROOT + r'\message\message_0.db'], ROOT)['matched'])
        bad = r'\Device\HarddiskVolume30' + ROOT[2:] + r'\message\message_0.db'
        self.assertFalse(probe.classify_paths([bad], ROOT, {device: 'C:'})['matched'])

    def snapshot(self, api, **options):
        with patch.object(probe, '_api', return_value=SimpleNamespace(GetCurrentProcess=lambda: 99)):
            return probe.snapshot_paths(api, 42, **options)

    def test_sdk_byte_count_decodes_exact_or_one_terminal_nul_including_bmp_path(self):
        path = r'C:\Users\synthetic\中文账号\db_storage\message\message_0.db'
        for raw, expected in (('File', 'File'), ('File\0', 'File'), (path + '\0', path)):
            with self.subTest(raw=raw):
                buffer = C.create_unicode_buffer(raw)
                pointer, byte_length = C.addressof(buffer), len(raw) * 2
                with patch.object(probe.C, 'wstring_at', wraps=C.wstring_at) as read:
                    self.assertEqual(probe._wide(pointer, byte_length), expected)
                read.assert_called_once_with(pointer, byte_length // 2)

    def test_snapshot_single_terminal_nuls_preserve_file_type_and_database_evidence(self):
        path = ROOT + r'\message\message_0.db'
        counts = {}
        api = Snapshot([('File\0', path + '\0')])
        self.assertEqual(self.snapshot(api, diagnostics=counts), [path])
        self.assertEqual(counts['file_type_handles'], 1)
        self.assertEqual(counts['file_names_retained'], 1)
        self.assertTrue(counts['query_completed'])
        result = self.direct(Snapshot([('File\0', path + '\0')]), diagnostics={})
        self.assertEqual(result['state'], 'matched')
        self.assertEqual(result['diagnostics']['matching_root_paths'], 1)
        self.assertTrue(result['diagnostics']['query_completed'])
        # Decoding a File type alone cannot supply the missing object name.
        class TypeOnly(Snapshot):
            def PssWalkSnapshot(self, *arguments):
                status = super().PssWalkSnapshot(*arguments)
                if status == 0:
                    C.cast(arguments[3], C.POINTER(probe.HandleEntry)).contents.Flags = 0
                return status
        missing = self.direct(TypeOnly([('File\0', '')]), diagnostics={})
        self.assertEqual(missing['state'], 'unknown')
        self.assertEqual(missing['reason'], 'windows_account_open_database_unavailable')
        self.assertEqual(missing['diagnostics']['file_type_handles'], 1)
        self.assertEqual(missing['diagnostics']['file_names_retained'], 0)
        self.assertTrue(missing['diagnostics']['query_completed'])

    def test_empty_embedded_or_repeated_nuls_are_rejected_and_cannot_match(self):
        path = ROOT + r'\message\message_0.db'
        for raw in ('\0', 'Fi\0le', 'File\0\0', path + '\0extra', path + '\0\0'):
            with self.subTest(raw=raw):
                buffer = C.create_unicode_buffer(raw)
                with self.assertRaises(probe.PlatformError) as raised:
                    probe._wide(C.addressof(buffer), len(raw) * 2)
                self.assertEqual(raised.exception.code, 'windows_account_handle_name_invalid')
        for kind, name in (('Fi\0le', path), ('File\0\0', path),
                           ('File', path + '\0extra'), ('File', path + '\0\0'), ('File', '\0')):
            with self.subTest(kind=kind, name=name):
                result = self.direct(Snapshot([(kind, name)]), diagnostics={})
                self.assertEqual(result['state'], 'unknown')
                self.assertFalse(result['matched'])
                self.assertFalse(result['diagnostics']['query_completed'])
                self.assertNotIn(ROOT, json.dumps(result))

    def test_terminal_nul_does_not_accept_another_root_or_swallow_whitespace(self):
        result = self.direct(Snapshot([('File\0', OTHER + r'\message\message_0.db' + '\0')]), diagnostics={})
        self.assertEqual(result['state'], 'mismatch')
        self.assertEqual(result['diagnostics']['other_root_paths'], 1)
        for raw in ('File ', 'File\n', 'File\u00a0', ROOT + r'\message\message_0.db' + ' '):
            with self.subTest(raw=raw):
                buffer = C.create_unicode_buffer(raw + '\0')
                self.assertEqual(probe._wide(C.addressof(buffer), (len(raw) + 1) * 2), raw)
        for kind, name in (('File \0', ROOT + r'\message\message_0.db' + '\0'),
                           ('File\0', ROOT + r'\message\message_0.db' + ' \0')):
            with self.subTest(kind=kind, name=name):
                self.assertEqual(self.direct(Snapshot([(kind, name)]))['state'], 'unknown')

    def test_sdk_layout_flags_and_file_names_only_and_local_resources_freed(self):
        api = Snapshot([('File', ROOT + r'\message\message_0.db'), ('Mutant', 'SYNTHETIC_NAMED_MUTEX')])
        self.assertEqual(C.sizeof(probe.HandleEntry), 136)
        self.assertEqual(self.snapshot(api), [ROOT + r'\message\message_0.db'])
        self.assertEqual(api.calls[0], ('capture', 42, 12, 0))
        self.assertNotEqual(api.calls[0][2] & 1, 1, 'VA clone is never requested')
        self.assertEqual(api.calls[-2:], [('free_marker', 8), ('free_snapshot', 99, 7)])
        self.assertTrue(all(item[1:] == (2, 136) for item in api.calls if item[0] == 'walk'))

    def test_partial_capture_errors_never_return_a_partial_account_result(self):
        for failure, freed in [('capture', []), ('marker', [('free_snapshot', 99, 7)]),
                               ('walk', [('free_marker', 8), ('free_snapshot', 99, 7)])]:
            api = Snapshot(failure=failure)
            with self.assertRaises(probe.PlatformError):
                self.snapshot(api)
            self.assertEqual([row for row in api.calls if row[0].startswith('free')], freed)

    def test_budget_or_deadline_does_not_accept_an_incomplete_handle_table(self):
        with patch.object(probe, 'MAX_HANDLES', 1):
            with self.assertRaisesRegex(probe.PlatformError, 'handle_limit'):
                self.snapshot(Snapshot([('File', ROOT + r'\message\message_0.db')]))
        ticks = iter([0, 4])
        with self.assertRaisesRegex(probe.PlatformError, 'deadline'):
            self.snapshot(Snapshot(), clock=lambda: next(ticks))

    def test_owned_probe_has_fixed_local_argv_timeout_no_shell_and_redacted_result(self):
        calls = []
        def runner(arguments, **options):
            calls.append((arguments, options))
            return subprocess.CompletedProcess(arguments, 0, json.dumps(probe._result('matched', 'windows_account_open_database_matched')).encode())
        check = probe.WindowsAccountProbe(12, 34, r'C:\synthetic\Weixin.exe', ROOT, runner=runner)
        self.assertTrue(check()['matched'])
        arguments, options = calls[0]
        self.assertEqual(arguments[:6], [sys.executable, '-I', '-S', '-B', '-X', 'utf8'])
        self.assertEqual(Path(arguments[6]).resolve(), Path(probe.__file__).resolve())
        self.assertEqual(options['timeout'], 6)
        self.assertFalse(options['shell'])
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertEqual(options['stdin'], subprocess.DEVNULL)

    def test_native_outer_revalidates_full_buffer_and_creation_time_around_snapshot(self):
        api = Snapshot([('File', ROOT + r'\message\message_0.db')])
        executable = r'C:\synthetic\Weixin.exe'
        capacities, opened, closed = [], [], []
        def query(handle, flags, image, size):
            capacity = C.cast(size, C.POINTER(C.c_uint32))
            capacities.append(capacity.contents.value)
            if capacity.contents.value <= len(executable):
                return 0
            image.value = executable
            capacity.contents.value = len(executable)
            return 1
        def process_times(handle, created, *unused):
            C.cast(created, C.POINTER(probe.FILETIME)).contents.low = 34
            return 1
        native = SimpleNamespace(OpenProcess=lambda *args: opened.append(args) or 42,
            CloseHandle=lambda handle: closed.append(handle) or 1,
            QueryFullProcessImageNameW=query, GetProcessTimes=process_times,
            GetCurrentProcess=lambda: 99)
        api.GetLogicalDrives = lambda: 0
        with patch.object(probe, '_api', return_value=native), patch.object(probe, '_SnapshotAPI', return_value=api), \
             patch.object(probe, 'path_is_local', return_value=True), \
             patch.object(probe,'verified_process_family',side_effect=lambda executable,pid,birth,**options:[{'pid':pid,'parent_pid':8,'creation_time':birth}]):
            self.assertTrue(probe.probe_direct(12, 34, executable, ROOT)['matched'])
            self.assertEqual(capacities, [32768, 32768])
            self.assertEqual(opened, [(0x450, False, 12)])
            self.assertEqual(closed, [42])
            api.calls.clear()
            self.assertEqual(probe.probe_direct(12, 35, executable, ROOT)['state'], 'unknown')
            self.assertFalse(any(row[0] == 'capture' for row in api.calls))
            self.assertEqual(closed, [42, 42])

    def test_child_timeout_failure_or_untrusted_output_fails_closed(self):
        bad = [b'not-json', b'{}', json.dumps({'state': 'matched', 'matched': True, 'reason': ROOT}).encode(),
               json.dumps(dict(probe._result('matched', 'windows_account_fake'), private=ROOT)).encode(), b' ' * 1025,
               json.dumps({'state': 'matched', 'matched': 1, 'reason': 'windows_account_fake'}).encode()]
        for data in bad:
            check = probe.WindowsAccountProbe(12, 34, 'SYNTHETIC_EXE', ROOT,
                runner=lambda *a, **k: subprocess.CompletedProcess([], 0, data))
            self.assertEqual(check()['state'], 'unknown')
        def timeout(*a, **k):
            raise subprocess.TimeoutExpired('SYNTHETIC_PRIVATE_COMMAND', 6)
        self.assertEqual(probe.WindowsAccountProbe(12, 34, 'SYNTHETIC_EXE', ROOT, runner=timeout)()['state'], 'unknown')

    def test_public_diagnostic_subsets_are_strict_counts_without_private_fields(self):
        subset={'schema_version':1,'handles_seen':80,'handles_with_name':20,
                'file_type_handles':0,'query_completed':True}
        self.assertEqual(probe.sanitize_diagnostics(subset),subset)
        self.assertEqual(probe.sanitize_diagnostics({'schema_version':1}),{'schema_version':1})
        self.assertNotEqual(id(probe.sanitize_diagnostics(subset)),id(subset))
        for value in (None,{},[],{'schema_version':True},{'schema_version':2},
                      dict(subset,pid=12),dict(subset,path=ROOT),dict(subset,handles_seen=True),
                      dict(subset,handles_seen=80.0),dict(subset,handles_seen=-1),
                      dict(subset,handles_seen=32769),dict(subset,query_completed=1)):
            with self.subTest(value=value):self.assertEqual(probe.sanitize_diagnostics(value),{})

    def test_missing_type_names_and_non_file_names_count_but_cannot_prove_account(self):
        diagnostics={}
        api=Snapshot([('',ROOT+r'\message\message_0.db'),('Mutant',OTHER+r'\contact\contact.db')])
        paths=self.snapshot(api,diagnostics=diagnostics)
        result=probe.classify_paths(paths,ROOT,diagnostics=diagnostics)
        self.assertEqual(result['state'],'unknown')
        counts=result['diagnostics']
        self.assertEqual(counts['handles_seen'],2)
        self.assertEqual(counts['handles_with_name'],2)
        self.assertEqual(counts['type_names_present'],1)
        self.assertEqual(counts['all_named_db_candidates'],2)
        self.assertEqual(counts['file_type_handles'],0)
        self.assertEqual(counts['file_names_retained'],0)
        self.assertEqual(counts['database_names_seen'],0)
        self.assertTrue(counts['query_completed'])
        self.assertNotIn(ROOT,json.dumps(result));self.assertNotIn(OTHER,json.dumps(result))
        self.assertEqual(api.calls[0],('capture',42,12,0))

    def test_completed_snapshot_counts_keep_existing_file_only_classification(self):
        counts={}
        api=Snapshot([('',OTHER+r'\message\message_0.db'),
                      ('Mutant',OTHER+r'\contact\contact.db'),('File',ROOT+r'\message\message_0.db')])
        result=self.direct(api,diagnostics=counts)
        self.assertEqual(result['state'],'matched')
        self.assertEqual(result['diagnostics']['handles_seen'],3)
        self.assertEqual(result['diagnostics']['type_names_present'],2)
        self.assertEqual(result['diagnostics']['file_type_handles'],1)
        self.assertEqual(result['diagnostics']['file_names_retained'],1)
        self.assertEqual(result['diagnostics']['all_named_db_candidates'],3)
        self.assertEqual(result['diagnostics']['database_names_seen'],1)
        self.assertEqual(result['diagnostics']['matching_root_paths'],1)
        self.assertEqual(result['diagnostics']['other_root_paths'],0)
        self.assertTrue(result['diagnostics']['query_completed'])
        self.assertNotIn('Users',json.dumps(result))

    def test_file_type_without_name_flag_counts_but_is_not_path_evidence(self):
        class UnnamedFile(Snapshot):
            def PssWalkSnapshot(self,*arguments):
                status=super().PssWalkSnapshot(*arguments)
                if status==0:C.cast(arguments[3],C.POINTER(probe.HandleEntry)).contents.Flags=0
                return status
        result=self.direct(UnnamedFile([('File',ROOT+r'\message\message_0.db')]),diagnostics={})
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(result['diagnostics']['handles_seen'],1)
        self.assertEqual(result['diagnostics']['handles_with_name'],0)
        self.assertEqual(result['diagnostics']['type_names_present'],1)
        self.assertEqual(result['diagnostics']['file_type_handles'],1)
        self.assertEqual(result['diagnostics']['file_names_retained'],0)
        self.assertEqual(result['diagnostics']['all_named_db_candidates'],0)
        self.assertEqual(result['diagnostics']['database_names_seen'],0)
        self.assertTrue(result['diagnostics']['query_completed'])

    def test_different_and_unresolved_roots_count_without_relaxing_proof(self):
        result=probe.classify_paths([ROOT+r'\message\message_0.db',OTHER+r'\contact\contact.db'],ROOT,diagnostics={})
        self.assertEqual(result['state'],'mismatch')
        self.assertEqual(result['diagnostics']['database_names_seen'],2)
        self.assertEqual(result['diagnostics']['matching_root_paths'],1)
        self.assertEqual(result['diagnostics']['other_root_paths'],1)
        self.assertFalse(result['diagnostics']['query_completed'],'pure paths do not prove a complete native query')
        unknown=probe.classify_paths([r'\Device\Unmapped\db_storage\message\message_0.db'],ROOT,diagnostics={})
        self.assertEqual(unknown['state'],'unknown')
        self.assertEqual(unknown['diagnostics']['unresolved_database_paths'],1)

    def test_interrupted_snapshot_keeps_safe_counts_and_never_reports_matched(self):
        class Interrupted(Snapshot):
            def PssWalkSnapshot(self,*arguments):
                if self.index:return 24
                return super().PssWalkSnapshot(*arguments)
        result=self.direct(Interrupted([('File',ROOT+r'\message\message_0.db')]),diagnostics={})
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(result['diagnostics']['handles_seen'],1)
        self.assertEqual(result['diagnostics']['file_names_retained'],1)
        self.assertFalse(result['diagnostics']['query_completed'])
        with patch.object(probe,'MAX_HANDLES',1):
            result=self.direct(Snapshot([('File',ROOT+r'\message\message_0.db')]),diagnostics={})
        self.assertEqual(result['state'],'unknown')
        self.assertFalse(result['diagnostics']['query_completed'])

    def test_diagnostic_non_file_parse_failure_does_not_change_file_only_proof(self):
        class OddNonFileName(Snapshot):
            def PssWalkSnapshot(self,*arguments):
                status=super().PssWalkSnapshot(*arguments)
                if status==0 and self.index==1:
                    C.cast(arguments[3],C.POINTER(probe.HandleEntry)).contents.ObjectNameLength=3
                return status
        result=self.direct(OddNonFileName([('Mutant','SYNTHETIC_NON_FILE'),('File',ROOT+r'\message\message_0.db')]),diagnostics={})
        self.assertEqual(result['state'],'matched')
        self.assertTrue(result['diagnostics']['query_completed'])

    def test_child_accepts_old_or_safe_diagnostics_and_rejects_partial_match_or_private_output(self):
        base=probe._result('matched','windows_account_open_database_matched')
        safe=dict(base,diagnostics={'schema_version':1,'handles_seen':1,'query_completed':True})
        def check(document):
            data=document if isinstance(document,bytes) else json.dumps(document).encode()
            return probe.WindowsAccountProbe(12,34,'SYNTHETIC_EXE',ROOT,
                runner=lambda *a,**k:subprocess.CompletedProcess([],0,data))()
        self.assertTrue(check(base)['matched'])
        self.assertEqual(check(safe),safe)
        unknown_subset=dict(probe._result('unknown','windows_account_open_database_unavailable'),diagnostics={'schema_version':1})
        self.assertEqual(check(unknown_subset),unknown_subset)
        invalid=[dict(base,diagnostics={}),dict(base,diagnostics={'schema_version':1}),
                 dict(base,diagnostics={'schema_version':1,'query_completed':False}),
                 dict(base,diagnostics={'schema_version':1,'handles_seen':True}),
                 dict(base,diagnostics={'schema_version':1,'file_names_retained':32769}),
                 dict(base,diagnostics={'schema_version':1,'object_name':ROOT}),dict(safe,process_id=12),
                 b'{"state":"matched","state":"matched","matched":true,"reason":"windows_account_fake"}']
        for document in invalid:
            with self.subTest(document=document):
                result=check(document)
                self.assertEqual(result['state'],'unknown')
                self.assertNotIn('diagnostics',result)
                self.assertNotIn(ROOT,json.dumps(result))
        data=json.dumps(safe).encode()
        self.assertTrue(check(data+b' '*(4096-len(data)))['matched'])
        self.assertEqual(check(data+b' '*(4097-len(data)))['state'],'unknown')

    def test_stable_main_without_db_and_helper_with_original_db_is_matched(self):
        result,api,evidence=self.family_probe({13:[('File',ROOT+r'\message\message_0.db')]})
        self.assertEqual(result['state'],'matched')
        counts=result['diagnostics']
        self.assertEqual(counts['family_process_count'],2)
        self.assertEqual(counts['family_processes_completed'],2)
        self.assertEqual(counts['main_database_names_seen'],0)
        self.assertEqual(counts['helper_database_names_seen'],1)
        self.assertTrue(counts['query_completed'])
        self.assertEqual([row[2] for row in evidence['opened']],[12,13])
        self.assertEqual(evidence['closed'],[12,13])
        self.assertEqual([(row[1],row[2]) for row in evidence['family_calls']],[(12,34),(12,34)])
        self.assertTrue(all(row[2:]==(12,0) for row in api.calls if row[0]=='capture'))
        self.assertEqual(set(result),{'state','matched','reason','diagnostics'})
        self.assertNotIn(ROOT,json.dumps(result));self.assertNotIn('pid',json.dumps(result))

    def test_family_other_account_root_is_mismatch_and_named_non_files_do_not_prove_it(self):
        result,_,_=self.family_probe({12:[('File',ROOT+r'\contact\contact.db')],
                                     13:[('File',OTHER+r'\message\message_0.db')]})
        self.assertEqual(result['state'],'mismatch')
        self.assertTrue(result['diagnostics']['query_completed'])
        self.assertEqual(result['diagnostics']['matching_root_paths'],1)
        self.assertEqual(result['diagnostics']['other_root_paths'],1)
        unknown,_,_=self.family_probe({13:[('',ROOT+r'\message\message_0.db'),('Mutant',ROOT+r'\contact\contact.db')]})
        self.assertEqual(unknown['state'],'unknown')
        self.assertEqual(unknown['diagnostics']['all_named_db_candidates'],2)
        self.assertEqual(unknown['diagnostics']['helper_database_names_seen'],0)

    def test_missing_new_reused_or_disconnected_family_members_cannot_match(self):
        before=[{'pid':12,'parent_pid':8,'creation_time':34},{'pid':13,'parent_pid':12,'creation_time':35}]
        variants=[before[:1],before+[{'pid':14,'parent_pid':12,'creation_time':36}],
                  [before[0],dict(before[1],creation_time=99)],
                  [before[0],dict(before[1],parent_pid=9)],
                  [dict(before[0],parent_pid=13),before[1]]]
        for after in variants:
            with self.subTest(after=after):
                result,_,_=self.family_probe({13:[('File',ROOT+r'\message\message_0.db')]},families=[before,after])
                self.assertEqual(result['state'],'unknown')
                self.assertFalse(result['diagnostics']['query_completed'])

    def test_any_inaccessible_incomplete_or_reused_member_prevents_partial_match(self):
        source={12:[('File',ROOT+r'\message\message_0.db')],13:[('File',ROOT+r'\contact\contact.db')]}
        for options in ({'denied':(13,)},{'fail_member':13},{'reused_member':13},{'wrong_image':13}):
            with self.subTest(options=options):
                result,_,evidence=self.family_probe(source,**options)
                self.assertEqual(result['state'],'unknown')
                self.assertFalse(result['diagnostics']['query_completed'])
                self.assertEqual(result['diagnostics']['family_processes_completed'],1)
                self.assertIn(12,evidence['closed'])

    def test_shared_family_deadline_does_not_reset_for_helpers_or_final_metadata(self):
        clock=SimpleNamespace(value=0)
        def capture(pid):clock.value=2 if pid==12 else 4
        result,api,evidence=self.family_probe({13:[('File',ROOT+r'\message\message_0.db')]},
            capture=capture,clock=lambda:clock.value)
        self.assertEqual(result['state'],'unknown')
        self.assertFalse(result['diagnostics']['query_completed'])
        self.assertEqual(evidence['family_calls'][0][3],3)
        self.assertEqual(result['diagnostics']['family_processes_completed'],1)
        self.assertEqual(len([row for row in api.calls if row[0]=='walk']),1)
        clock.value=0
        before=[{'pid':12,'parent_pid':8,'creation_time':34},{'pid':13,'parent_pid':12,'creation_time':35}]
        def family(phase):
            if phase==2:clock.value=4
            return before
        final,_,_=self.family_probe({13:[('File',ROOT+r'\message\message_0.db')]},families=family,clock=lambda:clock.value)
        self.assertEqual(final['state'],'unknown')
        self.assertFalse(final['diagnostics']['query_completed'])

    def test_family_handles_share_one_budget_and_member_limit_is_not_bypassed(self):
        with patch.object(probe,'MAX_HANDLES',2):
            result,_,_=self.family_probe({12:[('File',ROOT+r'\message\message_0.db')],
                                         13:[('File',ROOT+r'\contact\contact.db')]})
        self.assertEqual(result['state'],'unknown')
        self.assertLessEqual(result['diagnostics']['handles_seen'],2)
        self.assertFalse(result['diagnostics']['query_completed'])
        oversized=[{'pid':12,'parent_pid':8,'creation_time':34}]+[
            {'pid':pid,'parent_pid':12,'creation_time':pid+34} for pid in range(20,36)]
        invalid,api,evidence=self.family_probe({},families=[oversized])
        self.assertEqual(invalid['state'],'unknown')
        self.assertEqual(evidence['opened'],[])
        self.assertFalse(any(row[0]=='capture' for row in api.calls))

    def test_family_metadata_failure_is_unknown_and_four_new_counts_are_sanitized(self):
        def failed(_):raise probe.PlatformError('windows_process_access_unavailable')
        result,_,evidence=self.family_probe({},families=failed)
        self.assertEqual(result['state'],'unknown')
        self.assertFalse(result['diagnostics']['query_completed'])
        self.assertEqual(evidence['opened'],[])
        clean={'schema_version':1,'family_process_count':2,'family_processes_completed':2,
               'main_database_names_seen':0,'helper_database_names_seen':1,'query_completed':True}
        self.assertEqual(probe.sanitize_diagnostics(clean),clean)
        for value in (dict(clean,family_process_count=True),dict(clean,helper_database_names_seen=32769),
                      dict(clean,family_members=[12,13])):
            self.assertEqual(probe.sanitize_diagnostics(value),{})


if __name__ == '__main__':
    unittest.main()
