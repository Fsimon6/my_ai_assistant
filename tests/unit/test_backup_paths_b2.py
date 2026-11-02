# -*- coding: utf-8 -*-
"""Phase 1.3 B2：备份源路径显式化、URL 解析与 fail-closed 测试。"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = REPO_ROOT / 'scripts' / 'backup_db.py'


def _load_backup():
    spec = importlib.util.spec_from_file_location('_backup_paths_b2', BACKUP_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup_db = _load_backup()


def _make_source(root: Path, db_name: str = 'my_ai_assistant.db') -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    db = root / db_name
    con = sqlite3.connect(str(db))
    try:
        con.execute('CREATE TABLE path_probe (id INTEGER PRIMARY KEY, value TEXT)')
        con.execute('INSERT INTO path_probe (value) VALUES (?)', ('synthetic',))
        con.commit()
    finally:
        con.close()
    chroma = root / 'data' / 'chroma_db'
    chroma.mkdir(parents=True)
    (chroma / 'marker.txt').write_text('synthetic chroma', encoding='utf-8')
    excel = root / 'data' / 'excel'
    excel.mkdir(parents=True)
    (excel / 'marker.txt').write_text('synthetic excel', encoding='utf-8')
    return db, chroma, excel


def _run(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, 'argv', ['backup_db.py', *argv])
    return backup_db.main(argv)


def _print_paths(monkeypatch, capsys, argv: list[str]):
    code = _run(monkeypatch, [*argv, '--print-paths'])
    return code, capsys.readouterr().out


def test_default_paths_use_repo_root_and_print_does_not_create(tmp_path, monkeypatch, capsys):
    repo = tmp_path / 'repo'
    _make_source(repo)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.setattr(backup_db, 'REPO_ROOT', repo)
    monkeypatch.chdir(elsewhere)

    code, out = _print_paths(monkeypatch, capsys, [])

    assert code == 0, out
    assert 'source_root: %s' % repo.resolve() in out
    assert 'database_src: %s' % (repo / 'my_ai_assistant.db').resolve() in out
    assert 'chroma_src: %s' % (repo / 'data' / 'chroma_db').resolve() in out
    assert 'excel_src: %s' % (repo / 'data' / 'excel').resolve() in out
    assert 'diagnosis: OK' in out
    assert not (repo / 'backups').exists()
    assert not (tmp_path / 'backups').exists()


def test_explicit_paths_override_and_relative_to_source_root(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source'
    _make_source(source)
    (source / 'custom').mkdir()
    custom_db = source / 'custom' / 'custom.db'
    custom_db.write_bytes((source / 'my_ai_assistant.db').read_bytes())
    chroma = source / 'vector-custom'
    chroma.mkdir()
    (chroma / 'marker').write_text('v', encoding='utf-8')
    excel = source / 'excel-custom'
    excel.mkdir()
    (excel / 'marker').write_text('e', encoding='utf-8')

    code, out = _print_paths(monkeypatch, capsys, [
        '--source-root', str(source),
        '--database-url', 'sqlite:///custom/custom.db',
        '--vector-db-path', 'vector-custom',
        '--excel-data-root', 'excel-custom',
    ])

    assert code == 0, out
    assert 'database_src: %s' % custom_db.resolve() in out
    assert 'chroma_src: %s' % chroma.resolve() in out
    assert 'excel_src: %s' % excel.resolve() in out
    assert 'origin=explicit' in out


def test_relative_source_root_is_relative_to_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / 'relative-source'
    _make_source(source)

    code, out = _print_paths(monkeypatch, capsys, ['--source-root', 'relative-source'])

    assert code == 0, out
    assert 'source_root: %s' % source.resolve() in out
    assert 'database_src: %s' % (source / 'my_ai_assistant.db').resolve() in out


def test_default_manifest_records_actual_sources_and_origins(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    _make_source(source)
    backup_dir = tmp_path / 'backups'
    monkeypatch.setattr(backup_db, 'REPO_ROOT', source)

    code = _run(monkeypatch, ['--backup-dir', str(backup_dir), '--keep', '0', '--quiet'])
    assert code == 0

    backup_set = next(backup_dir.glob('ai_assistant_backup_*'))
    manifest = json.loads((backup_set / 'manifest.json').read_text(encoding='utf-8'))
    items = {i['type']: i for i in manifest['items']}
    assert set(items) == {'sqlite_database', 'vector_database', 'excel_artifacts'}
    assert items['sqlite_database']['src'] == str((source / 'my_ai_assistant.db').resolve())
    assert items['vector_database']['src'] == str((source / 'data' / 'chroma_db').resolve())
    assert items['excel_artifacts']['src'] == str((source / 'data' / 'excel').resolve())
    assert manifest['path_sources']['source_root'] == 'default'
    assert manifest['path_sources']['database'] == 'default'
    assert manifest['path_sources']['vector_database'] == 'default'
    assert manifest['path_sources']['excel_artifacts'] == 'default'
    assert manifest['source_paths']['database_src'] == items['sqlite_database']['src']
    assert 'database_url' not in json.dumps(manifest)
    assert '.env' not in [i['name'] for i in manifest['items']]


@pytest.mark.parametrize('label,action', [
    ('database', lambda db, chroma, excel: db.unlink()),
    ('chroma', lambda db, chroma, excel: (chroma / 'marker.txt').unlink()),
    ('excel', lambda db, chroma, excel: (excel / 'marker.txt').unlink()),
])
def test_fail_closed_missing_or_empty_source(tmp_path, monkeypatch, label, action):
    source = tmp_path / 'source'
    db, chroma, excel = _make_source(source)
    action(db, chroma, excel)
    backup_dir = tmp_path / 'backups'

    code = _run(monkeypatch, [
        '--source-root', str(source),
        '--backup-dir', str(backup_dir),
        '--keep', '0',
    ])

    assert code == backup_db.EXIT_SOURCE_ERROR
    assert not list(backup_dir.glob('ai_assistant_backup_*'))
    assert not (source / 'data' / 'chroma_db' / 'marker.txt').exists() or label != 'chroma'
    assert not backup_dir.exists() or not any(backup_dir.iterdir())


@pytest.mark.parametrize('sub', ['chroma_db', 'excel'])
def test_fail_closed_missing_source_directories(tmp_path, monkeypatch, sub):
    source = tmp_path / 'source'
    _make_source(source)
    (source / 'data' / sub).rename(source / 'data' / (sub + '-away'))
    backup_dir = tmp_path / 'backups'

    assert _run(monkeypatch, [
        '--source-root', str(source),
        '--backup-dir', str(backup_dir),
    ]) == backup_db.EXIT_SOURCE_ERROR
    assert not backup_dir.exists() or not list(backup_dir.glob('ai_assistant_backup_*'))


def test_backup_failure_cleans_current_artifact(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    _make_source(source)
    backup_dir = tmp_path / 'backups'
    before = {
        p.relative_to(source).as_posix(): p.read_bytes()
        for p in source.rglob('*') if p.is_file()
    }

    def fail_zip(_src, _dst):
        raise RuntimeError('simulated zip failure')

    monkeypatch.setattr(backup_db, 'zip_tree', fail_zip)
    assert _run(monkeypatch, [
        '--source-root', str(source),
        '--backup-dir', str(backup_dir),
    ]) == backup_db.EXIT_BACKUP_ERROR
    assert not backup_dir.exists()
    after = {
        p.relative_to(source).as_posix(): p.read_bytes()
        for p in source.rglob('*') if p.is_file()
    }
    assert after == before


def test_fail_closed_permission_error_is_reported(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source'
    _make_source(source)
    backup_dir = tmp_path / 'backups'

    def denied(_path):
        raise PermissionError('simulated access denied')

    monkeypatch.setattr(Path, 'iterdir', denied)
    code = _run(monkeypatch, [
        '--source-root', str(source),
        '--backup-dir', str(backup_dir),
    ])
    err = capsys.readouterr().err

    assert code == backup_db.EXIT_SOURCE_ERROR
    assert 'Chroma 目录无法读取' in err
    assert not list(backup_dir.glob('ai_assistant_backup_*'))


def test_print_paths_invalid_is_read_only(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source'
    _make_source(source)
    (source / 'data' / 'chroma_db' / 'marker.txt').unlink()
    before = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*')}

    code, out = _print_paths(monkeypatch, capsys, ['--source-root', str(source)])
    after = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*')}

    assert code == 1
    assert 'diagnosis: FAILED' in out
    assert before == after
    assert not (tmp_path / 'backups').exists()


@pytest.mark.parametrize('url', [
    'sqlite:///custom.db',
    'sqlite+pysqlite:///custom.db',
])
def test_sqlite_url_relative_forms(tmp_path, url):
    source = tmp_path / 'source'
    source.mkdir()
    path = backup_db.resolve_database_url(url, source)
    assert path == (source / 'custom.db').resolve()


def test_sqlite_url_windows_absolute(tmp_path):
    path = backup_db.resolve_database_url(
        'sqlite:///' + tmp_path.as_posix() + '/absolute.db', tmp_path)
    assert path == (tmp_path / 'absolute.db').resolve()
    assert path.is_absolute()


@pytest.mark.parametrize('url', [
    'sqlite:///:memory:',
    'sqlite:///file:custom.db?uri=true',
    'sqlite:///custom.db?mode=ro',
    'postgresql://user:secret@example.invalid/db',
    '',
    'not-a-url',
])
def test_sqlite_url_rejected_unsupported_forms(tmp_path, url):
    with pytest.raises(backup_db.BackupPathError):
        backup_db.resolve_database_url(url, tmp_path)


def test_uri_query_does_not_land_in_manifest(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    _make_source(source)
    backup_dir = tmp_path / 'backups'

    code = _run(monkeypatch, [
        '--source-root', str(source),
        '--database-url', 'sqlite:///custom.db?secret=should-not-leak',
        '--backup-dir', str(backup_dir),
    ])

    assert code == backup_db.EXIT_PATH_ERROR
    assert not list(backup_dir.glob('ai_assistant_backup_*'))


def test_cli_help_lists_new_path_options(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        backup_db.main(['--help'])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert '--source-root' in out
    assert '--database-url' in out
    assert '--vector-db-path' in out
    assert '--excel-data-root' in out
    assert '--print-paths' in out
