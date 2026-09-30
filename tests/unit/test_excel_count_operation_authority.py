# -*- coding: utf-8 -*-
"""F4（2026-09-30）：**纯计数问句的 operation authority**。

实测缺口（Golden A，离线可确定性复现）：
    用户：「有多少订单」+ LLM 候选 `operation=sum, column=Order Amount`
    -> 修复前实际执行 **SUM(Order Amount) = 1550**（正确：COUNT(行) = **19**）。

根因：`_repair_turn_with_signals` 的 count 规则只认「订单数量 / 单量 / 笔数」这类
``order_count_word``，且要求排行/分组语境；而 `_enforce_user_aggregate_params` 的 operation
分支又被 ``_deterministic_rank_metric(message) is not None`` 挡住 —— 该门槛的**本意**是拦住
「无度量时的 最多/最少 -> MAX/MIN」（见该函数 ③ 注释），对 **COUNT(\*)** 并不必要，
于是「有多少订单」这类纯计数问句**完全失去** operation authority。

既有正式语义（本文件据此修复，不新造业务语义）：
  * `nl_normalize.COUNT_RE` 把「有多少 / 几单 / 几条 / 行数 …」定义为**计数**信号；
  * `_user_operation_from_signals` 明确把 ``signals.count`` 映射为 COUNT，且口径混用 -> None（不猜）；
  * `backend/excel/aggregate.py` 契约：**COUNT 语义固定为"符合筛选条件的数据行数"，不实现
    COUNT(DISTINCT)**；
  * `test_excel_rank_unit.py` 对「一共有多少订单」的期望值即**行数**。

反向边界（不得机械地把「多少」都当 count）：
  * 「订单金额是多少」—— 无计数信号（`COUNT_RE` 不含裸「多少」）→ 不动作；
  * 「有多少订单金额总和」—— 计数与求和口径混用 -> 不猜、不动作；
  * TOP-N / 排行语境（`signals.top_n`）不在此权威范围内；
  * **延期边界（deferred，本轮不 invent 语义、也不新增能力）**：数「某一列的不同取值」的
    问法 —— 「SKU 有多少种」/「有多少个SKU」/「有多少物流商」—— 需要 COUNT(DISTINCT)，
    而系统契约固定 COUNT = **数据行数**、不实现去重（`backend/excel/aggregate.py` 顶部与
    COUNT 分支），因此计数权威**不接管**，保持既有「交给 LLM 判定」的行为。
    实测（Golden A / OrderSKUList）：数据行 **19**，而 SKU ID / Seller SKU 各有 **5 个**
    不同取值（各 14 行重复）、物流商 3 个 —— 行数 ≠ 去重数，故**不得**把
    「有多少个SKU」固化为 19（本文件因此**不**为它写 `count == 19` 的断言）。

Ground Truth 全部来自 `tests/helpers/fixtures.py` 的**人工可推算常量**（19 / 4 / 1550.00 /
分组计数 4、7、8），不调用生产 operation helper 生成 expected。
"""
import asyncio
import json
from typing import Any, Dict, List

import pytest

from backend.excel import aggregate as ag
from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from tests.helpers import fixtures as F

AMOUNT, CARRIER, SKU = F.AMOUNT, F.CARRIER, F.SKU
WRONG_SUM = F.SMALL_TOTAL_SUM          # 1550.00（把「有多少订单」当 SUM 时的错误结果）


@pytest.fixture(autouse=True)
def _no_user_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path / 'no_user_data')


@pytest.fixture(scope='module')
def rep():
    return F.parse_golden(F.GOLDEN_SMALL_NAME, document_id='f4-small')


class FakeLLM:
    """只伪造 provider 返回值；管线其余环节全部真实。"""

    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
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
        if kind == 'analysis':
            return
        if self.payload is None:
            raise RuntimeError('FakeLLM 未提供 %s 响应' % kind)
        yield json.dumps(self.payload, ensure_ascii=False)


def _turn(op='sum', column=AMOUNT, group_by=None, filters=None):
    return {'action': 'aggregate', 'aggregate_operation': op, 'column': column,
            'group_by': list(group_by or []), 'filters': list(filters or []),
            'sheet': F.SMALL_SHEET}


def _run(rep, message, turn):
    saved = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        llm = FakeLLM(turn)
        out = asyncio.run(nl.run_nl_query(
            message, F.catalog_of(rep), llm=llm, user_id=0, session_key='f4'))
        return out, llm.kinds
    finally:
        excel_store.load_representation = saved


def _single(out: Dict[str, Any]) -> Dict[str, Any]:
    return out.get('aggregate') or {}


def _group(out: Dict[str, Any]) -> Dict[str, Any]:
    return out.get('group_aggregate') or {}


# ==========================================================================
# 1) F4 核心：纯计数问句必须按 COUNT 执行（不只断言数字）
# ==========================================================================
def test_how_many_orders_overrides_wrong_sum_candidate(rep):
    """「有多少订单」+ LLM `sum/Order Amount` -> `count` / column=None / 19 行。"""
    out, _kinds = _run(rep, '有多少订单', _turn('sum', AMOUNT))
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert nl.describe_executor(out) == 'run_aggregate'
    agg = _single(out)
    assert agg['operation'] == 'count'                       # 不是 sum
    assert agg['column'] is None                             # COUNT(*) 不使用数值列
    assert agg['matched_rows'] == F.SMALL_ROW_COUNT == 19
    assert round(float(agg['value']), 2) == float(F.SMALL_ROW_COUNT) == 19.0
    assert round(float(agg['value']), 2) != WRONG_SUM        # 明确排除旧的 sum 结果


@pytest.mark.parametrize('message', ['一共有多少订单', '总共有多少订单', '有多少条订单行'])
def test_how_many_phrasings_all_become_count(rep, message):
    """COUNT_RE 的既有计数说法（多少 / 数 / 条）都必须落到 COUNT。"""
    out, _kinds = _run(rep, message, _turn('sum', AMOUNT))
    agg = _single(out)
    assert agg['operation'] == 'count' and agg['column'] is None
    assert round(float(agg['value']), 2) == 19.0


def test_how_many_with_filter_becomes_count_of_matched_rows(rep):
    """「物流商为SF的有几单」+ LLM `sum` -> COUNT(*)，命中 4 行（独立 GT）。"""
    out, _kinds = _run(rep, '物流商为SF的有几单',
                       _turn('sum', AMOUNT,
                             filters=[{'column': CARRIER, 'operator': 'contains',
                                       'value': 'SF'}]))
    agg = _single(out)
    assert agg['operation'] == 'count' and agg['column'] is None
    assert agg['matched_rows'] == F.SMALL_SF_MATCHED == 4
    assert round(float(agg['value']), 2) == 4.0
    assert round(float(agg['value']), 2) != F.SMALL_SF_SUM   # 不是 280.00


def test_grouped_how_many_becomes_count_per_group(rep):
    """「各物流商分别有多少订单」+ LLM `sum/Order Amount` -> 每组 COUNT（4 / 7 / 8）。"""
    out, _kinds = _run(rep, '各物流商分别有多少订单',
                       _turn('sum', AMOUNT, group_by=[CARRIER]))
    group = _group(out)
    assert group['operation'] == 'count' and group['column'] is None
    got = {r['group_display'][0]: round(float(r['value']), 2) for r in group['rows']}
    assert got == {k: float(v) for k, v in F.SMALL_PROVIDER_GROUPS.items()}
    assert sum(got.values()) == F.SMALL_ROW_COUNT
    assert got != {k: v for k, v in _group_sum_by_carrier().items()}   # 不是金额


def _group_sum_by_carrier() -> Dict[str, float]:
    """独立扫描：按物流商求 Order Amount 之和（用于排除"仍然按金额求和"）。"""
    sheet = F.sheet_of(F.parse_golden(F.GOLDEN_SMALL_NAME))
    gi, vi = sheet.column_names.index(CARRIER), sheet.column_names.index(AMOUNT)
    out: Dict[str, float] = {}
    for row in sheet.rows:
        num = F.independent_numeric(row[vi])          # 空值 / 非数值不计入（独立实现）
        if num is not None:
            key = str(row[gi])
            out[key] = round(out.get(key, 0.0) + num, 2)
    return out


# ==========================================================================
# 2) 反向边界：不得机械地把「多少」都当 COUNT
# ==========================================================================
def test_plain_how_much_order_amount_is_not_forced_to_count(rep):
    """「订单金额是多少」—— 无计数信号（COUNT_RE 不含裸「多少」）-> 权威**不动作**。"""
    out, _kinds = _run(rep, '订单金额是多少', _turn('sum', AMOUNT))
    agg = _single(out)
    assert agg['operation'] == 'sum'                         # 保持候选值
    assert round(float(agg['value']), 2) == WRONG_SUM


@pytest.mark.parametrize('message,why', [
    ('SKU 有多少种', '「种」= 去重计数标记'),
    ('有多少个SKU', '计数目标是 SKU 列（需 DISTINCT；行数 19 ≠ SKU 去重数 5）'),
    ('有多少物流商', '计数目标是物流商列（需 DISTINCT；行数 19 ≠ 物流商数 3）'),
])
def test_column_value_count_questions_are_deferred_boundary(rep, message, why):
    """**deferred semantic boundary（本轮不 invent 语义 / 不新增 DISTINCT 能力）**：

    数「某一列不同取值」的问法不由计数权威接管 —— 保持候选（既不静默返回行数，
    也不假装已解决）。本用例**只断言"未被强制成 count"**，不对其"正确值"下结论。
    """
    out, _kinds = _run(rep, message, _turn('sum', AMOUNT))
    agg = _single(out)
    assert agg['operation'] != 'count', '%s（%s）不得被强制成 COUNT(*)' % (message, why)
    assert agg['operation'] == 'sum'                         # 保持 LLM 候选（交给 LLM 判定）


def test_mixed_count_and_sum_caliber_is_not_guessed(rep):
    """「有多少订单金额总和」——「有多少」+「总和」口径混用 -> `_user_operation_from_signals`
    返回 None（不猜），operation 保持候选。"""
    out, _kinds = _run(rep, '有多少订单金额总和', _turn('sum', AMOUNT))
    agg = _single(out)
    assert agg['operation'] == 'sum'
    assert round(float(agg['value']), 2) == WRONG_SUM


# ==========================================================================
# 3) 无副作用：明确的 SUM / AVG / TOP-N / Quantity 语义不变
# ==========================================================================
def test_explicit_sum_and_avg_still_win(rep):
    """「订单金额总和是多少」+ LLM `avg` -> sum；「订单金额平均是多少」+ LLM `sum` -> avg。"""
    out, _kinds = _run(rep, '订单金额总和是多少', _turn('avg', AMOUNT))
    agg = _single(out)
    assert agg['operation'] == 'sum' and round(float(agg['value']), 2) == WRONG_SUM

    out2, _kinds2 = _run(rep, '订单金额平均是多少', _turn('sum', AMOUNT))
    agg2 = _single(out2)
    assert agg2['operation'] == 'avg'
    # AVG 口径：只对**可数值化**的行求平均（Golden A 数值行 17，见独立常量）
    assert round(float(agg2['value']), 4) == round(WRONG_SUM / F.SMALL_TOTAL_NUMERIC, 4)


def test_topn_quantity_semantics_untouched(rep):
    """TOP-N 语境（`signals.top_n` 非空）不在 operation 权威范围内：

    「订单数量最多的前3个订单」+ LLM `sum/Quantity` 必须保持 sum + Quantity（既有封板口径）。
    """
    out, _kinds = _run(rep, '订单数量最多的前3个订单',
                       _turn('sum', 'Quantity', group_by=[SKU]))
    assert out['status'] == nl.STATUS_OK, out.get('message')
    executed = out.get('group_aggregate') or {}
    assert executed['operation'] == 'sum'
    assert executed['column'] == 'Quantity'
