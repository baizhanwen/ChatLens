#!/usr/bin/env python3
"""Fixed system-Python bridge to Apple's LLDB module for private capture."""
from pathlib import Path
import subprocess
import sys


def lldb_python_path(run=subprocess.run):
    call = run(['/usr/bin/xcrun', 'lldb', '-P'], stdout=subprocess.PIPE,
               stderr=subprocess.DEVNULL, timeout=10, check=False, shell=False)
    if call.returncode != 0 or not isinstance(call.stdout, bytes) or not 1 <= len(call.stdout) <= 4096:
        raise ValueError('macos_lldb_runtime_unavailable')
    value = call.stdout.decode('utf-8').strip()
    if not value or '\n' in value or '\r' in value or '\0' in value or not Path(value).is_absolute() or not Path(value).is_dir():
        raise ValueError('macos_lldb_runtime_unavailable')
    return value


def main():
    try:
        if len(sys.argv) < 2 or sys.argv[1] != 'capture':
            return 2
        sys.path.insert(0, lldb_python_path())
        import prepare_wechat_read_test as preparation
        return preparation.main()
    except KeyboardInterrupt:
        # capture()'s own finally finishes before the exception reaches here.
        return 130
    except Exception:
        # This stream is suppressed by ReaderInitializer. Keep even standalone
        # failures fixed and free of subprocess/native/private exception data.
        print('reason=macos_capture_wrapper_failed', flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
