# -*- coding: utf-8 -*-
"""Phase 3B：Excel 分组统计（GROUP BY）测试矩阵

三方交叉验证（顺序无关，按 group_key 逐组比较）：
  A. DuckDB GROUP BY     backend/excel/aggregate.py::execute_group_aggregate_duckdb
  B. Python 参考实现      backend/excel/aggregate.py::execute_group_aggregate_python
  C. 本文件内独立扫描     _scan_ground_truth_group()（手写语义，不 import 被测实现）

比较维度：分组键 / 每组聚合值 / 每组匹配行数 / 每组可数值化行数 / 分组数量 / 总匹配行数。

注意：本阶段**不定义任何业务排序**，因此所有比较均按 group_key 映射进行，不比较顺序。
"""

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.excel import aggregate as agg
from backend.excel import duck as duck_engine
from backend.excel import engine as query_engine
from backend.excel import nl_query as nl
from backend.excel import query as q
from backend.excel import store as excel_store
from backend.excel.parser import ExcelParser
from backend.excel.query_context import (
    AggregateContext,
    AggregateContextStore,
    ExcelQueryContext,
    ExcelQueryContextStore,
)
from backend.excel.representation import ColumnMeta, SheetRepresentation, WorkbookRepresentation

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMALL_REAL = PROJECT_ROOT / '直邮一店 8.20号订单.xlsx'
BIG_CANDIDATES = [
    PROJECT_ROOT / '直邮5店 7.10号订单.xlsx',
]
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


def _scan_ground_truth_group(rep, payload: Dict[str, Any]) -> Dict[str, Any]:
    """独立扫描，返回 {group_key: {value, matched_rows, numeric_rows, empty_rows, non_numeric_rows}}
    以及 total_matched / total_groups。"""
    sheet = rep.sheets[payload.get('sheet_index') or 0]
    names = sheet.column_names
    filters = payload.get('filters') or []
    op = payload['operation']
    group_names = payload.get('group_by') or []
    group_idx = [_index_of(names, g) for g in group_names]

    buckets: Dict[Tuple[Any, ...], List[List[Any]]] = {}
    for row in sheet.rows:
        ok = True
        for f in filters:
            ci = _index_of(names, f['column'])
            if not _match(row[ci] if ci < len(row) else None, f['operator'], f.get('value')):
                ok = False
                break
        if not ok:
            continue
        key = tuple(row[i] if i < len(row) else None for i in group_idx)
        buckets.setdefault(key, []).append(row)

    value_ci = _index_of(names, payload['column']) if op != 'count' else None
    out: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for key, rows in buckets.items():
        matched = len(rows)
        if op == 'count':
            out[key] = {'value': float(matched), 'matched_rows': matched,
                        'numeric_rows': matched, 'empty_rows': 0, 'non_numeric_rows': 0}
            continue
        numbers: List[float] = []
        empty = non_numeric = 0
        for row in rows:
            raw = row[value_ci] if value_ci < len(row) else None
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
        out[key] = {'value': value, 'matched_rows': matched, 'numeric_rows': len(numbers),
                    'empty_rows': empty, 'non_numeric_rows': non_numeric}
    return {'groups': out, 'total_matched': sum(g['matched_rows'] for g in out.values()),
            'total_groups': len(out)}


# ==========================================================================
# 夹具
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
    return ExcelParser().parse(str(p), 'grp-big', user_id=1, filename=p.name)


@pytest.fixture(scope='module')
def small_rep():
    if not SMALL_REAL.exists():
        pytest.skip('未找到小表（直邮一店 8.20号订单.xlsx）')
    return ExcelParser().parse(str(SMALL_REAL), 'grp-small', user_id=1, filename=SMALL_REAL.name)


@pytest.fixture(autouse=True)
def _clean_registry():
    duck_engine.reset_registry()
    yield
    duck_engine.reset_registry()


def _synth_rep() -> WorkbookRepresentation:
    """合成表示：用于验证 NULL 与空字符串 '' 的分组语义（真实数据里空单元格统一为 None）。"""
    cols = [
        ColumnMeta(name='G', index=0, excel_column=1, excel_column_letter='A',
                   dtype='string', non_empty=3, null_count=1),
        ColumnMeta(name='V', index=1, excel_column=2, excel_column_letter='B',
                   dtype='number', non_empty=4, null_count=0),
        ColumnMeta(name='T', index=2, excel_column=3, excel_column_letter='C',
                   dtype='string', non_empty=3, null_count=1),
    ]
    sheet = SheetRepresentation(
        sheet_name='S', sheet_index=0, header_mode='single', columns=cols,
        rows=[
            [None, 1, 'a'],      # G=NULL
            ['', 2, 'a'],        # G='' （与 NULL 必须是两个分组）
            ['x', 3, 'b'],
            ['x', None, 'b'],    # V 为空
            ['x', 'not-a-number', 'b'],   # V 非数值
        ],
        row_excel_numbers=[2, 3, 4, 5, 6], row_count=5, column_count=3,
    )
    return WorkbookRepresentation(
        schema_version='1.0', document_id='grp-synth', user_id=1, filename='synth.csv',
        file_type='csv', parser='csv', sheet_count=1, sheets=[sheet],
    )


def _assert_value(actual: Optional[float], expected: Optional[float], label: str):
    if expected is None:
        assert actual is None, f'{label}: 期望 None，实际 {actual}'
    else:
        assert actual is not None, f'{label}: 期望 {expected}，实际 None'
        assert actual == pytest.approx(expected, **TOL), f'{label}: {actual} != {expected}'


def _three_way(rep, payload: Dict[str, Any], label: str = '') -> Any:
    """A(DuckDB) == B(Python) == C(独立扫描)，按 group_key 逐组比较。"""
    gt = _scan_ground_truth_guard(rep, payload)
    expected, exp_total, exp_groups = gt['groups'], gt['total_matched'], gt['total_groups']

    def check(res, engine_name: str):
        assert res.total_groups == exp_groups, \
            f'{label}/{engine_name}: 分组数 {res.total_groups} != GT {exp_groups}'
        assert res.matched_rows == exp_total, \
            f'{label}/{engine_name}: 总匹配行 {res.matched_rows} != GT {exp_total}'
        assert {r.key() for r in res.rows} == set(expected.keys()), \
            f'{label}/{engine_name}: 分组键集合不一致（{sorted(map(str, {r.key() for r in res.rows}))}）'
        for r in res.rows:
            e = expected[r.key()]
            _assert_value(r.value, e['value'], f'{label}/{engine_name}/{r.key()}')
            assert r.matched_rows == e['matched_rows'], f'{label}/{engine_name}/{r.key()}: matched'
            assert r.numeric_rows == e['numeric_rows'], f'{label}/{engine_name}/{r.key()}: numeric'
            assert r.empty_rows == e['empty_rows'], f'{label}/{engine_name}/{r.key()}: empty'
            assert r.non_numeric_rows == e['non_numeric_rows'], \
                f'{label}/{engine_name}/{r.key()}: non_numeric'
            if payload['operation'] != 'count':
                # 口径不变式
                assert r.matched_rows == r.numeric_rows + r.empty_rows + r.non_numeric_rows, \
                    f'{label}/{engine_name}/{r.key()}: 统计口径不变式被破坏'

    res_py, eng_py = query_engine.run_aggregate(rep, payload, engine=query_engine.ENGINE_PYTHON)
    assert eng_py == 'python'
    check(res_py, 'python')

    res_dk, eng_dk = query_engine.run_aggregate(rep, payload, engine=query_engine.ENGINE_DUCKDB)
    assert eng_dk == 'duckdb'
    check(res_dk, 'duckdb')
    return res_dk


def _scan_ground_truth_guard(rep, payload):
    """分组统计必须提供 group_by；顺手断言操作合法性。"""
    assert payload.get('group_by'), 'GT 仅用于分组统计'
    return _scan_ground_truth_group(rep, payload)


# ==========================================================================
# 1. 必测：真实物流商分组（核心验收点）
# ==========================================================================
def test_group_by_shipping_provider_count(small_rep):
    """每个物流商分别有多少单？ -> Yanwen 12 / SF 4 / JS 3。"""
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Shipping Provider Name']}, 'COUNT by 物流商')
    by_key = {r.key()[0]: r.matched_rows for r in res.rows}
    assert by_key == {'Yanwen Express': 12, 'SF International': 4, 'JS Express International': 3}, by_key
    assert res.total_groups == 3
    assert res.matched_rows == 19
    assert all(r.value == r.matched_rows for r in res.rows)


def test_group_by_shipping_provider_numeric_ops(small_rep):
    """每个物流商的订单金额 SUM / AVG / MIN / MAX（与独立 GT 逐组比较）。"""
    for op in agg.NUMERIC_OPERATIONS:
        _three_way(small_rep, {
            'sheet_index': 0, 'operation': op, 'column': 'Order Amount',
            'group_by': ['Shipping Provider Name']}, f'{op} by 物流商')

    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'sum', 'column': 'Order Amount',
        'group_by': ['Shipping Provider Name']}, 'SUM 明细')
    sums = {r.key()[0]: round(r.value or 0, 2) for r in res.rows}
    assert sums == {'SF International': 66.3, 'Yanwen Express': 167.57, 'JS Express International': 72.15}, sums


# ==========================================================================
# 2. 五个操作 × 分组（小表/大表）
# ==========================================================================
def test_all_operations_grouped_small(small_rep):
    cases = [
        ('count by 状态', {'operation': 'count', 'group_by': ['Order Status']}),
        ('count by 支付方式', {'operation': 'count', 'group_by': ['Payment Method']}),
        ('count by 数量', {'operation': 'count', 'group_by': ['Quantity']}),
        ('sum by 支付方式', {'operation': 'sum', 'column': 'Order Amount', 'group_by': ['Payment Method']}),
        ('avg by 数量', {'operation': 'avg', 'column': 'Order Amount', 'group_by': ['Quantity']}),
        ('min by 渠道', {'operation': 'min', 'column': 'Order Amount', 'group_by': ['Order Channel']}),
        ('max by 渠道', {'operation': 'max', 'column': 'Order Amount', 'group_by': ['Order Channel']}),
        ('sum by 州', {'operation': 'sum', 'column': 'Order Amount', 'group_by': ['State']}),
        ('count by 空值列', {'operation': 'count', 'group_by': ['Delivery Instruction']}),
    ]
    for label, extra in cases:
        _three_way(small_rep, {'sheet_index': 0, **extra}, label)


def test_all_operations_grouped_big(big_rep):
    cases = [
        ('count by 物流商(1组)', {'operation': 'count', 'group_by': ['Shipping Provider Name']}),
        ('count by 支付方式(4组)', {'operation': 'count', 'group_by': ['Payment Method']}),
        ('sum by 支付方式', {'operation': 'sum', 'column': 'Order Amount', 'group_by': ['Payment Method']}),
        ('avg by 支付方式', {'operation': 'avg', 'column': 'Order Amount', 'group_by': ['Payment Method']}),
        ('min by 支付方式', {'operation': 'min', 'column': 'Order Amount', 'group_by': ['Payment Method']}),
        ('max by 支付方式', {'operation': 'max', 'column': 'Order Amount', 'group_by': ['Payment Method']}),
        ('count by 配送说明(含NULL)', {'operation': 'count', 'group_by': ['Delivery Instruction']}),
        ('sum by 商品(3组)', {'operation': 'sum', 'column': 'Order Amount', 'group_by': ['Product Name']}),
    ]
    for label, extra in cases:
        _three_way(big_rep, {'sheet_index': 0, **extra}, label)


# ==========================================================================
# 3. NULL / 空字符串语义
# ==========================================================================
def test_null_is_its_own_group(small_rep):
    """真实数据：Normal or Pre-order 有 2 个空值 -> 独立 NULL 分组。"""
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Normal or Pre-order']}, 'NULL 分组')
    assert res.total_groups == 2
    null_rows = [r for r in res.rows if r.group[0].is_null]
    assert len(null_rows) == 1
    assert null_rows[0].group[0].value is None
    assert null_rows[0].group[0].display == agg.GROUP_NULL_LABEL == '<空>'
    assert null_rows[0].matched_rows == 2


def test_all_null_column_gives_single_null_group(small_rep):
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Delivery Instruction']}, '全空列')
    assert res.total_groups == 1
    assert res.rows[0].group[0].is_null is True
    assert res.rows[0].matched_rows == 19


def test_null_and_empty_string_are_distinct_groups():
    """NULL 与 '' 必须是两个不同分组（合成表示，绕过解析器）。"""
    rep = _synth_rep()
    res = _three_way(rep, {'sheet_index': 0, 'operation': 'count', 'group_by': ['G']}, 'NULL vs 空串')
    assert res.total_groups == 3
    labels = {r.group[0].display: r.matched_rows for r in res.rows}
    assert agg.GROUP_NULL_LABEL in labels, labels
    assert '' in labels, labels
    assert labels[agg.GROUP_NULL_LABEL] == 1
    assert labels[''] == 1
    assert labels['x'] == 3

    null_cell = [r for r in res.rows if r.group[0].is_null][0]
    empty_cell = [r for r in res.rows if r.group[0].display == ''][0]
    assert null_cell.group[0].value is None
    assert empty_cell.group[0].value == ''
    assert empty_cell.group[0].is_null is False


def test_group_aggregate_with_empty_and_non_numeric(small_rep):
    """分组内的空值/非数值不影响其它组，且口径不变式成立。"""
    rep = _synth_rep()
    res = _three_way(rep, {
        'sheet_index': 0, 'operation': 'sum', 'column': 'V', 'group_by': ['G']}, 'SUM 含空/非数值')
    by_key = {r.key()[0]: r for r in res.rows}
    assert by_key[None].matched_rows == 1 and by_key[None].numeric_rows == 1
    assert by_key[''].matched_rows == 1 and by_key[''].numeric_rows == 1
    assert by_key['x'].matched_rows == 3
    assert by_key['x'].numeric_rows == 1        # 只有 3
    assert by_key['x'].empty_rows == 1          # None
    assert by_key['x'].non_numeric_rows == 1    # 'not-a-number'
    _assert_value(by_key['x'].value, 3.0, 'x 组求和')


def test_group_without_numeric_values_returns_none():
    """某个分组没有可数值化数据 -> 该组 value=None（不编造 0）。"""
    cols = [
        ColumnMeta(name='G', index=0, excel_column=1, excel_column_letter='A', dtype='string', non_empty=2),
        ColumnMeta(name='V', index=1, excel_column=2, excel_column_letter='B', dtype='mixed', non_empty=2),
    ]
    sheet = SheetRepresentation(
        sheet_name='S', sheet_index=0, header_mode='single', columns=cols,
        rows=[['a', 5], ['b', 'text']], row_excel_numbers=[2, 3], row_count=2, column_count=2)
    rep = WorkbookRepresentation(schema_version='1.0', document_id='grp-none', user_id=1,
                                 filename='s.csv', file_type='csv', parser='csv',
                                 sheet_count=1, sheets=[sheet])
    res = _three_way(rep, {'sheet_index': 0, 'operation': 'sum', 'column': 'V', 'group_by': ['G']}, '无有效数值组')
    by_key = {r.key()[0]: r for r in res.rows}
    _assert_value(by_key['a'].value, 5.0, 'a 组')
    assert by_key['b'].value is None
    assert by_key['b'].numeric_rows == 0
    assert by_key['b'].non_numeric_rows == 1


# ==========================================================================
# 4. GROUP BY + WHERE
# ==========================================================================
def test_group_by_with_filters(small_rep):
    filters = [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Payment Method'], 'filters': filters},
        'SF 下按支付方式分组')
    assert res.matched_rows == 4          # 过滤先执行
    assert res.total_groups <= 4

    # 过滤后 SUM，必须等于该子集的 SUM（与单值统计一致）
    res2 = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'sum', 'column': 'Order Amount',
        'group_by': ['Shipping Provider Name'], 'filters': filters}, 'SF 下按物流商 SUM')
    assert res2.total_groups == 1
    _assert_value(res2.rows[0].value, 66.3, 'SF SUM')


def test_group_by_with_multi_filters(small_rep):
    combos = [
        ('2 条 AND', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'International'},
                     {'column': 'Quantity', 'operator': 'gte', 'value': 1}]),
        ('3 条 AND', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'n'},
                     {'column': 'Order Amount', 'operator': 'gt', 'value': 0},
                     {'column': 'Quantity', 'operator': 'gte', 'value': 1}]),
        ('neq 条件', [{'column': 'Shipping Provider Name', 'operator': 'neq', 'value': 'Yanwen Express'}]),
        ('lte 条件', [{'column': 'Order Amount', 'operator': 'lte', 'value': 20}]),
        ('eq 空值条件', [{'column': 'Normal or Pre-order', 'operator': 'eq', 'value': None}]),
    ]
    for label, filters in combos:
        for op, col in [('count', None), ('sum', 'Order Amount'), ('avg', 'Weight(kg)'),
                        ('min', 'Order Amount'), ('max', 'Order Amount')]:
            payload: Dict[str, Any] = {'sheet_index': 0, 'operation': op,
                                       'group_by': ['Shipping Provider Name'], 'filters': filters}
            if col:
                payload['column'] = col
            _three_way(small_rep, payload, f'{label}/{op}')


def test_group_by_operator_matrix(small_rep):
    """7 个 operator 均可作为分组统计的过滤条件。"""
    for op in ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'contains']:
        if op == 'contains':
            value: Any = 'SF'
        elif op in ('eq', 'neq'):
            value = 'SF International'
        else:
            value = 1
        column = 'Shipping Provider Name' if op in ('contains', 'eq', 'neq') else 'Quantity'
        _three_way(small_rep, {
            'sheet_index': 0, 'operation': 'count', 'group_by': ['Payment Method'],
            'filters': [{'column': column, 'operator': op, 'value': value}]}, f'filter={op}')


# ==========================================================================
# 5. 分组规模：0 / 1 / 2 / 3 / 多分组
# ==========================================================================
def test_group_size_spectrum(small_rep, big_rep):
    res0 = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Shipping Provider Name'],
        'filters': [{'column': 'Order Amount', 'operator': 'gt', 'value': 99999}]}, '0 分组')
    assert res0.total_groups == 0 and res0.rows == [] and res0.matched_rows == 0
    assert res0.row_excel_span is None

    res1 = _three_way(big_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Shipping Provider Name']}, '1 分组')
    assert res1.total_groups == 1

    res2 = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Normal or Pre-order']}, '2 分组')
    assert res2.total_groups == 2

    res3 = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Shipping Provider Name']}, '3 分组')
    assert res3.total_groups == 3

    res_many = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['State']}, '多分组')
    assert res_many.total_groups == 12


def test_multi_column_group_by(small_rep):
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count',
        'group_by': ['Shipping Provider Name', 'Order Status']}, '双列分组')
    assert res.total_groups == 3
    assert all(len(r.group) == 2 for r in res.rows)
    assert {g['name'] for g in res.group_by} == {'Shipping Provider Name', 'Order Status'}

    res2 = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'sum', 'column': 'Order Amount',
        'group_by': ['Payment Method', 'Order Channel']}, '双列分组+SUM')
    assert all(r.matched_rows == r.numeric_rows for r in res2.rows)


# ==========================================================================
# 6. schema 校验与错误
# ==========================================================================
def test_group_schema_errors(small_rep):
    with pytest.raises(q.ExcelQueryError) as e1:
        query_engine.run_aggregate(small_rep, {'operation': 'count', 'group_by': ['不存在']},
                                   engine='duckdb')
    assert e1.value.code == q.ERR_COLUMN_NOT_FOUND

    with pytest.raises(q.ExcelQueryError) as e2:
        query_engine.run_aggregate(small_rep, {'operation': 'count', 'group_by': ['']},
                                   engine='duckdb')
    assert e2.value.code == q.ERR_INVALID_PARAM

    with pytest.raises(q.ExcelQueryError) as e3:
        query_engine.run_aggregate(small_rep, {
            'operation': 'count',
            'group_by': ['Order ID', 'Order Status', 'Quantity', 'City']}, engine='duckdb')
    assert e3.value.code == agg.ERR_TOO_MANY_GROUP_COLUMNS

    with pytest.raises(q.ExcelQueryError) as e4:
        query_engine.run_aggregate(small_rep, {
            'operation': 'sum', 'column': 'Product Name',
            'group_by': ['Shipping Provider Name']}, engine='duckdb')
    assert e4.value.code == agg.ERR_NOT_NUMERIC_COLUMN

    with pytest.raises(q.ExcelQueryError) as e5:
        query_engine.run_aggregate(small_rep, {
            'operation': 'sum', 'group_by': ['Shipping Provider Name']}, engine='duckdb')
    assert e5.value.code == agg.ERR_COLUMN_REQUIRED


def test_group_by_case_and_alias_resolution(small_rep):
    for name in ['shipping provider name', 'SHIPPING PROVIDER NAME']:
        res, _ = query_engine.run_aggregate(
            small_rep, {'operation': 'count', 'group_by': [name]}, engine='duckdb')
        assert res.group_by[0]['name'] == 'Shipping Provider Name'
        assert res.total_groups == 3


def test_group_by_duplicate_columns_deduped(small_rep):
    res, _ = query_engine.run_aggregate(small_rep, {
        'operation': 'count', 'group_by': ['Shipping Provider Name', 'Shipping Provider Name']},
        engine='duckdb')
    assert len(res.group_by) == 1
    assert res.total_groups == 3


# ==========================================================================
# 7. SQL 安全
# ==========================================================================
def test_rendered_group_sql_is_safe(small_rep):
    from backend.excel import duck as dk

    sheet = small_rep.sheets[0]
    secret = "SF' OR '1'='1"
    req = agg.parse_aggregate_payload({
        'operation': 'sum', 'column': 'Order Amount',
        'group_by': ['Shipping Provider Name'], 'sheet_index': 0,
        'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': secret}],
    }, document_id=small_rep.document_id)
    req = agg.resolve_aggregate(small_rep, req)
    info = dk.get_registry().get(small_rep, 0)
    plan_filters = dk.build_plan_filters(sheet, [q.FilterCondition(**f) for f in req.filters])
    sql = agg.build_group_aggregate_sql(info['table'], plan_filters, req.operation,
                                        req.column_index, req.group_by_indexes)

    for key in ('main_sql', 'totals_sql'):
        text = sql[key].lower().lstrip()
        assert text.startswith('select')
        assert ';' not in sql[key]
        assert secret not in sql[key], '用户输入不得出现在 SQL 文本中'
        for banned in ('insert', 'update', 'delete', 'drop', 'alter', 'create', 'attach',
                       'copy', 'pragma', 'order by'):
            assert banned not in text, f'{key} 中不得出现 {banned}'
    assert 'GROUP BY "c41"' in sql['main_sql'], '分组列必须是 Python 生成的物理列名'
    assert secret in sql['main_params']


def test_group_by_injection_like_inputs(small_rep):
    # 注入式分组列 -> schema 校验拒绝
    for evil in ['Shipping Provider Name" ; DROP TABLE t; --',
                 'c0); DELETE FROM t; --',
                 "Order ID' OR '1'='1"]:
        with pytest.raises(q.ExcelQueryError):
            query_engine.run_aggregate(
                small_rep, {'operation': 'count', 'group_by': [evil]}, engine='duckdb')

    # 注入式分组值（作为筛选值）-> 字面量，0 行 0 分组
    for evil in ["' OR 1=1 --", "'; DROP TABLE t; --"]:
        res, _ = query_engine.run_aggregate(small_rep, {
            'operation': 'count', 'group_by': ['Shipping Provider Name'],
            'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': evil}]},
            engine='duckdb')
        assert res.total_groups == 0 and res.matched_rows == 0


def test_group_external_access_blocked(small_rep):
    query_engine.run_aggregate(small_rep, {'operation': 'count', 'group_by': ['Order Status']},
                               engine='duckdb')
    db = duck_engine.get_registry()._db_for(small_rep.document_id)
    blocked = 0
    for stmt in ["ATTACH 'evil.db' AS z", "COPY (SELECT 1) TO 'evil.csv'"]:
        try:
            db.con.execute(stmt)
        except Exception:
            blocked += 1
    assert blocked == 2


# ==========================================================================
# 8. 差分矩阵
# ==========================================================================
def test_group_differential_matrix(big_rep, small_rep):
    specs = {
        'small': ['Shipping Provider Name', 'Order Status', 'Payment Method', 'Normal or Pre-order',
                  'Order Channel', 'Quantity', 'Product Category', 'State'],
        'big': ['Shipping Provider Name', 'Order Status', 'Payment Method', 'Delivery Instruction',
                'Weight(kg)', 'Product Name', 'Order Channel'],
    }
    conditions = [
        ('无条件', []),
        ('contains SF', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]),
        ('gt 金额>5', [{'column': 'Order Amount', 'operator': 'gt', 'value': 5}]),
        ('0 行', [{'column': 'Order Amount', 'operator': 'gt', 'value': 99999}]),
    ]
    count = 0
    for rep, tag in [(small_rep, 'small'), (big_rep, 'big')]:
        for gcol in specs[tag]:
            for cond_label, filters in conditions:
                payload: Dict[str, Any] = {'sheet_index': 0, 'operation': 'count',
                                           'group_by': [gcol], 'filters': filters}
                _three_way(rep, payload, f'{tag}/count/{gcol}/{cond_label}')
                count += 1
                payload2 = dict(payload, operation='sum', column='Order Amount')
                _three_way(rep, payload2, f'{tag}/sum/{gcol}/{cond_label}')
                count += 1
        for op in ['avg', 'min', 'max']:
            _three_way(rep, {'sheet_index': 0, 'operation': op, 'column': 'Order Amount',
                             'group_by': [specs[tag][0]]}, f'{tag}/{op}')
            count += 1
    assert count > 60, f'分组差分用例过少：{count}'


# ==========================================================================
# 9. 摘要与来源可追溯
# ==========================================================================
def test_group_summary_is_python_generated(small_rep):
    res, _ = query_engine.run_aggregate(small_rep, {
        'operation': 'count', 'sheet_index': 0, 'group_by': ['Shipping Provider Name'],
    }, engine='duckdb')
    text = agg.format_group_aggregate_summary(res, '直邮一店 8.20号订单.xlsx')
    for expected in ['直邮一店 8.20号订单.xlsx', 'OrderSKUList', 'Shipping Provider Name',
                     'COUNT', 'Yanwen Express → 12', 'SF International → 4',
                     'JS Express International → 3', '分组数：3 个', '匹配总行数：19 行',
                     '统计口径', '未做排序']:
        assert expected in text, f'摘要缺少 {expected}'

    payload = res.to_dict()
    for key in ['kind', 'document_id', 'sheet_name', 'operation', 'column', 'group_by',
                'filters', 'rows', 'total_groups', 'matched_rows', 'row_excel_spans', 'definition']:
        assert key in payload, f'结果缺少字段 {key}'
    assert payload['kind'] == 'group_aggregate'
    assert payload['total_groups'] == 3
    assert payload['row_excel_spans'] == {'first': 3, 'last': 21}
    row0 = payload['rows'][0]
    for key in ['group', 'group_key', 'group_display', 'value', 'value_display',
                'matched_rows', 'numeric_rows', 'empty_rows', 'non_numeric_rows']:
        assert key in row0, f'分组行缺少字段 {key}'


# ==========================================================================
# 10. NL 层：意图解析 / 分组执行 / 上下文继承与隔离
# ==========================================================================
def _catalog(rep) -> List[Dict[str, Any]]:
    return [{
        'document_id': rep.document_id, 'filename': rep.filename, 'file_type': rep.file_type,
        'created_at': '2026-09-14T00:00:00', 'total_rows': rep.total_rows,
        'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                    'row_count': s.row_count, 'column_count': s.column_count,
                    'columns': s.column_names} for s in rep.sheets],
    }]


def _run_agg(rep, intent: 'nl.AggregateIntent', *, context=None, aggregate_context=None,
             session_key='p3b') -> Dict[str, Any]:
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


def test_nl_group_intent_parsing():
    intent = nl.aggregate_intent_from_dict({
        'aggregate_operation': 'sum', 'column': 'Order Amount',
        'group_by': ['Shipping Provider Name', 'Order Status', 'Shipping Provider Name'],
    })
    assert intent.group_by == ['Shipping Provider Name', 'Order Status']   # 去重保序

    single = nl.aggregate_intent_from_dict({'aggregate_operation': 'count', 'group_by': []})
    assert single.group_by == []
    assert nl.aggregate_intent_from_dict({'aggregate_operation': 'count'}).group_by == []
    assert nl.aggregate_intent_from_dict({'group_by': 'Order Status'}).group_by == ['Order Status']
    assert nl.aggregate_intent_from_dict({'group_by': [None, '', 'Order Status']}).group_by == ['Order Status']


def test_nl_group_count_by_carrier(small_rep):
    """每个物流商分别有多少单？"""
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['物流商'], document='一店'))
    assert out['status'] == 'ok', out.get('message')
    assert out['engine'] == 'duckdb'
    g = out['group_aggregate']
    assert g['group_by'][0]['name'] == 'Shipping Provider Name'   # 中文简称解析
    by_key = {r['group_key'][0]: r['matched_rows'] for r in g['rows']}
    assert by_key == {'Yanwen Express': 12, 'SF International': 4, 'JS Express International': 3}
    assert g['total_groups'] == 3
    assert out['aggregate'] is None
    assert 'new_aggregate_context' in out and 'new_context' not in out


def test_nl_group_sum_and_avg(small_rep):
    for op, col in [('sum', '订单金额'), ('avg', 'Order Amount'), ('min', '金额'), ('max', 'Order Amount')]:
        out = _run_agg(small_rep, nl.AggregateIntent(
            operation=op, column=col, group_by=['Shipping Provider Name']))
        assert out['status'] == 'ok', f'{op}: {out.get("message")}'
        assert out['group_aggregate']['total_groups'] == 3
        assert out['group_aggregate']['column'] == 'Order Amount'
        assert all(r['value'] is not None for r in out['group_aggregate']['rows'])


def test_nl_group_with_filter(small_rep):
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['Order Status'],
        filters=[{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]))
    assert out['status'] == 'ok'
    g = out['group_aggregate']
    assert g['matched_rows'] == 4
    assert g['total_groups'] == 1
    assert g['rows'][0]['value'] == 4


def test_nl_plain_aggregate_still_single_value(small_rep):
    """回归：非分组语义仍然走 Phase 3A 单值统计。"""
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', filters=[{'column': 'Shipping Provider Name',
                                     'operator': 'contains', 'value': 'SF'}]))
    assert out['status'] == 'ok'
    assert out['aggregate'] is not None and out['group_aggregate'] is None
    assert out['aggregate']['value_display'] == '4'

    out2 = _run_agg(small_rep, nl.AggregateIntent(operation='sum', column='Order Amount'))
    assert out2['aggregate']['value_display'] == '306.02'


def test_nl_group_context_inheritance(small_rep):
    """每个物流商分别有多少单？ -> 这些物流商的平均订单金额是多少？"""
    first = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['Shipping Provider Name']))
    assert first['status'] == 'ok'
    ctx = AggregateContext(**first['new_aggregate_context'])
    assert ctx.group_by == ['Shipping Provider Name']

    second = _run_agg(small_rep, nl.AggregateIntent(
        operation='avg', column='Order Amount', refers_to_previous=True), aggregate_context=ctx)
    assert second['status'] == 'ok', second.get('message')
    assert second['inherited_from'] == 'aggregate'
    g = second['group_aggregate']
    assert g['total_groups'] == 3                       # 继承了分组字段
    assert [x['name'] for x in g['group_by']] == ['Shipping Provider Name']
    by_key = {r['group_key'][0]: r['value_display'] for r in g['rows']}
    assert by_key['SF International'] == '16.575'
    assert '已沿用上一轮的分组字段' in second['message']


def test_nl_group_context_switch_to_plain(small_rep):
    """分组统计后再问单值统计（用户明确不给分组）：不应强行分组。"""
    first = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['Shipping Provider Name']))
    ctx = AggregateContext(**first['new_aggregate_context'])

    second = _run_agg(small_rep, nl.AggregateIntent(
        operation='sum', column='Order Amount', refers_to_previous=False), aggregate_context=ctx)
    assert second['status'] == 'ok'
    assert second['aggregate'] is not None and second['group_aggregate'] is None
    assert second['aggregate']['value_display'] == '306.02'


def test_nl_group_missing_column_clarifies(small_rep):
    out = _run_agg(small_rep, nl.AggregateIntent(operation='count', group_by=['不存在列']))
    assert out['status'] == 'clarify'
    assert '分组字段' in out['message']


def test_nl_group_too_many_clarifies(small_rep):
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['Order ID', 'Order Status', 'Quantity', 'City']))
    assert out['status'] == 'clarify'
    assert '最多支持' in out['message']


def test_group_context_isolated_from_pagination(small_rep):
    """分组统计上下文不得被当作分页上下文，反之亦然。"""
    page_store = ExcelQueryContextStore()
    agg_store = AggregateContextStore()

    agg_store.save_aggregate(AggregateContext(
        user_id=1, session_key='g', document_id=small_rep.document_id, filename=small_rep.filename,
        sheet_index=0, sheet_name=small_rep.sheets[0].sheet_name, operation='count',
        group_by=['Shipping Provider Name']))
    page_store.mark_other_turn(1, 'g')       # API 层规则
    assert page_store.get_for_pagination(1, 'g') is None        # 分组统计后不能"再来20条"
    inherited = agg_store.get_for_inheritance(1, 'g')
    assert inherited is not None and inherited.group_by == ['Shipping Provider Name']

    page_store.save_excel(ExcelQueryContext(
        user_id=1, session_key='g', document_id=small_rep.document_id, filename=small_rep.filename,
        sheet_index=0, sheet_name=small_rep.sheets[0].sheet_name, limit=20, offset=0))
    agg_store.mark_other_turn(1, 'g')
    assert agg_store.get_for_inheritance(1, 'g') is None        # 列表查询不被当成分组上下文
    assert page_store.get_for_pagination(1, 'g') is not None
