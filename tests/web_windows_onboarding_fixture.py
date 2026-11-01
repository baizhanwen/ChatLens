"""Real first-use coordinator/HTTP/workspace/store with synthetic Win adapters.

Only the process/account discovery, explicit key initialization and collector
source are synthetic. Browser requests run the actual authenticated handlers.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from queue import Empty, Queue
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts'), str(ROOT / 'scripts/windows-proof'), str(ROOT / 'tests')]
from tests.test_windows_onboarding import OnboardingTests
from windows_web_server import make_windows_server
from ai_web_server import attach_ai_routes


class Runtime:
    def __init__(self):
        self.case = OnboardingTests()
        self.case.setUp()
        self.case.choices = [
            {'chat_id': 'synthetic-peer', 'name': '合成客户甲', 'type': 'private'},
            {'chat_id': 'synthetic-peer-two', 'name': '合成客户乙', 'type': 'private'},
            {'chat_id': 'synthetic-group@chatroom', 'name': '合成测试群', 'type': 'group'}]
        self.sources = {'one': self.case.source, 'two': self.case.root / 'synthetic-two/db_storage'}
        self.sources['two'].mkdir(parents=True)
        self.which = 'one'
        self.jobs, self.stopping = Queue(), threading.Event()
        self.reader_snapshots = {}
        self.open()
        self.owner = threading.Thread(target=self.run, daemon=True)
        self.owner.start()

    def open(self):
        self.manager = self.case.manager()
        self.server = make_windows_server(self.manager, ROOT / 'apps/web', port=0)
        attach_ai_routes(self.server, self.manager.get_ai_service)
        self.http = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.http.start()

    def run(self):
        while not self.stopping.is_set():
            try:
                action, event, result = self.jobs.get(timeout=.03)
            except Empty:
                self.manager.tick()
                child, output = self.manager.child, self.manager.child_output
                if child is not None and child.poll() is None and output is not None:
                    from collector_service import Heartbeat
                    Heartbeat(output).emit('collecting', child.store.stats()['total_messages'], 0, child.store.resumed)
                continue
            try:
                result['value'] = action()
            except BaseException as error:
                result['error'] = error
            finally:
                event.set()

    def own(self, action):
        event, result = threading.Event(), {}
        self.jobs.put((action, event, result))
        if not event.wait(8):
            raise RuntimeError('synthetic_onboarding_owner_timeout')
        if 'error' in result:
            raise result['error']
        return result.get('value')

    def state(self):
        setup = self.manager.setup()
        delegate = self.manager._delegate
        account_count = len(self.manager.workspace._read()['accounts'])
        total = 0
        baseline = None
        stable = True
        if delegate is not None:
            root = delegate.legacy_data
            hashes = {name: hashlib.sha256((root / 'private' / name).read_bytes()).hexdigest()
                      for name in ('scope.json', 'keys.json')}
            account_key = delegate.account_identity
            if account_key in self.reader_snapshots:
                stable = self.reader_snapshots[account_key] == hashes
            else:
                self.reader_snapshots[account_key] = hashes
            if self.manager.child is not None:
                total = self.manager.child.store.stats()['total_messages']
                baseline = self.manager.child.store.snapshot()['baseline_completed_at']
            elif delegate.provider is not None:
                for conversation in delegate.conversations()['conversations']:
                    total += len(delegate.messages(conversation['id'])['messages'])
        return {'onboarding_state': setup['onboarding']['state'], 'mode': setup['onboarding']['mode'],
                'initialization_calls': len(self.case.initializations), 'workspace_account_count': account_count,
                'selected_conversation_count': len(self.manager.active_config['conversations']),
                'collector_running': setup['connection']['collector_running'],
                'collector_stage': setup['connection']['collector_stage'],
                'current_message_count': total, 'scope_generation': self.manager.scope_generation,
                'original_reader_files_preserved': stable, 'baseline_completed_at': baseline,
                'real_windows_or_cloud_used': False}

    def commit(self, body):
        child = self.manager.child
        if child is None:
            raise RuntimeError('synthetic_collector_not_running')
        snapshot = child.store.snapshot()
        before = snapshot['cursors']
        after = json.loads(json.dumps(before))
        row = self.manager.active_config['conversations'][0]
        current = after['message/message_0.db'][row['alias']]
        current['local_id'] += 1
        serial = current['local_id']
        now = datetime.now(timezone.utc)
        event = {'source_key': hashlib.sha256((self.which + ':' + row['alias'] + ':' + str(serial)).encode()).hexdigest(),
                 'alias': row['alias'], 'source_local_id': serial, 'sender_id': row['chat_id'],
                 'body': body, 'sent_at': int(now.timestamp()), 'observed_at': now.isoformat()}
        child.store.commit(before, after, [event], {})

    def command(self, value):
        name = value['command']
        if name == 'state':
            return self.own(self.state)
        if name == 'commit':
            self.own(lambda: self.commit(value.get('body', '合成新消息')))
        elif name == 'account':
            account = value.get('account', 'two')
            def change():
                self.which = account
                self.case.current = ({'state': 'waiting_for_wechat', 'reason': 'windows_process_not_running'}
                                     if account == 'waiting' else self.case.candidate(self.sources[account]))
                if self.manager._delegate is not None:
                    self.manager._delegate.last_process_at = self.manager._delegate.last_probe_at = float('-inf')
                self.manager.tick()
            self.own(change)
        elif name == 'relaunch':
            def reopen():
                self.manager.close()
                self.server.shutdown(); self.server.server_close(); self.http.join(timeout=2)
                self.open()
                return {'launch_url': self.server.launch_url, **self.state()}
            return self.own(reopen)
        else:
            raise RuntimeError('synthetic_onboarding_command_invalid')
        return self.own(self.state)

    def close(self):
        self.own(self.manager.close)
        self.stopping.set(); self.owner.join(timeout=2)
        self.server.shutdown(); self.server.server_close(); self.http.join(timeout=2)
        self.case.tearDown()


def main():
    from web_ai_fixture import install_loopback_guard
    guard = install_loopback_guard()
    runtime = Runtime()
    print(json.dumps({'ready': True, 'launch_url': runtime.server.launch_url,
                      'network_guard': guard, **runtime.state()}), flush=True)
    try:
        for line in sys.stdin:
            value = json.loads(line)
            if value['command'] == 'exit':
                print(json.dumps({'closed': True}), flush=True)
                break
            print(json.dumps({**runtime.command(value), 'network_guard': guard}), flush=True)
    finally:
        runtime.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'error': 'synthetic_windows_onboarding_fixture_failed'}), flush=True)
        raise SystemExit(1)
