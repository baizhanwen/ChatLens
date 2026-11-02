#!/bin/sh
set -eu
fail() { printf '%s\n' "$1" >&2; exit 2; }
[ "$(/usr/bin/uname -s)" = 'Darwin' ] || fail '此下载包仅供 macOS 使用。reason=macos_package_requires_macos'
[ "$(/usr/bin/uname -m)" = 'arm64' ] || fail '此测试包仅供 Apple Silicon Mac 原生终端使用。reason=macos_package_requires_arm64'
case "$(/usr/bin/sw_vers -productVersion)" in 27.*) ;; *) fail '当前测试包限定 macOS 27；其他系统版本尚未适配。reason=macos_package_requires_macos27' ;; esac
[ ! -L "$0" ] || fail 'reason=macos_package_launcher_redirected'
TASK_ROOT=$(CDPATH= cd -P -- "$(/usr/bin/dirname -- "$0")" && /bin/pwd -P)
TASK_PYTHON="$TASK_ROOT/runtime/bin/python3.12"
[ -x "$TASK_PYTHON" ] || fail '请完整解压下载包，保留 runtime 文件夹。reason=macos_package_runtime_missing'
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE
PYTHONDONTWRITEBYTECODE=1
PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE PYTHONNOUSERSITE
cd -- "$TASK_ROOT"
"$TASK_PYTHON" -I -S -B "$TASK_ROOT/scripts/macos_package_check.py" --check || exit 2
/usr/bin/xcode-select -p >/dev/null 2>&1 || fail '首次初始化需要 Apple Command Line Tools。请本人安装并完成 Apple 的提示后再启动。reason=macos_package_developer_tools_required'
/usr/bin/python3 -I -B -c 'import subprocess,sys; p=subprocess.run(["/usr/bin/xcrun","lldb","-P"],capture_output=True,timeout=10,check=True); sys.path.insert(0,p.stdout.decode().strip()); import lldb' >/dev/null 2>&1 || fail 'Apple 系统 Python 的 LLDB 组件尚不可用。请完成 Command Line Tools 配置后再启动。reason=macos_package_lldb_required'
printf '%s\n' 'ChatLens macOS arm64 Web 测试版 · 正在打开五步接入页。'
printf '%s\n' '资料保存在本机用户的 Library/Application Support/ChatLens/macOS-Web，账号与关注范围分别保存。'
printf '%s\n' '首次初始化、微信登录和会话选择由你在本机确认。保留此窗口，Ctrl+C 正常停止。'
exec "$TASK_PYTHON" -I -B -X utf8 "$TASK_ROOT/scripts/macos_web.py" --workspace "$HOME/Library/Application Support/ChatLens/macOS-Web" 2>/dev/null
