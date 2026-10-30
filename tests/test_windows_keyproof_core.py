"""Synthetic key/page/memory fixtures only; never access WeChat data."""
import hashlib
import hmac
import importlib.util
from pathlib import Path
import struct
import unittest

SPEC = importlib.util.spec_from_file_location('keyproof', Path(__file__).resolve().parents[1] / 'scripts/windows-proof/windows_keyproof_core.py')
k = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(k)

KEY = bytes(range(32))
SALT = bytes(range(80, 96))


def page(key=KEY, salt=SALT):
    body = bytes((i * 13) % 256 for i in range(4016))
    mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(v ^ 0x3a for v in salt), 2, 32)
    tag = hmac.new(mac_key, body + struct.pack('<I', 1), hashlib.sha512).digest()
    return salt + body + tag


def blob(key=KEY, salt=SALT):
    raw = b"x'" + key.hex().encode() + salt.hex().encode() + b"'"
    return bytes(v ^ k.MASK[i % len(k.MASK)] for i, v in enumerate(raw))


class Arena:
    def __init__(self, marker_image=False, marker_offset=0x80):
        self.base = 0x10000000
        self.data = bytearray(k.CHUNK + 2048)
        self.marker_offset = marker_offset
        self.data[marker_offset:marker_offset + len(k.MARKER)] = k.MARKER
        node = 0x200
        struct.pack_into('<QQ', self.data, node + 0x10, self.base + marker_offset, len(k.MARKER))
        struct.pack_into('<Q', self.data, node + 0x28, self.base + 0x400)
        struct.pack_into('<QQ', self.data, 0x400 + 0x88 + 8, self.base + 0x600, len(blob()))
        self.data[0x600:0x600 + len(blob())] = blob()
        self.calls = []
        self.marker_image = marker_image

    def regions(self, include_image=False):
        if self.marker_image and not include_image:
            # Image marker at 0x80 is skipped, while heap still contains its address.
            return iter([(self.base + 0x100, len(self.data) - 0x100)])
        return iter([(self.base, len(self.data))])

    def read(self, address, size):
        self.calls.append((address, size))
        offset = address - self.base
        if offset < 0 or offset + size > len(self.data):
            return b''
        return bytes(self.data[offset:offset + size])


class KeyproofTests(unittest.TestCase):
    def run_scan(self, arena, **limits):
        report = k.public_report()
        budget = k.Budget(arena, **limits)
        return k.scan(arena, page(), budget, report['scan_stats']), report, budget

    def test_full_page_authentication(self):
        self.assertTrue(k.page_authenticates(KEY, page()))
        self.assertFalse(k.page_authenticates(bytes(32), page()))
        damaged = bytearray(page()); damaged[100] ^= 1
        self.assertFalse(k.page_authenticates(KEY, bytes(damaged)))
        damaged = bytearray(page()); damaged[-1] ^= 1
        self.assertFalse(k.page_authenticates(KEY, bytes(damaged)))
        self.assertFalse(k.page_authenticates(KEY, page()[:-1]))
        self.assertFalse(k.page_authenticates(KEY, b'SQLite format 3\0' + page()[16:]))

    def test_authenticates_independently_created_sqlcipher4_fixture(self):
        try:
            import sqlcipher3.dbapi2 as cipher
        except ImportError:
            self.skipTest('isolated SQLCipher test driver required; never a Windows runtime dependency')
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'synthetic-only.db'
            connection = cipher.connect(str(path))
            try:
                connection.execute('PRAGMA key = "x\'%s\'"' % KEY.hex())
                connection.execute('PRAGMA cipher_compatibility = 4')
                connection.execute('PRAGMA cipher_page_size = 4096')
                connection.execute('CREATE TABLE synthetic_only (value TEXT)')
                connection.execute("INSERT INTO synthetic_only VALUES ('EXPLICIT_SYNTHETIC_FIXTURE')")
                connection.commit()
            finally:
                connection.close()
            first_page = path.read_bytes()[:4096]
            self.assertTrue(k.page_authenticates(KEY, first_page))
            self.assertFalse(k.page_authenticates(bytes(32), first_page))

    def test_candidate_is_salt_bound_and_exact_literal(self):
        self.assertEqual(list(k.blob_candidates(blob(), SALT)), [KEY])
        self.assertEqual(list(k.blob_candidates(blob(salt=bytes(16)), SALT)), [])
        self.assertEqual(list(k.blob_candidates(bytes(1025), SALT)), [])
        # No generic raw-key fallback or sliding search within longer hex literals.
        raw = b"x'" + KEY.hex().encode() + SALT.hex().encode() + b'00' + b"'"
        encrypted = bytes(v ^ k.MASK[i % len(k.MASK)] for i, v in enumerate(raw))
        self.assertEqual(list(k.blob_candidates(encrypted, SALT)), [])

    def test_complete_lookup_and_image_marker(self):
        for image in (False, True):
            found, report, budget = self.run_scan(Arena(marker_image=image))
            self.assertEqual(found, KEY)
            self.assertEqual(report['scan_stats']['candidate_count'], 1)
            self.assertEqual(report['chat_messages_read'], 0)
            self.assertGreater(budget.calls, 0)
            self.assertLessEqual(max(size for _, size in budget.source.calls), k.CHUNK)

    def test_marker_straddles_chunk_boundary(self):
        found, report, _ = self.run_scan(Arena(marker_offset=k.CHUNK - 9))
        self.assertEqual(found, KEY)
        self.assertEqual(report['scan_stats']['marker_count'], 1)

    def test_pair_straddles_chunk_boundary(self):
        a = Arena()
        node = k.CHUNK - 0x18
        a.data[node:node+0x50] = a.data[0x200:0x250]
        a.data[0x200:0x250] = bytes(0x50)
        found, _, _ = self.run_scan(a)
        self.assertEqual(found, KEY)

    def test_no_marker_no_candidates(self):
        a = Arena(); a.data[a.marker_offset:a.marker_offset+len(k.MARKER)] = bytes(len(k.MARKER))
        found, report, _ = self.run_scan(a)
        self.assertIsNone(found)
        self.assertEqual(report['scan_stats']['marker_count'], 0)

    def test_bad_pointer_or_hmac_never_returns_key(self):
        a = Arena(); struct.pack_into('<Q', a.data, 0x228, k.MAX_ADDRESS)
        self.assertIsNone(self.run_scan(a)[0])
        a = Arena(); a.data[0x600:0x600+len(blob())] = blob(key=bytes(32))
        self.assertIsNone(self.run_scan(a)[0])

    def test_byte_budget_counts_failed_reads(self):
        class Empty:
            def read(self, address, size): return b''
        budget = k.Budget(Empty(), max_bytes=64)
        self.assertEqual(budget.read(0x10000, 64), b'')
        with self.assertRaisesRegex(k.ProofError, '^scan_byte_limit$'):
            budget.read(0x10000, 1)
        self.assertEqual(budget.requested, 64)

    def test_time_budget(self):
        now = [0]
        budget = k.Budget(Arena(), seconds=1, clock=lambda:now[0])
        now[0] = 2
        with self.assertRaisesRegex(k.ProofError, '^scan_time_limit$'):
            budget.read(0x10000, 1)

    def test_partial_reads_do_not_join_unread_gap(self):
        a = Arena()
        def partial(address, size):
            return b'A' if address == a.base else b'B'
        a.read = partial
        budget = k.Budget(a)
        got = list(k.chunks([(a.base, k.CHUNK + 2)], budget, 10))
        self.assertEqual(got, [(a.base, b'A'), (a.base+k.CHUNK, b'B')])

    def test_cli_success_saves_key_only_in_private_output(self):
        import contextlib
        import io
        import json
        import tempfile
        import types
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = base / 'previous' / 'private' / 'config.json'
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({'schema_version': 1, 'selected_pid': 42,
                                       'db_root': str(base / 'db_storage'), 'exe_path': str(base / 'Weixin.exe')}))
            out = base / 'fresh'
            fake = types.ModuleType('windows_keyproof_platform')
            class Source(Arena):
                def __init__(self, pid, exe, creation):
                    super().__init__()
                    self.creation_time = creation
                def __enter__(self): return self
                def __exit__(self, *a): pass
            fake.WindowsProcess = Source
            fake.PlatformError = type('PlatformError', (Exception,), {})
            fake.path_is_local = lambda *a, **kw: True
            fake.read_page = lambda *a: page()
            fake.private_dir = lambda path: path.mkdir()
            fake.private_json = lambda path, obj: path.write_text(json.dumps(obj))
            args = ['keyproof', '--config', str(cfg), '--creation-time', '123',
                    '--client-version', '4.1.15.56', '--output', str(out)]
            stdout = io.StringIO()
            with patch.dict(k.sys.modules, {'windows_keyproof_platform': fake}), patch.object(k.sys, 'argv', args), patch.object(k.sys, 'platform', 'win32'), contextlib.redirect_stdout(stdout):
                self.assertEqual(k.main(), 0)
            private = json.loads((out / 'private' / 'validated-message0-key.json').read_text())
            public_text = (out / 'result-to-send.json').read_text()
            public = json.loads(public_text)
            self.assertEqual(private['key_hex'], KEY.hex())
            self.assertEqual(public['key_verification_status'], '通过')
            self.assertEqual(public['business_read_status'], '未验证')
            self.assertEqual(public['chat_messages_read'], 0)
            self.assertTrue(public['operations']['process_memory_read'])
            for text in (public_text, stdout.getvalue()):
                self.assertNotIn(KEY.hex(), text)
                self.assertNotIn(SALT.hex(), text)
                self.assertNotIn(str(base), text)
            # A saved private key without a saved public report is not a successful run.
            original_open = Path.open
            def deny_report(path, *a, **kw):
                if path.name == 'result-to-send.json':
                    raise PermissionError('synthetic-private-path-must-not-be-printed')
                return original_open(path, *a, **kw)
            args[-1] = str(base / 'report-denied')
            stdout = io.StringIO()
            with patch.dict(k.sys.modules, {'windows_keyproof_platform': fake}), patch.object(k.sys, 'argv', args), patch.object(k.sys, 'platform', 'win32'), patch.object(Path, 'open', deny_report), contextlib.redirect_stdout(stdout):
                self.assertEqual(k.main(), 2)
            self.assertFalse((base / 'report-denied' / 'result-to-send.json').exists())
            self.assertNotIn('synthetic-private-path', stdout.getvalue())

    def test_existing_output_is_never_modified(self):
        import contextlib
        import io
        import json
        import tempfile
        import types
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = base / 'private' / 'config.json'
            cfg.parent.mkdir()
            cfg.write_text(json.dumps({'schema_version': 1, 'selected_pid': 42,
                                       'db_root': str(base / 'db_storage'), 'exe_path': str(base / 'Weixin.exe')}))
            out = base / 'existing'
            out.mkdir()
            keep = out / 'keep.txt'
            keep.write_text('existing-user-data')
            fake = types.ModuleType('windows_keyproof_platform')
            fake.WindowsProcess = object
            fake.PlatformError = type('PlatformError', (Exception,), {})
            fake.path_is_local = lambda *a, **kw: True
            fake.read_page = lambda *a: page()
            fake.private_dir = lambda *a: None
            fake.private_json = lambda *a: None
            args = ['keyproof', '--config', str(cfg), '--creation-time', '1',
                    '--client-version', '4.1.15.56', '--output', str(out)]
            with patch.dict(k.sys.modules, {'windows_keyproof_platform': fake}), patch.object(k.sys, 'argv', args), patch.object(k.sys, 'platform', 'win32'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(k.main(), 2)
            self.assertEqual(keep.read_text(), 'existing-user-data')
            self.assertEqual(sorted(x.name for x in out.iterdir()), ['keep.txt'])

    def test_report_contains_no_candidates_or_memory_addresses(self):
        found, report, _ = self.run_scan(Arena())
        import json
        text = json.dumps(report)
        self.assertNotIn(found.hex(), text)
        self.assertNotIn(SALT.hex(), text)
        self.assertNotIn(str(0x10000000), text)
        self.assertEqual(report['business_read_status'], '未验证')
        self.assertFalse(report['operations']['database_decrypted'])


if __name__ == '__main__':
    unittest.main()
