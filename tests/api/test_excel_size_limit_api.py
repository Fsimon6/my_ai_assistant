# -*- coding: utf-8 -*-
"""P-8 规模上限的 **API 层** 回归（此前的真实缺口：413 语义零覆盖）。

覆盖：
  * 超过 EXCEL_MAX_SHEET_ROWS 的 xlsx → HTTP 413 + error_code=FILE_TOO_LARGE（**不是 500/400**）；
  * 超限失败必须**不留半成品**：没有文档进入列表、没有 excel artifacts 目录；
  * 超过 `settings.MAX_UPLOAD_SIZE` 的上传 → HTTP 413（应用层字节上限，与逻辑单元格上限相互独立）。

超限文件由 `tests/helpers/fixtures.build_probe_xlsx()` 生成（极小体积 + 极大使用范围），
因此不需要真实的大表数据。
"""
from pathlib import Path

from backend.config.settings import settings
from backend.excel import store as excel_store
from backend.excel.parser import _excel_limits
from tests.helpers import fixtures as F

from .conftest import XLSX_MIME

LIMITS = _excel_limits()
#: 唯一来源：settings.MAX_UPLOAD_SIZE（字节；B2-B）
MAX_UPLOAD_BYTES = settings.MAX_UPLOAD_SIZE


def test_over_limit_xlsx_returns_413_and_leaves_no_residue(api, tmp_path):
    headers = api.user('size_limit_user')
    path = F.build_probe_xlsx(tmp_path / 'too_big.xlsx',
                              [('S1', LIMITS['rows'] + 1, 1)])     # 200_001 行
    resp = api.upload_bytes(headers, 'too_big.xlsx', path.read_bytes(), XLSX_MIME)

    assert resp.status_code == 413, resp.text                 # 明确 413，而不是 500/400
    assert 'Traceback' not in resp.text
    detail = resp.json().get('detail')
    assert isinstance(detail, dict), resp.text
    assert detail['error_code'] == 'FILE_TOO_LARGE'
    assert '表格过大' in detail['message']

    # 不留半成品：文档列表为空，且没有 excel artifacts 目录
    listed = api.client.get('/api/v1/rag/documents', headers=headers).json()
    assert listed['documents'] == []
    root = Path(excel_store.EXCEL_DATA_ROOT)
    assert not root.exists() or list(root.iterdir()) == []


def test_upload_over_max_bytes_returns_413(api):
    """应用层字节上限（10MB）：与"逻辑单元格上限"是两套独立语义，这里锁定前者。"""
    headers = api.user('size_limit_bytes_user')
    content = b'x' * (MAX_UPLOAD_BYTES + 1)
    resp = api.upload_bytes(headers, 'too_large.txt', content, 'text/plain')

    assert resp.status_code == 413, resp.text
    assert resp.json().get('detail'), resp.text

    listed = api.client.get('/api/v1/rag/documents', headers=headers).json()
    assert listed['documents'] == []
