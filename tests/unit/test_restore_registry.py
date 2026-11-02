# -*- coding: utf-8 -*-
"""P2-A 第八轮：单用户 Registry 与重叠检测测试。

所有 Registry、目标目录和进程脚本都在 pytest 临时目录内；
不创建正式 %LOCALAPPDATA% Registry，不访问正式业务数据。
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

from restore_path_security import validate_target_directory  # noqa: E402
import restore_registry as registry  # noqa: E402


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
