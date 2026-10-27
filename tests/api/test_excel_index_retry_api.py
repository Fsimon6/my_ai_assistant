# -*- coding: utf-8 -*-
"""Excel 语义索引显式重试的 API 回归（隔离 Chroma + fake provider）。"""
import io
import json
from pathlib import Path

import pytest
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
    return api.upload_bytes(headers, 'retry.xlsx', _xlsx_bytes(), XLSX_MIME)


def _failed_doc(api, headers):
    api.fake.embedding_error = RuntimeError('initial embedding failure')
    resp = _upload(api, headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['index_status'] == 'index_failed'
    assert body['partial'] is True
    return body['document_id']


def _listed_doc(api, headers, doc_id):
    resp = api.client.get('/api/v1/rag/documents', headers=headers)
    assert resp.status_code == 200, resp.text
    matches = [d for d in resp.json()['documents'] if d.get('document_id') == doc_id]
    assert len(matches) == 1
    return resp, matches[0]


def _retry(api, headers, doc_id):
    return api.client.post(
        '/api/v1/rag/documents/%s/retry-index' % doc_id,
        headers=headers,
    )


def _upload_with_metadata(api, headers, *, form_value=None, query_value=None):
    data = {} if form_value is None else {'metadata': form_value}
    params = {} if query_value is None else {'metadata': query_value}
    return api.client.post(
        '/api/v1/rag/upload',
        headers=headers,
        params=params,
        data=data,
        files={'file': ('metadata.xlsx', _xlsx_bytes(), XLSX_MIME)},
    )


def test_retry_success_updates_status_and_can_retry(api):
    headers = api.user('retry_success_user')
    doc_id = _failed_doc(api, headers)

    listed, before = _listed_doc(api, headers, doc_id)
    assert before['can_retry_index'] is True
    assert 'vector_write_started' not in before or before['vector_write_started'] is False

    api.fake.embedding_error = None
    resp = _retry(api, headers, doc_id)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['success'] is True
    assert data['partial'] is False
    assert data['index_status'] == 'indexed'
    assert data['can_retry_index'] is False
    assert 'index_error' not in data

    _, after = _listed_doc(api, headers, doc_id)
    assert after['index_status'] == 'indexed'
    assert after['can_retry_index'] is False
    assert excel_store.load_meta(doc_id)['index_status'] == 'indexed'


@pytest.mark.parametrize(('status', 'vector_write_started'), [
    ('unknown', False),
    ('index_failed', True),
    ('index_failed', None),
    ('indexing', False),
    ('pending', False),
    ('indexed', False),
])
def test_retry_rejects_unsafe_states(api, status, vector_write_started):
    headers = api.user('retry_reject_%s_%s' % (status, vector_write_started))
    doc_id = _failed_doc(api, headers)
    api.fake.embedding_error = None
    excel_store.update_meta(
        doc_id,
        index_status=status,
        vector_write_started=vector_write_started,
    )
    before_requests = len(api.fake.embedding_requests)

    resp = _retry(api, headers, doc_id)

    assert resp.status_code == 409, resp.text
    detail = resp.json()['detail']
    assert detail['error_code'] in {
        'INDEX_RETRY_NOT_ALLOWED', 'INDEX_RETRY_NOT_SAFE'
    }
    assert len(api.fake.embedding_requests) == before_requests
    assert 'initial embedding failure' not in resp.text


def test_retry_rejects_missing_vector_write_marker(api):
    headers = api.user('retry_missing_marker_user')
    doc_id = _failed_doc(api, headers)
    api.fake.embedding_error = None
    meta_path = excel_store.EXCEL_DATA_ROOT / doc_id / 'meta.json'
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    meta.pop('vector_write_started', None)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    before_requests = len(api.fake.embedding_requests)

    resp = _retry(api, headers, doc_id)

    assert resp.status_code == 409, resp.text
    assert len(api.fake.embedding_requests) == before_requests


def test_retry_rejects_missing_original_without_embedding(api):
    headers = api.user('retry_missing_original_user')
    doc_id = _failed_doc(api, headers)
    api.fake.embedding_error = None
    original_path = Path(excel_store.load_meta(doc_id)['original_path'])
    original_path.unlink()
    before_requests = len(api.fake.embedding_requests)

    resp = _retry(api, headers, doc_id)

    assert resp.status_code == 409, resp.text
    assert len(api.fake.embedding_requests) == before_requests
    assert excel_store.load_representation(doc_id) is not None


def test_retry_rejects_normal_document_and_other_owner(api):
    owner_headers = api.user('retry_owner_user')
    doc_id = _failed_doc(api, owner_headers)
    api.fake.embedding_error = None

    other_headers = api.user('retry_other_user')
    other_resp = _retry(api, other_headers, doc_id)
    assert other_resp.status_code == 404, other_resp.text

    normal_headers = api.user('retry_normal_doc_user')
    normal = api.upload_bytes(normal_headers, 'note.txt', b'normal text', 'text/plain')
    assert normal.status_code == 200, normal.text
    normal_id = normal.json()['document_id']
    normal_resp = _retry(api, normal_headers, normal_id)
    assert normal_resp.status_code == 404, normal_resp.text


def test_retry_prestate_persistence_failure_does_not_add(api, monkeypatch):
    headers = api.user('retry_prestate_fail_user')
    doc_id = _failed_doc(api, headers)
    api.fake.embedding_error = None
    original_update_meta = excel_store.update_meta

    def fail_prestate(document_id, **updates):
        if updates.get('index_status') == 'unknown':
            raise OSError('simulated prestate meta failure')
        return original_update_meta(document_id, **updates)

    monkeypatch.setattr(excel_store, 'update_meta', fail_prestate)
    before_requests = len(api.fake.embedding_requests)

    resp = _retry(api, headers, doc_id)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['success'] is False
    assert data['partial'] is True
    assert data['index_status'] == 'unknown'
    # 前置状态未落盘时没有调用 add_documents，文档仍是安全的 index_failed/false。
    assert data['can_retry_index'] is True
    assert len(api.fake.embedding_requests) == before_requests
    assert excel_store.load_representation(doc_id) is not None
    assert excel_store.load_meta(doc_id)['index_status'] == 'index_failed'


def test_upload_metadata_supports_form_query_and_form_priority(api):
    headers = api.user('upload_metadata_sources_user')

    form_only = _upload_with_metadata(
        api, headers, form_value=json.dumps({'origin': 'form'})
    )
    assert form_only.status_code == 200, form_only.text
    form_id = form_only.json()['document_id']
    assert excel_store.load_meta(form_id)['custom_metadata'] == {'origin': 'form'}

    query_only = _upload_with_metadata(
        api, headers, query_value=json.dumps({'origin': 'query'})
    )
    assert query_only.status_code == 200, query_only.text
    query_id = query_only.json()['document_id']
    assert excel_store.load_meta(query_id)['custom_metadata'] == {'origin': 'query'}

    both = _upload_with_metadata(
        api,
        headers,
        form_value=json.dumps({'origin': 'form-wins'}),
        query_value=json.dumps({'origin': 'query-loses'}),
    )
    assert both.status_code == 200, both.text
    both_id = both.json()['document_id']
    assert excel_store.load_meta(both_id)['custom_metadata'] == {'origin': 'form-wins'}

    neither = _upload_with_metadata(api, headers)
    assert neither.status_code == 200, neither.text
    neither_id = neither.json()['document_id']
    assert excel_store.load_meta(neither_id)['custom_metadata'] == {}


def test_retry_preserves_custom_metadata_without_leaking(api):
    headers, user_id = api.user_with_id('retry_custom_metadata_user')
    api.fake.embedding_error = RuntimeError('initial metadata test failure')
    custom = {
        'retention_tag': 'KEEP-ME',
        'custom_note': 'custom',
        # 尝试覆盖系统字段，重试时仍必须保持真实归属。
        'document_id': 'evil-id',
        'user_id': 999999,
    }
    resp = api.client.post(
        '/api/v1/rag/upload',
        headers=headers,
        files={'file': ('metadata-retry.xlsx', _xlsx_bytes(), XLSX_MIME)},
        data={'metadata': json.dumps(custom)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['index_status'] == 'index_failed'
    doc_id = body['document_id']
    assert excel_store.load_meta(doc_id)['custom_metadata'] == custom

    api.fake.embedding_error = None
    retry = _retry(api, headers, doc_id)
    assert retry.status_code == 200, retry.text
    assert retry.json()['success'] is True

    manager = vec_mod._vector_store_manager
    data = manager.vector_store._collection.get(
        where={'document_id': doc_id},
        include=['metadatas'],
    )
    metas = data.get('metadatas') or []
    assert metas
    for meta in metas:
        assert meta.get('retention_tag') == 'KEEP-ME'
        assert meta.get('custom_note') == 'custom'
        assert meta.get('document_id') == doc_id
        assert meta.get('user_id') == user_id

    listed = api.client.get('/api/v1/rag/documents', headers=headers)
    assert listed.status_code == 200, listed.text
    match = [d for d in listed.json()['documents'] if d.get('document_id') == doc_id][0]
    assert 'custom_metadata' not in match
    assert 'retention_tag' not in listed.text
    assert 'KEEP-ME' not in listed.text


def test_retry_rejects_legacy_missing_custom_metadata(api):
    headers = api.user('retry_legacy_metadata_user')
    doc_id = _failed_doc(api, headers)
    api.fake.embedding_error = None
    meta_path = excel_store.EXCEL_DATA_ROOT / doc_id / 'meta.json'
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    meta.pop('custom_metadata', None)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    before_requests = len(api.fake.embedding_requests)

    resp = _retry(api, headers, doc_id)

    assert resp.status_code == 409, resp.text
    assert resp.json()['detail']['error_code'] == 'INDEX_RETRY_METADATA_MISSING'
    assert len(api.fake.embedding_requests) == before_requests
    assert 'custom_metadata' not in excel_store.load_meta(doc_id)


def test_retry_final_meta_failure_keeps_local_and_does_not_cleanup(api, monkeypatch):
    headers = api.user('retry_final_meta_fail_user')
    doc_id = _failed_doc(api, headers)
    api.fake.embedding_error = None
    original_update_meta = excel_store.update_meta

    def fail_indexed(document_id, **updates):
        if updates.get('index_status') == 'indexed':
            raise OSError('simulated final meta failure')
        return original_update_meta(document_id, **updates)

    manager = vec_mod._vector_store_manager
    deleted = []

    async def record_delete(document_ids, user_id=None, strict=False):
        deleted.append((list(document_ids), user_id, strict))
        return 0

    monkeypatch.setattr(manager, 'delete_documents', record_delete)
    monkeypatch.setattr(excel_store, 'update_meta', fail_indexed)

    resp = _retry(api, headers, doc_id)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['success'] is False
    assert data['partial'] is True
    assert data['index_status'] == 'unknown'
    assert data['can_retry_index'] is False
    assert deleted == []
    assert excel_store.load_representation(doc_id) is not None
    assert excel_store.load_meta(doc_id)['index_status'] == 'unknown'
    assert excel_store.load_meta(doc_id)['vector_write_started'] is True
