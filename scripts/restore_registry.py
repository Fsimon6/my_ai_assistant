# -*- coding: utf-8 -*-
"""Single-user restore registry and path-overlap detection.

Scope:
- first version: one Windows user, local NTFS
- registry under %LOCALAPPDATA% when used by default
- no ProgramData / cross-user registry
- no restore_db integration, no Root Lock integration, no stale auto-recovery
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from restore_path_security import (
    DirectoryHandle,
    DirectoryIdentity,
    open_secure_directory,
)
from restore_win32_native import (
    DELETE,
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_CREATE,
    FILE_OPEN,
    FILE_OPEN_REPARSE_POINT,
    FILE_NON_DIRECTORY_FILE,
    FILE_SYNCHRONOUS_IO_NONALERT,
    FILE_OPEN_IF,
    FILE_READ_ATTRIBUTES,
    FILE_READ_DATA,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    FILE_SHARE_DELETE,
    FILE_WRITE_DATA,
    GENERIC_READ,
    GENERIC_WRITE,
    SYNCHRONIZE,
    close_handle,
    delete_handle,
    flush_handle,
    get_attributes,
    get_file_id,
    nt_create_file,
    read_all,
    rename_relative,
    status_message,
    write_all,
)

SCHEMA_VERSION = 1
REGISTRY_FILENAME = 'registry.json'
REGISTRY_LOCK_FILENAME = 'registry.lock'
DEFAULT_REGISTRY_DIRNAME = 'my_ai_assistant_restore_registry'
_FILE_ID_RE = re.compile(r'^[0-9a-fA-F]{32}$')
_ENTRY_KEYS = (
    'run_id', 'target_identity', 'canonical_path', 'pid',
    'process_start_utc', 'user_sid', 'state', 'created_at',
)
_IDENTITY_KEYS = ('volume_serial', 'file_id', 'filesystem')
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_ID_INFO_CLASS = 18
_VOLUME_NAME_DOS = 0x00000000
_FILE_NAME_NORMALIZED = 0x00000000
_LOCKFILE_EXCLUSIVE = 0x00000002
_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [('Identifier', ctypes.c_ubyte * 16)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [
        ('VolumeSerialNumber', ctypes.c_ulonglong),
        ('FileId', _FILE_ID_128),
    ]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('dwFileAttributes', wintypes.DWORD),
        ('ftCreationTime', wintypes.FILETIME),
        ('ftLastAccessTime', wintypes.FILETIME),
        ('ftLastWriteTime', wintypes.FILETIME),
        ('dwVolumeSerialNumber', wintypes.DWORD),
        ('nFileSizeHigh', wintypes.DWORD),
        ('nFileSizeLow', wintypes.DWORD),
        ('nNumberOfLinks', wintypes.DWORD),
        ('nFileIndexHigh', wintypes.DWORD),
        ('nFileIndexLow', wintypes.DWORD),
    ]


class RegistryError(Exception):
    """Base registry error."""


class RegistryCorruptError(RegistryError):
    """Registry JSON/schema is invalid; fail closed."""


class RegistryConflictError(RegistryError):
    """Target overlaps an existing active registry entry."""


class RegistryLockError(RegistryError):
    """Registry lock could not be safely acquired/released."""


class RegistryLockBusy(RegistryLockError):
    """Registry lock is held by another process."""


@dataclass
class RegistryEntry:
    run_id: str
    target_identity: dict[str, Any]
    canonical_path: str
    pid: int
    process_start_utc: str
    user_sid: str
    state: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            'run_id': self.run_id,
            'target_identity': dict(self.target_identity),
            'canonical_path': self.canonical_path,
            'pid': self.pid,
            'process_start_utc': self.process_start_utc,
            'user_sid': self.user_sid,
            'state': self.state,
            'created_at': self.created_at,
        }


@dataclass
class RegistryData:
    schema_version: int = SCHEMA_VERSION
    generation: int = 0
    entries: list[RegistryEntry] = field(default_factory=list)


def _default_registry_root() -> Path:
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return Path(base) / DEFAULT_REGISTRY_DIRNAME


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        info = os.lstat(path)
    except (OSError, ValueError):
        return False
    return bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _normalize_ratio(value: Any) -> str:
    return str(value).replace('/', '\\').rstrip('\\').casefold()


def _components(value: str) -> tuple[str, ...]:
    return tuple(
        part.casefold() for part in value.replace('/', '\\').split('\\')
        if part
    )


def _is_prefix(parent: str, child: str) -> bool:
    parent_parts = _components(parent)
    child_parts = _components(child)
    return (
        bool(parent_parts)
        and len(parent_parts) <= len(child_parts)
        and child_parts[:len(parent_parts)] == parent_parts
    )


def _validate_identity(identity: Any) -> None:
    if not isinstance(identity, dict):
        raise RegistryCorruptError('target_identity 不是对象')
    for key in _IDENTITY_KEYS:
        if key not in identity:
            raise RegistryCorruptError('target_identity 缺少 %s' % key)
    if not isinstance(identity['volume_serial'], int):
        raise RegistryCorruptError('target_identity.volume_serial 类型错误')
    if identity['volume_serial'] <= 0:
        raise RegistryCorruptError('target_identity.volume_serial 非法')
    file_id = identity['file_id']
    if not isinstance(file_id, str) or not _FILE_ID_RE.fullmatch(file_id):
        raise RegistryCorruptError('target_identity.file_id 非法')
    if identity['filesystem'] != 'NTFS':
        raise RegistryCorruptError('target_identity.filesystem 非 NTFS')


def _validate_entry(entry: Any) -> None:
    if not isinstance(entry, dict):
        raise RegistryCorruptError('registry entry 不是对象')
    for key in _ENTRY_KEYS:
        if key not in entry:
            raise RegistryCorruptError('registry entry 缺少 %s' % key)
    if not isinstance(entry['run_id'], str) or not entry['run_id'].strip():
        raise RegistryCorruptError('registry entry run_id 无效')
    _validate_identity(entry['target_identity'])
    if not isinstance(entry['canonical_path'], str) or not entry['canonical_path']:
        raise RegistryCorruptError('registry entry canonical_path 无效')
    for key in ('pid',):
        if not isinstance(entry[key], int) or isinstance(entry[key], bool):
            raise RegistryCorruptError('registry entry %s 类型错误' % key)
    for key in ('process_start_utc', 'user_sid', 'state', 'created_at'):
        if not isinstance(entry[key], str) or not entry[key]:
            raise RegistryCorruptError('registry entry %s 类型错误' % key)
    if entry['state'] != 'ACTIVE':
        raise RegistryCorruptError('registry entry state 非 ACTIVE')


def _entry_from_identity(
        identity: DirectoryIdentity, run_id: str, pid: int,
        process_start_utc: str, user_sid: str, created_at: str,
        state: str = 'ACTIVE') -> RegistryEntry:
    return RegistryEntry(
        run_id=run_id,
        target_identity={
            'volume_serial': int(identity.volume_serial),
            'file_id': identity.file_id.lower(),
            'filesystem': identity.filesystem,
        },
        canonical_path=identity.final_path,
        pid=pid,
        process_start_utc=process_start_utc,
        user_sid=user_sid,
        state=state,
        created_at=created_at,
    )


def _validate_registry(data: Any) -> RegistryData:
    if not isinstance(data, dict):
        raise RegistryCorruptError('registry 根不是对象')
    if data.get('schema_version') != SCHEMA_VERSION:
        raise RegistryCorruptError('不支持的 registry schema_version')
    generation = data.get('generation')
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise RegistryCorruptError('registry generation 无效')
    entries_raw = data.get('entries')
    if not isinstance(entries_raw, list):
        raise RegistryCorruptError('registry entries 不是列表')
    entries: list[RegistryEntry] = []
    seen: set[str] = set()
    for raw in entries_raw:
        _validate_entry(raw)
        if raw['run_id'] in seen:
            raise RegistryCorruptError('registry 存在重复 run_id：%s' % raw['run_id'])
        seen.add(raw['run_id'])
        entries.append(RegistryEntry(
            run_id=raw['run_id'],
            target_identity=dict(raw['target_identity']),
            canonical_path=raw['canonical_path'],
            pid=raw['pid'],
            process_start_utc=raw['process_start_utc'],
            user_sid=raw['user_sid'],
            state=raw['state'],
            created_at=raw['created_at'],
        ))
    return RegistryData(
        schema_version=SCHEMA_VERSION,
        generation=generation,
        entries=entries,
    )


def _read_registry_native(root_handle: int) -> RegistryData:
    status, handle = nt_create_file(
        root_handle, REGISTRY_FILENAME, FILE_OPEN,
        access=FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        share=FILE_SHARE_READ,
        options=(FILE_OPEN_REPARSE_POINT | FILE_NON_DIRECTORY_FILE |
                 FILE_SYNCHRONOUS_IO_NONALERT),
    )
    if (status & 0xFFFFFFFF) == 0xC0000034:
        return RegistryData()
    if status < 0:
        raise RegistryCorruptError(
            '无法读取 registry.json：%s' % status_message(status))
    try:
        attributes = get_attributes(handle)
        if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise RegistryCorruptError('registry.json 是 reparse point')
        if attributes & FILE_ATTRIBUTE_DIRECTORY:
            raise RegistryCorruptError('registry.json 是目录')
        raw = read_all(handle)
    finally:
        close_handle(handle)
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RegistryCorruptError('registry.json 损坏') from exc
    return _validate_registry(data)


def _write_registry_native(root_handle: int, data: Any) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
    temp_name = '.%s.%s.%s.tmp' % (
        REGISTRY_FILENAME, os.getpid(), uuid.uuid4().hex)
    status, handle = nt_create_file(
        root_handle, temp_name, FILE_CREATE,
        access=(FILE_READ_DATA | FILE_WRITE_DATA |
                FILE_READ_ATTRIBUTES | DELETE | SYNCHRONIZE),
        share=0,
        options=(FILE_OPEN_REPARSE_POINT | FILE_NON_DIRECTORY_FILE |
                 FILE_SYNCHRONOUS_IO_NONALERT),
    )
    if status < 0:
        raise RegistryCorruptError(
            '无法创建 Registry 临时文件：%s' % status_message(status))
    renamed = False
    try:
        attributes = get_attributes(handle)
        if attributes & (FILE_ATTRIBUTE_REPARSE_POINT |
                         FILE_ATTRIBUTE_DIRECTORY):
            raise RegistryCorruptError('Registry 临时文件类型异常')
        temp_identity = get_file_id(handle)
        write_all(handle, payload)
        flush_handle(handle)

        # Validate the target (if present) before replacing it. The target
        # open is relative to the same validated root handle.
        status_target, target_handle = nt_create_file(
            root_handle, REGISTRY_FILENAME, FILE_OPEN,
            access=(FILE_READ_DATA | FILE_READ_ATTRIBUTES |
                    DELETE | SYNCHRONIZE),
            share=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            options=(FILE_OPEN_REPARSE_POINT | FILE_NON_DIRECTORY_FILE |
                     FILE_SYNCHRONOUS_IO_NONALERT),
        )
        if status_target < 0 and (status_target & 0xFFFFFFFF) != 0xC0000034:
            raise RegistryCorruptError(
                '无法验证 Registry 目标：%s' % status_message(status_target))
        if status_target >= 0:
            try:
                target_attrs = get_attributes(target_handle)
                if target_attrs & (FILE_ATTRIBUTE_REPARSE_POINT |
                                   FILE_ATTRIBUTE_DIRECTORY):
                    raise RegistryCorruptError('Registry 目标是异常对象')
            finally:
                close_handle(target_handle)

        status_rename = rename_relative(
            handle, root_handle, REGISTRY_FILENAME, replace=True)
        if status_rename < 0:
            raise RegistryCorruptError(
                'Registry 原子替换失败：%s' % status_message(status_rename))
        renamed = True
        if get_file_id(handle) != temp_identity:
            raise RegistryCorruptError('Registry 替换后 FileId 不一致')
    finally:
        if not renamed:
            try:
                delete_handle(handle)
            except Exception:
                pass
        close_handle(handle)


def _read_registry_unlocked(root: Path) -> RegistryData:
    path = root / REGISTRY_FILENAME
    if not path.exists():
        return RegistryData()
    if _is_reparse_point(path) or not path.is_file():
        raise RegistryCorruptError('registry.json 不是安全普通文件')
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegistryCorruptError('registry.json 损坏') from exc
    return _validate_registry(raw)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and _is_reparse_point(path):
        raise RegistryCorruptError('registry.json 目标为 reparse point')
    tmp = path.with_name(
        '.%s.%s.%s.tmp' % (path.name, os.getpid(), uuid.uuid4().hex))
    fd = None
    try:
        # 独占创建，避免预先植入同名临时文件。
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            fd = None
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


class RegistryLock:
    """Non-blocking machine-user registry file lock; no stale PID logic."""

    def __init__(self, path: Path, parent_handle: int | None = None,
                 parent_identity: DirectoryIdentity | None = None):
        self.path = Path(path)
        self._handle = None
        self.identity: tuple[int, str] | None = None
        self._parent_handle = parent_handle
        self.parent_identity = parent_identity
        self.identity_name = self.path.name + '.identity'

    def _read_expected_identity(self, parent_handle: int) -> tuple[int, str]:
        status, handle = nt_create_file(
            parent_handle, self.identity_name, FILE_OPEN,
            access=FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            share=FILE_SHARE_READ,
            options=(FILE_OPEN_REPARSE_POINT | FILE_NON_DIRECTORY_FILE |
                     FILE_SYNCHRONOUS_IO_NONALERT),
        )
        if status < 0:
            raise RegistryLockError(
                'Registry 锁身份记录不可读：%s' % status_message(status))
        try:
            attrs = get_attributes(handle)
            if attrs & (FILE_ATTRIBUTE_REPARSE_POINT |
                        FILE_ATTRIBUTE_DIRECTORY):
                raise RegistryLockError('Registry 锁身份记录类型异常')
            raw = read_all(handle)
        finally:
            close_handle(handle)
        try:
            record = json.loads(raw.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RegistryLockError('Registry 锁身份记录损坏') from exc
        if not isinstance(record, dict):
            raise RegistryLockError('Registry 锁身份记录不是对象')
        volume = record.get('volume_serial')
        file_id = record.get('file_id')
        if not isinstance(volume, int) or not isinstance(file_id, str):
            raise RegistryLockError('Registry 锁身份记录字段无效')
        return volume, file_id.lower()

    def _create_expected_identity(self, parent_handle: int,
                                  actual: tuple[int, str]) -> None:
        payload = json.dumps({
            'schema_version': 1,
            'volume_serial': int(actual[0]),
            'file_id': actual[1],
        }).encode('utf-8')
        status, handle = nt_create_file(
            parent_handle, self.identity_name, FILE_CREATE,
            access=(FILE_READ_DATA | FILE_WRITE_DATA |
                    FILE_READ_ATTRIBUTES | DELETE | SYNCHRONIZE),
            share=0,
            options=(FILE_OPEN_REPARSE_POINT | FILE_NON_DIRECTORY_FILE |
                     FILE_SYNCHRONOUS_IO_NONALERT),
        )
        if status < 0:
            raise RegistryLockError(
                '无法创建 Registry 锁身份记录：%s' % status_message(status))
        try:
            write_all(handle, payload)
            flush_handle(handle)
        finally:
            close_handle(handle)

    def set_parent(self, parent_handle: int,
                   parent_identity: DirectoryIdentity | None = None) -> None:
        self._parent_handle = parent_handle
        self.parent_identity = parent_identity

    def acquire(self) -> None:
        if self._parent_handle:
            if self.path.name != self.path.name.strip() or any(
                    ch in self.path.name for ch in ('\\', '/')):
                raise RegistryLockError('Registry 锁文件名无效')
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if _is_reparse_point(self.path.parent) or _is_reparse_point(self.path):
                raise RegistryLockError('Registry 锁路径包含 reparse point')
        if sys.platform != 'win32':
            raise RegistryLockError('RegistryLock 第一版只支持 Windows')
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.LockFileEx.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
        kernel32.LockFileEx.restype = wintypes.BOOL
        kernel32.UnlockFileEx.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.LPVOID]
        kernel32.UnlockFileEx.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        created = False
        if self._parent_handle:
            status, handle = nt_create_file(
                self._parent_handle,
                self.path.name,
                FILE_OPEN,
                access=GENERIC_READ | GENERIC_WRITE,
                share=0,
                options=FILE_OPEN_REPARSE_POINT | FILE_NON_DIRECTORY_FILE,
            )
            if (status & 0xFFFFFFFF) == 0xC0000034:
                status, handle = nt_create_file(
                    self._parent_handle,
                    self.path.name,
                    FILE_CREATE,
                    access=(FILE_READ_DATA | FILE_WRITE_DATA |
                            FILE_READ_ATTRIBUTES | SYNCHRONIZE),
                    share=0,
                    options=(FILE_OPEN_REPARSE_POINT | FILE_NON_DIRECTORY_FILE |
                             FILE_SYNCHRONOUS_IO_NONALERT),
                )
                created = status >= 0
            if not status >= 0:
                if (status & 0xFFFFFFFF) in (0xC0000043, 0xC0000054,
                                             0xC0000035):
                    raise RegistryLockBusy(
                        'Registry 锁被占用：%s' % status_message(status))
                raise RegistryLockError(
                    '无法相对打开 Registry 锁：%s' % status_message(status))
        else:
            kernel32.CreateFileW.argtypes = [
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                wintypes.HANDLE]
            kernel32.CreateFileW.restype = wintypes.HANDLE
            handle = kernel32.CreateFileW(
                str(self.path), 0x80000000 | 0x40000000, 0, None,
                _OPEN_ALWAYS,
                _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
                None)
            if handle == _INVALID_HANDLE_VALUE:
                error = ctypes.get_last_error()
                if error in (32, 33, 158):
                    raise RegistryLockBusy('Registry 锁被占用')
                raise RegistryLockError('无法打开 Registry 锁（WinError %s）' % error)
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        info = _BY_HANDLE_FILE_INFORMATION()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise RegistryLockError('无法验证 Registry 锁文件（WinError %s）' % error)
        if info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            kernel32.CloseHandle(handle)
            raise RegistryLockError('Registry 锁文件是 reparse point')
        if info.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
            kernel32.CloseHandle(handle)
            raise RegistryLockError('Registry 锁路径是目录')
        file_id_info = _FILE_ID_INFO()
        if not kernel32.GetFileInformationByHandleEx(
                handle, _FILE_ID_INFO_CLASS, ctypes.byref(file_id_info),
                ctypes.sizeof(file_id_info)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise RegistryLockError('无法取得 Registry 锁文件身份（WinError %s）' % error)
        volume_serial = int(file_id_info.VolumeSerialNumber)
        file_id = bytes(file_id_info.FileId.Identifier).hex()
        if volume_serial <= 0 or not any(file_id_info.FileId.Identifier):
            kernel32.CloseHandle(handle)
            raise RegistryLockError('Registry 锁文件身份为空')
        actual_identity = (volume_serial, file_id)
        if self._parent_handle:
            try:
                if created:
                    self._create_expected_identity(
                        self._parent_handle, actual_identity)
                else:
                    expected_identity = self._read_expected_identity(
                        self._parent_handle)
                    if expected_identity != actual_identity:
                        raise RegistryLockError(
                            'Registry 锁文件身份与预期不一致')
            except Exception:
                kernel32.CloseHandle(handle)
                raise
        size = 512
        while True:
            final_buffer = ctypes.create_unicode_buffer(size)
            result = kernel32.GetFinalPathNameByHandleW(
                handle, final_buffer, size,
                _FILE_NAME_NORMALIZED | _VOLUME_NAME_DOS)
            if result == 0:
                error = ctypes.get_last_error()
                kernel32.CloseHandle(handle)
                raise RegistryLockError(
                    '无法取得 Registry 锁文件最终路径（WinError %s）' % error)
            if result < size:
                break
            size = int(result)
        def _normalize_lock_path(value: str) -> str:
            text = os.path.normpath(str(value))
            if text.startswith('\\\\?\\'):
                text = text[4:]
            return os.path.normcase(text)

        final_lock = _normalize_lock_path(final_buffer.value)
        expected_lock = _normalize_lock_path(str(self.path.resolve()))
        if final_lock != expected_lock:
            kernel32.CloseHandle(handle)
            raise RegistryLockError('Registry 锁文件最终路径与预期不一致')
        class _OVERLAPPED(ctypes.Structure):
            _fields_ = [
                ('Internal', ctypes.c_void_p),
                ('InternalHigh', ctypes.c_void_p),
                ('Offset', wintypes.DWORD),
                ('OffsetHigh', wintypes.DWORD),
                ('hEvent', wintypes.HANDLE),
            ]
        overlapped = _OVERLAPPED()
        if not kernel32.LockFileEx(
                handle, _LOCKFILE_EXCLUSIVE | _LOCKFILE_FAIL_IMMEDIATELY,
                0, 1, 0, ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            if error in (33, 158):
                raise RegistryLockBusy('Registry 锁被占用')
            raise RegistryLockError('无法获取 Registry 锁（WinError %s）' % error)
        self._handle = (kernel32, handle, overlapped)
        self.identity = (volume_serial, file_id)

    def _close_handle(self, unlock: bool) -> None:
        if self._handle is None:
            return
        kernel32, handle, overlapped = self._handle
        try:
            if unlock:
                kernel32.UnlockFileEx(
                    handle, 0, 1, 0, ctypes.byref(overlapped))
        finally:
            # CloseHandle releases the OS lock even if unlock itself fails.
            kernel32.CloseHandle(handle)
            self._handle = None
            self.identity = None

    def release(self) -> None:
        self._close_handle(unlock=True)

    def abort(self) -> None:
        """Force-close the handle without requiring UnlockFileEx to succeed."""
        self._close_handle(unlock=False)


class RegistryStore:
    """Registry read/write helper. Lock held by context manager."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(os.path.abspath(str(root or _default_registry_root())))
        self._lock = RegistryLock(self.root / REGISTRY_LOCK_FILENAME)
        self._locked = False
        self._prepared = False
        self._owns_handles = False
        self._root_handle: DirectoryHandle | None = None
        self._roots_handle: DirectoryHandle | None = None
        self._root_identity: DirectoryIdentity | None = None
        self._roots_identity: DirectoryIdentity | None = None

    @property
    def root_handle(self) -> DirectoryHandle | None:
        return self._root_handle

    @property
    def roots_handle(self) -> DirectoryHandle | None:
        return self._roots_handle

    @property
    def root_identity(self) -> DirectoryIdentity | None:
        return self._root_identity

    @property
    def roots_identity(self) -> DirectoryIdentity | None:
        return self._roots_identity

    def prepare(self) -> 'RegistryStore':
        """Create/open and validate root and roots once; idempotent."""
        if self._prepared:
            return self
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._root_handle = open_secure_directory(self.root)
            self._root_identity = self._root_handle.identity
            roots = self.root / 'roots'
            roots.mkdir(parents=True, exist_ok=True)
            self._roots_handle = open_secure_directory(roots)
            self._roots_identity = self._roots_handle.identity
            self._prepared = True
            return self
        except Exception:
            self._close_handles()
            raise

    def __enter__(self) -> 'RegistryStore':
        try:
            if not self._prepared:
                self.prepare()
                self._owns_handles = True
            self._lock.set_parent(
                self._root_handle.native_handle, self._root_identity)
            self._lock.acquire()
            self._locked = True
            return self
        except Exception:
            if self._owns_handles:
                self._close_handles()
            raise

    def _close_handles(self) -> None:
        errors: list[tuple[str, BaseException]] = []
        for name, attr in (('roots', '_roots_handle'), ('root', '_root_handle')):
            handle = getattr(self, attr)
            if handle is None:
                continue
            try:
                handle.close()
            except BaseException as exc:  # noqa: BLE001 - collect all close errors
                errors.append((name, exc))
            finally:
                setattr(self, attr, None)
        self._root_identity = None
        self._roots_identity = None
        self._prepared = False
        if errors:
            raise RegistryLockError(
                'Registry 目录句柄关闭失败：' + '; '.join(
                    '%s=%s: %s' % (name, type(exc).__name__, exc)
                    for name, exc in errors))

    def close(self) -> None:
        errors: list[tuple[str, BaseException]] = []
        try:
            self._lock.release()
        except BaseException as exc:  # noqa: BLE001
            errors.append(('registry_lock', exc))
        finally:
            self._locked = False
        try:
            self._close_handles()
        except BaseException as exc:  # noqa: BLE001
            errors.append(('registry_handles', exc))
        finally:
            self._owns_handles = False
        if errors:
            raise RegistryLockError(
                'Registry 释放失败：' + '; '.join(
                    '%s=%s: %s' % (name, type(exc).__name__, exc)
                    for name, exc in errors))

    def __exit__(self, exc_type, exc, tb) -> bool:
        errors: list[tuple[str, BaseException]] = []
        try:
            self._lock.release()
        except BaseException as exc:  # noqa: BLE001
            errors.append(('registry_lock', exc))
        finally:
            self._locked = False
        if self._owns_handles:
            try:
                self._close_handles()
            except BaseException as exc:  # noqa: BLE001
                errors.append(('registry_handles', exc))
            finally:
                self._owns_handles = False
        if errors:
            raise RegistryLockError(
                'Registry 释放失败：' + '; '.join(
                    '%s=%s: %s' % (name, type(exc).__name__, exc)
                    for name, exc in errors))
        return False

    def _require_lock(self) -> None:
        if not self._locked:
            raise RegistryLockError('Registry 读写必须持有 Registry Lock')

    def load(self) -> RegistryData:
        self._require_lock()
        if self._root_handle is not None:
            return _read_registry_native(self._root_handle.native_handle)
        return _read_registry_unlocked(self.root)

    def save(self, data: RegistryData) -> RegistryData:
        self._require_lock()
        if not isinstance(data, RegistryData):
            raise RegistryCorruptError('RegistryData 类型错误')
        payload = {
            'schema_version': data.schema_version,
            'generation': data.generation + 1,
            'entries': [entry.to_dict() for entry in data.entries],
        }
        _validate_registry(payload)
        if self._root_handle is not None:
            _write_registry_native(self._root_handle.native_handle, payload)
        else:
            _atomic_write_json(self.root / REGISTRY_FILENAME, payload)
        return _validate_registry(payload)

    def add_entry(self, entry: RegistryEntry) -> RegistryData:
        self._require_lock()
        _validate_entry(entry.to_dict())
        data = self.load()
        for existing in data.entries:
            if existing.run_id == entry.run_id:
                raise RegistryConflictError('run_id 重复：%s' % entry.run_id)
            if self._entries_overlap(existing, entry):
                raise RegistryConflictError(
                    '目标与现有恢复记录重叠：%s' % existing.run_id)
        data.entries.append(entry)
        return self.save(data)

    def remove_entry(self, run_id: str) -> RegistryData:
        self._require_lock()
        data = self.load()
        data.entries = [entry for entry in data.entries if entry.run_id != run_id]
        return self.save(data)

    @staticmethod
    def _entries_overlap(left: RegistryEntry, right: RegistryEntry) -> bool:
        l_id = left.target_identity
        r_id = right.target_identity
        if (l_id['volume_serial'], l_id['file_id']) == (
                r_id['volume_serial'], r_id['file_id']):
            return True
        if l_id['volume_serial'] != r_id['volume_serial']:
            return False
        if not left.canonical_path or not right.canonical_path:
            return True
        return (
            _is_prefix(left.canonical_path, right.canonical_path)
            or _is_prefix(right.canonical_path, left.canonical_path)
        )


def make_entry(identity: DirectoryIdentity, run_id: str, pid: int,
               process_start_utc: str, user_sid: str,
               created_at: str) -> RegistryEntry:
    return _entry_from_identity(
        identity, run_id, pid, process_start_utc, user_sid, created_at)


__all__ = [
    'SCHEMA_VERSION',
    'RegistryError',
    'RegistryCorruptError',
    'RegistryConflictError',
    'RegistryLockError',
    'RegistryLockBusy',
    'RegistryEntry',
    'RegistryData',
    'RegistryLock',
    'RegistryStore',
    'make_entry',
]
