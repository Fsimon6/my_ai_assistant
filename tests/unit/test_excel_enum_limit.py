# -*- coding: utf-8 -*-
"""P1（2026-09-29）：枚举/列出类 Excel 查询默认返回**全部**命中行（≤ MAX_LIMIT=500）。

背景（真实审计）：用户「列出订单号和物流商」在 447 行表上只返回 50 行（后端默认 limit），
且前端无任何继续入口 -> 明确的枚举意图无法达成。

本轮语义（与项目既有「全部 SKU = 保留所有数据行、不去重」一致）：
  * **普通**结构化查询：默认 limit 仍是 50（不变）；
  * **明确枚举**（列出/所有/全部/哪些/…）且用户**未显式指定条数**：默认 limit = 500；
  * 用户显式条数（前20 / 20条 / 最多50）优先，绝不被枚举规则覆盖；
  * TOP-N / 排名 / 分析 / 统计：完全不碰（Stage 2A authority 不变）；
  * >500 行：仍截断到上限，但**明确提示**且保留 has_more/next_offset/total_matches。

本文件覆盖：
  1) 枚举识别纯函数边界矩阵
  2) `apply_enumeration_limit` 的作用域（只 new_query、只在未显式给 limit 时）
  3) 真实文档执行矩阵：19 / 50 / 51 / 100 / 447 行、显式 20、普通查询仍 50
  4) >500（缩放模拟：把上限压到 20）→ 上限 + has_more + 明确提示
  5) 历史快照：447 行完整落库、history_truncated=False
  6)「全部 SKU」全量语义（保留所有数据行、不去重）回归
"""
import asyncio
import glob
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from backend.api.v1.rag import build_excel_history_snapshot
from backend.excel import aggregate as excel_aggregate
from backend.excel import multi_step as excel_multi_step
from backend.excel import nl_query as nl
from backend.excel import query as excel_query
from backend.excel import store as excel_store

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ==========================================================================
# 夹具：真实 representation（优先用已落盘的 representation.json；缺失则 skip）
# ==========================================================================
def _load_rep_by_rows(low: int, high: int) -> 'nl.WorkbookRepresentation':
    patterns = [str(PROJECT_ROOT / 'data' / 'excel' / '*' / 'representation.json')]
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            did = os.path.basename(os.path.dirname(path))
            try:
                rep = excel_store.load_representation(did)
            except Exception:  # noqa: BLE001
                continue
            if rep is not None and low <= rep.total_rows <= high:
                return rep
    pytest.skip('未找到 %d~%d 行的真实 representation' % (low, high))


@pytest.fixture(scope='module')
def big_rep() -> 'nl.WorkbookRepresentation':
    """447 行「直邮5店」表。"""
    return _load_rep_by_rows(400, 600)


@pytest.fixture(scope='module')
def small_rep() -> 'nl.WorkbookRepresentation':
    """19 行「直邮一店」表。"""
    return _load_rep_by_rows(10, 30)


def _catalog(rep) -> List[Dict[str, Any]]:
    return [{
        'document_id': rep.document_id,
        'filename': rep.filename,
        'file_type': rep.file_type,
        'created_at': '2026-09-29T00:00:00',
        'total_rows': rep.total_rows,
        'sheets': [
            {'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
             'row_count': s.row_count, 'column_count': s.column_count,
             'columns': s.column_names}
            for s in rep.sheets
        ],
    }]


def _intent(rep, message: str, *, columns=None, explicit_limit=None) -> 'nl.NlIntent':
    """构造「LLM 解析结果」：默认 limit=50 且 limit_explicit=False（与真实审计一致）。"""
    raw: Dict[str, Any] = {'query_type': nl.INTENT_STRUCTURED, 'document': rep.filename,
                           'columns': list(columns or ['Order ID']), 'filters': []}
    if explicit_limit is not None:
        raw['limit'] = explicit_limit
    return nl.intent_from_dict(raw)


def _turn_for(rep, message: str, *, columns=None, explicit_limit=None) -> 'nl.TurnIntent':
    """构造 new_query turn，并**应用生产同款**枚举上限规则（llm_parse_turn 中的那一步）。"""
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY,
                         intent=_intent(rep, message, columns=columns,
                                        explicit_limit=explicit_limit))
    nl.apply_enumeration_limit(turn, message)
    return turn


def _run(rep, message: str, turn: 'nl.TurnIntent', *, session_key='enum') -> Dict[str, Any]:
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nl.run_nl_query(message, _catalog(rep), llm=None,
                                           pagination_override=turn, user_id=1,
                                           session_key=session_key))
    finally:
        excel_store.load_representation = original


# ==========================================================================
# 1) 枚举识别边界矩阵（纯函数）
# ==========================================================================
@pytest.mark.parametrize('text', [
    '列出订单号和物流商',
    '把所有 SKU 列出来',
    '全部订单',
    '哪些订单使用 SF',
    '哪几个SKU的金额大于10',
    '列出所有 Shipping Provider Name',
    '显示所有订单',
    '全部 SKU',
    '数量大于3的订单有哪些',
])
def test_enum_detection_positive(text):
    assert nl.is_full_enumeration(text) is True


@pytest.mark.parametrize('text', [
    '列出前20个订单',            # 显式条数
    '列出20条订单',
    '列出最多50条订单',
    '列出前十条订单',
    '列出5店前20条SKU',
    '销售额最高的前3个 SKU',      # TOP-N
    '订单金额最高的前3个SKU的销售额总和',   # 多步分析
    '哪个物流商订单最多',          # 统计排行（无列表词）
    '订单金额总和',              # 统计
    '各个物流商的订单金额总和',
    '各物流商订单金额从高到低排列',     # 排序
    '物流商为SF的订单金额总和',
    '下一页',                  # 分页
    '再来20条',
    '查一下订单的金额',           # 普通查询
    '',
])
def test_enum_detection_negative(text):
    assert nl.is_full_enumeration(text) is False


# ==========================================================================
# 2) 作用域：只 new_query、只在未显式给 limit 时
# ==========================================================================
def test_apply_enumeration_limit_upgrades_new_query(big_rep):
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=_intent(big_rep, '列出订单号和物流商'))
    notes = nl.apply_enumeration_limit(turn, '列出订单号和物流商')
    assert notes and turn.intent.limit == nl.MAX_NL_LIMIT == 500


def test_apply_enumeration_limit_keeps_explicit_limit(big_rep):
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY,
                         intent=_intent(big_rep, '列出前20个订单', explicit_limit=20))
    assert nl.apply_enumeration_limit(turn, '列出前20个订单') == []
    assert turn.intent.limit == 20


def test_apply_enumeration_limit_never_touches_analysis_or_aggregate(big_rep):
    analysis = nl.TurnIntent(action=nl.ACTION_ANALYSIS, analysis=nl.AnalysisIntent(steps=[
        {'type': excel_multi_step.STEP_GROUP_AGGREGATE, 'group_by': ['SKU ID'],
         'operation': excel_aggregate.OPERATION_SUM, 'column': 'Order Amount',
         'order_by': excel_aggregate.ORDER_BY_AGGREGATE, 'order_dir': 'desc', 'top_n': 3},
        {'type': excel_multi_step.STEP_AGGREGATE, 'operation': excel_aggregate.OPERATION_SUM,
         'source': excel_multi_step.SOURCE_STEP_1,
         'column': excel_multi_step.INTERMEDIATE_VALUE_COLUMN}]))
    assert nl.apply_enumeration_limit(analysis, '列出所有订单金额最高的前3个SKU的总和') == []

    agg = nl.TurnIntent(action=nl.ACTION_AGGREGATE,
                        aggregate=nl.AggregateIntent(operation=excel_aggregate.OPERATION_SUM,
                                                     column='Order Amount',
                                                     group_by=['Shipping Provider Name']))
    assert nl.apply_enumeration_limit(agg, '列出所有物流商的订单金额总和') == []


def test_apply_enumeration_limit_keeps_default_for_ordinary_query(big_rep):
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=_intent(big_rep, '查一下订单的金额'))
    assert nl.apply_enumeration_limit(turn, '查一下订单的金额') == []
    assert turn.intent.limit == nl.DEFAULT_NL_LIMIT == 50


# ==========================================================================
# 3) 真实执行矩阵
# ==========================================================================
def test_enum_small_table_returns_all(small_rep):
    """19 行枚举 -> 19。"""
    out = _run(small_rep, '列出订单号和物流商',
               _turn_for(small_rep, '列出订单号和物流商',
                         columns=['Order ID', 'Shipping Provider Name']),
               session_key='enum-small')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    r = out['result']
    assert r['total_matches'] == small_rep.total_rows == 19
    assert r['returned_count'] == 19
    assert r['has_more'] is False and r['next_offset'] is None
    assert len(r['rows']) == 19          # API rows == 19


def test_enum_big_table_returns_447(big_rep):
    """447 行枚举 -> 447（本 P1 的核心验收）。"""
    out = _run(big_rep, '列出订单号和物流商',
               _turn_for(big_rep, '列出订单号和物流商',
                         columns=['Order ID', 'Shipping Provider Name']),
               session_key='enum-big')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    r = out['result']
    assert r['total_matches'] == 447
    assert r['returned_count'] == 447
    assert len(r['rows']) == 447
    assert r['has_more'] is False
    assert r['limit'] == nl.MAX_NL_LIMIT
    assert r['row_excel_numbers'][0] == 3 and len(r['row_excel_numbers']) == 447


@pytest.mark.parametrize('limit,want', [(50, 50), (51, 51), (100, 100)])
def test_enum_explicit_limits_respected(big_rep, limit, want):
    """显式条数（或显式 limit）如何影响返回量：50 / 51 / 100。"""
    msg = '列出订单号和物流商'
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY,
                         intent=_intent(big_rep, msg, explicit_limit=limit))
    assert nl.apply_enumeration_limit(turn, msg) == []      # 显式 -> 不被枚举规则覆盖
    out = _run(big_rep, msg, turn, session_key='enum-%d' % limit)
    r = out['result']
    assert r['limit'] == limit and r['returned_count'] == want
    assert r['has_more'] is True and r['next_offset'] == limit


def test_enum_text_explicit_count_not_upgraded_even_if_llm_omitted_limit(big_rep):
    """文本写了「前20个」但 LLM 漏给 limit -> 绝不升级成 500（保持默认，不静默扩大）。"""
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=_intent(big_rep, '列出前20个订单'))
    assert nl.apply_enumeration_limit(turn, '列出前20个订单') == []
    assert turn.intent.limit == 50


def test_ordinary_query_still_default_50(big_rep):
    """普通（非枚举）查询仍然默认 50 —— 本轮不改变通用默认。"""
    out = _run(big_rep, '查一下订单的金额',
               _turn_for(big_rep, '查一下订单的金额', columns=['Order ID']),
               session_key='enum-non')
    r = out['result']
    assert r['limit'] == 50 and r['returned_count'] == 50
    assert r['total_matches'] == 447 and r['has_more'] is True
    assert '单次显示上限' in out['message']      # 超过默认上限必须明说


# ==========================================================================
# 4) >500（缩放模拟：把上限压到 20，验证"截断 + 明说 + 分页仍可用"）
# ==========================================================================
def test_cap_reached_marks_more_and_hints(big_rep):
    """到达单次上限（>上限的等价场景）：截断 + 明确提示 + 分页仍可用。

    说明：工作区最大真实表为 447 行（≤ MAX_LIMIT=500），故用「显式小 limit」走到
    同一条 ``has_more`` / `_cap_notice` 代码路径，验证"绝不假装结果完整"。
    """
    msg = '列出订单号和物流商'
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY,
                         intent=_intent(big_rep, msg, explicit_limit=20))
    out = _run(big_rep, msg, turn, session_key='enum-cap')
    r = out['result']
    assert r['limit'] == 20 and r['returned_count'] == 20
    assert r['total_matches'] == 447            # 总数正确，不假装"只有 20 条"
    assert r['has_more'] is True and r['next_offset'] == 20
    assert '单次显示上限' in out['message'] and '447' in out['message']
    assert '下一页' in out['message']


# ==========================================================================
# 5) 历史快照
# ==========================================================================
def test_history_snapshot_keeps_full_447(big_rep):
    out = _run(big_rep, '列出订单号和物流商',
               _turn_for(big_rep, '列出订单号和物流商',
                         columns=['Order ID', 'Shipping Provider Name']),
               session_key='enum-snap')
    snap = build_excel_history_snapshot(out)
    payload = snap['payload']
    assert snap['schema_version'] == 1
    assert snap['kind'] == 'result'
    assert len(payload['rows']) == 447
    assert payload['total_matches'] == 447
    assert payload['returned_count'] == 447
    assert payload['has_more'] is False
    assert snap['history_truncated'] is False


# ==========================================================================
# 6)「全部 SKU」全量语义回归（保留所有数据行、不去重）
# ==========================================================================
def test_all_sku_enumeration_keeps_every_row(big_rep):
    """「全部 SKU」= 保留所有数据行、不去重（与既有语义一致）。"""
    sheet = big_rep.sheets[0]
    ci = sheet.column_names.index('SKU ID')
    raw = [row[ci] for row in sheet.rows]
    msg = '全部 SKU'
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY,
                         intent=_intent(big_rep, msg, columns=['SKU ID']))
    assert nl.apply_enumeration_limit(turn, msg)          # 枚举 -> 500
    out = _run(big_rep, msg, turn, session_key='enum-allsku')
    r = out['result']
    assert r['returned_count'] == big_rep.total_rows == 447
    got = [row[0] for row in r['rows']]
    assert got == raw                                    # 逐行一致（不去重、不改顺序）
    # 该表物流商只有少数几种取值 -> 若按分组/去重会远少于 447；这里仍是 447 行，证明未去重
    carrier_ci = sheet.column_names.index('Shipping Provider Name')
    carriers = [row[carrier_ci] for row in sheet.rows if row[carrier_ci] is not None]
    assert len(set(carriers)) < 447 and r['returned_count'] == 447
