# -*- coding: utf-8 -*-
"""Stage 5 API Regression — 归属隔离（§八）与 删除 / 重新上传（§九）。

只验证**现有** ownership contract，不修改任何鉴权/隔离实现：
  * 文档归属由 `excel_store.load_representation(document_id, user_id=...)` 与
    Chroma metadata 的 user_id 共同保证（非本人一律 404 / 0 删除）；
  * 删除走 `DELETE /api/v1/rag/documents` + `duck.release_document`（精确释放该文档内存表）。
"""
import pytest

from tests.helpers import fixtures as F
from .conftest import GOLDEN_SMALL

SKU = F.SKU


@pytest.fixture()
def two_users(api):
    a_headers, a_id = api.user_with_id('owner_a')
    b_headers, b_id = api.user_with_id('owner_b')
    return api, a_headers, a_id, b_headers, b_id


# ==========================================================================
# §八 归属隔离
# ==========================================================================
def test_other_user_cannot_preview_or_read_or_query(two_users):
    """User B 不能预览 / 不能读 chunks / 不能结构化查询 / 不能在文档列表看到。"""
    api, a_headers, _a_id, b_headers, _b_id = two_users
    doc_id = api.upload_excel(a_headers, GOLDEN_SMALL)['document_id']

    assert api.client.get('/api/v1/rag/excel/%s/preview' % doc_id,
                          headers=b_headers).status_code == 404
    assert api.client.get('/api/v1/rag/documents/%s' % doc_id,
                          headers=b_headers).status_code == 404
    assert api.client.post('/api/v1/rag/excel/%s/query' % doc_id, headers=b_headers,
                           json={'columns': [SKU], 'limit': 5}).status_code == 404
    assert api.client.post('/api/v1/rag/excel/%s/aggregate' % doc_id, headers=b_headers,
                           json={'operation': 'count'}).status_code == 404

    listed = api.client.get('/api/v1/rag/documents', headers=b_headers).json()
    assert [d['document_id'] for d in listed['documents']] == []

    # B 的自然语言查询也拿不到 A 的表（catalog 为空 -> 不 ok）
    api.fake.turn_payload = {'action': 'aggregate', 'aggregate_operation': 'count',
                             'column': None, 'group_by': [], 'filters': [],
                             'sheet': F.SMALL_SHEET}
    body = api.nl_query(b_headers, '有多少订单').json()
    assert body['status'] != 'ok'
    assert not body.get('aggregate')


def test_other_user_cannot_delete_owner_document(two_users):
    """User B 删除 A 的文档：deleted_count=0，且 A 的文档仍然可用。"""
    api, a_headers, _a_id, b_headers, _b_id = two_users
    doc_id = api.upload_excel(a_headers, GOLDEN_SMALL)['document_id']

    resp = api.client.request('DELETE', '/api/v1/rag/documents', headers=b_headers,
                              json={'document_ids': [doc_id]})
    assert resp.status_code == 200, resp.text
    assert resp.json()['deleted_count'] == 0

    # A 侧仍可预览（未被越权删除）
    assert api.client.get('/api/v1/rag/excel/%s/preview' % doc_id,
                          headers=a_headers).status_code == 200


def test_owner_can_read_and_query_own_document(two_users):
    """对照组：同一份文档，属主 A 的读取/查询全部成功。"""
    api, a_headers, _a_id, _b_headers, _b_id = two_users
    doc_id = api.upload_excel(a_headers, GOLDEN_SMALL)['document_id']

    assert api.client.get('/api/v1/rag/excel/%s/preview' % doc_id,
                          headers=a_headers).status_code == 200
    resp = api.client.post('/api/v1/rag/excel/%s/query' % doc_id, headers=a_headers,
                           json={'columns': [SKU], 'limit': 5})
    assert resp.status_code == 200, resp.text
    assert resp.json()['total_matches'] == F.SMALL_ROW_COUNT == 19


# ==========================================================================
# §九 删除 -> 重新上传
# ==========================================================================
def test_delete_then_reupload_same_fixture_works(api):
    """upload -> query -> delete -> confirm -> re-upload -> query again。

    验证：删除后旧 document_id 彻底不可用（含 DuckDB 内存资源被精确释放），
    重新上传得到**新的** document_id 并可正常查询。
    """
    headers = api.user('reupload_user')
    upload = api.upload_excel(headers, GOLDEN_SMALL)
    first_id = upload['document_id']

    # 1) 首次查询（走 DuckDB：会为该文档物化内存表）
    api.fake.turn_payload = {'action': 'aggregate', 'aggregate_operation': 'sum',
                             'column': F.AMOUNT, 'group_by': [], 'filters': [],
                             'sheet': F.SMALL_SHEET}
    body = api.nl_ok(headers, '订单金额总和是多少', session_id='reupload-1')
    assert round(float(body['aggregate']['value']), 2) == F.SMALL_TOTAL_SUM

    # 2) 删除（属主）
    deleted = api.client.request('DELETE', '/api/v1/rag/documents', headers=headers,
                                 json={'document_ids': [first_id]})
    assert deleted.status_code == 200, deleted.text
    info = deleted.json()
    # deleted_count = 实际删除的**向量条数**（本文件每 Sheet 一条摘要 chunk）
    assert info['deleted_count'] == upload['total_chunks'] == 2
    assert info['deleted_artifacts'] == 1                 # 原文件 + representation 被清理
    assert info['released_duckdb_resources'] == 1         # DuckDB 内存表被精确释放（按文档）

    # 3) 删除后：列表不含、预览 404、结构化查询 404
    listed = api.client.get('/api/v1/rag/documents', headers=headers).json()
    assert first_id not in [d['document_id'] for d in listed['documents']]
    assert api.client.get('/api/v1/rag/excel/%s/preview' % first_id,
                          headers=headers).status_code == 404
    assert api.client.post('/api/v1/rag/excel/%s/query' % first_id, headers=headers,
                           json={'columns': [SKU], 'limit': 5}).status_code == 404

    # 4) 重新上传同一 fixture -> 新 document_id
    second_id = api.upload_excel(headers, GOLDEN_SMALL)['document_id']
    assert second_id != first_id

    # 5) 再次查询仍然正确（没有复用旧状态 / 旧内存表）
    body2 = api.nl_ok(headers, '订单金额总和是多少', session_id='reupload-2')
    assert round(float(body2['aggregate']['value']), 2) == F.SMALL_TOTAL_SUM
    assert body2['aggregate']['matched_rows'] == 19
