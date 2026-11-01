#!/bin/sh
# Fixed project environment; no installation, account discovery, or key arguments.
set -eu

fail() {
    printf '%s\n' "$1" >&2
    exit 2
}

[ "$(/usr/bin/uname -s)" = 'Darwin' ] || fail 'reason=macos_launcher_requires_macos'
[ ! -L "$0" ] || fail 'reason=macos_launcher_symlink_refused'
TASK_ROOT=$(CDPATH= cd -P -- "$(/usr/bin/dirname -- "$0")" && /bin/pwd -P)
RUNTIME_PYTHON="$TASK_ROOT/.build/wechat-read-venv/bin/python"
WEB_ENTRY="$TASK_ROOT/scripts/macos_web.py"
NATIVE_PROBE="$TASK_ROOT/scripts/native/bin/macos-probe-web"

[ -x "$RUNTIME_PYTHON" ] || fail '缺少项目隔离 Python；请按本机 Mac Web 指南准备环境。入口不会安装软件。reason=macos_web_project_runtime_missing'
[ -f "$WEB_ENTRY" ] && [ ! -L "$WEB_ENTRY" ] || fail 'reason=macos_web_public_entry_missing_or_redirected'
[ -x "$NATIVE_PROBE" ] && [ -f "$NATIVE_PROBE" ] && [ ! -L "$NATIVE_PROBE" ] || fail '缺少本机 Web 状态探针；请按本机 Mac Web 指南准备环境。入口不会编译或安装软件。reason=macos_web_native_probe_missing'

cd -- "$TASK_ROOT"
printf '%s\n' 'ChatLens · 利明智耀：正在启动本机 Web 工作台。'
printf '%s\n' '自动识别当前微信；请在网页选择关注会话。配置、密钥与聊天保留本机，不调用模型。'
printf '%s\n' '请保留本窗口；按一次 Ctrl+C 正常停止本次网页与采集。'
exec "$RUNTIME_PYTHON" -I -B -X utf8 "$WEB_ENTRY" 2>/dev/null
