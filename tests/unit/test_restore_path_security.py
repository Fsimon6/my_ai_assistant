# -*- coding: utf-8 -*-
"""P2-A 第七轮：Windows 目录身份与路径安全基础模块测试。

所有测试只使用 pytest 的 tmp_path；不读取正式数据库、Chroma、Excel。
需要 junction 的测试在无法创建 junction 时明确 skip。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / 'scripts' / 'restore_path_security.py'


def _load_module():
    module_name = '_restore_path_security_for_tests'
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


path_security = _load_module()


def _make_junction(link: Path, target: Path) -> None:
    if sys.platform != 'win32':
        pytest.skip('Windows junction test')
    result = subprocess.run(
        ['cmd', '/c', 'mklink', '/J', str(link), str(target)],
        capture_output=True, text=True)
    if result.returncode != 0:
        pytest.skip('mklink /J unavailable: %s' % (
            result.stdout + result.stderr).strip())


def _remove_junction(link: Path) -> None:
    if link.exists() or link.is_symlink():
        subprocess.run(
            ['cmd', '/c', 'rmdir', str(link)],
            capture_output=True, text=True)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_open_normal_ntfs_directory(tmp_path):
    handle = path_security.open_secure_directory(tmp_path)
    try:
        identity = handle.identity
        assert identity.filesystem == 'NTFS'
        assert identity.drive_type == path_security.DRIVE_FIXED
        assert identity.volume_serial > 0
        assert len(identity.file_id) == 32
        assert identity.final_path.startswith('\\\\?\\Volume{')
        assert handle.closed is False
    finally:
        handle.close()
    assert handle.closed is True


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_same_directory_different_path_forms_has_same_identity(tmp_path):
    child = tmp_path / 'child'
    child.mkdir()
    first = path_security.validate_target_directory(tmp_path)
    second = path_security.validate_target_directory(child / '..')
    assert first.volume_serial == second.volume_serial
    assert first.file_id == second.file_id


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_different_directories_have_different_identity(tmp_path):
    first_dir = tmp_path / 'first'
    second_dir = tmp_path / 'second'
    first_dir.mkdir()
    second_dir.mkdir()
    first = path_security.validate_target_directory(first_dir)
    second = path_security.validate_target_directory(second_dir)
    assert (first.volume_serial, first.file_id) != (
        second.volume_serial, second.file_id)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_file_path_is_rejected(tmp_path):
    file_path = tmp_path / 'not-a-directory.txt'
    file_path.write_text('x', encoding='utf-8')
    with pytest.raises(path_security.NotDirectoryError):
        path_security.validate_target_directory(file_path)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_missing_path_is_rejected(tmp_path):
    with pytest.raises(path_security.PathNotFoundError):
        path_security.validate_target_directory(tmp_path / 'missing')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_unc_and_volume_guid_inputs_are_rejected(tmp_path):
    with pytest.raises(path_security.UnsupportedPathError):
        path_security.validate_target_directory('\\\\server\\share\\path')
    with pytest.raises(path_security.UnsupportedPathError):
        path_security.validate_target_directory(
            '\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_relative_path_is_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(path_security.UnsupportedPathError):
        path_security.validate_target_directory('relative-dir')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_handle_close_is_idempotent(tmp_path):
    handle = path_security.open_secure_directory(tmp_path)
    handle.close()
    handle.close()
    assert handle.closed is True


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_junction_target_is_rejected(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = tmp_path / 'junction-target'
    try:
        _make_junction(link, outside)
        with pytest.raises(path_security.ReparsePointError):
            path_security.validate_target_directory(link)
    finally:
        _remove_junction(link)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_junction_ancestor_is_rejected(tmp_path):
    outside = tmp_path / 'outside-ancestor'
    child = outside / 'child'
    child.mkdir(parents=True)
    link = tmp_path / 'junction-ancestor'
    try:
        _make_junction(link, outside)
        with pytest.raises(path_security.ReparsePointError):
            path_security.validate_target_directory(link / 'child')
    finally:
        _remove_junction(link)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_symlink_target_is_rejected_if_privilege_available(tmp_path):
    outside = tmp_path / 'outside-symlink'
    outside.mkdir()
    link = tmp_path / 'symlink-target'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip('Windows symlink privilege unavailable: %s' % exc)
    try:
        with pytest.raises(path_security.ReparsePointError):
            path_security.validate_target_directory(link)
    finally:
        link.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_unsupported_filesystem_is_rejected_by_mock(tmp_path, monkeypatch):
    monkeypatch.setattr(
        path_security, '_volume_info',
        lambda _path, _final: ('ReFS', path_security.DRIVE_FIXED, 0))
    with pytest.raises(path_security.UnsupportedFilesystemError):
        path_security.validate_target_directory(tmp_path)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_remote_filesystem_is_rejected_by_mock(tmp_path, monkeypatch):
    monkeypatch.setattr(
        path_security, '_volume_info',
        lambda _path, _final: ('NTFS', 4, 0))
    with pytest.raises(path_security.UnsupportedFilesystemError):
        path_security.validate_target_directory(tmp_path)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only module')
def test_file_id_failure_is_fail_closed_and_handle_is_closed(tmp_path, monkeypatch):
    closed = []

    class _FakeKernel:
        @staticmethod
        def CloseHandle(handle):
            closed.append(handle)
            return True

    monkeypatch.setattr(path_security, '_validate_ancestors', lambda _path: None)
    monkeypatch.setattr(
        path_security, '_open_directory_handle',
        lambda _path: (_FakeKernel(), 12345))
    monkeypatch.setattr(
        path_security, '_file_attributes',
        lambda _path, _handle: path_security.FILE_ATTRIBUTE_DIRECTORY)

    def fail_fid(_path, _handle):
        raise path_security.UnsupportedPathError('simulated FileId failure')

    monkeypatch.setattr(path_security, '_file_id_info', fail_fid)
    with pytest.raises(path_security.UnsupportedPathError):
        path_security.validate_target_directory(tmp_path)
    assert closed == [12345]
