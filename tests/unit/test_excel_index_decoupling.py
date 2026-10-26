# -*- coding: utf-8 -*-
"""Excel 本地持久化 / 语义索引解耦的单元回归（合成数据，不访问真实服务）。"""
import asyncio
import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from backend.excel import store as excel_store
from backend.excel.representation import WorkbookRepresentation
from backend.services import rag_service as rag_mod
from backend.services.rag_service import RagService


def _make_xlsx(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = 'Data'
    ws.append(['name', 'value'])
    ws.append(['alpha', 1])
    wb.save(path)


class FakeVectorStore:
    def __init__(self, fail_add=False, fail_cleanup=False):
        self.fail_add = fail_add
        self.fail_cleanup = fail_cleanup
        self.added = []
        self.deleted = []

    async def add_documents(self, documents):
        self.added.extend(documents)
        if self.fail_add:
            raise RuntimeError('remote embedding exploded')
        return [d['id'] for d in documents]

    async def delete_documents(self, document_ids, user_id=None, strict=False):
        self.deleted.append((list(document_ids), user_id, strict))
        if self.fail_cleanup:
            raise RuntimeError('vector cleanup failed')
        return len(document_ids)


def _service(vector_store) -> RagService:
    svc = RagService.__new__(RagService)
    svc.vector_store = vector_store
    svc.cache = None

    async def noop(*args, **kwargs):
        return None

    svc._clear_related_cache = noop
    return svc


def _run(svc, path: Path, user_id=7):
    return asyncio.run(svc.process_and_store_document(
        file_path=str(path),
        user_id=user_id,
        original_filename=path.name,
        file_size=path.stat().st_size,
    ))


def test_constructor_does_not_initialize_llm_or_vector(monkeypatch):
    """RagService() 本身不能因缺少 API Key/Chroma 配置失败。"""
    def fail():
        raise RuntimeError('external initialization must be lazy')

    monkeypatch.setattr(rag_mod, 'get_llm', fail)
    monkeypatch.setattr(rag_mod, 'get_vector_store_manager', fail)

    svc = RagService()
    assert svc._llm is None
    assert svc._vector_store is None


@pytest.mark.parametrize('external_error', [
    RuntimeError('Chroma unavailable'),
    ValueError('provider=openai 需要设置 API_KEY'),
])
def test_vector_initialization_failure_keeps_local_excel(tmp_path, monkeypatch, external_error):
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path / 'excel')
    xlsx = tmp_path / 'local.xlsx'
    _make_xlsx(xlsx)

    def fail():
        raise external_error

    monkeypatch.setattr(rag_mod, 'get_vector_store_manager', fail)
    svc = RagService()
    out = _run(svc, xlsx)

    assert out['success'] is True
    assert out['index_status'] == 'index_failed'
    assert out['partial'] is True
    assert (tmp_path / 'excel' / out['document_id'] / 'representation.json').exists()
    meta = excel_store.load_meta(out['document_id'])
    assert meta['index_status'] == 'index_failed'
    assert meta['vector_write_started'] is False


def test_embedding_failure_keeps_local_excel_and_records_status(tmp_path, monkeypatch):
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path / 'excel')
    xlsx = tmp_path / 'embedding-fail.xlsx'
    _make_xlsx(xlsx)
    vs = FakeVectorStore(fail_add=True)
    svc = _service(vs)

    out = _run(svc, xlsx)

    assert out['success'] is True
    assert out['index_status'] == 'index_failed'
    assert out['partial'] is True
    assert out['total_chunks'] == 0
    assert (tmp_path / 'excel' / out['document_id'] / 'representation.json').exists()
    assert not xlsx.exists()
    meta = excel_store.load_meta(out['document_id'])
    assert meta['index_status'] == 'index_failed'
    assert meta['vector_write_started'] is False
    assert vs.deleted and vs.deleted[0][2] is True


def test_partial_vector_write_cleanup_failure_keeps_write_started_true(tmp_path, monkeypatch):
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path / 'excel')
    xlsx = tmp_path / 'partial.xlsx'
    _make_xlsx(xlsx)
    vs = FakeVectorStore(fail_add=True, fail_cleanup=True)
    svc = _service(vs)

    out = _run(svc, xlsx)

    assert out['success'] is True
    assert out['index_status'] == 'index_failed'
    meta = excel_store.load_meta(out['document_id'])
    assert meta['index_status'] == 'index_failed'
    assert meta['vector_write_started'] is True
    assert (tmp_path / 'excel' / out['document_id'] / 'representation.json').exists()


def test_successful_index_writes_status_and_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path / 'excel')
    xlsx = tmp_path / 'success.xlsx'
    _make_xlsx(xlsx)
    vs = FakeVectorStore()
    svc = _service(vs)

    out = _run(svc, xlsx)

    assert out['success'] is True
    assert out['index_status'] == 'indexed'
    assert out['partial'] is False
    assert out['total_chunks'] == 1
    assert len(vs.added) == 1
    meta = excel_store.load_meta(out['document_id'])
    assert meta['index_status'] == 'indexed'
    assert meta['vector_write_started'] is True
    assert meta['chunks'] == 1


def test_catalog_returns_unknown_for_legacy_meta_and_can_return_all(tmp_path, monkeypatch):
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path / 'excel')
    source = tmp_path / 'source.xlsx'
    source.write_bytes(b'synthetic')
    for i in range(11):
        rep = WorkbookRepresentation(
            schema_version='1.0',
            document_id=f'legacy-{i}',
            user_id=7,
            filename=f'legacy-{i}.xlsx',
            file_type='xlsx',
            parser='test',
            sheet_count=0,
            sheets=[],
            created_at=f'2026-10-26T00:00:{i:02d}',
        )
        excel_store.save_artifacts(rep, str(source), rep.filename, file_size=8)
        meta_path = tmp_path / 'excel' / rep.document_id / 'meta.json'
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        for key in (
            'index_status', 'index_error_code', 'index_error', 'indexed_at',
            'file_size', 'chunks', 'updated_at', 'vector_write_started',
        ):
            meta.pop(key, None)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')

    docs = excel_store.list_excel_catalog(user_id=7, max_docs=None)
    assert len(docs) == 11
    assert all(d['index_status'] == 'unknown' for d in docs)
    assert all(d['vector_write_started'] is None for d in docs)

    limited = excel_store.list_excel_catalog(user_id=7, max_docs=10)
    assert len(limited) == 10
