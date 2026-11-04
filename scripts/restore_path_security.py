# -*- coding: utf-8 -*-
"""Windows restore path security foundation.

First version scope:
- local NTFS only
- drive-letter absolute paths only
- no UNC / mapped network drive / volume-GUID input
- target directory and every ancestor must be ordinary, non-reparse directories

This module intentionally does not integrate with restore_db.py yet.
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from restore_win32_native import close_handle_once  # noqa: E402

FILE_LIST_DIRECTORY = 0x00000001
FILE_READ_ATTRIBUTES = 0x00000080
SYNCHRONIZE = 0x00100000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ID_INFO_CLASS = 18
FILE_NAME_NORMALIZED = 0x00000000
VOLUME_NAME_DOS = 0x00000000
VOLUME_NAME_GUID = 0x00000001
DRIVE_FIXED = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_DRIVE_PATH = re.compile(r'^[A-Za-z]:$')


class PathSecurityError(Exception):
    """Base error for path security failures."""


class UnsupportedPlatformError(PathSecurityError):
    """Module is only supported on Windows."""


class UnsupportedPathError(PathSecurityError):
    """Path is outside the first-version supported Windows scope."""


class ReparsePointError(UnsupportedPathError):
    """A target or ancestor is a reparse point."""


class PathNotFoundError(PathSecurityError):
    """Target path does not exist."""


class AccessDeniedPathError(PathSecurityError):
    """Target path cannot be accessed."""


class NotDirectoryError(PathSecurityError):
    """Target path is not a directory."""


class BusyPathError(PathSecurityError):
    """Target path is exclusively used by another process."""


class Win32PathError(PathSecurityError):
    """Unclassified Windows API error."""

    def __init__(self, message: str, winerror: int | None = None):
        super().__init__(message)
        self.winerror = winerror


class UnsupportedFilesystemError(UnsupportedPathError):
    """Filesystem is not supported by the first version."""


@dataclass(frozen=True)
class DirectoryIdentity:
    requested_path: str
    final_path: str
    volume_serial: int
    file_id: str
    filesystem: str
    drive_type: int


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


def _kernel32():
    if sys.platform != 'win32':
        raise UnsupportedPlatformError('restore_path_security requires Windows')
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.GetVolumePathNameW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.GetVolumePathNameW.restype = wintypes.BOOL
    kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD,
        wintypes.LPWSTR, wintypes.DWORD,
    ]
    kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    return kernel32


def _raise_win32(path: Path, error: int, operation: str) -> None:
    if error in (2, 3):
        raise PathNotFoundError('%s 不存在：%s' % (operation, path))
    if error == 5:
        raise AccessDeniedPathError('%s 访问被拒绝：%s' % (operation, path))
    if error in (32, 33):
        raise BusyPathError('%s 对象被占用：%s' % (operation, path))
    if error == 267:
        raise NotDirectoryError('%s 不是目录：%s' % (operation, path))
    raise Win32PathError(
        '%s 失败：%s（WinError %s）' % (operation, path, error), error)


def _normalize_input_path(path: str | Path) -> Path:
    raw = str(path)
    if raw.startswith('\\\\'):
        raise UnsupportedPathError('第一版不支持 UNC/网络路径：%s' % raw)
    if raw.startswith('\\\\?\\'):
        raise UnsupportedPathError('第一版不接受 volume-GUID 输入：%s' % raw)
    if not os.path.isabs(raw):
        raise UnsupportedPathError('第一版只接受绝对路径：%s' % raw)
    absolute = Path(os.path.abspath(raw))
    drive, tail = os.path.splitdrive(str(absolute))
    if not _DRIVE_PATH.fullmatch(drive):
        raise UnsupportedPathError('第一版只支持盘符绝对路径：%s' % raw)
    return Path(drive + '\\' + tail.lstrip('\\'))


def _ancestor_paths(path: Path) -> list[Path]:
    drive, tail = os.path.splitdrive(str(path))
    result = [Path(drive + '\\')]
    current = Path(drive + '\\')
    for part in tail.strip('\\').split('\\'):
        if not part:
            continue
        current = current / part
        result.append(current)
    return result


def _open_directory_handle(path: Path):
    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(
        str(path),
        FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        _raise_win32(path, error, '打开目录')
    return kernel32, handle


def _file_attributes(path: Path, handle) -> int:
    kernel32 = _kernel32()
    info = _BY_HANDLE_FILE_INFORMATION()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        error = ctypes.get_last_error()
        raise Win32PathError(
            '读取文件属性失败：%s（WinError %s）' % (path, error), error)
    return int(info.dwFileAttributes)


def _file_id_info(path: Path, handle) -> tuple[int, bytes]:
    kernel32 = _kernel32()
    info = _FILE_ID_INFO()
    if not kernel32.GetFileInformationByHandleEx(
            handle, FILE_ID_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.get_last_error()
        raise UnsupportedPathError(
            '无法取得 FileId：%s（WinError %s）' % (path, error))
    serial = int(info.VolumeSerialNumber)
    file_id = bytes(info.FileId.Identifier)
    if serial == 0 or not any(file_id):
        raise UnsupportedPathError('FileId/VolumeSerialNumber 为空：%s' % path)
    return serial, file_id


def _final_path(path: Path, handle, flags: int) -> str:
    kernel32 = _kernel32()
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        result = kernel32.GetFinalPathNameByHandleW(
            handle, buffer, size, flags)
        if result == 0:
            error = ctypes.get_last_error()
            raise UnsupportedPathError(
                '无法获取最终路径：%s（WinError %s）' % (path, error))
        if result < size:
            return buffer.value
        size = int(result)


def _validate_filesystem(path: Path, filesystem: str, drive_type: int) -> None:
    if drive_type != DRIVE_FIXED:
        raise UnsupportedFilesystemError(
            '第一版只支持本地固定盘（drive_type=%s）：%s' % (
                drive_type, path))
    if filesystem != 'NTFS':
        raise UnsupportedFilesystemError(
            '第一版只支持 NTFS，当前为 %s：%s' % (filesystem, path))


def _volume_info(path: Path, final_dos_path: str) -> tuple[str, int, int]:
    kernel32 = _kernel32()
    volume_buffer = ctypes.create_unicode_buffer(1024)
    if not kernel32.GetVolumePathNameW(
            final_dos_path, volume_buffer, len(volume_buffer)):
        error = ctypes.get_last_error()
        raise UnsupportedPathError(
            '无法获取卷路径：%s（WinError %s）' % (path, error))
    volume_root = volume_buffer.value
    drive_type = int(kernel32.GetDriveTypeW(volume_root))
    fs_name = ctypes.create_unicode_buffer(256)
    flags = wintypes.DWORD(0)
    if not kernel32.GetVolumeInformationW(
            volume_root, None, 0, None, None, ctypes.byref(flags),
            fs_name, len(fs_name)):
        error = ctypes.get_last_error()
        raise UnsupportedPathError(
            '无法获取文件系统信息：%s（WinError %s）' % (path, error))
    filesystem = fs_name.value.upper()
    _validate_filesystem(path, filesystem, drive_type)
    return filesystem, drive_type, int(flags.value)


def _attach_close_error(primary: BaseException, error: BaseException) -> None:
    """Attach a secondary close error without replacing the primary exception."""
    try:
        existing = tuple(getattr(primary, '_native_close_errors', ()))
        setattr(primary, '_native_close_errors', existing + (error,))
    except BaseException:  # noqa: BLE001 - diagnostics must not mask primary
        pass


class DirectoryHandle:
    """Held, validated directory handle and its physical identity."""

    OPEN = 'OPEN'
    CLOSING = 'CLOSING'
    CLOSED = 'CLOSED'
    CLOSE_FAILED_UNKNOWN = 'CLOSE_FAILED_UNKNOWN'

    def __init__(self, handle, identity: DirectoryIdentity):
        self._handle = handle
        self.identity = identity
        self._close_state = self.OPEN

    @property
    def closed(self) -> bool:
        """True only when CloseHandle returned TRUE."""
        return self._close_state == self.CLOSED

    @property
    def close_failed(self) -> bool:
        return self._close_state == self.CLOSE_FAILED_UNKNOWN

    @property
    def is_open(self) -> bool:
        return self._close_state == self.OPEN

    @property
    def close_state(self) -> str:
        return self._close_state

    @property
    def native_handle(self) -> int | None:
        """Opaque validated directory handle for relative child operations."""
        if self._close_state != self.OPEN:
            return None
        return self._handle

    def close(self) -> None:
        if self._close_state != self.OPEN:
            return
        handle = self._handle
        # Drop the raw value before the call: a failed close is never retried.
        self._handle = None
        self._close_state = self.CLOSING
        try:
            close_handle_once(
                handle, owner='DirectoryHandle', operation='close')
        except BaseException:
            self._close_state = self.CLOSE_FAILED_UNKNOWN
            raise
        self._close_state = self.CLOSED

    def __enter__(self) -> 'DirectoryHandle':
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            try:
                self.close()
            except BaseException as close_exc:
                _attach_close_error(exc, close_exc)
            return False
        self.close()
        return False


def _validate_ancestors(path: Path) -> None:
    for ancestor in _ancestor_paths(path):
        _kernel32_instance, handle = _open_directory_handle(ancestor)
        primary = None
        try:
            attributes = _file_attributes(ancestor, handle)
            if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                raise ReparsePointError(
                    '目标路径祖先包含 reparse point：%s' % ancestor)
            if not attributes & FILE_ATTRIBUTE_DIRECTORY:
                raise NotDirectoryError('目标路径祖先不是目录：%s' % ancestor)
        except BaseException as exc:  # noqa: BLE001 - close must still run
            primary = exc
        try:
            close_handle_once(
                handle, owner='path-ancestor', operation='close')
        except BaseException as close_exc:  # noqa: BLE001
            if primary is None:
                raise
            _attach_close_error(primary, close_exc)
        if primary is not None:
            raise primary.with_traceback(primary.__traceback__)


def open_secure_directory(path: str | Path) -> DirectoryHandle:
    """Open a local NTFS directory after validating every path component."""
    requested = _normalize_input_path(path)
    _validate_ancestors(requested)
    kernel32, handle = _open_directory_handle(requested)
    try:
        attributes = _file_attributes(requested, handle)
        if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise ReparsePointError('目标目录是 reparse point：%s' % requested)
        if not attributes & FILE_ATTRIBUTE_DIRECTORY:
            raise NotDirectoryError('目标路径不是目录：%s' % requested)
        serial, file_id = _file_id_info(requested, handle)
        final_guid = _final_path(requested, handle, VOLUME_NAME_GUID)
        final_dos = _final_path(requested, handle, VOLUME_NAME_DOS)
        filesystem, drive_type, _flags = _volume_info(requested, final_dos)
        _validate_filesystem(requested, filesystem, drive_type)
        identity = DirectoryIdentity(
            requested_path=str(requested),
            final_path=final_guid,
            volume_serial=serial,
            file_id=file_id.hex(),
            filesystem=filesystem,
            drive_type=drive_type,
        )
        return DirectoryHandle(handle, identity)
    except BaseException as primary:  # noqa: BLE001 - close must still run
        try:
            close_handle_once(
                handle, owner='open_secure_directory', operation='close')
        except BaseException as close_exc:  # noqa: BLE001
            _attach_close_error(primary, close_exc)
        raise primary.with_traceback(primary.__traceback__)


def validate_target_directory(path: str | Path) -> DirectoryIdentity:
    """Validate and close a target directory, returning physical identity."""
    with open_secure_directory(path) as handle:
        return handle.identity


__all__ = [
    'DirectoryHandle',
    'DirectoryIdentity',
    'PathSecurityError',
    'UnsupportedPlatformError',
    'UnsupportedPathError',
    'ReparsePointError',
    'PathNotFoundError',
    'AccessDeniedPathError',
    'NotDirectoryError',
    'BusyPathError',
    'UnsupportedFilesystemError',
    'Win32PathError',
    'open_secure_directory',
    'validate_target_directory',
]
