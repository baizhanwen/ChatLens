#!/usr/bin/env python3
"""Read-only WeChat prerequisites; never emit account names, keys or messages."""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess
from datetime import datetime, timedelta, timezone


def command(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=20)


def app_metadata(app):
    with (app / "Contents/Info.plist").open("rb") as source:
        info = plistlib.load(source)
    signature = command(["codesign", "-dv", "--verbose=2", str(app)])
    return {
        "version": info.get("CFBundleShortVersionString"),
        "build": info.get("CFBundleVersion"),
        "bundle_id": info.get("CFBundleIdentifier"),
        "signature": [
            line for line in (signature.stdout + signature.stderr).splitlines()
            if line.startswith(("Authority=", "TeamIdentifier=", "Signature="))
        ],
    }


def host_apps():
    pid = os.getpid()
    apps = []
    for _ in range(8):
        result = command(["ps", "-p", str(pid), "-o", "ppid=,comm="])
        parts = result.stdout.strip().split(None, 1)
        if len(parts) != 2:
            break
        for parent in Path(parts[1]).parents:
            if parent.suffix == ".app":
                if parent.name not in apps:
                    apps.append(parent.name)
                break
        pid = int(parts[0])
        if pid <= 1:
            break
    return apps


def probe():
    report = {
        "recorded_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "system": platform.platform(),
        "architecture": platform.machine(),
        "probe_kind": "read_only_metadata_and_16_byte_file_headers",
        "scope": "prerequisites_only_no_chat_query_no_key_extraction",
        "host_apps": host_apps(),
        "running_clients": [],
        "storage": {},
        "real_messages_read": 0,
        "background_reading": "未验证",
        "windows_reading": "未验证",
        "missed_messages": None,
        "duplicate_messages": None,
        "latency_ms": None,
    }
    result = command(["ps", "-axo", "pid=,comm="])
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[1].endswith("/WeChat.app/Contents/MacOS/WeChat"):
            continue
        app = Path(parts[1]).parents[2]
        client = app_metadata(app)
        client["pid"] = int(parts[0])
        client["location"] = "desktop_copy" if app.parent == Path.home() / "Desktop" else "other"
        report["running_clients"].append(client)
    installed = Path("/Applications/WeChat.app")
    if installed.exists():
        report["applications_copy"] = app_metadata(installed)

    storage = Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
    stats = {
        "root_exists": storage.exists(),
        "account_count": 0,
        "database_count": 0,
        "wal_count": 0,
        "plaintext_sqlite_headers": 0,
        "other_headers": 0,
        "unreadable_files": 0,
    }
    try:
        accounts = list(storage.iterdir())
        for account in accounts:
            database_root = account / "db_storage"
            if not account.is_dir() or not database_root.is_dir():
                continue
            stats["account_count"] += 1
            databases = list(database_root.rglob("*.db"))
            stats["database_count"] += len(databases)
            stats["wal_count"] += len(list(database_root.rglob("*-wal")))
            for database in databases:
                try:
                    with database.open("rb") as source:
                        header = source.read(16)
                    if header == b"SQLite format 3\x00":
                        stats["plaintext_sqlite_headers"] += 1
                    else:
                        stats["other_headers"] += 1
                except OSError:
                    stats["unreadable_files"] += 1
        stats["directory_access"] = "通过"
    except PermissionError:
        stats["directory_access"] = "失败"
        stats["error_code"] = "macos_privacy_access_denied"
    except FileNotFoundError:
        stats["directory_access"] = "失败"
        stats["error_code"] = "data_root_not_found"
    report["storage"] = stats
    report["preflight_status"] = "通过" if stats.get("directory_access") == "通过" else "待外部条件"
    report["note"] = "File-header access is not database decryption or background message verification."
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("This metadata probe currently targets macOS only.")
    report = probe()
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(args.output), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(serialized)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
    print(serialized, end="")


if __name__ == "__main__":
    main()
