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
    def __init__(self, entries=(), windows=(), version=(4,1,15,56)):
        self.entries, self.windows, self.version = list(entries), windows, version
        self.index, self.closed, self.accesses = 0, [], []

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
        C.cast(birth, C.POINTER(detection.FILETIME)).contents.low = pid + 1000
        return 1

    def EnumWindows(self, callback, data):
        for window in self.windows:
            callback(window, data)
        return 1

    def GetWindowThreadProcessId(self, window, pid):
        put(pid, C.c_uint32, window)
        return 1


class WindowsProcessDetectionTests(unittest.TestCase):
    def detect(self, api):
        with patch.object(detection,'path_is_local',return_value=True), \
             patch.object(detection.C,'get_last_error',create=True,return_value=18):
            return detection.detect_wechat_process(EXE,api=api,native=api)

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


if __name__=='__main__':
    unittest.main()
