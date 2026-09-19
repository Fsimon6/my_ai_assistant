# -*- coding: utf-8 -*-
"""Phase 3 自然语言**统计路由**测试矩阵。

为什么需要这个文件
------------------
Phase 3A/3B/3C 的统计引擎本身已被单独测试，但"用户这句话有没有走进统计引擎"
此前**没有任何测试覆盖**：只要 LLM 把统计问题判成 action="new_query"，
用户就会看到"普通表格查询直接列出原始行"，而统计链路根本没被触发。

本文件因此同时断言 **三个层次**（缺一不可）：
    intent/action 正确  +  executor 正确  +  最终结果正确

并且：
- 用 `FakeLLM` 把"LLM 说错"的场景钉死，验证**确定性统计兜底**能把统计问题救回统计链路；
- 用**独立 Ground Truth**（本文件内手写 扫描→分组→聚合→排序→TOP-N，不 import 被测实现）
  逐项比较聚合值 / 分组顺序 / TOP-N 截断 / total_groups。
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from backend.excel.parser import ExcelParser

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMALL_REAL = PROJECT_ROOT / '直邮一店 8.20号订单.xlsx'

TOL = dict(rel=1e-9, abs=1e-6)

SUM_VALUE_EXPECT = 306.02
AVG_VALUE_EXPECT = 16.106315789473683
MIN_VALUE_EXPECT = 0.0
MAX_VALUE_EXPECT = 35.33


# ==========================================================================
# 夹具：真实小表（19 行）
# ==========================================================================
@pytest.fixture(scope='module')
def small_rep():
    if not SMALL_REAL.exists():
        pytest.skip('未找到小表（直邮一店 8.20号订单.xlsx）')
    return ExcelParser().parse(str(SMALL_REAL), 'route-doc', user_id=1, filename=SMALL_REAL.name)


@pytest.fixture(scope='module')
def catalog(small_rep):
    return [{
        'document_id': small_rep.document_id,
        'filename': small_rep.filename,
        'file_type': small_rep.file_type,
        'created_at': small_rep.created_at,
        'total_rows': small_rep.total_rows,
        'sheets': [
            {
                'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                'row_count': s.row_count, 'column_count': s.column_count,
                'columns': s.column_names,
            }
            for s in small_rep.sheets
        ],
    }]


@pytest.fixture()
def patched_store(small_rep, monkeypatch):
    monkeypatch.setattr(excel_store, 'load_representation',
                        lambda document_id, user_id=None: small_rep)


# ==========================================================================
# Fake LLM：按 system prompt 区分「轮次解析」与「统计参数抽取」两次调用
# ==========================================================================
class FakeLLM:
    """可编程的假 LLM（只实现 chat_completion）。"""

    def __init__(self, turn_json: Dict[str, Any], aggregate_json: Optional[Dict[str, Any]] = None):
        self.turn_json = turn_json
        self.aggregate_json = aggregate_json
        self.calls: List[Dict[str, Any]] = []

    async def chat_completion(self, messages, stream=False, temperature=None):
        system = messages[0]['content']
        kind = 'aggregate' if system.startswith('你是一个「统计参数抽取器」') else 'turn'
        self.calls.append({'kind': kind, 'temperature': temperature})
        payload = self.aggregate_json if kind == 'aggregate' else self.turn_json
        if payload is None:
            raise RuntimeError(f'FakeLLM 未提供 {kind} 响应')
        yield json.dumps(payload, ensure_ascii=False)


def _run(message, llm, catalog, **kw):
    return asyncio.run(nl.run_nl_query(message, catalog, llm=llm, **kw))


# ==========================================================================
# 1. 确定性统计语义判定（纯 Python，不经过 LLM）
# ==========================================================================
STAT_POSITIVE = [
    '直邮一店订单金额总和是多少？',
    '一店订单金额合计是多少',
    '一共多少钱',
    '直邮一店平均订单金额是多少？',
    '订单金额平均值是多少',
    '一店订单金额最小是多少？',
    '一店订单金额最大是多少？',
    '一店订单金额最低是多少',
    '每个物流商分别有多少单？',
    '一店每个物流商的订单金额总和是多少？',
    '按物流商统计平均订单金额',
    '一店哪个物流商订单最多？',
    '一店按订单数量从高到低排列各物流商。',
    '一店SF物流下订单金额最高的3个支付方式是什么？',
    '找出一店中金额最高的10个SKU，并统计它们的总销售额。',
    '销量最高的10个SKU是哪些？',
    '物流商为SF的有几单？',
    '订单金额排名前10的物流商',
]

STAT_NEGATIVE = [
    # 普通列表查询（必须继续走 structured query）
    '物流商为 SF 的订单有哪些？',
    '列出5店前20条SKU',
    '显示全部订单',
    '给我看看一店的订单',
    '筛选出物流商为SF的订单',
    # 纯分页
    '下一页',
    '再来20条',
    '看第51到100条',
    # 与表格无关
    '你好',
]


@pytest.mark.parametrize('text', STAT_POSITIVE)
def test_looks_like_statistical_query_positive(text):
    assert nl.looks_like_statistical_query(text) is True, f'{text!r} 应被判定为统计意图'


@pytest.mark.parametrize('text', STAT_NEGATIVE)
def test_looks_like_statistical_query_negative(text):
    assert nl.looks_like_statistical_query(text) is False, f'{text!r} 不应被判定为统计意图'


# ==========================================================================
# 2. 统计兜底：LLM 误判成 new_query 时必须救回统计链路
# ==========================================================================
def test_guard_rescues_sum_when_llm_says_new_query(patched_store, catalog):
    """LLM 首轮误判为普通查询 -> 兜底改走 run_aggregate，且数值正确。"""
    llm = FakeLLM(
        turn_json={'action': 'new_query', 'document': '直邮一店 8.20号订单.xlsx', 'columns': []},
        aggregate_json={'aggregate_operation': 'sum', 'column': 'Order Amount',
                        'group_by': [], 'document': '直邮一店'},
    )
    out = _run('直邮一店订单金额总和是多少？', llm, catalog)

    assert out['status'] == 'ok'
    assert [c['kind'] for c in llm.calls] == ['turn', 'aggregate'], '必须发生第二次统计抽取调用'
    assert all(c['temperature'] == 0.0 for c in llm.calls), '意图解析必须 temperature=0（可复现）'
    assert nl.describe_executor(out) == 'run_aggregate'
    assert out['turn']['source'] == 'statistical_guard'
    assert out['statistical_guard'] is True
    assert out['aggregate']['operation'] == 'sum'
    assert out['aggregate']['value'] == pytest.approx(SUM_VALUE_EXPECT, **TOL)


def test_guard_rescues_group_by_when_llm_says_new_query(patched_store, catalog):
    """「每个物流商分别有多少单」被误判为普通查询 -> 兜底必须进入分组统计。"""
    llm = FakeLLM(
        turn_json={'action': 'new_query', 'document': '直邮一店 8.20号订单.xlsx', 'columns': []},
        aggregate_json={'aggregate_operation': 'count', 'column': None,
                        'group_by': ['Shipping Provider Name']},
    )
    out = _run('每个物流商分别有多少单？', llm, catalog)

    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_group_aggregate'
    assert out['group_aggregate']['operation'] == 'count'
    assert [c['name'] for c in out['group_aggregate']['group_by']] == ['Shipping Provider Name']
    assert out['group_aggregate']['total_groups'] == 3


def test_guard_rescues_topn_when_llm_says_new_query(patched_store, catalog):
    """「一店哪个物流商订单最多」被误判为普通查询 -> 兜底必须进入 TOP-1 排行。"""
    llm = FakeLLM(
        turn_json={'action': 'new_query', 'document': '直邮一店 8.20号订单.xlsx', 'columns': []},
        aggregate_json={'aggregate_operation': 'count', 'column': None,
                        'group_by': ['Shipping Provider Name'],
                        'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 1},
    )
    out = _run('一店哪个物流商订单最多？', llm, catalog)

    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_group_aggregate'
    g = out['group_aggregate']
    assert g['order_by'] == 'aggregate_value' and g['order_dir'] == 'desc' and g['top_n'] == 1
    assert len(g['rows']) == 1
    assert g['rows'][0]['group_display'] == ['Yanwen Express']


def test_guard_does_not_fire_for_plain_list_query(patched_store, catalog):
    """普通列表查询不得被统计兜底劫持：只允许一次 LLM 调用，且执行器仍是结构化查询。"""
    llm = FakeLLM(
        turn_json={'action': 'new_query', 'document': '直邮一店 8.20号订单.xlsx', 'columns': [],
                   'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]},
        aggregate_json={'aggregate_operation': 'count'},
    )
    out = _run('物流商为 SF 的订单有哪些？', llm, catalog)

    assert out['status'] == 'ok'
    assert [c['kind'] for c in llm.calls] == ['turn'], '普通查询不应触发统计兜底'
    assert nl.describe_executor(out) == 'run_structured_query'
    assert out.get('statistical_guard') is None
    assert out['result']['total_matches'] == 4


def test_guard_falls_back_when_aggregate_extraction_fails(patched_store, catalog):
    """兜底抽取失败时必须原样回退普通查询（绝不劣化原有行为）。"""
    class BrokenLLM:
        def __init__(self):
            self.calls = 0

        async def chat_completion(self, messages, stream=False, temperature=None):
            self.calls += 1
            if self.calls == 1:
                yield json.dumps({'action': 'new_query', 'document': '直邮一店 8.20号订单.xlsx',
                                  'columns': []}, ensure_ascii=False)
            else:
                raise RuntimeError('第二次调用失败')

    llm = BrokenLLM()
    out = _run('直邮一店订单金额总和是多少？', llm, catalog)
    assert out['status'] == 'ok'
    assert llm.calls == 2
    assert nl.describe_executor(out) == 'run_structured_query'


# ==========================================================================
# 3. 完整路由矩阵（真实 LLM 输出的 Intent -> 执行器）
# ==========================================================================
#: 与 E2E 记录的真实 LLM 输出一致（deepseek-v3.1）
_ROUTE_MATRIX: List[Tuple[str, Dict[str, Any], str]] = [
    ('物流商为 SF 的订单有哪些？',
     {'action': 'new_query', 'document': '直邮一店 8.20号订单.xlsx', 'columns': [],
      'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]},
     'run_structured_query'),
    ('直邮一店订单金额总和是多少？',
     {'action': 'aggregate', 'aggregate_operation': 'sum', 'column': 'Order Amount',
      'document': '直邮一店 8.20号订单.xlsx', 'group_by': []},
     'run_aggregate'),
    ('直邮一店平均订单金额是多少？',
     {'action': 'aggregate', 'aggregate_operation': 'avg', 'column': 'Order Amount',
      'document': '直邮一店 8.20号订单.xlsx', 'group_by': []},
     'run_aggregate'),
    ('一店订单金额最小是多少？',
     {'action': 'aggregate', 'aggregate_operation': 'min', 'column': 'Order Amount',
      'document': '一店', 'group_by': []},
     'run_aggregate'),
    ('一店订单金额最大是多少？',
     {'action': 'aggregate', 'aggregate_operation': 'max', 'column': 'Order Amount',
      'document': '一店', 'group_by': []},
     'run_aggregate'),
    ('每个物流商分别有多少单？',
     {'action': 'aggregate', 'aggregate_operation': 'count', 'column': None,
      'group_by': ['Shipping Provider Name']},
     'run_group_aggregate'),
    ('一店每个物流商的订单金额总和是多少？',
     {'action': 'aggregate', 'aggregate_operation': 'sum', 'column': 'Order Amount',
      'group_by': ['Shipping Provider Name'], 'document': '一店'},
     'run_group_aggregate'),
    ('一店哪个物流商订单最多？',
     {'action': 'aggregate', 'aggregate_operation': 'count', 'column': None,
      'group_by': ['Shipping Provider Name'],
      'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 1, 'document': '一店'},
     'run_group_aggregate'),
    ('一店按订单数量从高到低排列各物流商。',
     {'action': 'aggregate', 'aggregate_operation': 'count', 'column': None,
      'group_by': ['Shipping Provider Name'],
      'order_by': 'aggregate_value', 'order_dir': 'desc', 'document': '一店'},
     'run_group_aggregate'),
    ('一店SF物流下订单金额最高的3个支付方式是什么？',
     {'action': 'aggregate', 'aggregate_operation': 'sum', 'column': 'Order Amount',
      'group_by': ['Payment Method'], 'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 3,
      'document': '一店',
      'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]},
     'run_group_aggregate'),
    # 后续 2A：LLM 只给「分组 + TOP-N」时，Python 会**确定性升级**为两步分析——
    # 这句话的正确答案是"这 10 行的总量"，必须是 run_analysis（不是 10 行明细）。
    ('找出一店中金额最高的10个SKU，并统计它们的总销售额。',
     {'action': 'aggregate', 'aggregate_operation': 'sum', 'column': 'Order Amount',
      'group_by': ['SKU ID'], 'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 10,
      'document': '一店'},
     'run_analysis'),
]


@pytest.mark.parametrize('message,turn_json,expected_executor', _ROUTE_MATRIX)
def test_route_matrix(patched_store, catalog, message, turn_json, expected_executor):
    llm = FakeLLM(turn_json=turn_json)
    out = _run(message, llm, catalog)
    assert out['status'] == 'ok', f'{message} -> {out.get("message")}'
    assert nl.describe_executor(out) == expected_executor, f'{message} 路由错误'
    assert llm.calls[0]['temperature'] == 0.0


# ==========================================================================
# 3.5 最坏情况：LLM 把所有统计问题都误判为 new_query
# --------------------------------------------------------------------------
# 这是本次路由修复的"before / after"证据：
#   修复前 -> 10 个统计问题全部落成 structured_query（列出原始行）
#   修复后 -> 兜底把 10 个统计问题全部救回 run_aggregate / run_group_aggregate
# ==========================================================================
_WORST_CASE: List[Tuple[str, Dict[str, Any], str, str]] = [
    ('直邮一店订单金额总和是多少？', {'aggregate_operation': 'sum', 'column': 'Order Amount',
                             'document': '一店'}, 'run_aggregate', 'sum'),
    ('直邮一店平均订单金额是多少？', {'aggregate_operation': 'avg', 'column': 'Order Amount',
                             'document': '一店'}, 'run_aggregate', 'avg'),
    ('一店订单金额最小是多少？', {'aggregate_operation': 'min', 'column': 'Order Amount',
                           'document': '一店'}, 'run_aggregate', 'min'),
    ('一店订单金额最大是多少？', {'aggregate_operation': 'max', 'column': 'Order Amount',
                           'document': '一店'}, 'run_aggregate', 'max'),
    ('每个物流商分别有多少单？', {'aggregate_operation': 'count', 'column': None,
                           'group_by': ['Shipping Provider Name']}, 'run_group_aggregate', 'count'),
    ('一店每个物流商的订单金额总和是多少？', {'aggregate_operation': 'sum', 'column': 'Order Amount',
                                'group_by': ['Shipping Provider Name'], 'document': '一店'},
     'run_group_aggregate', 'sum'),
    ('一店哪个物流商订单最多？', {'aggregate_operation': 'count', 'column': None,
                           'group_by': ['Shipping Provider Name'], 'order_by': 'aggregate_value',
                           'order_dir': 'desc', 'top_n': 1, 'document': '一店'},
     'run_group_aggregate', 'count'),
    ('一店按订单数量从高到低排列各物流商。', {'aggregate_operation': 'count', 'column': None,
                                'group_by': ['Shipping Provider Name'], 'order_by': 'aggregate_value',
                                'order_dir': 'desc', 'document': '一店'},
     'run_group_aggregate', 'count'),
    ('一店SF物流下订单金额最高的3个支付方式是什么？',
     {'aggregate_operation': 'sum', 'column': 'Order Amount', 'group_by': ['Payment Method'],
      'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 3, 'document': '一店',
      'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]},
     'run_group_aggregate', 'sum'),
    ('找出一店中金额最高的10个SKU，并统计它们的总销售额。',
     {'aggregate_operation': 'sum', 'column': 'Order Amount', 'group_by': ['SKU ID'],
      'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 10, 'document': '一店'},
     'run_group_aggregate', 'sum'),
]


@pytest.mark.parametrize('message,guard_json,expected_executor,expected_op', _WORST_CASE)
def test_worst_case_llm_rescued_to_aggregate(patched_store, catalog,
                                             message, guard_json, expected_executor, expected_op):
    """LLM 首轮一律返回 new_query（最坏情况）——兜底必须把统计问题救回统计链路。"""
    worst_llm = FakeLLM(
        turn_json={'action': 'new_query', 'document': '直邮一店 8.20号订单.xlsx', 'columns': []},
        aggregate_json=guard_json,
    )
    out = _run(message, worst_llm, catalog)
    assert out['status'] == 'ok', f'{message} -> {out.get("message")}'
    assert nl.describe_executor(out) == expected_executor, f'{message} 未被救回统计链路'
    # 后续 2A：像「哪个物流商订单最多？」这类"取一个分组"的问题，会先被**确定性**
    # 分组排行(TOP-1) 救回（source=deterministic_top_group，连 LLM 都不用再问一次）；
    # 其余统计问题仍走 statistical_guard。两条路径都满足"必须救回统计链路"。
    assert out['turn']['source'] in ('statistical_guard', 'deterministic_top_group')
    if expected_executor == 'run_aggregate':
        assert out['aggregate']['operation'] == expected_op
    else:
        assert out['group_aggregate']['operation'] == expected_op
    # 修复前（无兜底）这条会变成结构化查询 -> 用同一次调用的 LLM 记录证明发生了二次抽取。
    # 注意：若该问题同时命中 Phase 4A 的两步分析兜底判定，会先多一次 analysis_guard 调用
    #（本文件使用的假 LLM 在该 prompt 下返回无 steps 的 JSON，因此 Phase 4A 兜底会自我否决），
    # 因此这里只断言"最后确实发生了统计参数抽取"。
    if out['turn']['source'] == 'statistical_guard':
        assert worst_llm.calls[-1]['kind'] == 'aggregate'
    else:
        # 确定性分组排行路径连第二次 LLM 调用都省掉了（更稳、更省额度）
        assert worst_llm.calls[-1]['kind'] == 'turn'
        assert len(worst_llm.calls) == 1
    assert worst_llm.calls[0]['kind'] == 'turn'


# ==========================================================================
# 4. 独立 Ground Truth（不 import 被测实现）
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


def _gt_single(rep, sheet_index, column, operation, filters) -> Optional[float]:
    sheet = rep.sheets[sheet_index]
    names = sheet.column_names
    ci = names.index(column) if column else None
    numbers: List[float] = []
    matched = 0
    for row in sheet.rows:
        if not all(_match(row[names.index(f['column'])] if names.index(f['column']) < len(row) else None,
                          f['operator'], f.get('value')) for f in filters):
            continue
        matched += 1
        if operation == 'count':
            continue
        v = _to_f(row[ci] if ci is not None and ci < len(row) else None)
        if v is not None:
            numbers.append(v)
    if operation == 'count':
        return float(matched)
    if not numbers:
        return None
    return {'sum': sum, 'avg': lambda xs: sum(xs) / len(xs), 'min': min, 'max': max}[operation](numbers)


def _gt_group_topn(rep, sheet_index, operation, column, group_by, filters,
                   order_by, order_dir, top_n) -> List[Tuple[Any, ...]]:
    """返回 [(group_key_tuple, value)]，**已按被测语义排序并截断**（Python 独立实现）。"""
    sheet = rep.sheets[sheet_index]
    names = sheet.column_names
    gi = [names.index(g) for g in group_by]
    ci = names.index(column) if column else None

    buckets: Dict[Tuple[Any, ...], List[Any]] = {}
    for row in sheet.rows:
        if not all(_match(row[names.index(f['column'])], f['operator'], f.get('value'))
                   for f in filters):
            continue
        key = tuple(row[i] if i < len(row) else None for i in gi)
        buckets.setdefault(key, []).append(row)

    entries: List[Tuple[Tuple[Any, ...], Optional[float]]] = []
    for key, rows in buckets.items():
        if operation == 'count':
            entries.append((key, float(len(rows))))
            continue
        nums = [n for n in (_to_f(r[ci] if ci is not None and ci < len(r) else None) for r in rows)
                if n is not None]
        if not nums:
            entries.append((key, None))
        elif operation == 'sum':
            entries.append((key, float(sum(nums))))
        elif operation == 'min':
            entries.append((key, float(min(nums))))
        elif operation == 'max':
            entries.append((key, float(max(nums))))
        else:
            entries.append((key, float(sum(nums) / len(nums))))

    if order_by == 'aggregate_value':
        reverse = order_dir == 'desc'
        numeric = [e for e in entries if e[1] is not None]
        non_numeric = [e for e in entries if e[1] is None]
        # tie-breaker：聚合值相同时按分组键升序（与 DuckDB 别名排序 + group ASC 一致）
        numeric.sort(key=lambda e: _key_sort(e[0]))
        numeric.sort(key=lambda e: e[1], reverse=reverse)
        entries = numeric + non_numeric
    elif order_by is not None:
        oi = names.index(order_by)
        entries.sort(key=lambda e: _key_sort(e[0]), reverse=(order_dir == 'desc'))

    return entries[:top_n] if top_n else entries


def _key_sort(key: Tuple[Any, ...]) -> Tuple[Any, ...]:
    return tuple((1, '') if k is None else (0, str(k)) for k in key)


def test_gt_single_value_questions(patched_store, catalog, small_rep):
    """Q2~Q5：单值统计必须等于独立 Ground Truth。"""
    cases = [
        ('直邮一店订单金额总和是多少？', {'action': 'aggregate', 'aggregate_operation': 'sum',
                                'column': 'Order Amount', 'group_by': [], 'document': '一店'}, 'sum'),
        ('直邮一店平均订单金额是多少？', {'action': 'aggregate', 'aggregate_operation': 'avg',
                                'column': 'Order Amount', 'group_by': [], 'document': '一店'}, 'avg'),
        ('一店订单金额最小是多少？', {'action': 'aggregate', 'aggregate_operation': 'min',
                              'column': 'Order Amount', 'group_by': [], 'document': '一店'}, 'min'),
        ('一店订单金额最大是多少？', {'action': 'aggregate', 'aggregate_operation': 'max',
                              'column': 'Order Amount', 'group_by': [], 'document': '一店'}, 'max'),
    ]
    for message, turn_json, op in cases:
        out = _run(message, FakeLLM(turn_json=turn_json), catalog)
        assert out['status'] == 'ok'
        assert nl.describe_executor(out) == 'run_aggregate'
        assert out['engine'] == 'duckdb'
        gt = _gt_single(small_rep, 0, 'Order Amount', op, [])
        assert out['aggregate']['value'] == pytest.approx(gt, **TOL), f'{message} 与 GT 不一致'
        assert out['aggregate']['matched_rows'] == 19


def test_gt_known_constants(patched_store, catalog):
    """数值锚点：与真实表独立手工计算一致。"""
    assert _gt_single(_rep_of(catalog), 0, 'Order Amount', 'sum', []) == pytest.approx(SUM_VALUE_EXPECT, **TOL)
    assert _gt_single(_rep_of(catalog), 0, 'Order Amount', 'max', []) == pytest.approx(MAX_VALUE_EXPECT, **TOL)


def _rep_of(catalog):
    import backend.excel.store as _s
    return _s.load_representation(catalog[0]['document_id'])


def test_gt_grouped_count_order_top1(patched_store, catalog, small_rep):
    """Q8：TOP-1 排行的分组、顺序、截断必须与独立 GT 逐项一致。"""
    turn_json = {'action': 'aggregate', 'aggregate_operation': 'count', 'column': None,
                 'group_by': ['Shipping Provider Name'],
                 'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 1}
    out = _run('一店哪个物流商订单最多？', FakeLLM(turn_json=turn_json), catalog)
    assert out['status'] == 'ok'
    g = out['group_aggregate']
    gt = _gt_group_topn(small_rep, 0, 'count', None, ['Shipping Provider Name'], [],
                        'aggregate_value', 'desc', 1)
    assert [(r['group_key'], r['value']) for r in g['rows']] == [(list(k), v) for k, v in gt]
    assert g['total_groups'] == 3 and g['returned_groups'] == 1


def test_gt_grouped_sum_order_top3_with_filter(patched_store, catalog, small_rep):
    """Q10：WHERE + GROUP BY + ORDER BY + TOP-N 与独立 GT 逐项一致。"""
    turn_json = {'action': 'aggregate', 'aggregate_operation': 'sum', 'column': 'Order Amount',
                 'group_by': ['Payment Method'], 'order_by': 'aggregate_value', 'order_dir': 'desc',
                 'top_n': 3, 'document': '一店',
                 'filters': [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}]}
    out = _run('一店SF物流下订单金额最高的3个支付方式是什么？', FakeLLM(turn_json=turn_json), catalog)
    assert out['status'] == 'ok'
    g = out['group_aggregate']
    gt = _gt_group_topn(small_rep, 0, 'sum', 'Order Amount', ['Payment Method'],
                        [{'column': 'Shipping Provider Name', 'operator': 'contains', 'value': 'SF'}],
                        'aggregate_value', 'desc', 3)
    assert [(r['group_key'], r['value']) for r in g['rows']] == [(list(k), v) for k, v in gt]
    assert g['matched_rows'] == 4


def test_gt_grouped_top10_sku(patched_store, catalog, small_rep):
    """Q11：分组求和 TOP-10 与独立 GT 逐项一致（顺序敏感）。

    注意问法：这里刻意**不带**「并统计它们的总销售额」——那种问法在后续 2A 起
    会被确定性升级为两步分析（`test_excel_multi_step` 覆盖），本用例只验证
    分组统计 executor 的 TOP-10 明细与 GT 逐位一致。
    """
    turn_json = {'action': 'aggregate', 'aggregate_operation': 'sum', 'column': 'Order Amount',
                 'group_by': ['SKU ID'], 'order_by': 'aggregate_value', 'order_dir': 'desc',
                 'top_n': 10, 'document': '一店'}
    out = _run('一店订单金额最高的10个SKU', FakeLLM(turn_json=turn_json), catalog)
    assert out['status'] == 'ok'
    g = out['group_aggregate']
    gt = _gt_group_topn(small_rep, 0, 'sum', 'Order Amount', ['SKU ID'], [],
                        'aggregate_value', 'desc', 10)
    assert [(r['group_key'], r['value']) for r in g['rows']] == [(list(k), v) for k, v in gt]
    assert g['total_groups'] == 15 and g['returned_groups'] == 10
    # 顺序必须单调不增
    values = [r['value'] for r in g['rows']]
    assert values == sorted(values, reverse=True)


def test_gt_desc_no_topn(patched_store, catalog, small_rep):
    """Q9：有排序、无 TOP-N —— 全部返回且顺序与 GT 一致。"""
    turn_json = {'action': 'aggregate', 'aggregate_operation': 'count', 'column': None,
                 'group_by': ['Shipping Provider Name'],
                 'order_by': 'aggregate_value', 'order_dir': 'desc', 'document': '一店'}
    out = _run('一店按订单数量从高到低排列各物流商。', FakeLLM(turn_json=turn_json), catalog)
    assert out['status'] == 'ok'
    g = out['group_aggregate']
    gt = _gt_group_topn(small_rep, 0, 'count', None, ['Shipping Provider Name'], [],
                        'aggregate_value', 'desc', None)
    assert [(r['group_key'], r['value']) for r in g['rows']] == [(list(k), v) for k, v in gt]
    assert g['returned_groups'] == 3 and g['top_n'] is None


def test_gt_no_sort_grouped(patched_store, catalog, small_rep):
    """Q6/Q7：无排序的分组统计（Phase 3B 回归）不得被加上排序。"""
    for message, turn_json, op, column in [
        ('每个物流商分别有多少单？',
         {'action': 'aggregate', 'aggregate_operation': 'count', 'column': None,
          'group_by': ['Shipping Provider Name']}, 'count', None),
        ('一店每个物流商的订单金额总和是多少？',
         {'action': 'aggregate', 'aggregate_operation': 'sum', 'column': 'Order Amount',
          'group_by': ['Shipping Provider Name'], 'document': '一店'}, 'sum', 'Order Amount'),
    ]:
        out = _run(message, FakeLLM(turn_json=turn_json), catalog)
        assert out['status'] == 'ok'
        g = out['group_aggregate']
        assert g['order_by'] is None and g['top_n'] is None, f'{message} 不应有排序'
        assert g['total_groups'] == 3 and g['returned_groups'] == 3
        got = {tuple(r['group_key']): r['value'] for r in g['rows']}
        gt = _gt_group_topn(small_rep, 0, op, column, ['Shipping Provider Name'], [], None, 'asc', None)
        assert got == {tuple(k): v for k, v in gt}


def test_gt_unfiltered_group_count_matches_sheet(patched_store, catalog, small_rep):
    """分组计数之和必须等于整表行数（19）。"""
    turn_json = {'action': 'aggregate', 'aggregate_operation': 'count', 'column': None,
                 'group_by': ['Shipping Provider Name']}
    out = _run('每个物流商分别有多少单？', FakeLLM(turn_json=turn_json), catalog)
    assert sum(r['value'] for r in out['group_aggregate']['rows']) == 19
    assert out['group_aggregate']['matched_rows'] == 19


def test_known_values_anchor(patched_store, catalog):
    """关键数值锚点（防止"看起来正确"）：实际执行结果与真实表一致。"""
    out = _run('直邮一店订单金额总和是多少？',
               FakeLLM(turn_json={'action': 'aggregate', 'aggregate_operation': 'sum',
                                  'column': 'Order Amount', 'group_by': [], 'document': '一店'}),
               catalog)
    assert out['aggregate']['value'] == pytest.approx(SUM_VALUE_EXPECT, **TOL)
    out = _run('直邮一店平均订单金额是多少？',
               FakeLLM(turn_json={'action': 'aggregate', 'aggregate_operation': 'avg',
                                  'column': 'Order Amount', 'group_by': [], 'document': '一店'}),
               catalog)
    assert out['aggregate']['value'] == pytest.approx(AVG_VALUE_EXPECT, **TOL)
    out = _run('一店订单金额最小是多少？',
               FakeLLM(turn_json={'action': 'aggregate', 'aggregate_operation': 'min',
                                  'column': 'Order Amount', 'group_by': [], 'document': '一店'}),
               catalog)
    assert out['aggregate']['value'] == pytest.approx(MIN_VALUE_EXPECT, **TOL)
    out = _run('一店订单金额最大是多少？',
               FakeLLM(turn_json={'action': 'aggregate', 'aggregate_operation': 'max',
                                  'column': 'Order Amount', 'group_by': [], 'document': '一店'}),
               catalog)
    assert out['aggregate']['value'] == pytest.approx(MAX_VALUE_EXPECT, **TOL)
