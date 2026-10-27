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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.excel.representation import WorkbookRepresentation

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 与 rag_service / 前端展示保持一致；indexing 是旧版可能遗留的合法状态，
# 但列表读取时不再视为当前可确认的进行中状态。
VALID_INDEX_STATUSES = frozenset({
    'pending',
    'indexing',
    'indexed',
    'index_failed',
    'unknown',
})


def _resolve_excel_root() -> Path:
    """Excel 原件/解析产物根目录（F2 统一配置）。

    优先取 `settings.EXCEL_DATA_ROOT`（`.env` 可覆盖 → 测试/特殊部署可隔离）；
    留空则保持原行为 `<项目根>/data/excel`。

    说明：仍然导出为**模块级全局名** `EXCEL_DATA_ROOT`，因为现有测试通过
    `monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', ...)` 做隔离，必须保持兼容。
    """
    try:
        from backend.config.settings import settings
        configured = (getattr(settings, 'EXCEL_DATA_ROOT', '') or '').strip()
        if configured:
            return Path(configured)
    except Exception:  # noqa: BLE001 - 配置不可用时回退默认路径
        pass
    return PROJECT_ROOT / 'data' / 'excel'


EXCEL_DATA_ROOT = _resolve_excel_root()


def _doc_dir(document_id: str) -> Path:
    return EXCEL_DATA_ROOT / document_id


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """原子写 JSON：临时文件 + os.replace，避免 meta 半写。"""
    tmp_path = path.with_name(path.name + '.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def save_artifacts(
    representation: WorkbookRepresentation,
    source_path: str,
    original_filename: str,
    file_size: int = 0,
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

    now = datetime.now().isoformat()
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
        # 索引状态：本地 artifacts 已保存，但尚未开始向量写入。
        'index_status': 'pending',
        'index_error_code': '',
        'index_error': '',
        'indexed_at': '',
        'file_size': int(file_size or 0),
        'chunks': 0,
        'updated_at': now,
        # 只有真正准备调用 add_documents 前才允许置为 True。
        'vector_write_started': False,
    }
    meta_path = doc_dir / 'meta.json'
    _write_json_atomic(meta_path, meta)

    logger.info('Excel 附件已保存：%s', doc_dir)
    return {
        'dir': str(doc_dir),
        'original_path': str(original_path),
        'representation_path': str(representation_path),
        'meta_path': str(meta_path),
    }


def update_meta(document_id: str, **updates: Any) -> Dict[str, Any]:
    """原子更新 meta.json；不存在时抛出 FileNotFoundError。

    调用方必须自行决定更新失败时是否继续索引；本函数不会把失败伪装成功。
    """
    meta_path = _doc_dir(document_id) / 'meta.json'
    meta = load_meta(document_id)
    if meta is None:
        raise FileNotFoundError(f'meta.json 不存在：{document_id}')
    meta.update(updates)
    meta['updated_at'] = datetime.now().isoformat()
    _write_json_atomic(meta_path, meta)
    return meta


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
    """删除该文档的全部 Excel 附件目录（幂等），并确认目录确实消失。"""
    doc_dir = _doc_dir(document_id)
    if not doc_dir.exists():
        return False
    try:
        shutil.rmtree(doc_dir)
        if doc_dir.exists():
            logger.error('删除 Excel 附件后目录仍存在：%s', doc_dir)
            return False
        logger.info('Excel 附件已删除：%s', doc_dir)
        return True
    except Exception as e:
        logger.error('删除 Excel 附件失败 %s：%s', doc_dir, e)
        return False


def list_excel_catalog(
    user_id: Optional[int] = None,
    max_docs: Optional[int] = 10,
    include_status: bool = True,
    include_legacy_marker: bool = False,
) -> List[Dict[str, Any]]:
    """列出某用户可查询的 Excel 表格目录（**仅 schema，不含任何数据行**）。

    用途：
    - Phase 1C NL 层：默认最多 10 条，仅提供 schema；
    - 文档列表：传 max_docs=None 获取全部本地 Excel 文档。

    实现：扫描 data/excel/<document_id>/meta.json（纯文件系统），**不依赖 Chroma**。
    用户隔离：meta.user_id 必须等于传入 user_id；representation.user_id 存在时也必须一致。
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

        rep_user_id = data.get('user_id')
        if user_id is not None and rep_user_id is not None and rep_user_id != user_id:
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

        file_type = meta.get('file_type') or data.get('file_type') or ''
        doc = {
            'document_id': doc_dir.name,
            'filename': meta.get('filename') or data.get('filename') or doc_dir.name,
            'file_type': file_type,
            # 与 Chroma 文档列表字段保持一致，供前端类型/图标复用。
            'type': file_type,
            'kind': 'excel',
            'local_available': True,
            'size': int(meta.get('file_size') or 0),
            'chunks': int(meta.get('chunks') or 0),
            'created_at': meta.get('created_at') or data.get('created_at') or '',
            'total_rows': meta.get('total_rows', 0),
            'sheet_count': int(meta.get('sheet_count') or 0),
            'sheets': sheets,
        }
        if include_status:
            # 旧版本可能在最终状态写入失败后遗留 indexing；本地索引流程是同步的，
            # 列表读取时无法确认它是否仍在进行，故保守展示为 unknown，避免永久“索引中”。
            raw_index_status = meta.get('index_status')
            # 只有键完全不存在才是旧 meta；显式 null/空字符串仍属于“已记录但未确认”。
            legacy_missing = 'index_status' not in meta
            # 非法状态统一保守归为 unknown，但不因此触发旧版缺失兼容。
            if (isinstance(raw_index_status, str)
                    and raw_index_status in VALID_INDEX_STATUSES):
                index_status = raw_index_status
            else:
                index_status = 'unknown'
            if index_status == 'indexing':
                index_status = 'unknown'
            doc.update({
                # 旧 meta 没有状态：明确为 unknown，不能默认声称已索引。
                'index_status': index_status,
                'index_error_code': meta.get('index_error_code') or '',
                # index_error 仅保留在 meta.json 供内部诊断，禁止进入列表 API 响应。
                'indexed_at': meta.get('indexed_at') or '',
                # None 表示旧数据未知；False 才是本代码可靠记录的“从未开始写入”。
                'vector_write_started': meta.get('vector_write_started'),
            })
            # 只给 /documents 的内部合并逻辑使用；不是公开字段，调用方必须 pop。
            if include_legacy_marker and legacy_missing:
                doc['_legacy_index_status_missing'] = True
        docs.append(doc)

    docs.sort(key=lambda d: d.get('created_at') or '', reverse=True)
    return docs if max_docs is None else docs[:max_docs]
