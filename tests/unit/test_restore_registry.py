# -*- coding: utf-8 -*-
"""P2-A 第八轮：单用户 Registry 与重叠检测测试。

所有 Registry、目标目录和进程脚本都在 pytest 临时目录内；
不创建正式 %LOCALAPPDATA% Registry，不访问正式业务数据。
"""
from __future__ import annotations

import ctypes
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

from restore_path_security import validate_target_directory  # noqa: E402
import restore_registry as registry  # noqa: E402
import restore_win32_native as native  # noqa: E402


def _entry(identity, run_id: str):
    return registry.make_entry(
        identity,
        run_id=run_id,
        pid=os.getpid(),
        process_start_utc='2026-10-31T00:00:00Z',
        user_sid='S-1-5-21-test',
        created_at='2026-10-31T00:00:01Z',
    )


def _new_store(tmp_path: Path) -> registry.RegistryStore:
    return registry.RegistryStore(tmp_path / 'registry-root')


def test_registry_missing_file_loads_empty_and_saves(tmp_path):
    store = _new_store(tmp_path)
    with store:
        data = store.load()
        assert data.generation == 0
        assert data.entries == []
        saved = store.save(data)
        assert saved.generation == 1
    assert (tmp_path / 'registry-root' / 'registry.json').is_file()


def test_registry_corrupt_is_fail_closed(tmp_path):
    root = tmp_path / 'registry-root'
    root.mkdir()
    (root / 'registry.json').write_text('{bad', encoding='utf-8')
    store = registry.RegistryStore(root)
    with store:
        with pytest.raises(registry.RegistryCorruptError):
            store.load()


def test_registry_schema_errors_are_rejected(tmp_path):
    root = tmp_path / 'registry-root'
    root.mkdir()
    cases = [
        {},
        {'schema_version': 99, 'generation': 0, 'entries': []},
        {'schema_version': 1, 'generation': -1, 'entries': []},
        {'schema_version': 1, 'generation': 0, 'entries': 'bad'},
    ]
    for payload in cases:
        (root / 'registry.json').write_text(
            json.dumps(payload), encoding='utf-8')
        store = registry.RegistryStore(root)
        with store:
            with pytest.raises(registry.RegistryCorruptError):
                store.load()


def test_registry_atomic_write_failure_keeps_old_file(
        tmp_path, monkeypatch):
    root = tmp_path / 'registry-root'
    root.mkdir()
    (root / 'registry.json').write_text(
        json.dumps({
            'schema_version': 1,
            'generation': 1,
            'entries': [],
        }), encoding='utf-8')
    store = registry.RegistryStore(root)

    def fail_rename(*_args, **_kwargs):
        raise OSError('simulated native replace failure')

    monkeypatch.setattr(registry, 'rename_relative', fail_rename)
    with store:
        data = store.load()
        with pytest.raises(OSError):
            store.save(data)
    persisted = json.loads(
        (root / 'registry.json').read_text(encoding='utf-8'))
    assert persisted['generation'] == 1
    assert not list(root.glob('*.tmp'))


def test_same_directory_and_alias_conflict(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    child = target / 'child'
    child.mkdir()
    identity_a = validate_target_directory(target)
    identity_b = validate_target_directory(child / '..')
    store = _new_store(tmp_path)
    with store:
        store.add_entry(_entry(identity_a, 'run-a'))
        with pytest.raises(registry.RegistryConflictError):
            store.add_entry(_entry(identity_b, 'run-b'))


def test_parent_child_and_three_level_nesting_conflict(tmp_path):
    outer = tmp_path / 'outer'
    middle = outer / 'middle'
    inner = middle / 'inner'
    inner.mkdir(parents=True)
    store = _new_store(tmp_path)
    with store:
        store.add_entry(_entry(validate_target_directory(outer), 'outer'))
        with pytest.raises(registry.RegistryConflictError):
            store.add_entry(_entry(validate_target_directory(middle), 'middle'))
        with pytest.raises(registry.RegistryConflictError):
            store.add_entry(_entry(validate_target_directory(inner), 'inner'))


def test_child_then_parent_conflict(tmp_path):
    outer = tmp_path / 'outer'
    child = outer / 'child'
    child.mkdir(parents=True)
    store = _new_store(tmp_path)
    with store:
        store.add_entry(_entry(validate_target_directory(child), 'child'))
        with pytest.raises(registry.RegistryConflictError):
            store.add_entry(_entry(validate_target_directory(outer), 'outer'))


def test_non_overlapping_directories_can_register(tmp_path):
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    store = _new_store(tmp_path)
    with store:
        store.add_entry(_entry(validate_target_directory(first), 'first'))
        store.add_entry(_entry(validate_target_directory(second), 'second'))
        assert len(store.load().entries) == 2


def test_component_boundary_not_string_prefix(tmp_path):
    data_dir = tmp_path / 'data'
    database_dir = tmp_path / 'database'
    data_dir.mkdir()
    database_dir.mkdir()
    store = _new_store(tmp_path)
    with store:
        store.add_entry(_entry(validate_target_directory(data_dir), 'data'))
        store.add_entry(_entry(validate_target_directory(database_dir), 'database'))
        assert len(store.load().entries) == 2


def test_duplicate_run_id_rejected(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    identity = validate_target_directory(target)
    store = _new_store(tmp_path)
    with store:
        store.add_entry(_entry(identity, 'same-run'))
        with pytest.raises(registry.RegistryConflictError):
            store.add_entry(_entry(identity, 'same-run'))


def test_remove_entry_only_removes_own_run(tmp_path):
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    store = _new_store(tmp_path)
    with store:
        store.add_entry(_entry(validate_target_directory(first), 'first'))
        store.add_entry(_entry(validate_target_directory(second), 'second'))
        store.remove_entry('first')
        remaining = store.load().entries
        assert [entry.run_id for entry in remaining] == ['second']


def test_registry_lock_busy_blocks_store(tmp_path):
    root = tmp_path / 'registry-root'
    holder = registry.RegistryLock(root / 'registry.lock')
    holder.acquire()
    try:
        with pytest.raises(registry.RegistryLockBusy):
            with registry.RegistryStore(root):
                pass
    finally:
        holder.release()


def test_registry_store_prepare_holds_root_and_roots_identities(tmp_path):
    root = tmp_path / 'registry-root'
    store = registry.RegistryStore(root)
    assert store.prepare() is store
    try:
        assert store.root_handle is not None
        assert store.roots_handle is not None
        assert store.root_identity is not None
        assert store.roots_identity is not None
        assert store.root_handle.closed is False
        assert store.roots_handle.closed is False
        assert store.prepare() is store
    finally:
        store.close()
    assert store.root_handle is None
    assert store.roots_handle is None


def test_registry_lock_uses_relative_parent_handle(tmp_path):
    root = tmp_path / 'registry-root'
    store = registry.RegistryStore(root)
    store.prepare()
    try:
        lock = registry.RegistryLock(
            root / 'registry.lock',
            parent_handle=store.root_handle.native_handle,
            parent_identity=store.root_identity,
        )
        lock.acquire()
        try:
            assert lock.identity is not None
            assert lock.parent_identity == store.root_identity
        finally:
            lock.release()
    finally:
        store.close()


def test_registry_lock_rejects_replaced_lock_file(tmp_path):
    root = tmp_path / 'registry-root'
    store = registry.RegistryStore(root)
    store.prepare()
    try:
        lock = registry.RegistryLock(
            root / 'registry.lock',
            parent_handle=store.root_handle.native_handle,
            parent_identity=store.root_identity,
        )
        lock.acquire()
        lock.release()
        lock_path = root / 'registry.lock'
        lock_path.unlink()
        lock_path.write_text('replacement', encoding='utf-8')
        replaced = registry.RegistryLock(
            lock_path,
            parent_handle=store.root_handle.native_handle,
            parent_identity=store.root_identity,
        )
        with pytest.raises(registry.RegistryLockError):
            replaced.acquire()
    finally:
        store.close()


def test_registry_lock_rejects_missing_identity_record(tmp_path):
    root = tmp_path / 'registry-root'
    store = registry.RegistryStore(root)
    store.prepare()
    try:
        lock = registry.RegistryLock(
            root / 'registry.lock',
            parent_handle=store.root_handle.native_handle,
            parent_identity=store.root_identity,
        )
        lock.acquire()
        lock.release()
        (root / 'registry.lock.identity').unlink()
        second = registry.RegistryLock(
            root / 'registry.lock',
            parent_handle=store.root_handle.native_handle,
            parent_identity=store.root_identity,
        )
        with pytest.raises(registry.RegistryLockError):
            second.acquire()
    finally:
        store.close()


def test_registry_lock_identity_bound_to_handle(tmp_path):
    root = tmp_path / 'registry-root'
    lock = registry.RegistryLock(root / 'registry.lock')
    lock.acquire()
    try:
        assert lock.identity is not None
        volume_serial, file_id = lock.identity
        assert volume_serial > 0
        assert len(file_id) == 32
    finally:
        lock.release()
    assert lock.identity is None


def _make_junction(link: Path, target: Path) -> bool:
    link.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ['cmd', '/c', 'mklink', '/J', str(link), str(target)],
        capture_output=True, text=True)
    return result.returncode == 0


def _remove_junction(link: Path) -> None:
    if link.exists() or link.is_symlink():
        subprocess.run(
            ['cmd', '/c', 'rmdir', str(link)],
            capture_output=True, text=True)


def test_registry_root_reparse_rejected(tmp_path):
    outside = tmp_path / 'outside-registry'
    outside.mkdir()
    link = tmp_path / 'registry-root'
    if not _make_junction(link, outside):
        pytest.skip('mklink /J unavailable')
    try:
        with pytest.raises(Exception):
            with registry.RegistryStore(link):
                pass
    finally:
        _remove_junction(link)


def test_registry_roots_directory_reparse_rejected(tmp_path):
    root = tmp_path / 'registry-root'
    root.mkdir()
    outside = tmp_path / 'outside-roots'
    outside.mkdir()
    link = root / 'roots'
    if not _make_junction(link, outside):
        pytest.skip('mklink /J unavailable')
    try:
        with pytest.raises(Exception):
            with registry.RegistryStore(root):
                pass
    finally:
        _remove_junction(link)


def test_registry_lock_second_process_busy(tmp_path):
    root = tmp_path / 'registry-root'
    script = tmp_path / 'hold_registry_live.py'
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, %r)\n"
        "import restore_registry as r\n"
        "store = r.RegistryStore(Path(sys.argv[1]))\n"
        "store.__enter__()\n"
        "print('LOCKED', flush=True)\n"
        "import time\n"
        "time.sleep(30)\n" % str(SCRIPTS),
        encoding='utf-8')
    process = subprocess.Popen(
        [sys.executable, str(script), str(root)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = process.stdout.readline()
        assert 'LOCKED' in line, process.stderr.read()
        with pytest.raises(registry.RegistryLockBusy):
            registry.RegistryLock(root / 'registry.lock').acquire()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    deadline = time.time() + 5
    while True:
        try:
            lock = registry.RegistryLock(root / 'registry.lock')
            lock.acquire()
            lock.release()
            break
        except registry.RegistryLockBusy:
            if time.time() >= deadline:
                raise
            time.sleep(0.05)


def test_registry_lock_cross_process_crash_releases(tmp_path):
    root = tmp_path / 'registry-root'
    source = SCRIPTS / 'restore_registry.py'
    script = tmp_path / 'hold_registry.py'
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, %r)\n"
        "import restore_registry as r\n"
        "store = r.RegistryStore(Path(sys.argv[1]))\n"
        "store.__enter__()\n"
        "print('LOCKED', flush=True)\n"
        "import os\n"
        "os._exit(0)\n" % str(SCRIPTS),
        encoding='utf-8')
    result = subprocess.run(
        [sys.executable, str(script), str(root)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'LOCKED' in result.stdout
    store = registry.RegistryStore(root)
    store.__enter__()
    store.__exit__(None, None, None)


def test_registry_lock_release_allows_next(tmp_path):
    root = tmp_path / 'registry-root'
    lock = registry.RegistryLock(root / 'registry.lock')
    lock.acquire()
    lock.release()
    second = registry.RegistryLock(root / 'registry.lock')
    second.acquire()
    second.release()


def _prepared_store(tmp_path):
    store = registry.RegistryStore(tmp_path / 'registry-root')
    store.prepare()
    return store


def _inject_close_failure(monkeypatch, only_owner=None):
    real_close = registry.close_handle_once
    calls = []

    def close_then_fail(handle, owner='unknown', operation='close',
                        data_committed=None):
        real_close(handle, owner=owner, operation=operation,
                   data_committed=data_committed)
        calls.append(owner)
        if only_owner is None or owner == only_owner:
            raise native.NativeHandleCloseError(
                operation='close', owner=owner, winerror=6,
                handle_value=handle, data_committed=data_committed)

    monkeypatch.setattr(registry, 'close_handle_once', close_then_fail)
    return calls


def test_read_registry_native_close_failure_raises(tmp_path, monkeypatch):
    store = _prepared_store(tmp_path)
    try:
        (store.root / registry.REGISTRY_FILENAME).write_text(
            json.dumps({'schema_version': 1, 'generation': 0, 'entries': []}),
            encoding='utf-8')
        _inject_close_failure(monkeypatch)
        with pytest.raises(native.NativeHandleCloseError):
            registry._read_registry_native(store.root_handle.native_handle)
    finally:
        store.close()


def test_read_expected_identity_close_failure_raises(tmp_path, monkeypatch):
    store = _prepared_store(tmp_path)
    try:
        lock = registry.RegistryLock(store.root / 'registry.lock')
        (store.root / lock.identity_name).write_text(
            json.dumps({
                'schema_version': 1,
                'volume_serial': 1,
                'file_id': 'a' * 32,
            }),
            encoding='utf-8')
        _inject_close_failure(monkeypatch)
        with pytest.raises(native.NativeHandleCloseError):
            lock._read_expected_identity(store.root_handle.native_handle)
    finally:
        store.close()


def test_create_expected_identity_close_failure_raises(tmp_path, monkeypatch):
    store = _prepared_store(tmp_path)
    try:
        lock = registry.RegistryLock(store.root / 'registry.lock')
        _inject_close_failure(monkeypatch)
        with pytest.raises(native.NativeHandleCloseError):
            lock._create_expected_identity(
                store.root_handle.native_handle, (1, 'b' * 32))
        assert (store.root / lock.identity_name).is_file()
    finally:
        store.close()


def test_write_registry_native_close_failure_raises(tmp_path, monkeypatch):
    store = _prepared_store(tmp_path)
    try:
        _inject_close_failure(monkeypatch, only_owner='registry-temp')
        with pytest.raises(native.NativeHandleCloseError) as caught:
            registry._write_registry_native(
                store.root_handle.native_handle,
                {'schema_version': 1, 'generation': 0, 'entries': []})
        assert caught.value.owner == 'registry-temp'
        assert caught.value.operation == 'close'
        assert caught.value.close_state == 'CLOSE_FAILED_UNKNOWN'
        assert caught.value.data_committed is True
        assert 'data_committed=true' in str(caught.value)
        assert (store.root / registry.REGISTRY_FILENAME).is_file()
    finally:
        store.close()


def test_write_registry_target_close_failure_aborts_replace(tmp_path, monkeypatch):
    store = _prepared_store(tmp_path)
    try:
        registry_path = store.root / registry.REGISTRY_FILENAME
        registry_path.write_text('old-registry', encoding='utf-8')
        _inject_close_failure(monkeypatch, only_owner='registry-target')
        with pytest.raises(native.NativeHandleCloseError) as caught:
            registry._write_registry_native(
                store.root_handle.native_handle,
                {'schema_version': 1, 'generation': 0, 'entries': []})
        assert caught.value.owner == 'registry-target'
        assert caught.value.data_committed is False
        assert 'data_committed=false' in str(caught.value)
        assert registry_path.read_text(encoding='utf-8') == 'old-registry'
    finally:
        store.close()


def test_write_registry_collects_both_close_failures(tmp_path, monkeypatch):
    store = _prepared_store(tmp_path)
    try:
        registry_path = store.root / registry.REGISTRY_FILENAME
        registry_path.write_text('old-registry', encoding='utf-8')
        _inject_close_failure(monkeypatch)
        with pytest.raises(native.NativeHandleCloseError) as caught:
            registry._write_registry_native(
                store.root_handle.native_handle,
                {'schema_version': 1, 'generation': 0, 'entries': []})
        err = caught.value
        assert err.owner == 'registry-target'
        assert err.operation == 'close'
        assert err.close_state == 'CLOSE_FAILED_UNKNOWN'
        assert err.data_committed is False
        attached = err._native_close_errors
        assert len(attached) == 1
        assert attached[0].owner == 'registry-temp'
        assert attached[0].operation == 'close'
        assert attached[0].close_state == 'CLOSE_FAILED_UNKNOWN'
        assert attached[0].data_committed is False
        assert registry_path.read_text(encoding='utf-8') == 'old-registry'
    finally:
        store.close()


def test_read_registry_preserves_primary_over_close_failure(tmp_path, monkeypatch):
    store = _prepared_store(tmp_path)
    try:
        (store.root / registry.REGISTRY_FILENAME).write_text('{}', encoding='utf-8')

        def fail_read(_handle):
            raise RuntimeError('primary read failure')

        monkeypatch.setattr(registry, 'read_all', fail_read)
        _inject_close_failure(monkeypatch)
        with pytest.raises(RuntimeError, match='primary read failure') as caught:
            registry._read_registry_native(store.root_handle.native_handle)
        assert caught.value._native_close_errors
    finally:
        store.close()


def test_read_expected_identity_preserves_primary_over_close_failure(
        tmp_path, monkeypatch):
    store = _prepared_store(tmp_path)
    try:
        lock = registry.RegistryLock(store.root / 'registry.lock')
        (store.root / lock.identity_name).write_text(
            '{}', encoding='utf-8')

        def fail_read(_handle):
            raise RuntimeError('identity read failure')

        monkeypatch.setattr(registry, 'read_all', fail_read)
        _inject_close_failure(monkeypatch)
        with pytest.raises(RuntimeError, match='identity read failure') as caught:
            lock._read_expected_identity(store.root_handle.native_handle)
        assert caught.value._native_close_errors
    finally:
        store.close()


def test_create_expected_identity_preserves_primary_over_close_failure(
        tmp_path, monkeypatch):
    store = _prepared_store(tmp_path)
    try:
        lock = registry.RegistryLock(store.root / 'registry.lock')

        def fail_write(_handle, _data):
            raise RuntimeError('identity write failure')

        monkeypatch.setattr(registry, 'write_all', fail_write)
        _inject_close_failure(monkeypatch)
        with pytest.raises(RuntimeError, match='identity write failure') as caught:
            lock._create_expected_identity(
                store.root_handle.native_handle, (1, 'c' * 32))
        assert caught.value._native_close_errors
    finally:
        store.close()


def test_write_registry_rename_failure_preserves_primary(
        tmp_path, monkeypatch):
    store = _prepared_store(tmp_path)
    try:
        registry_path = store.root / registry.REGISTRY_FILENAME
        registry_path.write_text('old-registry', encoding='utf-8')

        def fail_rename(*_args, **_kwargs):
            return -1

        monkeypatch.setattr(registry, 'rename_relative', fail_rename)
        _inject_close_failure(monkeypatch, only_owner='registry-temp')
        with pytest.raises(registry.RegistryCorruptError) as caught:
            registry._write_registry_native(
                store.root_handle.native_handle,
                {'schema_version': 1, 'generation': 0, 'entries': []})
        assert caught.value._native_close_errors
        assert caught.value._native_close_errors[0].data_committed is False
        assert registry_path.read_text(encoding='utf-8') == 'old-registry'
    finally:
        store.close()


def test_raise_close_errors_aggregate_fields():
    first = native.NativeHandleCloseError(
        operation='close', owner='first', winerror=6, handle_value=1)
    second = native.NativeHandleCloseError(
        operation='close', owner='second', winerror=6, handle_value=2)

    with pytest.raises(native.NativeHandleCloseError) as caught:
        registry._raise_close_errors([first, second], 'registry-write')

    assert caught.value.owner == 'registry-write'
    assert caught.value.operation == 'cleanup'
    assert caught.value.errors == (first, second)
    assert [exc.owner for exc in caught.value.errors] == ['first', 'second']


def test_attach_close_errors_failure_preserves_primary():
    class _NoAttachError(Exception):
        def __setattr__(self, _name, _value):
            raise RuntimeError('setattr unavailable')

    primary = _NoAttachError('primary failure')
    close_error = native.NativeHandleCloseError(
        operation='close', owner='test', winerror=6, handle_value=1)

    registry._attach_close_errors(primary, [close_error])

    assert str(primary) == 'primary failure'


class _FakeLockKernel:
    def __init__(self, unlock_error=None):
        self.unlock_error = unlock_error
        self.unlock_calls = 0

    def UnlockFileEx(self, *_args):
        self.unlock_calls += 1
        if self.unlock_error is not None:
            raise self.unlock_error
        return True


class _FakeCloseOnce:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def __call__(self, handle, owner='unknown', operation='close',
                 data_committed=None):
        self.calls.append((handle, owner, operation))
        if self.error is not None:
            raise self.error


def _held_lock(tmp_path, kernel, close_handle=123):
    lock = registry.RegistryLock(tmp_path / 'registry.lock')
    lock._handle = (kernel, close_handle, ctypes.c_int())
    lock.identity = (1, 'a' * 32)
    lock._close_state = registry.RegistryLock.OPEN
    return lock


def test_registry_lock_release_success_state(tmp_path, monkeypatch):
    kernel = _FakeLockKernel()
    close = _FakeCloseOnce()
    monkeypatch.setattr(registry, 'close_handle_once', close)
    lock = _held_lock(tmp_path, kernel)

    lock.release()

    assert kernel.unlock_calls == 1
    assert close.calls == [(123, 'RegistryLock', 'release')]
    assert lock._handle is None
    assert lock.identity is None
    assert lock.close_state == registry.RegistryLock.CLOSED
    assert lock.closed is True
    assert lock.is_held is False

    lock.release()
    lock.abort()
    assert close.calls == [(123, 'RegistryLock', 'release')]


def test_registry_lock_abort_success_does_not_unlock(tmp_path, monkeypatch):
    kernel = _FakeLockKernel()
    close = _FakeCloseOnce()
    monkeypatch.setattr(registry, 'close_handle_once', close)
    lock = _held_lock(tmp_path, kernel)

    lock.abort()

    assert kernel.unlock_calls == 0
    assert close.calls == [(123, 'RegistryLock', 'abort')]
    assert lock.close_state == registry.RegistryLock.CLOSED
    assert lock.closed is True


def test_registry_lock_close_failure_never_retried_by_abort(
        tmp_path, monkeypatch):
    error = native.NativeHandleCloseError(
        operation='release', owner='RegistryLock', winerror=6, handle_value=123)
    close = _FakeCloseOnce(error)
    monkeypatch.setattr(registry, 'close_handle_once', close)
    lock = _held_lock(tmp_path, _FakeLockKernel())

    with pytest.raises(native.NativeHandleCloseError) as caught:
        lock.release()
    assert caught.value is error
    assert close.calls == [(123, 'RegistryLock', 'release')]
    assert lock._handle is None
    assert lock.close_state == registry.RegistryLock.CLOSE_FAILED_UNKNOWN
    assert lock.close_failed is True
    assert lock.closed is False

    lock.abort()
    lock.release()
    assert close.calls == [(123, 'RegistryLock', 'release')]


def test_registry_lock_abort_failure_never_retried(tmp_path, monkeypatch):
    error = native.NativeHandleCloseError(
        operation='abort', owner='RegistryLock', winerror=6, handle_value=123)
    close = _FakeCloseOnce(error)
    monkeypatch.setattr(registry, 'close_handle_once', close)
    lock = _held_lock(tmp_path, _FakeLockKernel())

    with pytest.raises(native.NativeHandleCloseError):
        lock.abort()
    assert close.calls == [(123, 'RegistryLock', 'abort')]
    assert lock.close_state == registry.RegistryLock.CLOSE_FAILED_UNKNOWN

    lock.abort()
    lock.release()
    assert close.calls == [(123, 'RegistryLock', 'abort')]


def test_registry_lock_abort_after_release_is_noop(tmp_path, monkeypatch):
    kernel = _FakeLockKernel()
    close = _FakeCloseOnce()
    monkeypatch.setattr(registry, 'close_handle_once', close)
    lock = _held_lock(tmp_path, kernel)

    lock.release()
    lock.abort()

    assert close.calls == [(123, 'RegistryLock', 'release')]
    assert lock.close_state == registry.RegistryLock.CLOSED


def test_registry_lock_close_keyboard_interrupt_sets_unknown(
        tmp_path, monkeypatch):
    error = KeyboardInterrupt('close interrupted')
    close = _FakeCloseOnce(error)
    monkeypatch.setattr(registry, 'close_handle_once', close)
    lock = _held_lock(tmp_path, _FakeLockKernel())

    with pytest.raises(KeyboardInterrupt):
        lock.abort()
    assert close.calls == [(123, 'RegistryLock', 'abort')]
    assert lock.close_state == registry.RegistryLock.CLOSE_FAILED_UNKNOWN
    assert lock.close_failed is True

    lock.abort()
    lock.release()
    assert close.calls == [(123, 'RegistryLock', 'abort')]


def test_registry_lock_close_system_exit_sets_unknown(
        tmp_path, monkeypatch):
    error = SystemExit('close exit')
    close = _FakeCloseOnce(error)
    monkeypatch.setattr(registry, 'close_handle_once', close)
    lock = _held_lock(tmp_path, _FakeLockKernel())

    with pytest.raises(SystemExit):
        lock.release()
    assert close.calls == [(123, 'RegistryLock', 'release')]
    assert lock.close_state == registry.RegistryLock.CLOSE_FAILED_UNKNOWN
    assert lock.close_failed is True

    lock.abort()
    lock.release()
    assert close.calls == [(123, 'RegistryLock', 'release')]


def test_registry_lock_release_after_abort_is_noop(tmp_path, monkeypatch):
    kernel = _FakeLockKernel()
    close = _FakeCloseOnce()
    monkeypatch.setattr(registry, 'close_handle_once', close)
    lock = _held_lock(tmp_path, kernel)

    lock.abort()
    lock.release()

    assert close.calls == [(123, 'RegistryLock', 'abort')]
    assert lock.close_state == registry.RegistryLock.CLOSED


def test_registry_lock_unheld_release_and_abort_are_noop(tmp_path, monkeypatch):
    close = _FakeCloseOnce()
    monkeypatch.setattr(registry, 'close_handle_once', close)
    lock = registry.RegistryLock(tmp_path / 'registry.lock')

    lock.release()
    lock.abort()

    assert close.calls == []
    assert lock.close_state == registry.RegistryLock.UNHELD


def test_registry_lock_unlock_failure_still_closes(tmp_path, monkeypatch):
    kernel = _FakeLockKernel(unlock_error=RuntimeError('unlock failure'))
    close = _FakeCloseOnce()
    monkeypatch.setattr(registry, 'close_handle_once', close)
    lock = _held_lock(tmp_path, kernel)

    with pytest.raises(RuntimeError, match='unlock failure'):
        lock.release()

    assert close.calls == [(123, 'RegistryLock', 'release')]
    assert lock._handle is None
    assert lock.close_state == registry.RegistryLock.CLOSED
    assert lock.closed is True

    lock.abort()
    assert close.calls == [(123, 'RegistryLock', 'release')]


def test_registry_lock_unlock_and_close_failure_preserve_unlock(tmp_path, monkeypatch):
    unlock_error = RuntimeError('unlock failure')
    close_error = native.NativeHandleCloseError(
        operation='release', owner='RegistryLock', winerror=6, handle_value=123)
    kernel = _FakeLockKernel(unlock_error=unlock_error)
    close = _FakeCloseOnce(close_error)
    monkeypatch.setattr(registry, 'close_handle_once', close)
    lock = _held_lock(tmp_path, kernel)

    with pytest.raises(RuntimeError, match='unlock failure') as caught:
        lock.release()

    assert caught.value._native_close_errors == (close_error,)
    assert lock.close_state == registry.RegistryLock.CLOSE_FAILED_UNKNOWN
    assert lock.close_failed is True
    lock.abort()
    assert close.calls == [(123, 'RegistryLock', 'release')]


def test_registry_lock_acquire_busy_stays_unheld(tmp_path):
    holder = registry.RegistryLock(tmp_path / 'registry.lock')
    holder.acquire()
    try:
        second = registry.RegistryLock(tmp_path / 'registry.lock')
        with pytest.raises(registry.RegistryLockBusy):
            second.acquire()
        assert second._handle is None
        assert second.close_state == registry.RegistryLock.UNHELD
        second.release()
        second.abort()
    finally:
        holder.release()


def test_close_local_preserving_keeps_primary(monkeypatch):
    primary = RuntimeError('acquire failed')
    close_error = native.NativeHandleCloseError(
        operation='close', owner='RegistryLock.acquire',
        winerror=6, handle_value=123)
    close = _FakeCloseOnce(close_error)
    monkeypatch.setattr(registry, 'close_handle_once', close)

    registry._close_local_preserving(123, primary, 'RegistryLock.acquire')

    assert close.calls == [(123, 'RegistryLock.acquire', 'close')]
    assert primary._native_close_errors == (close_error,)


class _FakeStoreHandle:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0
        self.closed = False
        self.identity = object()
        self.native_handle = 123

    def close(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        self.closed = True


class _FakeStoreLock:
    def __init__(self, error=None):
        self.error = error
        self.release_calls = 0
        self.acquire_calls = 0

    def set_parent(self, _handle, _identity):
        pass

    def acquire(self):
        self.acquire_calls += 1

    def release(self):
        self.release_calls += 1
        if self.error is not None:
            raise self.error


def _store_with_handles(tmp_path, roots_error=None, root_error=None,
                        lock_error=None, owns=True):
    store = registry.RegistryStore(tmp_path / 'registry-root')
    store._lock = _FakeStoreLock(lock_error)
    store._root_handle = _FakeStoreHandle(root_error)
    store._roots_handle = _FakeStoreHandle(roots_error)
    store._root_identity = object()
    store._roots_identity = object()
    store._prepared = True
    store._state = registry.RegistryStore.PREPARED
    store._owns_handles = owns
    return store


def _store_handle_error(owner):
    return native.NativeHandleCloseError(
        operation='close', owner=owner, winerror=6, handle_value=123)


def test_registry_store_roots_failure_still_closes_root(
        tmp_path):
    store = _store_with_handles(
        tmp_path, roots_error=_store_handle_error('roots'))
    roots = store.roots_handle
    root = store.root_handle

    with pytest.raises(registry.RegistryLockError) as caught:
        store.close()

    assert roots.calls == 1
    assert root.calls == 1
    assert root.closed is True
    assert store.state == registry.RegistryStore.POISONED
    assert store.poisoned is True
    assert store.root_handle is None
    assert store.roots_handle is None
    assert store._prepared is False
    assert [name for name, _exc in caught.value.errors] == ['roots']


def test_registry_store_root_failure_roots_already_closed(
        tmp_path):
    store = _store_with_handles(
        tmp_path, root_error=_store_handle_error('root'))
    roots = store.roots_handle
    root = store.root_handle

    with pytest.raises(registry.RegistryLockError) as caught:
        store.close()

    assert roots.closed is True
    assert root.calls == 1
    assert store.state == registry.RegistryStore.POISONED
    assert [name for name, _exc in caught.value.errors] == ['root']


def test_registry_store_both_handle_failures_are_aggregated(
        tmp_path):
    store = _store_with_handles(
        tmp_path, roots_error=_store_handle_error('roots'),
        root_error=_store_handle_error('root'))
    roots = store.roots_handle
    root = store.root_handle

    with pytest.raises(registry.RegistryLockError) as caught:
        store.close()

    assert roots.calls == 1
    assert root.calls == 1
    assert store.state == registry.RegistryStore.POISONED
    assert [name for name, _exc in caught.value.errors] == ['roots', 'root']
    assert caught.value.errors[0][1].owner == 'roots'
    assert caught.value.errors[1][1].owner == 'root'


def test_registry_store_close_success_is_idempotent(tmp_path):
    store = _store_with_handles(tmp_path)
    roots = store.roots_handle
    root = store.root_handle

    store.close()
    assert store.state == registry.RegistryStore.CLOSED
    assert store.closed is True
    assert store._prepared is False
    assert store._owns_handles is False
    assert store.root_handle is None
    assert store.roots_handle is None
    assert roots.calls == 1
    assert root.calls == 1

    store.close()
    assert roots.calls == 1
    assert root.calls == 1
    assert store.state == registry.RegistryStore.CLOSED


def test_registry_store_poisoned_close_does_not_retry(tmp_path):
    store = _store_with_handles(
        tmp_path, roots_error=_store_handle_error('roots'))
    roots = store.roots_handle
    root = store.root_handle

    with pytest.raises(registry.RegistryLockError):
        store.close()
    assert roots.calls == 1
    assert root.calls == 1
    assert store.state == registry.RegistryStore.POISONED

    store.close()
    assert roots.calls == 1
    assert root.calls == 1
    assert store.state == registry.RegistryStore.POISONED


def test_registry_store_poisoned_rejects_prepare(tmp_path):
    store = registry.RegistryStore(tmp_path / 'registry-root')
    store._state = registry.RegistryStore.POISONED

    with pytest.raises(registry.RegistryLockError, match='POISONED'):
        store.prepare()
    assert store.state == registry.RegistryStore.POISONED


def test_registry_store_body_exception_wins_over_close_error(
        tmp_path):
    store = _store_with_handles(
        tmp_path, roots_error=_store_handle_error('roots'))
    store._context_owns = True

    with pytest.raises(RuntimeError, match='body failure') as caught:
        with store:
            raise RuntimeError('body failure')

    assert caught.value._native_close_errors
    assert store.state == registry.RegistryStore.POISONED
    assert store.root_handle is None
    assert store.roots_handle is None


def test_registry_store_attach_failure_does_not_override_body(
        tmp_path, monkeypatch):
    store = _store_with_handles(
        tmp_path, roots_error=_store_handle_error('roots'))
    store._context_owns = True

    def fail_attach(*_args, **_kwargs):
        raise RuntimeError('attach failed')

    monkeypatch.setattr(registry, '_safe_attach_close_errors', fail_attach)
    with pytest.raises(RuntimeError, match='body failure'):
        with store:
            raise RuntimeError('body failure')
    assert store.state == registry.RegistryStore.POISONED


def test_registry_store_lock_and_both_handles_fail(tmp_path):
    lock_error = RuntimeError('lock release failed')
    store = _store_with_handles(
        tmp_path, roots_error=_store_handle_error('roots'),
        root_error=_store_handle_error('root'), lock_error=lock_error)

    with pytest.raises(registry.RegistryLockError) as caught:
        store.close()

    assert [name for name, _exc in caught.value.errors] == [
        'registry_lock', 'roots', 'root']
    assert caught.value.errors[0][1] is lock_error
    assert store.state == registry.RegistryStore.POISONED
    assert store._locked is False


def test_registry_store_external_handles_not_closed(tmp_path):
    store = _store_with_handles(tmp_path, owns=False)
    roots = store.roots_handle
    root = store.root_handle

    store.close()

    assert roots.calls == 0
    assert root.calls == 0
    assert store.state == registry.RegistryStore.CLOSED
    assert store.root_handle is None
    assert store.roots_handle is None


def test_registry_store_close_order_roots_then_root(tmp_path):
    order = []

    class _OrderHandle(_FakeStoreHandle):
        def __init__(self, name):
            super().__init__()
            self.name = name

        def close(self):
            order.append(self.name)
            super().close()

    store = _store_with_handles(tmp_path)
    store._roots_handle = _OrderHandle('roots')
    store._root_handle = _OrderHandle('root')

    store.close()

    assert order == ['roots', 'root']


def test_registry_store_keyboard_interrupt_cleans_both(tmp_path):
    store = _store_with_handles(
        tmp_path, roots_error=KeyboardInterrupt('roots interrupted'))
    roots = store.roots_handle
    root = store.root_handle

    with pytest.raises(KeyboardInterrupt):
        store.close()
    assert roots.calls == 1
    assert root.calls == 1
    assert store.state == registry.RegistryStore.POISONED

    store.close()
    assert roots.calls == 1
    assert root.calls == 1


def test_registry_store_system_exit_cleans_both(tmp_path):
    store = _store_with_handles(
        tmp_path, roots_error=SystemExit('roots exit'))
    roots = store.roots_handle
    root = store.root_handle

    with pytest.raises(SystemExit):
        store.close()
    assert roots.calls == 1
    assert root.calls == 1
    assert store.state == registry.RegistryStore.POISONED

    store.close()
    assert roots.calls == 1
    assert root.calls == 1


def test_registry_store_partial_prepare_failure_poisoned(
        tmp_path, monkeypatch):
    store = registry.RegistryStore(tmp_path / 'registry-root')
    root = _FakeStoreHandle(_store_handle_error('root'))
    calls = []

    def open_side_effect(path):
        calls.append(path)
        if len(calls) == 1:
            return root
        raise RuntimeError('roots open failed')

    monkeypatch.setattr(registry, 'open_secure_directory', open_side_effect)

    with pytest.raises(RuntimeError, match='roots open failed') as caught:
        store.prepare()

    assert root.calls == 1
    assert store.state == registry.RegistryStore.POISONED
    assert store._prepared is False
    assert store.root_handle is None
    assert caught.value._native_close_errors


def test_registry_store_partial_prepare_failure_unprepared(
        tmp_path, monkeypatch):
    store = registry.RegistryStore(tmp_path / 'registry-root')
    root = _FakeStoreHandle()
    calls = []

    def open_side_effect(path):
        calls.append(path)
        if len(calls) == 1:
            return root
        raise RuntimeError('roots open failed')

    monkeypatch.setattr(registry, 'open_secure_directory', open_side_effect)

    with pytest.raises(RuntimeError, match='roots open failed'):
        store.prepare()

    assert root.closed is True
    assert store.state == registry.RegistryStore.UNPREPARED
    assert store._prepared is False
    assert store.root_handle is None
