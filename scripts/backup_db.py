#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跨平台备份（Windows / Linux / macOS）：SQLite 在线备份 + Chroma 向量库 + Excel 原件。

路径规则（B2）：
  * 默认数据源根 = 脚本所在仓库根（REPO_ROOT），不受启动 CWD 影响；
  * `--source-root` 可覆盖数据源根；相对值以当前 CWD 为基准；
  * 默认布局：
        <source-root>/my_ai_assistant.db
        <source-root>/data/chroma_db
        <source-root>/data/excel
  * 显式 `--database-url` / `--vector-db-path` / `--excel-data-root` 优先；
  * 显式相对路径统一以 `source-root` 为基准；
  * 不读取 `.env`，不导入 backend.config；不会声称自动跟随运行时 Settings。

安全行为：
  * 三类数据源必须全部存在且可备份，否则 fail closed；
  * 备份失败只清理本次创建的备份目录，不删除历史备份或正式数据；
  * `--print-paths` 只读诊断，不创建文件/目录。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKUP_PREFIX = 'ai_assistant_backup_'

EXIT_PATH_ERROR = 2
EXIT_SOURCE_ERROR = 3
EXIT_BACKUP_ERROR = 4


class BackupPathError(Exception):
    """路径配置或 SQLite URL 无法安全解析。"""


class BackupSourceError(Exception):
    """数据源不存在、类型错误、目录为空或不可读取。"""


def _resolve_path(value: str | Path, base: Path) -> Path:
    """解析路径：绝对路径保留，相对路径以 base 为基准；统一 resolve。"""
    raw = str(value).strip()
    if not raw:
        raise BackupPathError('路径不能为空')
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def resolve_database_url(database_url: str, source_root: Path) -> Path:
    """把 SQLite SQLAlchemy URL 解析为最终文件路径。

    仅支持普通 SQLite 文件数据库；拒绝查询参数、URI 文件、:memory: 和
    非 SQLite 方言，避免把查询参数误当文件名或静默退回默认库。
    """
    raw = (database_url or '').strip()
    if not raw:
        raise BackupPathError('--database-url 不能为空')
    try:
        parsed = make_url(raw)
    except Exception as exc:  # noqa: BLE001 - SQLAlchemy 的异常类型可能变化
        raise BackupPathError('无法解析 --database-url') from exc

    if parsed.get_backend_name() != 'sqlite':
        raise BackupPathError('--database-url 仅支持 SQLite')
    if parsed.query:
        raise BackupPathError(
            '--database-url 不支持查询参数/URI 形式，请传入普通 SQLite 文件 URL')
    database = parsed.database
    if not database or database in (':memory:', 'file::memory:'):
        raise BackupPathError('--database-url 必须指向普通 SQLite 文件，不支持 :memory:')
    if database.startswith('file:'):
        raise BackupPathError('--database-url 不支持 file: URI 形式')

    path = Path(database).expanduser()
    if not path.is_absolute():
        path = source_root / path
    return path.resolve()


def _source_status(path: Path, kind: str) -> tuple[bool, bool, bool]:
    """返回 (exists, type_ok, non_empty)。只读，不创建任何东西。"""
    exists = path.exists()
    if not exists:
        return False, False, False
    if kind == 'file':
        return True, path.is_file(), False
    if not path.is_dir():
        return True, False, False
    try:
        non_empty = any(path.iterdir())
    except OSError:
        return True, False, False
    return True, True, non_empty


def _validate_sources(db_src: Path, chroma_src: Path, excel_src: Path) -> None:
    """在创建任何备份目录前检查三类源；不允许部分备份。"""
    if not db_src.exists():
        raise BackupSourceError('SQLite 数据库不存在：%s' % db_src)
    if not db_src.is_file():
        raise BackupSourceError('SQLite 路径不是普通文件：%s' % db_src)

    if not chroma_src.exists():
        raise BackupSourceError('Chroma 目录不存在：%s' % chroma_src)
    if not chroma_src.is_dir():
        raise BackupSourceError('Chroma 路径不是目录：%s' % chroma_src)
    try:
        if not any(chroma_src.iterdir()):
            raise BackupSourceError('Chroma 目录为空：%s' % chroma_src)
    except BackupSourceError:
        raise
    except OSError as exc:
        raise BackupSourceError('Chroma 目录无法读取：%s（%s）' % (chroma_src, exc)) from exc

    if not excel_src.exists():
        raise BackupSourceError('Excel 目录不存在：%s' % excel_src)
    if not excel_src.is_dir():
        raise BackupSourceError('Excel 路径不是目录：%s' % excel_src)
    try:
        if not any(excel_src.iterdir()):
            raise BackupSourceError('Excel 目录为空：%s' % excel_src)
    except BackupSourceError:
        raise
    except OSError as exc:
        raise BackupSourceError('Excel 目录无法读取：%s（%s）' % (excel_src, exc)) from exc


def _print_paths(source_root: Path, db_src: Path, chroma_src: Path,
                 excel_src: Path, origins: dict[str, str]) -> int:
    checks = [
        ('source_root', source_root, 'dir', origins['source_root']),
        ('database_src', db_src, 'file', origins['database']),
        ('chroma_src', chroma_src, 'dir', origins['vector_database']),
        ('excel_src', excel_src, 'dir', origins['excel_artifacts']),
    ]
    ok = True
    for label, path, kind, origin in checks:
        exists, type_ok, non_empty = _source_status(path, kind)
        if kind == 'file':
            valid = exists and type_ok
        elif label == 'source_root':
            valid = exists and type_ok
        else:
            valid = exists and type_ok and non_empty
        ok = ok and valid
        print('%s: %s' % (label, path))
        print('  origin=%s exists=%s type_ok=%s non_empty=%s valid=%s'
              % (origin, exists, type_ok, non_empty, valid))
    print('diagnosis: %s' % ('OK' if ok else 'FAILED'))
    return 0 if ok else 1


def _make_manifest_paths(source_root: Path, db_src: Path, chroma_src: Path,
                         excel_src: Path, origins: dict[str, str]) -> dict[str, Any]:
    return {
        'source_root': str(source_root),
        'path_sources': dict(origins),
        # 只记录解析后的路径，不记录 --database-url 原文（可能含凭据）。
        'database_src': str(db_src),
        'chroma_src': str(chroma_src),
        'excel_src': str(excel_src),
    }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def zip_tree(src: Path, dst_zip: Path) -> None:
    """把目录打包为 zip（保留相对路径）。"""
    with zipfile.ZipFile(dst_zip, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, _dirs, files in os.walk(src):
            for name in files:
                fp = Path(dirpath) / name
                zf.write(fp, fp.relative_to(src.parent))


def sqlite_snapshot(src: Path, dst: Path) -> None:
    """用 SQLite 在线备份 API 生成一致性快照（运行中亦可）。"""
    con = sqlite3.connect('file:%s?mode=ro' % src.as_posix(), uri=True)
    try:
        out = sqlite3.connect(str(dst))
        try:
            con.backup(out)
        finally:
            out.close()
    finally:
        con.close()


def prune(backup_dir: Path, keep: int) -> list[str]:
    sets = sorted([d for d in backup_dir.iterdir()
                   if d.is_dir() and d.name.startswith(BACKUP_PREFIX)],
                  key=lambda d: d.stat().st_mtime, reverse=True)
    removed = []
    for d in sets[keep:]:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    return removed


def _resolve_sources(args: argparse.Namespace) -> tuple[
        Path, Path, Path, Path, dict[str, str]]:
    source_root = (
        _resolve_path(args.source_root, Path.cwd())
        if args.source_root is not None else REPO_ROOT.resolve()
    )
    origins = {
        'source_root': 'explicit' if args.source_root is not None else 'default',
        'database': 'explicit' if args.database_url is not None else 'default',
        'vector_database': 'explicit' if args.vector_db_path is not None else 'default',
        'excel_artifacts': 'explicit' if args.excel_data_root is not None else 'default',
    }

    if args.database_url is not None:
        db_src = resolve_database_url(args.database_url, source_root)
    else:
        db_src = (source_root / 'my_ai_assistant.db').resolve()

    if args.vector_db_path is not None:
        chroma_src = _resolve_path(args.vector_db_path, source_root)
    else:
        chroma_src = (source_root / 'data' / 'chroma_db').resolve()

    if args.excel_data_root is not None:
        excel_src = _resolve_path(args.excel_data_root, source_root)
    else:
        excel_src = (source_root / 'data' / 'excel').resolve()

    return source_root, db_src, chroma_src, excel_src, origins


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='备份业务库 + 向量库 + Excel 原件')
    ap.add_argument('--backup-dir', default=str(REPO_ROOT / 'backups'),
                    help='备份输出目录（默认 <repo>/backups；相对值相对当前 CWD）')
    ap.add_argument('--keep', type=int, default=7,
                    help='保留最近 N 套（默认 7；0 表示不清理）')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--source-root', default=None,
                    help='数据源根目录；默认脚本所在仓库根；相对值相对当前 CWD')
    ap.add_argument('--database-url', default=None,
                    help='SQLite SQLAlchemy URL；相对 SQLite 路径以 source-root 为基准')
    ap.add_argument('--vector-db-path', default=None,
                    help='Chroma 持久化目录；相对值以 source-root 为基准')
    ap.add_argument('--excel-data-root', default=None,
                    help='Excel artifacts 根目录；相对值以 source-root 为基准')
    ap.add_argument('--print-paths', action='store_true',
                    help='只读打印解析后的四类路径，不创建目录/文件、不执行备份')
    args = ap.parse_args(argv)

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg)

    try:
        source_root, db_src, chroma_src, excel_src, origins = _resolve_sources(args)
    except BackupPathError as exc:
        print('路径配置错误：%s' % exc, file=sys.stderr)
        return EXIT_PATH_ERROR

    if args.print_paths:
        return _print_paths(source_root, db_src, chroma_src, excel_src, origins)

    try:
        _validate_sources(db_src, chroma_src, excel_src)
    except BackupSourceError as exc:
        print('数据源预检失败：%s' % exc, file=sys.stderr)
        return EXIT_SOURCE_ERROR

    backup_dir = Path(args.backup_dir).resolve()
    backup_dir_existed = backup_dir.exists()
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print('无法创建备份目录：%s（%s）' % (backup_dir, exc), file=sys.stderr)
        return EXIT_BACKUP_ERROR

    name = BACKUP_PREFIX + datetime.now().strftime('%Y%m%d_%H%M%S')
    dest = backup_dir / name
    try:
        dest.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        print('无法创建本次备份目录：%s（%s）' % (dest, exc), file=sys.stderr)
        if not backup_dir_existed:
            try:
                if backup_dir.is_dir() and not any(backup_dir.iterdir()):
                    backup_dir.rmdir()
            except OSError:
                pass
        return EXIT_BACKUP_ERROR

    created_dest = False
    try:
        created_dest = True
        items: list[dict] = []
        say('开始备份 → %s' % dest)

        dst_db = dest / 'my_ai_assistant.db'
        sqlite_snapshot(db_src, dst_db)
        items.append({'name': dst_db.name, 'type': 'sqlite_database',
                      'src': str(db_src), 'size': dst_db.stat().st_size,
                      'sha256': sha256_file(dst_db)})
        say('  [OK] SQLite 快照：%s（%.1f MB）' % (db_src, dst_db.stat().st_size / 1e6))

        z = dest / 'chroma_db.zip'
        zip_tree(chroma_src, z)
        items.append({'name': z.name, 'type': 'vector_database', 'src': str(chroma_src),
                      'size': z.stat().st_size, 'sha256': sha256_file(z)})
        say('  [OK] 向量库：%s（%.1f MB）' % (chroma_src, z.stat().st_size / 1e6))

        z = dest / 'excel.zip'
        zip_tree(excel_src, z)
        items.append({'name': z.name, 'type': 'excel_artifacts', 'src': str(excel_src),
                      'size': z.stat().st_size, 'sha256': sha256_file(z)})
        say('  [OK] Excel 原件：%s（%.1f MB）' % (excel_src, z.stat().st_size / 1e6))

        manifest = {
            'name': name,
            'created_at': datetime.now().isoformat(timespec='seconds'),
            'hostname': socket.gethostname(),
            'app_root': str(source_root),
            'path_sources': dict(origins),
            'source_paths': _make_manifest_paths(
                source_root, db_src, chroma_src, excel_src, origins),
            'items': items,
            'note': '本备份不含 .env 等密钥文件；请单独安全保管密钥。',
        }
        (dest / 'manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

    except Exception as exc:  # noqa: BLE001 - 备份失败必须清理本次产物
        if created_dest:
            shutil.rmtree(dest, ignore_errors=True)
        if not backup_dir_existed:
            try:
                if backup_dir.is_dir() and not any(backup_dir.iterdir()):
                    backup_dir.rmdir()
            except OSError:
                pass
        print('备份失败：%s' % exc, file=sys.stderr)
        return EXIT_BACKUP_ERROR

    if args.keep > 0:
        removed = prune(backup_dir, args.keep)
        if removed:
            say('  清理旧备份：%s' % ', '.join(removed))

    total = sum(i['size'] for i in items)
    say('备份完成：%s（%d 项，合计 %.1f MB）' % (name, len(items), total / 1e6))
    say('恢复命令：python scripts/restore_db.py --from "%s" --yes' % dest)
    return 0


if __name__ == '__main__':
    sys.exit(main())
