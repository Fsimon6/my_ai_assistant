# -*- coding: utf-8 -*-
"""Stage 5 API Regression — Excel 上传 / 预览（HTTP 完整链路）。

覆盖：
  * `POST /api/v1/rag/upload`（xlsx -> Unified Representation + Chroma 摘要 chunk）
  * `GET  /api/v1/rag/documents` / `GET /api/v1/rag/documents/{id}`
  * `GET  /api/v1/rag/excel/{id}/preview`
  * 鉴权（未登录 -> 401）与不支持类型（400）

数据只使用合成 Golden fixture（`tests/fixtures/excel/*.xlsx`），不使用任何真实业务文件。
"""
import pytest

from tests.helpers import fixtures as F
from .conftest import GOLDEN_MEDIUM, GOLDEN_SMALL


@pytest.fixture()
def headers(api):
    return api.user('upload_user')


# ==========================================================================
# 上传
# ==========================================================================
def test_upload_golden_small_parses_and_returns_document_id(api, headers):
    """Golden A（19 行 / 2 Sheet）：HTTP 成功 + document_id + 解析元数据 + 摘要 chunk。"""
    body = api.upload_excel(headers, GOLDEN_SMALL)

    assert body['document_id']                                   # 必须返回 document_id
    assert body['doc_kind'] == 'excel'
    assert body['filename'] == GOLDEN_SMALL.name
    assert body['sheet_count'] == F.SMALL_SHEET_COUNT == 2
    # 单 Sheet 行数（OrderSKUList=19 / SkuMaster=5）；工作簿合计 = 两者之和
    sheets = {s['sheet_name']: s['row_count'] for s in body['sheets']}
    assert sheets['OrderSKUList'] == F.SMALL_ROW_COUNT == 19
    assert sheets['SkuMaster'] == 5
    assert body['total_rows'] == sum(sheets.values()) == 24
    # Phase 1A 兼容层：每 Sheet 一条摘要 chunk
    assert body['total_chunks'] == F.SMALL_SHEET_COUNT
    assert len(body['chunk_ids']) == body['total_chunks']


def test_upload_golden_medium_parses_447_rows(api, headers):
    """Golden B（447 行 / 1 Sheet）。"""
    body = api.upload_excel(headers, GOLDEN_MEDIUM)

    assert body['total_rows'] == F.MEDIUM_ROW_COUNT == 447
    assert body['sheet_count'] == 1
    assert body['total_chunks'] == 1

    preview = api.client.get('/api/v1/rag/excel/%s/preview' % body['document_id'],
                             headers=headers)
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data['total_rows'] == 447
    assert data['sheets'][0]['row_count'] == 447
    assert data['active_sheet']['sheet_name'] == F.MEDIUM_SHEET


def test_uploaded_excel_is_listed_for_owner(api, headers):
    """上传后必须出现在本人文档列表中（Chroma 侧聚合），且类型为表格。"""
    body = api.upload_excel(headers, GOLDEN_SMALL)

    listed = api.client.get('/api/v1/rag/documents', headers=headers)
    assert listed.status_code == 200, listed.text
    docs = listed.json()['documents']
    match = [d for d in docs if d.get('document_id') == body['document_id']]
    assert len(match) == 1
    assert match[0]['filename'] == GOLDEN_SMALL.name
    assert match[0]['chunks'] == body['total_chunks']


def test_document_chunks_endpoint_returns_summary_chunk(api, headers):
    """`GET /documents/{id}` 返回该文档的 chunk（摘要层），供预览/查看原文。"""
    body = api.upload_excel(headers, GOLDEN_SMALL)
    resp = api.client.get('/api/v1/rag/documents/%s' % body['document_id'], headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['document_id'] == body['document_id']
    assert data['total'] == body['total_chunks']
    assert all(c.get('content') for c in data['chunks'])


# ==========================================================================
# 预览
# ==========================================================================
def test_preview_returns_sheet_meta_and_limited_rows(api, headers):
    """预览只读 representation：返回 Sheet 元信息 + 前 N 行（不重新解析原文件）。"""
    body = api.upload_excel(headers, GOLDEN_SMALL)
    resp = api.client.get('/api/v1/rag/excel/%s/preview' % body['document_id'],
                          headers=headers, params={'sheet_index': 0, 'limit': 5})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data['document_id'] == body['document_id']
    assert data['filename'] == GOLDEN_SMALL.name
    assert data['file_type'] == 'xlsx'
    assert data['sheet_count'] == 2
    assert data['total_rows'] == 24                               # 工作簿合计（19 + 5）
    assert data['active_sheet']['row_count'] == F.SMALL_ROW_COUNT == 19
    names = [c['name'] for c in data['active_sheet']['columns']]
    assert names[:3] == ['Order ID', 'SKU ID', 'Seller SKU']
    assert len(data['active_sheet']['rows']) == 5                 # limit 生效
    # 数据从 Excel 第 3 行开始（与线上结构一致）
    assert data['active_sheet']['row_excel_numbers'][:2] == [3, 4]
    # 第二个 Sheet 可单独预览（SkuMaster 5 行）
    second = api.client.get('/api/v1/rag/excel/%s/preview' % body['document_id'],
                            headers=headers, params={'sheet_index': 1}).json()
    assert second['active_sheet']['sheet_name'] == 'SkuMaster'
    assert second['active_sheet']['row_count'] == 5


def test_preview_missing_document_returns_404(api, headers):
    resp = api.client.get('/api/v1/rag/excel/no-such-doc/preview', headers=headers)
    assert resp.status_code == 404


# ==========================================================================
# 鉴权 / 入参边界
# ==========================================================================
def test_upload_requires_authentication(api):
    resp = api.upload_file({}, GOLDEN_SMALL)
    assert resp.status_code == 401


def test_preview_requires_authentication(api, headers):
    body = api.upload_excel(headers, GOLDEN_SMALL)
    resp = api.client.get('/api/v1/rag/excel/%s/preview' % body['document_id'])
    assert resp.status_code == 401


def test_upload_rejects_unsupported_extension(api, headers):
    resp = api.upload_bytes(headers, 'bad.exe', b'MZ', 'application/octet-stream')
    assert resp.status_code == 400
    assert '不支持' in resp.text or 'not supported' in resp.text.lower()
