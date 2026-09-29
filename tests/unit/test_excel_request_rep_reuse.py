# -*- coding: utf-8 -*-
"""Stage 4 性能（request-local representation 复用）正式测试。

优化前（真实 instrumentation）：同一次 NL 请求内
  * 普通查询（表格 / 枚举 / 统计 / 分组）：``load_representation`` **3 次**
  * multi-step：**4 次**
优化后：**1 次**（同一请求内同一 document_id 只读盘一次）。

硬性边界（本文件同时是回归护栏）：
  * 只在**请求内**复用（无全局 dict / 无 TTL / 无 LRU / 无跨请求缓存）；
  * 不改变 user / document 归属校验（memo 只在同一 document_id 上复用同一份结果）；
  * 不改变任何查询语义：行数、total_matches、统计值、multi-step 计划、relaxed_filters、
    Excel 行 provenance 全部与优化前**逐字一致**。
"""
import asyncio
import glob
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from backend.excel import aggregate as ag
from backend.excel import multi_step as ms
from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from backend.excel.representation import WorkbookRepresentation

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CARRIER = 'Shipping Provider Name'


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------
def _load_big_real() -> Optional[WorkbookRepresentation]:
    for path in sorted(glob.glob(str(PROJECT_ROOT / 'data' / 'excel' / '*'
                                      / 'representation.json'))):
        try:
            rep = excel_store.load_representation(os.path.basename(os.path.dirname(path)))
        except Exception:  # noqa: BLE001
            continue
        if rep is not None and rep.total_rows > 300:
            return rep
    return None


@pytest.fixture(scope='module')
def big_rep() -> WorkbookRepresentation:
    rep = _load_big_real()
    if rep is None:
        pytest.skip('未找到 >300 行的真实 representation')
    return rep


def _catalog(rep: WorkbookRepresentation) -> List[Dict[str, Any]]:
    return [{
        'document_id': rep.document_id, 'filename': rep.filename,
        'file_type': rep.file_type, 'created_at': rep.created_at,
        'total_rows': rep.total_rows,
        'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                    'row_count': s.row_count, 'column_count': s.column_count,
                    'columns': s.column_names} for s in rep.sheets],
    }]


class _Counter:
    """统计 ``store.load_representation`` 的真实调用次数（并保留原行为）。"""

    def __init__(self, rep: WorkbookRepresentation):
        self.rep = rep
        self.calls: List[Optional[str]] = []

    def __call__(self, document_id, user_id=None):
        self.calls.append(document_id)
        return self.rep

    @property
    def count(self) -> int:
        return len(self.calls)


class _NoReuse(nl._RequestRepresentations):
    """基线模式：模拟优化前行为（每次都读盘），仅用于"结果等价"对照。"""

    def load(self, document_id):  # type: ignore[override]
        from backend.excel import store
        return store.load_representation(document_id)


def _run(rep, message: str, turn, monkeypatch, catalog=None, baseline=False,
         guards: bool = True):
    """执行一次 NL 请求，返回 (outcome, 读盘计数器)。

    ``guards=True`` 时同时给出确定性守卫返回值（复现真实链路：统计/分析守卫会再调一次
    LLM 解析器并可能直接执行），从而让"优化前读盘次数"与线上 instrumentation 一致。
    """
    counter = _Counter(rep)
    monkeypatch.setattr(excel_store, 'load_representation', counter)
    if baseline:
        monkeypatch.setattr(nl, '_RequestRepresentations', _NoReuse)

    async def fake_turn(llm, msg, cat, context=None, analysis_context=None):
        return turn
    monkeypatch.setattr(nl, 'llm_parse_turn', fake_turn)
    if guards:
        async def fake_agg(llm, msg, cat, context=None):
            return getattr(turn, 'aggregate', None)
        async def fake_ana(llm, msg, cat, context=None, analysis_context=None):
            return getattr(turn, 'analysis', None)
        monkeypatch.setattr(nl, 'llm_parse_aggregate', fake_agg)
        monkeypatch.setattr(nl, 'llm_parse_analysis', fake_ana)
    outcome = asyncio.run(nl.run_nl_query(
        message, catalog if catalog is not None else _catalog(rep),
        llm=object(), user_id=1, session_key='rep-reuse'))
    assert nl._REQUEST_REPS.get() is None, '请求结束后必须复位（绝不跨请求复用）'
    return outcome, counter


def _turn_table(columns=('Order ID', 'Shipping Provider Name'), limit=50):
    return nl.TurnIntent(action=nl.ACTION_NEW_QUERY,
                         intent=nl.intent_from_dict({'query_type': nl.INTENT_STRUCTURED,
                                                     'columns': list(columns),
                                                     'limit': limit}))


def _turn_aggregate(op=ag.OPERATION_COUNT, column=None, group_by=None, filters=None):
    return nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=op, column=column, group_by=list(group_by or []),
        filters=list(filters or [])))


def _turn_multi_step():
    return nl.TurnIntent(action=nl.ACTION_ANALYSIS, analysis=nl.AnalysisIntent(steps=[
        {'type': ms.STEP_GROUP_AGGREGATE, 'group_by': ['SKU ID'], 'operation': ag.OPERATION_SUM,
         'column': 'Order Amount', 'order_by': ag.ORDER_BY_AGGREGATE, 'order_dir': 'desc',
         'top_n': 3},
        {'type': ms.STEP_AGGREGATE, 'operation': ag.OPERATION_SUM,
         'source': ms.SOURCE_STEP_1, 'column': ms.INTERMEDIATE_VALUE_COLUMN}]))


def _stable(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


# ==========================================================================
# 1) 普通查询：3 -> 1，且结果不变
# ==========================================================================
def test_plain_table_query_loads_representation_once(big_rep, monkeypatch):
    out, counter = _run(big_rep, '列出订单号和物流商', _turn_table(limit=500), monkeypatch)
    assert counter.count == 1
    res = out['result']
    assert res['total_matches'] == big_rep.total_rows
    assert len(res['rows']) == big_rep.total_rows
    assert res['has_more'] is False


def test_full_enumeration_loads_once_and_keeps_every_row(big_rep, monkeypatch):
    """「全部 SKU」：行数不减、不去重、顺序与 representation 一致。"""
    # 真实 LLM 在"用户没给条数"时不会给 limit（limit_explicit=False）-> 枚举提升到 500
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY,
                         intent=nl.intent_from_dict({'query_type': nl.INTENT_STRUCTURED,
                                                     'columns': ['SKU ID']}))
    notes = nl.apply_enumeration_limit(turn, '列出所有SKU')      # 真实枚举提升（50 -> 500）
    assert notes and turn.intent.limit == nl.MAX_NL_LIMIT
    out, counter = _run(big_rep, '列出所有SKU', turn, monkeypatch)
    assert counter.count == 1
    res = out['result']
    sheet = big_rep.sheets[0]
    ci = sheet.column_names.index('SKU ID')
    expected = [row[ci] for row in sheet.rows]
    got = [row[0] for row in res['rows']]
    assert len(got) == len(expected) == big_rep.total_rows     # 不去重、不截断
    assert got == expected
    assert res['total_matches'] == big_rep.total_rows


def test_aggregate_loads_once_and_result_identical_to_baseline(big_rep, monkeypatch):
    turn = _turn_aggregate(filters=[{'column': CARRIER, 'operator': 'contains', 'value': 'SF'}])
    fast, counter = _run(big_rep, '物流商为SF的有几单？', turn, monkeypatch)
    assert counter.count == 1
    monkeypatch.undo()
    slow, slow_counter = _run(big_rep, '物流商为SF的有几单？', turn, monkeypatch, baseline=True)
    assert slow_counter.count >= 2                            # 基线确实重复读盘
    assert _stable(fast['aggregate']) == _stable(slow['aggregate'])
    assert fast['aggregate']['matched_rows'] == slow['aggregate']['matched_rows']
    assert (fast.get('relaxed_filters') or []) == (slow.get('relaxed_filters') or [])


def test_group_aggregate_loads_once(big_rep, monkeypatch):
    turn = _turn_aggregate(op=ag.OPERATION_SUM, column='Order Amount', group_by=[CARRIER])
    out, counter = _run(big_rep, '各物流商分别的订单金额汇总', turn, monkeypatch)
    assert counter.count == 1
    assert out['group_aggregate']['kind'] == 'group_aggregate'
    assert out['group_aggregate']['matched_row_runs']               # P2 成果保持
    assert out['group_aggregate']['matched_rows'] == big_rep.total_rows


# ==========================================================================
# 2) multi-step：4 -> 1，且两步计划/结果逐字不变
# ==========================================================================
def test_multi_step_loads_once_and_plan_unchanged(big_rep, monkeypatch):
    fast, counter = _run(big_rep, '订单金额最高的前3个SKU的销售额总和', _turn_multi_step(),
                         monkeypatch)
    assert counter.count == 1
    monkeypatch.undo()
    slow, slow_counter = _run(big_rep, '订单金额最高的前3个SKU的销售额总和', _turn_multi_step(),
                              monkeypatch, baseline=True)
    assert slow_counter.count >= 3                            # 优化前：3~4 次
    a, b = fast['multi_step'], slow['multi_step']
    assert _stable(a) == _stable(b)
    assert a['step2']['input_rows'] == b['step2']['input_rows']
    assert a['step2']['numeric_rows'] == b['step2']['numeric_rows']
    assert a['step1']['row_excel_spans'] == b['step1']['row_excel_spans']
    assert a['step1']['matched_row_runs'] == b['step1']['matched_row_runs']
    assert a['definition'] == b['definition']


def test_baseline_reproduces_pre_optimization_counts(big_rep, monkeypatch):
    """对照实验：关掉复用后，读盘次数回到优化前的 3 / 4 次。"""
    _out, counter = _run(big_rep, '列出订单号和物流商', _turn_table(limit=500), monkeypatch,
                         baseline=True)
    assert counter.count == 3
    monkeypatch.undo()
    _out2, counter2 = _run(big_rep, '订单金额最高的前3个SKU的销售额总和', _turn_multi_step(),
                           monkeypatch, baseline=True)
    assert counter2.count == 4


# ==========================================================================
# 3) request-local 边界：不跨请求复用、不在请求外生效
# ==========================================================================
def test_no_cross_request_reuse(big_rep, monkeypatch):
    counter = _Counter(big_rep)
    monkeypatch.setattr(excel_store, 'load_representation', counter)

    async def fake_turn(llm, msg, cat, context=None, analysis_context=None):
        return _turn_table(limit=500)
    monkeypatch.setattr(nl, 'llm_parse_turn', fake_turn)
    for _ in range(2):
        asyncio.run(nl.run_nl_query('列出订单号和物流商', _catalog(big_rep),
                                    llm=object(), user_id=1, session_key='twice'))
        assert nl._REQUEST_REPS.get() is None
    assert counter.count == 2          # 每个请求各读一次；不是进程级缓存


def test_load_rep_is_inert_outside_request(big_rep, monkeypatch):
    counter = _Counter(big_rep)
    monkeypatch.setattr(excel_store, 'load_representation', counter)
    assert nl._REQUEST_REPS.get() is None
    for _ in range(2):
        assert nl._load_rep(big_rep.document_id) is big_rep
    assert counter.count == 2          # 无请求上下文 -> 与旧行为一致（每次都读）


def test_memo_never_crosses_documents():
    memo = nl._RequestRepresentations()
    docs = {'docA': object(), 'docB': object()}
    calls: List[str] = []

    def fake_load(document_id, user_id=None):
        calls.append(document_id)
        return docs.get(document_id)
    import backend.excel.store as store_mod
    orig = store_mod.load_representation
    store_mod.load_representation = fake_load
    try:
        assert memo.load('docA') is docs['docA']
        assert memo.load('docB') is docs['docB']       # 不会把 A 当成 B
        assert memo.load('docA') is docs['docA']       # 同文档才复用
        assert memo.load('docA') is docs['docA']
    finally:
        store_mod.load_representation = orig
    assert calls == ['docA', 'docB']                   # 每个文档只读盘一次


def test_memo_does_not_cache_failures():
    memo = nl._RequestRepresentations()
    calls: List[str] = []
    import backend.excel.store as store_mod
    orig = store_mod.load_representation
    store_mod.load_representation = lambda document_id, user_id=None: calls.append(
        document_id) or None
    try:
        assert memo.load('missing') is None
        assert memo.load('missing') is None            # 失败不缓存（保持原语义）
    finally:
        store_mod.load_representation = orig
    assert calls == ['missing', 'missing']


# ==========================================================================
# 4) ownership / 非法文档：校验语义完全保留
# ==========================================================================
def test_owner_only_load_still_enforced(big_rep):
    """store 层的归属校验不被绕过（正确 user 拿到，错误 user 拿不到）。"""
    owner = big_rep.user_id
    assert excel_store.load_representation(big_rep.document_id, user_id=owner) is not None
    assert excel_store.load_representation(big_rep.document_id, user_id=(owner or 0) + 12345) is None


def test_other_user_catalog_cannot_reach_document(big_rep, monkeypatch):
    """别的用户的 catalog 为空 -> 仍然澄清（复用容器不会凭空引入文档）。"""
    counter = _Counter(big_rep)
    monkeypatch.setattr(excel_store, 'load_representation', counter)
    outcome = asyncio.run(nl.run_nl_query('列出订单号和物流商', [], llm=object(),
                                          user_id=99999, session_key='other'))
    assert outcome['status'] != nl.STATUS_OK
    assert counter.count == 0                      # 根本不读盘


def test_missing_representation_still_reports_error(monkeypatch):
    """catalog 里有条目但 representation 缺失 -> 保持既有错误（不因复用而改变）。"""
    calls: List[str] = []
    monkeypatch.setattr(excel_store, 'load_representation',
                        lambda document_id, user_id=None: calls.append(document_id) or None)

    async def fake_turn(llm, msg, cat, context=None, analysis_context=None):
        return _turn_table()
    monkeypatch.setattr(nl, 'llm_parse_turn', fake_turn)
    catalog = [{'document_id': 'no-such-doc', 'filename': '缺失.xlsx', 'file_type': 'xlsx',
                'created_at': '2026-09-29T00:00:00', 'total_rows': 0,
                'sheets': [{'sheet_name': 'S', 'sheet_index': 0, 'row_count': 0,
                            'column_count': 0, 'columns': []}]}]
    outcome = asyncio.run(nl.run_nl_query('列出订单号和物流商', catalog, llm=object(),
                                          user_id=1, session_key='missing'))
    assert outcome['status'] == nl.STATUS_ERROR
    assert '表示数据缺失' in outcome['message']
    # 失败**不缓存**（与优化前逐字一致）：该请求内每个读盘点都会各自尝试（共 3 处），
    # 而不是把 None 复用出去制造"假成功"。
    assert len(calls) >= 1 and set(calls) == {'no-such-doc'}


# ==========================================================================
# 5) 历史快照：复用了 representation 也不改变 snapshot 结构 / 内容
# ==========================================================================
def test_snapshot_unchanged_by_reuse(big_rep, monkeypatch):
    from backend.api.v1.rag import EXCEL_HISTORY_SCHEMA_VERSION, build_excel_history_snapshot
    fast, counter = _run(big_rep, '列出订单号和物流商', _turn_table(limit=500), monkeypatch)
    assert counter.count == 1
    monkeypatch.undo()
    slow, _c = _run(big_rep, '列出订单号和物流商', _turn_table(limit=500), monkeypatch,
                    baseline=True)
    snap_fast = build_excel_history_snapshot(fast)
    snap_slow = build_excel_history_snapshot(slow)
    assert snap_fast['schema_version'] == snap_slow['schema_version'] \
        == EXCEL_HISTORY_SCHEMA_VERSION == 1
    assert _stable(snap_fast) == _stable(snap_slow)


def test_multi_step_snapshot_unchanged_by_reuse(big_rep, monkeypatch):
    from backend.api.v1.rag import build_excel_history_snapshot
    fast, counter = _run(big_rep, '订单金额最高的前3个SKU的销售额总和', _turn_multi_step(),
                         monkeypatch)
    assert counter.count == 1
    monkeypatch.undo()
    slow, _c = _run(big_rep, '订单金额最高的前3个SKU的销售额总和', _turn_multi_step(),
                    monkeypatch, baseline=True)
    snap_fast = build_excel_history_snapshot(fast)
    snap_slow = build_excel_history_snapshot(slow)
    assert snap_fast['kind'] == 'multi_step'
    assert _stable(snap_fast) == _stable(snap_slow)
    assert snap_fast['payload']['step1']['matched_row_runs'] \
        == snap_slow['payload']['step1']['matched_row_runs']
