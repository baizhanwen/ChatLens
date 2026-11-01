"""Synthetic collector store + actual local HTTP service for browser acceptance.

IPC stays in the parent process: its launch fragment is never written as evidence.
No WeChat client, source database, key or existing private directory is accessed.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'windows-proof'))
from collector_private import ensure_private_dir, write_private_json_new
from continuous_store import CollectorStore
from web_data import CollectorWebData
from web_server import LocalWebServer, make_server


class DelayedSyntheticData(CollectorWebData):
    def messages(self, conversation, **query):
        # Keep the real readonly DB query, then delay one response so the
        # browser's cross-conversation race protection is deterministic.
        result = super().messages(conversation, **query)
        if query.get('q') == '合成慢速查询':
            time.sleep(0.8)
        return result


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    # Deliberately allow quick reuse only in this synthetic restart harness.
    LocalWebServer.allow_reuse_address = True
    (ROOT / '.build').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='synthetic-web-live-', dir=ROOT / '.build') as temporary:
        data = Path(temporary).resolve()
        ensure_private_dir(data)
        private = data / 'private'
        ensure_private_dir(private, create=True)
        directory = private / 'store'
        ensure_private_dir(directory, create=True)
        store = CollectorStore(directory, {'source': 'synthetic-web-browser-fixture'})
        cursors = {'private_test': 0, 'group_test': 0}
        epochs = {'synthetic': 'one'}
        store.initialize(cursors, epochs)
        started = now()
        stage = 'collecting'
        serial = 0

        def status(value=None):
            nonlocal stage
            stage = value or stage
            payload = {'schema_version': 1, 'stage': stage, 'reason': None,
                       'recorded_at_utc': now(), 'started_at_utc': started,
                       'resumed_from_checkpoint': True}
            target = private / 'web-status.json'
            temporary_status = private / 'web-status-next.json'
            write_private_json_new(temporary_status, payload)
            os.replace(temporary_status, target)

        def commit(alias, body, *, sender='synthetic-sender', count=1):
            nonlocal cursors, serial
            after = dict(cursors)
            events = []
            for _ in range(count):
                serial += 1
                after[alias] += 1
                events.append({'source_key': hashlib.sha256(('synthetic-' + str(serial)).encode()).hexdigest(),
                               'alias': alias, 'source_local_id': after[alias],
                               'sent_at': int(datetime.now(timezone.utc).timestamp()),
                               'sender_id': sender, 'body': body + (' ' + str(serial) if count > 1 else ''),
                               'observed_at': now()})
            result = store.commit(cursors, after, events, epochs)
            cursors = after
            status()
            return result['total_messages']

        write_private_json_new(private / 'catalog.json', {'schema_version': 1, 'conversations': [
            {'id': 'private_test', 'name': '合成客户·青禾', 'kind': 'private', 'sender_names': {'synthetic-sender': '合成客户'}},
            {'id': 'group_test', 'name': '合成项目测试群', 'kind': 'group', 'sender_names': {'synthetic-sender': '合成成员'}}
        ]})
        commit('private_test', '合成历史文字', count=125)
        commit('group_test', '合成群聊文字')
        provider = DelayedSyntheticData(data)
        server = make_server(provider, ROOT / 'apps' / 'web')
        token = server.token
        port = server.server_address[1]
        thread = None

        def start():
            nonlocal thread
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
            thread.start()

        def stop():
            nonlocal thread
            if thread is not None:
                server.shutdown()
                thread.join(timeout=5)
                thread = None
            server.server_close()

        start()
        print(json.dumps({'ready': True, 'launch_url': server.launch_url}), flush=True)
        try:
            for line in sys.stdin:
                request = json.loads(line)
                command = request.get('command')
                if command == 'commit':
                    result = {'total': commit(request.get('alias', 'private_test'), request['body'], sender=request.get('sender', 'synthetic-sender'), count=request.get('count', 1))}
                elif command == 'status':
                    status(request['stage'])
                    result = {'updated': True}
                elif command == 'offline':
                    stop()
                    result = {'online': False}
                elif command == 'online':
                    server = make_server(provider, ROOT / 'apps' / 'web', port=port, token=token)
                    status()
                    start()
                    result = {'online': True}
                elif command == 'heartbeat':
                    status()
                    result = {'updated': True}
                elif command == 'exit':
                    print(json.dumps({'closed': True}), flush=True)
                    break
                else:
                    result = {'error': 'synthetic_command_invalid'}
                print(json.dumps(result), flush=True)
        finally:
            if thread is not None:
                stop()
            store.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'error': 'synthetic_fixture_failed'}), flush=True)
        sys.exit(1)
