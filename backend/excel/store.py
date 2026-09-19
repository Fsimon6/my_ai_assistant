# -*- coding: utf-8 -*-
"""Excel 原始文件 + Unified Representation 的本地持久化（Phase 1A）

存储布局（沿用项目现有的本地文件存储方式，不引入对象存储）：

    <project_root>/data/excel/<document_id>/
        original.<ext>          # 用户上传的原始表格文件（保留，供后续阶段复用）
        representation.json     # Unified Table Representation
        meta.json               # 轻量元信息（document_id/user_id/filename/...）

设计要点：
- 以 document_id 作为唯一目录名；representation 内部记录 user_id，
  读取时校验 user_id 匹配，实现用户隔离（不匹配一律视为不存在）。
- Preview 必须读取该 representation.json，禁止重新解析原始文件。
"""

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.excel.representation import WorkbookRepresentation

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXCEL_DATA_ROOT = PROJECT_ROOT / 'data' / 'excel'


def _doc_dir(document_id: str) -> Path:
    return EXCEL_DATA_ROOT / document_id


def save_artifacts(
    representation: WorkbookRepresentation,
    source_path: str,
    original_filename: str,
) -> Dict[str, str]:
    """保存原始文件 + representation.json + meta.json，返回落盘路径。"""
    doc_dir = _doc_dir(representation.document_id)
    doc_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(original_filename or source_path).suffix.lower() or f'.{representation.file_type}'
    original_path = doc_dir / f'original{ext}'
    try:
        shutil.copyfile(source_path, original_path)
    except Exception as e:
        logger.error('保存 Excel 原始文件失败：%s', e)
        raise

    representation_path = doc_dir / 'representation.json'
    with open(representation_path, 'w', encoding='utf-8') as f:
        json.dump(representation.to_dict(), f, ensure_ascii=False, indent=2)

    meta = {
        'document_id': representation.document_id,
        'user_id': representation.user_id,
        'filename': representation.filename,
        'file_type': representation.file_type,
        'parser': representation.parser,
        'sheet_count': representation.sheet_count,
        'total_rows': representation.total_rows,
        'original_path': str(original_path),
        'representation_path': str(representation_path),
        'created_at': representation.created_at,
    }
    meta_path = doc_dir / 'meta.json'
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    logger.info('Excel 附件已保存：%s', doc_dir)
    return {
        'dir': str(doc_dir),
        'original_path': str(original_path),
        'representation_path': str(representation_path),
        'meta_path': str(meta_path),
    }


def load_representation(document_id: str, user_id: Optional[int] = None) -> Optional[WorkbookRepresentation]:
    """读取 representation；若提供 user_id 则强制校验归属（不匹配返回 None）。"""
    rep_path = _doc_dir(document_id) / 'representation.json'
    if not rep_path.exists():
        return None
    try:
        with open(rep_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        logger.error('读取 representation 失败 %s：%s', rep_path, e)
        return None

    rep = WorkbookRepresentation.from_dict(data)
    if user_id is not None and rep.user_id != user_id:
        logger.warning('representation 归属校验失败：document_id=%s', document_id)
        return None
    # 稳定性补丁：旧版 representation.json 没有 semantic_type，读取时按现有值补齐
    # （只补空缺，不覆盖解析期已有结论；失败不影响读取）
    try:
        for sheet in rep.sheets:
            sheet.ensure_semantic_types()
    except Exception as e:  # noqa: BLE001 - 语义判定失败不应导致文件不可读
        logger.warning('补齐业务语义类型失败（忽略）：%s', e)
    return rep


def load_meta(document_id: str) -> Optional[Dict[str, Any]]:
    meta_path = _doc_dir(document_id) / 'meta.json'
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def artifacts_exist(document_id: str) -> bool:
    return (_doc_dir(document_id) / 'representation.json').exists()


def delete_artifacts(document_id: str) -> bool:
    """删除该文档的全部 Excel 附件目录（幂等）。"""
    doc_dir = _doc_dir(document_id)
    if not doc_dir.exists():
        return False
    try:
        shutil.rmtree(doc_dir, ignore_errors=True)
        logger.info('Excel 附件已删除：%s', doc_dir)
        return True
    except Exception as e:
        logger.error('删除 Excel 附件失败 %s：%s', doc_dir, e)
        return False


def list_excel_catalog(user_id: Optional[int] = None, max_docs: int = 10) -> List[Dict[str, Any]]:
    """列出某用户可查询的 Excel 表格目录（**仅 schema，不含任何数据行**）。

    用途：Phase 1C 的 NL 层需要知道「有哪些文件 / Sheet / 列」才能把自然语言校验成参数。
    实现：扫描 data/excel/<document_id>/meta.json（纯文件系统），**不依赖 Chroma**。
    用户隔离：meta.user_id 必须等于传入 user_id。
    """
    if not EXCEL_DATA_ROOT.exists():
        return []
    docs: List[Dict[str, Any]] = []
    try:
        entries = list(EXCEL_DATA_ROOT.iterdir())
    except OSError:
        return []

    for doc_dir in entries:
        if not doc_dir.is_dir():
            continue
        meta = load_meta(doc_dir.name)
        if not meta:
            continue
        if user_id is not None and meta.get('user_id') != user_id:
            continue
        rep_path = doc_dir / 'representation.json'
        if not rep_path.exists():
            continue
        try:
            with open(rep_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            logger.warning('读取 representation 失败（catalog）：%s %s', rep_path, e)
            continue

        sheets: List[Dict[str, Any]] = []
        for sh in data.get('sheets', []):
            sheets.append({
                'sheet_name': sh.get('sheet_name', ''),
                'sheet_index': sh.get('sheet_index', 0),
                'row_count': sh.get('row_count', 0),
                'column_count': sh.get('column_count', 0),
                'columns': [c.get('name') for c in sh.get('columns', [])],
            })

        docs.append({
            'document_id': doc_dir.name,
            'filename': meta.get('filename') or data.get('filename') or doc_dir.name,
            'file_type': meta.get('file_type') or data.get('file_type') or '',
            'created_at': meta.get('created_at') or data.get('created_at') or '',
            'total_rows': meta.get('total_rows', 0),
            'sheets': sheets,
        })

    docs.sort(key=lambda d: d.get('created_at') or '', reverse=True)
    return docs[:max_docs]
