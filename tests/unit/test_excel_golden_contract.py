# -*- coding: utf-8 -*-
"""Stage 5：Golden fixture +「实时结果 → snapshot → SQLite → history」跨层契约。

本文件是**自动化回归护栏**，全部基于 `tests/fixtures/excel/` 下的**合成、去标识化**
Golden 数据（绝不使用真实业务 Excel、绝不触碰用户 `data/`、**零真实 LLM 调用**）。

契约覆盖（每一次性能/重构都应保持通过）：
  * Golden A（19 行 / 2 Sheet）：table / aggregate（含 relaxed_filters）/ group / multi-step
  * Golden B（447 行）：全量枚举（不去重）、大表统计
  * snapshot 顶层与 payload 的关键字段（schema_version=1、matched_row_runs、relaxed_filters、
    row provenance、multi-step step1/step2）
  * snapshot → persist_excel_turn → get_history：source=excel、payload 完整、>100 条历史仍能看到
  * 与**共享快照 fixture**（tests/fixtures/excel_history/*.json，前端 restore 契约读同一份）结构一致

独立性保证：
  * 期望值来自 `tests/helpers/fixtures.py` 的**人工可推算**常量与**独立**扫描函数
    （`row_runs` / `independent_filter_rows` / `independent_amount_stats`）；
    **不**调用生产 `collapse_row_runs()`、**不**用被测实现产出 expected。
"""
import json
import os
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api.v1.rag import (EXCEL_HISTORY_SCHEMA_VERSION, build_excel_history_snapshot,
                                build_excel_turn_meta, persist_excel_turn)
from backend.database.base import Base
from backend.excel import aggregate as ag
from backend.excel import multi_step as ms
from backend.excel import nl_query as nq
from backend.excel import store as excel_store
from backend.models import character as _character_models  # noqa: F401  (建表用)
from backend.services import conversation_service as conv_mod
from backend.services.conversation_service import conversation_service as conv
from tests.helpers import fixtures as F


class _FakeLLM:
    """确定性注入用占位（绝不触达真实 provider）。"""


# --------------------------------------------------------------------------
# 夹具：证明不依赖用户 data/（fresh clone 也能跑）
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_user_data_dir(tmp_path, monkeypatch):
    """把 Excel 存储根指向一个空目录：本文件所有测试都不得依赖用户 `data/`。"""
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path / 'no_user_data')


@pytest.fixture(scope='module')
def small_rep():
    return F.parse_golden(F.GOLDEN_SMALL_NAME)


@pytest.fixture(scope='module')
def medium_rep():
    return F.parse_golden(F.GOLDEN_MEDIUM_NAME)


def _run(rep, message, turn, key='golden'):
    """真实 NL 管线（LLM 解析被替换为确定性注入），返回 outcome。"""
    orig_turn = nq.llm_parse_turn
    orig_load = excel_store.load_representation

    async def fake(llm, msg, catalog, context=None, analysis_context=None):
        return turn
    nq.llm_parse_turn = fake
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        import asyncio
        return asyncio.run(nq.run_nl_query(message, F.catalog_of(rep), llm=_FakeLLM(),
                                           user_id=0, session_key=key))
    finally:
        nq.llm_parse_turn = orig_turn
        excel_store.load_representation = orig_load


def _table_turn(columns, limit=50):
    return nq.TurnIntent(action=nq.ACTION_NEW_QUERY, intent=nq.intent_from_dict(
        {'query_type': nq.INTENT_STRUCTURED, 'sheet': F.SMALL_SHEET,
         'columns': list(columns), 'limit': limit}))


def _agg_turn(op, column=None, filters=None):
    return nq.TurnIntent(action=nq.ACTION_AGGREGATE, aggregate=nq.AggregateIntent(
        operation=op, column=column, sheet=F.SMALL_SHEET, filters=list(filters or [])))


def _group_turn():
    return nq.TurnIntent(action=nq.ACTION_AGGREGATE, aggregate=nq.AggregateIntent(
        operation=ag.OPERATION_SUM, column=F.AMOUNT, group_by=[F.CARRIER],
        sheet=F.SMALL_SHEET, order_by=ag.ORDER_BY_AGGREGATE, order_dir='desc'))


def _multi_turn():
    return nq.TurnIntent(action=nq.ACTION_ANALYSIS, analysis=nq.AnalysisIntent(
        sheet=F.SMALL_SHEET, steps=[
            {'type': ms.STEP_GROUP_AGGREGATE, 'group_by': [F.SKU],
             'operation': ag.OPERATION_SUM, 'column': F.AMOUNT,
             'order_by': ag.ORDER_BY_AGGREGATE, 'order_dir': 'desc', 'top_n': 3},
            {'type': ms.STEP_AGGREGATE, 'operation': ag.OPERATION_SUM,
             'source': ms.SOURCE_STEP_1, 'column': ms.INTERMEDIATE_VALUE_COLUMN}]))


# ==========================================================================
# 0) Golden 数据本身：合成 / 无 PII / 结构确定
# ==========================================================================
PII_COLUMNS = ('Buyer Nickname', 'Buyer Username', 'Recipient', 'Phone #', 'Buyer Message',
               'Address Line 1', 'Address Line 2', 'Zipcode', 'Delivery Instruction')


def test_golden_fixtures_exist_and_are_synthetic(small_rep, medium_rep):
    small = F.sheet_of(small_rep)
    assert small_rep.sheet_count == F.SMALL_SHEET_COUNT          # 多 Sheet 覆盖
    assert small.row_count == F.SMALL_ROW_COUNT
    assert small.row_excel_numbers == F.SMALL_ALL_ROWS           # 3..21
    assert not (set(small.column_names) & set(PII_COLUMNS)), 'Golden 数据不得含个人信息列'
    assert medium_rep.sheets[0].row_count == F.MEDIUM_ROW_COUNT
    assert medium_rep.sheets[0].row_excel_numbers[0] == F.MEDIUM_FIRST_ROW
    assert medium_rep.sheets[0].row_excel_numbers[-1] == F.MEDIUM_LAST_ROW
    assert not (set(medium_rep.sheets[0].column_names) & set(PII_COLUMNS))


def test_golden_independent_scan_matches_design(small_rep):
    sheet = F.sheet_of(small_rep)
    sf_rows = F.independent_filter_rows(sheet, F.CARRIER, 'contains', 'SF')
    assert sf_rows == F.SMALL_SF_ROWS                            # [3, 12, 18, 19] 离散
    assert F.row_runs(sf_rows) == F.SMALL_SF_RUNS
    assert F.row_runs(F.SMALL_ALL_ROWS) == F.SMALL_ALL_RUNS      # [[3, 21]] 连续
    assert F.independent_amount_stats(sheet, sf_rows) == (3, 1, 0, F.SMALL_SF_SUM)
    assert F.independent_amount_stats(sheet, F.SMALL_ALL_ROWS) == (
        F.SMALL_TOTAL_NUMERIC, F.SMALL_TOTAL_EMPTY, F.SMALL_TOTAL_NON_NUMERIC,
        F.SMALL_TOTAL_SUM)


# ==========================================================================
# 1) table 契约：实时结果 → snapshot（关键字段 + 独立期望）
# ==========================================================================
def test_table_contract_snapshot_fields(small_rep):
    out = _run(small_rep, '列出订单号和物流商', _table_turn(['Order ID', F.CARRIER]), 'c-table')
    assert out['status'] == nq.STATUS_OK
    res = out['result']
    assert res['total_matches'] == F.SMALL_TOTAL_MATCHED
    assert res['row_excel_numbers'] == F.SMALL_ALL_ROWS
    assert res['matched_row_runs'] == F.SMALL_ALL_RUNS           # 人工固定期望（非生产折叠函数）
    assert res['matched_row_run_count'] == 1
    assert res['matched_row_runs_truncated'] is False
    assert res['has_more'] is False

    snap = build_excel_history_snapshot(out)
    assert snap['schema_version'] == EXCEL_HISTORY_SCHEMA_VERSION == 1
    assert snap['kind'] == 'result'
    for key in ('rows', 'total_matches', 'returned_count', 'row_excel_numbers',
                'matched_row_runs', 'matched_row_run_count', 'matched_row_runs_truncated'):
        assert key in snap['payload'], '快照缺字段 %s' % key


def test_table_contract_discrete_filter_keeps_gaps(small_rep):
    turn = nq.TurnIntent(action=nq.ACTION_NEW_QUERY, intent=nq.intent_from_dict(
        {'query_type': nq.INTENT_STRUCTURED, 'sheet': F.SMALL_SHEET,
         'columns': ['Order ID', F.CARRIER], 'limit': 50,
         'filters': [{'column': F.CARRIER, 'operator': 'contains', 'value': 'SF'}]}))
    out = _run(small_rep, '列出订单号和物流商', turn, 'c-table-discrete')
    res = out['result']
    assert res['total_matches'] == F.SMALL_SF_MATCHED
    assert res['row_excel_numbers'] == F.SMALL_SF_ROWS
    assert res['matched_row_runs'] == F.SMALL_SF_RUNS           # [[3,3],[12,12],[18,19]]
    assert res['matched_row_runs'] != [[3, 19]]                 # 绝不退化成包络
    assert res['row_excel_numbers'] == F.SMALL_SF_ROWS


# ==========================================================================
# 2) aggregate 契约（含 relaxed_filters + 空值/非数值口径）
# ==========================================================================
def test_aggregate_contract_with_relaxation(small_rep):
    turn = _agg_turn(ag.OPERATION_SUM, F.AMOUNT,
                     [{'column': F.CARRIER, 'operator': 'eq', 'value': 'SF'}])
    out = _run(small_rep, '帮我看看', turn, 'c-agg')      # 中性问法：保留 eq 以覆盖放宽
    assert out['status'] == nq.STATUS_OK
    agg = out['aggregate']
    assert round(float(agg['value']), 2) == F.SMALL_SF_SUM      # 280.00（人工可推算）
    assert agg['matched_rows'] == F.SMALL_SF_MATCHED
    assert agg['numeric_rows'] == F.SMALL_SF_NUMERIC              # 3（含 1 行空值）
    assert agg['empty_rows'] == F.SMALL_SF_EMPTY
    assert agg['non_numeric_rows'] == F.SMALL_SF_NON_NUMERIC
    assert agg['matched_row_runs'] == F.SMALL_SF_RUNS
    assert agg['row_excel_spans'] == {'first': 3, 'last': 19}
    # 最终执行条件 = contains；放宽过程单独记录（两者不可混为一谈）
    assert agg['applied_filters'][0]['operator'] == 'contains'
    assert out['relaxed_filters'], 'fixture 设计为触发唯一前缀放宽'

    snap = build_excel_history_snapshot(out)
    assert snap['kind'] == 'aggregate'
    assert snap['relaxed_filters']
    for key in ('value', 'matched_rows', 'numeric_rows', 'empty_rows', 'non_numeric_rows',
                'row_excel_spans', 'matched_row_runs'):
        assert key in snap['payload']


def test_aggregate_contract_full_table_counts(small_rep):
    # 中性问法：避免"有多少"把口径改写成 COUNT（本用例要验的是 SUM 的空值/非数值口径）
    out = _run(small_rep, '帮我看看', _agg_turn(ag.OPERATION_SUM, F.AMOUNT), 'c-agg-all')
    agg = out['aggregate']
    assert agg['matched_rows'] == F.SMALL_TOTAL_MATCHED
    assert agg['numeric_rows'] == F.SMALL_TOTAL_NUMERIC
    assert agg['empty_rows'] == F.SMALL_TOTAL_EMPTY
    assert agg['non_numeric_rows'] == F.SMALL_TOTAL_NON_NUMERIC
    assert round(float(agg['value']), 2) == F.SMALL_TOTAL_SUM
    assert agg['matched_row_runs'] == F.SMALL_ALL_RUNS


# ==========================================================================
# 3) group 契约（分组值/行数 + row provenance）
# ==========================================================================
def test_group_contract_groups_and_runs(small_rep):
    out = _run(small_rep, '各物流商的订单金额汇总', _group_turn(), 'c-group')
    g = out['group_aggregate']
    assert g['total_groups'] == len(F.SMALL_PROVIDER_GROUPS)
    counts = {r['group_display'][0]: r['matched_rows'] for r in g['rows']}
    assert counts == F.SMALL_PROVIDER_GROUPS                # 4 / 7 / 8（人工可推算）
    values = {r['group_display'][0]: round(float(r['value']), 2) for r in g['rows']}
    assert values == {'SF International': F.SMALL_SF_SUM, 'JS Express International': 350.00,
                      'Yanwen Express': 920.00}
    assert g['matched_rows'] == F.SMALL_TOTAL_MATCHED
    assert g['matched_row_runs'] == F.SMALL_ALL_RUNS
    assert g['row_excel_spans'] == {'first': 3, 'last': 21}

    snap = build_excel_history_snapshot(out)
    assert snap['kind'] == 'group_aggregate'
    for key in ('rows', 'total_groups', 'matched_rows', 'matched_row_runs', 'row_excel_spans'):
        assert key in snap['payload']
    assert 'relaxed_filters' in snap                       # 放宽说明属快照顶层（可为空）


# ==========================================================================
# 4) multi-step 契约（step1/step2 + 手工可推算的最终值）
# ==========================================================================
def test_multi_step_contract(small_rep):
    out = _run(small_rep, '订单金额最高的前3个SKU的销售额总和', _multi_turn(), 'c-multi')
    m = out['multi_step']
    assert round(float(m['value']), 2) == F.SMALL_MULTI_STEP_VALUE      # 1000+270+130
    assert m['step1']['matched_rows'] == F.SMALL_TOTAL_MATCHED
    assert m['step1']['matched_row_runs'] == F.SMALL_ALL_RUNS
    assert m['step1']['row_excel_spans'] == {'first': 3, 'last': 21}
    assert m['step2']['input_rows'] == F.SMALL_MULTI_STEP_INPUT_ROWS
    assert m['step2']['numeric_rows'] == F.SMALL_MULTI_STEP_NUMERIC_ROWS
    assert m['step2']['source'] == ms.SOURCE_STEP_1
    assert m['definition']

    snap = build_excel_history_snapshot(out)
    assert snap['kind'] == 'multi_step'
    for key in ('definition', 'step1', 'step2', 'value', 'value_display'):
        assert key in snap['payload']
    for key in ('input_rows', 'numeric_rows', 'source', 'source_text'):
        assert key in snap['payload']['step2']


# ==========================================================================
# 5) Golden B：全量枚举（不去重）/ 大表统计
# ==========================================================================
def test_medium_enumeration_keeps_all_rows(medium_rep):
    turn = nq.TurnIntent(action=nq.ACTION_NEW_QUERY, intent=nq.intent_from_dict(
        {'query_type': nq.INTENT_STRUCTURED, 'sheet': F.MEDIUM_SHEET,
         'columns': [F.SKU]}))
    notes = nq.apply_enumeration_limit(turn, '列出全部SKU')
    assert notes and turn.intent.limit == nq.MAX_NL_LIMIT
    out = _run(medium_rep, '列出全部SKU', turn, 'c-medium-enum')
    res = out['result']
    assert res['total_matches'] == F.MEDIUM_ROW_COUNT
    assert len(res['rows']) == F.MEDIUM_ROW_COUNT                 # 不截断
    sheet = medium_rep.sheets[0]
    ci = F.column_index(sheet, F.SKU)
    expected = [row[ci] for row in sheet.rows]
    got = [row[0] for row in res['rows']]
    assert got == expected                                        # 不去重、顺序一致
    assert len(set(got)) == F.MEDIUM_SKU_DISTINCT                 # 确有重复
    assert res['matched_row_runs'] == [[F.MEDIUM_FIRST_ROW, F.MEDIUM_LAST_ROW]]


def test_medium_aggregate_matches_independent_scan(medium_rep):
    out = _run(medium_rep, '帮我看看',                 # 中性问法：保持 SUM 口径
               nq.TurnIntent(action=nq.ACTION_AGGREGATE, aggregate=nq.AggregateIntent(
                   operation=ag.OPERATION_SUM, column=F.AMOUNT, sheet=F.MEDIUM_SHEET,
                   filters=[{'column': F.CARRIER, 'operator': 'contains', 'value': 'SF'}])),
               'c-medium-agg')
    sheet = F.sheet_of(medium_rep, F.MEDIUM_SHEET)
    rows = F.independent_filter_rows(sheet, F.CARRIER, 'contains', 'SF')
    numeric, empty, non_numeric, total = F.independent_amount_stats(sheet, rows)
    agg = out['aggregate']
    assert agg['matched_rows'] == len(rows) == F.MEDIUM_SF_COUNT
    assert agg['numeric_rows'] == numeric
    assert agg['empty_rows'] == empty
    assert agg['non_numeric_rows'] == non_numeric
    assert round(float(agg['value']), 2) == total


# ==========================================================================
# 6) snapshot → SQLite → history 契约
# ==========================================================================
@pytest.fixture()
def temp_conv(monkeypatch):
    """临时 SQLite + 会话（绝不触碰用户 DB）。"""
    tmpdir = tempfile.mkdtemp(prefix='golden_hist_')
    engine = create_engine('sqlite:///%s' % os.path.join(tmpdir, 'g.db'))
    Base.metadata.create_all(engine)
    monkeypatch.setattr(conv_mod, 'SessionLocal',
                        sessionmaker(bind=engine, autocommit=False, autoflush=False))
    return conv.get_or_create_conversation_id(0, 1)


@pytest.mark.parametrize('turn_builder,msg,kind', [
    (lambda: _table_turn(['Order ID', F.CARRIER]), '列出订单号和物流商', 'result'),
    (lambda: _agg_turn(ag.OPERATION_SUM, F.AMOUNT), '有多少订单金额', 'aggregate'),
    (_group_turn, '各物流商的订单金额汇总', 'group_aggregate'),
    (_multi_turn, '订单金额最高的前3个SKU的销售额总和', 'multi_step'),
])
def test_snapshot_persist_history_roundtrip(small_rep, temp_conv, turn_builder, msg, kind):
    out = _run(small_rep, msg, turn_builder(), 'c-hist-%s' % kind)
    snap = build_excel_history_snapshot(out)
    assert snap['kind'] == kind
    persist_excel_turn(temp_conv, 'assistant', '摘要文本', 'm', snap)

    history = conv.get_history(temp_conv)
    assert len(history) == 1
    meta = history[0]['meta_info']
    assert meta['source'] == 'excel'                     # Excel 轮次带来源标记
    restored = meta['excel']
    assert restored['schema_version'] == 1
    assert restored['kind'] == kind
    assert restored['payload'] == snap['payload']        # 落库/读回不改内容


def test_history_excel_not_filtered_and_keeps_latest(small_rep, temp_conv):
    """>100 条历史：Excel 轮次仍可见（不被普通 chat 过滤），且取到的是最新的一条。"""
    for i in range(105):
        # 普通聊天消息：直接 append（不带 source 标记），用于制造 >100 条历史
        conv.append_message(temp_conv, 'assistant', '普通第 %d 条' % i, 'm')
    out = _run(small_rep, '列出订单号和物流商',
               _table_turn(['Order ID', F.CARRIER]), 'c-hist-latest')
    snap = build_excel_history_snapshot(out)
    persist_excel_turn(temp_conv, 'assistant', '最新的 Excel 结果', 'm', snap)

    all_msgs = conv.get_history(temp_conv)
    assert len(all_msgs) == 106
    excel_msgs = [m for m in all_msgs if (m.get('meta_info') or {}).get('source') == 'excel']
    assert len(excel_msgs) == 1
    assert excel_msgs[0]['content'] == '最新的 Excel 结果'      # 最新的一条即可见
    # 普通聊天若显式排除 excel，则 Excel 轮次整体不进上下文（能力隔离）
    filtered = conv.get_history(temp_conv, exclude_sources=['excel'])
    assert all((m.get('meta_info') or {}).get('source') != 'excel' for m in filtered)


def test_build_excel_turn_meta_wraps_once(small_rep):
    out = _run(small_rep, '列出订单号和物流商', _table_turn(['Order ID', F.CARRIER]),
               'c-meta')
    snap = build_excel_history_snapshot(out)
    meta = build_excel_turn_meta(snap)
    assert meta['source'] == 'excel' and 'excel' in meta
    assert 'excel' not in meta['excel']                  # 绝不双层包装


def test_fresh_clone_equivalent_user_store_is_empty(small_rep, medium_rep):
    """fresh clone 等价性证明：本文件的存储根被指向**空目录**（≡ 全新 clone）。

    在此前提下：
      1. 用户侧读取必然为 None（说明这些测试**没有**从用户 data/ 拿到任何数据）；
      2. Golden 流程依旧跑通（数据 100% 来自随仓库入库的 fixture）。
    """
    assert excel_store.load_representation('whatever-doc-id') is None   # 空目录 == fresh clone
    assert excel_store.list_excel_catalog(user_id=0) == []
    assert F.golden_path(F.GOLDEN_SMALL_NAME).exists()                  # fixture 随仓库而来
    assert F.golden_path(F.GOLDEN_MEDIUM_NAME).exists()

    out = _run(small_rep, '列出订单号和物流商',
               _table_turn(['Order ID', F.CARRIER]), 'c-fresh-clone')
    assert out['status'] == nq.STATUS_OK
    assert out['result']['total_matches'] == F.SMALL_TOTAL_MATCHED
    assert build_excel_history_snapshot(out)['schema_version'] == 1

    out2 = _run(medium_rep, '列出全部SKU',
                nq.TurnIntent(action=nq.ACTION_NEW_QUERY, intent=nq.intent_from_dict(
                    {'query_type': nq.INTENT_STRUCTURED, 'sheet': F.MEDIUM_SHEET,
                     'columns': [F.SKU], 'limit': 500})), 'c-fresh-clone-2')
    assert out2['result']['total_matches'] == F.MEDIUM_ROW_COUNT


# ==========================================================================
# 7) 与共享快照 fixture 的结构契约（前端 restore 读同一份）
# ==========================================================================
@pytest.mark.parametrize('kind,turn_builder,msg', [
    ('table', lambda: _table_turn(['Order ID', F.CARRIER]), '列出订单号和物流商'),
    # 与 fixture 生成器保持同样的输入（eq SF + 中性问法），保证是"同一场景"的结构对照
    ('aggregate', lambda: _agg_turn(ag.OPERATION_SUM, F.AMOUNT,
                                    [{'column': F.CARRIER, 'operator': 'eq', 'value': 'SF'}]),
     '帮我看看'),
    ('group_aggregate', _group_turn, '各物流商的订单金额汇总'),
    ('multi_step', _multi_turn, '订单金额最高的前3个SKU的销售额总和'),
])
def test_shared_snapshot_fixture_structure_in_sync(small_rep, kind, turn_builder, msg):
    """实时快照与 `tests/fixtures/excel_history/*.json` 的**字段集合**必须一致。

    只比较结构（键集合），不比较值 —— 值由上面的独立期望断言，
    因此这里的 fixture 永远不会"替被测实现说话"。
    """
    live = build_excel_history_snapshot(_run(small_rep, msg, turn_builder(), 'c-sync-%s' % kind))
    fixture = F.load_history_snapshot(kind if kind != 'table' else 'table')
    assert live['kind'] == fixture['kind']
    assert live['schema_version'] == fixture['schema_version'] == 1
    assert set(live.keys()) == set(fixture.keys())
    assert set(live['payload'].keys()) == set(fixture['payload'].keys())


def test_legacy_fixture_shape_is_old_snapshot():
    """旧快照 fixture 必须**缺** Stage 3 新增字段（前端降级路径的输入）。"""
    legacy = F.load_history_snapshot('legacy_table')
    assert legacy['schema_version'] == 1
    assert 'matched_row_runs' not in legacy['payload']
    assert 'matched_row_run_count' not in legacy['payload']
    legacy_multi = F.load_history_snapshot('legacy_multi_step')
    assert 'matched_row_runs' not in (legacy_multi['payload']['step1'])
    assert (legacy_multi['payload']['step1']).get('row_excel_spans')   # 包络仍在
    assert json.loads(json.dumps(legacy, ensure_ascii=False))['kind'] == 'result'
