# -*- coding: utf-8 -*-
"""第 3 项「结果与可解释性」：**生产调用契约**测试（真实调用点 → SQLite → 读回）。

背景（2026-09-28 真实浏览器验收失败的根因）：
  端点曾以 ``_persist_excel_turn(..., {'excel': snapshot})`` 调用，而 helper 又包一层
  → 落库成 ``meta_info['excel']['excel']``（双层），前端 ``schema_version`` 读不到，
  历史卡片无法恢复。此前的测试只验证了 helper 单独调用的形状，**未覆盖生产调用点的参数契约**。

本文件用与端点**完全相同**的写入口 ``persist_excel_turn``（真实 pipeline 产出的 outcome），
写入**临时 SQLite**，再经 ``get_history`` 读回，直接断言最终 DB 行的结构。
不触碰用户 DB、不调用 LLM。
"""
import asyncio
import os
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api.v1.rag import (
    build_excel_history_snapshot,
    build_excel_turn_meta,
    persist_excel_turn,
)
from backend.database.base import Base
from backend.excel import aggregate as ag
from backend.excel import multi_step as ms
from backend.excel import nl_query as nq
from backend.excel import store as excel_store
from backend.models import character as _models          # noqa: F401  （注册表）
from backend.services import conversation_service as conv_mod
from backend.services.conversation_service import (
    SOURCE_CHAT, SOURCE_EXCEL, SOURCE_RAG, conversation_service as conv,
)

USER = int(os.environ.get('P_USER', '87'))
MODEL = 'contract-test-model'


class _FakeLLM:
    pass


# ---------------------------------------------------------------------------
# 真实 pipeline（离线确定性注入，真实数据）-> outcome
# ---------------------------------------------------------------------------
def _agg_turn(**kw) -> 'nq.TurnIntent':
    return nq.TurnIntent(action=nq.ACTION_AGGREGATE, aggregate=nq.AggregateIntent(**kw))


def _nq_turn(columns) -> 'nq.TurnIntent':
    return nq.TurnIntent(action=nq.ACTION_NEW_QUERY, intent=nq.intent_from_dict(
        {'query_type': nq.INTENT_STRUCTURED, 'columns': list(columns)}))


def _an_turn() -> 'nq.TurnIntent':
    return nq.TurnIntent(action=nq.ACTION_ANALYSIS, analysis=nq.AnalysisIntent(steps=[
        {'type': ms.STEP_GROUP_AGGREGATE, 'group_by': ['SKU ID'],
         'operation': ag.OPERATION_SUM, 'column': 'Order Amount',
         'order_by': ag.ORDER_BY_AGGREGATE, 'order_dir': 'desc', 'top_n': 3, 'filters': []},
        {'type': ms.STEP_AGGREGATE, 'operation': ag.OPERATION_SUM,
         'source': ms.SOURCE_STEP_1, 'column': ms.INTERMEDIATE_VALUE_COLUMN}]))


CASES = [
    ('result', '一店的订单号和金额', _nq_turn(['Order ID', 'Order Amount'])),
    ('aggregate', '订单金额总和',
     _agg_turn(operation=ag.OPERATION_SUM, column='Order Amount', group_by=[], filters=[])),
    ('group_aggregate', '各物流商的订单金额总和',
     _agg_turn(operation=ag.OPERATION_SUM, column='Order Amount',
               group_by=['Shipping Provider Name'], filters=[],
               top_n=3, order_by=ag.ORDER_BY_AGGREGATE, order_dir='desc')),
    ('multi_step', '订单金额最高的前3个SKU的销售额总和', _an_turn()),
]


async def _run_pipeline(msg, turn, key):
    catalog = nq.dedupe_catalog_by_filename(excel_store.list_excel_catalog(USER))
    orig = nq.llm_parse_turn

    async def fake(llm, m, c, context=None, analysis_context=None):
        return turn
    nq.llm_parse_turn = fake
    try:
        return await nq.run_nl_query(msg, catalog, llm=_FakeLLM(), user_id=USER, session_key=key)
    finally:
        nq.llm_parse_turn = orig


@pytest.fixture()
def temp_db(monkeypatch):
    """临时 SQLite（与生产同 schema），把 conversation_service 的会话工厂指向它。"""
    tmpdir = tempfile.mkdtemp(prefix='hist_contract_')
    engine = create_engine('sqlite:///%s' % os.path.join(tmpdir, 'contract.db'))
    Base.metadata.create_all(engine)
    monkeypatch.setattr(conv_mod, 'SessionLocal',
                        sessionmaker(bind=engine, autocommit=False, autoflush=False))
    conv_id = conv.get_or_create_conversation_id(USER, 1)
    yield conv_id, engine
    engine.dispose()


def _meta_of(history, role):
    return [h['meta_info'] for h in history if h['role'] == role]


# ---------------------------------------------------------------------------
# 1-4：四种 kind 的"生产调用 → DB → 读回"契约
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('kind,msg,turn', CASES, ids=[c[0] for c in CASES])
def test_production_call_contract_single_layer(temp_db, kind, msg, turn):
    conv_id, _engine = temp_db
    out = asyncio.run(_run_pipeline(msg, turn, 'contract-%s' % kind))
    snap = build_excel_history_snapshot(out)
    assert snap is not None and snap['kind'] == kind

    # 与端点完全相同的写入口（user 无快照 / assistant 带快照）
    persist_excel_turn(conv_id, 'user', msg, MODEL, None)
    persist_excel_turn(conv_id, 'assistant', out.get('message') or '完成', MODEL, snap)

    history = conv.get_history(conv_id)
    a_meta = _meta_of(history, 'assistant')[-1]
    u_meta = _meta_of(history, 'user')[-1]

    # 最终 DB 行断言（直接针对读回值）
    assert a_meta['source'] == 'excel'
    assert a_meta['excel']['schema_version'] == 1
    assert a_meta['excel']['kind'] == kind
    assert 'excel' not in a_meta['excel'], '❌ 双层包裹回归：meta_info["excel"] 里又出现了 "excel"'
    # 展示必需字段随快照进入 DB（抽查 kind 相关字段）
    payload = a_meta['excel']['payload']
    assert payload['sheet_name']
    assert a_meta['excel']['document']['filename']
    # user 侧无快照（旧契约不变）
    assert u_meta == {'source': 'excel'}


def test_production_contract_via_endpoint_closure_shape(temp_db):
    """端点闭包的调用形状（snapshot 位置传本体）在真实 DB 上等价于单层结构。"""
    conv_id, _engine = temp_db
    out = asyncio.run(_run_pipeline('订单金额总和', CASES[1][2], 'contract-closure'))
    snap = build_excel_history_snapshot(out)
    # 模拟端点闭包的委托调用：persist_excel_turn(conv_id, role, content, model, snapshot)
    persist_excel_turn(conv_id, 'assistant', '统计完成', MODEL, snap)
    a_meta = _meta_of(conv.get_history(conv_id), 'assistant')[-1]
    assert sorted(a_meta.keys()) == ['excel', 'source']
    assert 'excel' not in a_meta['excel']
    assert a_meta['excel']['kind'] == 'aggregate'


# ---------------------------------------------------------------------------
# 5：其它调用者（chat / RAG）的 meta 语义不被破坏 + 旧历史契约
# ---------------------------------------------------------------------------
def test_other_source_meta_semantics_unchanged(temp_db):
    """chat / RAG 写库仍只有 source，且**不会**出现 excel 键（本次修复不波及其它调用者）。"""
    conv_id, _engine = temp_db
    conv.append_message(conv_id, 'user', '普通聊天', MODEL, meta_info={'source': SOURCE_CHAT})
    conv.append_message(conv_id, 'assistant', '普通回复', MODEL, meta_info={'source': SOURCE_CHAT})
    conv.append_message(conv_id, 'user', '知识库问题', MODEL, meta_info={'source': SOURCE_RAG})
    history = conv.get_history(conv_id)
    for meta in [h['meta_info'] for h in history]:
        assert 'excel' not in (meta or {})
    assert [h['meta_info']['source'] for h in history] == [SOURCE_CHAT, SOURCE_CHAT, SOURCE_RAG]


def test_no_snapshot_keeps_old_history_contract(temp_db):
    """无结构化结果（澄清/报错）-> 只写 source（旧历史行为，前端保持纯文字）。"""
    conv_id, _engine = temp_db
    assert build_excel_turn_meta(None) == {'source': SOURCE_EXCEL}
    persist_excel_turn(conv_id, 'assistant', '需要补充信息', MODEL, None)
    meta = _meta_of(conv.get_history(conv_id), 'assistant')[-1]
    assert meta == {'source': 'excel'}


def test_empty_content_or_missing_conv_is_noop(temp_db):
    """守卫：空内容 / 无会话 -> 不写库（与既有行为一致）。"""
    conv_id, _engine = temp_db
    persist_excel_turn(conv_id, 'assistant', '', MODEL, {'schema_version': 1, 'kind': 'result'})
    persist_excel_turn(None, 'assistant', '内容', MODEL, {'schema_version': 1, 'kind': 'result'})
    assert conv.get_history(conv_id) == []


def test_source_filter_still_works_with_snapshot(temp_db):
    """exclude_sources=[excel] 仍整轮丢弃（快照的存在不影响过滤语义）。"""
    conv_id, _engine = temp_db
    persist_excel_turn(conv_id, 'user', '一店的订单', MODEL, None)
    persist_excel_turn(conv_id, 'assistant', '查询完成', MODEL,
                       {'schema_version': 1, 'kind': 'result', 'payload': {'sheet_name': 'S'}})
    assert len(conv.get_history(conv_id, exclude_sources=[SOURCE_EXCEL])) == 0
    assert len(conv.get_history(conv_id)) == 2
