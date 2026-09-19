# -*- coding: utf-8 -*-
"""Phase 4B：受控计算字段测试矩阵。

覆盖：
  1) 运算符白名单（add/sub/mul/div）+ 类型矩阵（整数/浮点/文本数字/空值/非数值/0）
  2) 除法语义（正常 / 除零->NULL / 分子为空 / 分母为空 / 分母非数值）
  3) schema 校验与注入拒绝（左右列不存在/表达式/operator 注入）
  4) 组合：计算 + WHERE / GROUP BY / ORDER BY / TOP-N
  5) 两阶段：Step 1 计算 → Step 2 聚合；超步数/嵌套/raw expression 一律拒绝
  6) 独立 Ground Truth（本文件手写 扫描→逐行计算→筛选→分组→排序→TOP-N→再聚合）
  7) SQL 安全（表达式形态白名单、参数绑定、只 SELECT）
  8) 双引擎差分（DuckDB vs Python 参考实现）
  9) 性能记录（19 行 / 447 行）
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from backend.excel import aggregate as agg
from backend.excel import calculation as calc_mod
from backend.excel import engine as eng
from backend.excel import multi_step as ms
from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from backend.excel.parser import ExcelParser
from backend.excel.query import ExcelQueryError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMALL_REAL = PROJECT_ROOT / '直邮一店 8.20号订单.xlsx'
DATA_ROOT = PROJECT_ROOT / 'data' / 'excel'
TOL = dict(rel=1e-9, abs=1e-6)

AMOUNT, QTY, SKU, CARRIER = 'Order Amount', 'Quantity', 'SKU ID', 'Shipping Provider Name'
DIV = {'operation': 'div', 'left_column': AMOUNT, 'right_column': QTY}
MUL = {'operation': 'mul', 'left_column': AMOUNT, 'right_column': QTY}


# ==========================================================================
# 夹具
# ==========================================================================
@pytest.fixture(scope='module')
def small_rep():
    if not SMALL_REAL.exists():
        pytest.skip('未找到小表（直邮一店 8.20号订单.xlsx）')
    return ExcelParser().parse(str(SMALL_REAL), 'calc-small', user_id=1, filename=SMALL_REAL.name)


@pytest.fixture(scope='module')
def big_rep():
    if not DATA_ROOT.exists():
        pytest.skip('没有 data/excel 目录')
    best = None
    for d in DATA_ROOT.iterdir():
        if not (d / 'representation.json').exists():
            continue
        rep = excel_store.load_representation(d.name)
        if rep is None:
            continue
        if rep.total_rows >= 100 and (best is None or rep.total_rows > best.total_rows):
            best = rep
    if best is None:
        pytest.skip('未找到 ≥100 行的真实表格')
    return best


def _col(rep, name: str) -> int:
    return rep.sheets[0].column_names.index(name)


# ==========================================================================
# 1. 独立 Ground Truth（手写，不 import 被测实现）
# ==========================================================================
def _f(v: Any) -> Optional[float]:
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


def gt_calc_row(op: str, left: Any, right: Any) -> Optional[float]:
    a, b = _f(left), _f(right)
    if a is None or b is None:
        return None
    if op == 'add':
        return a + b
    if op == 'sub':
        return a - b
    if op == 'mul':
        return a * b
    if b == 0:
        return None
    return a / b


def gt_rows(rep, op: str, left: str, right: str, sheet_index: int = 0) -> List[Optional[float]]:
    sheet = rep.sheets[sheet_index]
    li, ri = _col(rep, left), _col(rep, right)
    return [gt_calc_row(op, r[li] if li < len(r) else None,
                        r[ri] if ri < len(r) else None) for r in sheet.rows]


def _key_sort(key: Tuple[Any, ...]) -> Tuple[Any, ...]:
    return tuple((1, '') if k is None else (0, str(k)) for k in key)


def gt_group(rep, op: str, left: str, right: str, group_by: List[str],
             agg_op: str, order_dir: str = 'desc', top_n: Optional[int] = None,
             sheet_index: int = 0) -> Dict[str, Any]:
    """独立实现：逐行计算 → 分组 → 聚合 → 排序(+tie-breaker) → TOP-N。"""
    sheet = rep.sheets[sheet_index]
    names = sheet.column_names
    gi = [names.index(g) for g in group_by]
    li, ri = names.index(left), names.index(right)
    buckets: Dict[Tuple[Any, ...], List[float]] = {}
    for r in sheet.rows:
        v = gt_calc_row(op, r[li] if li < len(r) else None, r[ri] if ri < len(r) else None)
        if v is None:
            continue
        key = tuple(r[i] if i < len(r) else None for i in gi)
        buckets.setdefault(key, []).append(v)

    entries: List[Tuple[Tuple[Any, ...], Optional[float]]] = []
    for key, nums in buckets.items():
        if agg_op == 'sum':
            entries.append((key, float(sum(nums))))
        elif agg_op == 'avg':
            entries.append((key, float(sum(nums) / len(nums))))
        elif agg_op == 'min':
            entries.append((key, float(min(nums))))
        elif agg_op == 'max':
            entries.append((key, float(max(nums))))
        else:
            entries.append((key, float(len(nums))))
    entries.sort(key=lambda e: _key_sort(e[0]))
    entries.sort(key=lambda e: e[1], reverse=(order_dir == 'desc'))
    total = len(entries)
    return {'entries': entries[:top_n] if top_n else entries, 'total_groups': total}


# ==========================================================================
# 2. 运算符与类型矩阵（行级）
# ==========================================================================
def _row_payload(calc: Dict[str, Any], **kw) -> Dict[str, Any]:
    return {'sheet_index': 0, 'calculation': calc, 'limit': 500, **kw}


@pytest.mark.parametrize('op', ['add', 'sub', 'mul', 'div'])
def test_row_calc_operator_matrix(small_rep, op):
    """四种运算符：两种引擎的计算值逐行等于独立 GT。"""
    calc = {'operation': op, 'left_column': AMOUNT, 'right_column': QTY}
    expected = gt_rows(small_rep, op, AMOUNT, QTY)
    for engine in ('duckdb', 'python'):
        result, used = eng.run_structured_query(small_rep, _row_payload(calc), engine=engine)
        assert used == engine
        assert len(result.rows) == len(expected)
        assert [r[-1] for r in result.rows] == [pytest.approx(v, **TOL) if v is not None else None
                                                for v in expected]
        assert result.calculation['operation'] == op
        assert result.columns[-1]['is_calculated'] is True


@pytest.mark.parametrize('op,left,right,expected', [
    ('div', '10', '4', 2.5),          # 文本数字
    ('div', 10.0, 4, 2.5),            # 浮点
    ('div', 10, 4, 2.5),              # 整数
    ('div', '10', '0', None),         # 除零 -> NULL
    ('div', None, '4', None),         # 分子为空
    ('div', '10', None, None),        # 分母为空
    ('div', '10', 'abc', None),       # 分母非数值
    ('div', 'abc', '4', None),        # 分子非数值
    ('mul', 'abc', '10', None),       # 非数值乘法 -> NULL（不当作 0）
    ('add', None, '5', None),
    ('sub', '', '5', None),           # 空字符串 -> NULL
    ('mul', '2', '3', 6.0),
])
def test_calc_value_semantics(op, left, right, expected):
    got = gt_calc_row(op, left, right)
    assert (got is None) == (expected is None)
    if expected is not None:
        assert got == pytest.approx(expected, **TOL)
    # 与被测实现（Python 参考）一致
    from backend.excel.calculation import Calculation

    obj = Calculation(operation=op, left_column='L', right_column='R', left_index=0, right_index=1)
    assert calc_mod.calc_python_value(obj, [left, right]) == \
        (pytest.approx(expected, **TOL) if expected is not None else None)


def _fresh_small(tag: str):
    """重新解析一份独立 representation（独立的 DuckDB 表名，避免命中已物化缓存）。"""
    if not SMALL_REAL.exists():
        pytest.skip('未找到小表')
    return ExcelParser().parse(str(SMALL_REAL), f'calc-{tag}', user_id=1, filename=SMALL_REAL.name)


def test_calc_div_zero_and_null_in_real_data():
    """真实数据上人为制造除零/空值/非数值：两引擎都必须给出 NULL，且不报错。"""
    rep = _fresh_small('divzero')
    sheet = rep.sheets[0]
    qi = _col(rep, QTY)
    sheet.rows[0][qi] = '0'
    sheet.rows[1][qi] = None
    sheet.rows[2][qi] = 'abc'
    for engine in ('duckdb', 'python'):
        result, used = eng.run_structured_query(rep, _row_payload(DIV), engine=engine)
        values = [r[-1] for r in result.rows]
        assert values[0] is None and values[1] is None and values[2] is None
        assert values[3] is not None


def test_calc_null_rows_not_matched_by_range_filter():
    """计算值为 NULL 的行不匹配 > / < / neq 比较（既不当作 0，也不报错）。"""
    rep = _fresh_small('nullfilter')
    sheet = rep.sheets[0]
    qi = _col(rep, QTY)
    sheet.rows[0][qi] = '0'
    for operator, value, expected in [('gt', -1, 18), ('lt', 10 ** 9, 18), ('neq', 999, 18)]:
        payload = _row_payload(DIV, calc_filter={'operator': operator, 'value': value})
        for engine in ('duckdb', 'python'):
            result, _ = eng.run_structured_query(rep, payload, engine=engine)
            assert result.total_matches == expected


# ==========================================================================
# 3. schema 校验与注入拒绝
# ==========================================================================
@pytest.mark.parametrize('calc,label', [
    ({'operation': 'pow', 'left_column': AMOUNT, 'right_column': QTY}, 'pow 不在白名单'),
    ({'operation': 'mod', 'left_column': AMOUNT, 'right_column': QTY}, 'mod 不在白名单'),
    ({'operation': 'div', 'left_column': AMOUNT, 'right_column': QTY, 'extra': 1}, '多余字段无害但操作合法'),
    ({'operation': 'div; DROP TABLE t', 'left_column': AMOUNT, 'right_column': QTY}, 'operator 注入'),
    ({'operation': 'div', 'left_column': 'Order Amount) / (1); DROP TABLE t',
      'right_column': QTY}, '左列表达式注入'),
    ({'operation': 'div', 'left_column': AMOUNT, 'right_column': '1); DROP TABLE t'}, '右列表达式注入'),
    ({'operation': 'div', 'left_column': 'Order Amount / Quantity', 'right_column': QTY},
     '整段表达式塞进列名'),
    ({'operation': 'div', 'left_column': AMOUNT, 'right_column': '不存在的列'}, '右列不存在'),
    ({'operation': 'div', 'left_column': '', 'right_column': QTY}, '空列名'),
    ({'operation': 'div', 'left_column': AMOUNT}, '缺右列'),
    ({'operation': 'div', 'left_column': 'x' * 200, 'right_column': QTY}, '超长列名'),
])
def test_row_calc_injection_rejected(small_rep, calc, label):
    if label == '多余字段无害但操作合法':
        # 合法：多余字段被忽略（只取白名单三字段）
        result, _ = eng.run_structured_query(small_rep, _row_payload(calc), engine='duckdb')
        assert result.total_matches == 19
        return
    with pytest.raises(ExcelQueryError):
        eng.run_structured_query(small_rep, _row_payload(calc), engine='duckdb')


def test_row_calc_filter_injection_rejected(small_rep):
    for bad in [
        {'operator': 'contains', 'value': 'x'},
        {'operator': 'gt', 'value': '10; DROP TABLE t'},
        {'operator': 'gt'},
        {'operator': 'in', 'value': '1,2'},
    ]:
        with pytest.raises(ExcelQueryError):
            eng.run_structured_query(
                small_rep, _row_payload(DIV, calc_filter=bad), engine='duckdb')


def test_calc_filter_without_calculation_rejected(small_rep):
    with pytest.raises(ExcelQueryError):
        eng.run_structured_query(
            small_rep,
            {'sheet_index': 0, 'calc_filter': {'operator': 'gt', 'value': 1}},
            engine='duckdb')


def test_calc_sql_expression_shape(small_rep):
    """表达式的形态白名单：只允许 TRY_CAST 物理列 / NULLIF(..,0) / 四个运算符。"""
    sheet = small_rep.sheets[0]
    calc = calc_mod.resolve_calculation(sheet, calc_mod.normalize_calculation(DIV))
    expr = calc_mod.calc_sql_expr(calc)
    assert expr.startswith('TRY_CAST("c') and 'NULLIF(' in expr and ' / ' in expr
    calc_mod.assert_calc_expr_safe(expr)
    for bad in ['1=1', 'TRY_CAST("c1" AS DOUBLE); DROP TABLE t', 'random()',
                'TRY_CAST("c1" AS DOUBLE) + (SELECT 1)']:
        with pytest.raises(ExcelQueryError):
            calc_mod.assert_calc_expr_safe(bad)


# ==========================================================================
# 4. 组合：计算 + WHERE / GROUP BY / ORDER BY / TOP-N
# ==========================================================================
def test_calc_plus_where(small_rep):
    """计算字段 + 普通筛选（AND 语义）：两引擎与 GT 一致。"""
    payload = _row_payload(
        DIV, calc_filter={'operator': 'gt', 'value': 10},
        filters=[{'column': CARRIER, 'operator': 'contains', 'value': 'SF'}])
    expected = [v for r, v in zip(small_rep.sheets[0].rows, gt_rows(small_rep, 'div', AMOUNT, QTY))
                if v is not None and v > 10 and 'SF' in str(r[_col(small_rep, CARRIER)])]
    for engine in ('duckdb', 'python'):
        result, _ = eng.run_structured_query(small_rep, payload, engine=engine)
        assert result.total_matches == len(expected) == 2
        # 顺序 = representation 原始顺序（引擎一致），与 GT 按值集合比对
        assert sorted(r[-1] for r in result.rows) == \
               [pytest.approx(v, **TOL) for v in sorted(expected)]
        assert any(f.get('is_calculated') for f in result.applied_filters)


@pytest.mark.parametrize('agg_op', ['sum', 'avg', 'min', 'max'])
def test_calc_plus_group(small_rep, agg_op):
    """计算 + GROUP BY：逐组与独立 GT 一致（按 group_key 映射比对）。"""
    payload = {'sheet_index': 0, 'operation': agg_op, 'calculation': DIV,
               'group_by': [SKU]}
    gt = gt_group(small_rep, 'div', AMOUNT, QTY, [SKU], agg_op)
    for engine in ('duckdb', 'python'):
        result, used = eng.run_aggregate(small_rep, payload, engine=engine)
        got = {r.key(): r.value for r in result.rows}
        exp = {k: v for k, v in gt['entries']}
        assert set(got) == set(exp)
        for k, v in exp.items():
            assert got[k] == pytest.approx(v, **TOL)
        assert result.total_groups == gt['total_groups']
        assert result.calculation['operation'] == 'div'


def test_calc_plus_group_order_topn(small_rep):
    """计算 + GROUP BY + ORDER BY + TOP-N（顺序敏感，逐位与 GT 比对）。"""
    payload = {'sheet_index': 0, 'operation': 'avg', 'calculation': DIV, 'group_by': [SKU],
               'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 3}
    gt = gt_group(small_rep, 'div', AMOUNT, QTY, [SKU], 'avg', 'desc', 3)
    for engine in ('duckdb', 'python'):
        result, _ = eng.run_aggregate(small_rep, payload, engine=engine)
        assert [r.key() for r in result.rows] == [k for k, _ in gt['entries']]
        assert [r.value for r in result.rows] == \
               [pytest.approx(v, **TOL) for _, v in gt['entries']]
        assert result.total_groups == gt['total_groups'] == 15
        assert result.returned_groups == 3


def test_calc_mul_group_order_desc(small_rep):
    payload = {'sheet_index': 0, 'operation': 'sum', 'calculation': MUL,
               'group_by': [CARRIER], 'order_by': 'aggregate_value', 'order_dir': 'desc'}
    gt = gt_group(small_rep, 'mul', AMOUNT, QTY, [CARRIER], 'sum', 'desc')
    for engine in ('duckdb', 'python'):
        result, _ = eng.run_aggregate(small_rep, payload, engine=engine)
        assert [r.key() for r in result.rows] == [k for k, _ in gt['entries']]
        assert [r.value for r in result.rows] == \
               [pytest.approx(v, **TOL) for _, v in gt['entries']]


def test_calc_group_zero_denominator_excluded(small_rep):
    """分组聚合时，除零行被 NULL 语义排除（不当作 0），且不报错。"""
    sheet = small_rep.sheets[0]
    qi = _col(small_rep, QTY)
    ai = _col(small_rep, AMOUNT)
    original = [list(r) for r in sheet.rows]
    try:
        sheet.rows[0][qi] = '0'          # 除零 -> NULL -> 不计入
        sheet.rows[0][ai] = '1000'
        payload = {'sheet_index': 0, 'operation': 'sum', 'calculation': DIV, 'group_by': [SKU]}
        for engine in ('duckdb', 'python'):
            result, _ = eng.run_aggregate(small_rep, payload, engine=engine)
            keys = [r.key() for r in result.rows]
            zero_sku = tuple([original[0][_col(small_rep, SKU)]])
            if zero_sku in keys:
                row = result.rows[keys.index(zero_sku)]
                assert row.value != pytest.approx(1000.0)   # 绝不把除零当 0 参与求和
    finally:
        for i, row in enumerate(original):
            sheet.rows[i][:] = row
        from backend.excel import duck as duck_engine

        duck_engine.reset_registry()


def test_count_with_calculation_rejected(small_rep):
    with pytest.raises(ExcelQueryError):
        eng.run_aggregate(small_rep,
                          {'sheet_index': 0, 'operation': 'count', 'calculation': DIV},
                          engine='duckdb')


def test_column_and_calculation_conflict_rejected(small_rep):
    with pytest.raises(ExcelQueryError):
        eng.run_aggregate(small_rep,
                          {'sheet_index': 0, 'operation': 'sum', 'column': AMOUNT,
                           'calculation': DIV}, engine='duckdb')


# ==========================================================================
# 5. 两阶段：Step 1 计算 → Step 2 聚合
# ==========================================================================
def _plan(steps):
    return ms.normalize_analysis_plan({'steps': steps})


def test_multi_step_with_calculation(small_rep):
    """核心：Step1 = AVG(A÷Q) 按 SKU + TOP-5；Step2 = 对这 5 个平均单价再平均。"""
    steps = [{'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'avg',
              'calculation': DIV, 'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 5},
             {'type': 'aggregate', 'operation': 'avg', 'source': 'step_1',
              'column': 'aggregate_value'}]
    for engine in ('duckdb', 'python'):
        result, used = ms.execute_analysis(small_rep, _plan(steps), sheet_index=0, engine=engine)
        gt = gt_group(small_rep, 'div', AMOUNT, QTY, [SKU], 'avg', 'desc', 5)
        assert used == engine
        assert [r.key() for r in result.step1.rows] == [k for k, _ in gt['entries']]
        assert [r.value for r in result.step1.rows] == \
               [pytest.approx(v, **TOL) for _, v in gt['entries']]
        assert result.step2_value == \
            pytest.approx(sum(v for _, v in gt['entries']) / 5, **TOL)
        assert result.step1.calculation['operation'] == 'div'


def test_multi_step_sum_of_topn_calc(small_rep):
    """Step1 = SUM(A×Q) 按 SKU + TOP-10；Step2 = SUM（与全表对照必须不同）。"""
    steps = [{'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'sum',
              'calculation': MUL, 'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 10},
             {'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
              'column': 'aggregate_value'}]
    result, _ = ms.execute_analysis(small_rep, _plan(steps), sheet_index=0)
    gt = gt_group(small_rep, 'mul', AMOUNT, QTY, [SKU], 'sum', 'desc', 10)
    assert result.step2_value == pytest.approx(sum(v for _, v in gt['entries']), **TOL)
    full = gt_group(small_rep, 'mul', AMOUNT, QTY, [SKU], 'sum', 'desc', None)
    assert result.step2_value != pytest.approx(sum(v for _, v in full['entries']), **TOL)


def test_multi_step_calculation_rejected_in_step2():
    with pytest.raises(ExcelQueryError):
        _plan([{'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'sum',
                'calculation': DIV, 'order_by': 'aggregate_value', 'order_dir': 'desc',
                'top_n': 5},
               {'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
                'column': 'aggregate_value', 'calculation': DIV}])


def test_multi_step_calculation_conflicts_with_column():
    with pytest.raises(ExcelQueryError):
        _plan([{'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'sum',
                'column': AMOUNT, 'calculation': DIV, 'order_by': 'aggregate_value',
                'order_dir': 'desc', 'top_n': 5},
               {'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
                'column': 'aggregate_value'}])


def test_multi_step_three_steps_with_calc_rejected():
    with pytest.raises(ExcelQueryError):
        _plan([{'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'sum',
                'calculation': DIV, 'order_by': 'aggregate_value', 'order_dir': 'desc',
                'top_n': 5},
               {'type': 'aggregate', 'operation': 'avg', 'source': 'step_1',
                'column': 'aggregate_value'},
               {'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
                'column': 'aggregate_value'}])


def test_nested_calculation_rejected(small_rep):
    """嵌套/表达式型计算（(A/B)+C、A/B+C-D）必须拒绝：只允许两个真实列。

    注意：计算对象里多出来的**未知键**会被忽略（不是表达式），
    真正被拒绝的是"把表达式塞进列名"或"给不存在的运算符"。
    """
    for bad in [
        {'operation': 'add', 'left_column': 'Order Amount / Quantity',
         'right_column': 'Shipping Fee After Discount'},          # A/B 当左列
        {'operation': 'add', 'left_column': 'Order Amount + Quantity',
         'right_column': 'Taxes'},                                 # A+B 当左列
        {'operation': 'div', 'left_column': '(Order Amount + Taxes)',
         'right_column': QTY},                                     # 括号表达式
    ]:
        with pytest.raises(ExcelQueryError):
            eng.run_aggregate(small_rep, {'sheet_index': 0, 'operation': 'sum',
                                          'calculation': bad}, engine='duckdb')


# ==========================================================================
# 6. NL 路由（计算字段意图）
# ==========================================================================
class FakeLLM:
    def __init__(self, turn=None, analysis=None, stat=None):
        self.turn, self.analysis, self.stat = turn, analysis, stat
        self.calls: List[str] = []

    async def chat_completion(self, messages, stream=False, temperature=None):
        system = messages[0]['content']
        if system.startswith('你是一个「统计参数抽取器」'):
            kind = 'stat'
        elif system.startswith('你是一个「两阶段分析计划生成器」'):
            kind = 'analysis'
        else:
            kind = 'turn'
        self.calls.append(kind)
        payload = {'turn': self.turn, 'analysis': self.analysis, 'stat': self.stat}[kind]
        if payload is None:
            raise RuntimeError(f'no payload: {kind}')
        assert temperature == 0.0
        yield json.dumps(payload, ensure_ascii=False)


def _catalog(rep):
    return [{'document_id': rep.document_id, 'filename': rep.filename,
             'file_type': rep.file_type, 'created_at': rep.created_at,
             'total_rows': rep.total_rows,
             'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                         'row_count': s.row_count, 'column_count': s.column_count,
                         'columns': s.column_names} for s in rep.sheets]}]


def _nl(rep, message, llm, **kw):
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nl.run_nl_query(message, _catalog(rep), llm=llm, **kw))
    finally:
        excel_store.load_representation = original


def test_nl_row_calculation(small_rep):
    out = _nl(small_rep, '每笔订单的单价是多少？', FakeLLM(turn={
        'action': 'new_query', 'document': '一店', 'columns': [AMOUNT, QTY],
        'calculation': DIV}))
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_structured_query'
    assert out['result']['calculation']['label'] == 'Order Amount ÷ Quantity'
    assert out['result']['columns'][-1]['is_calculated'] is True
    assert '计算字段' in out['message']


def test_nl_calc_filter(small_rep):
    out = _nl(small_rep, '找出单价大于10的订单有哪些？', FakeLLM(turn={
        'action': 'new_query', 'calculation': DIV,
        'filters': [{'column': 'calculated_value', 'operator': 'gt', 'value': 10}]}))
    assert out['status'] == 'ok'
    assert out['result']['total_matches'] == 14
    assert out['result']['calc_filter'] == {'operator': 'gt', 'value': 10.0}
    assert '计算值筛选' in out['message']


def test_nl_calc_plus_normal_filter(small_rep):
    out = _nl(small_rep, 'SF物流中单价大于10的订单', FakeLLM(turn={
        'action': 'new_query', 'calculation': DIV,
        'filters': [{'column': CARRIER, 'operator': 'contains', 'value': 'SF'},
                    {'column': 'calculated_value', 'operator': 'gt', 'value': 10}]}))
    assert out['status'] == 'ok'
    assert out['result']['total_matches'] == 2


def test_nl_calc_group_explicit_per_row(small_rep):
    out = _nl(small_rep, '按SKU统计每笔订单的单价平均值', FakeLLM(turn={
        'action': 'aggregate', 'aggregate_operation': 'avg', 'group_by': [SKU],
        'calculation': DIV, 'document': '一店'}))
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_group_aggregate'
    assert out['group_aggregate']['calculation']['operation'] == 'div'
    assert out['group_aggregate']['total_groups'] == 15


@pytest.mark.parametrize('message', ['按SKU统计平均单价', '单价最高的10个SKU'])
def test_nl_calc_ambiguous_clarifies(small_rep, message):
    """口径歧义（AVG(A/B) vs SUM(A)/SUM(B)）必须澄清，不得擅自选择。"""
    out = _nl(small_rep, message, FakeLLM(turn={
        'action': 'aggregate', 'aggregate_operation': 'avg', 'group_by': [SKU],
        'calculation': DIV, 'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 10}))
    assert out['status'] == 'clarify'
    assert 'AVG' in out['message'] and 'SUM' in out['message']


def test_nl_ambiguous_followup_per_row_executes(small_rep):
    """用户补充「每笔」后同一问题可执行（澄清是可恢复的）。"""
    out = _nl(small_rep, '按SKU统计每笔订单单价的平均值', FakeLLM(turn={
        'action': 'aggregate', 'aggregate_operation': 'avg', 'group_by': [SKU],
        'calculation': DIV, 'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 10}))
    assert out['status'] == 'ok'
    assert len(out['group_aggregate']['rows']) == 10


def test_nl_direct_column_not_treated_as_calculation(small_rep):
    out = _nl(small_rep, '订单金额是多少？', FakeLLM(turn={
        'action': 'new_query', 'columns': [AMOUNT], 'document': '一店'}))
    assert out['status'] == 'ok'
    assert out['result']['calculation'] is None
    assert out['result']['columns'][0]['name'] == AMOUNT


@pytest.mark.parametrize('bad_calc', [
    {'operation': 'div; DROP TABLE t', 'left_column': AMOUNT, 'right_column': QTY},
    {'operation': 'pow', 'left_column': AMOUNT, 'right_column': QTY},
])
def test_nl_invalid_calculation_clarifies_not_silently_dropped(small_rep, bad_calc):
    """非法计算字段**绝不能**被静默丢弃后当成普通查询执行。"""
    out = _nl(small_rep, '每笔订单的单价是多少？', FakeLLM(turn={
        'action': 'new_query', 'document': '一店', 'calculation': bad_calc}))
    assert out['status'] == 'clarify'
    assert '计算字段不合法' in out['message']


def test_nl_calc_column_not_found_clarifies(small_rep):
    out = _nl(small_rep, '每笔订单的单价是多少？', FakeLLM(turn={
        'action': 'new_query', 'document': '一店',
        'calculation': {'operation': 'div', 'left_column': '不存在的列', 'right_column': QTY}}))
    assert out['status'] in ('clarify', 'error')


def test_nl_calc_filter_without_calculation(small_rep):
    out = _nl(small_rep, '找出计算值大于10的订单', FakeLLM(turn={
        'action': 'new_query', 'document': '一店',
        'filters': [{'column': 'calculated_value', 'operator': 'gt', 'value': 10}]}))
    assert out['status'] in ('clarify', 'error')


def test_nl_row_calc_not_hijacked_by_statistical_guard(small_rep):
    """行级计算问题（"…除以数量是多少？"）**不得**被统计兜底改写成统计查询。"""
    out = _nl(small_rep, '一店每笔订单的金额除以数量是多少？', FakeLLM(
        turn={'action': 'new_query', 'document': '一店',
              'columns': [AMOUNT, QTY], 'calculation': DIV},
        stat={'aggregate_operation': 'count', 'column': None, 'group_by': []}))
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_structured_query'
    assert out['result']['calculation']['operation'] == 'div'
    assert out['result']['total_matches'] == 19


def test_nl_overplanned_analysis_downgraded_to_single_step(small_rep):
    """LLM 把"分组+排序+TOP-N"过度规划成两步时，降级为单步分组统计（不能压成一个数字）。"""
    turn = {'action': 'analysis', 'document': '一店', 'steps': [
        {'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'avg',
         'calculation': DIV, 'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 5},
        {'type': 'aggregate', 'operation': 'avg', 'source': 'step_1',
         'column': 'aggregate_value'}]}
    out = _nl(small_rep, '按SKU统计每笔订单的单价平均值，从高到低排列，取前5个',
              FakeLLM(turn=turn))
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_group_aggregate'
    assert out.get('analysis_downgraded') is True
    g = out['group_aggregate']
    assert g['top_n'] == 5 and g['returned_groups'] == 5
    assert g['calculation']['operation'] == 'div'


def test_nl_real_multi_step_not_downgraded(small_rep):
    """真正的两步问题（有"并统计…总…"语义）不得被降级。"""
    turn = {'action': 'analysis', 'document': '一店', 'steps': [
        {'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'sum',
         'column': AMOUNT, 'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 10},
        {'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
         'column': 'aggregate_value'}]}
    out = _nl(small_rep, '找出金额最高的10个SKU，并统计它们的总销售额', FakeLLM(turn=turn))
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_analysis'
    assert not out.get('analysis_downgraded')


def test_nl_multi_step_with_calculation(small_rep):
    out = _nl(small_rep, '找出每笔订单单价平均值最高的5个SKU，并计算这5个SKU的单价平均值合计',
              FakeLLM(turn={'action': 'analysis', 'document': '一店', 'steps': [
                  {'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'avg',
                   'calculation': DIV, 'order_by': 'aggregate_value', 'order_dir': 'desc',
                   'top_n': 5},
                  {'type': 'aggregate', 'operation': 'avg', 'source': 'step_1',
                   'column': 'aggregate_value'}]}))
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_analysis'
    m = out['multi_step']
    assert m['step1']['calculation']['operation'] == 'div'
    gt = gt_group(small_rep, 'div', AMOUNT, QTY, [SKU], 'avg', 'desc', 5)
    assert [r['value'] for r in m['step1']['rows']] == \
           [pytest.approx(v, **TOL) for _, v in gt['entries']]
    assert m['value'] == pytest.approx(sum(v for _, v in gt['entries']) / 5, **TOL)


# ==========================================================================
# 7. 大表（447 行）+ 性能记录
# ==========================================================================
def test_big_table_row_calculation(big_rep):
    result, used = eng.run_structured_query(
        big_rep, _row_payload(DIV, limit=500), engine='duckdb')
    expected = gt_rows(big_rep, 'div', AMOUNT, QTY)
    assert used == 'duckdb'
    assert result.total_matches == len(expected)
    got = [r[-1] for r in result.rows]
    assert got == [pytest.approx(v, **TOL) if v is not None else None for v in expected[:len(got)]]


def test_big_table_group_calculation(big_rep):
    payload = {'sheet_index': 0, 'operation': 'sum', 'calculation': MUL,
               'group_by': [SKU], 'order_by': 'aggregate_value', 'order_dir': 'desc',
               'top_n': 5}
    gt = gt_group(big_rep, 'mul', AMOUNT, QTY, [SKU], 'sum', 'desc', 5)
    result, _ = eng.run_aggregate(big_rep, payload, engine='duckdb')
    assert [r.key() for r in result.rows] == [k for k, _ in gt['entries']]
    assert [r.value for r in result.rows] == [pytest.approx(v, **TOL) for _, v in gt['entries']]


def test_performance_small_and_big(small_rep, big_rep):
    """记录首次/重复耗时（只做记录，不做优化；断言宽松以防抖动）。"""
    def timed(rep, payload, times=3):
        eng.run_structured_query(rep, payload, engine='duckdb')       # 预热（含建表）
        t0 = time.perf_counter()
        for _ in range(times):
            eng.run_structured_query(rep, payload, engine='duckdb')
        return (time.perf_counter() - t0) / times

    row_payload = _row_payload(DIV, limit=500)
    group_payload = {'sheet_index': 0, 'operation': 'sum', 'calculation': MUL,
                     'group_by': [SKU], 'order_by': 'aggregate_value', 'order_dir': 'desc',
                     'top_n': 10}
    s_row = timed(small_rep, row_payload)
    s_grp = timed(small_rep, group_payload)
    b_row = timed(big_rep, row_payload)
    b_grp = timed(big_rep, group_payload)
    print(f'\n[perf] 19 行 行级计算 {s_row*1000:.1f}ms / 分组计算 {s_grp*1000:.1f}ms'
          f'｜{big_rep.total_rows} 行 行级计算 {b_row*1000:.1f}ms / 分组计算 {b_grp*1000:.1f}ms')
    assert s_row < 5 and s_grp < 5 and b_row < 10 and b_grp < 10
