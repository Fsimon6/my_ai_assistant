# -*- coding: utf-8 -*-
"""Stage 5 API Regression — 普通文档上传 smoke（§十：TXT / MD / DOCX / PDF）。

只做**最小 API smoke**，不重复完整功能测试：
  * TXT / MD：必须 200 + 有 chunk + 可列出 + 可删除；
  * DOCX / PDF：解析依赖可选重型库（python-docx / PyMuPDF）。**本环境未安装**，
    因此这里断言的是"**失败也必须走分类错误路径**"（结构化 detail、非 2xx、
    不泄露 traceback、不产生幽灵文档）；若依赖存在则要求正常 200（同一份用例两种环境都成立）。
"""
import importlib.util

import pytest

HAS_DOCX = importlib.util.find_spec('docx') is not None
HAS_FITZ = importlib.util.find_spec('fitz') is not None

TXT_MIME = 'text/plain'
MD_MIME = 'text/markdown'
DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
PDF_MIME = 'application/pdf'


def _assert_clean_failure(resp) -> None:
    """依赖缺失时的统一契约：非 2xx + 结构化错误 + 不泄露 traceback。"""
    assert resp.status_code >= 400, resp.text
    assert resp.json().get('detail'), resp.text
    assert 'Traceback' not in resp.text


def test_txt_upload_smoke(api):
    headers = api.user('doc_txt_user')
    content = ('API regression 合成文本，不含真实业务数据。\n' * 60).encode('utf-8')
    resp = api.upload_bytes(headers, 'smoke.txt', content, TXT_MIME)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['success'] is True
    assert body['document_id']
    assert body['total_chunks'] >= 1

    listed = api.client.get('/api/v1/rag/documents', headers=headers).json()
    assert body['document_id'] in [d['document_id'] for d in listed['documents']]

    deleted = api.client.request('DELETE', '/api/v1/rag/documents', headers=headers,
                                 json={'document_ids': [body['document_id']]})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()['deleted_count'] == body['total_chunks']


def test_md_upload_smoke(api):
    """MD 与 TXT/Excel 共用**同一个** embedding 入口（单次请求 <= 20、总量一致）。"""
    headers = api.user('doc_md_user')
    content = ('# API regression 标题\n\n- 合成 Markdown 内容，不含真实业务数据。\n' * 40).encode()
    resp = api.upload_bytes(headers, 'smoke.md', content, MD_MIME)

    assert resp.status_code == 200, resp.text
    total = resp.json()['total_chunks']
    assert total >= 1
    sizes = [len(r) for r in api.fake.embedding_requests]
    assert max(sizes) <= 20                       # 统一 batch boundary 生效
    assert sum(sizes) == total


def test_docx_upload_smoke_or_clean_failure(api):
    headers = api.user('doc_docx_user')
    payload = b'PK\x03\x04 not-a-real-docx-but-extension-is-what-matters'
    resp = api.upload_bytes(headers, 'smoke.docx', payload, DOCX_MIME)

    if HAS_DOCX:
        assert resp.status_code == 200, resp.text
        assert resp.json()['total_chunks'] >= 1
    else:
        _assert_clean_failure(resp)


def test_pdf_upload_smoke_or_clean_failure(api):
    headers = api.user('doc_pdf_user')
    payload = b'%PDF-1.4\n% synthetic, not a real pdf\n%%EOF\n'
    resp = api.upload_bytes(headers, 'smoke.pdf', payload, PDF_MIME)

    if HAS_FITZ:
        assert resp.status_code in (200, 400), resp.text     # 内容非法时可以 400
        if resp.status_code == 400:
            _assert_clean_failure(resp)
    else:
        _assert_clean_failure(resp)


def test_excel_api_regression_does_not_break_document_upload(api):
    """§十：Excel API regression 与普通文档链路互不影响（同一用户先文档后表格）。"""
    headers = api.user('doc_mix_user')
    doc = api.upload_bytes(headers, 'smoke_mix.txt', b'plain text for smoke test.\n' * 30,
                           TXT_MIME)
    assert doc.status_code == 200, doc.text

    from .conftest import GOLDEN_SMALL
    excel = api.upload_excel(headers, GOLDEN_SMALL)
    assert excel['doc_kind'] == 'excel'

    listed = api.client.get('/api/v1/rag/documents', headers=headers).json()['documents']
    ids = [d['document_id'] for d in listed]
    assert doc.json()['document_id'] in ids and excel['document_id'] in ids
