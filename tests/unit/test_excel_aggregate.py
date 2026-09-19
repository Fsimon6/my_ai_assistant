# -*- coding: utf-8 -*-
"""Phase 3A：Excel 基础统计（COUNT / SUM / AVG / MIN / MAX）测试矩阵

三方交叉验证（与 Phase 2 同款策略）：
  A. DuckDB 引擎        backend/excel/aggregate.py::execute_aggregate_duckdb
  B. Python 参考实现     backend/excel/aggregate.py::execute_aggregate_python
  C. 本文件内独立扫描    _scan_ground_truth()（手写语义，不 import 被测实现）

任何用例都必须满足 A == B == C（统计值 / 匹配行数 / 数值行数 / Excel 行号）。
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.excel import aggregate as agg
from backend.excel import duck as duck_engine
from backend.excel import engine as query_engine
from backend.excel import nl_query as nl
from backend.excel import query as q
from backend.excel import representation as repr_mod
from backend.excel import store as excel_store
from backend.excel.parser import ExcelParser
from backend.excel.query_context import AggregateContext, AggregateContextStore, ExcelQueryContext

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMALL_REAL = PROJECT_ROOT / '直邮一店 8.20号订单.xlsx'
BIG_CANDIDATES = [
    PROJECT_ROOT / '直邮5店 7.10号订单.xlsx',
]
OPERATIONS = ['count', 'sum', 'avg', 'min', 'max']
TOL = dict(rel=1e-9, abs=1e-6)


# ==========================================================================
# 独立 Ground Truth（手写语义）
# ==========================================================================
def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip()) if v.strip() else None
        except ValueError:
            return None
    return None


def _match(cell: Any, op: str, value: Any) -> bool:
    if op == 'eq':
        if cell is None and value is None:
            return True
        if cell is None or value is None:
            return False
        if str(cell) == str(value):
            return True
        a, b = _num(cell), _num(value)
        return a is not None and b is not None and abs(a - b) <= 1e-9
    if op == 'neq':
        return cell is not None and not _match(cell, 'eq', value)
    if op == 'contains':
        return cell is not None and value is not None and str(value).lower() in str(cell).lower()
    a, b = _num(cell), _num(value)
    if a is None or b is None:
        return False
    return {'gt': a > b, 'gte': a >= b, 'lt': a < b, 'lte': a <= b}[op]


def _index_of(names: List[str], name: str) -> int:
    if name in names:
        return names.index(name)
    cand = [i for i, n in enumerate(names) if n.lower() == name.lower()]
    assert len(cand) == 1, f'GT 无法唯一解析列名 {name!r}'
    return cand[0]


def _scan_ground_truth(rep, payload: Dict[str, Any]) -> Dict[str, Any]:
    """返回 {value, matched_rows, numeric_rows, empty_rows, non_numeric_rows, excel_rows}。"""
    sheet = rep.sheets[payload.get('sheet_index') or 0]
    names = sheet.column_names
    filters = payload.get('filters') or []

    matched: List[Tuple[List[Any], int]] = []
    for i, row in enumerate(sheet.rows):
        ok = True
        for f in filters:
            ci = _index_of(names, f['column'])
            if not _match(row[ci] if ci < len(row) else None, f['operator'], f.get('value')):
                ok = False
                break
        if ok:
            matched.append((row, sheet.row_excel_numbers[i]))

    matched_rows = len(matched)
    op = payload['operation']
    excel_rows = [r for _, r in matched]

    if op == 'count':
        return {'value': float(matched_rows), 'matched_rows': matched_rows,
                'numeric_rows': matched_rows, 'empty_rows': 0, 'non_numeric_rows': 0,
                'excel_rows': excel_rows}

    ci = _index_of(names, payload['column'])
    numbers: List[float] = []
    empty = non_numeric = 0
    for row, _ in matched:
        raw = row[ci] if ci < len(row) else None
        if raw is None:
            empty += 1
            continue
        n = _num(raw)
        if n is None:
            non_numeric += 1
        else:
            numbers.append(n)
    if not numbers:
        value = None
    elif op == 'sum':
        value = float(sum(numbers))
    elif op == 'min':
        value = float(min(numbers))
    elif op == 'max':
        value = float(max(numbers))
    else:
        value = float(sum(numbers) / len(numbers))
    return {'value': value, 'matched_rows': matched_rows, 'numeric_rows': len(numbers),
            'empty_rows': empty, 'non_numeric_rows': non_numeric, 'excel_rows': excel_rows}


# ==========================================================================
# 夹具与工具
# ==========================================================================
def _big_path() -> Optional[Path]:
    for p in BIG_CANDIDATES:
        if p.exists():
            return p
    return None


@pytest.fixture(scope='module')
def big_rep():
    p = _big_path()
    if p is None:
        pytest.skip('未找到大表（直邮5店 7.10号订单.xlsx）')
    return ExcelParser().parse(str(p), 'agg-big', user_id=1, filename=p.name)


@pytest.fixture(scope='module')
def small_rep():
    if not SMALL_REAL.exists():
        pytest.skip('未找到小表（直邮一店 8.20号订单.xlsx）')
    return ExcelParser().parse(str(SMALL_REAL), 'agg-small', user_id=1, filename=SMALL_REAL.name)


@pytest.fixture(autouse=True)
def _clean_registry():
    duck_engine.reset_registry()
    yield
    duck_engine.reset_registry()


def _assert_value(result, expected: Optional[float], label: str):
    if expected is None:
        assert result.value is None, f'{label}: 期望 None，实际 {result.value}'
    else:
        assert result.value is not None, f'{label}: 期望 {expected}，实际 None'
        assert result.value == pytest.approx(expected, **TOL), \
            f'{label}: {result.value} != {expected}'


def _three_way(rep, payload: Dict[str, Any], label: str = '') -> Any:
    """A(DuckDB) == B(Python) == C(独立扫描)。返回 DuckDB 结果。"""
    exp = _scan_ground_truth(rep, payload)

    res_py, eng_py = query_engine.run_aggregate(rep, payload, engine=query_engine.ENGINE_PYTHON)
    assert eng_py == 'python'
    _assert_value(res_py, exp['value'], f'{label}/python')
    assert res_py.matched_rows == exp['matched_rows'], f'{label}: python matched_rows 不一致'
    assert res_py.row_excel_numbers == exp['excel_rows'], f'{label}: python Excel 行号不一致'

    res_dk, eng_dk = query_engine.run_aggregate(rep, payload, engine=query_engine.ENGINE_DUCKDB)
    assert eng_dk == 'duckdb'
    _assert_value(res_dk, exp['value'], f'{label}/duckdb')
    assert res_dk.matched_rows == exp['matched_rows'], \
        f'{label}: DuckDB matched_rows {res_dk.matched_rows} != GT {exp["matched_rows"]}'
    assert res_dk.numeric_rows == exp['numeric_rows'], f'{label}: numeric_rows 不一致'
    assert res_dk.empty_rows == exp['empty_rows'], f'{label}: empty_rows 不一致'
    assert res_dk.non_numeric_rows == exp['non_numeric_rows'], f'{label}: non_numeric_rows 不一致'
    assert res_dk.row_excel_numbers == exp['excel_rows'], f'{label}: Excel 行号不一致'

    # 口径不变式：匹配行 = 可数值化 + 空值 + 非数值
    assert res_dk.matched_rows == res_dk.numeric_rows + res_dk.empty_rows + res_dk.non_numeric_rows, \
        f'{label}: 统计口径不变式被破坏'
    if res_dk.row_excel_span:
        assert res_dk.row_excel_span['first'] == exp['excel_rows'][0]
        assert res_dk.row_excel_span['last'] == exp['excel_rows'][-1]
    return res_dk


# ==========================================================================
# 1. 引擎与操作白名单
# ==========================================================================
def test_operations_whitelist():
    assert agg.SUPPORTED_OPERATIONS == ('count', 'sum', 'avg', 'min', 'max')
    assert agg.NUMERIC_OPERATIONS == ('sum', 'avg', 'min', 'max')
    assert agg.normalize_operation('COUNT') == 'count'
    assert agg.normalize_operation(' Sum ') == 'sum'
    for bad in ['group_by', 'distinct', 'median', 'order_by', '']:
        with pytest.raises(q.ExcelQueryError) as ei:
            agg.normalize_operation(bad)
        assert ei.value.code == agg.ERR_OPERATION_INVALID


def test_numeric_operations_require_column(big_rep):
    for op in agg.NUMERIC_OPERATIONS:
        with pytest.raises(q.ExcelQueryError) as ei:
            query_engine.run_aggregate(big_rep, {'operation': op})
        assert ei.value.code == agg.ERR_COLUMN_REQUIRED


def test_count_ignores_column_no_distinct(big_rep):
    """COUNT 语义固定为行数，不实现 COUNT(DISTINCT)。"""
    plain, _ = query_engine.run_aggregate(big_rep, {'operation': 'count'}, engine='duckdb')
    with_col, _ = query_engine.run_aggregate(
        big_rep, {'operation': 'count', 'column': 'Order ID'}, engine='duckdb')
    assert plain.value == with_col.value == 447
    assert with_col.column is None


def test_engine_auto_prefers_duckdb(small_rep):
    _res, used = query_engine.run_aggregate(small_rep, {'operation': 'count'})
    assert used == 'duckdb'


# ==========================================================================
# 2. 五个操作 × 无/有条件 × 两张表（含三方对比）
# ==========================================================================
def test_all_operations_big_table(big_rep):
    cases = [
        ('COUNT 无条件', {'operation': 'count'}),
        ('COUNT 物流商含SF', {'operation': 'count',
                          'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]}),
        ('SUM Order Amount', {'operation': 'sum', 'column': 'Order Amount'}),
        ('AVG Order Amount', {'operation': 'avg', 'column': 'Order Amount'}),
        ('MIN Order Amount', {'operation': 'min', 'column': 'Order Amount'}),
        ('MAX Order Amount', {'operation': 'max', 'column': 'Order Amount'}),
        ('SUM Taxes', {'operation': 'sum', 'column': 'Taxes'}),
        ('SUM Quantity', {'operation': 'sum', 'column': 'Quantity'}),
        ('AVG Quantity', {'operation': 'avg', 'column': 'Quantity'}),
        ('SUM 金额>10', {'operation': 'sum', 'column': 'Order Amount',
                       'filters': [{'column': 'Order Amount', 'operator': 'gt', 'value': 10}]}),
        ('MAX 金额>10', {'operation': 'max', 'column': 'Order Amount',
                       'filters': [{'column': 'Order Amount', 'operator': 'gt', 'value': 10}]}),
        ('COUNT 数量>100(0行)', {'operation': 'count',
                              'filters': [{'column': 'Quantity', 'operator': 'gt', 'value': 100}]}),
        ('SUM 数量>100(0行)', {'operation': 'sum', 'column': 'Order Amount',
                            'filters': [{'column': 'Quantity', 'operator': 'gt', 'value': 100}]}),
    ]
    for label, payload in cases:
        _three_way(big_rep, {'sheet_index': 0, **payload}, label)


def test_all_operations_small_table(small_rep):
    cases = [
        ('COUNT 无条件', {'operation': 'count'}),
        ('COUNT SF', {'operation': 'count',
                      'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]}),
        ('COUNT JS', {'operation': 'count',
                      'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'JS'}]}),
        ('SUM Order Amount', {'operation': 'sum', 'column': 'Order Amount'}),
        ('AVG Order Amount', {'operation': 'avg', 'column': 'Order Amount'}),
        ('MIN Order Amount', {'operation': 'min', 'column': 'Order Amount'}),
        ('MAX Order Amount', {'operation': 'max', 'column': 'Order Amount'}),
        ('SUM Order Amount (SF)', {'operation': 'sum', 'column': 'Order Amount',
                                   'filters': [{'column': 'Shipping Provider Name',
                                                'operator': 'contains', 'value': 'SF'}]}),
        ('AVG Order Amount (SF)', {'operation': 'avg', 'column': 'Order Amount',
                                   'filters': [{'column': 'Shipping Provider Name',
                                                'operator': 'contains', 'value': 'SF'}]}),
        ('SUM Quantity', {'operation': 'sum', 'column': 'Quantity'}),
        ('MAX Quantity', {'operation': 'max', 'column': 'Quantity'}),
        ('MIN Quantity', {'operation': 'min', 'column': 'Quantity'}),
        ('SUM 重量', {'operation': 'sum', 'column': 'Weight(kg)'}),
        ('AVG 税费', {'operation': 'avg', 'column': 'Taxes'}),
    ]
    for label, payload in cases:
        _three_way(small_rep, {'sheet_index': 0, **payload}, label)


def test_multi_condition_aggregate(small_rep):
    """多条件 AND 统计（复用 Phase 2 过滤）。"""
    combos = [
        ('双条件', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'},
                  {'column': 'Quantity', 'operator': 'gt', 'value': 1}]),
        ('三条件', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'International'},
                  {'column': 'Quantity', 'operator': 'gte', 'value': 1},
                  {'column': 'Order Amount', 'operator': 'gt', 'value': 0}]),
        ('矛盾条件', [{'column': 'Shipping Provider Name', 'operator': 'eq', 'value': 'SF International'},
                   {'column': 'Shipping Provider Name', 'operator': 'eq', 'value': 'Yanwen Express'}]),
        ('neq 条件', [{'column': 'Shipping Provider Name', 'operator': 'neq', 'value': 'SF International'}]),
        ('lte 条件', [{'column': 'Order Amount', 'operator': 'lte', 'value': 10}]),
        ('lt 条件', [{'column': 'Order Amount', 'operator': 'lt', 'value': 10}]),
        ('gte 条件', [{'column': 'Order Amount', 'operator': 'gte', 'value': 30}]),
        ('eq 空值条件', [{'column': 'Delivery Instruction', 'operator': 'eq', 'value': None}]),
    ]
    for label, filters in combos:
        for op, col in [('count', None), ('sum', 'Order Amount'), ('avg', 'Order Amount'),
                        ('min', 'Quantity'), ('max', 'Quantity')]:
            payload: Dict[str, Any] = {'sheet_index': 0, 'operation': op, 'filters': filters}
            if col:
                payload['column'] = col
            _three_way(small_rep, payload, f'{label}/{op}')


def test_operator_matrix_as_filters(small_rep):
    """统计层必须完整复用 Phase 2 的 7 个 operator。"""
    for op in ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'contains']:
        if op == 'contains':
            value: Any = 'SF'
        elif op in ('eq', 'neq'):
            value = 'SF International'
        else:
            value = 1
        column = 'Shipping Provider Name' if op in ('contains', 'eq', 'neq') else 'Quantity'
        _three_way(small_rep, {
            'sheet_index': 0, 'operation': 'count',
            'filters': [{'column': column, 'operator': op, 'value': value}],
        }, f'operator={op}')


# ==========================================================================
# 3. 数据类型：整数 / 浮点 / 文本数字 / 空值 / 非数值
# ==========================================================================
def test_data_type_handling(big_rep):
    """Quantity 的 representation dtype 是 string，但必须是可用的数值列。"""
    col = [c for c in big_rep.sheets[0].columns if c.name == 'Quantity'][0]
    assert col.dtype == 'string', '真实数据的 Quantity 是文本数字，本用例正是为此设计'
    res = _three_way(big_rep, {'sheet_index': 0, 'operation': 'sum', 'column': 'Quantity'}, '文本数字求和')
    assert res.value == 447.0
    assert res.numeric_rows == 447

    # 全空列：COUNT(*)=447，但 SUM/AVG/MIN/MAX 明确失败（整列不可数值化）
    empty_col = 'Cancelation/Return Type'
    res_count = _three_way(big_rep, {'sheet_index': 0, 'operation': 'count'}, '空列COUNT')
    assert res_count.value == 447
    for op in agg.NUMERIC_OPERATIONS:
        with pytest.raises(q.ExcelQueryError) as ei:
            query_engine.run_aggregate(big_rep, {'operation': op, 'column': empty_col}, engine='duckdb')
        assert ei.value.code == agg.ERR_NOT_NUMERIC_COLUMN, f'{op} 应对空列明确失败'


def test_empty_values_not_treated_as_zero(small_rep):
    """命中行里若存在空单元格：不计入、也不当 0；且不变式成立。"""
    # 'Order Amount' 无空值，这里用 Normal or Pre-order（有空值）+ 数值列组合验证口径
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'sum', 'column': 'Order Amount',
        'filters': [{'column': 'Normal or Pre-order', 'operator': 'eq', 'value': None}],
    }, '存在空值的筛选下求和')
    assert res.matched_rows == res.numeric_rows + res.empty_rows + res.non_numeric_rows


def test_no_valid_numbers_returns_none_not_zero(big_rep):
    """命中行没有可数值化单元格 -> value=None（不编造 0），并给出说明。"""
    res, _ = query_engine.run_aggregate(big_rep, {
        'sheet_index': 0, 'operation': 'sum', 'column': 'Order Amount',
        'filters': [{'column': 'Quantity', 'operator': 'gt', 'value': 100}],
    }, engine='duckdb')
    assert res.matched_rows == 0
    assert res.numeric_rows == 0
    assert res.value is None
    assert res.value_display() == '无有效数值'
    text = agg.format_aggregate_summary(res, 'x.xlsx')
    assert '未按 0 处理' in text


def test_real_zero_sum_is_reported_as_zero(small_rep):
    """真实求和结果恰好是 0 时必须如实返回 0（与"无有效数值"区分）。"""
    res = _three_way(small_rep, {'sheet_index': 0, 'operation': 'sum', 'column': 'Sku Quantity of return'},
                     '真实0求和')
    assert res.value == 0.0
    assert res.value_display() == '0'


def test_non_numeric_column_explicit_failure(small_rep):
    """非数值列做 SUM/AVG/MIN/MAX 必须明确失败，绝不返回错误数字。"""
    for col in ['Product Name', 'Shipping Provider Name', 'Variation', 'Recipient', 'City']:
        for op in agg.NUMERIC_OPERATIONS:
            with pytest.raises(q.ExcelQueryError) as ei:
                query_engine.run_aggregate(small_rep, {'operation': op, 'column': col}, engine='duckdb')
            assert ei.value.code == agg.ERR_NOT_NUMERIC_COLUMN
            details = ei.value.to_dict()['details']
            assert details['column'] == col
            assert '非数值' in ei.value.message or '数值列' in ei.value.message


def test_text_numeric_id_column_is_coercible(small_rep):
    """Order ID 是 18 位数字字符串。

    设计演进（稳定性补丁）：它**不再是**可统计的数值列，而是"业务标识列"。
    旧断言（允许 MAX(Order ID)）已按新语义更新为"必须被明确拒绝"——
    因为 TRY_CAST(... AS DOUBLE) 会让 18 位 ID 丢精度，
    且 SUM/AVG(Order ID) 在业务上没有任何意义。
    """
    with pytest.raises(q.ExcelQueryError) as ei:
        query_engine.run_aggregate(small_rep, {'operation': 'max', 'column': 'Order ID'},
                                   engine='duckdb')
    assert ei.value.code == agg.ERR_IDENTIFIER_NOT_NUMERIC
    assert ei.value.details['semantic_type'] == repr_mod.SEMANTIC_IDENTIFIER
    # 计数仍然允许（IDENTIFIER 只影响数值聚合）
    res, _ = query_engine.run_aggregate(small_rep, {'operation': 'count', 'column': 'Order ID'},
                                        engine='duckdb')
    assert res.value == 19


def test_short_numeric_text_column_still_coercible(small_rep):
    """短数字串列（如 Quantity 的 "1"/"2"）仍按数值列处理，不受标识列规则影响。"""
    res, _ = query_engine.run_aggregate(small_rep, {'operation': 'sum', 'column': 'Quantity'},
                                        engine='duckdb')
    assert res.numeric_rows == 19
    assert res.value == 20.0


# ==========================================================================
# 4. 结果规模：0 / 1 / 少量 / 全部
# ==========================================================================
def test_result_sizes(big_rep):
    # 全部
    res_all = _three_way(big_rep, {'sheet_index': 0, 'operation': 'count'}, '全部')
    assert res_all.matched_rows == 447
    assert len(res_all.row_excel_numbers) == 447
    assert res_all.row_excel_span == {'first': 3, 'last': 449}

    # 1 行（小表 : Quantity=2 只有 1 条）在另一个用例覆盖；这里大表用精确值
    res_one = _three_way(big_rep, {
        'sheet_index': 0, 'operation': 'sum', 'column': 'Order Amount',
        'filters': [{'column': 'Order Amount', 'operator': 'gte', 'value': 27.6}]}, '少量')
    assert res_one.matched_rows == len(res_one.row_excel_numbers)

    # 0 行
    res_zero = _three_way(big_rep, {
        'sheet_index': 0, 'operation': 'count',
        'filters': [{'column': 'Quantity', 'operator': 'gt', 'value': 99999}]}, '0行')
    assert res_zero.matched_rows == 0
    assert res_zero.row_excel_numbers == []
    assert res_zero.row_excel_span is None


def test_single_row_aggregate(small_rep):
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'max', 'column': 'Order Amount',
        'filters': [{'column': 'Quantity', 'operator': 'eq', 'value': 2}]}, '单行')
    assert res.matched_rows == 1
    assert len(res.row_excel_numbers) == 1
    assert res.row_excel_span == {'first': res.row_excel_numbers[0],
                                  'last': res.row_excel_numbers[0]}


# ==========================================================================
# 5. schema 校验
# ==========================================================================
def test_schema_errors(big_rep):
    with pytest.raises(q.ExcelQueryError) as e1:
        query_engine.run_aggregate(big_rep, {'operation': 'sum', 'column': '不存在的列'}, engine='duckdb')
    assert e1.value.code == q.ERR_COLUMN_NOT_FOUND

    # 严格解析器不做模糊匹配：模糊名 -> column_not_found + suggestions（绝不自动猜）
    with pytest.raises(q.ExcelQueryError) as e2:
        query_engine.run_aggregate(big_rep, {'operation': 'sum', 'column': 'Order'}, engine='duckdb')
    assert e2.value.code == q.ERR_COLUMN_NOT_FOUND
    assert 'Order ID' in e2.value.to_dict()['details']['suggestions']

    with pytest.raises(q.ExcelQueryError) as e3:
        query_engine.run_aggregate(big_rep, {
            'operation': 'count',
            'filters': [{'column': '不存在', 'operator': 'eq', 'value': 1}]}, engine='duckdb')
    assert e3.value.code == q.ERR_COLUMN_NOT_FOUND

    with pytest.raises(q.ExcelQueryError) as e4:
        query_engine.run_aggregate(big_rep, {
            'operation': 'count',
            'filters': [{'column': 'Order ID', 'operator': 'like', 'value': '5%'}]}, engine='duckdb')
    assert e4.value.code == q.ERR_INVALID_OPERATOR

    with pytest.raises(q.ExcelQueryError) as e5:
        query_engine.run_aggregate(big_rep, {'operation': 'count', 'sheet_index': 99}, engine='duckdb')
    assert e5.value.code == q.ERR_SHEET_NOT_FOUND

    # 统计不分页：limit / offset 一律忽略（聚合结果恒为单值，不做分页）
    res, _ = query_engine.run_aggregate(
        big_rep, {'operation': 'count', 'limit': 0, 'offset': -5}, engine='duckdb')
    assert res.value == 447.0


def test_ambiguous_column_rejected(tmp_path):
    """仅大小写不同的重名列 -> column_ambiguous（明确失败，不猜）。"""
    path = tmp_path / 'amb.csv'
    path.write_text('Qty,qty,Name\n1,2,a\n3,4,b\n', encoding='utf-8')
    rep = ExcelParser().parse(str(path), 'agg-amb', user_id=1, filename='amb.csv')
    for op in agg.NUMERIC_OPERATIONS:
        with pytest.raises(q.ExcelQueryError) as ei:
            query_engine.run_aggregate(rep, {'operation': op, 'column': 'QTY', 'sheet_index': 0},
                                       engine='duckdb')
        assert ei.value.code == q.ERR_COLUMN_AMBIGUOUS
        assert set(ei.value.to_dict()['details']['candidates']) == {'Qty', 'qty'}


def test_column_case_resolution(big_rep):
    for name in ['order amount', 'ORDER AMOUNT', 'Order Amount']:
        res, _ = query_engine.run_aggregate(
            big_rep, {'operation': 'sum', 'column': name}, engine='duckdb')
        assert res.column == 'Order Amount'
        assert res.value == pytest.approx(3587.34, **TOL)


# ==========================================================================
# 6. 安全
# ==========================================================================
def test_rendered_aggregate_sql_is_safe(big_rep):
    from backend.excel import duck as dk

    sheet = big_rep.sheets[0]
    secret = "SF' OR '1'='1"
    req = agg.parse_aggregate_payload({
        'operation': 'sum', 'column': 'Order Amount', 'sheet_index': 0,
        'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': secret}],
    }, document_id=big_rep.document_id)
    req = agg.resolve_aggregate(big_rep, req)
    info = dk.get_registry().get(big_rep, 0)
    plan_filters = dk.build_plan_filters(
        sheet, [q.FilterCondition(**f) for f in req.filters])
    sql = agg.build_aggregate_sql(info['table'], plan_filters, req.operation, req.column_index)

    for key in ('main_sql', 'rows_sql'):
        text = sql[key].lower().lstrip()
        assert text.startswith('select'), f'{key} 必须以 SELECT 开头'
        assert ';' not in sql[key]
        assert secret not in sql[key], '用户输入不得出现在 SQL 文本中'
        for banned in ('insert', 'update', 'delete', 'drop', 'alter', 'create', 'attach', 'copy', 'pragma'):
            assert banned not in text, f'{key} 中不得出现 {banned}'
    assert secret in sql['main_params'], '筛选值必须走参数绑定'
    assert '"c26"' in sql['main_sql'], '目标列必须是 Python 生成的物理列名'


def test_injection_like_values_are_literal(big_rep):
    for evil in ["' OR 1=1 --", '"; DROP TABLE t; --', '1; DELETE FROM t', "%' OR '1'='1"]:
        res, _ = query_engine.run_aggregate(big_rep, {
            'sheet_index': 0, 'operation': 'count',
            'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': evil}]},
            engine='duckdb')
        assert res.matched_rows == 0
        assert res.value == 0.0


def test_external_access_still_blocked(big_rep):
    query_engine.run_aggregate(big_rep, {'operation': 'count'}, engine='duckdb')
    db = duck_engine.get_registry()._db_for(big_rep.document_id)
    blocked = 0
    for stmt in ["ATTACH 'evil.db' AS z", "COPY (SELECT 1) TO 'evil.csv'"]:
        try:
            db.con.execute(stmt)
        except Exception:
            blocked += 1
    assert blocked == 2


def test_cross_user_document_isolation():
    base = PROJECT_ROOT / 'data' / 'excel'
    if not base.exists():
        pytest.skip('无已落盘 representation')
    doc_dirs = [d for d in base.iterdir() if (d / 'representation.json').exists()]
    if not doc_dirs:
        pytest.skip('无已落盘 representation')
    doc_dir = doc_dirs[0]
    with open(doc_dir / 'representation.json', 'r', encoding='utf-8') as f:
        meta = json.load(f)
    owner = meta.get('user_id')
    assert owner is not None
    assert excel_store.load_representation(doc_dir.name, user_id=owner) is not None
    assert excel_store.load_representation(doc_dir.name, user_id=owner + 100000) is None


# ==========================================================================
# 7. 差分矩阵（跨表 / 跨列 / 跨操作 / 跨条件）
# ==========================================================================
def test_differential_matrix(big_rep, small_rep):
    numeric_cols = {
        'big': ['Order Amount', 'Taxes', 'Quantity', 'Weight(kg)', 'Shipping Fee After Discount',
                'SKU Unit Original Price', 'Original Shipping Fee', 'SKU Platform Discount'],
        'small': ['Order Amount', 'Taxes', 'Quantity', 'Weight(kg)', 'SKU Seller Discount',
                  'Original Shipping Fee', 'SKU Subtotal After Discount'],
    }
    conditions = [
        ('无条件', []),
        ('contains SF', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]),
        ('gt 金额>5', [{'column': 'Order Amount', 'operator': 'gt', 'value': 5}]),
        ('双条件', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'n'},
                  {'column': 'Order Amount', 'operator': 'gte', 'value': 0}]),
        ('0行', [{'column': 'Order Amount', 'operator': 'gt', 'value': 99999}]),
    ]
    count = 0
    for rep, tag in [(big_rep, 'big'), (small_rep, 'small')]:
        for cond_label, filters in conditions:
            payload = {'sheet_index': 0, 'operation': 'count', 'filters': filters}
            _three_way(rep, payload, f'{tag}/count/{cond_label}')
            count += 1
            for col in numeric_cols[tag]:
                for op in agg.NUMERIC_OPERATIONS:
                    _three_way(rep, {'sheet_index': 0, 'operation': op, 'column': col,
                                     'filters': filters}, f'{tag}/{op}/{col}/{cond_label}')
                    count += 1
    assert count > 100, f'差分用例过少：{count}'


# ==========================================================================
# 8. 摘要与来源可追溯
# ==========================================================================
def test_summary_is_python_generated_and_traceable(big_rep):
    res, _ = query_engine.run_aggregate(big_rep, {
        'sheet_index': 0, 'operation': 'count',
        'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]},
        engine='duckdb')
    text = agg.format_aggregate_summary(res, '直邮5店 7.10号订单.xlsx')
    for expected in ['直邮5店 7.10号订单.xlsx', 'OrderSKUList', 'COUNT', '447',
                     'Shipping Provider Name 包含 SF', '匹配 Excel 行：3 ~ 449', '统计口径']:
        assert expected in text, f'摘要缺少 {expected}'

    payload = res.to_dict()
    for key in ['document_id', 'sheet_name', 'operation', 'column', 'filters', 'value',
                'matched_rows', 'numeric_rows', 'empty_rows', 'non_numeric_rows',
                'row_excel_numbers', 'row_excel_spans', 'definition', 'value_display']:
        assert key in payload, f'结果缺少字段 {key}'
    assert payload['value_display'] == '447'
    assert payload['row_excel_spans'] == {'first': 3, 'last': 449}


# ==========================================================================
# 9. NL → AggregateIntent → 统计执行（上下文继承 / 隔离）
# ==========================================================================
def _catalog(rep) -> List[Dict[str, Any]]:
    return [{
        'document_id': rep.document_id,
        'filename': rep.filename,
        'file_type': rep.file_type,
        'created_at': '2026-09-14T00:00:00',
        'total_rows': rep.total_rows,
        'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                    'row_count': s.row_count, 'column_count': s.column_count,
                    'columns': s.column_names} for s in rep.sheets],
    }]


def _run_agg(rep, intent: 'nl.AggregateIntent', *, context=None, aggregate_context=None,
             session_key='p3a') -> Dict[str, Any]:
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nl.run_nl_query(
            '统计', _catalog(rep),
            aggregate_override=nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=intent),
            context=context, aggregate_context=aggregate_context,
            user_id=1, session_key=session_key,
        ))
    finally:
        excel_store.load_representation = original


def test_nl_aggregate_intent_parsing():
    intent = nl.aggregate_intent_from_dict({
        'aggregate_operation': 'SUM', 'column': ' Order Amount ', 'document': '5店',
        'filters': [{'column': 'Quantity', 'operator': 'GT', 'value': '10'}],
    })
    assert intent.operation == 'sum'
    assert intent.column == 'Order Amount'
    assert intent.filters == [{'column': 'Quantity', 'operator': 'gt', 'value': '10'}]

    # count 时忽略 column；未知 operation 兜底 count
    assert nl.aggregate_intent_from_dict({'aggregate_operation': 'count', 'column': 'Order ID'}).column is None
    assert nl.aggregate_intent_from_dict({'aggregate_operation': 'median'}).operation == 'count'
    assert nl.aggregate_intent_from_dict({}).operation == 'count'

    # refers_to_previous 容错
    assert nl.aggregate_intent_from_dict({'refers_to_previous': True}).refers_to_previous is True
    assert nl.aggregate_intent_from_dict({'refers_to_previous': 'true'}).refers_to_previous is True

    # turn 解析必须挂上 aggregate
    turn = nl.turn_intent_from_dict({'action': 'aggregate', 'aggregate_operation': 'avg',
                                     'column': 'Order Amount'})
    assert turn.action == nl.ACTION_AGGREGATE
    assert turn.aggregate is not None and turn.aggregate.operation == 'avg'


def test_nl_aggregate_count_and_numeric_ops(small_rep):
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', sheet='OrderSKUList',
        filters=[{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]))
    assert out['status'] == 'ok'
    assert out['engine'] == 'duckdb'
    assert out['aggregate']['value_display'] == '4'
    assert out['aggregate']['matched_rows'] == 4
    assert 'new_aggregate_context' in out and 'new_context' not in out

    expected = {'sum': '306.02', 'avg': '16.106316', 'min': '0', 'max': '35.33'}
    for op, display in expected.items():
        out = _run_agg(small_rep, nl.AggregateIntent(operation=op, column='订单金额'))
        assert out['status'] == 'ok', f'{op}: {out.get("message")}'
        assert out['aggregate']['value_display'] == display, f'{op}: {out["aggregate"]["value_display"]}'
        assert out['aggregate']['column'] == 'Order Amount'   # 中文简称解析到真实列


def test_nl_aggregate_inherits_from_aggregate_context(small_rep):
    """「物流商为SF的有几单」-> 「这些订单的平均订单金额是多少」。"""
    first = _run_agg(small_rep, nl.AggregateIntent(
        operation='count',
        filters=[{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]))
    assert first['status'] == 'ok' and first['aggregate']['value_display'] == '4'
    agg_ctx = AggregateContext(**first['new_aggregate_context'])

    second = _run_agg(small_rep, nl.AggregateIntent(
        operation='avg', column='Order Amount', refers_to_previous=True),
        aggregate_context=agg_ctx)
    assert second['status'] == 'ok', second.get('message')
    assert second['inherited_from'] == 'aggregate'
    assert second['aggregate']['matched_rows'] == 4          # 继承了 SF 条件
    assert second['aggregate']['value_display'] == '16.575'  # 4 行的平均（66.30 / 4）
    assert second['query']['filters'] == agg_ctx.filters


def test_nl_aggregate_inherits_from_query_context(small_rep):
    """先做列表查询，再问「这些订单一共有多少条」-> 继承分页上下文的筛选条件。"""
    ctx = ExcelQueryContext(
        user_id=1, session_key='p3a', document_id=small_rep.document_id, filename=small_rep.filename,
        sheet_index=0, sheet_name=small_rep.sheets[0].sheet_name, columns=['Order ID'],
        filters=[{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}],
        limit=20, offset=0, total_matches=4, total_rows_in_sheet=19,
    )
    out = _run_agg(small_rep, nl.AggregateIntent(operation='count', refers_to_previous=True),
                   context=ctx)
    assert out['status'] == 'ok'
    assert out['inherited_from'] == 'query'
    assert out['aggregate']['value_display'] == '4'


def test_nl_aggregate_refers_to_previous_without_context_clarifies(small_rep):
    out = _run_agg(small_rep, nl.AggregateIntent(operation='count', refers_to_previous=True))
    assert out['status'] == 'clarify'
    assert '这些订单' in out['message']


def test_nl_aggregate_extra_filters_are_anded_with_inherited(small_rep):
    """继承条件 + 本轮额外条件 = AND。"""
    agg_ctx = AggregateContext(
        user_id=1, session_key='p3a', document_id=small_rep.document_id, filename=small_rep.filename,
        sheet_index=0, sheet_name=small_rep.sheets[0].sheet_name, operation='count',
        filters=[{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}],
        matched_rows=4,
    )
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='sum', column='Order Amount', refers_to_previous=True,
        filters=[{'column': 'Quantity', 'operator': 'gt', 'value': 1}]),
        aggregate_context=agg_ctx)
    assert out['status'] == 'ok'
    assert out['aggregate']['matched_rows'] == 1     # SF(4) ∩ Quantity>1(1) = 1
    assert out['aggregate']['value_display'] == '33.2'


def test_nl_aggregate_non_numeric_column_clarifies(small_rep):
    out = _run_agg(small_rep, nl.AggregateIntent(operation='sum', column='Product Name'))
    assert out['status'] == 'clarify'
    assert '不是数值列' in out['message']


def test_nl_aggregate_unknown_column_clarifies(small_rep):
    out = _run_agg(small_rep, nl.AggregateIntent(operation='sum', column='不存在列名'))
    assert out['status'] == 'clarify'
    assert '目标列' in out['message']


def test_nl_aggregate_ambiguous_document_clarifies(small_rep):
    """目录里有两个文件且用户的描述无法唯一匹配 -> clarify（不猜）。"""
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: small_rep
    try:
        catalog = _catalog(small_rep) + [{
            'document_id': 'other-doc', 'filename': '直邮5店 7.10号订单.xlsx', 'file_type': 'xlsx',
            'created_at': '2026-09-14T00:00:00', 'total_rows': 447, 'sheets': [],
        }]
        out = asyncio.run(nl.run_nl_query(
            '统计', catalog,
            aggregate_override=nl.TurnIntent(action=nl.ACTION_AGGREGATE,
                                             aggregate=nl.AggregateIntent(operation='count')),
            user_id=1, session_key='p3a-doc'))
    finally:
        excel_store.load_representation = original
    assert out['status'] == 'clarify'


def test_aggregate_context_store_isolation():
    store = AggregateContextStore()
    ctx = AggregateContext(user_id=1, session_key='s1', document_id='d', filename='f',
                           sheet_index=0, sheet_name='S', operation='count')
    store.save_aggregate(ctx)
    assert store.get_for_inheritance(1, 's1') is not None
    assert store.get_for_inheritance(2, 's1') is None       # 不同 user 不串
    assert store.get_for_inheritance(1, 's2') is None       # 不同 session 不串
    store.mark_other_turn(1, 's1')
    assert store.get_for_inheritance(1, 's1') is None       # 非统计轮次不可继承


def test_context_stores_do_not_pollute_each_other():
    """统计上下文与分页上下文必须物理隔离（互不继承、互不覆盖）。

    注意：两个 store 是独立的容器；「一轮之后另一类上下文失效」由 API 层
    mark_other_turn 完成（下面的断言复现了 API 层的这段规则）。
    """
    from backend.excel.query_context import ExcelQueryContextStore

    agg_store = AggregateContextStore()
    page_store = ExcelQueryContextStore()

    # 1) 统计成功：只产生统计上下文，不会凭空产生分页上下文
    agg_store.save_aggregate(AggregateContext(
        user_id=1, session_key='k', document_id='d', filename='f',
        sheet_index=0, sheet_name='S', operation='count'))
    assert page_store.get_for_pagination(1, 'k') is None
    # API 层在统计成功后会让分页上下文失效
    page_store.mark_other_turn(1, 'k')
    assert page_store.get_for_pagination(1, 'k') is None
    # 但统计追问仍然可以继承
    assert agg_store.get_for_inheritance(1, 'k') is not None

    # 2) 列表查询成功：不会把统计上下文当成可继承来源
    page_store.save_excel(ExcelQueryContext(
        user_id=1, session_key='k', document_id='d', filename='f',
        sheet_index=0, sheet_name='S', limit=20, offset=0))
    agg_store.mark_other_turn(1, 'k')
    assert agg_store.get_for_inheritance(1, 'k') is None

    # 3) 两个 store 互不读取对方的数据
    fresh_page = ExcelQueryContextStore()
    fresh_agg = AggregateContextStore()
    agg_store2 = AggregateContextStore()
    agg_store2.save_aggregate(AggregateContext(
        user_id=1, session_key='z', document_id='d', filename='f',
        sheet_index=0, sheet_name='S', operation='count'))
    assert fresh_page.get_for_pagination(1, 'z') is None
    assert fresh_agg.get_for_inheritance(1, 'z') is None
