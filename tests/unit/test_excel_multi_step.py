# -*- coding: utf-8 -*-
"""Phase 4A：复杂多步分析（受控两阶段）测试矩阵。

核心验收问题：
    找出一店中金额最高的 10 个 SKU，并统计它们的总销售额。

本文件同时覆盖：
  1) 计划校验（2 步 / 1 步 / 3 步 / 注入）
  2) Step 1 五种 operation（count/sum/avg/min/max）+ 排序 + TOP-N
  3) Step 2 五种 operation（**只读 Step 1 结果**）
  4) 确定性 tie-breaker（与 Phase 3C 一致）
  5) 边界：top_n 小于/等于/大于分组数、0 行、1 组、无数据
  6) 独立 Ground Truth（本文件手写 扫描→分组→聚合→排序→TOP-N→再聚合，不 import 被测实现）
  7) NL 路由：analysis 动作、兜底、上下文继承、超步数澄清
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from backend.excel import aggregate as agg
from backend.excel import multi_step as ms
from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from backend.excel.parser import ExcelParser
from backend.excel.query_context import AnalysisContext, reset_analysis_store
from backend.excel.query import ExcelQueryError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMALL_REAL = PROJECT_ROOT / '直邮一店 8.20号订单.xlsx'
DATA_ROOT = PROJECT_ROOT / 'data' / 'excel'

TOL = dict(rel=1e-9, abs=1e-6)

SKU = 'SKU ID'
AMOUNT = 'Order Amount'
CARRIER = 'Shipping Provider Name'
QTY = 'Quantity'

#: 真实锚点（小表 19 行；独立手算得到）
GT_TOP10_SKU_TOTAL = 267.61
GT_ALL_SKU_TOTAL = 306.02


# ==========================================================================
# 夹具
# ==========================================================================
@pytest.fixture(scope='module')
def small_rep():
    if not SMALL_REAL.exists():
        pytest.skip('未找到小表（直邮一店 8.20号订单.xlsx）')
    return ExcelParser().parse(str(SMALL_REAL), 'ms-small', user_id=1, filename=SMALL_REAL.name)


@pytest.fixture(scope='module')
def big_rep():
    """从 data/excel 中取一份 ≥100 行的真实 representation（447 行大表）。"""
    if not DATA_ROOT.exists():
        pytest.skip('没有 data/excel 目录')
    best = None
    for d in DATA_ROOT.iterdir():
        rep_path = d / 'representation.json'
        if not rep_path.exists():
            continue
        try:
            rep = excel_store.load_representation(d.name)
        except Exception:  # noqa: BLE001
            continue
        if rep is None:
            continue
        rows = rep.total_rows
        if rows >= 100 and (best is None or rows > best.total_rows):
            best = rep
    if best is None:
        pytest.skip('未找到 ≥100 行的真实表格')
    return best


@pytest.fixture(autouse=True)
def _isolate_contexts():
    reset_analysis_store()
    yield


def _catalog(rep) -> List[Dict[str, Any]]:
    return [{
        'document_id': rep.document_id,
        'filename': rep.filename,
        'file_type': rep.file_type,
        'created_at': rep.created_at,
        'total_rows': rep.total_rows,
        'sheets': [{
            'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
            'row_count': s.row_count, 'column_count': s.column_count,
            'columns': s.column_names,
        } for s in rep.sheets],
    }]


def _plan(steps: List[Dict[str, Any]]) -> ms.AnalysisPlan:
    return ms.normalize_analysis_plan({'steps': steps})


def _group_step(**kw) -> Dict[str, Any]:
    base = {'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'sum',
            'column': AMOUNT, 'order_by': 'aggregate_value', 'order_dir': 'desc'}
    base.update(kw)
    return base


def _agg_step(**kw) -> Dict[str, Any]:
    base = {'type': 'aggregate', 'operation': 'sum',
            'source': 'step_1', 'column': 'aggregate_value'}
    base.update(kw)
    return base


# ==========================================================================
# 1. 独立 Ground Truth（手写，不依赖被测实现）
# ==========================================================================
def _to_f(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _key_sort(key: Tuple[Any, ...]) -> Tuple[Any, ...]:
    return tuple((1, '') if k is None else (0, str(k)) for k in key)


def gt_two_step(rep, *, operation1, column, group_by, order_dir='desc', top_n=10,
                operation2='sum', filters=(), sheet_index=0) -> Dict[str, Any]:
    """独立实现：扫描 → 分组 → 聚合 → 排序(+tie-breaker) → TOP-N → 再聚合。"""
    sheet = rep.sheets[sheet_index]
    names = sheet.column_names
    gi = [names.index(g) for g in group_by]
    ci = names.index(column) if column else None

    buckets: Dict[Tuple[Any, ...], List[Any]] = {}
    matched = 0
    for row in sheet.rows:
        ok = True
        for f in filters:
            col_i = names.index(f['column'])
            cell = row[col_i] if col_i < len(row) else None
            if f['operator'] == 'contains':
                if cell is None or str(f['value']).lower() not in str(cell).lower():
                    ok = False
            elif f['operator'] == 'eq':
                if str(cell) != str(f['value']):
                    ok = False
            if not ok:
                break
        if not ok:
            continue
        matched += 1
        key = tuple(row[i] if i < len(row) else None for i in gi)
        buckets.setdefault(key, []).append(row)

    entries: List[Tuple[Tuple[Any, ...], Optional[float]]] = []
    for key, rows in buckets.items():
        if operation1 == 'count':
            entries.append((key, float(len(rows))))
            continue
        nums = [n for n in (_to_f(r[ci] if ci is not None and ci < len(r) else None) for r in rows)
                if n is not None]
        if not nums:
            entries.append((key, None))
        elif operation1 == 'sum':
            entries.append((key, float(sum(nums))))
        elif operation1 == 'avg':
            entries.append((key, float(sum(nums) / len(nums))))
        elif operation1 == 'min':
            entries.append((key, float(min(nums))))
        else:
            entries.append((key, float(max(nums))))

    numeric = [e for e in entries if e[1] is not None]
    nulls = [e for e in entries if e[1] is None]
    numeric.sort(key=lambda e: _key_sort(e[0]))          # tie-breaker：分组键升序
    numeric.sort(key=lambda e: e[1], reverse=(order_dir == 'desc'))
    ordered = numeric + nulls

    total_groups = len(ordered)
    selected = ordered[:top_n] if top_n else ordered
    values = [v for _, v in selected if v is not None]
    if operation2 == 'count':
        value2: Optional[float] = float(len(selected))
    elif not values:
        value2 = None
    elif operation2 == 'sum':
        value2 = float(sum(values))
    elif operation2 == 'avg':
        value2 = float(sum(values) / len(values))
    elif operation2 == 'min':
        value2 = float(min(values))
    else:
        value2 = float(max(values))

    return {'ordered': selected, 'total_groups': total_groups, 'matched_rows': matched,
            'value2': value2, 'input_rows': len(selected)}


def _run_plan(rep, steps, sheet_index=0):
    return ms.execute_analysis(rep, _plan(steps), sheet_index=sheet_index)


# ==========================================================================
# 2. 计划校验矩阵
# ==========================================================================
def test_two_steps_accepted():
    plan = _plan([_group_step(top_n=10), _agg_step()])
    assert plan.step_count == 2
    assert plan.step1.is_group_step and plan.step2.is_aggregate_step
    assert plan.to_dict()['max_steps'] == ms.MAX_STEPS


def test_one_step_rejected():
    with pytest.raises(ExcelQueryError) as e:
        _plan([_group_step(top_n=10)])
    assert e.value.code == ms.ERR_STEP_INVALID


def test_three_steps_rejected():
    with pytest.raises(ExcelQueryError) as e:
        _plan([_group_step(top_n=10), _agg_step(), _agg_step(operation='avg')])
    assert e.value.code == ms.ERR_STEPS_UNSUPPORTED


def test_four_steps_rejected():
    with pytest.raises(ExcelQueryError) as e:
        _plan([_group_step(top_n=10), _agg_step(), _agg_step(), _agg_step()])
    assert e.value.code == ms.ERR_STEPS_UNSUPPORTED
    assert e.value.details['max_steps'] == 2


@pytest.mark.parametrize('bad_step,expected_code', [
    ({'type': 'select_raw', 'operation': 'sum'}, ms.ERR_STEP_TYPE_INVALID),
    ({'type': 'aggregate', 'operation': 'sum', 'source': 'raw_table',
      'column': 'aggregate_value'}, ms.ERR_STEP_SOURCE_INVALID),
    ({'type': 'aggregate', 'operation': 'sum', 'source': 'step_2',
      'column': 'aggregate_value'}, ms.ERR_STEP_SOURCE_INVALID),
    ({'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
      'column': 'Order Amount'}, ms.ERR_STEP_COLUMN_INVALID),
    ({'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
      'column': 'aggregate_value; DROP TABLE t'}, ms.ERR_STEP_COLUMN_INVALID),
    ({'type': 'aggregate', 'operation': 'sum; DROP TABLE t', 'source': 'step_1',
      'column': 'aggregate_value'}, ms.ERR_STEP_INVALID),
    ({'type': 'aggregate', 'operation': 'median', 'source': 'step_1',
      'column': 'aggregate_value'}, ms.ERR_STEP_INVALID),
    ({'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
      'column': 'aggregate_value', 'group_by': [SKU]}, ms.ERR_STEP_INVALID),
    ({'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
      'column': 'aggregate_value', 'top_n': 5}, ms.ERR_STEP_INVALID),
    ({'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
      'column': 'aggregate_value', 'filters': [{'column': 'x', 'operator': 'eq', 'value': 1}]},
     ms.ERR_STEP_INVALID),
])
def test_step2_injection_rejected(bad_step, expected_code):
    with pytest.raises(ExcelQueryError) as e:
        _plan([_group_step(top_n=10), bad_step])
    assert e.value.code == expected_code


@pytest.mark.parametrize('bad_top_n', [999999999, 0, -1, 201, 'DROP TABLE', 'abc', True])
def test_step1_top_n_out_of_range_rejected(bad_top_n):
    with pytest.raises(ExcelQueryError) as e:
        _plan([_group_step(top_n=bad_top_n), _agg_step()])
    assert e.value.code == ms.ERR_STEP_INVALID


def test_step1_bad_order_dir_rejected(small_rep):
    """非法排序方向必须拒绝（不能静默当成默认方向）。"""
    with pytest.raises(ExcelQueryError):
        _run_plan(small_rep, [_group_step(order_dir='desc; DROP TABLE t', top_n=3), _agg_step()])


def test_first_step_must_be_group_step():
    with pytest.raises(ExcelQueryError) as e:
        _plan([_agg_step(), _agg_step()])
    assert e.value.code == ms.ERR_STEP_TYPE_INVALID


def test_second_step_must_be_aggregate_step():
    with pytest.raises(ExcelQueryError) as e:
        _plan([_group_step(top_n=10), _group_step(top_n=10)])
    assert e.value.code == ms.ERR_STEP_TYPE_INVALID


# ==========================================================================
# 3. 核心问题：TOP-10 SKU 的总销售额
# ==========================================================================
def test_core_top10_sku_total_sales(small_rep):
    result, engine = _run_plan(small_rep, [_group_step(top_n=10), _agg_step()])
    gt = gt_two_step(small_rep, operation1='sum', column=AMOUNT, group_by=[SKU], top_n=10,
                     operation2='sum')

    assert engine == 'duckdb'
    # Step 1：逐位与独立 GT 一致
    got_keys = [tuple(r.key()) for r in result.step1.rows]
    exp_keys = [k for k, _ in gt['ordered']]
    assert got_keys == exp_keys
    assert [r.value for r in result.step1.rows] == [pytest.approx(v, **TOL) for _, v in gt['ordered']]
    assert result.step1.total_groups == gt['total_groups'] == 15
    assert result.step1.returned_groups == 10
    # Step 2
    assert result.step2_matched == gt['input_rows'] == 10
    assert result.step2_value == pytest.approx(gt['value2'], **TOL)
    assert result.step2_value == pytest.approx(GT_TOP10_SKU_TOTAL, **TOL)
    # 必须与「全表求和」区分（证明第 2 步用的是 TOP-N 结果，不是整表）
    assert result.step2_value != pytest.approx(GT_ALL_SKU_TOTAL, **TOL)
    # 等于 Step 1 返回值的直接相加（可追溯）
    assert result.step2_value == pytest.approx(sum(r.value for r in result.step1.rows), **TOL)


def test_step2_input_is_exactly_step1_rows(small_rep):
    """Step 2 的输入必须**恰好**是 Step 1 返回的行（顺序与集合都一致）。"""
    result, _ = _run_plan(small_rep, [_group_step(top_n=7), _agg_step()])
    assert result.intermediate_values == [r.value for r in result.step1.rows]
    assert len(result.intermediate_values) == 7
    assert result.step2_value == pytest.approx(sum(result.intermediate_values), **TOL)


def test_step2_never_rescans_raw_data(small_rep):
    """对照实验：TOP-10 求和 ≠ 整表求和 ≠ 任意其它截断值。"""
    r10, _ = _run_plan(small_rep, [_group_step(top_n=10), _agg_step()])
    r15, _ = _run_plan(small_rep, [_group_step(top_n=15), _agg_step()])
    assert r15.step2_value == pytest.approx(GT_ALL_SKU_TOTAL, **TOL)   # 15 组 = 全部
    assert r10.step2_value != pytest.approx(r15.step2_value, **TOL)


# ==========================================================================
# 4. Step 1 五种 operation
# ==========================================================================
@pytest.mark.parametrize('operation,column', [
    ('count', None), ('sum', AMOUNT), ('avg', AMOUNT), ('min', AMOUNT), ('max', AMOUNT),
])
def test_step1_all_operations(small_rep, operation, column):
    steps = [_group_step(operation=operation, column=column, top_n=5), _agg_step(operation='sum')]
    result, engine = _run_plan(small_rep, steps)
    gt = gt_two_step(small_rep, operation1=operation, column=column, group_by=[SKU],
                     top_n=5, operation2='sum')
    assert engine == 'duckdb'
    assert [tuple(r.key()) for r in result.step1.rows] == [k for k, _ in gt['ordered']]
    assert [r.value for r in result.step1.rows] == [pytest.approx(v, **TOL) for _, v in gt['ordered']]
    assert result.step2_value == pytest.approx(gt['value2'], **TOL)


def test_step1_count_over_carrier(small_rep):
    """「订单数量最多的5个物流商」的 Step 1。"""
    steps = [_group_step(group_by=[CARRIER], operation='count', column=None, top_n=5),
             _agg_step(operation='sum')]
    result, _ = _run_plan(small_rep, steps)
    gt = gt_two_step(small_rep, operation1='count', column=None, group_by=[CARRIER],
                     top_n=5, operation2='sum')
    assert [(tuple(r.key()), r.value) for r in result.step1.rows] == \
           [(k, pytest.approx(v, **TOL)) for k, v in gt['ordered']]
    assert result.step2_value == pytest.approx(gt['value2'], **TOL)
    # 只有 3 个物流商，TOP-5 即全部 -> 19
    assert result.step2_value == pytest.approx(19.0, **TOL)
    assert result.step1.returned_groups == 3 and result.step1.total_groups == 3


# ==========================================================================
# 5. Step 2 五种 operation
# ==========================================================================
@pytest.mark.parametrize('operation2', ['sum', 'avg', 'count', 'min', 'max'])
def test_step2_all_operations(small_rep, operation2):
    steps = [_group_step(top_n=10), _agg_step(operation=operation2)]
    result, _ = _run_plan(small_rep, steps)
    gt = gt_two_step(small_rep, operation1='sum', column=AMOUNT, group_by=[SKU],
                     top_n=10, operation2=operation2)
    assert result.step2_operation == operation2
    assert result.step2_matched == 10
    if operation2 == 'count':
        assert result.step2_value == pytest.approx(10.0, **TOL)   # 中间结果行数
    else:
        assert result.step2_value == pytest.approx(gt['value2'], **TOL)
    if operation2 == 'avg':
        assert result.step2_value == pytest.approx(GT_TOP10_SKU_TOTAL / 10, **TOL)


def test_step2_avg_of_top3(small_rep):
    """「订单金额最高的3个物流商，并计算这3个物流商的平均销售额」。"""
    steps = [_group_step(group_by=[CARRIER], top_n=3), _agg_step(operation='avg')]
    result, _ = _run_plan(small_rep, steps)
    gt = gt_two_step(small_rep, operation1='sum', column=AMOUNT, group_by=[CARRIER],
                     top_n=3, operation2='avg')
    assert result.step2_value == pytest.approx(gt['value2'], **TOL)
    assert result.step2_matched == 3


# ==========================================================================
# 6. 确定性 tie-breaker 与 TOP-N 边界
# ==========================================================================
def _run_repeated(rep, steps, times=5):
    outs = []
    for _ in range(times):
        r, _ = _run_plan(rep, steps)
        outs.append([(tuple(x.key()), x.value) for x in r.step1.rows])
    return outs


def test_tie_breaker_stable_across_runs(small_rep):
    """聚合值相同的分组：多次执行顺序必须完全一致（Phase 3C 确定性 tie-breaker 保持）。"""
    outs = _run_repeated(small_rep, [_group_step(top_n=15), _agg_step()])
    assert all(o == outs[0] for o in outs), '多次执行顺序不稳定'
    # 确认确实存在并列（29.66 出现两次）
    values = [v for _, v in outs[0]]
    dup = [v for v in set(values) if values.count(v) > 1]
    assert dup, '真实数据里应存在并列聚合值（本用例要求）'
    # 并列值的分组键必须升序
    for v in dup:
        keys = [k for k, val in outs[0] if val == v]
        assert keys == sorted(keys, key=str), f'并列值 {v} 的 tie-breaker 不是分组键升序'


@pytest.mark.parametrize('top_n,expect_rows,expect_same_as_all', [
    (1, 1, False), (5, 5, False), (10, 10, False),
    (15, 15, True), (20, 15, True), (200, 15, True),
])
def test_top_n_boundaries(small_rep, top_n, expect_rows, expect_same_as_all):
    steps = [_group_step(top_n=top_n), _agg_step()]
    result, _ = _run_plan(small_rep, steps)
    assert result.step1.returned_groups == expect_rows
    assert result.step1.total_groups == 15
    if expect_same_as_all:
        assert result.step2_value == pytest.approx(GT_ALL_SKU_TOTAL, **TOL)
    else:
        assert result.step2_value != pytest.approx(GT_ALL_SKU_TOTAL, **TOL)
    assert result.step2_matched == expect_rows


def test_single_group_result(small_rep):
    """TOP-1：第 2 步的输入只有 1 行。"""
    result, _ = _run_plan(small_rep, [_group_step(top_n=1), _agg_step()])
    assert result.step1.returned_groups == 1
    assert result.step2_matched == 1
    assert result.step2_value == pytest.approx(36.73, **TOL)
    assert result.intermediate_values == [pytest.approx(36.73, **TOL)]


def test_zero_match_result(small_rep):
    """筛选命中 0 行：第 1 步 0 组，第 2 步不得返回编造数字。"""
    steps = [_group_step(top_n=10, filters=[{'column': CARRIER, 'operator': 'contains',
                                             'value': '不存在的物流商XYZ'}]), _agg_step()]
    result, _ = _run_plan(small_rep, steps)
    assert result.step1.rows == []
    assert result.step2_matched == 0
    assert result.step2_value is None
    # None 的展示文本沿用 Phase 3A 口径（不编造 0）
    assert result.step2_value_display == '无有效数值'
    assert result.step1.matched_rows == 0


def test_zero_match_count_step2_returns_zero(small_rep):
    """0 行时 COUNT 语义是 0（明确，不是 None）——并且不读值列。"""
    steps = [_group_step(top_n=10, filters=[{'column': CARRIER, 'operator': 'contains',
                                             'value': '不存在的物流商XYZ'}]),
             _agg_step(operation='count', column=None)]
    result, _ = _run_plan(small_rep, steps)
    assert result.step2_value == pytest.approx(0.0, **TOL)


def test_group_with_null_and_non_numeric_values(small_rep):
    """中间结果里出现 None（该组无可数值化单元格）时，第 2 步按口径忽略，不当作 0。"""
    steps = [_group_step(group_by=['Order Status'], operation='sum', column=AMOUNT, top_n=200),
             _agg_step(operation='sum')]
    result, _ = _run_plan(small_rep, steps)
    gt = gt_two_step(small_rep, operation1='sum', column=AMOUNT, group_by=['Order Status'],
                     top_n=200, operation2='sum')
    assert result.step2_value == pytest.approx(gt['value2'], **TOL)
    assert result.step2_matched == result.step1.returned_groups


# ==========================================================================
# 7. 大表（447 行）——同一套语义
# ==========================================================================
def test_big_table_core_problem(big_rep):
    steps = [_group_step(top_n=10), _agg_step(), ]
    steps[0]['group_by'] = [_first_present(big_rep, [SKU, 'SKU ID', 'Seller SKU'])]
    col = _first_present(big_rep, [AMOUNT, 'Order Amount'])
    steps[0]['column'] = col
    result, engine = _run_plan(big_rep, steps)
    gt = gt_two_step(big_rep, operation1='sum', column=col,
                     group_by=steps[0]['group_by'], top_n=10, operation2='sum')
    assert engine == 'duckdb'
    assert [tuple(r.key()) for r in result.step1.rows] == [k for k, _ in gt['ordered']]
    assert result.step2_value == pytest.approx(gt['value2'], **TOL)
    assert result.step1.total_groups == gt['total_groups']
    assert result.step2_matched == 10


def test_big_table_carrier_top5_count(big_rep):
    carrier = _first_present(big_rep, [CARRIER, 'Shipping Provider Name'])
    steps = [_group_step(group_by=[carrier], operation='count', column=None, top_n=5),
             _agg_step(operation='sum')]
    result, _ = _run_plan(big_rep, steps)
    gt = gt_two_step(big_rep, operation1='count', column=None, group_by=[carrier],
                     top_n=5, operation2='sum')
    assert result.step2_value == pytest.approx(gt['value2'], **TOL)
    assert result.step1.total_groups == gt['total_groups']


def _first_present(rep, candidates: Sequence[str]) -> str:
    names = rep.sheets[0].column_names
    for c in candidates:
        if c in names:
            return c
    raise AssertionError(f'大表中找不到任何候选列：{candidates}')


# ==========================================================================
# 8. SQL 安全
# ==========================================================================
def test_step2_sql_only_select_and_bound_params():
    sql = ms.build_step2_sql('sum', 3)
    head = sql['sql'].lstrip().lower()
    assert head.startswith('select')
    assert ';' not in sql['sql']
    assert 'drop' not in sql['sql'].lower()
    assert sql['sql'].count('?') == 3
    assert sql['params'] == [None, None, None]
    # 标识符只可能是 Python 常量
    assert 'AS t(v)' in sql['sql']


def test_step2_sql_rejects_bad_operation():
    with pytest.raises(ExcelQueryError):
        ms.build_step2_sql('sum; DROP TABLE t', 1)


def test_step2_sql_rejects_too_many_rows():
    with pytest.raises(ExcelQueryError) as e:
        ms.build_step2_sql('sum', ms.MAX_INTERMEDIATE_ROWS + 1)
    assert e.value.code == ms.ERR_INTERMEDIATE_TOO_LARGE


def test_step2_python_matches_duckdb(small_rep):
    """Python 参考实现与 DuckDB 结果一致（降级路径可信）。"""
    values = [1.0, 2.0, 3.0, None, 4.0]
    for op in ('sum', 'avg', 'min', 'max'):
        sql = ms.build_step2_sql(op, 4)          # None 不进入 SQL（与 DuckDB 聚合忽略 NULL 等价）
        numeric = [v for v in values if v is not None]
        duck_val = _duck_scalar(small_rep, sql, numeric, op)
        py_val = ms.execute_step2_python(op, values)['value']
        assert duck_val == pytest.approx(py_val, **TOL)
    assert ms.execute_step2_python('count', values)['value'] == 5.0


def _duck_scalar(rep, sql, numeric_values, operation):
    return ms._run_step2_duckdb(rep, rep.sheets[0].sheet_index, sql, numeric_values, operation)


# ==========================================================================
# 9. NL 路由：analysis 动作 / 兜底 / 上下文 / 澄清
# ==========================================================================
class FakeLLM:
    def __init__(self, turn=None, analysis=None):
        self.turn, self.analysis = turn, analysis
        self.calls: List[str] = []

    async def chat_completion(self, messages, stream=False, temperature=None):
        system = messages[0]['content']
        if system.startswith('你是一个「两阶段分析计划生成器」'):
            kind = 'analysis_guard'
        elif system.startswith('你是一个「统计参数抽取器」'):
            kind = 'stat_guard'
        else:
            kind = 'turn'
        self.calls.append(kind)
        payload = {'turn': self.turn, 'analysis_guard': self.analysis, 'stat_guard': None}[kind]
        if payload is None:
            raise RuntimeError(f'no payload for {kind}')
        assert temperature == 0.0
        yield json.dumps(payload, ensure_ascii=False)


def _nl_run(rep, message, llm, **kw):
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nl.run_nl_query(message, _catalog(rep), llm=llm, **kw))
    finally:
        excel_store.load_representation = original


ANALYSIS_TURN = {
    'action': 'analysis',
    'document': '直邮一店 8.20号订单.xlsx',
    'steps': [
        {'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'sum', 'column': AMOUNT,
         'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 10},
        {'type': 'aggregate', 'operation': 'sum', 'source': 'step_1', 'column': 'aggregate_value'},
    ],
}


def test_nl_analysis_action_direct(small_rep):
    out = _nl_run(small_rep, '找出一店中金额最高的10个SKU，并统计它们的总销售额。',
                  FakeLLM(turn=ANALYSIS_TURN))
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_analysis'
    assert out['engine'] == 'duckdb'
    m = out['multi_step']
    assert m['step1']['returned_groups'] == 10
    assert m['step2']['value'] == pytest.approx(GT_TOP10_SKU_TOTAL, **TOL)
    assert m['value_display'] == '267.61'
    assert m['step1']['source'] and 'representation' in m['step1']['source']
    assert 'Step 1' in m['step2']['source_text']


@pytest.mark.parametrize('llm_turn,expected_source', [
    # 后续 2A：LLM 给出「分组 + TOP-N」的单步统计 -> Python 直接补第 2 步（**零额外 LLM 调用**）
    ({'action': 'aggregate', 'aggregate_operation': 'sum', 'column': AMOUNT, 'group_by': [SKU],
      'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 10}, 'aggregate_upgrade'),
    # LLM 判成普通查询（没有 group_by）-> 只能走 temperature=0 的两阶段计划抽取器
    ({'action': 'new_query', 'document': '一店', 'columns': []}, 'analysis_guard'),
])
def test_nl_guard_rescues_to_analysis(small_rep, llm_turn, expected_source):
    """LLM 误判成 aggregate/new_query 时，确定性兜底必须救回两步分析。"""
    llm = FakeLLM(turn=llm_turn, analysis={'document': '一店', 'steps': ANALYSIS_TURN['steps']})
    out = _nl_run(small_rep, '找出一店中金额最高的10个SKU，并统计它们的总销售额。', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_analysis'
    assert out['turn']['source'] == expected_source
    assert out['multi_step']['value'] == pytest.approx(GT_TOP10_SKU_TOTAL, **TOL)
    if expected_source == 'aggregate_upgrade':
        assert out['analysis_upgraded'] is True
        assert llm.calls == ['turn']          # 确定性升级不需要第二次 LLM 调用
    else:
        assert out['analysis_guard'] is True


def test_nl_broken_analysis_plan_repaired_by_guard(small_rep):
    """LLM 判成 analysis 但第 1 步缺少聚合目标（既无 column 也无 calculation）-> 专用抽取器修复。"""
    broken = {
        'action': 'analysis',
        'document': '直邮一店 8.20号订单.xlsx',
        'steps': [
            {'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'sum', 'column': None,
             'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 10},
            {'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
             'column': 'aggregate_value'},
        ],
    }
    llm = FakeLLM(turn=broken, analysis={'document': '一店', 'steps': ANALYSIS_TURN['steps']})
    out = _nl_run(small_rep, '找出一店中金额最高的10个SKU，并统计它们的总销售额。', llm)
    assert llm.calls == ['turn', 'analysis_guard']
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_analysis'
    assert out['analysis_guard'] is True
    assert out['multi_step']['value'] == pytest.approx(GT_TOP10_SKU_TOTAL, **TOL)


def test_analysis_plan_missing_target_detection():
    """缺少聚合目标的判定：目标缺失算缺；COUNT 不需要列；计算字段算有目标。"""
    def plan(step):
        return nl.analysis_intent_from_dict({'steps': [step, {
            'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
            'column': 'aggregate_value'}]})

    assert nl._analysis_plan_missing_target(plan(
        {'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'sum', 'column': None,
         'top_n': 3})) is True
    assert nl._analysis_plan_missing_target(plan(
        {'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'count', 'column': None,
         'top_n': 3})) is False
    assert nl._analysis_plan_missing_target(plan(
        {'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'sum', 'column': AMOUNT,
         'top_n': 3})) is False
    assert nl._analysis_plan_missing_target(plan(
        {'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'avg', 'column': None,
         'calculation': {'operation': 'div', 'left_column': AMOUNT, 'right_column': QTY},
         'top_n': 3})) is False
    assert nl._analysis_plan_missing_target(plan(
        {'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
         'column': 'aggregate_value'})) is False


def test_nl_guard_not_fired_for_single_step_ranking(small_rep):
    """纯排行（无第二步汇总）不得被兜底劫持为两步分析。"""
    llm = FakeLLM(turn={'action': 'aggregate', 'aggregate_operation': 'sum', 'column': AMOUNT,
                        'group_by': [SKU], 'order_by': 'aggregate_value', 'order_dir': 'desc',
                        'top_n': 10})
    out = _nl_run(small_rep, '金额最高的10个SKU', llm)
    assert out['status'] == 'ok'
    assert llm.calls == ['turn']
    assert nl.describe_executor(out) == 'run_group_aggregate'
    assert out['group_aggregate']['top_n'] == 10


def test_nl_three_steps_clarify(small_rep):
    turn = {'action': 'analysis', 'steps': ANALYSIS_TURN['steps'] + [
        {'type': 'aggregate', 'operation': 'avg', 'source': 'step_1', 'column': 'aggregate_value'}]}
    out = _nl_run(small_rep, '先汇总再取前三然后再平均一次', FakeLLM(turn=turn))
    assert out['status'] == 'clarify'
    assert '2' in out['message']


def test_nl_unsupported_raw_rejoin_clarifies(small_rep):
    """「回到原始订单」的语义必须 clarify（本阶段不支持）。"""
    turn = {'action': 'analysis', 'steps': [
        {'type': 'aggregate', 'operation': 'avg', 'source': 'step_1', 'column': AMOUNT}]}
    out = _nl_run(small_rep, '金额最高的10个SKU对应的所有订单的平均订单金额是多少？',
                  FakeLLM(turn=turn))
    assert out['status'] == 'clarify'
    assert '原始订单' in out['message']


def test_nl_context_inheritance_changes_only_step2(small_rep):
    """「再算一下这10个SKU的平均销售额」：第 1 步沿用，只换第 2 步。"""
    first = _nl_run(small_rep, '找出一店中金额最高的10个SKU，并统计它们的总销售额。',
                    FakeLLM(turn=ANALYSIS_TURN))
    ctx = AnalysisContext(**first['new_analysis_context'])

    follow_turn = {'action': 'analysis', 'refers_to_previous': True, 'steps': [
        {'type': 'aggregate', 'operation': 'avg', 'source': 'step_1', 'column': 'aggregate_value'}]}
    out = _nl_run(small_rep, '再算一下这10个SKU的平均销售额。',
                  FakeLLM(turn=follow_turn), analysis_context=ctx)

    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_analysis'
    assert out['inherited_from'] == 'analysis'
    m = out['multi_step']
    assert m['step2']['operation'] == 'avg'
    assert m['step2']['input_rows'] == 10
    assert m['value'] == pytest.approx(GT_TOP10_SKU_TOTAL / 10, **TOL)
    # 第 1 步的 TOP-10 集合与上一轮完全一致
    assert [r['group_key'] for r in m['step1']['rows']] == \
           [r['group_key'] for r in first['multi_step']['step1']['rows']]


def test_nl_followup_rescued_when_llm_clarifies(small_rep):
    """追问时 LLM 判成 clarify：有 AnalysisContext + 指代词 → 兜底只替换第 2 步。"""
    first = _nl_run(small_rep, '找出一店中金额最高的10个SKU，并统计它们的总销售额。',
                    FakeLLM(turn=ANALYSIS_TURN))
    ctx = AnalysisContext(**first['new_analysis_context'])

    llm = FakeLLM(turn={'action': 'clarify', 'clarification': '不确定指的是哪批数据'},
                  analysis={'steps': [{'type': 'aggregate', 'operation': 'avg',
                                       'source': 'step_1', 'column': 'aggregate_value'}]})
    out = _nl_run(small_rep, '再算一下这10个SKU的平均销售额。', llm, analysis_context=ctx)

    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_analysis'
    assert out['inherited_from'] == 'analysis'
    assert out['analysis_guard'] is True
    assert out['multi_step']['step2']['operation'] == 'avg'
    assert out['multi_step']['value'] == pytest.approx(GT_TOP10_SKU_TOTAL / 10, **TOL)


def test_nl_followup_without_context_stays_clarify(small_rep):
    """没有上一轮两步分析时，「这10个」无法解析 -> 必须 clarify，不猜。"""
    llm = FakeLLM(turn={'action': 'clarify', 'clarification': '需要更明确的信息'},
                  analysis={'steps': [{'type': 'aggregate', 'operation': 'avg',
                                       'source': 'step_1', 'column': 'aggregate_value'}]})
    out = _nl_run(small_rep, '再算一下这10个SKU的平均销售额。', llm)
    assert out['status'] == 'clarify'


def test_nl_followup_marker_matrix():
    assert nl.looks_like_analysis_followup('再算一下这10个SKU的平均销售额。') is True
    assert nl.looks_like_analysis_followup('这些的合计是多少') is True
    assert nl.looks_like_analysis_followup('上述10个分组的平均值') is True
    assert nl.looks_like_analysis_followup('列出这些明细') is False
    assert nl.looks_like_analysis_followup('每个物流商分别有多少单？') is False


def test_nl_multi_step_marker_matrix():
    positive = [
        '找出一店中金额最高的10个SKU，并统计它们的总销售额。',
        '找出订单数量最多的5个物流商，并计算这5个物流商的总订单数。',
        '找出订单金额最高的3个物流商，并计算这3个物流商的平均销售额。',
        '选出金额最高的5个SKU，然后求它们的合计',
    ]
    negative = [
        '订单金额最高的5个物流商',
        '每个物流商分别有多少单？',
        '金额最高的10个SKU',
        '物流商为SF的有几单？',
        '列出5店前20条SKU',
        '列出前20条SKU，并统计它们的总数量',
        '订单金额总和是多少？',
    ]
    for t in positive:
        assert nl.looks_like_multi_step_query(t) is True, t
    for t in negative:
        assert nl.looks_like_multi_step_query(t) is False, t


def test_nl_summary_contains_both_steps(small_rep):
    out = _nl_run(small_rep, '找出一店中金额最高的10个SKU，并统计它们的总销售额。',
                  FakeLLM(turn=ANALYSIS_TURN))
    msg = out['message']
    assert '第 1 步' in msg and '第 2 步' in msg
    assert '267.61' in msg
    assert 'Step 1 的 TOP-N 结果' in msg
