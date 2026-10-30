"""Narrow Windows x64 I/O for a local, first-page-only key proof.

No DLL is loaded on import. No process writes, privilege changes or discovery.
The caller owns the total memory-read budget and public-result redaction.
"""
from contextlib import contextmanager
import ctypes as C
import json
import ntpath
import os
import re
import time

U32, U16, U64, BOOL, PTR, SIZE = C.c_uint32, C.c_uint16, C.c_uint64, C.c_int32, C.c_void_p, C.c_size_t
P32, PPTR = C.POINTER(U32), C.POINTER(PTR)
MAX_ADDRESS, MAX_READ = 0x800000000000, 1024 * 1024


class PlatformError(Exception):
    """Contains only a static reason code, never native errors or paths."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class MBI64(C.Structure):
    _fields_ = [('BaseAddress', U64), ('AllocationBase', U64), ('AllocationProtect', U32),
                ('Alignment1', U32), ('RegionSize', U64), ('State', U32),
                ('Protect', U32), ('Type', U32), ('Alignment2', U32)]


class FILETIME(C.Structure):
    _fields_ = [('low', U32), ('high', U32)]


class FILEINFO(C.Structure):
    _fields_ = [('attributes', U32), ('creation', FILETIME), ('access', FILETIME),
                ('write', FILETIME), ('volume', U32), ('size_high', U32), ('size_low', U32),
                ('links', U32), ('index_high', U32), ('index_low', U32)]


class SA(C.Structure):
    _fields_ = [('length', U32), ('descriptor', PTR), ('inherit', BOOL)]


class ACLINFO(C.Structure):
    _fields_ = [('count', U32), ('used', U32), ('free', U32)]


class ACE(C.Structure):
    _fields_ = [('kind', C.c_ubyte), ('flags', C.c_ubyte), ('size', U16), ('mask', U32)]


class _API:
    def __init__(self):
        if os.name != 'nt' or C.sizeof(PTR) != 8:
            raise PlatformError('windows_x64_python_required')
        try:
            k, a = C.WinDLL('kernel32', use_last_error=True), C.WinDLL('advapi32', use_last_error=True)
        except OSError:
            raise PlatformError('required_windows_api_unavailable') from None
        signatures = {
            'OpenProcess': (k, [U32, BOOL, U32], PTR), 'CloseHandle': (k, [PTR], BOOL),
            'QueryFullProcessImageNameW': (k, [PTR, U32, C.c_wchar_p, P32], BOOL),
            'GetProcessTimes': (k, [PTR] + [C.POINTER(FILETIME)] * 4, BOOL),
            'IsWow64Process2': (k, [PTR, C.POINTER(U16), C.POINTER(U16)], BOOL),
            'VirtualQueryEx': (k, [PTR, PTR, C.POINTER(MBI64), SIZE], SIZE),
            'ReadProcessMemory': (k, [PTR, PTR, PTR, SIZE, C.POINTER(SIZE)], BOOL),
            'GetDriveTypeW': (k, [C.c_wchar_p], U32), 'GetFileAttributesW': (k, [C.c_wchar_p], U32),
            'CreateFileW': (k, [C.c_wchar_p, U32, U32, C.POINTER(SA), U32, U32, PTR], PTR),
            'CreateDirectoryW': (k, [C.c_wchar_p, C.POINTER(SA)], BOOL),
            'GetFinalPathNameByHandleW': (k, [PTR, C.c_wchar_p, U32, U32], U32),
            'GetFileInformationByHandle': (k, [PTR, C.POINTER(FILEINFO)], BOOL),
            'ReadFile': (k, [PTR, PTR, U32, P32, PTR], BOOL),
            'WriteFile': (k, [PTR, PTR, U32, P32, PTR], BOOL), 'FlushFileBuffers': (k, [PTR], BOOL),
            'SetFileInformationByHandle': (k, [PTR, U32, PTR, U32], BOOL),
            'LocalFree': (k, [PTR], PTR), 'GetCurrentProcess': (k, [], PTR),
            'OpenProcessToken': (a, [PTR, U32, PPTR], BOOL),
            'GetTokenInformation': (a, [PTR, U32, PTR, U32, P32], BOOL),
            'ConvertSidToStringSidW': (a, [PTR, PPTR], BOOL),
            'ConvertStringSecurityDescriptorToSecurityDescriptorW': (a, [C.c_wchar_p, U32, PPTR, P32], BOOL),
            'GetSecurityInfo': (a, [PTR, U32, U32] + [PPTR] * 5, U32),
            'GetSecurityDescriptorControl': (a, [PTR, C.POINTER(U16), P32], BOOL),
            'GetAclInformation': (a, [PTR, PTR, U32, U32], BOOL),
            'GetAce': (a, [PTR, U32, PPTR], BOOL),
        }
        try:
            for name, (dll, args, result) in signatures.items():
                fn = getattr(dll, name)
                fn.argtypes, fn.restype = args, result
                setattr(self, name, fn)
        except AttributeError:
            raise PlatformError('required_windows_api_unavailable') from None

    def sid_text(self, sid):
        result = PTR()
        if not self.ConvertSidToStringSidW(sid, C.byref(result)):
            raise PlatformError('sid_query_failed')
        try:
            return C.wstring_at(result)
        finally:
            self.LocalFree(result)

    def current_sid(self):
        token, needed = PTR(), U32()
        if not self.OpenProcessToken(self.GetCurrentProcess(), 8, C.byref(token)):
            raise PlatformError('current_user_query_failed')
        try:
            self.GetTokenInformation(token, 1, None, 0, C.byref(needed))
            if not 8 <= needed.value <= 65536:
                raise PlatformError('current_user_query_failed')
            buffer = C.create_string_buffer(needed.value)
            if not self.GetTokenInformation(token, 1, buffer, len(buffer), C.byref(needed)):
                raise PlatformError('current_user_query_failed')
            return self.sid_text(C.cast(buffer, C.POINTER(PTR))[0])
        finally:
            self.CloseHandle(token)


_native = None


def _api():
    global _native
    if _native is None:
        _native = _API()
    return _native


def _path(path):
    try:
        value = os.fspath(path).replace('/', '\\')
        if not re.fullmatch(r'[A-Za-z]:\\[^\x00:]*', value):
            raise ValueError
        return ntpath.normpath(value)
    except (TypeError, ValueError):
        raise PlatformError('local_absolute_path_required') from None


def path_is_local(path, require_dir=False):
    """Existing, fixed-drive path with no reparse component (never discover)."""
    api = _api()
    try:
        value = _path(path)
        root = value[:3]
        if api.GetDriveTypeW(root) != 3:
            return False
        parts = [root]
        for part in value[3:].split('\\'):
            if part:
                parts.append(ntpath.join(parts[-1], part))
        for i, part in enumerate(parts):
            attr = api.GetFileAttributesW(part)
            if attr == 0xffffffff or attr & 0x400 or ((i < len(parts) - 1 or require_dir) and not attr & 16):
                return False
        return True
    except PlatformError:
        return False


def _checked(api, handle, path, directory):
    info, final = FILEINFO(), C.create_unicode_buffer(32768)
    count = api.GetFinalPathNameByHandleW(handle, final, len(final), 0)
    if not count or count >= len(final) or not api.GetFileInformationByHandle(handle, C.byref(info)):
        raise PlatformError('file_identity_query_failed')
    actual = final.value[4:] if final.value.startswith('\\\\?\\') else final.value
    if ntpath.normcase(actual) != ntpath.normcase(_path(path)) or info.attributes & 0x400 or bool(info.attributes & 16) != directory:
        raise PlatformError('file_identity_mismatch')


@contextmanager
def _parents(api, path):
    parent, handles = ntpath.dirname(path), []
    if not path_is_local(parent, True):
        raise PlatformError('unsafe_parent_path')
    current = parent[:3]
    try:
        for part in [''] + parent[3:].split('\\'):
            if part:
                current = ntpath.join(current, part)
            elif handles:
                continue
            # No DELETE sharing pins each ancestor against rename/reparse swaps.
            handle = api.CreateFileW(current, 0x80 | 0x20000, 3, None, 3, 0x02200000, None)
            if handle in (None, C.c_void_p(-1).value):
                raise PlatformError('parent_open_failed')
            handles.append(handle)
            _checked(api, handle, current, True)
        yield handles[-1]
    finally:
        for handle in reversed(handles):
            api.CloseHandle(handle)


def read_page(path):
    api, path = _api(), _path(path)
    if not path_is_local(path):
        raise PlatformError('unsafe_database_path')
    with _parents(api, path):
        handle = api.CreateFileW(path, 0x80000000, 7, None, 3, 0x00200000, None)
        if handle in (None, C.c_void_p(-1).value):
            raise PlatformError('database_open_failed')
        try:
            _checked(api, handle, path, False)
            buffer, count = C.create_string_buffer(4096), U32()
            if not api.ReadFile(handle, buffer, 4096, C.byref(count), None) or count.value != 4096:
                raise PlatformError('database_first_page_unavailable')
            return buffer.raw
        finally:
            api.CloseHandle(handle)


@contextmanager
def _descriptor(api, directory):
    sid, descriptor = api.current_sid(), PTR()
    text = 'O:' + sid + 'D:P(A;' + ('OICI' if directory else '') + ';FA;;;' + sid + ')'
    if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(text, 1, C.byref(descriptor), None):
        raise PlatformError('private_acl_creation_failed')
    try:
        yield SA(C.sizeof(SA), descriptor, False)
    finally:
        api.LocalFree(descriptor)


def _verify_private(api, handle, directory):
    owner, acl, descriptor = PTR(), PTR(), PTR()
    if api.GetSecurityInfo(handle, 1, 5, C.byref(owner), None, C.byref(acl), None, C.byref(descriptor)):
        raise PlatformError('private_acl_query_failed')
    try:
        control, revision, info, ace_ptr = U16(), U32(), ACLINFO(), PTR()
        valid = (api.GetSecurityDescriptorControl(descriptor, C.byref(control), C.byref(revision))
                 and control.value & 0x1000 and owner and acl
                 and api.sid_text(owner) == api.current_sid()
                 and api.GetAclInformation(acl, C.byref(info), C.sizeof(info), 2) and info.count == 1
                 and api.GetAce(acl, 0, C.byref(ace_ptr)))
        if valid:
            ace = C.cast(ace_ptr, C.POINTER(ACE)).contents
            valid = (ace.kind == 0 and ace.flags == (3 if directory else 0) and ace.mask == 0x1f01ff
                     and ace.size >= 16 and api.sid_text(ace_ptr.value + 8) == api.current_sid())
        if not valid:
            raise PlatformError('private_acl_verification_failed')
    finally:
        api.LocalFree(descriptor)


def private_dir(path):
    api, path = _api(), _path(path)
    with _parents(api, path), _descriptor(api, True) as attributes:
        if not api.CreateDirectoryW(path, C.byref(attributes)):
            raise PlatformError('private_directory_create_failed')
        handle = api.CreateFileW(path, 0x20080, 3, None, 3, 0x02200000, None)
        if handle in (None, C.c_void_p(-1).value):
            raise PlatformError('private_directory_open_failed')
        try:
            _checked(api, handle, path, True)
            _verify_private(api, handle, True)
        finally:
            api.CloseHandle(handle)


def private_json(path, data):
    api, path = _api(), _path(path)
    try:
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError):
        raise PlatformError('private_json_invalid') from None
    if len(payload) > MAX_READ:
        raise PlatformError('private_json_size_limit')
    with _parents(api, path) as parent, _descriptor(api, False) as attributes:
        _verify_private(api, parent, True)
        handle = api.CreateFileW(path, 0x40030000, 0, C.byref(attributes), 1, 0x00200000, None)
        if handle in (None, C.c_void_p(-1).value):
            raise PlatformError('private_file_create_failed')
        complete = False
        try:
            _checked(api, handle, path, False)
            _verify_private(api, handle, False)
            count = U32()
            if not api.WriteFile(handle, payload, len(payload), C.byref(count), None) or count.value != len(payload) or not api.FlushFileBuffers(handle):
                raise PlatformError('private_file_write_failed')
            complete = True
        finally:
            if not complete:
                # Delete only this newly-created file through its still-open handle.
                disposition = C.c_ubyte(True)  # FILE_DISPOSITION_INFO.Boolean
                api.SetFileInformationByHandle(handle, 4, C.byref(disposition), C.sizeof(disposition))
            api.CloseHandle(handle)


class WindowsProcess:
    def __init__(self, pid, expected_exe, expected_creation_time=None):
        if type(pid) is not int or not 0 < pid <= 0xffffffff:
            raise PlatformError('invalid_process_id')
        if expected_creation_time is not None and (type(expected_creation_time) is not int or not 0 < expected_creation_time < 2**64):
            raise PlatformError('invalid_process_creation_time')
        self.pid, self.expected_exe = pid, _path(expected_exe)
        self.expected_creation_time, self.creation_time, self.handle = expected_creation_time, None, None

    def __enter__(self):
        if self.handle:
            raise PlatformError('process_already_open')
        self.api = _api()
        self.handle = self.api.OpenProcess(0x410, False, self.pid)
        if not self.handle:
            raise PlatformError('process_open_failed')
        try:
            if not path_is_local(self.expected_exe):
                raise PlatformError('unsafe_executable_path')
            image, length, process_machine, native_machine = C.create_unicode_buffer(32768), U32(32768), U16(), U16()
            if not self.api.QueryFullProcessImageNameW(self.handle, 0, image, C.byref(length)):
                raise PlatformError('process_identity_query_failed')
            if ntpath.normcase(_path(image.value)) != ntpath.normcase(self.expected_exe):
                raise PlatformError('process_executable_mismatch')
            if not self.api.IsWow64Process2(self.handle, C.byref(process_machine), C.byref(native_machine)) or process_machine.value != 0 or native_machine.value != 0x8664:
                raise PlatformError('target_amd64_required')
            values = [FILETIME() for _ in range(4)]
            if not self.api.GetProcessTimes(self.handle, *(C.byref(v) for v in values)):
                raise PlatformError('process_time_query_failed')
            self.creation_time = values[0].low | values[0].high << 32
            if self.expected_creation_time is not None and self.creation_time != self.expected_creation_time:
                raise PlatformError('process_creation_time_mismatch')
            self.region_deadline = time.monotonic() + 90
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def regions(self, include_image=False):
        """Image literals are opt-in; object/reference scanning stays private."""
        address, deadline = 0, self.region_deadline
        for _ in range(100000):
            if not self.handle:
                raise PlatformError('process_not_open')
            if time.monotonic() >= deadline:
                raise PlatformError('region_time_limit')
            info = MBI64()
            size = self.api.VirtualQueryEx(self.handle, address, C.byref(info), C.sizeof(info))
            if not size:
                if C.get_last_error() == 87:
                    return
                raise PlatformError('region_query_failed')
            end = info.BaseAddress + info.RegionSize
            if size != C.sizeof(info) or not info.RegionSize or info.BaseAddress > address or end <= address or end > MAX_ADDRESS:
                raise PlatformError('invalid_memory_region')
            kinds = (0x20000, 0x1000000) if include_image else (0x20000,)
            if info.State == 0x1000 and info.Type in kinds and not info.Protect & 0x101 and info.Protect & 0xff in (2, 4, 8, 0x20, 0x40, 0x80):
                yield int(info.BaseAddress), int(info.RegionSize)
            address = end
            if address >= MAX_ADDRESS:
                return
        raise PlatformError('region_query_limit')

    def read(self, address, size):
        if not self.handle:
            raise PlatformError('process_not_open')
        if type(address) is not int or type(size) is not int or not 0 < size <= MAX_READ or not 0x10000 <= address < MAX_ADDRESS - size + 1:
            raise PlatformError('invalid_memory_read')
        info = MBI64()
        result = self.api.VirtualQueryEx(self.handle, address, C.byref(info), C.sizeof(info))
        if (result != C.sizeof(info) or info.State != 0x1000 or info.Type not in (0x20000, 0x1000000)
                or info.Protect & 0x101 or info.Protect & 0xff not in (2, 4, 8, 0x20, 0x40, 0x80)
                or not info.BaseAddress <= address < address + size <= info.BaseAddress + info.RegionSize):
            return b''
        buffer, received = C.create_string_buffer(size), SIZE()
        ok = self.api.ReadProcessMemory(self.handle, address, buffer, size, C.byref(received))
        # A concurrent unmap/partial copy is a gap, not contiguous readable bytes.
        if not ok or received.value != size:
            return b''
        return buffer.raw
