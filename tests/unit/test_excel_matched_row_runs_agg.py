# -*- coding: utf-8 -*-
"""相邻 P2：aggregate / group / multi_step 的「匹配 Excel 行」改为**真实连续段**。

背景（只读审查确认）：表格卡片已用真实最大连续段（`matched_row_runs`），
而单值统计 / 分组统计 / 多步第 1 步仍用**包络** `row_excel_spans={first,last}`：
真实命中 3、12、18、19 时显示 `3 ~ 19`，会被误读为"连续命中 3~19"。

本轮只改**解释/展示**（不碰 filter / parser / authority / group_by / 聚合值 / SQL）：
  * 数据来源（零新增 SQL）：
      - 单值统计 DuckDB：`rows_sql` **本就取回全部命中行号**（fetched）；
      - 单值统计 Python：逐行收集的 `excel_rows`；
      - 分组统计 Python：`_matched_excel_rows` 的完整列表；
      - 分组统计 DuckDB：`totals_sql` 只有 MIN/MAX（包络），因此复用本模块既有的确定性
        匹配器 `_matched_excel_rows`（与 Python 引擎同一实现，不新增 SQL、不改任何聚合值）；
  * 折叠算法**复用** `query.collapse_row_runs` + `MAX_MATCHED_ROW_RUNS`（与表格同一套，不重造）；
  * 拿不到 runs（旧快照 / 无命中）时回退既有包络文案，**绝不伪造连续**。
"""
import glob
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.api.v1.rag import EXCEL_HISTORY_SCHEMA_VERSION, build_excel_history_snapshot
from backend.excel import aggregate as ag
from backend.excel import engine as query_engine
from backend.excel import multi_step as ms
from backend.excel import nl_query as nl
from backend.excel import store as excel_store

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CARRIER = 'Shipping Provider Name'
SF = [{'column': CARRIER, 'operator': 'contains', 'value': 'SF'}]
NONE_HIT = [{'column': CARRIER, 'operator': 'contains', 'value': 'ZZZ_NO_SUCH_VALUE'}]
ENGINES = (query_engine.ENGINE_DUCKDB, query_engine.ENGINE_PYTHON)


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
def small_rep() -> 'nl.WorkbookRepresentation':
    """19 行表：contains SF 的真实命中为 3、12、18、19（**不连续**）。"""
    return _load_rep(10, 30)


@pytest.fixture(scope='module')
def big_rep() -> 'nl.WorkbookRepresentation':
    """447 行表：无筛选时全命中 3~449（连续）。"""
    return _load_rep(400, 600)


def _agg(rep, payload: Dict[str, Any], engine: str):
    return query_engine.run_aggregate(rep, payload, engine=engine)


# ==========================================================================
# A / B / C / D：连续 / 离散 / 单行 / 空
# ==========================================================================
@pytest.mark.parametrize('engine', ENGINES)
def test_a_continuous_matches_single_run(big_rep, engine):
    res, used = _agg(big_rep, {'sheet_index': 0, 'operation': 'count'}, engine)
    d = res.to_dict()
    assert d['matched_rows'] == 447
    assert d['row_excel_spans'] == {'first': 3, 'last': 449}      # 既有字段保持
    assert d['matched_row_runs'] == [[3, 449]]
    assert d['matched_row_run_count'] == 1
    assert d['matched_row_runs_truncated'] is False
    text = ag.format_aggregate_summary(res, big_rep.filename)
    assert '- 匹配 Excel 行：3 ~ 449（共 447 行）' in text          # 与既有断言兼容


@pytest.mark.parametrize('engine', ENGINES)
def test_b_discontinuous_matches_keep_gaps(small_rep, engine):
    """核心用例：真实命中 3、12、18、19 -> 必须表达为 `3、12、18~19`，**不是** `3 ~ 19`。"""
    res, _used = _agg(small_rep, {'sheet_index': 0, 'operation': 'count', 'filters': SF}, engine)
    d = res.to_dict()
    assert d['matched_rows'] == 4
    assert d['row_excel_spans'] == {'first': 3, 'last': 19}        # 包络仍在（兼容）
    assert d['matched_row_runs'] == [[3, 3], [12, 12], [18, 19]]   # 真实连续段
    assert d['matched_row_runs'] != [[3, 19]]                      # 绝不折叠成包络
    assert d['matched_row_run_count'] == 3
    assert d['row_excel_numbers'][:4] == [3, 12, 18, 19]           # 行号数据未被改动
    text = ag.format_aggregate_summary(res, small_rep.filename)
    assert '- 匹配 Excel 行：3、12、18~19（共 4 行）' in text


@pytest.mark.parametrize('engine', ENGINES)
def test_c_single_row_match(small_rep, engine):
    """单行命中 -> 只显示该行号（不留 `7 ~ 7`）。"""
    sheet = small_rep.sheets[0]
    ci = sheet.column_names.index('Order ID')
    unique = sheet.rows[0][ci]
    res, _u = _agg(small_rep, {'sheet_index': 0, 'operation': 'count',
                               'filters': [{'column': 'Order ID', 'operator': 'eq',
                                            'value': str(unique)}]}, engine)
    d = res.to_dict()
    assert d['matched_rows'] == 1
    assert d['matched_row_runs'] == [[3, 3]]
    assert '- 匹配 Excel 行：3（共 1 行）' in ag.format_aggregate_summary(res, small_rep.filename)


@pytest.mark.parametrize('engine', ENGINES)
def test_d_empty_result_has_no_line(small_rep, engine):
    """空结果 -> 不显示行信息（也不伪造 0）。"""
    res, _u = _agg(small_rep, {'sheet_index': 0, 'operation': 'count', 'filters': NONE_HIT}, engine)
    d = res.to_dict()
    assert d['matched_rows'] == 0
    assert d['matched_row_runs'] == [] and d['matched_row_run_count'] == 0
    assert d['row_excel_spans'] is None
    assert '匹配 Excel 行' not in ag.format_aggregate_summary(res, small_rep.filename)


# ==========================================================================
# E：段数超过 cap 的折叠文案（与表格同一套 cap 机制）
# ==========================================================================
def test_e_truncated_runs_text():
    pairs = [[2 * i + 1, 2 * i + 1] for i in range(8)]
    assert ag.format_matched_row_runs(pairs, run_count=87, truncated=True, total_matches=447) \
        == '共 447 个命中行，分布在 87 段｜范围 1 ~ 15'
    # 未截断时逐段列出
    assert ag.format_matched_row_runs([[3, 3], [12, 12], [18, 19]]) == '3、12、18~19'
    assert ag.format_matched_row_runs([[3, 449]]) == '3 ~ 449'
    assert ag.format_matched_row_runs([[7, 7]]) == '7'
    assert ag.format_matched_row_runs([]) == ''


# ==========================================================================
# F：分组统计（card + narrative 同源）
# ==========================================================================
@pytest.mark.parametrize('engine', ENGINES)
def test_f_group_aggregate_discontinuous(small_rep, engine):
    res, _u = _agg(small_rep, {'sheet_index': 0, 'operation': 'sum', 'column': 'Order Amount',
                               'group_by': [CARRIER], 'filters': SF}, engine)
    d = res.to_dict()
    assert d['kind'] == 'group_aggregate'
    assert d['matched_rows'] == 4
    assert d['matched_row_runs'] == [[3, 3], [12, 12], [18, 19]]
    assert d['row_excel_spans'] == {'first': 3, 'last': 19}        # 兼容
    text = ag.format_group_aggregate_summary(res, small_rep.filename)
    assert '- 匹配 Excel 行：3、12、18~19（共 4 行）' in text
    assert '3 ~ 19（共 4 行）' not in text                        # 不再出现包络式表达


@pytest.mark.parametrize('engine', ENGINES)
def test_f2_group_aggregate_continuous(big_rep, engine):
    res, _u = _agg(big_rep, {'sheet_index': 0, 'operation': 'sum', 'column': 'Order Amount',
                             'group_by': [CARRIER]}, engine)
    d = res.to_dict()
    assert d['matched_rows'] == 447 and d['matched_row_runs'] == [[3, 449]]
    assert '- 匹配 Excel 行：3 ~ 449（共 447 行）' in ag.format_group_aggregate_summary(
        res, big_rep.filename)


# ==========================================================================
# G：multi_step 第 1 步（叙述 + card 同源）
# ==========================================================================
def _analysis_plan(filters=None) -> Dict[str, Any]:
    return {'steps': [
        {'type': ms.STEP_GROUP_AGGREGATE, 'group_by': ['SKU ID'],
         'operation': ag.OPERATION_SUM, 'column': 'Order Amount',
         'order_by': ag.ORDER_BY_AGGREGATE, 'order_dir': 'desc', 'top_n': 3,
         'filters': list(filters or [])},
        {'type': ms.STEP_AGGREGATE, 'operation': ag.OPERATION_SUM,
         'source': ms.SOURCE_STEP_1, 'column': ms.INTERMEDIATE_VALUE_COLUMN}]}


def _run_analysis(rep, plan):
    saved = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return ms.execute_analysis(rep, ms.normalize_analysis_plan(plan))[0]
    finally:
        excel_store.load_representation = saved


def test_g_multi_step_discontinuous(small_rep):
    result = _run_analysis(small_rep, _analysis_plan(filters=SF))
    d = result.to_dict()
    s1 = d['step1']
    assert s1['matched_row_runs'] == [[3, 3], [12, 12], [18, 19]]
    assert s1['row_excel_spans'] == {'first': 3, 'last': 19}       # G1 字段保持
    assert s1['matched_row_run_count'] == 3
    text = ms.format_analysis_summary(result)
    assert '- 第 1 步匹配 Excel 行：3、12、18~19（共 4 行）' in text
    assert '不会重新回到原始 Excel 数据行' in text                    # R4 语义未变


def test_g2_multi_step_continuous(small_rep):
    result = _run_analysis(small_rep, _analysis_plan())
    s1 = result.to_dict()['step1']
    assert s1['matched_row_runs'] == [[3, 21]]                     # 19 行全命中（3..21）
    assert '- 第 1 步匹配 Excel 行：3 ~ 21（共 19 行）' in ms.format_analysis_summary(result)


# ==========================================================================
# H / I：snapshot（新字段落库 + 旧快照降级），schema 不变
# ==========================================================================
def _outcome_for(rep, payload_kind: str) -> Dict[str, Any]:
    import asyncio

    if payload_kind == 'aggregate':
        turn = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
            operation=ag.OPERATION_COUNT, column=None, filters=SF))
        msg = '物流商为SF的有几单'
    elif payload_kind == 'group':
        turn = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
            operation=ag.OPERATION_SUM, column='Order Amount', group_by=[CARRIER], filters=SF))
        msg = '各物流商的订单金额汇总'
    else:
        turn = nl.TurnIntent(action=nl.ACTION_ANALYSIS,
                             analysis=nl.AnalysisIntent(steps=_analysis_plan(filters=SF)['steps']))
        msg = '订单金额最高的前3个SKU的销售额总和'

    class _F:
        pass

    orig = nl.llm_parse_turn

    async def fake(llm, message, catalog, context=None, analysis_context=None):
        return turn
    nl.llm_parse_turn = fake
    saved = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nl.run_nl_query(msg, [{
            'document_id': rep.document_id, 'filename': rep.filename, 'file_type': rep.file_type,
            'created_at': rep.created_at, 'total_rows': rep.total_rows,
            'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                        'row_count': s.row_count, 'column_count': s.column_count,
                        'columns': s.column_names} for s in rep.sheets]}],
            llm=_F(), user_id=1, session_key='runs-%s' % payload_kind))
    finally:
        nl.llm_parse_turn = orig
        excel_store.load_representation = saved


@pytest.mark.parametrize('kind', ['aggregate', 'group', 'multi_step'])
def test_h_snapshot_keeps_runs(small_rep, kind):
    out = _outcome_for(small_rep, kind)
    snap = build_excel_history_snapshot(out)
    assert snap['schema_version'] == EXCEL_HISTORY_SCHEMA_VERSION == 1
    payload = snap['payload']
    if kind == 'multi_step':
        # multi_step 的行信息属于第 1 步（step1），不在顶层
        assert payload['step1']['matched_row_runs'] == [[3, 3], [12, 12], [18, 19]]
        assert payload['step1']['matched_row_run_count'] == 3
        assert payload['step1']['matched_row_runs_truncated'] is False
        assert payload['step1']['row_excel_spans'] == {'first': 3, 'last': 19}
    else:
        assert payload['matched_row_runs'] == [[3, 3], [12, 12], [18, 19]]
        assert payload['matched_row_run_count'] == 3
        assert payload['matched_row_runs_truncated'] is False


@pytest.mark.parametrize('kind', ['aggregate', 'group', 'multi_step'])
def test_i_old_snapshot_without_runs_degrades(small_rep, kind):
    """旧快照缺新字段 -> 仍可构建/序列化（前端回退包络文案）；版本不变、不回填。"""
    out = _outcome_for(small_rep, kind)
    key = {'aggregate': 'aggregate', 'group': 'group_aggregate', 'multi_step': 'multi_step'}[kind]
    legacy = dict(out)
    body = dict(out[key])
    for k in list(body):
        if k.startswith('matched_row_'):
            body.pop(k)
    if kind == 'multi_step':
        body['step1'] = {k: v for k, v in body['step1'].items()
                         if not k.startswith('matched_row_')}
    legacy[key] = body

    snap = build_excel_history_snapshot(legacy)
    assert snap is not None
    payload = snap['payload']
    assert snap['schema_version'] == 1
    assert 'matched_row_runs' not in payload
    if kind == 'multi_step':
        assert 'matched_row_runs' not in payload['step1']
        assert payload['step1']['row_excel_spans'] == {'first': 3, 'last': 19}
    else:
        assert payload['row_excel_spans'] == {'first': 3, 'last': 19}   # 包络仍可用（降级展示）
