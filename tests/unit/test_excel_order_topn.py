# -*- coding: utf-8 -*-
"""Phase 3C：Excel 排序 + TOP-N 测试矩阵

三方交叉验证（**顺序敏感**，逐位比较）：
  A. DuckDB          ORDER BY / LIMIT 由 Python 生成 SQL
  B. Python 参考实现   aggregate.sort_group_rows()
  C. 本文件内独立扫描   手写 分组 → 聚合 → 排序 → TOP-N（不 import 被测实现）

比较维度：分组顺序（逐位） / 每个分组的聚合值 / matched_rows / TOP-N 截断结果 /
         total_groups（截断前） / returned_groups（截断后）。
"""

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from backend.excel import aggregate as agg
from backend.excel import duck as duck_engine
from backend.excel import engine as query_engine
from backend.excel import nl_query as nl
from backend.excel import query as q
from backend.excel import store as excel_store
from backend.excel.parser import ExcelParser
from backend.excel.query_context import AggregateContext, AggregateContextStore
from backend.excel.representation import ColumnMeta, SheetRepresentation, WorkbookRepresentation

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMALL_REAL = PROJECT_ROOT / '直邮一店 8.20号订单.xlsx'
BIG_CANDIDATES = [
    PROJECT_ROOT / '直邮5店 7.10号订单.xlsx',
]
TOL = dict(rel=1e-9, abs=1e-6)


# ==========================================================================
# 独立 Ground Truth（手写语义：分组 -> 聚合 -> 排序 -> TOP-N）
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


def _match(cell: Any, op: str, value: Any) -> bool:
    if op == 'eq':
        if cell is None and value is None:
            return True
        if cell is None or value is None:
            return False
        if str(cell) == str(value):
            return True
        a, b = _to_f(cell), _to_f(value)
        return a is not None and b is not None and abs(a - b) <= 1e-9
    if op == 'neq':
        return cell is not None and not _match(cell, 'eq', value)
    if op == 'contains':
        return cell is not None and value is not None and str(value).lower() in str(cell).lower()
    a, b = _to_f(cell), _to_f(value)
    if a is None or b is None:
        return False
    return {'gt': a > b, 'gte': a >= b, 'lt': a < b, 'lte': a <= b}[op]


def _index_of(names: List[str], name: str) -> int:
    if name in names:
        return names.index(name)
    cand = [i for i, n in enumerate(names) if n.lower() == name.lower()]
    assert len(cand) == 1, f'GT 无法唯一解析列名 {name!r}'
    return cand[0]


def _col_is_numeric(values: Sequence[Any]) -> bool:
    non_null = [v for v in values if v is not None]
    return bool(non_null) and all(_to_f(v) is not None for v in non_null)


def _gt_group_sort_topn(rep, payload: Dict[str, Any]) -> Dict[str, Any]:
    """返回 {'ordered': [(key, value, matched_rows), ...], 'total_groups': n, 'matched_rows': n}。"""
    sheet = rep.sheets[payload.get('sheet_index') or 0]
    names = sheet.column_names
    filters = payload.get('filters') or []
    op = payload['operation']
    group_names = payload.get('group_by') or []
    gidx = [_index_of(names, g) for g in group_names]

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
        key = tuple(row[i] if i < len(row) else None for i in gidx)
        buckets.setdefault(key, []).append(row)

    value_ci = _index_of(names, payload['column']) if op != 'count' else None
    entries: List[Tuple[Tuple[Any, ...], Optional[float], int]] = []
    for key, rows in buckets.items():
        matched = len(rows)
        if op == 'count':
            entries.append((key, float(matched), matched))
            continue
        numbers = [n for n in (_to_f(r[value_ci] if value_ci < len(r) else None) for r in rows)
                   if n is not None]
        if not numbers:
            value: Optional[float] = None
        elif op == 'sum':
            value = float(sum(numbers))
        elif op == 'min':
            value = float(min(numbers))
        elif op == 'max':
            value = float(max(numbers))
        else:
            value = float(sum(numbers) / len(numbers))
        entries.append((key, value, matched))

    total_groups = len(entries)
    order_by = payload.get('order_by')
    order_dir = payload.get('order_dir') or 'asc'
    top_n = payload.get('top_n')

    if order_by:
        # 分组列物理类型（决定 tie-breaker 的比较方式）
        gtypes = [_col_is_numeric([r[i] for r in sheet.rows if i < len(r)]) for i in gidx]

        def norm_group(k: Tuple[Any, ...]) -> Tuple[Any, ...]:
            out = []
            for pos, v in enumerate(k):
                if v is None:
                    out.append((1, 0.0, ''))
                elif gtypes[pos]:
                    out.append((0, _to_f(v) or 0.0, ''))
                else:
                    out.append((0, 0.0, str(v)))
            return tuple(out)

        primary_idx = None
        if order_by.startswith('group_column_'):
            primary_idx = int(order_by.split('_')[-1])
        desc = order_dir == 'desc'
        tie_idx = [i for i in range(len(gidx)) if i != primary_idx]

        def tie_key(e):
            return tuple(norm_group(e[0])[i] for i in tie_idx)

        if primary_idx is None:
            non_null = [e for e in entries if e[1] is not None]
            nulls = [e for e in entries if e[1] is None]
            non_null.sort(key=tie_key)
            non_null.sort(key=lambda e: e[1], reverse=desc)
        else:
            non_null = [e for e in entries if e[0][primary_idx] is not None]
            nulls = [e for e in entries if e[0][primary_idx] is None]
            non_null.sort(key=tie_key)
            non_null.sort(key=lambda e: norm_group(e[0])[primary_idx][1:], reverse=desc)
        nulls.sort(key=tie_key)
        entries = non_null + nulls

    if top_n is not None:
        entries = entries[:top_n]

    return {'ordered': entries, 'total_groups': total_groups,
            'matched_rows': sum(len(rows) for rows in buckets.values()),
            'all_matched_rows': sum(e[2] for e in entries)}


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
    return ExcelParser().parse(str(p), 'ord-big', user_id=1, filename=p.name)


@pytest.fixture(scope='module')
def small_rep():
    if not SMALL_REAL.exists():
        pytest.skip('未找到小表（直邮一店 8.20号订单.xlsx）')
    return ExcelParser().parse(str(SMALL_REAL), 'ord-small', user_id=1, filename=SMALL_REAL.name)


@pytest.fixture(autouse=True)
def _clean_registry():
    duck_engine.reset_registry()
    yield
    duck_engine.reset_registry()


def _synth_rep() -> WorkbookRepresentation:
    """含"无有效数值分组"的合成表：验证 NULL 排序恒定最后。"""
    cols = [
        ColumnMeta(name='G', index=0, excel_column=1, excel_column_letter='A', dtype='string'),
        ColumnMeta(name='V', index=1, excel_column=2, excel_column_letter='B', dtype='mixed'),
    ]
    sheet = SheetRepresentation(
        sheet_name='S', sheet_index=0, header_mode='single', columns=cols,
        rows=[['a', '1'], ['b', 'text'], ['c', '3'], ['d', '2']],
        row_excel_numbers=[2, 3, 4, 5], row_count=4, column_count=2)
    return WorkbookRepresentation(schema_version='1.0', document_id='ord-synth', user_id=1,
                                  filename='synth.csv', file_type='csv', parser='csv',
                                  sheet_count=1, sheets=[sheet])


def _cmp_key(value: Any) -> Tuple[int, str]:
    """排序比较用的归一化键（None 视为最大，与后端 NULLS LAST 一致）。"""
    return (1, '') if value is None else (0, str(value))


def _three_way(rep, payload: Dict[str, Any], label: str = '') -> Any:
    """A(DuckDB) == B(Python) == C(独立扫描)，按**顺序**逐位比较。"""
    gt = _gt_group_sort_topn(rep, payload)
    expected = gt['ordered']

    def check(rows, engine_name: str, total_groups: int = -1):
        got = [(r.key(), r.value, r.matched_rows) for r in rows]
        assert len(got) == len(expected), \
            f'{label}/{engine_name}: 返回 {len(got)} 组 != GT {len(expected)} 组'
        for i, (g, e) in enumerate(zip(got, expected)):
            assert g[0] == e[0], f'{label}/{engine_name}: 第{i + 1}位分组键 {g[0]} != GT {e[0]}'
            assert g[2] == e[2], f'{label}/{engine_name}: 第{i + 1}位匹配行数不一致'
            if e[1] is None:
                assert g[1] is None, f'{label}/{engine_name}: 第{i + 1}位期望 None，实际 {g[1]}'
            else:
                assert g[1] is not None and g[1] == pytest.approx(e[1], **TOL), \
                    f'{label}/{engine_name}: 第{i + 1}位聚合值 {g[1]} != GT {e[1]}'

    res_py, eng_py = query_engine.run_aggregate(rep, payload, engine=query_engine.ENGINE_PYTHON)
    assert eng_py == 'python'
    check(res_py.rows, 'python')
    assert res_py.total_groups == gt['total_groups'], 'python total_groups 与 GT 不一致'

    res_dk, eng_dk = query_engine.run_aggregate(rep, payload, engine=query_engine.ENGINE_DUCKDB)
    assert eng_dk == 'duckdb'
    check(res_dk.rows, 'duckdb')
    assert res_dk.total_groups == gt['total_groups'], \
        f'{label}: total_groups {res_dk.total_groups} != GT {gt["total_groups"]}'
    assert res_dk.matched_rows == gt['matched_rows'], f'{label}: matched_rows 不一致'
    assert [r.value for r in res_dk.rows] == [r.value for r in res_py.rows], '两引擎顺序不一致'
    return res_dk


# ==========================================================================
# 1. 必测：真实 TOP-1 / TOP-N 场景
# ==========================================================================
def test_top1_which_carrier_has_most_orders(small_rep):
    """哪个物流商订单最多？ -> COUNT DESC TOP-1 = Yanwen Express 12。"""
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Shipping Provider Name'],
        'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 1}, 'TOP-1')
    assert res.returned_groups == 1
    assert res.total_groups == 3          # 截断前 3 组
    assert res.is_truncated is True
    assert res.rows[0].key() == ('Yanwen Express',)
    assert res.rows[0].value == 12.0
    assert res.top_n == 1 and res.order_dir == 'desc'
    assert res.order_by_label() == '分组行数'


def test_order_by_desc_without_topn(small_rep):
    """按订单数量从高到低排列各物流商：12 / 4 / 3。"""
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Shipping Provider Name'],
        'order_by': 'aggregate_value', 'order_dir': 'desc'}, 'DESC')
    assert [r.value for r in res.rows] == [12.0, 4.0, 3.0]
    assert [r.key()[0] for r in res.rows] == [
        'Yanwen Express', 'SF International', 'JS Express International']
    assert res.total_groups == res.returned_groups == 3
    assert res.is_truncated is False


def test_sum_topn_desc(big_rep):
    """订单金额最高的 5 个支付方式（大表只有 4 组 -> 全部返回）。"""
    res = _three_way(big_rep, {
        'sheet_index': 0, 'operation': 'sum', 'column': 'Order Amount',
        'group_by': ['Payment Method'], 'order_by': 'aggregate_value',
        'order_dir': 'desc', 'top_n': 5}, 'SUM TOP-5')
    values = [round(r.value or 0, 2) for r in res.rows]
    assert values == sorted(values, reverse=True)
    assert values[0] == 1367.43
    assert res.total_groups == 4 and res.returned_groups == 4


def test_sum_topn_asc(big_rep):
    """订单金额最低的 3 个支付方式。"""
    res = _three_way(big_rep, {
        'sheet_index': 0, 'operation': 'sum', 'column': 'Order Amount',
        'group_by': ['Payment Method'], 'order_by': 'aggregate_value',
        'order_dir': 'asc', 'top_n': 3}, 'SUM TOP-3 ASC')
    values = [round(r.value or 0, 2) for r in res.rows]
    assert values == sorted(values)
    assert res.returned_groups == 3 and res.total_groups == 4
    assert res.is_truncated is True


def test_order_by_group_column(small_rep):
    """按物流商名称升序 / 降序（排序键 = 分组字段）。"""
    asc = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Shipping Provider Name'],
        'order_by': 'group_column_0', 'order_dir': 'asc'}, '分组列 ASC')
    assert [r.key()[0] for r in asc.rows] == sorted(r.key()[0] for r in asc.rows)
    assert asc.order_by_label() == 'Shipping Provider Name'

    desc = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Shipping Provider Name'],
        'order_by': 'group_column_0', 'order_dir': 'desc'}, '分组列 DESC')
    assert [r.key()[0] for r in desc.rows] == sorted(
        (r.key()[0] for r in desc.rows), reverse=True)


def test_order_by_group_column_name_is_accepted(small_rep):
    """LLM 可以直接给分组列名作为排序键（映射为 group_column_<i>）。"""
    res, _ = query_engine.run_aggregate(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Shipping Provider Name'],
        'order_by': 'Shipping Provider Name', 'order_dir': 'asc'}, engine='duckdb')
    assert res.order_by == 'group_column_0'
    by_index, _ = query_engine.run_aggregate(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Shipping Provider Name'],
        'order_by': 'group_column_0', 'order_dir': 'asc'}, engine='duckdb')
    assert [r.key() for r in res.rows] == [r.key() for r in by_index.rows]


# ==========================================================================
# 2. 五个操作 × 排序
# ==========================================================================
def test_all_operations_with_order(small_rep):
    for op, col in [('count', None), ('sum', 'Order Amount'), ('avg', 'Order Amount'),
                    ('min', 'Order Amount'), ('max', 'Order Amount')]:
        for direction in ('asc', 'desc'):
            payload: Dict[str, Any] = {
                'sheet_index': 0, 'operation': op, 'group_by': ['Shipping Provider Name'],
                'order_by': 'aggregate_value', 'order_dir': direction}
            if col:
                payload['column'] = col
            _three_way(small_rep, payload, f'{op}/{direction}')


def test_order_by_second_group_column(small_rep):
    """多列分组时按第 2 个分组列排序。"""
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count',
        'group_by': ['Shipping Provider Name', 'Payment Method'],
        'order_by': 'group_column_1', 'order_dir': 'asc'}, '按第2分组列')
    assert all(len(r.group) == 2 for r in res.rows)


# ==========================================================================
# 3. TOP-N 规模与边界
# ==========================================================================
def test_topn_sizes(big_rep):
    for n in (1, 2, 5, 10, 200):
        res = _three_way(big_rep, {
            'sheet_index': 0, 'operation': 'count', 'group_by': ['Payment Method'],
            'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': n}, f'top{n}')
        assert res.returned_groups == min(n, 4), f'top_n={n} 返回 {res.returned_groups}'
        assert res.total_groups == 4
    # N == 组数
    res = _three_way(big_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Payment Method'],
        'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 4}, 'top4==组数')
    assert res.returned_groups == 4 and res.is_truncated is False
    # N < 组数
    res = _three_way(big_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Payment Method'],
        'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 2}, 'top2<组数')
    assert res.returned_groups == 2 and res.total_groups == 4 and res.is_truncated is True


def test_topn_boundaries_rejected(small_rep):
    for bad in (0, -1, 201, 999999999, 'DROP TABLE', 5.5, True, '12.5'):
        with pytest.raises(q.ExcelQueryError) as ei:
            query_engine.run_aggregate(small_rep, {
                'sheet_index': 0, 'operation': 'count', 'group_by': ['Order Status'],
                'order_by': 'aggregate_value', 'top_n': bad}, engine='duckdb')
        assert ei.value.code == agg.ERR_TOP_N_INVALID, f'top_n={bad!r} 应被拒绝'

    # 允许范围内的字符串数字可被接受（LLM 常见输出）
    res, _ = query_engine.run_aggregate(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Order Status'],
        'order_by': 'aggregate_value', 'top_n': '3'}, engine='duckdb')
    assert res.top_n == 3


def test_topn_requires_order(small_rep):
    with pytest.raises(q.ExcelQueryError) as ei:
        query_engine.run_aggregate(small_rep, {
            'sheet_index': 0, 'operation': 'count', 'group_by': ['Order Status'], 'top_n': 5},
            engine='duckdb')
    assert ei.value.code == agg.ERR_TOP_N_REQUIRES_ORDER


def test_order_requires_group(small_rep):
    with pytest.raises(q.ExcelQueryError) as ei:
        query_engine.run_aggregate(small_rep, {
            'sheet_index': 0, 'operation': 'count', 'order_by': 'aggregate_value'}, engine='duckdb')
    assert ei.value.code == agg.ERR_ORDER_REQUIRES_GROUP


def test_topn_on_unsorted_result_is_not_pagination(big_rep):
    """TOP-N 与 Phase 1D 分页严格区分：这里必须明确报错，而不是当作 offset/limit。"""
    with pytest.raises(q.ExcelQueryError) as ei:
        query_engine.run_aggregate(big_rep, {
            'sheet_index': 0, 'operation': 'count', 'group_by': ['Payment Method'], 'top_n': 2},
            engine='duckdb')
    assert ei.value.code == agg.ERR_TOP_N_REQUIRES_ORDER


# ==========================================================================
# 4. Tie-breaker 稳定性（硬性要求）
# ==========================================================================
def test_tiebreaker_is_stable_across_runs(small_rep):
    """State 有大量并列计数：连续多次运行顺序必须完全相同。"""
    payload = {'sheet_index': 0, 'operation': 'count', 'group_by': ['State'],
               'order_by': 'aggregate_value', 'order_dir': 'desc'}
    orders = []
    for _ in range(3):
        dk, _e = query_engine.run_aggregate(small_rep, payload, engine='duckdb')
        orders.append([r.key()[0] for r in dk.rows])
        py, _e2 = query_engine.run_aggregate(small_rep, payload, engine='python')
        assert [r.key() for r in py.rows] == [r.key() for r in dk.rows], '两引擎顺序不一致'
    assert orders[0] == orders[1] == orders[2], f'顺序不稳定：{orders}'

    res = _three_way(small_rep, payload, 'State tie-breaker')
    values = [r.value for r in res.rows]
    assert values == sorted(values, reverse=True)
    # 并列者按分组键（Unicode 码点）升序
    for i in range(len(res.rows) - 1):
        if res.rows[i].value == res.rows[i + 1].value:
            assert _cmp_key(res.rows[i].key()[0]) < _cmp_key(res.rows[i + 1].key()[0])


def test_tiebreaker_big_table(big_rep):
    """大表 Delivery Instruction：Mail room / Garage 计数相同 -> 稳定。"""
    res = _three_way(big_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Delivery Instruction'],
        'order_by': 'aggregate_value', 'order_dir': 'desc'}, '大表 tie-breaker')
    keys = [r.key()[0] for r in res.rows]
    assert keys[0] is None                     # 367 行的空值组最大
    assert keys.index('Garage') < keys.index('Mail room')   # 计数相同 -> 名称升序


def test_tiebreaker_multi_group_columns(small_rep):
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count',
        'group_by': ['Shipping Provider Name', 'Payment Method'],
        'order_by': 'aggregate_value', 'order_dir': 'desc'}, '双列 tie-breaker')
    values = [r.value for r in res.rows]
    assert values == sorted(values, reverse=True)
    for i in range(len(res.rows) - 1):
        if res.rows[i].value == res.rows[i + 1].value:
            a = tuple(_cmp_key(x) for x in res.rows[i].key())
            b = tuple(_cmp_key(x) for x in res.rows[i + 1].key())
            assert a < b, f'并列时未按分组键升序：{a} vs {b}'


def test_tiebreaker_generated_by_python_only(small_rep):
    """tie-breaker 由 Python 生成：SQL 中必然出现剩余分组列的升序键。"""
    from backend.excel import duck as dk

    sheet = small_rep.sheets[0]
    req = agg.parse_aggregate_payload({
        'operation': 'count', 'sheet_index': 0,
        'group_by': ['Shipping Provider Name', 'Payment Method'],
        'order_by': 'aggregate_value', 'order_dir': 'desc',
    }, document_id=small_rep.document_id)
    req = agg.resolve_aggregate(small_rep, req)
    info = dk.get_registry().get(small_rep, 0)
    sql = agg.build_group_aggregate_sql(info['table'], [], req.operation, req.column_index,
                                        req.group_by_indexes, order_by=req.order_by,
                                        order_dir=req.order_dir, top_n=req.top_n)
    assert sql['sort_keys'] == ['matched_rows DESC NULLS LAST', 'g0 ASC NULLS LAST',
                                'g1 ASC NULLS LAST']


# ==========================================================================
# 5. NULL 排序语义
# ==========================================================================
def test_null_values_sort_last_both_directions():
    """无有效数值的分组（value=NULL）在升序/降序下都排最后。"""
    rep = _synth_rep()
    for direction in ('asc', 'desc'):
        res = _three_way(rep, {
            'sheet_index': 0, 'operation': 'sum', 'column': 'V', 'group_by': ['G'],
            'order_by': 'aggregate_value', 'order_dir': direction}, f'NULL/{direction}')
        assert res.rows[-1].key() == ('b',), f'{direction}: NULL 组未排最后'
        assert res.rows[-1].value is None
    asc = _three_way(rep, {'sheet_index': 0, 'operation': 'sum', 'column': 'V',
                           'group_by': ['G'], 'order_by': 'aggregate_value',
                           'order_dir': 'asc'}, 'NULL asc 值')
    assert [r.value for r in asc.rows] == [1.0, 2.0, 3.0, None]


def test_null_group_column_sorts_last(small_rep):
    """分组键本身为 NULL（Payment Method 的 <空>）在按分组列排序时排最后。"""
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Payment Method'],
        'order_by': 'group_column_0', 'order_dir': 'asc'}, 'NULL 分组键')
    assert res.rows[-1].group[0].is_null is True


# ==========================================================================
# 6. WHERE + GROUP BY + ORDER BY + TOP-N
# ==========================================================================
def test_where_group_order_topn(small_rep):
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'sum', 'column': 'Order Amount',
        'group_by': ['Payment Method'], 'order_by': 'aggregate_value',
        'order_dir': 'desc', 'top_n': 3,
        'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]},
        'SF + ORDER + TOP3')
    # 过滤先发生：只统计 4 条 SF 行
    assert res.matched_rows == 4
    assert res.total_groups == 3
    assert res.returned_groups == 3


def test_filters_with_order_matrix(small_rep):
    combos = [
        ('无条件', []),
        ('eq', [{'column': 'Shipping Provider Name', 'operator': 'eq', 'value': 'SF International'}]),
        ('neq', [{'column': 'Shipping Provider Name', 'operator': 'neq', 'value': 'Yanwen Express'}]),
        ('gt', [{'column': 'Order Amount', 'operator': 'gt', 'value': 10}]),
        ('gte', [{'column': 'Order Amount', 'operator': 'gte', 'value': 10}]),
        ('lt', [{'column': 'Order Amount', 'operator': 'lt', 'value': 10}]),
        ('lte', [{'column': 'Order Amount', 'operator': 'lte', 'value': 10}]),
        ('contains', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'n'}]),
        ('双条件 AND', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'n'},
                     {'column': 'Order Amount', 'operator': 'gte', 'value': 0}]),
    ]
    for label, filters in combos:
        _three_way(small_rep, {
            'sheet_index': 0, 'operation': 'count', 'group_by': ['Payment Method'],
            'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 5,
            'filters': filters}, f'filter/{label}')


def test_zero_groups_with_order_topn(small_rep):
    res = _three_way(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Shipping Provider Name'],
        'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 5,
        'filters': [{'column': 'Order Amount', 'operator': 'gt', 'value': 99999}]}, '0 组')
    assert res.returned_groups == 0 and res.total_groups == 0
    assert res.is_truncated is False
    assert res.row_excel_span is None


# ==========================================================================
# 7. 安全
# ==========================================================================
def test_order_sql_is_safe(small_rep):
    from backend.excel import duck as dk

    req = agg.parse_aggregate_payload({
        'operation': 'sum', 'column': 'Order Amount', 'sheet_index': 0,
        'group_by': ['Shipping Provider Name'], 'order_by': 'aggregate_value',
        'order_dir': 'desc', 'top_n': 3,
        'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': "SF' OR 1=1"}],
    }, document_id=small_rep.document_id)
    req = agg.resolve_aggregate(small_rep, req)
    info = dk.get_registry().get(small_rep, 0)
    plan_filters = dk.build_plan_filters(
        small_rep.sheets[0], [q.FilterCondition(**f) for f in req.filters])
    sql = agg.build_group_aggregate_sql(info['table'], plan_filters, req.operation,
                                        req.column_index, req.group_by_indexes,
                                        order_by=req.order_by, order_dir=req.order_dir,
                                        top_n=req.top_n)
    text = sql['main_sql']
    assert text.lstrip().lower().startswith('select')
    assert ';' not in text
    assert "SF' OR 1=1" not in text
    # AVG/MIN/MAX 必须按"与 operation 对应"的排序别名排序（agg_value 恒为 SUM，不能用）
    assert 'ORDER BY agg_sort_value DESC NULLS LAST, g0 ASC NULLS LAST LIMIT ?' in text
    assert sql['main_params'][-1] == 3          # LIMIT 走参数绑定
    for banned in ('insert', 'update', 'delete', 'drop', 'alter', 'create', 'attach', 'copy'):
        assert banned not in text.lower()


def test_order_injection_rejected(small_rep):
    cases = [
        {'order_by': 'aggregate_value DESC; DROP TABLE t'},
        {'order_by': 'aggregate_value) OR 1=1'},
        {'order_by': 'random()'},
        {'order_by': '(SELECT 1)'},
        {'order_by': 'CASE WHEN 1=1 THEN 1 ELSE 0 END'},
        {'order_by': 'group_column_9'},
        {'order_by': ['aggregate_value']},
        {'order_dir': 'desc; DROP TABLE t'},
        {'order_dir': 'DESC NULLS FIRST'},
        {'order_dir': 1},
        {'top_n': 'DROP TABLE'},
        {'top_n': '999999999999'},
    ]
    for extra in cases:
        payload = {'sheet_index': 0, 'operation': 'count',
                   'group_by': ['Shipping Provider Name'], 'order_by': 'aggregate_value'}
        payload.update(extra)
        if 'order_dir' in extra and 'order_by' not in extra:
            payload['order_by'] = 'aggregate_value'
        with pytest.raises(q.ExcelQueryError) as ei:
            query_engine.run_aggregate(small_rep, payload, engine='duckdb')
        assert ei.value.code in (agg.ERR_ORDER_INVALID, agg.ERR_TOP_N_INVALID), \
            f'{extra} 未被拒绝（code={ei.value.code}）'


def test_unsorted_sql_has_no_order_by(small_rep):
    """未要求排序时，SQL 中不得出现 ORDER BY（Phase 3B 语义保持）。"""
    from backend.excel import duck as dk

    req = agg.parse_aggregate_payload({
        'operation': 'count', 'sheet_index': 0, 'group_by': ['Shipping Provider Name'],
    }, document_id=small_rep.document_id)
    req = agg.resolve_aggregate(small_rep, req)
    info = dk.get_registry().get(small_rep, 0)
    sql = agg.build_group_aggregate_sql(info['table'], [], req.operation, req.column_index,
                                        req.group_by_indexes)
    assert 'ORDER BY' not in sql['main_sql']
    assert 'LIMIT' not in sql['main_sql']
    assert sql['sort_keys'] == []


def test_external_access_still_blocked(small_rep):
    query_engine.run_aggregate(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Order Status'],
        'order_by': 'aggregate_value', 'top_n': 1}, engine='duckdb')
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
def test_order_differential_matrix(big_rep, small_rep):
    specs = {
        'small': ['Shipping Provider Name', 'State', 'Payment Method', 'Normal or Pre-order',
                  'Quantity', 'Order Channel'],
        'big': ['Payment Method', 'Delivery Instruction', 'State', 'Weight(kg)'],
    }
    count = 0
    for rep, tag in [(small_rep, 'small'), (big_rep, 'big')]:
        for gcol in specs[tag]:
            for direction in ('asc', 'desc'):
                _three_way(rep, {'sheet_index': 0, 'operation': 'count', 'group_by': [gcol],
                                 'order_by': 'aggregate_value', 'order_dir': direction},
                           f'{tag}/count/{gcol}/{direction}')
                count += 1
                _three_way(rep, {'sheet_index': 0, 'operation': 'count', 'group_by': [gcol],
                                 'order_by': 'group_column_0', 'order_dir': direction},
                           f'{tag}/gcol/{gcol}/{direction}')
                count += 1
                for n in (1, 3):
                    _three_way(rep, {'sheet_index': 0, 'operation': 'sum', 'column': 'Order Amount',
                                     'group_by': [gcol], 'order_by': 'aggregate_value',
                                     'order_dir': direction, 'top_n': n},
                               f'{tag}/sum/{gcol}/{direction}/top{n}')
                    count += 1
        _three_way(rep, {'sheet_index': 0, 'operation': 'avg', 'column': 'Order Amount',
                         'group_by': [specs[tag][0]], 'order_by': 'aggregate_value',
                         'order_dir': 'desc'}, f'{tag}/avg')
        count += 1
        _three_way(rep, {'sheet_index': 0, 'operation': 'min', 'column': 'Order Amount',
                         'group_by': [specs[tag][0]], 'order_by': 'aggregate_value',
                         'order_dir': 'asc'}, f'{tag}/min')
        count += 1
        _three_way(rep, {'sheet_index': 0, 'operation': 'max', 'column': 'Order Amount',
                         'group_by': [specs[tag][0]], 'order_by': 'aggregate_value',
                         'order_dir': 'desc'}, f'{tag}/max')
        count += 1
    assert count > 40, f'排序差分用例过少：{count}'


# ==========================================================================
# 9. 摘要与前端契约
# ==========================================================================
def test_summary_and_payload_contract(small_rep):
    res, _ = query_engine.run_aggregate(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Shipping Provider Name'],
        'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 2}, engine='duckdb')
    text = agg.format_group_aggregate_summary(res, '直邮一店 8.20号订单.xlsx')
    for expected in ['TOP-2 截取前 2 个', '排序：按「分组行数」从高到低（降序）',
                     '相同值时按分组字段升序稳定排序', 'Yanwen Express → 12']:
        assert expected in text, f'摘要缺少 {expected}'

    payload = res.to_dict()
    for key in ['sorted', 'order_by', 'order_by_label', 'order_by_column', 'order_dir',
                'order_dir_label', 'top_n', 'truncated_by_top_n', 'sort_description',
                'total_groups', 'returned_groups']:
        assert key in payload, f'结果缺少字段 {key}'
    assert payload['sorted'] is True
    assert payload['order_by'] == 'aggregate_value'
    assert payload['order_by_label'] == '分组行数'
    assert payload['order_dir'] == 'desc'
    assert payload['top_n'] == 2
    assert payload['truncated_by_top_n'] is True
    assert payload['total_groups'] == 3 and payload['returned_groups'] == 2
    assert '降序' in payload['sort_description'] and '前 2 个分组' in payload['sort_description']


def test_unsorted_payload_contract(small_rep):
    res, _ = query_engine.run_aggregate(small_rep, {
        'sheet_index': 0, 'operation': 'count', 'group_by': ['Shipping Provider Name']},
        engine='duckdb')
    payload = res.to_dict()
    assert payload['sorted'] is False
    assert payload['order_by'] is None and payload['order_dir'] is None
    assert payload['top_n'] is None and payload['truncated_by_top_n'] is False
    assert payload['sort_description'] == '未做排序（结果顺序为数据库返回顺序）'


# ==========================================================================
# 10. NL 层：意图解析 / 排序执行 / 上下文继承
# ==========================================================================
def _catalog(rep) -> List[Dict[str, Any]]:
    return [{
        'document_id': rep.document_id, 'filename': rep.filename, 'file_type': rep.file_type,
        'created_at': '2026-09-14T00:00:00', 'total_rows': rep.total_rows,
        'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                    'row_count': s.row_count, 'column_count': s.column_count,
                    'columns': s.column_names} for s in rep.sheets],
    }]


def _run_agg(rep, intent: 'nl.AggregateIntent', *, aggregate_context=None,
             session_key='p3c') -> Dict[str, Any]:
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nl.run_nl_query(
            '统计', _catalog(rep),
            aggregate_override=nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=intent),
            aggregate_context=aggregate_context,
            user_id=1, session_key=session_key,
        ))
    finally:
        excel_store.load_representation = original


def test_nl_intent_parsing_order_fields():
    intent = nl.aggregate_intent_from_dict({
        'aggregate_operation': 'sum', 'column': 'Order Amount',
        'group_by': ['Shipping Provider Name'], 'order_by': 'aggregate_value',
        'order_dir': 'DESC', 'top_n': '5',
    })
    assert intent.order_by == 'aggregate_value'
    assert intent.order_dir == 'DESC'
    assert intent.top_n == 5
    assert nl.aggregate_intent_from_dict({}).order_by is None
    assert nl.aggregate_intent_from_dict({'top_n': 'x'}).top_n is None
    assert nl.aggregate_intent_from_dict({'top_n': True}).top_n is None


def test_nl_top1_which_carrier_most_orders(small_rep):
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['物流商'], order_by='aggregate_value',
        order_dir='desc', top_n=1))
    assert out['status'] == 'ok', out.get('message')
    assert out['engine'] == 'duckdb'
    g = out['group_aggregate']
    assert g['top_n'] == 1 and g['total_groups'] == 3 and g['returned_groups'] == 1
    assert g['rows'][0]['group_key'] == ['Yanwen Express']
    assert g['rows'][0]['value'] == 12
    assert g['sorted'] is True and g['order_dir'] == 'desc'


def test_nl_order_desc_without_topn(small_rep):
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['Shipping Provider Name'],
        order_by='aggregate_value', order_dir='desc'))
    assert out['status'] == 'ok'
    g = out['group_aggregate']
    assert [r['group_key'][0] for r in g['rows']] == [
        'Yanwen Express', 'SF International', 'JS Express International']
    assert g['top_n'] is None and g['truncated_by_top_n'] is False


def test_nl_order_by_group_name(small_rep):
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['物流商'], order_by='物流商', order_dir='asc'))
    assert out['status'] == 'ok'
    g = out['group_aggregate']
    assert g['order_by'] == 'group_column_0'
    assert g['order_by_column'] == 'Shipping Provider Name'
    assert [r['group_key'][0] for r in g['rows']] == sorted(r['group_key'][0] for r in g['rows'])


def test_nl_top_n_without_order_clarifies(small_rep):
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['Shipping Provider Name'], top_n=5))
    assert out['status'] == 'clarify'
    assert '排序依据' in out['message']


def test_nl_invalid_order_by_clarifies(small_rep):
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['Shipping Provider Name'],
        order_by='aggregate_value DESC; DROP TABLE t'))
    assert out['status'] == 'clarify'
    assert 'order_by 非法' in out['message']


def test_nl_invalid_top_n_clarifies(small_rep):
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['Shipping Provider Name'],
        order_by='aggregate_value', top_n=999999999))
    assert out['status'] == 'clarify'
    assert '1~200' in out['message']


def test_nl_plain_aggregate_not_affected(small_rep):
    """回归：无排序需求时 group_by/单值统计都不受影响。"""
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['Shipping Provider Name']))
    assert out['status'] == 'ok'
    assert out['group_aggregate']['sorted'] is False
    assert out['group_aggregate']['total_groups'] == 3

    single = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', filters=[{'column': 'Shipping Provider Name',
                                     'operator': 'contains', 'value': 'SF'}]))
    assert single['status'] == 'ok'
    assert single['aggregate'] is not None and single['group_aggregate'] is None
    assert single['aggregate']['value_display'] == '4'


def test_nl_order_context_inheritance(small_rep):
    """每个物流商订单最多的5个 -> 再看前10个（继承分组/排序，只改 top_n）。"""
    first = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['Shipping Provider Name'],
        order_by='aggregate_value', order_dir='desc', top_n=5))
    assert first['status'] == 'ok'
    ctx = AggregateContext(**first['new_aggregate_context'])
    assert ctx.order_by == 'aggregate_value' and ctx.order_dir == 'desc' and ctx.top_n == 5
    assert ctx.group_by == ['Shipping Provider Name']

    second = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', refers_to_previous=True, top_n=2), aggregate_context=ctx)
    assert second['status'] == 'ok', second.get('message')
    g = second['group_aggregate']
    assert second['inherited_from'] == 'aggregate'
    assert g['top_n'] == 2                       # 本轮显式给出
    assert g['order_dir'] == 'desc'              # 继承
    assert g['sorted'] is True
    assert [x['name'] for x in g['group_by']] == ['Shipping Provider Name']
    assert '已沿用上一轮的排序与 TOP-N 设置' in second['message']


def test_nl_topn_only_followup_inherits_even_without_flag(small_rep):
    """「再看前10个」只有 TOP-N、没有分组/排序依据 -> 即使 LLM 没标 refers_to_previous 也继承。"""
    ctx = AggregateContext(
        user_id=1, session_key='p3c', document_id=small_rep.document_id,
        filename=small_rep.filename, sheet_index=0,
        sheet_name=small_rep.sheets[0].sheet_name, operation='count',
        group_by=['Shipping Provider Name'], order_by='aggregate_value',
        order_dir='desc', top_n=5)
    out = _run_agg(small_rep, nl.AggregateIntent(top_n=2), aggregate_context=ctx)
    assert out['status'] == 'ok', out.get('message')
    g = out['group_aggregate']
    assert out['inherited_from'] == 'aggregate'
    assert g['top_n'] == 2 and g['order_dir'] == 'desc' and g['sorted'] is True
    assert [x['name'] for x in g['group_by']] == ['Shipping Provider Name']

    # 没有上下文时同样的说法必须澄清（不猜）
    out2 = _run_agg(small_rep, nl.AggregateIntent(top_n=2))
    assert out2['status'] == 'clarify'


def test_deterministic_topn_followup_parsing():
    """确定性兜底：只识别有限的「（再看/换成/只看）前N个」说法。"""
    for text, expect in [('再看前10个', 10), ('换成前20个', 20), ('只看前3个', 3),
                         ('前5个', 5), ('再看前10条', 10), ('看前2名', 2), ('前1组', 1)]:
        assert nl.deterministic_topn_followup(text) == expect, text
    for text in ['前5个物流商', '前1000个', '再看前10个物流商', '看第51到100条', '前abc个', '']:
        assert nl.deterministic_topn_followup(text) is None, text


def test_nl_clarify_on_topn_followup_is_recovered(small_rep):
    """LLM 对「再看前10个」返回 clarify 时，也必须能继承上一轮分组排行。"""
    ctx = AggregateContext(
        user_id=1, session_key='p3c', document_id=small_rep.document_id,
        filename=small_rep.filename, sheet_index=0,
        sheet_name=small_rep.sheets[0].sheet_name, operation='count',
        group_by=['Shipping Provider Name'], order_by='aggregate_value',
        order_dir='desc', top_n=5)

    async def run():
        return await nl.run_nl_query(
            '再看前10个', _catalog(small_rep),
            aggregate_context=ctx,
            pagination_override=None,
            intent_override=None,
            aggregate_override=None,
            llm=_ClarifyLLM(),
            user_id=1, session_key='p3c',
        )
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: small_rep
    try:
        out = asyncio.run(run())
    finally:
        excel_store.load_representation = original
    assert out['status'] == 'ok', out.get('message')
    g = out['group_aggregate']
    assert g['top_n'] == 10 and g['sorted'] is True
    assert out['inherited_from'] == 'aggregate'
    assert [x['name'] for x in g['group_by']] == ['Shipping Provider Name']


class _ClarifyLLM:
    """永远返回 clarify 的假 LLM（用于验证确定性兜底路径）。"""

    async def chat_completion(self, messages, stream=False, temperature=None):
        # temperature：意图解析会以 temperature=0 调用（单次覆盖），假 LLM 接受但忽略
        yield '{"action":"clarify","clarification":"当前无上下文无法确定"}'
        return


def test_nl_new_query_does_not_inherit_order(small_rep):
    """新查询（refers_to_previous=false）不得继承旧的排序/TOP-N。"""
    ctx = AggregateContext(
        user_id=1, session_key='p3c', document_id=small_rep.document_id,
        filename=small_rep.filename, sheet_index=0,
        sheet_name=small_rep.sheets[0].sheet_name, operation='count',
        group_by=['Shipping Provider Name'], order_by='aggregate_value',
        order_dir='desc', top_n=1)
    out = _run_agg(small_rep, nl.AggregateIntent(
        operation='count', group_by=['Payment Method'], refers_to_previous=False),
        aggregate_context=ctx)
    assert out['status'] == 'ok'
    g = out['group_aggregate']
    assert g['sorted'] is False and g['top_n'] is None
    assert g['total_groups'] == 8
    assert [x['name'] for x in g['group_by']] == ['Payment Method']


def test_order_context_isolated_from_pagination(small_rep):
    """排序/TOP-N 上下文不得进入分页上下文。"""
    from backend.excel.query_context import ExcelQueryContextStore

    agg_store = AggregateContextStore()
    page_store = ExcelQueryContextStore()
    agg_store.save_aggregate(AggregateContext(
        user_id=1, session_key='c', document_id=small_rep.document_id,
        filename=small_rep.filename, sheet_index=0, sheet_name=small_rep.sheets[0].sheet_name,
        operation='count', group_by=['Shipping Provider Name'],
        order_by='aggregate_value', order_dir='desc', top_n=5))
    page_store.mark_other_turn(1, 'c')
    assert page_store.get_for_pagination(1, 'c') is None     # 不能"再来20条"
    inherited = agg_store.get_for_inheritance(1, 'c')
    assert inherited is not None and inherited.top_n == 5
