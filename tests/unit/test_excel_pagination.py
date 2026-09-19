# -*- coding: utf-8 -*-
"""Phase 1D：对话内连续分页 / 追问 测试矩阵

测试策略：
- LLM 只负责「判断动作」，因此把动作直接注入（TurnIntent），
  从而稳定测试真正决定正确性的链路：
      (上一轮上下文 + 动作) -> Python 计算 offset/limit -> Phase 1B 取数 -> Ground Truth 对照
- Ground Truth 独立计算：直接遍历 representation.rows / row_excel_numbers，
  逐行比较 row content + 顺序 + Excel 行号。
- 另测「LLM 不可用时的严格分页短语降级」与「上下文隔离/失效」。
"""

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from backend.excel import nl_query as nl
from backend.excel import query as q
from backend.excel import store as excel_store
from backend.excel.parser import ExcelParser
from backend.excel.query_context import (
    ExcelQueryContext,
    ExcelQueryContextStore,
    TURN_EXCEL,
    TURN_OTHER,
    reset_context_store,
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
def big_rep() -> 'nl.WorkbookRepresentation':
    path = _big_path()
    if path is None:
        pytest.skip('未找到大表（直邮5店 7.10号订单.xlsx）')
    return ExcelParser().parse(str(path), 'big-doc', user_id=1, filename=path.name)


@pytest.fixture(scope='module')
def small_rep() -> 'nl.WorkbookRepresentation':
    if not SMALL_REAL.exists():
        pytest.skip('未找到小表（直邮一店 8.20号订单.xlsx）')
    return ExcelParser().parse(str(SMALL_REAL), 'small-doc', user_id=1, filename=SMALL_REAL.name)


def _catalog(rep) -> List[Dict[str, Any]]:
    return [{
        'document_id': rep.document_id,
        'filename': rep.filename,
        'file_type': rep.file_type,
        'created_at': '2026-09-14T00:00:00',
        'total_rows': rep.total_rows,
        'sheets': [
            {'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
             'row_count': s.row_count, 'column_count': s.column_count,
             'columns': s.column_names}
            for s in rep.sheets
        ],
    }]


def _turn(rep, message: str, *, ctx=None, override=None, llm=None,
          user_id=1, session_key='s1') -> Dict[str, Any]:
    """执行一轮对话（store 读取替换为内存 representation）。"""
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nl.run_nl_query(
            message, _catalog(rep), llm=llm, context=ctx,
            pagination_override=override, user_id=user_id, session_key=session_key,
        ))
    finally:
        excel_store.load_representation = original


def _ctx_of(out: Dict[str, Any]) -> ExcelQueryContext:
    return ExcelQueryContext(**out['new_context'])


def _gt(rep, offset: int, limit: int, columns: List[str]):
    """独立 Ground Truth：返回 (期望行, 期望 Excel 行号, 命中总数)。"""
    sheet = rep.sheets[0]
    names = sheet.column_names
    idx = [names.index(c) for c in columns] if columns else list(range(len(names)))
    total = len(sheet.rows)
    rows = [[sheet.rows[i][j] for j in idx] for i in range(offset, min(offset + limit, total))]
    nums = sheet.row_excel_numbers[offset:min(offset + limit, total)]
    return rows, nums, total


def _start(rep, *, columns=None, doc='5店', filters=None, limit=20) -> Dict[str, Any]:
    """发起一轮新的表格查询。"""
    intent = nl.NlIntent(document=doc, columns=columns or ['SKU'], filters=filters or [], limit=limit)
    return _turn(rep, '列出前N条', override=nl.TurnIntent(action=nl.ACTION_NEW_QUERY, intent=intent))


def _paginate(rep, ctx, action, *, limit=None, start_index=None, end_index=None, message='翻页'):
    override = nl.TurnIntent(action=action, limit=limit, start_index=start_index, end_index=end_index)
    return _turn(rep, message, ctx=ctx, override=override)


# ==========================================================================
# 1. 分页短语的确定性解析（降级路径）
# ==========================================================================
def test_deterministic_pagination_next():
    for text, exp_limit in [('下一页', None), ('再来20条', 20), ('继续', None),
                            ('继续20条', 20), ('往下再看20条', 20), ('继续看30条', 30)]:
        t = nl.deterministic_pagination(text)
        assert t is not None and t.action == nl.ACTION_NEXT, text
        assert t.limit == exp_limit, f'{text} -> {t.limit}'


def test_deterministic_pagination_prev():
    for text, exp_limit in [('上一页', None), ('往前20条', 20), ('往前看10条', 10)]:
        t = nl.deterministic_pagination(text)
        assert t is not None and t.action == nl.ACTION_PREV, text
        assert t.limit == exp_limit, f'{text} -> {t.limit}'


def test_deterministic_pagination_range_and_start():
    for text, a, b in [('看第51到100条', 51, 100), ('第51~100条', 51, 100),
                       ('给我51-100条', 51, 100), ('查看第101到150条', 101, 150)]:
        t = nl.deterministic_pagination(text)
        assert t is not None and t.action == nl.ACTION_RANGE, text
        assert (t.start_index, t.end_index) == (a, b), f'{text} -> {t.start_index},{t.end_index}'

    for text, a, lim in [('从第101条开始给我20条', 101, 20), ('从101开始看20条', 101, 20),
                         ('从第51条开始', 51, None)]:
        t = nl.deterministic_pagination(text)
        assert t is not None and t.action == nl.ACTION_START, text
        assert (t.start_index, t.limit) == (a, lim), f'{text} -> {t.start_index},{t.limit}'


def test_deterministic_pagination_returns_none_for_query():
    assert nl.deterministic_pagination('列出5店前20条SKU') is None
    assert nl.deterministic_pagination('你好') is None


# ==========================================================================
# 2. offset / limit 计算规则（纯 Python）
# ==========================================================================
def _ctx(offset=0, limit=20, total=447):
    return ExcelQueryContext(user_id=1, session_key='s1', document_id='d', filename='f.xlsx',
                             sheet_index=0, sheet_name='S', columns=['SKU ID'], filters=[],
                             limit=limit, offset=offset, total_matches=total)


def test_compute_page_next_prev():
    c = _ctx(offset=0, limit=20)
    assert nl.compute_page(nl.TurnIntent(action=nl.ACTION_NEXT), c).offset == 20
    assert nl.compute_page(nl.TurnIntent(action=nl.ACTION_NEXT, limit=20), c).offset == 20
    assert nl.compute_page(nl.TurnIntent(action=nl.ACTION_NEXT, limit=50), c).limit == 50

    c2 = _ctx(offset=40, limit=20)
    assert nl.compute_page(nl.TurnIntent(action=nl.ACTION_NEXT), c2).offset == 60
    assert nl.compute_page(nl.TurnIntent(action=nl.ACTION_PREV), c2).offset == 20
    assert nl.compute_page(nl.TurnIntent(action=nl.ACTION_PREV, limit=20), c2).offset == 20

    # 第一页"上一页" -> 保持 0（不为负）
    c3 = _ctx(offset=0, limit=20)
    assert nl.compute_page(nl.TurnIntent(action=nl.ACTION_PREV), c3).offset == 0


def test_compute_page_range_and_start():
    c = _ctx(offset=40, limit=20)
    p = nl.compute_page(nl.TurnIntent(action=nl.ACTION_RANGE, start_index=51, end_index=100), c)
    assert (p.offset, p.limit) == (50, 50)
    p2 = nl.compute_page(nl.TurnIntent(action=nl.ACTION_START, start_index=101, limit=20), c)
    assert (p2.offset, p2.limit) == (100, 20)
    p3 = nl.compute_page(nl.TurnIntent(action=nl.ACTION_START, start_index=51), c)
    assert (p3.offset, p3.limit) == (50, 20)   # limit 继承上一轮
    # 非法区间
    assert nl.compute_page(nl.TurnIntent(action=nl.ACTION_RANGE, start_index=100, end_index=51), c).error
    assert nl.compute_page(nl.TurnIntent(action=nl.ACTION_START), c).error
    # 超长区间被收敛到上限
    p4 = nl.compute_page(nl.TurnIntent(action=nl.ACTION_RANGE, start_index=1, end_index=5000), c)
    assert p4.limit == nl.MAX_NL_LIMIT and p4.offset == 0


# ==========================================================================
# 3. 上下文存储：隔离 / 失效
# ==========================================================================
def test_context_store_user_and_session_isolation():
    store = ExcelQueryContextStore()
    c = _ctx()
    store.save_excel(c)
    assert store.get_for_pagination(1, 's1') is not None
    assert store.get_for_pagination(2, 's1') is None      # 不同 user 不串
    assert store.get_for_pagination(1, 's2') is None      # 不同 session 不串


def test_context_store_other_turn_blocks_pagination():
    store = ExcelQueryContextStore()
    store.save_excel(_ctx())
    assert store.get_last_turn(1, 's1') == TURN_EXCEL
    store.mark_other_turn(1, 's1')
    assert store.get_last_turn(1, 's1') == TURN_OTHER
    assert store.get_for_pagination(1, 's1') is None      # 普通聊天后不允许复用


def test_context_store_ttl_expiry():
    store = ExcelQueryContextStore(ttl_seconds=1)
    c = _ctx()
    store.save_excel(c)
    store._contexts[list(store._contexts)[0]].updated_at = time.time() - 10
    assert store.get_for_pagination(1, 's1') is None


def test_context_store_clear():
    store = ExcelQueryContextStore()
    store.save_excel(_ctx())
    store.clear(1, 's1')
    assert store.get_for_pagination(1, 's1') is None


# ==========================================================================
# 4. 核心：连续分页（大表 447 行）与 Ground Truth 逐行对照
# ==========================================================================
def test_continuous_pagination_three_pages(big_rep, capsys):
    """列出5店前20条SKU -> 再来20条 -> 再来20条（三次结果连续且不重复）。"""
    o1 = _start(big_rep, columns=['SKU'], limit=20)
    assert o1['status'] == 'ok' and o1['continued'] is False
    ctx = _ctx_of(o1)

    o2 = _paginate(big_rep, ctx, nl.ACTION_NEXT, limit=20, message='再来20条')
    assert o2['status'] == 'ok' and o2['continued'] is True
    ctx = _ctx_of(o2)

    o3 = _paginate(big_rep, ctx, nl.ACTION_NEXT, limit=20, message='再来20条')
    assert o3['status'] == 'ok'
    ctx = _ctx_of(o3)

    sheet = big_rep.sheets[0]
    si = sheet.column_names.index('SKU ID')

    pages = []
    for label, out, off in [('第一次', o1, 0), ('第二次', o2, 20), ('第三次', o3, 40)]:
        exp_rows = [[r[si]] for r in sheet.rows[off:off + 20]]
        exp_nums = sheet.row_excel_numbers[off:off + 20]
        assert out['result']['rows'] == exp_rows, f'{label} 行内容必须与 Ground Truth 一致'
        assert out['result']['row_excel_numbers'] == exp_nums, f'{label} Excel 行号不一致'
        assert out['result']['offset'] == off and out['result']['limit'] == 20
        pages.append(exp_rows)

    # 三次不重复、且拼起来正是前 60 条
    flat = [r[0] for page in pages for r in page]
    assert len(set(flat)) == 60
    assert flat == [r[si] for r in sheet.rows[:60]]

    with capsys.disabled():
        print('\n[NL 连续分页] 列出5店前20条SKU')
        for label, out in [('第一次', o1), ('第二次', o2), ('第三次', o3)]:
            r = out['result']
            print(f'  {label}: Excel 数据 {r["offset"] + 1}~{r["offset"] + r["returned_count"]} '
                  f'| Excel 行号 {r["row_excel_numbers"][0]}~{r["row_excel_numbers"][-1]} '
                  f'| 首行 SKU={r["rows"][0][0]}')


def test_next_page_keeps_context_no_reparse(big_rep):
    """『下一页』必须继承 document/sheet/columns，不允许重新猜。"""
    o1 = _start(big_rep, columns=['SKU'], limit=20)
    ctx = _ctx_of(o1)
    o2 = _paginate(big_rep, ctx, nl.ACTION_NEXT, message='下一页')

    assert o2['document']['document_id'] == o1['document']['document_id']
    assert o2['sheet']['sheet_name'] == o1['sheet']['sheet_name']
    assert o2['query']['columns'] == o1['query']['columns'] == ['SKU ID']
    assert o2['result']['offset'] == 20 and o2['result']['limit'] == 20
    assert o2['pagination']['from_offset'] == 0


def test_prev_page_returns_previous_window(big_rep):
    o1 = _start(big_rep, limit=20)
    ctx = _ctx_of(o1)
    ctx = _ctx_of(_paginate(big_rep, ctx, nl.ACTION_NEXT, message='下一页'))   # offset=20
    ctx = _ctx_of(_paginate(big_rep, ctx, nl.ACTION_NEXT, message='下一页'))   # offset=40
    o = _paginate(big_rep, ctx, nl.ACTION_PREV, message='上一页')
    assert o['result']['offset'] == 20 and o['result']['limit'] == 20

    sheet = big_rep.sheets[0]
    si = sheet.column_names.index('SKU ID')
    assert o['result']['rows'] == [[r[si]] for r in sheet.rows[20:40]]


def test_prev_on_first_page_stays_zero(big_rep):
    o1 = _start(big_rep, limit=20)
    o = _paginate(big_rep, _ctx_of(o1), nl.ACTION_PREV, message='上一页')
    assert o['status'] == 'ok'
    assert o['result']['offset'] == 0
    sheet = big_rep.sheets[0]
    si = sheet.column_names.index('SKU ID')
    assert o['result']['rows'] == [[r[si]] for r in sheet.rows[:20]]


def test_range_51_to_100(big_rep):
    o1 = _start(big_rep, limit=20)
    o = _paginate(big_rep, _ctx_of(o1), nl.ACTION_RANGE, start_index=51, end_index=100,
                  message='看第51到100条')
    assert o['status'] == 'ok'
    assert o['result']['offset'] == 50 and o['result']['limit'] == 50
    assert o['result']['returned_count'] == 50
    sheet = big_rep.sheets[0]
    si = sheet.column_names.index('SKU ID')
    assert o['result']['rows'] == [[r[si]] for r in sheet.rows[50:100]]
    assert o['result']['row_excel_numbers'] == sheet.row_excel_numbers[50:100]
    # 用户说的"第51条"是数据序号，不是 Excel 行号
    assert o['result']['row_excel_numbers'][0] != 51


def test_start_from_101_with_20(big_rep):
    o1 = _start(big_rep, limit=20)
    o = _paginate(big_rep, _ctx_of(o1), nl.ACTION_START, start_index=101, limit=20,
                  message='从第101条开始给我20条')
    assert o['result']['offset'] == 100 and o['result']['limit'] == 20
    sheet = big_rep.sheets[0]
    si = sheet.column_names.index('SKU ID')
    assert o['result']['rows'] == [[r[si]] for r in sheet.rows[100:120]]


def test_next_page_with_new_limit(big_rep):
    """『下一页给我50条』-> 继承筛选/列，offset=上一页结束位置，limit=50。"""
    o1 = _start(big_rep, limit=20)
    o = _paginate(big_rep, _ctx_of(o1), nl.ACTION_NEXT, limit=50, message='下一页给我50条')
    assert o['result']['offset'] == 20 and o['result']['limit'] == 50
    sheet = big_rep.sheets[0]
    si = sheet.column_names.index('SKU ID')
    assert o['result']['rows'] == [[r[si]] for r in sheet.rows[20:70]]


def test_last_page_next_returns_empty_and_clear_message(big_rep):
    sheet = big_rep.sheets[0]
    total = sheet.row_count
    o1 = _start(big_rep, limit=20)
    ctx = _ctx_of(o1)
    ctx.offset = total - 5      # 模拟已到末尾附近
    o = _paginate(big_rep, ctx, nl.ACTION_NEXT, message='下一页')
    assert o['status'] == 'ok'
    assert o['result']['returned_count'] == 0
    assert '已经超出范围' in o['message']
    assert o['pagination']['has_next'] is False
    assert o['pagination']['has_prev'] is True


def test_offset_beyond_total_via_start(big_rep):
    o1 = _start(big_rep, limit=20)
    o = _paginate(big_rep, _ctx_of(o1), nl.ACTION_START, start_index=100000, limit=20)
    assert o['status'] == 'ok' and o['result']['returned_count'] == 0


# ==========================================================================
# 5. 边界：没有上下文 / 上下文被阻断
# ==========================================================================
def test_pagination_without_context_clarifies(big_rep):
    o = _turn(big_rep, '再来20条', ctx=None,
              override=nl.TurnIntent(action=nl.ACTION_NEXT, limit=20))
    assert o['status'] == 'clarify'
    assert '没有可继续的表格查询' in o['message']


def test_llm_clarify_on_pagination_phrase_without_context_is_explicit(big_rep):
    """LLM 只回了一句泛泛的 clarify，但用户说的确实是「再来20条」-> 必须给明确提示。"""
    o = _turn(big_rep, '再来20条', ctx=None,
              override=nl.TurnIntent(action=nl.ACTION_CLARIFY, clarification='信息不足'))
    assert o['status'] == 'clarify'
    assert '没有可继续的表格查询' in o['message']


def test_pagination_after_other_turn_clarifies(big_rep):
    """普通聊天之后再说『再来20条』：上下文被 mark_other_turn 阻断 -> 明确提示。"""
    store = ExcelQueryContextStore()
    store.save_excel(_ctx())          # 模拟上一轮 Excel 成功
    store.mark_other_turn(1, 's1')    # 模拟中间插入了普通聊天
    assert store.get_for_pagination(1, 's1') is None

    o = _turn(big_rep, '再来20条', ctx=None,
              override=nl.TurnIntent(action=nl.ACTION_NEXT, limit=20))
    assert o['status'] == 'clarify' and '没有可继续的表格查询' in o['message']


def test_new_query_then_next_uses_new_context(big_rep, small_rep):
    """重新查询另一张表后，『下一页』必须基于新查询。"""
    # 第一次：大表
    o1 = _start(big_rep, columns=['SKU'], limit=20)
    ctx_big = _ctx_of(o1)

    # 换成小表查询（模拟重新发起）
    o2 = _turn(small_rep, '列出直邮一店前5条SKU',
               override=nl.TurnIntent(action=nl.ACTION_NEW_QUERY,
                                      intent=nl.NlIntent(document='直邮一店', columns=['SKU'], limit=5)))
    assert o2['status'] == 'ok'
    ctx_small = _ctx_of(o2)
    assert ctx_small.document_id != ctx_big.document_id

    # 基于新上下文翻页（使用小表 rep 执行）
    o3 = _turn(small_rep, '下一页', ctx=ctx_small, override=nl.TurnIntent(action=nl.ACTION_NEXT))
    assert o3['document']['document_id'] == ctx_small.document_id
    assert o3['result']['offset'] == 5
    sheet = small_rep.sheets[0]
    si = sheet.column_names.index('SKU ID')
    assert o3['result']['rows'] == [[r[si]] for r in sheet.rows[5:10]]


# ==========================================================================
# 6. 小表 / 多列 / 带 filter 的继承
# ==========================================================================
def test_small_table_pagination(small_rep):
    o1 = _start(small_rep, doc='直邮一店', columns=['SKU ID'], limit=5)
    assert o1['result']['offset'] == 0 and o1['result']['returned_count'] == 5
    ctx = _ctx_of(o1)
    o2 = _paginate(small_rep, ctx, nl.ACTION_NEXT, limit=5, message='再来5条')
    assert o2['result']['offset'] == 5
    sheet = small_rep.sheets[0]
    si = sheet.column_names.index('SKU ID')
    assert o2['result']['rows'] == [[r[si]] for r in sheet.rows[5:10]]
    o3 = _paginate(small_rep, _ctx_of(o2), nl.ACTION_NEXT, limit=20, message='下一页')
    assert o3['result']['returned_count'] == sheet.row_count - 10


def test_multi_column_inherited_on_pagination(big_rep):
    o1 = _start(big_rep, columns=['Order ID', 'SKU ID'], limit=20)
    assert o1['query']['columns'] == ['Order ID', 'SKU ID']
    o2 = _paginate(big_rep, _ctx_of(o1), nl.ACTION_NEXT, message='再来20条')
    assert o2['query']['columns'] == ['Order ID', 'SKU ID']      # 继承多列
    sheet = big_rep.sheets[0]
    oi, si = sheet.column_names.index('Order ID'), sheet.column_names.index('SKU ID')
    assert o2['result']['rows'] == [[r[oi], r[si]] for r in sheet.rows[20:40]]


def test_filter_inherited_on_pagination(big_rep):
    """带 filter 的查询：翻页必须沿用同一 filter，只改 offset/limit。"""
    o1 = _start(big_rep, columns=['Order ID', 'Variation'],
                filters=[{'column': 'Variation', 'operator': 'contains', 'value': 'Black'}], limit=20)
    assert o1['status'] == 'ok'
    assert o1['result']['total_matches'] == 1
    ctx = _ctx_of(o1)
    assert ctx.filters and ctx.filters[0]['column'] == 'Variation'

    o2 = _paginate(big_rep, ctx, nl.ACTION_NEXT, message='下一页')
    assert o2['status'] == 'ok'
    assert o2['query']['filters'] == ctx.filters          # filter 被继承
    assert o2['result']['total_matches'] == 1
    assert o2['result']['returned_count'] == 0            # 只有 1 条命中，第 2 页为空
    assert '已经超出范围' in o2['message']


# ==========================================================================
# 7. LLM 不可用时的降级（严格分页短语）
# ==========================================================================
def test_llm_unavailable_uses_deterministic_pagination(big_rep):
    o1 = _start(big_rep, limit=20)
    ctx = _ctx_of(o1)
    o = _turn(big_rep, '再来20条', ctx=ctx, llm=None)
    assert o['status'] == 'ok' and o['continued'] is True
    assert o['result']['offset'] == 20 and o['result']['limit'] == 20
    sheet = big_rep.sheets[0]
    si = sheet.column_names.index('SKU ID')
    assert o['result']['rows'] == [[r[si]] for r in sheet.rows[20:40]]


def test_llm_unavailable_without_context_errors(big_rep):
    o = _turn(big_rep, '再来20条', ctx=None, llm=None)
    assert o['status'] == 'error'


def test_turn_intent_from_dict_tolerates_dirty_output():
    t = nl.turn_intent_from_dict({'action': 'NEXT', 'limit': '20'})
    assert t.action == nl.ACTION_NEXT and t.limit == 20
    t2 = nl.turn_intent_from_dict({'query_type': 'not_excel'})
    assert t2.action == nl.ACTION_NOT_EXCEL
    t3 = nl.turn_intent_from_dict({'action': '不存在的动作'})
    assert t3.action == nl.ACTION_NEW_QUERY
    t4 = nl.turn_intent_from_dict({'action': 'range', 'start_index': '51', 'end_index': 100})
    assert (t4.start_index, t4.end_index) == (51, 100)
