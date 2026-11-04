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
    RegistryLockError,
    make_entry,
)

ROOTS_DIRNAME = 'roots'


class RootLockError(Exception):
    """Root Lock/Registry protocol failure."""


class RootLockReleaseError(RootLockError):
    """All cleanup stages were attempted, but at least one failed."""

    def __init__(self, message: str, errors: list[tuple[str, BaseException]]):
        self.errors = list(errors)
        details = '; '.join(
            '%s=%s: %s' % (name, type(exc).__name__, exc)
            for name, exc in self.errors
        )
        super().__init__('%s（%s）' % (message, details))


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

    def __init__(self, root: str | Path | None = None,
                 registry_root: str | Path | None = None,
                 target_handle: DirectoryHandle | None = None):
        self.registry_root = Path(os.path.abspath(str(
            registry_root or Path(
                os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
            ) / 'my_ai_assistant_restore_registry')))
        self._owns_target = target_handle is None
        if target_handle is None:
            if root is None:
                raise ValueError('root or target_handle is required')
            self.target: DirectoryHandle = open_secure_directory(root)
        else:
            self.target = target_handle
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
        except BaseException as primary:  # noqa: BLE001
            errors: list[tuple[str, BaseException]] = []
            try:
                self.registry.close()
            except BaseException as exc:  # noqa: BLE001
                errors.append(('registry', exc))
            try:
                self._close_target()
            except BaseException as exc:  # noqa: BLE001
                errors.append(('target', exc))
            if errors:
                raise RootLockReleaseError(
                    'RootLock 初始化失败清理未完成', errors) from primary
            raise

    def _close_target(self) -> None:
        if self._owns_target and self.target is not None:
            self.target.close()

    def _cleanup_root_lock(self, errors: list[tuple[str, BaseException]]) -> None:
        try:
            self.root_lock.release()
        except BaseException as exc:  # noqa: BLE001 - cleanup must continue
            errors.append(('root_lock', exc))
            try:
                self.root_lock.abort()
            except BaseException as abort_exc:  # noqa: BLE001
                errors.append(('root_lock_abort', abort_exc))

    def _cleanup_registry(self, errors: list[tuple[str, BaseException]]) -> None:
        try:
            self.registry.close()
        except BaseException as exc:  # noqa: BLE001 - cleanup must continue
            errors.append(('registry', exc))
            return
        if self.registry.poisoned:
            errors.append((
                'registry',
                RegistryLockError(
                    'RegistryStore 已 POISONED，无法确认资源已释放'),
            ))

    def _cleanup_target(self, errors: list[tuple[str, BaseException]]) -> None:
        try:
            self._close_target()
        except BaseException as exc:  # noqa: BLE001 - cleanup must continue
            errors.append(('target', exc))

    def _cleanup_after_failure(self, primary: BaseException) -> None:
        errors: list[tuple[str, BaseException]] = []
        self._cleanup_root_lock(errors)
        self._cleanup_registry(errors)
        self._cleanup_target(errors)
        if errors:
            raise RootLockReleaseError(
                'RootLock 失败清理未完成', errors) from primary
        raise primary

    @classmethod
    def from_target_handle(cls, target_handle: DirectoryHandle,
                           registry_root: str | Path | None = None) -> 'RootLock':
        return cls(registry_root=registry_root, target_handle=target_handle)

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
        except BaseException as primary:  # noqa: BLE001
            self._cleanup_after_failure(primary)
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
        except BaseException as primary:  # noqa: BLE001
            self._cleanup_after_failure(primary)

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
        errors: list[tuple[str, BaseException]] = []
        primary: BaseException | None = None
        if self._held:
            try:
                with self.registry:
                    data = self.registry.load()
                    data.entries = [
                        entry for entry in data.entries
                        if entry.run_id != self.run_id
                    ]
                    self.registry.save(data)
            except BaseException as exc:  # noqa: BLE001
                primary = exc
            finally:
                self._cleanup_root_lock(errors)
                self._cleanup_registry(errors)
                self._cleanup_target(errors)
                self._held = False
        else:
            self._cleanup_registry(errors)
            self._cleanup_target(errors)
        if errors:
            error = RootLockReleaseError('RootLock 释放未完成', errors)
            if primary is not None:
                raise error from primary
            raise error
        if primary is not None:
            raise primary

    def __enter__(self) -> 'RootLock':
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            try:
                self.release()
            except BaseException as release_exc:  # noqa: BLE001
                try:
                    existing = tuple(getattr(
                        exc, '_root_lock_release_errors', ()))
                    setattr(
                        exc, '_root_lock_release_errors',
                        existing + (release_exc,))
                except BaseException:  # noqa: BLE001
                    pass
            return False
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
        lock._close_target()
        raise
    return lock


__all__ = [
    'RootLockError',
    'RootLockReleaseError',
    'RootLock',
    'acquire_root_lock',
]
