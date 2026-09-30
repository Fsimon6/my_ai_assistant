# -*- coding: utf-8 -*-
"""Stage 5 第三项：**自然语言语义回归**（第一版：Golden fixture + 独立 Ground Truth）。

边界（严格遵循本轮要求）：
  * **零真实 LLM**：`FakeLLM` 只伪造 provider 的返回值；`llm_parse_turn` / JSON 解析 /
    全部**确定性语义层**（authority、repair、guard、枚举上限、日期落点…）都是真实生产代码；
  * **独立 Ground Truth**：expected 来自 fixture（合成数据）上的**独立扫描**
    （本文件手写的筛选/分段/分组/聚合扫描 + `tests/helpers/fixtures.py` 的人工可推算常量），
    **不**调用生产 `collapse_row_runs` / matcher / 执行结果来生成 expected；
  * **只做回归覆盖，不改生产代码**：若发现与既有语义不符，先报告、不擅自修改。

三层组织：
  A. 确定性语义解析层（纯函数，完全不经过 LLM）
  B. 端到端语义回归（中文问句 + FakeLLM 候选 -> 真实管线 -> 独立 GT）
  C. Authority 覆盖（17 项口径的回归护栏）
"""
import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.excel import multi_step as ms
from backend.excel import nl_normalize as nl_norm
from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from tests.helpers import fixtures as F

CARRIER, AMOUNT, SKU, ORDER_ID, CREATED = (
    F.CARRIER, F.AMOUNT, F.SKU, 'Order ID', 'Created Time')


# --------------------------------------------------------------------------
# 夹具 / 运行器
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_user_data_dir(tmp_path, monkeypatch):
    """Golden 语义回归不得依赖用户 `data/`（fresh clone 可跑）。"""
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path / 'no_user_data')


@pytest.fixture(scope='module')
def small_rep():
    return F.parse_golden(F.GOLDEN_SMALL_NAME)


@pytest.fixture(scope='module')
def medium_rep():
    return F.parse_golden(F.GOLDEN_MEDIUM_NAME)


@pytest.fixture(scope='module')
def small_sheet(small_rep):
    return F.sheet_of(small_rep)


@pytest.fixture(scope='module')
def medium_sheet(medium_rep):
    return F.sheet_of(medium_rep, F.MEDIUM_SHEET)


class FakeLLM:
    """只伪造 provider 返回值（按 system prompt 区分轮次解析 / 统计抽取 / 两步计划）。"""

    def __init__(self, turn=None, aggregate=None, analysis=None):
        self.turn, self.aggregate, self.analysis = turn, aggregate, analysis
        self.kinds: List[str] = []

    async def chat_completion(self, messages, stream=False, temperature=None):
        system = messages[0]['content']
        if system.startswith('你是一个「统计参数抽取器」'):
            kind = 'aggregate'
        elif system.startswith('你是一个「两阶段分析计划生成器」'):
            kind = 'analysis'
        else:
            kind = 'turn'
        self.kinds.append(kind)
        payload = {'turn': self.turn, 'aggregate': self.aggregate,
                   'analysis': self.analysis}[kind]
        if payload is None:
            raise RuntimeError('FakeLLM 未提供 %s 响应' % kind)
        yield json.dumps(payload, ensure_ascii=False)


def _run(rep, message, *, turn=None, aggregate=None, analysis=None):
    saved = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nl.run_nl_query(
            message, F.catalog_of(rep), llm=FakeLLM(turn, aggregate, analysis),
            user_id=0, session_key='sem-reg'))
    finally:
        excel_store.load_representation = saved


def _turn_new_query(columns=(), sheet=F.SMALL_SHEET, filters=None, limit=None):
    """LLM 轮次候选（统计类用例里故意给"普通查询"，让确定性统计兜底接管）。"""
    payload = {'action': 'new_query', 'sheet': sheet, 'columns': list(columns),
               'filters': list(filters or [])}
    if limit:
        payload['limit'] = limit
    return payload


def _agg_candidate(op, column=None, group_by=None, filters=None, order_by=None,
                   order_dir=None, top_n=None, sheet=F.SMALL_SHEET):
    payload = {'action': 'aggregate', 'aggregate_operation': op, 'column': column,
               'group_by': list(group_by or []), 'filters': list(filters or []),
               'sheet': sheet}
    if order_by:
        payload['order_by'] = order_by
        payload['order_dir'] = order_dir
    if top_n:
        payload['top_n'] = top_n
    return payload


# --------------------------------------------------------------------------
# 独立 Ground Truth（本文件手写，不调用生产实现）
# --------------------------------------------------------------------------
def gt_projection(sheet, columns: List[str]) -> Tuple[List[List[Any]], List[int]]:
    idx = [F.column_index(sheet, c) for c in columns]
    rows = [[row[i] if i < len(row) else None for i in idx] for row in sheet.rows]
    return rows, list(sheet.row_excel_numbers)


def gt_aggregate(sheet, op: str, column: Optional[str] = None,
                 row_numbers: Optional[List[int]] = None) -> Dict[str, Any]:
    """独立聚合扫描（count / sum）：空值与非数值分别计数，SUM 只累加可数值化单元格。"""
    if row_numbers is None:
        row_numbers = list(sheet.row_excel_numbers)
    if op == 'count':
        return {'value': float(len(row_numbers)), 'matched_rows': len(row_numbers),
                'numeric_rows': len(row_numbers), 'empty_rows': 0, 'non_numeric_rows': 0}
    ci = F.column_index(sheet, column)
    by_row = {sheet.row_excel_numbers[i]: sheet.rows[i] for i in range(len(sheet.rows))}
    numeric = empty = non_numeric = 0
    total = 0.0
    for r in row_numbers:
        row = by_row[r]
        v = row[ci] if ci < len(row) else None
        if v is None or (isinstance(v, str) and not v.strip()):
            empty += 1
            continue
        num = F.independent_numeric(v)
        if num is None:
            non_numeric += 1
        else:
            numeric += 1
            total += num
    return {'value': round(total, 2), 'matched_rows': len(row_numbers),
            'numeric_rows': numeric, 'empty_rows': empty, 'non_numeric_rows': non_numeric}


def gt_group(sheet, group_column: str, op: str = 'sum',
             value_column: str = AMOUNT) -> Dict[str, Dict[str, Any]]:
    gi, vi = F.column_index(sheet, group_column), F.column_index(sheet, value_column)
    buckets: Dict[str, List[Any]] = {}
    for row in sheet.rows:
        key = str(row[gi] if gi < len(row) else '')
        buckets.setdefault(key, []).append(row[vi] if vi < len(row) else None)
    out: Dict[str, Dict[str, Any]] = {}
    for key, vals in buckets.items():
        numeric = [v for v in vals if F.independent_numeric(v) is not None]
        out[key] = {'value': round(sum(F.independent_numeric(v) for v in numeric), 2),
                    'matched_rows': len(vals), 'numeric_rows': len(numeric)}
    return out


def gt_filter_rows(sheet, column: str, operator: str, value: Any) -> List[int]:
    return F.independent_filter_rows(sheet, column, operator, value)


# ==========================================================================
# A. 确定性语义解析层（纯函数；完全不经过 LLM）
# ==========================================================================
@pytest.mark.parametrize('message,expected,note', [
    ('物流商为 SF 的有几单',
     {'column': CARRIER, 'operator': 'contains', 'value': 'SF'}, '子串 -> contains'),
    ('订单金额为 100.00 的订单',
     {'column': AMOUNT, 'operator': 'eq', 'value': '100.00'}, '与真实单元格完全相等 -> eq'),
    ('客户为张三的订单', None, '列不存在 -> None（宁可不补，也不猜）'),
])
def test_a1_simple_filter_deterministic(small_sheet, message, expected, note):
    got = nl_norm.parse_simple_filter(message, small_sheet.column_names, nl.COLUMN_ALIASES,
                                      lambda col: small_sheet.column_values(
                                          F.column_index(small_sheet, col)))
    assert got == expected, note


def test_a2_simple_filter_value_stops_at_whitespace(small_sheet):
    """既有边界（记录，不修改）：值片段不含空白 -> 多词值被截断为前缀。"""
    got = nl_norm.parse_simple_filter(
        '物流商为Yanwen Express的订单有哪些', small_sheet.column_names, nl.COLUMN_ALIASES,
        lambda col: small_sheet.column_values(F.column_index(small_sheet, col)))
    assert got == {'column': CARRIER, 'operator': 'contains', 'value': 'Yanwen'}


@pytest.mark.parametrize('message,expected', [
    ('订单金额大于100的订单', {'column': AMOUNT, 'operator': 'gt', 'value': 100}),
    ('订单金额小于50的订单', {'column': AMOUNT, 'operator': 'lt', 'value': 50}),
])
def test_a3_comparison_filter_deterministic(small_sheet, message, expected):
    assert nl_norm.parse_comparison_filter(message, small_sheet.column_names,
                                           nl.COLUMN_ALIASES) == expected


def test_a4_multi_condition_two_deterministic_parsers(small_sheet):
    names = small_sheet.column_names
    msg = '物流商为SF且订单金额大于100的订单'
    assert nl_norm.parse_simple_filter(msg, names, nl.COLUMN_ALIASES,
                                       lambda col: small_sheet.column_values(
                                           F.column_index(small_sheet, col))) \
        == {'column': CARRIER, 'operator': 'contains', 'value': 'SF'}
    assert nl_norm.parse_comparison_filter(msg, names, nl.COLUMN_ALIASES) \
        == {'column': AMOUNT, 'operator': 'gt', 'value': 100}


def test_a5_date_expression_deterministic():
    assert nl_norm.parse_date_expression('2026年8月5日的订单') == {
        'kind': 'day', 'year': 2026, 'month': 8, 'day': 5}
    # 既有边界：短横线写法不由此解析器处理（交回 LLM / 澄清）
    assert nl_norm.parse_date_expression('2026-08-05的订单') is None


@pytest.mark.parametrize('message,order_dir', [
    ('按订单金额从高到低列出订单', 'desc'),
    ('按订单金额从低到高列出订单', 'asc'),
])
def test_a6_order_dir_signals(message, order_dir):
    assert nl_norm.detect_signals(message).order_dir == order_dir


def test_a7_topn_and_unit_signals():
    sig = nl_norm.detect_signals('订单金额最高的3个订单')
    assert sig.top_n == 3 and getattr(sig, 'topn_unit', '') == 'group'
    # 「前3笔」是行级单位，不属于统计 TOP-N 单位
    assert getattr(nl_norm.detect_signals('订单金额最高的前3笔订单'), 'topn_unit', '') != 'group'


@pytest.mark.parametrize('message,enumerating', [
    ('把所有 SKU 列出来', True),
    ('列出全部订单', True),
    ('列出所有SKU', True),
    ('列出前20条SKU', False),
    ('下一页', False),
])
def test_a8_enumeration_classification_and_limit(message, enumerating):
    assert nl.is_full_enumeration(message) is enumerating
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=nl.intent_from_dict(
        {'query_type': nl.INTENT_STRUCTURED, 'sheet': F.SMALL_SHEET, 'columns': [SKU]}))
    nl.apply_enumeration_limit(turn, message)
    assert turn.intent.limit == (nl.MAX_NL_LIMIT if enumerating else nl.DEFAULT_NL_LIMIT)


@pytest.mark.parametrize('term,expected', [
    ('物流商', CARRIER), ('订单金额', AMOUNT), ('SKU', SKU), ('订单号', ORDER_ID),
])
def test_a9_column_resolution_with_chinese_aliases(small_sheet, term, expected):
    col, _cands = nl_norm.resolve_column_deterministic(small_sheet.column_names, term,
                                                       nl.COLUMN_ALIASES)
    assert col == expected


def test_a10_sheet_resolution_and_ambiguity(small_rep):
    names = [s.sheet_name for s in small_rep.sheets]
    assert nl_norm.resolve_sheet_deterministic(names, 'SkuMaster')[0] == 'SkuMaster'
    bad, cands = nl_norm.resolve_sheet_deterministic(names, '不存在的表')
    assert bad is None and set(cands) == set(names)


@pytest.mark.parametrize('message,expected', [
    ('订单金额从低到高排列', ('asc', None)),
    ('订单金额从高到低排列', ('desc', None)),
    ('订单金额最高的3个SKU', ('desc', 3)),
])
def test_a11_user_sort_params(message, expected):
    assert nl._user_sort_params(message) == expected


@pytest.mark.parametrize('message,metric,top_n', [
    ('订单金额最高的前3个订单', AMOUNT, 3),      # 权威触发问法（Stage 2A-P0）
    ('订单数量最多的前2个订单', 'Quantity', 2),
])
def test_a12_deterministic_unit_topn_turn(small_rep, monkeypatch, message, metric, top_n):
    """单位级 TOP-N：group_by=[Order ID] + SUM(<度量>) + top_n + desc（确定性口径）。"""
    monkeypatch.setattr(excel_store, 'load_representation',
                        lambda document_id, user_id=None: small_rep)
    turn = nl._deterministic_unit_topn_turn(message, F.catalog_of(small_rep),
                                            nl_norm.detect_signals(message),
                                            nl.intent_from_dict(
                                                {'query_type': nl.INTENT_STRUCTURED,
                                                 'sheet': F.SMALL_SHEET, 'columns': []}))
    assert turn is not None and turn.aggregate is not None
    agg = turn.aggregate
    assert agg.group_by == [ORDER_ID]
    assert agg.column == metric and agg.operation == 'sum'
    assert agg.top_n == top_n and agg.order_dir == 'desc'


def test_a12b_unit_topn_with_dimension_alias_defers_to_group_ranking(small_rep, monkeypatch):
    """既有边界（记录，不修改）：出现维度别名（SKU）时**不**走单位级 TOP-N，交给分组排行。"""
    monkeypatch.setattr(excel_store, 'load_representation',
                        lambda document_id, user_id=None: small_rep)
    assert nl._deterministic_unit_topn_turn(
        '订单金额最高的3个SKU', F.catalog_of(small_rep),
        nl_norm.detect_signals('订单金额最高的3个SKU'),
        nl.intent_from_dict({'query_type': nl.INTENT_STRUCTURED, 'sheet': F.SMALL_SHEET,
                             'columns': []})) is None


def test_a13_metric_authority_picks_amount_column(small_sheet):
    assert nl._deterministic_rank_metric('订单金额最高的10个SKU', small_sheet) == AMOUNT


# ==========================================================================
# B. 端到端语义回归（中文问句 + FakeLLM 候选 -> 真实管线 -> 独立 GT）
# ==========================================================================
# --- 枚举 ---------------------------------------------------------------
def test_b1_enumeration_small_keeps_all_rows(small_rep, small_sheet):
    out = _run(small_rep, '把所有 SKU 列出来', turn=_turn_new_query([SKU]))
    res = out['result']
    rows, row_numbers = gt_projection(small_sheet, [SKU])
    assert [r[0] for r in res['rows']] == [r[0] for r in rows]     # 不去重、顺序一致
    assert res['row_excel_numbers'] == row_numbers == F.SMALL_ALL_ROWS
    assert res['total_matches'] == F.SMALL_ROW_COUNT


def test_b2_enumeration_medium_no_dedupe(medium_rep, medium_sheet):
    out = _run(medium_rep, '列出全部SKU',
               turn=_turn_new_query([SKU], sheet=F.MEDIUM_SHEET))
    res = out['result']
    got = [r[0] for r in res['rows']]
    assert len(got) == F.MEDIUM_ROW_COUNT
    assert got == [row[F.column_index(medium_sheet, SKU)] for row in medium_sheet.rows]
    assert len(set(got)) == F.MEDIUM_SKU_DISTINCT                  # 有重复但不去重
    assert res['matched_row_runs'] == F.row_runs(F.MEDIUM_ALL_ROWS) == [[3, 449]]


# --- 列表 ---------------------------------------------------------------
def test_b3_plain_list_matches_independent_projection(small_rep, small_sheet):
    out = _run(small_rep, '列出订单号和物流商',
               turn=_turn_new_query([ORDER_ID, CARRIER]))
    res = out['result']
    rows, row_numbers = gt_projection(small_sheet, [ORDER_ID, CARRIER])
    assert [[r[0], r[1]] for r in res['rows']] == rows
    assert res['row_excel_numbers'] == row_numbers
    assert res['total_matches'] == F.SMALL_ROW_COUNT


# --- 筛选 ---------------------------------------------------------------
def test_b4_filter_discrete_rows_and_runs(small_rep, small_sheet):
    out = _run(small_rep, '物流商为SF的订单有哪些',
               turn=_turn_new_query([ORDER_ID, CARRIER],
                                    filters=[{'column': CARRIER, 'operator': 'contains',
                                              'value': 'SF'}]))
    res = out['result']
    expected = gt_filter_rows(small_sheet, CARRIER, 'contains', 'SF')
    assert res['row_excel_numbers'] == expected == F.SMALL_SF_ROWS
    assert res['matched_row_runs'] == F.row_runs(expected) == F.SMALL_SF_RUNS
    assert res['matched_row_runs'] != [[3, 19]]                    # 绝不退化成包络


def test_b5_count_filter_matches_independent_scan(small_rep, small_sheet):
    out = _run(small_rep, '物流商为 SF 的有几单',
               turn=_turn_new_query([]),
               aggregate=_agg_candidate('count', None,
                                        filters=[{'column': CARRIER, 'operator': 'contains',
                                                  'value': 'SF'}]))
    agg = out['aggregate']
    expected = gt_filter_rows(small_sheet, CARRIER, 'contains', 'SF')
    assert agg['operation'] == 'count'
    assert agg['matched_rows'] == len(expected) == F.SMALL_SF_MATCHED


def test_b6_empty_and_non_numeric_boundary(small_rep, small_sheet):
    """空值 / 非数值：matched 计入、SUM 不计入（与独立扫描完全一致）。"""
    out = _run(small_rep, '订单金额总和是多少', turn=_turn_new_query([]),
               aggregate=_agg_candidate('sum', AMOUNT))
    agg = out['aggregate']
    gt = gt_aggregate(small_sheet, 'sum', AMOUNT)
    assert agg['matched_rows'] == gt['matched_rows'] == F.SMALL_TOTAL_MATCHED
    assert agg['numeric_rows'] == gt['numeric_rows'] == F.SMALL_TOTAL_NUMERIC
    assert agg['empty_rows'] == gt['empty_rows'] == F.SMALL_TOTAL_EMPTY
    assert agg['non_numeric_rows'] == gt['non_numeric_rows'] == F.SMALL_TOTAL_NON_NUMERIC
    assert round(float(agg['value']), 2) == gt['value'] == F.SMALL_TOTAL_SUM


def test_b7_multi_condition_and_semantics(small_rep, small_sheet):
    """「物流商为SF **且** 订单金额>100」：逐行 AND（独立扫描判定）。"""
    out = _run(small_rep, '物流商为SF且订单金额大于100的订单',
               turn=_turn_new_query([ORDER_ID, AMOUNT],
                                    filters=[{'column': CARRIER, 'operator': 'contains',
                                              'value': 'SF'},
                                             {'column': AMOUNT, 'operator': 'gt',
                                              'value': 100}]))
    res = out['result']
    ci = F.column_index(small_sheet, AMOUNT)
    expected = [small_sheet.row_excel_numbers[i] for i, row in enumerate(small_sheet.rows)
                if 'SF' in str(row[F.column_index(small_sheet, CARRIER)])
                and (F.independent_numeric(row[ci]) or 0) > 100]
    assert expected == [19]                       # 人工可推算：10 / 100 / 空 / 170 中仅 170
    assert res['row_excel_numbers'] == expected
    assert res['matched_row_runs'] == F.row_runs(expected) == [[19, 19]]


# --- 聚合 ---------------------------------------------------------------
def test_b8_sum_aggregate_matches_independent_scan(small_rep, small_sheet):
    out = _run(small_rep, '订单金额总和是多少', turn=_turn_new_query([]),
               aggregate=_agg_candidate('sum', AMOUNT))
    agg = out['aggregate']
    gt = gt_aggregate(small_sheet, 'sum', AMOUNT)
    assert round(float(agg['value']), 2) == gt['value'] == F.SMALL_TOTAL_SUM
    assert agg['matched_row_runs'] == F.SMALL_ALL_RUNS


def test_b9_sum_aggregate_medium(medium_rep, medium_sheet):
    out = _run(medium_rep, '订单金额总和是多少', turn=_turn_new_query([], F.MEDIUM_SHEET),
               aggregate=_agg_candidate('sum', AMOUNT, sheet=F.MEDIUM_SHEET))
    agg = out['aggregate']
    gt = gt_aggregate(medium_sheet, 'sum', AMOUNT)
    assert agg['matched_rows'] == F.MEDIUM_ROW_COUNT
    assert agg['numeric_rows'] == gt['numeric_rows'] == F.MEDIUM_ROW_COUNT
    assert round(float(agg['value']), 2) == gt['value'] == F.MEDIUM_TOTAL_SUM


# --- 分组 ---------------------------------------------------------------
def test_b10_group_by_carrier_matches_independent_group_scan(small_rep, small_sheet):
    out = _run(small_rep, '各个物流商的订单金额总和', turn=_turn_new_query([]),
               aggregate=_agg_candidate('sum', AMOUNT, group_by=[CARRIER],
                                        order_by='aggregate_value', order_dir='desc'))
    g = out['group_aggregate']
    gt = gt_group(small_sheet, CARRIER, 'sum')
    assert {r['group_display'][0]: r['matched_rows'] for r in g['rows']} \
        == {k: v['matched_rows'] for k, v in gt.items()} == F.SMALL_PROVIDER_GROUPS
    assert {r['group_display'][0]: round(float(r['value']), 2) for r in g['rows']} \
        == {k: v['value'] for k, v in gt.items()}
    vals = [round(float(r['value']), 2) for r in g['rows']]
    assert vals == sorted(vals, reverse=True)                      # 排序口径


def test_b11_group_order_dir_asc_reverses(small_rep, small_sheet):
    out = _run(small_rep, '各个物流商的订单金额总和', turn=_turn_new_query([]),
               aggregate=_agg_candidate('sum', AMOUNT, group_by=[CARRIER],
                                        order_by='aggregate_value', order_dir='asc'))
    vals = [round(float(r['value']), 2) for r in out['group_aggregate']['rows']]
    gt = gt_group(small_sheet, CARRIER, 'sum')
    assert vals == sorted(v['value'] for v in gt.values())


# --- 排序 / TOP-N -------------------------------------------------------
def test_b12_group_topn_sku_ranking(small_rep, small_sheet):
    out = _run(small_rep, '订单金额最高的3个SKU', turn=_turn_new_query([]),
               aggregate=_agg_candidate('sum', AMOUNT, group_by=[SKU],
                                        order_by='aggregate_value', order_dir='desc',
                                        top_n=3))
    g = out['group_aggregate']
    gt = gt_group(small_sheet, SKU, 'sum')
    top_expected = sorted(gt.items(), key=lambda kv: (-kv[1]['value'], kv[0]))[:3]
    assert [(k, v['value']) for k, v in top_expected] == [
        (F.SMALL_SKU_ID(5), 1000.00), (F.SMALL_SKU_ID(4), 270.00), (F.SMALL_SKU_ID(3), 130.00)]
    assert [(r['group_display'][0], round(float(r['value']), 2)) for r in g['rows']] \
        == [(k, v['value']) for k, v in top_expected]
    assert g['total_groups'] == 5 and g['returned_groups'] == 3


def test_b13_ranking_unit_authority_order_vs_sku(small_rep, small_sheet, monkeypatch):
    """排名单位口径：无维度别名 -> 单位级 TOP-N（单位=Order ID）；
    有维度别名（SKU）-> 交给分组排行（单位=SKU，由执行结果体现）。"""
    monkeypatch.setattr(excel_store, 'load_representation',
                        lambda document_id, user_id=None: small_rep)
    unit_turn = nl._deterministic_unit_topn_turn(
        '订单金额最高的前3个订单', F.catalog_of(small_rep),
        nl_norm.detect_signals('订单金额最高的前3个订单'),
        nl.intent_from_dict({'query_type': nl.INTENT_STRUCTURED, 'sheet': F.SMALL_SHEET,
                             'columns': []}))
    assert unit_turn.aggregate.group_by == [ORDER_ID]

    out = _run(small_rep, '订单金额最高的3个SKU', turn=_turn_new_query([]),
               aggregate=_agg_candidate('sum', AMOUNT, group_by=[SKU],
                                        order_by='aggregate_value', order_dir='desc',
                                        top_n=3))
    g = out['group_aggregate']
    assert [x['name'] for x in g['group_by']] == [SKU]
    gt = gt_group(small_sheet, SKU, 'sum')
    assert len(g['rows']) == 3 and g['total_groups'] == len(gt) == 5


# --- 日期 ---------------------------------------------------------------
def test_b14_date_condition_lands_and_filters(small_rep, small_sheet):
    """日期语义：由确定性时间落点产出 date_between，并按独立扫描核对命中与聚合值。"""
    out = _run(small_rep, '2026年8月5日的订单金额总和', turn=_turn_new_query([]),
               aggregate=_agg_candidate('sum', AMOUNT))
    agg = out['aggregate']
    applied = agg['applied_filters']
    assert applied and applied[0]['column'] == CREATED              # 落到真实日期列
    assert applied[0]['operator'] == 'date_between'
    assert list(applied[0]['value']) == ['2026-08-05', '2026-08-05']
    ci = F.column_index(small_sheet, CREATED)
    expected = [small_sheet.row_excel_numbers[i] for i, row in enumerate(small_sheet.rows)
                if str(row[ci]).startswith('2026-08-05')]
    assert expected == [7]                                          # i=4 -> Excel 行 7
    assert agg['matched_rows'] == 1
    assert round(float(agg['value']), 2) == 50.00
    assert agg['matched_row_runs'] == [[7, 7]]


def test_b15_date_condition_in_multi_step_step1(small_rep, small_sheet):
    """两步分析的日期条件必须落在 **step1**（Stage 2A D1 修复的回归护栏）。"""
    out = _run(small_rep, '2026年8月5日订单金额最高的前3个SKU的销售额总和',
               turn=_turn_new_query([]),
               analysis={'document': F.GOLDEN_SMALL_NAME, 'sheet': F.SMALL_SHEET,
                         'steps': [
                             {'type': 'group_aggregate', 'group_by': [SKU],
                              'operation': 'sum', 'column': AMOUNT,
                              'order_by': 'aggregate_value', 'order_dir': 'desc',
                              'top_n': 3},
                             {'type': 'aggregate', 'operation': 'sum',
                              'source': 'step_1', 'column': ms.INTERMEDIATE_VALUE_COLUMN}]})
    m = out['multi_step']
    step1_filters = m['step1']['filters']
    assert step1_filters and step1_filters[0]['column'] == CREATED
    assert step1_filters[0]['operator'] == 'date_between'
    # 该日期只有 1 行 -> step1 只有 1 个分组（独立可推算）
    assert m['step1']['matched_rows'] == 1
    assert m['step2']['input_rows'] == 1


# --- 边界：18 位 ID / 重复 SKU ------------------------------------------
def test_b16_eighteen_digit_ids_keep_full_precision(small_rep, small_sheet):
    """18 位 ID 逐字符保持（不发生 float / 科学计数法 / 截断）。"""
    out = _run(small_rep, '列出订单号', turn=_turn_new_query([ORDER_ID]))
    got = [r[0] for r in out['result']['rows']]
    expected = [row[F.column_index(small_sheet, ORDER_ID)] for row in small_sheet.rows]
    assert got == expected
    assert all(isinstance(v, str) and len(v) == 18 for v in got)
    blob = json.dumps(out['result'], ensure_ascii=False)
    assert 'e+' not in blob and 'e-' not in blob                    # 不出现科学计数法
    for value in expected:
        assert value in blob                                        # 原样序列化


def test_b17_duplicate_sku_keeps_row_semantics(small_rep, small_sheet):
    """重复 SKU：枚举保留全部行；只有显式分组才聚合。"""
    enum_out = _run(small_rep, '把所有 SKU 列出来', turn=_turn_new_query([SKU]))
    assert len(enum_out['result']['rows']) == F.SMALL_ROW_COUNT
    grp_out = _run(small_rep, '各个SKU的订单金额总和', turn=_turn_new_query([]),
                   aggregate=_agg_candidate('sum', AMOUNT, group_by=[SKU]))
    gt = gt_group(small_sheet, SKU, 'sum')
    groups = {r['group_display'][0]: r['matched_rows']
              for r in grp_out['group_aggregate']['rows']}
    assert groups == {k: v['matched_rows'] for k, v in gt.items()}
    assert sum(groups.values()) == F.SMALL_ROW_COUNT
    assert len(groups) == 5                                          # 19 行 -> 5 组


# ==========================================================================
# C. Authority 覆盖（17 项口径：回归护栏）
# ==========================================================================
def test_c1_document_authority_user_text_picks_document(small_rep):
    other = dict(F.catalog_of(small_rep)[0])
    other['document_id'], other['filename'] = 'other-doc', 'golden_medium.xlsx'
    catalog = F.catalog_of(small_rep) + [other]
    doc, cands = nl.resolve_document(catalog, F.GOLDEN_SMALL_NAME)
    assert doc and doc['document_id'] == small_rep.document_id and not cands


def test_c2_sheet_authority_second_sheet_selectable(small_rep):
    out = _run(small_rep, '列出SkuMaster的SKU ID',
               turn=_turn_new_query([SKU], sheet='SkuMaster'))
    assert out['sheet']['sheet_name'] == 'SkuMaster'
    assert out['result']['total_matches'] == 5


def test_c3_unknown_sheet_clarifies(small_rep):
    out = _run(small_rep, '列出不存在表的SKU',
               turn=_turn_new_query([SKU], sheet='不存在的表'))
    assert out['status'] != nl.STATUS_OK


def test_c4_group_by_authority(small_rep):
    out = _run(small_rep, '各个物流商的订单金额总和', turn=_turn_new_query([]),
               aggregate=_agg_candidate('sum', AMOUNT, group_by=[CARRIER]))
    assert [g['name'] for g in out['group_aggregate']['group_by']] == [CARRIER]


@pytest.mark.parametrize('op,column,message', [
    ('sum', AMOUNT, '订单金额总和是多少'),
    ('count', None, '有多少订单'),
    ('avg', AMOUNT, '订单金额平均是多少'),
])
def test_c5_operation_authority(small_rep, op, column, message):
    out = _run(small_rep, message, turn=_turn_new_query([]),
               aggregate=_agg_candidate(op, column))
    assert out['aggregate']['operation'] == op


def test_c6_filter_authority_keeps_column_operator_value(small_rep):
    out = _run(small_rep, '物流商为SF的订单有哪些',
               turn=_turn_new_query([ORDER_ID],
                                    filters=[{'column': CARRIER, 'operator': 'contains',
                                              'value': 'SF'}]))
    applied = out['result']['applied_filters'][0]
    assert (applied['column'], applied['operator'], applied['value']) \
        == (CARRIER, 'contains', 'SF')


def test_c7_order_by_and_dir_authority(small_rep):
    out = _run(small_rep, '各个物流商的订单金额总和', turn=_turn_new_query([]),
               aggregate=_agg_candidate('sum', AMOUNT, group_by=[CARRIER],
                                        order_by='aggregate_value', order_dir='desc'))
    g = out['group_aggregate']
    assert (g['order_by'], g['order_dir']) == ('aggregate_value', 'desc')
    assert g['order_by_label']


def test_c8_top_n_authority_truncates(small_rep):
    out = _run(small_rep, '订单金额最高的2个SKU', turn=_turn_new_query([]),
               aggregate=_agg_candidate('sum', AMOUNT, group_by=[SKU],
                                        order_by='aggregate_value', order_dir='desc',
                                        top_n=2))
    g = out['group_aggregate']
    assert g['top_n'] == 2 and g['returned_groups'] == 2 and g['total_groups'] == 5
    assert g['truncated_by_top_n'] is True


def test_c9_analysis_step1_step2_authority(small_rep, small_sheet):
    """multi-step：step1 = SKU 分组 SUM + top3；step2 = SUM(step1 值)（不回原始行）。"""
    out = _run(small_rep, '订单金额最高的前3个SKU的销售额总和',
               turn=_turn_new_query([]),
               analysis={'document': F.GOLDEN_SMALL_NAME, 'sheet': F.SMALL_SHEET,
                         'steps': [
                             {'type': 'group_aggregate', 'group_by': [SKU],
                              'operation': 'sum', 'column': AMOUNT,
                              'order_by': 'aggregate_value', 'order_dir': 'desc',
                              'top_n': 3},
                             {'type': 'aggregate', 'operation': 'sum',
                              'source': 'step_1', 'column': ms.INTERMEDIATE_VALUE_COLUMN}]})
    m = out['multi_step']
    gt = gt_group(small_sheet, SKU, 'sum')
    top3 = sorted(gt.items(), key=lambda kv: (-kv[1]['value'], kv[0]))[:3]
    assert round(float(m['value']), 2) == round(sum(v['value'] for _k, v in top3), 2) \
        == F.SMALL_MULTI_STEP_VALUE
    assert m['step1']['returned_groups'] == 3
    assert m['step2']['input_rows'] == 3 and m['step2']['numeric_rows'] == 3
    assert m['step2']['source'] == 'step_1'
    assert m['step1']['matched_row_runs'] == F.SMALL_ALL_RUNS
