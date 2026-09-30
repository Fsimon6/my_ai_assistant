# -*- coding: utf-8 -*-
"""F3（2026-09-30）：**两步分析第 1 步口径的 authority 不得被 analysis_guard 绕过**。

实测缺口（离线可确定性复现）：
    用户：「订单金额最高的前3个SKU的销售额总和是多少」(Golden A，正确值 1400.00)

    1. 第 1 次 LLM 给出 step1.operation=count（错误候选）；
    2. `_run_nl_query_impl` 1.5 的 `_repair_analysis_plan_from_text` 能按用户原文把它修成
       `operation=sum` / `column=Order Amount`（本文件 T1 直接证明该能力存在）；
    3. 但 2.3 的**两步分析守卫**会再问一次 LLM（`llm_parse_analysis`），并用**第 2 份计划**
       构造 `TurnIntent(..., source='analysis_guard')`（`raw` 为空）后直接执行并 return；
    4. 第 2 份计划**没有**经过第 2 步的权威层 → step1 仍是 count → 最终 **15**（应为 1400）。

正确语义边界（本轮修复目标）：
    只要计划是由**自然语言解析派生**的（第一轮解析 / analysis_guard 第二轮 / 确定性升级），
    第 1 步口径就必须与用户原文对齐，与第 1 次解析同等对待；调用方**显式给定**的计划
    （override）仍原样执行。
"""
import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from backend.excel import aggregate as ag
from backend.excel import multi_step as ms
from backend.excel import nl_normalize as nl_norm
from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from tests.helpers import fixtures as F

AMOUNT, SKU = F.AMOUNT, F.SKU
MSG = '订单金额最高的前3个SKU的销售额总和是多少'
WRONG_VALUE = 15.0                      # 把 count 当口径时，top-3 分组计数之和（实测）


@pytest.fixture(autouse=True)
def _no_user_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path / 'no_user_data')


@pytest.fixture(scope='module')
def rep():
    return F.parse_golden(F.GOLDEN_SMALL_NAME, document_id='f3-small')


@pytest.fixture(scope='module')
def catalog(rep):
    return F.catalog_of(rep)


@pytest.fixture()
def patched_store(rep, monkeypatch):
    monkeypatch.setattr(excel_store, 'load_representation',
                        lambda document_id, user_id=None: rep)


# ==========================================================================
# 错误候选：step1.operation=count（不含度量列）-> 分组计数排行
# ==========================================================================
def _wrong_step1_plan() -> Dict[str, Any]:
    return {'action': 'analysis', 'document': None, 'sheet': F.SMALL_SHEET, 'steps': [
        {'type': ms.STEP_GROUP_AGGREGATE, 'group_by': [SKU], 'operation': ag.OPERATION_COUNT,
         'column': None, 'filters': [], 'order_by': ag.ORDER_BY_AGGREGATE,
         'order_dir': 'desc', 'top_n': 3},
        {'type': ms.STEP_AGGREGATE, 'operation': ag.OPERATION_SUM,
         'source': ms.SOURCE_STEP_1, 'column': ms.INTERMEDIATE_VALUE_COLUMN}]}


class FakeLLM:
    """只伪造 provider 返回值；管线其余环节全部真实。"""

    def __init__(self, turn=None, analysis=None):
        self.turn, self.analysis = turn, analysis
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
        payload = {'turn': self.turn, 'analysis': self.analysis}[kind]
        if payload is None:
            raise RuntimeError('FakeLLM 未提供 %s 响应' % kind)
        yield json.dumps(payload, ensure_ascii=False)


def _run(rep, message, *, turn=None, analysis=None):
    llm = FakeLLM(turn, analysis)
    out = asyncio.run(nl.run_nl_query(
        message, F.catalog_of(rep), llm=llm, user_id=0, session_key='f3'))
    return out, llm.kinds


def _executed_step1(out: Dict[str, Any]) -> Dict[str, Any]:
    return ((out.get('multi_step') or {}).get('step1') or {})


def _assert_authority_applied(out: Dict[str, Any]) -> None:
    """**不只断言数字**：最终执行的第 1 步口径必须已按用户原文校正。"""
    assert out['status'] == nl.STATUS_OK, out.get('message')
    step1 = _executed_step1(out)
    assert step1['operation'] == ag.OPERATION_SUM                 # 不是 count
    assert step1['column'] == AMOUNT                             # 不是空 / 不是 Quantity
    assert step1['top_n'] == 3                                   # 用户原文「前3个」
    multi = out['multi_step']
    assert round(float(multi['value']), 2) == F.SMALL_MULTI_STEP_VALUE == 1400.00
    assert round(float(multi['value']), 2) != WRONG_VALUE        # 明确排除旧错误结果
    assert multi['step2']['input_rows'] == F.SMALL_MULTI_STEP_INPUT_ROWS == 3


# ==========================================================================
# T1：既有修复函数**本身**有能力按用户原文恢复 step1 口径（存在性证明）
# ==========================================================================
def test_first_pass_repair_can_fix_step1_from_user_text(catalog, patched_store):
    """第 1 次解析产出的计划（`raw` 非空）会被 `_repair_analysis_plan_from_text` 校正。"""
    turn = nl.turn_intent_from_dict(_wrong_step1_plan(), source='llm')
    notes = nl._repair_analysis_plan_from_text(turn, MSG, catalog,
                                               nl_norm.detect_signals(MSG))
    assert notes, '既有修复函数必须能识别并纠正 step1 口径'
    step1 = turn.analysis.steps[0]
    assert step1['operation'] == ag.OPERATION_SUM
    assert step1['column'] == AMOUNT
    assert step1['top_n'] == 3                                   # 原文的 N 未被破坏


# ==========================================================================
# T2（F3 核心）：analysis_guard 用第 2 份计划执行时，同样不得绕过该权威
# ==========================================================================
def test_analysis_guard_plan_cannot_bypass_step1_authority(rep, patched_store):
    """第 1 次与守卫第 2 次都给同一份**错误**计划 -> 最终执行口径仍须由用户原文决定。"""
    plan = _wrong_step1_plan()
    out, kinds = _run(rep, MSG, turn=plan, analysis=plan)
    assert kinds == ['turn', 'analysis'], '本用例必须覆盖 analysis_guard 这条路径'
    assert out.get('analysis_guard') is True                     # 确认走的是守卫路径
    _assert_authority_applied(out)


# ==========================================================================
# T3（路径 D）：确定性升级（aggregate -> analysis）同样不得绕过该权威
# ==========================================================================
def test_aggregate_upgrade_cannot_bypass_step1_authority(rep, patched_store):
    """LLM 只给「分组 + TOP-N(cout)」的错误统计，用户问的是前 N 名总量 -> 升级后口径同校。"""
    turn = {'action': 'aggregate', 'aggregate_operation': ag.OPERATION_COUNT, 'column': None,
            'group_by': [SKU], 'filters': [], 'sheet': F.SMALL_SHEET,
            'order_by': ag.ORDER_BY_AGGREGATE, 'order_dir': 'desc', 'top_n': 3}
    out, kinds = _run(rep, MSG, turn=turn)
    assert out.get('analysis_upgraded') is True, '本用例必须覆盖确定性升级路径'
    _assert_authority_applied(out)
