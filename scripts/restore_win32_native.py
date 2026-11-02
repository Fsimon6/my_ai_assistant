# -*- coding: utf-8 -*-
"""Minimal Windows Native API wrapper for relative file operations.

The wrapper intentionally accepts only a validated parent directory handle and
a single relative file name. It never falls back to a full path.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

ntdll = ctypes.WinDLL('ntdll', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

FILE_READ_DATA = 0x00000001
FILE_WRITE_DATA = 0x00000002
FILE_READ_ATTRIBUTES = 0x00000080
DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_OPEN_REPARSE_POINT = 0x00200000
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
OBJ_CASE_INSENSITIVE = 0x00000040
FILE_CREATE = 2
FILE_OPEN = 1
FILE_OPEN_IF = 3
FILE_ID_INFO_CLASS = 18
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
STATUS_OBJECT_NAME_COLLISION = 0xC0000035
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_OBJECT_NAME_INVALID = 0xC0000033
STATUS_SHARING_VIOLATION = 0xC0000043
STATUS_ACCESS_DENIED = 0xC0000022


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ('Length', ctypes.c_ushort),
        ('MaximumLength', ctypes.c_ushort),
        ('Buffer', wintypes.LPWSTR),
    ]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ('Length', wintypes.ULONG),
        ('RootDirectory', wintypes.HANDLE),
        ('ObjectName', ctypes.POINTER(UNICODE_STRING)),
        ('Attributes', wintypes.ULONG),
        ('SecurityDescriptor', wintypes.LPVOID),
        ('SecurityQualityOfService', wintypes.LPVOID),
    ]


class IO_STATUS_BLOCK_UNION(ctypes.Union):
    _fields_ = [
        ('Status', ctypes.c_long),
        ('Pointer', ctypes.c_void_p),
    ]


class IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [
        ('Status', IO_STATUS_BLOCK_UNION),
        ('Information', ctypes.c_size_t),
    ]


class FILE_ID_128(ctypes.Structure):
    _fields_ = [('Identifier', ctypes.c_ubyte * 16)]


class FILE_ID_INFO(ctypes.Structure):
    _fields_ = [
        ('VolumeSerialNumber', ctypes.c_ulonglong),
        ('FileId', FILE_ID_128),
    ]


class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
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


class FILE_RENAME_INFO(ctypes.Structure):
    _fields_ = [
        ('ReplaceIfExists', ctypes.c_ubyte),
        ('RootDirectory', wintypes.HANDLE),
        ('FileNameLength', wintypes.DWORD),
        ('FileName', wintypes.WCHAR * 1),
    ]


class FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [('DeleteFile', ctypes.c_ubyte)]


ntdll.NtCreateFile.argtypes = [
    ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
    ctypes.POINTER(OBJECT_ATTRIBUTES), ctypes.POINTER(IO_STATUS_BLOCK),
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
ntdll.NtCreateFile.restype = ctypes.c_long
ntdll.NtSetInformationFile.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(IO_STATUS_BLOCK),
    wintypes.LPVOID, wintypes.ULONG, wintypes.ULONG]
ntdll.NtSetInformationFile.restype = ctypes.c_long
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetFileInformationByHandle.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
kernel32.GetFileInformationByHandleEx.argtypes = [
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
kernel32.FlushFileBuffers.restype = wintypes.BOOL


def is_success(status: int) -> bool:
    return int(status) >= 0


def status_hex(status: int) -> str:
    return '0x%08X' % (int(status) & 0xFFFFFFFF)


def status_message(status: int) -> str:
    known = {
        STATUS_OBJECT_NAME_COLLISION: 'OBJECT_NAME_COLLISION',
        STATUS_OBJECT_NAME_NOT_FOUND: 'OBJECT_NAME_NOT_FOUND',
        STATUS_OBJECT_NAME_INVALID: 'OBJECT_NAME_INVALID',
        STATUS_SHARING_VIOLATION: 'SHARING_VIOLATION',
        STATUS_ACCESS_DENIED: 'ACCESS_DENIED',
    }
    label = known.get(int(status) & 0xFFFFFFFF)
    return '%s (%s)' % (status_hex(status), label or 'unknown NTSTATUS')


def validate_relative_name(name: str) -> None:
    if not isinstance(name, str) or not name or name in ('.', '..'):
        raise ValueError('relative file name is empty or dot segment')
    if any(ch in name for ch in ('\\', '/', ':')):
        raise ValueError('relative file name contains a path separator')
    if name.rstrip(' .') != name:
        raise ValueError('relative file name has trailing dot/space')
    if name.startswith(('\\?\\', '\\\\.\\')):
        raise ValueError('relative file name contains a device prefix')


def _unicode_string(name: str):
    buffer = ctypes.create_unicode_buffer(name)
    return buffer, UNICODE_STRING(
        Length=len(name) * 2,
        MaximumLength=(len(name) + 1) * 2,
        Buffer=ctypes.cast(buffer, wintypes.LPWSTR),
    )


def nt_create_file(
        parent_handle: int,
        name: str,
        disposition: int,
        access: int = (FILE_READ_DATA | FILE_WRITE_DATA |
                       FILE_READ_ATTRIBUTES | SYNCHRONIZE),
        share: int = 0,
        options: int = (FILE_OPEN_REPARSE_POINT | FILE_NON_DIRECTORY_FILE |
                        FILE_SYNCHRONOUS_IO_NONALERT),
) -> tuple[int, int]:
    """Create/open a child of parent_handle by one relative name."""
    validate_relative_name(name)
    if not parent_handle:
        raise ValueError('parent_handle is required')
    buffer, unicode_name = _unicode_string(name)
    attributes = OBJECT_ATTRIBUTES(
        Length=ctypes.sizeof(OBJECT_ATTRIBUTES),
        RootDirectory=parent_handle,
        ObjectName=ctypes.pointer(unicode_name),
        Attributes=OBJ_CASE_INSENSITIVE,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    io_status = IO_STATUS_BLOCK()
    handle = wintypes.HANDLE()
    status = ntdll.NtCreateFile(
        ctypes.byref(handle), access, ctypes.byref(attributes),
        ctypes.byref(io_status), None, FILE_ATTRIBUTE_NORMAL,
        share, disposition, options, None, 0)
    del buffer
    return int(status), int(handle.value or 0)


def close_handle(handle: int | None) -> None:
    if handle:
        kernel32.CloseHandle(handle)


def get_file_id(handle: int) -> tuple[int, str]:
    info = FILE_ID_INFO()
    if not kernel32.GetFileInformationByHandleEx(
            handle, FILE_ID_INFO_CLASS, ctypes.byref(info),
            ctypes.sizeof(info)):
        raise OSError('FileIdInfo failed: %s' % ctypes.get_last_error())
    return int(info.VolumeSerialNumber), bytes(info.FileId.Identifier).hex()


def get_attributes(handle: int) -> int:
    info = BY_HANDLE_FILE_INFORMATION()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise OSError('GetFileInformationByHandle failed: %s' %
                      ctypes.get_last_error())
    return int(info.dwFileAttributes)


def rename_relative(handle: int, target_parent: int, target_name: str,
                    replace: bool = False) -> int:
    """Rename an open file relative to target_parent; returns NTSTATUS."""
    validate_relative_name(target_name)
    if not target_parent:
        raise ValueError('target_parent is required')
    name_buffer = ctypes.create_unicode_buffer(target_name)
    offset = FILE_RENAME_INFO.FileName.offset
    total = offset + ctypes.sizeof(name_buffer)
    raw = ctypes.create_string_buffer(total)
    info = ctypes.cast(raw, ctypes.POINTER(FILE_RENAME_INFO)).contents
    info.ReplaceIfExists = 1 if replace else 0
    info.RootDirectory = target_parent
    info.FileNameLength = len(target_name) * 2
    ctypes.memmove(
        ctypes.addressof(raw) + offset,
        ctypes.byref(name_buffer),
        ctypes.sizeof(name_buffer))
    io_status = IO_STATUS_BLOCK()
    status = ntdll.NtSetInformationFile(
        handle, ctypes.byref(io_status), ctypes.byref(raw), total, 10)
    return int(status)


def read_all(handle: int, chunk_size: int = 65536) -> bytes:
    chunks: list[bytes] = []
    while True:
        buffer = ctypes.create_string_buffer(chunk_size)
        read = wintypes.DWORD(0)
        if not kernel32.ReadFile(
                handle, buffer, chunk_size, ctypes.byref(read), None):
            raise OSError('ReadFile failed: %s' % ctypes.get_last_error())
        if read.value == 0:
            break
        chunks.append(buffer.raw[:read.value])
        if read.value < chunk_size:
            break
    return b''.join(chunks)


def write_all(handle: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + 65536]
        written = wintypes.DWORD(0)
        if not kernel32.WriteFile(
                handle, chunk, len(chunk), ctypes.byref(written), None):
            raise OSError('WriteFile failed: %s' % ctypes.get_last_error())
        if written.value <= 0:
            raise OSError('WriteFile wrote zero bytes')
        offset += written.value


def flush_handle(handle: int) -> None:
    if not kernel32.FlushFileBuffers(handle):
        raise OSError('FlushFileBuffers failed: %s' % ctypes.get_last_error())


def delete_handle(handle: int) -> int:
    """Mark an open file for deletion; requires DELETE access."""
    info = FILE_DISPOSITION_INFO(DeleteFile=1)
    io_status = IO_STATUS_BLOCK()
    status = ntdll.NtSetInformationFile(
        handle, ctypes.byref(io_status), ctypes.byref(info),
        ctypes.sizeof(info), 13)
    return int(status)


__all__ = [
    'FILE_CREATE', 'FILE_OPEN', 'FILE_OPEN_IF',
    'FILE_OPEN_REPARSE_POINT', 'FILE_NON_DIRECTORY_FILE',
    'FILE_SYNCHRONOUS_IO_NONALERT',
    'FILE_ATTRIBUTE_REPARSE_POINT', 'FILE_ATTRIBUTE_DIRECTORY',
    'FILE_SHARE_READ', 'FILE_SHARE_WRITE', 'FILE_SHARE_DELETE',
    'DELETE', 'SYNCHRONIZE', 'GENERIC_READ', 'GENERIC_WRITE',
    'is_success', 'status_hex', 'status_message',
    'validate_relative_name', 'nt_create_file',
    'close_handle', 'get_file_id', 'get_attributes',
    'read_all', 'write_all', 'flush_handle',
    'rename_relative', 'delete_handle',
]
