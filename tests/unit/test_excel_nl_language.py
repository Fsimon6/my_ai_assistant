# -*- coding: utf-8 -*-
"""后续 2A：自然语言解析稳定性（确定性优先层）测试矩阵。

分类覆盖（用户要求 §19）：
  文档 / Sheet / 列 / Query / COUNT / SUM / AVG / MIN / MAX / GROUP BY /
  ORDER BY / TOP-N / Calculation / Pagination / 安全注入 / 确定性快路径

约定：
- "确定性层" 的测试直接断言纯函数（不涉及 LLM）；
- "路由" 测试用**最坏情况 FakeLLM**（首轮一律返回某动作）证明 Python 兜底/降级的作用；
- 任何需要真实 LLM 的 20 次稳定性测试放在 HTTP E2E（见报告），不在此文件。
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from backend.excel import aggregate as agg
from backend.excel import engine as query_engine
from backend.excel import multi_step as ms
from backend.excel import nl_normalize as nn
from backend.excel import nl_query as nl
from backend.excel import query as q
from backend.excel import store as excel_store
from backend.excel.parser import ExcelParser

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SMALL = sorted(PROJECT_ROOT.glob('直邮一店*.xlsx'))
SMALL_REAL = _SMALL[0] if _SMALL else (PROJECT_ROOT / '直邮一店 8.20号订单.xlsx')

# 真实表（直邮一店 8.20号订单.xlsx）里的列名
AMOUNT = 'Order Amount'
QTY = 'Quantity'
SKU = 'SKU ID'


# ==========================================================================
# 1. 中文数字
# ==========================================================================
@pytest.mark.parametrize('text,expected', [
    ('五店', '5店'), ('五', '5'), ('十', '10'), ('十一', '11'), ('二十', '20'),
    ('二十一', '21'), ('一百', '100'), ('一百零一', '101'), ('一百二十三', '123'),
    ('两千', '2000'), ('两', '2'), ('一万', '10000'), ('二十万', '200000'),
    ('前二十条', '前20条'), ('前十', '前10'), ('前一百个', '前100个'),
    ('第十一条', '第11条'), ('第21到30条', '第21到30条'),
])
def test_chinese_number_to_arabic(text, expected):
    assert nn.cn_to_arabic(text) == expected


def test_chinese_number_does_not_break_words():
    """单字「一」出现在词语里时不会被误转换（只影响数值场景的调用方）。"""
    # 整句转换会替换「一」，因此数值解析必须走 extract_number / parse_pagination
    assert nn.extract_number('二十') == 20
    assert nn.extract_number('五') == 5
    assert nn.extract_number('第51') == 51
    assert nn.parse_pagination('下一页') is not None          # 不被「一」误伤
    assert nn.parse_pagination('上一页') is not None
    assert nn.parse_pagination('再来二十条').limit == 20
    assert nn.parse_pagination('前二十条') is None            # 不是纯分页指令


def test_extract_number_bounds():
    assert nn.extract_number('99999999') == 99999999
    assert nn.extract_number('一亿') is None                 # 超出支持范围 -> 不猜


# ==========================================================================
# 2. 文档名解析
# ==========================================================================
CATALOG = [
    {'document_id': 'a', 'filename': '直邮一店 8.20号订单.xlsx', 'created_at': '2026-09-01'},
    {'document_id': 'b', 'filename': '直邮5店 7.10号订单.xlsx', 'created_at': '2026-09-02'},
]


@pytest.mark.parametrize('hint', [
    '5店', '五店', '直邮5店', '直邮五店', '五店订单', '5店订单', '直邮五店订单',
    '直邮5店 7.10号订单', '直邮5店 7.10号订单.xlsx', '７店' if False else '7.10',
])
def test_document_hint_variants_map_to_same_file(hint):
    m = nn.resolve_document_deterministic(CATALOG, hint)
    assert m.doc is not None, f'{hint} 未命中'
    assert m.doc['filename'] == '直邮5店 7.10号订单.xlsx'


@pytest.mark.parametrize('hint', ['一店', '直邮一店', '一店订单'])
def test_document_hint_other_file(hint):
    m = nn.resolve_document_deterministic(CATALOG, hint)
    assert m.doc is not None and m.doc['filename'] == '直邮一店 8.20号订单.xlsx'


def test_document_fullwidth_and_spaces():
    m = nn.resolve_document_deterministic(CATALOG, ' 直邮５店　７.１０号订单 ')
    assert m.doc is not None and m.doc['document_id'] == 'b'


def test_similar_documents_must_clarify():
    """三份「直邮5店 X.10」文件，只说「5店」必须澄清，不许随机挑。"""
    catalog = [
        {'document_id': 'a', 'filename': '直邮5店 7.10号订单.xlsx', 'created_at': '1'},
        {'document_id': 'b', 'filename': '直邮5店 8.10号订单.xlsx', 'created_at': '2'},
        {'document_id': 'c', 'filename': '直邮5店 9.10号订单.xlsx', 'created_at': '3'},
    ]
    m = nn.resolve_document_deterministic(catalog, '5店')
    assert m.doc is None
    assert len(m.candidates) == 3
    # 给出完整名字才能唯一命中
    m2 = nn.resolve_document_deterministic(catalog, '直邮5店 8.10号订单')
    assert m2.doc is not None and m2.doc['document_id'] == 'b'
    m3 = nn.resolve_document_deterministic(catalog, '8.10')
    assert m3.doc is not None and m3.doc['document_id'] == 'b'


def test_single_catalog_ignores_hint():
    """只有一份文件时，口语别名（甚至没说文件）都能落到它。"""
    only = [CATALOG[0]]
    for hint in (None, '那个表', '一店', '随便哪个'):
        m = nn.resolve_document_deterministic(only, hint)
        assert m.doc is not None


def test_unknown_document_returns_candidates():
    m = nn.resolve_document_deterministic(CATALOG, '利润表2026')
    assert m.doc is None and len(m.candidates) == 2


def test_dedupe_same_filename_keeps_latest():
    cat = [
        {'document_id': 'old', 'filename': 'x.xlsx', 'created_at': '2026-09-01'},
        {'document_id': 'new', 'filename': 'x.xlsx', 'created_at': '2026-09-05'},
    ]
    kept = nl.dedupe_catalog_by_filename(cat)
    assert len(kept) == 1 and kept[0]['document_id'] == 'new'


# ==========================================================================
# 3. Sheet 解析
# ==========================================================================
def test_sheet_exact_beats_fuzzy():
    name, cands = nn.resolve_sheet_deterministic(
        ['OrderSKUList', 'OrderSKUListBackup'], 'OrderSKUList')
    assert name == 'OrderSKUList' and cands == []


def test_sheet_case_insensitive():
    for hint in ('orderskulist', 'ORDERSKULIST', 'OrderSkuList'):
        name, _ = nn.resolve_sheet_deterministic(['OrderSKUList', 'Other'], hint)
        assert name == 'OrderSKUList'


def test_sheet_ambiguous_substring_clarifies():
    name, cands = nn.resolve_sheet_deterministic(
        ['OrderSKUList', 'OrderSKUListBackup'], 'SKU')
    assert name is None and set(cands) == {'OrderSKUList', 'OrderSKUListBackup'}


def test_sheet_cn_alias_when_exists():
    name, _ = nn.resolve_sheet_deterministic(['订单SKU', 'Sheet3'], '订单SKU')
    assert name == '订单SKU'


# ==========================================================================
# 4. 列解析（真实 schema 决定一切）
# ==========================================================================
REAL_COLUMNS = ['Order ID', 'Order Status', 'SKU ID', 'Seller SKU', 'Product Name',
                'Variation', 'Quantity', 'Sku Quantity of return',
                'SKU Unit Original Price', 'SKU Subtotal After Discount',
                'Order Amount', 'Order Refund Amount', 'Shipping Provider Name',
                'Payment Method', 'Recipient', 'City', 'State', 'Country',
                'Weight(kg)', 'Product Category', 'Warehouse Name', 'Package ID',
                'Tracking ID', 'Taxes']


@pytest.mark.parametrize('hint,expected', [
    ('SKU ID', 'SKU ID'), ('sku id', 'SKU ID'), ('SKU', 'SKU ID'), ('sku', 'SKU ID'),
    ('SKU编号', 'SKU ID'), ('SKU号', 'SKU ID'),
    ('订单号', 'Order ID'), ('订单编号', 'Order ID'), ('Order ID', 'Order ID'),
    ('物流商', 'Shipping Provider Name'), ('物流公司', 'Shipping Provider Name'),
    ('快递', 'Shipping Provider Name'), ('Shipping Provider Name', 'Shipping Provider Name'),
    ('订单金额', 'Order Amount'), ('金额', 'Order Amount'), ('总金额', 'Order Amount'),
    ('Order Amount', 'Order Amount'),
    ('数量', 'Quantity'), ('件数', 'Quantity'), ('Quantity', 'Quantity'),
    ('状态', 'Order Status'), ('订单状态', 'Order Status'),
    ('支付方式', 'Payment Method'), ('付款方式', 'Payment Method'),
    ('退货数量', 'Sku Quantity of return'),
    ('收件人', 'Recipient'), ('城市', 'City'),
])
def test_column_alias_matrix_on_real_schema(hint, expected):
    col, cands = nl.resolve_column_nl(_FakeSheet(REAL_COLUMNS), hint)
    assert col == expected, f'{hint} -> {col} {cands}'


def test_column_alias_only_when_target_exists():
    """同义词指向的列不存在时，必须澄清而不是猜。"""
    col, cands = nl.resolve_column_nl(_FakeSheet(['A', 'B']), '订单金额')
    assert col is None and set(cands) == {'A', 'B'}


def test_column_alias_wins_when_target_exists():
    """「SKU」在电商语境下指 SKU ID（同义词表命中且目标唯一存在）。"""
    col, _ = nl.resolve_column_nl(_FakeSheet(['SKU ID', 'SKU Subtotal After Discount']), 'SKU')
    assert col == 'SKU ID'


def test_column_ambiguous_clarifies():
    col, cands = nl.resolve_column_nl(_FakeSheet(['Quantity', 'Quantity Returned']), 'Quan')
    assert col is None and set(cands) == {'Quantity', 'Quantity Returned'}


def test_column_unknown_clarifies():
    col, cands = nl.resolve_column_nl(_FakeSheet(REAL_COLUMNS), '不存在的列')
    assert col is None and cands


class _FakeSheet:
    """只需要 column_names 的最小替身（resolve_column_nl 的实际依赖）。"""

    def __init__(self, names: List[str]):
        self.column_names = list(names)


# ==========================================================================
# 5. 意图信号：Query / COUNT / SUM / AVG / MIN / MAX / GROUP / ORDER / TOP-N
# ==========================================================================
def test_query_vs_count_signals():
    assert nn.looks_statistical('数量大于1的订单有哪些？') is False
    assert nn.looks_statistical('列出所有订单') is False
    assert nn.looks_statistical('显示前20条订单') is False
    assert nn.looks_statistical('有哪些订单金额超过10？') is False
    assert nn.looks_statistical('有哪些物流商？') is False

    assert nn.looks_statistical('数量大于1的订单有几条？') is True
    assert nn.looks_statistical('有多少单？') is True
    assert nn.looks_statistical('订单数量是多少？') is True

    s = nn.detect_signals('数量大于1的订单有几条？')
    assert s.count and not s.sum_ and not s.avg
    s2 = nn.detect_signals('一店每笔订单的金额除以数量是多少？')
    assert nn.is_row_calc_only('一店每笔订单的金额除以数量是多少？') is True
    assert nn.looks_statistical('一店按SKU统计每笔订单的单价平均值') is True
    assert nn.is_row_calc_only('一店按SKU统计每笔订单的单价平均值') is False


@pytest.mark.parametrize('text', [
    '订单金额总和是多少？', '订单金额合计是多少？', '订单金额总额是多少？',
    '把订单金额加起来是多少？', '订单金额总计多少', '一共多少钱？', '总销售额是多少？',
])
def test_sum_phrasings(text):
    s = nn.detect_signals(text)
    assert s.sum_ and nn.looks_statistical(text)


@pytest.mark.parametrize('text', [
    '平均订单金额是多少？', '订单金额平均值是多少？', '订单金额平均下来多少？', '均值是多少？',
])
def test_avg_phrasings(text):
    assert nn.detect_signals(text).avg


@pytest.mark.parametrize('text', ['订单金额最小是多少？', '订单金额最低是多少？', '最低金额是多少？'])
def test_min_phrasings(text):
    assert nn.detect_signals(text).min_


@pytest.mark.parametrize('text', ['订单金额最大是多少？', '订单金额最高是多少？', '最高金额是多少？'])
def test_max_phrasings(text):
    assert nn.detect_signals(text).max_


def test_max_vs_topn():
    """单值 MAX 与 TOP-N 必须区分。"""
    single = nn.detect_signals('订单金额最高是多少？')
    assert single.max_ and single.top_n is None

    topn_group = nn.detect_signals('订单金额最高的5个物流商')
    assert topn_group.top_n == 5 and topn_group.topn_unit == 'group'

    topn_rows = nn.detect_signals('金额最高的10个订单')
    assert topn_rows.top_n == 10

    cn_topn = nn.detect_signals('找出金额最高的十个SKU，并统计它们的总销售额。')
    assert cn_topn.top_n == 10 and cn_topn.multi_step is True


@pytest.mark.parametrize('text', [
    '每个物流商分别有多少单？', '各物流商分别有多少单？', '按物流商统计订单数量',
    '按物流商分别统计', '每种支付方式有多少单？', '按照物流商分组',
])
def test_group_by_phrasings(text):
    assert nn.detect_signals(text).group, text


def test_group_by_not_triggered_by_single_value_filter():
    s = nn.detect_signals('物流商为SF的有几单？')
    assert s.count and not s.group


@pytest.mark.parametrize('text,expected', [
    ('按订单数量从高到低排列', 'desc'), ('由大到小排列', 'desc'), ('降序排列', 'desc'),
    ('从多到少排列各物流商', 'desc'), ('按订单数量从低到高排列', 'asc'),
    ('由小到大排列', 'asc'), ('升序排列', 'asc'),
])
def test_order_dir_phrasings(text, expected):
    assert nn.detect_signals(text).order_dir == expected


@pytest.mark.parametrize('text,n,unit', [
    # 量词决定语义：'group'=分组统计的 top_n，'row'=普通查询的 limit，None=未写量词（交由上下文判定）
    ('前5个', 5, 'group'), ('前五个', 5, 'group'), ('排名前5', 5, None),
    ('TOP5', 5, None), ('Top 5', 5, None), ('最高的5个', 5, 'group'),
    ('最多的10个', 10, 'group'), ('前十名', 10, 'group'),
    ('前20条', 20, 'row'), ('前五十条', 50, 'row'),
])
def test_topn_phrasings(text, n, unit):
    s = nn.detect_signals(f'订单金额{text}物流商')
    assert s.top_n == n, text
    assert s.topn_unit == unit


def test_topn_out_of_range_kept_for_validation():
    """超界 N 在这里只做识别，真正拒绝由 aggregate.normalize_top_n 负责。"""
    assert nn.detect_signals('前1000个物流商').top_n == 1000
    with pytest.raises(q.ExcelQueryError):
        agg.normalize_top_n(1000)
    assert agg.normalize_top_n(200) == 200


@pytest.mark.parametrize('text,op', [
    ('金额除以数量', 'div'), ('金额/数量', 'div'), ('金额 ÷ 数量', 'div'), ('金额相除', 'div'),
    ('金额乘以数量', 'mul'), ('金额×数量', 'mul'), ('金额相乘', 'mul'),
    ('金额加上运费', 'add'), ('金额减去退款', 'sub'),
])
def test_calculation_phrasings(text, op):
    assert op in nn.detect_signals(text).calc_ops, text


# ==========================================================================
# 6. 确定性快路径（分页）
# ==========================================================================
@pytest.mark.parametrize('text,action,limit', [
    ('下一页', 'next', None), ('上一页', 'prev', None),
    ('再来20条', 'next', 20), ('再来二十条', 'next', 20),
    ('继续看10条', 'next', 10), ('往下看20条', 'next', 20), ('往前看10条', 'prev', 10),
])
def test_pagination_fastpath_phrases(text, action, limit):
    page = nn.parse_pagination(text)
    assert page is not None and page.action == action and page.limit == limit


@pytest.mark.parametrize('text,a,b', [
    ('第51到100条', 51, 100), ('给我51-100条', 51, 100),
    ('查看第101到150条', 101, 150), ('看第21到30条', 21, 30),
])
def test_pagination_range_phrases(text, a, b):
    page = nn.parse_pagination(text)
    assert page is not None and (page.start_index, page.end_index) == (a, b)


@pytest.mark.parametrize('text,a,n', [('从第101条开始给我20条', 101, 20), ('从101开始看20条', 101, 20)])
def test_pagination_start_phrases(text, a, n):
    page = nn.parse_pagination(text)
    assert page is not None and page.start_index == a and page.limit == n


@pytest.mark.parametrize('text', [
    '再来10条SKU', '列出一店前10条SKU', '把这20条SKU列出来', '订单金额最高的5个物流商',
    '有哪些订单', '5店前20条',
])
def test_pagination_fastpath_must_not_hijack(text):
    """带新条件的问句绝不能走分页快路径。"""
    assert nn.parse_pagination(text) is None


def test_topn_followup_phrases():
    assert nn.parse_topn_followup('再看前10个') == 10
    assert nn.parse_topn_followup('只看前五个') == 5
    assert nn.parse_topn_followup('换成前20名') == 20
    assert nn.parse_topn_followup('再算一下这10个的平均') is None
    assert nn.parse_topn_followup('前1000个') is None


# ==========================================================================
# 7. 路由：Python 兜底 / 降级 / 不劫持（用最坏情况 FakeLLM）
# ==========================================================================
@pytest.fixture(scope='module')
def small_rep():
    if not SMALL_REAL.exists():
        pytest.skip(f'缺少测试文件：{SMALL_REAL}')
    rep = ExcelParser().parse(file_path=str(SMALL_REAL), document_id='nl_lang',
                              user_id=1, filename=SMALL_REAL.name, file_type='xlsx')
    for s in rep.sheets:
        s.ensure_semantic_types()
    return rep


class ScriptedLLM:
    """按 system prompt 分派、可记录调用的假 LLM。"""

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
    return [{'document_id': rep.document_id, 'filename': rep.filename,
             'file_type': rep.file_type, 'created_at': rep.created_at,
             'total_rows': rep.total_rows,
             'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                         'row_count': s.row_count, 'column_count': s.column_count,
                         'columns': s.column_names} for s in rep.sheets]}]


def _run(rep, message, llm, **kw):
    original = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nl.run_nl_query(message, _catalog(rep), llm=llm, **kw))
    finally:
        excel_store.load_representation = original


def test_plain_list_is_not_hijacked_by_statistical_guard(small_rep):
    """最坏情况：LLM 把「有哪些物流商？」判成 new_query（本就正确）→ 不得被统计兜底改写。"""
    llm = ScriptedLLM(turn={'action': 'new_query', 'document': '一店',
                            'columns': ['Shipping Provider Name'], 'limit': 20},
                      stat={'aggregate_operation': 'count', 'column': None, 'group_by': []})
    out = _run(small_rep, '有哪些物流商？', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_structured_query'
    assert llm.calls == ['turn']            # 一次 LLM 调用，未走统计兜底


def test_plain_list_downgrade_when_llm_says_aggregate(small_rep):
    """LLM 把纯列表问题误判为 aggregate → 确定性降级回普通查询。"""
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'count',
                            'column': None, 'group_by': ['Shipping Provider Name'],
                            'document': '一店'})
    out = _run(small_rep, '有哪些物流商？', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_structured_query'
    assert out['turn']['source'] == 'deterministic_downgrade'


def test_clear_count_question_rescued_from_clarify(small_rep):
    """LLM 判成 clarify 的明确计数问题 → 统计兜底救回（§9 的核心诉求）。"""
    llm = ScriptedLLM(turn={'action': 'clarify', 'clarification': '我不确定你想统计什么'},
                      stat={'aggregate_operation': 'count', 'column': None, 'group_by': [],
                            'filters': [{'column': 'Quantity', 'operator': 'gt', 'value': 1}],
                            'document': '一店'})
    out = _run(small_rep, '数量大于1的订单有几条？', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_aggregate'
    assert out['aggregate']['operation'] == 'count'
    assert out['aggregate']['value'] == 1.0         # 一店：Quantity > 1 的只有 1 行（其余 18 行为 1）


def test_clear_query_not_rescued_from_clarify(small_rep):
    """纯列表问题被判成 clarify 时**不得**被统计兜底改写成统计。"""
    llm = ScriptedLLM(turn={'action': 'clarify', 'clarification': '请补充条件'},
                      stat={'aggregate_operation': 'count', 'column': None, 'group_by': []})
    out = _run(small_rep, '数量大于1的订单有哪些？', llm)
    assert out['status'] == 'clarify'


def test_fast_path_pagination_skips_llm(small_rep, tmp_path):
    """有上下文时，「再来20条」必须走 Python 快路径（**零 LLM 调用**）。"""
    llm = ScriptedLLM(turn={'action': 'new_query', 'document': '一店', 'columns': []})
    first = _run(small_rep, '列出一店前5条SKU', llm)
    assert first['status'] == 'ok'
    ctx = nl.ExcelQueryContext(**first['new_context'])
    llm.calls.clear()
    out = _run(small_rep, '再来二十条', llm, context=ctx)
    assert llm.calls == []                     # 没有调用 LLM
    assert out.get('fast_path') is True
    assert out['pagination']['mode'] == nl.ACTION_NEXT
    assert out['pagination']['limit'] == 20


def test_fast_path_not_used_without_context(small_rep):
    llm = ScriptedLLM(turn={'action': 'clarify', 'clarification': '没有上一轮'})
    out = _run(small_rep, '下一页', llm)
    assert out['status'] == 'clarify'
    assert llm.calls == ['turn']               # 无上下文时不走快路径


def test_turn_repair_fills_order_dir_and_top_n(small_rep):
    """LLM 漏填 order_dir/top_n 时，用文本里的确定性信号补齐（只填空）。"""
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'count',
                            'column': None, 'group_by': ['Shipping Provider Name'],
                            'document': '一店'})
    out = _run(small_rep, '按订单数量从高到低排列各物流商，只看前5个', llm)
    assert out['status'] == 'ok'
    g = out['group_aggregate']
    assert g['top_n'] == 5 and g['order_dir'] == 'desc'
    assert g['returned_groups'] == 3            # 一店只有 3 个物流商


def test_order_count_word_is_repaired_to_count(small_rep):
    """「按订单数量排行」被 LLM 当成 SUM(Quantity) 时 → 纠正为 COUNT（订单条数）。"""
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'sum',
                            'column': 'Quantity', 'group_by': ['Shipping Provider Name'],
                            'order_by': 'aggregate_value', 'order_dir': 'desc',
                            'document': '一店'})
    out = _run(small_rep, '一店按订单数量从高到低排列各物流商。', llm)
    assert out['status'] == 'ok'
    g = out['group_aggregate']
    assert g['operation'] == 'count' and g['column'] is None
    assert [r['value'] for r in g['rows']] == [12.0, 4.0, 3.0]


def test_order_count_word_keeps_sum_when_user_says_sum(small_rep):
    """「订单数量的总和」仍是 SUM(Quantity)，不得被纠正为 count。"""
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'sum',
                            'column': 'Quantity', 'document': '一店'})
    out = _run(small_rep, '一店订单数量的总和是多少？', llm)
    assert out['status'] == 'ok'
    assert out['aggregate']['operation'] == 'sum'
    assert out['aggregate']['value'] == 20.0


def test_calculation_guard_fills_missing_calculation(small_rep):
    """LLM 漏掉 calculation 的逐行计算问法 → 用抽取器补上（只取 calculation）。"""
    llm = ScriptedLLM(turn={'action': 'new_query', 'document': '一店', 'columns': [],
                            'limit': 20},
                      stat={'aggregate_operation': 'avg',
                            'calculation': {'operation': 'div', 'left_column': 'Order Amount',
                                            'right_column': 'Quantity'},
                            'document': '一店'})
    out = _run(small_rep, '一店每笔订单的金额/数量', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_structured_query'
    assert out['result']['calculation']['operation'] == 'div'
    assert out['result']['total_matches'] == 19


def test_calculation_guard_not_used_for_plain_list(small_rep):
    """普通列表问题（没有逐行计算提示）不得触发计算兜底。"""
    llm = ScriptedLLM(turn={'action': 'new_query', 'document': '一店', 'columns': [], 'limit': 20})
    out = _run(small_rep, '列出一店前10条SKU', llm)
    assert out['status'] == 'ok'
    assert llm.calls == ['turn']


def test_turn_repair_does_not_override_llm(small_rep):
    """LLM 已给出 asc 时不得被文本里的「最高」覆盖（只填空）。"""
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'count',
                            'column': None, 'group_by': ['Shipping Provider Name'],
                            'order_by': 'aggregate_value', 'order_dir': 'asc',
                            'document': '一店'})
    out = _run(small_rep, '哪个物流商订单最多？', llm)
    assert out['status'] == 'ok'
    assert out['group_aggregate']['order_dir'] == 'asc'


def test_multi_step_guard_handles_chinese_numerals(small_rep):
    """「最高的十个SKU…并统计总销售额」必须进入多步分析（中文数字）。"""
    plan = {'document': '一店', 'steps': [
        {'type': 'group_aggregate', 'group_by': ['SKU ID'], 'operation': 'sum',
         'column': 'Order Amount', 'order_by': 'aggregate_value', 'order_dir': 'desc',
         'top_n': 10},
        {'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
         'column': 'aggregate_value'}]}
    llm = ScriptedLLM(turn={'action': 'new_query', 'document': '一店', 'columns': []},
                      analysis=plan)
    out = _run(small_rep, '找出金额最高的十个SKU，并统计它们的总销售额。', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_analysis'
    assert out['multi_step']['value'] == pytest.approx(267.61, rel=1e-9)


# ==========================================================================
# 8. 安全：注入一律被 schema 校验拒绝（自然语言层不得放行）
# ==========================================================================
INJECTION_CASES: List[Any] = [
    ('column', '订单金额总和是多少？',
     {'action': 'aggregate', 'aggregate_operation': 'sum',
      'column': 'Order Amount; DROP TABLE t', 'document': '一店'}),
    ('document', '有哪些订单？',
     {'action': 'new_query', 'document': "一店' OR 1=1 --", 'columns': []}),
    ('sheet', '有哪些订单？',
     {'action': 'new_query', 'document': '一店', 'sheet': 'OrderSKUList; DROP TABLE t'}),
    ('filter_operator', '数量大于1的订单有哪些？',
     {'action': 'new_query', 'document': '一店',
      'filters': [{'column': 'Quantity', 'operator': 'gt; DROP TABLE t', 'value': 1}]}),
    ('group_by', '每个物流商分别有多少单？',
     {'action': 'aggregate', 'aggregate_operation': 'count', 'column': None,
      'group_by': ['Shipping Provider Name); DROP TABLE t --'], 'document': '一店'}),
]


@pytest.mark.parametrize('kind,message,payload', INJECTION_CASES,
                         ids=[c[0] for c in INJECTION_CASES])
def test_injection_is_rejected_not_executed(small_rep, kind, message, payload):
    """注入串绝不能被当作 SQL/列名/分组列执行；必须澄清或报错。"""
    llm = ScriptedLLM(turn=payload)
    out = _run(small_rep, message, llm)
    assert out['status'] in ('clarify', 'error'), f'{kind} 注入未被拒绝：{out["status"]}'
    # 结果里不允许出现任何已执行的成功产物
    assert not out.get('result') and not out.get('aggregate') and not out.get('group_aggregate')


def test_top_n_over_limit_is_clarified(small_rep):
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'count',
                            'column': None, 'group_by': ['Shipping Provider Name'],
                            'order_by': 'aggregate_value', 'order_dir': 'desc',
                            'top_n': 1000, 'document': '一店'})
    out = _run(small_rep, '每个物流商前1000个', llm)
    assert out['status'] == 'clarify'
    assert '1~200' in out['message'] or '200' in out['message']


# ==========================================================================
# 8. 后续 2A 补丁：把总量"直接问出来"的两步语义（真实 LLM 不一致项）
# ==========================================================================
@pytest.mark.parametrize('text,expected', [
    # 无连接词，但语义仍是对前 N 名再汇总一次（真实 LLM 曾把它判成单步分组统计）
    ('一店金额最高的10个SKU的销售额总和是多少？', True),
    ('一店中金额最高的10个SKU的销售额总和是多少', True),
    ('找出金额最高的5个物流商的订单数量合计。', True),
    ('一店金额最高的5个SKU的销售额平均值是多少？', True),
    # 单步：没有 TOP-N / "总和"只是排名口径 / 明确要求"每组一个值"
    ('一店订单金额总和是多少？', False),
    ('一店订单金额总和最高的5个物流商', False),
    ('金额最高的10个SKU分别的总销售额是多少？', False),
    ('每个物流商前1000个', False),
    ('订单金额最高的5个物流商', False),
    ('一店每个SKU的销售额平均值是多少？', False),
])
def test_multi_step_total_asked_phrases(text, expected):
    assert nn.looks_multi_step(text) is expected


CALC_COLS = ['Order Amount', 'Quantity', 'SKU ID', 'Shipping Provider Name']
CALC_ALIASES = {'金额': 'Order Amount', '订单金额': 'Order Amount', '数量': 'Quantity'}
_AMOUNT = 'Order Amount'
_QTY = 'Quantity'


@pytest.mark.parametrize('text,expected', [
    ('一店每笔订单的金额/数量', ('div', _AMOUNT, _QTY)),
    ('一店每笔订单的金额除以数量是多少？', ('div', _AMOUNT, _QTY)),
    ('每笔订单的订单金额÷数量是多少？', ('div', _AMOUNT, _QTY)),
    ('每笔订单金额 乘以 数量', ('mul', _AMOUNT, _QTY)),
])
def test_parse_calculation_operands_positive(text, expected):
    assert nn.parse_calculation_operands(text, CALC_COLS, CALC_ALIASES) == expected


@pytest.mark.parametrize('text', [
    '一店每笔订单的金额',            # 没有运算符
    '一店每笔订单的金额/数量/单价',   # 多个运算符（嵌套）-> 不猜
    '每笔订单的 SF/JS 物流商',        # 两侧都不是真实列
    '每笔订单的金额/未知列',          # 右侧不是真实列
    '每笔订单的金额/金额',            # A/A 无意义
])
def test_parse_calculation_operands_rejects_uncertain(text):
    assert nn.parse_calculation_operands(text, CALC_COLS, CALC_ALIASES) is None


def test_total_asked_aggregate_is_upgraded_to_analysis_without_llm(small_rep):
    """LLM 只给「分组 + TOP-N」单步统计，但用户问的是这 N 行的总量 → 确定性升级（零额外 LLM）。

    真实故障：同一句话有时走两步得到 267.61，有时只返回 10 行明细。
    """
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'sum',
                            'column': AMOUNT, 'group_by': [SKU],
                            'order_by': 'aggregate_value', 'order_dir': 'desc',
                            'top_n': 10, 'document': '一店'})
    out = _run(small_rep, '一店金额最高的10个SKU的销售额总和是多少？', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_analysis'
    assert out.get('analysis_upgraded') is True
    assert llm.calls == ['turn']                       # 没有第二次 LLM 调用
    m = out['multi_step']
    assert m['step2']['operation'] == 'sum'
    assert len(m['step1']['rows']) == 10
    assert m['value'] == pytest.approx(267.61, abs=0.01)   # 独立 GT（见 Phase 4A 报告）


def test_step2_operation_follows_user_wording(small_rep):
    """第 2 步口径由用户措辞决定：说「平均值」就是 avg（不是默认的 sum）。"""
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'sum',
                            'column': AMOUNT, 'group_by': [SKU],
                            'order_by': 'aggregate_value', 'order_dir': 'desc',
                            'top_n': 5, 'document': '一店'})
    out = _run(small_rep, '一店金额最高的5个SKU的销售额平均值是多少？', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_analysis'
    assert llm.calls == ['turn']
    m = out['multi_step']
    assert m['step2']['operation'] == 'avg'
    assert len(m['step1']['rows']) == 5


def test_deterministic_calculation_repair_skips_llm(small_rep):
    """「一店每笔订单的金额/数量」用确定性规则补出计算字段（零额外 LLM 调用）。"""
    llm = ScriptedLLM(turn={'action': 'new_query', 'document': '一店',
                            'columns': [AMOUNT, QTY]})
    out = _run(small_rep, '一店每笔订单的金额/数量', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_structured_query'
    calc = out['result']['calculation']
    assert calc['operation'] == 'div'
    assert calc['left_column'] == AMOUNT and calc['right_column'] == QTY
    assert out['result']['total_matches'] == 19
    assert llm.calls == ['turn']


def test_calculation_falls_back_to_llm_when_rules_uncertain(small_rep):
    """规则切不出左右列时仍走原来的 LLM 兜底（不劣化）。"""
    llm = ScriptedLLM(
        turn={'action': 'new_query', 'document': '一店', 'columns': [AMOUNT, QTY]},
        stat={'aggregate_operation': 'sum', 'column': AMOUNT, 'group_by': [],
              'document': '一店',
              'calculation': {'operation': 'div', 'left_column': AMOUNT,
                              'right_column': QTY}})
    out = _run(small_rep, '一店每笔订单的金额/数量/单价', llm)
    assert out['status'] == 'ok'
    assert llm.calls == ['turn', 'stat_guard']
    assert out['result']['calculation']['operation'] == 'div'


# ==========================================================================
# 9. 后续 2A：provider 侧 LLM 错误 → 稳定 error_code + 中文文案（不外泄原始报文）
# ==========================================================================
#: 真实报文（DashScope 免费额度耗尽）；浏览器上曾原样显示给用户
FREE_QUOTA = ("Error code: 400 - {'error': {'message': 'Free quota exhausted. To continue "
              "accessing the model on a paid basis, please add funds or disable the "
              "\"use free tier only\" mode in the management console.', "
              "'type': 'AllocationQuota.FreeTierOnly', 'code': 'AllocationQuota.FreeTierOnly'}}")


class FailingLLM:
    """首轮 LLM 调用就抛 provider 错误（模拟欠费/限流/鉴权失败）。"""

    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    async def chat_completion(self, messages, stream=False, temperature=None):
        self.calls += 1
        raise self.exc
        yield  # pragma: no cover - 让函数成为异步生成器（与真实 LLM 接口一致）


@pytest.mark.parametrize('exc_text,expected_code', [
    (FREE_QUOTA, 'LLM_QUOTA_EXCEEDED'),
    ("Error code: 429 - rate limit reached", 'LLM_RATE_LIMITED'),
    ("Error code: 401 - Incorrect API key provided: sk-fake-test-key-0000000000000000000000",
     'LLM_AUTH_FAILED'),
])
def test_provider_error_is_classified_and_sanitized(small_rep, exc_text, expected_code):
    """LLM 侧 4xx/欠费：返回 error_code + 可读中文；**绝不**回传 provider 原始报文/密钥。"""
    out = _run(small_rep, '一店订单金额总和是多少？', FailingLLM(RuntimeError(exc_text)))
    assert out['status'] == 'error'
    assert out['error_code'] == expected_code
    body = json.dumps(out, ensure_ascii=False, default=str)
    for leak in ('Free quota', 'AllocationQuota', 'chatcmpl', 'sk-fake-test-', 'Error code'):
        assert leak not in body, f'{leak} 泄漏到响应体：{body[:300]}'
    assert '服务' in out['message']


def test_business_error_keeps_original_message(small_rep):
    """业务类错误（不是 provider 问题）保持原样，不被误分类。"""
    out = _run(small_rep, '有哪些物流商？', ScriptedLLM(turn={'action': 'clarify',
                                                            'clarification': '请补充条件'}))
    assert out['status'] == 'clarify'
    assert not out.get('error_code')


# ==========================================================================
# 10. 后续 2A：LLM 漏参数时的确定性补全（浏览器验收暴露的两个真实缺陷）
# ==========================================================================
_CARRIER = 'Shipping Provider Name'
_STATUS = 'Order Status'
_FILTER_VALUES = {
    _CARRIER: ['Yanwen Express', 'SF International', 'JS Express International'],
    _STATUS: ['Cancelled', 'Shipped', 'Delivered'],
}


def _values_of(name):
    return _FILTER_VALUES.get(name, [])


@pytest.mark.parametrize('text,expected', [
    # 子串命中 -> contains（真实值是 'SF International'）
    ('物流商为SF的订单有哪些？', {'column': _CARRIER, 'operator': 'contains', 'value': 'SF'}),
    ('物流商是SF的有几单？', {'column': _CARRIER, 'operator': 'contains', 'value': 'SF'}),
    # 完全相等 -> eq
    ('订单状态为Cancelled的订单', {'column': _STATUS, 'operator': 'eq', 'value': 'Cancelled'}),
    ('订单状态等于Delivered的记录', {'column': _STATUS, 'operator': 'eq', 'value': 'Delivered'}),
])
def test_parse_simple_filter_positive(text, expected):
    assert nn.parse_simple_filter(text, list(_FILTER_VALUES), nl.COLUMN_ALIASES,
                                 _values_of) == expected


@pytest.mark.parametrize('text', [
    '物流商为ZZ的订单有哪些？',       # 值在真实数据里不存在 -> 不造条件
    '未知列为SF的订单',               # 列不存在
    '一店订单金额总和是多少？',        # 没有「为/是/等于」
    '一店订单金额平均是多少？',        # 「是」后面是「多少」，不是真实值
])
def test_parse_simple_filter_rejects_uncertain(text):
    assert nn.parse_simple_filter(text, list(_FILTER_VALUES), nl.COLUMN_ALIASES,
                                  _values_of) is None


def test_missing_filter_is_repaired(small_rep):
    """「物流商为SF的订单有哪些？」LLM 漏了筛选 -> 确定性补回，返回 4 行而不是全表 19 行。"""
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'count',
                            'column': None, 'group_by': [], 'filters': [],
                            'document': small_rep.filename, 'sheet': small_rep.sheets[0].sheet_name})
    out = _run(small_rep, '物流商为SF的订单有哪些？', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_structured_query'
    assert out['result']['total_matches'] == 4                      # GT：SF 共 4 单
    applied = out['result']['applied_filters']
    assert len(applied) == 1 and applied[0]['column'] == _CARRIER
    assert applied[0]['operator'] == 'contains' and applied[0]['value'] == 'SF'


def test_missing_aggregate_column_is_repaired(small_rep):
    """「把订单金额加起来是多少？」LLM 给 sum 但漏了目标列 -> 确定性补回 Order Amount。"""
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'sum',
                            'column': None, 'group_by': [], 'filters': [],
                            'document': small_rep.filename, 'sheet': small_rep.sheets[0].sheet_name})
    out = _run(small_rep, '把订单金额加起来是多少？', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_aggregate'
    assert out['aggregate']['operation'] == 'sum'
    assert out['aggregate']['column'] == AMOUNT
    assert out['aggregate']['value'] == pytest.approx(306.02, abs=0.01)


def test_missing_avg_column_is_repaired(small_rep):
    """「一店订单金额平均是多少？」LLM 给 avg 但漏了目标列 -> 补回（GT 16.1063）。"""
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'avg',
                            'column': None, 'group_by': [], 'filters': [], 'document': '一店'})
    out = _run(small_rep, '一店订单金额平均是多少？', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_aggregate'
    assert out['aggregate']['column'] == AMOUNT
    assert out['aggregate']['value'] == pytest.approx(16.1063, abs=0.001)


_GROUP_COLS = ['Shipping Provider Name', 'Quantity', 'Order Amount', 'Payment Method']
_NUMERIC_COLS = {'Quantity', 'Order Amount'}


@pytest.mark.parametrize('text,expected', [
    ('每个物流商分别有多少单？', _CARRIER),
    ('一店按订单数量从高到低排列各物流商。', _CARRIER),      # 必须跳过数值列 Quantity
    ('一店按物流商统计订单数量', _CARRIER),
    ('每种支付方式有多少单？', 'Payment Method'),
])
def test_parse_group_hint_positive(text, expected):
    assert nn.parse_group_hint(text, _GROUP_COLS, nl.COLUMN_ALIASES,
                               lambda n: n in _NUMERIC_COLS) == expected


@pytest.mark.parametrize('text', [
    '一店订单金额平均是多少？',        # 没有分组标记
    '一店订单金额加起来是多少？',
    '一店订单金额总和是多少？',
    '一店有多少单？',
])
def test_parse_group_hint_rejects_uncertain(text):
    assert nn.parse_group_hint(text, _GROUP_COLS, nl.COLUMN_ALIASES,
                               lambda n: n in _NUMERIC_COLS) is None


def test_missing_group_by_is_repaired(small_rep):
    """「每个物流商分别有多少单？」LLM 漏了 group_by -> 补回（GT：3 组 12/4/3）。"""
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'count',
                            'column': None, 'group_by': [], 'filters': [],
                            'document': '一店', 'sheet': small_rep.sheets[0].sheet_name})
    out = _run(small_rep, '每个物流商分别有多少单？', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_group_aggregate'
    g = out['group_aggregate']
    assert [c['name'] for c in g['group_by']] == [_CARRIER]
    assert sorted((r['group_key'][0], r['value']) for r in g['rows']) == [
        ('JS Express International', 3.0), ('SF International', 4.0), ('Yanwen Express', 12.0)]


def test_spurious_group_by_is_cleared(small_rep):
    """「一店订单金额最大是多少？」LLM 多加 group_by -> 清掉，回到单值 35.33。"""
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'max',
                            'column': AMOUNT, 'group_by': [_CARRIER], 'filters': [],
                            'document': '一店', 'sheet': small_rep.sheets[0].sheet_name})
    out = _run(small_rep, '一店订单金额最大是多少？', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_aggregate'
    assert not out.get('group_aggregate')
    assert out['aggregate']['value'] == pytest.approx(35.33, abs=0.001)


def test_group_by_not_cleared_for_which_question(small_rep):
    """「哪个物流商订单最多？」是"选一个分组"，**不得**被当成多余分组清掉。"""
    llm = ScriptedLLM(turn={'action': 'aggregate', 'aggregate_operation': 'count',
                            'column': None, 'group_by': [_CARRIER],
                            'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 1,
                            'filters': [], 'document': '一店'})
    out = _run(small_rep, '哪个物流商订单最多？', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_group_aggregate'
    assert out['group_aggregate']['top_n'] == 1


@pytest.mark.parametrize('question,llm_turn', [
    # ① LLM 答成单值 count=19（真实故障）
    ('哪个物流商订单最多？',
     {'action': 'aggregate', 'aggregate_operation': 'count', 'column': None,
      'group_by': [], 'filters': [], 'document': '一店'}),
    # ② LLM 答成 19 行普通表格（真实故障）
    ('订单数量最多的物流商？',
     {'action': 'new_query', 'document': '一店', 'columns': [], 'limit': 50}),
])
def test_top_group_is_repaired(small_rep, question, llm_turn):
    """「哪个X最多」类问题：确定性纠正为分组排行 TOP-1（GT：Yanwen Express 12）。"""
    out = _run(small_rep, question, ScriptedLLM(turn=llm_turn))
    assert out['status'] == 'ok', out.get('message')
    assert nl.describe_executor(out) == 'run_group_aggregate'
    g = out['group_aggregate']
    assert g['total_groups'] == 3 and g['returned_groups'] == 1 and g['top_n'] == 1
    assert g['rows'][0]['group_key'][0] == 'Yanwen Express'
    assert g['rows'][0]['value'] == 12.0


@pytest.mark.parametrize('question', ['列出所有物流商', '有哪些物流商？'])
def test_top_group_does_not_hijack_list_requests(small_rep, question):
    """"列出/有哪些"这类列清单请求不得被升级成分组排行。"""
    llm = ScriptedLLM(turn={'action': 'new_query', 'document': '一店',
                            'columns': ['Shipping Provider Name'], 'limit': 20})
    out = _run(small_rep, question, llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_structured_query'


# ==========================================================================
# 11. 两步计划第 1 步口径的确定性纠正（Stage B 真实失败样本）
# ==========================================================================
def _analysis_turn(step1):
    return {'action': 'analysis', 'document': '一店',
            'steps': [step1,
                      {'type': 'aggregate', 'operation': 'sum', 'source': 'step_1',
                       'column': 'aggregate_value'}]}


def test_analysis_step1_operation_repaired_to_sum(small_rep):
    """「金额最高的10个SKU，并统计总销售额」：LLM 把第 1 步写成 count（得到 14.0）

    -> 确定性纠正为 SUM(Order Amount)（GT 267.61）。
    """
    llm = ScriptedLLM(turn=_analysis_turn(
        {'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'count', 'column': None,
         'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 10}))
    out = _run(small_rep, '找出一店中金额最高的10个SKU，并统计它们的总销售额。', llm)
    assert out['status'] == 'ok'
    assert nl.describe_executor(out) == 'run_analysis'
    m = out['multi_step']
    assert m['step1']['operation'] == 'sum'
    assert m['step1']['column'] == AMOUNT
    assert m['value'] == pytest.approx(267.61, abs=0.01)       # GT


def test_analysis_step1_count_wording_kept(small_rep):
    """「订单数量最多的5个物流商，并计算总订单数」：口径本来就是 count -> 不得被改成 sum。"""
    llm = ScriptedLLM(turn=_analysis_turn(
        {'type': 'group_aggregate', 'group_by': [SKU], 'operation': 'count', 'column': None,
         'order_by': 'aggregate_value', 'order_dir': 'desc', 'top_n': 5}))
    out = _run(small_rep, '找出订单数量最多的5个SKU，并计算这5个SKU的总订单数。', llm)
    assert out['status'] == 'ok'
    m = out['multi_step']
    assert m['step1']['operation'] == 'count'      # 口径未被误改成 sum
    assert m['step1']['column'] is None
    assert 1.0 <= float(m['value']) <= 19.0        # 前 5 个 SKU 的订单条数合计（≤ 总行数 19）
