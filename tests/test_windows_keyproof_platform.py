"""Synthetic Win32 API contracts only; never touch user processes/files."""
import contextlib
import ctypes as C
import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('keyproof_platform_under_test', Path(__file__).parents[1] / 'scripts/windows-proof/windows_keyproof_platform.py')
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)


def put(pointer, kind, value):
    C.cast(pointer, C.POINTER(kind))[0] = value


class FakeAPI:
    def __init__(self):
        self.closed, self.opens, self.reads, self.queries = [], [], [], 0
        self.exe, self.creation, self.machine = r'C:\safe\Weixin.exe', 0x123456789, 0x8664
        self.partial, self.guard = False, False
        self.file_handles, self.next_handle, self.writes, self.deletes = {}, 100, [], []
        self.mismatch, self.existing = False, False

    def GetDriveTypeW(self, path):
        return 3

    def GetFileAttributesW(self, path):
        return 0 if path.lower().endswith(('.exe', '.db', '.json')) else 16

    def OpenProcess(self, access, inherit, pid):
        self.opens.append((access, inherit, pid))
        return 99

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True

    def QueryFullProcessImageNameW(self, handle, flags, image, length):
        image.value = self.exe
        return True

    def IsWow64Process2(self, handle, process, native):
        put(process, P.U16, 0)
        put(native, P.U16, self.machine)
        return True

    def GetProcessTimes(self, handle, creation, *rest):
        value = C.cast(creation, C.POINTER(P.FILETIME)).contents
        value.low, value.high = self.creation & 0xffffffff, self.creation >> 32
        return True

    def VirtualQueryEx(self, handle, address, result, size):
        self.queries += 1
        info = C.cast(result, C.POINTER(P.MBI64)).contents
        base = int(address)
        info.BaseAddress, info.RegionSize = base, 4096
        info.State, info.Type, info.Protect = 0x1000, 0x20000, 4 | (0x100 if self.guard else 0)
        return 48

    def ReadProcessMemory(self, handle, address, buffer, size, count):
        self.reads.append((address, size))
        C.memmove(buffer, b'X' * size, size)
        put(count, P.SIZE, size - 1 if self.partial else size)
        return True

    def CreateFileW(self, path, access, share, security, disposition, flags, template):
        self.opens.append((path, access, share, disposition, flags))
        if disposition == 1 and self.existing:
            return C.c_void_p(-1).value
        self.next_handle += 1
        self.file_handles[self.next_handle] = path
        return self.next_handle

    def GetFinalPathNameByHandleW(self, handle, output, length, flags):
        output.value = '\\\\?\\' + (r'C:\changed\other.db' if self.mismatch else self.file_handles[handle])
        return len(output.value)

    def GetFileInformationByHandle(self, handle, pointer):
        C.cast(pointer, C.POINTER(P.FILEINFO)).contents.attributes = self.GetFileAttributesW(self.file_handles[handle])
        return True

    def ReadFile(self, handle, buffer, size, count, overlapped):
        self.reads.append((handle, size))
        C.memmove(buffer, b'F' * size, size)
        put(count, P.U32, size)
        return True

    def WriteFile(self, handle, payload, size, count, overlapped):
        self.writes.append(bytes(payload[:size]))
        put(count, P.U32, size)
        return True

    def FlushFileBuffers(self, handle):
        return True

    def SetFileInformationByHandle(self, handle, kind, info, size):
        self.deletes.append((handle, kind, size))
        return True


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.patch = patch.object(P, '_native', self.api)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def source(self):
        return P.WindowsProcess(42, self.api.exe, self.api.creation)

    def test_import_struct_layout_and_native_signatures(self):
        self.assertEqual(C.sizeof(P.MBI64), 48)
        self.assertEqual([getattr(P.MBI64, name).offset for name in ('RegionSize', 'State', 'Type')], [24, 32, 40])
        self.assertEqual(C.sizeof(P.FILEINFO), 52)
        class Function:
            pass
        class DLL:
            def __getattr__(self, name):
                result = Function()
                setattr(self, name, result)
                return result
        with patch.object(P.os, 'name', 'nt'), patch.object(P.C, 'WinDLL', create=True, side_effect=lambda *a, **k: DLL()):
            api = P._API()
        self.assertEqual(api.ReadProcessMemory.argtypes[-1], C.POINTER(P.SIZE))
        self.assertEqual(api.VirtualQueryEx.restype, P.SIZE)
        self.assertEqual(api.OpenProcess.restype, P.PTR)
        self.assertEqual(api.GetSecurityInfo.restype, P.U32)
        self.assertEqual(api.CreateFileW.argtypes[3], C.POINTER(P.SA))

    def test_process_access_identity_and_same_handle(self):
        with self.source() as source:
            self.assertEqual(source.creation_time, self.api.creation)
            self.assertEqual(source.read(0x10000, 32), b'X' * 32)
        self.assertEqual(self.api.opens[0], (0x410, False, 42))
        self.assertEqual(self.api.closed, [99])

    def test_pid_reuse_path_and_architecture_rejected_and_closed(self):
        cases = [(r'C:\safe\other.exe', self.api.creation, 0x8664, 'process_executable_mismatch'),
                 (self.api.exe, self.api.creation + 1, 0x8664, 'process_creation_time_mismatch'),
                 (self.api.exe, self.api.creation, 0xaa64, 'target_amd64_required')]
        for expected, creation, machine, code in cases:
            self.api.machine = machine
            with self.assertRaises(P.PlatformError) as caught:
                with P.WindowsProcess(42, expected, creation):
                    self.fail('must not enter')
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.api.closed, [99] * 3)
        self.assertEqual(self.api.reads, [])

    def test_read_limit_guard_and_partial_are_gaps(self):
        with self.source() as source:
            for address, size in [(0x10000, 0), (0x10000, 1048577), (True, 1), (P.MAX_ADDRESS, 1)]:
                with self.assertRaises(P.PlatformError):
                    source.read(address, size)
            self.api.guard = True
            self.assertEqual(source.read(0x10000, 32), b'')
            self.assertEqual(self.api.reads, [])
            self.api.guard, self.api.partial = False, True
            self.assertEqual(source.read(0x10000, 32), b'')

    def test_region_filter_private_image_guard_and_sparse_limit(self):
        def query(handle, address, result, size):
            self.api.queries += 1
            info = C.cast(result, C.POINTER(P.MBI64)).contents
            info.BaseAddress, info.RegionSize = address, 4096
            info.State, info.Type, info.Protect = 0x1000, 0x20000, 4
            if address == 0:  # free low addresses
                info.State, info.RegionSize = 0x10000, 0x10000
            elif address == 0x11000:
                info.Protect = 0x104
            elif address == 0x12000:
                info.Type = 0x1000000
            elif address == 0x13000:
                info.Type = 0x40000
            elif address == 0x14000:
                info.State, info.RegionSize = 0x10000, P.MAX_ADDRESS - address
            return 48
        self.api.VirtualQueryEx = query
        with self.source() as source:
            self.assertEqual(list(source.regions()), [(0x10000, 4096)])
            self.assertEqual(list(source.regions(True)), [(0x10000, 4096), (0x12000, 4096)])
            source.region_deadline = time.monotonic() - 1
            before = self.api.queries
            with self.assertRaisesRegex(P.PlatformError, '^region_time_limit$'):
                list(source.regions())
            self.assertEqual(self.api.queries, before)
        def no_yield_query(handle, address, result, size):
            value = FakeAPI.VirtualQueryEx(self.api, handle, address, result, size)
            C.cast(result, C.POINTER(P.MBI64)).contents.State = 0x2000
            return value
        self.api.VirtualQueryEx = no_yield_query
        with self.source() as source:
            before = self.api.queries
            with self.assertRaisesRegex(P.PlatformError, '^region_query_limit$'):
                list(source.regions())
            self.assertEqual(self.api.queries - before, 100000)

    def test_paths_reject_network_reparse_removable_and_require_dir(self):
        self.assertFalse(P.path_is_local(r'\\server\share\message_0.db'))
        self.assertFalse(P.path_is_local(r'C:relative'))
        self.assertFalse(P.path_is_local(r'C:\safe\file.db:stream'))
        self.assertFalse(P.path_is_local(r'C:\safe\file.db', True))
        self.api.GetDriveTypeW = lambda _: 2
        self.assertFalse(P.path_is_local(r'C:\safe'))
        self.api.GetDriveTypeW = lambda _: 3
        self.api.GetFileAttributesW = lambda path: 0x410 if path.lower() == r'c:\safe' else 16
        self.assertFalse(P.path_is_local(r'C:\safe\nested'))

    def test_page_only_4096_shared_read_final_path_and_pin(self):
        self.assertEqual(P.read_page(r'C:\safe\message_0.db'), b'F' * 4096)
        self.assertEqual(len(self.api.reads), 1)
        self.assertEqual(self.api.reads[0][1], 4096)
        file_open = self.api.opens[-1]
        self.assertEqual(file_open[1:4], (0x80000000, 7, 3))
        self.assertTrue(all(call[2] == 3 for call in self.api.opens[:-1]))
        self.assertEqual(len(self.api.closed), 3)
        self.api.mismatch = True
        with self.assertRaisesRegex(P.PlatformError, '^file_identity_mismatch$'):
            P.read_page(r'C:\safe\message_0.db')
        self.assertEqual(len(self.api.reads), 1)

    def test_private_write_checks_acl_before_any_bytes_no_overwrite(self):
        @contextlib.contextmanager
        def descriptor(api, directory):
            yield P.SA()
        checks = []
        def verify(api, handle, directory):
            checks.append(directory)
            if not directory and reject[0]:
                raise P.PlatformError('private_acl_verification_failed')
        reject = [False]
        with patch.object(P, '_descriptor', descriptor), patch.object(P, '_verify_private', verify):
            P.private_json(r'C:\safe\key.json', {'synthetic': 'not-a-key'})
            self.assertEqual(checks, [True, False])
            self.assertEqual(self.api.writes, [b'{"synthetic": "not-a-key"}'])
            self.assertEqual(self.api.opens[-1][1:4], (0x40030000, 0, 1))
            self.api.existing = True
            with self.assertRaisesRegex(P.PlatformError, '^private_file_create_failed$'):
                P.private_json(r'C:\safe\key.json', {})
            self.assertEqual(self.api.deletes, [])
            self.api.existing, reject[0] = False, True
            with self.assertRaisesRegex(P.PlatformError, '^private_acl_verification_failed$'):
                P.private_json(r'C:\safe\key.json', {})
            self.assertEqual(len(self.api.writes), 1)
            self.assertEqual(self.api.deletes[-1][1:], (4, 1))

    def test_exact_current_user_acl_protected_one_ace_only(self):
        api = self.api
        ace = P.ACE(0, 3, 20, 0x1f01ff)
        api.current_sid, api.sid_text, api.LocalFree = lambda: 'S-1-5-21-123', lambda _: 'S-1-5-21-123', lambda _: None
        def security(handle, kind, requested, owner, group, acl, sacl, descriptor):
            put(owner, P.PTR, 101)
            put(acl, P.PTR, 102)
            put(descriptor, P.PTR, 103)
            return 0
        control = [0x1000]
        count = [1]
        def acl_info(acl, result, size, kind):
            C.cast(result, C.POINTER(P.ACLINFO)).contents.count = count[0]
            return True
        api.GetSecurityInfo = security
        api.GetSecurityDescriptorControl = lambda sd, flags, rev: put(flags, P.U16, control[0]) or True
        api.GetAclInformation = acl_info
        api.GetAce = lambda acl, index, result: put(result, P.PTR, C.addressof(ace)) or True
        P._verify_private(api, 42, True)
        for change, restore in [(lambda: control.__setitem__(0, 0), lambda: control.__setitem__(0, 0x1000)),
                                (lambda: count.__setitem__(0, 2), lambda: count.__setitem__(0, 1)),
                                (lambda: setattr(ace, 'flags', 0x13), lambda: setattr(ace, 'flags', 3)),
                                (lambda: setattr(ace, 'mask', 1), lambda: setattr(ace, 'mask', 0x1f01ff))]:
            change()
            with self.assertRaisesRegex(P.PlatformError, '^private_acl_verification_failed$'):
                P._verify_private(api, 42, True)
            restore()


if __name__ == '__main__':
    unittest.main()
