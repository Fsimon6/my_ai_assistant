# -*- coding: utf-8 -*-
"""Phase 1B：Structured Query 测试矩阵（Ground Truth 对照）

原则：
- Ground Truth **独立计算**：直接用 Python 遍历 representation.rows 得到期望值，
  不复用 query.py 的任何函数，确保"expected == actual"是真对照。
- 逐行比较：row content + row order + source excel row number（不是只比 count）。
- 大表以真实 representation 为准，**不硬编码行数**（仅做 >400 量级断言）。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.excel import query as q
from backend.excel.parser import ExcelParser
from backend.excel.query import ExcelQueryError
from backend.excel.representation import (
    HEADER_SINGLE,
    ColumnMeta,
    SheetRepresentation,
    WorkbookRepresentation,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMALL_REAL = PROJECT_ROOT / '直邮一店 8.20号订单.xlsx'
BIG_REAL_CANDIDATES = [
    PROJECT_ROOT / '直邮5店 7.10号订单.xlsx',
]
LONG_ID = '577532993098256512'


def _letter(idx0: int) -> str:
    s = ''
    n = idx0 + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def _sheet(name: str, index: int, header: List[str], rows: List[List[Any]],
           excel_row_start: int = 3) -> SheetRepresentation:
    columns = [
        ColumnMeta(
            name=h, index=i, excel_column=i + 1, excel_column_letter=_letter(i), dtype='mixed'
        )
        for i, h in enumerate(header)
    ]
    return SheetRepresentation(
        sheet_name=name,
        sheet_index=index,
        header_mode=HEADER_SINGLE,
        header_rows_excel=[1],
        header_depth=1,
        columns=columns,
        rows=rows,
        row_excel_numbers=[excel_row_start + i for i in range(len(rows))],
        row_count=len(rows),
        column_count=len(columns),
    )


SYN_HEADER = ['订单号', 'Order ID', 'Order ID Description', 'SKU', 'sku',
              '数量', '单价', '启用', '日期', '备注']
SYN_ROWS: List[List[Any]] = [
    ['A001', LONG_ID, 'desc1', 'S1', 's1', 2, 19.9, True, '2026-08-20 07:55:37', None],
    ['A002', '577532993098256513', 'desc2', 'S2', 's2', 1, 33.99, False, '2026-08-21 10:00:00', '中文备注'],
    ['A003', '577532993098256514', None, 'S3', 's3', 0, 0, True, None, 'Black 黑色'],
    ['A004', '577532993098256515', 'desc4', 'S1', 's1', 5, 12.5, None, '2026-09-01', '第二段'],
    [None, None, None, None, None, None, None, None, None, None],
]


@pytest.fixture(scope='module')
def synth_rep() -> WorkbookRepresentation:
    return WorkbookRepresentation(
        schema_version='1.0',
        document_id='synth-doc',
        user_id=1,
        filename='synthetic.xlsx',
        file_type='xlsx',
        parser='openpyxl',
        sheet_count=2,
        sheets=[
            _sheet('主表', 0, SYN_HEADER, [list(r) for r in SYN_ROWS]),
            _sheet('第二张表', 1, ['名称', '值'], [['甲', 1], ['乙', 2]]),
        ],
    )


# --------------------------------------------------------------------------
# Ground Truth 计算器（独立实现，不复用 query.py）
# --------------------------------------------------------------------------
def gt_rows(sheet: SheetRepresentation, cols: List[str]) -> Tuple[List[List[Any]], List[int]]:
    idx = [sheet.column_names.index(c) for c in cols]
    rows = [[r[i] for i in idx] for r in sheet.rows]
    return rows, list(sheet.row_excel_numbers)


def gt_filter(sheet: SheetRepresentation, filters: List[Dict[str, Any]]) -> List[int]:
    """返回匹配行的下标（独立实现）。

    eq       : None<->None 匹配；否则 str(cell)==str(value)，或原生数值等值
    contains : 大小写不敏感子串
    """
    idx = {c: sheet.column_names.index(c) for c in {f['column'] for f in filters}}
    out = []
    for i, row in enumerate(sheet.rows):
        ok = True
        for f in filters:
            cell = row[idx[f['column']]]
            val = f['value']
            op = f.get('operator', 'eq')
            if op == 'eq':
                if cell is None and val is None:
                    ok = ok and True
                elif cell is None or val is None:
                    ok = False
                elif str(cell) == str(val):
                    ok = True
                elif isinstance(cell, (int, float)) and not isinstance(cell, bool):
                    try:
                        ok = ok and abs(float(cell) - float(val)) <= 1e-9
                    except (TypeError, ValueError):
                        ok = False
                else:
                    ok = False
            else:
                ok = ok and (cell is not None and str(val).lower() in str(cell).lower())
            if not ok:
                break
        if ok:
            out.append(i)
    return out


def page_of(items: List[Any], limit: int, offset: int) -> List[Any]:
    return items[offset:offset + limit]


def run(rep: WorkbookRepresentation, payload: Dict[str, Any]) -> q.StructuredQueryResult:
    return q.query_representation(rep, payload)


def expect_error(rep: WorkbookRepresentation, payload: Dict[str, Any], code: str, status: int):
    with pytest.raises(ExcelQueryError) as ei:
        run(rep, payload)
    assert ei.value.code == code, f'期望 {code}，实际 {ei.value.code}'
    assert ei.value.status == status, f'期望 status={status}，实际 {ei.value.status}'
    return ei.value


# ==========================================================================
# 1. 单列 / 2. 多列
# ==========================================================================
def test_single_column(synth_rep):
    res = run(synth_rep, {'columns': ['Order ID'], 'limit': 100})
    expected, expected_rows = gt_rows(synth_rep.sheets[0], ['Order ID'])
    assert res.rows == expected
    assert res.row_excel_numbers == expected_rows
    assert res.total_matches == len(synth_rep.sheets[0].rows)
    assert [c['name'] for c in res.columns] == ['Order ID']
    assert res.columns[0]['excel_column_letter'] == 'B'


def test_multi_column_row_alignment(synth_rep):
    cols = ['订单号', 'Order ID', 'SKU']
    res = run(synth_rep, {'columns': cols, 'limit': 100})
    expected, expected_rows = gt_rows(synth_rep.sheets[0], cols)
    assert res.rows == expected, '多列必须行级对应，不得错位'
    assert res.row_excel_numbers == expected_rows
    # 显式验证第 N 行三列同源
    for i, row in enumerate(res.rows):
        gt = synth_rep.sheets[0].rows[i]
        assert row == [gt[0], gt[1], gt[3]]


def test_multi_column_range(synth_rep):
    """订单号(第1列 A) + SKU(第4列 D) -> A3:D3"""
    res = run(synth_rep, {'columns': ['订单号', 'SKU'], 'limit': 1})
    assert res.row_ranges[0] == 'A3:D3'


# ==========================================================================
# 3. eq / 4. contains / 5. AND
# ==========================================================================
def test_eq_filter(synth_rep):
    filters = [{'column': 'SKU', 'operator': 'eq', 'value': 'S1'}]
    res = run(synth_rep, {'columns': ['订单号', 'SKU'], 'filters': filters, 'limit': 100})
    matched = gt_filter(synth_rep.sheets[0], filters)
    assert res.total_matches == len(matched) == 2
    assert res.rows == [[synth_rep.sheets[0].rows[i][0], 'S1'] for i in matched]
    assert res.row_excel_numbers == [synth_rep.sheets[0].row_excel_numbers[i] for i in matched]


def test_contains_filter(synth_rep):
    filters = [{'column': '备注', 'operator': 'contains', 'value': '段'}]
    res = run(synth_rep, {'columns': ['订单号'], 'filters': filters, 'limit': 100})
    matched = gt_filter(synth_rep.sheets[0], filters)
    assert res.total_matches == len(matched) == 1
    assert res.rows == [['A004']]


def test_contains_is_case_insensitive(synth_rep):
    filters = [{'column': '备注', 'operator': 'contains', 'value': 'black'}]
    res = run(synth_rep, {'columns': ['订单号'], 'filters': filters, 'limit': 100})
    assert res.total_matches == 1
    assert res.rows == [['A003']]


def test_and_filter(synth_rep):
    filters = [
        {'column': 'SKU', 'operator': 'eq', 'value': 'S1'},
        {'column': '数量', 'operator': 'eq', 'value': 5},
    ]
    res = run(synth_rep, {'columns': ['订单号'], 'filters': filters, 'limit': 100})
    matched = gt_filter(synth_rep.sheets[0], filters)
    assert res.total_matches == len(matched) == 1
    assert res.rows == [['A004']]
    assert len(res.applied_filters) == 2
    assert res.applied_filters[1]['column_letter'] == 'F'


# ==========================================================================
# 6. / 7. limit 与 offset 分页
# ==========================================================================
def test_limit_and_offset_pagination(synth_rep):
    cols = ['订单号']
    expected, _ = gt_rows(synth_rep.sheets[0], cols)
    total = len(expected)

    r1 = run(synth_rep, {'columns': cols, 'limit': 2, 'offset': 0})
    assert r1.rows == page_of(expected, 2, 0)
    assert r1.total_matches == total and r1.returned_count == 2 and r1.has_more is True
    assert r1.next_offset == 2

    r2 = run(synth_rep, {'columns': cols, 'limit': 2, 'offset': 2})
    assert r2.rows == page_of(expected, 2, 2)
    assert r2.rows != r1.rows, '分页不得重复'

    # 分页拼接 == 全量、无丢失、无重复、顺序一致
    collected = []
    offset = 0
    while True:
        r = run(synth_rep, {'columns': cols, 'limit': 2, 'offset': offset})
        collected.extend(r.rows)
        if not r.has_more:
            break
        offset = r.next_offset
    assert collected == expected


def test_limit_larger_than_total(synth_rep):
    res = run(synth_rep, {'columns': ['订单号'], 'limit': 500})
    assert res.returned_count == res.total_matches == 5
    assert res.has_more is False and res.next_offset is None


def test_offset_beyond_total(synth_rep):
    res = run(synth_rep, {'columns': ['订单号'], 'limit': 10, 'offset': 9999})
    assert res.total_matches == 5
    assert res.returned_count == 0 and res.rows == [] and res.has_more is False


# ==========================================================================
# 8. 不存在值
# ==========================================================================
def test_no_match_returns_empty(synth_rep):
    res = run(synth_rep, {
        'columns': ['订单号'],
        'filters': [{'column': 'SKU', 'operator': 'eq', 'value': 'NOT_EXIST'}],
        'limit': 50,
    })
    assert res.total_matches == 0 and res.returned_count == 0 and res.rows == []
    assert res.has_more is False


# ==========================================================================
# 9. 超长 ID / 10. None / 11. 中文 / 类型安全
# ==========================================================================
def test_long_id_exact_and_no_precision_loss(synth_rep):
    res = run(synth_rep, {
        'columns': ['Order ID'],
        'filters': [{'column': 'Order ID', 'operator': 'eq', 'value': LONG_ID}],
        'limit': 10,
    })
    assert res.total_matches == 1
    assert res.rows[0][0] == LONG_ID
    assert isinstance(res.rows[0][0], str)
    assert res.rows[0][0] != float(LONG_ID), '绝不能退化为科学计数法'
    assert 'e+' not in str(res.rows[0][0]).lower()


def test_eq_none_matches_only_none(synth_rep):
    res = run(synth_rep, {
        'columns': ['订单号', '备注'],
        'filters': [{'column': '备注', 'operator': 'eq', 'value': None}],
        'limit': 50,
    })
    matched = gt_filter(synth_rep.sheets[0], [{'column': '备注', 'operator': 'eq', 'value': None}])
    assert res.total_matches == len(matched) == 2
    assert res.rows == [['A001', None], [None, None]]


def test_eq_empty_string_differs_from_none(synth_rep):
    """representation 中空单元格统一为 None，不存在 '' -> eq '' 应匹配 0 行。"""
    res = run(synth_rep, {
        'columns': ['备注'],
        'filters': [{'column': '备注', 'operator': 'eq', 'value': ''}],
        'limit': 50,
    })
    assert res.total_matches == 0


def test_chinese_contains_and_eq(synth_rep):
    res = run(synth_rep, {
        'columns': ['订单号'],
        'filters': [{'column': '备注', 'operator': 'contains', 'value': '中文'}],
        'limit': 50,
    })
    assert res.total_matches == 1 and res.rows == [['A002']]

    res2 = run(synth_rep, {'columns': ['备注'], 'filters': [
        {'column': '订单号', 'operator': 'eq', 'value': 'A003'}], 'limit': 50})
    assert res2.rows == [['Black 黑色']]


def test_numeric_and_boolean_filter(synth_rep):
    # 原生数值 + 字符串形式数值 -> 数值等值
    res = run(synth_rep, {'columns': ['订单号'], 'filters': [
        {'column': '数量', 'operator': 'eq', 'value': '5'}], 'limit': 50})
    assert res.rows == [['A004']]

    res2 = run(synth_rep, {'columns': ['订单号'], 'filters': [
        {'column': '单价', 'operator': 'eq', 'value': 19.9}], 'limit': 50})
    assert res2.rows == [['A001']]

    # 布尔：字符串比较，布尔不参与数值容差
    res3 = run(synth_rep, {'columns': ['订单号'], 'filters': [
        {'column': '启用', 'operator': 'eq', 'value': 'True'}], 'limit': 50})
    assert res3.total_matches == 2


# ==========================================================================
# 12. 列定位：不存在 / 歧义 / 绝不模糊匹配
# ==========================================================================
def test_column_not_found_never_fuzzy_resolves(synth_rep):
    err = expect_error(synth_rep, {'columns': ['Order I'], 'limit': 10}, 'column_not_found', 400)
    assert 'Order ID' in err.details['suggestions']
    assert len(err.details['available']) == len(SYN_HEADER)


def test_order_id_does_not_match_description_column(synth_rep):
    """Order ID 必须精确落到 Order ID，而不是 Order ID Description。"""
    res = run(synth_rep, {'columns': ['Order ID'], 'limit': 10})
    assert res.columns[0]['name'] == 'Order ID'
    assert res.columns[0]['excel_column_letter'] == 'B'
    assert res.rows[0][0] == LONG_ID   # 不是 'desc1'


def test_column_ambiguous(synth_rep):
    """'Sku' 大小写不敏感同时命中 'SKU' 与 'sku' -> 必须报歧义，不得随机选。"""
    err = expect_error(synth_rep, {'columns': ['Sku'], 'limit': 10}, 'column_ambiguous', 400)
    assert set(err.details['candidates']) == {'SKU', 'sku'}


def test_filter_column_unknown(synth_rep):
    expect_error(synth_rep, {'filters': [{'column': '不存在列', 'operator': 'eq', 'value': 1}]},
                 'column_not_found', 400)


# ==========================================================================
# 13. Sheet 定位
# ==========================================================================
def test_sheet_by_index_and_name(synth_rep):
    by_index = run(synth_rep, {'sheet_index': 1, 'columns': ['名称'], 'limit': 10})
    by_name = run(synth_rep, {'sheet_name': '第二张表', 'columns': ['名称'], 'limit': 10})
    assert by_index.sheet_name == by_name.sheet_name == '第二张表'
    assert by_index.rows == by_name.rows == [['甲'], ['乙']]


def test_sheet_conflict(synth_rep):
    expect_error(synth_rep, {'sheet_index': 0, 'sheet_name': '第二张表'}, 'sheet_conflict', 400)


def test_sheet_not_found(synth_rep):
    expect_error(synth_rep, {'sheet_name': '不存在的表'}, 'sheet_not_found', 404)
    expect_error(synth_rep, {'sheet_index': 99}, 'sheet_not_found', 404)


def test_sheet_default_is_first(synth_rep):
    res = run(synth_rep, {'columns': ['订单号'], 'limit': 1})
    assert res.sheet_index == 0 and res.sheet_name == '主表'


# ==========================================================================
# 14/17/18. 边界与非法参数
# ==========================================================================
def test_limit_zero_and_negative(synth_rep):
    expect_error(synth_rep, {'columns': ['订单号'], 'limit': 0}, 'invalid_param', 400)
    expect_error(synth_rep, {'columns': ['订单号'], 'limit': -5}, 'invalid_param', 400)


def test_limit_over_max(synth_rep):
    expect_error(synth_rep, {'columns': ['订单号'], 'limit': q.MAX_LIMIT + 1}, 'invalid_param', 400)


def test_offset_negative(synth_rep):
    expect_error(synth_rep, {'columns': ['订单号'], 'offset': -1}, 'invalid_param', 400)


def test_empty_columns_means_all(synth_rep):
    res = run(synth_rep, {'columns': [], 'limit': 10})
    assert [c['name'] for c in res.columns] == SYN_HEADER
    assert res.rows == synth_rep.sheets[0].rows


def test_empty_filters_means_all_rows(synth_rep):
    res = run(synth_rep, {'filters': [], 'limit': 100})
    assert res.total_matches == 5


def test_contains_empty_value_is_invalid(synth_rep):
    expect_error(synth_rep, {'filters': [{'column': '备注', 'operator': 'contains', 'value': ''}]},
                 'invalid_param', 400)
    expect_error(synth_rep, {'filters': [{'column': '备注', 'operator': 'contains', 'value': 123}]},
                 'invalid_param', 400)


def test_filter_column_empty_is_invalid(synth_rep):
    expect_error(synth_rep, {'filters': [{'column': '', 'operator': 'eq', 'value': 'x'}]},
                 'invalid_param', 400)
    expect_error(synth_rep, {'filters': [{'column': None, 'operator': 'eq', 'value': 'x'}]},
                 'invalid_param', 400)


def test_unsupported_operator(synth_rep):
    err = expect_error(synth_rep, {'filters': [{'column': '备注', 'operator': 'like', 'value': 'x'}]},
                       'invalid_operator', 400)
    # Phase 2 起白名单扩展为 7 个 operator；2A-P0 新增 date_between（日期区间，仅由 Python 按真实日期列生成）
    assert err.details['supported'] == [
        'eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'contains', 'date_between']
    # gt 已是合法 operator，但值必须可数值化
    expect_error(synth_rep, {'filters': [{'column': '备注', 'operator': 'gt', 'value': 'x'}]},
                 'invalid_param', 400)


def test_or_match_mode_not_supported(synth_rep):
    expect_error(synth_rep, {'match_mode': 'or'}, 'invalid_param', 400)


def test_limit1_offset_keeps_order(synth_rep):
    """逐行翻页必须保持原始顺序。"""
    seq = []
    for off in range(len(SYN_ROWS)):
        seq.append(run(synth_rep, {'columns': ['订单号'], 'limit': 1, 'offset': off}).rows[0][0])
    assert seq == [r[0] for r in SYN_ROWS]


# ==========================================================================
# 16. 真实大表（447 级）Ground Truth 逐行对照
# ==========================================================================
def _big_file() -> Optional[Path]:
    for p in BIG_REAL_CANDIDATES:
        if p.exists():
            return p
    return None


@pytest.fixture(scope='module')
def big_rep():
    path = _big_file()
    if path is None:
        pytest.skip('未找到大表测试文件（直邮5店 7.10号订单.xlsx）')
    rep = ExcelParser().parse(str(path), 'big-doc', user_id=1, filename=path.name)
    assert rep.total_rows > 400, f'大表规模异常：{rep.total_rows}'
    return rep


def test_big_table_pagination_ground_truth(big_rep, capsys):
    sheet = big_rep.sheets[0]
    total = sheet.row_count
    all_excel = list(sheet.row_excel_numbers)

    # 单一完整页 == Ground Truth
    full = run(big_rep, {'columns': ['Order ID'], 'limit': 500, 'offset': 0})
    assert full.total_matches == total
    assert full.returned_count == total
    assert full.rows == [[r[0]] for r in sheet.rows]
    assert full.row_excel_numbers == all_excel

    # 多组 limit / offset
    for limit, offset in [(10, 0), (50, 0), (100, 0), (50, 50), (50, 100), (50, 400), (10, total - 3)]:
        res = run(big_rep, {'columns': ['Order ID'], 'limit': limit, 'offset': offset})
        exp_rows = [r[0] for r in sheet.rows[offset:offset + limit]]
        exp_excel = all_excel[offset:offset + limit]
        assert res.rows == [[v] for v in exp_rows], f'limit={limit} offset={offset} 行内容不一致'
        assert res.row_excel_numbers == exp_excel, f'limit={limit} offset={offset} 行号不一致'
        assert res.returned_count == len(exp_rows)

    # 分页拼接：无丢失 / 无重复 / 顺序一致
    collected_rows: List[Any] = []
    collected_excel: List[int] = []
    offset = 0
    pages = 0
    while True:
        res = run(big_rep, {'columns': ['Order ID'], 'limit': 50, 'offset': offset})
        collected_rows.extend([r[0] for r in res.rows])
        collected_excel.extend(res.row_excel_numbers)
        pages += 1
        if not res.has_more:
            break
        offset = res.next_offset
    assert collected_rows == [r[0] for r in sheet.rows]
    assert collected_excel == all_excel
    assert len(set(collected_excel)) == total, '不得重复'

    with capsys.disabled():
        print(f'\n[GroundTruth 大表] {big_rep.filename} rows={total} cols={sheet.column_count} '
              f'pages(limit=50)={pages} 全部逐行一致')


def test_big_table_eq_filter_ground_truth(big_rep, capsys):
    sheet = big_rep.sheets[0]
    order_i = sheet.column_names.index('Order ID')
    prov_i = sheet.column_names.index('Shipping Provider Name')
    filters = [{'column': 'Shipping Provider Name', 'operator': 'eq', 'value': 'SF International'}]
    res = run(big_rep, {'columns': ['Order ID', 'Shipping Provider Name'],
                        'filters': filters, 'limit': 500})
    matched = gt_filter(sheet, filters)
    assert res.total_matches == len(matched)
    assert res.rows == [[sheet.rows[i][order_i], sheet.rows[i][prov_i]] for i in matched]
    assert res.row_excel_numbers == [sheet.row_excel_numbers[i] for i in matched]
    with capsys.disabled():
        print(f'[GroundTruth 大表] Shipping Provider Name == SF International -> {res.total_matches} 行')


def test_big_table_contains_filter_ground_truth(big_rep, capsys):
    sheet = big_rep.sheets[0]
    order_i = sheet.column_names.index('Order ID')
    var_i = sheet.column_names.index('Variation')
    filters = [{'column': 'Variation', 'operator': 'contains', 'value': 'Black'}]
    res = run(big_rep, {'columns': ['Order ID', 'Variation'], 'filters': filters, 'limit': 500})
    matched = gt_filter(sheet, filters)
    assert res.total_matches == len(matched) >= 1
    assert res.rows == [[sheet.rows[i][order_i], sheet.rows[i][var_i]] for i in matched]
    assert res.row_excel_numbers == [sheet.row_excel_numbers[i] for i in matched]
    with capsys.disabled():
        print(f'[GroundTruth 大表] Variation contains Black -> {res.total_matches} 行')


def test_big_table_real_sku_id(big_rep):
    """用 representation 中真实存在的 SKU ID 做精确查找（不硬编码）。"""
    sheet = big_rep.sheets[0]
    sku_idx = sheet.column_names.index('SKU ID')
    order_idx = sheet.column_names.index('Order ID')
    sample = sheet.rows[0][sku_idx]
    assert isinstance(sample, str)

    res = run(big_rep, {
        'columns': ['Order ID', 'SKU ID'],
        'filters': [{'column': 'SKU ID', 'operator': 'eq', 'value': sample}],
        'limit': 500,
    })
    expected_idx = [i for i, r in enumerate(sheet.rows) if r[sku_idx] == sample]
    assert res.total_matches == len(expected_idx) >= 1
    assert res.rows == [[sheet.rows[i][order_idx], sheet.rows[i][sku_idx]] for i in expected_idx]


def test_big_table_multi_column_alignment(big_rep):
    cols = ['Order ID', 'SKU ID', 'Variation']
    res = run(big_rep, {'columns': cols, 'limit': 20})
    sheet = big_rep.sheets[0]
    idx = [sheet.column_names.index(c) for c in cols]
    expected = [[r[i] for i in idx] for r in sheet.rows[:20]]
    assert res.rows == expected, '多列必须行级同源'
    assert res.row_excel_numbers == sheet.row_excel_numbers[:20]
    # 行范围 = 投影列跨度（A..J）
    assert res.row_ranges[0] == f'A{sheet.row_excel_numbers[0]}:J{sheet.row_excel_numbers[0]}'


def test_big_table_no_match(big_rep):
    res = run(big_rep, {
        'columns': ['Order ID'],
        'filters': [{'column': 'Shipping Provider Name', 'operator': 'eq', 'value': '__NOPE__'}],
        'limit': 50,
    })
    assert res.total_matches == 0 and res.rows == []


# ==========================================================================
# 小表真实文件（19 行）
# ==========================================================================
def test_small_real_file_ground_truth():
    if not SMALL_REAL.exists():
        pytest.skip(f'未找到 {SMALL_REAL.name}')
    rep = ExcelParser().parse(str(SMALL_REAL), 'small-doc', user_id=1)
    sheet = rep.sheets[0]
    total = sheet.row_count

    full = run(rep, {'columns': [], 'limit': 500})
    assert full.total_matches == total
    assert full.rows == sheet.rows
    assert full.row_excel_numbers == sheet.row_excel_numbers

    page = run(rep, {'columns': ['Order ID'], 'limit': 5, 'offset': 0})
    assert page.rows == [[r[0]] for r in sheet.rows[:5]]
    assert page.row_excel_numbers == sheet.row_excel_numbers[:5]
    assert page.has_more is True
    assert page.row_ranges[0] == f'A{sheet.row_excel_numbers[0]}:A{sheet.row_excel_numbers[0]}'
