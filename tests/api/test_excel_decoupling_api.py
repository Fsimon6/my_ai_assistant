# -*- coding: utf-8 -*-
"""Excel 本地成功 / 语义索引失败解耦的 API 回归（合成数据 + fake provider）。"""
import io

from openpyxl import Workbook

from backend.excel import store as excel_store
from backend.services import vector_service as vec_mod

from .conftest import XLSX_MIME


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = 'Data'
    ws.append(['name', 'value'])
    ws.append(['alpha', 1])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(api, headers):
    return api.upload_bytes(headers, 'decoupled.xlsx', _xlsx_bytes(), XLSX_MIME)


def test_index_failed_excel_is_listed_and_queryable(api):
    headers = api.user('decoupled_index_user')
    sensitive_detail = 'SENSITIVE_PROVIDER_DETAIL_remote_embedding_exploded'
    api.fake.embedding_error = RuntimeError(sensitive_detail)

    resp = _upload(api, headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['success'] is True
    assert body['doc_kind'] == 'excel'
    assert body['local_status'] == 'ready'
    assert body['index_status'] == 'index_failed'
    assert body['partial'] is True

    doc_id = body['document_id']
    listed = api.client.get('/api/v1/rag/documents', headers=headers)
    assert listed.status_code == 200, listed.text
    assert sensitive_detail not in listed.text
    matches = [d for d in listed.json()['documents'] if d.get('document_id') == doc_id]
    assert len(matches) == 1
    assert matches[0]['kind'] == 'excel'
    assert matches[0]['type'] == 'xlsx'
    assert matches[0]['sheet_count'] == 1
    assert matches[0]['index_status'] == 'index_failed'
    assert matches[0]['index_error_code']
    assert 'index_error' not in matches[0]
    assert matches[0]['local_available'] is True

    query = api.client.post(
        '/api/v1/rag/excel/%s/query' % doc_id,
        headers=headers,
        json={'sheet_index': 0},
    )
    assert query.status_code == 200, query.text
    assert query.json()['total_matches'] == 1

    meta = excel_store.load_meta(doc_id)
    assert meta['index_status'] == 'index_failed'
    assert meta['vector_write_started'] is False
    # 内部诊断详情仍保存在 meta.json，只是不再进入 /documents 响应。
    assert sensitive_detail in meta['index_error']


def test_list_keeps_local_excel_when_chroma_unavailable(api, monkeypatch):
    headers = api.user('decoupled_list_user')
    body = _upload(api, headers).json()
    doc_id = body['document_id']

    def fail():
        raise RuntimeError('Chroma unavailable')

    monkeypatch.setattr(vec_mod, 'get_vector_store_manager', fail)

    resp = api.client.get('/api/v1/rag/documents', headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['partial'] is True
    assert data['warnings']
    matches = [d for d in data['documents'] if d.get('document_id') == doc_id]
    assert len(matches) == 1
    assert matches[0]['index_status'] == 'indexed'
    assert 'index_error' not in matches[0]
    assert matches[0]['local_available'] is True


def test_delete_index_failed_local_excel(api):
    headers = api.user('decoupled_delete_local_user')
    api.fake.embedding_error = RuntimeError('remote embedding exploded')
    body = _upload(api, headers).json()
    doc_id = body['document_id']
    assert body['index_status'] == 'index_failed'

    resp = api.client.request(
        'DELETE',
        '/api/v1/rag/documents',
        headers=headers,
        json={'document_ids': [doc_id]},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result['success'] is True
    assert result['partial'] is False
    assert result['deleted_count'] == 0
    assert result['deleted_artifacts'] == 1

    listed = api.client.get('/api/v1/rag/documents', headers=headers).json()
    assert doc_id not in [d.get('document_id') for d in listed['documents']]
    assert api.client.get('/api/v1/rag/excel/%s/preview' % doc_id,
                          headers=headers).status_code == 404


def test_vector_delete_success_zero_still_deletes_local(api):
    """严格删除成功返回 0 条时，是“确认无向量”，不是删除失败。"""
    headers = api.user('decoupled_delete_zero_user')
    # 先制造 index_failed 的本地文档；再把状态改为“曾开始写入”，强制走严格删除。
    api.fake.embedding_error = RuntimeError('remote embedding exploded')
    body = _upload(api, headers).json()
    doc_id = body['document_id']
    excel_store.update_meta(doc_id, vector_write_started=True)

    resp = api.client.request(
        'DELETE',
        '/api/v1/rag/documents',
        headers=headers,
        json={'document_ids': [doc_id]},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result['success'] is True
    assert result['partial'] is False
    assert result['deleted_count'] == 0
    assert result['deleted_artifacts'] == 1
    assert result['vector_delete_confirmed'] is True


def test_vector_delete_failure_keeps_local_excel_and_is_partial(api, monkeypatch):
    headers = api.user('decoupled_delete_vector_fail_user')
    body = _upload(api, headers).json()
    doc_id = body['document_id']
    assert body['index_status'] == 'indexed'

    manager = vec_mod._vector_store_manager
    assert manager is not None

    async def fail_delete(document_ids, user_id=None, strict=False):
        raise RuntimeError('vector delete unavailable')

    monkeypatch.setattr(manager, 'delete_documents', fail_delete)

    resp = api.client.request(
        'DELETE',
        '/api/v1/rag/documents',
        headers=headers,
        json={'document_ids': [doc_id]},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result['success'] is False
    assert result['partial'] is True
    assert result['vector_delete_confirmed'] is False
    assert result['deleted_artifacts'] == 0
    assert doc_id in result['failed_document_ids']

    # 本地文件与精确查询仍可用；列表可能仍显示该文档（向量未被确认删除）。
    assert excel_store.load_representation(doc_id) is not None
    assert api.client.get('/api/v1/rag/excel/%s/preview' % doc_id,
                          headers=headers).status_code == 200


def test_index_failed_excel_is_isolated_by_owner(api):
    user_a = api.user('decoupled_owner_a')
    api.fake.embedding_error = RuntimeError('remote embedding exploded')
    body = _upload(api, user_a).json()
    doc_id = body['document_id']

    # 重新注册一个独立用户，确认本地 catalog 过滤 user_id。
    user_b = api.user('decoupled_owner_b')
    listed_b = api.client.get('/api/v1/rag/documents', headers=user_b).json()
    assert doc_id not in [d.get('document_id') for d in listed_b['documents']]
    assert api.client.get('/api/v1/rag/excel/%s/preview' % doc_id,
                          headers=user_b).status_code == 404
