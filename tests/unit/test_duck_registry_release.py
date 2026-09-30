# -*- coding: utf-8 -*-
"""Stage 4：删除文档时**精确**释放 DuckDB 内存资源（registry 生命周期）。

背景（只读审计结论）：``duck.DuckTableRegistry`` 以 ``document_id`` 为 key 缓存
``_DocDatabase``（LRU=8，进程内内存库）；文档删除路径只清理了磁盘 artifacts 与向量，
**从未**释放内存库 —— 已删除文档会一直占内存直到 LRU 淘汰。

本轮实现：``DuckTableRegistry.remove_document(document_id)``（精确、幂等、带文档锁）
+ ``duck.release_document()`` 便捷入口 + 删除端点接入。

安全/语义护栏（本文件断言）：
  * 删除 A **不得**影响 B/C（绝不是"全量 reset"偷懒实现）；
  * 只对**归属当前用户**的文档释放（非本人文档一律不动）；
  * 删除是幂等的；未缓存文档删除不报错；
  * 删除后重新上传（新 document_id）得到**新的**内存库，绝不复用旧表；
  * LRU=8 行为不变；
  * 查询 / 枚举 / SQL / snapshot 语义完全未变。
"""
import asyncio
import dataclasses
import glob
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from backend.excel import duck as duck_engine
from backend.excel import engine as query_engine
from backend.excel import query as excel_query
from backend.excel import store as excel_store
from backend.excel.representation import (ColumnMeta, SheetRepresentation,
                                          WorkbookRepresentation)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 夹具：合成文档（快、无磁盘依赖）+ 真实 19 行文档（端点级验证用）
# --------------------------------------------------------------------------
def _synth_rep(document_id: str, rows: int = 20, user_id: int = 7) -> WorkbookRepresentation:
    names = ['Order ID', 'SKU ID', 'Order Amount']
    cols = [ColumnMeta(name=n, index=i, excel_column=i + 1,
                       excel_column_letter=chr(65 + i), dtype='string', non_empty=rows)
            for i, n in enumerate(names)]
    body = [['ID%03d' % i, 'SKU%02d' % (i % 4), round(10 + i * 1.5, 2)] for i in range(rows)]
    sheet = SheetRepresentation(sheet_name='S1', sheet_index=0, header_mode='single',
                               columns=cols, rows=body,
                               row_excel_numbers=list(range(3, 3 + rows)),
                               row_count=rows, column_count=len(names))
    return WorkbookRepresentation(schema_version='1.0', document_id=document_id, user_id=user_id,
                                  filename='%s.xlsx' % document_id, file_type='xlsx',
                                  parser='openpyxl', sheet_count=1, sheets=[sheet],
                                  created_at='2026-09-30T00:00:00')


def _real_small_rep() -> Optional[WorkbookRepresentation]:
    for path in sorted(glob.glob(str(PROJECT_ROOT / 'data' / 'excel' / '*'
                                      / 'representation.json'))):
        try:
            rep = excel_store.load_representation(os.path.basename(os.path.dirname(path)))
        except Exception:  # noqa: BLE001
            continue
        if rep is not None and 10 <= rep.total_rows <= 30:
            return rep
    return None


@pytest.fixture(autouse=True)
def _clean_registry():
    duck_engine.reset_registry()
    yield
    duck_engine.reset_registry()


def _ingest(rep: WorkbookRepresentation) -> None:
    """物化该文档的表（等价于一次真实查询会做的事）。"""
    duck_engine.get_registry().get(rep, 0)


def _registry_ids() -> List[str]:
    return list(duck_engine.get_registry()._dbs.keys())


def _query_rows(rep: WorkbookRepresentation, limit: int = 50) -> List[List[Any]]:
    res, _engine = query_engine.run_structured_query(
        rep, {'sheet_index': 0, 'limit': limit, 'offset': 0}, engine='duckdb')
    return res.to_dict()['rows']


# ==========================================================================
# A：缓存 A -> 删除 A -> A 不在 registry
# ==========================================================================
def test_a_removed_document_leaves_registry():
    a = _synth_rep('doc-a')
    _ingest(a)
    assert 'doc-a' in _registry_ids()
    assert duck_engine.release_document('doc-a') is True
    assert 'doc-a' not in _registry_ids()
    assert duck_engine.get_registry().stats()['documents'] == 0


# ==========================================================================
# B：缓存 A + B -> 删除 A -> B 仍可正常查询（绝不牵连）
# ==========================================================================
def test_b_deleting_a_keeps_b_usable():
    a, b = _synth_rep('doc-a'), _synth_rep('doc-b', rows=25)
    _ingest(a)
    _ingest(b)
    assert sorted(_registry_ids()) == ['doc-a', 'doc-b']
    rows_b_before = _query_rows(b)

    assert duck_engine.release_document('doc-a') is True
    assert _registry_ids() == ['doc-b']                       # 只少了一个
    assert duck_engine.get_registry().stats()['documents'] == 1
    assert _query_rows(b) == rows_b_before                    # B 仍可查询，结果不变
    assert duck_engine.get_registry().stats()['sheets'] == 1


def test_b2_deleting_middle_document_keeps_others():
    reps = [_synth_rep('doc-%d' % i) for i in range(3)]
    for rep in reps:
        _ingest(rep)
    assert duck_engine.release_document('doc-1') is True
    assert sorted(_registry_ids()) == ['doc-0', 'doc-2']
    assert _query_rows(reps[0]) and _query_rows(reps[2])       # 首尾都还能查询


# ==========================================================================
# C / D：未缓存不报错 + 重复删除幂等
# ==========================================================================
def test_c_removing_unknown_document_is_quiet():
    assert duck_engine.release_document('not-cached') is False   # 不抛异常
    assert duck_engine.get_registry().stats()['documents'] == 0


def test_d_remove_is_idempotent():
    a = _synth_rep('doc-a')
    _ingest(a)
    assert duck_engine.release_document('doc-a') is True
    assert duck_engine.release_document('doc-a') is False
    assert duck_engine.release_document('doc-a') is False
    assert 'doc-a' not in _registry_ids()


# ==========================================================================
# E / F：删除后重新上传得到新资源；旧 document_id 不可能复用旧内存库
# ==========================================================================
def test_e_reupload_same_file_gets_fresh_resource():
    old = _synth_rep('doc-old')
    _ingest(old)
    old_db = duck_engine.get_registry()._db_for('doc-old')
    assert duck_engine.release_document('doc-old') is True

    # 重新上传：真实链路每次都是新 UUID（本测试用新 id 模拟）
    new = _synth_rep('doc-new', rows=old.sheets[0].row_count)
    _ingest(new)
    new_db = duck_engine.get_registry()._db_for('doc-new')
    assert new_db is not old_db                     # 全新的内存库，不是被关闭的旧库
    assert 'doc-old' not in _registry_ids()
    assert len(_query_rows(new)) == old.sheets[0].row_count


def test_f_old_document_id_cannot_reuse_released_registry():
    old = _synth_rep('doc-old')
    _ingest(old)
    assert duck_engine.release_document('doc-old') is True
    # 旧 id 的资源已释放：再次询问 registry 不会拿到旧库（会被 LRU/键控重新物化）
    assert 'doc-old' not in _registry_ids()
    assert duck_engine.release_document('doc-old') is False


# ==========================================================================
# G：LRU=8 行为不变；删除不打乱其余文档
# ==========================================================================
def test_g_lru_unchanged_after_removal():
    assert duck_engine.MAX_CACHED_DOCUMENTS == 8
    reps = [_synth_rep('lru-%d' % i) for i in range(9)]
    for rep in reps:                                  # 第 9 个触发 LRU 淘汰最老的
        _ingest(rep)
    ids = _registry_ids()
    assert len(ids) == 8
    assert 'lru-0' not in ids                         # 最老的被淘汰（既有行为）
    assert duck_engine.get_registry().stats()['documents'] == 8
    assert duck_engine.release_document('lru-4') is True
    assert len(_registry_ids()) == 7
    assert 'lru-8' in _registry_ids()                 # 其余文档仍在
    assert _query_rows(reps[8])


# ==========================================================================
# H：并发/锁边界 —— 关闭前必须取得该文档自己的锁（不会在 execute 中途关闭）
# ==========================================================================
def test_h_remove_waits_for_document_lock():
    a = _synth_rep('doc-a')
    _ingest(a)
    db = duck_engine.get_registry()._db_for('doc-a')

    done = threading.Event()
    result: List[bool] = []

    def worker():
        result.append(duck_engine.release_document('doc-a'))
        done.set()

    db.lock.acquire()                                 # 模拟"查询正在使用该库"
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        assert not done.wait(0.3), '必须在文档锁释放前阻塞（不能在 execute 中途关闭连接）'
    finally:
        db.lock.release()
    assert done.wait(2.0)
    t.join(timeout=2.0)
    assert result == [True]
    assert 'doc-a' not in _registry_ids()


# ==========================================================================
# 端点级：DELETE /rag/documents 真实调用链（磁盘 artifacts + 内存 registry）
# ==========================================================================
class _FakeVectorStore:
    def __init__(self):
        self.deleted: List[Any] = []

    async def delete_documents(self, document_ids, user_id=None):
        self.deleted.append((list(document_ids), user_id))
        return len(document_ids)


class _User:
    def __init__(self, uid: int):
        self.id = uid


def _prepare_doc_dir(tmp_root: Path, rep: WorkbookRepresentation) -> WorkbookRepresentation:
    """把 representation 落到临时 EXCEL_DATA_ROOT（含原文件 + meta.json）。"""
    src = PROJECT_ROOT / '直邮一店 8.20号订单.xlsx'
    if not src.exists():
        pytest.skip('缺少真实测试表格')
    excel_store.save_artifacts(rep, str(src), src.name)
    return rep


def _call_delete(monkeypatch, ids: List[str], uid: int) -> Dict[str, Any]:
    from backend.api.v1 import rag as rag_api
    from backend.services import vector_service

    fake = _FakeVectorStore()
    monkeypatch.setattr(vector_service, 'get_vector_store_manager', lambda: fake)
    req = rag_api.DeleteDocumentsRequest(document_ids=list(ids))
    return asyncio.run(rag_api.delete_documents(req, current_user=_User(uid)))


def test_endpoint_releases_registry_and_keeps_other_documents(tmp_path, monkeypatch):
    real = _real_small_rep()
    if real is None:
        pytest.skip('缺少可用真实 representation')
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path)
    doc_a = dataclasses.replace(real, document_id='api-doc-a', user_id=7)
    doc_b = dataclasses.replace(real, document_id='api-doc-b', user_id=7)
    _prepare_doc_dir(tmp_path, doc_a)
    _prepare_doc_dir(tmp_path, doc_b)
    _ingest(doc_a)
    _ingest(doc_b)
    assert sorted(_registry_ids()) == ['api-doc-a', 'api-doc-b']

    out = _call_delete(monkeypatch, ['api-doc-a'], 7)
    assert out['success'] is True
    assert out['deleted_artifacts'] == 1
    assert out['released_duckdb_resources'] == 1
    assert 'api-doc-a' not in _registry_ids()             # 精确释放
    assert not (tmp_path / 'api-doc-a').exists()          # artifacts 已删除
    assert 'api-doc-b' in _registry_ids()                 # B 完好
    assert _query_rows(doc_b)                             # B 仍可查询
    assert (tmp_path / 'api-doc-b').exists()


def test_endpoint_does_not_release_other_users_document(tmp_path, monkeypatch):
    real = _real_small_rep()
    if real is None:
        pytest.skip('缺少可用真实 representation')
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path)
    doc = dataclasses.replace(real, document_id='api-doc-own', user_id=7)
    _prepare_doc_dir(tmp_path, doc)
    _ingest(doc)

    out = _call_delete(monkeypatch, ['api-doc-own'], 999)   # 另一个用户
    assert out['deleted_artifacts'] == 0
    assert out['released_duckdb_resources'] == 0            # 归属校验拦住，绝不释放别人资源
    assert 'api-doc-own' in _registry_ids()
    assert (tmp_path / 'api-doc-own').exists()


def test_endpoint_delete_is_idempotent(tmp_path, monkeypatch):
    real = _real_small_rep()
    if real is None:
        pytest.skip('缺少可用真实 representation')
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path)
    doc = dataclasses.replace(real, document_id='api-doc-i', user_id=7)
    _prepare_doc_dir(tmp_path, doc)
    _ingest(doc)

    first = _call_delete(monkeypatch, ['api-doc-i'], 7)
    assert (first['deleted_artifacts'], first['released_duckdb_resources']) == (1, 1)
    second = _call_delete(monkeypatch, ['api-doc-i'], 7)
    assert (second['deleted_artifacts'], second['released_duckdb_resources']) == (0, 0)
    assert second['success'] is True
    assert 'api-doc-i' not in _registry_ids()


def test_endpoint_release_failure_is_logged_not_hidden(tmp_path, monkeypatch, caplog):
    """DuckDB 释放失败：删除仍成功返回，但必须留下带 document_id 的告警日志。"""
    real = _real_small_rep()
    if real is None:
        pytest.skip('缺少可用真实 representation')
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path)
    doc = dataclasses.replace(real, document_id='api-doc-err', user_id=7)
    _prepare_doc_dir(tmp_path, doc)
    _ingest(doc)

    def boom(document_id):
        raise RuntimeError('registry exploded')
    monkeypatch.setattr(duck_engine, 'release_document', boom)
    with caplog.at_level('WARNING'):
        out = _call_delete(monkeypatch, ['api-doc-err'], 7)
    assert out['success'] is True
    assert out['deleted_artifacts'] == 1                    # 业务删除结果不被隐藏
    assert out['released_duckdb_resources'] == 0
    assert any('api-doc-err' in r.message and '释放 DuckDB 文档资源失败' in r.message
               for r in caplog.records)


def test_nl_query_after_delete_cannot_reach_document(tmp_path, monkeypatch):
    """删除后（artifacts + registry 都没了）NL 查询按"文档不存在"语义澄清，且不重建资源。"""
    from backend.excel import nl_query as nl
    real = _real_small_rep()
    if real is None:
        pytest.skip('缺少可用真实 representation')
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path)
    doc = dataclasses.replace(real, document_id='api-doc-gone', user_id=7)
    _prepare_doc_dir(tmp_path, doc)
    _ingest(doc)
    assert _call_delete(monkeypatch, ['api-doc-gone'], 7)['released_duckdb_resources'] == 1

    catalog = excel_store.list_excel_catalog(user_id=7)
    assert catalog == []                                    # 已不在目录中

    async def fake_turn(llm, msg, cat, context=None, analysis_context=None):
        return nl.TurnIntent(action=nl.ACTION_NEW_QUERY,
                             intent=nl.intent_from_dict({'query_type': nl.INTENT_STRUCTURED,
                                                         'columns': ['Order ID'],
                                                         'limit': 10}))
    monkeypatch.setattr(nl, 'llm_parse_turn', fake_turn)
    outcome = asyncio.run(nl.run_nl_query('列出订单号', catalog, llm=object(), user_id=7,
                                          session_key='after-delete'))
    assert outcome['status'] != nl.STATUS_OK
    assert _registry_ids() == []                            # 没有为已删除文档重建内存库
