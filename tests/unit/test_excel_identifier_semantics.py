# -*- coding: utf-8 -*-
"""稳定性补丁：业务标识列（18/19 位数字 ID）的语义、精度与拒绝规则。

覆盖：
1. classify_semantic_type 判定（纯函数）；
2. 真实表格：SKU ID / Order ID 被判为 identifier；
3. SUM/AVG/MIN/MAX(identifier) 必须被明确拒绝（COUNT 允许）；
4. 18 位 ID 精确匹配逐位保真，且**不会**因 double 舍入误命中相邻 ID；
5. GROUP BY / ORDER BY / 多步分析中的 ID 逐位保真；
6. NL 层把该错误映射成"明确澄清 + 可统计的数值列候选"。
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from backend.excel import aggregate as agg
from backend.excel import duck as duck_engine
from backend.excel import engine as query_engine
from backend.excel import multi_step as ms
from backend.excel import nl_query as nl
from backend.excel import query as q
from backend.excel import representation as repr_mod
from backend.excel import store as excel_store
from backend.excel.parser import ExcelParser

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMALL_REAL = PROJECT_ROOT / '直邮一店 8.20号订单.xlsx'


# ==========================================================================
# 0. 纯函数：语义类型判定
# ==========================================================================
@pytest.mark.parametrize('name,values,dtype,expected', [
    # 18/19 位数字串 + 像 ID 的名字
    ('SKU ID', ['1732479499051831993', '1732479499051831994'], 'string',
     repr_mod.SEMANTIC_IDENTIFIER),
    ('Order ID', ['577471376427028924'], 'string', repr_mod.SEMANTIC_IDENTIFIER),
    # 名字不像 ID，但值是很长的数字串（时间戳 / 雪花 ID）-> 仍判标识
    ('外键', ['1234567890123456789'], 'string', repr_mod.SEMANTIC_IDENTIFIER),
    # 长数字串 + 名字像 ID -> 标识（如 "订单编号 100001"）
    ('订单编号', ['100001', '100002', '100003'], 'string', repr_mod.SEMANTIC_IDENTIFIER),
    # 文本型数字列（"1"/"2"）不是标识，而是数值列
    ('Quantity', ['1', '2', '1'], 'string', repr_mod.SEMANTIC_NUMBER),
    # 真实金额列
    ('Order Amount', [0.0, 18.12, 35.33], 'number', repr_mod.SEMANTIC_NUMBER),
    ('Order Amount', ['0', '18.12', '35.33'], 'string', repr_mod.SEMANTIC_NUMBER),
    # 纯文本
    ('Product Name', ['A', 'B'], 'string', repr_mod.SEMANTIC_TEXT),
    # 全空
    ('Buyer Message', [None, None], 'empty', repr_mod.SEMANTIC_EMPTY),
    # 日期
    ('Created', ['2026-08-20'], 'date', repr_mod.SEMANTIC_DATE),
    # 布尔
    ('Is Paid', [True, False], 'boolean', repr_mod.SEMANTIC_BOOLEAN),
])
def test_classify_semantic_type(name, values, dtype, expected):
    assert repr_mod.classify_semantic_type(name, values, dtype) == expected


def test_identifier_requires_digit_majority():
    """字母数字混合（如物流单号 LP123456789CN）不算纯数字编号列。"""
    values = ['LP123456789CN', 'LP987654321CN', 'LP111111111CN']
    assert repr_mod.classify_semantic_type('Tracking ID', values, 'string') == repr_mod.SEMANTIC_TEXT


# ==========================================================================
# 1. 真实表格 fixture（与其它 Excel 测试一致：解析真实 xlsx）
# ==========================================================================
@pytest.fixture(scope='module')
def small_rep():
    assert SMALL_REAL.exists(), f'缺少测试文件：{SMALL_REAL}'
    rep = ExcelParser().parse(file_path=str(SMALL_REAL), document_id='idsem_small',
                              user_id=1, filename=SMALL_REAL.name, file_type='xlsx')
    for sheet in rep.sheets:
        sheet.ensure_semantic_types()
    return rep


def _sheet(rep):
    return rep.sheets[0]


def test_real_sheet_identifies_business_id_columns(small_rep):
    sheet = _sheet(small_rep)
    ids = set(sheet.identifier_columns())
    assert 'SKU ID' in ids
    assert 'Order ID' in ids
    col = {c.name: c for c in sheet.columns}
    assert col['Order Amount'].semantic_type == repr_mod.SEMANTIC_NUMBER
    # Quantity 是短数字串，不属于标识列（仍可数值统计）
    assert col['Quantity'].semantic_type != repr_mod.SEMANTIC_IDENTIFIER


def test_old_representation_backfilled_on_load(small_rep, tmp_path, monkeypatch):
    """旧 representation.json（无 semantic_type）在读取时自动补齐。"""
    data = small_rep.to_dict()
    for sheet in data['sheets']:
        for col in sheet['columns']:
            col.pop('semantic_type', None)   # 模拟旧版本落盘
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path)
    doc_dir = tmp_path / 'legacy_doc'
    doc_dir.mkdir(parents=True)
    (doc_dir / 'representation.json').write_text(
        json.dumps(data, ensure_ascii=False), encoding='utf-8')
    loaded = excel_store.load_representation('legacy_doc')
    assert loaded is not None
    col = {c.name: c for c in loaded.sheets[0].columns}
    assert col['SKU ID'].semantic_type == repr_mod.SEMANTIC_IDENTIFIER


# ==========================================================================
# 2. 数值聚合必须拒绝标识列
# ==========================================================================
@pytest.mark.parametrize('op', ['sum', 'avg', 'min', 'max'])
def test_numeric_aggregate_on_identifier_rejected(small_rep, op):
    for col in ('SKU ID', 'Order ID'):
        with pytest.raises(q.ExcelQueryError) as ei:
            query_engine.run_aggregate(small_rep, {'operation': op, 'column': col},
                                       engine='duckdb')
        assert ei.value.code == agg.ERR_IDENTIFIER_NOT_NUMERIC
        assert col in ei.value.message
        assert 'COUNT' in ei.value.message
        # Python 参考引擎必须同样拒绝（两条链路语义一致）
        with pytest.raises(q.ExcelQueryError) as ei2:
            query_engine.run_aggregate(small_rep, {'operation': op, 'column': col},
                                       engine='python')
        assert ei2.value.code == agg.ERR_IDENTIFIER_NOT_NUMERIC


def test_count_on_identifier_still_allowed(small_rep):
    res, engine = query_engine.run_aggregate(
        small_rep, {'operation': 'count', 'column': 'SKU ID'}, engine='duckdb')
    assert engine == 'duckdb'
    assert res.value == 19


def test_group_by_identifier_count_allowed(small_rep):
    """按 SKU ID 分组计数是合法用法（不是对 ID 做数值聚合）。"""
    res, _ = query_engine.run_aggregate(
        small_rep, {'operation': 'count', 'group_by': ['SKU ID']}, engine='duckdb')
    assert res.total_groups == 15


# ==========================================================================
# 3. 18 位 ID 精度：逐位保真 + 不因 double 舍入误命中
# ==========================================================================
def _synthetic_rep(prefix: str, ids: List[str], amounts: List[float]) -> Any:
    """构造只含 [SKU ID, Order Amount] 两列的最小 representation。"""
    from backend.excel.representation import ColumnMeta, SheetRepresentation, WorkbookRepresentation
    return WorkbookRepresentation(
        schema_version=repr_mod.SCHEMA_VERSION,
        document_id=prefix,
        user_id=1,
        filename=f'{prefix}.xlsx',
        file_type='xlsx',
        parser='synthetic',
        sheet_count=1,
        sheets=[SheetRepresentation(
            sheet_name='S1', sheet_index=0, header_mode=repr_mod.HEADER_SINGLE,
            header_rows_excel=[1], header_depth=1,
            columns=[
                ColumnMeta(name='SKU ID', index=0, excel_column=1, excel_column_letter='A',
                           dtype='string', semantic_type=repr_mod.SEMANTIC_IDENTIFIER,
                           non_empty=len(ids)),
                ColumnMeta(name='Order Amount', index=1, excel_column=2, excel_column_letter='B',
                           dtype='number', semantic_type=repr_mod.SEMANTIC_NUMBER,
                           non_empty=len(amounts)),
            ],
            rows=[[i, a] for i, a in zip(ids, amounts)],
            row_excel_numbers=list(range(2, 2 + len(ids))),
            row_count=len(ids), column_count=2,
        )],
    )


#: 相邻两个 19 位 ID：作为 double 会舍入成**同一个值**（差值 1 << ulp≈256）
NEAR_A = '1732479499051831993'
NEAR_B = '1732479499051831994'


def test_long_digit_eq_is_bit_exact_not_double():
    """精确匹配必须逐位比较：相邻 ID（double 相同）不得互相误命中。"""
    assert float(NEAR_A) == float(NEAR_B), '前置条件：两者作为 double 相等'
    rep = _synthetic_rep('idsem_near', [NEAR_A, NEAR_B], [1.0, 2.0])
    res, engine = query_engine.run_structured_query(rep, {
        'filters': [{'column': 'SKU ID', 'operator': 'eq', 'value': NEAR_A}]},
        engine='duckdb')
    assert engine == 'duckdb'
    assert res.total_matches == 1
    assert res.rows[0][0] == NEAR_A

    res_py, engine_py = query_engine.run_structured_query(rep, {
        'filters': [{'column': 'SKU ID', 'operator': 'eq', 'value': NEAR_A}]},
        engine='python')
    assert engine_py == 'python'
    assert res_py.total_matches == 1
    assert res_py.rows[0][0] == NEAR_A

    # neq 同理：不应把"另一个相邻 ID"当成相等而漏掉
    res_neq, _ = query_engine.run_structured_query(rep, {
        'filters': [{'column': 'SKU ID', 'operator': 'neq', 'value': NEAR_A}]},
        engine='duckdb')
    assert res_neq.total_matches == 1
    assert res_neq.rows[0][0] == NEAR_B


def test_real_19_digit_lookup_is_exact(small_rep):
    sheet = _sheet(small_rep)
    idx = sheet.column_names.index('SKU ID')
    target = sheet.rows[0][idx]
    res, _ = query_engine.run_structured_query(small_rep, {
        'filters': [{'column': 'SKU ID', 'operator': 'eq', 'value': target}]}, engine='duckdb')
    assert res.total_matches == 1
    assert res.rows[0][idx] == target
    assert len(str(res.rows[0][idx])) == len(target)


def test_group_keys_and_sort_use_string_semantics():
    """分组键逐位保真；按 ID 排序是字符串序（18 位 '9…' 排在 19 位 '1…' 之前）。"""
    big = '1000000000000000000'      # 19 位 -> 数值更大
    small = '900000000000000000'     # 18 位 -> 字符串更小
    rep = _synthetic_rep('idsem_sort', [big, small], [1.0, 2.0])
    res, _ = query_engine.run_aggregate(rep, {
        'operation': 'count', 'group_by': ['SKU ID'],
        'order_by': 'SKU ID', 'order_dir': 'asc'}, engine='duckdb')
    keys = [r.group[0].value for r in res.rows]
    assert keys == [big, small]          # 字符串序
    assert keys != sorted(keys, key=lambda k: float(k))   # 数值序会相反
    assert all(isinstance(k, str) for k in keys)

    res_py, _ = query_engine.run_aggregate(rep, {
        'operation': 'count', 'group_by': ['SKU ID'],
        'order_by': 'SKU ID', 'order_dir': 'asc'}, engine='python')
    assert [r.group[0].value for r in res_py.rows] == [big, small]


def test_multi_step_keeps_id_bit_exact(small_rep):
    """多步分析：Step 1 的分组键（19 位 SKU ID）逐位保真，Step 2 只用 Step 1 结果。"""
    sheet = _sheet(small_rep)
    idx = sheet.column_names.index('SKU ID')
    all_ids = {r[idx] for r in sheet.rows}
    plan = ms.normalize_analysis_plan({'document': small_rep.filename, 'steps': [
        {'type': 'group_aggregate', 'group_by': ['SKU ID'], 'operation': 'sum',
         'column': 'Order Amount', 'order_by': 'aggregate_value', 'order_dir': 'desc',
         'top_n': 10},
        {'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
         'column': 'aggregate_value'}]})
    result, engine = ms.execute_analysis(small_rep, plan)
    assert engine == 'duckdb'
    keys = [r.group[0].value for r in result.step1.rows]
    assert keys and all(k in all_ids for k in keys)
    assert all(isinstance(k, str) for k in keys)
    # Step 2 = Step 1 十个值之和
    assert result.step2_matched == len(keys)
    assert result.step2_value == pytest.approx(sum(r.value for r in result.step1.rows), rel=1e-9)


# ==========================================================================
# 4. NL 层：必须"明确澄清"，而不是执行或瞎猜
# ==========================================================================
class _FakeLLM:
    """按 system prompt 分派：turn / stat_guard / analysis_guard。"""

    def __init__(self, turn=None, stat=None, analysis=None):
        self.turn, self.stat, self.analysis = turn, stat, analysis
        self.calls: List[str] = []

    async def chat_completion(self, messages, stream=False, temperature=None):
        system = messages[0]['content']
        if system.startswith('你是一个「统计参数抽取器」'):
            kind, payload = 'stat_guard', self.stat
        elif system.startswith('你是一个「两阶段分析计划生成器」'):
            kind, payload = 'analysis_guard', self.analysis
        else:
            kind, payload = 'turn', self.turn
        self.calls.append(kind)
        if payload is None:
            raise RuntimeError(f'no payload for {kind}')
        yield json.dumps(payload, ensure_ascii=False)


def _catalog(rep):
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


def _nl(rep, message, llm):
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nl.run_nl_query(message, _catalog(rep), llm=llm))
    finally:
        excel_store.load_representation = original


def test_nl_sum_on_id_clarifies(small_rep):
    llm = _FakeLLM(turn={'action': 'aggregate', 'aggregate_operation': 'sum',
                         'column': 'SKU ID', 'document': '一店'})
    out = _nl(small_rep, '一店 SKU ID 总和是多少？', llm)
    assert out['status'] == 'clarify'
    assert 'SKU ID' in out['message'] and 'COUNT' in out['message']
    # 候选列里不应再出现标识列本身，避免把用户又引回同一个错误
    assert 'SKU ID' not in (out.get('candidates') or [])


def test_nl_avg_on_order_id_clarifies(small_rep):
    llm = _FakeLLM(turn={'action': 'aggregate', 'aggregate_operation': 'avg',
                         'column': 'Order ID', 'document': '一店'})
    out = _nl(small_rep, '一店 Order ID 的平均值是多少？', llm)
    assert out['status'] == 'clarify'
    assert 'Order ID' in out['message']


def test_nl_amount_still_works_after_patch(small_rep):
    """打补丁后，真实数值列的统计不受影响（回归）。"""
    llm = _FakeLLM(turn={'action': 'aggregate', 'aggregate_operation': 'sum',
                         'column': 'Order Amount', 'document': '一店'})
    out = _nl(small_rep, '直邮一店订单金额总和是多少？', llm)
    assert out['status'] == 'ok'
    assert out['aggregate']['value'] == pytest.approx(306.02, rel=1e-9)
