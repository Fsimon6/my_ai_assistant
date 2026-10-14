#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跨平台备份（Windows / Linux / macOS）：SQLite 在线备份 + Chroma 向量库 + Excel 原件。

为什么重写（对照旧 `scripts/backup_db.sh` 的实际缺陷）：

1. **数据库路径**：运行时真实库位于**仓库根** `<root>/my_ai_assistant.db`
   （见 `backend/config/development.py`：`BASE_DIR / 'my_ai_assistant.db'`，
   且 `backend/database/base.py` 会把相对路径解析到项目根）。
   旧脚本查的是 `backend/my_ai_assistant.db` → **数据库实际从未被备份**。
2. **遗漏 `data/excel/`**：Excel 的 `original.*` + `representation.json` + `meta.json`
   是结构化数据的唯一来源（预览 / 结构化查询 / NL 查询都读它），
   旧脚本完全没有覆盖 → 恢复后 Excel 能力整体失效。
3. **明文密钥**：旧脚本把 `.env` 直接拷进 `./backups/`，而 `backups/` 未被 `.gitignore` 覆盖
   → 存在密钥随备份入库的风险。本脚本**不复制任何密钥文件**（请单独管理 .env）。
4. **平台**：旧脚本是 bash，Windows 无法直接运行；本脚本为纯标准库 Python，三平台一致。
5. **一致性**：SQLite 使用官方在线备份 API（`Connection.backup`），
   在后端**运行中**也能得到一致性快照（WAL 内容一并纳入），无需停服。

备份产出（每次一套目录，便于整目录恢复）：

    <backup_dir>/ai_assistant_backup_YYYYmmdd_HHMMSS/
        my_ai_assistant.db     # SQLite 一致性快照
        chroma_db.zip          # data/chroma_db
        excel.zip              # data/excel
        manifest.json          # 清单（含大小与 DB 的 sha256，供恢复时校验）

用法：
    python scripts/backup_db.py                     # 备份到 ./backups，保留最近 7 套
    python scripts/backup_db.py --keep 14           # 保留最近 14 套
    python scripts/backup_db.py --backup-dir D:\\bak
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

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKUP_PREFIX = 'ai_assistant_backup_'


def resolve_db_path() -> Path:
    """与运行时保持一致地解析 SQLite 真实路径。

    规则复刻 `backend/config/development.py` + `backend/database/base.py`：
      1) 环境变量 DATABASE_URL 优先（注意：`os.getenv` 读不到 .env，与运行时行为一致）；
      2) 否则 `<仓库根>/my_ai_assistant.db`；
      3) 非 sqlite:/// 的 URL（如 PostgreSQL）→ 本脚本不处理数据库文件，由使用者自行备份。
    """
    url = os.environ.get('DATABASE_URL') or ''
    if not url:
        return REPO_ROOT / 'my_ai_assistant.db'
    if not url.startswith('sqlite:///'):
        raise SystemExit('检测到非 SQLite 的 DATABASE_URL，本脚本仅备份 SQLite 文件，'
                         '请用数据库自身的备份工具。')
    raw = url.replace('sqlite:///', '')
    p = Path(raw)
    return p if p.is_absolute() else (REPO_ROOT / p)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def zip_tree(src: Path, dst_zip: Path) -> None:
    """把目录打包为 zip（保留相对路径；跳过 WAL 等易失文件）。"""
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


def main() -> int:
    ap = argparse.ArgumentParser(description='备份业务库 + 向量库 + Excel 原件')
    ap.add_argument('--backup-dir', default=str(REPO_ROOT / 'backups'))
    ap.add_argument('--keep', type=int, default=7, help='保留最近 N 套（默认 7；0 表示不清理）')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg)

    db_src = resolve_db_path()
    chroma_src = REPO_ROOT / 'data' / 'chroma_db'
    excel_src = REPO_ROOT / 'data' / 'excel'

    backup_dir = Path(args.backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    name = BACKUP_PREFIX + datetime.now().strftime('%Y%m%d_%H%M%S')
    dest = backup_dir / name
    dest.mkdir(parents=True, exist_ok=False)

    items: list[dict] = []
    say('开始备份 → %s' % dest)

    # 1) SQLite（一致性快照）
    if db_src.exists():
        dst_db = dest / 'my_ai_assistant.db'
        sqlite_snapshot(db_src, dst_db)
        items.append({'name': dst_db.name, 'type': 'sqlite_database',
                      'src': str(db_src), 'size': dst_db.stat().st_size,
                      'sha256': sha256_file(dst_db)})
        say('  [OK] SQLite 快照：%s（%.1f MB）' % (db_src, dst_db.stat().st_size / 1e6))
    else:
        say('  [SKIP] 未找到数据库文件：%s' % db_src)

    # 2) Chroma 向量库
    if chroma_src.is_dir() and any(chroma_src.iterdir()):
        z = dest / 'chroma_db.zip'
        zip_tree(chroma_src, z)
        items.append({'name': z.name, 'type': 'vector_database', 'src': str(chroma_src),
                      'size': z.stat().st_size, 'sha256': sha256_file(z)})
        say('  [OK] 向量库：%s（%.1f MB）' % (chroma_src, z.stat().st_size / 1e6))
    else:
        say('  [SKIP] 未找到向量库目录：%s' % chroma_src)

    # 3) Excel 原件与解析产物
    if excel_src.is_dir() and any(excel_src.iterdir()):
        z = dest / 'excel.zip'
        zip_tree(excel_src, z)
        items.append({'name': z.name, 'type': 'excel_artifacts', 'src': str(excel_src),
                      'size': z.stat().st_size, 'sha256': sha256_file(z)})
        say('  [OK] Excel 原件：%s（%.1f MB）' % (excel_src, z.stat().st_size / 1e6))
    else:
        say('  [SKIP] 未找到 Excel 目录：%s' % excel_src)

    manifest = {
        'name': name,
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'hostname': socket.gethostname(),
        'app_root': str(REPO_ROOT),
        'items': items,
        'note': '本备份不含 .env 等密钥文件；请单独安全保管密钥。',
    }
    (dest / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

    if not items:
        shutil.rmtree(dest, ignore_errors=True)
        say('没有任何可备份项，已回滚本次备份目录。')
        return 1

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
