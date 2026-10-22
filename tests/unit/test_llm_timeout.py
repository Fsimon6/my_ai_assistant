# -*- coding: utf-8 -*-
"""Stage 4 / B2-A：LLM timeout 真正接线（配置值传到 transport 层）。

事实（B2-A 后）：
  * 全仓**只有一处** OpenAI client 创建：``llm_service.OpenAILikeLLM.__init__`` 的
    ``AsyncOpenAI(api_key=..., base_url=...)`` —— chat / streaming / Excel NL 解析 /
    character-specific 模型 / agent pipeline 全部共用这一个实例（``get_llm()` 单例 +
    ``LLMFactory.create_llm``）；
  * 超时唯一来源 = ``settings.LLM_TIMEOUT``（进程环境变量 > `.env` > 默认 30；范围 1..600）；
  * ``LLMConfig.timeout`` 仅保留为兼容字段，不再参与 transport（见 B2 用例）。

隔离约定（重要）：
  * 本文件在父 pytest 进程中**不导入任何 `backend.*` / `openai` / `httpx`**，
    避免缓存使用测试密钥构造的 Settings 单例；
  * 所有用例在**子进程**中执行：子进程先清除继承的真实配置，再注入
    `JWT_SECRET=unit-test-only-secret`（LLM 用例按需注入 `API_KEY=unit-test-only-key`）；
  * fake `AsyncOpenAI` 在子进程内、导入 backend 之前替换，仅捕获真实构造参数，
    不联网、不调用真实 Provider；
  * 断言基于子进程输出的结构化 JSON，而不是仅看退出码。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_JWT_SECRET = 'unit-test-only-secret'
TEST_API_KEY = 'unit-test-only-key'

_CLEAR_ENV = (
    'ENVIRONMENT', 'JWT_SECRET', 'DATABASE_URL', 'LLM_MODEL', 'LLM_TIMEOUT',
    'MAX_UPLOAD_SIZE', 'HOST', 'PORT', 'API_KEY', 'LLM_PROVIDER', 'LLM_BASE_URL',
    'EMBEDDING_MODEL',
)

#: 子进程公共头：fake client + 辅助函数（必须在导入 backend 之前替换 AsyncOpenAI）
PROBE_HEADER = r'''
import asyncio
import json
from types import SimpleNamespace

import httpx
import openai

CATALOG = [{
    'document_id': 'doc-1', 'filename': '一店.xlsx', 'file_type': 'xlsx',
    'created_at': '2026-09-30T00:00:00', 'total_rows': 19,
    'sheets': [{'sheet_name': 'OrderSKUList', 'sheet_index': 0, 'row_count': 19,
                'column_count': 2, 'columns': ['Order ID', 'Shipping Provider Name']}],
}]


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
            if piece is None:
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
    created = 0
    last_kwargs = {}

    def __init__(self, **kwargs):
        FakeAsyncOpenAI.created += 1
        FakeAsyncOpenAI.last_kwargs = dict(kwargs)
        self.kwargs = dict(kwargs)
        self.calls = []
        self.emb_calls = []
        self.raise_exc = None
        self.content = '{}'
        self.stream_pieces = []
        self.chat = SimpleNamespace(completions=_ChatCompletions(self))
        self.embeddings = _Embeddings(self)

    @property
    def timeout(self):
        return self.kwargs.get('timeout')


openai.AsyncOpenAI = FakeAsyncOpenAI


def _llm(model='test-model', api_key='test-key', **kw):
    from backend.services.llm_service import LLMConfig, OpenAILikeLLM
    return OpenAILikeLLM(LLMConfig(provider='openai', api_key=api_key,
                                   base_url='http://fake.local/v1', model=model, **kw))


def _consume(agen):
    async def _run():
        return [chunk async for chunk in agen]
    return asyncio.run(_run())


def _timeout_error():
    return openai.APITimeoutError(
        request=httpx.Request('POST', 'http://fake.local/v1/chat'))


def emit(data):
    print('PROBE_JSON:' + json.dumps(data, ensure_ascii=False, default=str))
'''


def _run(code: str, env_extra=None):
    env = dict(os.environ)
    for key in _CLEAR_ENV:
        env.pop(key, None)
    env['JWT_SECRET'] = TEST_JWT_SECRET
    env.update(env_extra or {})
    env['PYTHONPATH'] = str(REPO_ROOT)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    return subprocess.run(
        [sys.executable, '-c', code], cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300,
    )


def _probe(scenario: str, env_extra=None) -> dict:
    proc = _run(PROBE_HEADER + scenario, env_extra)
    assert proc.returncode == 0, proc.stderr[-3000:]
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith('PROBE_JSON:'):
            return json.loads(line[len('PROBE_JSON:'):])
    raise AssertionError(
        'probe 未输出 PROBE_JSON；stdout=%r stderr=%r'
        % (proc.stdout[-800:], proc.stderr[-800:])
    )


# ==========================================================================
# A：全局 client
# ==========================================================================
def test_a_global_client_uses_configured_timeout():
    data = _probe(
        r'''
from backend.config.settings import settings
from backend.services.llm_service import LLMFactory

llm = LLMFactory.from_env()
emit({
    'expected': float(settings.LLM_TIMEOUT),
    'request_timeout': llm.request_timeout,
    'client_timeout': FakeAsyncOpenAI.last_kwargs.get('timeout'),
    'has_base_url': bool(FakeAsyncOpenAI.last_kwargs.get('base_url')),
    'has_model': bool(llm.config.model),
})
''',
        {'API_KEY': TEST_API_KEY, 'LLM_PROVIDER': 'openai',
         'LLM_MODEL': 'test-model', 'LLM_BASE_URL': 'http://fake.local/v1'},
    )
    assert data['expected'] == 30.0
    assert data['request_timeout'] == data['expected']
    assert data['client_timeout'] == data['expected']   # 真正传给了 transport
    assert data['has_base_url'] is True                 # base_url 规则未变
    assert data['has_model'] is True                    # model 解析未变


def test_a2_default_config_timeout_is_30():
    data = _probe(
        r'''
from backend.config.settings import Settings
from backend.services.llm_service import LLMConfig, resolve_request_timeout

cfg = LLMConfig(provider='openai', api_key='k')
emit({
    'config_timeout': cfg.timeout,
    'resolved': resolve_request_timeout(cfg),
    'settings_default': Settings.model_fields['LLM_TIMEOUT'].default,
})
'''
    )
    assert data['config_timeout'] == 30
    assert data['resolved'] == 30.0
    assert data['settings_default'] == 30


# ==========================================================================
# B：character-specific（只改参数，不改 model 解析规则）
# ==========================================================================
def test_b_character_client_keeps_model_and_key_and_gets_timeout():
    data = _probe(
        r'''
llm = _llm(model='char-model-xyz', api_key='char-key')
emit({
    'client_timeout': FakeAsyncOpenAI.last_kwargs.get('timeout'),
    'client_api_key': FakeAsyncOpenAI.last_kwargs.get('api_key'),
    'model': llm.config.model,
    'request_timeout': llm.request_timeout,
})
'''
    )
    assert data['client_timeout'] == 30.0
    assert data['client_api_key'] == 'char-key'    # 角色 key 原样
    assert data['model'] == 'char-model-xyz'       # 角色 model 原样
    assert data['request_timeout'] == 30.0


def test_b2_settings_timeout_wins():
    """B2-A：`settings.LLM_TIMEOUT` 是唯一来源，并真正传给 transport。"""
    data = _probe(
        r'''
from backend.config.settings import settings
from backend.services.llm_service import LLMConfig, resolve_request_timeout

resolved = resolve_request_timeout(LLMConfig(provider='openai', api_key='k'))
llm = _llm()
emit({
    'settings_timeout': settings.LLM_TIMEOUT,
    'resolved': resolved,
    'request_timeout': llm.request_timeout,
    'client_timeout': FakeAsyncOpenAI.last_kwargs.get('timeout'),
})
''',
        {'LLM_TIMEOUT': '7'},
    )
    assert data['settings_timeout'] == 7
    assert data['resolved'] == 7.0
    assert data['request_timeout'] == 7.0
    assert data['client_timeout'] == 7.0


def test_b3_settings_wins_over_llmconfig_timeout():
    """`LLMConfig.timeout` 仅保留为兼容字段，不再能覆盖 Settings。"""
    data = _probe(
        r'''
from backend.services.llm_service import LLMConfig, resolve_request_timeout
emit({
    'resolved': resolve_request_timeout(
        LLMConfig(provider='openai', api_key='k', timeout=12)),
})
''',
        {'LLM_TIMEOUT': '9'},
    )
    assert data['resolved'] == 9.0


# ==========================================================================
# C：streaming / 非流式都仍可用
# ==========================================================================
def test_c_streaming_works_with_timeout():
    data = _probe(
        r'''
from backend.config.settings import settings

llm = _llm()
llm.client.stream_pieces = ['你', None, '好']       # 含空 choices 结束帧
chunks = _consume(llm.chat_completion(
    [{'role': 'user', 'content': 'hi'}], stream=True))
emit({
    'chunks': chunks,
    'client_timeout': llm.client.timeout,
    'settings_timeout': float(settings.LLM_TIMEOUT),
    'stream_flag': llm.client.calls[0].get('stream'),
})
'''
    )
    assert data['chunks'] == ['你', '好']
    assert data['client_timeout'] == data['settings_timeout'] == 30.0
    assert data['stream_flag'] is True


def test_c2_non_streaming_works_with_timeout():
    data = _probe(
        r'''
from backend.config.settings import settings

llm = _llm()
llm.client.content = '完整答复'
chunks = _consume(llm.chat_completion(
    [{'role': 'user', 'content': 'hi'}], stream=False))
emit({
    'chunks': chunks,
    'client_timeout': llm.client.timeout,
    'settings_timeout': float(settings.LLM_TIMEOUT),
    'call_has_stream': 'stream' in llm.client.calls[0],
})
'''
    )
    assert data['chunks'] == ['完整答复']
    assert data['client_timeout'] == data['settings_timeout'] == 30.0
    assert data['call_has_stream'] is False      # 非流式：与既有调用形态一致


# ==========================================================================
# D：Excel NL 解析（含 multi-step）走同一个带 timeout 的 client
# ==========================================================================
def test_d_excel_nl_turn_parse_uses_timeout_client():
    data = _probe(
        r'''
import asyncio
import json as _json

from backend.config.settings import settings
from backend.excel import nl_query as nl

llm = _llm()
llm.client.content = _json.dumps({'action': 'new_query',
                                  'columns': ['Order ID'], 'limit': 10},
                                 ensure_ascii=False)
turn = asyncio.run(nl.llm_parse_turn(llm, '列出订单号', CATALOG, None))
emit({
    'action': turn.action,
    'expected_action': nl.ACTION_NEW_QUERY,
    'columns': list(turn.intent.columns),
    'client_timeout': llm.client.timeout,
    'settings_timeout': float(settings.LLM_TIMEOUT),
    'temperature': llm.client.calls[0].get('temperature'),
})
'''
    )
    assert data['action'] == data['expected_action']
    assert data['columns'] == ['Order ID']
    assert data['client_timeout'] == data['settings_timeout'] == 30.0
    assert data['temperature'] == 0.0


def test_d2_excel_nl_multi_step_parse_uses_same_client():
    data = _probe(
        r'''
import asyncio
import json as _json

from backend.config.settings import settings
from backend.excel import nl_query as nl

llm = _llm()
llm.client.content = _json.dumps({'steps': [
    {'type': 'group_aggregate', 'group_by': ['SKU ID'], 'operation': 'sum',
     'column': 'Order Amount', 'top_n': 3},
    {'type': 'aggregate', 'operation': 'sum', 'source': 'step_1'}]},
    ensure_ascii=False)
analysis = asyncio.run(nl.llm_parse_analysis(
    llm, '前3个SKU的销售额总和', CATALOG, None, None))
emit({
    'steps_len': len(analysis.steps),
    'created': FakeAsyncOpenAI.created,
    'client_timeout': llm.client.timeout,
    'settings_timeout': float(settings.LLM_TIMEOUT),
})
'''
    )
    assert data['steps_len'] == 2
    assert data['created'] == 1                 # 全程只有一个 client 实例
    assert data['client_timeout'] == data['settings_timeout'] == 30.0


# ==========================================================================
# E：embedding 与 chat 共用同一 client
# ==========================================================================
def test_e_embedding_uses_same_client_and_model_unhanged():
    data = _probe(
        r'''
import asyncio

llm = _llm(embedding_model='embed-xyz')
vecs = asyncio.run(llm.generate_embeddings(['a']))
emit({
    'vecs': vecs,
    'emb_calls': len(llm.client.emb_calls),
    'model': llm.client.emb_calls[0].get('model'),
    'created': FakeAsyncOpenAI.created,
})
'''
    )
    assert data['vecs'] == [[0.1, 0.2]]
    assert data['emb_calls'] == 1
    assert data['model'] == 'embed-xyz'         # 模型解析未变
    assert data['created'] == 1                 # 未新建第二个 client


# ==========================================================================
# timeout 异常：传播 + 既有分类 + Excel NL 不产生半截 snapshot
# ==========================================================================
def test_timeout_error_propagates_and_is_classified():
    data = _probe(
        r'''
from backend.utils import provider_errors as pe

llm = _llm()
llm.client.raise_exc = _timeout_error()
raised = None
try:
    _consume(llm.chat_completion(
        [{'role': 'user', 'content': 'hi'}], stream=True))
except openai.APITimeoutError:
    raised = 'APITimeoutError'
emit({
    'raised': raised,
    'error_code': pe.classify_llm_error(_timeout_error()).error_code,
    'expected_code': 'LLM_' + pe.TIMEOUT,
})
'''
    )
    assert data['raised'] == 'APITimeoutError'
    assert data['error_code'] == data['expected_code'] == 'LLM_TIMEOUT'


def test_excel_nl_timeout_returns_error_without_partial_snapshot():
    data = _probe(
        r'''
import asyncio

from backend.api.v1.rag import build_excel_history_snapshot
from backend.excel import nl_query as nl
from backend.utils import provider_errors as pe

llm = _llm()
llm.client.raise_exc = _timeout_error()
outcome = asyncio.run(nl.run_nl_query(
    '列出订单号和物流商', CATALOG, llm=llm, user_id=1, session_key='timeout-case'))
emit({
    'status': outcome.get('status'),
    'expected_status': nl.STATUS_ERROR,
    'error_code': outcome.get('error_code'),
    'expected_code': 'LLM_' + pe.TIMEOUT,
    'result_is_none': outcome.get('result') is None,
    'snapshot_is_none': build_excel_history_snapshot(outcome) is None,
    'has_query': bool(outcome.get('query')),
})
'''
    )
    assert data['status'] == data['expected_status']
    assert data['error_code'] == data['expected_code']
    assert data['result_is_none'] is True
    assert data['snapshot_is_none'] is True     # 绝不写半截快照
    assert data['has_query'] is False


def test_success_path_history_shape_unchanged():
    """成功路径：snapshot 结构照旧（timeout 只在 transport 层，不改变契约）。"""
    data = _probe(
        r'''
import asyncio
import glob
import json as _json
import os

from backend.api.v1.rag import (EXCEL_HISTORY_SCHEMA_VERSION,
                                build_excel_history_snapshot)
from backend.config.settings import settings
from backend.excel import nl_query as nl
from backend.excel import store as excel_store

rep = None
for path in sorted(glob.glob('data/excel/*/representation.json')):
    candidate = excel_store.load_representation(
        os.path.basename(os.path.dirname(path)))
    if candidate and 10 <= candidate.total_rows <= 30:
        rep = candidate
        break

if rep is None:
    emit({'skipped': True})
else:
    llm = _llm()
    llm.client.content = _json.dumps({'action': 'new_query',
                                      'columns': ['Order ID'], 'limit': 5},
                                     ensure_ascii=False)
    catalog = [{'document_id': rep.document_id, 'filename': rep.filename,
                'file_type': rep.file_type, 'created_at': rep.created_at,
                'total_rows': rep.total_rows,
                'sheets': [{'sheet_name': s.sheet_name,
                            'sheet_index': s.sheet_index,
                            'row_count': s.row_count,
                            'column_count': s.column_count,
                            'columns': s.column_names} for s in rep.sheets]}]
    outcome = asyncio.run(nl.run_nl_query(
        '列出订单号', catalog, llm=llm, user_id=1, session_key='ok-case'))
    snap = build_excel_history_snapshot(outcome)
    emit({
        'skipped': False,
        'status': outcome.get('status'),
        'expected_status': nl.STATUS_OK,
        'schema_version': snap.get('schema_version') if snap else None,
        'expected_schema': EXCEL_HISTORY_SCHEMA_VERSION,
        'kind': snap.get('kind') if snap else None,
        'client_timeout': llm.client.timeout,
        'settings_timeout': float(settings.LLM_TIMEOUT),
    })
'''
    )
    if data.get('skipped'):
        pytest.skip('缺少真实 19 行 representation（隔离副本未包含 data/excel）')
    assert data['status'] == data['expected_status']
    assert data['schema_version'] == data['expected_schema'] == 1
    assert data['kind'] == 'result'
    assert data['client_timeout'] == data['settings_timeout'] == 30.0
