# -*- coding: utf-8 -*-
"""Stage 5 第三项：**Candidate Injection Matrix**（对抗式候选注入回归）。

思路（与本轮要求一致）：

    用户文本携带**正确语义** + FakeLLM 故意返回**错误候选**
        -> 真实生产管线（`nl_query.run_nl_query` 全链路）
        -> 断言最终**执行语义**是否仍然忠于用户文本

三种被接受的结果形态：

    A. authority 能由文本**确定**用户语义 -> 纠正错误候选（断言纠正后的执行结果）
    B. 用户语义不足以确定 -> 保持既有澄清 / 不猜行为
    C. 候选无法安全纠正 -> 明确拒绝（clarify / 既有错误路径），绝不静默执行

边界与纪律：
  * **零真实 LLM**：`FakeLLM` 只负责伪造 provider 的返回值（按 system prompt 区分
    轮次解析 / 统计抽取 / 两步计划），其余全部是真实生产代码；
  * **Expected 独立**：期望值来自 `tests/helpers/fixtures.py` 的**人工可推算常量**与本文件
    **手写扫描**（`_gt_*`），**不调用**生产 authority / matcher / 执行结果来生成 expected；
  * **只做回归覆盖，不改生产代码**：下文中 `*_boundary` 命名的用例记录的是**既有产品边界**
    （权威层刻意不做的事），不是本轮新增语义，也不是对缺陷的背书。
"""
import asyncio
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from backend.excel import aggregate as ag
from backend.excel import multi_step as ms
from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from tests.helpers import fixtures as F

CARRIER, AMOUNT, SKU = F.CARRIER, F.AMOUNT, F.SKU
ORDER_ID, CREATED, QUANTITY = 'Order ID', 'Created Time', 'Quantity'
STATUS_FILTER = 'Order Status'


# ==========================================================================
# 夹具 / 运行器（只伪造 LLM 返回值）
# ==========================================================================
@pytest.fixture(autouse=True)
def _no_user_data_dir(tmp_path, monkeypatch):
    """Golden fixture 回归不得依赖用户 `data/`（fresh clone 可跑）。"""
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path / 'no_user_data')


@pytest.fixture(scope='module')
def small_rep():
    return F.parse_golden(F.GOLDEN_SMALL_NAME, document_id='inject-small')


@pytest.fixture(scope='module')
def medium_rep():
    return F.parse_golden(F.GOLDEN_MEDIUM_NAME, document_id='inject-medium')


@pytest.fixture(scope='module')
def small_sheet(small_rep):
    return F.sheet_of(small_rep)


@pytest.fixture(scope='module')
def medium_sheet(medium_rep):
    return F.sheet_of(medium_rep, F.MEDIUM_SHEET)


class FakeLLM:
    """只伪造 provider 返回值；管线中其余环节全部真实。"""

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


def _catalog(reps: Sequence[Any]) -> List[Dict[str, Any]]:
    return [{
        'document_id': r.document_id, 'filename': r.filename, 'file_type': r.file_type,
        'created_at': r.created_at, 'total_rows': r.total_rows,
        'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                    'row_count': s.row_count, 'column_count': s.column_count,
                    'columns': s.column_names} for s in r.sheets],
    } for r in reps]


def _run(reps: Sequence[Any], message: str, *, turn=None, aggregate=None, analysis=None):
    """跑一次真实 NL 请求；只有 `load_representation` 被指向内存 representation。"""
    mapping = {r.document_id: r for r in reps}
    saved = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: mapping.get(document_id)
    try:
        return asyncio.run(nl.run_nl_query(
            message, _catalog(reps), llm=FakeLLM(turn, aggregate, analysis),
            user_id=0, session_key='candidate-injection'))
    finally:
        excel_store.load_representation = saved


# --- 候选构造 --------------------------------------------------------------
def _t_new(columns=(), sheet=F.SMALL_SHEET, filters=None):
    return {'action': 'new_query', 'sheet': sheet, 'columns': list(columns),
            'filters': list(filters or [])}


def _t_agg(operation, column=None, *, group_by=None, filters=None, sheet=F.SMALL_SHEET,
           order_by=None, order_dir=None, top_n=None, document=None):
    payload = {'action': 'aggregate', 'aggregate_operation': operation, 'column': column,
               'group_by': list(group_by or []), 'filters': list(filters or []), 'sheet': sheet}
    if document:
        payload['document'] = document
    if order_by:
        payload['order_by'] = order_by
        payload['order_dir'] = order_dir
    if top_n:
        payload['top_n'] = top_n
    return payload


def _step1(group_by, *, operation=ag.OPERATION_SUM, column=AMOUNT, top_n=3,
           order_by='aggregate_value', order_dir='desc'):
    return {'type': ms.STEP_GROUP_AGGREGATE, 'group_by': list(group_by),
            'operation': operation, 'column': column, 'filters': [],
            'order_by': order_by, 'order_dir': order_dir, 'top_n': top_n}


def _step2(operation=ag.OPERATION_SUM, column=ms.INTERMEDIATE_VALUE_COLUMN):
    return {'type': ms.STEP_AGGREGATE, 'operation': operation, 'source': ms.SOURCE_STEP_1,
            'column': column}


def _plan(steps, sheet=F.SMALL_SHEET):
    return {'action': 'analysis', 'document': None, 'sheet': sheet, 'steps': list(steps)}


# ==========================================================================
# 独立 Ground Truth（本文件手写；不复用生产 authority / matcher / 执行结果）
# ==========================================================================
def _idx(sheet, name: str) -> int:
    return sheet.column_names.index(name)


def _num(value: Any) -> Optional[float]:
    """独立数值判定（仅覆盖 fixture 用到的形态）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _match_cell(cell: Any, operator: str, value: Any) -> bool:
    text = '' if cell is None else str(cell)
    if operator == 'contains':
        return str(value) in text
    if operator == 'eq':
        return text == str(value)
    if operator == 'gt':
        num = _num(cell)
        return num is not None and num > float(value)
    raise AssertionError('未支持的 operator：%s' % operator)


def _gt_rows(sheet, conditions: Sequence[Tuple[str, str, Any]]) -> List[int]:
    """独立筛选扫描 -> 命中的 Excel 行号（AND 语义）。"""
    out: List[int] = []
    for i, row in enumerate(sheet.rows):
        if all(_match_cell(row[_idx(sheet, c)], op, v) for c, op, v in conditions):
            out.append(sheet.row_excel_numbers[i])
    return out


def _gt_sum(sheet, rows: Sequence[int], column: str = AMOUNT) -> float:
    by_row = {sheet.row_excel_numbers[i]: sheet.rows[i] for i in range(len(sheet.rows))}
    ci = _idx(sheet, column)
    total = 0.0
    for r in rows:
        num = _num(by_row[r][ci])
        if num is not None:
            total += num
    return round(total, 2)


def _gt_group_sum(sheet, group_column: str, value_column: str = AMOUNT) -> Dict[str, float]:
    gi, vi = _idx(sheet, group_column), _idx(sheet, value_column)
    buckets: Dict[str, float] = {}
    for row in sheet.rows:
        key = str(row[gi])
        num = _num(row[vi])
        buckets[key] = round(buckets.get(key, 0.0) + (num or 0.0), 2)
    return buckets


def _gt_rows_for_sum(sheet, column: str = AMOUNT) -> float:
    return _gt_sum(sheet, list(sheet.row_excel_numbers), column)


def _applied(out: Dict[str, Any], key: str = 'aggregate') -> List[Tuple[str, str, Any]]:
    return [(f['column'], f['operator'], f['value'])
            for f in ((out.get(key) or {}).get('applied_filters') or [])]


def _group_names(out: Dict[str, Any]) -> List[str]:
    return [g['name'] for g in ((out.get('group_aggregate') or {}).get('group_by') or [])]


def _group_values(out: Dict[str, Any]) -> Dict[str, float]:
    return {r['group_display'][0]: round(float(r['value']), 2)
            for r in ((out.get('group_aggregate') or {}).get('rows') or [])}


def _step1_names(out: Dict[str, Any]) -> List[str]:
    steps = ((out.get('multi_step') or {}).get('step1') or {}).get('group_by') or []
    return [g['name'] if isinstance(g, dict) else str(g) for g in steps]


# ==========================================================================
# 1. document：用户点名文件 / LLM 给另一个文件
# ==========================================================================
def test_document_candidate_is_overridden_by_user_text(small_rep, medium_rep):
    """用户点名 golden_small.xlsx，LLM 给 golden_medium.xlsx -> 必须执行 small。"""
    out = _run([small_rep, medium_rep], 'golden_small.xlsx 的订单金额总和是多少',
               turn=_t_agg('sum', AMOUNT, document='golden_medium.xlsx'))
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert (out.get('document') or {}).get('filename') == F.GOLDEN_SMALL_NAME
    agg = out['aggregate']
    assert round(float(agg['value']), 2) == F.SMALL_TOTAL_SUM          # 1550.00（小表）
    assert agg['value'] != F.MEDIUM_TOTAL_SUM                          # 绝不落成大表
    assert agg['total_rows_in_sheet'] == F.SMALL_ROW_COUNT


# ==========================================================================
# 2. sheet：用户点名 Sheet / LLM 给另一个 Sheet
# ==========================================================================
def test_sheet_candidate_is_overridden_by_user_text(small_rep):
    """用户点名 SkuMaster，LLM 给 OrderSKUList -> 必须执行 SkuMaster。"""
    out = _run([small_rep], '列出SkuMaster里的SKU ID',
               turn=_t_new([SKU], sheet=F.SMALL_SHEET))
    assert out['status'] == nl.STATUS_OK, out.get('message')
    res = out['result']
    assert res['sheet_name'] == 'SkuMaster'
    assert res['total_matches'] == 5                                   # SkuMaster 只有 5 行
    assert [r[0] for r in res['rows']] == [F.SMALL_SKU_ID(k) for k in range(1, 6)]


# ==========================================================================
# 3. columns：用户指定输出列 / LLM 给其他列
# ==========================================================================
def test_columns_candidate_is_overridden_by_user_text(small_rep, small_sheet):
    """「列出订单号和物流商」+ LLM `columns=['SKU ID']` -> 必须返回用户点名的两列。"""
    out = _run([small_rep], '列出订单号和物流商', turn=_t_new([SKU]))
    res = out['result']
    assert [c['name'] for c in res['columns']] == [ORDER_ID, CARRIER]
    expected = [[row[_idx(small_sheet, ORDER_ID)], row[_idx(small_sheet, CARRIER)]]
                for row in small_sheet.rows]
    assert [[r[0], r[1]] for r in res['rows']] == expected
    assert res['total_matches'] == F.SMALL_ROW_COUNT


def test_columns_candidate_is_overridden_on_medium(medium_rep, medium_sheet):
    """Golden B：447 行也必须整批返回用户点名的两列（不被 LLM 的列候选改写）。"""
    out = _run([medium_rep], '列出订单号和物流商',
               turn=_t_new([QUANTITY], sheet=F.MEDIUM_SHEET))
    res = out['result']
    assert [c['name'] for c in res['columns']] == [ORDER_ID, CARRIER]
    assert res['total_matches'] == F.MEDIUM_ROW_COUNT == 447
    assert len(res['rows']) == F.MEDIUM_ROW_COUNT
    assert [r[0] for r in res['rows']] == [
        row[_idx(medium_sheet, ORDER_ID)] for row in medium_sheet.rows]


# ==========================================================================
# 4. metric：用户指定度量列 / LLM 给 Quantity
# ==========================================================================
def test_metric_candidate_is_overridden_by_user_text(small_rep, small_sheet):
    """「各物流商的订单金额总和」+ LLM `column=Quantity` -> 必须按 Order Amount 求和。"""
    out = _run([small_rep], '各物流商的订单金额总和是多少',
               turn=_t_agg('sum', QUANTITY, group_by=[CARRIER]))
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _group_names(out) == [CARRIER]
    gt = _gt_group_sum(small_sheet, CARRIER, AMOUNT)
    assert _group_values(out) == gt
    assert gt['SF International'] == F.SMALL_SF_SUM == 280.00          # 独立可推算
    assert sum(gt.values()) == F.SMALL_TOTAL_SUM                       # 1550.00
    # 若真的按 Quantity 求和，结果会完全不同 -> 明确排除
    assert _group_values(out) != _gt_group_sum(small_sheet, CARRIER, QUANTITY)


# ==========================================================================
# 5. operation：用户要求 SUM / LLM 给 COUNT
# ==========================================================================
def test_operation_candidate_is_overridden_by_user_text(small_rep):
    """「订单金额总和是多少」+ LLM `operation=count` -> 必须是 SUM（1550.00）。"""
    out = _run([small_rep], '订单金额总和是多少', turn=_t_agg('count', AMOUNT))
    assert out['status'] == nl.STATUS_OK, out.get('message')
    agg = out['aggregate']
    assert agg['operation'] == 'sum'
    assert round(float(agg['value']), 2) == F.SMALL_TOTAL_SUM != F.SMALL_ROW_COUNT


# ==========================================================================
# 6. group_by：用户要求按物流商 / LLM 给 SKU
# ==========================================================================
def test_group_by_candidate_is_overridden_by_user_text(small_rep, small_sheet):
    """「各物流商…」+ LLM `group_by=['SKU ID']` -> 必须按 Shipping Provider Name 分组。"""
    out = _run([small_rep], '各物流商的订单金额总和是多少',
               turn=_t_agg('sum', AMOUNT, group_by=[SKU]))
    assert _group_names(out) == [CARRIER]
    assert set(_group_values(out)) == set(_gt_group_sum(small_sheet, CARRIER)) \
        == set(F.SMALL_PROVIDER_GROUPS)
    assert SKU not in _group_names(out)


# ==========================================================================
# 7. filter operator：用户「大于」/ LLM 给 eq
# ==========================================================================
def test_filter_operator_candidate_is_overridden_by_user_text(small_rep, small_sheet):
    """「数量大于3的订单金额总和」+ LLM `(Quantity, eq, 3)` -> 必须按 gt 3 执行。"""
    out = _run([small_rep], '数量大于3的订单金额总和是多少',
               turn=_t_agg('sum', AMOUNT, filters=[{'column': QUANTITY, 'operator': 'eq',
                                                    'value': 3}]))
    assert _applied(out) == [(QUANTITY, 'gt', 3.0)]                    # 同列被整体替换
    expected_rows = _gt_rows(small_sheet, [(QUANTITY, 'gt', 3)])
    agg = out['aggregate']
    assert agg['matched_rows'] == len(expected_rows)
    assert round(float(agg['value']), 2) == _gt_sum(small_sheet, expected_rows)
    assert agg['value'] != _gt_sum(small_sheet, _gt_rows(small_sheet, [(QUANTITY, 'eq', 3)]))


# ==========================================================================
# 8. filter value：用户 SF / LLM 给 UPS
# ==========================================================================
def test_filter_value_candidate_is_overridden_by_user_text(small_rep, small_sheet):
    """「物流商为SF的订单金额总和」+ LLM `(Shipping Provider Name, contains, 'UPS')`。"""
    out = _run([small_rep], '物流商为SF的订单金额总和是多少',
               turn=_t_agg('sum', AMOUNT, filters=[{'column': CARRIER,
                                                    'operator': 'contains', 'value': 'UPS'}]))
    assert _applied(out) == [(CARRIER, 'contains', 'SF')]
    agg = out['aggregate']
    assert agg['matched_rows'] == len(F.SMALL_SF_ROWS) == F.SMALL_SF_MATCHED
    assert round(float(agg['value']), 2) == _gt_sum(small_sheet, _gt_rows(
        small_sheet, [(CARRIER, 'contains', 'SF')])) == F.SMALL_SF_SUM


def test_filter_value_candidate_is_overridden_on_medium(medium_rep):
    """Golden B：「物流商为SF的订单有几单」+ LLM 给 JS -> 必须计 SF（149）。"""
    out = _run([medium_rep], '物流商为SF的订单有几单',
               turn=_t_agg('count', None, sheet=F.MEDIUM_SHEET,
                           filters=[{'column': CARRIER, 'operator': 'contains',
                                     'value': 'JS'}]))
    assert _applied(out) == [(CARRIER, 'contains', 'SF')]
    assert out['aggregate']['matched_rows'] == F.MEDIUM_SF_COUNT == 149


# ==========================================================================
# 9. order_by：用户按度量排序 / LLM 给按分组列排序
# ==========================================================================
def test_order_by_candidate_is_overridden_by_user_text(small_rep, small_sheet):
    """「各物流商按订单金额降序排列」+ LLM `order_by=物流商` -> 必须按聚合值排序。"""
    out = _run([small_rep], '各物流商按订单金额降序排列',
               turn=_t_agg('sum', AMOUNT, group_by=[CARRIER], order_by=CARRIER,
                           order_dir='desc'))
    group = out['group_aggregate']
    assert group['order_by'] == 'aggregate_value'                       # 不是维度列
    values = [round(float(r['value']), 2) for r in group['rows']]
    assert values == sorted(values, reverse=True)
    assert values == sorted(_gt_group_sum(small_sheet, CARRIER).values(), reverse=True)


# ==========================================================================
# 10. order_dir：用户「从低到高」/ LLM 给 desc
# ==========================================================================
def test_order_dir_candidate_is_overridden_by_user_text(small_rep, small_sheet):
    """「…从低到高排列」+ LLM `order_dir=desc` -> 必须升序（绝不静默反序）。"""
    out = _run([small_rep], '各物流商订单金额总和从低到高排列',
               turn=_t_agg('sum', AMOUNT, group_by=[CARRIER], order_by='aggregate_value',
                           order_dir='desc'))
    group = out['group_aggregate']
    assert group['order_dir'] == 'asc'
    values = [round(float(r['value']), 2) for r in group['rows']]
    assert values == sorted(_gt_group_sum(small_sheet, CARRIER).values())


# ==========================================================================
# 11. top_n：用户「3个」/ LLM 给 10
# ==========================================================================
def test_top_n_candidate_is_overridden_by_user_text(small_rep, small_sheet):
    """「订单金额最高的3个SKU」+ LLM `top_n=10` -> 必须只返回 3 个分组。"""
    out = _run([small_rep], '订单金额最高的3个SKU',
               turn=_t_agg('sum', AMOUNT, group_by=[SKU], order_by='aggregate_value',
                           order_dir='desc', top_n=10))
    group = out['group_aggregate']
    assert group['top_n'] == 3 and group['returned_groups'] == 3
    assert group['truncated_by_top_n'] is True
    gt = _gt_group_sum(small_sheet, SKU)
    expected_top3 = sorted(gt.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    assert [(r['group_display'][0], round(float(r['value']), 2)) for r in group['rows']] \
        == expected_top3 == [(F.SMALL_SKU_ID(5), 1000.00), (F.SMALL_SKU_ID(4), 270.00),
                             (F.SMALL_SKU_ID(3), 130.00)]


# ==========================================================================
# 12. ranking_unit：用户要订单级 TOP-N / LLM 给 SKU 级
# ==========================================================================
def test_ranking_unit_candidate_is_overridden_by_user_text(small_rep):
    """「订单金额最高的前3个订单」（单位=订单）+ LLM `group_by=[SKU ID]` -> 单位必须纠正。"""
    out = _run([small_rep], '订单金额最高的前3个订单',
               turn=_t_agg('sum', AMOUNT, group_by=[SKU], order_by='aggregate_value',
                           order_dir='desc', top_n=3))
    assert _group_names(out) == [ORDER_ID]
    assert SKU not in _group_names(out)
    assert out['group_aggregate']['returned_groups'] == 3


# ==========================================================================
# 13. date condition：用户明确日期 / LLM 退化成 contains
# ==========================================================================
def test_date_condition_candidate_is_repaired_to_date_range(small_rep, small_sheet):
    """「2026年8月5日的订单金额总和」+ LLM `(Created Time, contains, ...)` -> 必须落日期区间。"""
    out = _run([small_rep], '2026年8月5日的订单金额总和是多少',
               turn=_t_agg('sum', AMOUNT, filters=[{'column': CREATED, 'operator': 'contains',
                                                    'value': '2026-08-05'}]))
    assert _applied(out) == [(CREATED, 'date_between', ['2026-08-05', '2026-08-05'])]
    agg = out['aggregate']
    rows = [n for i, n in enumerate(small_sheet.row_excel_numbers)
            if str(small_sheet.rows[i][_idx(small_sheet, CREATED)]).startswith('2026-08-05')]
    assert rows == [7] and agg['matched_rows'] == 1
    assert round(float(agg['value']), 2) == _gt_sum(small_sheet, rows) == 50.00


# ==========================================================================
# 14. analysis step1：口径（度量列 / TOP-N）错误候选
# ==========================================================================
MULTI_MSG = '订单金额最高的前3个SKU的销售额总和是多少'


def test_analysis_step1_metric_candidate_is_overridden(small_rep):
    """两步分析 step1 的**度量列**：LLM 给 Quantity -> 必须以 Order Amount 汇总。"""
    plan = _plan([_step1([SKU], column=QUANTITY), _step2()])
    out = _run([small_rep], MULTI_MSG, turn=plan, analysis=plan)
    assert out['status'] == nl.STATUS_OK, out.get('message')
    multi = out['multi_step']
    assert multi['step1']['column'] == AMOUNT
    assert round(float(multi['value']), 2) == F.SMALL_MULTI_STEP_VALUE == 1400.00


def test_analysis_step1_top_n_candidate_is_overridden(small_rep):
    """两步分析 step1 的 **TOP-N**：LLM 给 10 -> 必须以用户的「前 3 个」执行。"""
    plan = _plan([_step1([SKU], top_n=10), _step2()])
    out = _run([small_rep], MULTI_MSG, turn=plan, analysis=plan)
    multi = out['multi_step']
    assert multi['step1']['top_n'] == 3
    assert multi['step2']['input_rows'] == F.SMALL_MULTI_STEP_INPUT_ROWS == 3
    assert round(float(multi['value']), 2) == F.SMALL_MULTI_STEP_VALUE


# ==========================================================================
# 15. analysis step2：operation 错误候选
# ==========================================================================
def test_analysis_step2_operation_candidate_is_overridden(small_rep):
    """用户「…销售额**总和**」+ LLM step2 `avg` -> 必须 SUM（1400.00，而非 466.67）。"""
    plan = _plan([_step1([SKU]), _step2(ag.OPERATION_AVG)])
    out = _run([small_rep], MULTI_MSG, turn=plan, analysis=plan)
    multi = out['multi_step']
    assert multi['step2']['operation'] == 'sum'
    assert round(float(multi['value']), 2) == F.SMALL_MULTI_STEP_VALUE
    assert multi['step2']['input_rows'] == multi['step2']['numeric_rows'] == 3


# ==========================================================================
# 16. analysis step2：第二步读取原始列（无法安全执行）-> 必须明确拒绝
# ==========================================================================
def test_analysis_step2_raw_column_candidate_is_rejected(small_rep):
    """step2 指向**原始列**而非第 1 步产出值列 -> 属于形态 C：明确澄清，绝不静默执行。"""
    plan = _plan([_step1([SKU]), _step2(ag.OPERATION_SUM, AMOUNT)])
    out = _run([small_rep], MULTI_MSG, turn=plan, analysis=plan)
    assert out['status'] == nl.STATUS_CLARIFY
    assert '第 2 步' in (out.get('message') or '')
    assert not out.get('multi_step') and not out.get('result')


# ==========================================================================
# 既有产品边界（记录，不修改；不是本轮新增语义）
# ==========================================================================
def test_filter_authority_is_same_column_only_boundary(small_rep, small_sheet):
    """**既有边界**：filter authority 只覆盖**同列**条件（`nl_query` 文档字符串：

    「用户明确的 field / operator / value 覆盖 LLM 的**同列**条件」）。
    LLM 在**另一列**上给出的条件按既有 AND 语义**追加保留** —— 本用例固定这一行为，
    使未来任何改动都必须是有意为之（而不是被误当成"用户条件已全权接管"）。
    """
    extra = (STATUS_FILTER, 'eq', 'Shipped')
    out = _run([small_rep], '物流商为SF的订单金额总和是多少',
               turn=_t_agg('sum', AMOUNT,
                           filters=[{'column': extra[0], 'operator': extra[1],
                                     'value': extra[2]}]))
    applied = _applied(out)
    assert (CARRIER, 'contains', 'SF') in applied                        # 用户条件生效
    assert extra in applied                                              # 另一列被保留（AND）
    rows = _gt_rows(small_sheet, [(CARRIER, 'contains', 'SF'), extra])
    assert out['aggregate']['matched_rows'] == len(rows)
    assert round(float(out['aggregate']['value']), 2) == _gt_sum(small_sheet, rows)
    assert out['aggregate']['value'] != F.SMALL_SF_SUM                   # 与"仅 SF"不同


def test_temporal_repair_keeps_llm_extra_column_filter_boundary(small_rep, small_sheet):
    """**既有边界**：时间语义落点只**新增/替换日期列条件**，不会删除 LLM 在其它列上的条件。

    因此「日期落点正确」与「最终结果正确」在这个对抗场景下并不等价：额外条件仍然参与 AND。
    本用例把这一边界固定下来（与上一条同源：`_enforce_user_filters` 仅同列替换）。
    """
    out = _run([small_rep], '2026年8月5日的订单金额总和是多少',
               turn=_t_agg('sum', AMOUNT, filters=[{'column': ORDER_ID,
                                                    'operator': 'contains', 'value': '2026'}]))
    applied = _applied(out)
    assert (CREATED, 'date_between', ['2026-08-05', '2026-08-05']) in applied   # 落点正确
    assert (ORDER_ID, 'contains', '2026') in applied                            # 额外条件保留
    rows = _gt_rows(small_sheet, [(ORDER_ID, 'contains', '2026'),
                                  (CREATED, 'contains', '2026-08-05')])
    assert rows == []                                                    # 订单号里没有日期
    assert out['aggregate']['matched_rows'] == 0
    assert out['aggregate']['value'] is None


def test_analysis_step1_group_by_has_no_authority_boundary(small_rep):
    """**既有边界**：TOP-N 语境下，`_enforce_user_aggregate_params` 的 group_by 分支**刻意不生效**
    （其判据含 ``signals.top_n is None``），两步分析的第 1 步分组维度因此没有对应权威层
    （单步分组统计有 —— 见 `test_group_by_candidate_is_overridden_by_user_text`）。

    本用例固定该边界：step1 的分组维度沿用候选，值也因此不同（1550 而非 1400）。
    """
    plan = _plan([_step1([CARRIER]), _step2()])
    out = _run([small_rep], MULTI_MSG, turn=plan, analysis=plan)
    assert out['status'] == nl.STATUS_OK, out.get('message')
    multi = out['multi_step']
    assert _step1_names(out) == [CARRIER]                                 # 候选维度沿用
    assert round(float(multi['value']), 2) == F.SMALL_TOTAL_SUM           # 1550.00（全表）
    assert round(float(multi['value']), 2) != F.SMALL_MULTI_STEP_VALUE    # 1400.00
