#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 `scripts/backup_db.py` 产出的备份集恢复（Windows / Linux / macOS）。

主体使用标准库；恢复后 Chroma 验证需要项目环境已安装 `chromadb`。

安全设计：
  * 必须显式 `--yes` 才会写入（防误触）；
  * 恢复前**统一预检** SQLite / Chroma / Excel 三类对象：字段、文件、
    size、SHA256、ZIP CRC 和成员路径全部通过后才允许任何目标写入；
  * 先在同卷 `.restore_staging/<run_id>/` 解压并二次校验，全部通过后才切换；
  * 切换前把旧 SQLite（含 WAL/SHM）、Chroma、Excel 保护到 pre_restore /
    `.restore_rollback/<run_id>/`；逐对象切换，任一失败自动逆序回滚；
  * 持久化 `.restore_state/<run_id>/state.json`，发现未完成 run 时拒绝新恢复；
  * 使用目标根物理身份对应的 Registry Root Lock 做跨进程互斥；
  * 恢复后校验实际 Chroma collection、Excel artifacts 和 staging 文件清单；
  * **不是三类数据的全局原子事务**：只承诺逐对象可切换、可回滚、可识别失败；
  * `--target-root` 支持恢复到演练目录（不碰真实数据），用于定期演练。

返回码：
  0  成功（或未加 --yes 的预演）
  2  manifest / 备份集结构无效，或缺少必需对象
  3  size / SHA256 校验失败
  4  文件不存在、不是普通文件、路径不安全，或 ZIP 结构/成员不安全
  5  检测到未完成/未知的恢复 run，拒绝继续
  6  自定义来源路径尚未实现目标映射，拒绝恢复到默认目录
  7  恢复事务失败（成功回滚返回 7；回滚失败会保留材料并要求人工处理）
  8  同一或重叠目标已有恢复进程持锁，本次未修改目标

用法：
    # 1) 先停止后端（Windows：Ctrl+C 结束 uvicorn；Docker：docker compose stop backend）
    # 2) 恢复
    python scripts/restore_db.py --from backups/ai_assistant_backup_20261014_160000 --yes
    # 3) 演练（写到临时目录，不动真实数据）
    python scripts/restore_db.py --from backups/<set> --yes --target-root D:\\drill
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

# Keep sibling safety modules importable when this script is loaded by path
# (tests and direct CLI invocation both use scripts/ as the module home).
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from restore_path_security import PathSecurityError, open_secure_directory
from restore_registry import (
    RegistryConflictError,
    RegistryLockBusy,
    RegistryLockError,
)
from restore_root_lock import RootLock, RootLockError

REPO_ROOT = Path(__file__).resolve().parents[1]

EXIT_INVALID_BACKUP = 2
EXIT_CHECKSUM = 3
EXIT_PREFLIGHT = 4
EXIT_INCOMPLETE_RUN = 5
EXIT_PATH_MAPPING = 6
EXIT_TRANSACTION = 7
EXIT_LOCKED = 8

REQUIRED_TYPES = ('sqlite_database', 'vector_database', 'excel_artifacts')
ZIP_TYPES = ('vector_database', 'excel_artifacts')
_SHA_RE = re.compile(r'^[0-9a-fA-F]{64}$')
STAGING_DIRNAME = '.restore_staging'
STATE_DIRNAME = '.restore_state'
ROLLBACK_DIRNAME = '.restore_rollback'
LOCK_DIRNAME = '.restore_control'
LOCK_FILENAME = 'restore.lock'
REGISTRY_DIRNAME = 'my_ai_assistant_restore_registry'
RESTORE_REGISTRY_ROOT_ENV = 'MY_AI_ASSISTANT_RESTORE_REGISTRY_ROOT'
# 与 backend/services/vector_service.py:92-95 的生产 collection 初始化保持一致。
CHROMA_COLLECTION_NAME = 'ai_assistant_docs'
CHROMA_SQLITE_NAME = 'chroma.sqlite3'
CHROMA_READ_BATCH = 500
STATE_REQUIRED_FIELDS = (
    'run_id', 'state', 'target_root', 'staging', 'rollback',
    'pre_restore', 'targets',
)
STATE_TERMINAL = ('COMMITTED', 'ROLLED_BACK')
STATE_NAMES = frozenset({
    'INIT', 'PREFLIGHT_OK', 'STAGING', 'STAGING_VALIDATED',
    'TARGETS_BACKED_UP', 'SWITCHING_SQLITE', 'SQLITE_SWITCHED',
    'SWITCHING_CHROMA', 'CHROMA_SWITCHED', 'SWITCHING_EXCEL',
    'EXCEL_SWITCHED', 'SWITCHED', 'VERIFYING', 'COMMITTED',
    'ROLLING_BACK', 'ROLLED_BACK', 'FAILED_RECOVERY_REQUIRED',
})
_RUN_ID_RE = re.compile(r'^\d{8}T\d{6}_\d+_[0-9a-f]{8}$')


class BackupValidationError(Exception):
    """预检失败：code 用于 CLI 返回码，message 面向用户。"""

    def __init__(self, message: str, code: int = EXIT_PREFLIGHT):
        super().__init__(message)
        self.code = code


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _manifest_error(message: str) -> BackupValidationError:
    return BackupValidationError(message, EXIT_INVALID_BACKUP)


def _file_error(message: str) -> BackupValidationError:
    return BackupValidationError(message, EXIT_PREFLIGHT)


def _checksum_error(message: str) -> BackupValidationError:
    return BackupValidationError(message, EXIT_CHECKSUM)


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def _safe_manifest_name(name: Any) -> bool:
    """manifest 里的 name 只能是备份集目录内的普通文件名。"""
    if not isinstance(name, str) or not name.strip():
        return False
    if name in ('.', '..'):
        return False
    if any(sep in name for sep in ('/', '\\', ':')):
        return False
    if '\x00' in name:
        return False
    # Windows 盘符 / 绝对路径 / 目录分量一律拒绝。
    if PureWindowsPath(name).is_absolute() or PureWindowsPath(name).drive:
        return False
    if PurePosixPath(name).is_absolute():
        return False
    return PureWindowsPath(name).name == name


def _safe_zip_member(name: str) -> bool:
    """ZIP 成员路径必须保持在解压目标目录内。"""
    if not isinstance(name, str) or not name or '\x00' in name:
        return False
    if '\\' in name:
        return False
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if posix.is_absolute() or '..' in posix.parts:
        return False
    if windows.is_absolute() or windows.drive or windows.root:
        return False
    return True


def _zip_member_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return stat.S_ISLNK(mode)


def _validate_zip(path: Path, item_type: str) -> None:
    """验证 ZIP 可读、CRC 正确、成员路径安全且不含符号链接。"""
    try:
        with zipfile.ZipFile(path) as zf:
            bad_member = zf.testzip()
            if bad_member is not None:
                raise _file_error(
                    '%s ZIP 内容损坏（CRC 失败：%s）' % (item_type, bad_member))
            for info in zf.infolist():
                if not _safe_zip_member(info.filename):
                    raise _file_error(
                        '%s ZIP 包含不安全成员路径：%r' % (item_type, info.filename))
                if _zip_member_is_symlink(info):
                    raise _file_error(
                        '%s ZIP 包含符号链接成员：%r' % (item_type, info.filename))
    except BackupValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - CRC/解压库可能抛 zlib.error 等非 BadZipFile
        raise _file_error('%s ZIP 无法读取或已损坏：%s' % (item_type, exc)) from exc


def _validated_item(src: Path, item: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    item_type = item.get('type')
    name = item.get('name')
    size = item.get('size')
    sha256 = item.get('sha256')

    if item_type not in REQUIRED_TYPES:
        raise _manifest_error('manifest 包含不支持的对象类型：%r' % (item_type,))
    if not _safe_manifest_name(name):
        raise _file_error('%s 的文件名不安全：%r' % (item_type, name))
    if (not isinstance(size, int) or isinstance(size, bool) or size < 0):
        raise _manifest_error('%s 的 size 字段无效：%r' % (item_type, size))
    if not _valid_sha256(sha256):
        raise _manifest_error('%s 的 sha256 字段无效' % item_type)

    src_resolved = src.resolve()
    path = (src_resolved / name).resolve()
    try:
        path.relative_to(src_resolved)
    except ValueError as exc:
        raise _file_error('%s 的文件路径逃逸备份目录：%r' % (item_type, name)) from exc
    if path == src_resolved:
        raise _file_error('%s 的文件名指向备份目录本身：%r' % (item_type, name))
    if _is_reparse_point(path):
        raise _file_error('%s 的备份文件不能是符号链接/reparse point：%s' % (item_type, name))
    if not path.is_file():
        raise _file_error('%s 的备份文件不存在或不是普通文件：%s' % (item_type, name))

    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        raise _file_error('%s 的备份文件无法读取：%s' % (item_type, name)) from exc
    if actual_size != size:
        raise _checksum_error(
            '%s 的 size 不匹配：manifest=%s actual=%s'
            % (item_type, size, actual_size))

    try:
        actual_sha = sha256_file(path)
    except OSError as exc:
        raise _file_error('%s 的备份文件无法计算 SHA256：%s' % (item_type, name)) from exc
    if actual_sha.lower() != sha256.lower():
        raise _checksum_error('%s 的 SHA256 不匹配' % item_type)

    if item_type in ZIP_TYPES:
        _validate_zip(path, item_type)
    return item, path


def validate_backup_set(src: Path, manifest: Any) -> dict[str, tuple[dict, Path]]:
    """恢复前完整预检；不写任何目标数据。

    返回 {type: (item, verified_path)}，仅在全部对象通过后返回。
    """
    if not isinstance(manifest, dict):
        raise _manifest_error('manifest 顶层必须是 JSON 对象')
    raw_items = manifest.get('items')
    if not isinstance(raw_items, list) or not raw_items:
        raise _manifest_error('manifest.items 必须是非空列表')

    by_type: dict[str, tuple[dict, Path]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            raise _manifest_error('manifest.items 中包含非对象条目')
        item_type = item.get('type')
        if item_type not in REQUIRED_TYPES:
            raise _manifest_error('manifest 包含不支持的对象类型：%r' % (item_type,))
        if item_type in by_type:
            raise _manifest_error('manifest 包含重复的对象类型：%s' % item_type)
        by_type[item_type] = _validated_item(src, item)

    missing = [t for t in REQUIRED_TYPES if t not in by_type]
    if missing:
        raise _manifest_error(
            '备份集缺少必需对象：%s' % ', '.join(missing))
    return by_type


def unzip_into(src_zip: Path, dest_parent: Path) -> None:
    """把 zip 解压回其原始父目录结构（backup_db.zip_tree 保留 src.parent 起的相对路径）。"""
    dest_parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src_zip) as zf:
        zf.extractall(dest_parent)


def _load_manifest(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _manifest_error('manifest.json 无法读取或不是合法 JSON：%s' % exc) from exc


class RestoreTransactionError(Exception):
    """恢复切换或回滚无法安全完成。"""


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def _new_run_id() -> str:
    return '%s_%s_%s' % (
        datetime.now().strftime('%Y%m%dT%H%M%S'),
        os.getpid(),
        uuid.uuid4().hex[:8],
    )


def _within(path: Path, root: Path, label: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise RestoreTransactionError('%s 路径不在目标根内：%s' % (label, path)) from exc


_REPARSE_ATTRIBUTE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


def _is_reparse_point(path: Path) -> bool:
    """识别 Windows symlink/junction/其他 reparse point，也兼容 POSIX symlink。"""
    try:
        if path.is_symlink():
            return True
        info = os.lstat(path)
    except (OSError, ValueError):
        return False
    attributes = getattr(info, 'st_file_attributes', 0)
    return bool(attributes & _REPARSE_ATTRIBUTE)


def _reject_symlink(path: Path, root: Path) -> None:
    current = path
    while True:
        if _is_reparse_point(current):
            raise RestoreTransactionError('拒绝符号链接或 reparse point 路径：%s' % current)
        if current == current.parent:
            break
        try:
            current.relative_to(root)
        except ValueError:
            pass
        current = current.parent


def _safe_run_dir(root: Path, name: str, run_id: str) -> Path:
    base = root / name
    _within(base, root, name)
    _reject_symlink(base, root)
    if base.exists() and not base.is_dir():
        raise RestoreTransactionError('恢复工作目录不是目录：%s' % base)
    base.mkdir(parents=True, exist_ok=True)
    run_dir = base / run_id
    if run_dir.exists():
        raise RestoreTransactionError('恢复 run 目录已存在，拒绝复用：%s' % run_dir)
    run_dir.mkdir()
    _within(run_dir, root, name)
    return run_dir


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_state(state: dict[str, Any]) -> None:
    state['updated_at'] = _now()
    _atomic_write_json(Path(state['state_path']), state)


def _set_state(state: dict[str, Any], name: str, error: str | None = None) -> None:
    state['state'] = name
    if error is not None:
        state['error'] = error
    _write_state(state)


def _validate_control_paths(root: Path) -> None:
    for name in (LOCK_DIRNAME, STAGING_DIRNAME, STATE_DIRNAME, ROLLBACK_DIRNAME):
        base = root / name
        _within(base, root, name)
        _reject_symlink(base, root)
        if base.exists() and not base.is_dir():
            raise RestoreTransactionError('恢复控制路径不是目录：%s' % base)


def _norm_path(value: Any) -> str:
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))
    except (OSError, TypeError, ValueError):
        return ''


def _same_path(left: Any, right: Any) -> bool:
    return bool(_norm_path(left)) and _norm_path(left) == _norm_path(right)


def _path_has_symlink(path: Path, stop: Path) -> bool:
    current = path
    stop_norm = _norm_path(stop)
    while True:
        if _is_reparse_point(current):
            return True
        if _norm_path(current) == stop_norm:
            break
        if current == current.parent:
            break
        current = current.parent
    return False


def _state_record_error(data: Any, run_dir: Path, root: Path,
                        backup_dir: Path | None) -> str | None:
    """校验状态 schema、run_id、目标根和全部控制路径边界。"""
    if not isinstance(data, dict):
        return '状态 JSON 不是对象'
    missing = [key for key in STATE_REQUIRED_FIELDS if key not in data]
    if missing:
        return '状态缺少必需字段：%s' % ', '.join(missing)

    for key in ('run_id', 'state', 'target_root', 'staging',
                'rollback', 'pre_restore'):
        if not isinstance(data[key], str) or not data[key].strip():
            return '状态字段 %s 类型无效' % key

    run_id = data['run_id']
    if run_id != run_dir.name:
        return 'run_id 与状态目录名不一致'
    if not _RUN_ID_RE.fullmatch(run_id):
        return 'run_id 不符合生成规则'

    if data['state'] not in STATE_NAMES:
        return '未知状态：%s' % data['state']
    if not _same_path(data['target_root'], root):
        return 'target_root 与当前目标根不一致'

    targets = data['targets']
    if not isinstance(targets, dict):
        return 'targets 不是对象'
    expected_targets = {
        'root': str(root),
        'database': str(root / 'my_ai_assistant.db'),
        'chroma': str(root / 'data' / 'chroma_db'),
        'excel': str(root / 'data' / 'excel'),
    }
    for key, expected in expected_targets.items():
        actual = targets.get(key)
        if not isinstance(actual, str) or not _same_path(actual, expected):
            return 'targets.%s 与当前默认目标不一致' % key

    staging = Path(data['staging'])
    rollback = Path(data['rollback'])
    pre = Path(data['pre_restore'])
    expected_staging = root / STAGING_DIRNAME / run_id
    expected_rollback = root / ROLLBACK_DIRNAME / run_id
    if not _same_path(staging, expected_staging):
        return 'staging 不在当前 run 的 staging 根内'
    if not _same_path(rollback, expected_rollback):
        return 'rollback 不在当前 run 的 rollback 根内'
    if _path_has_symlink(staging, root):
        return 'staging 路径包含符号链接'
    if _path_has_symlink(rollback, root):
        return 'rollback 路径包含符号链接'

    if backup_dir is not None:
        expected_pre = Path(backup_dir) / ('pre_restore_' + run_id)
        if not _same_path(pre, expected_pre):
            return 'pre_restore 不在允许的备份保护目录内'
        if _path_has_symlink(pre, Path(backup_dir)):
            return 'pre_restore 路径包含符号链接'
    else:
        if not pre.is_absolute() or pre.name != 'pre_restore_' + run_id:
            return 'pre_restore 路径格式无效'
        if _path_has_symlink(pre, pre.parent):
            return 'pre_restore 路径包含符号链接'
    return None


def _scan_incomplete_runs(root: Path, backup_dir: Path | None = None) -> list[str]:
    """只读扫描未完成/损坏的恢复状态，不自动清理任何材料。"""
    problems: list[str] = []
    state_base = root / STATE_DIRNAME
    if _is_reparse_point(state_base):
        return ['%s（控制目录是符号链接/reparse point）' % state_base]
    if state_base.is_dir():
        for run_dir in sorted(state_base.iterdir()):
            if _is_reparse_point(run_dir):
                problems.append('%s（run 目录是符号链接/reparse point）' % run_dir.name)
                continue
            if not run_dir.is_dir():
                continue
            state_path = run_dir / 'state.json'
            if _is_reparse_point(state_path):
                problems.append('%s（状态文件是符号链接/reparse point）' % run_dir.name)
                continue
            if not state_path.is_file():
                problems.append('%s（run 缺少状态文件）' % run_dir.name)
                continue
            try:
                data = json.loads(state_path.read_text(encoding='utf-8'))
            except (OSError, UnicodeError, json.JSONDecodeError):
                problems.append('%s（状态文件损坏）' % run_dir.name)
                continue
            error = _state_record_error(data, run_dir, root, backup_dir)
            if error:
                problems.append('%s（%s）' % (run_dir.name, error))
                continue
            if data['state'] not in STATE_TERMINAL:
                problems.append('%s（状态：%s）' % (run_dir.name, data['state']))
    for base_name, label in ((STAGING_DIRNAME, 'staging'),
                             (ROLLBACK_DIRNAME, 'rollback')):
        base = root / base_name
        if _is_reparse_point(base):
            problems.append('%s（控制目录是符号链接/reparse point）' % base)
            continue
        if not base.is_dir():
            continue
        for run_dir in base.iterdir():
            if _is_reparse_point(run_dir):
                problems.append('%s（%s 是符号链接/reparse point）' % (run_dir.name, label))
                continue
            if not run_dir.is_dir():
                continue
            state_path = state_base / run_dir.name / 'state.json'
            if not state_path.is_file():
                problems.append('%s（%s 无状态文件）' % (run_dir.name, label))
    return problems


def _reject_non_default_source_layout(manifest: dict[str, Any]) -> None:
    """B1-a 不实现自定义目标映射；非默认来源布局必须拒绝。"""
    origins = manifest.get('path_sources')
    if origins is not None:
        if not isinstance(origins, dict):
            raise BackupValidationError('manifest.path_sources 结构无效', EXIT_PATH_MAPPING)
        for key in ('source_root', 'database', 'vector_database', 'excel_artifacts'):
            if origins.get(key, 'default') != 'default':
                raise BackupValidationError(
                    '备份来源含自定义路径（%s）；B1-a 未实现目标路径映射，拒绝恢复到默认目录' % key,
                    EXIT_PATH_MAPPING)

    source_paths = manifest.get('source_paths')
    if source_paths is None:
        return
    if not isinstance(source_paths, dict):
        raise BackupValidationError('manifest.source_paths 结构无效', EXIT_PATH_MAPPING)
    source_root = source_paths.get('source_root') or manifest.get('app_root')
    if not source_root:
        raise BackupValidationError(
            'manifest 缺少 source_root，无法确认默认布局',
            EXIT_PATH_MAPPING)

    expected = {
        'database_src': os.path.join(str(source_root), 'my_ai_assistant.db'),
        'chroma_src': os.path.join(str(source_root), 'data', 'chroma_db'),
        'excel_src': os.path.join(str(source_root), 'data', 'excel'),
    }
    for key, wanted in expected.items():
        actual = source_paths.get(key)
        if actual is None:
            raise BackupValidationError(
                'manifest.source_paths 缺少 %s，拒绝恢复' % key, EXIT_PATH_MAPPING)
        if os.path.normcase(os.path.normpath(str(actual))) != os.path.normcase(
                os.path.normpath(wanted)):
            raise BackupValidationError(
                '备份来源不是默认布局（%s）；B1-a 不自动重映射，拒绝恢复' % key,
                EXIT_PATH_MAPPING)


def _validate_target_types(root: Path) -> None:
    if _is_reparse_point(root):
        raise RestoreTransactionError('目标根是符号链接/reparse point：%s' % root)
    db = root / 'my_ai_assistant.db'
    if db.exists() and (_is_reparse_point(db) or not db.is_file()):
        raise RestoreTransactionError('SQLite 目标不是普通文件：%s' % db)
    data_dir = root / 'data'
    if data_dir.exists() and (_is_reparse_point(data_dir) or not data_dir.is_dir()):
        raise RestoreTransactionError('data 目标不是安全目录：%s' % data_dir)
    for sub in ('chroma_db', 'excel'):
        target = data_dir / sub
        if target.exists() and (_is_reparse_point(target) or not target.is_dir()):
            raise RestoreTransactionError('%s 目标不是安全目录：%s' % (sub, target))


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob('*'):
        if item.is_file():
            total += item.stat().st_size
    return total


def _zip_uncompressed_size(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        return sum(info.file_size for info in zf.infolist() if not info.is_dir())


def _check_disk_space(root: Path, validated: dict[str, tuple[dict, Path]]) -> None:
    """在所有目标修改前做保守空间检查。"""
    new_size = validated['sqlite_database'][1].stat().st_size
    new_size += _zip_uncompressed_size(validated['vector_database'][1])
    new_size += _zip_uncompressed_size(validated['excel_artifacts'][1])
    old_size = 0
    cur_db = root / 'my_ai_assistant.db'
    for suffix in ('', '-wal', '-shm'):
        item = Path(str(cur_db) + suffix)
        if item.is_file():
            old_size += item.stat().st_size
    old_size += _dir_size(root / 'data' / 'chroma_db')
    old_size += _dir_size(root / 'data' / 'excel')
    free = shutil.disk_usage(str(root)).free
    needed = new_size + old_size + 64 * 1024 * 1024
    if free < needed:
        raise RestoreTransactionError(
            '磁盘空间不足：需要约 %d bytes，可用 %d bytes' % (needed, free))


def _zip_tree_to(src: Path, dst: Path) -> None:
    with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, _dirs, files in os.walk(src):
            for name in files:
                fp = Path(dirpath) / name
                zf.write(fp, fp.relative_to(src.parent))


def _sqlite_integrity(path: Path) -> None:
    if not path.is_file():
        raise RestoreTransactionError('SQLite 文件不存在：%s' % path)
    con = sqlite3.connect('file:%s?mode=ro' % path.as_posix(), uri=True)
    try:
        row = con.execute('PRAGMA integrity_check').fetchone()
        if not row or row[0] != 'ok':
            raise RestoreTransactionError('SQLite 完整性检查失败：%s' % path)
    except sqlite3.Error as exc:
        raise RestoreTransactionError('SQLite 无法打开或校验：%s' % path) from exc
    finally:
        con.close()


def _snapshot_pre_restore(pre_dir: Path, root: Path) -> None:
    if pre_dir.exists():
        raise RestoreTransactionError('pre_restore 目录已存在：%s' % pre_dir)
    pre_dir.mkdir(parents=True, exist_ok=False)
    cur_db = root / 'my_ai_assistant.db'
    if cur_db.is_file():
        shutil.copy2(cur_db, pre_dir / 'my_ai_assistant.db')
    for suffix in ('-wal', '-shm'):
        src = Path(str(cur_db) + suffix)
        if src.is_file():
            shutil.copy2(src, pre_dir / (cur_db.name + suffix))
    for sub in ('chroma_db', 'excel'):
        cur = root / 'data' / sub
        if cur.is_dir():
            if _is_reparse_point(cur):
                raise RestoreTransactionError('拒绝符号链接/reparse point 目标目录：%s' % cur)
            _zip_tree_to(cur, pre_dir / ('%s.zip' % sub))


def _extract_zip_verified(zip_path: Path, staging: Path, label: str) -> dict[str, dict[str, Any]]:
    expected: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if not _safe_zip_member(info.filename):
                raise RestoreTransactionError('%s ZIP 成员路径不安全：%r' % (label, info.filename))
            dest = (staging / info.filename).resolve()
            _within(dest, staging, '%s staging' % label)
            dest.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            size = 0
            with zf.open(info) as source, open(dest, 'wb') as target:
                for chunk in iter(lambda: source.read(1 << 20), b''):
                    target.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
            if size != info.file_size:
                raise RestoreTransactionError('%s ZIP 成员大小不一致：%s' % (label, info.filename))
            expected[info.filename] = {'size': size, 'sha256': h.hexdigest()}
    if not expected:
        raise RestoreTransactionError('%s 备份目录为空，拒绝恢复' % label)
    return expected


def _verify_staged(staged: dict[str, Any], state: dict[str, Any]) -> None:
    db = Path(staged['database'])
    if not db.is_file():
        raise RestoreTransactionError('staging 数据库不存在')
    if sha256_file(db) != staged['database_sha256']:
        raise RestoreTransactionError('staging 数据库 SHA256 不匹配')
    _sqlite_integrity(db)

    for key, dirname in (('chroma', 'chroma_db'), ('excel', 'excel')):
        base = Path(staged['staging']) / dirname
        if not base.is_dir():
            raise RestoreTransactionError('staging %s 目录不存在' % key)
        actual: dict[str, dict[str, Any]] = {}
        for item in base.rglob('*'):
            if item.is_file():
                rel = item.relative_to(Path(staged['staging'])).as_posix()
                actual[rel] = {
                    'size': item.stat().st_size,
                    'sha256': sha256_file(item),
                }
        if actual != staged[key]:
            raise RestoreTransactionError('staging %s 文件清单/大小/SHA256 不一致' % key)


def _stage_inputs(state: dict[str, Any], validated: dict[str, tuple[dict, Path]]) -> dict[str, Any]:
    staging = Path(state['staging'])
    _within(staging, Path(state['target_root']), 'staging')
    _reject_symlink(staging, Path(state['target_root']))
    staging.mkdir(parents=True, exist_ok=False)
    db_dir = staging / 'database'
    db_dir.mkdir()
    staged_db = db_dir / 'my_ai_assistant.db'
    shutil.copy2(validated['sqlite_database'][1], staged_db)
    db_sha = sha256_file(staged_db)
    if db_sha != validated['sqlite_database'][0]['sha256']:
        raise RestoreTransactionError('staging 数据库 SHA256 不匹配')
    _sqlite_integrity(staged_db)

    chroma = _extract_zip_verified(validated['vector_database'][1], staging, 'Chroma')
    excel = _extract_zip_verified(validated['excel_artifacts'][1], staging, 'Excel')
    staged = {
        'staging': str(staging),
        'database': str(staged_db),
        'database_sha256': db_sha,
        'chroma': chroma,
        'excel': excel,
    }
    _atomic_write_json(staging / 'stage_manifest.json', staged)
    return staged


def _replace_file(src: Path, dst: Path) -> None:
    """同卷文件替换；不用于目录，不使用跨卷 copy。"""
    os.replace(str(src), str(dst))


def _same_volume(src: Path, dst: Path) -> bool:
    return src.resolve().drive.lower() == dst.resolve().drive.lower()


def _rename_dir(src: Path, dst: Path) -> None:
    if not src.is_dir() or _is_reparse_point(src):
        raise RestoreTransactionError('目录切换源无效：%s' % src)
    if dst.exists():
        raise RestoreTransactionError('目录切换目标已存在：%s' % dst)
    if not _same_volume(src, dst):
        raise RestoreTransactionError('目录切换不能跨卷：%s -> %s' % (src, dst))
    src.rename(dst)


def _remove_dir(path: Path) -> None:
    if _is_reparse_point(path):
        raise RestoreTransactionError('拒绝删除符号链接/reparse point 目录：%s' % path)
    if path.exists():
        shutil.rmtree(path)


def _switch_sqlite(state: dict[str, Any], staged: dict[str, Any],
                   root: Path) -> None:
    target = root / 'my_ai_assistant.db'
    if target.exists() and _is_reparse_point(target):
        raise RestoreTransactionError('拒绝符号链接/reparse point SQLite 目标：%s' % target)
    rollback_db = Path(state['rollback']) / 'database'
    rollback_db.mkdir(parents=True, exist_ok=True)
    for suffix in ('-wal', '-shm'):
        old = Path(str(target) + suffix)
        if old.exists():
            if _is_reparse_point(old):
                raise RestoreTransactionError('拒绝符号链接/reparse point SQLite 旁文件：%s' % old)
            _replace_file(old, rollback_db / old.name)
            _write_state(state)
    _replace_file(Path(staged['database']), target)
    state['objects']['sqlite'] = 'switched'
    _write_state(state)
    _set_state(state, 'SQLITE_SWITCHED')


def _switch_directory(state: dict[str, Any], staging: Path, target: Path,
                      rollback: Path, staging_name: str, object_name: str,
                      rollback_name: str) -> None:
    staging_dir = staging / staging_name
    rollback_dir = rollback / rollback_name
    if target.exists():
        if _is_reparse_point(target) or not target.is_dir():
            raise RestoreTransactionError('目标路径不是安全目录：%s' % target)
        _rename_dir(target, rollback_dir)
        state['objects'][object_name] = 'old_moved'
        _write_state(state)
    else:
        state['objects'][object_name] = 'no_old'
        _write_state(state)
    _rename_dir(staging_dir, target)
    state['objects'][object_name] = 'switched'
    _write_state(state)


def _switch_all(state: dict[str, Any], staged: dict[str, Any], target: dict[str, Path],
                rollback: Path) -> None:
    state['targets_touched'] = True
    _set_state(state, 'SWITCHING_SQLITE')
    _switch_sqlite(state, staged, target['root'])
    _set_state(state, 'SWITCHING_CHROMA')
    _switch_directory(state, Path(staged['staging']), target['chroma'],
                      rollback, 'chroma_db', 'chroma', 'chroma_db')
    _set_state(state, 'CHROMA_SWITCHED')
    _set_state(state, 'SWITCHING_EXCEL')
    _switch_directory(state, Path(staged['staging']), target['excel'],
                      rollback, 'excel', 'excel', 'excel')
    _set_state(state, 'EXCEL_SWITCHED')
    _set_state(state, 'SWITCHED')


def _restore_file_copy(src: Path, dst: Path) -> None:
    """复制回滚材料后再替换，保留 pre_restore 中的唯一副本。"""
    tmp = dst.with_name(dst.name + '.restore_rollback_tmp')
    if tmp.exists():
        tmp.unlink()
    shutil.copy2(src, tmp)
    _replace_file(tmp, dst)


def _restore_old_db(state: dict[str, Any], root: Path) -> None:
    target = root / 'my_ai_assistant.db'
    pre = Path(state['pre_restore'])
    pre_db = pre / 'my_ai_assistant.db'
    if pre_db.is_file():
        _restore_file_copy(pre_db, target)
        for suffix in ('-wal', '-shm'):
            old = pre / ('my_ai_assistant.db' + suffix)
            now = Path(str(target) + suffix)
            if old.is_file():
                _restore_file_copy(old, now)
            elif now.exists():
                now.unlink()
    else:
        if target.exists():
            target.unlink()
        for suffix in ('-wal', '-shm'):
            item = Path(str(target) + suffix)
            if item.exists():
                item.unlink()
    state['objects']['sqlite'] = 'rolled_back'


def _rollback(state: dict[str, Any], root: Path, target: dict[str, Path],
              rollback: Path, pre: Path) -> None:
    # 状态持久化失败时仍必须优先恢复旧数据；最终状态写入失败会由外层标记
    # FAILED_RECOVERY_REQUIRED，但不会阻止已开始的回滚。
    try:
        _set_state(state, 'ROLLING_BACK')
    except Exception:
        pass
    # 逆序恢复目录：Excel -> Chroma，最后 SQLite。
    for object_name, rb_name in (('excel', 'excel'), ('chroma', 'chroma_db')):
        rb = rollback / rb_name
        dst = target[object_name]
        if rb.exists():
            if dst.exists():
                _remove_dir(dst)
            _rename_dir(rb, dst)
            state['objects'][object_name] = 'rolled_back'
            try:
                _write_state(state)
            except Exception:
                pass
        elif state['objects'].get(object_name) in ('switched', 'no_old'):
            if dst.exists():
                _remove_dir(dst)
            state['objects'][object_name] = 'rolled_back'
            try:
                _write_state(state)
            except Exception:
                pass
    _restore_old_db(state, root)
    try:
        _write_state(state)
    except Exception:
        pass
    db = root / 'my_ai_assistant.db'
    if state.get('old_sqlite_existed'):
        _sqlite_integrity(db)
    elif db.exists():
        raise RestoreTransactionError('旧数据库原本不存在，但回滚后目标仍存在')
    _set_state(state, 'ROLLED_BACK')


def _tree_file_map(base: Path, prefix: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not base.is_dir() or _is_reparse_point(base):
        raise RestoreTransactionError('%s 目标目录不存在或不是安全目录' % prefix)
    for item in base.rglob('*'):
        if _is_reparse_point(item):
            raise RestoreTransactionError('%s 目标包含符号链接/reparse point：%s' % (prefix, item))
        if item.is_file():
            rel = item.relative_to(base).as_posix()
            key = '%s/%s' % (prefix, rel)
            result[key] = {
                'size': item.stat().st_size,
                'sha256': sha256_file(item),
            }
    return result


def _verify_restored_files(root: Path, staged: dict[str, Any]) -> None:
    """切换后核对最终目录清单、size、SHA256，防止切换后丢失或改变。"""
    db = root / 'my_ai_assistant.db'
    if not db.is_file() or _is_reparse_point(db):
        raise RestoreTransactionError('恢复后 SQLite 目标不存在或不是普通文件')
    if sha256_file(db) != staged['database_sha256']:
        raise RestoreTransactionError('恢复后 SQLite SHA256 与 staging 不一致')

    expected_chroma = staged['chroma']
    expected_excel = staged['excel']
    actual_chroma = _tree_file_map(root / 'data' / 'chroma_db', 'chroma_db')
    actual_excel = _tree_file_map(root / 'data' / 'excel', 'excel')
    if actual_chroma != expected_chroma:
        raise RestoreTransactionError('恢复后 Chroma 文件清单/size/SHA256 与 staging 不一致')
    if actual_excel != expected_excel:
        raise RestoreTransactionError('恢复后 Excel 文件清单/size/SHA256 与 staging 不一致')


def _json_object(path: Path, label: str) -> dict[str, Any]:
    if _is_reparse_point(path) or not path.is_file():
        raise RestoreTransactionError('%s 不存在或不是普通文件：%s' % (label, path))
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RestoreTransactionError('%s 无法解析：%s' % (label, path)) from exc
    if not isinstance(data, dict):
        raise RestoreTransactionError('%s 不是 JSON 对象：%s' % (label, path))
    return data


def _verify_chroma_records(collection: Any) -> int:
    """分批读取全部记录，校验生产所需的 metadata/embedding 关联。"""
    count = int(collection.count())
    if count < 0:
        raise RestoreTransactionError('Chroma collection 计数无效')
    if count == 0:
        return 0

    seen = 0
    offset = 0
    dimension: int | None = None
    while seen < count:
        batch_limit = min(CHROMA_READ_BATCH, count - seen)
        try:
            result = collection.get(
                limit=batch_limit,
                offset=offset,
                include=['metadatas', 'embeddings'],
            )
        except Exception as exc:
            raise RestoreTransactionError('Chroma 记录读取失败：%s' % exc) from exc

        ids = result.get('ids')
        metadatas = result.get('metadatas')
        embeddings = result.get('embeddings')
        if not isinstance(ids, list) or not ids:
            raise RestoreTransactionError('Chroma 记录读取结果为空')
        if metadatas is None or embeddings is None:
            raise RestoreTransactionError('Chroma 记录缺少 metadata/embedding 列表')
        try:
            id_count = len(ids)
            meta_count = len(metadatas)
            emb_count = len(embeddings)
        except TypeError as exc:
            raise RestoreTransactionError('Chroma 记录列表无法读取长度') from exc
        if not (id_count == meta_count == emb_count):
            raise RestoreTransactionError('Chroma ID/metadata/embedding 数量不一致')

        for record_id, metadata, embedding in zip(ids, metadatas, embeddings):
            if not isinstance(record_id, str) or not record_id:
                raise RestoreTransactionError('Chroma 记录 ID 无效')
            if not isinstance(metadata, dict):
                raise RestoreTransactionError('Chroma 记录缺少 metadata')
            document_id = metadata.get('document_id')
            user_id = metadata.get('user_id')
            if not isinstance(document_id, str) or not document_id.strip():
                raise RestoreTransactionError('Chroma 记录缺少有效 document_id')
            if not isinstance(user_id, int) or isinstance(user_id, bool):
                raise RestoreTransactionError('Chroma 记录缺少有效 user_id')
            if embedding is None:
                raise RestoreTransactionError('Chroma 记录缺少 embedding')
            try:
                emb_len = len(embedding)
            except TypeError as exc:
                raise RestoreTransactionError('Chroma embedding 不是序列') from exc
            if emb_len <= 0:
                raise RestoreTransactionError('Chroma embedding 为空')
            if dimension is None:
                dimension = emb_len
            elif emb_len != dimension:
                raise RestoreTransactionError('Chroma embedding 维度不一致')

        seen += len(ids)
        offset += len(ids)

    if seen != count:
        raise RestoreTransactionError(
            'Chroma 记录数与 collection count 不一致：%d != %d' % (seen, count))
    return seen


def _verify_chroma(root: Path) -> None:
    chroma = root / 'data' / 'chroma_db'
    if not chroma.is_dir() or _is_reparse_point(chroma):
        raise RestoreTransactionError('恢复后 Chroma 目录不存在或不是安全目录')
    chroma_db = chroma / CHROMA_SQLITE_NAME
    if _is_reparse_point(chroma_db) or not chroma_db.is_file():
        raise RestoreTransactionError('恢复后 Chroma 缺少 %s' % CHROMA_SQLITE_NAME)
    if chroma_db.stat().st_size <= 0:
        raise RestoreTransactionError('恢复后 Chroma 数据库为空文件')
    _sqlite_integrity(chroma_db)

    try:
        import chromadb
    except ImportError as exc:
        raise RestoreTransactionError('无法导入 chromadb，不能验证 Chroma 数据') from exc

    client = None
    collection = None
    try:
        # PersistentClient 只在已存在 chroma.sqlite3 后打开；不调用 get_or_create。
        client = chromadb.PersistentClient(path=str(chroma))
        try:
            collection = client.get_collection(CHROMA_COLLECTION_NAME)
        except Exception as exc:
            raise RestoreTransactionError(
                'Chroma collection 不存在或无法访问：%s' % CHROMA_COLLECTION_NAME) from exc
        _verify_chroma_records(collection)
    except RestoreTransactionError:
        raise
    except Exception as exc:
        raise RestoreTransactionError('Chroma 无法打开或验证：%s' % exc) from exc
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        del collection
        del client
        gc.collect()


def _verify_excel(root: Path) -> None:
    excel = root / 'data' / 'excel'
    if not excel.is_dir() or _is_reparse_point(excel):
        raise RestoreTransactionError('恢复后 Excel 目录不存在或不是安全目录')

    entries = list(excel.iterdir())
    if not entries:
        # 合法空 Excel 状态：本轮不改变 B2 是否允许生成空备份；
        # 如果未来 manifest 明确支持空数据集，这里允许空目标目录。
        return

    for doc_dir in entries:
        if _is_reparse_point(doc_dir) or not doc_dir.is_dir():
            raise RestoreTransactionError('Excel 根存在非文档文件或符号链接/reparse point：%s' % doc_dir)
        meta = _json_object(doc_dir / 'meta.json', 'Excel meta.json')
        rep = _json_object(doc_dir / 'representation.json', 'Excel representation.json')

        required_meta = (
            'document_id', 'user_id', 'filename', 'file_type',
            'original_path', 'representation_path',
        )
        missing = [key for key in required_meta if key not in meta]
        if missing:
            raise RestoreTransactionError(
                'Excel meta.json 缺少字段：%s' % ', '.join(missing))
        if meta.get('document_id') != doc_dir.name:
            raise RestoreTransactionError('Excel meta document_id 与目录不一致')
        meta_user_id = meta.get('user_id')
        if meta_user_id is not None and (
                not isinstance(meta_user_id, int) or isinstance(meta_user_id, bool)):
            raise RestoreTransactionError('Excel meta user_id 类型无效')
        if not isinstance(meta.get('filename'), str) or not meta['filename'].strip():
            raise RestoreTransactionError('Excel meta filename 无效')
        if not isinstance(meta.get('file_type'), str) or not meta['file_type'].strip():
            raise RestoreTransactionError('Excel meta file_type 无效')
        if not isinstance(meta.get('original_path'), str) or not meta['original_path'].strip():
            raise RestoreTransactionError('Excel meta original_path 无效')
        if not isinstance(meta.get('representation_path'), str) or not meta['representation_path'].strip():
            raise RestoreTransactionError('Excel meta representation_path 无效')
        if Path(meta['representation_path']).name != 'representation.json':
            raise RestoreTransactionError('Excel meta representation_path 未指向 representation.json')

        originals = [
            item for item in doc_dir.iterdir()
            if item.is_file() and not _is_reparse_point(item)
            and item.name.startswith('original.')
        ]
        if not originals:
            raise RestoreTransactionError('Excel 文档缺少 original.<ext>：%s' % doc_dir)
        original_names = {item.name for item in originals}
        if Path(meta['original_path']).name not in original_names:
            raise RestoreTransactionError('Excel meta original_path 与实际 original 文件不一致')
        if any(item.stat().st_size <= 0 for item in originals):
            raise RestoreTransactionError('Excel original 文件为空：%s' % doc_dir)

        for key in ('document_id', 'user_id', 'filename', 'file_type', 'sheets'):
            if key not in rep:
                raise RestoreTransactionError('Excel representation.json 缺少字段：%s' % key)
        if rep.get('document_id') != doc_dir.name:
            raise RestoreTransactionError('Excel representation document_id 与目录不一致')
        if rep.get('user_id') != meta_user_id:
            raise RestoreTransactionError('Excel representation 与 meta 的 user_id 不一致')
        if rep.get('filename') != meta.get('filename'):
            raise RestoreTransactionError('Excel representation 与 meta 的 filename 不一致')
        if rep.get('file_type') != meta.get('file_type'):
            raise RestoreTransactionError('Excel representation 与 meta 的 file_type 不一致')
        sheets = rep.get('sheets')
        if not isinstance(sheets, list):
            raise RestoreTransactionError('Excel representation.sheets 不是列表')
        for sheet in sheets:
            if not isinstance(sheet, dict):
                raise RestoreTransactionError('Excel representation.sheets 成员不是对象')
            if not isinstance(sheet.get('sheet_name'), str):
                raise RestoreTransactionError('Excel sheet_name 无效')
            if not isinstance(sheet.get('columns'), list):
                raise RestoreTransactionError('Excel sheet columns 不是列表')
            if not isinstance(sheet.get('rows'), list):
                raise RestoreTransactionError('Excel sheet rows 不是列表')
        if 'sheet_count' in meta and meta['sheet_count'] != len(sheets):
            raise RestoreTransactionError('Excel meta sheet_count 与 representation 不一致')


def _verify_restored(root: Path, staged: dict[str, Any] | None = None) -> None:
    if staged is not None:
        _verify_restored_files(root, staged)
    _verify_chroma(root)
    _verify_excel(root)


def _cleanup_staging(staging: Path, root: Path,
                     state: dict[str, Any] | None) -> bool:
    """只清理当前 run 且状态文件可验证的 staging。"""
    try:
        if not isinstance(state, dict):
            raise RestoreTransactionError('缺少当前 run 状态，拒绝清理 staging')
        run_id = state.get('run_id')
        state_path_value = state.get('state_path')
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise RestoreTransactionError('run_id 无效，拒绝清理 staging')
        if not isinstance(state_path_value, str) or not state_path_value:
            raise RestoreTransactionError('缺少 state_path，拒绝清理 staging')
        state_path = Path(state_path_value)
        expected_staging = root / STAGING_DIRNAME / run_id
        expected_state = root / STATE_DIRNAME / run_id / 'state.json'
        if not _same_path(staging, expected_staging) or staging.name != run_id:
            raise RestoreTransactionError('staging 不是当前 run 的目录')
        if not _same_path(state_path, expected_state):
            raise RestoreTransactionError('state_path 不是当前 run 的状态文件')
        if _is_reparse_point(staging):
            raise RestoreTransactionError('staging 是符号链接/reparse point')
        if _is_reparse_point(state_path) or not state_path.is_file():
            raise RestoreTransactionError('当前 run 状态文件不存在或不是普通文件')
        try:
            record = json.loads(state_path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RestoreTransactionError('当前 run 状态文件无法读取') from exc
        error = _state_record_error(
            record, root / STATE_DIRNAME / run_id, root,
            Path(record.get('pre_restore')).parent
            if isinstance(record, dict) and isinstance(record.get('pre_restore'), str)
            else None,
        )
        if error:
            raise RestoreTransactionError('当前 run 状态无效：%s' % error)
        if record.get('run_id') != run_id:
            raise RestoreTransactionError('状态 run_id 不匹配')
        _within(staging, root, 'staging')
        _reject_symlink(staging, root)
        if not staging.exists():
            return True
        if not staging.is_dir():
            raise RestoreTransactionError('staging 不是目录')
        shutil.rmtree(staging)
        return True
    except Exception as exc:  # noqa: BLE001 - 清理失败不能覆盖主恢复结论
        print('警告：拒绝/失败清理 staging，请手工检查：%s（%s）' % (staging, exc))
        return False


class RestoreLockError(Exception):
    """恢复锁无法安全获取或释放。"""


class RestoreLockBusy(RestoreLockError):
    """同一或重叠目标已有恢复进程持锁。"""


class _FileLock:
    """进程级文件锁，不删除锁文件；Windows 使用原生句柄锁。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None
        self._fd = None
        self._overlapped = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(self.path.parent) or _is_reparse_point(self.path):
            raise RestoreLockError('锁路径包含符号链接/reparse point：%s' % self.path)
        if sys.platform == 'win32':
            return self._acquire_windows()
        return self._acquire_posix()

    def _acquire_windows(self) -> bool:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        generic_read = 0x80000000
        generic_write = 0x40000000
        open_always = 4
        file_attribute_normal = 0x80
        lockfile_exclusive = 0x00000002
        lockfile_fail_immediately = 0x00000001
        invalid_handle = ctypes.c_void_p(-1).value

        class _Overlapped(ctypes.Structure):
            _fields_ = [
                ('Internal', ctypes.c_void_p),
                ('InternalHigh', ctypes.c_void_p),
                ('Offset', wintypes.DWORD),
                ('OffsetHigh', wintypes.DWORD),
                ('hEvent', wintypes.HANDLE),
            ]

        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.LockFileEx.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
        ]
        kernel32.LockFileEx.restype = wintypes.BOOL
        kernel32.UnlockFileEx.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, ctypes.POINTER(_Overlapped),
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateFileW(
            str(self.path), generic_read | generic_write, 0, None,
            open_always, file_attribute_normal, None)
        if handle == invalid_handle:
            error = ctypes.get_last_error()
            if error in (32, 33, 158):
                return False
            raise RestoreLockError('无法打开锁文件：%s（WinError %s）' % (
                self.path, error))
        overlapped = _Overlapped()
        ok = kernel32.LockFileEx(
            handle, lockfile_exclusive | lockfile_fail_immediately,
            0, 1, 0, ctypes.byref(overlapped))
        if not ok:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            if error in (33, 158):
                return False
            raise RestoreLockError('无法获取锁：%s（WinError %s）' % (
                self.path, error))
        self._handle = handle
        self._overlapped = overlapped
        return True

    def _acquire_posix(self) -> bool:
        import fcntl
        import errno
        try:
            self._fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise RestoreLockError('无法打开锁文件：%s（%s）' % (self.path, exc)) from exc
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._fd)
            self._fd = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise RestoreLockError('无法获取锁：%s（%s）' % (self.path, exc)) from exc
        return True

    def release(self) -> None:
        if self._handle is not None:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.UnlockFileEx.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                wintypes.DWORD, ctypes.c_void_p,
            ]
            kernel32.UnlockFileEx(
                self._handle, 0, 1, 0,
                ctypes.byref(self._overlapped) if self._overlapped else None)
            kernel32.CloseHandle(self._handle)
            self._handle = None
            self._overlapped = None
        if self._fd is not None:
            import fcntl
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def _default_registry_root() -> Path:
    base = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
    return Path(base) / REGISTRY_DIRNAME


def _canonical_root(root: Path) -> str:
    if not root.is_dir() or _is_reparse_point(root):
        raise RestoreLockError('目标根不存在或不是安全目录：%s' % root)
    return os.path.normcase(os.path.normpath(str(root.resolve())))


def _roots_overlap(left: str, right: str) -> bool:
    left = os.path.normcase(os.path.normpath(left))
    right = os.path.normcase(os.path.normpath(right))
    left_drive = os.path.splitdrive(left)[0].lower()
    right_drive = os.path.splitdrive(right)[0].lower()
    if left_drive != right_drive:
        return False
    try:
        common = os.path.commonpath([left, right])
    except ValueError:
        return True
    return os.path.normcase(os.path.normpath(common)) in (left, right)


class RestoreLock:
    """目标根级恢复互斥锁；全局注册表只用于嵌套/重叠目标检查。"""

    def __init__(self, root: Path, registry_root: Path | None = None):
        self.root = Path(root).resolve()
        self.canonical_root = _canonical_root(self.root)
        self.lock_path = self.root / LOCK_DIRNAME / LOCK_FILENAME
        self.registry_root = Path(
            registry_root or _default_registry_root()).resolve()
        self._lock: _FileLock | None = None

    def _registry_path(self) -> Path:
        return self.registry_root / 'registry.json'

    def _read_registry(self) -> list[str]:
        path = self._registry_path()
        if not path.exists():
            return []
        if _is_reparse_point(path):
            raise RestoreLockError('恢复锁注册表是符号链接/reparse point')
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RestoreLockError('恢复锁注册表损坏') from exc
        if not isinstance(data, list) or not all(
                isinstance(item, str) for item in data):
            raise RestoreLockError('恢复锁注册表结构无效')
        return data

    def _write_registry(self, entries: list[str]) -> None:
        self.registry_root.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(self.registry_root):
            raise RestoreLockError('恢复锁注册表目录是符号链接/reparse point')
        _atomic_write_json(self._registry_path(), entries)

    def _probe_stale(self, entry: str) -> bool:
        entry_root = Path(entry)
        if not entry_root.is_dir():
            return True
        if _is_reparse_point(entry_root):
            raise RestoreLockError('重叠目标根是 reparse point：%s' % entry)
        other = _FileLock(entry_root / LOCK_DIRNAME / LOCK_FILENAME)
        if other.acquire():
            other.release()
            return True
        return False

    def acquire(self) -> None:
        if self._lock is not None:
            raise RestoreLockError('当前进程已持有恢复锁')
        lock = _FileLock(self.lock_path)
        if not lock.acquire():
            raise RestoreLockBusy(
                '同一目标已有恢复进程持锁：%s' % self.root)
        guard = _FileLock(self.registry_root / 'registry.lock')
        try:
            if not guard.acquire():
                raise RestoreLockBusy('恢复锁注册表正被其他恢复进程使用')
            try:
                entries = self._read_registry()
                retained: list[str] = []
                for entry in entries:
                    if os.path.normcase(os.path.normpath(entry)) == (
                            self.canonical_root):
                        # 当前进程已持有同名目标锁，注册表条目只能是上次崩溃残留。
                        continue
                    if not _roots_overlap(entry, self.canonical_root):
                        retained.append(entry)
                        continue
                    if self._probe_stale(entry):
                        continue
                    raise RestoreLockBusy(
                        '目标与正在恢复的目录重叠：%s' % entry)
                retained.append(self.canonical_root)
                self._write_registry(retained)
            finally:
                guard.release()
        except Exception:
            lock.release()
            raise
        self._lock = lock

    def release(self) -> None:
        if self._lock is None:
            return
        try:
            guard = _FileLock(self.registry_root / 'registry.lock')
            if guard.acquire():
                try:
                    entries = self._read_registry()
                    self._write_registry([
                        entry for entry in entries
                        if os.path.normcase(os.path.normpath(entry))
                        != self.canonical_root
                    ])
                finally:
                    guard.release()
            else:
                print('警告：无法获取恢复锁注册表锁，注册表条目可能残留。')
        except Exception as exc:  # noqa: BLE001
            print('警告：释放恢复锁注册表条目失败：%s' % exc)
        finally:
            self._lock.release()
            self._lock = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def _build_state(root: Path, src: Path, backup_dir: Path, run_id: str) -> dict[str, Any]:
    _validate_control_paths(root)
    staging = root / STAGING_DIRNAME / run_id
    state_dir = root / STATE_DIRNAME / run_id
    state_dir.mkdir(parents=True, exist_ok=False)
    rollback = root / ROLLBACK_DIRNAME / run_id
    state = {
        'run_id': run_id,
        'state': 'INIT',
        'backup_source': str(src),
        'target_root': str(root),
        'staging': str(staging),
        'rollback': str(rollback),
        'pre_restore': str(backup_dir / ('pre_restore_' + run_id)),
        'targets': {
            'root': str(root),
            'database': str(root / 'my_ai_assistant.db'),
            'chroma': str(root / 'data' / 'chroma_db'),
            'excel': str(root / 'data' / 'excel'),
        },
        'objects': {'sqlite': 'pending', 'chroma': 'pending', 'excel': 'pending'},
        'targets_touched': False,
        'old_sqlite_existed': (root / 'my_ai_assistant.db').is_file(),
        'started_at': _now(),
        'updated_at': _now(),
        'error': None,
        'state_path': str(state_dir / 'state.json'),
    }
    _write_state(state)
    return state


def _execute(state: dict[str, Any], root: Path, validated: dict[str, tuple[dict, Path]],
             backup_dir: Path) -> None:
    _set_state(state, 'PREFLIGHT_OK')
    _validate_target_types(root)
    _check_disk_space(root, validated)
    _set_state(state, 'STAGING')
    staged = _stage_inputs(state, validated)
    _verify_staged(staged, state)
    _set_state(state, 'STAGING_VALIDATED')
    _snapshot_pre_restore(Path(state['pre_restore']), root)
    Path(state['rollback']).mkdir(parents=True, exist_ok=False)
    _set_state(state, 'TARGETS_BACKED_UP')
    targets = {
        'root': root,
        'database': root / 'my_ai_assistant.db',
        'chroma': root / 'data' / 'chroma_db',
        'excel': root / 'data' / 'excel',
    }
    _switch_all(state, staged, targets, Path(state['rollback']))
    _set_state(state, 'VERIFYING')
    _verify_restored(root, staged)
    _set_state(state, 'COMMITTED')


def _handle_failure(state: dict[str, Any], root: Path,
                    exc: Exception) -> int:
    error = '%s: %s' % (type(exc).__name__, exc)
    if state.get('targets_touched'):
        try:
            targets = {
                'root': root,
                'database': root / 'my_ai_assistant.db',
                'chroma': root / 'data' / 'chroma_db',
                'excel': root / 'data' / 'excel',
            }
            _rollback(state, root, targets, Path(state['rollback']),
                      Path(state['pre_restore']))
        except Exception as rollback_exc:  # noqa: BLE001
            state['error'] = '%s；回滚失败：%s: %s' % (
                error, type(rollback_exc).__name__, rollback_exc)
            try:
                _set_state(state, 'FAILED_RECOVERY_REQUIRED', state['error'])
            except Exception:
                pass
            print('恢复失败且自动回滚失败，必须人工处理。')
            print('状态目录：%s' % Path(state['state_path']).parent)
            print('pre_restore：%s' % state['pre_restore'])
            print('rollback：%s' % state['rollback'])
            return EXIT_TRANSACTION
        try:
            _set_state(state, 'ROLLED_BACK', error)
        except Exception:
            pass
        _cleanup_staging(Path(state['staging']), root, state)
        print('恢复失败，已自动回滚：%s' % error)
        return EXIT_TRANSACTION

    try:
        _set_state(state, 'ROLLED_BACK', error)
    except Exception:
        pass
    _cleanup_staging(Path(state['staging']), root, state)
    print('恢复失败，目标未修改：%s' % error)
    return EXIT_TRANSACTION


def main() -> int:
    ap = argparse.ArgumentParser(description='从备份集恢复业务库 / 向量库 / Excel 原件')
    ap.add_argument('--from', dest='src', required=True, help='备份集目录')
    ap.add_argument('--target-root', default=str(REPO_ROOT),
                    help='恢复到哪个项目根（默认当前仓库根；演练时可指定临时目录）')
    ap.add_argument('--yes', action='store_true', help='确认执行（必需）')
    ap.add_argument('--backup-dir', default=str(REPO_ROOT / 'backups'),
                    help='pre_restore 快照与备份集所在目录')
    args = ap.parse_args()

    src = Path(args.src).resolve()
    root = Path(args.target_root).resolve()
    if not src.is_dir():
        print('备份集目录不存在：%s' % src)
        return EXIT_INVALID_BACKUP
    manifest_path = src / 'manifest.json'
    if not manifest_path.is_file():
        print('备份集缺少 manifest.json（不是本工具产出的备份）：%s' % src)
        return EXIT_INVALID_BACKUP
    if not root.is_dir() or _is_reparse_point(root):
        print('恢复目标根不存在或不是安全目录：%s' % root)
        return EXIT_PATH_MAPPING

    try:
        manifest = _load_manifest(manifest_path)
        validated = validate_backup_set(src, manifest)
        _reject_non_default_source_layout(manifest)
    except BackupValidationError as exc:
        print('预检失败：%s' % exc)
        return exc.code

    _db_item, db_file = validated['sqlite_database']
    print('备份集      ：%s（%s）' % (
        manifest.get('name', '(未命名)'), manifest.get('created_at')))
    print('源项目根    ：%s' % manifest.get('app_root'))
    print('恢复目标    ：%s' % root)
    print('包含项      ：%s' % ', '.join(sorted(validated)))

    if not args.yes:
        print('\n这是预演（未加 --yes，未做任何写入）。确认后请追加 --yes。')
        return 0

    target_handle = None
    lock = None
    run_id = _new_run_id()
    try:
        try:
            target_handle = open_secure_directory(root)
        except (PathSecurityError, OSError) as exc:
            print('恢复目标安全验证失败：%s' % exc)
            return EXIT_PATH_MAPPING

        registry_root = os.environ.get(RESTORE_REGISTRY_ROOT_ENV)
        try:
            lock = RootLock.from_target_handle(
                target_handle,
                registry_root=(Path(registry_root) if registry_root else None),
            )
            lock.acquire(run_id)
        except (RegistryConflictError, RegistryLockBusy,
                RegistryLockError, RootLockError, PathSecurityError) as exc:
            print('无法获取恢复锁：%s' % exc)
            print('同一或重叠目标已有恢复进程，本次未修改任何目标数据。')
            return EXIT_LOCKED
        except Exception as exc:  # noqa: BLE001 - 锁初始化失败必须 fail-closed
            print('恢复锁初始化失败：%s' % exc)
            return EXIT_LOCKED

        try:
            _validate_target_types(root)
        except RestoreTransactionError as exc:
            print('恢复前检查失败：%s' % exc)
            return EXIT_TRANSACTION
        except Exception as exc:  # noqa: BLE001
            print('恢复前检查失败：%s' % exc)
            return EXIT_TRANSACTION

        incomplete = _scan_incomplete_runs(root, Path(args.backup_dir).resolve())
        if incomplete:
            print('检测到未完成或未知的恢复 run，拒绝开始新的恢复：')
            for item in incomplete:
                print('  - %s' % item)
            print('请先人工检查 .restore_state / .restore_staging / .restore_rollback。')
            return EXIT_INCOMPLETE_RUN

        try:
            state = _build_state(root, src, Path(args.backup_dir).resolve(), run_id)
            _execute(state, root, validated, Path(args.backup_dir).resolve())
            _cleanup_staging(Path(state['staging']), root, state)
        except Exception as exc:  # noqa: BLE001
            state = locals().get('state')
            if not isinstance(state, dict):
                print('恢复失败：%s' % exc)
                return EXIT_TRANSACTION
            return _handle_failure(state, root, exc)

        print('\n恢复完成（逐对象切换，非全局原子事务）。')
        print('恢复后校验：SQLite integrity_check、Chroma 文件、Excel JSON 已通过。')
        print('旧数据保留：pre_restore=%s；rollback=%s' % (
            state['pre_restore'], state['rollback']))
        print('请启动后端并确认：登录、知识库文档列表、Excel 预览、历史卡片均正常。')
        return 0
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception as exc:  # noqa: BLE001
                print('警告：释放恢复锁失败：%s' % exc)
        if target_handle is not None:
            target_handle.close()


if __name__ == '__main__':
    sys.exit(main())
