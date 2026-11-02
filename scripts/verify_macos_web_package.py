#!/usr/bin/env python3
"""Move an extracted Mac package and exercise its runtime and synthetic HTTP.

No real active-account detector, WeChat, source database or model is used.
The output directory must be new. Every test workspace is temporary and local.
"""
import argparse
import json
import os
import re
from pathlib import Path
import stat
import subprocess
import sys
import zipfile


FIXTURE = r'''
import http.client,json,socket,sys,tempfile,threading
from pathlib import Path
package=Path(sys.argv[1])
sys.path[:0]=[str(package/'scripts'),str(package/'scripts/windows-proof')]
from macos_active_account import ActiveAccount
from macos_web import MacWebManager
from macos_web_server import make_macos_server
connect=socket.socket.connect
def local_only(self,address):
    if not isinstance(address,tuple) or address[0]!='127.0.0.1':
        raise RuntimeError('synthetic_non_loopback_refused')
    return connect(self,address)
socket.socket.connect=local_only
with tempfile.TemporaryDirectory(prefix='chatlens-synthetic-extracted-mac-') as temporary:
    root=Path(temporary).resolve()
    account=root/'synthetic-account/db_storage';account.mkdir(parents=True)
    workspace=root/'synthetic-workspace';workspace.mkdir(mode=0o700)
    observed=ActiveAccount(account,12345,1700000000.0,'4.1.11')
    manager=MacWebManager(package,workspace,package/'scripts/native/bin/macos-probe-web',detector=lambda _:observed)
    manager.start()
    server=make_macos_server(manager,package/'apps/web')
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05},daemon=True)
    thread.start()
    try:
        client=http.client.HTTPConnection(*server.server_address,timeout=3)
        client.request('GET','/api/session',headers={'Authorization':'Bearer '+server.token})
        response=client.getresponse();session=json.loads(response.read());assert response.status==200
        cookie=response.getheader('Set-Cookie').split(';')[0]
        client.request('GET','/api/setup',headers={'Cookie':cookie})
        response=client.getresponse();setup=json.loads(response.read());assert response.status==200
        assert setup['account']['detected'] is True and setup['stage']=='needs_initialization'
        assert setup['checks']['wechat_running'] is True and setup['checks']['reader_ready'] is False
        client.request('GET','/connect.html');response=client.getresponse();html=response.read()
        assert response.status==200 and response.getheader('Cache-Control')=='no-store'
        assert all(text.encode() in html for text in ('识别微信程序','核验当前账号','准备读取能力','选择关注会话','开始采集'))
        client.request('GET','/index.html');response=client.getresponse();index=response.read()
        assert response.status==200 and b'id="ai-cloud-confirm" type="checkbox" checked' in index
        client.close()
    finally:
        server.shutdown();thread.join(timeout=2);server.server_close();manager.close()
print(json.dumps({'status':'passed','http_requests':4,'manager':'production','server':'production','detector':'explicit_synthetic','native_probe_executed':False,'source_database_opened':False,'model_called':False,'network_guard':'loopback_only'}))
'''


def call(arguments, *, env=None):
    result = subprocess.run([str(value) for value in arguments], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40,
                            check=False, shell=False, env=env)
    if result.returncode:
        raise RuntimeError('macos_package_verification_child_failed')
    return result.stdout


def verify(package, output):
    package, output = Path(package), Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError('verification_output_already_exists')
    package, output = package.resolve(), output.resolve()
    output.mkdir(parents=True)
    extract = output / 'initial-extraction'
    extract.mkdir()
    with zipfile.ZipFile(package) as archive:
        if archive.testzip() is not None:
            raise ValueError('archive_crc_failed')
        roots = set()
        for entry in archive.infolist():
            relative = Path(entry.filename)
            if (relative.is_absolute() or '..' in relative.parts or '\\' in entry.filename
                    or not stat.S_ISREG(entry.external_attr >> 16)):
                raise ValueError('archive_member_unsafe')
            roots.add(relative.parts[0])
            target = extract / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry))
            target.chmod(stat.S_IMODE(entry.external_attr >> 16))
        if roots != {'ChatLens-macOS-arm64-Web'}:
            raise ValueError('archive_root_invalid')
    moved_parent = output / 'relocated directory with spaces'
    moved_parent.mkdir()
    moved = moved_parent / 'ChatLens-macOS-arm64-Web'
    (extract / 'ChatLens-macOS-arm64-Web').rename(moved)
    runtime = moved / 'runtime/bin/python3.12'
    checked = json.loads(call([runtime, '-I', '-S', '-B', moved / 'scripts/macos_package_check.py', '--check']))
    tls_env = dict(os.environ)
    tls_env.pop('SSL_CERT_FILE', None)
    tls_env.pop('SSL_CERT_DIR', None)
    tls = json.loads(call([runtime, '-I', '-S', '-B', '-c',
        'import json,ssl;c=ssl.create_default_context();print(json.dumps({"certificate_required":c.verify_mode==ssl.CERT_REQUIRED,"hostname_checked":c.check_hostname,"ca_count":len(c.get_ca_certs())}))'], env=tls_env))
    if not tls['certificate_required'] or not tls['hostname_checked'] or tls['ca_count'] <= 0:
        raise ValueError('runtime_default_tls_ca_unavailable')
    bridge = json.loads(call(['/bin/sh', moved / '.build/wechat-read-venv/bin/python', '-c',
                              'import json,sys; print(json.dumps({"python":list(sys.version_info[:3])}))']))
    if bridge['python'] != [3, 12, 14]:
        raise ValueError('relative_runtime_bridge_failed')
    # Simulate the existing Apple helper's local import without starting any
    # capture. It has no -B flag and must inherit this launcher's cache policy.
    child_env = dict(os.environ)
    for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONUSERBASE'):
        child_env.pop(name, None)
    child_env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    call(['/usr/bin/python3', '-c', 'import sys;sys.path.insert(0,' + repr(str(moved / 'scripts')) + ');import prepare_wechat_read_test'], env=child_env)
    if list(moved.rglob('__pycache__')):
        raise ValueError('package_bytecode_cache_created')
    call([runtime, '-I', '-S', '-B', moved / 'scripts/macos_package_check.py', '--check'])
    probe = moved / 'scripts/native/bin/macos-probe-web'
    call(['/usr/bin/codesign', '--verify', '--strict', probe])
    binaries = [runtime, probe, *sorted((moved / 'runtime/lib').rglob('*.so')),
                *sorted((moved / 'runtime/lib').rglob('*.dylib'))]
    for binary in binaries:
        result = call(['/usr/bin/otool', '-L', binary]).decode()
        # otool -L includes a dylib's own LC_ID_DYLIB as its first item.
        # Keep that identity separate from actual LC_LOAD_DYLIB dependencies.
        identities = set(call(['/usr/bin/otool', '-D', binary]).decode().splitlines()[1:]) if binary.suffix == '.dylib' else set()
        for line in result.splitlines()[1:]:
            value = line.strip().split(' (', 1)[0]
            if value in identities:
                continue
            if value and not value.startswith(('/usr/lib/', '/System/Library/', '@loader_path/', '@rpath/', '@executable_path/')):
                raise ValueError('nonportable_binary_dependency')
    fixture = output / 'synthetic-http-fixture.py'
    fixture.write_text(FIXTURE)
    http = json.loads(call([runtime, '-I', '-B', fixture, moved]))
    result = {'status': 'passed', 'archive_extracted_and_moved': True,
              'moved_directory_contains_spaces': True, 'runtime_check': checked,
              'offline_default_tls_context': tls,
              'relative_initializer_bridge': 'passed', 'apple_helper_import_without_cache': 'passed',
              'second_package_check_after_helper_import': 'passed', 'native_probe_signature': 'passed',
              'binary_dependency_checks': len(binaries), 'binary_dependencies_system_or_relative': True,
              'dylib_ids_checked_separately': True,
              'synthetic_http': http, 'real_wechat_or_private_workspace_accessed': False,
              'terminal_gatekeeper_or_another_machine_tested': False}
    (output / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.package, args.output), ensure_ascii=False, indent=2))
    except Exception as error:
        code = str(error)
        print(json.dumps({'status': 'failed', 'reason': type(error).__name__,
                          'code': code if re.fullmatch(r'[a-z_]+', code) else None}), flush=True)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
