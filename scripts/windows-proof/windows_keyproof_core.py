"""Bounded Config.Cipher lookup; authenticate one encrypted first page only.

The layout is version-dependent. No database is decrypted, queried or exported.
Algorithm reference and license: THIRD-PARTY-NOTICES.txt in the delivery package.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from pathlib import Path
import re
import struct
import sys
import time
from datetime import datetime, timezone

MARKER = b'com.Tencent.WCDB.Config.Cipher'
MASK = bytes.fromhex('d2c7442458020000004889442450488b450048844c2448488944254048584c24')
MAX_ADDRESS = 0x0000800000000000
LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64})([0-9a-fA-F]{32})'")
CHUNK = 1024 * 1024
MAX_SECONDS = 90
MAX_BYTES = 2 * 1024**3


class ProofError(Exception):
    """Only static, non-sensitive error codes may be passed here."""


class Budget:
    def __init__(self, source, *, seconds=MAX_SECONDS, max_bytes=MAX_BYTES, clock=time.monotonic):
        self.source, self.clock = source, clock
        self.deadline = clock() + seconds
        self.max_bytes = max_bytes
        self.requested = self.received = self.calls = 0

    def check(self):
        if self.clock() >= self.deadline:
            raise ProofError('scan_time_limit')

    def read(self, address, size):
        self.check()
        if not 0 < size <= CHUNK or address < 0x10000 or address + size > MAX_ADDRESS:
            return b''
        if self.requested + size > self.max_bytes:
            raise ProofError('scan_byte_limit')
        self.requested += size
        self.calls += 1
        data = self.source.read(address, size)
        if not isinstance(data, bytes) or len(data) > size:
            raise ProofError('invalid_memory_read_result')
        self.received += len(data)
        return data


def chunks(regions, budget, overlap):
    for base, size in regions:
        tail = b''
        for offset in range(0, size, CHUNK):
            wanted = min(CHUNK, size - offset)
            data = budget.read(base + offset, wanted)
            # A failed/partial read must not bridge an unread address range.
            if data:
                yield base + offset - len(tail), tail + data
            tail = (tail + data)[-overlap:] if overlap and len(data) == wanted else b''


def page_authenticates(key, page):
    if len(key) != 32 or len(page) != 4096 or page[:16] == b'SQLite format 3\0':
        return False
    mac_salt = bytes(x ^ 0x3a for x in page[:16])
    mac_key = hashlib.pbkdf2_hmac('sha512', key, mac_salt, 2, 32)
    tag = hmac.new(mac_key, page[16:4032] + struct.pack('<I', 1), hashlib.sha512).digest()
    return hmac.compare_digest(tag, page[4032:])


def blob_candidates(blob, selected_salt):
    if not 0 < len(blob) <= 1024:
        return
    decoded = bytes(v ^ MASK[i % len(MASK)] for i, v in enumerate(blob))
    for match in LITERAL.finditer(decoded):
        if bytes.fromhex(match.group(2).decode('ascii')) == selected_salt:
            yield bytes.fromhex(match.group(1).decode('ascii'))


def u64(blob, offset):
    return struct.unpack_from('<Q', blob, offset)[0] if 0 <= offset <= len(blob) - 8 else 0


def scan(source, page, budget, stats):
    """Return one authenticated key, without persisting any memory/page bytes."""
    regions = []
    for region in source.regions(include_image=True):
        budget.check()
        regions.append(region)
        if len(regions) > 100000:
            raise ProofError('scan_region_limit')
    stats['marker_search_region_count'] = len(regions)
    markers = set()
    for base, data in chunks(regions, budget, len(MARKER) - 1):
        pos = data.find(MARKER)
        while pos >= 0:
            budget.check()
            markers.add(base + pos)
            if len(markers) > 128:
                raise ProofError('scan_marker_limit')
            pos = data.find(MARKER, pos + 1)
    stats['marker_count'] = len(markers)
    if not markers:
        return None
    # References live in the runtime heap; do not scan image regions twice.
    regions = []
    for region in source.regions():
        budget.check()
        regions.append(region)
        if len(regions) > 100000:
            raise ProofError('scan_region_limit')
    stats['readable_private_region_count'] = len(regions)
    patterns = [struct.pack('<QQ', address, len(MARKER)) for address in sorted(markers)]
    seen_refs, seen_configs, seen_keys = set(), set(), set()
    for base, data in chunks(regions, budget, 0x80):
        for pattern in patterns:
            budget.check()
            pos = data.find(pattern)
            while pos >= 0:
                budget.check()
                ref = base + pos
                pos = data.find(pattern, pos + 1)
                if ref in seen_refs:
                    continue
                seen_refs.add(ref)
                stats['string_reference_count'] = len(seen_refs)
                if len(seen_refs) > 4096:
                    raise ProofError('scan_reference_limit')
                node = budget.read(ref - 0x10, 0x50)
                if len(node) != 0x50 or u64(node, 0x10) not in markers or u64(node, 0x18) != len(MARKER):
                    continue
                config = u64(node, 0x28)
                if not 0x10000 <= config < MAX_ADDRESS - 0xb0 or config in seen_configs:
                    continue
                seen_configs.add(config)
                obj = budget.read(config + 0x88, 0x28)
                if len(obj) != 0x28:
                    continue
                address, size = u64(obj, 8), u64(obj, 0x10)
                if not 0 < size <= 1024 or not 0x10000 <= address < MAX_ADDRESS - size:
                    continue
                blob = budget.read(address, size)
                if len(blob) != size:
                    continue
                stats['config_blob_count'] += 1
                for key in blob_candidates(blob, page[:16]):
                    budget.check()
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    stats['candidate_count'] = len(seen_keys)
                    if len(seen_keys) > 2048:
                        raise ProofError('scan_candidate_limit')
                    if page_authenticates(key, page):
                        return key
    return None


def public_report():
    return {
        'schema_version': 1, 'stage': 'keyproof',
        'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'selected_process_config_lookup_and_message_0_first_page_hmac_only',
        'status': '未验证', 'reason': 'not_started',
        'key_verification_status': '未验证', 'business_read_status': '未验证',
        'chat_messages_read': 0, 'python_version': sys.version.split()[0],
        'target_client_version': '4.1.15.56', 'target_database': 'message/message_0.db',
        'source_process_identity_verified': False, 'first_page_read': False,
        'private_key_saved': False, 'private_acl_verified': False,
        'scan_limits': {'seconds': MAX_SECONDS, 'read_request_bytes': MAX_BYTES, 'chunk_bytes': CHUNK},
        'scan_stats': {'marker_search_region_count': 0, 'readable_private_region_count': 0, 'marker_count': 0,
                       'string_reference_count': 0, 'config_blob_count': 0, 'candidate_count': 0,
                       'read_calls': 0, 'read_request_bytes': 0, 'read_returned_bytes': 0},
        'operations': {'process_memory_read': False, 'database_decrypted': False,
                       'chat_contents_parsed': False, 'processes_changed': False,
                       'privileges_elevated': False, 'network_requested': False},
        'note': 'Process memory may contain conversations; only key structures are interpreted. No memory dump or page bytes are saved.'
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--creation-time', type=int, required=True)
    parser.add_argument('--client-version', choices=['4.1.15.56'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = public_report()
    budget = None
    owned_output = False
    report_saved = False
    try:
        from windows_keyproof_platform import (WindowsProcess, PlatformError, path_is_local,
                                               read_page, private_dir, private_json)
        if sys.platform != 'win32' or struct.calcsize('P') != 8:
            raise ProofError('windows_x64_runtime_required')
        if not path_is_local(args.config) or args.config.name != 'config.json' or args.config.parent.name != 'private':
            raise ProofError('selected_local_private_config_required')
        if args.config.stat().st_size > 16384:
            raise ProofError('config_size_limit')
        config = json.loads(args.config.read_text(encoding='utf-8-sig'))
        if config.get('schema_version') != 1 or type(config.get('selected_pid')) is not int:
            raise ProofError('invalid_setup_config')
        db_root = Path(config.get('db_root') or '')
        exe = Path(config.get('exe_path') or '')
        if not path_is_local(db_root, require_dir=True) or db_root.name.lower() != 'db_storage' or not path_is_local(exe):
            raise ProofError('invalid_selected_paths')
        database = db_root / 'message' / 'message_0.db'
        if not path_is_local(database):
            raise ProofError('selected_message_0_unavailable')
        # This is a new run directory; never overwrite user files or previous keys.
        if args.output.exists() or not path_is_local(args.output.parent, require_dir=True):
            raise ProofError('new_local_output_required')
        args.output.mkdir()
        owned_output = True
        private_dir(args.output / 'private')
        report['private_acl_verified'] = True
        page = read_page(database)
        report['first_page_read'] = True
        if len(page) != 4096 or page[:16] == b'SQLite format 3\0':
            raise ProofError('encrypted_sqlcipher4_page_required')
        with WindowsProcess(config['selected_pid'], str(exe), args.creation_time) as source:
            report['source_process_identity_verified'] = True
            budget = Budget(source)
            key = scan(source, page, budget, report['scan_stats'])
            if key is None:
                report['reason'] = 'no_authenticated_key_for_selected_page_layout_may_differ'
            else:
                private_json(args.output / 'private' / 'validated-message0-key.json', {
                    'schema_version': 1, 'kind': 'direct_sqlcipher_encryption_key',
                    'db_root': str(db_root), 'database': 'message/message_0.db',
                    'exe_path': str(exe), 'process_creation_time': source.creation_time,
                    'key_hex': key.hex(), 'salt_hex': page[:16].hex(),
                    'key_verified_by': 'sqlcipher4_first_page_hmac_sha512',
                })
                report.update(status='通过', reason='selected_first_page_key_authenticated_only',
                              key_verification_status='通过', private_key_saved=True)
    except Exception as error:
        code = str(error) if isinstance(error, ProofError) else None
        if error.__class__.__name__ == 'PlatformError':
            code = getattr(error, 'code', 'windows_api_failed')
        allowed = {
            'scan_time_limit', 'scan_byte_limit', 'scan_region_limit', 'scan_marker_limit',
            'scan_reference_limit', 'scan_candidate_limit', 'invalid_memory_read_result',
            'windows_x64_runtime_required', 'selected_local_private_config_required',
            'config_size_limit', 'invalid_setup_config', 'invalid_selected_paths',
            'selected_message_0_unavailable', 'new_local_output_required', 'encrypted_sqlcipher4_page_required'
        }
        # Platform codes are static by contract, but never pass arbitrary exception messages through.
        if code not in allowed and not (isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,80}', code)):
            code = 'keyproof_operation_failed'
        report['status'] = '待外部条件' if code.startswith('scan_') or code in ('region_time_limit', 'region_query_limit') else '失败'
        report['reason'] = code or 'keyproof_operation_failed'
    finally:
        if budget is not None:
            report['scan_stats'].update(read_calls=budget.calls, read_request_bytes=budget.requested,
                                        read_returned_bytes=budget.received)
            report['operations']['process_memory_read'] = budget.calls > 0
        # Failures before a new output directory was made must not touch any existing directory.
        if owned_output:
            try:
                with (args.output / 'result-to-send.json').open('x', encoding='utf-8') as target:
                    json.dump(report, target, ensure_ascii=False, indent=2)
                report_saved = True
                print('Finished. Send only this run\'s result-to-send.json. Keep private on this PC.')
            except OSError:
                print('Report could not be saved. Do not send private files.')
        else:
            print('Stopped before report creation. Re-run from a fresh local output folder.')
    return 0 if report['status'] == '通过' and report_saved else 2


if __name__ == '__main__':
    raise SystemExit(main())
