"""Standalone native canaries using this worker's own synthetic file only.

Run with the existing portable Windows x64 Python, using -I -S -B -X utf8.
The parent owns a temporary directory and a six-second worker deadline. The
worker captures only GetCurrentProcess(), never another process or WeChat.
No raw name, path, handle value, PID, file bytes or exception leaves the worker.

SDK: https://learn.microsoft.com/en-us/windows/win32/api/processsnapshot/ns-processsnapshot-pss_handle_entry
TypeNameLength and ObjectNameLength are byte counts, not character counts.
--mapped-path tests K32GetMappedFileNameW on this worker's own 4096-byte map.
It does not run PSS or inspect any other process.
"""
from __future__ import annotations

import ctypes as C
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
import tempfile
import time

KIND = 'chatlens_windows_pss_selfcheck'
MAPPED_KIND = 'chatlens_windows_mapped_path_selfcheck'
TOOL_VERSION = '20261101-02'
MAPPED_NAME_CAPACITY = 32768
MAPPED_FILE_BYTES = 4096
MAX_HANDLES = 32768
MAX_NAME_BYTES = 65532
CAPTURE_FLAGS = 0x04 | 0x08
TYPE_LABELS = ('File', 'Event', 'Thread', 'Process', 'Section', 'Mutant',
               'Semaphore', 'Directory', 'Key', 'Other')
COUNTS = ('handles_seen', 'type_names_present', 'names_present', 'type_exact_file',
          'type_single_trailing_nul_file', 'type_embedded_nul',
          'type_invalid_utf16_or_length', 'object_single_trailing_nul',
          'object_embedded_nul', 'object_invalid_utf16_or_length')
BOOLEANS = ('native_api_attempted', 'query_completed', 'cleanup_succeeded',
            'fixture_created', 'fixture_closed', 'parent_fixture_removed',
            'drive_mapping_succeeded', 'self_canary_handle_seen',
            'self_canary_type_name_present', 'self_canary_name_present',
            'self_canary_type_exact_file', 'self_canary_type_single_trailing_nul_file',
            'self_canary_exact_path_seen', 'self_canary_trimmed_path_seen')
REASONS = {'pss_selfcheck_windows_x64_required', 'pss_selfcheck_invalid_arguments',
           'pss_selfcheck_worker_timeout', 'pss_selfcheck_worker_unavailable',
           'pss_selfcheck_worker_output_invalid', 'pss_selfcheck_fixture_unavailable',
           'pss_selfcheck_snapshot_unavailable', 'pss_selfcheck_capture_incomplete',
           'pss_selfcheck_cleanup_unavailable', 'pss_selfcheck_canary_not_observed',
           'pss_selfcheck_canary_file_metadata_observed'}
MAPPED_COUNTS = ('mapped_regions_created', 'mapped_name_characters')
MAPPED_BOOLEANS = ('native_api_attempted', 'fixture_created', 'fixture_closed',
                  'parent_fixture_removed', 'drive_mapping_succeeded', 'map_created',
                  'map_closed', 'mapped_query_completed', 'mapped_query_truncated',
                  'self_canary_exact_path_seen', 'cleanup_succeeded')
MAPPED_REASONS = {'mapped_selfcheck_' + suffix for suffix in (
    'windows_x64_required', 'worker_timeout', 'worker_unavailable', 'worker_output_invalid',
    'fixture_unavailable', 'mapping_unavailable', 'path_unavailable', 'path_mismatch',
    'device_mapping_unavailable', 'query_truncated', 'capture_incomplete', 'cleanup_unavailable',
    'canary_file_metadata_observed')}


class FileTime(C.Structure):
    _fields_ = [('low', C.c_uint32), ('high', C.c_uint32)]


class HandleEntry(C.Structure):
    _fields_ = [('Handle', C.c_void_p), ('Flags', C.c_uint32), ('ObjectType', C.c_uint32),
                ('CaptureTime', FileTime), ('Attributes', C.c_uint32),
                ('GrantedAccess', C.c_uint32), ('HandleCount', C.c_uint32),
                ('PointerCount', C.c_uint32), ('PagedPoolCharge', C.c_uint32),
                ('NonPagedPoolCharge', C.c_uint32), ('CreationTime', FileTime),
                ('TypeNameLength', C.c_uint16), ('TypeName', C.c_void_p),
                ('ObjectNameLength', C.c_uint16), ('ObjectName', C.c_void_p),
                ('TypeSpecificInformation', C.c_uint64 * 6)]


def _windows_x64():
    return (sys.platform == 'win32' and C.sizeof(C.c_void_p) == 8
            and sysconfig.get_platform().casefold() == 'win-amd64')


def _diagnostics(kind=KIND):
    if kind == MAPPED_KIND:
        return {**{key: 0 for key in MAPPED_COUNTS}, **{key: False for key in MAPPED_BOOLEANS}}
    return {**{key: 0 for key in COUNTS}, **{key: False for key in BOOLEANS},
            'known_type_histogram': {label: 0 for label in TYPE_LABELS}}


def _report(status, reason, diagnostics=None, *, kind=KIND):
    return {'schema_version': 1, 'tool_version': TOOL_VERSION, 'kind': kind, 'status': status, 'reason': reason,
            'diagnostics': diagnostics if diagnostics is not None else _diagnostics(kind)}


def decode_name(pointer, byte_length):
    """Read only the caller-owned snapshot buffer using the SDK byte count."""
    if (not pointer or type(byte_length) is not int or not 0 < byte_length <= MAX_NAME_BYTES
            or byte_length % 2):
        raise ValueError('invalid_name')
    return C.string_at(pointer, byte_length).decode('utf-16-le', errors='strict')


def name_shape(value):
    """Keep exact and one terminal NUL separate; embedded/repeated NULs fail."""
    if '\0' not in value:
        return 'exact', value
    if value.endswith('\0') and '\0' not in value[:-1]:
        return 'single_trailing_nul', value[:-1]
    return 'embedded_nul', None


def _canonical(value):
    if value.startswith('\\\\?\\') or value.startswith('\\??\\'):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value.replace('/', '\\')))


class SnapshotAPI:
    def __init__(self):
        kernel = C.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'GetCurrentProcess': ([], C.c_void_p),
            'PssCaptureSnapshot': ([C.c_void_p, C.c_uint32, C.c_uint32, C.POINTER(C.c_void_p)], C.c_uint32),
            'PssFreeSnapshot': ([C.c_void_p, C.c_void_p], C.c_uint32),
            'PssWalkMarkerCreate': ([C.c_void_p, C.POINTER(C.c_void_p)], C.c_uint32),
            'PssWalkMarkerFree': ([C.c_void_p], C.c_uint32),
            'PssWalkSnapshot': ([C.c_void_p, C.c_uint32, C.c_void_p, C.c_void_p, C.c_uint32], C.c_uint32),
            'QueryDosDeviceW': ([C.c_wchar_p, C.c_wchar_p, C.c_uint32], C.c_uint32),
        }
        for name, (arguments, result) in signatures.items():
            method = getattr(kernel, name)
            method.argtypes, method.restype = arguments, result
            setattr(self, name, method)


class MappedAPI:
    """Only metadata query and mapping lifecycle for the worker's own file."""
    def __init__(self):
        kernel = C.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'GetCurrentProcess': ([], C.c_void_p),
            'QueryDosDeviceW': ([C.c_wchar_p, C.c_wchar_p, C.c_uint32], C.c_uint32),
            'CreateFileMappingW': ([C.c_void_p, C.c_void_p, C.c_uint32, C.c_uint32,
                                    C.c_uint32, C.c_wchar_p], C.c_void_p),
            'MapViewOfFile': ([C.c_void_p, C.c_uint32, C.c_uint32, C.c_uint32, C.c_size_t], C.c_void_p),
            'UnmapViewOfFile': ([C.c_void_p], C.c_int32),
            'CloseHandle': ([C.c_void_p], C.c_int32),
            'K32GetMappedFileNameW': ([C.c_void_p, C.c_void_p, C.c_wchar_p, C.c_uint32], C.c_uint32),
        }
        for name, (arguments, result) in signatures.items():
            method = getattr(kernel, name)
            method.argtypes, method.restype = arguments, result
            setattr(self, name, method)


def observe_mapped_self(api, canary_path, canary_handle, *, clock=time.monotonic):
    """Map only a caller-owned synthetic file; query its exact known view."""
    detail = _diagnostics(MAPPED_KIND)
    detail['native_api_attempted'] = detail['fixture_created'] = True
    mapping = view = None
    status, reason = '未验证', 'mapped_selfcheck_capture_incomplete'
    deadline = clock() + 3
    def budget():
        if clock() > deadline:
            raise ValueError
    try:
        expected = {_canonical(canary_path)}
        drive, target = ntpath.splitdrive(canary_path)[0], C.create_unicode_buffer(32768)
        if not re.fullmatch(r'[A-Za-z]:', drive):
            raise ValueError
        if api.QueryDosDeviceW(drive, target, len(target)) and target.value:
            expected.add(_canonical(target.value + canary_path[len(drive):]))
            detail['drive_mapping_succeeded'] = True
        budget()
        reason = 'mapped_selfcheck_mapping_unavailable'
        mapping = api.CreateFileMappingW(canary_handle, None, 4, 0, MAPPED_FILE_BYTES, None)
        if not mapping:
            raise ValueError
        budget()
        view = api.MapViewOfFile(mapping, 2, 0, 0, MAPPED_FILE_BYTES)
        if not view:
            raise ValueError
        detail['map_created'] = True
        detail['mapped_regions_created'] = 1
        budget()
        process = api.GetCurrentProcess()
        if not process:
            raise ValueError
        reason = 'mapped_selfcheck_capture_incomplete'
        buffer = C.create_unicode_buffer(MAPPED_NAME_CAPACITY)
        length = api.K32GetMappedFileNameW(process, view, buffer, len(buffer))
        budget()
        if type(length) is not int or length < 0:
            raise ValueError
        detail['mapped_name_characters'] = min(length, MAPPED_NAME_CAPACITY)
        if length >= len(buffer):
            detail['mapped_query_truncated'] = True
            reason = 'mapped_selfcheck_query_truncated'
        elif not length:
            status, reason = '失败', 'mapped_selfcheck_path_unavailable'
        else:
            # API length is WCHAR characters excluding the terminator. Unlike
            # the PSS byte counts, it must never be divided by two.
            text = C.wstring_at(buffer, length)
            if '\0' in text or buffer[length] != '\0':
                raise ValueError
            detail['mapped_query_completed'] = True
            detail['self_canary_exact_path_seen'] = _canonical(text) in expected
            if detail['self_canary_exact_path_seen']:
                status, reason = '通过', 'mapped_selfcheck_canary_file_metadata_observed'
            elif not detail['drive_mapping_succeeded']:
                reason = 'mapped_selfcheck_device_mapping_unavailable'
            else:
                status, reason = '失败', 'mapped_selfcheck_path_mismatch'
    except Exception:
        status = '未验证'
        detail['mapped_query_completed'] = False
    finally:
        cleanup_ok = True
        try:
            if view:
                cleanup_ok &= bool(api.UnmapViewOfFile(view))
        except Exception:
            cleanup_ok = False
        try:
            if mapping:
                cleanup_ok &= bool(api.CloseHandle(mapping))
        except Exception:
            cleanup_ok = False
        detail['cleanup_succeeded'] = cleanup_ok
        detail['map_closed'] = detail['map_created'] and cleanup_ok
    if not detail['cleanup_succeeded']:
        status, reason = '未验证', 'mapped_selfcheck_cleanup_unavailable'
    return _report(status, reason, detail, kind=MAPPED_KIND)


def mapped_worker(fixture_dir, *, platform_check=_windows_x64, api_factory=MappedAPI):
    if not platform_check():
        return _report('未验证', 'mapped_selfcheck_windows_x64_required', kind=MAPPED_KIND)
    reason = 'mapped_selfcheck_worker_unavailable'
    try:
        import msvcrt
        api = api_factory()
        reason = 'mapped_selfcheck_fixture_unavailable'
        with tempfile.NamedTemporaryFile(mode='w+b', prefix='chatlens-map-canary-',
                                         suffix='.synthetic', dir=fixture_dir) as fixture:
            fixture.write(bytes(MAPPED_FILE_BYTES))
            fixture.flush()
            result = observe_mapped_self(api, str(Path(fixture.name).resolve()),
                                         msvcrt.get_osfhandle(fixture.fileno()))
        result['diagnostics']['fixture_closed'] = True
        return result
    except Exception:
        return _report('未验证', reason, kind=MAPPED_KIND)


def observe_self(api, canary_path, canary_handle, *, clock=time.monotonic):
    """Bounded capture of this process, with the owned file's known handle."""
    detail = _diagnostics()
    detail['native_api_attempted'] = detail['fixture_created'] = True
    snapshot, marker = C.c_void_p(), C.c_void_p()
    process = None
    reason = 'pss_selfcheck_capture_incomplete'
    deadline = clock() + 3
    try:
        if (C.sizeof(HandleEntry) != 136 or HandleEntry.TypeName.offset != 64
                or HandleEntry.ObjectName.offset != 80):
            raise ValueError
        expected = {_canonical(canary_path)}
        drive, target = ntpath.splitdrive(canary_path)[0], C.create_unicode_buffer(32768)
        if not re.fullmatch(r'[A-Za-z]:', drive):
            raise ValueError
        if api.QueryDosDeviceW(drive, target, len(target)) and target.value:
            expected.add(_canonical(target.value + canary_path[len(drive):]))
            detail['drive_mapping_succeeded'] = True
        if clock() > deadline:
            raise ValueError
        process = api.GetCurrentProcess()
        if not process or api.PssCaptureSnapshot(process, CAPTURE_FLAGS, 0, C.byref(snapshot)) or not snapshot:
            reason = 'pss_selfcheck_snapshot_unavailable'
            raise ValueError
        if api.PssWalkMarkerCreate(None, C.byref(marker)) or not marker:
            raise ValueError
        for _ in range(MAX_HANDLES):
            if clock() > deadline:
                raise ValueError
            entry = HandleEntry()
            status = api.PssWalkSnapshot(snapshot, 2, marker, C.byref(entry), C.sizeof(entry))
            if status == 259:
                detail['query_completed'] = True
                break
            if status:
                raise ValueError
            detail['handles_seen'] += 1
            is_canary = entry.Handle == C.c_void_p(canary_handle).value
            detail['self_canary_handle_seen'] |= is_canary
            if entry.TypeNameLength or entry.TypeName:
                detail['type_names_present'] += bool(entry.TypeNameLength and entry.TypeName)
                if is_canary:
                    detail['self_canary_type_name_present'] = bool(entry.TypeNameLength and entry.TypeName)
                try:
                    text = decode_name(entry.TypeName, entry.TypeNameLength)
                    shape, normalized = name_shape(text)
                    if shape == 'embedded_nul':
                        detail['type_embedded_nul'] += 1
                    label = next((label for label in TYPE_LABELS[:-1]
                                  if normalized is not None and normalized.casefold() == label.casefold()), 'Other')
                    detail['known_type_histogram'][label] += 1
                    if normalized is not None and normalized.casefold() == 'file':
                        key = 'type_exact_file' if shape == 'exact' else 'type_single_trailing_nul_file'
                        detail[key] += 1
                        if is_canary:
                            detail['self_canary_' + key] = True
                except (ValueError, UnicodeError):
                    detail['type_invalid_utf16_or_length'] += 1
            if entry.Flags & 2:
                detail['names_present'] += 1
                if is_canary:
                    detail['self_canary_name_present'] = True
                try:
                    text = decode_name(entry.ObjectName, entry.ObjectNameLength)
                    shape, normalized = name_shape(text)
                    if shape != 'exact':
                        detail['object_' + shape] += 1
                    if is_canary and normalized is not None and _canonical(normalized) in expected:
                        detail['self_canary_exact_path_seen' if shape == 'exact' else 'self_canary_trimmed_path_seen'] = True
                except (ValueError, UnicodeError):
                    detail['object_invalid_utf16_or_length'] += 1
        else:
            raise ValueError
        if clock() > deadline:
            raise ValueError
        reason = 'pss_selfcheck_canary_not_observed'
    except Exception:
        detail['query_completed'] = False
    finally:
        cleanup_ok = True
        try:
            if marker:
                cleanup_ok &= api.PssWalkMarkerFree(marker) == 0
        except Exception:
            cleanup_ok = False
        try:
            if snapshot:
                cleanup_ok &= api.PssFreeSnapshot(process, snapshot) == 0
        except Exception:
            cleanup_ok = False
        detail['cleanup_succeeded'] = cleanup_ok
    if not detail['cleanup_succeeded']:
        return _report('未验证', 'pss_selfcheck_cleanup_unavailable', detail)
    if not detail['query_completed']:
        return _report('未验证', reason, detail)
    observed = (detail['self_canary_handle_seen']
                and (detail['self_canary_type_exact_file'] or detail['self_canary_type_single_trailing_nul_file'])
                and (detail['self_canary_exact_path_seen'] or detail['self_canary_trimmed_path_seen']))
    return _report('通过' if observed else '失败',
                   'pss_selfcheck_canary_file_metadata_observed' if observed else 'pss_selfcheck_canary_not_observed', detail)


def worker(fixture_dir, *, platform_check=_windows_x64, api_factory=SnapshotAPI):
    if not platform_check():
        return _report('未验证', 'pss_selfcheck_windows_x64_required')
    reason = 'pss_selfcheck_worker_unavailable'
    try:
        import msvcrt
        api = api_factory()
        reason = 'pss_selfcheck_fixture_unavailable'
        with tempfile.NamedTemporaryFile(mode='w+b', prefix='chatlens-pss-canary-',
                                         suffix='.synthetic', dir=fixture_dir) as fixture:
            result = observe_self(api, str(Path(fixture.name).resolve()),
                                  msvcrt.get_osfhandle(fixture.fileno()))
        result['diagnostics']['fixture_closed'] = True
        return result
    except Exception:
        return _report('未验证', reason)


def _validated_mapped_detail(value):
    detail = value['diagnostics']
    if (not isinstance(detail, dict) or set(detail) != {*MAPPED_COUNTS, *MAPPED_BOOLEANS}
            or type(detail['mapped_regions_created']) is not int or not 0 <= detail['mapped_regions_created'] <= 1
            or type(detail['mapped_name_characters']) is not int or not 0 <= detail['mapped_name_characters'] <= MAPPED_NAME_CAPACITY
            or any(type(detail[key]) is not bool for key in MAPPED_BOOLEANS)):
        raise ValueError
    if value['status'] in ('通过', '失败'):
        if (not all(detail[key] for key in ('native_api_attempted', 'fixture_created', 'fixture_closed',
                                            'map_created', 'map_closed', 'cleanup_succeeded'))
                or detail['mapped_regions_created'] != 1 or detail['mapped_query_truncated']):
            raise ValueError
        if value['status'] == '通过':
            if (not detail['mapped_query_completed'] or not detail['self_canary_exact_path_seen']
                    or not 0 < detail['mapped_name_characters'] < MAPPED_NAME_CAPACITY
                    or value['reason'] != 'mapped_selfcheck_canary_file_metadata_observed'):
                raise ValueError
        elif value['reason'] == 'mapped_selfcheck_path_unavailable':
            if detail['mapped_name_characters'] != 0 or detail['mapped_query_completed'] or detail['self_canary_exact_path_seen']:
                raise ValueError
        elif value['reason'] == 'mapped_selfcheck_path_mismatch':
            if (not detail['mapped_query_completed'] or detail['self_canary_exact_path_seen']
                    or not 0 < detail['mapped_name_characters'] < MAPPED_NAME_CAPACITY):
                raise ValueError
        else:
            raise ValueError
    return value


def _validated_report(raw, *, kind=KIND):
    if not isinstance(raw, bytes) or len(raw) > 16384:
        raise ValueError
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'tool_version', 'kind', 'status', 'reason', 'diagnostics'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['tool_version'] != TOOL_VERSION or value['kind'] != kind
            or value['status'] not in ('通过', '失败', '未验证')
            or not isinstance(value['reason'], str)
            or value['reason'] not in (MAPPED_REASONS if kind == MAPPED_KIND else REASONS)):
        raise ValueError
    if kind == MAPPED_KIND:
        return _validated_mapped_detail(value)
    detail = value['diagnostics']
    if (not isinstance(detail, dict) or set(detail) != {*COUNTS, *BOOLEANS, 'known_type_histogram'}
            or any(type(detail[key]) is not int or not 0 <= detail[key] <= MAX_HANDLES for key in COUNTS)
            or any(type(detail[key]) is not bool for key in BOOLEANS)):
        raise ValueError
    histogram = detail['known_type_histogram']
    if (not isinstance(histogram, dict) or set(histogram) != set(TYPE_LABELS)
            or any(type(count) is not int or not 0 <= count <= MAX_HANDLES for count in histogram.values())):
        raise ValueError
    observed = (detail['self_canary_handle_seen']
                and (detail['self_canary_type_exact_file'] or detail['self_canary_type_single_trailing_nul_file'])
                and (detail['self_canary_exact_path_seen'] or detail['self_canary_trimmed_path_seen']))
    if value['status'] in ('通过', '失败'):
        if not all(detail[key] for key in ('native_api_attempted', 'query_completed', 'cleanup_succeeded', 'fixture_created', 'fixture_closed')):
            raise ValueError
        if ((value['status'] == '通过') != observed
                or value['reason'] != ('pss_selfcheck_canary_file_metadata_observed' if observed else 'pss_selfcheck_canary_not_observed')):
            raise ValueError
    return value


def run_selfcheck(*, runner=subprocess.run, platform_check=_windows_x64, mode='pss'):
    if mode not in ('pss', 'mapped-path'):
        return _report('未验证', 'pss_selfcheck_invalid_arguments')
    kind = MAPPED_KIND if mode == 'mapped-path' else KIND
    prefix = 'mapped_selfcheck_' if kind == MAPPED_KIND else 'pss_selfcheck_'
    if not platform_check():
        return _report('未验证', prefix + 'windows_x64_required', kind=kind)
    reason = prefix + 'worker_unavailable'
    try:
        with tempfile.TemporaryDirectory(prefix='chatlens-pss-selfcheck-') as owned_dir:
            arguments = [sys.executable, '-I', '-S', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
                         '--worker-mapped' if kind == MAPPED_KIND else '--worker', owned_dir]
            options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.PIPE,
                       'stderr': subprocess.DEVNULL, 'timeout': 6, 'check': False, 'shell': False}
            if os.name == 'nt':
                options['creationflags'] = subprocess.CREATE_NO_WINDOW
            completed = runner(arguments, **options)
            if completed.returncode != 0:
                raise ValueError
            reason = prefix + 'worker_output_invalid'
            result = _validated_report(completed.stdout, kind=kind)
        result['diagnostics']['parent_fixture_removed'] = True
        return result
    except subprocess.TimeoutExpired:
        return _report('未验证', prefix + 'worker_timeout', kind=kind)
    except (Exception, KeyboardInterrupt):
        return _report('未验证', reason, kind=kind)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        result = run_selfcheck()
    elif arguments == ['--mapped-path']:
        result = run_selfcheck(mode='mapped-path')
    elif len(arguments) == 2 and arguments[0] == '--worker':
        result = worker(arguments[1])
    elif len(arguments) == 2 and arguments[0] == '--worker-mapped':
        result = mapped_worker(arguments[1])
    else:
        result = _report('未验证', 'pss_selfcheck_invalid_arguments')
    # ASCII JSON framing survives older Windows shells' native-output codecs;
    # JSON readers still recover the fixed Chinese status strings losslessly.
    print(json.dumps(result, ensure_ascii=True, separators=(',', ':')))


if __name__ == '__main__':
    main()
