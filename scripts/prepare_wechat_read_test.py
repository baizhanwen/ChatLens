#!/usr/bin/env python3
"""Local, bounded prerequisite for an explicitly authorized WeChat read test.

Prepare a private debug copy; capture only keys validated against message DBs.
Never modifies the original app, attaches to an existing process, sends messages,
requests a password, changes SIP, or prints database paths, keys or chat content.
The CommonCrypto/SQLCipher-v4 validation method was reviewed against the MIT
reference https://gist.github.com/acmerfight/0a01249ef72970d07a2603dbb629e80f .
This task-owned helper does not execute that reference's bootstrap scripts.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


class SafeTestError(RuntimeError):
    """A task-owned, non-sensitive error code safe for public progress output."""


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def run(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise SafeTestError("command_failed_" + Path(arguments[0]).name)
    return result.stdout or result.stderr


def private_json(destination, value, overwrite=False):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    if destination.exists() and not overwrite:
        raise SafeTestError("private_output_already_exists")
    temporary = destination.with_name(destination.name + ".new")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def original_signature(app):
    run(["codesign", "--verify", "--deep", "--strict", str(app)])
    return run(["codesign", "-dvv", str(app)])


def running_wechat():
    result = subprocess.run(["pgrep", "-x", "WeChat"], capture_output=True, text=True)
    return bool(result.returncode == 0 and result.stdout.strip())


def only_database_root():
    root = Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
    roots = [entry / "db_storage" for entry in root.iterdir() if (entry / "db_storage").is_dir()]
    if len(roots) != 1:
        raise SafeTestError("exactly_one_account_required")
    return roots[0]


def selected_database_pages(database_scope="message"):
    root = only_database_root()
    if database_scope == "message":
        paths = sorted((root / "message").glob("message_*.db"))
    elif database_scope == "contact_metadata_only":
        paths = [root / "contact/contact.db"]
        if not paths[0].is_file():
            raise SafeTestError("contact_metadata_database_missing")
    else:
        raise SafeTestError("database_scope_not_allowlisted")
    if not paths:
        raise SafeTestError("message_database_missing")
    rows = []
    for path in paths:
        with path.open("rb") as source:
            page = source.read(4096)
        if len(page) == 4096 and not page.startswith(b"SQLite format 3\x00"):
            rows.append((str(path.relative_to(root)), page))
    if not rows:
        raise SafeTestError("encrypted_message_pages_missing")
    return rows


def key_validates_page(key, page):
    if len(key) != 32 or len(page) != 4096:
        return False
    salt = bytes(value ^ 0x3A for value in page[:16])
    authentication_key = hashlib.pbkdf2_hmac("sha512", key, salt, 2, 32)
    authentication = hmac.new(authentication_key, page[16:4032] + (1).to_bytes(4, "little"), hashlib.sha512).digest()
    return hmac.compare_digest(authentication, page[4032:4096])


def prepare(app, state, database_scope="message"):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    if (state / "manifest.json").exists() or (state / "WeChatProbe.app").exists():
        raise SafeTestError("test_copy_already_prepared")
    pages = selected_database_pages(database_scope)
    signature = original_signature(app)
    executable = app / "Contents/MacOS/WeChat"
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    private_json(state / "manifest.json", {
        "original_app": str(app), "original_signature": signature,
        "original_executable_sha256": digest,
        "prepared_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "selected_message_databases": len(pages),
        "database_scope": database_scope,
    })
    shadow = state / "WeChatProbe.app"
    run(["ditto", str(app), str(shadow)])
    run(["codesign", "--force", "--deep", "--sign", "-", str(shadow)])
    run(["codesign", "--verify", "--deep", "--strict", str(shadow)])
    if original_signature(app) != signature:
        raise SafeTestError("original_signature_changed")
    emit("private_copy_prepared", selected_databases=len(pages), database_scope=database_scope, original_signature_unchanged=True)


def capture(state, timeout, expected_app=None, expected_scope="message"):
    import lldb
    if running_wechat():
        raise SafeTestError("quit_original_wechat_normally_before_capture")
    manifest = json.loads((state / "manifest.json").read_text(encoding="utf-8"))
    original = Path(manifest["original_app"]).resolve(strict=True)
    database_scope = manifest.get("database_scope", "message")
    if database_scope != expected_scope:
        raise SafeTestError("capture_scope_does_not_match_prepared_scope")
    if expected_app is not None and original != expected_app.resolve(strict=True):
        raise SafeTestError("capture_app_does_not_match_prepared_original")
    if original_signature(original) != manifest["original_signature"]:
        raise SafeTestError("original_signature_no_longer_matches")
    if (state / "keys.json").exists():
        raise SafeTestError("keys_already_exist_do_not_repeat_capture")
    shadow = state / "WeChatProbe.app"
    if not shadow.is_dir():
        raise SafeTestError("prepared_private_copy_missing")
    rows = selected_database_pages(database_scope)
    salts = {page[:16] for _, page in rows}
    authentication_salts = {bytes(value ^ 0x3A for value in salt): salt for salt in salts}
    debugger = None
    process = None
    found = {}
    calls = 0
    try:
        debugger = lldb.SBDebugger.Create()
        debugger.SkipLLDBInitFiles(True)
        debugger.SetAsync(True)
        target = debugger.CreateTarget(str(shadow / "Contents/MacOS/WeChat"))
        if not target.IsValid():
            raise SafeTestError("debug_copy_target_invalid")
        breakpoint = target.BreakpointCreateByName("CCKeyDerivationPBKDF")
        if not breakpoint.IsValid():
            raise SafeTestError("debug_copy_breakpoint_invalid")
        launch = lldb.SBLaunchInfo([])
        # Keep the app's normal ASLR; stop once at entry so startup failures
        # can be distinguished from a running app awaiting local login.
        launch.SetLaunchFlags((launch.GetLaunchFlags() | lldb.eLaunchFlagStopAtEntry) & ~lldb.eLaunchFlagDisableASLR)
        for descriptor in (0, 1, 2):
            launch.AddSuppressFileAction(descriptor, descriptor == 0, descriptor != 0)
        launch.SetWorkingDirectory(str(shadow / "Contents/MacOS"))
        if running_wechat():
            raise SafeTestError("wechat_started_during_capture_preparation")
        error = lldb.SBError()
        process = target.Launch(launch, error)
        if error.Fail() or not process.IsValid():
            raise SafeTestError("debug_copy_launch_denied")
        if not target.GetTriple().startswith("arm64"):
            raise SafeTestError("native_arm64_required")
        emit("test_copy_launched", pid=process.GetProcessID(), expected_databases=len(rows), database_scope=database_scope, breakpoint_locations=breakpoint.GetNumLocations(), launch_flags=int(launch.GetLaunchFlags()))
        listener = debugger.GetListener()
        deadline = time.monotonic() + timeout
        last_notice = time.monotonic()

        def memory(address, length):
            failure = lldb.SBError()
            result = process.ReadMemory(address, length, failure)
            return bytes(result) if failure.Success() and len(result) == length else b""

        while time.monotonic() < deadline:
            event = lldb.SBEvent()
            if listener.WaitForEvent(1, event) and lldb.SBProcess.EventIsProcessEvent(event):
                if lldb.SBProcess.GetProcessFromEvent(event).GetProcessID() != process.GetProcessID():
                    continue
                status = lldb.SBProcess.GetStateFromEvent(event)
                if status in (lldb.eStateExited, lldb.eStateCrashed, lldb.eStateDetached):
                    emit("test_copy_ended", state_code=int(status), exit_code=process.GetExitStatus())
                    break
                if status == lldb.eStateStopped:
                    if calls == 0:
                        emit("test_copy_stopped", stop_reason_codes=[int(thread.GetStopReason()) for thread in process if thread.GetStopReason() != lldb.eStopReasonNone], breakpoint_locations=breakpoint.GetNumLocations())
                    for thread in process:
                        if thread.GetStopReason() != lldb.eStopReasonBreakpoint:
                            continue
                        if thread.GetStopReasonDataCount() < 2 or thread.GetStopReasonDataAtIndex(0) != breakpoint.GetID():
                            continue
                        calls += 1
                        frame = thread.GetFrameAtIndex(0)
                        values = [frame.FindRegister("x" + str(index)).GetValueAsUnsigned() for index in range(1, 7)]
                        pointer, length, salt_pointer, salt_length, prf, rounds = values
                        if salt_length != 16 or not 0 < length <= 256 or prf != 5 or rounds not in (2, 256000):
                            continue
                        observed_salt = memory(salt_pointer, 16)
                        salt = observed_salt if rounds == 256000 else authentication_salts.get(observed_salt)
                        if salt not in salts:
                            continue
                        secret = memory(pointer, length)
                        if len(secret) != length:
                            continue
                        key = hashlib.pbkdf2_hmac("sha512", secret, salt, 256000, 32) if rounds == 256000 else secret
                        changed = False
                        for relative, page in rows:
                            if relative not in found and page[:16] == salt and key_validates_page(key, page):
                                found[relative] = {"enc_key": key.hex()}
                                changed = True
                        if changed:
                            private_json(state / "keys.json", {"keys": found}, overwrite=True)
                            emit("validated_database_keys", count=len(found), expected=len(rows), database_scope=database_scope)
                    if process.Continue().Fail():
                        raise SafeTestError("owned_debug_process_cannot_continue")
                    if len(found) == len(rows):
                        break
            if time.monotonic() - last_notice >= 15:
                emit("waiting_for_local_login", validated_message_databases=len(found), observed_derivations=calls)
                last_notice = time.monotonic()
        emit("capture_finished", validated_message_databases=len(found), expected=len(rows), observed_derivations=calls)
        return 0 if found else 3
    finally:
        body_failed = sys.exc_info()[0] is not None
        cleanup_failed = False
        try:
            if process is not None and process.IsValid() and process.GetState() not in (lldb.eStateExited, lldb.eStateDetached, lldb.eStateInvalid):
                if process.Kill().Fail():
                    cleanup_failed = True
                    emit("owned_debug_process_stop_failed")
        except Exception:
            cleanup_failed = True
            emit("owned_debug_process_stop_failed")
        finally:
            if debugger is not None:
                try:
                    lldb.SBDebugger.Destroy(debugger)
                except Exception:
                    cleanup_failed = True
                    emit("debugger_cleanup_failed")
        signature_ok = False
        executable_ok = False
        try:
            signature_ok = original_signature(original) == manifest["original_signature"]
            executable_ok = hashlib.sha256((original / "Contents/MacOS/WeChat").read_bytes()).hexdigest() == manifest["original_executable_sha256"]
        except Exception:
            cleanup_failed = True
        try:
            can_restore = signature_ok and executable_ok and not running_wechat()
        except Exception:
            can_restore = False
        if can_restore:
            registration = Path("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister")
            try:
                subprocess.run([str(registration), "-u", str(shadow)], capture_output=True, timeout=10)
            except Exception:
                cleanup_failed = True
                emit("private_copy_unregister_failed")
            try:
                disabled = state / "WeChatProbe.disabled"
                if disabled.exists():
                    raise SafeTestError("disabled_private_copy_already_exists")
                shadow.rename(disabled)
            except Exception:
                cleanup_failed = True
                emit("private_copy_disable_failed")
            # A failure to unregister/rename the private copy must not prevent
            # restoring the verified original by its exact executable path.
            try:
                restored = subprocess.Popen([str(original / "Contents/MacOS/WeChat")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                emit("original_launch_requested", pid=restored.pid, original_signature_unchanged=True, original_executable_unchanged=True)
            except Exception:
                cleanup_failed = True
                emit("original_launch_failed")
        else:
            cleanup_failed = True
            emit("manual_restore_required", signature_matches=signature_ok, executable_matches=executable_ok)
        if cleanup_failed:
            emit("capture_cleanup_requires_attention")
            if not body_failed:
                raise SafeTestError("capture_cleanup_requires_attention")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "capture"])
    parser.add_argument("--app", type=Path, default=Path.home() / "Desktop/WeChat.app")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--database-scope", choices=["message", "contact_metadata_only"], default="message")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64" or os.geteuid() == 0:
        parser.error("native Apple Silicon user session required; no sudo")
    if not 10 <= args.timeout <= 300:
        parser.error("timeout must be 10..300 seconds")
    os.umask(0o077)
    state = args.state_dir.resolve()
    app = args.app.resolve()
    if app == state or app in state.parents:
        parser.error("private state must be outside original application")
    if args.phase == "prepare":
        prepare(app, state, args.database_scope)
        return 0
    return capture(state, args.timeout, app, args.database_scope)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        emit("test_stopped", error_code="interrupted")
        sys.exit(130)
    except Exception as failure:
        # Only explicitly generated codes are printable; never leak library data.
        code = str(failure) if isinstance(failure, SafeTestError) else type(failure).__name__
        emit("test_stopped", error_code=code)
        sys.exit(2)
