# -*- coding: utf-8 -*-
"""Stage 4：LLM timeout 真正接线（把项目既有 timeout 传到 transport 层）。

只读审计事实：
  * 全仓**只有一处** OpenAI client 创建：``llm_service.OpenAILikeLLM.__init__`` 的
    ``AsyncOpenAI(api_key=..., base_url=...)`` —— chat / streaming / Excel NL 解析 /
    character-specific 模型 / agent pipeline 全部共用这一个实例（``get_llm()` 单例 +
    ``LLMFactory.create_llm``）；
  * ``LLMConfig.timeout = 30``（dataclass 默认）从未被使用；
    ``backend.config.settings.settings`` **没有** ``LLM_TIMEOUT`` 字段；
    仅 ``ProductionConfig.LLM_TIMEOUT = 30`` 存在 —— 因此配置存在但从未生效。

本文件断言（使用 fake client 捕获真实构造参数，不发真实请求、不改生产配置）：
  A. 全局 client -> timeout = 配置值
  B. character-specific client -> 同样带 timeout，且 model / api_key 解析规则不变
  C. streaming 与非流式都仍可用（timeout 不会破坏流式）
  D. Excel NL 解析（llm_parse_turn / llm_parse_analysis，含 multi-step）走同一个带 timeout 的 client
  E. embedding 与 chat 共用同一 client（本项目实际走本地 embedding，未误接）
  + timeout 异常按既有 provider 错误体系向上传播；Excel NL 超时**不产生半截 snapshot**
"""
import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import openai
import pytest

from backend.services import llm_service as ls
from backend.services.llm_service import (LLMConfig, LLMFactory, OpenAILikeLLM,
                                          resolve_request_timeout)
from backend.utils import provider_errors as pe

CATALOG: List[Dict[str, Any]] = [{
    'document_id': 'doc-1', 'filename': '一店.xlsx', 'file_type': 'xlsx',
    'created_at': '2026-09-30T00:00:00', 'total_rows': 19,
    'sheets': [{'sheet_name': 'OrderSKUList', 'sheet_index': 0, 'row_count': 19,
                'column_count': 2, 'columns': ['Order ID', 'Shipping Provider Name']}],
}]


# --------------------------------------------------------------------------
# fake AsyncOpenAI：捕获构造参数，并提供可控的 chat / embeddings 行为
# --------------------------------------------------------------------------
class _ChatCompletions:
    def __init__(self, owner):
        self.owner = owner

    async def create(self, **kwargs):
        self.owner.calls.append(kwargs)
        if self.owner.raise_exc is not None:
            raise self.owner.raise_exc
        if kwargs.get('stream'):
            return self._stream()
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.owner.content or ''))])

    async def _stream(self):
        for piece in self.owner.stream_pieces:
            if piece is None:                      # 模拟 DashScope 的空 choices 结束帧
                yield SimpleNamespace(choices=[])
            else:
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=piece))])


class _Embeddings:
    def __init__(self, owner):
        self.owner = owner

    async def create(self, **kwargs):
        self.owner.emb_calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])])


class FakeAsyncOpenAI:
    """替身 client：记录 **真实** 构造参数（含 timeout），不联网。"""

    created = 0
    last_kwargs: Dict[str, Any] = {}

    def __init__(self, **kwargs):
        FakeAsyncOpenAI.created += 1
        FakeAsyncOpenAI.last_kwargs = dict(kwargs)
        self.kwargs = dict(kwargs)
        self.calls: List[Dict[str, Any]] = []
        self.emb_calls: List[Dict[str, Any]] = []
        self.raise_exc: Any = None
        self.content: str = '{}'
        self.stream_pieces: List[Any] = []
        self.chat = SimpleNamespace(completions=_ChatCompletions(self))
        self.embeddings = _Embeddings(self)

    @property
    def timeout(self):
        return self.kwargs.get('timeout')


@pytest.fixture(autouse=True)
def _fake_openai(monkeypatch):
    FakeAsyncOpenAI.created = 0
    FakeAsyncOpenAI.last_kwargs = {}
    monkeypatch.setattr(openai, 'AsyncOpenAI', FakeAsyncOpenAI)
    yield


def _llm(model: str = 'test-model', api_key: str = 'test-key', **kw) -> OpenAILikeLLM:
    return OpenAILikeLLM(LLMConfig(provider='openai', api_key=api_key,
                                   base_url='http://fake.local/v1', model=model, **kw))


def _consume(agen) -> List[str]:
    async def _run():
        return [chunk async for chunk in agen]
    return asyncio.run(_run())


# ==========================================================================
# A：全局 client
# ==========================================================================
def test_a_global_client_uses_configured_timeout():
    llm = LLMFactory.from_env()
    assert llm.request_timeout == 30.0                       # 项目既有配置值
    assert FakeAsyncOpenAI.last_kwargs['timeout'] == 30.0     # 真正传给了 transport
    assert FakeAsyncOpenAI.last_kwargs['base_url']            # base_url 规则未变
    assert llm.config.model                                  # model 解析未变


def test_a2_default_config_timeout_is_30():
    assert LLMConfig(provider='openai', api_key='k').timeout == 30
    assert resolve_request_timeout(LLMConfig(provider='openai', api_key='k')) == 30.0


# ==========================================================================
# B：character-specific（只改参数，不改 model 解析规则）
# ==========================================================================
def test_b_character_client_keeps_model_and_key_and_gets_timeout():
    llm = _llm(model='char-model-xyz', api_key='char-key')
    assert FakeAsyncOpenAI.last_kwargs['timeout'] == 30.0
    assert FakeAsyncOpenAI.last_kwargs['api_key'] == 'char-key'    # 角色 key 原样
    assert llm.config.model == 'char-model-xyz'                    # 角色 model 原样
    assert llm.request_timeout == 30.0


def test_b2_env_config_timeout_wins_when_declared(monkeypatch):
    """环境配置里声明了 LLM_TIMEOUT（如 ProductionConfig）时以它为准。"""
    import backend.config as cfg
    monkeypatch.setattr(cfg, 'config', SimpleNamespace(LLM_TIMEOUT=7))
    assert resolve_request_timeout(LLMConfig(provider='openai', api_key='k')) == 7.0
    llm = _llm()
    assert llm.request_timeout == 7.0
    assert FakeAsyncOpenAI.last_kwargs['timeout'] == 7.0


def test_b3_falls_back_to_llmconfig_when_env_missing(monkeypatch):
    import backend.config as cfg
    monkeypatch.setattr(cfg, 'config', SimpleNamespace())        # 开发环境：未定义
    assert resolve_request_timeout(LLMConfig(provider='openai', api_key='k',
                                             timeout=12)) == 12.0
    assert resolve_request_timeout(LLMConfig(provider='openai', api_key='k',
                                             timeout=None)) == 30.0


# ==========================================================================
# C：streaming / 非流式都仍可用
# ==========================================================================
def test_c_streaming_works_with_timeout():
    llm = _llm()
    llm.client.stream_pieces = ['你', None, '好']       # 含空 choices 结束帧
    chunks = _consume(llm.chat_completion([{'role': 'user', 'content': 'hi'}], stream=True))
    assert chunks == ['你', '好']
    assert llm.client.timeout == 30.0
    assert llm.client.calls[0]['stream'] is True


def test_c2_non_streaming_works_with_timeout():
    llm = _llm()
    llm.client.content = '完整答复'
    chunks = _consume(llm.chat_completion([{'role': 'user', 'content': 'hi'}], stream=False))
    assert chunks == ['完整答复']
    assert llm.client.timeout == 30.0
    assert 'stream' not in llm.client.calls[0]          # 非流式：与既有调用形态一致


# ==========================================================================
# D：Excel NL 解析（含 multi-step）走同一个带 timeout 的 client
# ==========================================================================
def test_d_excel_nl_turn_parse_uses_timeout_client():
    from backend.excel import nl_query as nl
    llm = _llm()
    llm.client.content = json.dumps({'action': 'new_query', 'columns': ['Order ID'],
                                     'limit': 10}, ensure_ascii=False)
    turn = asyncio.run(nl.llm_parse_turn(llm, '列出订单号', CATALOG, None))
    assert turn.action == nl.ACTION_NEW_QUERY
    assert turn.intent.columns == ['Order ID']
    assert llm.client.timeout == 30.0                  # 解析用的就是这个 client
    assert llm.client.calls[0]['temperature'] == 0.0


def test_d2_excel_nl_multi_step_parse_uses_same_client():
    from backend.excel import nl_query as nl
    llm = _llm()
    llm.client.content = json.dumps({'steps': [
        {'type': 'group_aggregate', 'group_by': ['SKU ID'], 'operation': 'sum',
         'column': 'Order Amount', 'top_n': 3},
        {'type': 'aggregate', 'operation': 'sum', 'source': 'step_1'}]}, ensure_ascii=False)
    analysis = asyncio.run(nl.llm_parse_analysis(llm, '前3个SKU的销售额总和', CATALOG, None, None))
    assert len(analysis.steps) == 2
    assert FakeAsyncOpenAI.created == 1                # 全程只有一个 client 实例
    assert llm.client.timeout == 30.0


# ==========================================================================
# E：embedding 与 chat 共用同一 client（本项目实际走本地 embedding，未被误接）
# ==========================================================================
def test_e_embedding_uses_same_client_and_model_unhanged():
    llm = _llm(embedding_model='embed-xyz')
    vecs = asyncio.run(llm.generate_embeddings(['a']))
    assert vecs == [[0.1, 0.2]]
    assert len(llm.client.emb_calls) == 1
    assert llm.client.emb_calls[0]['model'] == 'embed-xyz'     # 模型解析未变
    assert FakeAsyncOpenAI.created == 1                        # 未新建第二个 client


# ==========================================================================
# timeout 异常：传播 + 既有分类 + Excel NL 不产生半截 snapshot
# ==========================================================================
def _timeout_error() -> 'openai.APITimeoutError':
    return openai.APITimeoutError(request=httpx.Request('POST', 'http://fake.local/v1/chat'))


def test_timeout_error_propagates_and_is_classified():
    llm = _llm()
    llm.client.raise_exc = _timeout_error()
    with pytest.raises(openai.APITimeoutError):
        _consume(llm.chat_completion([{'role': 'user', 'content': 'hi'}], stream=True))
    # 错误码沿用既有 provider 体系（LLM_ 前缀 + TIMEOUT），未新增错误类型
    assert pe.classify_llm_error(_timeout_error()).error_code \
        == 'LLM_' + pe.TIMEOUT == 'LLM_TIMEOUT'


def test_excel_nl_timeout_returns_error_without_partial_snapshot():
    from backend.api.v1.rag import build_excel_history_snapshot
    from backend.excel import nl_query as nl

    llm = _llm()
    llm.client.raise_exc = _timeout_error()
    outcome = asyncio.run(nl.run_nl_query('列出订单号和物流商', CATALOG, llm=llm,
                                          user_id=1, session_key='timeout-case'))
    assert outcome['status'] == nl.STATUS_ERROR
    assert outcome.get('error_code') == 'LLM_' + pe.TIMEOUT
    assert outcome.get('result') is None
    assert build_excel_history_snapshot(outcome) is None      # 绝不写半截快照
    assert 'query' not in outcome or not outcome.get('query')


def test_success_path_history_shape_unchanged():
    """成功路径：snapshot 结构照旧（timeout 只在 transport 层，不改变契约）。"""
    from backend.api.v1.rag import EXCEL_HISTORY_SCHEMA_VERSION, build_excel_history_snapshot
    from backend.excel import store as excel_store
    from backend.excel import query as excel_query

    rep = None
    import glob
    import os
    from backend.excel import nl_query as nl
    for path in sorted(glob.glob('data/excel/*/representation.json')):
        r = excel_store.load_representation(os.path.basename(os.path.dirname(path)))
        if r and 10 <= r.total_rows <= 30:
            rep = r
            break
    if rep is None:
        pytest.skip('缺少真实 19 行 representation')

    llm = _llm()
    llm.client.content = json.dumps({'action': 'new_query', 'columns': ['Order ID'],
                                     'limit': 5}, ensure_ascii=False)
    catalog = [{'document_id': rep.document_id, 'filename': rep.filename,
                'file_type': rep.file_type, 'created_at': rep.created_at,
                'total_rows': rep.total_rows,
                'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                            'row_count': s.row_count, 'column_count': s.column_count,
                            'columns': s.column_names} for s in rep.sheets]}]
    outcome = asyncio.run(nl.run_nl_query('列出订单号', catalog, llm=llm, user_id=1,
                                          session_key='ok-case'))
    assert outcome['status'] == nl.STATUS_OK
    snap = build_excel_history_snapshot(outcome)
    assert snap is not None and snap['schema_version'] == EXCEL_HISTORY_SCHEMA_VERSION == 1
    assert snap['kind'] == 'result'
    assert llm.client.timeout == 30.0
