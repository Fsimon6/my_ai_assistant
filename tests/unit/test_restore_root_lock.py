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

import restore_path_security as path_security  # noqa: E402
import restore_registry as registry  # noqa: E402
import restore_root_lock as root_lock_module  # noqa: E402
from restore_root_lock import RootLock, RootLockReleaseError  # noqa: E402


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


def test_release_failure_closes_all_resources_and_os_lock(tmp_path, monkeypatch):
    target = tmp_path / 'release-failure-target'
    target.mkdir()
    registry_root = _reg(tmp_path)
    lock = RootLock(target, registry_root)
    lock.acquire('release-failure')

    def fail_root_release():
        raise RuntimeError('simulated root lock release failure')

    monkeypatch.setattr(lock.root_lock, 'release', fail_root_release)
    with pytest.raises(RootLockReleaseError) as caught:
        lock.release()
    assert any(name == 'root_lock' for name, _ in caught.value.errors)
    # abort() must force-close the native handle even though release raised.
    assert lock.root_lock._handle is None
    assert lock.target.closed
    assert lock.registry.root_handle is None
    assert lock.registry.roots_handle is None

    retry = RootLock(target, registry_root)
    retry.acquire('after-release-failure')
    retry.release()


def test_registry_close_failure_still_closes_owned_target(tmp_path, monkeypatch):
    target = tmp_path / 'registry-close-failure-target'
    target.mkdir()
    registry_root = _reg(tmp_path)
    lock = RootLock(target, registry_root)
    lock.acquire('registry-close-failure')

    original_close = lock.registry.close

    def fail_registry_close():
        raise RuntimeError('simulated registry close failure')

    monkeypatch.setattr(lock.registry, 'close', fail_registry_close)
    with pytest.raises(RootLockReleaseError) as caught:
        lock.release()
    assert any(name == 'registry' for name, _ in caught.value.errors)
    assert lock.target.closed
    # Restore and explicitly close resources that the injected close skipped.
    monkeypatch.setattr(lock.registry, 'close', original_close)
    lock.registry.close()


def test_caller_owned_target_is_not_closed_by_root_lock(tmp_path, monkeypatch):
    target = tmp_path / 'caller-owned-target'
    target.mkdir()
    registry_root = _reg(tmp_path)
    handle = __import__('restore_path_security').open_secure_directory(target)
    lock = RootLock.from_target_handle(handle, registry_root=registry_root)
    lock.acquire('caller-owned')

    def fail_registry_close():
        raise RuntimeError('simulated registry close failure')

    monkeypatch.setattr(lock.registry, 'close', fail_registry_close)
    with pytest.raises(RootLockReleaseError):
        lock.release()
    assert not handle.closed
    handle.close()


def test_multiple_release_failures_are_all_reported(tmp_path, monkeypatch):
    target = tmp_path / 'multi-release-failure-target'
    target.mkdir()
    registry_root = _reg(tmp_path)
    lock = RootLock(target, registry_root)
    lock.acquire('multi-failure')
    original_target_close = lock.target.close

    def fail_root_release():
        raise RuntimeError('root lock release failure')

    def fail_registry_close():
        raise RuntimeError('registry close failure')

    def fail_target_close():
        raise RuntimeError('target close failure')

    monkeypatch.setattr(lock, '_cleanup_root_lock', lambda errors: errors.append(
        ('root_lock', RuntimeError('root lock release failure'))))
    monkeypatch.setattr(lock, '_cleanup_registry', lambda errors: errors.append(
        ('registry', RuntimeError('registry close failure'))))
    monkeypatch.setattr(lock, '_cleanup_target', lambda errors: errors.append(
        ('target', RuntimeError('target close failure'))))
    with pytest.raises(RootLockReleaseError) as caught:
        lock.release()
    assert {name for name, _ in caught.value.errors} == {
        'root_lock', 'registry', 'target'}
    # Restore the real close functions and release underlying resources.
    lock._cleanup_root_lock = lambda errors: RootLock._cleanup_root_lock(lock, errors)
    lock._cleanup_registry = lambda errors: RootLock._cleanup_registry(lock, errors)
    lock._cleanup_target = lambda errors: RootLock._cleanup_target(lock, errors)
    lock._held = True
    lock._cleanup_root_lock([])
    lock._cleanup_registry([])
    lock._cleanup_target([])


def test_acquire_failure_registry_close_failure_still_closes_target(
        tmp_path, monkeypatch):
    target = tmp_path / 'acquire-cleanup-target'
    target.mkdir()
    registry_root = _reg(tmp_path)
    holder = RootLock(target, registry_root)
    holder.acquire('holder')
    second = RootLock(target, registry_root)

    def fail_registry_close():
        raise RuntimeError('simulated acquire cleanup close failure')

    monkeypatch.setattr(second.registry, 'close', fail_registry_close)
    try:
        with pytest.raises(RootLockReleaseError):
            second.acquire('second')
        assert second.target.closed
    finally:
        holder.release()


def test_poisoned_registry_is_reported_on_release(tmp_path):
    target = tmp_path / 'poisoned-registry-target'
    target.mkdir()
    registry_root = _reg(tmp_path)
    lock = RootLock(target, registry_root)
    lock.acquire('poisoned-registry')

    # Simulate a prior unrecoverable close failure: handles are already gone,
    # while the Store correctly remains POISONED.
    lock.registry._close_handles()
    lock.registry._state = registry.RegistryStore.POISONED

    with pytest.raises(RootLockReleaseError) as caught:
        lock.release()

    assert any(name == 'registry' for name, _ in caught.value.errors)
    assert lock.target.closed
    assert lock.registry.poisoned is True

    retry = RootLock(target, registry_root)
    retry.acquire('after-poisoned-release')
    retry.release()


def _with_release_failure(lock, monkeypatch, error):
    original_release = lock.release

    def release_then_fail():
        original_release()
        raise error

    monkeypatch.setattr(lock, 'release', release_then_fail)


def _root_lock_release_interrupt(lock, monkeypatch, error):
    original_release = lock.root_lock.release

    def release_then_interrupt():
        original_release()
        raise error

    monkeypatch.setattr(lock.root_lock, 'release', release_then_interrupt)


def _registry_close_interrupt(lock, monkeypatch, error):
    original_close = lock.registry.close

    def close_then_interrupt():
        original_close()
        raise error

    monkeypatch.setattr(lock.registry, 'close', close_then_interrupt)


def _target_close_interrupt(lock, monkeypatch, error):
    original_close = lock.target.close

    def close_then_interrupt():
        original_close()
        raise error

    monkeypatch.setattr(lock.target, 'close', close_then_interrupt)


def test_release_mixed_ordinary_and_interrupt_preserves_both(
        tmp_path, monkeypatch):
    target = tmp_path / 'mixed-cleanup-target'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('mixed-cleanup')
    ordinary = RuntimeError('root lock ordinary cleanup failure')
    interrupt = KeyboardInterrupt('registry cleanup interrupt')
    _root_lock_release_interrupt(lock, monkeypatch, ordinary)
    _registry_close_interrupt(lock, monkeypatch, interrupt)
    calls = _track_cleanup_calls(
        lock, monkeypatch, _resource_handle_snapshot(lock))

    with pytest.raises(KeyboardInterrupt) as caught:
        lock.release()

    assert caught.value is interrupt
    assert interrupt._root_lock_cleanup_errors == (
        ('root_lock', ordinary),)
    assert calls['root_lock_release'] == 1
    assert calls['registry_close'] == 1
    assert calls['target_close'] == 1
    assert len(calls['native']) == 4
    _assert_owned_cleanup_counts(calls)
    assert lock._held is False
    assert lock.root_lock._handle is None
    assert lock.registry.closed
    assert lock.target.closed


def test_release_multiple_cleanup_interrupts_propagate_first(
        tmp_path, monkeypatch):
    target = tmp_path / 'multiple-interrupt-target'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('multiple-interrupt')
    first = KeyboardInterrupt('first cleanup interrupt')
    second = SystemExit('second cleanup interrupt')
    _root_lock_release_interrupt(lock, monkeypatch, first)
    _registry_close_interrupt(lock, monkeypatch, second)
    calls = _track_cleanup_calls(
        lock, monkeypatch, _resource_handle_snapshot(lock))

    with pytest.raises(KeyboardInterrupt) as caught:
        lock.release()

    assert caught.value is first
    assert first._root_lock_cleanup_errors == (
        ('registry', second),)
    assert calls['root_lock_release'] == 1
    assert calls['registry_close'] == 1
    assert calls['target_close'] == 1
    assert len(calls['native']) == 4
    _assert_owned_cleanup_counts(calls)
    assert lock._held is False
    assert lock.root_lock._handle is None
    assert lock.registry.closed
    assert lock.target.closed


def test_attach_cleanup_errors_failure_preserves_primary():
    class _NoAttachError(Exception):
        def __setattr__(self, _name, _value):
            raise RuntimeError('setattr unavailable')

    primary = _NoAttachError('primary failure')
    cleanup_error = KeyboardInterrupt('cleanup interrupt')

    root_lock_module._attach_cleanup_errors(
        primary, [('registry', cleanup_error)])

    assert type(primary) is _NoAttachError
    assert primary.args == ('primary failure',)
    assert str(primary) == 'primary failure'
    assert not hasattr(primary, '_root_lock_cleanup_errors')


def _resource_handle_snapshot(lock):
    handles = []
    root_lock_handle = lock.root_lock._handle
    if root_lock_handle is not None:
        handles.append(root_lock_handle[1])
    for handle in (
            lock.registry._root_handle,
            lock.registry._roots_handle,
            lock.target):
        raw = getattr(handle, '_handle', None)
        if raw is not None:
            handles.append(raw)
    return frozenset(handles)


def _track_cleanup_calls(lock, monkeypatch, owned_handles):
    calls = {
        'root_lock_release': 0,
        'registry_close': 0,
        'target_close': 0,
        'native': [],
    }

    def track_method(obj, name, key):
        original = getattr(obj, name)

        def counted(*args, **kwargs):
            calls[key] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(obj, name, counted)

    def track_native(module):
        original = module.close_handle_once

        def counted(handle, owner='unknown', operation='close',
                    data_committed=None):
            if handle in owned_handles:
                calls['native'].append((owner, handle))
            return original(
                handle, owner=owner, operation=operation,
                data_committed=data_committed)

        monkeypatch.setattr(module, 'close_handle_once', counted)

    track_method(lock.root_lock, 'release', 'root_lock_release')
    track_method(lock.registry, 'close', 'registry_close')
    track_method(lock.target, 'close', 'target_close')
    track_native(path_security)
    track_native(registry)
    return calls


def _assert_owned_cleanup_counts(calls):
    assert calls['root_lock_release'] == 1
    assert calls['registry_close'] == 1
    assert calls['target_close'] == 1
    assert len(calls['native']) == 4
    registry_handles = [
        handle for owner, handle in calls['native']
        if owner == 'RegistryLock']
    directory_handles = [
        handle for owner, handle in calls['native']
        if owner == 'DirectoryHandle']
    assert len(registry_handles) == 1
    assert len(directory_handles) == 3
    assert len(set(registry_handles)) == 1
    assert len(set(directory_handles)) == 3


def test_release_root_lock_interrupt_propagates_after_all_cleanup(
        tmp_path, monkeypatch):
    target = tmp_path / 'root-lock-interrupt-target'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('root-lock-interrupt')
    error = KeyboardInterrupt('root lock release interrupted')
    _root_lock_release_interrupt(lock, monkeypatch, error)
    calls = _track_cleanup_calls(
        lock, monkeypatch, _resource_handle_snapshot(lock))

    with pytest.raises(KeyboardInterrupt) as caught:
        lock.release()

    assert caught.value is error
    assert lock._held is False
    assert lock.root_lock._handle is None
    assert lock.registry.closed
    assert lock.target.closed
    assert calls['root_lock_release'] == 1
    assert calls['registry_close'] == 1
    assert calls['target_close'] == 1
    assert len(calls['native']) == 4
    assert sum(owner == 'RegistryLock' for owner, _ in calls['native']) == 1
    assert sum(
        owner == 'DirectoryHandle' for owner, _ in calls['native']) == 3
    _assert_owned_cleanup_counts(calls)


def test_release_registry_interrupt_propagates_after_all_cleanup(
        tmp_path, monkeypatch):
    target = tmp_path / 'registry-interrupt-target'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('registry-interrupt')
    error = KeyboardInterrupt('registry close interrupted')
    _registry_close_interrupt(lock, monkeypatch, error)
    calls = _track_cleanup_calls(
        lock, monkeypatch, _resource_handle_snapshot(lock))

    with pytest.raises(KeyboardInterrupt) as caught:
        lock.release()

    assert caught.value is error
    assert lock._held is False
    assert lock.root_lock._handle is None
    assert lock.registry.closed
    assert lock.target.closed
    assert calls['root_lock_release'] == 1
    assert calls['registry_close'] == 1
    assert calls['target_close'] == 1
    assert len(calls['native']) == 4
    assert sum(owner == 'RegistryLock' for owner, _ in calls['native']) == 1
    assert sum(
        owner == 'DirectoryHandle' for owner, _ in calls['native']) == 3
    _assert_owned_cleanup_counts(calls)


def test_release_target_interrupt_propagates_after_all_cleanup(
        tmp_path, monkeypatch):
    target = tmp_path / 'target-exit-target'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('target-exit')
    error = SystemExit('target close interrupted')
    _target_close_interrupt(lock, monkeypatch, error)
    calls = _track_cleanup_calls(
        lock, monkeypatch, _resource_handle_snapshot(lock))

    with pytest.raises(SystemExit) as caught:
        lock.release()

    assert caught.value is error
    assert lock._held is False
    assert lock.root_lock._handle is None
    assert lock.registry.closed
    assert lock.target.closed
    assert calls['root_lock_release'] == 1
    assert calls['registry_close'] == 1
    assert calls['target_close'] == 1
    assert len(calls['native']) == 4
    assert sum(owner == 'RegistryLock' for owner, _ in calls['native']) == 1
    assert sum(
        owner == 'DirectoryHandle' for owner, _ in calls['native']) == 3
    _assert_owned_cleanup_counts(calls)


def test_root_lock_exit_without_body_propagates_internal_interrupt(
        tmp_path, monkeypatch):
    target = tmp_path / 'exit-internal-interrupt-target'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('exit-internal-interrupt')
    error = KeyboardInterrupt('cleanup interrupted without body')
    _root_lock_release_interrupt(lock, monkeypatch, error)

    with pytest.raises(KeyboardInterrupt) as caught:
        with lock:
            pass

    assert caught.value is error
    assert lock._held is False
    assert lock.root_lock._handle is None
    assert lock.registry.closed
    assert lock.target.closed


def test_root_lock_exit_body_exception_survives_internal_interrupt(
        tmp_path, monkeypatch):
    target = tmp_path / 'exit-body-internal-interrupt-target'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('exit-body-internal-interrupt')
    interrupt = KeyboardInterrupt('cleanup interrupted after body error')
    _root_lock_release_interrupt(lock, monkeypatch, interrupt)

    with pytest.raises(ValueError, match='body value error') as caught:
        with lock:
            raise ValueError('body value error')

    assert caught.value._root_lock_release_errors == (interrupt,)
    assert lock._held is False
    assert lock.root_lock._handle is None
    assert lock.registry.closed
    assert lock.target.closed


def test_acquire_failure_cleanup_interrupt_preserves_primary(
        tmp_path, monkeypatch):
    target = tmp_path / 'acquire-interrupt-target'
    target.mkdir()
    registry_root = _reg(tmp_path)
    holder = RootLock(target, registry_root)
    holder.acquire('holder')
    second = RootLock(target, registry_root)
    interrupt = KeyboardInterrupt('acquire cleanup interrupted')
    _registry_close_interrupt(second, monkeypatch, interrupt)
    try:
        with pytest.raises(registry.RegistryLockBusy) as caught:
            second.acquire('second')
        assert caught.value._root_lock_cleanup_errors == (
            ('registry', interrupt),)
        assert second.target.closed
        assert second.registry.closed
    finally:
        holder.release()


def test_root_lock_exit_preserves_value_error(tmp_path, monkeypatch):
    target = tmp_path / 'exit-value-error'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('exit-value-error')
    error = RuntimeError('release failed')
    _with_release_failure(lock, monkeypatch, error)

    with pytest.raises(ValueError, match='body value error') as caught:
        with lock:
            raise ValueError('body value error')

    assert caught.value._root_lock_release_errors == (error,)
    assert lock.target.closed
    assert lock.registry.closed


def test_root_lock_exit_preserves_keyboard_interrupt(tmp_path, monkeypatch):
    target = tmp_path / 'exit-keyboard-interrupt'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('exit-keyboard-interrupt')
    _with_release_failure(lock, monkeypatch, RuntimeError('release failed'))

    with pytest.raises(KeyboardInterrupt):
        with lock:
            raise KeyboardInterrupt('body interrupt')

    assert lock.target.closed
    assert lock.registry.closed


def test_root_lock_exit_preserves_system_exit(tmp_path, monkeypatch):
    target = tmp_path / 'exit-system-exit'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('exit-system-exit')
    _with_release_failure(lock, monkeypatch, RuntimeError('release failed'))

    with pytest.raises(SystemExit):
        with lock:
            raise SystemExit('body exit')

    assert lock.target.closed
    assert lock.registry.closed


def test_root_lock_exit_attach_failure_preserves_body(tmp_path, monkeypatch):
    target = tmp_path / 'exit-attach-failure'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('exit-attach-failure')
    _with_release_failure(lock, monkeypatch, RuntimeError('release failed'))

    class _NoAttachError(Exception):
        def __setattr__(self, _name, _value):
            raise RuntimeError('setattr unavailable')

    with pytest.raises(_NoAttachError, match='body failure'):
        with lock:
            raise _NoAttachError('body failure')

    assert lock.target.closed


def test_root_lock_exit_without_body_propagates_release_failure(
        tmp_path, monkeypatch):
    target = tmp_path / 'exit-release-failure'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('exit-release-failure')
    _with_release_failure(lock, monkeypatch, RuntimeError('release failed'))

    with pytest.raises(RuntimeError, match='release failed'):
        with lock:
            pass

    assert lock.target.closed
    assert lock.registry.closed


def test_acquire_failure_then_release_is_safe(tmp_path):
    target = tmp_path / 'acquire-failure-release'
    target.mkdir()
    registry_root = _reg(tmp_path)
    holder = RootLock(target, registry_root)
    holder.acquire('holder')
    second = RootLock(target, registry_root)
    try:
        with pytest.raises(registry.RegistryLockBusy):
            second.acquire('second')
        assert second.target.closed
        assert second.registry.closed
        assert second.root_lock._handle is None
        second.release()
        assert second.target.closed
    finally:
        holder.release()


def test_registry_poisoned_by_real_close_then_root_release(
        tmp_path, monkeypatch):
    target = tmp_path / 'poisoned-real-close'
    target.mkdir()
    lock = RootLock(target, _reg(tmp_path))
    lock.acquire('poisoned-real-close')
    registry_lock = lock.registry._lock
    original_release = registry_lock.release

    def fail_release():
        raise RuntimeError('registry lock close failure')

    monkeypatch.setattr(registry_lock, 'release', fail_release)
    with pytest.raises(registry.RegistryLockError):
        lock.registry.close()
    assert lock.registry.poisoned is True

    monkeypatch.setattr(registry_lock, 'release', original_release)
    with pytest.raises(RootLockReleaseError) as caught:
        lock.release()
    assert any(name == 'registry' for name, _ in caught.value.errors)
    assert lock.target.closed


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
