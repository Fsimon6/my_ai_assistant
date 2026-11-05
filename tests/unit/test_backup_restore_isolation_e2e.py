# -*- coding: utf-8 -*-
"""第一阶段 1.3：备份/恢复的隔离端到端验证。

覆盖：
  * SQLite（含 WAL）在线备份 → 恢复 → 记录完整性；
  * 合成 Chroma collection 的文件级备份 → 恢复 → 重新读取；
  * 合成 Excel meta.json / representation.json / original 的备份与恢复；
  * manifest 项、SHA256 和数据库损坏拒绝；
  * pre_restore 对恢复目标原数据的保护；
  * restore --target-root 只写目标目录。

安全边界：
  * 所有源数据、目标、备份、pre_restore 都位于 tmp_path；
  * 不读取正式 data/、数据库、Chroma、Excel 或 .env；
  * backup_db 的 REPO_ROOT 在测试内临时指向合成源根；
  * 不修改生产代码。
"""
from __future__ import annotations

import gc
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path

import pytest

from backend.excel import store as excel_store
from backend.excel.representation import (
    HEADER_SINGLE,
    ColumnMeta,
    SheetRepresentation,
    WorkbookRepresentation,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = REPO_ROOT / 'scripts' / 'backup_db.py'
RESTORE_SCRIPT = REPO_ROOT / 'scripts' / 'restore_db.py'

EXCEL_USER = 990002
EXCEL_DOC_ID = 'backup-synthetic-doc'
CHROMA_COLLECTION = 'ai_assistant_docs'


def _load_script(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup_db = _load_script(BACKUP_SCRIPT, '_backup_db_for_e2e')
restore_db = _load_script(RESTORE_SCRIPT, '_restore_db_for_e2e')

# restore_db inserts scripts/ into sys.path during import.
import restore_registry as registry_module  # noqa: E402
from restore_path_security import open_secure_directory  # noqa: E402
from restore_root_lock import RootLockReleaseError  # noqa: E402
import restore_win32_native as native  # noqa: E402


def _file_identity(path: Path) -> tuple[int, str]:
    with open_secure_directory(path.parent) as parent:
        status, handle = native.nt_create_file(
            parent.native_handle,
            path.name,
            native.FILE_OPEN,
            access=native.FILE_READ_ATTRIBUTES | native.SYNCHRONIZE,
            share=(native.FILE_SHARE_READ | native.FILE_SHARE_WRITE |
                   native.FILE_SHARE_DELETE),
            options=(native.FILE_OPEN_REPARSE_POINT |
                     native.FILE_NON_DIRECTORY_FILE |
                     native.FILE_SYNCHRONOUS_IO_NONALERT),
        )
        if status < 0:
            raise RuntimeError('FileId open failed: %s' % native.status_hex(status))
        try:
            return native.get_file_id(handle)
        finally:
            native.close_handle(handle)


def _create_source_db(db_path: Path) -> sqlite3.Connection:
    """创建带 WAL 的合成 SQLite；调用方在备份期间保持连接打开。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA wal_autocheckpoint=0')
    con.execute(
        'CREATE TABLE backup_probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
    con.executemany(
        'INSERT INTO backup_probe (id, value) VALUES (?, ?)',
        [(1, 'synthetic-alpha'), (2, 'synthetic-beta')],
    )
    con.commit()
    assert Path(str(db_path) + '-wal').exists(), '测试需要真实的 WAL 文件'
    return con


def _create_target_old_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute('CREATE TABLE old_probe (id INTEGER PRIMARY KEY, value TEXT)')
        con.execute('INSERT INTO old_probe (id, value) VALUES (1, ?)', ('old-target',))
        con.commit()
    finally:
        con.close()


def _make_excel_representation(root: Path) -> str:
    """通过生产 save_artifacts 写入合成 Excel artifacts。"""
    root.mkdir(parents=True, exist_ok=True)
    columns = [
        ColumnMeta(
            name='Order ID', index=0, excel_column=1, excel_column_letter='A',
            dtype='string', header_row_excel=1, non_empty=2, null_count=0,
            semantic_type='identifier',
        ),
        ColumnMeta(
            name='Order Amount', index=1, excel_column=2, excel_column_letter='B',
            dtype='number', header_row_excel=1, non_empty=2, null_count=0,
            semantic_type='number',
        ),
    ]
    sheet = SheetRepresentation(
        sheet_name='BackupProbe',
        sheet_index=0,
        header_mode=HEADER_SINGLE,
        header_rows_excel=[1],
        header_depth=1,
        columns=columns,
        rows=[['B-1', 11], ['B-2', 22]],
        row_excel_numbers=[2, 3],
        row_count=2,
        column_count=2,
        excel_range={'row_start': 1, 'row_end': 3,
                     'column_start': 1, 'column_end': 2},
    )
    rep = WorkbookRepresentation(
        schema_version='1.0',
        document_id=EXCEL_DOC_ID,
        user_id=EXCEL_USER,
        filename='backup_synthetic.xlsx',
        file_type='xlsx',
        parser='openpyxl',
        sheet_count=1,
        sheets=[sheet],
        created_at='2026-01-01T00:00:00',
    )
    source_file = root.parent / 'backup_synthetic.xlsx'
    source_file.write_bytes(b'synthetic workbook placeholder; never parsed')
    excel_store.save_artifacts(
        rep, str(source_file), rep.filename, file_size=len(source_file.read_bytes()))
    return rep.document_id


def _make_chroma(chroma_dir: Path) -> None:
    """用实际 chromadb PersistentClient 构造合成持久化 collection。"""
    import chromadb

    chroma_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(chroma_dir))
    collection = client.get_or_create_collection(CHROMA_COLLECTION)
    collection.add(
        ids=['backup-vec-1', 'backup-vec-2'],
        documents=['synthetic chroma alpha', 'synthetic chroma beta'],
        embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        metadatas=[
            {'user_id': EXCEL_USER, 'document_id': 'chroma-doc-1'},
            {'user_id': EXCEL_USER, 'document_id': 'chroma-doc-2'},
        ],
    )
    assert collection.count() == 2
    # 释放客户端，尽量让文件处于静止状态后再做文件级备份。
    client.close()
    del collection
    del client
    gc.collect()


def _run_backup(monkeypatch, source_root: Path, backup_dir: Path) -> Path:
    monkeypatch.setattr(backup_db, 'REPO_ROOT', source_root)
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setattr(sys, 'argv', [
        'backup_db.py',
        '--backup-dir', str(backup_dir),
        '--keep', '0',
        '--quiet',
    ])
    assert backup_db.main() == 0
    backups = sorted(backup_dir.glob('ai_assistant_backup_*'))
    assert len(backups) == 1, backups
    return backups[0]


def _run_restore(monkeypatch, backup_set: Path, target_root: Path,
                 pre_root: Path, confirm: bool = True) -> int:
    monkeypatch.setenv(
        'MY_AI_ASSISTANT_RESTORE_REGISTRY_ROOT',
        str(pre_root.parent / 'restore-registry'),
    )
    argv = [
        'restore_db.py',
        '--from', str(backup_set),
        '--target-root', str(target_root),
        '--backup-dir', str(pre_root),
    ]
    if confirm:
        argv.append('--yes')
    monkeypatch.setattr(sys, 'argv', argv)
    return restore_db.main()


def _make_source_tree(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    db_path = source / 'my_ai_assistant.db'
    db_con = _create_source_db(db_path)

    chroma_dir = source / 'data' / 'chroma_db'
    _make_chroma(chroma_dir)

    excel_root = source / 'data' / 'excel'
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', excel_root)
    _make_excel_representation(excel_root)

    return source, db_path, db_con


def _prepare_target(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    _create_target_old_db(target / 'my_ai_assistant.db')
    (target / 'data' / 'chroma_db').mkdir(parents=True)
    (target / 'data' / 'chroma_db' / 'old-chroma-marker.txt').write_text(
        'old chroma target data', encoding='utf-8')
    (target / 'data' / 'excel').mkdir(parents=True)
    (target / 'data' / 'excel' / 'old-excel-marker.txt').write_text(
        'old excel target data', encoding='utf-8')
    return target


@pytest.fixture()
def full_backup_and_target(tmp_path, monkeypatch):
    """生成完整三类备份和目标旧数据，供预检拒绝测试复用。"""
    source, _source_db, db_con = _make_source_tree(tmp_path, monkeypatch)
    try:
        backup_set = _run_backup(monkeypatch, source, tmp_path / 'backups')
    finally:
        db_con.close()
    return backup_set, _prepare_target(tmp_path)


def _manifest_of(backup_set: Path):
    return json.loads((backup_set / 'manifest.json').read_text(encoding='utf-8'))


def _write_manifest(backup_set: Path, manifest) -> None:
    (backup_set / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')


def _manifest_item(backup_set: Path, item_type: str):
    return next(i for i in _manifest_of(backup_set)['items'] if i['type'] == item_type)


def _refresh_manifest_item(backup_set: Path, item_type: str,
                           artifact: Path | None = None) -> None:
    manifest = _manifest_of(backup_set)
    item = next(i for i in manifest['items'] if i['type'] == item_type)
    artifact = artifact or (backup_set / item['name'])
    item['size'] = artifact.stat().st_size
    item['sha256'] = backup_db.sha256_file(artifact)
    _write_manifest(backup_set, manifest)


def _corrupt_zip_payload(path: Path) -> None:
    """破坏首个非目录成员的压缩数据，保留 ZIP 结构以便触发 CRC/解压错误。"""
    with zipfile.ZipFile(path) as zf:
        info = next(i for i in zf.infolist() if not i.is_dir())
        offset = (info.header_offset + 30
                  + len(info.filename.encode('utf-8')) + len(info.extra))
    data = bytearray(path.read_bytes())
    assert offset < len(data)
    data[offset] ^= 0xFF
    path.write_bytes(bytes(data))


def _snapshot_tree(root: Path) -> dict[str, str]:
    """记录目标目录的目录结构和普通文件 SHA256，用于断言未被覆盖。"""
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[rel + '/'] = 'directory'
        elif path.is_file():
            snapshot[rel] = backup_db.sha256_file(path)
    return snapshot


def _assert_rejected(monkeypatch, backup_set: Path, target: Path,
                     pre_root: Path, expected_code: int | None = None) -> None:
    before = _snapshot_tree(target)
    code = _run_restore(monkeypatch, backup_set, target, pre_root)
    assert code != 0
    if expected_code is not None:
        assert code == expected_code
    assert _snapshot_tree(target) == before
    assert not list(pre_root.glob('pre_restore_*'))


def test_backup_restore_sqlite_chroma_excel_roundtrip(tmp_path, monkeypatch):
    source, source_db, db_con = _make_source_tree(tmp_path, monkeypatch)
    target = _prepare_target(tmp_path)
    backup_dir = tmp_path / 'backups'
    pre_root = tmp_path / 'pre_restore'

    try:
        backup_set = _run_backup(monkeypatch, source, backup_dir)
    finally:
        db_con.close()

    # manifest：三类对象、SHA、无密钥文件。
    manifest = json.loads((backup_set / 'manifest.json').read_text(encoding='utf-8'))
    items = {item['type']: item for item in manifest['items']}
    assert set(items) == {'sqlite_database', 'vector_database', 'excel_artifacts'}
    for item in items.values():
        artifact = backup_set / item['name']
        assert artifact.is_file()
        assert artifact.stat().st_size == item['size']
        assert backup_db.sha256_file(artifact) == item['sha256']
    assert not (backup_set / '.env').exists()
    assert all(item['name'] != '.env' for item in manifest['items'])

    assert _run_restore(monkeypatch, backup_set, target, pre_root) == 0

    # SQLite：恢复后打开并读取合成记录。
    restored = sqlite3.connect(str(target / 'my_ai_assistant.db'))
    try:
        rows = restored.execute(
            'SELECT id, value FROM backup_probe ORDER BY id').fetchall()
        assert rows == [(1, 'synthetic-alpha'), (2, 'synthetic-beta')]
        assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert restored.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='old_probe'"
        ).fetchone() is None
    finally:
        restored.close()

    # pre_restore：恢复前目标数据库和目录快照都存在。
    pre_dirs = sorted(pre_root.glob('pre_restore_*'))
    assert len(pre_dirs) == 1
    pre_db = sqlite3.connect(str(pre_dirs[0] / 'my_ai_assistant.db'))
    try:
        assert pre_db.execute('SELECT value FROM old_probe WHERE id=1').fetchone() == (
            'old-target',)
    finally:
        pre_db.close()
    with zipfile.ZipFile(pre_dirs[0] / 'chroma_db.zip') as zf:
        assert 'chroma_db/old-chroma-marker.txt' in zf.namelist()
    with zipfile.ZipFile(pre_dirs[0] / 'excel.zip') as zf:
        assert 'excel/old-excel-marker.txt' in zf.namelist()

    # Chroma：重新打开恢复后的持久化目录，读取 collection 和向量。
    import chromadb
    client = chromadb.PersistentClient(path=str(target / 'data' / 'chroma_db'))
    collection = client.get_collection(CHROMA_COLLECTION)
    assert collection.count() == 2
    result = collection.query(
        query_embeddings=[[1.0, 0.0, 0.0]],
        n_results=1,
        include=['documents', 'metadatas'],
    )
    assert result['ids'] == [['backup-vec-1']]
    assert result['documents'] == [['synthetic chroma alpha']]
    del collection
    del client
    gc.collect()

    # Excel：恢复后使用生产 store 读取 meta / representation / catalog。
    monkeypatch.setattr(
        excel_store, 'EXCEL_DATA_ROOT', target / 'data' / 'excel')
    meta = excel_store.load_meta(EXCEL_DOC_ID)
    assert meta is not None
    assert meta['user_id'] == EXCEL_USER
    assert meta['filename'] == 'backup_synthetic.xlsx'
    assert (target / 'data' / 'excel' / EXCEL_DOC_ID / 'original.xlsx').is_file()

    rep = excel_store.load_representation(EXCEL_DOC_ID, user_id=EXCEL_USER)
    assert rep is not None
    assert rep.user_id == EXCEL_USER
    assert rep.sheets[0].rows == [['B-1', 11], ['B-2', 22]]

    catalog = excel_store.list_excel_catalog(EXCEL_USER)
    assert len(catalog) == 1
    assert catalog[0]['document_id'] == EXCEL_DOC_ID
    assert catalog[0]['filename'] == 'backup_synthetic.xlsx'
    assert catalog[0]['local_available'] is True

    # 恢复只写 target；source 的合成数据仍然存在。
    assert source_db.exists()
    assert (source / 'data' / 'chroma_db').is_dir()
    assert (source / 'data' / 'excel' / EXCEL_DOC_ID / 'meta.json').is_file()


def test_manifest_rejects_corrupt_database_snapshot(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    db_file = backup_set / 'my_ai_assistant.db'
    original = db_file.read_bytes()
    db_file.write_bytes(original + b'corruption')

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 3)


def test_preflight_rejects_chroma_sha_mismatch(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    item = _manifest_item(backup_set, 'vector_database')
    path = backup_set / item['name']
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 3)


def test_preflight_rejects_excel_sha_mismatch(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    item = _manifest_item(backup_set, 'excel_artifacts')
    path = backup_set / item['name']
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 3)


def test_preflight_rejects_missing_chroma_item(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    manifest = _manifest_of(backup_set)
    manifest['items'] = [
        i for i in manifest['items'] if i['type'] != 'vector_database']
    _write_manifest(backup_set, manifest)

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 2)


def test_preflight_rejects_missing_excel_item(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    manifest = _manifest_of(backup_set)
    manifest['items'] = [
        i for i in manifest['items'] if i['type'] != 'excel_artifacts']
    _write_manifest(backup_set, manifest)

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 2)


def test_preflight_rejects_missing_sqlite_item(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    manifest = _manifest_of(backup_set)
    manifest['items'] = [
        i for i in manifest['items'] if i['type'] != 'sqlite_database']
    _write_manifest(backup_set, manifest)

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 2)


def test_preflight_rejects_missing_chroma_zip(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    item = _manifest_item(backup_set, 'vector_database')
    (backup_set / item['name']).unlink()

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 4)


def test_preflight_rejects_missing_excel_zip(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    item = _manifest_item(backup_set, 'excel_artifacts')
    (backup_set / item['name']).unlink()

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 4)


def test_preflight_rejects_chroma_zip_crc(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    item = _manifest_item(backup_set, 'vector_database')
    path = backup_set / item['name']
    _corrupt_zip_payload(path)
    _refresh_manifest_item(backup_set, 'vector_database', path)

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 4)


def test_preflight_rejects_excel_zip_crc(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    item = _manifest_item(backup_set, 'excel_artifacts')
    path = backup_set / item['name']
    _corrupt_zip_payload(path)
    _refresh_manifest_item(backup_set, 'excel_artifacts', path)

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 4)


def test_preflight_rejects_zip_parent_traversal(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    item = _manifest_item(backup_set, 'vector_database')
    path = backup_set / item['name']
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('../escaped-marker.txt', 'must not escape')
    _refresh_manifest_item(backup_set, 'vector_database', path)

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 4)
    assert not (tmp_path / 'escaped-marker.txt').exists()


def test_preflight_rejects_zip_absolute_or_drive_path(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    item = _manifest_item(backup_set, 'excel_artifacts')
    path = backup_set / item['name']
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('C:/escaped-marker.txt', 'must not escape')
    _refresh_manifest_item(backup_set, 'excel_artifacts', path)

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 4)


def test_preflight_rejects_duplicate_type(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    manifest = _manifest_of(backup_set)
    manifest['items'].append(dict(manifest['items'][0]))
    _write_manifest(backup_set, manifest)

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 2)


@pytest.mark.parametrize('field', ['size', 'sha256'])
def test_preflight_rejects_missing_required_field(
        full_backup_and_target, monkeypatch, tmp_path, field):
    backup_set, target = full_backup_and_target
    manifest = _manifest_of(backup_set)
    item = next(i for i in manifest['items'] if i['type'] == 'vector_database')
    item.pop(field)
    _write_manifest(backup_set, manifest)

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 2)


def test_preflight_rejects_size_mismatch(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    manifest = _manifest_of(backup_set)
    item = next(i for i in manifest['items'] if i['type'] == 'excel_artifacts')
    item['size'] += 1
    _write_manifest(backup_set, manifest)

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 3)


def test_preflight_rejects_manifest_filename_escape(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    manifest = _manifest_of(backup_set)
    item = next(i for i in manifest['items'] if i['type'] == 'vector_database')
    item['name'] = '../' + item['name']
    _write_manifest(backup_set, manifest)

    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore', 4)


def test_preview_without_yes_does_not_write(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    before = _snapshot_tree(target)

    assert _run_restore(
        monkeypatch, backup_set, target, pre_root, confirm=False) == 0
    assert _snapshot_tree(target) == before
    assert not list(pre_root.glob('pre_restore_*'))


def test_restore_zip_extraction_does_not_escape_target(tmp_path):
    target_parent = tmp_path / 'target-data'
    target_parent.mkdir()
    evil = tmp_path / 'evil.zip'
    with zipfile.ZipFile(evil, 'w') as zf:
        zf.writestr('../escaped-marker.txt', 'must not escape')
        zf.writestr('safe-marker.txt', 'inside')

    restore_db.unzip_into(evil, target_parent)

    assert not (tmp_path / 'escaped-marker.txt').exists()
    assert (target_parent / 'safe-marker.txt').read_text(encoding='utf-8') == 'inside'


# ---- B1-a transaction tests ----

class _SimulatedCrash(BaseException):
    """用 BaseException 模拟进程被强制结束（main 只捕获 Exception）。"""


def _business_snapshot(root: Path) -> dict[str, str]:
    """只比较业务数据；忽略 .restore_* 控制目录。"""
    result: dict[str, str] = {}
    control = {
        '.restore_control', '.restore_state',
        '.restore_staging', '.restore_rollback',
    }
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in control:
            continue
        if path.is_file():
            result[rel.as_posix()] = backup_db.sha256_file(path)
    return result


def _state_data(root: Path):
    states = sorted((root / '.restore_state').glob('*/state.json'))
    assert len(states) == 1, states
    return json.loads(states[0].read_text(encoding='utf-8'))


def test_b1_staging_validation_failure_keeps_target(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    before = _business_snapshot(target)

    def fail(_staged, _state):
        raise RuntimeError('simulated staging validation failure')

    monkeypatch.setattr(restore_db, '_verify_staged', fail)
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_TRANSACTION)
    assert _business_snapshot(target) == before
    state = _state_data(target)
    assert state['state'] == 'ROLLED_BACK'
    assert not Path(state['staging']).exists()
    assert not list(pre_root.glob('pre_restore_*'))


def test_b1_sqlite_permission_failure_rolls_back(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    before = _business_snapshot(target)
    target_db = target / 'my_ai_assistant.db'
    original_replace = restore_db._replace_file

    def locked(src, dst):
        if Path(dst) == target_db and Path(src).parent.name == 'database':
            raise PermissionError('simulated Windows file lock')
        return original_replace(src, dst)

    monkeypatch.setattr(restore_db, '_replace_file', locked)
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_TRANSACTION)
    assert _business_snapshot(target) == before
    state = _state_data(target)
    assert state['state'] == 'ROLLED_BACK'
    pre_db = Path(state['pre_restore']) / 'my_ai_assistant.db'
    assert pre_db.is_file()
    con = sqlite3.connect(str(pre_db))
    try:
        assert con.execute('SELECT value FROM old_probe WHERE id=1').fetchone() == (
            'old-target',)
    finally:
        con.close()


def test_b1_chroma_switch_failure_rolls_back_all(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    before = _business_snapshot(target)
    original_switch = restore_db._switch_directory

    def fail_after_chroma(state, staging, target_dir, rollback,
                          staging_name, object_name, rollback_name):
        original_switch(state, staging, target_dir, rollback,
                        staging_name, object_name, rollback_name)
        if staging_name == 'chroma_db':
            raise RuntimeError('simulated chroma switch failure')

    monkeypatch.setattr(restore_db, '_switch_directory', fail_after_chroma)
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_TRANSACTION)
    assert _business_snapshot(target) == before
    state = _state_data(target)
    assert state['state'] == 'ROLLED_BACK'
    assert (target / 'data' / 'chroma_db' / 'old-chroma-marker.txt').is_file()


def test_b1_excel_switch_failure_rolls_back_all(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    before = _business_snapshot(target)
    original_switch = restore_db._switch_directory

    def fail_after_excel(state, staging, target_dir, rollback,
                         staging_name, object_name, rollback_name):
        original_switch(state, staging, target_dir, rollback,
                        staging_name, object_name, rollback_name)
        if staging_name == 'excel':
            raise RuntimeError('simulated excel switch failure')

    monkeypatch.setattr(restore_db, '_switch_directory', fail_after_excel)
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_TRANSACTION)
    assert _business_snapshot(target) == before
    state = _state_data(target)
    assert state['state'] == 'ROLLED_BACK'
    assert (target / 'data' / 'excel' / 'old-excel-marker.txt').is_file()


def test_b1_crash_after_sqlite_switch_is_detected(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    original_switch = restore_db._switch_sqlite

    def crash_after_sqlite(*args, **kwargs):
        original_switch(*args, **kwargs)
        raise _SimulatedCrash('simulated process death after sqlite switch')

    monkeypatch.setattr(restore_db, '_switch_sqlite', crash_after_sqlite)
    with pytest.raises(_SimulatedCrash):
        _run_restore(monkeypatch, backup_set, target, tmp_path / 'pre_restore')
    state = _state_data(target)
    assert state['state'] == 'SQLITE_SWITCHED'
    assert (target / 'data' / 'chroma_db' / 'old-chroma-marker.txt').is_file()
    before = _business_snapshot(target)
    assert _run_restore(monkeypatch, backup_set, target, tmp_path / 'pre_restore') == (
        restore_db.EXIT_INCOMPLETE_RUN)
    assert _business_snapshot(target) == before


def test_b1_crash_during_chroma_switch_is_detected(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    original_switch = restore_db._switch_directory

    def crash_after_chroma(state, staging, target_dir, rollback,
                           staging_name, object_name, rollback_name):
        original_switch(state, staging, target_dir, rollback,
                        staging_name, object_name, rollback_name)
        if staging_name == 'chroma_db':
            raise _SimulatedCrash('simulated chroma crash')

    monkeypatch.setattr(restore_db, '_switch_directory', crash_after_chroma)
    with pytest.raises(_SimulatedCrash):
        _run_restore(monkeypatch, backup_set, target, tmp_path / 'pre_restore')
    state = _state_data(target)
    assert state['state'] == 'SWITCHING_CHROMA'
    before = _business_snapshot(target)
    assert _run_restore(monkeypatch, backup_set, target, tmp_path / 'pre_restore') == (
        restore_db.EXIT_INCOMPLETE_RUN)
    assert _business_snapshot(target) == before


def test_b1_crash_during_excel_switch_is_detected(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    original_switch = restore_db._switch_directory

    def crash_after_excel(state, staging, target_dir, rollback,
                          staging_name, object_name, rollback_name):
        original_switch(state, staging, target_dir, rollback,
                        staging_name, object_name, rollback_name)
        if staging_name == 'excel':
            raise _SimulatedCrash('simulated excel crash')

    monkeypatch.setattr(restore_db, '_switch_directory', crash_after_excel)
    with pytest.raises(_SimulatedCrash):
        _run_restore(monkeypatch, backup_set, target, tmp_path / 'pre_restore')
    state = _state_data(target)
    assert state['state'] == 'SWITCHING_EXCEL'
    before = _business_snapshot(target)
    assert _run_restore(monkeypatch, backup_set, target, tmp_path / 'pre_restore') == (
        restore_db.EXIT_INCOMPLETE_RUN)
    assert _business_snapshot(target) == before


def test_b1_rollback_failure_is_fail_closed(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    before = _business_snapshot(target)

    def fail_switch(*_args, **_kwargs):
        raise RuntimeError('simulated switch failure')

    def fail_rollback(*_args, **_kwargs):
        raise RuntimeError('simulated rollback failure')

    monkeypatch.setattr(restore_db, '_switch_sqlite', fail_switch)
    monkeypatch.setattr(restore_db, '_rollback', fail_rollback)
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_TRANSACTION)
    assert _business_snapshot(target) == before
    state = _state_data(target)
    assert state['state'] == 'FAILED_RECOVERY_REQUIRED'
    assert Path(state['staging']).exists()
    assert (Path(state['pre_restore']) / 'my_ai_assistant.db').is_file()


def test_b1_rollback_can_be_called_twice(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    original_switch = restore_db._switch_directory

    def fail_after_chroma(state, staging, target_dir, rollback,
                          staging_name, object_name, rollback_name):
        original_switch(state, staging, target_dir, rollback,
                        staging_name, object_name, rollback_name)
        if staging_name == 'chroma_db':
            raise RuntimeError('simulated chroma switch failure')

    monkeypatch.setattr(restore_db, '_switch_directory', fail_after_chroma)
    assert _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore') == restore_db.EXIT_TRANSACTION
    before = _business_snapshot(target)
    state = _state_data(target)
    targets = {
        'root': target,
        'database': target / 'my_ai_assistant.db',
        'chroma': target / 'data' / 'chroma_db',
        'excel': target / 'data' / 'excel',
    }
    restore_db._rollback(
        state, target, targets, Path(state['rollback']),
        Path(state['pre_restore']))
    assert _business_snapshot(target) == before
    assert _state_data(target)['state'] == 'ROLLED_BACK'


def test_b1_disk_space_failure_happens_before_target_write(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    before = _business_snapshot(target)

    class _LowDisk:
        free = 0

    monkeypatch.setattr(restore_db.shutil, 'disk_usage', lambda _root: _LowDisk())
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_TRANSACTION)
    assert _business_snapshot(target) == before
    assert not list(pre_root.glob('pre_restore_*'))
    state = _state_data(target)
    assert state['state'] == 'ROLLED_BACK'
    assert not Path(state['staging']).exists()


def test_b1_state_write_failure_before_target_write(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    before = _business_snapshot(target)

    def fail_write(_state):
        raise OSError('simulated state write failure')

    monkeypatch.setattr(restore_db, '_write_state', fail_write)
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_TRANSACTION)
    assert _business_snapshot(target) == before
    assert not list(pre_root.glob('pre_restore_*'))
    assert not any((target / '.restore_staging').glob('*'))


def test_b1_state_write_failure_after_switch_rolls_back(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    before = _business_snapshot(target)
    original_write = restore_db._write_state
    failed_once = {'done': False}

    def flaky_write(state):
        if state.get('state') == 'SQLITE_SWITCHED' and not failed_once['done']:
            failed_once['done'] = True
            raise OSError('simulated post-switch state failure')
        return original_write(state)

    monkeypatch.setattr(restore_db, '_write_state', flaky_write)
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_TRANSACTION)
    assert _business_snapshot(target) == before
    state = _state_data(target)
    assert state['state'] == 'ROLLED_BACK'


def test_b1_persistent_state_write_failure_still_restores_old_data(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    before = _business_snapshot(target)
    original_write = restore_db._write_state

    def fail_after_switch(state):
        if state.get('state') in (
                'SQLITE_SWITCHED', 'ROLLING_BACK',
                'ROLLED_BACK', 'FAILED_RECOVERY_REQUIRED'):
            raise OSError('persistent state write failure')
        return original_write(state)

    monkeypatch.setattr(restore_db, '_write_state', fail_after_switch)
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_TRANSACTION)
    assert _business_snapshot(target) == before
    assert (Path(_state_data(target)['pre_restore']) /
            'my_ai_assistant.db').is_file()


def test_b1_custom_source_path_is_rejected(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    manifest = _manifest_of(backup_set)
    manifest['path_sources']['database'] = 'explicit'
    _write_manifest(backup_set, manifest)
    _assert_rejected(
        monkeypatch, backup_set, target, tmp_path / 'pre_restore',
        restore_db.EXIT_PATH_MAPPING)


def test_b1_cross_volume_rename_fails_closed(tmp_path, monkeypatch):
    src = tmp_path / 'source-dir'
    dst = tmp_path / 'target-dir'
    src.mkdir()
    (src / 'marker.txt').write_text('keep', encoding='utf-8')
    monkeypatch.setattr(restore_db, '_same_volume', lambda _a, _b: False)
    with pytest.raises(restore_db.RestoreTransactionError):
        restore_db._rename_dir(src, dst)
    assert src.is_dir()
    assert not dst.exists()


def test_b1_pre_restore_copies_wal_and_shm(tmp_path):
    target = tmp_path / 'wal-target'
    target.mkdir()
    db = target / 'my_ai_assistant.db'
    con = sqlite3.connect(str(db))
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA wal_autocheckpoint=0')
        con.execute('CREATE TABLE old_probe (id INTEGER PRIMARY KEY, value TEXT)')
        con.execute('INSERT INTO old_probe (id, value) VALUES (1, ?)', ('wal-old',))
        con.commit()
        wal = Path(str(db) + '-wal')
        shm = Path(str(db) + '-shm')
        assert wal.is_file()
        assert shm.is_file()
        pre = tmp_path / 'wal-pre'
        restore_db._snapshot_pre_restore(pre, target)
        assert (pre / 'my_ai_assistant.db').is_file()
        assert backup_db.sha256_file(pre / 'my_ai_assistant.db-wal') == (
            backup_db.sha256_file(wal))
        assert backup_db.sha256_file(pre / 'my_ai_assistant.db-shm') == (
            backup_db.sha256_file(shm))
    finally:
        con.close()


def test_b1_unknown_staging_blocks_new_restore(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    unknown = target / '.restore_staging' / 'unknown-run'
    unknown.mkdir(parents=True)
    (unknown / 'leftover.txt').write_text('unfinished', encoding='utf-8')
    before = _business_snapshot(target)
    assert _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore') == (
        restore_db.EXIT_INCOMPLETE_RUN)
    assert _business_snapshot(target) == before
    assert not list((tmp_path / 'pre_restore').glob('pre_restore_*'))


def test_b1_success_keeps_pre_and_rollback_materials(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == 0
    state = _state_data(target)
    assert state['state'] == 'COMMITTED'
    assert not Path(state['staging']).exists()
    rollback = Path(state['rollback'])
    assert (rollback / 'chroma_db' / 'old-chroma-marker.txt').is_file()
    assert (rollback / 'excel' / 'old-excel-marker.txt').is_file()
    pre_dirs = sorted(pre_root.glob('pre_restore_*'))
    assert len(pre_dirs) == 1
    assert (pre_dirs[0] / 'my_ai_assistant.db').is_file()
    assert (pre_dirs[0] / 'chroma_db.zip').is_file()
    assert (pre_dirs[0] / 'excel.zip').is_file()


# ---- B1-a hardening: Chroma / Excel / state schema / cleanup ----

HARDENING_RUN_ID = '20261031T120000_12345_abcdef12'


def _make_state_record(target: Path, pre_root: Path,
                       run_id: str = HARDENING_RUN_ID,
                       phase: str = 'COMMITTED') -> dict:
    return {
        'run_id': run_id,
        'state': phase,
        'target_root': str(target),
        'staging': str(target / '.restore_staging' / run_id),
        'rollback': str(target / '.restore_rollback' / run_id),
        'pre_restore': str(pre_root / ('pre_restore_' + run_id)),
        'targets': {
            'root': str(target),
            'database': str(target / 'my_ai_assistant.db'),
            'chroma': str(target / 'data' / 'chroma_db'),
            'excel': str(target / 'data' / 'excel'),
        },
        'state_path': str(target / '.restore_state' / run_id / 'state.json'),
    }


def _write_state_record(target: Path, pre_root: Path,
                        run_id: str = HARDENING_RUN_ID,
                        phase: str = 'COMMITTED',
                        overrides: dict | None = None,
                        remove: str | None = None) -> dict:
    data = _make_state_record(target, pre_root, run_id, phase)
    if overrides:
        data.update(overrides)
    if remove:
        data.pop(remove, None)
    state_dir = target / '.restore_state' / run_id
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'state.json').write_text(
        json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return data


def _make_marker_backup(tmp_path, monkeypatch, marker: str):
    source, _db_path, db_con = _make_source_tree(tmp_path, monkeypatch)
    db_con.close()
    target_dir = source / 'data' / marker
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True)
    (target_dir / 'marker.txt').write_text('marker only', encoding='utf-8')
    backup_set = _run_backup(monkeypatch, source, tmp_path / 'backups')
    return backup_set, _prepare_target(tmp_path)


def test_b1_marker_only_chroma_is_rejected(tmp_path, monkeypatch):
    backup_set, target = _make_marker_backup(tmp_path, monkeypatch, 'chroma_db')
    before = _business_snapshot(target)
    pre_root = tmp_path / 'pre_restore'
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_TRANSACTION)
    assert _business_snapshot(target) == before
    state = _state_data(target)
    assert state['state'] == 'ROLLED_BACK'
    assert (target / 'data' / 'chroma_db' / 'old-chroma-marker.txt').is_file()


def test_b1_marker_only_excel_is_rejected(tmp_path, monkeypatch):
    backup_set, target = _make_marker_backup(tmp_path, monkeypatch, 'excel')
    before = _business_snapshot(target)
    pre_root = tmp_path / 'pre_restore'
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_TRANSACTION)
    assert _business_snapshot(target) == before
    state = _state_data(target)
    assert state['state'] == 'ROLLED_BACK'
    assert (target / 'data' / 'excel' / 'old-excel-marker.txt').is_file()


def test_b1_missing_expected_chroma_collection_is_rejected(
        tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    db_con = _create_source_db(source / 'my_ai_assistant.db')
    db_con.close()
    import chromadb
    client = chromadb.PersistentClient(path=str(source / 'data' / 'chroma_db'))
    collection = client.get_or_create_collection('other_collection')
    collection.add(
        ids=['other-1'],
        documents=['other'],
        embeddings=[[1.0, 0.0, 0.0]],
    )
    client.close()
    del collection
    del client
    gc.collect()
    monkeypatch.setattr(
        excel_store, 'EXCEL_DATA_ROOT', source / 'data' / 'excel')
    _make_excel_representation(source / 'data' / 'excel')
    backup_set = _run_backup(monkeypatch, source, tmp_path / 'backups')
    target = _prepare_target(tmp_path)
    before = _business_snapshot(target)
    assert _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore') == restore_db.EXIT_TRANSACTION
    assert _business_snapshot(target) == before
    assert _state_data(target)['state'] == 'ROLLED_BACK'


def test_b1_corrupt_chroma_database_is_rejected(tmp_path, monkeypatch):
    source, _db_path, db_con = _make_source_tree(tmp_path, monkeypatch)
    db_con.close()
    chroma_db = source / 'data' / 'chroma_db' / 'chroma.sqlite3'
    assert chroma_db.is_file()
    chroma_db.write_bytes(b'not a valid chroma sqlite file')
    backup_set = _run_backup(monkeypatch, source, tmp_path / 'backups')
    target = _prepare_target(tmp_path)
    before = _business_snapshot(target)
    assert _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore') == restore_db.EXIT_TRANSACTION
    assert _business_snapshot(target) == before
    assert _state_data(target)['state'] == 'ROLLED_BACK'


def test_b1_invalid_excel_representation_is_rejected(tmp_path, monkeypatch):
    source, _db_path, db_con = _make_source_tree(tmp_path, monkeypatch)
    db_con.close()
    doc_dir = source / 'data' / 'excel' / EXCEL_DOC_ID
    (doc_dir / 'representation.json').write_text('{}', encoding='utf-8')
    backup_set = _run_backup(monkeypatch, source, tmp_path / 'backups')
    target = _prepare_target(tmp_path)
    before = _business_snapshot(target)
    assert _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore') == restore_db.EXIT_TRANSACTION
    assert _business_snapshot(target) == before
    assert _state_data(target)['state'] == 'ROLLED_BACK'


def test_b1_valid_committed_and_rolled_back_states_are_recognized(
        tmp_path, monkeypatch):
    target = _prepare_target(tmp_path)
    pre_root = tmp_path / 'pre_restore'
    _write_state_record(target, pre_root, phase='COMMITTED')
    assert restore_db._scan_incomplete_runs(
        target, pre_root) == []
    _write_state_record(target, pre_root, phase='ROLLED_BACK')
    assert restore_db._scan_incomplete_runs(
        target, pre_root) == []


@pytest.mark.parametrize('remove', [
    'target_root', 'staging', 'rollback', 'pre_restore', 'targets',
])
def test_b1_incomplete_state_fields_block_restore(
        full_backup_and_target, monkeypatch, tmp_path, remove):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    _write_state_record(target, pre_root, remove=remove)
    before = _business_snapshot(target)
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_INCOMPLETE_RUN)
    assert _business_snapshot(target) == before
    assert not list(pre_root.glob('pre_restore_*'))


@pytest.mark.parametrize('overrides', [
    {'state': 'UNKNOWN'},
    {'run_id': 'other'},
    {'target_root': 'D:/wrong-target'},
    {'targets': []},
    {'staging': 'D:/outside-staging'},
    {'rollback': 'D:/outside-rollback'},
    {'pre_restore': 'D:/outside-pre-restore'},
])
def test_b1_invalid_state_values_block_restore(
        full_backup_and_target, monkeypatch, tmp_path, overrides):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    _write_state_record(target, pre_root, overrides=overrides)
    before = _business_snapshot(target)
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_INCOMPLETE_RUN)
    assert _business_snapshot(target) == before
    assert not list(pre_root.glob('pre_restore_*'))


def test_b1_bare_committed_state_blocks_restore(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    state_dir = target / '.restore_state' / HARDENING_RUN_ID
    state_dir.mkdir(parents=True)
    (state_dir / 'state.json').write_text(
        json.dumps({'state': 'COMMITTED'}), encoding='utf-8')
    before = _business_snapshot(target)
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_INCOMPLETE_RUN)
    assert _business_snapshot(target) == before
    assert not list(pre_root.glob('pre_restore_*'))


def test_b1_missing_or_corrupt_state_blocks_restore(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    state_dir = target / '.restore_state' / HARDENING_RUN_ID
    state_dir.mkdir(parents=True)
    before = _business_snapshot(target)
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_INCOMPLETE_RUN)
    (state_dir / 'state.json').write_text('{bad', encoding='utf-8')
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
        restore_db.EXIT_INCOMPLETE_RUN)
    assert _business_snapshot(target) == before


def test_b1_state_symlink_is_rejected(tmp_path):
    target = _prepare_target(tmp_path)
    pre_root = tmp_path / 'pre_restore'
    external = tmp_path / 'external-state.json'
    external.write_text(json.dumps(
        _make_state_record(target, pre_root)), encoding='utf-8')
    state_dir = target / '.restore_state' / HARDENING_RUN_ID
    state_dir.mkdir(parents=True)
    link = state_dir / 'state.json'
    try:
        link.symlink_to(external)
    except OSError as exc:
        pytest.skip('Windows symlink privilege unavailable: %s' % exc)
    problems = restore_db._scan_incomplete_runs(target, pre_root)
    assert any('符号链接' in problem for problem in problems)


def test_b1_cleanup_only_current_run(tmp_path):
    target = _prepare_target(tmp_path)
    pre_root = tmp_path / 'pre_restore'
    state = _write_state_record(target, pre_root)
    staging = target / '.restore_staging' / HARDENING_RUN_ID
    staging.mkdir(parents=True)
    (staging / 'file.txt').write_text('current', encoding='utf-8')
    assert restore_db._cleanup_staging(staging, target, state)
    assert not staging.exists()


def test_b1_cleanup_rejects_other_or_unknown_staging(tmp_path):
    target = _prepare_target(tmp_path)
    pre_root = tmp_path / 'pre_restore'
    state = _write_state_record(target, pre_root)
    bad = target / '.restore_staging' / '202bad'
    bad.mkdir(parents=True)
    other = target / '.restore_staging' / 'other-run'
    other.mkdir(parents=True)
    assert not restore_db._cleanup_staging(bad, target, state)
    assert not restore_db._cleanup_staging(other, target, state)
    assert bad.exists()
    assert other.exists()


def test_b1_cleanup_rejects_missing_state_and_outside_path(tmp_path):
    target = _prepare_target(tmp_path)
    pre_root = tmp_path / 'pre_restore'
    state = _write_state_record(target, pre_root)
    staging = target / '.restore_staging' / HARDENING_RUN_ID
    staging.mkdir(parents=True)
    outside = tmp_path / 'outside-staging'
    outside.mkdir()
    assert not restore_db._cleanup_staging(staging, target, {})
    assert not restore_db._cleanup_staging(outside, target, state)
    assert staging.exists()
    assert outside.exists()


def test_b1_final_file_manifest_mismatch_is_rejected(tmp_path):
    target = _prepare_target(tmp_path)
    staged = {
        'database_sha256': backup_db.sha256_file(
            target / 'my_ai_assistant.db'),
        'chroma': {},
        'excel': {},
    }
    with pytest.raises(restore_db.RestoreTransactionError):
        restore_db._verify_restored_files(target, staged)


def test_b1_cleanup_symlink_staging_is_rejected(tmp_path):
    target = _prepare_target(tmp_path)
    pre_root = tmp_path / 'pre_restore'
    state = _write_state_record(target, pre_root)
    outside = tmp_path / 'outside-staging-symlink'
    outside.mkdir()
    link = target / '.restore_staging' / HARDENING_RUN_ID
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip('Windows symlink privilege unavailable: %s' % exc)
    assert not restore_db._cleanup_staging(link, target, state)
    assert outside.exists()


def test_b1_cleanup_exception_does_not_escape(tmp_path, monkeypatch):
    target = _prepare_target(tmp_path)
    pre_root = tmp_path / 'pre_restore'
    state = _write_state_record(target, pre_root)
    staging = target / '.restore_staging' / HARDENING_RUN_ID
    staging.mkdir(parents=True)

    def fail_rmtree(_path):
        raise OSError('simulated cleanup failure')

    monkeypatch.setattr(restore_db.shutil, 'rmtree', fail_rmtree)
    assert not restore_db._cleanup_staging(staging, target, state)
    assert staging.exists()


# ---- B1-a hardening round 2: Chroma metadata and Windows reparse points ----

def _make_chroma_source(tmp_path, monkeypatch, metadatas):
    source = tmp_path / 'source'
    source.mkdir()
    db_con = _create_source_db(source / 'my_ai_assistant.db')
    db_con.close()

    import chromadb
    chroma = source / 'data' / 'chroma_db'
    chroma.mkdir(parents=True)
    client = chromadb.PersistentClient(path=str(chroma))
    collection = client.get_or_create_collection(CHROMA_COLLECTION)
    count = len(metadatas) if metadatas is not None else 1
    kwargs = {}
    if metadatas is not None:
        kwargs['metadatas'] = metadatas
    collection.add(
        ids=['meta-%d' % i for i in range(count)],
        documents=['synthetic %d' % i for i in range(count)],
        embeddings=[[1.0, 0.0, 0.0] for _ in range(count)],
        **kwargs,
    )
    client.close()
    del collection
    del client

    monkeypatch.setattr(
        excel_store, 'EXCEL_DATA_ROOT', source / 'data' / 'excel')
    _make_excel_representation(source / 'data' / 'excel')
    return source


@pytest.mark.parametrize('metadatas', [
    None,
    [None],
    [{'document_id': 'doc-only'}],
    [{'user_id': EXCEL_USER}],
    [{'user_id': 'wrong-type', 'document_id': 'doc'}],
])
def test_b1_chroma_metadata_required_for_full_restore(
        tmp_path, monkeypatch, metadatas):
    source = _make_chroma_source(tmp_path, monkeypatch, metadatas)
    backup_set = _run_backup(monkeypatch, source, tmp_path / 'backups')
    target = _prepare_target(tmp_path)
    before = _business_snapshot(target)
    assert _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore') == restore_db.EXIT_TRANSACTION
    assert _business_snapshot(target) == before
    assert _state_data(target)['state'] == 'ROLLED_BACK'


def test_b1_chroma_later_record_missing_metadata_rejected(
        tmp_path, monkeypatch):
    metadatas = [
        {'user_id': EXCEL_USER, 'document_id': 'good-doc'},
        None,
    ]
    source = _make_chroma_source(tmp_path, monkeypatch, metadatas)
    backup_set = _run_backup(monkeypatch, source, tmp_path / 'backups')
    target = _prepare_target(tmp_path)
    before = _business_snapshot(target)
    assert _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore') == restore_db.EXIT_TRANSACTION
    assert _business_snapshot(target) == before
    assert _state_data(target)['state'] == 'ROLLED_BACK'


def _make_junction(link: Path, outside: Path) -> None:
    if sys.platform != 'win32':
        pytest.skip('Windows junction test')
    link.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
        capture_output=True, text=True)
    if result.returncode != 0:
        pytest.skip('mklink /J unavailable: %s' % (
            result.stdout + result.stderr).strip())


def _remove_junction(link: Path) -> None:
    if link.exists() or link.is_symlink():
        subprocess.run(
            ['cmd', '/c', 'rmdir', str(link)],
            capture_output=True, text=True)


def test_b1_junction_target_chroma_rejected(tmp_path):
    target = tmp_path / 'junction-target'
    target.mkdir()
    (target / 'data').mkdir()
    outside = tmp_path / 'outside-chroma'
    outside.mkdir()
    link = target / 'data' / 'chroma_db'
    try:
        _make_junction(link, outside)
        with pytest.raises(restore_db.RestoreTransactionError):
            restore_db._validate_target_types(target)
        assert outside.exists()
    finally:
        _remove_junction(link)


def test_b1_junction_data_directory_rejected(tmp_path):
    target = tmp_path / 'junction-data-target'
    target.mkdir()
    outside = tmp_path / 'outside-data'
    outside.mkdir()
    link = target / 'data'
    try:
        _make_junction(link, outside)
        with pytest.raises(restore_db.RestoreTransactionError):
            restore_db._validate_target_types(target)
        assert outside.exists()
    finally:
        _remove_junction(link)


@pytest.mark.parametrize('control_name', [
    '.restore_staging', '.restore_state', '.restore_rollback',
])
def test_b1_junction_control_directory_rejected(tmp_path, control_name):
    target = tmp_path / 'junction-control-target'
    target.mkdir()
    outside = tmp_path / 'outside-control'
    outside.mkdir()
    link = target / control_name
    try:
        _make_junction(link, outside)
        with pytest.raises(restore_db.RestoreTransactionError):
            restore_db._validate_control_paths(target)
        assert outside.exists()
    finally:
        _remove_junction(link)


def test_b1_junction_target_root_rejected(tmp_path):
    outside = tmp_path / 'outside-target-root'
    outside.mkdir()
    target = tmp_path / 'target-root-link'
    try:
        _make_junction(target, outside)
        with pytest.raises(restore_db.RestoreTransactionError):
            restore_db._validate_target_types(target)
        assert outside.exists()
    finally:
        _remove_junction(target)


def test_b1_junction_staging_run_rejected_by_cleanup(tmp_path):
    target = _prepare_target(tmp_path)
    pre_root = tmp_path / 'pre_restore'
    state = _write_state_record(target, pre_root)
    outside = tmp_path / 'outside-staging-run'
    outside.mkdir()
    link = target / '.restore_staging' / HARDENING_RUN_ID
    try:
        _make_junction(link, outside)
        assert not restore_db._cleanup_staging(link, target, state)
        assert outside.exists()
    finally:
        _remove_junction(link)


def test_b1_junction_rollback_run_rejected_by_scan(tmp_path):
    target = _prepare_target(tmp_path)
    pre_root = tmp_path / 'pre_restore'
    _write_state_record(target, pre_root)
    outside = tmp_path / 'outside-rollback-run'
    outside.mkdir()
    link = target / '.restore_rollback' / HARDENING_RUN_ID
    try:
        _make_junction(link, outside)
        problems = restore_db._scan_incomplete_runs(target, pre_root)
        assert any('符号链接' in problem or 'reparse' in problem
                   for problem in problems)
        assert outside.exists()
    finally:
        _remove_junction(link)


def test_b1_junction_pre_restore_rejected_by_scan(tmp_path):
    target = _prepare_target(tmp_path)
    pre_root = tmp_path / 'pre_restore'
    pre_root.mkdir()
    outside = tmp_path / 'outside-pre-restore'
    outside.mkdir()
    link = pre_root / ('pre_restore_' + HARDENING_RUN_ID)
    try:
        _make_junction(link, outside)
        _write_state_record(target, pre_root)
        problems = restore_db._scan_incomplete_runs(target, pre_root)
        assert any('符号链接' in problem or 'reparse' in problem
                   for problem in problems)
        assert outside.exists()
    finally:
        _remove_junction(link)


class _FakeChromaCollection:
    def __init__(self, count, result):
        self._count = count
        self._result = result

    def count(self):
        return self._count

    def get(self, **kwargs):
        return self._result


def test_b1_chroma_embedding_missing_rejected():
    missing = _FakeChromaCollection(1, {
        'ids': ['a'],
        'metadatas': [{'user_id': 1, 'document_id': 'doc'}],
        'embeddings': None,
    })
    with pytest.raises(restore_db.RestoreTransactionError):
        restore_db._verify_chroma_records(missing)


def test_b1_chroma_embedding_dimension_mismatch_rejected():
    collection = _FakeChromaCollection(2, {
        'ids': ['a', 'b'],
        'metadatas': [
            {'user_id': 1, 'document_id': 'doc-a'},
            {'user_id': 1, 'document_id': 'doc-b'},
        ],
        'embeddings': [[1.0, 0.0, 0.0], [0.0, 1.0]],
    })
    with pytest.raises(restore_db.RestoreTransactionError):
        restore_db._verify_chroma_records(collection)


# ---- B1-a cross-process restore lock ----

def test_b1_restore_lock_same_target_exclusive(tmp_path):
    target = tmp_path / 'lock-target'
    target.mkdir()
    registry = tmp_path / 'lock-registry'
    first = restore_db.RestoreLock(target, registry)
    second = restore_db.RestoreLock(target, registry)
    first.acquire()
    try:
        with pytest.raises(restore_db.RestoreLockBusy):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_b1_restore_lock_different_targets_parallel(tmp_path):
    first_target = tmp_path / 'lock-target-a'
    second_target = tmp_path / 'lock-target-b'
    first_target.mkdir()
    second_target.mkdir()
    registry = tmp_path / 'lock-registry'
    first = restore_db.RestoreLock(first_target, registry)
    second = restore_db.RestoreLock(second_target, registry)
    first.acquire()
    try:
        second.acquire()
        second.release()
    finally:
        first.release()


def test_b1_restore_lock_nested_targets_rejected(tmp_path):
    outer = tmp_path / 'lock-outer'
    inner = outer / 'inner'
    inner.mkdir(parents=True)
    registry = tmp_path / 'lock-registry'
    outer_lock = restore_db.RestoreLock(outer, registry)
    inner_lock = restore_db.RestoreLock(inner, registry)
    outer_lock.acquire()
    try:
        with pytest.raises(restore_db.RestoreLockBusy):
            inner_lock.acquire()
    finally:
        outer_lock.release()
    inner_lock.acquire()
    inner_lock.release()


def test_b1_restore_lock_is_released_after_process_crash(tmp_path):
    target = tmp_path / 'lock-crash-target'
    target.mkdir()
    registry = tmp_path / 'lock-registry'
    scripts = Path(__file__).resolve().parents[2] / 'scripts'
    script = tmp_path / 'hold_lock.py'
    script.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, %r)\n"
        "from restore_root_lock import RootLock\n"
        "lock = RootLock(Path(sys.argv[1]), Path(sys.argv[2]))\n"
        "lock.acquire('child')\n"
        "print('LOCKED', flush=True)\n"
        "os._exit(0)\n" % str(scripts),
        encoding='utf-8')
    result = subprocess.run(
        [sys.executable, str(script), str(target), str(registry)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'LOCKED' in result.stdout
    lock = restore_db.RootLock(target, registry_root=registry)
    lock.acquire('after-crash')
    lock.release()


def test_b1_restore_lock_failure_has_no_target_state(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    registry_root = pre_root.parent / 'restore-registry'
    with restore_db.open_secure_directory(target) as handle:
        before_identity = handle.identity
    lock = restore_db.RootLock(target, registry_root=registry_root)
    lock.acquire('holder')
    try:
        before = _business_snapshot(target)
        business_files = [
            target / 'my_ai_assistant.db',
            target / 'data' / 'chroma_db' / 'old-chroma-marker.txt',
            target / 'data' / 'excel' / 'old-excel-marker.txt',
        ]
        before_ids = {str(path): _file_identity(path) for path in business_files}
        assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
            restore_db.EXIT_LOCKED)
        assert _business_snapshot(target) == before
        assert {str(path): _file_identity(path) for path in business_files} == before_ids
        with restore_db.open_secure_directory(target) as handle:
            assert handle.identity == before_identity
        with registry_module.RegistryStore(registry_root) as registry_store:
            entries = registry_store.load().entries
            assert [entry.run_id for entry in entries] == ['holder']
        assert not (target / '.restore_state').exists()
        assert not (target / '.restore_staging').exists()
        assert not list(pre_root.glob('pre_restore_*'))
    finally:
        lock.release()


def test_b1_second_real_main_process_is_locked_out(
        full_backup_and_target, monkeypatch, tmp_path):
    """A second real restore_db.main() must not enter staging."""
    backup_set, target = full_backup_and_target
    pre_root = tmp_path / 'pre_restore'
    registry_root = pre_root.parent / 'restore-registry'
    marker = tmp_path / 'main-held.marker'
    scripts = Path(__file__).resolve().parents[2] / 'scripts'
    child = tmp_path / 'hold_main_lock.py'
    child.write_text(
        "import importlib.util, os, sys, time\n"
        "from pathlib import Path\n"
        "scripts = Path(%r)\n"
        "sys.path.insert(0, str(scripts))\n"
        "spec = importlib.util.spec_from_file_location('rd_holder', %r)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "original = module._build_state\n"
        "def hold(*args, **kwargs):\n"
        "    Path(%r).write_text('LOCKED', encoding='utf-8')\n"
        "    time.sleep(30)\n"
        "    return original(*args, **kwargs)\n"
        "module._build_state = hold\n"
        "os.environ['MY_AI_ASSISTANT_RESTORE_REGISTRY_ROOT'] = %r\n"
        "sys.argv = ['restore_db.py', '--from', %r, '--target-root', %r,\n"
        "            '--backup-dir', %r, '--yes']\n"
        "raise SystemExit(module.main())\n" % (
            str(scripts), str(RESTORE_SCRIPT), str(marker),
            str(registry_root), str(backup_set), str(target), str(pre_root)),
        encoding='utf-8')
    process = subprocess.Popen(
        [sys.executable, str(child)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 15
        while not marker.exists() and time.time() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.05)
        assert marker.exists(), process.stderr.read()
        before = _business_snapshot(target)
        assert _run_restore(monkeypatch, backup_set, target, pre_root) == (
            restore_db.EXIT_LOCKED)
        assert _business_snapshot(target) == before
        assert not (target / '.restore_state').exists()
        assert not (target / '.restore_staging').exists()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    # The crashed holder leaves a stale registry record; the next main must
    # re-probe the OS lock and proceed without auto-recovering its state.
    assert _run_restore(monkeypatch, backup_set, target, pre_root) == 0


def test_b1_main_does_not_call_legacy_restore_lock(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target

    def fail_legacy(*_args, **_kwargs):
        raise AssertionError('legacy RestoreLock must not be called')

    monkeypatch.setattr(restore_db, 'RestoreLock', fail_legacy)
    assert _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore') == 0


def test_b1_restore_lock_released_after_success(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    assert _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore') == 0
    lock = restore_db.RootLock(
        target, registry_root=tmp_path / 'restore-registry')
    lock.acquire('after-success')
    lock.release()


def test_b1_restore_lock_released_after_rollback(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target

    def fail_switch(*_args, **_kwargs):
        raise RuntimeError('simulated switch failure for lock release')

    monkeypatch.setattr(restore_db, '_switch_sqlite', fail_switch)
    assert _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore') == restore_db.EXIT_TRANSACTION
    lock = restore_db.RootLock(
        target, registry_root=tmp_path / 'restore-registry')
    lock.acquire('after-rollback')
    lock.release()


def test_b1_restore_lock_file_symlink_rejected(tmp_path):
    target = tmp_path / 'lock-file-symlink-target'
    target.mkdir()
    lock_dir = target / '.restore_control'
    lock_dir.mkdir()
    external = tmp_path / 'external-lock-file'
    external.write_text('external', encoding='utf-8')
    link = lock_dir / 'restore.lock'
    try:
        link.symlink_to(external)
    except OSError as exc:
        pytest.skip('Windows symlink privilege unavailable: %s' % exc)
    with pytest.raises(restore_db.RestoreLockError):
        restore_db.RestoreLock(target, tmp_path / 'lock-registry').acquire()
    assert external.exists()


def test_b1_restore_lock_control_junction_rejected(tmp_path):
    target = tmp_path / 'lock-junction-target'
    target.mkdir()
    outside = tmp_path / 'outside-lock-control'
    outside.mkdir()
    link = target / '.restore_control'
    try:
        _make_junction(link, outside)
        with pytest.raises(restore_db.RestoreLockError):
            restore_db.RestoreLock(target, tmp_path / 'lock-registry').acquire()
        assert outside.exists()
    finally:
        _remove_junction(link)


class _FatalRestore(BaseException):
    """Test-only non-Exception failure to verify primary traceback retention."""


class _FailingTargetHandle:
    """Delegate to a real target handle, then fail exactly once on close."""

    def __init__(self, inner, error):
        self._inner = inner
        self._error = error
        self.close_attempts = 0

    @property
    def closed(self):
        return self._inner.closed

    @property
    def native_handle(self):
        return self._inner.native_handle

    @property
    def identity(self):
        return self._inner.identity

    def close(self):
        self.close_attempts += 1
        self._inner.close()
        raise self._error


def _track_opened_handles(monkeypatch):
    opened = []
    original = restore_db.open_secure_directory

    def tracked(path):
        handle = original(path)
        opened.append(handle)
        return handle

    monkeypatch.setattr(restore_db, 'open_secure_directory', tracked)
    return opened


def _inject_target_close_failure(monkeypatch, error=None):
    wrappers = []
    original = restore_db.open_secure_directory
    if error is None:
        error = RuntimeError('simulated target close failure')

    def wrapped(path):
        wrapper = _FailingTargetHandle(original(path), error)
        wrappers.append(wrapper)
        return wrapper

    monkeypatch.setattr(restore_db, 'open_secure_directory', wrapped)
    return wrappers


def _inject_release_failure(monkeypatch, error=None):
    calls = []
    original = restore_db.RootLock.release
    if error is None:
        error = RuntimeError('simulated release failure')

    def release_then_fail(lock):
        calls.append(lock)
        # Run the real release first so the test never leaks an OS lock.
        original(lock)
        raise error

    monkeypatch.setattr(restore_db.RootLock, 'release', release_then_fail)
    return calls


def _inject_root_lock_cleanup_interrupt(monkeypatch, error):
    """Make the real RootLock cleanup release raise after closing the OS lock."""
    captured = []
    original_from_handle = restore_db.RootLock.from_target_handle

    def from_target_handle(target_handle, registry_root=None):
        lock = original_from_handle(
            target_handle, registry_root=registry_root)
        original_release = lock.root_lock.release

        def release_then_interrupt():
            original_release()
            raise error

        monkeypatch.setattr(
            lock.root_lock, 'release', release_then_interrupt)
        captured.append(lock)
        return lock

    monkeypatch.setattr(
        restore_db.RootLock, 'from_target_handle', from_target_handle)
    return captured


def _assert_root_lock_released(target, registry_root, release=None):
    """Prove the OS RootLock is free by acquiring it with a fresh RootLock."""
    probe = restore_db.RootLock(target, registry_root=registry_root)
    acquired = False
    try:
        probe.acquire('post-cleanup-probe')
        acquired = True
    finally:
        if acquired:
            if release is None:
                probe.release()
            else:
                release(probe)


def test_p1_1_root_lock_probe_success_releases(monkeypatch):
    calls = []

    class _Probe:
        def __init__(self, target, registry_root=None):
            self.target = target
            self.registry_root = registry_root

        def acquire(self, run_id):
            calls.append(('acquire', run_id))

        def release(self):
            calls.append(('release',))

    monkeypatch.setattr(restore_db, 'RootLock', _Probe)
    _assert_root_lock_released('target', 'registry')
    assert calls == [('acquire', 'post-cleanup-probe'), ('release',)]


def test_p1_1_root_lock_probe_acquire_failure_skips_release(monkeypatch):
    released = []

    class _Probe:
        def __init__(self, target, registry_root=None):
            pass

        @staticmethod
        def acquire(_run_id):
            raise RuntimeError('probe acquire failed')

        @staticmethod
        def release():
            released.append(True)

    monkeypatch.setattr(restore_db, 'RootLock', _Probe)
    with pytest.raises(RuntimeError, match='probe acquire failed'):
        _assert_root_lock_released('target', 'registry')
    assert released == []


def test_p1_1_root_lock_probe_release_failure_is_exposed(monkeypatch):
    class _Probe:
        def __init__(self, target, registry_root=None):
            pass

        @staticmethod
        def acquire(_run_id):
            return None

        @staticmethod
        def release():
            raise RuntimeError('probe release failed')

    monkeypatch.setattr(restore_db, 'RootLock', _Probe)
    with pytest.raises(RuntimeError, match='probe release failed'):
        _assert_root_lock_released('target', 'registry')


def test_p1_1_main_success_cleanup_success(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    opened = _track_opened_handles(monkeypatch)
    assert _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore') == 0
    out, err = capsys.readouterr()
    assert '恢复完成' in out
    assert 'main 层资源释放失败' not in err
    assert opened and opened[0].closed
    _assert_root_lock_released(target, tmp_path / 'restore-registry')


def test_p1_1_main_success_release_failure_returns_9(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    opened = _track_opened_handles(monkeypatch)
    release_calls = _inject_release_failure(monkeypatch)

    code = _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert code == restore_db.EXIT_RELEASE_FAILURE
    assert release_calls
    assert opened and opened[0].closed
    assert 'lock_release' in err
    assert '数据阶段已完成，但资源释放失败' in err
    assert '最终退出码：9' in err
    assert '恢复完成' not in out


def test_p1_1_main_failure_cleanup_success_keeps_code(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target

    def fail_switch(*_args, **_kwargs):
        raise RuntimeError('simulated switch failure for cleanup success')

    monkeypatch.setattr(restore_db, '_switch_sqlite', fail_switch)
    code = _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert code == restore_db.EXIT_TRANSACTION
    assert 'simulated switch failure for cleanup success' in out
    assert 'main 层资源释放失败' not in err


def test_p1_1_main_failure_release_failure_keeps_original(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    opened = _track_opened_handles(monkeypatch)
    release_calls = _inject_release_failure(monkeypatch)

    def fail_switch(*_args, **_kwargs):
        raise RuntimeError('simulated switch failure with release failure')

    monkeypatch.setattr(restore_db, '_switch_sqlite', fail_switch)
    code = _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert code == restore_db.EXIT_TRANSACTION
    assert release_calls
    assert opened and opened[0].closed
    assert 'simulated switch failure with release failure' in out
    assert 'lock_release' in err
    assert '原始 Restore 退出码 7 保持不变' in err


def test_p1_1_main_success_target_close_failure_returns_9(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    wrappers = _inject_target_close_failure(monkeypatch)

    code = _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert code == restore_db.EXIT_RELEASE_FAILURE
    assert wrappers and wrappers[0].close_attempts == 1
    assert wrappers[0].closed
    assert 'target_close' in err
    assert '数据阶段已完成，但资源释放失败' in err
    assert '恢复完成' not in out
    _assert_root_lock_released(target, tmp_path / 'restore-registry')


def test_p1_1_main_exception_release_failure_preserves_traceback(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    opened = _track_opened_handles(monkeypatch)
    _inject_release_failure(monkeypatch)
    fatal = _FatalRestore('fatal restore failure')

    def boom(*_args, **_kwargs):
        raise fatal

    monkeypatch.setattr(restore_db, '_execute', boom)
    with pytest.raises(_FatalRestore) as caught:
        _run_restore(monkeypatch, backup_set, target,
                     tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert caught.value is fatal
    assert str(caught.value) == 'fatal restore failure'
    frames = traceback.extract_tb(caught.value.__traceback__)
    assert any(frame.name == 'boom' for frame in frames)
    assert opened and opened[0].closed
    assert 'lock_release' in err
    assert '原始 Restore 异常保留并原样传播' in err
    assert '恢复完成' not in out


def test_p1_1_main_preflight_failure_skips_cleanup(
        full_backup_and_target, monkeypatch, tmp_path):
    _backup_set, target = full_backup_and_target
    opened = _track_opened_handles(monkeypatch)
    release_calls = _inject_release_failure(monkeypatch)

    code = _run_restore(monkeypatch, tmp_path / 'missing-backup',
                        target, tmp_path / 'pre_restore')
    assert code == restore_db.EXIT_INVALID_BACKUP
    assert opened == []
    assert release_calls == []


def test_p1_1_main_root_lock_release_error_returns_9(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    opened = _track_opened_handles(monkeypatch)
    original_release = restore_db.RootLock.release

    def release_then_fail(lock):
        original_release(lock)
        raise RootLockReleaseError(
            'root release failure',
            [('registry', RuntimeError('registry close failed'))])

    monkeypatch.setattr(
        restore_db.RootLock, 'release', release_then_fail)
    code = _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert code == restore_db.EXIT_RELEASE_FAILURE
    assert opened and opened[0].closed
    assert 'root release failure' in err
    assert 'registry close failed' in err
    assert '恢复完成' not in out


def test_p1_2_main_success_internal_cleanup_interrupt_propagates(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    interrupt = KeyboardInterrupt('internal RootLock cleanup interrupted')
    captured = _inject_root_lock_cleanup_interrupt(monkeypatch, interrupt)

    with pytest.raises(KeyboardInterrupt) as caught:
        _run_restore(monkeypatch, backup_set, target,
                     tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert caught.value is interrupt
    assert captured
    assert captured[0].target.closed
    assert captured[0].registry.closed
    assert '恢复完成' not in out


def test_p1_2_main_failure_internal_cleanup_interrupt_propagates(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    interrupt = KeyboardInterrupt('internal cleanup interrupted after failure')
    captured = _inject_root_lock_cleanup_interrupt(monkeypatch, interrupt)

    def fail_switch(*_args, **_kwargs):
        raise RuntimeError('data failure before internal cleanup interrupt')

    monkeypatch.setattr(restore_db, '_switch_sqlite', fail_switch)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_restore(monkeypatch, backup_set, target,
                     tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert caught.value is interrupt
    assert captured and captured[0].target.closed
    assert 'data failure before internal cleanup interrupt' in out
    assert 'lock_release' in err


def test_p1_2_main_primary_exception_wins_over_internal_cleanup_interrupt(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    interrupt = KeyboardInterrupt('cleanup interrupt after primary')
    captured = _inject_root_lock_cleanup_interrupt(monkeypatch, interrupt)
    fatal = _FatalRestore('primary restore failure with cleanup interrupt')

    def boom(*_args, **_kwargs):
        raise fatal

    monkeypatch.setattr(restore_db, '_execute', boom)
    with pytest.raises(_FatalRestore) as caught:
        _run_restore(monkeypatch, backup_set, target,
                     tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert caught.value is fatal
    assert captured and captured[0].target.closed
    assert 'lock_release' in err
    assert '原始 Restore 异常保留并原样传播' in err
    assert '恢复完成' not in out


def test_p1_1_main_success_both_cleanup_failures_return_9(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    original_release = restore_db.RootLock.release
    wrappers = _inject_target_close_failure(monkeypatch)
    release_calls = _inject_release_failure(monkeypatch)

    code = _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert code == restore_db.EXIT_RELEASE_FAILURE
    assert release_calls
    assert wrappers and wrappers[0].close_attempts == 1
    assert wrappers[0].closed
    assert 'lock_release' in err
    assert 'target_close' in err
    assert '恢复完成' not in out
    _assert_root_lock_released(
        target, tmp_path / 'restore-registry', release=original_release)


def test_p1_1_main_failure_target_close_failure_keeps_code(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    wrappers = _inject_target_close_failure(monkeypatch)

    def fail_switch(*_args, **_kwargs):
        raise RuntimeError('simulated failure with target close failure')

    monkeypatch.setattr(restore_db, '_switch_sqlite', fail_switch)
    code = _run_restore(monkeypatch, backup_set, target,
                        tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert code == restore_db.EXIT_TRANSACTION
    assert 'simulated failure with target close failure' in out
    assert 'target_close' in err
    assert '原始 Restore 退出码 7 保持不变' in err
    assert wrappers and wrappers[0].closed


def test_p1_1_main_exception_target_close_failure_preserves_primary(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    wrappers = _inject_target_close_failure(monkeypatch)
    fatal = _FatalRestore('fatal with target close failure')

    def boom(*_args, **_kwargs):
        raise fatal

    monkeypatch.setattr(restore_db, '_execute', boom)
    with pytest.raises(_FatalRestore) as caught:
        _run_restore(monkeypatch, backup_set, target,
                     tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert caught.value is fatal
    assert any(frame.name == 'boom'
               for frame in traceback.extract_tb(caught.value.__traceback__))
    assert wrappers and wrappers[0].closed
    assert 'target_close' in err
    assert '原始 Restore 异常保留并原样传播' in err
    assert '恢复完成' not in out


def test_p1_1_main_exception_both_cleanup_failures_preserves_primary(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    wrappers = _inject_target_close_failure(monkeypatch)
    release_calls = _inject_release_failure(monkeypatch)
    fatal = _FatalRestore('fatal with both cleanup failures')

    def boom(*_args, **_kwargs):
        raise fatal

    monkeypatch.setattr(restore_db, '_execute', boom)
    with pytest.raises(_FatalRestore) as caught:
        _run_restore(monkeypatch, backup_set, target,
                     tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert caught.value is fatal
    assert release_calls
    assert wrappers and wrappers[0].closed
    assert 'lock_release' in err
    assert 'target_close' in err
    assert '原始 Restore 异常保留并原样传播' in err
    assert '恢复完成' not in out


def test_p1_1_main_cleanup_keyboard_interrupt_propagates(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    opened = _track_opened_handles(monkeypatch)
    _inject_release_failure(monkeypatch, KeyboardInterrupt('cleanup interrupt'))

    with pytest.raises(KeyboardInterrupt):
        _run_restore(monkeypatch, backup_set, target,
                     tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert opened and opened[0].closed
    assert 'lock_release' in err
    assert '清理阶段发生中断' in err
    assert '恢复完成' not in out


def test_p1_1_main_cleanup_system_exit_propagates(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    wrappers = _inject_target_close_failure(
        monkeypatch, SystemExit('cleanup exit'))

    with pytest.raises(SystemExit):
        _run_restore(monkeypatch, backup_set, target,
                     tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert wrappers and wrappers[0].closed
    assert 'target_close' in err
    assert '清理阶段发生中断' in err
    assert '恢复完成' not in out
    _assert_root_lock_released(target, tmp_path / 'restore-registry')


def test_p1_1_main_failure_cleanup_interrupt_propagates(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    opened = _track_opened_handles(monkeypatch)
    _inject_release_failure(monkeypatch, KeyboardInterrupt('failure interrupt'))

    def fail_switch(*_args, **_kwargs):
        raise RuntimeError('data failure before cleanup interrupt')

    monkeypatch.setattr(restore_db, '_switch_sqlite', fail_switch)
    with pytest.raises(KeyboardInterrupt):
        _run_restore(monkeypatch, backup_set, target,
                     tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert opened and opened[0].closed
    assert 'data failure before cleanup interrupt' in out
    assert 'lock_release' in err
    assert '清理阶段发生中断' in err


def test_p1_1_main_report_failure_preserves_primary(
        full_backup_and_target, monkeypatch, tmp_path):
    backup_set, target = full_backup_and_target
    opened = _track_opened_handles(monkeypatch)
    _inject_release_failure(monkeypatch)
    fatal = _FatalRestore('fatal despite report failure')

    def boom(*_args, **_kwargs):
        raise fatal

    def fail_report(_report):
        raise RuntimeError('simulated report failure')

    monkeypatch.setattr(restore_db, '_execute', boom)
    monkeypatch.setattr(restore_db, '_report_cleanup_errors', fail_report)
    with pytest.raises(_FatalRestore) as caught:
        _run_restore(monkeypatch, backup_set, target,
                     tmp_path / 'pre_restore')
    assert caught.value is fatal
    assert opened and opened[0].closed


def test_p1_1_main_helper_failure_preserves_primary(
        full_backup_and_target, monkeypatch, tmp_path, capsys):
    backup_set, target = full_backup_and_target
    opened = _track_opened_handles(monkeypatch)
    fatal = _FatalRestore('fatal despite helper failure')
    original = restore_db._release_main_resources

    def helper_then_fail(report, lock, target_handle):
        original(report, lock, target_handle)
        raise RuntimeError('simulated helper failure')

    def boom(*_args, **_kwargs):
        raise fatal

    monkeypatch.setattr(restore_db, '_execute', boom)
    monkeypatch.setattr(
        restore_db, '_release_main_resources', helper_then_fail)
    with pytest.raises(_FatalRestore) as caught:
        _run_restore(monkeypatch, backup_set, target,
                     tmp_path / 'pre_restore')
    out, err = capsys.readouterr()
    assert caught.value is fatal
    assert opened and opened[0].closed
    assert 'helper' in err
    assert '恢复完成' not in out


def test_p1_1_cleanup_helper_internal_failure_still_closes_target():
    closed = []

    class _ExplodingReport:
        def __init__(self):
            self.target_error = None
            self.helper_error = None

        @property
        def lock_error(self):
            return None

        @lock_error.setter
        def lock_error(self, _value):
            raise RuntimeError('slot write failure')

    class _FailingLock:
        @staticmethod
        def release():
            raise RuntimeError('lock release failure')

    class _Target:
        @staticmethod
        def close():
            closed.append(True)

    report = restore_db._release_main_resources(
        _ExplodingReport(), _FailingLock(), _Target())
    assert closed == [True]
    assert isinstance(report.helper_error, RuntimeError)
    assert 'slot write failure' in str(report.helper_error)
