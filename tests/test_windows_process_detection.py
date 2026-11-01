"""Virtual process tables/version resources; no native DLL or user process."""
import ctypes as C
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

MODULES = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))
import windows_process_detection as detection

EXE = r'C:\synthetic\Weixin.exe'


def put(pointer, kind, value):
    C.cast(pointer, C.POINTER(kind))[0] = value


class SyntheticAPI:
    def __init__(self, entries=(), windows=(), version=(4,1,15,56), hidden=(), owned=()):
        self.entries, self.windows, self.version = list(entries), windows, version
        self.index, self.closed, self.accesses = 0, [], []
        self.hidden, self.owned = set(hidden), set(owned)

    def GetFileVersionInfoSizeW(self, *arguments):
        return 64

    def GetFileVersionInfoW(self, executable, ignored, size, buffer):
        assert executable == EXE
        value = detection.FixedVersion()
        value.signature = 0xfeef04bd
        value.product_ms = self.version[0] << 16 | self.version[1]
        value.product_ls = self.version[2] << 16 | self.version[3]
        C.memmove(buffer, C.byref(value), C.sizeof(value))
        return 1

    def VerQueryValueW(self, buffer, block, pointer, length):
        assert block == '\\'
        put(pointer, C.c_void_p, C.addressof(buffer))
        put(length, C.c_uint32, C.sizeof(detection.FixedVersion))
        return 1

    def CreateToolhelp32Snapshot(self, flags, pid):
        assert (flags, pid) == (2, 0)
        return 99

    def _fill(self, target):
        entry = C.cast(target, C.POINTER(detection.ProcessEntry)).contents
        assert entry.dwSize == 568
        row = self.entries[self.index]
        entry.th32ProcessID, entry.th32ParentProcessID = row['pid'], row['parent_pid']
        raw = row.get('name','Weixin.exe').encode('utf-16-le')
        C.memset(entry.szExeFile, 0, 520)
        C.memmove(entry.szExeFile, raw, len(raw))
        return 1

    def Process32FirstW(self, snapshot, target):
        self.index = 0
        return self._fill(target) if self.entries else 0

    def Process32NextW(self, snapshot, target):
        self.index += 1
        return self._fill(target) if self.index < len(self.entries) else 0

    def OpenProcess(self, access, inherit, pid):
        self.accesses.append((access, inherit, pid))
        return pid

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def QueryFullProcessImageNameW(self, pid, flags, image, size):
        row = next(row for row in self.entries if row['pid'] == pid)
        image.value = row.get('exe',EXE)
        put(size, C.c_uint32, len(image.value))
        return 1

    def IsWow64Process2(self, handle, machine, host):
        put(machine, C.c_uint16, 0);put(host, C.c_uint16, 0x8664)
        return 1

    def GetProcessTimes(self, pid, birth, *unused):
        row=next(row for row in self.entries if row['pid']==pid)
        C.cast(birth, C.POINTER(detection.FILETIME)).contents.low = row.get('creation_time',pid + 1000)
        return 1

    def EnumWindows(self, callback, data):
        for window in self.windows:
            callback(window, data)
        return 1

    def GetWindowThreadProcessId(self, window, pid):
        put(pid, C.c_uint32, window)
        return 1

    def IsWindowVisible(self, window):
        return int(window not in self.hidden)

    def GetWindow(self, window, relation):
        assert relation==4
        return 99 if window in self.owned else 0


class WindowsProcessDetectionTests(unittest.TestCase):
    def detect(self, api):
        with patch.object(detection,'path_is_local',return_value=True), \
             patch.object(detection.C,'get_last_error',create=True,return_value=18):
            return detection.detect_wechat_process(EXE,api=api,native=api)

    def family(self, api, pid=12, birth=1012, **options):
        with patch.object(detection,'path_is_local',return_value=True), \
             patch.object(detection.C,'get_last_error',create=True,return_value=18):
            return detection.verified_process_family(EXE,pid,birth,api=api,native=api,**options)

    def test_hidden_main_process_is_detected_without_window_or_login_claim(self):
        api = SyntheticAPI([{'pid':12,'parent_pid':8},{'pid':13,'parent_pid':12},{'pid':14,'parent_pid':12}])
        result=self.detect(api)
        self.assertEqual(result,{'state':'ready','reason':'windows_process_candidate_ready','pid':12,'creation_time':1012})
        self.assertNotIn('logged_in',result)
        self.assertTrue(all(row[0:2]==(0x1000,False) for row in api.accesses))
        self.assertEqual(sorted(api.closed),[12,13,14,99])

    def test_unique_window_can_resolve_multiple_roots_without_reading_titles(self):
        api=SyntheticAPI([{'pid':12,'parent_pid':8},{'pid':21,'parent_pid':9}],windows=[21])
        self.assertEqual(self.detect(api)['pid'],21)
        api.windows=[12,21]
        self.assertEqual(self.detect(api)['state'],'ambiguous')

    def test_launcher_parent_does_not_override_its_main_window_child(self):
        api=SyntheticAPI([{'pid':12,'parent_pid':8},{'pid':21,'parent_pid':12},
                          {'pid':22,'parent_pid':21}],windows=[21])
        result=self.detect(api)
        self.assertEqual((result['pid'],result['creation_time']),(21,1021))
        self.assertEqual(result['state'],'ready')
        self.assertTrue(all(row[0:2]==(0x1000,False) for row in api.accesses))

    def test_hidden_launcher_window_does_not_compete_with_visible_main_child(self):
        api=SyntheticAPI([{'pid':12,'parent_pid':8},{'pid':21,'parent_pid':12}],
                         windows=[12,21],hidden=[12])
        result=self.detect(api)
        self.assertEqual((result['pid'],result['creation_time']),(21,1021))

    def test_owned_popup_does_not_compete_with_unowned_main_window(self):
        api=SyntheticAPI([{'pid':12,'parent_pid':8},{'pid':21,'parent_pid':12}],
                         windows=[12,21],owned=[12])
        self.assertEqual(self.detect(api)['pid'],21)

    def test_no_eligible_window_preserves_unique_hidden_root_fallback(self):
        api=SyntheticAPI([{'pid':12,'parent_pid':8},{'pid':21,'parent_pid':12}],
                         windows=[12,21],hidden=[12],owned=[21])
        self.assertEqual(self.detect(api)['pid'],12)

    def test_multiple_window_children_do_not_fall_back_to_launcher_parent(self):
        api=SyntheticAPI([{'pid':12,'parent_pid':8},{'pid':21,'parent_pid':12},
                          {'pid':22,'parent_pid':12}],windows=[21,22])
        result=self.detect(api)
        self.assertEqual(result['state'],'ambiguous')
        self.assertIsNone(result['pid'])

    def test_failed_window_enumeration_cannot_establish_a_hidden_root(self):
        api=SyntheticAPI([{'pid':12,'parent_pid':8}])
        api.EnumWindows=lambda *_:0
        self.assertEqual(self.detect(api)['state'],'unavailable')

    def test_window_metadata_error_cannot_be_reinterpreted_as_no_window(self):
        api=SyntheticAPI([{'pid':12,'parent_pid':8}],windows=[12])
        api.GetWindow=lambda *_:(_ for _ in ()).throw(OSError('SYNTHETIC_PRIVATE_DETAIL'))
        result=self.detect(api)
        self.assertEqual(result['state'],'unavailable')
        self.assertNotIn('SYNTHETIC_PRIVATE_DETAIL',str(result))

    def test_invalid_or_reused_process_metadata_is_rejected(self):
        for rows in ([{'pid':True,'parent_pid':1,'creation_time':123}],
                     [{'pid':12,'parent_pid':1,'creation_time':0}],
                     [{'pid':12,'parent_pid':1,'creation_time':123}]*2):
            with self.subTest(rows=rows):
                self.assertEqual(detection.select_candidate(rows,[12])['state'],'unavailable')

    def test_not_running_is_distinct_from_unsupported_version(self):
        self.assertEqual(self.detect(SyntheticAPI())['state'],'not_running')
        result=self.detect(SyntheticAPI(version=(4,2,0,1)))
        self.assertEqual(result['state'],'unavailable')
        self.assertEqual(result['reason'],'windows_process_version_unsupported')

    def test_other_executable_path_and_non_wechat_processes_are_not_selected(self):
        api=SyntheticAPI([{'pid':12,'parent_pid':8,'exe':r'C:\unrelated\Weixin.exe'},
                          {'pid':99,'parent_pid':8,'name':'unrelated.exe'}])
        self.assertEqual(self.detect(api)['state'],'not_running')
        self.assertEqual(api.accesses,[(0x1000,False,12)])

    def test_inaccessible_process_does_not_fall_back_to_an_unverified_candidate(self):
        api=SyntheticAPI([{'pid':12,'parent_pid':8}])
        api.OpenProcess=lambda *arguments:None
        result=self.detect(api)
        self.assertEqual(result['state'],'unavailable')
        self.assertEqual(result['reason'],'windows_process_access_unavailable')

    def test_sdk_resource_layout_and_invalid_pointer_fail_closed(self):
        self.assertEqual(C.sizeof(detection.ProcessEntry),568)
        self.assertEqual(detection.ProcessEntry.szExeFile.offset,44)
        self.assertEqual(C.sizeof(detection.FixedVersion),52)
        api=SyntheticAPI()
        def invalid(buffer, block, pointer, length):
            put(pointer,C.c_void_p,1);put(length,C.c_uint32,52)
            return 1
        api.VerQueryValueW=invalid
        self.assertEqual(self.detect(api)['reason'],'windows_process_version_unavailable')

    def test_truncated_snapshot_cannot_be_accepted_and_handle_is_closed(self):
        api=SyntheticAPI([{'pid':12,'parent_pid':8}])
        with patch.object(detection,'MAX_PROCESSES',0):
            self.assertEqual(self.detect(api)['state'],'unavailable')
        self.assertEqual(api.closed,[99])

    def test_errors_do_not_expose_private_executable_paths_or_native_messages(self):
        with patch.object(detection,'path_is_local',side_effect=OSError('SYNTHETIC_PRIVATE_PATH')):
            result=detection.detect_wechat_process(EXE)
        self.assertEqual(result['state'],'unavailable')
        self.assertNotIn('SYNTHETIC_PRIVATE',str(result))
        self.assertNotIn('synthetic\\',str(result))

    def test_verified_family_reuses_exact_path_version_architecture_and_birth_queries(self):
        api=SyntheticAPI([{'pid':12,'parent_pid':8},{'pid':21,'parent_pid':12},{'pid':22,'parent_pid':21}])
        family=self.family(api,pid=21,birth=1021)
        self.assertEqual([row['pid'] for row in family],[12,21,22])
        self.assertEqual(family[1]['creation_time'],1021)
        self.assertTrue(all(access==0x1000 and not inherit for access,inherit,_ in api.accesses))
        self.assertEqual(sorted(api.closed),[12,21,22,99])
        self.assertTrue(all(set(row)=={'pid','parent_pid','creation_time'} for row in family))

    def test_family_rejects_missing_main_wrong_birth_independent_roots_cycles_and_parent_reuse(self):
        cases=[([{'pid':21,'parent_pid':8}],12,1012),
               ([{'pid':12,'parent_pid':8}],12,9999),
               ([{'pid':12,'parent_pid':8},{'pid':21,'parent_pid':9}],12,1012),
               ([{'pid':12,'parent_pid':21},{'pid':21,'parent_pid':12}],12,1012),
               ([{'pid':12,'parent_pid':8},{'pid':21,'parent_pid':22},{'pid':22,'parent_pid':21}],12,1012),
               ([{'pid':12,'parent_pid':8,'creation_time':2000},{'pid':21,'parent_pid':12,'creation_time':1000}],12,2000)]
        for rows,pid,birth in cases:
            with self.subTest(rows=rows):
                with self.assertRaises(detection.PlatformError):self.family(SyntheticAPI(rows),pid,birth)

    def test_family_member_cap_and_any_inaccessible_process_fail_closed(self):
        rows=[{'pid':12,'parent_pid':8}]+[{'pid':n,'parent_pid':12} for n in range(20,36)]
        with self.assertRaises(detection.PlatformError):self.family(SyntheticAPI(rows))
        api=SyntheticAPI([{'pid':12,'parent_pid':8},{'pid':21,'parent_pid':12}])
        original=api.OpenProcess
        api.OpenProcess=lambda access,inherit,pid:None if pid==21 else original(access,inherit,pid)
        with self.assertRaises(detection.PlatformError):self.family(api)
        self.assertIn(12,api.closed)
        self.assertIn(99,api.closed)

    def test_family_rejects_wrong_version_architecture_and_shared_deadline(self):
        with self.assertRaises(detection.PlatformError):self.family(SyntheticAPI([{'pid':12,'parent_pid':8}],version=(4,2,0,1)))
        api=SyntheticAPI([{'pid':12,'parent_pid':8}])
        api.IsWow64Process2=lambda *args:0
        with self.assertRaises(detection.PlatformError):self.family(api)
        with self.assertRaises(detection.PlatformError):self.family(SyntheticAPI([{'pid':12,'parent_pid':8}]),clock=lambda:4,deadline=3)


if __name__=='__main__':
    unittest.main()
