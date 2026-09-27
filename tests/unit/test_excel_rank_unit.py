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
              top_n: Optional[int] = None,
              order_by: Optional[str] = None,
              order_dir: Optional[str] = None) -> 'nl.TurnIntent':
    """LLM 草图。默认不带 order_by/order_dir：排序口径由用户文本/守卫决定。"""
    return nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=operation, column=column, group_by=list(group_by), top_n=top_n,
        order_by=order_by, order_dir=order_dir, filters=[]))


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


# ===========================================================================
# G-1 / G-2（最终验收审计）：用户明确表达的**排序方向 / TOP-N 数量**不得被 LLM 覆盖
#   GT（合成表）：sum(Order Amount) by 物流商 = SF 35 / Yanwen Express 40
# ===========================================================================
@pytest.mark.parametrize('text,llm_dir,want_dir,want_vals', [
    ('订单金额从低到高排名前三的物流商', 'desc', 'asc', [35.0, 40.0]),
    ('订单金额从高到低排名前三的物流商', 'asc', 'desc', [40.0, 35.0]),
])
def test_g1_user_order_dir_wins(rep, monkeypatch, text, llm_dir, want_dir, want_vals):
    """G-1：用户明确「从低到高 / 从高到低」-> 覆盖 LLM 的 order_dir，且结果顺序 == GT。"""
    out = _run(rep, text, monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], top_n=3,
                              order_by=excel_aggregate.ORDER_BY_AGGREGATE, order_dir=llm_dir),
               session_key='g1-%s-%s' % (text, llm_dir))
    assert out['status'] == nl.STATUS_OK, out.get('message')
    gg = out.get('group_aggregate') or {}
    assert gg.get('order_dir') == want_dir
    assert _executed_vals(out) == want_vals


@pytest.mark.parametrize('text,llm_top,want_top', [
    ('各物流商订单金额前5个', 3, 5),
    ('各物流商订单金额前10个', 3, 10),
])
def test_g2_user_top_n_wins(rep, monkeypatch, text, llm_top, want_top):
    """G-2：用户明确「前N个」-> 覆盖 LLM 的 top_n（原先只在为空时补，冲突值会被执行）。"""
    out = _run(rep, text, monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], top_n=llm_top,
                              order_by=excel_aggregate.ORDER_BY_AGGREGATE, order_dir='desc'),
               session_key='g2-%s-%s' % (text, llm_top))
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert (out.get('group_aggregate') or {}).get('top_n') == want_top


@pytest.mark.parametrize('text,llm_dir,want_dir,want_vals', [
    # 极值词方向（G-1 补全）：「最低」= asc（原先被默认成 desc -> 执行了最高的 3 个）
    ('订单金额最低的3个订单', None, 'asc', [5.0, 15.0, 25.0]),
    ('金额最低的3个物流商', 'desc', 'asc', [35.0, 40.0]),
    ('金额最高的3个物流商', None, 'desc', [40.0, 35.0]),
])
def test_g1_extremum_word_direction_wins(rep, monkeypatch, text, llm_dir, want_dir, want_vals):
    """G-1 补全：用户用「最低/最高」表达方向 -> 覆盖 LLM/默认方向，且结果顺序 == GT。"""
    group = ['Order ID'] if '订单' in text else ['Shipping Provider Name']
    out = _run(rep, text, monkeypatch,
               turn=_agg_turn(group, top_n=3,
                              order_by=excel_aggregate.ORDER_BY_AGGREGATE, order_dir=llm_dir),
               session_key='g1x-%s-%s' % (text, llm_dir))
    assert out['status'] == nl.STATUS_OK, out.get('message')
    gg = out.get('group_aggregate') or {}
    assert gg.get('order_dir') == want_dir
    assert _executed_vals(out) == want_vals


def test_g2_ordinary_topn_with_wrong_llm_size(rep, monkeypatch):
    """G-2 补充：订单级 TOP-N 的 N 也必须来自文本（LLM 给 5，文本说 3）。"""
    out = _run(rep, '订单金额最高的前3个订单', monkeypatch,
               turn=_agg_turn(['Order ID'], top_n=5,
                              order_by=excel_aggregate.ORDER_BY_AGGREGATE, order_dir='desc'),
               session_key='g2-b1')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    gg = out.get('group_aggregate') or {}
    assert gg.get('top_n') == 3
    assert _executed_group(out) == ['Order ID']


# ===========================================================================
# G-4：用户文本已明确的**排序键**不得被 LLM 的 order_by 覆盖
#   GT（合成表）：count by 物流商 = SF 3 / Yanwen Express 2
#     - 按「名称」升序 -> SF(3), Yanwen(2)      => 值 [3, 2]（执行 order_by=group_column_0）
#     - 按「聚合值（计数）」升序 -> Yanwen(2), SF(3) => 值 [2, 3]（执行 aggregate_value）
# ===========================================================================
def test_g4a_dimension_sort_key_from_text_wins(rep, monkeypatch):
    """G-4a：「按物流商名称升序排列」+ LLM order_by=aggregate_value -> 必须按**分组列**排序。"""
    out = _run(rep, '按物流商名称升序排列', monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], column=None,
                              operation=excel_aggregate.OPERATION_COUNT,
                              order_by=excel_aggregate.ORDER_BY_AGGREGATE, order_dir='asc'),
               session_key='g4a')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    gg = out.get('group_aggregate') or {}
    assert gg.get('order_by') == 'group_column_0'
    assert _executed_vals(out) == [3, 2]          # 按名称升序（SF 在前）


def test_g4b_metric_sort_key_from_text_wins(rep, monkeypatch):
    """G-4b：「各物流商按订单数量降序排列」+ LLM order_by=物流商 -> 必须按**聚合值**排序。"""
    out = _run(rep, '各物流商按订单数量降序排列', monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], column='Order Amount',
                              operation=excel_aggregate.OPERATION_SUM,
                              order_by='Shipping Provider Name', order_dir='desc'),
               session_key='g4b')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    gg = out.get('group_aggregate') or {}
    assert gg.get('order_by') == excel_aggregate.ORDER_BY_AGGREGATE
    assert gg.get('operation') == excel_aggregate.OPERATION_COUNT   # 「订单数量」= 计数（既有口径）
    assert _executed_vals(out) == [3, 2]          # 按计数降序


def test_g4c_no_explicit_sort_key_keeps_llm_order_by(rep, monkeypatch):
    """G-4c 对照：用户未明确排序键 -> **保持** LLM/既有行为（不做全量 deterministic 覆盖）。"""
    out = _run(rep, '各物流商分别有多少订单', monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], column=None,
                              operation=excel_aggregate.OPERATION_COUNT,
                              order_by=excel_aggregate.ORDER_BY_AGGREGATE, order_dir='desc'),
               session_key='g4c')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    gg = out.get('group_aggregate') or {}
    assert gg.get('order_by') == excel_aggregate.ORDER_BY_AGGREGATE
    assert gg.get('order_dir') == 'desc'
    assert _executed_vals(out) == [3, 2]


def test_g4_unresolvable_sort_key_keeps_llm_value(rep, monkeypatch):
    """文本里的排序键无法解析（非真实列）-> 不介入，保持 LLM 值（绝不猜）。"""
    out = _run(rep, '按某个奇怪的维度升序排列', monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], column=None,
                              operation=excel_aggregate.OPERATION_COUNT,
                              order_by=excel_aggregate.ORDER_BY_AGGREGATE, order_dir='asc'),
               session_key='g4-unres')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert (out.get('group_aggregate') or {}).get('order_by') == excel_aggregate.ORDER_BY_AGGREGATE


# ===========================================================================
# N-1 / N-2（最终扫描）：用户明确的「排序指标」与「统计目标列」不得被 LLM 覆盖
#   GT（合成表）：sum(Order Amount) by 物流商 = SF 35 / Yanwen Express 40
#                 sum(Quantity)     by 物流商 = SF 3  / Yanwen Express 7
# ===========================================================================
@pytest.mark.parametrize('text,llm_dir,want_dir,want_vals', [
    ('各物流商订单金额从高到低排列', 'desc', 'desc', [40.0, 35.0]),
    ('各物流商订单金额从低到高排列', 'asc', 'asc', [35.0, 40.0]),
])
def test_n1_metric_sort_with_direction_wins(rep, monkeypatch, text, llm_dir, want_dir, want_vals):
    """N-1：文本「<度量>从高到低/从低到高排列」（无「按X」）-> order_by=aggregate_value。"""
    out = _run(rep, text, monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], order_by='Shipping Provider Name',
                              order_dir=llm_dir),
               session_key='n1-%s' % text)
    assert out['status'] == nl.STATUS_OK, out.get('message')
    gg = out.get('group_aggregate') or {}
    assert gg.get('order_by') == excel_aggregate.ORDER_BY_AGGREGATE   # 不是 group_column_0
    assert gg.get('order_dir') == want_dir
    assert _executed_vals(out) == want_vals


def test_n1_helper_boundary_aggregate_only_not_sort_semantics(rep):
    """N-1 边界：无方向词的聚合口径（「订单金额最高是多少」）不得被当成排序语义。"""
    sheet = rep.sheets[0]
    assert nl._user_sort_key('各物流商订单金额最高是多少', sheet,
                             ['Shipping Provider Name']) == (None, None)
    # 有方向词时才成立
    assert nl._user_sort_key('各物流商订单金额从高到低排列', sheet,
                             ['Shipping Provider Name'])[0] == excel_aggregate.ORDER_BY_AGGREGATE


@pytest.mark.parametrize('text,llm_col,want_col,want_op,want_vals', [
    ('各物流商的订单金额总和', 'Quantity', 'Order Amount', 'sum', [40.0, 35.0]),
    ('各物流商的订单金额最大值', 'Quantity', 'Order Amount', 'max', [25.0, 20.0]),
    ('各物流商的订单数量总和', 'Order Amount', 'Quantity', 'sum', [7.0, 3.0]),
])
def test_n2_user_metric_column_wins(rep, monkeypatch, text, llm_col, want_col, want_op, want_vals):
    """N-2：用户明确 metric -> 覆盖 LLM 的 column（且口径由既有规则决定）。"""
    out = _run(rep, text, monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], column=llm_col,
                              operation=(excel_aggregate.OPERATION_MAX
                                         if want_op == 'max' else excel_aggregate.OPERATION_SUM)),
               session_key='n2-%s-%s' % (text, llm_col))
    assert out['status'] == nl.STATUS_OK, out.get('message')
    gg = out.get('group_aggregate') or {}
    assert gg.get('column') == want_col
    assert gg.get('operation') == want_op
    assert sorted(_executed_vals(out)) == sorted(want_vals)


def test_n2_no_metric_word_keeps_llm_column(rep, monkeypatch):
    """N-2 对照：文本没有 metric 词 -> 保持 LLM 合法 column（不做全量 deterministic）。"""
    out = _run(rep, '各物流商的分组汇总', monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], column='Quantity'),
               session_key='n2-nokeep')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert (out.get('group_aggregate') or {}).get('column') == 'Quantity'


def test_n2_count_semantics_not_affected(rep, monkeypatch):
    """N-2 保护（§五）：COUNT 语义不受影响 —— 仍是 COUNT + 分组列，column 保持 None。"""
    out = _run(rep, '各物流商分别有多少订单', monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], column=None,
                              operation=excel_aggregate.OPERATION_COUNT),
               session_key='n2-count')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    gg = out.get('group_aggregate') or {}
    assert gg.get('operation') == 'count' and gg.get('column') is None
    assert sorted(_executed_vals(out)) == [2, 3]      # 无排序语境 -> 不锁定分组顺序


# ===========================================================================
# S-A ~ S-E（2026-09-27 最终扫描）：operation / group_by / filter 同样"用户语义优先"
#   GT（合成表）：sum(Amount) by 物流商 = SF 35 / Yanwen 40
#                 avg(Amount) by 物流商 = SF 11.67 / Yanwen 20.0
#                 max(Amount) by 物流商 = SF 20 / Yanwen 25
#                 sum(Amount) by SKU ID = S1 30 / S2 20 / S3 15 / S4 5
# ===========================================================================
def test_sa_user_sum_overrides_llm_avg(rep, monkeypatch):
    """S-A：文本「总和」+ LLM operation=avg -> 必须执行 SUM。"""
    out = _run(rep, '各物流商的订单金额总和', monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], operation=excel_aggregate.OPERATION_AVG),
               session_key='sa')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    gg = out.get('group_aggregate') or {}
    assert gg.get('operation') == 'sum' and gg.get('column') == 'Order Amount'
    assert sorted(_executed_vals(out)) == [35.0, 40.0]


def test_sb_user_avg_overrides_llm_max(rep, monkeypatch):
    """S-B：文本「平均值」+ LLM operation=max -> 必须执行 AVG。"""
    out = _run(rep, '各物流商的订单金额平均值', monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], operation=excel_aggregate.OPERATION_MAX),
               session_key='sb')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    gg = out.get('group_aggregate') or {}
    assert gg.get('operation') == 'avg'
    assert sorted(_executed_vals(out)) == [11.67, 20.0]


def test_sc_user_group_overrides_llm_sku(rep, monkeypatch):
    """S-C：文本「各物流商」+ LLM group_by=['SKU ID'] -> 必须按物流商分组。"""
    out = _run(rep, '各物流商的订单金额总和', monkeypatch,
               turn=_agg_turn(['SKU ID']), session_key='sc')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _executed_group(out) == ['Shipping Provider Name']
    assert sorted(_executed_vals(out)) == [35.0, 40.0]


def test_sd_user_group_sku_overrides_llm_carrier(rep, monkeypatch):
    """S-D：文本「各SKU」+ LLM group_by=['Shipping Provider Name'] -> 必须按 SKU ID 分组。"""
    out = _run(rep, '各SKU的订单金额总和', monkeypatch,
               turn=_agg_turn(['Shipping Provider Name']), session_key='sd')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _executed_group(out) == ['SKU ID']
    # GT：S1=10+25=35、S2=20、S3=15、S4=5
    assert sorted(_executed_vals(out)) == [5.0, 15.0, 20.0, 35.0]


def test_se_user_filter_value_overrides_llm_value(rep, monkeypatch):
    """S-E：文本「物流商为SF」+ LLM 给了别的筛选值 -> 必须以用户文本为准（值=SF）。"""
    turn = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=excel_aggregate.OPERATION_SUM, column='Order Amount',
        group_by=[], filters=[{'column': 'Shipping Provider Name', 'operator': 'eq',
                               'value': 'Yanwen Express'}]))
    out = _run(rep, '物流商为SF的订单金额总和', monkeypatch, turn=turn, session_key='se')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    a = out.get('aggregate') or {}
    assert [f.get('value') for f in (a.get('filters') or [])] == ['SF']
    assert a.get('value') == pytest.approx(35.0, abs=0.001)   # SF 的订单金额合计


@pytest.mark.parametrize('text,llm_op,want_op', [
    ('各物流商的金额汇总', 'avg', 'avg'),        # 无「总和/平均」词 -> 保持 LLM 口径
    ('各物流商的金额汇总', 'max', 'max'),
])
def test_s_boundary_no_operation_word_keeps_llm(rep, monkeypatch, text, llm_op, want_op):
    """对照：文本没有明确口径词 -> 保持 LLM 合法 operation。"""
    out = _run(rep, text, monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], operation=llm_op),
               session_key='s-noop-%s-%s' % (text, llm_op))
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert (out.get('group_aggregate') or {}).get('operation') == want_op


def test_s_boundary_no_group_word_keeps_llm_group(rep, monkeypatch):
    """对照：文本没有分组语义 -> 保持 LLM 合法 group_by。"""
    out = _run(rep, '一店的订单金额总和', monkeypatch,
               turn=_agg_turn(['SKU ID']), session_key='s-nogroup')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _executed_group(out) == ['SKU ID']


def test_s_boundary_no_filter_word_keeps_llm_filter(rep, monkeypatch):
    """对照：文本没有筛选语义 -> 保持 LLM 合法 filter。"""
    turn = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=excel_aggregate.OPERATION_SUM, column='Order Amount', group_by=[],
        filters=[{'column': 'Shipping Provider Name', 'operator': 'eq',
                  'value': 'Yanwen Express'}]))
    out = _run(rep, '各物流商的订单金额总和', monkeypatch, turn=turn, session_key='s-nofilter')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    blk = out.get('aggregate') or out.get('group_aggregate') or {}
    assert [f.get('value') for f in (blk.get('filters') or [])] == ['Yanwen Express']


def test_s_unresolvable_filter_still_rejected(rep, monkeypatch):
    """对照：不可解析的 filter 列 -> 仍走既有澄清路径（不被 authority 洗白）。"""
    turn = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=excel_aggregate.OPERATION_SUM, column='Order Amount', group_by=[],
        filters=[{'column': 'NoSuchColumn', 'operator': 'eq', 'value': 'X'}]))
    out = _run(rep, '物流商为SF的订单金额总和', monkeypatch, turn=turn, session_key='s-badfilter')
    assert out['status'] in (nl.STATUS_CLARIFY, nl.STATUS_ERROR)


def test_s_injection_filter_value_not_laundered(rep, monkeypatch):
    """安全：注入样式的 filter 值不得被 authority helper 换成合法值。"""
    turn = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=excel_aggregate.OPERATION_SUM, column='Order Amount', group_by=[],
        filters=[{'column': 'Shipping Provider Name', 'operator': 'eq',
                  'value': 'SF; DROP TABLE t'}]))
    out = _run(rep, '物流商为SF的订单金额总和', monkeypatch, turn=turn, session_key='s-inj')
    a = (out.get('aggregate') or {})
    vals = [f.get('value') for f in (a.get('filters') or [])]
    assert vals == ['SF; DROP TABLE t']            # 未被"洗白"成 SF


def test_s_count_semantics_preserved(rep, monkeypatch):
    """§八：COUNT 语义不因本轮 operation authority 改变（无度量词 -> 不动作）。"""
    out = _run(rep, '各物流商分别有多少订单', monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], column=None,
                              operation=excel_aggregate.OPERATION_COUNT),
               session_key='s-count')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    gg = out.get('group_aggregate') or {}
    assert gg.get('operation') == 'count' and gg.get('column') is None
    assert sorted(_executed_vals(out)) == [2.0, 3.0]


def test_s_which_question_never_becomes_max(rep, monkeypatch):
    """边界：「哪个物流商订单最多？」的「最多」是排名依据，**不得**被当成 MAX 聚合。"""
    out = _run(rep, '哪个物流商订单最多？', monkeypatch,
               turn=_agg_turn(['Shipping Provider Name'], column=None,
                              operation=excel_aggregate.OPERATION_COUNT, top_n=1,
                              order_by=excel_aggregate.ORDER_BY_AGGREGATE, order_dir='desc'),
               session_key='s-which')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    gg = out.get('group_aggregate') or {}
    assert gg.get('operation') == 'count'          # 不是 max


def test_s_analysis_step1_group_and_filter_authority(rep, monkeypatch):
    """§七：analysis 第 1 步的 group_by / filter 同样受用户文本约束（同一 helper）。"""
    from types import SimpleNamespace

    from backend.excel import nl_normalize as nl_norm
    monkeypatch.setattr(excel_store, 'load_representation',
                        lambda document_id, user_id=None: rep)
    sheet = rep.sheets[0]
    ns = SimpleNamespace(operation='sum', column='Order Amount',
                         group_by=['SKU ID'], filters=[], calculation=None)
    msg = '各物流商的订单金额总和是多少'
    notes = nl._enforce_user_aggregate_params(ns, msg, sheet,
                                              nl_norm.detect_signals(msg), _catalog(rep),
                                              enforce_operation=False)
    assert notes and ns.group_by == ['Shipping Provider Name']

    ns2 = SimpleNamespace(operation='sum', column='Order Amount', group_by=[],
                          filters=[{'column': 'Shipping Provider Name', 'operator': 'eq',
                                    'value': 'Yanwen Express'}], calculation=None)
    msg2 = '物流商为SF的订单金额总和是多少'
    notes2 = nl._enforce_user_aggregate_params(ns2, msg2, sheet,
                                               nl_norm.detect_signals(msg2), _catalog(rep),
                                               enforce_operation=False)
    assert notes2 and [f.get('value') for f in ns2.filters] == ['SF']


# ===========================================================================
# P1-A / P1-B（参数全集扫描）：new_query 的「列清单」与 filter 的「operator」
# 同样必须"用户语义优先"（LLM 的合法但冲突值不得胜出）
# ===========================================================================
def _nq_turn(columns=None, filters=None, limit=None) -> 'nl.TurnIntent':
    payload: Dict[str, Any] = {'query_type': nl.INTENT_STRUCTURED}
    if columns is not None:
        payload['columns'] = list(columns)
    if filters:
        payload['filters'] = list(filters)
    if limit is not None:
        payload['limit'] = limit
    return nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=nl.intent_from_dict(payload))


def _result_columns(out: Dict[str, Any]) -> List[str]:
    return [c.get('name') for c in ((out.get('result') or {}).get('columns') or [])]


def test_p1a_user_columns_override_llm_multi(rep, monkeypatch):
    """P1-A：文本「列出订单号和物流商」+ LLM columns=['Quantity'] -> 以用户点名为准（保序）。"""
    out = _run(rep, '列出订单号和物流商', monkeypatch,
               turn=_nq_turn(columns=['Quantity']), session_key='p1a-multi')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _result_columns(out) == ['Order ID', 'Shipping Provider Name']
    # 上下文同样带上用户列 -> 后续分页/续查自动复用（不回到 LLM 列）
    assert (out.get('new_context') or {}).get('columns') == ['Order ID', 'Shipping Provider Name']


def test_p1a_user_columns_override_llm_single(rep, monkeypatch):
    """P1-A：单列点名同样以用户文本为准。"""
    out = _run(rep, '列出订单号', monkeypatch,
               turn=_nq_turn(columns=['Quantity']), session_key='p1a-single')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _result_columns(out) == ['Order ID']


def test_p1a_no_list_verb_keeps_llm_columns(rep, monkeypatch):
    """对照：文本没有列清单语义 -> 保持 LLM 合法 columns。"""
    out = _run(rep, '有哪些订单？', monkeypatch,
               turn=_nq_turn(columns=['Quantity']), session_key='p1a-keep')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _result_columns(out) == ['Quantity']


def test_p1a_unresolvable_llm_column_still_clarifies(rep, monkeypatch):
    """对照：LLM 列不可解析 -> 仍走既有澄清路径（不被 authority 洗白）。"""
    out = _run(rep, '列出订单号和物流商', monkeypatch,
               turn=_nq_turn(columns=['NoSuchColumn']), session_key='p1a-bad')
    assert out['status'] == nl.STATUS_CLARIFY
    assert not out.get('result')


def test_p1a_malicious_llm_column_not_laundered(rep, monkeypatch):
    """安全：注入型 LLM 列不得被替换成合法列后执行。"""
    out = _run(rep, '列出订单号和物流商', monkeypatch,
               turn=_nq_turn(columns=['Quantity; DROP TABLE t']), session_key='p1a-inj')
    assert out['status'] in (nl.STATUS_CLARIFY, nl.STATUS_ERROR)
    assert not out.get('result')


def test_p1a_all_rows_enumeration_preserved(rep, monkeypatch):
    """§十一：「列出所有SKU」仍是全量枚举（5 行、不去重、列=SKU ID）。"""
    out = _run(rep, '列出所有SKU', monkeypatch,
               turn=_nq_turn(columns=['SKU ID'], limit=50), session_key='p1a-all')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _result_columns(out) == ['SKU ID']
    assert (out.get('result') or {}).get('total_matches') == len(_ROWS)


def _agg_filters(out: Dict[str, Any]) -> List[Any]:
    blk = out.get('aggregate') or out.get('group_aggregate') or {}
    return [(f.get('column'), f.get('operator'), f.get('value')) for f in (blk.get('filters') or [])]


def test_p1b_gt_overrides_llm_lt(rep, monkeypatch):
    """P1-B：「数量大于3」+ LLM operator=lt -> 必须执行 gt（GT：O2=25）。"""
    turn = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=excel_aggregate.OPERATION_SUM, column='Order Amount', group_by=[],
        filters=[{'column': 'Quantity', 'operator': 'lt', 'value': 3}]))
    out = _run(rep, '数量大于3的订单金额总和', monkeypatch, turn=turn, session_key='p1b-gt')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _agg_filters(out) == [('Quantity', 'gt', 3.0)]
    assert (out.get('aggregate') or {}).get('value') == pytest.approx(25.0, abs=0.001)


def test_p1b_lt_overrides_llm_gt(rep, monkeypatch):
    """P1-B：「数量小于3」+ LLM operator=gt -> 必须执行 lt（GT：10+20+15+5=50）。"""
    turn = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=excel_aggregate.OPERATION_SUM, column='Order Amount', group_by=[],
        filters=[{'column': 'Quantity', 'operator': 'gt', 'value': 3}]))
    out = _run(rep, '数量小于3的订单金额总和', monkeypatch, turn=turn, session_key='p1b-lt')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _agg_filters(out) == [('Quantity', 'lt', 3.0)]
    assert (out.get('aggregate') or {}).get('value') == pytest.approx(50.0, abs=0.001)


@pytest.mark.parametrize('text,llm_op,want_op,want_val', [
    ('数量大于等于5的订单金额总和', 'lt', 'gte', 25.0),
    ('数量小于等于1的订单金额总和', 'gt', 'lte', 35.0),
])
def test_p1b_gte_lte_supported_words(rep, monkeypatch, text, llm_op, want_op, want_val):
    """§四：既有语义表内的 >= / <= 表达同样以用户文本为准。"""
    turn = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=excel_aggregate.OPERATION_SUM, column='Order Amount', group_by=[],
        filters=[{'column': 'Quantity', 'operator': llm_op, 'value': 3}]))
    out = _run(rep, text, monkeypatch, turn=turn, session_key='p1b-%s' % want_op)
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert _agg_filters(out) == [('Quantity', want_op, float(text.split('于')[-1][0]))]
    assert (out.get('aggregate') or {}).get('value') == pytest.approx(want_val, abs=0.001)


def test_p1b_no_comparison_word_keeps_llm_operator(rep, monkeypatch):
    """对照：文本没有比较语义 -> 保持 LLM 合法 operator。"""
    turn = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=excel_aggregate.OPERATION_SUM, column='Order Amount',
        group_by=['Shipping Provider Name'],
        filters=[{'column': 'Quantity', 'operator': 'lt', 'value': 3}]))
    out = _run(rep, '各物流商的订单金额总和', monkeypatch, turn=turn, session_key='p1b-keep')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    assert ('Quantity', 'lt', 3.0) in _agg_filters(out)


def test_p1b_invalid_llm_operator_not_laundered(rep, monkeypatch):
    """安全：LLM 的非法 operator 不得被用户语义"洗白"成合法条件。"""
    turn = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=excel_aggregate.OPERATION_SUM, column='Order Amount', group_by=[],
        filters=[{'column': 'Quantity', 'operator': 'lt; DROP TABLE t', 'value': 3}]))
    out = _run(rep, '数量大于3的订单金额总和', monkeypatch, turn=turn, session_key='p1b-inj')
    assert out['status'] in (nl.STATUS_CLARIFY, nl.STATUS_ERROR)
    assert not out.get('aggregate')


def test_p1b_field_operator_value_combined(rep, monkeypatch):
    """§五：field / operator / value 三层同时以用户文本为准（不再出现 LLM 的 JS）。"""
    turn = nl.TurnIntent(action=nl.ACTION_AGGREGATE, aggregate=nl.AggregateIntent(
        operation=excel_aggregate.OPERATION_SUM, column='Order Amount', group_by=[],
        filters=[{'column': 'Shipping Provider Name', 'operator': 'eq', 'value': 'Yanwen Express'}]))
    out = _run(rep, '物流商为SF且数量大于3的订单金额总和', monkeypatch, turn=turn,
               session_key='p1b-combined')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    got = _agg_filters(out)
    assert ('Shipping Provider Name', 'eq', 'SF') in got
    assert ('Quantity', 'gt', 3.0) in got
    assert all(v != 'Yanwen Express' for _c, _o, v in got)


def test_p1b_new_query_operator_authority(rep, monkeypatch):
    """P1-B（new_query 路径）：同一套 authority 在 `build_validated_query` 生效。"""
    out = _run(rep, '数量大于3的订单有哪些？', monkeypatch,
               turn=_nq_turn(filters=[{'column': 'Quantity', 'operator': 'lt', 'value': 3}]),
               session_key='p1b-nq')
    assert out['status'] == nl.STATUS_OK, out.get('message')
    got = [((out.get('result') or {}).get('total_matches')),
           [f.get('operator') for f in ((out.get('new_context') or {}).get('filters') or [])]]
    assert got == [1, ['gt']]


# ---- A3 语义保护：不得因本轮收紧而改变「前N名 / 前N条」的既有归属 ----
def test_a3_rank_phrase_without_unit_still_clarifies_not_row_limit(rep, monkeypatch):
    """「排名前5的订单」仍是**排名语义**（无单位 -> clarify），不得退化为行级 limit。"""
    out = _run(rep, '排名前5的订单', monkeypatch,
               turn=_new_query_turn(limit=5), session_key='a3-rank')
    assert out['status'] == nl.STATUS_CLARIFY
    assert out.get('stage') == 'order'


def test_a3_row_phrases_keep_row_level_limit(rep, monkeypatch):
    """「前5条 / 前5行」仍是**行级截取**（new_query + limit），不受 TOP-N 收紧影响。"""
    for msg in ('前5条订单', '前5行订单'):
        out = _run(rep, msg, monkeypatch, turn=_new_query_turn(limit=5),
                   session_key='a3-row-%s' % msg)
        assert out['status'] == nl.STATUS_OK, out.get('message')
        assert (out.get('turn') or {}).get('action') == nl.ACTION_NEW_QUERY
        assert (out.get('query') or {}).get('limit') == 5
        assert not out.get('group_aggregate')
