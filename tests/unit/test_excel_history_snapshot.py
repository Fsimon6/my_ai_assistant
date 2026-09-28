# -*- coding: utf-8 -*-
"""第 3 项「结果与可解释性」：Excel 历史快照（meta_info['excel']）单元测试。

覆盖：
  * 四类结构化结果的快照契约（前端 builder 依赖字段必须齐全）；
  * ``source`` 键保持不变（旧历史兼容 / exclude_sources 过滤不受影响）；
  * 标准 JSON 可序列化（Decimal / datetime / date 安全转换）；
  * 快照**不包含**实时执行上下文与内部细节、**不包含**任何密钥 / prompt；
  * 体积控制（超上限截断 + ``history_truncated`` 显式标记）。

全部为离线纯函数测试，不调用 LLM、不触碰 DB。
"""
import json
from datetime import date, datetime
from decimal import Decimal

from backend.api.v1.rag import (
    EXCEL_HISTORY_MAX_ROWS,
    EXCEL_HISTORY_SCHEMA_VERSION,
    _json_safe,
    build_excel_history_snapshot,
    build_excel_turn_meta,
)


# ---------------------------------------------------------------------------
# 合成 outcome（形状与真实执行器 to_dict 一致）
# ---------------------------------------------------------------------------
def _outcome(kind: str) -> dict:
    base = {
        'status': 'ok',
        'message': '摘要文本',
        'engine': 'duckdb',
        'inherited_from': '',
        'relaxed_filters': ['物流商 eq SF → contains SF'],
        'continued': False,
        'document': {'document_id': 'DOC1', 'filename': '直邮一店 8.20号订单.xlsx'},
        'sheet': {'sheet_index': 0, 'sheet_name': 'OrderSKUList'},
        # 实时执行状态：**绝不允许**进入快照
        'new_context': {'document_id': 'DOC1', 'session_key': 's1'},
        'new_aggregate_context': {'document_id': 'DOC1', 'matched_rows': 19},
        'new_analysis_context': {'document_id': 'DOC1', 'step2_value': 106.6},
        'intent': {'document': 'DOC1', 'api_key': 'sk-should-never-appear'},
        'turn': {'action': 'aggregate'},
        'statistical_guard': False,
    }
    cols = [{'name': 'Order ID', 'index': 0, 'excel_column': 1, 'excel_column_letter': 'A',
             'dtype': 'string', 'semantic_type': 'text', 'non_empty': 19, 'null_count': 0},
            {'name': 'Order Amount', 'index': 1, 'excel_column': 27, 'excel_column_letter': 'AA',
             'dtype': 'string', 'semantic_type': 'number', 'non_empty': 19, 'null_count': 0}]
    group_cols = [{'name': 'Shipping Provider Name', 'index': 9, 'excel_column': 10,
                   'excel_column_letter': 'J'}]
    rows = [{'group_key': ['SF International'], 'group_display': ['SF International'],
             'value': 66.3, 'value_display': '66.3', 'matched_rows': 4,
             'numeric_rows': 4, 'empty_rows': 0, 'non_numeric_rows': 0,
             'group': [{'column': 'Shipping Provider Name', 'display': 'SF International',
                        'is_null': False, 'value': 'SF International'}]}]
    if kind == 'result':
        base['result'] = {
            'document_id': 'DOC1', 'sheet_index': 0, 'sheet_name': 'OrderSKUList',
            'columns': cols, 'rows': [['577531902952575236', '59.32']],
            'row_excel_numbers': [3], 'row_ranges': ['3~3'],
            'offset': 0, 'limit': 50, 'returned_count': 1, 'total_matches': 19,
            'total_rows_in_sheet': 19, 'has_more': True, 'next_offset': 1,
            'applied_filters': [{'column': 'Shipping Provider Name', 'resolved_column': 'Shipping Provider Name',
                                 'operator': 'contains', 'value': 'SF'}],
            'calculation': {'label': '金额/数量', 'operation': 'div', 'operation_label': '相除',
                            'right_column': 'Quantity'},
            'calc_filter': {'operator': 'gt', 'value': 10},
            'engine': 'duckdb', 'created_at': datetime(2026, 9, 28, 12, 0, 0),
        }
    elif kind == 'aggregate':
        base['aggregate'] = {
            'document_id': 'DOC1', 'sheet_index': 0, 'sheet_name': 'OrderSKUList',
            'operation': 'sum', 'operation_label': '求和', 'column': 'Order Amount',
            'column_index': 26, 'column_letter': 'AA', 'column_numeric_in_sheet': True,
            'value': Decimal('306.02'), 'value_display': '306.02', 'matched_rows': 19,
            'numeric_rows': 19, 'empty_rows': 0, 'non_numeric_rows': 0,
            'total_rows_in_sheet': 19, 'row_excel_numbers': [3, 4, 5],
            'row_excel_spans': {'first': 3, 'last': 21},
            'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}],
            'definition': 'SUM(Order Amount)',
            'variants': date(2026, 9, 28),      # 非 JSON 类型 -> 需安全转换
        }
    elif kind == 'group_aggregate':
        base['group_aggregate'] = {
            'kind': 'group', 'document_id': 'DOC1', 'sheet_index': 0,
            'sheet_name': 'OrderSKUList', 'operation': 'sum', 'operation_label': '求和',
            'column': 'Order Amount', 'column_index': 26, 'column_letter': 'AA',
            'column_numeric_in_sheet': True, 'group_by': group_cols, 'rows': rows,
            'total_groups': 3, 'returned_groups': 1, 'top_n': 1,
            'order_by': 'aggregate_value', 'order_by_column': '', 'order_by_label': '聚合值',
            'order_dir': 'desc', 'order_dir_label': '从高到低', 'sorted': True,
            'sort_description': '按聚合值从高到低', 'truncated_by_top_n': True,
            'matched_rows': 19, 'total_rows_in_sheet': 19,
            'row_excel_spans': {'first': 3, 'last': 21},
            'applied_filters': [{'column': 'Quantity', 'operator': 'gt', 'value': 3}],
            'definition': 'SUM(Order Amount) GROUP BY Shipping Provider Name',
        }
    else:  # multi_step
        base['multi_step'] = {
            'kind': 'multi_step', 'document_id': 'DOC1', 'filename': '直邮一店 8.20号订单.xlsx',
            'sheet_index': 0, 'sheet_name': 'OrderSKUList', 'engine': 'duckdb',
            'max_steps': 2, 'step_count': 2, 'value': 106.6, 'value_display': '106.6',
            'step1': {'type': 'group_aggregate', 'sheet_name': 'OrderSKUList', 'operation': 'sum',
                      'operation_label': '求和', 'column': 'Order Amount',
                      'group_by': [{'name': 'SKU ID', 'index': 1, 'excel_column': 2}],
                      'rows': rows, 'total_groups': 15, 'returned_groups': 3, 'top_n': 3,
                      'order_by': 'aggregate_value', 'order_by_label': '聚合值',
                      'order_dir': 'desc', 'sorted': True, 'sort_description': '按聚合值从高到低',
                      'truncated_by_top_n': True, 'matched_rows': 19, 'total_rows_in_sheet': 19,
                      'row_excel_spans': {'first': 3, 'last': 21},
                      'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains',
                                   'value': 'SF'}],
                      'source': 'excel'},
            'step2': {'type': 'aggregate', 'operation': 'sum', 'operation_label': '求和',
                      'source': 'step_1', 'source_text': 'Step 1 的结果',
                      'input_rows': 3, 'numeric_rows': 3, 'value': 106.6,
                      'value_display': '106.6'},
            'step2_input_values': [59.32, 35.33, 33.2],
            'plan': {'max_steps': 2, 'steps': [
                {'type': 'group_aggregate', 'operation': 'sum', 'column': 'Order Amount',
                 'group_by': ['SKU ID'], 'order_by': 'aggregate_value', 'order_dir': 'desc',
                 'top_n': 3},
                {'type': 'aggregate', 'operation': 'sum', 'column': 'aggregate_value'}]},
            'definition': '两步分析',
        }
    return base


ALL_KINDS = ('result', 'aggregate', 'group_aggregate', 'multi_step')


# ---------------------------------------------------------------------------
# 1-4：四类快照契约（前端 builder 依赖字段必须齐全）
# ---------------------------------------------------------------------------
def test_result_snapshot_contract():
    snap = build_excel_history_snapshot(_outcome('result'))
    assert snap['schema_version'] == EXCEL_HISTORY_SCHEMA_VERSION
    assert snap['kind'] == 'result'
    assert snap['document']['filename'] == '直邮一店 8.20号订单.xlsx'
    assert snap['sheet']['sheet_name'] == 'OrderSKUList'
    p = snap['payload']
    for key in ('columns', 'rows', 'row_excel_numbers', 'row_ranges', 'offset', 'limit',
                'returned_count', 'total_matches', 'total_rows_in_sheet', 'has_more',
                'next_offset', 'applied_filters', 'calculation', 'calc_filter', 'sheet_name'):
        assert key in p, 'result 快照缺少前端 builder 依赖字段：%s' % key
    assert p['columns'][1]['excel_column_letter'] == 'AA'      # 解释性：列字母
    assert p['applied_filters'][0]['operator'] == 'contains'   # 解释性：筛选口径


def test_aggregate_snapshot_contract():
    snap = build_excel_history_snapshot(_outcome('aggregate'))
    assert snap['kind'] == 'aggregate'
    p = snap['payload']
    for key in ('operation', 'operation_label', 'column', 'column_letter', 'value',
                'value_display', 'matched_rows', 'numeric_rows', 'empty_rows',
                'non_numeric_rows', 'total_rows_in_sheet', 'row_excel_numbers',
                'row_excel_spans', 'filters', 'definition', 'sheet_name'):
        assert key in p, 'aggregate 快照缺少字段：%s' % key
    assert p['value'] == 306.02                                # Decimal -> float（JSON 安全）


def test_group_aggregate_snapshot_contract():
    snap = build_excel_history_snapshot(_outcome('group_aggregate'))
    assert snap['kind'] == 'group_aggregate'
    p = snap['payload']
    for key in ('group_by', 'rows', 'operation', 'operation_label', 'column', 'total_groups',
                'returned_groups', 'top_n', 'order_by', 'order_by_label', 'order_dir',
                'order_dir_label', 'sorted', 'sort_description', 'truncated_by_top_n',
                'matched_rows', 'total_rows_in_sheet', 'row_excel_spans', 'kind',
                'applied_filters', 'sheet_name'):
        assert key in p, 'group_aggregate 快照缺少字段：%s' % key
    assert p['group_by'][0]['name'] == 'Shipping Provider Name'
    row = p['rows'][0]
    for key in ('group_key', 'group_display', 'value', 'value_display', 'matched_rows',
                'numeric_rows'):
        assert key in row, 'group 行缺少字段：%s' % key


def test_multi_step_snapshot_contract():
    snap = build_excel_history_snapshot(_outcome('multi_step'))
    assert snap['kind'] == 'multi_step'
    p = snap['payload']
    for key in ('step1', 'step2', 'plan', 'value', 'value_display', 'step_count', 'max_steps',
                'engine', 'filename', 'definition', 'step2_input_values', 'sheet_name'):
        assert key in p, 'multi_step 快照缺少字段：%s' % key
    assert p['step1']['group_by'][0]['name'] == 'SKU ID'
    assert p['step1']['rows'][0]['value_display'] == '66.3'
    assert p['step2']['input_rows'] == 3


# ---------------------------------------------------------------------------
# 5：source 键保持不变（旧历史兼容 / exclude_sources 过滤依据）
# ---------------------------------------------------------------------------
def test_source_key_preserved_and_old_history_meta():
    snap = build_excel_history_snapshot(_outcome('result'))
    meta = build_excel_turn_meta(snap)
    assert meta['source'] == 'excel'          # 键名/取值不得变化
    assert meta['excel'] is snap
    assert 'source' not in snap               # 快照本身不重复存 source
    # 旧历史 / 无结构化结果：meta 只有 source（前端回退纯文字）
    assert build_excel_turn_meta(None) == {'source': 'excel'}
    old_meta = {'source': 'excel'}            # 模拟旧历史行
    assert old_meta.get('excel') is None


# ---------------------------------------------------------------------------
# 6：标准 JSON 序列化（Decimal / datetime / date 安全转换）
# ---------------------------------------------------------------------------
def test_snapshot_is_json_serializable():
    for kind in ALL_KINDS:
        snap = build_excel_history_snapshot(_outcome(kind))
        dumped = json.dumps(snap, ensure_ascii=False)       # 不抛异常即通过
        assert json.loads(dumped)['kind'] == kind
    p = build_excel_history_snapshot(_outcome('aggregate'))['payload']
    assert isinstance(p['value'], float)                     # Decimal -> float
    # 白名单：非展示所需键（即使后端给了）一律不落快照
    assert 'variants' not in p
    # 非 JSON 类型的安全转换（Decimal / date / datetime / set / numpy 风格 item()）
    assert _json_safe(Decimal('1.5')) == 1.5
    assert _json_safe(date(2026, 9, 28)) == '2026-09-28'
    assert _json_safe(datetime(2026, 9, 28, 12, 0, 0)) == '2026-09-28T12:00:00'
    assert _json_safe({'a': [Decimal('2'), {'b': date(2026, 1, 1)}]}) == \
        {'a': [2.0, {'b': '2026-01-01'}]}
    assert json.dumps(_json_safe({'x': {Decimal('3')}}), default=None) is not None


# ---------------------------------------------------------------------------
# 7：不包含实时执行上下文与内部细节
# ---------------------------------------------------------------------------
def test_snapshot_excludes_execution_context_and_internals():
    for kind in ALL_KINDS:
        dumped = json.dumps(build_excel_history_snapshot(_outcome(kind)), ensure_ascii=False)
        for forbidden in ('new_context', 'new_aggregate_context', 'new_analysis_context',
                          'session_key', 'api_key', 'statistical_guard', '"intent"', '"turn"'):
            assert forbidden not in dumped, '%s 快照泄漏了 %s' % (kind, forbidden)


# ---------------------------------------------------------------------------
# 8：不包含任何密钥 / prompt
# ---------------------------------------------------------------------------
def test_snapshot_contains_no_secrets():
    for kind in ALL_KINDS:
        dumped = json.dumps(build_excel_history_snapshot(_outcome(kind)),
                            ensure_ascii=False).lower()
        for forbidden in ('sk-', 'secret', 'password', 'bearer', 'prompt:', 'dashscope'):
            assert forbidden not in dumped, '%s 快照疑似含密钥/prompt：%s' % (kind, forbidden)


# ---------------------------------------------------------------------------
# 9：无结构化结果 -> None（澄清 / 报错 / not_excel 不写快照）
# ---------------------------------------------------------------------------
def test_no_structured_result_returns_none():
    assert build_excel_history_snapshot({'status': 'clarify', 'message': '需要补充信息'}) is None
    assert build_excel_history_snapshot({'status': 'error', 'message': '失败'}) is None
    assert build_excel_history_snapshot(None) is None
    assert build_excel_history_snapshot({'status': 'not_excel'}) is None


# ---------------------------------------------------------------------------
# 体积控制：超上限截断 + 显式 history_truncated（不假装完整）
# ---------------------------------------------------------------------------
def test_history_snapshot_truncates_with_flag():
    over = EXCEL_HISTORY_MAX_ROWS + 1
    out = _outcome('result')
    out['result']['rows'] = [['O%d' % i, '1'] for i in range(over)]
    out['result']['row_excel_numbers'] = list(range(over))
    snap = build_excel_history_snapshot(out)
    assert snap['history_truncated'] is True
    assert len(snap['payload']['rows']) == EXCEL_HISTORY_MAX_ROWS
    assert len(snap['payload']['row_excel_numbers']) == EXCEL_HISTORY_MAX_ROWS
    assert snap['payload']['total_matches'] == 19      # 真实总数仍保留（不误导）

    out2 = _outcome('group_aggregate')
    out2['group_aggregate']['rows'] = [dict(out2['group_aggregate']['rows'][0])] * over
    snap2 = build_excel_history_snapshot(out2)
    assert snap2['history_truncated'] is True
    assert len(snap2['payload']['rows']) == EXCEL_HISTORY_MAX_ROWS
    assert snap2['payload']['total_groups'] == 3       # 真实分组总数仍保留

    out3 = _outcome('multi_step')
    out3['multi_step']['step1']['rows'] = [dict(out3['multi_step']['step1']['rows'][0])] * over
    snap3 = build_excel_history_snapshot(out3)
    assert snap3['history_truncated'] is True
    assert len(snap3['payload']['step1']['rows']) == EXCEL_HISTORY_MAX_ROWS

    normal = build_excel_history_snapshot(_outcome('result'))
    assert normal['history_truncated'] is False
