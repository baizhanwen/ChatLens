"""Fixed, public-only phase updates for the selected-chat supervisor."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from windows_keyproof_platform import path_is_local


KIND = 'chatlens_selected_progress'
MAX_BYTES = 16384
STAGES = {
    'startup': '程序已启动，正在准备本轮验证。',
    'private_setup': '正在建立本机受保护的本轮输出目录。',
    'config_validation': '正在核对本机配置与已保存密钥的格式。',
    'driver_smoke': '正在验证虚构加密数据库、WAL 和只读驱动。',
    'file_preflight': '正在核对固定的两个消息库和联系人库。',
    'read_message0': '正在只读核对第一个消息库的首页。',
    'read_message1': '正在只读核对第二个消息库的首页。',
    'read_contact': '正在只读核对联系人库的首页。',
    'cached_key_check': '正在验证本机已有密钥与首页是否匹配。',
    'process_identity': '正在核对所选微信进程的身份。',
    'memory_markers': '正在只读定位密钥结构标记，请等待。',
    'memory_references': '正在只读核对固定三库的密钥结构，请等待。',
    'saving_keys': '正在将通过验证的密钥保存到本机私有目录。',
    'checking_wal': '正在核对固定三库的 WAL 与 SHM 配对条件。',
    'waiting_private_name': '等待输入：请在本窗口输入本轮私聊的准确名称，然后按回车。',
    'waiting_group_name': '等待输入：请在本窗口输入本轮群聊的准确名称，然后按回车。',
    'contact_lookup': '正在用联系人元信息精确定位所选私聊和群聊。',
    'saving_config': '正在将本轮会话范围与测试码保存在本机私有目录。',
    'native_precheck': '正在核对微信后台运行与屏幕未锁定状态。',
    'message_baseline': '正在建立旧消息基线，不读取历史正文。',
    'waiting_start': '等待确认：看到 READY 和四个测试码后，请在本窗口按回车开始计时。',
    'collecting': '正在等待所选私聊和群聊的新测试文字。',
    'finalizing': '正在结束本轮验证并保存脱敏结果。',
}
FIELDS = {'schema_version', 'kind', 'stage', 'recorded_at_utc',
          'stdin_tty', 'stdout_tty', 'stderr_tty'}


class ProgressError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _identity(info):
    return info.st_dev, info.st_ino


def _regular(info):
    return (stat.S_ISREG(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400
            and info.st_nlink == 1 and 2 <= info.st_size <= MAX_BYTES)


def _load(path, expected=None):
    try:
        before = path.lstat()
        if not _regular(before) or expected is not None and _identity(before) != expected:
            raise ProgressError('progress_file_identity_invalid')
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if not _regular(opened) or _identity(opened) != _identity(before):
                raise ProgressError('progress_file_identity_invalid')
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ProgressError('progress_file_size_limit')
        value = json.loads(raw.decode('utf-8-sig'))
        if (not isinstance(value, dict) or not set(value).issubset(FIELDS)
                or type(value.get('schema_version')) is not int or value['schema_version'] != 1
                or value.get('kind') != KIND or value.get('stage') not in STAGES
                or any(field in value and type(value[field]) is not bool for field in
                       ('stdin_tty', 'stdout_tty', 'stderr_tty'))
                or 'recorded_at_utc' in value and (type(value['recorded_at_utc']) is not str
                                                 or len(value['recorded_at_utc']) > 64)):
            raise ProgressError('progress_file_marker_invalid')
        return value, _identity(opened)
    except (OSError, ValueError, UnicodeError, TypeError):
        raise ProgressError('progress_file_invalid') from None


def _tty(stream):
    try:
        return bool(stream.isatty())
    except Exception:
        return False


class PhaseProgress:
    def __init__(self, path=None, sink=print, *, output_dir=None):
        self.path, self.sink, self.stage = None, sink, None
        self._identity, self._persisted_stage = None, None
        if path is None:
            return
        if output_dir is None:
            raise ProgressError('progress_output_binding_required')
        try:
            candidate = Path(os.path.abspath(os.fspath(path)))
            output = Path(os.path.abspath(os.fspath(output_dir)))
            pattern = r'\.' + re.escape(output.name) + r'-[0-9a-fA-F]{8,64}\.progress\.json'
            if (candidate.parent != output.parent or not re.fullmatch(pattern, candidate.name)
                    or not path_is_local(candidate.parent, require_dir=True)):
                raise ProgressError('progress_path_binding_invalid')
            initial, identity = _load(candidate)
            if initial['stage'] != 'startup':
                raise ProgressError('progress_initial_stage_invalid')
            self.path, self._identity, self._persisted_stage = candidate, identity, 'startup'
        except ProgressError:
            raise
        except Exception:
            raise ProgressError('progress_path_binding_invalid') from None

    def _write(self, payload):
        temporary, installed = None, False
        try:
            if not path_is_local(self.path.parent, require_dir=True):
                raise ProgressError('progress_path_binding_invalid')
            previous, _ = _load(self.path, self._identity)
            if previous['stage'] != self._persisted_stage:
                raise ProgressError('progress_file_identity_invalid')
            descriptor, temporary = tempfile.mkstemp(prefix='.' + self.path.name + '.',
                                                     suffix='.tmp', dir=self.path.parent)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(json.dumps(payload, ensure_ascii=True, allow_nan=False).encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
                identity = _identity(os.fstat(stream.fileno()))
            # Recheck the owned destination immediately before replacing it.
            current = self.path.lstat()
            if not _regular(current) or _identity(current) != self._identity:
                raise ProgressError('progress_file_identity_invalid')
            os.replace(temporary, self.path)
            installed = True
            self._identity, self._persisted_stage = identity, payload['stage']
        except ProgressError:
            raise
        except Exception:
            raise ProgressError('progress_write_failed') from None
        finally:
            if temporary is not None and not installed:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def set(self, stage):
        if type(stage) is not str or stage not in STAGES:
            raise ProgressError('invalid_progress_stage')
        if stage == self.stage:
            return
        payload = {'schema_version': 1, 'kind': KIND, 'stage': stage,
                   'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
                   'stdin_tty': _tty(sys.stdin), 'stdout_tty': _tty(sys.stdout),
                   'stderr_tty': _tty(sys.stderr)}
        if self.path is not None:
            self._write(payload)
        try:
            self.sink('[阶段] ' + STAGES[stage], flush=True)
        except Exception:
            raise ProgressError('progress_output_failed') from None
        self.stage = stage
