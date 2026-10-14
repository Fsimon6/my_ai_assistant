#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 `scripts/backup_db.py` 产出的备份集恢复（Windows / Linux / macOS，纯标准库）。

安全设计：
  * 必须显式 `--yes` 才会写入（防误触）；
  * 恢复前**自动**把当前数据库 / 向量库 / Excel 目录另存为
    `<backup_dir>/pre_restore_<时间戳>/`（出现问题时可按同法回退）；
  * 用 manifest 里的 sha256 校验数据库快照完整性，校验失败即中止；
  * `--target-root` 支持恢复到演练目录（不碰真实数据），用于定期演练。

用法：
    # 1) 先停止后端（Windows：Ctrl+C 结束 uvicorn；Docker：docker compose stop backend）
    # 2) 恢复
    python scripts/restore_db.py --from backups/ai_assistant_backup_20261014_160000 --yes
    # 3) 演练（写到临时目录，不动真实数据）
    python scripts/restore_db.py --from backups/<set> --yes --target-root D:\\drill
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def unzip_into(src_zip: Path, dest_parent: Path) -> None:
    """把 zip 解压回其原始父目录结构（backup_db.zip_tree 保留 src.parent 起的相对路径）。"""
    dest_parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src_zip) as zf:
        zf.extractall(dest_parent)


def main() -> int:
    ap = argparse.ArgumentParser(description='从备份集恢复业务库 / 向量库 / Excel 原件')
    ap.add_argument('--from', dest='src', required=True, help='备份集目录')
    ap.add_argument('--target-root', default=str(REPO_ROOT),
                    help='恢复到哪个项目根（默认当前仓库根；演练时可指定临时目录）')
    ap.add_argument('--yes', action='store_true', help='确认执行（必需）')
    ap.add_argument('--backup-dir', default=str(REPO_ROOT / 'backups'),
                    help='pre_restore 快照与备份集所在目录')
    args = ap.parse_args()

    src = Path(args.src).resolve()
    root = Path(args.target_root).resolve()
    if not src.is_dir():
        print('备份集目录不存在：%s' % src)
        return 2
    manifest_path = src / 'manifest.json'
    if not manifest_path.exists():
        print('备份集缺少 manifest.json（不是本工具产出的备份）：%s' % src)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))

    items = {i['type']: i for i in manifest.get('items', [])}
    db_item = items.get('sqlite_database')
    if not db_item:
        print('备份集内没有数据库快照，拒绝恢复。')
        return 2
    db_file = src / db_item['name']
    if sha256_file(db_file) != db_item.get('sha256'):
        print('数据库快照 sha256 校验失败，备份可能已损坏，已中止。')
        return 3

    print('备份集      ：%s（%s）' % (manifest['name'], manifest.get('created_at')))
    print('源项目根    ：%s' % manifest.get('app_root'))
    print('恢复目标    ：%s' % root)
    print('包含项      ：%s' % ', '.join(sorted(items)))

    if not args.yes:
        print('\n这是预演（未加 --yes，未做任何写入）。确认后请追加 --yes。')
        return 0

    # 恢复前快照（按同一套目录结构，便于回退）
    pre_dir = Path(args.backup_dir).resolve() / ('pre_restore_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    pre_dir.mkdir(parents=True, exist_ok=True)
    cur_db = root / 'my_ai_assistant.db'
    if cur_db.exists():
        shutil.copy2(cur_db, pre_dir / 'my_ai_assistant.db')
    for sub in ('chroma_db', 'excel'):
        cur = root / 'data' / sub
        if cur.is_dir():
            with zipfile.ZipFile(pre_dir / ('%s.zip' % sub), 'w', zipfile.ZIP_DEFLATED) as zf:
                for dirpath, _d, files in os.walk(cur):
                    for n in files:
                        fp = Path(dirpath) / n
                        zf.write(fp, fp.relative_to(cur.parent))
    print('\n已保存恢复前快照 → %s' % pre_dir)

    # 1) 数据库（并清掉旧的 -wal/-shm，避免与新快照不一致）
    shutil.copy2(db_file, cur_db)
    for suffix in ('-wal', '-shm'):
        stale = Path(str(cur_db) + suffix)
        if stale.exists():
            stale.unlink()
    print('  [OK] 已恢复数据库：%s' % cur_db)

    # 2) 向量库 / 3) Excel 原件
    for sub, key in (('chroma_db', 'vector_database'), ('excel', 'excel_artifacts')):
        if key not in items:
            print('  [SKIP] 备份集不含 %s' % sub)
            continue
        zi = src / items[key]['name']
        target_parent = root / 'data'
        old = target_parent / sub
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        unzip_into(zi, target_parent)
        print('  [OK] 已恢复目录：%s' % old)

    print('\n恢复完成。请启动后端并确认：登录、知识库文档列表、Excel 预览、历史卡片均正常。')
    print('如需回退本次恢复：用 %s 里的快照按同样的方式覆盖回去。' % pre_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
