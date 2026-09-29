# -*- coding: utf-8 -*-
"""相邻 P2：表格结果暴露「全部命中 Excel 行」的真实最大连续段。

设计要点（只读确认后确定）：
  * 来源：`query.py::execute_query` 在**分页切片之前**构造的 `matched_excel_rows`
    （遍历 `sheet.rows` 的顺序 -> 已是升序物理行号；含**全部**命中行；可能不连续）；
  * 不新增 SQL、不重复执行、不改 rows/total_matches/排序/过滤/分页；
  * 结构：`matched_row_runs`（最多 8 段，`[first, last]`，单行时 first==last）+
    `matched_row_run_count`（真实段数）+ `matched_row_runs_truncated`（是否截断）；
  * **绝不**把不相邻的行合并成包络（例：`3,12,18,19` 必须表达为 `3、12、18~19`，不是 `3~19`）。

DuckDB 引擎只取回本页行（count_sql + page_sql），因此仅当"本页即全部命中"
（offset=0 且 !has_more）时折叠；否则留空（不猜、不额外查询）。
"""
import asyncio
import glob
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.api.v1.rag import EXCEL_HISTORY_SCHEMA_VERSION, build_excel_history_snapshot
from backend.excel import nl_query as nl
from backend.excel import query as excel_query
from backend.excel import store as excel_store

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CARRIER = 'Shipping Provider Name'
CONTAINS_SF = [{'column': CARRIER, 'operator': 'contains', 'value': 'SF'}]


# ==========================================================================
# 纯函数：最大连续段折叠
# ==========================================================================
def test_collapse_continuous():
    runs, count, truncated = excel_query.collapse_row_runs([3, 4, 5, 6, 7])
    assert runs == [[3, 7]] and count == 1 and truncated is False


def test_collapse_discontinuous_keeps_gaps():
    runs, count, truncated = excel_query.collapse_row_runs([3, 5, 8, 12, 20])
    assert runs == [[3, 3], [5, 5], [8, 8], [12, 12], [20, 20]]
    assert count == 5 and truncated is False
    assert runs != [[3, 20]]                       # 绝不合并成包络


def test_collapse_single_row():
    assert excel_query.collapse_row_runs([7]) == ([[7, 7]], 1, False)


def test_collapse_empty():
    assert excel_query.collapse_row_runs([]) == ([], 0, False)


def test_collapse_large_continuous():
    runs, count, truncated = excel_query.collapse_row_runs(list(range(3, 450)))
    assert runs == [[3, 449]] and count == 1 and truncated is False


def test_collapse_truncates_many_runs():
    nums = [2 * i + 1 for i in range(1, 13)]        # 3,5,7,…,25 -> 12 段
    runs, count, truncated = excel_query.collapse_row_runs(nums, cap=8)
    assert len(runs) == 8 and count == 12 and truncated is True
    assert runs[0] == [3, 3] and runs[-1] == [17, 17]


# ==========================================================================
# 夹具：真实文档
# ==========================================================================
def _load_rep(low: int, high: int) -> 'nl.WorkbookRepresentation':
    for path in sorted(glob.glob(str(PROJECT_ROOT / 'data' / 'excel' / '*'
                                      / 'representation.json'))):
        did = os.path.basename(os.path.dirname(path))
        try:
            rep = excel_store.load_representation(did)
        except Exception:  # noqa: BLE001
            continue
        if rep is not None and low <= rep.total_rows <= high:
            return rep
    pytest.skip('未找到 %d~%d 行的真实 representation' % (low, high))


@pytest.fixture(scope='module')
def small_rep() -> 'nl.WorkbookRepresentation':
    return _load_rep(10, 30)


@pytest.fixture(scope='module')
def big_rep() -> 'nl.WorkbookRepresentation':
    return _load_rep(400, 600)


def _catalog(rep) -> List[Dict[str, Any]]:
    return [{
        'document_id': rep.document_id, 'filename': rep.filename, 'file_type': rep.file_type,
        'created_at': '2026-09-29T00:00:00', 'total_rows': rep.total_rows,
        'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                    'row_count': s.row_count, 'column_count': s.column_count,
                    'columns': s.column_names} for s in rep.sheets],
    }]


def _run(rep, turn, message: str, *, session_key='runs') -> Dict[str, Any]:
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nl.run_nl_query(message, _catalog(rep), llm=None,
                                           pagination_override=turn, user_id=1,
                                           session_key=session_key))
    finally:
        excel_store.load_representation = original


def _table_turn(rep, *, filters=None, limit=50, offset=0):
    return nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=nl.intent_from_dict(
        {'query_type': nl.INTENT_STRUCTURED, 'document': rep.filename,
         'columns': ['Order ID', CARRIER], 'filters': list(filters or []),
         'limit': limit, 'offset': offset}))


# ==========================================================================
# 真实文档：连续 / 不连续 / 空
# ==========================================================================
def test_real_discontinuous_matches_keep_gaps(small_rep):
    """一店 + contains SF：真实命中 3、12、18~19 -> 必须保留不连续性。"""
    out = _run(small_rep, _table_turn(small_rep, filters=CONTAINS_SF),
               '列出订单号和物流商', session_key='runs-small')
    r = out['result']
    assert r['total_matches'] == 4
    assert r['matched_row_runs'] == [[3, 3], [12, 12], [18, 19]]     # 真实不连续
    assert r['matched_row_run_count'] == 3
    assert r['matched_row_runs_truncated'] is False
    assert r['row_excel_numbers'] == [3, 12, 18, 19]                 # 本页行号（未改）
    assert out['engine'] in ('duckdb', 'python')


def test_real_continuous_matches_single_run(big_rep):
    """5 店 447 行无筛选：一次取全（limit=500，等价于 P1 枚举默认）-> 单段 3~449。"""
    out = _run(big_rep, _table_turn(big_rep, limit=500), '列出订单号和物流商',
               session_key='runs-big')
    r = out['result']
    assert r['total_matches'] == 447 and r['returned_count'] == 447 and r['has_more'] is False
    assert r['matched_row_runs'] == [[3, 449]]
    assert r['matched_row_run_count'] == 1 and r['matched_row_runs_truncated'] is False


def test_empty_result_has_no_runs(small_rep):
    out = _run(small_rep, _table_turn(small_rep, filters=[
        {'column': CARRIER, 'operator': 'contains', 'value': 'ZZZ_NO_SUCH'}],
        ), '列出订单号和物流商', session_key='runs-empty')
    r = out['result']
    assert r['total_matches'] == 0 and r['returned_count'] == 0
    assert r['matched_row_runs'] == [] and r['matched_row_run_count'] == 0


# ==========================================================================
# 不改变既有行为 + 分页时留空（不猜）
# ==========================================================================
def test_fields_do_not_change_existing_result_fields(big_rep):
    out = _run(big_rep, _table_turn(big_rep, limit=5), '列出订单号和物流商',
               session_key='runs-page1')
    r = out['result']
    # 分页语义完全不变
    assert r['limit'] == 5 and r['returned_count'] == 5
    assert r['total_matches'] == 447 and r['has_more'] is True and r['next_offset'] == 5
    assert len(r['rows']) == 5 and r['row_excel_numbers'] == [3, 4, 5, 6, 7]
    # 本页 ≠ 全部命中 -> 不产生"全部命中行"结论（绝不外推）
    assert r['matched_row_runs'] == [] and r['matched_row_run_count'] == 0


def test_python_engine_computes_runs_from_all_matches(big_rep):
    """Python 引擎（逐行）在分页查询下也持有全部命中行 -> 仍可如实给出完整分布。"""
    payload = {'document_id': big_rep.document_id, 'sheet_index': 0,
               'columns': ['Order ID'], 'filters': [], 'limit': 5, 'offset': 0}
    res = excel_query.query_representation(big_rep, payload)          # 明确走 Python 引擎
    assert res.total_matches == 447 and res.returned_count == 5
    assert res.matched_row_runs == [[3, 449]]                        # 全量分布（真实）
    assert res.matched_row_run_count == 1


def test_offset_page_has_no_runs(small_rep):
    out = _run(small_rep, _table_turn(small_rep, limit=2, offset=2), '列出订单号和物流商',
               session_key='runs-offset')
    r = out['result']
    assert r['offset'] == 2 and r['returned_count'] == 2
    assert r['matched_row_runs'] == []       # 非首页 -> 不外推（DuckDB 路径）


# ==========================================================================
# snapshot：新增可选字段 / 旧快照降级
# ==========================================================================
def test_snapshot_keeps_runs_and_stays_v1(small_rep):
    out = _run(small_rep, _table_turn(small_rep, filters=CONTAINS_SF),
               '列出订单号和物流商', session_key='runs-snap')
    snap = build_excel_history_snapshot(out)
    payload = snap['payload']
    assert snap['schema_version'] == EXCEL_HISTORY_SCHEMA_VERSION == 1
    assert snap['kind'] == 'result'
    assert payload['matched_row_runs'] == [[3, 3], [12, 12], [18, 19]]
    assert payload['matched_row_run_count'] == 3
    assert payload['matched_row_runs_truncated'] is False
    assert payload['total_matches'] == 4                             # 既有字段不受影响


def test_old_snapshot_without_runs_degrades(small_rep):
    """旧快照没有新字段：仍可构建/序列化，字段缺失（前端不显示该行）。"""
    out = _run(small_rep, _table_turn(small_rep, filters=CONTAINS_SF),
               '列出订单号和物流商', session_key='runs-legacy')
    legacy = {k: v for k, v in out.items() if k != 'result'}
    legacy['result'] = {k: v for k, v in out['result'].items()
                        if not k.startswith('matched_row_')}
    snap = build_excel_history_snapshot(legacy)
    payload = snap['payload']
    assert snap['schema_version'] == 1
    assert 'matched_row_runs' not in payload
    assert payload['total_matches'] == 4
