# -*- coding: utf-8 -*-
"""Stage 2A-P0：**单位级 TOP-N**（「<度量>最高的前N个<单位>」）语义测试。

背景（2026-09-24 实测，同一句话、同一模型、同一天两种错答）：
    「订单金额最高的前3个订单」
      ① LLM 给 `new_query + limit=3`（query 内**无任何排序**）-> 返回表内前 3 行却 status=ok
      ② LLM 自行 clarify
    两者都不是用户要的"金额最高的 3 个订单"。

正确语义（沿用项目既有口径「X 金额最高的 N 个 Y」）：
    SUM(<度量>) + GROUP BY <单位列> + ORDER BY aggregate_value DESC + TOP-N

本文件为**纯确定性测试**（不调用 LLM / 不消耗额度）：
  - 直接测确定性规则 `_deterministic_unit_topn_turn` 的判定矩阵；
  - 用 monkeypatch 注入"LLM 的真实错答"，验证管线最终产出正确语义与真实 Top-3。
"""
import asyncio
from typing import Any, Dict, List

import pytest

from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from backend.excel.representation import (
    ColumnMeta, SheetRepresentation, WorkbookRepresentation,
)

# ---------------------------------------------------------------------------
# 合成表：**一个订单包含多行 SKU**（行级结果 != 订单级结果，用于区分 LIMIT 行与分组 TOP-N）
#   O1: 10 + 20 = 30（2 行）      O2: 25      O3: 15      O4: 5
#   金额 GT(订单级) = [O1=30, O2=25, O3=15]
#   金额 GT(行级前3行按原始序) = [O1=10, O1=20, O2=25]  <- 与上面不同
# ---------------------------------------------------------------------------
_COLS = ['Order ID', 'Order Amount', 'Quantity', 'Shipping Provider Name']
_ROWS = [
    ['O1', '10', '1', 'SF'],
    ['O1', '20', '1', 'SF'],
    ['O2', '25', '5', 'Yanwen Express'],
    ['O3', '15', '2', 'Yanwen Express'],
    ['O4', '5', '1', 'SF'],
]


def _synth_rep() -> WorkbookRepresentation:
    cols = [ColumnMeta(name=n, index=i, excel_column=i + 1,
                       excel_column_letter=chr(ord('A') + i), dtype='string')
            for i, n in enumerate(_COLS)]
    sheet = SheetRepresentation(
        sheet_name='OrderSKUList', sheet_index=0, header_mode='single', columns=cols,
        rows=[list(r) for r in _ROWS],
        row_excel_numbers=list(range(2, 2 + len(_ROWS))),
        row_count=len(_ROWS), column_count=len(_COLS))
    return WorkbookRepresentation(schema_version='1.0', document_id='ord-unit',
                                  user_id=1, filename='unit_topn.xlsx',
                                  file_type='xlsx', parser='xlsx', sheet_count=1,
                                  sheets=[sheet])


@pytest.fixture()
def rep() -> WorkbookRepresentation:
    return _synth_rep()


def _catalog(r: WorkbookRepresentation) -> List[Dict[str, Any]]:
    return [{
        'document_id': r.document_id, 'filename': r.filename, 'file_type': r.file_type,
        'created_at': '2026-09-24T00:00:00', 'total_rows': r.total_rows,
        'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                    'row_count': s.row_count, 'column_count': s.column_count,
                    'columns': s.column_names} for s in r.sheets],
    }]


def _patch_store(monkeypatch, r: WorkbookRepresentation) -> None:
    """让 `_resolve_sheet_for_message` 能读到合成表（规则测试需要真实 schema）。"""
    monkeypatch.setattr(excel_store, 'load_representation',
                        lambda document_id, user_id=None: r)


class _FakeLLM:
    """占位 LLM：管线的 LLM 调用点全部被 monkeypatch。"""


def _run_with_llm_turn(rep: WorkbookRepresentation, message: str, turn,
                       monkeypatch, session_key: str = 'unit-topn') -> Dict[str, Any]:
    """跑真实管线，但把 `llm_parse_turn` 固定成给定的（LLM 的）错答。"""
    orig_load = excel_store.load_representation
    orig_parse = nl.llm_parse_turn
    excel_store.load_representation = lambda document_id, user_id=None: rep

    async def _fake_parse(llm, msg, catalog, context=None, analysis_context=None):
        return turn

    nl.llm_parse_turn = _fake_parse
    try:
        return asyncio.run(nl.run_nl_query(
            message, _catalog(rep), llm=_FakeLLM(),
            user_id=1, session_key=session_key,
        ))
    finally:
        nl.llm_parse_turn = orig_parse
        excel_store.load_representation = orig_load


# ===========================================================================
# 1) 确定性规则的判定矩阵（不经过管线）
# ===========================================================================
FIRE_CASES = {
    '订单金额最高的前3个订单': ('Order Amount', 3),
    '订单金额排名前3的订单': ('Order Amount', 3),
    '订单数量最多的前2个订单': ('Quantity', 2),
}
SKIP_CASES = [
    # A：无明确指标 -> 必须澄清（"订单"不得变成全局维度）
    '排名前三的订单', '查看排名前三订单', '前3名订单', '哪些订单排名前三',
    '排名前5的订单', '排行前3的订单', '第1名到第3名订单', '哪几个订单排名最高',
    # C：没有"单位位置" -> 保持澄清
    '按订单金额排名前三', '按数量排名前三',
    # 有维度别名 -> 交给既有分组排行（不得被订单级规则抢走）
    '订单最多的前3个物流商', '排名前三的物流商',
    # 两步分析 -> 交给 analysis 路径
    '订单金额最高的前3个订单的总和是多少',
]


@pytest.mark.parametrize('message,col,n', [(m, c, n) for m, (c, n) in FIRE_CASES.items()])
def test_unit_topn_rule_fires(message, col, n, rep, monkeypatch):
    _patch_store(monkeypatch, rep)
    sig = nl.nl_norm.detect_signals(message)
    turn = nl._deterministic_unit_topn_turn(message, _catalog(rep), sig, None)
    assert turn is not None, message
    agg = turn.aggregate
    assert turn.action == nl.ACTION_AGGREGATE
    assert agg.operation == 'sum'
    assert agg.column == col
    assert agg.group_by == ['Order ID']
    assert agg.top_n == n
    assert agg.order_by == 'aggregate_value'
    assert agg.order_dir == 'desc'


@pytest.mark.parametrize('message', SKIP_CASES)
def test_unit_topn_rule_skips(message, rep, monkeypatch):
    _patch_store(monkeypatch, rep)
    sig = nl.nl_norm.detect_signals(message)
    assert nl._deterministic_unit_topn_turn(message, _catalog(rep), sig, None) is None, message


# ===========================================================================
# 2) 管线：LLM 给 `new_query + limit=3`（真实错答①）-> 必须被纠正为订单级 TOP-N
# ===========================================================================
def test_pipeline_overrides_new_query_limit(rep, monkeypatch):
    wrong = nl.TurnIntent(action=nl.ACTION_NEW_QUERY,
                          intent=nl.intent_from_dict({
                              'query_type': nl.INTENT_STRUCTURED,
                              'filters': [], 'limit': 3}))
    out = _run_with_llm_turn(rep, '订单金额最高的前3个订单', wrong, monkeypatch,
                             session_key='unit-b1-a')
    assert out['status'] == 'ok', out.get('message')
    turn = out['turn']
    assert turn['action'] == 'aggregate'
    agg = turn['aggregate']
    assert agg['operation'] == 'sum' and agg['column'] == 'Order Amount'
    assert list(agg['group_by']) == ['Order ID'] and agg['top_n'] == 3
    g = out['group_aggregate']
    assert g['operation'] == 'sum' and g['column'] == 'Order Amount'
    assert [c['name'] for c in g['group_by']] == ['Order ID']
    assert g['top_n'] == 3 and g['returned_groups'] == 3 and g['sorted'] is True
    # 真实 Top-3（订单级）：O1=30 > O2=25 > O3=15
    assert [r['group_key'] for r in g['rows']] == [['O1'], ['O2'], ['O3']]
    assert [r['value'] for r in g['rows']] == [30, 25, 15]


# ===========================================================================
# 3) 管线：LLM 直接 clarify（真实错答②）-> 同样必须被纠正（保证确定性）
# ===========================================================================
def test_pipeline_overrides_clarify(rep, monkeypatch):
    wrong = nl.TurnIntent(action=nl.ACTION_CLARIFY, clarification='请提供分组维度')
    out = _run_with_llm_turn(rep, '订单金额最高的前3个订单', wrong, monkeypatch,
                             session_key='unit-b1-b')
    assert out['status'] == 'ok', out.get('message')
    g = out['group_aggregate']
    assert [r['group_key'] for r in g['rows']] == [['O1'], ['O2'], ['O3']]
    assert [r['value'] for r in g['rows']] == [30, 25, 15]


# ===========================================================================
# 4) 数量口径：SUM(Quantity) + GROUP BY Order ID + TOP-N
# ===========================================================================
def test_pipeline_quantity_unit_topn(rep, monkeypatch):
    wrong = nl.TurnIntent(action=nl.ACTION_CLARIFY)
    out = _run_with_llm_turn(rep, '订单数量最多的前3个订单', wrong, monkeypatch,
                             session_key='unit-b1-q')
    assert out['status'] == 'ok', out.get('message')
    g = out['group_aggregate']
    assert g['operation'] == 'sum' and g['column'] == 'Quantity'
    assert [c['name'] for c in g['group_by']] == ['Order ID']
    # 数量 GT：O2=5 > O1=2 > O3=2
    assert [r['group_key'] for r in g['rows']] == [['O2'], ['O1'], ['O3']]
    assert [r['value'] for r in g['rows']] == [5, 2, 2]


# ===========================================================================
# 5) A 组回归：无明确指标排名 -> 仍必须澄清（LLM 猜出 Order Amount 也不得执行）
# ===========================================================================
@pytest.mark.parametrize('message', [
    '查看排名前三订单', '排名前三的订单', '前3名订单', '哪些订单排名前三',
    '排名前5的订单', '排行前3的订单', '第1名到第3名订单', '哪几个订单排名最高',
])
def test_no_metric_rank_still_clarifies(message, rep, monkeypatch):
    guessed = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation='sum', column='Order Amount', group_by=[], top_n=3))
    out = _run_with_llm_turn(rep, message, guessed, monkeypatch,
                             session_key='unit-a-%s' % abs(hash(message)))
    assert out['status'] == 'clarify', (message, out.get('status'), out.get('aggregate'))
    assert not out.get('aggregate') and not out.get('group_aggregate')


# ===========================================================================
# 5b) 「前N名/前N位」= **排名语义**（A3 修正，2026-09-24）-> 无指标必须澄清
#     依据：prompt 第 6 条把行级截取定义为「前N条/前N行」；第 13 条与「只要前 N 名而无
#     第二步汇总 -> action=aggregate」都把「前N名」当排名；analysis 路径对"有 top_n 无
#     order_by"本来就是澄清。修正前它会降级为「行级截取 limit=N」（返回原始表前 3 行）。
# ===========================================================================
def test_first_n_rank_clarifies_without_metric(rep, monkeypatch):
    guessed = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation='sum', column='Order Amount', group_by=[], top_n=3))
    out = _run_with_llm_turn(rep, '前3名订单', guessed, monkeypatch, session_key='unit-firstn')
    assert out['status'] == 'clarify', out.get('status')
    assert not out.get('group_aggregate') and not out.get('aggregate')


# ===========================================================================
# 5c) 「前N条 / 前N行」= 行级截取（prompt 第 6 条）-> 必须保持 limit=N 且返回原始顺序行
# ===========================================================================
@pytest.mark.parametrize('message', ['前3条订单', '前3行订单'])
def test_row_truncation_phrases_keep_limit(message, rep, monkeypatch):
    # 这两种说法按 prompt 第 6 条应产出 new_query + limit=N（这里注入 LLM 的规范答案）
    ok_turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=nl.intent_from_dict({
        'query_type': nl.INTENT_STRUCTURED, 'filters': [],
        'columns': ['Order ID', 'Order Amount'], 'limit': 3}))
    out = _run_with_llm_turn(rep, message, ok_turn, monkeypatch,
                             session_key='unit-rows-%s' % abs(hash(message)))
    assert out['status'] == 'ok', (message, out.get('status'), out.get('message'))
    assert not out.get('group_aggregate'), out.get('group_aggregate')   # 不是聚合/排名
    assert (out.get('query') or {}).get('limit') == 3
    res = out['result']
    names = [c if isinstance(c, str) else (c or {}).get('name') for c in res['columns']]
    i = names.index('Order Amount')
    # 原始表前 3 行：10 / 20 / 25（**未排序**；若被误排序会变成 25 / 20 / 15）
    assert [r[i] for r in res['rows'][:3]] == ['10', '20', '25']


# ===========================================================================
# 6) C 组回归：分组 TOP-N（COUNT + GROUP BY 物流商 + TOP-3）不被订单级规则破坏
# ===========================================================================
def test_group_topn_by_carrier_still_works(rep, monkeypatch):
    guessed = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation='count', group_by=['Shipping Provider Name'],
        order_by='aggregate_value', order_dir='desc', top_n=3))
    out = _run_with_llm_turn(rep, '订单最多的前3个物流商', guessed, monkeypatch,
                             session_key='unit-c')
    assert out['status'] == 'ok', out.get('message')
    g = out['group_aggregate']
    assert g['operation'] == 'count'
    assert [c['name'] for c in g['group_by']] == ['Shipping Provider Name']
    assert g['top_n'] == 3 and g['returned_groups'] == 2
    assert [r['group_key'] for r in g['rows']] == [['SF'], ['Yanwen Express']]
    assert [r['value'] for r in g['rows']] == [3, 2]


# ===========================================================================
# 7) 「按X排名前三」（没有排名单位）-> 保持澄清
# ===========================================================================
@pytest.mark.parametrize('message', ['按订单金额排名前三', '按数量排名前三'])
def test_metric_only_rank_still_clarifies(message, rep, monkeypatch):
    guessed = nl.TurnIntent(action=nl.ACTION_CLARIFY)
    out = _run_with_llm_turn(rep, message, guessed, monkeypatch,
                             session_key='unit-b2-%s' % abs(hash(message)))
    assert out['status'] == 'clarify', (message, out.get('status'))
