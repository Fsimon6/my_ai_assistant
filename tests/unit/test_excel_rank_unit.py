# -*- coding: utf-8 -*-
"""Stage 2A-P1：**TOP-N 排名单位必须来自用户文本**（排名单位语义安全边界）。

背景（2026-09-27 审计实证，真实文档）：
    「按订单金额排名前三」在 qwen3.6-plus 下 7 次采样 6 次 clarify、1 次 ok ——
    差别完全取决于 LLM 是否**自行**给出 `group_by`：
      * LLM 给 `group_by=["SKU ID"]` -> 命中 `_guard_rank_semantics` 的"指标排行"
        放行分支（只判 `agg.group_by` 真值）-> 直接执行；
      * LLM 给空 -> 澄清。
    更严重的一面：用户文本**已明确**表达单位时，LLM 的单位反而胜出
    （`agg.group_by = agg.group_by or [group_hint]` 只在为空时补）。

本轮安全边界（M1 + M2）：
    M1 用户没有表达排名单位 -> **clarify(stage='order')**，无论 LLM 给出什么 group_by；
    M2 用户已表达排名单位 -> **以用户文本为准**（LLM 的 group_by 只是候选）。
    LLM 可以提出候选 group_by，但不能凭空创造用户没有表达的排名单位。

排名单位的确定性来源（复用既有机制，未新增语法）：
    ① `DIMENSION_ALIASES` 命中（物流商 / SKU / 商品 / 城市 …），命中多个不同列 -> 歧义 -> clarify；
    ② `RANK_UNIT_RE`（「…个/的/名 + 订单」）**且**文本存在唯一显式度量（`_deterministic_rank_metric`）
       ——即「订单金额最高的前3个订单」；只有单位位置而无度量（「前3名订单」）**不构成**依据；
    ③ 真实列名唯一命中且该列可作维度（非数值、非日期）。

全部为离线测试（注入 turn / monkeypatch LLM），不调用真实 LLM。
"""
import asyncio
from typing import Any, Dict, List, Optional

import pytest

from backend.excel import aggregate as excel_aggregate
from backend.excel import nl_query as nl
from backend.excel import store as excel_store
from backend.excel.representation import (
    ColumnMeta, SheetRepresentation, WorkbookRepresentation,
)

# ---------------------------------------------------------------------------
# 合成表：Order ID 与 SKU ID 分组结果**不同**（用于区分"按订单"与"按SKU"）
#   SUM(Order Amount) by Order ID = O1=30, O2=25, O3=15, O4=5      -> TOP3 [30,25,15]
#   SUM(Order Amount) by SKU ID   = S1=35, S2=20, S3=15, S4=5      -> TOP3 [35,20,15]
#   COUNT by Shipping Provider Name = SF=3, Yanwen Express=2        -> TOP3 正常
# ---------------------------------------------------------------------------
_COLS = ['Order ID', 'SKU ID', 'Order Amount', 'Quantity', 'Shipping Provider Name']
_ROWS = [
    ['O1', 'S1', '10', '1', 'SF'],
    ['O1', 'S2', '20', '1', 'SF'],
    ['O2', 'S1', '25', '5', 'Yanwen Express'],
    ['O3', 'S3', '15', '2', 'Yanwen Express'],
    ['O4', 'S4', '5', '1', 'SF'],
]


def _synth_rep() -> WorkbookRepresentation:
    cols = [ColumnMeta(name=n, index=i, excel_column=i + 1,
                       excel_column_letter=chr(ord('A') + i), dtype='string')
            for i, n in enumerate(_COLS)]
    sheet = SheetRepresentation(
        sheet_name='OrderSKUList', sheet_index=0, header_mode='single', columns=cols,
        rows=[list(r) for r in _ROWS], row_excel_numbers=list(range(2, 2 + len(_ROWS))),
        row_count=len(_ROWS), column_count=len(_COLS))
    return WorkbookRepresentation(schema_version='1.0', document_id='rank-unit',
                                  user_id=1, filename='rank_unit.xlsx',
                                  file_type='xlsx', parser='xlsx', sheet_count=1,
                                  sheets=[sheet])


def _catalog(rep: WorkbookRepresentation) -> List[Dict[str, Any]]:
    return [{
        'document_id': rep.document_id, 'filename': rep.filename,
        'file_type': rep.file_type, 'created_at': '2026-09-27T00:00:00',
        'total_rows': rep.total_rows,
        'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                    'row_count': s.row_count, 'column_count': s.column_count,
                    'columns': s.column_names} for s in rep.sheets],
    }]


class _FakeLLM:
    """占位 LLM：管线的 LLM 调用点全部被 monkeypatch。"""


def _agg_turn(group_by: List[str], column: Optional[str] = 'Order Amount',
              operation: str = excel_aggregate.OPERATION_SUM,
              top_n: Optional[int] = None) -> 'nl.TurnIntent':
    """LLM 草图。**不带** order_by/order_dir：排序口径由名次守卫按用户文本决定。"""
    return nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=operation, column=column, group_by=list(group_by), top_n=top_n,
        filters=[]))


def _new_query_turn(limit: Optional[int] = None) -> 'nl.TurnIntent':
    payload: Dict[str, Any] = {'query_type': nl.INTENT_STRUCTURED}
    if limit is not None:
        payload['limit'] = limit
    return nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=nl.intent_from_dict(payload))


def _run(rep: WorkbookRepresentation, message: str, monkeypatch, *,
         turn=None, guard_agg=None, session_key: str = 'rank-unit') -> Dict[str, Any]:
    monkeypatch.setattr(excel_store, 'load_representation',
                        lambda document_id, user_id=None: rep)
    orig_turn, orig_agg = nl.llm_parse_turn, nl.llm_parse_aggregate

    async def _fake_turn(llm, msg, catalog, context=None, analysis_context=None):
        return turn

    async def _fake_agg(llm, msg, catalog, context=None):
        return guard_agg

    if turn is not None:
        monkeypatch.setattr(nl, 'llm_parse_turn', _fake_turn)
    if guard_agg is not None:
        monkeypatch.setattr(nl, 'llm_parse_aggregate', _fake_agg)
    try:
        return asyncio.run(nl.run_nl_query(
            message, _catalog(rep), llm=_FakeLLM(), user_id=1, session_key=session_key))
    finally:
        monkeypatch.setattr(nl, 'llm_parse_turn', orig_turn)
        monkeypatch.setattr(nl, 'llm_parse_aggregate', orig_agg)


def _executed_group(out: Dict[str, Any]) -> List[str]:
    gg = out.get('group_aggregate') or {}
    return [x.get('name') for x in (gg.get('group_by') or [])]


def _executed_vals(out: Dict[str, Any]) -> List[float]:
    gg = out.get('group_aggregate') or {}
    return [round(v, 2) for v in (r.get('value') for r in (gg.get('rows') or []))]


@pytest.fixture()
def rep() -> WorkbookRepresentation:
    return _synth_rep()


# ===========================================================================
# P1–P6：用户**没有**表达排名单位 -> 必须 clarify（不接受 LLM 的 group_by）
# ===========================================================================
@pytest.mark.parametrize('llm_group', [
    ['SKU ID'], ['Order ID'], ['Shipping Provider Name'], [],
])
def test_p1_p4_metric_only_topn_clarifies(rep, monkeypatch, llm_group):
    """P1–P4：「按订单金额排名前三」+ LLM 任意 group_by -> clarify(stage=order)。"""
    out = _run(rep, '按订单金额排名前三', monkeypatch,
               turn=_agg_turn(llm_group), session_key='p1-%s' % (llm_group or 'empty'))
    assert out['status'] == nl.STATUS_CLARIFY
    assert out.get('stage') == 'order'
    assert not out.get('group_aggregate')


def test_p5_statistical_guard_group_by_also_clarifies(rep, monkeypatch):
    """P5：主轮 new_query，**统计守卫**（第二次 LLM）给出 group_by -> 仍须 clarify。"""
    out = _run(rep, '按订单金额排名前三', monkeypatch, turn=_new_query_turn(),
               guard_agg=nl.AggregateIntent(
                   operation=excel_aggregate.OPERATION_SUM, column='Order Amount',
                   group_by=['SKU ID'], top_n=3,
                   order_by=excel_aggregate.ORDER_BY_AGGREGATE, order_dir='desc'),
               session_key='p5')
    assert out['status'] == nl.STATUS_CLARIFY
    assert out.get('stage') == 'order'
    assert not out.get('group_aggregate')


def test_p6_quantity_metric_only_topn_clarifies(rep, monkeypatch):
    """P6：「按数量排名前三」+ LLM group_by=['SKU ID'] -> clarify。"""
    out = _run(rep, '按数量排名前三', monkeypatch,
               turn=_agg_turn(['SKU ID'], column='Quantity'), session_key='p6')
    assert out['status'] == nl.STATUS_CLARIFY
    assert out.get('stage') == 'order'


# ===========================================================================
# P7–P10：用户**已**表达排名单位 -> 以用户文本为准（LLM 的 group_by 只是候选）
# ===========================================================================
def test_p7_order_unit_from_text_wins(rep, monkeypatch):
    """P7：「按订单金额排名前三的订单」+ LLM group_by=['SKU ID'] -> 必须按 Order ID 执行。"""
    out = _run(rep, '按订单金额排名前三的订单', monkeypatch,
               turn=_agg_turn(['SKU ID']), session_key='p7')
    assert out['status'] == nl.STATUS_OK
    assert _executed_group(out) == ['Order ID']
    assert _executed_vals(out) == [30.0, 25.0, 15.0]


def test_p8_carrier_unit_from_text_wins(rep, monkeypatch):
    """P8：「各物流商订单金额排名前三」+ LLM group_by=['SKU ID'] -> 按物流商执行。"""
    out = _run(rep, '各物流商订单金额排名前三', monkeypatch,
               turn=_agg_turn(['SKU ID']), session_key='p8')
    assert out['status'] == nl.STATUS_OK
    assert _executed_group(out) == ['Shipping Provider Name']


def test_p9_sku_unit_from_text_wins(rep, monkeypatch):
    """P9：「订单金额最高的前3个SKU」+ LLM group_by=['Order ID'] -> 按 SKU ID 执行。"""
    out = _run(rep, '订单金额最高的前3个SKU', monkeypatch,
               turn=_agg_turn(['Order ID']), session_key='p9')
    assert out['status'] == nl.STATUS_OK
    assert _executed_group(out) == ['SKU ID']
    assert _executed_vals(out) == [35.0, 20.0, 15.0]


def test_p10_carrier_topn_still_works(rep, monkeypatch):
    """P10（对应已封板 C2）：「排名前三的物流商」+ LLM group_by=['SKU ID'] -> 物流商 TOP-3。"""
    out = _run(rep, '排名前三的物流商', monkeypatch,
               turn=_agg_turn(['SKU ID'], column=None,
                              operation=excel_aggregate.OPERATION_COUNT),
               session_key='p10')
    assert out['status'] == nl.STATUS_OK
    gg = out.get('group_aggregate') or {}
    assert gg.get('operation') == excel_aggregate.OPERATION_COUNT
    assert _executed_group(out) == ['Shipping Provider Name']
    assert gg.get('top_n') == 3


def test_c1_carrier_count_topn_from_text_wins(rep, monkeypatch):
    """C1 保护：「订单最多的前3个物流商」+ LLM group_by=['SKU ID'] -> 物流商 COUNT TOP-3。"""
    out = _run(rep, '订单最多的前3个物流商', monkeypatch,
               turn=_agg_turn(['SKU ID'], column=None,
                              operation=excel_aggregate.OPERATION_COUNT),
               session_key='c1')
    assert out['status'] == nl.STATUS_OK
    assert _executed_group(out) == ['Shipping Provider Name']


# ===========================================================================
# 多维度歧义：文本同时出现多个不同维度 -> 不猜、由 LLM 决定也不行 -> clarify
# ===========================================================================
def test_multiple_dimensions_in_text_clarifies(rep, monkeypatch):
    """§六：「各物流商的SKU金额排名前三」-> 两个候选维度 -> clarify（不猜）。"""
    out = _run(rep, '各物流商的SKU金额排名前三', monkeypatch,
               turn=_agg_turn(['SKU ID']), session_key='ambig')
    assert out['status'] == nl.STATUS_CLARIFY
    assert out.get('stage') == 'order'
    assert not out.get('group_aggregate')


# ===========================================================================
# 已有能力保护：A 组 / ROW / B1 / D / E 不得被本轮收紧误伤
# ===========================================================================
@pytest.mark.parametrize('msg', [
    '查看排名前三订单', '排名前三的订单', '前3名订单', '哪几个订单排名最高',
])
def test_a_group_still_clarifies_even_if_llm_gives_order_id(rep, monkeypatch, msg):
    """A 组保护：无指标的名次问法 + LLM 自行给出 group_by=['Order ID'] -> 仍 clarify。"""
    out = _run(rep, msg, monkeypatch,
               turn=_agg_turn(['Order ID'], column=None,
                              operation=excel_aggregate.OPERATION_COUNT),
               session_key='a-%s' % msg)
    assert out['status'] == nl.STATUS_CLARIFY
    assert out.get('stage') == 'order'
    assert not out.get('group_aggregate')


@pytest.mark.parametrize('msg', ['前3条订单', '前3行订单'])
def test_row_truncation_not_affected(rep, monkeypatch, msg):
    """ROW 保护：「前N条 / 前N行」仍是行级截取 limit=N，不受名次守卫影响。

    （注入 LLM 的规范答案 limit=3 —— prompt 第 6 条要求「前N条/前N行」-> limit=N，
    与既有 test_excel_unit_topn.py::test_row_truncation_phrases_keep_limit 同口径。）
    """
    out = _run(rep, msg, monkeypatch, turn=_new_query_turn(limit=3),
               session_key='row-%s' % msg)
    assert out['status'] == nl.STATUS_OK
    assert (out.get('turn') or {}).get('action') == nl.ACTION_NEW_QUERY
    assert (out.get('query') or {}).get('limit') == 3
    assert not out.get('group_aggregate')


def test_b1_order_amount_topn_keeps_ground_truth(rep, monkeypatch):
    """B1 保护：「订单金额最高的前3个订单」-> sum + Order Amount + Order ID + TOP3 + GT。"""
    out = _run(rep, '订单金额最高的前3个订单', monkeypatch,
               turn=_agg_turn(['SKU ID']), session_key='b1')
    t = out.get('turn') or {}
    a = (t.get('aggregate') if isinstance(t, dict) else None) or {}
    assert out['status'] == nl.STATUS_OK
    assert a.get('operation') == 'sum' and a.get('column') == 'Order Amount'
    assert _executed_group(out) == ['Order ID']
    assert _executed_vals(out) == [30.0, 25.0, 15.0]


def test_b_qty_quantity_topn_keeps_ground_truth(rep, monkeypatch):
    """B-QTY 保护：「订单数量最多的前3个订单」-> sum + Quantity + Order ID + TOP3。"""
    out = _run(rep, '订单数量最多的前3个订单', monkeypatch,
               turn=_agg_turn(['SKU ID'], column='Quantity'), session_key='bqty')
    t = out.get('turn') or {}
    a = (t.get('aggregate') if isinstance(t, dict) else None) or {}
    assert out['status'] == nl.STATUS_OK
    assert a.get('operation') == 'sum' and a.get('column') == 'Quantity'
    assert _executed_group(out) == ['Order ID']


def test_d_and_e_plain_queries_not_affected(rep, monkeypatch):
    """D / E 保护：普通统计与过滤不受本轮修改影响。"""
    out = _run(rep, '一共有多少订单', monkeypatch,
               turn=_agg_turn([], column=None, operation=excel_aggregate.OPERATION_COUNT),
               session_key='d1')
    assert out['status'] == nl.STATUS_OK
    assert (out.get('aggregate') or {}).get('value') == 5.0

    out2 = _run(rep, 'SF物流商有多少订单', monkeypatch,
                turn=_new_query_turn(), session_key='e1')
    assert out2['status'] == nl.STATUS_OK


# ===========================================================================
# F2：ranking metric 同样必须来自用户文本（不能被 LLM 覆盖）
#   GT（合成表）：
#     sum(Order Amount) by 物流商 = SF 35 / Yanwen Express 40
#     sum(Quantity)     by 物流商 = SF 3  / Yanwen Express 7
#     count             by 物流商 = SF 3  / Yanwen Express 2
#     sum(Order Amount) by SKU    = S1 35 / S2 20 / S3 15
# ===========================================================================
def _exec(out):
    gg = out.get('group_aggregate') or {}
    return (gg.get('operation'), gg.get('column'), _executed_group(out), _executed_vals(out))


@pytest.mark.parametrize('text,llm_col,want_op,want_col,want_group,want_vals', [
    # F2-1：用户说「订单金额」，LLM 给 Quantity -> 必须 Order Amount
    ('各物流商订单金额排名前三', 'Quantity', 'sum', 'Order Amount',
     ['Shipping Provider Name'], [40.0, 35.0]),
    # F2-2：用户说「订单数量」，LLM 给 Order Amount -> 必须 Quantity
    ('各物流商订单数量排名前三', 'Order Amount', 'sum', 'Quantity',
     ['Shipping Provider Name'], [7.0, 3.0]),
    # F2-3：用户没说指标 -> 保持既有正式默认口径 COUNT（LLM 的 Order Amount 不生效）
    ('各物流商排名前三', 'Order Amount', 'count', None,
     ['Shipping Provider Name'], [3, 2]),
    # F2-4：同上（LLM 给 Quantity）
    ('各物流商排名前三', 'Quantity', 'count', None,
     ['Shipping Provider Name'], [3, 2]),
    # F2-5：C2 已封板形态 -> 保持 COUNT
    ('排名前三的物流商', 'Order Amount', 'count', None,
     ['Shipping Provider Name'], [3, 2]),
    # F2-6：单位级 TOP-N 的指标也必须来自文本
    ('订单金额最高的前3个SKU', 'Quantity', 'sum', 'Order Amount',
     ['SKU ID'], [35.0, 20.0, 15.0]),
])
def test_f2_ranking_metric_from_text_wins(rep, monkeypatch, text, llm_col,
                                          want_op, want_col, want_group, want_vals):
    """F2-1~F2-6：用户文本已表达指标 -> 以文本为准；未表达 -> 既有默认 COUNT。"""
    out = _run(rep, text, monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], column=llm_col,
                              top_n=3 if '排名' in text else None),
               session_key='f2-%s-%s' % (text, llm_col))
    assert out['status'] == nl.STATUS_OK, out.get('message')
    op, col, group, vals = _exec(out)
    assert (op, col) == (want_op, want_col), (op, col)
    assert group == want_group
    assert vals == want_vals


def test_f2_metric_and_unit_both_from_text(rep, monkeypatch):
    """§12：用户同时明确「单位 + 指标」时，**两个参数**都必须由用户语义决定。"""
    out = _run(rep, '各物流商订单金额排名前三', monkeypatch,
               turn=_agg_turn(['SKU ID'], column='Quantity', top_n=3),
               session_key='f2-both')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    op, col, group, vals = _exec(out)
    assert group == ['Shipping Provider Name']       # 单位由文本决定
    assert (op, col) == ('sum', 'Order Amount')      # 指标由文本决定
    assert vals == [40.0, 35.0]


def test_f2_ambiguous_metric_clarifies(rep, monkeypatch):
    """文本出现两个不同统计指标且无法唯一确定 -> clarify（不猜）。"""
    out = _run(rep, '各物流商订单金额和订单数量排名前三', monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], column='Order Amount', top_n=3),
               session_key='f2-ambig')
    assert out['status'] == nl.STATUS_CLARIFY
    assert out.get('stage') == 'order'
