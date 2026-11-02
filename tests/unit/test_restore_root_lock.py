# -*- coding: utf-8 -*-
"""P2-A 第九轮：Root Lock + Registry 组合测试。

所有目录都在 pytest tmp_path；不创建正式 Registry，不访问正式数据。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import restore_registry as registry  # noqa: E402
from restore_root_lock import RootLock  # noqa: E402


def _reg(tmp_path: Path) -> Path:
    return tmp_path / 'registry-root'


def test_same_target_root_lock_exclusive(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    root = _reg(tmp_path)
    first = RootLock(target, root)
    first.acquire('run-a')
    second = RootLock(target, root)
    try:
        with pytest.raises(registry.RegistryLockBusy):
            second.acquire('run-b')
    finally:
        first.release()
    retry = RootLock(target, root)
    retry.acquire('run-b')
    retry.release()


def test_release_allows_next_registration(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    first = RootLock(target, _reg(tmp_path))
    first.acquire('run-a')
    first.release()
    second = RootLock(target, _reg(tmp_path))
    second.acquire('run-b')
    second.release()


def test_parent_child_root_lock_conflict(tmp_path):
    outer = tmp_path / 'outer'
    child = outer / 'child'
    child.mkdir(parents=True)
    outer_lock = RootLock(outer, _reg(tmp_path))
    outer_lock.acquire('outer')
    child_lock = RootLock(child, _reg(tmp_path))
    try:
        with pytest.raises(registry.RegistryConflictError):
            child_lock.acquire('child')
    finally:
        outer_lock.release()
    retry = RootLock(child, _reg(tmp_path))
    retry.acquire('child')
    retry.release()


def test_child_then_parent_conflict(tmp_path):
    outer = tmp_path / 'outer'
    child = outer / 'child'
    child.mkdir(parents=True)
    child_lock = RootLock(child, _reg(tmp_path))
    child_lock.acquire('child')
    outer_lock = RootLock(outer, _reg(tmp_path))
    try:
        with pytest.raises(registry.RegistryConflictError):
            outer_lock.acquire('outer')
    finally:
        child_lock.release()
    retry = RootLock(outer, _reg(tmp_path))
    retry.acquire('outer')
    retry.release()


def test_three_level_nested_conflict(tmp_path):
    outer = tmp_path / 'outer'
    middle = outer / 'middle'
    inner = middle / 'inner'
    inner.mkdir(parents=True)
    first = RootLock(outer, _reg(tmp_path))
    first.acquire('outer')
    try:
        with pytest.raises(registry.RegistryConflictError):
            RootLock(middle, _reg(tmp_path)).acquire('middle')
        with pytest.raises(registry.RegistryConflictError):
            RootLock(inner, _reg(tmp_path)).acquire('inner')
    finally:
        first.release()


def test_non_overlapping_root_locks_parallel(tmp_path):
    first_dir = tmp_path / 'first'
    second_dir = tmp_path / 'second'
    first_dir.mkdir()
    second_dir.mkdir()
    first = RootLock(first_dir, _reg(tmp_path))
    second = RootLock(second_dir, _reg(tmp_path))
    first.acquire('first')
    try:
        second.acquire('second')
        second.release()
    finally:
        first.release()


def test_same_identity_via_different_path_form(tmp_path):
    target = tmp_path / 'target'
    child = target / 'child'
    child.mkdir(parents=True)
    first = RootLock(target, _reg(tmp_path))
    first.acquire('first')
    second = RootLock(child / '..', _reg(tmp_path))
    try:
        with pytest.raises(registry.RegistryLockBusy):
            second.acquire('second')
    finally:
        first.release()


def test_registry_corrupt_fails_closed(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    registry_root = _reg(tmp_path)
    registry_root.mkdir()
    (registry_root / 'registry.json').write_text('{bad', encoding='utf-8')
    lock = RootLock(target, registry_root)
    with pytest.raises(registry.RegistryCorruptError):
        lock.acquire('run')
    # A later valid registry can be used only after manual repair.
    (registry_root / 'registry.json').write_text(json.dumps({
        'schema_version': 1, 'generation': 0, 'entries': []
    }), encoding='utf-8')
    second = RootLock(target, registry_root)
    second.acquire('run-2')
    second.release()


def test_registry_lock_busy_releases_root_lock(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    root = _reg(tmp_path)
    holder = registry.RegistryStore(root)
    holder.prepare()
    registry_lock = registry.RegistryLock(
        root / 'registry.lock',
        parent_handle=holder.root_handle.native_handle,
        parent_identity=holder.root_identity,
    )
    registry_lock.acquire()
    lock = RootLock(target, root)
    try:
        with pytest.raises(registry.RegistryLockBusy):
            lock.acquire('run')
    finally:
        registry_lock.release()
        holder.close()
    retry = RootLock(target, root)
    retry.acquire('run-2')
    retry.release()


def test_root_lock_release_is_idempotent(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('run')
    lock.release()
    lock.release()


def _child_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / ('root_lock_child_%s.py' % os.getpid())
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, %r)\n"
        "from restore_root_lock import RootLock\n"
        "from restore_registry import RegistryLockBusy\n"
        "root = Path(sys.argv[1])\n"
        "reg = Path(sys.argv[2])\n"
        "%s\n" % (str(SCRIPTS), body),
        encoding='utf-8')
    return script


def test_second_process_root_lock_busy(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    root = _reg(tmp_path)
    script = _child_script(
        tmp_path,
        "lock = RootLock(root, reg)\n"
        "lock.acquire('child')\n"
        "print('LOCKED', flush=True)\n"
        "import time\n"
        "time.sleep(30)\n")
    process = subprocess.Popen(
        [sys.executable, str(script), str(target), str(root)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = process.stdout.readline()
        assert 'LOCKED' in line, process.stderr.read()
        with pytest.raises(registry.RegistryLockBusy):
            RootLock(target, root).acquire('parent')
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_crash_stale_registry_is_reprobed(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    root = _reg(tmp_path)
    script = _child_script(
        tmp_path,
        "lock = RootLock(root, reg)\n"
        "lock.acquire('child')\n"
        "print('LOCKED', flush=True)\n"
        "import os\n"
        "os._exit(0)\n")
    result = subprocess.run(
        [sys.executable, str(script), str(target), str(root)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'LOCKED' in result.stdout
    lock = RootLock(target, root)
    lock.acquire('parent')
    lock.release()
