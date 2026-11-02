# -*- coding: utf-8 -*-
"""First-version single-user Root Lock + Registry protocol.

Order:
  target directory handle/identity
    -> per-identity root lock (under user registry roots/)
    -> registry lock
    -> read/validate registry
    -> probe overlapping root locks
    -> remove only OS-lock-proven stale entries
    -> add own entry
    -> release registry lock
    -> hold root lock during restore

This module does not integrate with restore_db.main() and does not auto-recover.
"""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from restore_path_security import (
    DirectoryHandle,
    DirectoryIdentity,
    open_secure_directory,
)
from restore_registry import (
    RegistryConflictError,
    RegistryEntry,
    RegistryStore,
    RegistryLock,
    RegistryLockBusy,
    make_entry,
)

ROOTS_DIRNAME = 'roots'


class RootLockError(Exception):
    """Root Lock/Registry protocol failure."""


def _identity_key(identity: DirectoryIdentity) -> str:
    raw = '%s:%s:%s' % (
        identity.volume_serial, identity.file_id.lower(), identity.filesystem)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _root_lock_path(registry_root: Path, identity: DirectoryIdentity) -> Path:
    return registry_root / ROOTS_DIRNAME / (_identity_key(identity) + '.lock')


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class RootLock:
    """Holds a target directory handle and an identity-keyed root lock."""

    def __init__(self, root: str | Path, registry_root: str | Path | None = None):
        self.registry_root = Path(os.path.abspath(str(
            registry_root or Path(
                os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
            ) / 'my_ai_assistant_restore_registry')))
        self.target: DirectoryHandle = open_secure_directory(root)
        self.identity = self.target.identity
        self.registry = RegistryStore(self.registry_root)
        self.root_lock_path = _root_lock_path(self.registry_root, self.identity)
        self.run_id: str | None = None
        self._held = False
        try:
            self.registry.prepare()
            self.root_lock = RegistryLock(
                self.root_lock_path,
                parent_handle=self.registry.roots_handle.native_handle,
                parent_identity=self.registry.roots_identity,
            )
        except Exception:
            self.registry.close()
            self.target.close()
            raise

    def _probe_other_root(self, entry: RegistryEntry) -> bool:
        """Return True when the other root lock is free (stale entry)."""
        other = RegistryEntry(
            run_id=entry.run_id,
            target_identity=entry.target_identity,
            canonical_path=entry.canonical_path,
            pid=entry.pid,
            process_start_utc=entry.process_start_utc,
            user_sid=entry.user_sid,
            state=entry.state,
            created_at=entry.created_at,
        )
        other_identity = DirectoryIdentity(
            requested_path=other.canonical_path,
            final_path=other.canonical_path,
            volume_serial=int(other.target_identity['volume_serial']),
            file_id=str(other.target_identity['file_id']),
            filesystem=str(other.target_identity['filesystem']),
            drive_type=3,
        )
        path = _root_lock_path(self.registry_root, other_identity)
        probe = RegistryLock(
            path,
            parent_handle=self.registry.roots_handle.native_handle,
            parent_identity=self.registry.roots_identity,
        )
        try:
            probe.acquire()
        except RegistryLockBusy:
            return False
        finally:
            probe.release()
        return True

    def acquire(self, run_id: str, pid: int | None = None,
                user_sid: str | None = None) -> RegistryEntry:
        if self._held:
            raise RootLockError('当前 RootLock 已持有')
        if not isinstance(run_id, str) or not run_id.strip():
            raise RootLockError('run_id 无效')
        try:
            self.root_lock.acquire()
        except Exception:
            self.registry.close()
            self.target.close()
            raise
        try:
            with self.registry:
                data = self.registry.load()
                retained: list[RegistryEntry] = []
                for entry in data.entries:
                    if (
                        entry.target_identity.get('volume_serial')
                        == self.identity.volume_serial
                        and entry.target_identity.get('file_id', '').lower()
                        == self.identity.file_id.lower()
                    ):
                        # 当前进程已经持有该 identity 的 Root Lock；同名记录只能是
                        # 上一次崩溃留下的 Registry 残留。
                        continue
                    if not self.registry._entries_overlap(entry, self._entry_for(run_id)):
                        retained.append(entry)
                        continue
                    if not self._probe_other_root(entry):
                        raise RegistryConflictError(
                            '目标与正在恢复的目录重叠：%s' % entry.run_id)
                    # Stale registry only; never auto-recover its Restore state.
                entry = self._entry_for(
                    run_id, pid=pid, user_sid=user_sid)
                data.entries = retained + [entry]
                self.registry.save(data)
            self.run_id = run_id
            self._held = True
            return entry
        except Exception:
            self.root_lock.release()
            self.registry.close()
            self.target.close()
            raise

    def _entry_for(self, run_id: str, pid: int | None = None,
                   user_sid: str | None = None) -> RegistryEntry:
        now = _now_utc()
        return make_entry(
            self.identity,
            run_id=run_id,
            pid=os.getpid() if pid is None else pid,
            process_start_utc=now,
            user_sid=user_sid or os.environ.get('USERNAME', 'unknown'),
            created_at=now,
        )

    def release(self) -> None:
        if not self._held:
            self.registry.close()
            self.target.close()
            return
        try:
            with self.registry:
                data = self.registry.load()
                data.entries = [
                    entry for entry in data.entries
                    if entry.run_id != self.run_id
                ]
                self.registry.save(data)
        finally:
            self.root_lock.release()
            self.registry.close()
            self.target.close()
            self._held = False

    def __enter__(self) -> 'RootLock':
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


def acquire_root_lock(target_root: str | Path, run_id: str,
                      registry_root: str | Path | None = None,
                      pid: int | None = None,
                      user_sid: str | None = None) -> RootLock:
    lock = RootLock(target_root, registry_root=registry_root)
    try:
        lock.acquire(run_id, pid=pid, user_sid=user_sid)
    except Exception:
        lock.target.close()
        raise
    return lock


__all__ = [
    'RootLockError',
    'RootLock',
    'acquire_root_lock',
]
