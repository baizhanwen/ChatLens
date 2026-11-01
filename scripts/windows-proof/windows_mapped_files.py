"""Bounded Windows x64 mapped-file metadata queries.

Only documented region attributes and backing file names are queried. This
module never opens a process, reads process pages or database contents, copies
handles, or exports paths. The caller owns its already verified process handle.

SDK references:
https://learn.microsoft.com/windows/win32/api/memoryapi/nf-memoryapi-virtualqueryex
https://learn.microsoft.com/windows/win32/api/memoryapi/nf-memoryapi-queryvirtualmemoryinformation
https://learn.microsoft.com/windows/win32/api/memoryapi/ns-memoryapi-win32_memory_region_information
https://learn.microsoft.com/windows/win32/api/psapi/nf-psapi-getmappedfilenamew
"""
from __future__ import annotations

import ctypes as C
import math
import os
import time

from windows_keyproof_platform import MBI64, PlatformError

MAX_REGIONS = 32768
NAME_CAPACITY = 32768
MAPPED_COUNTS = ('mapped_regions_queried', 'mapped_data_regions_seen',
                 'mapped_pagefile_regions_skipped', 'mapped_names_seen',
                 'mapped_name_query_failures')
MEM_COMMIT, MEM_RESERVE, MEM_FREE = 0x1000, 0x2000, 0x10000
MEM_PRIVATE, MEM_MAPPED, MEM_IMAGE = 0x20000, 0x40000, 0x1000000


class Architecture(C.Structure):
    _fields_ = [('wProcessorArchitecture', C.c_uint16), ('wReserved', C.c_uint16)]


class ArchitectureUnion(C.Union):
    _anonymous_ = ('architecture',)
    _fields_ = [('dwOemId', C.c_uint32), ('architecture', Architecture)]


class SystemInfo(C.Structure):
    _anonymous_ = ('architecture',)
    _fields_ = [('architecture', ArchitectureUnion), ('dwPageSize', C.c_uint32),
                ('lpMinimumApplicationAddress', C.c_void_p),
                ('lpMaximumApplicationAddress', C.c_void_p),
                ('dwActiveProcessorMask', C.c_size_t), ('dwNumberOfProcessors', C.c_uint32),
                ('dwProcessorType', C.c_uint32), ('dwAllocationGranularity', C.c_uint32),
                ('wProcessorLevel', C.c_uint16), ('wProcessorRevision', C.c_uint16)]


class RegionFlags(C.LittleEndianStructure):
    # SDK bit fields, rather than guessing from an unsuccessful name query.
    _fields_ = [('Private', C.c_uint32, 1), ('MappedDataFile', C.c_uint32, 1),
                ('MappedImage', C.c_uint32, 1), ('MappedPageFile', C.c_uint32, 1),
                ('MappedPhysical', C.c_uint32, 1), ('DirectMapped', C.c_uint32, 1),
                ('Reserved', C.c_uint32, 26)]


class RegionFlagsUnion(C.Union):
    _anonymous_ = ('bits',)
    _fields_ = [('bits', RegionFlags), ('_storage', C.c_uint32)]


class MemoryRegionInformation(C.Structure):
    _anonymous_ = ('flags',)
    _fields_ = [('AllocationBase', C.c_void_p), ('AllocationProtect', C.c_uint32),
                ('flags', RegionFlagsUnion), ('RegionSize', C.c_size_t),
                ('CommitSize', C.c_size_t)]


class MappedFilesAPI:
    def __init__(self):
        if os.name != 'nt' or C.sizeof(C.c_void_p) != 8:
            raise PlatformError('windows_x64_python_required')
        try:
            kernel = C.WinDLL('kernel32', use_last_error=True)
            regions = C.WinDLL('api-ms-win-core-memory-l1-1-4.dll', use_last_error=True)
            signatures = {
                'GetNativeSystemInfo': (kernel, [C.POINTER(SystemInfo)], None),
                'VirtualQueryEx': (kernel, [C.c_void_p, C.c_void_p, C.POINTER(MBI64), C.c_size_t], C.c_size_t),
                'K32GetMappedFileNameW': (kernel, [C.c_void_p, C.c_void_p, C.c_wchar_p, C.c_uint32], C.c_uint32),
                'QueryVirtualMemoryInformation': (regions, [C.c_void_p, C.c_void_p, C.c_int32, C.c_void_p,
                                                           C.c_size_t, C.POINTER(C.c_size_t)], C.c_int32),
                'GetLogicalDrives': (kernel, [], C.c_uint32),
                'QueryDosDeviceW': (kernel, [C.c_wchar_p, C.c_wchar_p, C.c_uint32], C.c_uint32),
            }
            for name, (library, arguments, result) in signatures.items():
                method = getattr(library, name)
                method.argtypes, method.restype = arguments, result
                setattr(self, name, method)
        except (AttributeError, OSError):
            raise PlatformError('windows_mapped_api_unavailable') from None


def _basic_key(info):
    return (info.BaseAddress, info.AllocationBase, info.AllocationProtect,
            info.RegionSize, info.State, info.Protect, info.Type)


def _region_key(info):
    # CommitSize can change as copy-on-write pages are charged. It does not
    # change the identity of the mapped allocation.
    return (info.AllocationBase, info.AllocationProtect, info.RegionSize,
            info.Private, info.MappedDataFile, info.MappedImage,
            info.MappedPageFile, info.MappedPhysical, info.DirectMapped, info.Reserved)


def mapped_paths(api, process_handle, *, diagnostics=None, deadline=None,
                 max_regions=MAX_REGIONS, clock=time.monotonic):
    """Return complete data-file backing names or raise a fixed-code error.

    mapped_regions_queried counts every VirtualQueryEx call, including the
    post-query checks of mapped allocations. The caller can share this budget
    across a whole verified process family and more than one pass.
    """
    detail = diagnostics if isinstance(diagnostics, dict) else {}
    detail.clear()
    detail.update({key: 0 for key in MAPPED_COUNTS})
    detail['query_completed'] = False
    try:
        if (type(max_regions) is not int or not 0 <= max_regions <= MAX_REGIONS
                or deadline is not None and (type(deadline) not in (int, float)
                                               or not math.isfinite(deadline))):
            raise PlatformError('windows_mapped_budget_invalid')
        if not process_handle:
            raise PlatformError('windows_mapped_process_unavailable')
        end_time = clock() + 3
        if deadline is not None:
            end_time = min(end_time, deadline)

        def budget():
            if clock() >= end_time:
                raise PlatformError('windows_mapped_query_deadline')

        budget()
        if (C.sizeof(SystemInfo) != 48 or SystemInfo.lpMaximumApplicationAddress.offset != 16
                or C.sizeof(MBI64) != 48 or MBI64.RegionSize.offset != 24
                or C.sizeof(MemoryRegionInformation) != 32
                or MemoryRegionInformation.RegionSize.offset != 16):
            raise PlatformError('windows_mapped_sdk_layout_invalid')
        if not max_regions:
            raise PlatformError('windows_mapped_region_limit')
        system = SystemInfo()
        api.GetNativeSystemInfo(C.byref(system))
        budget()
        minimum, maximum, page = (system.lpMinimumApplicationAddress,
                                  system.lpMaximumApplicationAddress, system.dwPageSize)
        if (system.wProcessorArchitecture != 9 or not minimum or maximum is None
                or not page or page & (page - 1) or minimum % page
                or not minimum <= maximum < 2**64 - 1 or (maximum + 1) % page):
            raise PlatformError('windows_mapped_system_bounds_invalid')
        limit = maximum + 1

        def basic(address):
            budget()
            if detail['mapped_regions_queried'] >= max_regions:
                raise PlatformError('windows_mapped_region_limit')
            detail['mapped_regions_queried'] += 1
            info = MBI64()
            returned = api.VirtualQueryEx(process_handle, address, C.byref(info), C.sizeof(info))
            budget()
            high = info.BaseAddress + info.RegionSize
            if (type(returned) is not int or returned != C.sizeof(info) or not info.RegionSize
                    or info.BaseAddress % page or info.RegionSize % page
                    or info.BaseAddress > address or high <= address or high > 2**64):
                raise PlatformError('windows_mapped_region_invalid')
            if info.State not in (MEM_FREE, MEM_RESERVE, MEM_COMMIT):
                raise PlatformError('windows_mapped_region_invalid')
            if info.State != MEM_FREE and (not info.AllocationBase
                    or info.AllocationBase > info.BaseAddress or info.AllocationBase % page
                    or high > limit):
                raise PlatformError('windows_mapped_region_invalid')
            return info, min(high, limit)

        def allocation(address, info):
            budget()
            value, returned = MemoryRegionInformation(), C.c_size_t()
            if not api.QueryVirtualMemoryInformation(process_handle, address, 0,
                    C.byref(value), C.sizeof(value), C.byref(returned)):
                raise PlatformError('windows_mapped_allocation_unavailable')
            budget()
            allocation_end = (value.AllocationBase or 0) + value.RegionSize
            if (returned.value != C.sizeof(value) or not value.AllocationBase
                    or value.AllocationBase != info.AllocationBase
                    or value.AllocationProtect != info.AllocationProtect
                    or not value.RegionSize or value.RegionSize % page
                    or allocation_end < info.BaseAddress + info.RegionSize or allocation_end > limit
                    or value.CommitSize > value.RegionSize or value.Reserved):
                raise PlatformError('windows_mapped_allocation_invalid')
            return value

        def kind(value):
            data = bool(value.MappedDataFile or value.DirectMapped)
            nondata = bool(value.Private or value.MappedImage or value.MappedPageFile or value.MappedPhysical)
            if data and not nondata:
                return 'data'
            if (not data and not value.Private and not value.MappedImage
                    and bool(value.MappedPageFile) != bool(value.MappedPhysical)):
                return 'pagefile' if value.MappedPageFile else 'physical'
            raise PlatformError('windows_mapped_allocation_kind_unknown')

        paths, address = [], minimum
        while address < limit:
            info, high = basic(address)
            if info.State == MEM_FREE:
                address = high
                continue
            if info.Type in (MEM_PRIVATE, MEM_IMAGE):
                address = high
                continue
            if info.Type != MEM_MAPPED:
                raise PlatformError('windows_mapped_region_kind_unknown')
            before = allocation(address, info)
            allocation_kind = kind(before)
            name = None
            if allocation_kind == 'data':
                detail['mapped_data_regions_seen'] += 1
                buffer = C.create_unicode_buffer(NAME_CAPACITY)
                budget()
                try:
                    copied = api.K32GetMappedFileNameW(process_handle, address, buffer, len(buffer))
                except Exception:
                    detail['mapped_name_query_failures'] += 1
                    raise PlatformError('windows_mapped_name_unavailable') from None
                budget()
                if type(copied) is not int or not 0 < copied < len(buffer):
                    detail['mapped_name_query_failures'] += 1
                    raise PlatformError('windows_mapped_name_unavailable')
                try:
                    name = C.wstring_at(buffer, copied)
                except Exception:
                    detail['mapped_name_query_failures'] += 1
                    raise PlatformError('windows_mapped_name_invalid') from None
                if not name or '\0' in name or buffer[copied] != '\0':
                    detail['mapped_name_query_failures'] += 1
                    raise PlatformError('windows_mapped_name_invalid')
            after, _ = basic(address)
            after_allocation = allocation(address, after)
            if _basic_key(after) != _basic_key(info) or _region_key(after_allocation) != _region_key(before):
                raise PlatformError('windows_mapped_allocation_changed')
            if allocation_kind == 'data':
                paths.append(name)
                detail['mapped_names_seen'] += 1
            elif allocation_kind == 'pagefile':
                detail['mapped_pagefile_regions_skipped'] += 1
            address = high
        budget()
        detail['query_completed'] = True
        return paths
    except PlatformError:
        detail['query_completed'] = False
        raise
    except Exception:
        detail['query_completed'] = False
        raise PlatformError('windows_mapped_metadata_unavailable') from None
