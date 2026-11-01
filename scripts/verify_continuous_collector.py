#!/usr/bin/env python3
"""Synthetic encrypted-source stress/restart check; never opens real WeChat."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

MODULES = Path(__file__).resolve().parent / 'windows-proof'
sys.path.insert(0, str(MODULES))
from continuous_reader import ContinuousReader
from continuous_store import CollectorStore

BACKGROUND = {'probe_succeeded': True, 'running': True, 'frontmost': False, 'screen_locked': False}


def verify():
    import sqlcipher3.dbapi2 as cipher
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='chatlens-synthetic-continuous-') as temporary:
        base = Path(temporary).resolve()
        root = base / 'synthetic-source'
        (root / 'message').mkdir(parents=True)
        state = base / 'synthetic-private-store'
        state.mkdir(mode=0o700)
        peers = ['synthetic_peer', 'synthetic_group@chatroom']
        config = {'conversations': [{'alias': alias, 'chat_id': chat,
                  'table': 'Msg_' + hashlib.md5(chat.encode()).hexdigest()} for alias, chat in
                  zip(('private_test', 'group_test'), peers)]}
        keys, writers = {}, []
        for index in (0, 1):
            relative = 'message/message_%d.db' % index
            keys[relative] = ('13' if index == 0 else '37') * 32
            con = cipher.connect(str(root / relative))
            con.execute('PRAGMA key="x\'%s\'"' % keys[relative])
            con.execute('PRAGMA cipher_compatibility=4')
            con.execute('CREATE TABLE Name2Id(user_name TEXT)')
            con.execute("INSERT INTO Name2Id(rowid,user_name) VALUES(1,'synthetic_peer')")
            for row in config['conversations']:
                con.execute('CREATE TABLE "%s" (local_id INTEGER PRIMARY KEY,local_type INTEGER,create_time INTEGER,'
                            'real_sender_id INTEGER,message_content BLOB,WCDB_CT_message_content INTEGER)' % row['table'])
                con.execute('INSERT INTO "%s" VALUES(1,1,1900000000,1,?,0)' % row['table'], ('SYNTHETIC_OLD_MUST_NOT_READ',))
            con.execute('CREATE TABLE Msg_unselected_private(local_id INTEGER, message_content TEXT)')
            con.execute("INSERT INTO Msg_unselected_private VALUES(1,'SYNTHETIC_UNSELECTED_MUST_NOT_READ')")
            con.commit()
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA wal_autocheckpoint=0')
            writers.append(con)

        class GuardedDriver:
            def connect(self, *args, **kwargs):
                con = cipher.connect(*args, **kwargs)
                def authorize(action, table, column, *_):
                    if action == cipher.SQLITE_READ and table == 'Msg_unselected_private':
                        raise AssertionError('unselected source queried')
                    return cipher.SQLITE_OK
                def text(raw):
                    if raw in (b'SYNTHETIC_OLD_MUST_NOT_READ', b'SYNTHETIC_UNSELECTED_MUST_NOT_READ'):
                        raise AssertionError('old or unselected source body read')
                    return raw.decode()
                con.set_authorizer(authorize)
                con.text_factory = text
                return con

        expected = set()
        def send(first, last):
            for number in range(first, last + 1):
                row = config['conversations'][number % 2]
                shard = (number // 2) % 2
                # Every pair includes identical bodies with separate physical IDs.
                body = 'SYNTHETIC_BODY_%06d' % (number // 4)
                value = peers[0] + ':\n' + body if row['alias'] == 'group_test' else body
                writers[shard].execute('INSERT INTO "%s" VALUES(?,1,?,1,?,0)' % row['table'],
                                       (number + 1, 1900000000 + number, value))
                expected.add((row['alias'], number + 1, body))
            for writer in writers:
                writer.commit()

        binding = {'schema_version': 1, 'scope': 'explicit_synthetic_two_conversations'}
        polls = 0
        try:
            with CollectorStore(state, binding) as store:
                with ContinuousReader(GuardedDriver(), root, keys, config, store, lambda: BACKGROUND) as reader:
                    assert not reader.resumed
                    assert store.stats()['total_messages'] == 0
                    send(1, 5000)
                    while store.stats()['total_messages'] < 5000:
                        assert reader.poll()['new_messages'] > 0
                        polls += 1
                    assert reader.poll()['new_messages'] == 0
            # The collector is actually closed before this second source batch.
            send(5001, 10000)
            with CollectorStore(state, binding) as store:
                restored = store.stats()['total_messages']
                assert restored == 5000
                with ContinuousReader(GuardedDriver(), root, keys, config, store, lambda: BACKGROUND) as reader:
                    assert reader.resumed
                    caught = 0
                    while store.stats()['total_messages'] < 10000:
                        result = reader.poll()
                        assert result['duplicates'] == 0 and result['new_messages'] > 0
                        caught += result['new_messages']
                        polls += 1
                    assert caught == 5000
                    assert reader.poll()['new_messages'] == 0
            with CollectorStore(state, binding) as store:
                with ContinuousReader(GuardedDriver(), root, keys, config, store, lambda: BACKGROUND) as reader:
                    replay = reader.poll()
                    assert replay['new_messages'] == replay['duplicates'] == 0
                    assert replay['total_messages'] == 10000
            destination = sqlite3.connect((state / 'collector.sqlite3').as_uri() + '?mode=ro', uri=True)
            try:
                actual = set(destination.execute('SELECT alias,source_local_id,body FROM messages'))
                assert actual == expected
                assert destination.execute('PRAGMA quick_check').fetchone() == ('ok',)
            finally:
                destination.close()
        finally:
            for writer in writers:
                writer.close()
    return {'schema_version': 1, 'evidence_kind': 'explicit_synthetic_sqlcipher_sources_and_private_sqlite_destination',
            'generated_at_utc': datetime.now(timezone.utc).isoformat(), 'status': '通过',
            'controlled_events': 10000, 'first_run_committed': 5000, 'restored_on_restart': restored,
            'caught_up_after_closed_collector': caught, 'replayed_committed_events': 0,
            'all_body_sender_route_rows_checked': 'body_and_conversation_and_source_local_id_exact; sender resolved by synthetic mapping',
            'identical_body_distinct_source_ids_preserved': True, 'old_and_unselected_body_reads': 0,
            'empty_restart_new_events': 0, 'destination_quick_check': 'ok', 'bounded_nonempty_polls': polls,
            'elapsed_seconds': round(time.monotonic() - started, 3),
            'real_windows_execution_status': '未验证', 'real_wechat_continuous_recovery_status': '未验证',
            'boundary': 'Synthetic encrypted DB/WAL and actual local persistence only; not real WeChat, long-duration stability or Windows API validation.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('existing_evidence_preserved')
    result = verify()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
