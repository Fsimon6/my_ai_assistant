# -*- coding: utf-8 -*-
"""Stage 2A-P1（D1/D2）：**文件与 Sheet 的用户语义 authority**。

背景（2026-09-28 最终验收确定性复现）：
  * D1 三条执行入口的 `doc_hint` 都只来自 LLM（`intent.document`）——「直邮一店
    8.20号订单.xlsx 的订单金额总和」+ LLM 给「直邮5店 7.10号订单.xlsx」会执行**后者**；
  * D2 同理 `sheet_hint` 只来自 LLM（`intent.sheet`）——「OrderSKUList 里的订单金额总和」
    + LLM 给 `RefundList` 会执行**后者**。

本轮 authority（复用既有确定性能力，未新造 tokenizer）：
  * 文件：`score_document_hint` 的**强匹配**档（≥300：精确 / 文件名在文本中 / 同「N店」）；
    唯一 -> 采用；并列 -> 澄清；无 -> 保持既有 LLM/继承行为；疑似注入 -> 不参与；
  * Sheet：`normalize_name` 唯一子串（与 `resolve_sheet_deterministic` 同规则）；
    命中 1 个 -> 采用；≥2 个 -> 澄清；0 个 -> 保持既有行为；疑似注入 -> 不参与。

全部为离线测试（monkeypatch LLM + store），不调用真实 LLM。
"""
import asyncio
from typing import Any, Dict, List, Optional

import pytest

from backend.excel import aggregate as excel_aggregate
from backend.excel import multi_step as excel_multi_step
from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from backend.excel.representation import ColumnMeta, SheetRepresentation, WorkbookRepresentation

FILE_A = '直邮一店 8.20号订单.xlsx'
FILE_B = '直邮5店 7.10号订单.xlsx'
FILE_A2 = '直邮一店 8.10号订单.xlsx'          # 同店另一份（用于「歧义」用例）
SHEET_MAIN = 'OrderSKUList'                   # 合计 300
SHEET_REFUND = 'RefundList'                   # 合计 15
_COLS = ['Order ID', 'Order Amount']


class _FakeLLM:
    """占位 LLM：管线中的 LLM 调用点全部被 monkeypatch。"""


def _mk_sheet(name: str, idx: int, rows: List[List[Any]]) -> SheetRepresentation:
    cols = [ColumnMeta(name=n, index=i, excel_column=i + 1,
                       excel_column_letter=chr(ord('A') + i), dtype='string')
            for i, n in enumerate(_COLS)]
    return SheetRepresentation(sheet_name=name, sheet_index=idx, header_mode='single',
                               columns=cols, rows=[list(r) for r in rows],
                               row_excel_numbers=list(range(2, 2 + len(rows))),
                               row_count=len(rows), column_count=len(cols))


def _mk_rep(doc_id: str, filename: str,
            sheets: Optional[List[SheetRepresentation]] = None) -> WorkbookRepresentation:
    if sheets is None:
        sheets = [_mk_sheet(SHEET_MAIN, 0, [['A1', '100'], ['A2', '200']])]
    return WorkbookRepresentation(schema_version='1.0', document_id=doc_id, user_id=1,
                                  filename=filename, file_type='xlsx', parser='xlsx',
                                  sheet_count=len(sheets), sheets=sheets)


def _two_sheet_rep(doc_id: str, filename: str) -> WorkbookRepresentation:
    return _mk_rep(doc_id, filename, [
        _mk_sheet(SHEET_MAIN, 0, [['A1', '100'], ['A2', '200']]),      # 300
        _mk_sheet(SHEET_REFUND, 1, [['R1', '7'], ['R2', '8']]),        # 15
    ])


def _catalog_of(reps: List[WorkbookRepresentation]) -> List[Dict[str, Any]]:
    out = []
    for rep in reps:
        out.append({
            'document_id': rep.document_id, 'filename': rep.filename,
            'file_type': rep.file_type, 'created_at': '2026-09-28T00:00:00',
            'total_rows': rep.total_rows,
            'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                        'row_count': s.row_count, 'column_count': s.column_count,
                        'columns': s.column_names} for s in rep.sheets],
        })
    return out


def _agg_turn(document: Optional[str], sheet: Optional[str]) -> 'nl.TurnIntent':
    return nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=excel_aggregate.OPERATION_SUM, column='Order Amount',
        group_by=[], filters=[], document=document, sheet=sheet))


def _analysis_turn(document: Optional[str], sheet: Optional[str]) -> 'nl.TurnIntent':
    return nl.TurnIntent(action=nl.ACTION_ANALYSIS, analysis=nl.AnalysisIntent(
        document=document, sheet=sheet, steps=[
            {'type': excel_multi_step.STEP_GROUP_AGGREGATE, 'group_by': ['Order ID'],
             'operation': excel_aggregate.OPERATION_SUM, 'column': 'Order Amount', 'filters': []},
            {'type': excel_multi_step.STEP_AGGREGATE, 'operation': excel_aggregate.OPERATION_SUM,
             'source': excel_multi_step.SOURCE_STEP_1,
             'column': excel_multi_step.INTERMEDIATE_VALUE_COLUMN}]))


def _nq_turn(document: Optional[str], sheet: Optional[str], columns=None,
             limit: Optional[int] = None) -> 'nl.TurnIntent':
    payload: Dict[str, Any] = {'query_type': nl.INTENT_STRUCTURED,
                               'columns': list(columns or ['Order Amount'])}
    if document:
        payload['document'] = document
    if sheet:
        payload['sheet'] = sheet
    if limit is not None:
        payload['limit'] = limit
    return nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=nl.intent_from_dict(payload))


def _run(reps: List[WorkbookRepresentation], message: str, turn, monkeypatch,
         *, context=None, key: str = 'doc-sheet') -> Dict[str, Any]:
    mapping = {r.document_id: r for r in reps}
    monkeypatch.setattr(excel_store, 'load_representation',
                        lambda document_id, user_id=None: mapping.get(document_id))

    async def _fake(llm, msg, catalog, ctx=None, analysis_context=None):
        return turn

    monkeypatch.setattr(nl, 'llm_parse_turn', _fake)
    return asyncio.run(nl.run_nl_query(message, _catalog_of(reps), llm=_FakeLLM(),
                                       user_id=1, session_key=key, context=context))


def _doc_of(out: Dict[str, Any]) -> Optional[str]:
    return (out.get('document') or {}).get('filename')


def _sheet_of(out: Dict[str, Any]) -> Optional[str]:
    agg = out.get('aggregate') or {}
    res = out.get('result') or {}
    return agg.get('sheet_name') or res.get('sheet_name')


def _rep_a() -> WorkbookRepresentation:
    return _two_sheet_rep('DUP_A', FILE_A)


def _rep_a_one_sheet() -> WorkbookRepresentation:
    """单 Sheet 版 A（D1 只验「文件」authority，避免多 Sheet 澄清干扰）。"""
    return _mk_rep('DUP_A', FILE_A)


def _rep_a2() -> WorkbookRepresentation:
    return _mk_rep('DUP_A2', FILE_A2)


def _rep_b() -> WorkbookRepresentation:
    return _mk_rep('DUP_B', FILE_B, [_mk_sheet(SHEET_MAIN, 0, [['X1', '3'], ['X2', '4']])])


# ===========================================================================
# D1：文件 authority
# ===========================================================================
def test_d1_user_named_file_wins(monkeypatch):
    """① 用户明确文件 A + LLM 文件 B -> 必须执行 A。"""
    out = _run([_rep_a_one_sheet(), _rep_b()], '%s 的订单金额总和' % FILE_A,
               _agg_turn(document=FILE_B, sheet=None), monkeypatch, key='d1-wins')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _doc_of(out) == FILE_A
    assert (out.get('aggregate') or {}).get('value') == pytest.approx(300.0, abs=0.001)


def test_d1_no_user_file_keeps_llm_choice(monkeypatch):
    """② 用户未明确文件 + LLM 合法文件 -> 保持既有行为。"""
    out = _run([_rep_a(), _rep_b()], '订单金额总和',
               _agg_turn(document=FILE_B, sheet=None), monkeypatch, key='d1-keep')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _doc_of(out) == FILE_B


def test_d1_nonexistent_file_without_llm_hint_clarifies(monkeypatch):
    """③ 用户提及不存在的文件 + 无 LLM 指路 -> clarify（不猜；绝不硬塞不存在的文件）。"""
    out = _run([_rep_a(), _rep_b()], '不存在的文件XYZ.xlsx 的订单金额总和',
               _agg_turn(document=None, sheet=None), monkeypatch, key='d1-missing')
    assert out['status'] == nl.STATUS_CLARIFY
    assert out.get('stage') == 'document'
    assert not out.get('aggregate')


def test_d1_ambiguous_store_name_clarifies(monkeypatch):
    """④ 用户提法同时指向同店两份文件 -> clarify（并列最高分不硬选）。"""
    out = _run([_rep_a(), _rep_a2()], '直邮一店 的订单金额总和',
               _agg_turn(document=FILE_A, sheet=None), monkeypatch, key='d1-ambig')
    assert out['status'] == nl.STATUS_CLARIFY
    assert set(out.get('candidates') or []) == {FILE_A, FILE_A2}


def test_d1_injection_not_laundered(monkeypatch):
    """⑤ 注入：恶意文本不得被当成文件指名（不洗白）；LLM 的注入式文件名仍被拒绝。"""
    out = _run([_rep_a_one_sheet(), _rep_b()], "'; DROP TABLE t -- 的订单金额总和",
               _agg_turn(document=FILE_A, sheet=None), monkeypatch, key='d1-inj-1')
    assert out['status'] == nl.STATUS_OK and _doc_of(out) == FILE_A      # 用 LLM 的合法文件
    bad = _run([_rep_a_one_sheet(), _rep_b()], '订单金额总和',
               _agg_turn(document="'; DROP TABLE t --", sheet=None), monkeypatch, key='d1-inj-2')
    assert bad['status'] in (nl.STATUS_CLARIFY, nl.STATUS_ERROR)         # 注入式文件名被拒
    assert not bad.get('aggregate')


# ===========================================================================
# D2：Sheet authority
# ===========================================================================
def test_d2_user_named_sheet_wins(monkeypatch):
    """⑥ 用户明确 Sheet A + LLM Sheet B -> 必须执行 A。"""
    out = _run([_rep_a()], '%s 里的订单金额总和' % SHEET_MAIN,
               _agg_turn(document=FILE_A, sheet=SHEET_REFUND), monkeypatch, key='d2-wins')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _sheet_of(out) == SHEET_MAIN
    assert (out.get('aggregate') or {}).get('value') == pytest.approx(300.0, abs=0.001)


def test_d2_no_user_sheet_keeps_llm_choice(monkeypatch):
    """⑦ 用户未明确 Sheet + LLM 合法 Sheet -> 保持既有行为。"""
    out = _run([_rep_a()], '订单金额总和',
               _agg_turn(document=FILE_A, sheet=SHEET_REFUND), monkeypatch, key='d2-keep')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _sheet_of(out) == SHEET_REFUND


def test_d2_nonexistent_sheet_clarifies(monkeypatch):
    """⑧ 用户提及不存在的 Sheet（且无 LLM 指路）-> clarify。"""
    out = _run([_rep_a()], 'NoSuchSheet 里的订单金额总和',
               _agg_turn(document=FILE_A, sheet=None), monkeypatch, key='d2-missing')
    assert out['status'] == nl.STATUS_CLARIFY
    assert not out.get('aggregate')


def test_d2_ambiguous_sheet_mention_clarifies(monkeypatch):
    """⑨ 用户同时提到两个 Sheet -> clarify（不猜）。"""
    out = _run([_rep_a()], '%s 和 %s 里的订单金额总和' % (SHEET_MAIN, SHEET_REFUND),
               _agg_turn(document=FILE_A, sheet=SHEET_REFUND), monkeypatch, key='d2-ambig')
    assert out['status'] == nl.STATUS_CLARIFY
    assert set(out.get('candidates') or []) == {SHEET_MAIN, SHEET_REFUND}


def test_d2_injection_not_laundered(monkeypatch):
    """⑩ 注入：恶意 Sheet 值不得被洗白成合法 Sheet。"""
    bad = _run([_rep_a()], '订单金额总和',
               _agg_turn(document=FILE_A, sheet='%s; DROP TABLE t' % SHEET_MAIN),
               monkeypatch, key='d2-inj')
    assert bad['status'] in (nl.STATUS_CLARIFY, nl.STATUS_ERROR)
    assert not bad.get('aggregate')


# ===========================================================================
# 组合 / 各执行路径
# ===========================================================================
def test_aggregate_doc_and_sheet_authority_combined(monkeypatch):
    """⑪ aggregate：文件与 Sheet 同时冲突 -> 两者都以用户文本为准。"""
    msg = '%s 的 %s 里的订单金额总和' % (FILE_A, SHEET_MAIN)
    out = _run([_rep_a(), _rep_b()], msg,
               _agg_turn(document=FILE_B, sheet=SHEET_REFUND), monkeypatch, key='agg-both')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _doc_of(out) == FILE_A and _sheet_of(out) == SHEET_MAIN
    assert (out.get('aggregate') or {}).get('value') == pytest.approx(300.0, abs=0.001)


def test_analysis_doc_and_sheet_authority(monkeypatch):
    """⑫ analysis：同一套 authority 在 `_run_analysis_turn` 生效。"""
    msg = '%s 的 %s 里的订单金额总和' % (FILE_A, SHEET_MAIN)
    out = _run([_rep_a(), _rep_b()], msg,
               _analysis_turn(document=FILE_B, sheet=SHEET_REFUND), monkeypatch, key='an-both')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _doc_of(out) == FILE_A
    assert (out.get('multi_step') or {}).get('value') == pytest.approx(300.0, abs=0.001)


def test_new_query_doc_and_sheet_authority(monkeypatch):
    """⑬ new_query：`build_validated_query` 边界同样以用户文本为准。"""
    msg = '%s 的 %s 里的订单金额' % (FILE_A, SHEET_REFUND)
    out = _run([_rep_a(), _rep_b()], msg,
               _nq_turn(document=FILE_B, sheet=SHEET_MAIN), monkeypatch, key='nq-both')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _doc_of(out) == FILE_A
    rows = (out.get('result') or {}).get('rows') or []
    assert [list(r) for r in rows] == [['7'], ['8']]       # RefundList 的数据（结构化查询按行返回）


# ===========================================================================
# C / F（2026-09-28 补充授权）：**「提到但不存在」必须澄清，绝不退回 LLM 候选**
# ===========================================================================
def test_c_missing_file_with_extension_clarifies(monkeypatch):
    """③ 用户明确提到**不存在**的文件（带扩展名）+ LLM 合法文件 -> clarify（不执行 B）。"""
    out = _run([_rep_a_one_sheet(), _rep_b()], '根本不存在的文件XYZ.xlsx 的订单金额总和',
               _agg_turn(document=FILE_B, sheet=None), monkeypatch, key='c-missing')
    assert out['status'] == nl.STATUS_CLARIFY
    assert out.get('stage') == 'document'
    assert set(out.get('candidates') or []) == {FILE_A, FILE_B}
    assert not out.get('aggregate')


def test_c_missing_file_without_extension_keeps_llm(monkeypatch):
    """③′ 无扩展名、且无法匹配任何 catalog 的普通短语 -> 视为"未指定"（保持既有 LLM 行为）。

    这是 mention detection 的**明确能力边界**：只有"明确资源格式"（.xlsx/.xlsm/.xls/.csv/.tsv）
    才被当作"用户提出了资源选择意图"，避免把任意中文短语当成文件名（§八）。
    """
    out = _run([_rep_a_one_sheet(), _rep_b()], '不存在的文件XYZ 的订单金额总和',
               _agg_turn(document=FILE_B, sheet=None), monkeypatch, key='c-noext')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _doc_of(out) == FILE_B


def test_f_missing_sheet_clarifies(monkeypatch):
    """③ 用户明确提到**不存在**的 Sheet + LLM 合法 Sheet -> clarify（不执行 B）。"""
    out = _run([_rep_a()], 'NoSuchSheet 里的订单金额总和',
               _agg_turn(document=FILE_A, sheet=SHEET_REFUND), monkeypatch, key='f-missing')
    assert out['status'] == nl.STATUS_CLARIFY
    assert set(out.get('candidates') or []) == {SHEET_MAIN, SHEET_REFUND}
    assert not out.get('aggregate')


def test_f_missing_sheet_analysis_and_new_query(monkeypatch):
    """③ 同一条三态在 analysis / new_query 边界同样成立。"""
    an = _run([_rep_a()], 'NoSuchSheet 里的订单金额总和',
              _analysis_turn(document=FILE_A, sheet=SHEET_REFUND), monkeypatch, key='f-an')
    assert an['status'] == nl.STATUS_CLARIFY and not an.get('multi_step')
    nq = _run([_rep_a()], 'NoSuchSheet 里的订单金额',
              _nq_turn(document=FILE_A, sheet=SHEET_REFUND), monkeypatch, key='f-nq')
    assert nq['status'] == nl.STATUS_CLARIFY and not nq.get('result')


def test_c_missing_file_analysis_clarifies(monkeypatch):
    """③ 文件版三态在 analysis 边界同样成立。"""
    out = _run([_rep_a_one_sheet(), _rep_b()], '根本不存在的文件XYZ.xlsx 的订单金额总和',
               _analysis_turn(document=FILE_B, sheet=None), monkeypatch, key='c-an')
    assert out['status'] == nl.STATUS_CLARIFY and not out.get('multi_step')


# ===========================================================================
# §八：普通业务文本**不得**被误判为 document / Sheet 选择
# ===========================================================================
@pytest.mark.parametrize('text', [
    '订单金额是多少？',
    '哪些物流商最多？',
    'SKU有哪些？',
    '查询Order Amount',
    '物流商为SF',
    '各物流商的订单金额总和',
    '数量大于3的订单金额总和',
    '列出订单号和物流商',
])
def test_ordinary_text_does_not_trigger_resource_choice(monkeypatch, text):
    """普通业务文本（含列名 / 英文词 / 大小写混合缩写）不得触发 document/sheet authority。"""
    out = _run([_rep_a_one_sheet(), _rep_b()], text,
               _agg_turn(document=FILE_B, sheet=None), monkeypatch, key='fp-%s' % text[:4])
    assert out['status'] == nl.STATUS_OK, out.get('message')      # 不被强制澄清
    assert _doc_of(out) == FILE_B                                 # 保持 LLM 的选择


@pytest.mark.parametrize('text', [
    '订单金额是多少？',
    'SKU有哪些？',
    '查询Order Amount',
    'Created Time 是多少',
])
def test_ordinary_text_does_not_pick_sheet(text, monkeypatch):
    """普通业务文本不得被当成 Sheet 选择（LLM 的合法 Sheet 保持生效）。"""
    out = _run([_rep_a()], text,
               _agg_turn(document=FILE_A, sheet=SHEET_REFUND), monkeypatch, key='fp-sh-%s' % text[:3])
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _sheet_of(out) == SHEET_REFUND


def test_pagination_context_not_regressed(monkeypatch):
    """⑭ 分页：仍完全继承上下文（不重新解析文件/Sheet，不因 authority 漂移）。"""
    reps = [_rep_a()]
    first = _run(reps, '%s 的 %s 里的订单金额' % (FILE_A, SHEET_REFUND),
                 _nq_turn(document=FILE_B, sheet=SHEET_MAIN, limit=1),
                 monkeypatch, key='pg-doc-sheet')
    assert first['status'] == nl.STATUS_OK, first.get('message')
    ctx = nl.ExcelQueryContext(**(first.get('new_context') or {}))
    nxt = _run(reps, '下一页', _nq_turn(document=FILE_B, sheet=SHEET_MAIN),
               monkeypatch, context=ctx, key='pg-doc-sheet')
    assert nxt['status'] == nl.STATUS_OK, nxt.get('message')
    assert nxt.get('fast_path') is True
    assert (nxt.get('new_context') or {}).get('document_id') == 'DUP_A'
    assert (nxt.get('new_context') or {}).get('sheet_name') == SHEET_REFUND
    assert [list(r) for r in ((nxt.get('result') or {}).get('rows') or [])] == [['8']]
