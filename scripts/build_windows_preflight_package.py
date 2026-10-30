#!/usr/bin/env python3
"""Package only reviewed public Windows preflight files, never workspace data."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / "scripts/windows-proof"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["environment", "reader-setup"], default="environment")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.stage == "environment":
        names = ("Check-Windows.ps1", "Start-Check.cmd", "README-FIRST.txt")
        prefix = "ChatLens-Windows-Check"
        scope = "windows_environment_preflight_only_no_chat_or_key_reading"
    else:
        names = ("Check-ReaderSetup.ps1", "Start-ReaderSetup.cmd", "README-READER-SETUP.txt")
        prefix = "ChatLens-Windows-ReaderSetup"
        scope = "windows_reader_prerequisites_only_no_process_memory_or_chat_reading"
    if args.output is None:
        args.output = root / "artifacts/deliverables/windows-check" / (prefix + "-20261027.zip")
    content = {}
    for name in names:
        path = source / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
            raise RuntimeError("invalid_public_package_source")
        content[name] = path.read_bytes()
    manifest = {
        "built_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "scope": scope,
        "windows_execution_status": "未验证",
        "contains_user_database_or_keys": False,
        "files": [{"name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                  for name, data in content.items()],
    }
    content["PACKAGE-MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in content.items():
            archive.writestr(prefix + "/" + name, data)
    with zipfile.ZipFile(args.output) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(content):
            raise RuntimeError("package_integrity_check_failed")
    print(json.dumps({"output": str(args.output), "files": len(content),
                      "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                      "bytes": args.output.stat().st_size,
                      "windows_execution_status": "未验证"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
