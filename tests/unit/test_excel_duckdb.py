# -*- coding: utf-8 -*-
"""Phase 2：Excel 精确查询 + 筛选（DuckDB 引擎）测试矩阵

三方交叉验证（关键）：
  A. DuckDB 引擎       backend/excel/duck.py
  B. Python 参考引擎   backend/excel/query.py
  C. 本文件内的独立扫描 _scan_ground_truth()（不 import 上面两者，纯手写语义）

任何一个 payload 都必须满足：A == B == C（逐行、逐值、逐 Excel 行号）。
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.excel import duck as duck_engine
from backend.excel import engine as query_engine
from backend.excel import query as q
from backend.excel import store as excel_store
from backend.excel.parser import ExcelParser

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMALL_REAL = PROJECT_ROOT / '直邮一店 8.20号订单.xlsx'
BIG_CANDIDATES = [
    PROJECT_ROOT / '直邮5店 7.10号订单.xlsx',
]
OP_ALL = ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'contains']


# ==========================================================================
# 独立 Ground Truth（手写语义，不依赖被测代码）
# ==========================================================================
def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
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
        if cell is None:
            return False
        return not _match(cell, 'eq', value)
    if op == 'contains':
        return cell is not None and value is not None and str(value).lower() in str(cell).lower()
    a, b = _num(cell), _num(value)
    if a is None or b is None:
        return False
    if op == 'gt':
        return a > b
    if op == 'gte':
        return a >= b
    if op == 'lt':
        return a < b
    if op == 'lte':
        return a <= b
    raise AssertionError(op)


def _resolve_index(names: List[str], name: str) -> int:
    """独立列名解析（精确 → 大小写不敏感），与引擎的解析规则一致。"""
    if name in names:
        return names.index(name)
    ci = [i for i, n in enumerate(names) if n.lower() == name.lower()]
    assert len(ci) == 1, f'Ground Truth 无法唯一解析列名 {name!r}（候选 {ci}）'
    return ci[0]


def _scan_ground_truth(rep, payload: Dict[str, Any]) -> Tuple[List[List[Any]], List[int], int]:
    """独立扫描 representation，返回 (投影行, Excel 行号, 命中总数)。"""
    sheet = rep.sheets[payload.get('sheet_index') or 0]
    names = sheet.column_names
    cols = payload.get('columns') or names
    # 投影列去重（保留首次出现顺序），与引擎的 documented 行为一致
    idx: List[int] = []
    for c in cols:
        i = _resolve_index(names, c)
        if i not in idx:
            idx.append(i)

    matched: List[Tuple[List[Any], int]] = []
    for i, row in enumerate(sheet.rows):
        ok = True
        for f in payload.get('filters') or []:
            ci = _resolve_index(names, f['column'])
            if not _match(row[ci] if ci < len(row) else None, f['operator'], f.get('value')):
                ok = False
                break
        if ok:
            matched.append(([row[j] for j in idx], sheet.row_excel_numbers[i]))

    total = len(matched)
    off = int(payload.get('offset') or 0)
    lim = int(payload.get('limit') or q.DEFAULT_LIMIT)
    page = matched[off:off + lim]
    return [p[0] for p in page], [p[1] for p in page], total


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
    return ExcelParser().parse(str(p), 'duck-big', user_id=1, filename=p.name)


@pytest.fixture(scope='module')
def small_rep():
    if not SMALL_REAL.exists():
        pytest.skip('未找到小表（直邮一店 8.20号订单.xlsx）')
    return ExcelParser().parse(str(SMALL_REAL), 'duck-small', user_id=1, filename=SMALL_REAL.name)


@pytest.fixture(autouse=True)
def _clean_registry():
    duck_engine.reset_registry()
    yield
    duck_engine.reset_registry()


def _run(rep, payload: Dict[str, Any], engine: str = query_engine.ENGINE_AUTO):
    return query_engine.run_structured_query(rep, payload, engine=engine)


def _three_way(rep, payload: Dict[str, Any], label: str = '') -> Dict[str, Any]:
    """A(DuckDB) == B(Python) == C(独立扫描)。返回 duckdb 结果。"""
    exp_rows, exp_nums, exp_total = _scan_ground_truth(rep, payload)

    res_py, eng_py = _run(rep, payload, query_engine.ENGINE_PYTHON)
    assert eng_py == 'python'
    assert res_py.rows == exp_rows, f'{label}: Python 引擎行内容 != Ground Truth'
    assert res_py.row_excel_numbers == exp_nums, f'{label}: Python 引擎 Excel 行号 != Ground Truth'
    assert res_py.total_matches == exp_total, f'{label}: Python 引擎命中数 != Ground Truth'

    res_dk, eng_dk = _run(rep, payload, query_engine.ENGINE_DUCKDB)
    assert eng_dk == 'duckdb'
    assert res_dk.total_matches == exp_total, f'{label}: DuckDB 命中数 {res_dk.total_matches} != GT {exp_total}'
    assert res_dk.rows == exp_rows, f'{label}: DuckDB 行内容 != Ground Truth'
    assert res_dk.row_excel_numbers == exp_nums, f'{label}: DuckDB Excel 行号 != Ground Truth'
    assert res_dk.returned_count == len(exp_rows)
    return res_dk


# ==========================================================================
# 1. 引擎可用性
# ==========================================================================
def test_duckdb_available():
    assert duck_engine.DUCKDB_AVAILABLE, 'Phase 2 需要 duckdb'
    st = query_engine.engine_status()
    assert st['duckdb_available'] is True
    assert st['duckdb_version']


def test_engine_auto_prefers_duckdb(small_rep):
    _res, used = _run(small_rep, {'sheet_index': 0, 'columns': ['Order ID'], 'limit': 1})
    assert used == 'duckdb'


def test_invalid_engine_rejected(small_rep):
    with pytest.raises(q.ExcelQueryError) as ei:
        _run(small_rep, {'sheet_index': 0}, engine='mysql')
    assert ei.value.code == q.ERR_INVALID_PARAM


# ==========================================================================
# 2. operator 矩阵（小表 19 行 + 大表 447 行）
# ==========================================================================
def test_operator_matrix_small(small_rep):
    cases = [
        ('eq 物流商精确', {'filters': [{'column': 'Shipping Provider Name', 'operator': 'eq', 'value': 'SF International'}]}),
        ('neq 物流商', {'filters': [{'column': 'Shipping Provider Name', 'operator': 'neq', 'value': 'SF International'}]}),
        ('contains 物流商SF', {'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]}),
        ('contains 小写sf', {'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'sf'}]}),
        ('gt 数量>1', {'filters': [{'column': 'Quantity', 'operator': 'gt', 'value': 1}]}),
        ('gte 数量>=2', {'filters': [{'column': 'Quantity', 'operator': 'gte', 'value': 2}]}),
        ('lt 数量<2', {'filters': [{'column': 'Quantity', 'operator': 'lt', 'value': 2}]}),
        ('lte 数量<=1', {'filters': [{'column': 'Quantity', 'operator': 'lte', 'value': 1}]}),
        ('gt 金额>10', {'filters': [{'column': 'Order Amount', 'operator': 'gt', 'value': 10}]}),
        ('gt 重量>0.5', {'filters': [{'column': 'Weight(kg)', 'operator': 'gt', 'value': 0.5}]}),
        ('range 10<x<50', {'filters': [{'column': 'Order Amount', 'operator': 'gt', 'value': 10},
                                       {'column': 'Order Amount', 'operator': 'lt', 'value': 50}]}),
        ('contains Black', {'filters': [{'column': 'Variation', 'operator': 'contains', 'value': 'Black'}]}),
    ]
    for label, extra in cases:
        payload = {'sheet_index': 0, 'columns': ['Order ID', 'SKU ID'], 'limit': 100}
        payload.update(extra)
        _three_way(small_rep, payload, label)


def test_operator_matrix_big(big_rep):
    cases = [
        ('eq 物流商', {'filters': [{'column': 'Shipping Provider Name', 'operator': 'eq', 'value': 'SF International'}]}),
        ('neq 物流商', {'filters': [{'column': 'Shipping Provider Name', 'operator': 'neq', 'value': 'SF International'}]}),
        ('contains SF', {'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]}),
        ('gt 数量>0', {'filters': [{'column': 'Quantity', 'operator': 'gt', 'value': 0}]}),
        ('gt 数量>100(空)', {'filters': [{'column': 'Quantity', 'operator': 'gt', 'value': 100}]}),
        ('gt 金额>10', {'filters': [{'column': 'Order Amount', 'operator': 'gt', 'value': 10}]}),
        ('lte 金额<=5', {'filters': [{'column': 'Order Amount', 'operator': 'lte', 'value': 5}]}),
        ('eq 订单状态', {'filters': [{'column': 'Order Status', 'operator': 'eq', 'value': '已发货'}]}),
        ('neq 订单状态(空)', {'filters': [{'column': 'Order Status', 'operator': 'neq', 'value': '已发货'}]}),
        ('contains Tracking SF', {'filters': [{'column': 'Tracking ID', 'operator': 'contains', 'value': 'SF'}]}),
    ]
    for label, extra in cases:
        payload = {'sheet_index': 0, 'columns': ['Order ID', 'Quantity'], 'limit': 500}
        payload.update(extra)
        _three_way(big_rep, payload, label)


# ==========================================================================
# 3. 条件数量：0 / 1 / 2 / 3 个 AND
# ==========================================================================
def test_condition_count_and_semantics(small_rep):
    base = {'sheet_index': 0, 'columns': ['Order ID'], 'limit': 100}
    combos = [
        ('无条件', []),
        ('单条件', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]),
        ('双条件', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'nternational'},
                   {'column': 'Quantity', 'operator': 'gte', 'value': 1}]),
        ('三条件', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'nternational'},
                   {'column': 'Quantity', 'operator': 'gte', 'value': 1},
                   {'column': 'Order Amount', 'operator': 'gt', 'value': 0}]),
        ('双条件矛盾(0行)', [{'column': 'Shipping Provider Name', 'operator': 'eq', 'value': 'SF International'},
                            {'column': 'Shipping Provider Name', 'operator': 'eq', 'value': 'Yanwen Express'}]),
    ]
    counts = {}
    for label, filters in combos:
        payload = dict(base, filters=filters)
        res = _three_way(small_rep, payload, label)
        counts[label] = res.total_matches
    assert counts['无条件'] == 19
    assert counts['双条件矛盾(0行)'] == 0
    assert counts['单条件'] <= 19


def test_and_matches_intersection(small_rep):
    """双条件结果必须等于两个单条件结果集合的交集。"""
    a = {'sheet_index': 0, 'columns': ['Order ID'], 'limit': 100,
         'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]}
    b = {'sheet_index': 0, 'columns': ['Order ID'], 'limit': 100,
         'filters': [{'column': 'Quantity', 'operator': 'gte', 'value': 1}]}
    both = {'sheet_index': 0, 'columns': ['Order ID'], 'limit': 100,
            'filters': a['filters'] + b['filters']}
    ra, rb, rc = _three_way(small_rep, a), _three_way(small_rep, b), _three_way(small_rep, both)
    set_a = {r[0] for r in ra.rows}
    set_b = {r[0] for r in rb.rows}
    assert {r[0] for r in rc.rows} == (set_a & set_b)


# ==========================================================================
# 4. 返回列：单列 / 多列 / 全部列；NULL 单元格
# ==========================================================================
def test_projection_variants(big_rep):
    sheet = big_rep.sheets[0]
    for label, cols, limit in [
        ('单列', ['SKU ID'], 10),
        ('多列', ['Order ID', 'SKU ID', 'Quantity'], 10),
        ('多列乱序', ['Quantity', 'Order ID'], 10),
        ('全部列', None, 5),
    ]:
        payload = {'sheet_index': 0, 'limit': limit}
        if cols:
            payload['columns'] = cols
        res = _three_way(big_rep, payload, label)
        expected_names = cols or sheet.column_names
        assert [c['name'] for c in res.columns] == expected_names
        for row in res.rows:
            assert len(row) == len(expected_names)

    # 重复列只保留一次
    res = _three_way(big_rep, {'sheet_index': 0, 'columns': ['SKU ID', 'SKU ID'], 'limit': 3}, '重复列')
    assert len(res.columns) == 1


def test_null_and_empty_semantics(big_rep):
    """空单元格(None) 与字面空字符串的区分。"""
    cases = [
        ('eq None 全空列', {'filters': [{'column': 'Cancelation/Return Type', 'operator': 'eq', 'value': None}]}, 447),
        ('neq None 全空列', {'filters': [{'column': 'Cancelation/Return Type', 'operator': 'neq', 'value': None}]}, 0),
        ('eq "" 全空列', {'filters': [{'column': 'Cancelation/Return Type', 'operator': 'eq', 'value': ''}]}, 0),
        ('eq None 部分空列', {'filters': [{'column': 'Address Line 2', 'operator': 'eq', 'value': None}]}, 385),
        ('gt 全空列', {'filters': [{'column': 'Cancelation/Return Type', 'operator': 'gt', 'value': 0}]}, 0),
        ('contains 全空列', {'filters': [{'column': 'Cancelation/Return Type', 'operator': 'contains', 'value': 'x'}]}, 0),
    ]
    for label, extra, expected in cases:
        payload = {'sheet_index': 0, 'columns': ['Order ID'], 'limit': 500}
        payload.update(extra)
        res = _three_way(big_rep, payload, label)
        assert res.total_matches == expected, f'{label}: {res.total_matches} != {expected}'


def test_range_on_non_numeric_column_returns_empty(big_rep):
    """文本列做数值范围比较 -> 0 行（不报错，与 Python 引擎一致）。"""
    res = _three_way(big_rep, {
        'sheet_index': 0, 'columns': ['Product Name'], 'limit': 10,
        'filters': [{'column': 'Product Name', 'operator': 'gt', 'value': 10}],
    }, '文本列范围')
    assert res.total_matches == 0

    # 混合列（字符串形式数字）可以参与数值比较
    res2 = _three_way(big_rep, {
        'sheet_index': 0, 'columns': ['SKU Unit Original Price'], 'limit': 500,
        'filters': [{'column': 'SKU Unit Original Price', 'operator': 'gt', 'value': 10}],
    }, '文本数字列范围')
    assert res2.total_matches > 0


# ==========================================================================
# 5. 结果规模：0 / 1 / 少量 / 全部 447，以及 limit/offset
# ==========================================================================
def test_result_sizes_and_pagination(big_rep):
    total = big_rep.sheets[0].row_count
    # 全部
    res = _three_way(big_rep, {'sheet_index': 0, 'columns': ['Order ID'], 'limit': 500}, '全部')
    assert res.total_matches == total == 447
    assert res.returned_count == 447

    # 0 行
    res0 = _three_way(big_rep, {'sheet_index': 0, 'columns': ['Order ID'], 'limit': 10,
                                'filters': [{'column': 'Quantity', 'operator': 'gt', 'value': 99999}]}, '0行')
    assert res0.total_matches == 0 and res0.rows == [] and res0.has_more is False

    # 少量 + offset 分页
    for off, lim in [(0, 20), (20, 20), (400, 20), (440, 20), (447, 20), (1000, 20)]:
        payload = {'sheet_index': 0, 'columns': ['Order ID'], 'limit': lim, 'offset': off}
        res = _three_way(big_rep, payload, f'offset={off}')
        assert res.offset == off and res.limit == lim
        assert res.total_matches == 447


def test_one_row_result(small_rep):
    """精确到 1 行的筛选（小表 Quantity=2 只有 1 条）。"""
    res = _three_way(small_rep, {
        'sheet_index': 0, 'columns': ['Order ID', 'SKU ID', 'Quantity'], 'limit': 100,
        'filters': [{'column': 'Quantity', 'operator': 'eq', 'value': 2}],
    }, '单行')
    assert res.total_matches == 1
    assert res.returned_count == 1


# ==========================================================================
# 6. schema 校验
# ==========================================================================
def test_schema_errors(big_rep):
    with pytest.raises(q.ExcelQueryError) as e1:
        _run(big_rep, {'sheet_index': 0, 'columns': ['不存在的列'], 'limit': 5})
    assert e1.value.code == q.ERR_COLUMN_NOT_FOUND

    with pytest.raises(q.ExcelQueryError) as e2:
        _run(big_rep, {'sheet_index': 0, 'limit': 5,
                       'filters': [{'column': '不存在', 'operator': 'eq', 'value': 1}]})
    assert e2.value.code == q.ERR_COLUMN_NOT_FOUND

    with pytest.raises(q.ExcelQueryError) as e3:
        _run(big_rep, {'sheet_index': 0, 'limit': 5,
                       'filters': [{'column': 'Order ID', 'operator': 'like', 'value': '5%'}]})
    assert e3.value.code == q.ERR_INVALID_OPERATOR

    with pytest.raises(q.ExcelQueryError) as e4:
        _run(big_rep, {'sheet_index': 99, 'limit': 5})
    assert e4.value.code == q.ERR_SHEET_NOT_FOUND

    # 数值范围必须给数值
    with pytest.raises(q.ExcelQueryError) as e5:
        _run(big_rep, {'sheet_index': 0, 'limit': 5,
                       'filters': [{'column': 'Quantity', 'operator': 'gt', 'value': '大于十'}]})
    assert e5.value.code == q.ERR_INVALID_PARAM


def test_schema_case_and_alias_resolution(big_rep):
    """大小写不同仍应解析到真实列（Python 与 DuckDB 结果一致）。"""
    for name in ['quantity', 'QUANTITY', 'Quantity']:
        res = _three_way(big_rep, {
            'sheet_index': 0, 'columns': [name], 'limit': 5,
            'filters': [{'column': name, 'operator': 'gte', 'value': 1}],
        }, f'列名={name}')
        assert res.columns[0]['name'] == 'Quantity'

    with pytest.raises(q.ExcelQueryError):
        _run(big_rep, {'sheet_index': 0, 'columns': ['ship'], 'limit': 5})


# ==========================================================================
# 7. 安全：注入式输入 / 非法 operator / 非法 SQL / 跨用户
# ==========================================================================
def test_sql_injection_like_inputs(big_rep):
    """注入式输入只能被当成"普通字面值"，返回 0 行或正常结果，绝不报错/越权。"""
    needles = [
        "' OR 1=1 --",
        '"; DROP TABLE',
        '1; DELETE FROM t',
        "%' OR '1'='1",
        "x' UNION SELECT * FROM t --",
        "SF\\'; --",
    ]
    for n in needles:
        res = _three_way(big_rep, {
            'sheet_index': 0, 'columns': ['Order ID'], 'limit': 10,
            'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': n}],
        }, f'注入 needle={n!r}')
        assert res.total_matches == 0

    # 注入式列名 -> schema 校验拒绝
    with pytest.raises(q.ExcelQueryError):
        _run(big_rep, {'sheet_index': 0, 'columns': ['Order ID; DROP TABLE t'], 'limit': 5})


def test_rendered_sql_is_always_select_and_parameterized(big_rep):
    """直接检查 SQL 渲染结果：只有 SELECT、无分号、值不出现在 SQL 文本里。"""
    from backend.excel import duck as dk

    sheet = big_rep.sheets[0]
    secret = "SF' OR '1'='1"
    req = q.parse_request({
        'sheet_index': 0, 'columns': ['Order ID'], 'limit': 5,
        'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': secret}],
    }, document_id=big_rep.document_id)
    info = dk.get_registry().get(big_rep, 0)
    plan = dk.build_plan(info['table'], sheet, req)
    sql = dk.render_sql(plan)

    for key in ('count_sql', 'page_sql'):
        text = sql[key].lower().lstrip()
        assert text.startswith('select')
        assert ';' not in sql[key]
        assert secret not in sql[key], '用户输入不得出现在 SQL 文本中'
        assert 'drop' not in text and 'delete' not in text and 'insert' not in text
    assert secret in sql['page_params']  # 值走参数绑定
    # 标识符必须是 Python 生成的物理名
    assert '"c41"' in sql['page_sql']


def test_illegal_external_access_blocked(big_rep):
    """连接层就已禁止外部访问（ATTACH / COPY / 读文件）。"""
    _run(big_rep, {'sheet_index': 0, 'columns': ['Order ID'], 'limit': 1})
    info = duck_engine.get_registry().get(big_rep, 0)
    db = duck_engine.get_registry()._db_for(big_rep.document_id)
    blocked = 0
    for stmt in ["ATTACH 'evil.db' AS z", "COPY (SELECT 1) TO 'evil.csv'",
                 "CREATE TABLE x AS SELECT * FROM read_csv_auto('evil.csv')"]:
        try:
            db.con.execute(stmt)
        except Exception:
            blocked += 1
    assert blocked == 3
    assert info['row_count'] == 447


def test_cross_user_document_isolation():
    """跨用户 document_id 无法读取 representation（user_id 隔离）。"""
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
    assert excel_store.load_representation(doc_dir.name, user_id=(owner + 100000)) is None


# ==========================================================================
# 8. DuckDB 差分矩阵（多表 × 多列 × 多 operator × 多数据类型）
# ==========================================================================
def test_differential_matrix(big_rep, small_rep):
    """跨表、跨列、跨 operator 的组合差分：A == B == C。"""
    cases: List[Tuple[str, Any, Dict[str, Any]]] = []

    for rep, tag, cols in [
        (small_rep, 'small', ['Shipping Provider Name', 'Quantity', 'Order Amount',
                              'Weight(kg)', 'Variation', 'Order Status', 'Taxes',
                              'SKU Seller Discount', 'Buyer Nickname', 'Zipcode']),
        (big_rep, 'big', ['Shipping Provider Name', 'Quantity', 'Order Amount',
                          'Weight(kg)', 'Order Status', 'Taxes', 'Variation',
                          'Tracking ID', 'Order Channel', 'SKU Platform Discount']),
    ]:
        for col in cols:
            values = _sample_values(rep, col)
            for v in values:
                for op in OP_ALL:
                    if op == 'contains' and not isinstance(v, str):
                        continue
                    if op in ('gt', 'gte', 'lt', 'lte') and _num(v) is None:
                        continue
                    cases.append((f'{tag}/{col}/{op}={v!r}', rep, {
                        'sheet_index': 0, 'columns': [col], 'limit': 30,
                        'filters': [{'column': col, 'operator': op, 'value': v}],
                    }))

    assert len(cases) > 60, f'差分用例过少：{len(cases)}'
    for label, rep, payload in cases:
        _three_way(rep, payload, label)


def _sample_values(rep, col: str, max_n: int = 3) -> List[Any]:
    """取该列有代表性的样本值（前若干不同值 + None）。"""
    sheet = rep.sheets[0]
    ci = sheet.column_names.index(col)
    seen: List[Any] = []
    has_none = False
    for row in sheet.rows:
        v = row[ci] if ci < len(row) else None
        if v is None:
            has_none = True
            continue
        if all(str(v) != str(s) for s in seen):
            seen.append(v)
        if len(seen) >= max_n:
            break
    if has_none:
        seen.append(None)
    return seen
