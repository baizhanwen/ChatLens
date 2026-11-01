"""Owned Windows onboarding worker. Private discovery stays in a local pipe.

Discovery reads process/file-mapping metadata only. Key initialization is a
separate, explicitly requested command using the existing bounded reader.
Nothing here sends messages, uploads data, injects code, or changes privileges.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import ntpath
import os
from pathlib import Path
import re
import secrets
import sys
import time

from collector_private import ensure_private_dir, read_private_json, write_private_json_new, validate_local_path
from windows_keyproof_platform import FILETIME, PlatformError, _api, _path, path_is_local

REQUIRED_DATABASES = ('message/message_0.db', 'message/message_1.db', 'contact/contact.db')


class OnboardingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def mapped_roots(paths, devices):
    """Resolve only known database path shapes; unresolved evidence rejects."""
    from windows_account_probe import _database_path_shape
    roots = set()
    for path in paths:
        if not isinstance(path, str) or '\0' in path or len(path) > 32768:
            raise OnboardingError('windows_onboarding_mapping_unavailable')
        value = ntpath.normcase(path.replace('/', '\\'))
        if value.startswith('\\\\?\\'):
            value = value[4:]
        if value.startswith('\\device\\'):
            for device, drive in devices.items():
                if value.startswith(device.casefold() + '\\'):
                    value = drive + value[len(device):]
                    break
        match = _database_path_shape(value)
        if match:
            root = ntpath.normcase(ntpath.normpath(match[1]))
            if not re.fullmatch(r'[a-z]:\\[^\x00:]*\\db_storage', root):
                raise OnboardingError('windows_onboarding_mapping_unavailable')
            roots.add(root)
    return roots


def discover_executable(*, native=None, api=None, clock=time.monotonic, deadline=None):
    from windows_process_detection import _DetectionAPI, process_entries, detect_wechat_process
    native, api = native or _api(), api or _DetectionAPI()
    paths = set()
    for row in process_entries(api, native, clock=clock, deadline=deadline):
        handle = native.OpenProcess(0x1000, False, row['pid'])
        if not handle:
            raise OnboardingError('windows_onboarding_process_unavailable')
        try:
            image, size = C.create_unicode_buffer(32768), C.c_uint32(32768)
            if not native.QueryFullProcessImageNameW(handle, 0, image, C.byref(size)) or not 0 < size.value < len(image):
                raise OnboardingError('windows_onboarding_process_unavailable')
            value = _path(image.value)
            if ntpath.basename(value).casefold() not in ('weixin.exe', 'wechat.exe') or not path_is_local(value):
                raise OnboardingError('windows_onboarding_process_unavailable')
            paths.add(ntpath.normcase(value))
        finally:
            if not native.CloseHandle(handle):
                raise OnboardingError('windows_onboarding_process_unavailable')
    if not paths:
        return {'state': 'waiting_for_wechat', 'reason': 'windows_process_not_running', 'process_running': False}
    if len(paths) != 1:
        return {'state': 'ambiguous', 'reason': 'windows_onboarding_account_ambiguous', 'process_running': True}
    executable = next(iter(paths))
    result = detect_wechat_process(executable, api=api, native=native)
    if result.get('state') != 'ready':
        reason = result.get('reason')
        return {'state': 'ambiguous' if result.get('state') == 'ambiguous' else 'waiting_for_wechat',
                'reason': 'windows_onboarding_account_ambiguous' if result.get('state') == 'ambiguous'
                          else reason if reason in ('windows_process_version_unsupported', 'windows_process_architecture_unsupported')
                          else 'windows_onboarding_process_unavailable', 'process_running': True}
    return {**result, 'exe_path': executable}


def discover_direct(*, clock=time.monotonic):
    """Two complete same-tree passes must name exactly one current account."""
    from windows_process_detection import verified_process_family
    from windows_mapped_files import MappedFilesAPI, mapped_paths
    deadline = clock() + 3
    def check():
        if clock() > deadline:
            raise OnboardingError('windows_onboarding_discovery_timeout')
    candidate = discover_executable(clock=clock, deadline=deadline)
    if candidate['state'] != 'ready':
        return candidate
    executable, pid, birth = candidate['exe_path'], candidate['pid'], candidate['creation_time']
    family = verified_process_family(executable, pid, birth, clock=clock, deadline=deadline)
    native, api = _api(), MappedFilesAPI()
    devices = {}
    mask = api.GetLogicalDrives()
    for index in range(26):
        check()
        if mask & (1 << index):
            drive, target = chr(65 + index) + ':', C.create_unicode_buffer(32768)
            if api.QueryDosDeviceW(drive, target, len(target)):
                devices[target.value.casefold()] = drive.casefold()
    roots, queried = [], 0
    for _ in range(2):
        paths = []
        for member in family:
            check()
            handle = native.OpenProcess(0x400, False, member['pid'])
            if not handle:
                raise OnboardingError('windows_onboarding_process_unavailable')
            try:
                def verify():
                    check()
                    image, size = C.create_unicode_buffer(32768), C.c_uint32(32768)
                    stamps = [FILETIME() for _ in range(4)]
                    if (not native.QueryFullProcessImageNameW(handle, 0, image, C.byref(size))
                            or not 0 < size.value < len(image)
                            or ntpath.normcase(image.value) != ntpath.normcase(executable)
                            or not native.GetProcessTimes(handle, *(C.byref(item) for item in stamps))
                            or stamps[0].low | stamps[0].high << 32 != member['creation_time']):
                        raise OnboardingError('windows_onboarding_process_changed')
                verify()
                diagnostics = {}
                paths.extend(mapped_paths(api, handle, diagnostics=diagnostics, deadline=deadline,
                                          max_regions=32768 - queried, clock=clock))
                queried += diagnostics.get('mapped_regions_queried', 0)
                if diagnostics.get('query_completed') is not True or queried > 32768:
                    raise OnboardingError('windows_onboarding_mapping_unavailable')
                verify()
            finally:
                if not native.CloseHandle(handle):
                    raise OnboardingError('windows_onboarding_process_unavailable')
        roots.append(mapped_roots(paths, devices))
    check()
    if verified_process_family(executable, pid, birth, clock=clock, deadline=deadline) != family:
        raise OnboardingError('windows_onboarding_process_changed')
    if any(len(value) > 1 for value in roots) or roots[0] != roots[1]:
        return {'state': 'ambiguous', 'reason': 'windows_onboarding_account_ambiguous', 'process_running': True}
    if len(roots[0]) != 1:
        return {'state': 'waiting_for_wechat', 'reason': 'windows_onboarding_account_not_observed', 'process_running': True}
    root = next(iter(roots[0]))
    if not path_is_local(root, require_dir=True):
        raise OnboardingError('windows_onboarding_source_unavailable')
    return {'state': 'ready', 'reason': 'windows_onboarding_account_detected', 'pid': pid,
            'creation_time': birth, 'exe_path': executable, 'db_root': root}


def validate_candidate(candidate):
    if (not isinstance(candidate, dict) or set(candidate) != {'state', 'reason', 'pid', 'creation_time', 'exe_path', 'db_root'}
            or candidate['state'] != 'ready' or candidate['reason'] != 'windows_onboarding_account_detected'
            or type(candidate['pid']) is not int or not 0 < candidate['pid'] <= 0xffffffff
            or type(candidate['creation_time']) is not int or not 0 < candidate['creation_time'] < 2**64):
        raise OnboardingError('windows_onboarding_candidate_invalid')
    try:
        executable, root = _path(candidate['exe_path']), _path(candidate['db_root'])
    except PlatformError:
        raise OnboardingError('windows_onboarding_candidate_invalid') from None
    if (ntpath.basename(executable).casefold() not in ('weixin.exe', 'wechat.exe')
            or ntpath.basename(root).casefold() != 'db_storage'):
        raise OnboardingError('windows_onboarding_candidate_invalid')
    return candidate


def publish_json_once(path, document):
    """Publish only complete JSON; cancelling a worker cannot poison retries."""
    path = Path(path)
    temporary = path.with_name('.reader-init-' + secrets.token_hex(12) + '.json')
    try:
        write_private_json_new(temporary, document)
        if path.exists() or path.is_symlink():
            raise OnboardingError('windows_onboarding_existing_configuration_invalid')
        if os.name == 'nt':
            # Windows rename is atomic and refuses an existing destination.
            os.rename(temporary, path)
        else:
            # Explicit POSIX test path: link is create-only as well. Remove the
            # private temporary immediately so the final file has one link.
            os.link(temporary, path)
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def initialize_direct(candidate, data_dir, *, progress=lambda phase: None):
    """Explicit local scan, bounded 90s/2GiB; authenticate all three pages."""
    import windows_keyproof_core as proof
    import windows_keyproof_platform as platform
    from windows_selected_runner import fixed_shards, selected_keys
    from windows_account_probe import probe_direct
    from collector_service import validate_scope
    validate_candidate(candidate)
    root, executable = Path(candidate['db_root']), Path(candidate['exe_path'])
    def binding():
        result = probe_direct(candidate['pid'], candidate['creation_time'], str(executable), str(root))
        if result.get('state') != 'matched':
            raise OnboardingError('windows_onboarding_account_changed')
    progress('detecting_account')
    binding()
    try:
        fixed_shards(root)
    except Exception as error:
        if getattr(error, 'code', None) in ('expected_two_message_shards_changed', 'selected_message_directory_unsafe'):
            raise OnboardingError('windows_onboarding_database_layout_unsupported') from None
        raise
    progress('validating_sources')
    pages = {}
    for relative in REQUIRED_DATABASES:
        if not (root / relative).is_file():
            raise OnboardingError('windows_onboarding_database_layout_unsupported')
        try:
            pages[relative] = platform.read_page(root / relative)
        except PlatformError as error:
            if error.code == 'database_first_page_unavailable':
                raise OnboardingError('windows_onboarding_database_layout_unsupported') from None
            raise
    data_dir = ensure_private_dir(Path(data_dir))
    private = ensure_private_dir(data_dir / 'private', create=True)
    keys_path, scope_path = private / 'keys.json', private / 'scope.json'
    keys = read_private_json(keys_path) if keys_path.exists() or keys_path.is_symlink() else None
    if keys is not None:
        if (not isinstance(keys, dict) or set(keys.get('keys', {})) != set(REQUIRED_DATABASES)
                or os.path.normcase(keys.get('db_root', '')) != os.path.normcase(str(root))
                or os.path.normcase(keys.get('exe_path', '')) != os.path.normcase(str(executable))):
            raise OnboardingError('windows_onboarding_existing_configuration_invalid')
        try:
            values = {relative: bytes.fromhex(keys['keys'][relative]['enc_key']) for relative in REQUIRED_DATABASES}
        except (KeyError, TypeError, ValueError):
            raise OnboardingError('windows_onboarding_existing_configuration_invalid') from None
        if not all(proof.page_authenticates(values[relative], page) for relative, page in pages.items()):
            raise OnboardingError('windows_onboarding_existing_configuration_invalid')
    else:
        stats = {}
        with platform.WindowsProcess(candidate['pid'], str(executable), candidate['creation_time']) as source:
            budget = proof.Budget(source, seconds=90, max_bytes=2 * 1024**3)
            values = selected_keys(source, pages, b'', budget, stats, progress=progress)
            budget.check()
        keys = {'schema_version': 1, 'db_root': str(root), 'exe_path': str(executable),
                'keys': {relative: {'enc_key': key.hex()} for relative, key in values.items()}}
    progress('validating_keys')
    # Authenticate current pages again; neither an old readable database nor a
    # stale discovery result alone authorizes this commit.
    if not all(proof.page_authenticates(values[relative], platform.read_page(root / relative))
               for relative in REQUIRED_DATABASES):
        raise OnboardingError('windows_onboarding_key_validation_failed')
    binding()
    scope = read_private_json(scope_path) if scope_path.exists() or scope_path.is_symlink() else {
        'schema_version': 1, 'scope_kind': 'unselected_reader', 'session_id': secrets.token_hex(16),
        'db_root': str(root), 'exe_path': str(executable), 'config': {'conversations': []}, 'test_tokens': {}}
    validate_scope(scope, keys)
    progress('preparing_workspace')
    if not keys_path.exists():
        publish_json_once(keys_path, keys)
    if not scope_path.exists():
        publish_json_once(scope_path, scope)
    ensure_private_dir(private / 'store', create=True)
    return {'state': 'ready', 'reason': 'windows_onboarding_reader_initialized'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owned-command', choices=('discover', 'initialize'), required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        return 2
    try:
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise OnboardingError('windows_onboarding_request_invalid')
        request = json.loads(raw or b'{}')
        if not isinstance(request, dict):
            raise OnboardingError('windows_onboarding_request_invalid')
        def emit(value):
            print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)
        if args.owned_command == 'discover':
            if request:
                raise OnboardingError('windows_onboarding_request_invalid')
            emit({'kind': 'result', 'value': discover_direct()})
        else:
            if set(request) != {'candidate', 'data_dir'}:
                raise OnboardingError('windows_onboarding_request_invalid')
            result = initialize_direct(request['candidate'], request['data_dir'],
                                       progress=lambda phase: emit({'kind': 'progress', 'phase': phase}))
            emit({'kind': 'result', 'value': result})
        return 0
    except Exception as error:
        code = getattr(error, 'code', None) or str(error)
        code = {'scan_time_limit': 'windows_onboarding_timeout', 'region_time_limit': 'windows_onboarding_timeout',
                'scan_byte_limit': 'windows_onboarding_read_budget_exceeded',
                'selected_database_keys_incomplete': 'windows_onboarding_keys_unavailable',
                'expected_two_message_shards_changed': 'windows_onboarding_database_layout_unsupported'}.get(code, code)
        allowed = {'windows_onboarding_mapping_unavailable', 'windows_onboarding_process_unavailable',
                   'windows_onboarding_discovery_timeout', 'windows_onboarding_process_changed',
                   'windows_onboarding_source_unavailable', 'windows_onboarding_candidate_invalid',
                   'windows_onboarding_account_changed', 'windows_onboarding_existing_configuration_invalid',
                   'windows_onboarding_key_validation_failed', 'windows_onboarding_timeout',
                   'windows_onboarding_read_budget_exceeded', 'windows_onboarding_keys_unavailable',
                   'windows_onboarding_database_layout_unsupported'}
        print(json.dumps({'kind': 'error', 'reason': code if code in allowed else 'windows_onboarding_operation_failed'}), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
