# -*- coding: utf-8 -*-
"""自然语言解析的**确定性优先层**（后续 2A）。

设计原则（与 `nl_query` 的分工）：

    用户输入
      ↓ ① Python 预归一化            <- 本模块
      ↓ ② Python 明确可判定的意图      <- 本模块（快路径 / 信号）
      ↓ ③ LLM 处理剩余语义            <- nl_query
      ↓ ④ Python 严格 schema 校验      <- query / aggregate / multi_step
      ↓ ⑤ 必要时 Python deterministic fallback
      ↓ ⑥ executor

本模块**不产生任何数据**、不接触 SQL、不调用 LLM，只做：
- 文本归一化（全角→半角、中文数字→阿拉伯数字、空白折叠）
- 数字提取（支持中文数字：五 / 十 / 十一 / 二十 / 一百零一 / 前二十条）
- 意图信号（普通列查询 / 计数 / 求和 / 平均 / 最值 / 分组 / 排序方向 / TOP-N / 逐行计算 / 多步）
- 纯分页短语的快路径解析（下一页 / 再来20条 / 第51到100条 …）
- 文档名确定性匹配（多候选 → 澄清，绝不随机选）

所有规则都刻意保守：**宁可漏判交给 LLM，不可误判劫持普通查询**。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 1) 文本归一化
# ---------------------------------------------------------------------------
#: 中文数字字符（识别用；转换用下面的有限状态解析器）
_CN_NUM_CHARS = '零〇一二两三四五六七八九十百千万亿廿卅'
_CN_NUM_RE = re.compile(f'[{_CN_NUM_CHARS}]+')
_CN_DIGIT = {'零': 0, '〇': 0, '一': 1, '两': 2, '二': 2, '三': 3, '四': 4,
             '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_CN_UNIT = {'十': 10, '百': 100, '千': 1000}
_CN_BIG_UNIT = {'万': 10_000, '亿': 100_000_000}
#: 解析上限（超出视为不是数字，保持原样）——避免天文数字或异常
_CN_MAX = 99_999_999


def to_halfwidth(text: Any) -> str:
    """全角→半角（含全角空格），并折叠连续空白。中文数字保持原样。"""
    if text is None:
        return ''
    out: List[str] = []
    for ch in str(text):
        code = ord(ch)
        if code == 0x3000:               # 全角空格
            out.append(' ')
        elif 0xFF01 <= code <= 0xFF5E:   # 全角 ASCII
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return re.sub(r'\s+', ' ', ''.join(out)).strip()


def _cn_run_to_int(run: str) -> Optional[int]:
    """把一段纯中文数字转成整数（支持 十/百/千/万/亿 与 零）。

    只支持 1 ~ 99,999,999 的常见写法；无法可靠解析时返回 None（调用方保持原文）。
    - 纯数字串按位拼接：五六 -> 56
    - 「十」=10、「十一」=11、「二十」=20、「一百零一」=101、「两千」=2000
    """
    if not run:
        return None
    if all(ch in _CN_DIGIT for ch in run):
        val = 0
        for ch in run:
            val = val * 10 + _CN_DIGIT[ch]
        return val if 0 < val <= _CN_MAX else None

    total = 0
    section = 0
    number = 0
    for ch in run:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if number == 0:
                number = 1              # 「十一」= 11
            section += number * unit
            number = 0
        elif ch in _CN_BIG_UNIT:
            unit = _CN_BIG_UNIT[ch]
            section = (section + number) * unit
            total += section
            section = 0
            number = 0
        elif ch == '廿':
            section += 20
        elif ch == '卅':
            section += 30
        else:
            return None
    value = total + section + number
    if value <= 0 or value > _CN_MAX:
        return None
    return value


def cn_to_arabic(text: Any) -> str:
    """把文本中**可可靠解析**的中文数字片段替换成阿拉伯数字。"""
    s = to_halfwidth(text)
    if not s:
        return ''

    def _sub(m: re.Match) -> str:
        value = _cn_run_to_int(m.group(0))
        return str(value) if value is not None else m.group(0)

    return _CN_NUM_RE.sub(_sub, s)


def normalize_name(text: Any) -> str:
    """文档/Sheet/列 名归一化：半角化 + 中文数字数字化 + 去空白/下划线/点/连字符 + 小写。

    与旧的逐字替换（``二十``→``210``）不同：这里用有限状态解析，
    ``二十``→``20``、``十一``→``11``、``一百零一``→``101``。
    """
    s = cn_to_arabic(text).lower()
    return re.sub(r'[\s_\-\./\\()（）\[\]【】]+', '', s)


def extract_number(token: Any) -> Optional[int]:
    """从"数字/中文数字"片段取整数（``5`` / ``五`` / ``二十`` / ``第十一`` → 5/5/20/11）。"""
    if token is None or isinstance(token, bool):
        return None
    if isinstance(token, int):
        return token
    if isinstance(token, float) and float(token).is_integer():
        return int(token)
    s = to_halfwidth(token)
    m = re.search(r'\d+', s)
    if m:
        return int(m.group(0))
    m = _CN_NUM_RE.search(s)
    if m:
        return _cn_run_to_int(m.group(0))
    return None


# ---------------------------------------------------------------------------
# 2) 意图信号（确定性词元判定）
# ---------------------------------------------------------------------------
#: 普通列表/展示说法（出现这些且**没有**明确统计词时，必须保持普通查询）
PLAIN_LIST_RE = re.compile(
    r'有哪些|有哪几个|哪些|列出|列一下|列举|显示|展示|看看|查看|给我看|给我列|筛选|过滤|找出|挑出'
)
#: 会**阻断两步分析判定**的纯展示说法（刻意比 PLAIN_LIST_RE 窄）：
#: 「找出金额最高的10个SKU，并统计它们的总销售额」是两步分析（找出不算阻断词），
#: 而「列出前20条SKU，并统计它们的总数量」只是列表查询 + 统计。
MULTI_STEP_BLOCK_RE = re.compile(
    r'有哪些|有哪几个|列出|列一下|列举|显示|展示|看看|查看|给我看|筛选|过滤'
)
#: 计数
COUNT_RE = re.compile(
    r'有多少|几单|几笔|几条|几个|几件|多少条|多少单|多少行|多少笔|多少件|行数|条数|单数|数量是多少')
#: 「订单数量 / 订单数 / 单量 / 笔数」= **订单条数**（不是某个叫"数量"的列之和）。
#: 仅在没有求和/平均/最值词时生效（「订单数量的总和」仍是 SUM(Quantity)）。
ORDER_COUNT_RE = re.compile(r'订单数量|订单数|单量|订单量|订单笔数|笔数|条数|单据数')
#: 求和
SUM_RE = re.compile(
    r'总和|合计|总额|总计|求和|加起来|一共多少|总销售额|总金额|销量总计|金额总计|共计|加总')
#: 平均
AVG_RE = re.compile(r'平均|均值|平均数')
#: 最小值
MIN_RE = re.compile(r'最小|最低|最少|最小值')
#: 最大值（单值语义；与 TOP-N 的区分见 TOPN_RE）
MAX_RE = re.compile(r'最大|最高|最多|极大值')
#: 分组
GROUP_RE = re.compile(
    r'每个|各个|各(?!位|个)|分别|每种|每类|每款|分组|分门别类'
    r'|按.{0,12}?(?:统计|分组|汇总|求和|分别|计数|数量|多少)'
)
#: 排序方向
ORDER_ASC_RE = re.compile(r'从低到高|从少到多|由小到大|由少到多|升序|递增|正序')
ORDER_DESC_RE = re.compile(r'从高到低|从多到少|由大到小|由多到少|降序|递减|倒序')
#: 排序词（不区分方向）
ORDER_ANY_RE = re.compile(r'排序|排列|排名|排行|靠前|从高到低|从低到高|升序|降序')
#: TOP-N：数字可以是阿拉伯数字或中文数字
_NUM_TOKEN = r'(?:\d+|[零〇一二两三四五六七八九十百千万]+)'
TOPN_RE = re.compile(
    rf'(?:前\s*({_NUM_TOKEN})\s*(?:个|条|名|组|位|只|项|款|种|的)?'
    rf'|top\s*({_NUM_TOKEN})'
    r'|(?:最高|最大|最多|最低|最小|最少)\s*的?\s*(' + _NUM_TOKEN + r')\s*(?:个|条|名|组|位|款|种)'
    rf'|({_NUM_TOKEN})\s*(?:个|条|名|组|位|款|种)\s*(?:最高|最大|最多|最低|最小|最少))',
    re.IGNORECASE,
)
#: 逐行计算（每笔/每行）
ROW_CALC_CUE_RE = re.compile(r'每笔|每行|每一单|每一个订单|单笔|逐笔|逐行')
CALC_OPS = {
    'div': re.compile(r'除以|相除|÷|/'),
    'mul': re.compile(r'乘以|相乘|×|\*'),
    'add': re.compile(r'加上|相加|\+'),
    'sub': re.compile(r'减去|相减'),
}
#: 多步（先排行 → 再对前 N 名汇总）
MULTI_RANK_RE = TOPN_RE
MULTI_AGG_RE = re.compile(
    r'总(?:销售额|金额|订单数|数量|个数|和|额|计|数)|合计|加起来|求和|汇总|平均'
)
MULTI_CONNECT_RE = re.compile(r'并|再|然后|以及|同时|之后|并且|还要')
#: 「…（前N名）的<总词>是多少」——**没有连接词**，但语义同样是对前 N 名再做一次汇总。
#: 判定必须严格：总词后面要紧跟「是多少 / 多少 / 是几 / 问号 / 句末」，
#: 这样才能把「销售额总和是多少？」（要对前 N 名求和）与
#: 「总和最高的5个物流商」（"总和"只是排名口径，仍是单步 TOP-N）区分开。
TOTAL_ASKED_RE = re.compile(
    r'(?:总和|合计|总额|总计|加起来|一共|总共|共计|总销售额|总金额|总数量|总订单数'
    r'|平均值|平均数|均值)'
    r'\s*(?:是多少|是几|为多少|多少|呢|\?|？|。|$)'
)
#: 分配性说法（每个 / 分别 / 各…）：出现即说明"每组一个值"，绝不是对整体的汇总。
DISTRIBUTIVE_RE = re.compile(r'每个|每种|每类|每一|分别|各自|逐个|逐一|各(?!位|个)')
#: 是否出现分页词（用于"没有上下文"时给出更准确的澄清）
PAGINATION_ANY_RE = re.compile(r'下一页|上一页|再来|继续看|往下看|往前看|next|prev', re.IGNORECASE)


@dataclass
class IntentSignals:
    """一句话里**确定性可见**的意图信号（不含任何猜测）。"""

    text: str = ''
    normalized: str = ''
    plain_list: bool = False
    count: bool = False
    #: 文本里出现「订单数量 / 单量 / 笔数」这类"条数"说法（用于把 SUM(Quantity) 纠回 COUNT）
    order_count_word: bool = False
    sum_: bool = False
    avg: bool = False
    min_: bool = False
    max_: bool = False
    group: bool = False
    order_asc: bool = False
    order_desc: bool = False
    top_n: Optional[int] = None
    #: TOP-N 的量词语义：'row'（条/行/笔/单 -> 普通查询的 limit）
    #: / 'group'（个/名/组/款/种 -> 分组统计的 top_n）/ None（未出现）
    topn_unit: Optional[str] = None
    row_calc: bool = False
    multi_step: bool = False
    pagination_words: bool = False
    calc_ops: List[str] = field(default_factory=list)

    @property
    def any_stat(self) -> bool:
        return bool(self.count or self.sum_ or self.avg or self.min_ or self.max_)

    @property
    def order_dir(self) -> Optional[str]:
        if self.order_desc and not self.order_asc:
            return 'desc'
        if self.order_asc and not self.order_desc:
            return 'asc'
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'plain_list': self.plain_list, 'count': self.count,
            'order_count_word': self.order_count_word, 'sum': self.sum_,
            'avg': self.avg, 'min': self.min_, 'max': self.max_, 'group': self.group,
            'order_dir': self.order_dir, 'top_n': self.top_n, 'topn_unit': self.topn_unit,
            'row_calc': self.row_calc,
            'multi_step': self.multi_step, 'pagination_words': self.pagination_words,
            'calc_ops': list(self.calc_ops),
        }


def detect_signals(message: Any) -> IntentSignals:
    """提取确定性意图信号（纯词元，不做任何执行决策）。"""
    half = to_halfwidth(message)
    sig = IntentSignals(text=half, normalized=cn_to_arabic(half))
    if not half:
        return sig
    # 中文数字在 normalized 里已数字化；两种文本都判定，避免「前五个」漏判
    both = [half, sig.normalized]
    sig.plain_list = any(PLAIN_LIST_RE.search(t) for t in both)
    sig.count = any(COUNT_RE.search(t) for t in both)
    sig.order_count_word = any(ORDER_COUNT_RE.search(t) for t in both)
    sig.sum_ = any(SUM_RE.search(t) for t in both)
    sig.avg = any(AVG_RE.search(t) for t in both)
    sig.min_ = any(MIN_RE.search(t) for t in both)
    sig.max_ = any(MAX_RE.search(t) for t in both)
    sig.group = any(GROUP_RE.search(t) for t in both)
    sig.order_asc = any(ORDER_ASC_RE.search(t) for t in both)
    sig.order_desc = any(ORDER_DESC_RE.search(t) for t in both)
    sig.row_calc = bool(ROW_CALC_CUE_RE.search(half))
    sig.calc_ops = [op for op, regex in CALC_OPS.items() if regex.search(half)]
    for t in both:
        m = TOPN_RE.search(t)
        if not m:
            continue
        raw = next((g for g in m.groups() if g), None)
        value = extract_number(raw)
        if value is not None:
            sig.top_n = value
            matched = m.group(0)
            if re.search(r'[条行笔单]', matched):
                sig.topn_unit = 'row'       # 「前10条SKU」-> 普通查询的 limit
            elif re.search(r'[个名组位款种]', matched):
                sig.topn_unit = 'group'     # 「前5个物流商」-> 分组统计的 top_n
            break
    sig.multi_step = bool(
        MULTI_RANK_RE.search(sig.normalized)
        and MULTI_AGG_RE.search(half)
        # ① 有连接词（「…，并统计它们的总销售额」）
        # ② 或把总量**直接问出来**（「…的销售额总和是多少？」）
        and (MULTI_CONNECT_RE.search(half) or TOTAL_ASKED_RE.search(half))
        and not MULTI_STEP_BLOCK_RE.search(half)
        # 分配性说法（每个/分别/各…）说明要的是"每组一个值"，不是整体汇总
        and not DISTRIBUTIVE_RE.search(half)
    )
    sig.pagination_words = bool(PAGINATION_ANY_RE.search(half))
    return sig


def looks_statistical(message: Any) -> bool:
    """确定性判定：这句话是否**明确要求统计**（保守：纯展示说法一律不算）。"""
    sig = detect_signals(message)
    if not sig.text:
        return False
    if sig.any_stat:
        return True
    # 「排名/排行/排序」这类说法不决定方向，但足以说明是在做排行统计
    has_order_word = bool(ORDER_ANY_RE.search(sig.text)
                          or ORDER_ANY_RE.search(sig.normalized))
    if sig.plain_list and not has_order_word:
        return False
    if sig.group and (has_order_word or sig.top_n is not None):
        return True
    if sig.top_n is not None and has_order_word:
        return True
    return False


def is_row_calc_only(message: Any) -> bool:
    """是否只是"逐行算一个值"——用于抑制统计兜底。

    判定：出现算术（除以/乘以/加/减）**且**没有任何"聚合动词"
    （求和 / 平均 / 最值 / 分组）。

    注意 COUNT 不算聚合动词：像「每笔订单的金额除以数量是多少？」这种问法的
    「多少」指**每行的计算值**，不是行数；它必须仍是普通查询。
    """
    sig = detect_signals(message)
    if not sig.text:
        return False
    if not (sig.row_calc or sig.calc_ops):
        return False
    return not (sig.sum_ or sig.avg or sig.min_ or sig.max_ or sig.group)


def looks_multi_step(message: Any) -> bool:
    """是否「先排行、再对前 N 名汇总一次」的两步语义。"""
    return detect_signals(message).multi_step


# ---------------------------------------------------------------------------
# 3) 分页快路径（确定性短语 → 分页指令，不调用 LLM）
# ---------------------------------------------------------------------------
@dataclass
class Pagination:
    """快路径解析出的分页指令。"""

    action: str
    limit: Optional[int] = None
    start_index: Optional[int] = None
    end_index: Optional[int] = None


_PAGE_N = rf'({_NUM_TOKEN})?\s*(?:条|行|个|笔|单)?'
#: 允许的礼貌前缀（可组合、可省略）：看 / 查看 / 给我 / 帮我 / 请 …
_PAGE_PREFIX = r'(?:请|帮我|帮忙|给我|我要|要|看一下|看|查看|来|再|下)*\s*'
#: **整句**匹配：带新条件的问句（如「再来10条SKU」）不会被劫持为翻页
_PAGINATION_STRICT_RE: List[Tuple[str, re.Pattern]] = [
    # 「第51到100条」/「给我51-100条」/「查看第101到150条」（"第"可省略）
    ('range', re.compile(
        rf'^{_PAGE_PREFIX}(?:第\s*)?({_NUM_TOKEN})\s*(?:到|至|~|-|—|－)\s*'
        rf'({_NUM_TOKEN})\s*(?:条|行|个|笔|单)?$')),
    ('start', re.compile(
        rf'^{_PAGE_PREFIX}(?:从)?\s*(?:第\s*)?({_NUM_TOKEN})\s*(?:条|行|个|笔|单)?\s*(?:开始|起)\s*'
        rf'(?:给(?:我)?|看|来|要)?\s*({_NUM_TOKEN})?\s*(?:条|行|个|笔|单)?$')),
    ('next', re.compile(
        rf'^(?:下(?:一)?页|再(?:来|看|给(?:我)?)?\s*{_PAGE_N}|继续(?:看|来)?\s*{_PAGE_N}'
        rf'|往下(?:再看|看)?\s*{_PAGE_N}|next)$')),
    ('prev', re.compile(
        rf'^(?:上(?:一)?页|往前(?:再看|看)?\s*{_PAGE_N}|prev)$')),
]


def parse_pagination(message: Any) -> Optional[Pagination]:
    """严格整句解析分页短语；不是纯分页指令时返回 None。

    注意：这里**不做整句中文数字转换**（否则「下一页」会被转成「下1页」），
    数字由 `_NUM_TOKEN` 直接识别中文数字，再用 `extract_number` 取值。
    """
    text = to_halfwidth(message)
    if not text:
        return None
    for action, regex in _PAGINATION_STRICT_RE:
        m = regex.match(text)
        if not m:
            continue
        groups = [g for g in m.groups() if g]
        if action == 'range':
            a, b = extract_number(groups[0]), extract_number(groups[1])
            if a is None or b is None:
                return None
            return Pagination(action='range', start_index=a, end_index=b)
        if action == 'start':
            a = extract_number(groups[0])
            n = extract_number(groups[1]) if len(groups) > 1 else None
            if a is None:
                return None
            return Pagination(action='start', start_index=a, limit=n)
        n = extract_number(groups[0]) if groups else None
        return Pagination(action=action, limit=n)
    return None


#: 「再看前N个 / 换成前N个 / 只看前N个」——上一轮是**分组统计**时才可解释为改 TOP-N
_TOPN_FOLLOWUP_RE = re.compile(
    rf'^\s*(?:再|重新|换(?:成)?|只|就)?\s*(?:看|来|给(?:我)?|要)?\s*前\s*({_NUM_TOKEN})\s*'
    rf'(?:个|条|名|组)?\s*$'
)


def parse_topn_followup(message: Any, min_n: int = 1, max_n: int = 200) -> Optional[int]:
    """严格解析「再看前N个」这类追问（仅在上一轮是分组统计时使用）。

    同样不做整句转换（保留原文，靠 `_NUM_TOKEN` 识别中文数字）。
    """
    text = to_halfwidth(message)
    m = _TOPN_FOLLOWUP_RE.match(text or '')
    if not m:
        return None
    n = extract_number(m.group(1))
    if n is not None and min_n <= n <= max_n:
        return n
    return None


# ---------------------------------------------------------------------------
# 4) 文档名确定性匹配
# ---------------------------------------------------------------------------
#: 口语里的「N店」特征（中文数字先数字化）
_STORE_RE = re.compile(r'(\d{1,3})\s*店')


def _name_tokens(stem_norm: str) -> set:
    return {t for t in re.split(r'[^0-9a-z\u4e00-\u9fff]+', stem_norm) if t}


#: 注入特征（纵深防御：真正的安全由 schema 校验 + 受控 SQL 保证，
#: 这里只是让"看起来像注入"的输入**明确不可解析**，从而进入澄清/报错，而不是被宽松匹配吞掉）
_INJECTION_RE = re.compile(
    r"(--|;|/\*|\*/|'|\"|`)"
    r'|\b(drop|delete|update|insert|alter|union|select|exec|xp_|information_schema|'
    r'truncate|grant|revoke)\b',
    re.IGNORECASE,
)


def looks_like_injection(text: Any) -> bool:
    """判断名称类输入（文档/Sheet/列）是否含有 SQL 注入特征。"""
    if text is None:
        return False
    return bool(_INJECTION_RE.search(str(text)))


def score_document_hint(stem_norm: str, hint_norm: str) -> float:
    """确定性打分（每一档都有明确语义，不是黑箱相似度）。"""
    if not stem_norm or not hint_norm:
        return 0.0
    if stem_norm == hint_norm:
        return 1000.0
    if hint_norm in stem_norm:
        return 500.0 + len(hint_norm)
    if stem_norm in hint_norm:
        return 400.0 + len(stem_norm)
    hint_store = _STORE_RE.search(hint_norm)
    if hint_store:
        stem_stores = {m.group(1) for m in _STORE_RE.finditer(stem_norm)}
        if hint_store.group(1) in stem_stores:
            return 300.0
    hint_tokens = _name_tokens(hint_norm)
    stem_tokens = _name_tokens(stem_norm)
    if hint_tokens and stem_tokens:
        common = hint_tokens & stem_tokens
        if common:
            return 100.0 * len(common) / max(len(hint_tokens), len(stem_tokens))
    return 0.0


@dataclass
class DocumentMatch:
    """文档匹配结果：doc 为 None 时 candidates 是需要用户确认的候选文件名。"""

    doc: Optional[Dict[str, Any]] = None
    candidates: List[str] = field(default_factory=list)
    reason: str = ''


def resolve_document_deterministic(
    catalog: Sequence[Dict[str, Any]], hint: Optional[str]
) -> DocumentMatch:
    """确定性文档解析（绝不随机挑）。

    1. 只有 1 个文档 → 直接返回（用户没有选择）；
    2. hint 为空 → 返回全部候选（需澄清）；
    3. 归一化精确相等 / hint 是文件名子串 → 命中；**多个并列 → 澄清**（相似文件不合并）；
    4. 「N店」特征 + token 重叠 → 最高分唯一才命中，否则澄清。
    """
    docs = list(catalog)
    if not docs:
        return DocumentMatch(None, [], 'empty_catalog')
    if hint and str(hint).strip() and looks_like_injection(hint):
        # 纵深防御优先于任何捷径：疑似注入的名称一律"不可解析"
        return DocumentMatch(None, [str(d.get('filename') or '') for d in docs], 'injection')
    if len(docs) == 1:
        return DocumentMatch(docs[0], [], 'single_catalog')
    if not hint or not str(hint).strip():
        return DocumentMatch(None, [str(d.get('filename') or '') for d in docs], 'no_hint')

    hint_norm = normalize_name(hint)
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for d in docs:
        stem = normalize_name(Path(str(d.get('filename') or '')).stem)
        scored.append((score_document_hint(stem, hint_norm), d))
    scored.sort(key=lambda x: x[0], reverse=True)

    best_score = scored[0][0]
    if best_score < 100.0:
        return DocumentMatch(None, [str(d.get('filename') or '') for d in docs], 'no_match')
    tied = [d for s, d in scored if abs(s - best_score) < 1e-9]
    if len(tied) > 1:
        return DocumentMatch(
            None, [str(d.get('filename') or '') for d in tied], 'ambiguous')
    return DocumentMatch(scored[0][1], [], 'matched')


# ---------------------------------------------------------------------------
# 5) Sheet / 列 名确定性解析（精确优先，绝不模糊覆盖精确）
# ---------------------------------------------------------------------------
def resolve_sheet_deterministic(
    sheet_names: Sequence[str], hint: Optional[str]
) -> Tuple[Optional[str], List[str]]:
    """Sheet 解析：精确 → 大小写不敏感 → 归一化唯一子串；多义则返回候选。"""
    names = list(sheet_names)
    if not names:
        return None, []
    if hint and str(hint).strip() and looks_like_injection(hint):
        return None, names
    if len(names) == 1:
        return names[0], []
    if not hint or not str(hint).strip():
        return None, names

    exact = [n for n in names if n == hint]
    if len(exact) == 1:
        return exact[0], []
    ci = [n for n in names if n.lower() == str(hint).lower()]
    if len(ci) == 1:
        return ci[0], []
    h = normalize_name(hint)
    if h:
        norm = [n for n in names
                if h in normalize_name(n) or normalize_name(n) in h]
        if len(norm) == 1:
            return norm[0], []
        if len(norm) > 1:
            return None, norm
    return None, names


def resolve_column_deterministic(
    column_names: Sequence[str],
    hint: Optional[str],
    aliases: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], List[str]]:
    """列解析：精确 → 大小写不敏感 → 同义词（必须真实存在） → 归一化一致 → 唯一子串。

    任何一步出现多个候选都**不猜**，直接返回候选列表让上层澄清。
    """
    names = list(column_names)
    if not names:
        return None, []
    if not hint or not str(hint).strip():
        return None, names
    if hint not in names and looks_like_injection(hint):
        # 纵深防御：疑似注入的名称一律"不可解析"（真正的执行安全仍由受控 SQL 保证）
        return None, names
    if hint in names:
        return hint, []
    ci = [n for n in names if n.lower() == str(hint).lower()]
    if len(ci) == 1:
        return ci[0], []
    if len(ci) > 1:
        return None, ci
    if aliases:
        alias = aliases.get(normalize_name(hint))
        if alias:
            hit = [n for n in names if normalize_name(n) == normalize_name(alias)]
            if len(hit) == 1:
                return hit[0], []
    nh = normalize_name(hint)
    ne = [n for n in names if normalize_name(n) == nh]
    if len(ne) == 1:
        return ne[0], []
    if nh:
        sub = [n for n in names if nh in normalize_name(n) or normalize_name(n) in nh]
        if len(sub) == 1:
            return sub[0], []
        if len(sub) > 1:
            return None, sub
    return None, names


# ---------------------------------------------------------------------------
# 6) 逐行计算「A op B」的确定性切分（后续 2A）
# ---------------------------------------------------------------------------
#: 单个算术运算符（按**优先顺序**匹配；`/` 必须是独立运算符，不能是文件名/日期里的斜杠）
_CALC_OP_TOKENS: List[Tuple[str, re.Pattern]] = [
    ('div', re.compile(r'除以|相除|÷|/')),
    ('mul', re.compile(r'乘以|相乘|×|\*')),
    ('add', re.compile(r'加上|相加|\+')),
    ('sub', re.compile(r'减去|相减')),
]
#: 每个操作数最多向右/向左扫描的字符数（真实列名不会这么长）
_MAX_OPERAND_SCAN = 24
#: 操作数最少长度（单字几乎一定是噪声）
_MIN_OPERAND_LEN = 2


def _longest_resolvable_column(
    fragment: str,
    column_names: Sequence[str],
    aliases: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """从一段含噪声的片段里找出**最长且唯一可解析**的真实列名（找不到返回 None）。

    例：「一店每笔订单的金额」→「Order Amount」（「金额」是同义词）；
        「数量是多少？」          →「Quantity」（尾部噪声被忽略）。
    只在**唯一命中**时返回；多义/无命中一律返回 None（绝不猜）。
    """
    text = (fragment or '').strip()
    if len(text) < _MIN_OPERAND_LEN:
        return None
    # 最长优先：避免短子串（如「金额」）先命中而丢掉了更长/更精确的名字
    for length in range(min(len(text), _MAX_OPERAND_SCAN), _MIN_OPERAND_LEN - 1, -1):
        for start in range(0, len(text) - length + 1):
            name, candidates = resolve_column_deterministic(
                column_names, text[start:start + length], aliases)
            if name is not None and not candidates:
                return name
    return None


#: 公开别名（供 nl_query 复用同一套"最长唯一命中"规则）
longest_resolvable_column = _longest_resolvable_column


def resolvable_columns(
    message: Any,
    column_names: Sequence[str],
    aliases: Optional[Dict[str, str]] = None,
    max_scan: int = _MAX_OPERAND_SCAN,
) -> List[str]:
    """列出文本里**全部**能唯一解析到的真实列（最长优先、去重）。

    用途：当"最长命中"不可靠时（例如「金额最高的10个SKU」里 `SKU` 会命中 `SKU ID`），
    由调用方按业务语义（可数值化、非业务标识）再筛一遍，而不是只看最长的那个。
    """
    text = to_halfwidth(message)
    out: List[str] = []
    if not text or not column_names:
        return out
    n = len(text)
    for length in range(min(n, max_scan), 1, -1):
        for start in range(0, n - length + 1):
            name, cands = resolve_column_deterministic(
                column_names, text[start:start + length], aliases)
            if name is not None and not cands and name not in out:
                out.append(name)
    return out


# ---------------------------------------------------------------------------
# 7) 简单等值筛选的确定性抽取（后续 2A：LLM 漏抽 「<列>为 <值>」 时补全）
# ---------------------------------------------------------------------------
#: 「<列>为/是/等于 <值>」——只支持这几种最明确的写法（范围/包含等一律交回 LLM）
_SIMPLE_FILTER_RE = re.compile(
    r'([A-Za-z0-9_\u4e00-\u9fff]{1,20}?)\s*(?:为|是|等于|＝|=)\s*'
    r'([^\s，,。；;：:？?、（）()]{1,40})'
)


def _cell_matches(value: Any, probe: str) -> Optional[str]:
    """把用户写的值片段与真实单元格比较：完全相等 → eq；子串 → contains；否则 None。"""
    if value is None:
        return None
    cell = to_halfwidth(value).strip()
    if not cell:
        return None
    if cell.lower() == probe.lower():
        return 'eq'
    if probe and probe.lower() in cell.lower():
        return 'contains'
    return None


def parse_simple_filter(
    message: Any,
    column_names: Sequence[str],
    aliases: Optional[Dict[str, str]] = None,
    values_of: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """确定性抽取「<列>为 <值>」等值/包含筛选（**必须能在真实数据里找到该值**）。

    设计要点（宁可不补，也不猜）：
    - 列名必须唯一解析到真实列；
    - 值必须**在真实单元格里存在**（完全相等 → eq；只是子串 → contains）；
      写错的值不会凭空造出一个条件；
    - 值片段按"最长优先"逐字回退匹配，从而自动裁掉「SF的订单有哪些」里的尾部噪声；
    - 找不到任何真实值时返回 None（交给 LLM / 澄清）。

    ``values_of`` 是 ``col_name -> 该列全部原始值`` 的取值函数（由调用方提供，
    这样本函数依然不接触 representation）。
    """
    text = to_halfwidth(message)
    if not text or not column_names or values_of is None:
        return None
    for m in _SIMPLE_FILTER_RE.finditer(text):
        col_name, _cands = resolve_column_deterministic(column_names, m.group(1), aliases)
        if col_name is None:
            continue
        raw = m.group(2)
        for length in range(len(raw), 0, -1):
            probe = raw[:length].strip()
            if not probe:
                break
            for value in values_of(col_name):
                operator = _cell_matches(value, probe)
                if operator:
                    return {'column': col_name, 'operator': operator, 'value': probe}
    return None


# ---------------------------------------------------------------------------
# 8) 分组维度列的确定性判定（后续 2A）
# ---------------------------------------------------------------------------
#: 分组标记（后面紧跟的一般就是"分组维度"）
GROUP_MARKER_RE = re.compile(
    r'(?:每个|每一种|每种|每一类|每类|各个|各|按|分别)\s*'
    r'([^\s，,。；;：:？?、（）()]{1,24})'
)
#: 「哪个/哪些/哪家/哪种/哪款」= "选一个分组"的问法
#: （出现时**不得**把 LLM 给出的 group_by 当成"多余分组"清掉）
WHICH_RE = re.compile(r'哪(?:个|一?个|家|种|类|些|款|位)')
#: 「取一个分组」的极值词（配合分组维度列 -> 分组排行 TOP-1）
TOP_GROUP_MAX_RE = re.compile(r'最多|最高|最大')
TOP_GROUP_MIN_RE = re.compile(r'最少|最低|最小')
#: 明确的"列清单"请求（出现时不得把问题升级成分组 TOP-1）
LIST_REQUEST_RE = re.compile(r'列出|列一下|列举|显示|展示|筛选|过滤|给我看|看看|查看')


def parse_group_hint(
    message: Any,
    column_names: Sequence[str],
    aliases: Optional[Dict[str, str]] = None,
    is_numeric: Optional[Any] = None,
) -> Optional[str]:
    """从「每个 / 各 / 按 / 分别 <列>」里确定性找出**分组维度列**。

    - 每个标记取右侧片段，按"最长优先"逐字回退，直到命中真实列；
    - **跳过数值列**：数值列通常是被统计的目标（如「按订单数量排列」里的"数量"），
      而不是分组维度（「各物流商」里的"物流商"）；
    - 多个标记给出**不同**候选 → None（不猜）。

    例：「每个物流商分别有多少单？」-> Shipping Provider Name
        「一店按订单数量从高到低排列各物流商。」-> Shipping Provider Name（跳过 Quantity）
        「一店按物流商统计订单数量」-> Shipping Provider Name
    """
    text = to_halfwidth(message)
    if not text or not column_names:
        return None
    found: List[str] = []
    for m in GROUP_MARKER_RE.finditer(text):
        frag = m.group(1)
        # 必须扫**所有子串**（不能只看前缀）：分组维度常常在片段末尾，
        # 例如「按 订单数量从高到低排列**各物流商**」里的"物流商"。
        for length in range(min(len(frag), _MAX_OPERAND_SCAN), 1, -1):
            hit = None
            for start in range(0, len(frag) - length + 1):
                name, cands = resolve_column_deterministic(
                    column_names, frag[start:start + length], aliases)
                if name is None or cands:
                    continue
                if is_numeric is not None and is_numeric(name):
                    continue      # 数值列是被统计的目标，不是分组维度
                hit = name
                break
            if hit:
                if hit not in found:
                    found.append(hit)
                break
    return found[0] if len(found) == 1 else None


def parse_calculation_operands(
    message: Any,
    column_names: Sequence[str],
    aliases: Optional[Dict[str, str]] = None,
) -> Optional[Tuple[str, str, str]]:
    """确定性解析「A op B」→ ``(operation, left_column, right_column)``。

    严格边界（与 Phase 4B 一致：只允许**一个运算符 + 两个真实列**）：
    - 出现 0 个或 **2 个及以上**运算符（``A/B/C``）→ None（不做嵌套、不做链式）；
    - 左右操作数都必须**唯一解析**到真实列 → 否则 None（交给 LLM 或澄清）；
    - 两侧解析到同一列（``A/A``）→ None（无业务意义）。

    任何一步不确定都返回 None：这是"宁可不补，也不猜"的确定性层约定。
    """
    text = to_halfwidth(message)
    if not text or not column_names:
        return None
    hits: List[Tuple[int, int, str]] = []
    for op, regex in _CALC_OP_TOKENS:
        for m in regex.finditer(text):
            hits.append((m.start(), m.end(), op))
    if len(hits) != 1:
        return None
    start, end, op = hits[0]
    left = _longest_resolvable_column(
        text[max(0, start - _MAX_OPERAND_SCAN):start], column_names, aliases)
    right = _longest_resolvable_column(
        text[end:end + _MAX_OPERAND_SCAN], column_names, aliases)
    if not left or not right or left == right:
        return None
    return op, left, right
