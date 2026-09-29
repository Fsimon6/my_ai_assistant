# -*- coding: utf-8 -*-
"""批次 A（R4 文案统一 + R5 叙述术语清理）：**用户可见叙述文本**的文案契约。

背景（只读审查确认）：后端 AI 叙述里仍残留内部/技术化措辞：
  * `Step 1 的 TOP-N 结果（本阶段不回到原始行）`（R4 遗留，与前端卡片不一致）
  * `原始 Excel 数据（representation，先筛选后分组）`（内部实现词）
  * `→ TOP-3 截取前 3 个`（内部措辞）
  * `本次返回：5 行（offset=0, limit=5）`（分页实现参数）
  * `Excel 行号：3 ~ 7`（未标明"本页"）

本轮只改**文案**，不改执行逻辑、不改 snapshot schema；语义必须保留：
"第 1 步的结果 / 不会重新回到原始 Excel 数据行"。
"""
import asyncio
import glob
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.api.v1.rag import EXCEL_HISTORY_SCHEMA_VERSION, build_excel_history_snapshot
from backend.excel import aggregate as excel_aggregate
from backend.excel import multi_step as ms
from backend.excel import nl_query as nl
from backend.excel import store as excel_store

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ==========================================================================
# 夹具
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
    return _load_rep(10, 40)


@pytest.fixture(scope='module')
def big_rep() -> 'nl.WorkbookRepresentation':
    return _load_rep(400, 600)


def _catalog(r) -> List[Dict[str, Any]]:
    return [{
        'document_id': r.document_id, 'filename': r.filename, 'file_type': r.file_type,
        'created_at': '2026-09-29T00:00:00', 'total_rows': r.total_rows,
        'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                    'row_count': s.row_count, 'column_count': s.column_count,
                    'columns': s.column_names} for s in r.sheets],
    }]


def _run(r, turn, message: str, *, session_key='wording') -> Dict[str, Any]:
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: r
    try:
        return asyncio.run(nl.run_nl_query(message, _catalog(r), llm=None,
                                           pagination_override=turn, user_id=1,
                                           session_key=session_key))
    finally:
        excel_store.load_representation = original


def _table_turn(r, message: str, *, limit=None):
    payload: Dict[str, Any] = {'query_type': nl.INTENT_STRUCTURED, 'document': r.filename,
                               'columns': ['Order ID'], 'filters': []}
    if limit is not None:
        payload['limit'] = limit
    turn = nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=nl.intent_from_dict(payload))
    nl.apply_enumeration_limit(turn, message)
    return turn


def _analysis_turn(r, *, top_n=3):
    return nl.TurnIntent(action=nl.ACTION_ANALYSIS, analysis=nl.AnalysisIntent(steps=[
        {'type': ms.STEP_GROUP_AGGREGATE, 'group_by': ['SKU ID'],
         'operation': excel_aggregate.OPERATION_SUM, 'column': 'Order Amount',
         'order_by': excel_aggregate.ORDER_BY_AGGREGATE, 'order_dir': 'desc', 'top_n': top_n},
        {'type': ms.STEP_AGGREGATE, 'operation': excel_aggregate.OPERATION_SUM,
         'source': ms.SOURCE_STEP_1, 'column': ms.INTERMEDIATE_VALUE_COLUMN}]))


# ==========================================================================
# R5：表格叙述（不暴露 offset/limit；行号写明"本页"）
# ==========================================================================
def test_table_narrative_hides_pagination_params(rep):
    msg = '列出订单号和物流商'
    out = _run(rep, _table_turn(rep, msg, limit=5), msg, session_key='w-table')
    text = out['message']
    assert '- 本次返回：5 行（第 1~5 条）' in text
    assert 'offset=' not in text and 'limit=' not in text
    assert '- 本页 Excel 行号：' in text
    assert '- 命中总数：' in text                       # 总数仍然如实显示


def test_table_narrative_cap_notice_when_more(big_rep):
    """还有更多命中行时仍然明确提示上限（P1 行为保持，文案同为自然中文）。"""
    msg = '列出订单号和物流商'
    out = _run(big_rep, _table_turn(big_rep, msg, limit=50), msg, session_key='w-table-cap')
    text = out['message']
    assert '本次返回：50 行（第 1~50 条）' in text
    assert '超过单次显示上限' in text and '「下一页」' in text
    assert 'offset=' not in text and 'limit=' not in text


# ==========================================================================
# R4 + R5：两步分析叙述
# ==========================================================================
def test_analysis_narrative_uses_natural_wording(rep):
    msg = '订单金额最高的前3个SKU的销售额总和'
    out = _run(rep, _analysis_turn(rep, top_n=3), msg, session_key='w-analysis')
    text = out['message']
    # R4：第 1 步 / 不回到原始行（与前端卡片同一语义）
    assert '- 第 2 步来源：第 1 步的结果（3 个分组的聚合值），不会重新回到原始 Excel 数据行' in text
    assert '第 1 步的结果' in text
    assert '不会重新回到原始 Excel 数据行' in text
    # R5：不出现内部措辞
    assert 'Step 1' not in text
    assert 'Step 2' not in text
    assert 'representation' not in text
    assert 'TOP-' not in text
    assert '不回原始行' not in text
    # R5：第 1 步取前 N 名改为自然中文（信息不丢：总数 + 本次返回数）
    assert '取前 3 名（本次返回 3 个分组）' in text
    assert '分组数 15 个' in text
    # 口径行同样自然化（原本含 "Step 1 结果，不回原始行"）
    assert '第 2 步：对上述 3 个分组的**聚合值**再执行' in text
    assert '数据来源：第 1 步的结果' in text


def test_step2_source_text_helper_is_single_source():
    """来源说明单一来源：常量 = 不带数量的静态句；带数量时动态生成。"""
    assert ms.SOURCE_STEP2_TEXT == ms.step2_source_text()
    assert ms.SOURCE_STEP2_TEXT == '第 1 步的结果（第 1 步返回的分组聚合值），不会重新回到原始 Excel 数据行'
    assert ms.step2_source_text(7) == '第 1 步的结果（7 个分组的聚合值），不会重新回到原始 Excel 数据行'
    assert 'Step 1' not in ms.step2_source_text(7) and 'TOP' not in ms.step2_source_text(7)


def test_step1_source_text_has_no_internal_word():
    assert ms.SOURCE_STEP1_TEXT == '原始 Excel 数据（先筛选后分组）'
    assert 'representation' not in ms.SOURCE_STEP1_TEXT


# ==========================================================================
# snapshot 兼容（结构不变 / 旧值不影响展示）
# ==========================================================================
def test_snapshot_structure_unchanged_and_old_source_text_tolerated(rep):
    msg = '订单金额最高的前3个SKU的销售额总和'
    out = _run(rep, _analysis_turn(rep, top_n=3), msg, session_key='w-snap')
    snap = build_excel_history_snapshot(out)
    assert snap['schema_version'] == EXCEL_HISTORY_SCHEMA_VERSION == 1
    # schema 未变：snapshot 的 step2 键 = 白名单命中键 (+ builder 追加的 applied_filters/group_by)
    from backend.api.v1.rag import _STEP_KEYS
    live = out['multi_step']['step2']
    kept = {k for k in _STEP_KEYS if k in live}
    assert 'source_text' in kept
    assert kept.issubset(set(snap['payload']['step2']))
    # snapshot 结构未变：额外键**恰好**是 builder 固定追加的那 4 个（多/少任何一个都算结构变化）
    assert set(snap['payload']['step2']) - kept == {'applied_filters', 'filters', 'group_by', 'rows'}
    assert '第 1 步的结果' in snap['payload']['step2']['source_text']

    # 旧快照（source_text 为历史旧文案）仍能原样构建/序列化：不回填、不改结构、不报错
    legacy = {**out, 'multi_step': {**out['multi_step'], 'step2': {
        **out['multi_step']['step2'], 'source_text': 'Step 1 的 TOP-N 结果（本阶段不回到原始行）'}}}
    snap_legacy = build_excel_history_snapshot(legacy)
    assert snap_legacy['schema_version'] == 1
    assert snap_legacy['payload']['step2']['source_text'].startswith('Step 1')
    assert 'rows' in snap_legacy['payload']['step1']


def test_snapshot_step2_source_carries_dynamic_group_count(rep):
    """快照里的来源说明带出本步实际输入的分组数（与叙述/前端一致）。"""
    msg = '订单金额最高的前2个SKU的销售额总和'
    out = _run(rep, _analysis_turn(rep, top_n=2), msg, session_key='w-snap2')
    step2 = out['multi_step']['step2']
    assert step2['input_rows'] == 2
    assert '（2 个分组的聚合值）' in step2['source_text']
