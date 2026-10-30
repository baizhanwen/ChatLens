#!/usr/bin/env python3
"""Read-only metadata probe. No chat text, screenshots, keys or database copies."""
import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def probe(client, system=None):
    system = system or platform.system()
    if system == 'Darwin':
        binary=ROOT/'scripts/native/bin/macos-probe'
        cmd = [str(binary),client] if binary.exists() else ['swift', str(ROOT/'scripts/native/macos_probe.swift'), client]
    elif system == 'Windows':
        cmd = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT/'scripts/native/windows_probe.ps1'), '-Platform', client]
    else:
        return {'platform':client,'os':system,'status':'不支持','reason':'Only macOS and Windows probe interfaces are provided'}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if p.returncode:
            return {'platform':client,'os':system,'status':'受限','reason':'native_probe_failed','exit_code':p.returncode}
        return json.loads(p.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return {'platform':client,'os':system,'status':'受限','reason':type(e).__name__}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--platform',choices=['wechat','qq','all'],default='all')
    args = parser.parse_args()
    clients = ['wechat','qq'] if args.platform == 'all' else [args.platform]
    result = {'recorded_at':datetime.now(timezone.utc).isoformat(),'system':platform.platform(),'machine':platform.machine(),'python':platform.python_version(),'probe_kind':'read-only metadata, no original text','clients':[probe(x) for x in clients]}
    data=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(data+'\n',encoding='utf-8')
    print(data)

if __name__=='__main__': main()
