# -*- coding: utf-8 -*-
"""Embedding 统一分片（≤20/请求）回归 —— 生产缺口修复的护栏。

背景（只读审查结论）：本仓库唯一的"接收完整 texts + 直接调用远端 embedding"入口是
    ``OpenAILikeLLM.generate_embeddings``（`backend/services/llm_service.py`）
        -> ``self.client.embeddings.create(model=..., input=texts)``
它此前把**全部** texts 一次性发给 provider；上传链路（TXT/MD/DOCX/PDF 与 Excel 摘要 chunk）
都经 ``VectorStoreManager.add_documents`` -> Chroma ``add_texts`` ->
``AIAssistantEmbeddings.embed_documents`` 汇聚到该函数，因此**唯一 batch boundary 就在这一层**。

本文件只替换"远端 API 客户端"（``llm.client``），`OpenAILikeLLM` 本身与调用方全部真实；
Fake 客户端记录**每次** remote 调用的输入长度，并可指定"第 N 次调用失败"。

覆盖：21 / 50 / 100 / 448 四档规模、顺序（MARK 标记）、失败批次冒泡、返回数量校验。
"""
import asyncio
from types import SimpleNamespace
from typing import List, Optional

import pytest

from backend.services.llm_service import LLMConfig, LLMFactory

BATCH_CAP = 20


# --------------------------------------------------------------------------
# 伪远端 embedding API（只记录 + 可注入失败；不打日志、不涉及密钥）
# --------------------------------------------------------------------------
class RecordingEmbeddingsAPI:
    def __init__(self, fail_on_call: Optional[int] = None,
                 wrong_count_on_call: Optional[int] = None) -> None:
        self.calls: List[List[str]] = []
        self._fail_on_call = fail_on_call
        self._wrong_count_on_call = wrong_count_on_call
        self.models: List[Optional[str]] = []

    async def create(self, model=None, input=None):
        batch = list(input or [])
        self.calls.append(batch)
        self.models.append(model)
        call_index = len(self.calls)
        if self._fail_on_call == call_index:
            raise RuntimeError('remote embedding exploded at call %d' % call_index)
        data = [SimpleNamespace(embedding=_marker_vector(t)) for t in batch]
        if self._wrong_count_on_call == call_index and data:
            data = data[:-1]                       # 故意少返回一条
        return SimpleNamespace(data=data)


class RecordingClient:
    def __init__(self, api: RecordingEmbeddingsAPI) -> None:
        self.embeddings = api


def _marker_vector(text: str) -> List[float]:
    """按输入文本生成**可辨识**的向量：MARKdddd -> [dddd, len(text)]。"""
    index = _marker_index(text)
    return [float(index if index is not None else -1), float(len(str(text)))]


def _marker_index(text: str) -> Optional[int]:
    pos = str(text).find('MARK')
    if pos < 0:
        return None
    digits = str(text)[pos + 4:pos + 8]
    return int(digits) if digits.isdigit() else None


def _marker_texts(n: int) -> List[str]:
    return ['MARK%04d 合成文本用于 embedding 分片回归（无真实业务数据）' % i for i in range(n)]


def _llm(api: RecordingEmbeddingsAPI):
    """构造真实 OpenAILikeLLM 实例，但把远端客户端替换为记录器（不外发任何请求）。"""
    llm = LLMFactory.create_llm(LLMConfig(
        provider='openai', api_key='test-key-not-used', base_url='http://127.0.0.1:1/v1',
        model='test-model', embedding_model='test-embedding-model'))
    llm.client = RecordingClient(api)
    return llm


# ==========================================================================
# §三/§六：任意 N 个输入必须自动分片，且单次请求 <= 20
# ==========================================================================
@pytest.mark.parametrize('n,expected_sizes', [
    (21, [20, 1]),
    (50, [20, 20, 10]),
    (100, [20, 20, 20, 20, 20]),
    (448, [20] * 22 + [8]),
])
def test_remote_embedding_requests_are_capped(n, expected_sizes):
    """单次 remote embedding 请求 <= 20，且总数量与输入一致。"""
    api = RecordingEmbeddingsAPI()
    llm = _llm(api)
    texts = _marker_texts(n)

    vectors = asyncio.run(llm.generate_embeddings(texts, model='test-embedding-model'))

    sizes = [len(c) for c in api.calls]
    assert sizes == expected_sizes
    assert max(sizes) <= BATCH_CAP                       # 绝不超上限
    assert len(vectors) == n                             # 数量一致（不丢/不重）
    assert all(len(v) == 2 for v in vectors)


def test_small_input_is_single_request_and_empty_input_makes_no_call():
    """<= 20 条仍然只发 1 次请求（Excel 2 条摘要 chunk 场景不变）；空输入不发请求。"""
    api = RecordingEmbeddingsAPI()
    llm = _llm(api)

    vectors = asyncio.run(llm.generate_embeddings(_marker_texts(2)))
    assert [len(c) for c in api.calls] == [2]
    assert len(vectors) == 2

    api2 = RecordingEmbeddingsAPI()
    llm2 = _llm(api2)
    assert asyncio.run(llm2.generate_embeddings([])) == []
    assert api2.calls == []


# ==========================================================================
# §七：顺序必须保持（跨 batch 边界也不得错位）
# ==========================================================================
@pytest.mark.parametrize('n', [21, 50, 100, 448])
def test_embeddings_keep_input_order_across_batches(n):
    """embedding[i] 必须对应 texts[i]（用 MARK 标记独立验证，跨批边界同样成立）。"""
    api = RecordingEmbeddingsAPI()
    llm = _llm(api)
    texts = _marker_texts(n)

    vectors = asyncio.run(llm.generate_embeddings(texts))

    for i, vec in enumerate(vectors):
        assert vec[0] == float(i), 'embedding 顺序错位：索引 %d' % i
        assert vec[1] == float(len(texts[i]))
    # 每个输入文本恰好被发送一次（不重复、不漏）
    sent = [t for c in api.calls for t in c]
    assert sent == texts


# ==========================================================================
# §四/§八：某一批失败 -> 记录 batch index/total/size/reason 并向上抛出（不吞）
# ==========================================================================
def test_failing_batch_is_reported_and_propagates(caplog):
    """448 条（23 批）时让第 7 批失败：必须记录 7/23 与 size，并把异常抛给调用方。"""
    api = RecordingEmbeddingsAPI(fail_on_call=7)
    llm = _llm(api)

    with caplog.at_level('ERROR'):
        with pytest.raises(RuntimeError) as err:
            asyncio.run(llm.generate_embeddings(_marker_texts(448)))

    assert 'remote embedding exploded at call 7' in str(err.value)     # 原异常冒泡
    assert len(api.calls) == 7                                          # 失败即止，不继续
    assert len(api.calls[6]) == 20                                      # 第 7 批 size=20
    logged = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'batch=7' in logged and 'total=23' in logged and 'size=20' in logged
    assert 'RuntimeError' in logged                                     # 异常原因被记录
    assert 'test-key-not-used' not in logged                            # 不泄露密钥
    assert 'MARK0000' not in logged                                     # 不打印被嵌入文本


def test_batch_returning_wrong_count_is_rejected():
    """某批返回条数不足 -> 显式报错（绝不静默错位合并）。"""
    api = RecordingEmbeddingsAPI(wrong_count_on_call=2)
    llm = _llm(api)

    with pytest.raises(Exception) as err:
        asyncio.run(llm.generate_embeddings(_marker_texts(50)))

    assert 'batch' in str(err.value) or '数量' in str(err.value)
    assert len(api.calls) == 2                                          # 第 2 批出错即止
