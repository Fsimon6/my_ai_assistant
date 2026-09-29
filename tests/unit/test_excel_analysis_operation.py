# -*- coding: utf-8 -*-
"""Stage 2A-P1（F1）：两步分析的**第 2 步汇总口径**必须由用户文本决定。

背景（2026-09-27 审计实证，离线可确定性复现）：
    `_repair_analysis_plan_from_text` 只纠正 **step1** 的口径，从不碰 step2；
    `multi_step` 校验只限制 step2 的 source/column/group/order/filters（`multi_step.py:245-265`），
    **不校验 operation** —— 于是用户明确说「销售额总和是多少」而 LLM 给 `avg` 时，
    最终执行 **AVG**（实测 35.53，正确 SUM 应为 106.6）；反向「平均值」+ LLM `sum` 亦同样被执行。

正确语义边界：
    A. 用户文本**明确**表达汇总口径（总和 / 合计 / 总计 / 求和 / 汇总 / 平均 / 最大值 / 最小 …）
       -> **覆盖** LLM 的 step2.operation（确定性层优先）；
    B. 用户没有明确表达 -> **不动**（保持既有行为，不强行设 SUM）。

本文件为纯离线测试（注入计划 / monkeypatch LLM），不调用真实 LLM。

Ground Truth（合成表，step1 = SUM(Order Amount) by SKU ID, TOP-3 降序）：
    S1 = 10+20 = 30；S2 = 25；S3 = 15；S4 = 5   ->  TOP3 = [30, 25, 15]
    SUM(70) / AVG(23.33) / MAX(30) / MIN(15) 四者互不相同，可区分。
"""
import asyncio
import re
from typing import Any, Dict, List, Optional

import pytest

from backend.excel import aggregate as excel_aggregate
from backend.excel import multi_step as excel_multi_step
from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from backend.excel.representation import (
    ColumnMeta, SheetRepresentation, WorkbookRepresentation,
)

_COLS = ['SKU ID', 'Order Amount']
_ROWS = [['S1', '10'], ['S1', '20'], ['S2', '25'], ['S3', '15'], ['S4', '5']]
_GT = {excel_aggregate.OPERATION_SUM: 70.0,
       excel_aggregate.OPERATION_MAX: 30.0,
       excel_aggregate.OPERATION_MIN: 15.0}
_GT_AVG = 70.0 / 3.0


def _synth_rep() -> WorkbookRepresentation:
    cols = [ColumnMeta(name=n, index=i, excel_column=i + 1,
                       excel_column_letter=chr(ord('A') + i), dtype='string')
            for i, n in enumerate(_COLS)]
    sheet = SheetRepresentation(
        sheet_name='OrderSKUList', sheet_index=0, header_mode='single', columns=cols,
        rows=[list(r) for r in _ROWS], row_excel_numbers=list(range(2, 2 + len(_ROWS))),
        row_count=len(_ROWS), column_count=len(_COLS))
    return WorkbookRepresentation(schema_version='1.0', document_id='an-op',
                                  user_id=1, filename='analysis_op.xlsx',
                                  file_type='xlsx', parser='xlsx', sheet_count=1,
                                  sheets=[sheet])


def _catalog(rep: WorkbookRepresentation) -> List[Dict[str, Any]]:
    return [{
        'document_id': rep.document_id, 'filename': rep.filename,
        'file_type': rep.file_type, 'created_at': '2026-09-27T00:00:00',
        'total_rows': rep.total_rows,
        'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                    'row_count': s.row_count, 'column_count': s.column_count,
                    'columns': s.column_names} for s in rep.sheets],
    }]


class _FakeLLM:
    """占位 LLM：管线的 LLM 调用点全部被 monkeypatch。"""


def _plan(step2_operation: str, top_n: int = 3,
          order_dir: str = 'desc') -> 'nl.AnalysisIntent':
    return nl.AnalysisIntent(steps=[
        {'type': excel_multi_step.STEP_GROUP_AGGREGATE, 'group_by': ['SKU ID'],
         'operation': excel_aggregate.OPERATION_SUM, 'column': 'Order Amount',
         'order_by': excel_aggregate.ORDER_BY_AGGREGATE, 'order_dir': order_dir,
         'top_n': top_n, 'filters': []},
        {'type': excel_multi_step.STEP_AGGREGATE, 'operation': step2_operation,
         'source': excel_multi_step.SOURCE_STEP_1,
         'column': excel_multi_step.INTERMEDIATE_VALUE_COLUMN},
    ])


def _run(rep: WorkbookRepresentation, message: str, plan, monkeypatch) -> Dict[str, Any]:
    monkeypatch.setattr(excel_store, 'load_representation',
                        lambda document_id, user_id=None: rep)
    orig_turn, orig_analysis = nl.llm_parse_turn, nl.llm_parse_analysis

    async def _fake_turn(llm, msg, catalog, context=None, analysis_context=None):
        return nl.TurnIntent(action=nl.ACTION_ANALYSIS, analysis=plan)

    async def _fake_analysis(llm, msg, catalog, context=None, analysis_context=None):
        return None                                   # 不触发第二次 LLM 兜底

    monkeypatch.setattr(nl, 'llm_parse_turn', _fake_turn)
    monkeypatch.setattr(nl, 'llm_parse_analysis', _fake_analysis)
    try:
        return asyncio.run(nl.run_nl_query(
            message, _catalog(rep), llm=_FakeLLM(), user_id=1, session_key='an-op'))
    finally:
        monkeypatch.setattr(nl, 'llm_parse_turn', orig_turn)
        monkeypatch.setattr(nl, 'llm_parse_analysis', orig_analysis)


def _step2_operation(out: Dict[str, Any]) -> Optional[str]:
    m = re.search(r'第 2 步：操作 ([A-Z]+)', str(out.get('message') or ''))
    return m.group(1) if m else None


def _final_number(out: Dict[str, Any]) -> Optional[float]:
    m = re.search(r'最终结果：([\d.]+)', str(out.get('message') or ''))
    return float(m.group(1)) if m else None


@pytest.fixture()
def rep() -> WorkbookRepresentation:
    return _synth_rep()


# ===========================================================================
# F1-1 ~ F1-4：用户明确口径，LLM 给相反的/错误的 operation -> 必须被覆盖
# ===========================================================================
@pytest.mark.parametrize('text,llm_op,want_op,want_val', [
    # 用户「总和」，LLM 给 avg  -> 必须 SUM
    ('找出金额最高的前3个SKU，并统计它们的总销售额是多少',
     excel_aggregate.OPERATION_AVG, excel_aggregate.OPERATION_SUM,
     _GT[excel_aggregate.OPERATION_SUM]),
    # 用户「平均值」，LLM 给 sum -> 必须 AVG
    ('找出金额最高的前3个SKU，并统计它们的平均销售额是多少',
     excel_aggregate.OPERATION_SUM, excel_aggregate.OPERATION_AVG, _GT_AVG),
])
def test_f1_user_operation_overrides_llm(rep, monkeypatch, text, llm_op, want_op, want_val):
    """F1-1/F1-2：用户明确汇总口径 -> 覆盖 LLM 的 step2.operation，且**真实结果 == GT**。"""
    out = _run(rep, text, _plan(llm_op), monkeypatch)
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _step2_operation(out) == want_op.upper()
    assert _final_number(out) == pytest.approx(want_val, abs=0.01)


@pytest.mark.parametrize('text,llm_op,want_op', [
    # F1-3：用户「最大值」，LLM 给 min
    ('找出金额最高的前3个SKU，并统计它们的最大销售额是多少',
     excel_aggregate.OPERATION_MIN, excel_aggregate.OPERATION_MAX),
    # F1-4：用户「最小值」，LLM 给 max
    ('找出金额最高的前3个SKU，并统计它们的最小销售额是多少',
     excel_aggregate.OPERATION_MAX, excel_aggregate.OPERATION_MIN),
])
def test_f1_max_min_operation_corrected_at_repair_level(text, llm_op, want_op):
    """F1-3/F1-4：max/min 口径同样由文本决定（在**修复函数级**断言）。

    注：这两种措辞会被 2.4 的"过度规划"判定（`looks_like_multi_step_query` 只认
    总/合计/平均…，不含 最大/最小）**降级为单步分组统计**，因此不会走到 step2
    —— 属既有设计行为（本轮只记录，不扩大修改）。但一旦 plan 真的要执行第 2 步，
    口径必须由文本决定。
    """
    steps = _plan(llm_op).steps
    notes = nl._repair_analysis_step2_operation(steps, text)
    assert steps[1]['operation'] == want_op
    assert notes                      # 发生了覆盖（有 note 可追溯）


def test_f1_no_operation_word_keeps_llm_value_at_repair_level():
    """边界 B：用户没有表达汇总口径 -> 确定性层**不介入**（不强行设 SUM）。"""
    steps = _plan(excel_aggregate.OPERATION_AVG).steps
    notes = nl._repair_analysis_step2_operation(steps, '找出金额最高的前3个SKU，并看看它们的数据')
    assert steps[1]['operation'] == excel_aggregate.OPERATION_AVG
    assert not notes


# ===========================================================================
# F1-5 / F1-6：LLM 本来就给对 -> 保持（不重复覆盖、不劣化）
# ===========================================================================
@pytest.mark.parametrize('text,op,want_val', [
    ('找出金额最高的前3个SKU，并统计它们的总销售额是多少',
     excel_aggregate.OPERATION_SUM, _GT[excel_aggregate.OPERATION_SUM]),
    ('找出金额最高的前3个SKU，并统计它们的平均销售额是多少',
     excel_aggregate.OPERATION_AVG, _GT_AVG),
])
def test_f1_llm_correct_operation_kept(rep, monkeypatch, text, op, want_val):
    """F1-5/F1-6：LLM 口径与用户一致 -> 继续执行同一口径，结果 == GT。"""
    out = _run(rep, text, _plan(op), monkeypatch)
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _step2_operation(out) == op.upper()
    assert _final_number(out) == pytest.approx(want_val, abs=0.01)


# ===========================================================================
# G-3（最终验收审计）：analysis **step1 的 TOP-N 数量**必须来自用户文本
#   GT（合成表）：top3 = 30+25+15 = 70；top4/top5 = 75
# ===========================================================================
def _step1_message_top_n(out: Dict[str, Any]) -> Optional[int]:
    """从**执行文案**里读第 1 步实际截取的 N（当 N >= 分组数时文案省略该子句 -> None）。

    注：回传的 ``turn`` 是**LLM 原始计划**；执行用的是经过 user-authority 校正后的
    ``raw_steps`` 副本，因此以文案 + 数值为准（数值本身可区分 top3 与 top4+）。
    """
    m = re.search(r'取前 (\d+) 名', str(out.get('message') or ''))
    return int(m.group(1)) if m else None


@pytest.mark.parametrize('text,want_top,want_val', [
    ('找出金额最高的前3个SKU，并统计它们的总销售额是多少', 3, 70.0),   # plan 给 10
    ('找出金额最高的前5个SKU，并统计它们的总销售额是多少', 5, 75.0),   # plan 给 3
])
def test_g3_analysis_step1_top_n_from_text(rep, monkeypatch, text, want_top, want_val):
    """G-3：用户文本明确的 N 覆盖 plan.step1.top_n，且**真实结果 == GT**。

    GT：top3 = 30+25+15 = 70；top5 = 全部 4 组 = 75（两者可区分，故数值即证明执行口径）。
    """
    llm_top = 10 if want_top == 3 else 3
    # ① 修复函数级：注入错误 N -> 校正为用户文本的 N
    assert nl._enforce_user_sort_params(None, llm_top, text, enabled=True)[1] == want_top
    # ② 端到端：执行文案（若含）+ 数值双证据
    out = _run(rep, text, _plan(excel_aggregate.OPERATION_SUM, top_n=llm_top), monkeypatch)
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _step1_message_top_n(out) in (None, want_top)
    assert _final_number(out) == pytest.approx(want_val, abs=0.01)


def test_g3_analysis_step1_order_dir_from_text(rep, monkeypatch):
    """G-3 补充：第 1 步的排序方向同样以用户文本为准（「最低」-> asc）。"""
    msg = '找出金额最低的3个SKU，并统计它们的总销售额是多少'
    _dir, _top, notes = nl._enforce_user_sort_params('desc', 10, msg, enabled=True)
    assert (_dir, _top) == ('asc', 3)
    assert notes and notes[0].startswith('排序方向=asc')
    # 用户未表达方向 -> 原样返回（不改既有默认）
    assert nl._enforce_user_sort_params(
        'desc', 3, '找出金额最高的3个SKU，并统计它们的总销售额是多少', enabled=True)[:2] == ('desc', 3)
    # 语境不成立（无排序/分组）-> 不介入
    assert nl._enforce_user_sort_params('desc', 3, msg, enabled=False)[:2] == ('desc', 3)
    # 端到端：plan 给 desc/10，用户说「最低的3个」-> 执行 asc + TOP-3（GT 5+15+25=45）
    out = _run(rep, msg, _plan(excel_aggregate.OPERATION_SUM, top_n=10, order_dir='desc'),
               monkeypatch)
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _step1_message_top_n(out) == 3
    assert _final_number(out) == pytest.approx(45.0, abs=0.01)


# ===========================================================================
# G-4（analysis 侧）：第 1 步的**排序键**同样必须来自用户文本
#   实测：文本「金额最高的前3个SKU」而 plan 给 order_by='SKU ID' -> 曾按 SKU 名排序（64.48）
# ===========================================================================
def test_g4_analysis_step1_sort_key_from_text(rep, monkeypatch):
    """G-4：plan 给 order_by='SKU ID'，但文本说的是「金额最高」-> 必须按聚合值排序。"""
    msg = '找出金额最高的前3个SKU，并统计它们的总销售额是多少'
    plan = nq_plan_with_sort('SKU ID')
    out = _run(rep, msg, plan, monkeypatch)
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert 'SKU ID」降序' not in str(out.get('message'))
    assert _final_number(out) == pytest.approx(70.0, abs=0.01)   # GT: 按金额取 top3 = 30+25+15


def nq_plan_with_sort(order_by: str) -> 'nl.AnalysisIntent':
    """构建 plan 并注入错误的 step1.order_by（模拟 LLM 误判排序键）。"""
    plan = _plan(excel_aggregate.OPERATION_SUM)
    plan.steps[0]['order_by'] = order_by
    return plan


# ===========================================================================
# 说明：max/min（F1-3/F1-4）与"无口径词"（边界 B）在**修复函数级**断言 ——
# 见上方 `test_f1_max_min_operation_corrected_at_repair_level` 与
# `test_f1_no_operation_word_keeps_llm_value_at_repair_level`。
# ===========================================================================
