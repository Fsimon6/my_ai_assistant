# -*- coding: utf-8 -*-
"""Stage 2A-P0（D1/G1）：日期语义正式回归（date_between / 日期列选择权 / analysis 不丢日期）。

背景（2026-09-24 审计实证，真实文档 OrderSKUList 有 4 个日期列）：
    analysis 路径（含 `2.3` 确定性升级、`analysis_guard`）在 `_run_analysis_turn(...)`
    之后立刻 `return`，位置**早于** `2.45` 的时间守卫 —— 于是
    「8月21日 订单金额最高的前3个SKU的销售额总和是多少」**静默丢弃**用户日期条件，
    按全表返回 106.6；而同一句日期在 aggregate 路径会正确澄清（0/0/4/5 各列结果不同）。

铁律（本文件固化为回归）：
    用户明说了日期 → 只能「落 date_between」或「澄清」；**绝不**无过滤继续执行；
    LLM 自己点名的日期列 ≠ 用户明确指定。

全部为离线测试（注入 turn / monkeypatch LLM），不调用真实 LLM、不消耗额度。
"""
import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from backend.excel import aggregate as excel_aggregate
from backend.excel import multi_step as excel_multi_step
from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from backend.excel.parser import ExcelParser
from backend.excel.representation import (
    ColumnMeta, SheetRepresentation, WorkbookRepresentation,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMALL_REAL = PROJECT_ROOT / '直邮一店 8.20号订单.xlsx'


# ===========================================================================
# 合成表
# ===========================================================================
def _rep(doc_id: str, cols: List[str], rows: List[List[str]]) -> WorkbookRepresentation:
    cms = [ColumnMeta(name=n, index=i, excel_column=i + 1,
                      excel_column_letter=chr(ord('A') + i), dtype='string')
           for i, n in enumerate(cols)]
    sheet = SheetRepresentation(
        sheet_name='OrderSKUList', sheet_index=0, header_mode='single', columns=cms,
        rows=[list(r) for r in rows],
        row_excel_numbers=list(range(2, 2 + len(rows))),
        row_count=len(rows), column_count=len(cms))
    return WorkbookRepresentation(schema_version='1.0', document_id=doc_id, user_id=1,
                                  filename='%s.xlsx' % doc_id, file_type='xlsx',
                                  parser='xlsx', sheet_count=1, sheets=[sheet])


#: 单日期列：8/20 命中 1 行
_SINGLE = _rep('date-single', ['Order ID', 'Created Time'], [
    ['O1', '08/20/2026 1:00:00 AM'],
    ['O2', '08/21/2026 1:00:00 AM'],
])

#: 多日期列且结果**不同**：8/20 -> Created=2 / Paid=1；8/18 -> 0/0（一致）
_MULTI_DIFFER = _rep('date-multi', ['Order ID', 'Created Time', 'Paid Time'], [
    ['O1', '08/20/2026 1:00:00 AM', '08/20/2026 2:00:00 AM'],
    ['O2', '08/20/2026 1:00:00 AM', '08/21/2026 2:00:00 AM'],
])
_MULTI_SAME_DAY = '8月18日'          # 两列都命中 0 行 -> 结果一致，无歧义

#: analysis 用表：SKU 级分组 + SUM(Order Amount) + 两个日期列
#:   无过滤         -> S1=35, S2=35, S3=5        -> 75
#:   Created=8/20   -> S1=25, S2=15              -> 40（命中 2 行）
#:   Paid=8/20      -> S2=35, S1=25, S3=5        -> 65（命中 4 行）
_ANALYSIS = _rep('date-analysis',
                 ['Order ID', 'SKU ID', 'Order Amount', 'Created Time', 'Paid Time'], [
                     ['O1', 'S1', '10', '08/19/2026 1:00:00 AM', '08/19/2026 1:00:00 AM'],
                     ['O1', 'S2', '20', '08/19/2026 1:00:00 AM', '08/20/2026 1:00:00 AM'],
                     ['O2', 'S1', '25', '08/20/2026 1:00:00 AM', '08/20/2026 1:00:00 AM'],
                     ['O2', 'S2', '15', '08/20/2026 1:00:00 AM', '08/20/2026 1:00:00 AM'],
                     ['O3', 'S3', '5', '08/21/2026 1:00:00 AM', '08/20/2026 1:00:00 AM'],
                 ])

_ANALYSIS_NO_FILTER_GT = 75.0
_ANALYSIS_CREATED_8_20_GT = 40.0     # 用户点名 Created Time
_ANALYSIS_PAID_8_20_GT = 65.0        # 用户点名 Paid Time


def _catalog(rep: WorkbookRepresentation) -> List[Dict[str, Any]]:
    return [{
        'document_id': rep.document_id, 'filename': rep.filename,
        'file_type': rep.file_type, 'created_at': '2026-09-24T00:00:00',
        'total_rows': rep.total_rows,
        'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                    'row_count': s.row_count, 'column_count': s.column_count,
                    'columns': s.column_names} for s in rep.sheets],
    }]


class _FakeLLM:
    """占位 LLM：管线里的 LLM 调用点全部被 monkeypatch。"""


def _patch_store(monkeypatch, rep: WorkbookRepresentation) -> None:
    monkeypatch.setattr(excel_store, 'load_representation',
                        lambda document_id, user_id=None: rep)


def _run(rep: WorkbookRepresentation, message: str, monkeypatch,
         *, turn=None, analysis_plan=None, analysis_guard=None,
         session_key: str = 'unit-date') -> Dict[str, Any]:
    """跑真实管线，把 LLM 的产出固定为给定值（不调用真实 LLM）。"""
    _patch_store(monkeypatch, rep)
    orig_turn, orig_analysis = nl.llm_parse_turn, nl.llm_parse_analysis

    async def _fake_turn(llm, msg, catalog, context=None, analysis_context=None):
        return turn

    async def _fake_analysis(llm, msg, catalog, context=None, analysis_context=None):
        return analysis_guard

    if turn is not None:
        monkeypatch.setattr(nl, 'llm_parse_turn', _fake_turn)
    if analysis_guard is not None:
        monkeypatch.setattr(nl, 'llm_parse_analysis', _fake_analysis)
    try:
        return asyncio.run(nl.run_nl_query(
            message, _catalog(rep), llm=_FakeLLM(), user_id=1, session_key=session_key,
        ))
    finally:
        monkeypatch.setattr(nl, 'llm_parse_turn', orig_turn)
        monkeypatch.setattr(nl, 'llm_parse_analysis', orig_analysis)


def _group_topn_plan() -> 'nl.AnalysisIntent':
    """两阶段计划：step1 = SUM(Order Amount) by SKU ID → TOP3；step2 = 对聚合值求和。"""
    return nl.AnalysisIntent(steps=[
        {'type': excel_multi_step.STEP_GROUP_AGGREGATE, 'group_by': ['SKU ID'],
         'operation': excel_aggregate.OPERATION_SUM, 'column': 'Order Amount',
         'order_by': excel_aggregate.ORDER_BY_AGGREGATE, 'order_dir': 'desc',
         'top_n': 3, 'filters': []},
        {'type': excel_multi_step.STEP_AGGREGATE, 'operation': excel_aggregate.OPERATION_SUM,
         'source': excel_multi_step.SOURCE_STEP_1,
         'column': excel_multi_step.INTERMEDIATE_VALUE_COLUMN},
    ])


def _analysis_turn() -> 'nl.TurnIntent':
    return nl.TurnIntent(action=nl.ACTION_ANALYSIS, analysis=_group_topn_plan())


def _agg_turn(group_by: List[str], top_n: int = 3) -> 'nl.TurnIntent':
    return nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=excel_aggregate.OPERATION_SUM, column='Order Amount',
        group_by=list(group_by), order_by=excel_aggregate.ORDER_BY_AGGREGATE,
        order_dir='desc', top_n=top_n, filters=[]))


def _final_number(outcome: Dict[str, Any]) -> Optional[float]:
    m = re.search(r'最终结果：([\d.]+)', str(outcome.get('message') or ''))
    return float(m.group(1)) if m else None


def _has_date_between(outcome: Dict[str, Any]) -> bool:
    return 'date_between' in str(outcome)


# ===========================================================================
# 1) 确定性层：日期列选择权只来自用户文本
# ===========================================================================
def test_pre_execution_temporal_guard_injects_into_analysis_step1_only(monkeypatch):
    """①（D1 核心）执行前时间语义落点把日期落进 **step1**，且 step2 不带筛选条件。"""
    _patch_store(monkeypatch, _ANALYSIS)
    turn = _analysis_turn()
    msg = '按 Created Time 统计8月20日 订单金额最高的前3个SKU的销售额总和是多少'
    notes, clarify = nl._apply_temporal_guard_before_execution(
        turn, msg, _catalog(_ANALYSIS))
    assert clarify is None and notes
    step1, step2 = turn.analysis.steps
    assert step1['filters'] == [{'column': 'Created Time', 'operator': 'date_between',
                                 'value': ['2026-08-20', '2026-08-20']}]
    assert not step2.get('filters')


def test_pre_execution_temporal_guard_clarifies_for_analysis_without_named_column(monkeypatch):
    """② 执行前时间语义落点：多日期列结果不同 + 未点名 -> 返回 clarify（不执行）。"""
    _patch_store(monkeypatch, _ANALYSIS)
    msg = '8月20日 订单金额最高的前3个SKU的销售额总和是多少'
    notes, clarify = nl._apply_temporal_guard_before_execution(
        _analysis_turn(), msg, _catalog(_ANALYSIS))
    assert clarify is not None and clarify['stage'] == 'date'
    assert not notes


def test_single_date_column_filters_correctly():
    """① 单日期列：自动使用唯一日期列，闭区间正确。"""
    filt, clarify = nl._temporal_filter_for_message('8月20日的订单', _SINGLE.sheets[0])
    assert clarify is None
    assert filt == {'column': 'Created Time', 'operator': 'date_between',
                    'value': ['2026-08-20', '2026-08-20']}


def test_date_range_and_year_month_are_closed_intervals():
    """⑧⑨ 日期区间 / 年月：闭区间且两端包含。"""
    sheet = _SINGLE.sheets[0]
    f1, c1 = nl._temporal_filter_for_message('8月1日到8月15日的订单', sheet)
    assert c1 is None and f1['value'] == ['2026-08-01', '2026-08-15']
    f2, c2 = nl._temporal_filter_for_message('2026年8月的订单', sheet)
    assert c2 is None and f2['value'] == ['2026-08-01', '2026-08-31']


def test_multi_date_columns_differ_without_named_column_clarifies():
    """② 多日期列 + 结果不同 + 用户未指定 -> clarify（绝不选列）。"""
    filt, clarify = nl._temporal_filter_for_message('8月20日的订单', _MULTI_DIFFER.sheets[0])
    assert filt is None
    assert clarify is not None and clarify['stage'] == 'date'
    assert set(clarify['candidates']) == {'Created Time', 'Paid Time'}


def test_multi_date_columns_equal_allows_execution():
    """⑩ 多日期列但各列结果一致 -> 允许执行（无歧义）。"""
    filt, clarify = nl._temporal_filter_for_message(_MULTI_SAME_DAY + '的订单',
                                                    _MULTI_DIFFER.sheets[0])
    assert clarify is None and filt is not None
    assert filt['value'] == ['2026-08-18', '2026-08-18']


def test_user_named_date_column_is_used():
    """③ 用户文本点名日期列 -> 严格使用该列。"""
    filt, clarify = nl._temporal_filter_for_message('按 Paid Time 看8月20日订单',
                                                    _MULTI_DIFFER.sheets[0])
    assert clarify is None
    assert filt['column'] == 'Paid Time'
    assert filt['value'] == ['2026-08-20', '2026-08-20']


@pytest.mark.parametrize('column', ['Created Time', 'Paid Time'])
def test_llm_named_date_column_cannot_bypass(column, monkeypatch):
    """④ LLM 自己点名日期列（用户没说）-> 仍必须 clarify（用户文本才是依据）。"""
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=nl.intent_from_dict({
        'query_type': nl.INTENT_STRUCTURED, 'limit': 0,
        'filters': [{'column': column, 'operator': 'contains', 'value': '8/20'}],
    }))
    out = _run(_MULTI_DIFFER, '8月20日的订单', monkeypatch, turn=turn,
               session_key='date-llm-%s' % column.replace(' ', '-'))
    assert out['status'] == nl.STATUS_CLARIFY
    assert out.get('stage') == 'date'
    assert not _has_date_between(out)


# ===========================================================================
# 2) analysis / 2.3 升级 / analysis_guard：日期条件不得丢失（D1/G1 核心）
# ===========================================================================
def test_analysis_unnamed_date_clarifies_instead_of_full_table(monkeypatch):
    """⑤ 多日期列 + 结果不同 + 未指定 -> analysis 也必须在执行前澄清。"""
    out = _run(_ANALYSIS, '8月20日 订单金额最高的前3个SKU的销售额总和是多少',
               monkeypatch, turn=_analysis_turn(), session_key='date-analysis-clarify')
    assert out['status'] == nl.STATUS_CLARIFY
    assert out.get('stage') == 'date'
    assert _final_number(out) is None          # 绝不能返回全表数值 75.0


@pytest.mark.parametrize('column,gt', [
    ('Created Time', _ANALYSIS_CREATED_8_20_GT),
    ('Paid Time', _ANALYSIS_PAID_8_20_GT),
])
def test_analysis_named_date_lands_in_step1_with_ground_truth(column, gt, monkeypatch):
    """⑤⑦ 用户点名日期列 -> analysis step1 携带 date_between，且结果 == GT。"""
    msg = '按 %s 统计8月20日 订单金额最高的前3个SKU的销售额总和是多少' % column
    out = _run(_ANALYSIS, msg, monkeypatch, turn=_analysis_turn(),
               session_key='date-analysis-%s' % column.replace(' ', '-'))
    assert out['status'] == nl.STATUS_OK
    assert _has_date_between(out)
    assert _final_number(out) == gt


def test_aggregate_upgrade_to_analysis_keeps_date(monkeypatch):
    """⑥ 2.3 确定性升级（aggregate -> analysis）同样必须携带日期条件。

    未点名日期列 -> 升级前就澄清；点名 -> step1 落 date_between 且结果 == GT。
    """
    msg_unnamed = '8月20日 订单金额最高的前3个SKU的销售额总和是多少'
    out = _run(_ANALYSIS, msg_unnamed, monkeypatch, turn=_agg_turn(['SKU ID']),
               session_key='date-upgrade-unnamed')
    assert out['status'] == nl.STATUS_CLARIFY and out.get('stage') == 'date'
    assert _final_number(out) is None

    msg_named = '按 Created Time 统计8月20日 订单金额最高的前3个SKU的销售额总和是多少'
    out2 = _run(_ANALYSIS, msg_named, monkeypatch, turn=_agg_turn(['SKU ID']),
                session_key='date-upgrade-named')
    assert out2['status'] == nl.STATUS_OK
    assert out2.get('analysis_upgraded') is True
    assert _has_date_between(out2) and _final_number(out2) == _ANALYSIS_CREATED_8_20_GT


def test_analysis_guard_keeps_date(monkeypatch):
    """⑦ analysis_guard（第二次 LLM 调用产出计划）同样不得丢日期条件。"""
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=nl.intent_from_dict({
        'query_type': nl.INTENT_STRUCTURED, 'limit': 0,
    }))
    msg = '按 Created Time 统计8月20日 订单金额最高的前3个SKU的销售额总和是多少'
    out = _run(_ANALYSIS, msg, monkeypatch, turn=turn,
               analysis_guard=_group_topn_plan(), session_key='date-guard')
    assert out['status'] == nl.STATUS_OK
    assert out.get('analysis_guard') is True
    assert _has_date_between(out) and _final_number(out) == _ANALYSIS_CREATED_8_20_GT


def test_analysis_unnamed_date_clarifies_even_with_llm_plan_ready(monkeypatch):
    """⑦ 反向：未点名日期列 + 各列结果不同 -> 即使 LLM 计划已就绪也必须澄清。

    注：该形态由 `2.26` 在分析守卫之前拦截（更早因此更安全）；此处断言的是
    "绝不静默执行"，而非具体由哪一个守卫拦截。
    """
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=nl.intent_from_dict({
        'query_type': nl.INTENT_STRUCTURED, 'limit': 0,
    }))
    out = _run(_ANALYSIS, '8月20日 订单金额最高的前3个SKU的销售额总和是多少',
               monkeypatch, turn=turn, analysis_guard=_group_topn_plan(),
               session_key='date-guard-clarify')
    assert out['status'] == nl.STATUS_CLARIFY and out.get('stage') == 'date'
    assert _final_number(out) is None


# ===========================================================================
# 3) 真实文档（19 行 × 63 列）：D1 的原始复现场景
# ===========================================================================
@pytest.fixture(scope='module')
def small_real() -> WorkbookRepresentation:
    if not SMALL_REAL.exists():
        pytest.skip('未找到小表（直邮一店 8.20号订单.xlsx）')
    return ExcelParser().parse(str(SMALL_REAL), 'date-real', user_id=1,
                               filename=SMALL_REAL.name)


def _real_gt(rep: WorkbookRepresentation, column: str) -> float:
    """独立 Ground Truth：step1 SUM(Order Amount) by SKU ID -> TOP3 -> step2 求和。"""
    sheet = rep.sheets[0]
    cols = sheet.column_names
    i_sku, i_amt, i_col = cols.index('SKU ID'), cols.index('Order Amount'), cols.index(column)
    sums: Dict[Any, float] = {}
    for row in sheet.rows:
        if not nl.excel_query.date_between_match(row[i_col], ['2026-08-21', '2026-08-21']):
            continue
        amount = excel_aggregate.aggregate_to_number(row[i_amt])
        if amount is None:
            continue
        sums[row[i_sku]] = sums.get(row[i_sku], 0.0) + amount
    return round(sum(sorted(sums.values(), reverse=True)[:3]), 2)


def test_real_analysis_unnamed_date_never_returns_full_table(small_real, monkeypatch):
    """D1 复现条件：未点名日期列 -> 必须 clarify，绝不返回全表 106.6。"""
    out = _run(small_real, '8月21日 订单金额最高的前3个SKU的销售额总和是多少', monkeypatch,
               turn=_analysis_turn(), session_key='date-real-unnamed')
    assert out['status'] == nl.STATUS_CLARIFY
    assert out.get('stage') == 'date'
    assert _final_number(out) is None


def test_real_analysis_named_date_matches_ground_truth(small_real, monkeypatch):
    """用户点名 Shipped Time -> step1 落 date_between，结果 == 独立 GT（且 != 全表值）。"""
    gt = _real_gt(small_real, 'Shipped Time')
    assert gt != 106.6                                   # 与"全表"值必须可区分
    msg = '按 Shipped Time 统计8月21日 订单金额最高的前3个SKU的销售额总和是多少'
    out = _run(small_real, msg, monkeypatch, turn=_analysis_turn(),
               session_key='date-real-named')
    assert out['status'] == nl.STATUS_OK
    assert _has_date_between(out)
    assert _final_number(out) == gt
