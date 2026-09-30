# -*- coding: utf-8 -*-
"""Stage 5 API Regression — `POST /api/v1/rag/excel/nl-query`（HTTP -> authority -> 执行 -> 结果）。

本文件只验证 **API 层完整链路**（HTTP 入口 → service → 确定性 authority → 执行 → 返回体），
NL parser / authority 的内部细节已由 tests/unit 覆盖（含 F3 / F4 的单元回归）。

FakeProviderLLM 只控制"模型返回值"：
  * 故意返回**错误候选**（如把「有多少订单」答成 SUM），用于在 HTTP 层确认
    F3 / F4 的 authority 仍然生效（不是只断言数字，同时断言最终执行口径）。
  * authority / 执行 / DuckDB / 历史落库全部真实。
"""
import pytest

from tests.helpers import fixtures as F
from .conftest import GOLDEN_MEDIUM, GOLDEN_SMALL

CARRIER, AMOUNT, SKU = F.CARRIER, F.AMOUNT, F.SKU


@pytest.fixture()
def ctx(api):
    headers = api.user('nl_user')
    doc = api.upload_excel(headers, GOLDEN_SMALL)
    return api, headers, doc


def _turn_new(columns, sheet=F.SMALL_SHEET, filters=None):
    return {'action': 'new_query', 'sheet': sheet, 'columns': list(columns),
            'filters': list(filters or [])}


def _turn_agg(op, column=None, group_by=None, filters=None, sheet=F.SMALL_SHEET,
              order_by=None, order_dir=None, top_n=None):
    payload = {'action': 'aggregate', 'aggregate_operation': op, 'column': column,
               'group_by': list(group_by or []), 'filters': list(filters or []),
               'sheet': sheet}
    if order_by:
        payload['order_by'] = order_by
        payload['order_dir'] = order_dir
    if top_n:
        payload['top_n'] = top_n
    return payload


def _analysis_plan(step1_group, step1_op='sum', step1_column=AMOUNT, top_n=3, step2_op='sum'):
    from backend.excel import multi_step as ms
    return {'action': 'analysis', 'sheet': F.SMALL_SHEET, 'steps': [
        {'type': ms.STEP_GROUP_AGGREGATE, 'group_by': list(step1_group),
         'operation': step1_op, 'column': step1_column, 'filters': [],
         'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': top_n},
        {'type': ms.STEP_AGGREGATE, 'operation': step2_op, 'source': ms.SOURCE_STEP_1,
         'column': ms.INTERMEDIATE_VALUE_COLUMN}]}


# ==========================================================================
# §五：五条核心问法（HTTP 层）
# ==========================================================================
def test_how_many_orders_counts_rows_over_wrong_sum_candidate(ctx):
    """F4（HTTP 层）：「有多少订单」+ 模型给 `sum/Order Amount` -> 24 行里本表 19 行。"""
    api, headers, doc = ctx
    api.fake.turn_payload = _turn_agg('sum', AMOUNT)          # 故意给错候选
    body = api.nl_ok(headers, '有多少订单')

    agg = body['aggregate']
    assert agg['operation'] == 'count'                        # authority 纠正生效
    assert agg['column'] is None
    assert agg['matched_rows'] == F.SMALL_ROW_COUNT == 19
    assert round(float(agg['value']), 2) == 19.0
    assert round(float(agg['value']), 2) != F.SMALL_TOTAL_SUM  # 不是 1550.00


def test_count_with_filter_returns_matched_rows(ctx):
    """「物流商为SF的有几单」-> 4（表格过滤 + COUNT(*)）。"""
    api, headers, doc = ctx
    api.fake.turn_payload = _turn_agg(
        'count', None, filters=[{'column': CARRIER, 'operator': 'contains', 'value': 'SF'}])
    body = api.nl_ok(headers, '物流商为SF的有几单')

    agg = body['aggregate']
    assert agg['operation'] == 'count'
    assert agg['matched_rows'] == F.SMALL_SF_MATCHED == 4
    applied = [(f['column'], f['operator'], f['value']) for f in agg['applied_filters']]
    assert applied == [(CARRIER, 'contains', 'SF')]


def test_sum_of_order_amount(ctx):
    """「订单金额总和是多少」-> 1550.00。"""
    api, headers, doc = ctx
    api.fake.turn_payload = _turn_agg('sum', AMOUNT)
    body = api.nl_ok(headers, '订单金额总和是多少')

    agg = body['aggregate']
    assert agg['operation'] == 'sum' and agg['column'] == AMOUNT
    assert round(float(agg['value']), 2) == F.SMALL_TOTAL_SUM == 1550.00
    assert agg['matched_rows'] == 19


def test_grouped_count_per_provider(ctx):
    """「各物流商分别有多少订单」-> 分组 COUNT：SF 4 / JS 7 / Yanwen 8。"""
    api, headers, doc = ctx
    api.fake.turn_payload = _turn_agg('count', None, group_by=[CARRIER])
    body = api.nl_ok(headers, '各物流商分别有多少订单')

    group = body['group_aggregate']
    assert group['operation'] == 'count' and group['column'] is None
    got = {r['group_display'][0]: int(r['value']) for r in group['rows']}
    assert got == F.SMALL_PROVIDER_GROUPS == {'SF International': 4,
                                             'JS Express International': 7,
                                             'Yanwen Express': 8}
    assert sum(got.values()) == 19


def test_multi_step_step1_authority_survives_guard(api, ctx):
    """F3（HTTP 层）：两步分析第 1 步口径错误候选必须被 authority 纠正（守卫路径同理）。

    模型第 1 次与守卫第 2 次都给 `step1.operation=count`；用户原文是「订单金额最高的前3个
    SKU 的销售额总和」-> 最终必须以 SUM(Order Amount) 取 top3 再求和 = 1400.00。
    """
    _api, headers, doc = ctx
    plan = _analysis_plan([SKU], step1_op='count', step1_column=None)
    api.fake.turn_payload = plan
    api.fake.analysis_payload = plan                # 守卫返回同一份未修复计划
    body = api.nl_ok(headers, '订单金额最高的前3个SKU的销售额总和是多少')

    multi = body['multi_step']
    step1 = multi['step1']
    assert step1['operation'] == 'sum'              # 不是 count
    assert step1['column'] == AMOUNT
    assert step1['top_n'] == 3
    assert round(float(multi['value']), 2) == F.SMALL_MULTI_STEP_VALUE == 1400.00
    assert multi['step2']['input_rows'] == 3


# ==========================================================================
# §六：Golden B 全量枚举（447 行、不去重）
# ==========================================================================
def test_full_enumeration_returns_all_447_rows_without_dedupe(api):
    """「列出全部SKU」-> 447 行（不是前 50、不去重、无分页丢失）。"""
    headers = api.user('enum_user')
    api.upload_excel(headers, GOLDEN_MEDIUM)
    api.fake.turn_payload = _turn_new([SKU], sheet=F.MEDIUM_SHEET)

    body = api.nl_ok(headers, '列出全部SKU')
    result = body['result']

    assert result['total_matches'] == F.MEDIUM_ROW_COUNT == 447
    assert len(result['rows']) == 447                      # 不是默认 50
    assert result['has_more'] is False
    assert result['matched_row_runs'] == [[3, 449]]
    got = [r[0] for r in result['rows']]
    assert len(got) == 447 and len(set(got)) == F.MEDIUM_SKU_DISTINCT == 120   # 有重复、不去重

    sheet = F.sheet_of(F.parse_golden(F.GOLDEN_MEDIUM_NAME), F.MEDIUM_SHEET)
    expected = [row[F.column_index(sheet, SKU)] for row in sheet.rows]          # 独立 GT
    assert got == expected                                                      # 顺序一致


# ==========================================================================
# 入参 / 鉴权边界
# ==========================================================================
def test_nl_query_requires_authentication(api):
    resp = api.client.post('/api/v1/rag/excel/nl-query', json={'message': '有多少订单'})
    assert resp.status_code == 401


def test_nl_query_empty_message_is_400(api, ctx):
    _api, headers, _doc = ctx
    resp = api.client.post('/api/v1/rag/excel/nl-query', headers=headers,
                           json={'message': '   '})
    assert resp.status_code == 400


def test_nl_query_without_any_excel_clarifies(api):
    """没有任何表格的用户：明确澄清，不编造结果（HTTP 仍 200）。"""
    headers = api.user('empty_user')
    api.fake.turn_payload = _turn_new([SKU])
    resp = api.nl_query(headers, '有多少订单')
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['status'] != 'ok'
    assert not body.get('aggregate') and not body.get('result')
