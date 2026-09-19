# -*- coding: utf-8 -*-
"""Phase 1C：自然语言 -> Structured Query 测试矩阵

测试策略（关键）：
- LLM 的职责只有「产出意图草图」，因此**刻意把 LLM 从测试中隔离**：
  用 `NlIntent` 直接注入（等价于 LLM 的输出），从而可以稳定、可复现地测试
  「草图 -> schema 校验 -> Phase 1B 取数 -> Ground Truth」这条真正决定正确性的链路。
- 另有一组测试验证「LLM 输出不可信时」的行为（列名不存在 / Sheet 不存在 / 值不存在 /
  文件歧义 / 非表格问题），确保**不猜、不编造**。
- Ground Truth 独立计算：直接遍历 representation.rows，逐行比较内容 + 顺序 + Excel 行号。
"""

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from backend.excel import nl_query as nl
from backend.excel import query as q
from backend.excel import store as excel_store
from backend.excel.parser import ExcelParser
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


def _big_path() -> Optional[Path]:
    for p in BIG_REAL_CANDIDATES:
        if p.exists():
            return p
    return None


@pytest.fixture(scope='module')
def big_rep() -> WorkbookRepresentation:
    path = _big_path()
    if path is None:
        pytest.skip('未找到大表（直邮5店 7.10号订单.xlsx）')
    return ExcelParser().parse(str(path), 'big-doc', user_id=1, filename=path.name)


@pytest.fixture(scope='module')
def small_rep() -> WorkbookRepresentation:
    if not SMALL_REAL.exists():
        pytest.skip('未找到小表（直邮一店 8.20号订单.xlsx）')
    return ExcelParser().parse(str(SMALL_REAL), 'small-doc', user_id=1, filename=SMALL_REAL.name)


def _letter(i: int) -> str:
    s, n = '', i + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def _sheet(name: str, header: List[str], rows: List[List[Any]]) -> SheetRepresentation:
    cols = [ColumnMeta(name=h, index=i, excel_column=i + 1, excel_column_letter=_letter(i), dtype='mixed')
            for i, h in enumerate(header)]
    return SheetRepresentation(
        sheet_name=name, sheet_index=0, header_mode=HEADER_SINGLE, header_rows_excel=[1], header_depth=1,
        columns=cols, rows=rows, row_excel_numbers=[3 + i for i in range(len(rows))],
        row_count=len(rows), column_count=len(cols),
    )


SYNTH_HEADER = ['Order ID', 'SKU', 'sku', 'Seller SKU', 'Variation', '备注']
SYNTH_ROWS = [
    ['577532993098256512', 'S1', 'l1', 'SS1', 'Black', None],
    ['577532993098256513', 'S2', 'l2', 'SS2', 'White', '中文备注'],
    ['577532993098256514', 'S1', 'l1', 'SS3', 'Blue', '第二段'],
]


@pytest.fixture()
def synth_rep() -> WorkbookRepresentation:
    return WorkbookRepresentation(
        schema_version='1.0', document_id='synth-nl', user_id=1, filename='synth.xlsx',
        file_type='xlsx', parser='openpyxl', sheet_count=2,
        sheets=[
            _sheet('主表', SYNTH_HEADER, [list(r) for r in SYNTH_ROWS]),
            _sheet('第二张表', ['名称', '值'], [['甲', 1]]),
        ],
    )


def _catalog(rep: WorkbookRepresentation, filename: Optional[str] = None) -> List[Dict[str, Any]]:
    """按 store.list_excel_catalog 的结构手工构造目录（只含 schema）。"""
    return [{
        'document_id': rep.document_id,
        'filename': filename or rep.filename,
        'file_type': rep.file_type,
        'created_at': '2026-09-14T00:00:00',
        'total_rows': rep.total_rows,
        'sheets': [
            {
                'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                'row_count': s.row_count, 'column_count': s.column_count,
                'columns': s.column_names,
            }
            for s in rep.sheets
        ],
    }]


def _run(rep, intent, message='test', catalog=None) -> Dict[str, Any]:
    """执行 NL 查询；把 store 读取替换为内存中的 representation（避免依赖磁盘产物）。"""
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nl.run_nl_query(
            message, catalog if catalog is not None else _catalog(rep), intent_override=intent
        ))
    finally:
        excel_store.load_representation = original


# ==========================================================================
# 1. 文件名解析（模糊 / 中文数字 / 不存在 / 歧义）
# ==========================================================================
def _doc_catalog() -> List[Dict[str, Any]]:
    return [
        {'document_id': 'd5', 'filename': '直邮5店 7.10号订单.xlsx', 'file_type': 'xlsx',
         'created_at': '2026-09-14T10:00:00', 'total_rows': 447, 'sheets': []},
        {'document_id': 'd1', 'filename': '直邮一店 8.20号订单.xlsx', 'file_type': 'xlsx',
         'created_at': '2026-09-14T10:01:00', 'total_rows': 19, 'sheets': []},
    ]


def test_resolve_document_digit_and_chinese_numeral():
    cat = _doc_catalog()
    for hint in ['5店', '五店', '直邮5店', '直邮五店', '直邮5店 7.10号订单.xlsx']:
        doc, cands = nl.resolve_document(cat, hint)
        assert doc is not None and doc['document_id'] == 'd5', f'hint={hint} 应解析到 5 店'
        assert cands == []


def test_resolve_document_small_table():
    doc, cands = nl.resolve_document(_doc_catalog(), '直邮一店')
    assert doc is not None and doc['document_id'] == 'd1' and cands == []


def test_resolve_document_not_found_returns_candidates():
    doc, cands = nl.resolve_document(_doc_catalog(), '完全不存在的报表')
    assert doc is None
    assert set(cands) == {'直邮5店 7.10号订单.xlsx', '直邮一店 8.20号订单.xlsx'}


def test_resolve_document_ambiguous_does_not_guess():
    cat = [
        {'document_id': 'a', 'filename': '订单A.xlsx', 'file_type': 'xlsx', 'created_at': '1', 'sheets': []},
        {'document_id': 'b', 'filename': '订单B.xlsx', 'file_type': 'xlsx', 'created_at': '2', 'sheets': []},
    ]
    doc, cands = nl.resolve_document(cat, '订单')
    assert doc is None and len(cands) == 2


def test_resolve_document_no_hint_with_multiple_docs_asks_clarify():
    doc, cands = nl.resolve_document(_doc_catalog(), None)
    assert doc is None and len(cands) == 2


def test_dedupe_same_filename_keeps_latest():
    cat = [
        {'document_id': 'old', 'filename': 'x.xlsx', 'created_at': '2026-09-01T00:00:00', 'sheets': []},
        {'document_id': 'new', 'filename': 'x.xlsx', 'created_at': '2026-09-14T00:00:00', 'sheets': []},
    ]
    out = nl.dedupe_catalog_by_filename(cat)
    assert len(out) == 1 and out[0]['document_id'] == 'new'


# ==========================================================================
# 2. Sheet / 列解析（含自然语言简称、中英文表达、歧义）
# ==========================================================================
def test_sheet_resolution_single_sheet_lenient(big_rep):
    """单 Sheet 文件：不存在 Sheet 歧义，故任何 hint 都落到该唯一 Sheet（摘要会告知真实 Sheet 名）。"""
    sheet, cands = nl.resolve_sheet_nl(big_rep, 'OrderSKUList')
    assert sheet is not None and sheet.sheet_name == 'OrderSKUList' and cands == []
    sheet2, _ = nl.resolve_sheet_nl(big_rep, 'orderskulist')
    assert sheet2 is not None and sheet2.sheet_name == 'OrderSKUList'
    sheet3, cands3 = nl.resolve_sheet_nl(big_rep, '不存在的表')
    assert sheet3 is not None and sheet3.sheet_name == 'OrderSKUList' and cands3 == []


def test_sheet_resolution_multi_sheet_unknown_clarifies(synth_rep):
    """多 Sheet 文件 + 无法匹配的 Sheet 名 -> 不猜，返回候选。"""
    sheet, cands = nl.resolve_sheet_nl(synth_rep, '不存在的表')
    assert sheet is None and set(cands) == {'主表', '第二张表'}


def test_sheet_resolution_multi_sheet_no_hint_clarifies(synth_rep):
    """多 Sheet 文件且用户未指定 Sheet -> 不猜，要求澄清。"""
    sheet, cands = nl.resolve_sheet_nl(synth_rep, None)
    assert sheet is None and len(cands) == 2


def test_column_nl_sku_short_name(big_rep):
    """「SKU」必须解析到真实存在的 'SKU ID'，而不是 Seller SKU / Sku Quantity。"""
    sheet = big_rep.sheets[0]
    for hint in ['SKU', 'sku', 'Sku', 'SKU ID', 'sku id']:
        col, cands = nl.resolve_column_nl(sheet, hint)
        assert col == 'SKU ID', f'hint={hint} -> {col}'


def test_column_nl_chinese_and_english(big_rep):
    sheet = big_rep.sheets[0]
    assert nl.resolve_column_nl(sheet, 'Order ID')[0] == 'Order ID'
    assert nl.resolve_column_nl(sheet, '订单号')[0] == 'Order ID'
    assert nl.resolve_column_nl(sheet, '物流商')[0] == 'Shipping Provider Name'


def test_column_nl_ambiguous_returns_candidates(synth_rep):
    """Sheet 同时存在 'SKU' 与 'sku' 时，'Sku' 必须报歧义而不是随机选。"""
    sheet = synth_rep.sheets[0]
    col, cands = nl.resolve_column_nl(sheet, 'Sku')
    assert col is None and set(cands) == {'SKU', 'sku'}


def test_column_nl_not_found_returns_all_columns(synth_rep):
    col, cands = nl.resolve_column_nl(synth_rep.sheets[0], '根本不存在的列')
    assert col is None and len(cands) == len(SYNTH_HEADER)


# ==========================================================================
# 3. Ground Truth：小表 / 大表（前N / offset / 单列 / 多列 / eq / contains / AND）
# ==========================================================================
def test_nl_list_top20_sku_big_table(big_rep, capsys):
    """「列出5店前20条SKU」-> 必须是真实 Excel 的前 20 条 SKU。"""
    out = _run(big_rep, nl.NlIntent(document='5店', columns=['SKU'], limit=20, offset=0))
    assert out['status'] == 'ok', out
    assert out['query']['columns'] == ['SKU ID']
    assert out['query']['limit'] == 20 and out['query']['offset'] == 0

    sheet = big_rep.sheets[0]
    sku_i = sheet.column_names.index('SKU ID')
    gt_rows = [[r[sku_i]] for r in sheet.rows[:20]]
    gt_nums = sheet.row_excel_numbers[:20]

    res = out['result']
    assert res['rows'] == gt_rows, '前 20 条 SKU 必须与 Ground Truth 逐行一致'
    assert res['row_excel_numbers'] == gt_nums
    assert res['total_matches'] == sheet.row_count
    assert res['returned_count'] == 20
    with capsys.disabled():
        print(f'\n[NL GT] 列出5店前20条SKU -> rows={len(res["rows"])} excel_rows={gt_nums[0]}~{gt_nums[-1]}')
        print(f'  首行 SKU={gt_rows[0][0]}  末行 SKU={gt_rows[-1][0]}')


def test_nl_multi_column_alignment(big_rep):
    out = _run(big_rep, nl.NlIntent(document='5店', columns=['Order ID', 'SKU ID'], limit=20))
    assert out['status'] == 'ok'
    sheet = big_rep.sheets[0]
    oi, si = sheet.column_names.index('Order ID'), sheet.column_names.index('SKU ID')
    gt = [[r[oi], r[si]] for r in sheet.rows[:20]]
    assert out['result']['rows'] == gt
    assert out['result']['row_excel_numbers'] == sheet.row_excel_numbers[:20]


def test_nl_offset_pagination_matches_ground_truth(big_rep):
    for limit, offset in [(50, 0), (50, 50), (50, 100), (10, 400)]:
        out = _run(big_rep, nl.NlIntent(document='5店', columns=['SKU ID'], limit=limit, offset=offset))
        assert out['status'] == 'ok'
        sheet = big_rep.sheets[0]
        si = sheet.column_names.index('SKU ID')
        gt = [[r[si]] for r in sheet.rows[offset:offset + limit]]
        gt_nums = sheet.row_excel_numbers[offset:offset + limit]
        assert out['result']['rows'] == gt, f'limit={limit} offset={offset}'
        assert out['result']['row_excel_numbers'] == gt_nums
        assert out['result']['returned_count'] == len(gt)


def test_nl_exact_lookup_order_id(big_rep):
    sheet = big_rep.sheets[0]
    oi = sheet.column_names.index('Order ID')
    target = sheet.rows[0][oi]
    out = _run(big_rep, nl.NlIntent(
        document='5店', columns=['Order ID'],
        filters=[{'column': '订单号', 'operator': 'eq', 'value': target}], limit=20,
    ))
    assert out['status'] == 'ok'
    expected_idx = [i for i, r in enumerate(sheet.rows) if r[oi] == target]
    assert out['result']['total_matches'] == len(expected_idx) >= 1
    assert out['result']['rows'] == [[target]] * len(expected_idx)
    assert isinstance(out['result']['rows'][0][0], str)


def test_nl_contains_filter(big_rep):
    out = _run(big_rep, nl.NlIntent(
        document='5店', columns=['Order ID', 'Variation'],
        filters=[{'column': 'Variation', 'operator': 'contains', 'value': 'Black'}], limit=500,
    ))
    assert out['status'] == 'ok'
    sheet = big_rep.sheets[0]
    vi = sheet.column_names.index('Variation')
    expected = [i for i, r in enumerate(sheet.rows) if r[vi] is not None and 'black' in str(r[vi]).lower()]
    assert out['result']['total_matches'] == len(expected) >= 1
    assert out['result']['row_excel_numbers'] == [sheet.row_excel_numbers[i] for i in expected]


def test_nl_and_condition(big_rep):
    sheet = big_rep.sheets[0]
    vi = sheet.column_names.index('Variation')
    first_var = str(sheet.rows[0][vi])
    out = _run(big_rep, nl.NlIntent(
        document='5店', columns=['Order ID'],
        filters=[
            {'column': '物流商', 'operator': 'contains', 'value': 'SF'},
            {'column': 'Variation', 'operator': 'eq', 'value': first_var},
        ], limit=500,
    ))
    assert out['status'] == 'ok'
    pi = sheet.column_names.index('Shipping Provider Name')
    expected = [i for i, r in enumerate(sheet.rows)
                if 'sf' in str(r[pi]).lower() and r[vi] == sheet.rows[0][vi]]
    assert out['result']['total_matches'] == len(expected)


def test_nl_small_table_ground_truth(small_rep):
    out = _run(small_rep, nl.NlIntent(document='直邮一店', columns=['SKU ID'], limit=5))
    assert out['status'] == 'ok'
    sheet = small_rep.sheets[0]
    si = sheet.column_names.index('SKU ID')
    assert out['result']['rows'] == [[r[si]] for r in sheet.rows[:5]]
    assert out['result']['row_excel_numbers'] == sheet.row_excel_numbers[:5] == [3, 4, 5, 6, 7]


# ==========================================================================
# 4. 失败与澄清路径（不猜、不编造）
# ==========================================================================
def test_nl_existing_value_no_match(big_rep):
    out = _run(big_rep, nl.NlIntent(
        document='5店', columns=['Order ID'],
        filters=[{'column': 'Order ID', 'operator': 'eq', 'value': '__NOT_EXIST__'}], limit=20,
    ))
    assert out['status'] == 'ok'
    assert out['result']['total_matches'] == 0 and out['result']['rows'] == []


def test_nl_unknown_column_clarifies(big_rep):
    out = _run(big_rep, nl.NlIntent(document='5店', columns=['完全不存在的列'], limit=20))
    assert out['status'] == 'clarify'
    assert '无法确定列' in out['message']


def test_nl_unknown_sheet_clarifies(synth_rep):
    out = _run(synth_rep, nl.NlIntent(document='synth.xlsx', sheet='不存在的Sheet', columns=['Order ID']))
    assert out['status'] == 'clarify'
    assert set(out['candidates']) == {'主表', '第二张表'}


def test_nl_unknown_document_clarifies_with_candidates(small_rep, big_rep):
    cat = _catalog(big_rep, '直邮5店 7.10号订单.xlsx') + _catalog(small_rep, '直邮一店 8.20号订单.xlsx')
    out = _run(big_rep, nl.NlIntent(document='麦当劳门店报表', columns=['SKU ID']), catalog=cat)
    assert out['status'] == 'clarify'
    assert len(out['candidates']) == 2


def test_nl_not_excel_passthrough(big_rep):
    out = _run(big_rep, nl.NlIntent(query_type=nl.INTENT_NOT_EXCEL), message='你好，讲个笑话')
    assert out['status'] == 'not_excel'


def test_nl_llm_clarify_passthrough(big_rep):
    out = _run(big_rep, nl.NlIntent(query_type=nl.INTENT_CLARIFY, clarification='你想查哪个文件？'))
    assert out['status'] == 'clarify' and out['message'] == '你想查哪个文件？'


def test_nl_empty_catalog_clarifies():
    out = asyncio.run(nl.run_nl_query('列出前20条SKU', [], intent_override=nl.NlIntent()))
    assert out['status'] == 'clarify' and '还没有上传' in out['message']


def test_nl_without_llm_returns_error_not_fabrication():
    """没有 LLM 且没有注入意图时，必须明确报错，绝不返回编造内容。"""
    cat = _doc_catalog()
    out = asyncio.run(nl.run_nl_query('列出前20条SKU', cat, llm=None))
    assert out['status'] == 'error'


def test_nl_contains_empty_value_is_rejected(big_rep):
    out = _run(big_rep, nl.NlIntent(
        document='5店', columns=['Order ID'],
        filters=[{'column': 'Variation', 'operator': 'contains', 'value': ''}], limit=10,
    ))
    assert out['status'] == 'error'


# ==========================================================================
# 5. 意图解析健壮性（LLM 输出不可信时的兜底）
# ==========================================================================
def test_intent_from_dict_tolerates_dirty_llm_output():
    intent = nl.intent_from_dict({
        'query_type': 'excel_structured',
        'document': ' 5店 ',
        'columns': ['SKU', None, '', 123],
        'filters': [
            {'column': 'Variation', 'operator': 'LIKE', 'value': 'Black'},
            {'column': '', 'operator': 'eq', 'value': 'x'},
            'not-a-dict',
        ],
        'limit': '0',
        'offset': '-3',
    })
    assert intent.document == '5店'
    assert intent.columns == ['SKU']
    # 设计演进（后续 2A）：未知 operator **不再静默降级为 eq**——
    # 那会把用户/模型的条件偷偷改成"等值匹配"，可能给出错误结果。
    # 现在保留原样（小写化），交给严格校验层以 ERR_INVALID_OPERATOR 明确拒绝。
    assert len(intent.filters) == 1 and intent.filters[0]['operator'] == 'like'
    with pytest.raises(nl.NlQueryError) as ei:
        nl.resolve_filters_nl(_DirtySheet(), intent.filters)
    assert ei.value.code == 'filter_operator_invalid'
    assert intent.limit >= 1 and intent.offset >= 0


class _DirtySheet:
    """最小 Sheet 替身（resolve_filters_nl 需要 column_names）。"""

    column_names = ['Variation']


def test_invalid_operator_is_rejected_by_validator(big_rep):
    """端到端：非法 operator 必须被拒绝，绝不静默变成 eq。"""
    out = _run(big_rep, nl.NlIntent(
        document='5店', columns=['Order ID'],
        filters=[{'column': 'Variation', 'operator': 'like', 'value': 'Black'}], limit=10))
    assert out['status'] in ('error', 'clarify')


def test_parse_llm_json_handles_code_fence():
    data = nl.parse_llm_json('```json\n{"query_type":"not_excel","clarification":"x"}\n```')
    assert data['query_type'] == 'not_excel'


def test_parse_llm_json_invalid_raises():
    with pytest.raises(nl.NlQueryError):
        nl.parse_llm_json('这不是 JSON')


def test_format_summary_is_python_generated(big_rep):
    out = _run(big_rep, nl.NlIntent(document='5店', columns=['SKU ID'], limit=5))
    summary = out['message']
    assert '命中总数：447' in summary
    assert '本次返回：5' in summary
    assert 'SKU ID' in summary


# ==========================================================================
# Phase 2：自然语言筛选（新 operator）-> 真实取数
# ==========================================================================
def _run_p2(rep, doc, columns, filters, limit=50, offset=0, session_key='p2') -> Dict[str, Any]:
    intent = nl.NlIntent(document=doc, columns=columns, filters=filters, limit=limit, offset=offset)
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nl.run_nl_query(
            '自然语言筛选', _catalog(rep), intent_override=intent,
            user_id=1, session_key=session_key,
        ))
    finally:
        excel_store.load_representation = original


def _expect_rows(rep, sheet_index, filter_dicts, columns):
    """独立的期望行计算（直接扫 representation）。"""
    sheet = rep.sheets[sheet_index]
    names = sheet.column_names
    ci = [names.index(c) for c in columns]
    out = []
    for r in sheet.rows:
        ok = True
        for f in filter_dicts:
            i = names.index(f['column'])
            cell = r[i]
            op, val = f['operator'], f['value']
            if op == 'contains':
                ok = cell is not None and str(val).lower() in str(cell).lower()
            elif op == 'eq':
                ok = cell is not None and str(cell) == str(val)
            elif op == 'neq':
                ok = cell is not None and str(cell) != str(val)
            else:
                n = _to_f(cell)
                t = _to_f(val)
                ok = n is not None and t is not None and {
                    'gt': n > t, 'gte': n >= t, 'lt': n < t, 'lte': n <= t}[op]
            if not ok:
                break
        if ok:
            out.append([r[j] for j in ci])
    return out


def _to_f(v):
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def test_p2_intent_accepts_new_operators():
    intent = nl.intent_from_dict({
        'query_type': 'excel_structured',
        'filters': [
            {'column': 'Quantity', 'operator': 'GT', 'value': '10'},
            {'column': 'Quantity', 'operator': 'lte', 'value': 50},
            {'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'},
            {'column': 'Order Status', 'operator': 'neq', 'value': '已发货'},
        ],
    })
    ops = [f['operator'] for f in intent.filters]
    assert ops == ['gt', 'lte', 'contains', 'neq']   # 大小写归一 + 新 operator 保留


def test_p2_prefix_relaxation_when_eq_has_no_exact_match(small_rep):
    """『物流商为SF』被解析成 eq 'SF'：该列无精确值，唯一前缀是 SF International
    -> 放宽为 contains 并明确告知（不是静默改语义）。"""
    out = _run_p2(small_rep, '一店', [],
                  [{'column': 'Shipping Provider Name', 'operator': 'eq', 'value': 'SF'}],
                  limit=100, session_key='p2relax')
    assert out['status'] == 'ok'
    assert out['result']['total_matches'] == 4          # 与「包含 SF」一致
    assert out['query']['filters'][0]['operator'] == 'contains'
    assert out['relaxed_filters'] and '前缀放宽' in out['relaxed_filters'][0]
    assert '提示' in out['message']
    # 上下文保存的必须是放宽后的条件，保证「下一页」语义一致
    ctx = nl.ExcelQueryContext(**out['new_context'])
    assert ctx.filters[0]['operator'] == 'contains'


def test_p2_no_relaxation_when_exact_match_exists(big_rep):
    out = _run_p2(big_rep, '5店', ['SKU ID'],
                  [{'column': 'Shipping Provider Name', 'operator': 'eq', 'value': 'SF International'}],
                  limit=5, session_key='p2no1')
    assert out['status'] == 'ok'
    assert out['result']['total_matches'] == 447
    assert out['query']['filters'][0]['operator'] == 'eq'     # 精确命中，不放宽
    assert out['relaxed_filters'] == []


def test_p2_no_relaxation_when_prefix_is_ambiguous(small_rep):
    """前缀对应多个不同真实值 -> 有歧义，不放宽（宁可为空也不猜）。"""
    out = _run_p2(small_rep, '一店', ['Variation'],
                  [{'column': 'Variation', 'operator': 'eq', 'value': 'Christmas'}],
                  limit=50, session_key='p2no2')
    assert out['status'] == 'ok'
    assert out['result']['total_matches'] == 0
    assert out['query']['filters'][0]['operator'] == 'eq'
    assert out['relaxed_filters'] == []


def test_p2_range_operator_requires_number(big_rep):
    """gt/gte/lt/lte 的值不是数值 -> 明确报错，不静默取数。"""
    out = _run_p2(big_rep, '5店', ['SKU ID'],
                  [{'column': 'Quantity', 'operator': 'gt', 'value': '大于十'}])
    assert out['status'] == 'error'
    assert '数值' in out['message']


def test_p2_unknown_operator_rejected_by_validator(big_rep):
    """绕过 intent_from_dict 直接构造非法 operator -> 由校验层拒绝。"""
    intent = nl.NlIntent(document='5店', columns=['SKU ID'],
                         filters=[{'column': 'Quantity', 'operator': 'between', 'value': 5}])
    out = _run_p2(big_rep, '5店', ['SKU ID'], [])
    assert out['status'] == 'ok'   # 空过滤正常
    # 直接调用校验函数验证非法 operator 被拒
    with pytest.raises(nl.NlQueryError) as ei:
        nl.build_validated_query(intent, big_rep)
    assert ei.value.code == 'filter_operator_invalid'


def test_p2_nl_structured_filters_hit_real_rows(big_rep):
    """NL 层使用新 operator 时，结果必须与独立扫描一致。"""
    cases = [
        ('物流商包含SF', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]),
        ('物流商精确等于', [{'column': 'Shipping Provider Name', 'operator': 'eq', 'value': 'SF International'}]),
        ('物流商不等于', [{'column': 'Shipping Provider Name', 'operator': 'neq', 'value': 'Yanwen Express'}]),
        ('金额大于10', [{'column': 'Order Amount', 'operator': 'gt', 'value': 10}]),
        ('金额大于等于20', [{'column': 'Order Amount', 'operator': 'gte', 'value': 20}]),
        ('数量小于5', [{'column': 'Quantity', 'operator': 'lt', 'value': 5}]),
        ('数量小于等于1', [{'column': 'Quantity', 'operator': 'lte', 'value': 1}]),
        ('双条件AND', [{'column': 'Shipping Provider Name', 'operator': 'eq', 'value': 'SF International'},
                      {'column': 'Order Amount', 'operator': 'gt', 'value': 10}]),
        ('三条件AND', [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'},
                     {'column': 'Order Amount', 'operator': 'gt', 'value': 5},
                     {'column': 'Quantity', 'operator': 'gte', 'value': 1}]),
        ('0行', [{'column': 'Quantity', 'operator': 'gt', 'value': 1000}]),
    ]
    for label, filters in cases:
        out = _run_p2(big_rep, '5店', ['Order ID', 'SKU ID'], filters, limit=500)
        assert out['status'] == 'ok', f'{label}: {out.get("message")}'
        assert out['engine'] == 'duckdb', f'{label}: 未走 DuckDB 引擎'
        exp = _expect_rows(big_rep, 0, filters, ['Order ID', 'SKU ID'])
        assert out['result']['rows'] == exp, f'{label}: 行内容与独立扫描不一致'
        assert out['result']['total_matches'] == len(exp), f'{label}: 命中数不一致'
        assert [f['operator'] for f in out['result']['applied_filters']] == [f['operator'] for f in filters]


def test_p2_pagination_over_filtered_query(big_rep):
    """Phase 2 筛选 + Phase 1D 分页：筛选条件必须被继承，offset 由 Python 计算。"""
    filters = [{'column': 'Order Amount', 'operator': 'gt', 'value': 1}]
    o1 = _run_p2(big_rep, '5店', ['Order ID'], filters, limit=20, session_key='p2page')
    assert o1['status'] == 'ok'
    ctx = nl.ExcelQueryContext(**o1['new_context'])
    assert ctx.filters == filters

    exp_all = _expect_rows(big_rep, 0, filters, ['Order ID'])
    assert o1['result']['rows'] == exp_all[:20]

    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: big_rep
    try:
        o2 = asyncio.run(nl.run_nl_query(
            '再来20条', _catalog(big_rep), context=ctx,
            pagination_override=nl.TurnIntent(action=nl.ACTION_NEXT, limit=20),
            user_id=1, session_key='p2page',
        ))
    finally:
        excel_store.load_representation = original

    assert o2['status'] == 'ok' and o2['continued'] is True
    assert o2['engine'] == 'duckdb'
    assert o2['query']['filters'] == filters              # 筛选条件被继承
    assert o2['result']['rows'] == exp_all[20:40]
    assert o2['result']['offset'] == 20
