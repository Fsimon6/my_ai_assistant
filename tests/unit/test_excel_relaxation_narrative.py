# -*- coding: utf-8 -*-
"""R2：筛选条件与「放宽过程」的解释完整性（叙述层一致性）。

背景（2026-09-29 只读审查 + 真实验证）：
  * 卡片层已经同时展示「筛选条件」（最终执行条件）+「注意：…放宽…」；
  * 但 AI 叙述层不一致：
      - aggregate / group / table 把放宽说明塞进**通用「提示：」**（与"已沿用上一轮…"混在一起），
        且表格叙述**完全没有筛选条件行**；
      - multi-step 的 formatter 无法接收放宽说明。

本轮（只改解释层，不动执行逻辑）：
  * 四类叙述统一为：`- 筛选条件：…`（最终执行条件）→ `- 筛选过程：…`
    （**仅当执行层真的产生了 `relaxed_filters`** 时输出；文字原样取自执行层，
      不在展示层重新解析用户文本、不硬编码任何值）；
  * 表格叙述补上「筛选条件」行（无筛选时不输出"无"，保持简洁）；
  * 放宽说明不再混进通用「提示」，也不会重复出现多次。

本文件用**确定性**方式制造真实 relaxation：注入 `eq 'SF'`（该列无精确相等值，唯一前缀
`SF International`）→ 执行层必然放宽；再断言无放宽时绝不出现任何"放宽"字样。
"""
import asyncio
import glob
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.api.v1.rag import EXCEL_HISTORY_SCHEMA_VERSION, build_excel_history_snapshot
from backend.excel import aggregate as ag
from backend.excel import multi_step as ms
from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from backend.excel.query_context import ExcelQueryContext

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CARRIER = 'Shipping Provider Name'
EQ_SF = [{'column': CARRIER, 'operator': 'eq', 'value': 'SF'}]
CONTAINS_SF = [{'column': CARRIER, 'operator': 'contains', 'value': 'SF'}]
RELAX_KEY = '筛选过程：'
NOTE_KEY = '已按前缀放宽为'


# ==========================================================================
# 夹具 / 辅助
# ==========================================================================
def _load_rep(low: int, high: int) -> 'nl.WorkbookRepresentation':
    for path in sorted(glob.glob(str(PROJECT_ROOT / 'data' / 'excel' / '*'
                                      / 'representation.json'))):
        did = os.path.basename(os.path.dirname(path))
        try:
            rep = excel_store.load_representation(did)
        except Exception:  # noqa: BLE001
            continue
        if rep is not None and low <= rep.total_rows <= high:
            return rep
    pytest.skip('未找到 %d~%d 行的真实 representation' % (low, high))


@pytest.fixture(scope='module')
def rep() -> 'nl.WorkbookRepresentation':
    """19 行真实表（含 SF International / Yanwen Express）。"""
    return _load_rep(10, 30)


def _catalog(r) -> List[Dict[str, Any]]:
    return [{
        'document_id': r.document_id, 'filename': r.filename, 'file_type': r.file_type,
        'created_at': '2026-09-29T00:00:00', 'total_rows': r.total_rows,
        'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                    'row_count': s.row_count, 'column_count': s.column_count,
                    'columns': s.column_names} for s in r.sheets],
    }]


def _run(r, turn, message: str, *, session_key='r2', context=None) -> Dict[str, Any]:
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: r
    try:
        return asyncio.run(nl.run_nl_query(message, _catalog(r), llm=None,
                                           pagination_override=turn, context=context,
                                           user_id=1, session_key=session_key))
    finally:
        excel_store.load_representation = original


def _agg_turn(*, group_by=None, filters=None):
    return nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=ag.OPERATION_SUM, column='Order Amount', group_by=list(group_by or []),
        filters=list(filters or [])))


def _table_turn(r, *, filters=None, limit=50):
    return nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=nl.intent_from_dict(
        {'query_type': nl.INTENT_STRUCTURED, 'document': r.filename,
         'columns': ['Order ID', CARRIER], 'filters': list(filters or []), 'limit': limit}))


_ANALYSIS_STEPS = [
    {'type': ms.STEP_GROUP_AGGREGATE, 'group_by': ['SKU ID'],
     'operation': ag.OPERATION_SUM, 'column': 'Order Amount',
     'order_by': ag.ORDER_BY_AGGREGATE, 'order_dir': 'desc', 'top_n': 3},
    {'type': ms.STEP_AGGREGATE, 'operation': ag.OPERATION_SUM,
     'source': ms.SOURCE_STEP_1, 'column': ms.INTERMEDIATE_VALUE_COLUMN}]


def _analysis_turn():
    return nl.TurnIntent(action=nl.ACTION_ANALYSIS,
                         analysis=nl.AnalysisIntent(steps=list(_ANALYSIS_STEPS)))


def _analysis_result(rep) -> 'ms.AnalysisResult':
    saved = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        result, _engine = ms.execute_analysis(rep, ms.normalize_analysis_plan(
            {'steps': list(_ANALYSIS_STEPS)}))
        return result
    finally:
        excel_store.load_representation = saved


# ==========================================================================
# A / B / C / D：真实放宽时，四类叙述都要出现「筛选过程」
# ==========================================================================
def test_aggregate_narrative_discloses_relaxation(rep):
    out = _run(rep, _agg_turn(filters=EQ_SF), '各订单的金额汇总', session_key='r2-agg')
    notes = out['relaxed_filters']
    assert notes and NOTE_KEY in notes[0]                        # 执行层真的放宽了
    text = out['message']
    assert '- 筛选条件：Shipping Provider Name 包含 SF' in text   # 最终执行条件
    assert RELAX_KEY + notes[0] in text                          # 放宽过程（原样）
    assert text.count(RELAX_KEY) == 1                            # 只出现一次
    assert '- 提示：' not in text                                # 不再混进通用提示


def test_group_aggregate_narrative_discloses_relaxation(rep):
    out = _run(rep, _agg_turn(group_by=[CARRIER], filters=EQ_SF),
               '各物流商的订单金额汇总', session_key='r2-grp')
    notes = out['relaxed_filters']
    assert notes
    text = out['message']
    assert '- 筛选条件：Shipping Provider Name 包含 SF' in text
    assert RELAX_KEY + notes[0] in text
    assert text.count(RELAX_KEY) == 1
    assert '- 结果（' in text                                     # 分组明细仍在（信息未删）


def test_multi_step_narrative_discloses_relaxation_once(rep):
    """multi_step：放宽说明放在第 1 步筛选条件之后，且只出现一次。"""
    out = _run(rep, _analysis_turn(), '订单金额最高的前3个SKU的销售额总和', session_key='r2-ms')
    # 分析路径本身没有放宽 -> 叙述**不编造**放宽说明
    assert (out.get('relaxed_filters') or []) == []
    assert RELAX_KEY not in out['message'] and NOTE_KEY not in out['message']

    note = ('「Shipping Provider Name」中没有与「SF」完全相等的值；'
            '已按前缀放宽为「包含 SF」')
    text = ms.format_analysis_summary(_analysis_result(rep), relaxed_notes=[note])
    assert '- 筛选条件：无（全表）' in text
    assert RELAX_KEY + note in text
    assert text.count(RELAX_KEY) == 1
    assert '不会重新回到原始 Excel 数据行' in text                 # 既有语义未破坏


def test_table_narrative_has_filters_and_relaxation(rep):
    out = _run(rep, _table_turn(rep, filters=EQ_SF), '列出订单号和物流商', session_key='r2-tbl')
    notes = out['relaxed_filters']
    assert notes
    text = out['message']
    assert '- 筛选条件：Shipping Provider Name 包含 SF' in text   # 新增：表格叙述的筛选条件
    assert RELAX_KEY + notes[0] in text
    assert text.count(RELAX_KEY) == 1
    assert '- 本页 Excel 行号：' in text                          # 既有信息未删


def test_pagination_narrative_uses_same_explanation(rep):
    """分页页沿用同一解释关系：`筛选过程` 出现 ⇔ 本页执行层确实产生了 relaxed_filters。

    注意：放宽后的**最终条件**会写进分页上下文，因此后续页执行的是 contains（不再放宽）
    -> 后续页只显示「筛选条件」，**不会**重复声称"已放宽"（这正是我们要的语义）。
    """
    first = _run(rep, _table_turn(rep, filters=EQ_SF), '列出订单号和物流商', session_key='r2-pg')
    assert first['relaxed_filters']                       # 首页真实放宽
    assert RELAX_KEY + first['relaxed_filters'][0] in first['message']
    assert first['new_context']['filters'][0]['operator'] == 'contains'   # 放宽后的条件进入上下文

    ctx = ExcelQueryContext(**first['new_context'])
    nxt = _run(rep, nl.TurnIntent(action=nl.ACTION_NEXT), '下一页',
               session_key='r2-pg', context=ctx)
    text = nxt['message']
    assert '已继续上一张表格查询' in text
    assert '- 筛选条件：Shipping Provider Name 包含 SF' in text            # 最终执行条件仍可见
    assert (nxt.get('relaxed_filters') or []) == []                        # 本页未再放宽
    assert RELAX_KEY not in text                                           # 因而不显示"筛选过程"


# ==========================================================================
# E / F / G：无放宽 / 正常筛选 / 无筛选
# ==========================================================================
def test_no_relaxation_never_claims_relaxation(rep):
    """E：relaxed_filters 为空时，叙述里不得出现任何"筛选过程/放宽"字样。"""
    cases = {
        'aggregate': (_agg_turn(), '各订单的金额汇总'),
        'group': (_agg_turn(group_by=[CARRIER]), '各物流商的订单金额汇总'),
        'table': (_table_turn(rep), '列出订单号和物流商'),
    }
    for name, (turn, msg) in cases.items():
        out = _run(rep, turn, msg, session_key='r2-none-%s' % name)
        assert out['relaxed_filters'] == []
        assert RELAX_KEY not in out['message'], name
        assert NOTE_KEY not in out['message'], name

    out_ms = _run(rep, _analysis_turn(), '订单金额最高的前3个SKU的销售额总和',
                  session_key='r2-none-ms')
    assert RELAX_KEY not in out_ms['message'] and NOTE_KEY not in out_ms['message']


def test_normal_filter_shows_final_condition_only(rep):
    """F：正常命中（contains）-> 只输出「筛选条件」，不输出「筛选过程」。"""
    out = _run(rep, _agg_turn(filters=CONTAINS_SF), '各订单的金额汇总', session_key='r2-ok')
    assert out['relaxed_filters'] == []
    assert '- 筛选条件：Shipping Provider Name 包含 SF' in out['message']
    assert RELAX_KEY not in out['message'] and NOTE_KEY not in out['message']

    out_t = _run(rep, _table_turn(rep, filters=CONTAINS_SF), '列出订单号和物流商',
                 session_key='r2-ok-t')
    assert '- 筛选条件：Shipping Provider Name 包含 SF' in out_t['message']
    assert RELAX_KEY not in out_t['message']


def test_no_filter_table_narrative_stays_concise(rep):
    """G：无筛选时表格叙述不输出"筛选条件：无"；统计类保留既有「无（全表）」。"""
    out = _run(rep, _table_turn(rep), '列出订单号和物流商', session_key='r2-nofilter-t')
    assert '筛选条件' not in out['message']
    assert RELAX_KEY not in out['message']

    out_a = _run(rep, _agg_turn(), '各订单的金额汇总', session_key='r2-nofilter-a')
    assert '- 筛选条件：无（全表）' in out_a['message']


# ==========================================================================
# H：snapshot / schema 兼容
# ==========================================================================
def test_snapshot_keeps_relaxed_filters_and_schema(rep):
    out = _run(rep, _agg_turn(filters=EQ_SF), '各订单的金额汇总', session_key='r2-snap')
    assert out['relaxed_filters']                                    # 执行层产生的放宽说明
    snap = build_excel_history_snapshot(out)
    assert snap['schema_version'] == EXCEL_HISTORY_SCHEMA_VERSION == 1
    assert snap['relaxed_filters'] == out['relaxed_filters']          # 原字段保留、内容一致
    assert 'payload' in snap                                          # 结构未变

    # 旧快照（无 relaxed_filters 字段 / 旧叙述文本）仍能正常构建、不报错、不回填
    legacy = {k: v for k, v in out.items() if k != 'relaxed_filters'}
    legacy['message'] = '（旧历史消息文本）'
    snap_legacy = build_excel_history_snapshot(legacy)
    assert snap_legacy['schema_version'] == 1
    assert snap_legacy['relaxed_filters'] == []
