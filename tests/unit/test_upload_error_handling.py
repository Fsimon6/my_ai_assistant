# -*- coding: utf-8 -*-
"""稳定性补丁：上传链路的错误分类 / 一致性 / 不留半成品。

覆盖：
1. provider 错误分类（欠费 / 鉴权 / 限流 / 超时 / 网络 / 模型不存在 / 未知）；
2. 绝不泄露密钥，且不回传 provider 原始 JSON；
3. Excel 上传成功；embedding 失败时：明确 error_code + 无半成品（附件目录/临时文件/向量都被清理）；
4. 解析失败与 provider 失败**必须区分**（不把向量化不可用伪装成解析失败，反之亦然）；
5. API 层映射：错误分类 -> 对应 HTTP 状态码 + {error_code, message} 结构。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from backend.services.rag_service import RagService
from backend.utils import provider_errors as pe

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 文件名里的空格在全角/半角之间可能不同，用 glob 定位更稳
_SMALL_CANDIDATES = sorted(PROJECT_ROOT.glob('直邮一店*.xlsx'))
SMALL_REAL = _SMALL_CANDIDATES[0] if _SMALL_CANDIDATES else (PROJECT_ROOT / '直邮一店 8.20号订单.xlsx')

#: 真实 provider 报文（DashScope 欠费）——必须被识别为"额度不足"，且不外泄细节
ARREARAGE = ("Error code: 400 - {'error': {'message': 'Access denied, please make sure your "
             "account is in good standing. For details, see: https://help.aliyun.com/"
             "zh/model-studio/error-code#overdue-payment', 'type': 'Arrearage', 'param': None, "
             "'code': 'Arrearage'}, 'id': '137a1c72-3001-96fc-b546-329229ffb8a6', "
             "'request_id': '137a1c72-3001-96fc-b546-329229ffb8a6'}")
# 注意：这里必须使用**伪造**的密钥（真实密钥一律不得出现在代码/用例/仓库中）
AUTH_FAIL = ("Error code: 401 - {'error': {'message': 'Incorrect API key provided: "
             "sk-fake-test-key-0000000000000000000000', 'type': 'invalid_request_error'}}")


# ==========================================================================
# 1. 分类
# ==========================================================================
@pytest.mark.parametrize('exc_text,expected_code,expected_status', [
    (ARREARAGE, 'EMBEDDING_QUOTA_EXCEEDED', 503),
    ("{'code': 'AllocationQuota.FreeTierOnly', 'message': 'Free quota exhausted'}",
     'EMBEDDING_QUOTA_EXCEEDED', 503),
    (AUTH_FAIL, 'EMBEDDING_AUTH_FAILED', 502),
    ("Error code: 429 - rate limit reached", 'EMBEDDING_RATE_LIMITED', 429),
    ('Request timed out after 30s', 'EMBEDDING_TIMEOUT', 504),
    ('Connection error: [Errno 11001] getaddrinfo failed', 'EMBEDDING_NETWORK_ERROR', 502),
    ("Error code: 404 - model 'qwen3.7-text-embedding-flash' does not exist",
     'EMBEDDING_MODEL_NOT_FOUND', 502),
    ('something unexpected happened', 'EMBEDDING_PROVIDER_ERROR', 502),
])
def test_classify_embedding_errors(exc_text, expected_code, expected_status):
    class EmbeddingError(Exception):
        pass

    classified = pe.classify_provider_error(EmbeddingError(exc_text))
    assert classified.error_code == expected_code
    assert classified.http_status == expected_status
    assert classified.message            # 一定有面向用户的中文说明
    assert classified.kind == 'provider'


def test_classify_llm_error_uses_llm_prefix():
    class LLMError(Exception):
        pass

    c = pe.classify_provider_error(LLMError('chat completion failed: rate limit'))
    assert c.error_code == 'LLM_RATE_LIMITED'


def test_classified_never_leaks_secret():
    class EmbeddingError(Exception):
        pass

    c = pe.classify_provider_error(EmbeddingError(AUTH_FAIL))
    body = json.dumps(c.to_response(), ensure_ascii=False)
    assert 'sk-fake-test-' not in body
    assert 'sk-fake-test-' not in c.message
    assert 'request_id' not in body
    # 原始细节只保留在（脱敏后的）日志字段里
    assert 'sk-fake-test-' not in c.detail
    assert '***' in c.detail


def test_parse_error_is_not_provider_error():
    from backend.excel import query as q

    c = pe.classify_upload_error(q.ExcelQueryError(q.ERR_INVALID_PARAM, 'Sheet 不存在'))
    assert c.error_code == pe.DOCUMENT_PARSE_FAILED
    assert c.http_status == 400
    assert c.kind == 'parser'


def test_unknown_error_is_internal():
    c = pe.classify_upload_error(ValueError('boom'))
    assert c.error_code == pe.INTERNAL_ERROR
    assert c.http_status == 500


# ==========================================================================
# 2. RagService 行为（不起 Chroma / LLM：直接构造对象）
# ==========================================================================
class FakeVectorStore:
    def __init__(self, fail_with: Optional[Exception] = None):
        self.fail_with = fail_with
        self.added: List[Dict[str, Any]] = []
        self.deleted: List[tuple] = []

    async def add_documents(self, documents):
        if self.fail_with is not None:
            raise self.fail_with
        self.added.extend(documents)
        return [d['id'] for d in documents]

    async def delete_documents(self, document_ids, user_id=None):
        self.deleted.append((list(document_ids), user_id))
        return len(document_ids)


def _service(vector_store) -> RagService:
    svc = RagService.__new__(RagService)
    svc.vector_store = vector_store
    svc.cache = None

    async def _noop(*args, **kwargs):
        return None

    svc._clear_related_cache = _noop          # 不触碰真实缓存
    return svc


@pytest.mark.skipif(not SMALL_REAL.exists(), reason='缺少真实测试表格')
def test_excel_upload_success(tmp_path, monkeypatch):
    from backend.excel import store as excel_store

    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path)
    # 上传链路会把"临时上传文件"在处理成功后删除，
    # 因此**必须**用副本，绝不能把仓库里的测试表格本身传进去。
    upload_copy = tmp_path / 'uploaded.xlsx'
    upload_copy.write_bytes(SMALL_REAL.read_bytes())

    vs = FakeVectorStore()
    svc = _service(vs)
    out = asyncio.run(svc.process_and_store_document(
        file_path=str(upload_copy), user_id=7, original_filename=SMALL_REAL.name, file_size=1234))

    assert out['success'] is True
    assert out['doc_kind'] == 'excel'
    assert out['total_rows'] == 19
    assert len(vs.added) == 1                    # 每 Sheet 一条摘要 chunk
    doc_dir = tmp_path / out['document_id']
    assert (doc_dir / 'representation.json').exists()
    assert (doc_dir / 'original.xlsx').exists()   # 原文件已持久化
    assert not upload_copy.exists()              # 临时文件按预期被清理
    assert vs.deleted == []                      # 成功时不清理向量
    assert SMALL_REAL.exists(), '测试夹具不能被上传链路删除'


@pytest.mark.skipif(not SMALL_REAL.exists(), reason='缺少真实测试表格')
def test_excel_upload_embedding_failure_leaves_no_residue(tmp_path, monkeypatch):
    """embedding 欠费：必须明确报错 + 不留半成品（目录/临时文件/向量都不留）。"""
    from backend.excel import store as excel_store
    from backend.services.vector_service import EmbeddingError

    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path)
    upload_copy = tmp_path / 'uploaded.xlsx'
    upload_copy.write_bytes(SMALL_REAL.read_bytes())

    vs = FakeVectorStore(fail_with=EmbeddingError(f'远程 embedding 调用失败：{ARREARAGE}'))
    svc = _service(vs)
    out = asyncio.run(svc.process_and_store_document(
        file_path=str(upload_copy), user_id=7, original_filename=SMALL_REAL.name, file_size=1234))

    assert out['success'] is False
    assert out['error_code'] == 'EMBEDDING_QUOTA_EXCEEDED'
    assert out['http_status'] == 503
    assert '额度' in out['message']
    assert 'sk-' not in json.dumps(out, ensure_ascii=False, default=str)
    # 无半成品
    assert list(tmp_path.glob('*/representation.json')) == []
    assert not upload_copy.exists()
    # 向量侧被显式清理（按 document_id）
    assert vs.deleted and len(vs.deleted[0][0]) == 1


@pytest.mark.skipif(not SMALL_REAL.exists(), reason='缺少真实测试表格')
def test_excel_parse_failure_classified_as_parse_error(tmp_path, monkeypatch):
    """解析失败必须是 DOCUMENT_PARSE_FAILED，绝不能伪装成 embedding 故障。"""
    from backend.excel import store as excel_store

    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path)
    bad = tmp_path / 'broken.xlsx'
    bad.write_bytes(b'this is not a real xlsx')

    vs = FakeVectorStore()
    svc = _service(vs)
    out = asyncio.run(svc.process_and_store_document(
        file_path=str(bad), user_id=7, original_filename='broken.xlsx', file_size=20))

    assert out['success'] is False
    assert out['error_code'] == pe.DOCUMENT_PARSE_FAILED
    assert out['http_status'] == 400
    assert '解析' in out['message']
    assert vs.added == []
    assert not bad.exists()                       # 临时文件已清理


# ==========================================================================
# 3. API 层映射（直接调用端点函数，不起 HTTP）
# ==========================================================================
def _upload_with(monkeypatch, tmp_path, service_result):
    from backend.api.v1 import rag as rag_api

    class FakeSvc:
        async def process_and_store_document(self, *args, **kwargs):
            return service_result

    async def fake_save(upload_file):
        p = tmp_path / 'uploaded.xlsx'
        p.write_bytes(b'x')
        return str(p)

    monkeypatch.setattr(rag_api, 'save_uploaded_file', fake_save)
    monkeypatch.setattr(rag_api, 'get_rag_service', lambda: FakeSvc())

    class FakeUpload:
        filename = 'x.xlsx'

    user = SimpleNamespace(id=1)
    return asyncio.run(rag_api.upload_document(file=FakeUpload(), metadata=None, current_user=user))


def test_api_maps_embedding_failure_to_503(monkeypatch, tmp_path):
    result = {'success': False, 'error': ARREARAGE, 'filename': 'x.xlsx'}
    with pytest.raises(HTTPException) as ei:
        _upload_with(monkeypatch, tmp_path, result)
    assert ei.value.status_code == 503
    assert ei.value.detail['error_code'] == 'EMBEDDING_QUOTA_EXCEEDED'
    assert '额度' in ei.value.detail['message']
    assert 'sk-' not in json.dumps(ei.value.detail, ensure_ascii=False)


def test_api_maps_parse_failure_to_400_with_message(monkeypatch, tmp_path):
    from backend.excel import query as q

    svc_result = {'success': False,
                  'error': q.ExcelQueryError(q.ERR_INVALID_PARAM, 'Sheet 不存在').message,
                  'error_code': 'DOCUMENT_PARSE_FAILED',
                  'message': '文档解析失败：Sheet 不存在。该文件未被保存。',
                  'http_status': 400,
                  'filename': 'x.xlsx'}
    with pytest.raises(HTTPException) as ei:
        _upload_with(monkeypatch, tmp_path, svc_result)
    assert ei.value.status_code == 400
    assert ei.value.detail['error_code'] == 'DOCUMENT_PARSE_FAILED'
    assert '解析失败' in ei.value.detail['message']


def test_api_success_shape_unchanged(monkeypatch, tmp_path):
    """成功响应结构保持兼容（前端依赖这些字段）。"""
    result = {'success': True, 'document_id': 'abc', 'chunk_ids': ['abc_0'], 'filename': 'x.xlsx',
              'total_chunks': 1, 'doc_kind': 'excel', 'file_type': 'xlsx', 'sheet_count': 1,
              'total_rows': 19, 'sheets': [], 'parse_ms': 3, 'warnings': []}
    out = _upload_with(monkeypatch, tmp_path, result)
    assert out['success'] is True
    assert out['document_id'] == 'abc'
    assert out['total_rows'] == 19


def test_unsupported_file_type_still_400(monkeypatch, tmp_path):
    from backend.api.v1 import rag as rag_api

    class FakeUpload:
        filename = 'x.exe'

    user = SimpleNamespace(id=1)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(rag_api.upload_document(file=FakeUpload(), metadata=None, current_user=user))
    assert ei.value.status_code == 400
    assert '不支持的文件类型' in str(ei.value.detail)


# ==========================================================================
# 4. 后续 2A：NL / 对话链路的 LLM 错误也必须分类（不再把 provider 原始报文给用户）
# ==========================================================================
#: 真实报文（DashScope 免费额度耗尽）——浏览器上曾经原样显示过这一整段
FREE_QUOTA = ("Error code: 400 - {'error': {'message': 'Free quota exhausted. To continue "
              "accessing the model on a paid basis, please add funds or disable the "
              "\"use free tier only\" mode in the management console.', "
              "'type': 'AllocationQuota.FreeTierOnly', 'param': None, "
              "'code': 'AllocationQuota.FreeTierOnly'}, 'id': 'chatcmpl-e55f55be'}")


@pytest.mark.parametrize('exc_text,expected_code,expected_status', [
    (FREE_QUOTA, 'LLM_QUOTA_EXCEEDED', 503),
    (ARREARAGE, 'LLM_QUOTA_EXCEEDED', 503),
    (AUTH_FAIL, 'LLM_AUTH_FAILED', 502),
    ('Error code: 429 - rate limit reached for requests', 'LLM_RATE_LIMITED', 429),
    ('Request timed out.', 'LLM_TIMEOUT', 504),
    ("Error code: 404 - model 'foo' does not exist", 'LLM_MODEL_NOT_FOUND', 502),
    ('Connection error: [Errno 11001] getaddrinfo failed', 'LLM_NETWORK_ERROR', 502),
])
def test_classify_llm_errors(exc_text, expected_code, expected_status):
    class LLMCallError(Exception):
        pass

    c = pe.classify_llm_error(LLMCallError(exc_text))
    assert c.error_code == expected_code
    assert c.http_status == expected_status
    assert c.kind == 'provider'
    body = json.dumps(c.to_response(), ensure_ascii=False)
    # 用户可见文案里绝不出现 provider 原始报文 / 字段名 / 密钥
    for leak in ('Error code', 'Free quota', 'AllocationQuota', 'request_id', 'sk-', 'chatcmpl'):
        assert leak not in body, f'{leak} 泄漏到用户可见文案：{body}'
    assert '额度' in c.message or '服务' in c.message
