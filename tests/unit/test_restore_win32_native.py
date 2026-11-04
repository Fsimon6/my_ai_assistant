# -*- coding: utf-8 -*-
"""Native relative file-operation tests; isolated temp directories only."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from restore_path_security import open_secure_directory  # noqa: E402
import restore_win32_native as native  # noqa: E402


pytestmark = pytest.mark.skipif(
    sys.platform != 'win32', reason='Windows Native API only')


def _parent(tmp_path: Path):
    directory = tmp_path / 'parent'
    directory.mkdir()
    return directory, open_secure_directory(directory)


def _close(handle) -> None:
    # Test helpers use 0 as the sentinel for a failed CreateFile/NtCreateFile.
    if handle:
        native.close_handle(handle)


def test_relative_create_and_reopen_same_file(tmp_path):
    directory, parent = _parent(tmp_path)
    try:
        status, handle = native.nt_create_file(
            parent.native_handle, 'one.txt', native.FILE_CREATE)
        assert native.is_success(status)
        assert handle
        first = native.get_file_id(handle)
        _close(handle)
        status, handle = native.nt_create_file(
            parent.native_handle, 'one.txt', native.FILE_OPEN)
        try:
            assert native.is_success(status)
            assert native.get_file_id(handle) == first
        finally:
            _close(handle)
    finally:
        parent.close()
    assert (directory / 'one.txt').is_file()


def test_file_create_collision_is_reported(tmp_path):
    _directory, parent = _parent(tmp_path)
    try:
        status, handle = native.nt_create_file(
            parent.native_handle, 'collision.txt', native.FILE_CREATE)
        assert native.is_success(status)
        _close(handle)
        status, handle = native.nt_create_file(
            parent.native_handle, 'collision.txt', native.FILE_CREATE)
        _close(handle)
        assert not native.is_success(status)
        assert (status & 0xFFFFFFFF) == native.STATUS_OBJECT_NAME_COLLISION
    finally:
        parent.close()


@pytest.mark.parametrize('name', [
    '', '.', '..', '..\\outside', '../outside', 'a/b', 'a\\b',
    'C:\\tmp\\x', '\\\\server\\share\\x', 'name.',
])
def test_invalid_relative_names_are_rejected(tmp_path, name):
    _directory, parent = _parent(tmp_path)
    try:
        with pytest.raises(ValueError):
            native.validate_relative_name(name)
    finally:
        parent.close()


def test_relative_rename_and_replace(tmp_path):
    directory, parent = _parent(tmp_path)
    try:
        (directory / 'source.txt').write_text('source', encoding='utf-8')
        status, handle = native.nt_create_file(
            parent.native_handle, 'source.txt', native.FILE_OPEN,
            access=native.DELETE | native.SYNCHRONIZE,
            share=(native.FILE_SHARE_READ | native.FILE_SHARE_WRITE |
                   native.FILE_SHARE_DELETE))
        try:
            status = native.rename_relative(
                handle, parent.native_handle, 'target.txt')
            assert native.is_success(status)
        finally:
            _close(handle)
        assert not (directory / 'source.txt').exists()
        assert (directory / 'target.txt').is_file()
    finally:
        parent.close()


def test_relative_atomic_replace(tmp_path):
    directory, parent = _parent(tmp_path)
    try:
        (directory / 'registry.json').write_text('old', encoding='utf-8')
        (directory / 'registry.tmp').write_text('new', encoding='utf-8')
        status, handle = native.nt_create_file(
            parent.native_handle, 'registry.tmp', native.FILE_OPEN,
            access=native.DELETE | native.SYNCHRONIZE,
            share=(native.FILE_SHARE_READ | native.FILE_SHARE_WRITE |
                   native.FILE_SHARE_DELETE))
        try:
            status = native.rename_relative(
                handle, parent.native_handle, 'registry.json', replace=True)
            assert native.is_success(status)
        finally:
            _close(handle)
        assert (directory / 'registry.json').read_text(encoding='utf-8') == 'new'
        assert not (directory / 'registry.tmp').exists()
    finally:
        parent.close()


def test_relative_delete(tmp_path):
    directory, parent = _parent(tmp_path)
    try:
        (directory / 'delete.txt').write_text('delete', encoding='utf-8')
        status, handle = native.nt_create_file(
            parent.native_handle, 'delete.txt', native.FILE_OPEN,
            access=native.DELETE | native.SYNCHRONIZE,
            share=(native.FILE_SHARE_READ | native.FILE_SHARE_WRITE |
                   native.FILE_SHARE_DELETE))
        try:
            status = native.delete_handle(handle)
            assert native.is_success(status)
        finally:
            _close(handle)
        assert not (directory / 'delete.txt').exists()
    finally:
        parent.close()


def test_relative_junction_open_and_reject_attribute(tmp_path):
    directory, parent = _parent(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = directory / 'junction'
    result = subprocess.run(
        ['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
        capture_output=True, text=True)
    if result.returncode != 0:
        parent.close()
        pytest.skip('mklink /J unavailable: %s' % (
            result.stdout + result.stderr).strip())
    try:
        status, handle = native.nt_create_file(
            parent.native_handle, 'junction', native.FILE_OPEN,
            access=native.SYNCHRONIZE | native.FILE_READ_ATTRIBUTES,
            options=native.FILE_OPEN_REPARSE_POINT,
        )
        try:
            assert native.is_success(status)
            assert native.get_attributes(handle) & (
                native.FILE_ATTRIBUTE_REPARSE_POINT)
        finally:
            _close(handle)
    finally:
        parent.close()
        subprocess.run(['cmd', '/c', 'rmdir', str(link)],
                       capture_output=True, text=True)


def test_handle_close_once_true(tmp_path):
    _directory, parent = _parent(tmp_path)
    try:
        status, handle = native.nt_create_file(
            parent.native_handle, 'close.txt', native.FILE_CREATE)
        assert native.is_success(status)
        native.close_handle_once(handle, owner='test', operation='close')
    finally:
        parent.close()


def test_handle_close_false_is_reported(monkeypatch):
    class _FakeKernel:
        calls = 0

        @classmethod
        def CloseHandle(cls, _handle):
            cls.calls += 1
            return 0

    monkeypatch.setattr(native, 'kernel32', _FakeKernel())
    monkeypatch.setattr(native.ctypes, 'get_last_error', lambda: 6)

    with pytest.raises(native.NativeHandleCloseError) as caught:
        native.close_handle_once(0x1234, owner='UnitTest', operation='close')

    err = caught.value
    assert err.owner == 'UnitTest'
    assert err.operation == 'close'
    assert err.winerror == 6
    assert err.handle_value == 0x1234
    assert err.close_state == 'CLOSE_FAILED_UNKNOWN'
    assert err.error_source == 'CloseHandle'
    assert 'WinError 6' in str(err)
    assert 'UnitTest' in str(err)
    assert _FakeKernel.calls == 1


def test_close_handle_wrapper_raises_on_false(monkeypatch):
    class _FakeKernel:
        @staticmethod
        def CloseHandle(_handle):
            return 0

    monkeypatch.setattr(native, 'kernel32', _FakeKernel())
    monkeypatch.setattr(native.ctypes, 'get_last_error', lambda: 6)

    with pytest.raises(native.NativeHandleCloseError):
        native.close_handle(0x1234)


def test_handle_close_python_exception_does_not_use_stale_winerror(monkeypatch):
    class _FakeKernel:
        @staticmethod
        def CloseHandle(_handle):
            raise RuntimeError('simulated close exception')

    monkeypatch.setattr(native, 'kernel32', _FakeKernel())
    monkeypatch.setattr(native.ctypes, 'get_last_error', lambda: 999)

    with pytest.raises(native.NativeHandleCloseError) as caught:
        native.close_handle_once(0x1234, owner='UnitTest', operation='close')

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert caught.value.winerror is None
    assert caught.value.error_source == 'python_exception'


def test_handle_close_non_exception_is_not_wrapped(monkeypatch):
    class _FakeKernel:
        @staticmethod
        def CloseHandle(_handle):
            raise KeyboardInterrupt('close interrupted')

    monkeypatch.setattr(native, 'kernel32', _FakeKernel())

    with pytest.raises(KeyboardInterrupt):
        native.close_handle_once(0x1234, owner='UnitTest', operation='close')


def test_handle_close_missing_winerror_still_raises(monkeypatch):
    class _FakeKernel:
        @staticmethod
        def CloseHandle(_handle):
            return 0

    def fail_last_error():
        raise OSError('last error unavailable')

    monkeypatch.setattr(native, 'kernel32', _FakeKernel())
    monkeypatch.setattr(native.ctypes, 'get_last_error', fail_last_error)

    with pytest.raises(native.NativeHandleCloseError) as caught:
        native.close_handle_once(0x1234, owner='UnitTest', operation='close')

    assert caught.value.winerror is None
    assert caught.value.error_source == 'CloseHandle'
    assert 'no WinError' in str(caught.value)


def test_close_handle_once_none_is_noop():
    assert native.close_handle_once(None) is None


def test_close_handle_once_zero_is_invalid():
    with pytest.raises(native.NativeHandleCloseError) as caught:
        native.close_handle_once(0, owner='UnitTest', operation='close')

    assert caught.value.error_source == 'invalid_handle'
    assert caught.value.winerror is None
    assert caught.value.handle_value == 0


def test_close_handle_once_void_pointer_none_is_invalid():
    with pytest.raises(native.NativeHandleCloseError) as caught:
        native.close_handle_once(
            ctypes.c_void_p(None), owner='UnitTest', operation='close')

    assert caught.value.error_source == 'invalid_handle'
    assert caught.value.winerror is None


def test_native_close_error_aggregate_fields():
    first = native.NativeHandleCloseError(
        operation='close', owner='first', winerror=6, handle_value=1)
    second = native.NativeHandleCloseError(
        operation='close', owner='second', winerror=6, handle_value=2)
    aggregate = native.NativeHandleCloseError(
        operation='cleanup', owner='aggregate', errors=(first, second))

    assert aggregate.errors == (first, second)
    assert aggregate.data_committed is None
    assert '2 close error(s)' in str(aggregate)


def test_native_close_error_data_committed_message():
    err = native.NativeHandleCloseError(
        operation='close_after_commit', owner='registry-temp',
        winerror=None, handle_value=1, data_committed=True)
    assert err.data_committed is True
    assert 'data_committed=true' in str(err)


def test_ntstatus_error_is_reported(tmp_path):
    _directory, parent = _parent(tmp_path)
    try:
        status, handle = native.nt_create_file(
            parent.native_handle, 'missing.txt', native.FILE_OPEN)
        _close(handle)
        assert not native.is_success(status)
        assert '0x' in native.status_message(status)
    finally:
        parent.close()
