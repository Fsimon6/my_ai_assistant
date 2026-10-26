# -*- coding: utf-8 -*-
"""Excel 空白结构上传回归（合成数据，不读取真实业务 Excel）。

覆盖：
A. 单个空白 Sheet -> 400，不留下文档/artifacts/临时文件/向量；
B. 多个空白 Sheet -> 整本拒绝；
C. 有合法列/表头但零数据行 -> 允许上传，查询返回 0 行；
D. 有效 Sheet + 空白 Sheet 共存 -> 允许上传；有效 Sheet 可查，
   空白 Sheet 查询返回可分类的 400（不能变成 DuckDB 500）。
"""
import io
from pathlib import Path

import pytest
from openpyxl import Workbook

from backend.excel import store as excel_store
from backend.services import document_service

from .conftest import XLSX_MIME


def _xlsx_bytes(sheets):
    """构造只含指定 Sheet 的合成工作簿；sheets=[(name, rows), ...]。"""
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets:
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture()
def upload_root(tmp_path, monkeypatch):
    """把上传临时目录固定到用例临时目录，便于验证失败后无残留。"""
    root = tmp_path / 'uploads'
    monkeypatch.setattr(document_service.settings, 'UPLOAD_TEMP_DIR', str(root))
    return root


def _assert_no_upload_residue(api, headers, upload_root):
    listed = api.client.get('/api/v1/rag/documents', headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()['documents'] == []

    excel_root = Path(excel_store.EXCEL_DATA_ROOT)
    assert not excel_root.exists() or list(excel_root.glob('**/representation.json')) == []

    assert not upload_root.exists() or list(upload_root.glob('*')) == []
    assert api.fake.embedding_requests == []


@pytest.mark.parametrize(
    'sheets,filename',
    [
        ([('Blank', [])], 'blank.xlsx'),
        ([('Blank1', []), ('Blank2', [])], 'two_blank_sheets.xlsx'),
    ],
)
def test_rejects_workbook_without_usable_columns(api, upload_root, sheets, filename):
    """A/B：全空白工作簿（单 Sheet 和多 Sheet）都必须在上传阶段被拒绝。"""
    headers = api.user('blank_excel_user')
    resp = api.upload_bytes(headers, filename, _xlsx_bytes(sheets), XLSX_MIME)

    assert resp.status_code == 400, resp.text
    detail = resp.json().get('detail')
    assert isinstance(detail, dict), resp.text
    assert detail['error_code'] == 'DOCUMENT_PARSE_FAILED'
    assert '没有任何可用' in detail['message']
    _assert_no_upload_residue(api, headers, upload_root)


def test_rejects_empty_csv(api, upload_root):
    """空 CSV 也走同一表示层校验，不得返回上传成功。"""
    headers = api.user('empty_csv_user')
    resp = api.upload_bytes(headers, 'empty.csv', b'', 'text/csv')

    assert resp.status_code == 400, resp.text
    detail = resp.json().get('detail')
    assert isinstance(detail, dict), resp.text
    assert detail['error_code'] == 'DOCUMENT_PARSE_FAILED'
    _assert_no_upload_residue(api, headers, upload_root)


def test_header_only_workbook_is_allowed_and_queryable(api, upload_root):
    """C：有合法列/表头但没有数据行，不应被“空白结构”校验误杀。"""
    headers = api.user('header_only_user')
    content = _xlsx_bytes([('Data', [['name', 'value']])])
    resp = api.upload_bytes(headers, 'header_only.xlsx', content, XLSX_MIME)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['success'] is True
    assert body['total_rows'] == 0
    assert body['total_chunks'] == 1

    query = api.client.post(
        '/api/v1/rag/excel/%s/query' % body['document_id'],
        headers=headers,
        json={'sheet_index': 0},
    )
    assert query.status_code == 200, query.text
    assert query.json()['total_matches'] == 0


def test_mixed_valid_and_blank_sheets_keeps_valid_sheet_queryable(api, upload_root):
    """D：有有效 Sheet 时允许整本上传；空白 Sheet 查询必须是 400，而不是 500。"""
    headers = api.user('mixed_sheet_user')
    content = _xlsx_bytes([
        ('Blank', []),
        ('Data', [['name', 'value'], ['alpha', 1]]),
    ])
    resp = api.upload_bytes(headers, 'mixed.xlsx', content, XLSX_MIME)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['success'] is True
    assert body['sheet_count'] == 2
    document_id = body['document_id']

    blank_query = api.client.post(
        '/api/v1/rag/excel/%s/query' % document_id,
        headers=headers,
        json={'sheet_index': 0},
    )
    assert blank_query.status_code == 400, blank_query.text
    blank_detail = blank_query.json().get('detail')
    assert isinstance(blank_detail, dict), blank_query.text
    assert blank_detail['code'] == 'invalid_param'
    assert '没有任何列' in blank_detail['message']

    valid_query = api.client.post(
        '/api/v1/rag/excel/%s/query' % document_id,
        headers=headers,
        json={'sheet_index': 1},
    )
    assert valid_query.status_code == 200, valid_query.text
    assert valid_query.json()['total_matches'] == 1
