# -*- coding: utf-8 -*-
"""Natural Language -> Structured Query（Phase 1C）

职责边界（严格）：
- **LLM 只负责**：理解用户意图 + 输出一份候选的 StructuredQueryRequest 草图
  （选哪个文件 / Sheet / 列，什么条件，limit/offset）。
- **Python 负责**：把草图里的每一个名字（文件、Sheet、列）拿**真实 schema**校验，
  解析不到或有多义时**返回澄清**；随后调用 Phase 1B 的 Structured Query 精确取数。
- **LLM 绝不生成事实数据**：结果行 100% 来自 representation.json，
  摘要文字也由 Python 生成。

本模块不修改 Phase 1B 的 query.py / representation.py / store.py 语义，
只在其之上做「自然语言 → 参数」的翻译与校验。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from backend.excel import aggregate as excel_aggregate
from backend.excel import calculation as excel_calculation
from backend.excel import engine as query_engine
from backend.excel import multi_step as excel_multi_step
from backend.excel import nl_normalize as nl_norm
from backend.excel import query as excel_query
from backend.excel.query_context import AggregateContext, AnalysisContext, ExcelQueryContext
from backend.excel.representation import (
    SheetRepresentation,
    WorkbookRepresentation,
    is_identifier_column,
)
from backend.utils.provider_errors import classify_llm_error, log_classified

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
INTENT_STRUCTURED = 'excel_structured'
INTENT_CLARIFY = 'clarify'
INTENT_NOT_EXCEL = 'not_excel'

STATUS_OK = 'ok'
STATUS_CLARIFY = 'clarify'
STATUS_NOT_EXCEL = 'not_excel'
STATUS_ERROR = 'error'

DEFAULT_NL_LIMIT = excel_query.DEFAULT_LIMIT      # 50
MAX_NL_LIMIT = excel_query.MAX_LIMIT              # 500
MAX_CATALOG_COLUMNS = 120                         # prompt 中每个 Sheet 最多列出的列数

# 中文数字 -> 阿拉伯数字（用于「五店」->「5店」这类口语表达）
_CN_DIGITS = {
    '零': '0', '一': '1', '二': '2', '两': '2', '三': '3', '四': '4',
    '五': '5', '六': '6', '七': '7', '八': '8', '九': '9', '十': '10',
}

# 业务同义词（**刻意保持很小**）：仅在 Sheet 中确实存在目标列、且**唯一命中**时才生效。
# 说明：这只是兜底；首选是 LLM 直接给出真实列名，再由 Python 精确校验。
# 后续 2A：补齐用户高频使用的口语列名（键都是 `normalize_name` 之后的形式）。
COLUMN_ALIASES: Dict[str, str] = {
    # --- 主键/编号 ---
    'sku': 'SKU ID',
    'skuid': 'SKU ID',
    'sku编号': 'SKU ID',
    'sku号': 'SKU ID',
    '订单号': 'Order ID',
    '订单id': 'Order ID',
    '订单编号': 'Order ID',
    '单号': 'Order ID',
    '卖家sku': 'Seller SKU',
    '包裹号': 'Package ID',
    '物流单号': 'Tracking ID',
    '运单号': 'Tracking ID',
    # --- 物流 ---
    '物流商': 'Shipping Provider Name',
    '物流公司': 'Shipping Provider Name',
    '物流服务商': 'Shipping Provider Name',
    '快递公司': 'Shipping Provider Name',
    '快递': 'Shipping Provider Name',
    # --- 金额/数值 ---
    '订单金额': 'Order Amount',
    '金额': 'Order Amount',
    '总金额': 'Order Amount',
    '成交金额': 'Order Amount',
    '数量': 'Quantity',
    '件数': 'Quantity',
    '购买数量': 'Quantity',
    '退货数量': 'Sku Quantity of return',
    '税费': 'Taxes',
    '退款金额': 'Order Refund Amount',
    '重量': 'Weight(kg)',
    # --- 状态/其它 ---
    '订单状态': 'Order Status',
    '状态': 'Order Status',
    '支付方式': 'Payment Method',
    '付款方式': 'Payment Method',
    '商品名称': 'Product Name',
    '商品': 'Product Name',
    '产品名称': 'Product Name',
    '收件人': 'Recipient',
    '城市': 'City',
    '州': 'State',
    '国家': 'Country',
    '仓库': 'Warehouse Name',
    '分类': 'Product Category',
}

# 「度量列」目标：这些别名回答的是**排什么**（指标），不能当作**按什么分组**的维度。
# 实测依据：「按订单金额排名前三」/「按数量排名前三」曾被误当成"分组排行"，
# 进而执行了一次没有分组依据的聚合（order_by=aggregate_value 但 group_by 为空）。
METRIC_COLUMN_TARGETS: FrozenSet[str] = frozenset({
    'Order Amount', 'Quantity', 'Sku Quantity of return',
    'Taxes', 'Order Refund Amount', 'Weight(kg)',
})

# 维度别名（= 别名表中除度量列以外的部分）：可用于确定性推导"按什么分组"。
DIMENSION_ALIASES: Dict[str, str] = {
    _k: _v for _k, _v in COLUMN_ALIASES.items()
    if _v not in METRIC_COLUMN_TARGETS
}

_CODE_FENCE_RE = re.compile(r'```(?:json)?\s*(.*?)```', re.S | re.I)


class NlQueryError(Exception):
    """NL 层错误（含 HTTP 状态码）。"""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status = status

    def to_dict(self) -> Dict[str, Any]:
        return {'code': self.code, 'message': self.message, 'details': self.details}


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------
@dataclass
class NlIntent:
    """LLM 返回的「查询意图草图」（尚未经过 schema 校验）。"""

    query_type: str = INTENT_STRUCTURED
    document: Optional[str] = None
    sheet: Optional[str] = None
    columns: List[str] = field(default_factory=list)
    filters: List[Dict[str, Any]] = field(default_factory=list)
    limit: int = DEFAULT_NL_LIMIT
    offset: int = 0
    #: limit 是否由 LLM **显式**给出（后续 2A：用于区分默认 50 与用户明确说的「前N条」）
    limit_explicit: bool = False
    clarification: str = ''
    raw: Dict[str, Any] = field(default_factory=dict)
    # Phase 4B：受控计算字段（逐行）与"对计算值筛选"
    calculation: Optional[Dict[str, Any]] = None
    calc_filter: Optional[Dict[str, Any]] = None
    # 计算字段解析失败的原因（**绝不静默丢弃**：有值时一律澄清/报错）
    calc_error: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'query_type': self.query_type,
            'document': self.document,
            'sheet': self.sheet,
            'columns': self.columns,
            'filters': self.filters,
            'limit': self.limit,
            'offset': self.offset,
            'clarification': self.clarification,
            'calculation': self.calculation,
            'calc_filter': self.calc_filter,
            'calc_error': self.calc_error,
        }


def intent_from_dict(data: Dict[str, Any]) -> NlIntent:
    """把 LLM 的 JSON 安全地规约为 NlIntent（字段缺失/类型不对都兜住，不抛错）。"""

    def _s(v: Any) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s or None
        return None

    def _int(v: Any, default: int) -> int:
        if isinstance(v, bool):
            return default
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        if isinstance(v, str) and v.strip().lstrip('+-').isdigit():
            return int(v.strip())
        return default

    raw_type = _s(data.get('query_type')) or INTENT_STRUCTURED
    qtype = raw_type if raw_type in (INTENT_STRUCTURED, INTENT_CLARIFY, INTENT_NOT_EXCEL) else INTENT_STRUCTURED

    cols = data.get('columns')
    columns = [c.strip() for c in cols if isinstance(c, str) and c.strip()] if isinstance(cols, list) else []

    filters: List[Dict[str, Any]] = []
    raw_filters = data.get('filters')
    if isinstance(raw_filters, list):
        for item in raw_filters:
            if not isinstance(item, dict):
                continue
            col = _s(item.get('column'))
            if not col:
                continue
            op = (_s(item.get('operator')) or excel_query.OPERATOR_EQ).lower()
            # 后续 2A：**不再静默把未知 operator 降级为 eq**——那会把用户/模型的怪条件
            # 偷偷改成"等值匹配"，既掩盖问题又可能给出错误结果。
            # 保留原样，交给严格校验层拒绝（ERR_INVALID_OPERATOR）。
            filters.append({'column': col, 'operator': op, 'value': item.get('value')})

    limit = _int(data.get('limit'), DEFAULT_NL_LIMIT)
    offset = _int(data.get('offset'), 0)
    limit = max(1, min(limit, MAX_NL_LIMIT))
    offset = max(0, offset)

    # Phase 4B：计算字段（形状校验；真实列解析在 build_validated_query 中）
    # 注意：`filters` 里指向"计算值"的条件会被拆到 calc_filter（不进入真实列解析）。
    calc_filter_from_filters: Optional[Dict[str, Any]] = None
    kept_filters: List[Dict[str, Any]] = []
    for f in filters:
        if excel_calculation.is_calc_filter_column(f.get('column')):
            calc_filter_from_filters = {'operator': f.get('operator') or 'gt', 'value': f.get('value')}
        else:
            kept_filters.append(f)
    filters = kept_filters
    calc_raw = None
    calc_error = ''
    if data.get('calculation') is not None:
        try:
            calc_obj = excel_calculation.normalize_calculation(data.get('calculation'))
            if calc_obj is not None:
                calc_raw = calc_obj.to_dict()
        except excel_query.ExcelQueryError as e:
            # **绝不静默丢弃**：非法计算字段必须让用户看到原因
            calc_error = f'计算字段不合法：{e.message}'
            logger.warning('[nl] 计算字段被拒绝：%s', e.message)
    calc_filter = None
    if calc_error == '':
        try:
            parsed_calc_filter = excel_calculation.normalize_calc_filter(
                data.get('calc_filter') or calc_filter_from_filters)
        except excel_query.ExcelQueryError as e:
            calc_error = f'计算值筛选不合法：{e.message}'
            parsed_calc_filter = None
        if parsed_calc_filter is not None:
            calc_filter = parsed_calc_filter

    return NlIntent(
        query_type=qtype,
        document=_s(data.get('document')),
        sheet=_s(data.get('sheet')),
        columns=columns,
        filters=filters,
        limit=limit,
        offset=offset,
        limit_explicit=data.get('limit') is not None,
        clarification=_s(data.get('clarification')) or '',
        raw=data,
        calculation=calc_raw,
        calc_filter=calc_filter,
        calc_error=calc_error,
    )


def parse_llm_json(text: str) -> Dict[str, Any]:
    """从 LLM 输出中稳健地抽出 JSON 对象。"""
    if not text:
        raise NlQueryError('llm_empty_response', '模型未返回内容', status=502)
    candidate = text.strip()
    m = _CODE_FENCE_RE.search(candidate)
    if m:
        candidate = m.group(1).strip()
    if not candidate.startswith('{'):
        start = candidate.find('{')
        end = candidate.rfind('}')
        if start >= 0 and end > start:
            candidate = candidate[start:end + 1]
    try:
        data = json.loads(candidate)
    except Exception as e:
        raise NlQueryError(
            'llm_invalid_json', f'模型返回的不是合法 JSON：{e}', {'raw': text[:500]}, status=502
        )
    if not isinstance(data, dict):
        raise NlQueryError('llm_invalid_json', '模型返回的 JSON 不是对象', {'raw': text[:500]}, status=502)
    return data


# ----------------------------------------------------------------------------
# 名称归一化与模糊解析（确定性，不使用 LLM）
# ----------------------------------------------------------------------------
def _digitize(text: str) -> str:
    """兼容接口：中文数字数字化（后续 2A 起改用 nl_normalize 的有限状态解析）。"""
    return nl_norm.cn_to_arabic(text)


def normalize_name(text: Optional[str]) -> str:
    """归一化：小写、全角转半角、去空白/下划线/连字符、中文数字转阿拉伯数字。

    后续 2A：改由 `nl_normalize.normalize_name` 实现（有限状态解析中文数字，
    `二十`→`20` 而不是旧的 `210`），保证「五店」「二十店」都能稳定匹配文件名。
    """
    return nl_norm.normalize_name(text)


def _overlap_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    common = sum(1 for ch in set(a) if ch in b)
    return common / max(len(set(a)), len(set(b)))


def dedupe_catalog_by_filename(catalog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同名文件只保留「最新一次上传」。

    规则说明（确定性、非猜测）：同一个 filename 被重复上传时内容视为同一份，
    用户口语里也只会说文件名，因此取 created_at 最新的那条；
    若 created_at 相同则取 document_id 较大者，保证结果稳定可复现。
    """
    best: Dict[str, Dict[str, Any]] = {}
    for doc in catalog:
        key = doc.get('filename') or doc.get('document_id')
        cur = best.get(key)
        if cur is None:
            best[key] = doc
            continue
        newer = (doc.get('created_at') or '', doc.get('document_id') or '')
        older = (cur.get('created_at') or '', cur.get('document_id') or '')
        if newer > older:
            best[key] = doc
    return sorted(best.values(), key=lambda d: d.get('created_at') or '', reverse=True)


def resolve_document(catalog: List[Dict[str, Any]], hint: Optional[str]) -> Tuple[Optional[Dict], List[str]]:
    """按文件名（或口语描述）定位唯一文档。

    后续 2A：改由 `nl_normalize.resolve_document_deterministic` 实现——
    「5店 / 五店 / 直邮五店 / 五店订单 / 5店订单 / 7.10」等说法都能稳定命中同一份文件；
    相似文件（直邮5店 7.10 / 8.10 / 9.10）无法唯一确定时返回全部候选（**不随机挑**）。

    返回 (doc, candidates)：doc 为 None 时 candidates 为候选文件名（供澄清/报错）。
    """
    match = nl_norm.resolve_document_deterministic(catalog, hint)
    if match.doc is None and hint and match.reason in ('no_match',):
        # 明确说了文件但谁也匹配不上：把全部候选返回给上层澄清（不要静默选第一个）
        logger.info('[nl] 文档名无法匹配：hint=%r', hint)
    return match.doc, match.candidates


def resolve_sheet_nl(rep: WorkbookRepresentation, hint: Optional[str]) -> Tuple[Optional[SheetRepresentation], List[str]]:
    """Sheet 解析：精确 → 大小写不敏感 → 归一化唯一子串；多义则澄清。

    后续 2A：改由 `nl_normalize.resolve_sheet_deterministic` 实现，
    **精确匹配优先**（OrderSKUList 不会被子串匹配抢到 OrderSKUListBackup）。
    """
    sheets = rep.sheets
    if not sheets:
        return None, []
    name, candidates = nl_norm.resolve_sheet_deterministic([s.sheet_name for s in sheets], hint)
    if name is None:
        return None, candidates
    for s in sheets:
        if s.sheet_name == name:
            return s, []
    return None, candidates  # pragma: no cover - 理论上不可达


def resolve_column_nl(sheet: SheetRepresentation, hint: Optional[str]) -> Tuple[Optional[str], List[str]]:
    """列解析：精确 → 大小写不敏感 → 同义词 → 归一化一致 → 归一化唯一子串；多义则澄清。

    后续 2A：改由 `nl_normalize.resolve_column_deterministic` 实现（同一规则集），
    同义词表**只在目标列真实存在且唯一命中**时才生效（否则一律澄清，绝不猜）。
    """
    return nl_norm.resolve_column_deterministic(sheet.column_names, hint, COLUMN_ALIASES)


# ----------------------------------------------------------------------------
# Prompt 构造
# ----------------------------------------------------------------------------
SYSTEM_PROMPT = """你是一个「表格查询参数解析器」。你的唯一职责是把用户的中文/英文问题翻译成一份 JSON 查询草图。

硬性规则：
1. 只输出 JSON，不要输出任何解释文字、不要用 markdown 代码块。
2. 你只能从下面提供的「可用表格目录」里选择 document / sheet / columns / filters.column。
   目录里没有的名字，绝对不要编造；宁可在 query_type 填 "clarify" 并在 clarification 说明需要用户澄清什么。
3. 你绝不负责生成任何数据行。你只产出查询参数。
4. 支持的 operator 只有这 7 种，必须严格从下面挑：
   - "eq"       精确相等（「是」「为」「等于」「=」）
   - "neq"      不等于（「不是」「不等于」「≠」）
   - "gt"       大于（「大于」「超过」「多于」「>」）
   - "gte"      大于等于（「大于等于」「不小于」「至少」「>=」）
   - "lt"       小于（「小于」「少于」「低于」「<」）
   - "lte"      小于等于（「小于等于」「不大于」「至多」「<=」）
   - "contains" 包含（「包含」「含有」「带」「关键词是」）
5. **值的选择极其重要（最常出错的地方）**：
   - 中文口语里的「X 是 Y / 为 Y」如果 Y 只是某个真实值的**简称或前缀**
     （例如「物流商是SF」「物流商为SF」，而数据里的真实值是 "SF International"），
     **必须用 "contains"**，绝对不要用 "eq"；
   - 只有当用户给出的是**完整精确值**时才用 "eq"；
   - 拿不准时一律用 "contains"，不要用 "eq" 去赌一个可能不存在的精确值
     （用 "eq" 猜错会得到 0 行，等于给了用户错误答案）。
6. 「前N条」「前N行」表示 limit=N、offset=0；只有用户明确说「第51条开始」等才使用非 0 offset。
6b. 数值范围用两个 filter 表达，不要用 between/min/max 字段：
   - 「数量大于10小于50」→ [{"column":"Quantity","operator":"gt","value":10},
                          {"column":"Quantity","operator":"lt","value":50}]
   - 「数量不小于100」→ [{"column":"Quantity","operator":"gte","value":100}]
   - gt/gte/lt/lte 的 value 必须是**纯数字**（不要带引号、不要带单位），例如 100 而不是 "100件"。
6c. 多个条件之间是 AND 关系（全部满足）。
7. 用户提到列时请映射到目录中真实存在的列名。例如目录里有 "SKU ID"、"Seller SKU" 时，
   用户说「SKU」通常指 "SKU ID"（电商语境下的 SKU 主键）。
8. 如果用户的问题与表格查询无关（例如闲聊、问概念、写代码），query_type 填 "not_excel"。
9. 如果无法确定要查询哪个文件/Sheet/列（或存在多个同样合理的候选），query_type 填 "clarify"。

输出 JSON 结构（字段缺失时给 null / 空数组）：
{
  "query_type": "excel_structured" | "clarify" | "not_excel",
  "document": "文件名或用户对文件的描述",
  "sheet": "Sheet 名或 null",
  "columns": ["要返回的列名，空数组表示返回全部列"],
  "filters": [{"column": "列名", "operator": "eq|neq|gt|gte|lt|lte|contains", "value": "值"}],
  "limit": 20,
  "offset": 0,
  "clarification": "当 query_type=clarify 时，向用户说明需要澄清什么（一句话）"
}

示例（假设目录里有「直邮5店 7.10号订单.xlsx」/ Sheet「OrderSKUList」，
列：Order ID | SKU ID | Quantity | Shipping Provider Name | Variation | Order Amount）：

- 「物流商为SF的订单有哪些？」
  → {"query_type":"excel_structured","document":"5店","sheet":null,"columns":["Order ID"],
     "filters":[{"column":"Shipping Provider Name","operator":"contains","value":"SF"}],"limit":20,"offset":0}

- 「物流商是SF International，并且数量大于10，列出Order ID和SKU ID」
  → {"query_type":"excel_structured","document":"5店","sheet":null,"columns":["Order ID","SKU ID"],
     "filters":[{"column":"Shipping Provider Name","operator":"eq","value":"SF International"},
                {"column":"Quantity","operator":"gt","value":10}],"limit":20,"offset":0}

- 「数量大于100的SKU有哪些？」
  → {"columns":["SKU ID"],"filters":[{"column":"Quantity","operator":"gt","value":100}]}

- 「数量大于10小于50的SKU ID有哪些？」
  → {"columns":["SKU ID"],"filters":[{"column":"Quantity","operator":"gt","value":10},
                                       {"column":"Quantity","operator":"lt","value":50}]}

- 「Order Amount 大于等于 20 的订单」
  → {"columns":["Order ID"],"filters":[{"column":"Order Amount","operator":"gte","value":20}]}

- 「Order Status 不是已发货的订单」
  → {"columns":["Order ID"],"filters":[{"column":"Order Status","operator":"neq","value":"已发货"}]}

- 「数量小于 5 的」
  → {"filters":[{"column":"Quantity","operator":"lt","value":5}]}

- 「全部订单」（无条件）
  → {"columns":["Order ID"],"filters":[],"limit":20,"offset":0}
"""


def build_catalog_text(catalog: List[Dict[str, Any]]) -> str:
    """把可用表格目录压成文本（只含 schema，不含任何数据行）。"""
    if not catalog:
        return '（用户当前没有任何可查询的表格文件）'
    lines: List[str] = []
    for i, doc in enumerate(catalog, 1):
        # 稳定性补丁：catalog 由调用方组装，字段缺失时不能把整个 NL 解析打断
        # （旧实现用 sh["row_count"] 直接取键，缺字段会抛 KeyError 并被包装成"自然语言解析失败"）。
        lines.append(
            f'{i}. filename: {doc.get("filename") or "(未命名)"}  '
            f'(type={doc.get("file_type")}, sheets={len(doc.get("sheets", []) or [])})'
        )
        for sh in doc.get('sheets') or []:
            cols = (sh.get('columns') or [])[:MAX_CATALOG_COLUMNS]
            lines.append(
                f'   - sheet: {sh.get("sheet_name")}  '
                f'({sh.get("row_count", 0)} 行 × {sh.get("column_count", 0)} 列)'
            )
            lines.append(f'     columns: {" | ".join(str(c) for c in cols)}')
    return '\n'.join(lines)


def build_messages(user_message: str, catalog_text: str) -> List[Dict[str, str]]:
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': (
                f'【可用表格目录】\n{catalog_text}\n\n'
                f'【用户问题】\n{user_message}\n\n'
                f'请输出 JSON。'
            ),
        },
    ]


async def llm_parse_intent(llm, user_message: str, catalog: List[Dict[str, Any]]) -> NlIntent:
    """调用 LLM 得到意图草图（只解析参数，不取数）。"""
    messages = build_messages(user_message, build_catalog_text(catalog))
    parts: List[str] = []
    async for chunk in llm.chat_completion(messages, stream=False):
        if chunk:
            parts.append(chunk)
    text = ''.join(parts)
    data = parse_llm_json(text)
    intent = intent_from_dict(data)
    logger.info('[nl] intent=%s', json.dumps(intent.to_dict(), ensure_ascii=False)[:600])
    return intent


# ----------------------------------------------------------------------------
# 编排：NL -> 校验 -> Phase 1B 取数（LLM 不参与取数）
# ----------------------------------------------------------------------------
def _clarify(message: str, candidates: Optional[List[str]] = None, stage: str = '') -> Dict[str, Any]:
    return {
        'status': STATUS_CLARIFY,
        'message': message,
        'candidates': candidates or [],
        'stage': stage,
    }


def build_validated_query(
    intent: NlIntent,
    rep: WorkbookRepresentation,
) -> Dict[str, Any]:
    """把意图草图按真实 schema 校验成 Phase 1B 的 payload。解析失败抛 NlQueryError。"""
    sheet, sheet_candidates = resolve_sheet_nl(rep, intent.sheet)
    if sheet is None:
        raise NlQueryError(
            'sheet_ambiguous_or_missing',
            f'无法确定要查询哪个 Sheet（候选：{", ".join(sheet_candidates) or "无"}）',
            {'candidates': sheet_candidates},
        )

    resolved_columns: List[str] = []
    for name in (intent.columns or []):
        col, cands = resolve_column_nl(sheet, name)
        if col is None:
            raise NlQueryError(
                'column_ambiguous_or_missing',
                f'无法确定列「{name}」（候选：{", ".join(cands[:10]) or "无"}）',
                {'column': name, 'candidates': cands[:30]},
            )
        if col not in resolved_columns:
            resolved_columns.append(col)

    resolved_filters: List[Dict[str, Any]] = []
    for f in intent.filters:
        col, cands = resolve_column_nl(sheet, f.get('column'))
        if col is None:
            raise NlQueryError(
                'column_ambiguous_or_missing',
                f'筛选列「{f.get("column")}」无法确定（候选：{", ".join(cands[:10]) or "无"}）',
                {'column': f.get('column'), 'candidates': cands[:30]},
            )
        value = f.get('value')
        operator = f.get('operator') or excel_query.OPERATOR_EQ
        if operator not in excel_query.SUPPORTED_OPERATORS:
            raise NlQueryError(
                'filter_operator_invalid',
                f'不支持的筛选运算符：{operator!r}（可用：{", ".join(excel_query.SUPPORTED_OPERATORS)}）',
                {'column': col, 'operator': operator},
            )
        if operator == excel_query.OPERATOR_CONTAINS:
            if not isinstance(value, str) or value == '':
                raise NlQueryError(
                    'filter_value_invalid',
                    f'筛选条件「{col} 包含 ...」缺少有效的文本值',
                    {'column': col, 'value': value},
                )
            guard_contains_unsafe(sheet, col, value)
        elif operator in excel_query.RANGE_OPERATORS:
            # gt/gte/lt/lte 必须是数值（允许 LLM 写成字符串 "100"）
            num = excel_query.to_number(value)
            if num is None:
                raise NlQueryError(
                    'filter_value_invalid',
                    f'筛选条件「{col} {operator} ?」需要一个数值，收到 {value!r}',
                    {'column': col, 'operator': operator, 'value': value},
                )
            value = num
        resolved_filters.append({'column': col, 'operator': operator, 'value': value})

    payload: Dict[str, Any] = {
        'sheet_index': sheet.sheet_index,
        'filters': resolved_filters,
        'match_mode': excel_query.MATCH_MODE_AND,
        'limit': max(1, min(int(intent.limit or DEFAULT_NL_LIMIT), MAX_NL_LIMIT)),
        'offset': max(0, int(intent.offset or 0)),
    }
    if resolved_columns:
        payload['columns'] = resolved_columns

    # Phase 4B：受控计算字段（真实列必须唯一解析；否则给出可读的澄清/报错）
    if intent.calculation:
        try:
            calc = excel_calculation.ensure_resolved(sheet, intent.calculation)
        except excel_query.ExcelQueryError as e:
            raise NlQueryError(
                'calculation_column_invalid',
                f'无法解析计算字段的列：{e.message}',
                dict(e.details or {}, candidates=sheet.column_names[:30]),
            ) from e
        payload['calculation'] = calc.to_dict()
        if intent.calc_filter:
            payload['calc_filter'] = dict(intent.calc_filter)
    elif intent.calc_filter:
        raise NlQueryError(
            'calculation_required',
            '对计算值筛选前需要先说明计算字段（例如「订单金额 ÷ 数量」）',
            {'calc_filter': intent.calc_filter},
        )
    return payload


def format_summary(result: 'excel_query.StructuredQueryResult', filename: str) -> str:
    """由 Python 生成结果摘要（不经过 LLM，避免编造事实）。"""
    col_names = [c['name'] for c in result.columns]
    head = (
        f'已从「{filename}」的 Sheet「{result.sheet_name}」中执行结构化查询。\n'
        f'- 命中总数：{result.total_matches} 行（该 Sheet 共 {result.total_rows_in_sheet} 行）\n'
        f'- 本次返回：{result.returned_count} 行（offset={result.offset}, limit={result.limit}）\n'
        f'- 返回列：{", ".join(col_names)}'
    )
    # Phase 4B：计算字段与计算值筛选必须显式告知（口径可核对）
    if getattr(result, 'calculation', None):
        calc = result.calculation
        head += (f'\n- 计算字段：{calc["label"]}'
                 f'（对每行「{calc["left_column"]}」与「{calc["right_column"]}」做 {calc["symbol"]} 运算；'
                 f'空值/非数值/除零不计入）')
    if getattr(result, 'calc_filter', None):
        cf = result.calc_filter
        op_label = {'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<=',
                    'eq': '=', 'neq': '≠'}.get(cf['operator'], cf['operator'])
        head += (f'\n- 计算值筛选：{result.calculation["label"]} {op_label} {cf["value"]}'
                 f'（计算值为空的行不匹配任何比较，不会当作 0）')
    if result.row_excel_numbers:
        head += f'\n- Excel 行号：{result.row_excel_numbers[0]} ~ {result.row_excel_numbers[-1]}'
    return head


def format_pagination_summary(result: 'excel_query.StructuredQueryResult', filename: str, mode: str) -> str:
    """由 Python 生成「继续分页」的摘要（不经过 LLM）。"""
    col_names = [c['name'] for c in result.columns]
    mode_label = {
        ACTION_NEXT: '下一页',
        ACTION_PREV: '上一页',
        ACTION_RANGE: '指定区间',
        ACTION_START: '从指定位置开始',
    }.get(mode, '继续')

    effective_offset = result.offset
    if result.returned_count == 0:
        body = (
            f'- 命中总数：{result.total_matches} 行\n'
            f'- 本次返回：0 行 —— 已经超出范围（起点为第 {effective_offset + 1} 条，'
            f'共计 {result.total_matches} 行）'
        )
    else:
        seq_start = effective_offset + 1
        seq_end = effective_offset + result.returned_count
        body = (
            f'- 命中总数：{result.total_matches} 行\n'
            f'- 本次返回：{result.returned_count} 行（第 {seq_start}~{seq_end} 条）\n'
            f'- 返回列：{", ".join(col_names)}'
        )

    head = (
        f'已继续上一张表格查询（{mode_label}）：「{filename}」/ Sheet「{result.sheet_name}」。\n'
        + body
    )
    if result.row_excel_numbers:
        head += f'\n- Excel 行号：{result.row_excel_numbers[0]} ~ {result.row_excel_numbers[-1]}'
    return head


# ============================================================================
# Phase 1D：对话内连续分页 / 追问
# ============================================================================
ACTION_NEXT = 'next'
ACTION_PREV = 'prev'
ACTION_RANGE = 'range'
ACTION_START = 'start'
ACTION_NEW_QUERY = 'new_query'
ACTION_NOT_EXCEL = 'not_excel'
ACTION_CLARIFY = 'clarify'
# Phase 3A：统计动作（COUNT / SUM / AVG / MIN / MAX）
ACTION_AGGREGATE = 'aggregate'
# Phase 4A：多步分析（受控两阶段：分组统计 → TOP-N → 再聚合）
ACTION_ANALYSIS = 'analysis'

# 纯分页动作集合（这些动作必须复用上一轮上下文，不允许重新猜文件/列）
PAGINATION_ACTIONS: Tuple[str, ...] = (ACTION_NEXT, ACTION_PREV, ACTION_RANGE, ACTION_START)
ALL_ACTIONS: Tuple[str, ...] = (
    PAGINATION_ACTIONS
    + (ACTION_NEW_QUERY, ACTION_AGGREGATE, ACTION_ANALYSIS, ACTION_NOT_EXCEL, ACTION_CLARIFY)
)


@dataclass
class AggregateIntent:
    """Phase 3A：一次统计请求的意图草图（未经 schema 校验）。

    - operation 只允许 count/sum/avg/min/max；
    - column 仅数值统计需要（count 固定为 None）；
    - refers_to_previous=True 表示用户在指代上一轮结果（"这些订单"），
      此时 filters/document/sheet 应交由 Python 从上下文继承，LLM 不得编造。
    """

    operation: str = 'count'
    document: Optional[str] = None
    sheet: Optional[str] = None
    column: Optional[str] = None
    filters: List[Dict[str, Any]] = field(default_factory=list)
    refers_to_previous: bool = False
    clarification: str = ''
    # Phase 3B：分组列（空列表 = 单值统计）
    group_by: List[str] = field(default_factory=list)
    # Phase 3C：排序 + TOP-N（未归一化的原始值，由 Python 严格校验后使用）
    order_by: Optional[str] = None
    order_dir: Optional[str] = None
    top_n: Optional[int] = None
    # Phase 4B：受控计算字段（与 column 互斥；用于"计算值参与聚合"）
    calculation: Optional[Dict[str, Any]] = None
    # 计算字段解析失败的原因（**绝不静默丢弃**）
    calc_error: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'operation': self.operation,
            'document': self.document,
            'sheet': self.sheet,
            'column': self.column,
            'group_by': self.group_by,
            'order_by': self.order_by,
            'order_dir': self.order_dir,
            'top_n': self.top_n,
            'filters': self.filters,
            'refers_to_previous': self.refers_to_previous,
            'clarification': self.clarification,
            'calculation': self.calculation,
        }


@dataclass
class AnalysisIntent:
    """Phase 4A：多步分析意图草图。

    - ``steps`` 是 LLM 给出的**原始步骤列表**（每步一个 dict），这里不做严格校验，
      真正的白名单/schema 校验在 :mod:`backend.excel.multi_step` 中完成；
    - ``refers_to_previous=True`` 表示用户在指代上一轮的多步分析结果
      （例如「再算一下这 10 个 SKU 的平均销售额」），此时第 1 步应与上文一致。
    """

    document: Optional[str] = None
    sheet: Optional[str] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)
    refers_to_previous: bool = False
    clarification: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'document': self.document,
            'sheet': self.sheet,
            'steps': self.steps,
            'step_count': len(self.steps),
            'max_steps': excel_multi_step.MAX_STEPS,
            'refers_to_previous': self.refers_to_previous,
            'clarification': self.clarification,
        }


@dataclass
class TurnIntent:
    """一轮对话的动作意图。

    - 分页类动作只带「自然的序号」（第 N 条 / 每页几条），**offset 由 Python 计算**；
    - new_query 时携带完整的查询草图（NlIntent）；
    - aggregate 时携带统计意图（AggregateIntent）。
    """

    action: str = ACTION_NEW_QUERY
    limit: Optional[int] = None
    start_index: Optional[int] = None    # 用户说的「第 N 条」（1-based 数据序号）
    end_index: Optional[int] = None      # 用户说的「到第 M 条」（1-based，含）
    intent: Optional[NlIntent] = None
    aggregate: Optional[AggregateIntent] = None
    # Phase 4A：多步分析计划草图（未经 schema 校验）
    analysis: Optional['AnalysisIntent'] = None
    clarification: str = ''
    source: str = 'llm'                  # llm | deterministic | override
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'action': self.action,
            'limit': self.limit,
            'start_index': self.start_index,
            'end_index': self.end_index,
            'aggregate': self.aggregate.to_dict() if self.aggregate else None,
            'analysis': self.analysis.to_dict() if self.analysis else None,
            'clarification': self.clarification,
            'source': self.source,
        }


@dataclass
class PagePlan:
    """Python 计算出的分页参数。"""

    offset: int = 0
    limit: int = DEFAULT_NL_LIMIT
    mode: str = ACTION_NEXT
    error: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {'offset': self.offset, 'limit': self.limit, 'mode': self.mode, 'error': self.error}


def _positive_int(v: Any) -> Optional[int]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip('+-').isdigit():
        return int(v.strip())
    return None


def _as_text(v: Any) -> Optional[str]:
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return None


def _as_str_list(v: Any) -> List[str]:
    """把单值/数组规约成去重后的字符串数组（列名用）。"""
    if isinstance(v, str):
        v = [v]
    out: List[str] = []
    if isinstance(v, (list, tuple)):
        for item in v:
            name = _as_text(item)
            if name and name not in out:
                out.append(name)
    return out


def normalize_filters(raw_filters: Any) -> List[Dict[str, Any]]:
    """把 LLM 给出的 filters 规约为 [{column, operator, value}]（未知 operator 保留原样由校验层拒绝）。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw_filters, list):
        return out
    for item in raw_filters:
        if not isinstance(item, dict):
            continue
        col = _as_text(item.get('column'))
        if not col:
            continue
        op = (_as_text(item.get('operator')) or excel_query.OPERATOR_EQ).lower()
        if op not in excel_query.SUPPORTED_OPERATORS:
            op = excel_query.OPERATOR_EQ
        out.append({'column': col, 'operator': op, 'value': item.get('value')})
    return out


def aggregate_intent_from_dict(data: Dict[str, Any]) -> AggregateIntent:
    """把 LLM 的 JSON 规约为 AggregateIntent（未知 operation 兜底 count，不抛错）。"""
    raw_op = data.get('aggregate_operation')
    if not _as_text(raw_op):
        raw_op = data.get('operation')
    op = (_as_text(raw_op) or excel_aggregate.OPERATION_COUNT).lower()
    if op not in excel_aggregate.SUPPORTED_OPERATIONS:
        op = excel_aggregate.OPERATION_COUNT

    column = _as_text(data.get('column'))
    if op == excel_aggregate.OPERATION_COUNT:
        column = None

    refers = data.get('refers_to_previous')
    refers_to_previous = bool(refers) if isinstance(refers, bool) else str(refers).strip().lower() in ('true', '1', 'yes')

    # Phase 3B：分组列
    raw_group = data.get('group_by')
    group_by: List[str] = []
    if isinstance(raw_group, str):
        raw_group = [raw_group]
    if isinstance(raw_group, (list, tuple)):
        for g in raw_group:
            name = _as_text(g)
            if name and name not in group_by:
                group_by.append(name)

    # Phase 3C：排序 + TOP-N（这里只做宽松取值，严格校验在编排层）
    raw_top_n = data.get('top_n')
    top_n: Optional[int] = None
    if isinstance(raw_top_n, bool):
        top_n = None
    elif isinstance(raw_top_n, int):
        top_n = raw_top_n
    elif isinstance(raw_top_n, float) and raw_top_n.is_integer():
        top_n = int(raw_top_n)
    elif isinstance(raw_top_n, str) and raw_top_n.strip().isdigit():
        top_n = int(raw_top_n.strip())

    # Phase 4B：计算字段（宽松取值；严格白名单在 aggregate 层）
    calc_raw = None
    calc_error = ''
    if data.get('calculation') is not None:
        try:
            calc_obj = excel_calculation.normalize_calculation(data.get('calculation'))
            if calc_obj is not None:
                calc_raw = calc_obj.to_dict()
        except excel_query.ExcelQueryError as e:
            calc_error = f'计算字段不合法：{e.message}'
            logger.warning('[nl] 统计的计算字段被拒绝：%s', e.message)

    return AggregateIntent(
        operation=op,
        document=_as_text(data.get('document')),
        sheet=_as_text(data.get('sheet')),
        column=column,
        filters=normalize_filters(data.get('filters')),
        refers_to_previous=refers_to_previous,
        clarification=_as_text(data.get('clarification')) or '',
        group_by=group_by,
        order_by=_as_text(data.get('order_by')),
        order_dir=_as_text(data.get('order_dir')),
        top_n=top_n,
        calculation=calc_raw,
        calc_error=calc_error,
    )


def _extract_steps(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 LLM 输出中抽取多步计划（兼容 steps 数组与 step_1/step_2 两种写法）。"""
    raw_steps = data.get('steps')
    if not isinstance(raw_steps, list):
        raw_steps = []
        for key in ('step_1', 'step1', 'stepA', 'step_2', 'step2', 'stepB'):
            if key in data:
                raw_steps.append(data.get(key))
    return [s for s in raw_steps if isinstance(s, dict)]


def analysis_intent_from_dict(data: Dict[str, Any]) -> AnalysisIntent:
    """把 LLM 的 JSON 规约为 AnalysisIntent（只做宽松取值，严格校验在 multi_step 层）。"""
    refers = data.get('refers_to_previous')
    refers_to_previous = (bool(refers) if isinstance(refers, bool)
                          else str(refers).strip().lower() in ('true', '1', 'yes'))
    return AnalysisIntent(
        document=_as_text(data.get('document')),
        sheet=_as_text(data.get('sheet')),
        steps=_extract_steps(data),
        refers_to_previous=refers_to_previous,
        clarification=_as_text(data.get('clarification')) or '',
    )


def turn_intent_from_dict(data: Dict[str, Any], source: str = 'llm') -> TurnIntent:
    """把 LLM 的 JSON 安全规约为 TurnIntent。"""
    raw_action = data.get('action')
    action = raw_action.strip().lower() if isinstance(raw_action, str) else ''
    if action not in ALL_ACTIONS:
        # 兼容：模型可能沿用 Phase 1C 的 query_type 字段
        qt = data.get('query_type')
        if qt == INTENT_NOT_EXCEL:
            action = ACTION_NOT_EXCEL
        elif qt == INTENT_CLARIFY:
            action = ACTION_CLARIFY
        elif qt == INTENT_STRUCTURED:
            action = ACTION_NEW_QUERY
        else:
            action = ACTION_NEW_QUERY

    clarification = data.get('clarification')
    clarification = clarification.strip() if isinstance(clarification, str) else ''

    turn = TurnIntent(
        action=action,
        limit=_positive_int(data.get('limit')),
        start_index=_positive_int(data.get('start_index')),
        end_index=_positive_int(data.get('end_index')),
        clarification=clarification,
        source=source,
        raw=data,
    )
    if action == ACTION_NEW_QUERY:
        turn.intent = intent_from_dict({**data, 'query_type': INTENT_STRUCTURED})
    elif action == ACTION_AGGREGATE:
        turn.aggregate = aggregate_intent_from_dict(data)
    elif action == ACTION_ANALYSIS:
        turn.analysis = analysis_intent_from_dict(data)
    return turn


def compute_page(turn: TurnIntent, ctx: 'ExcelQueryContext') -> PagePlan:
    """**由 Python 计算 offset/limit**（LLM 只说自然语言里的序号）。

    规则：
      next  : offset = ctx.offset + ctx.limit（即"上一页结束位置"）；limit = 指定值或继承
      prev  : offset = max(0, ctx.offset - 每页数量)；limit = 指定值或继承
      range : 第 a~b 条（1-based 含端点）-> offset = a-1, limit = b-a+1
      start : 从第 a 条开始 -> offset = a-1, limit = 指定值或继承
    """
    page_limit = turn.limit or ctx.limit or DEFAULT_NL_LIMIT

    def _clamp(off: int, lim: int) -> PagePlan:
        return PagePlan(offset=max(0, off), limit=max(1, min(lim, MAX_NL_LIMIT)), mode=turn.action)

    if turn.action == ACTION_NEXT:
        return _clamp(ctx.offset + (ctx.limit or DEFAULT_NL_LIMIT), page_limit)

    if turn.action == ACTION_PREV:
        return _clamp(ctx.offset - page_limit, page_limit)

    if turn.action == ACTION_RANGE:
        a, b = turn.start_index, turn.end_index
        if a is None or b is None or a < 1 or b < 1:
            return PagePlan(error='请说明要查看的区间，例如"看第51到100条"。', mode=turn.action)
        if a > b:
            return PagePlan(error=f'区间起点不能大于终点（收到 第{a}~{b}条）。', mode=turn.action)
        span = b - a + 1
        if span > MAX_NL_LIMIT:
            return _clamp(a - 1, MAX_NL_LIMIT)
        return _clamp(a - 1, span)

    if turn.action == ACTION_START:
        a = turn.start_index
        if a is None or a < 1:
            return PagePlan(error='请说明从第几条开始，例如"从第101条开始给我20条"。', mode=turn.action)
        return _clamp(a - 1, page_limit)

    return PagePlan(error=f'无法识别的分页动作：{turn.action}', mode=turn.action)


# ---- 降级路径：LLM 不可用/输出不可用时的严格分页短语解析（有界、可解释）----
_PAGINATION_PATTERNS = [
    # (action, 正则)  —— 顺序有意义：先匹配"区间/起点"，再"上/下页"
    (ACTION_RANGE, re.compile(r'第?\s*(\d+)\s*(?:到|至|~|-|—|－)\s*(\d+)\s*(?:条|行)?')),
    (ACTION_START, re.compile(r'从\s*第?\s*(\d+)\s*(?:条|行)?\s*(?:开始|起)\s*(?:给(?:我|你)?|看|来)?\s*(\d+)?\s*(?:条|行)?')),
    (ACTION_NEXT, re.compile(r'^(?:下(?:一)?页|再(?:来|看)?\s*(\d+)?\s*(?:条|行)?|继续(?:看)?\s*(\d+)?\s*(?:条|行)?|往下(?:再看)?\s*(\d+)?\s*(?:条|行)?|next)$')),
    (ACTION_PREV, re.compile(r'^(?:上(?:一)?页|往前(?:看)?\s*(\d+)?\s*(?:条|行)?|prev)$')),
]


#: 「再看前N个 / 换成前N个 / 只看前N个」——上一轮是**分组统计**时才可解释为改 TOP-N
_TOPN_FOLLOWUP_RE = re.compile(
    r'^\s*(?:再|重新|换(?:成)?|只|就)?\s*(?:看|来|给(?:我)?|要)?\s*前\s*(\d{1,3})\s*(?:个|条|名|组)?\s*$'
)


def deterministic_topn_followup(message: str) -> Optional[int]:
    """严格解析「再看前N个」这类追问（仅在 LLM 返回 clarify 且上一轮是分组统计时使用）。

    后续 2A：实现搬到 `nl_normalize.parse_topn_followup`（支持「再看前五个」）。
    其它任何说法一律返回 None（继续走澄清，不猜）。
    """
    return nl_norm.parse_topn_followup(
        message,
        min_n=excel_aggregate.MIN_TOP_N,
        max_n=excel_aggregate.MAX_TOP_N,
    )


def deterministic_pagination(message: str) -> Optional[TurnIntent]:
    """严格分页短语解析（后续 2A 起同时用于**快路径**与 LLM 降级）。

    只识别有限的、可解释的**整句**分页说法（下一页 / 再来20条 / 第51到100条 /
    从第101条开始给我20条）；不参与任何事实数据生成。
    实现见 `nl_normalize.parse_pagination`（整句匹配，绝不会劫持带新条件的问句）。
    """
    page = nl_norm.parse_pagination(message)
    if page is None:
        return None
    return TurnIntent(
        action=page.action,
        limit=page.limit,
        start_index=page.start_index,
        end_index=page.end_index,
        source='deterministic',
    )


# ---- 统一的「一轮意图」Prompt（一次 LLM 调用同时覆盖新查询与分页）----
CONTINUATION_PROMPT_HEADER = """你是一个「表格对话轮次解析器」。你的唯一职责是判断用户这句话属于下面哪种动作，并输出 JSON。

动作（action）只能是以下 9 种之一：
- "new_query"  ：发起一个**新的**表格查询（用户给出了要查的文件/列/条件，或换了一个问题）
- "next"       ：在上一轮查询结果上**往后翻**（下一页 / 再来20条 / 继续 / 往下看20条）
- "prev"       ：在上一轮查询结果上**往前翻**（上一页 / 往前20条）
- "range"      ：要看**指定的第 a~b 条**（看第51到100条 / 给我51-100条）
- "start"      ：**从第 a 条开始**看（从第101条开始给我20条 / 从第51条开始）
- "aggregate"  ：**一步统计**（有多少 / 几单 / 几条 / 多少条 / 总和 / 合计 / 求和 / 一共多少 / 平均 / 平均值 /
                 均值 / 最大 / 最大值 / 最多 / 最小 / 最小值 / 最少）
- "analysis"   ：**两步分析**（先把数据分组排出前 N 名，**再对这 N 名做一次汇总**）
- "not_excel"  ：与表格查询无关（闲聊、问概念、写代码）
- "clarify"    ：用户想做表格查询，但信息不足以下判断

统计操作（仅 action="aggregate" 时填写 aggregate_operation）：
- "count" ：问**行数/条数**（「有几单」「有多少条」「一共多少个」）-> column 必须为 null
- "sum"   ：**求和/合计**（「订单金额总和」「一共多少钱」）-> 必须给出 column（数值列）
- "avg"   ：**平均/平均值**
- "min"   ：**最小/最小值**
- "max"   ：**最大/最大值**
注意：**你绝不计算任何数字**，只负责把上面 5 种操作之一和列名填出来，真实数字由数据库计算。

分组统计（GROUP BY）：
- 当用户表达「每个 / 各个 / 各 / 按…分别 / 分别统计 / 按…分组 / 分不同…」时，说明要**分组**，
  请把这些"分组维度列"填到 group_by 数组（可以一列或多列，最多 3 列）。
- group_by 是**分组维度**，column 是**被统计的数值列**（count 时为空），两者含义不同，不要混淆。
- **不要把普通统计误判成分组**：「物流商为SF的有几单？」是单值 COUNT，group_by 必须是空数组 []。
  同理「订单金额总和是多少？」「订单金额平均是多少？」都是单值统计，group_by 留空。

计算字段（Phase 4B，**受控**）：
- 只有当用户**要求基于两个真实列做算术**时才填 calculation；**能直接用一列就不要用计算字段**
  （「订单金额是多少？」是直接列 Order Amount，**不是**计算）。
- 只能**一个运算符 + 两个真实列**，operation 只能是：
  "add"(+) / "sub"(−) / "mul"(×) / "div"(÷)。
  **绝不**输出表达式字符串（如 "Order Amount / Quantity"）、**绝不**嵌套（如 (A+B)*C）、
  **绝不**生成 SQL 或调用任何函数（SUM/ROUND/ABS/CASE… 一律不许写进 calculation）。
- 字段固定为 left_column / right_column（真实列名，不是 Excel 列号）。
- 典型映射：
  * 「每笔订单的单价是多少？」-> calculation={"operation":"div","left_column":"Order Amount","right_column":"Quantity"}
  * 「数量乘以单价」-> {"operation":"mul","left_column":"Quantity","right_column":"SKU Unit Original Price"}
  * 「金额减去运费」-> {"operation":"sub", ...}
- **对计算值筛选**：用户说「单价大于10的订单」时，过滤条件写成
  {"column":"calculated_value","operator":"gt","value":10}（`calculated_value` 是固定别名）。
  注意：`filters` 里可以同时有普通列条件（如物流商）与 calculated_value 条件。
- **计算字段参与分组统计**：把 calculation 与 group_by/order_by/top_n 一起给出，
  column 必须留空（calculation 与 column 互斥）。
- **口径歧义必须澄清（硬性）**：计算字段用于**分组聚合**时，本阶段固定语义是
  「先逐行计算、再聚合」（如 AVG(Order Amount ÷ Quantity)）。但如果用户只是说
  「按SKU统计平均单价」「单价最高的10个SKU」这类说法，很可能指的是
  SUM(Order Amount) ÷ SUM(Quantity)（两种结果不同）-> **必须**输出 action="clarify"，
  并在 clarification 里说明两种口径、请用户明确（例如「按每笔订单的单价求平均」）。
  只有当用户明确说了**逐行**措辞（每笔 / 每行 / 每一单 / 逐笔）时，才按上式执行。

排序与 TOP-N（ORDER BY / TOP-N，只作用于**分组统计结果**）：
- order_by（排序依据，只能是下面两类）：
  * 按**统计值**排序（按订单数量/金额/总和/平均/最大/最小 排序或排列）-> 填 "aggregate_value"；
  * 按**分组字段本身**排序（按物流商名称、按状态名称、按字母顺序）-> 填该分组列的名字。
  * 没有排序需求时 order_by 必须是 null（例如「每个物流商分别有多少单？」不要填排序）。
  * **「订单金额最高的N个X」「金额最低的3个X」这类排行，金额指的是该分组的总金额 ->
    用 aggregate_operation="sum"，不要用 min/max**（除非用户明确说"平均/最大单笔/最小单笔"）。
- order_dir（方向）：
  * 「从高到低 / 降序 / 由大到小 / 最多 / 最高 / 最大」-> "desc"
  * 「从低到高 / 升序 / 由小到大 / 最少 / 最低 / 最小」-> "asc"
- top_n（只看前几名，1~200 的整数）：
  * 「前5个 / 最高的5个 / 排名前10 / Top3 / 最多的3个」-> 5 / 5 / 10 / 3 / 3
  * 「哪个X最多/最高/最大」这类只问一个的 -> order_by="aggregate_value", order_dir="desc", top_n=1
- **top_n 必须有排序依据**：如果用户只说「前5个物流商」而没说按什么排序，
  不要猜、不要填 top_n，请改为 action="clarify" 并说明"需要明确排序依据"。
  （例外：如果**上一轮已经是分组排行**，而用户说「再看前10个 / 换成前20个 / 只看前3个」，
   这不是缺排序依据，而是"在上一轮排行上改数量"：请输出 action="aggregate"、top_n=N、
   refers_to_previous=true，group_by/order_by 留空由 Python 继承，**绝不要返回 clarify**。）
- 排序/TOP-N 都只用于**分组**结果；普通列表查询（分页）不涉及排序。

两步分析（action="analysis"，Phase 4A）：
- 触发条件：用户先要「某分组的前 N 名」（排行），**紧接着要求对这 N 名再做一次汇总**，
  典型连接词：「并统计…的总销售额」「并计算这5个物流商的总订单数」「以及它们的合计」。
- 必须输出恰好 **2 步**的 steps 数组（最多 2 步，超过 2 步一律不要输出）：
  * 第 1 步 type="group_aggregate"：按什么分组、对什么列做什么聚合、怎么排序、取前几名；
  * 第 2 步 type="aggregate"：对**第 1 步结果的聚合值**再聚合，
    必须写 source="step_1"、column="aggregate_value"，**不能**写 group_by / order_by / top_n / filters。
- 第 2 步**只能**读第 1 步算出来的每个分组的聚合值（不回原始订单行）。
- 第 2 步的 operation **必须严格按用户的汇总词选择**：总和/合计/加起来/一共/总… → sum；
  平均/均值 → avg；最多/最大 → max；最少/最小 → min；条数/个数 → count。
- 聚合口径示例：
  * 「金额最高的10个SKU，并统计它们的总销售额」→ 第 2 步 sum；
  * 「订单数量最多的5个物流商，并计算这5个物流商的总订单数」→ 第 2 步 sum；
  * 「这10个分组的平均销售额」→ 第 2 步 avg。
- 如果用户要的是「**回到原始订单**再算」（例如「金额最高的10个SKU**对应的所有订单**的平均订单金额是多少」），
  这不在本阶段能力范围内 → 输出 action="clarify" 并说明原因，**绝不猜**。
- 第 1 步的**聚合目标必须二选一且不能都为空**：要么给真实数值列 "column"，
  要么（当聚合目标是表里没有的**逐行计算值**，如「每笔订单的单价 = 订单金额 ÷ 数量」）给 "calculation"
  并把 "column" 留空。**绝不允许** "column":null 同时又没有 "calculation"
  （例如「每笔订单单价平均值最高的5个SKU…」的第 1 步必须是
  {"group_by":["SKU ID"],"operation":"avg","calculation":{"operation":"div",
  "left_column":"Order Amount","right_column":"Quantity"},"order_by":"aggregate_value",
  "order_dir":"desc","top_n":5}），否则该步无法执行、只能澄清。
- 如果用户只要「前 N 名」而**没有**第二步汇总 → 用 action="aggregate"（Phase 3C），不要用 analysis。
- 如果用户说了 3 步以上（「先…再…然后再…」）→ 输出 action="clarify" 说明当前最多支持两步。
- **指代上一轮两步分析**：若上面给出了「上一轮多步分析」，而用户说
  「再算一下这10个的平均销售额 / 换成合计 / 这些的总数是多少」这类只改汇总方式的话，
  请输出 action="analysis"、refers_to_previous=true，并且 **steps 里只放第 2 步**
  （第 1 步的分组、排序、TOP-N 由 Python 继承，**不要**重写第 1 步）。

硬性规则：
1. 只输出 JSON，不要解释、不要 markdown 代码块。
2. 你**绝不能生成任何数据行或统计数字**，也不负责计算 offset/统计值。你只负责判断动作与填参数。
3. 分页类动作（next/prev/range/start）只有在**下面提供了上一轮查询上下文**时才允许使用；没有上下文时必须用 "new_query" 或 "clarify"。
4. 用户说的「第 N 条」是**结果序号**（第 N 条数据），不是 Excel 行号。请原样填 N，不要做任何换算。
5. limit 只在用户**明确说了每页数量**时填写（"再来20条"→limit=20；"下一页"→limit=null 表示沿用上一轮每页数量）。
6. "next"/"prev" 里出现的数字是**每页数量**，不是起点：例如"下一页给我50条" → action="next", limit=50。
7. 用户换了文件、换了列、换了筛选条件的，一律 "new_query"，不要当作翻页。
8. 与表格无关的闲聊 → "not_excel"。
9. **指代上一轮结果**：如果用户在说「这些订单」「刚才那些」「其中」「这批」「它们」，
   或「再看前10个 / 换成前20个 / 只看前3个」这类**在上一轮排行基础上改数量**的说法，
   而**没有**重新给出文件和筛选条件，请设 action="aggregate"、refers_to_previous=true，
   **把 filters 留成空数组、document/sheet/group_by/order_by 留 null**，
   绝对不要自己编造筛选条件（Python 会继承上一轮的条件、分组与排序）。
   如果用户明确给出了新的文件或条件（例如「5店总共有多少单」），refers_to_previous=false，按常规填写。
10. 统计时如果需要筛选（「物流商为SF的有几单」），照常填写 filters（沿用下面第 11 条的 operator 规则）。
11. operator 语义：eq 等于 / neq 不等于 / gt 大于 / gte 大于等于 / lt 小于 / lte 小于等于 / contains 包含。
    数值范围请拆成两个 filter，例如「数量大于10小于50」→
    [{"column":"Quantity","operator":"gt","value":10},{"column":"Quantity","operator":"lt","value":50}]
12. **group_by 只在用户明确表达分组语义时才填写**（每个 / 各个 / 各 / 按…分别 / 分别统计 / 按…分组）；
    其它情况一律填空数组 []。绝不为了"信息更全"而自行添加分组。
13. order_by / order_dir / top_n 同样只在用户**明确要求排序或前 N 名**时才填写；
    其它情况一律为 null。**你绝不生成 SQL、也绝不自己给结果排序**。
14. action="analysis" 时**必须**给出恰好 2 步的 steps，且第 2 步固定是
    {"type":"aggregate","operation":"…","source":"step_1","column":"aggregate_value"}，
    不得携带 group_by / order_by / top_n / filters，也不得引用原始表列名。
    你**绝不生成 SQL、绝不算数字**；真实数字一律由数据库计算。

输出 JSON：
{
  "action": "new_query" | "next" | "prev" | "range" | "start" | "aggregate" | "analysis" | "not_excel" | "clarify",
  "steps": [
    {"type": "group_aggregate", "group_by": ["分组列"], "operation": "sum", "column": "数值列",
     "order_by": "aggregate_value", "order_dir": "desc", "top_n": 10},
    {"type": "aggregate", "operation": "sum", "source": "step_1", "column": "aggregate_value"}
  ],
  "limit": 20 | null,
  "start_index": 51 | null,
  "end_index": 100 | null,
  "document": "文件名或用户的描述（new_query / aggregate 需要时填）",
  "sheet": "Sheet 名或 null",
  "columns": ["列名（仅 new_query 需要，空数组=全部列）"],
  "aggregate_operation": "count" | "sum" | "avg" | "min" | "max" | null,
  "column": "统计目标列名（count 时为 null；sum/avg/min/max 必须是数值列）",
  "group_by": ["分组维度列名（没有分组语义时必须填空数组 []）"],
  "order_by": "aggregate_value" | "分组列名" | null,
  "order_dir": "asc" | "desc" | null,
  "top_n": 5 | null,
  "refers_to_previous": false,
  "filters": [{"column": "列名", "operator": "eq|neq|gt|gte|lt|lte|contains", "value": "值"}],
  "calculation": {"operation": "add|sub|mul|div", "left_column": "真实列名", "right_column": "真实列名"},
  "clarification": "action=clarify 时说明需要澄清什么（一句话）"
}
（calculation 为 null 或省略 = 不使用计算字段；只允许上面这一个固定形状。）

示例（假设目录里有「直邮5店 7.10号订单.xlsx」/ Sheet「OrderSKUList」，
列：Order ID | SKU ID | Quantity | Shipping Provider Name | Variation | Order Amount）：

- 「物流商为SF的有几单？」
  → {"action":"aggregate","aggregate_operation":"count","document":"5店","filters":[{"column":"Shipping Provider Name","operator":"contains","value":"SF"}],"refers_to_previous":false}
- 「直邮一店订单金额总和是多少？」
  → {"action":"aggregate","aggregate_operation":"sum","column":"Order Amount","document":"一店","filters":[],"refers_to_previous":false}
- 「订单金额平均是多少？」
  → {"action":"aggregate","aggregate_operation":"avg","column":"Order Amount","refers_to_previous":false}
- 「订单金额最大是多少？」→ {"action":"aggregate","aggregate_operation":"max","column":"Order Amount"}
- 「数量最小是多少？」   → {"action":"aggregate","aggregate_operation":"min","column":"Quantity"}
- 「5店总共有多少单？」 → {"action":"aggregate","aggregate_operation":"count","document":"5店","refers_to_previous":false}
- 「这些订单的平均订单金额是多少？」
  → {"action":"aggregate","aggregate_operation":"avg","column":"Order Amount","filters":[],"refers_to_previous":true}
- 「他们一共有多少条？」→ {"action":"aggregate","aggregate_operation":"count","filters":[],"refers_to_previous":true}
- 「物流商为SF且Quantity大于1的订单金额总和是多少？」
  → {"action":"aggregate","aggregate_operation":"sum","column":"Order Amount","filters":[{"column":"Shipping Provider Name","operator":"contains","value":"SF"},{"column":"Quantity","operator":"gt","value":1}]}

分组统计（group_by）示例：
- 「每个物流商分别有多少单？」/「各物流商的订单数量是多少？」
  → {"action":"aggregate","aggregate_operation":"count","group_by":["Shipping Provider Name"],"refers_to_previous":false}
- 「每个物流商的订单金额总和是多少？」
  → {"action":"aggregate","aggregate_operation":"sum","column":"Order Amount","group_by":["Shipping Provider Name"]}
- 「按物流商统计平均订单金额」/「各物流商的平均订单金额」
  → {"action":"aggregate","aggregate_operation":"avg","column":"Order Amount","group_by":["Shipping Provider Name"]}
- 「每种订单状态分别有多少条？」
  → {"action":"aggregate","aggregate_operation":"count","group_by":["Order Status"]}
- 「SF物流下每个订单状态分别有多少单？」
  → {"action":"aggregate","aggregate_operation":"count","group_by":["Order Status"],"filters":[{"column":"Shipping Provider Name","operator":"contains","value":"SF"}]}
- 「按支付方式统计订单金额总和」
  → {"action":"aggregate","aggregate_operation":"sum","column":"Order Amount","group_by":["Payment Method"]}
- 「按SKU统计每笔订单单价平均值」（明确逐行 -> 可执行）
  → {"action":"aggregate","aggregate_operation":"avg","group_by":["SKU ID"],
     "calculation":{"operation":"div","left_column":"Order Amount","right_column":"Quantity"}}
- 「按SKU统计平均单价」（口径歧义 -> clarify）
  → {"action":"clarify","clarification":"「平均单价」有 ① 逐行 AVG(订单金额÷数量) 与
     ② SUM(订单金额)÷SUM(数量) 两种口径，请明确。"}
- 「物流商为SF的有几单？」（**单值统计，不要分组**）
  → {"action":"aggregate","aggregate_operation":"count","group_by":[],"filters":[{"column":"Shipping Provider Name","operator":"contains","value":"SF"}]}
- 「这些物流商的平均订单金额是多少？」（继承上一轮分组）
  → {"action":"aggregate","aggregate_operation":"avg","column":"Order Amount","group_by":[],"refers_to_previous":true}

排序 / TOP-N 示例：
- 「哪个物流商订单最多？」
  → {"action":"aggregate","aggregate_operation":"count","group_by":["Shipping Provider Name"],
     "order_by":"aggregate_value","order_dir":"desc","top_n":1}
- 「按订单数量从高到低排列各物流商」
  → {"action":"aggregate","aggregate_operation":"count","group_by":["Shipping Provider Name"],
     "order_by":"aggregate_value","order_dir":"desc","top_n":null}
- 「订单金额最高的5个物流商」
  → {"action":"aggregate","aggregate_operation":"sum","column":"Order Amount",
     "group_by":["Shipping Provider Name"],"order_by":"aggregate_value","order_dir":"desc","top_n":5}
- 「订单金额最低的3个物流商」
  → {"action":"aggregate","aggregate_operation":"sum","column":"Order Amount",
     "group_by":["Shipping Provider Name"],"order_by":"aggregate_value","order_dir":"asc","top_n":3}
- 「按物流商名称升序排列」
  → {"action":"aggregate","aggregate_operation":"count","group_by":["Shipping Provider Name"],
     "order_by":"Shipping Provider Name","order_dir":"asc","top_n":null}
- 「SF物流下订单金额最高的3个订单状态」
  → {"action":"aggregate","aggregate_operation":"sum","column":"Order Amount",
     "group_by":["Order Status"],"order_by":"aggregate_value","order_dir":"desc","top_n":3,
     "filters":[{"column":"Shipping Provider Name","operator":"contains","value":"SF"}]}
- 「每个物流商分别有多少单？」（**没有排序需求**）
  → {"action":"aggregate","aggregate_operation":"count","group_by":["Shipping Provider Name"],
     "order_by":null,"order_dir":null,"top_n":null}
- 「前5个物流商」（**没有排序依据**）
  → {"action":"clarify","clarification":"需要明确按什么排序（例如「订单最多的5个物流商」）。"}
- 「再看前10个」（继承上一轮分组与排序，只把 top_n 改成 10）
  → {"action":"aggregate","aggregate_operation":null,"group_by":[],"order_by":null,
     "order_dir":null,"top_n":10,"refers_to_previous":true}

计算字段（Phase 4B）示例（假设列：Order Amount | Quantity | SKU ID | Shipping Provider Name）：
- 「每笔订单的单价是多少？」
  -> {"action":"new_query","columns":["Order Amount","Quantity"],
      "calculation":{"operation":"div","left_column":"Order Amount","right_column":"Quantity"}}
- 「找出单价大于10的订单有哪些？」
  -> {"action":"new_query","calculation":{"operation":"div","left_column":"Order Amount",
      "right_column":"Quantity"},
      "filters":[{"column":"calculated_value","operator":"gt","value":10}]}
- 「SF物流中单价大于10的订单」
  -> {"action":"new_query","calculation":{"operation":"div","left_column":"Order Amount",
      "right_column":"Quantity"},
      "filters":[{"column":"Shipping Provider Name","operator":"contains","value":"SF"},
                 {"column":"calculated_value","operator":"gt","value":10}]}
- 「按SKU统计每笔订单的单价平均值」（**用户明确说"每笔" -> 可执行**）
  -> {"action":"aggregate","aggregate_operation":"avg","group_by":["SKU ID"],
      "calculation":{"operation":"div","left_column":"Order Amount","right_column":"Quantity"}}
- 「按SKU统计平均单价」（**口径歧义 -> 必须澄清**）
  -> {"action":"clarify","clarification":"「平均单价」有两种口径：① 逐行 AVG(Order Amount ÷ Quantity)；
      ② SUM(Order Amount) ÷ SUM(Quantity)。两者结果不同，请明确（例如「按每笔订单的单价求平均」）。"}
- 「单价最高的10个SKU」（**口径歧义 -> 必须澄清**）
  -> {"action":"clarify","clarification":"「单价最高的10个SKU」需要先确定单价的聚合口径：
      ① 该 SKU 每笔订单单价的平均；② 该 SKU 金额合计 ÷ 数量合计。请明确后我再执行。"}
- 「订单金额是多少？」（**直接用列，不要用计算字段**）
  -> {"action":"new_query","columns":["Order Amount"]}

两步分析（action="analysis"）示例：
- 「找出一店中金额最高的10个SKU，并统计它们的总销售额。」
  → {"action":"analysis","document":"一店","steps":[
      {"type":"group_aggregate","group_by":["SKU ID"],"operation":"sum","column":"Order Amount",
       "order_by":"aggregate_value","order_dir":"desc","top_n":10},
      {"type":"aggregate","operation":"sum","source":"step_1","column":"aggregate_value"}]}
- 「找出订单数量最多的5个物流商，并计算这5个物流商的总订单数。」
  → {"action":"analysis","steps":[
      {"type":"group_aggregate","group_by":["Shipping Provider Name"],"operation":"count",
       "order_by":"aggregate_value","order_dir":"desc","top_n":5},
      {"type":"aggregate","operation":"sum","source":"step_1","column":"aggregate_value"}]}
- 「找出订单金额最高的3个物流商，并计算这3个物流商的平均销售额。」
  → {"action":"analysis","steps":[
      {"type":"group_aggregate","group_by":["Shipping Provider Name"],"operation":"sum",
       "column":"Order Amount","order_by":"aggregate_value","order_dir":"desc","top_n":3},
      {"type":"aggregate","operation":"avg","source":"step_1","column":"aggregate_value"}]}
- 「金额最高的10个SKU对应的所有订单的平均订单金额是多少？」
  → {"action":"clarify","clarification":"本阶段只能对「前 N 名的汇总值」再聚合，暂不支持回到原始订单行重新统计。"}
- 「订单金额最高的5个物流商」（**只有排行、没有第二步汇总**）
  → {"action":"aggregate","aggregate_operation":"sum","column":"Order Amount",
     "group_by":["Shipping Provider Name"],"order_by":"aggregate_value","order_dir":"desc","top_n":5}
- 「先按物流商汇总金额，再取前3，然后按支付方式再分一次」（**超过两步**）
  → {"action":"clarify","clarification":"当前最多支持两步分析（先排行、再对前 N 名汇总一次）。"}

示例（假设已提供上一轮上下文，offset=40, limit=20）：
- "下一页"            → {"action":"next","limit":null}
- "再来20条"          → {"action":"next","limit":20}
- "继续"              → {"action":"next","limit":null}
- "往下再看20条"      → {"action":"next","limit":20}
- "上一页"            → {"action":"prev","limit":null}
- "往前20条"          → {"action":"prev","limit":20}
- "看第51到100条"     → {"action":"range","start_index":51,"end_index":100}
- "给我51-100条"      → {"action":"range","start_index":51,"end_index":100}
- "从第101条开始给我20条" → {"action":"start","start_index":101,"limit":20}
- "从第51条开始"       → {"action":"start","start_index":51,"limit":null}
- "下一页给我50条"     → {"action":"next","limit":50}
- "列出5店前20条SKU"   → {"action":"new_query","document":"5店","columns":["SKU ID"],"limit":20,"offset":0}
"""


def build_turn_messages(
    user_message: str,
    catalog_text: str,
    context: Optional['ExcelQueryContext'],
    analysis_context: Optional['AnalysisContext'] = None,
) -> List[Dict[str, str]]:
    """构造「一轮意图」的 messages；有上下文时附带上一轮查询参数（不含数据行）。"""
    rules = CONTINUATION_PROMPT_HEADER
    if context is None:
        rules += '\n注意：**当前没有上一轮表格查询上下文**，因此 next/prev/range/start 一律不可用。\n'
        ctx_block = '【上一轮查询上下文】\n（无）'
    else:
        cols = ', '.join(context.columns) if context.columns else '（全部列）'
        flt = context.filters or '（无筛选）'
        seq_end = context.offset + context.limit
        ctx_block = (
            '【上一轮查询上下文】\n'
            f'- 文件：{context.filename}\n'
            f'- Sheet：{context.sheet_name}\n'
            f'- 返回列：{cols}\n'
            f'- 筛选条件：{json.dumps(flt, ensure_ascii=False)}\n'
            f'- 上一轮 offset={context.offset}, limit={context.limit}'
            f'（即上一轮展示的是第 {context.offset + 1}~{seq_end} 条）\n'
            f'- 命中总数：{context.total_matches} 行\n'
        )
    if analysis_context is not None:
        ctx_block += '\n' + _analysis_context_block(analysis_context)

    return [
        {'role': 'system', 'content': rules},
        {
            'role': 'user',
            'content': (
                f'【可用表格目录】\n{catalog_text}\n\n'
                f'{ctx_block}\n'
                f'【用户这句话】\n{user_message}\n\n'
                f'请输出 JSON。'
            ),
        },
    ]


def _analysis_context_block(ctx: 'AnalysisContext') -> str:
    """把「上一轮两步分析」写进 prompt（只含参数，不含数据行）。"""
    steps = (ctx.plan or {}).get('steps') or []
    step1 = steps[0] if steps else {}

    def _txt(key, default=''):
        value = step1.get(key)
        if isinstance(value, (list, tuple)):
            return '、'.join(str(v) for v in value) if value else default
        return str(value) if value not in (None, '') else default

    top_n = step1.get('top_n')
    return (
        '【上一轮多步分析（两步）】\n'
        f'- 文件：{ctx.filename}\n'
        f'- Sheet：{ctx.sheet_name}\n'
        f'- 第 1 步：按「{_txt("group_by")}」分组，操作 {_txt("operation")}'
        + (f'，目标列 {_txt("column")}' if step1.get('column') else '')
        + (f'，排序 {_txt("order_by")} {_txt("order_dir")}' if step1.get('order_by') else '')
        + (f'，取前 {top_n} 名' if top_n else '')
        + '\n'
        f'- 第 2 步：{ctx.step2_operation} → {ctx.step2_value}\n'
        f'- 用户说「这 {top_n or ctx.step2_input_rows} 个 / 这些 / 上述」时，指的是**第 1 步的 TOP-N 结果**；'
        f'此时请输出 action="analysis"、refers_to_previous=true，'
        f'并且 **steps 里只给第 2 步**（第 1 步由 Python 继承，不要重写）。\n'
    )


async def llm_parse_turn(
    llm,
    user_message: str,
    catalog: List[Dict[str, Any]],
    context: Optional['ExcelQueryContext'],
    analysis_context: Optional['AnalysisContext'] = None,
) -> TurnIntent:
    """一次 LLM 调用得到「本轮动作 + 参数」（不取数）。"""
    messages = build_turn_messages(user_message, build_catalog_text(catalog), context,
                                   analysis_context)
    parts: List[str] = []
    # 意图分类必须可复现：固定 temperature=0.0（单次覆盖，不影响该实例的聊天行为）
    async for chunk in llm.chat_completion(messages, stream=False, temperature=0.0):
        if chunk:
            parts.append(chunk)
    data = parse_llm_json(''.join(parts))
    turn = turn_intent_from_dict(data)
    logger.info('[nl] turn=%s', json.dumps(turn.to_dict(), ensure_ascii=False)[:600])
    return turn


# ============================================================================
# 统计意图兜底（Phase 3 路由加固）
# ----------------------------------------------------------------------------
# 背景：一轮对话的路由完全依赖 LLM 给出的 action。若 LLM 把「订单金额总和是多少」
# 这类**统计问题**判成了 action="new_query"，用户就会看到"普通表格查询直接列出原始行"，
# 而 Phase 3A/3B/3C 的统计链路根本不会被触发。
#
# 对策（确定性 + 不猜测，双层）：
#   1) `looks_like_statistical_query()`：**纯 Python 词元判定**"这句话是不是在要统计"，
#      保守触发（出现普通列表/分页说法时一律不触发）。
#   2) 触发后**再给 LLM 一次"只抽统计参数"的机会**（temperature=0），
#      成功则改走 aggregate 链路；失败/无法解析则**原样回退**普通查询（绝不劣化）。
# 该兜底只改变"路由到哪个执行器"，不生成任何数据。
# ============================================================================

#: 求和语义
_STAT_SUM_RE = re.compile(r'总和|合计|总额|总计|求和|加起来|一共多少(?:钱|元)|总销售额|总金额|销量总计|金额总计')
#: 平均语义
_STAT_AVG_RE = re.compile(r'平均|均值')
#: 最小值语义
_STAT_MIN_RE = re.compile(r'最小|最低|最少')
#: 最大值语义（含"最多/最高"；与 TOP-N 的区分交给第二层 LLM 抽取）
_STAT_MAX_RE = re.compile(r'最大|最高|最多')
#: 计数语义
_STAT_COUNT_RE = re.compile(r'有多少|几单|几笔|几条|几个|多少条|多少单|多少行|数量是多少|行数')
#: 分组语义
_STAT_GROUP_RE = re.compile(r'每个|各个|各|分别|每种|每类|按.{0,12}(?:统计|分组|汇总|汇总|求和)|分组')
#: 排序语义
_STAT_ORDER_RE = re.compile(r'从高到低|从低到高|由大到小|由小到大|升序|降序|排序|排列|排名|排行')
#: TOP-N 语义（2A-P0：补充中文"名次/排行"写法）
_STAT_TOPN_RE = re.compile(
    r'(?:排名|排行)\s*前\s*\d+'
    r'|(?:排名|排行)\s*第\s*\d+'
    r'|第\s*\d+\s*(?:名|位)'
    r'|前\s*\d+\s*(?:名|位)'
    r'|前\s*\d+\s*(?:个|条|名|组|位|只|项|款|种|的)?'
    r'|top\s*\d+'
    r'|最高的\s*\d+\s*个|最低的\s*\d+\s*个',
    re.IGNORECASE,
)

#: 明显的"普通列表查询 / 分页"说法：出现这些时**不做**统计兜底
_PLAIN_LIST_RE = re.compile(
    r'有哪些|有哪几个|列出|列一下|列举|显示|展示|看看|查看|给我看|查看一下|筛选|过滤'
    r'|下一页|上一页|再来\s*\d+\s*条|继续看|往下看|往前看'
)


def looks_like_statistical_query(message: str) -> bool:
    """确定性判定：这句话是否**明确要求统计**（只看词元，不调用 LLM）。

    后续 2A：实现搬到 `nl_normalize.looks_statistical`（统一词元表 + 支持中文数字，
    例如「前五个」），此处保留同名薄封装，保证既有调用点与测试不受影响。

    设计原则（宁可漏判、不可误判）：
    - 出现普通列表/分页说法（"有哪些 / 列出 / 显示 / 下一页"）且没有统计词 -> False；
    - 出现求和/平均/最小/最大/计数语义 -> True；
    - 只出现分组 + （排序/TOP-N）组合 -> True；
    - 只出现排序/TOP-N（例如"列出前20条SKU"）-> False（普通列表查询）。
    """
    return nl_norm.looks_statistical(message)


#: 「只抽统计参数」的专用 Prompt（temperature=0；LLM 仍不生成 SQL、不计算数字）
AGGREGATE_GUARD_PROMPT = """你是一个「统计参数抽取器」。用户这句话属于**统计 / 分组统计 / 排序排行**需求（不是普通列表查询）。
你的唯一职责是抽取统计参数并输出 JSON；**绝不生成 SQL、绝不计算或编造任何数字**。

aggregate_operation（必填，五选一）：
- "count"：问行数 / 条数 / 单数 / 个数（「有多少单」「一共几条」）-> column 必须为 null
- "sum"  ：求和 / 合计 / 总额 / 总金额 / 总销售额 -> 必须给 column（数值列）
- "avg"  ：平均 / 均值 -> 必须给 column
- "min"  ：最小 / 最低 / 最少 -> 必须给 column
- "max"  ：最大 / 最高 / 最多（**单值最大**，如「订单金额最高是多少」）-> 必须给 column

column：被统计的**数值列**的真实列名（count 时必须是 null）。

group_by（分组维度，最多 3 列）：
- 出现「每个 / 各个 / 各 / 分别 / 每种 / 每类 / 按…统计 / 分组」时填写；
- 否则必须填空数组 []。
- **「某个条件下的订单有几单」是单值统计，group_by 必须是 []。**

order_by / order_dir / top_n（只作用于分组统计）：
- order_by：按**统计值**排序 -> "aggregate_value"；按**分组字段本身**排序 -> 填该分组列名；没有排序需求 -> null。
- order_dir："从高到低 / 降序 / 由大到小 / 最多 / 最高 / 最大" -> "desc"；"从低到高 / 升序 / 由小到大 / 最少 / 最低 / 最小" -> "asc"。
- top_n：明确说「前 N 个 / 最高的 N 个 / 排名前 N / TopN」时填 N（1~200 整数），否则 null。
- 「哪个 X 最多 / 最高 / 最大」（只问一个）-> order_by="aggregate_value", order_dir="desc", top_n=1。
- **「X 金额最高的 N 个 Y」= 先按 Y 分组求 X 之和再排行**，用 aggregate_operation="sum" + group_by=[Y] + top_n=N，**不要用 max**。
- 只说「前 N 个 X」而没有任何排序依据时：order_by/order_dir/top_n 全部留 null（由 Python 决定是否澄清）。

filters：筛选条件，operator 只能取 eq/neq/gt/gte/lt/lte/contains。
document / sheet：用户提到的文件名 / Sheet 名；没提到就 null。

calculation（可选，受控计算字段）：只有当用户要求**基于两个真实列做算术**时才填：
{"operation":"add|sub|mul|div","left_column":"真实列名","right_column":"真实列名"}。
- 只允许一个运算符 + 两个真实列；**绝不**输出表达式字符串、嵌套公式或任何函数；
- 给出 calculation 时 **column 必须为 null**；COUNT 不需要 calculation；
- 口径歧义：若用户只说「按X统计平均单价 / 单价最高的N个X」而**没有**逐行措辞
  （每笔/每行/每一单/逐笔），他可能想表达 SUM(A)÷SUM(B) 而不是 AVG(A÷B)
  -> 必须输出 clarify 说明两种口径，不要擅自选择；只有明确逐行时才用 calculation。

只输出 JSON（不要解释、不要 markdown）：
{"aggregate_operation":"count|sum|avg|min|max","column":"列名或null","group_by":[],
 "order_by":"aggregate_value|分组列名|null","order_dir":"asc|desc|null","top_n":5,
 "document":null,"sheet":null,"filters":[],"refers_to_previous":false}

示例：
- 「直邮一店订单金额总和是多少？」
  -> {"aggregate_operation":"sum","column":"Order Amount","group_by":[],"document":"直邮一店"}
- 「每个物流商分别有多少单？」
  -> {"aggregate_operation":"count","column":null,"group_by":["Shipping Provider Name"]}
- 「一店哪个物流商订单最多？」
  -> {"aggregate_operation":"count","column":null,"group_by":["Shipping Provider Name"],
      "order_by":"aggregate_value","order_dir":"desc","top_n":1}
- 「一店按订单数量从高到低排列各物流商。」
  -> {"aggregate_operation":"count","column":null,"group_by":["Shipping Provider Name"],
      "order_by":"aggregate_value","order_dir":"desc"}
- 「一店SF物流下订单金额最高的3个支付方式是什么？」
  -> {"aggregate_operation":"sum","column":"Order Amount","group_by":["Payment Method"],
      "order_by":"aggregate_value","order_dir":"desc","top_n":3,
      "filters":[{"column":"Shipping Provider Name","operator":"contains","value":"SF"}]}
- 「找出一店中金额最高的10个SKU，并统计它们的总销售额。」
  -> {"aggregate_operation":"sum","column":"Order Amount","group_by":["SKU ID"],
      "order_by":"aggregate_value","order_dir":"desc","top_n":10}
"""


def build_aggregate_messages(
    user_message: str,
    catalog_text: str,
    context: Optional['ExcelQueryContext'],
) -> List[Dict[str, str]]:
    """构造「统计参数抽取」的 messages（只带 schema 与上一轮参数，不含数据行）。"""
    rules = AGGREGATE_GUARD_PROMPT
    if context is None:
        ctx_block = '【上一轮查询上下文】\n（无）'
    else:
        cols = ', '.join(context.columns) if context.columns else '（全部列）'
        ctx_block = (
            '【上一轮查询上下文】\n'
            f'- 文件：{context.filename}\n'
            f'- Sheet：{context.sheet_name}\n'
            f'- 返回列：{cols}\n'
            f'- 筛选条件：{json.dumps(context.filters or [], ensure_ascii=False)}\n'
        )
    return [
        {'role': 'system', 'content': rules},
        {
            'role': 'user',
            'content': (
                f'【可用表格目录】\n{catalog_text}\n\n'
                f'{ctx_block}\n'
                f'【用户这句话】\n{user_message}\n\n'
                f'请输出 JSON。'
            ),
        },
    ]


async def llm_parse_aggregate(
    llm,
    user_message: str,
    catalog: List[Dict[str, Any]],
    context: Optional['ExcelQueryContext'],
) -> Optional[AggregateIntent]:
    """统计意图兜底的第二次 LLM 调用（temperature=0，只抽统计参数）。

    任何失败（网络 / JSON 不合法 / 模型不可用）都返回 None，由调用方回退原路径，
    绝不因为兜底而让原本可用的普通查询失败。
    """
    try:
        messages = build_aggregate_messages(user_message, build_catalog_text(catalog), context)
        parts: List[str] = []
        async for chunk in llm.chat_completion(messages, stream=False, temperature=0.0):
            if chunk:
                parts.append(chunk)
        data = parse_llm_json(''.join(parts))
    except Exception as e:  # noqa: BLE001 - 兜底失败不影响主链路
        logger.warning('[nl] 统计意图兜底解析失败（将回退普通查询）：%s', e)
        return None

    agg = aggregate_intent_from_dict(data)
    logger.info('[nl] statistical_guard intent=%s raw=%s',
                json.dumps(agg.to_dict(), ensure_ascii=False)[:400],
                json.dumps(data, ensure_ascii=False)[:400])
    return agg


def _repair_turn_with_signals(turn: Optional[TurnIntent],
                              signals: Optional['nl_norm.IntentSignals']) -> List[str]:
    """用**确定性信号**补齐 LLM 漏掉的参数（后续 2A）。

    铁律：**只填空，不覆盖**；且只在文本唯一确定时才补。任何补不上的情况一律留给
    后续的严格校验/澄清，绝不让规则去猜。
    覆盖的场景：
    - 分组统计里 `order_dir` 缺失但文本明确（从高到低 / 最少 …）；
    - 分组统计里 `top_n` 缺失但文本明确（前5个 / 最高的5个）→ 同时补排序键与方向；
    - 普通查询里 `limit` 缺失但文本明确说了「前N条 / N行」→ 补 limit。
    """
    notes: List[str] = []
    if turn is None or signals is None:
        return notes

    if turn.action == ACTION_AGGREGATE and turn.aggregate is not None:
        agg = turn.aggregate
        grouped = bool(agg.group_by) or agg.refers_to_previous
        # 「订单数量 / 单量 / 笔数」= **订单条数**（count），不是某个叫「数量」的列之和。
        # 仅在没有求和/平均/最值说法、且处于"按它排行/分组"的语境时纠正。
        if (signals.order_count_word
                and agg.operation != excel_aggregate.OPERATION_COUNT
                and not (signals.sum_ or signals.avg or signals.min_ or signals.max_)
                and (grouped or signals.group or signals.order_dir or signals.top_n is not None)):
            agg.operation = excel_aggregate.OPERATION_COUNT
            agg.column = None
            agg.calculation = None
            notes.append('operation=count（「订单数量」= 订单条数）')
        if not agg.order_dir and signals.order_dir and (agg.order_by or grouped):
            agg.order_dir = signals.order_dir
            notes.append(f'order_dir={agg.order_dir}')
        if (agg.top_n is None and signals.top_n is not None and grouped
                and signals.topn_unit != 'row'):
            agg.top_n = signals.top_n
            notes.append(f'top_n={agg.top_n}')
            if not agg.order_dir and signals.order_dir:
                agg.order_dir = signals.order_dir
                notes.append(f'order_dir={agg.order_dir}')
            if not agg.order_by and signals.order_dir:
                # 文本已明确"最高的前N个"→ 按聚合值排行（与 LLM 提示词口径一致）
                agg.order_by = excel_aggregate.ORDER_BY_AGGREGATE
                notes.append(f'order_by={agg.order_by}')
        return notes

    if turn.action == ACTION_NEW_QUERY and turn.intent is not None:
        intent = turn.intent
        if (signals.plain_list and signals.top_n is not None
                and signals.topn_unit == 'row'
                and not intent.limit_explicit):
            intent.limit = max(1, min(signals.top_n, MAX_NL_LIMIT))
            intent.limit_explicit = True
            notes.append(f'limit={intent.limit}')
        return notes
    return notes


def _plain_list_downgrade(turn: Optional[TurnIntent],
                          signals: Optional['nl_norm.IntentSignals']) -> Tuple[Optional[TurnIntent], str]:
    """把被误判为统计的**纯列表问题**降级回普通查询（后续 2A，§18）。

    例：「有哪些物流商？」「列出所有SKU」「有哪些订单金额超过10？」——
    它们只是要看数据行，不应变成 COUNT/GROUP BY。

    触发条件（全部满足才降级，宁可漏判）：
    - LLM 给出 aggregate；
    - 文本是纯展示/列表说法（有哪些/列出/显示…）；
    - 没有任何统计词（求和/平均/最值/计数）与分组语义；
    - TOP-N 要么没出现，要么是"行"量词（前20条）。

    降级方式：复用 LLM 同一份 JSON 的 document/sheet/filters/columns 构造普通查询意图，
    因此不会丢失用户的筛选与列要求；失败则保持原动作（不劣化）。
    """
    if turn is None or signals is None or turn.action != ACTION_AGGREGATE or turn.aggregate is None:
        return turn, ''
    if not signals.plain_list or signals.any_stat or signals.group:
        return turn, ''
    # 量词是"个/名/组"（或没写量词）时一律按分组排行处理，不退化成列表查询
    if signals.top_n is not None and signals.topn_unit != 'row':
        return turn, ''
    raw = turn.raw if isinstance(turn.raw, dict) else {}
    if not raw:
        return turn, ''
    # 注意：`raw` 是 LLM 的**原始** JSON；后面步骤（确定性补全）可能已经往
    # aggregate 对象里补了 document/sheet/filters。必须以**修补后**的对象为准，
    # 否则「物流商为SF的订单有哪些？」补上的 SF 筛选会在降级时被丢掉（真实故障）。
    agg = turn.aggregate
    merged = {
        **raw,
        'filters': agg.filters or raw.get('filters') or [],
        'document': agg.document or raw.get('document'),
        'sheet': agg.sheet or raw.get('sheet'),
    }
    intent = intent_from_dict({**merged, 'query_type': INTENT_STRUCTURED})
    downgraded = TurnIntent(action=ACTION_NEW_QUERY, intent=intent,
                            source='deterministic_downgrade', raw=merged)
    return downgraded, 'plain-list → new_query'


def describe_executor(outcome: Dict[str, Any]) -> str:
    """把一次 NL 结果映射到**实际执行的执行器名**（用于日志与测试证据）。"""
    if outcome.get('multi_step'):
        return 'run_analysis'
    if outcome.get('group_aggregate'):
        return 'run_group_aggregate'
    if outcome.get('aggregate'):
        return 'run_aggregate'
    if outcome.get('pagination'):
        return 'run_pagination'
    if outcome.get('result'):
        return 'run_structured_query'
    return ''


# ============================================================================
# Phase 4A：两步分析的确定性判定与兜底抽取
# ----------------------------------------------------------------------------
# 与 Phase 3 的统计兜底同一思路：先用**纯 Python 词元**判定"这句话是不是两步分析"，
# 命中则再给 LLM 一次「只输出两阶段计划」的机会（temperature=0）；
# 成功就走多步链路，失败则原样回退（绝不劣化原行为）。
# ============================================================================

#: 排行/前 N 名语义
_MULTI_STEP_RANK_RE = re.compile(
    r'最高|最低|最多|最少|最大|最小|前\s*\d+\s*(?:个|条|名|组|位|的)?|排名|排行|top\s*\d+'
)
#: 第二步的"汇总"语义（必须是明确的汇总词，避免把普通排行误判为两步）
_MULTI_STEP_AGG_RE = re.compile(
    r'总(?:销售额|金额|订单数|数量|个数|和|额|计|数)|合计|加起来|求和|汇总|平均'
)
#: 两步之间的连接词（必须是真的"接着再做一步"的连接词；
#: 普通的逗号「，」不算 —— 否则「…平均值，从高到低排列，取前5个」会被误判为两步）
_MULTI_STEP_CONNECT_RE = re.compile(r'并|再|然后|以及|同时|之后|并且|还要')

#: 追问时指代上一轮 TOP-N 结果的说法
_ANALYSIS_REF_RE = re.compile(r'这\s*\d*\s*(?:个|条|名|组)|这些|上述|前面|刚刚|它们|他们|这批')


def looks_like_analysis_followup(message: str) -> bool:
    """确定性判定：是否在**指代上一轮两步分析的 TOP-N 结果**做追问。

    仅在上一轮确实是两步分析（有 AnalysisContext）时才有意义，
    用于「再算一下这 10 个的平均销售额」这类说明被 LLM 判成 clarify 时的兜底。
    """
    text = (message or '').strip()
    if not text:
        return False
    if _PLAIN_LIST_RE.search(text):
        return False
    return bool(_ANALYSIS_REF_RE.search(text) and _MULTI_STEP_AGG_RE.search(text))


def looks_like_multi_step_query(message: str) -> bool:
    """确定性判定：这句话是否是「先排行、再对前 N 名汇总一次」的两步分析。

    保守触发（宁可漏判、不可误判）：必须同时出现
    【排行或前 N 名】+【汇总词】+【连接词】三者。
    后续 2A：实现搬到 `nl_normalize.looks_multi_step`（TOP-N 支持中文数字，
    例如「找出金额最高的十个SKU，并统计它们的总销售额」）。
    """
    return nl_norm.looks_multi_step(message)


#: 「只输出两阶段计划」的专用 Prompt（temperature=0；LLM 仍不生成 SQL、不计算数字）
ANALYSIS_PLAN_PROMPT = """你是一个「两阶段分析计划生成器」。用户这句话是**两步分析**需求：
先把数据**分组排出前 N 名**，**再对这 N 名做一次汇总**。
你的唯一职责是输出两阶段计划 JSON；**绝不生成 SQL、绝不计算或编造任何数字**。

最多 2 步（超过 2 步不要输出）：

第 1 步 {"type":"group_aggregate", ...}：
- group_by：分组维度列名（1~3 列）
- operation：count / sum / avg / min / max
- column：被统计的数值列名（count 时为 null）
- calculation（可选，Phase 4B 受控计算字段）：**只能一个运算符 + 两个真实列**
  {"operation":"add|sub|mul|div","left_column":"真实列名","right_column":"真实列名"}；
  给出 calculation 时 column 必须省略；**绝不**输出表达式字符串或嵌套公式。
- order_by："aggregate_value"（按统计值排序）或分组列名
- order_dir："desc"（从高到低）或 "asc"（从低到高）
- top_n：前几名（1~200 的整数）
- 口径歧义（硬性）：若用户只说「按X统计平均单价 / 单价最高的N个X」而没有明确的**逐行**措辞
  （每笔 / 每行 / 每一单 / 逐笔），说明他可能指 SUM(A)÷SUM(B)，与 AVG(A÷B) 结果不同
  -> 输出 {"action":"clarify", ...} 说明两种口径，不要擅自选择。只有明确逐行时才用 calculation。
第 2 步 {"type":"aggregate", ...}：
- operation：对第 1 步结果的**聚合值**再聚合（sum / avg / count / min / max）
- **第 2 步的 operation 必须严格按用户的汇总词选择，不要凭感觉**：
  总和 / 合计 / 加起来 / 一共 / 总… → "sum"；
  平均 / 均值 → "avg"；最多 / 最大 → "max"；最少 / 最小 → "min"；条数 / 个数 → "count"。
- **必须**写 "source":"step_1"、"column":"aggregate_value"
- **不能**写 group_by / order_by / top_n / filters（第 2 步只做一次汇总）

硬性规则：
- 若上面给出了「上一轮多步分析」，而用户只是换一种汇总方式
  （「再算一下这10个的平均销售额」「换成合计」），则 **steps 里只放第 2 步**即可
  （第 1 步由 Python 继承），不要重写第 1 步。
- 第 2 步的数据来源**只能是第 1 步的结果**，不回到原始订单行。
- 如果用户要的是「回到原始订单重新统计」（例如「前 10 个 SKU **对应的所有订单**的平均金额」），
  不要勉强套用，改为输出 {"action":"clarify","clarification":"…原因…"}。
- 如果用户要 3 步以上，改为 clarify 并说明当前最多两步。
- 只输出 JSON。

输出 JSON：
{"document": "文件名或 null", "sheet": "Sheet 名或 null", "steps": [第1步, 第2步]}

示例：
- 「找出金额最高的10个SKU，并统计它们的总销售额。」
  -> {"steps":[{"type":"group_aggregate","group_by":["SKU ID"],"operation":"sum",
      "column":"Order Amount","order_by":"aggregate_value","order_dir":"desc","top_n":10},
      {"type":"aggregate","operation":"sum","source":"step_1","column":"aggregate_value"}]}
- 「找出订单数量最多的5个物流商，并计算这5个物流商的总订单数。」
  -> {"steps":[{"type":"group_aggregate","group_by":["Shipping Provider Name"],"operation":"count",
      "order_by":"aggregate_value","order_dir":"desc","top_n":5},
      {"type":"aggregate","operation":"sum","source":"step_1","column":"aggregate_value"}]}
- 「找出订单金额最高的3个物流商，并计算这3个物流商的平均销售额。」
  -> {"steps":[{"type":"group_aggregate","group_by":["Shipping Provider Name"],"operation":"sum",
      "column":"Order Amount","order_by":"aggregate_value","order_dir":"desc","top_n":3},
      {"type":"aggregate","operation":"avg","source":"step_1","column":"aggregate_value"}]}
- 「找出每笔订单单价平均值最高的5个SKU，并计算这5个SKU的单价平均值合计。」（含受控计算字段）
  -> {"steps":[{"type":"group_aggregate","group_by":["SKU ID"],"operation":"avg",
      "calculation":{"operation":"div","left_column":"Order Amount","right_column":"Quantity"},
      "order_by":"aggregate_value","order_dir":"desc","top_n":5},
      {"type":"aggregate","operation":"avg","source":"step_1","column":"aggregate_value"}]}
"""


def build_analysis_messages(
    user_message: str,
    catalog_text: str,
    context: Optional['ExcelQueryContext'],
    analysis_context: Optional['AnalysisContext'] = None,
) -> List[Dict[str, str]]:
    """构造「两阶段计划抽取」的 messages（只带 schema 与上一轮参数，不含数据行）。"""
    if context is None:
        ctx_block = '【上一轮查询上下文】\n（无）'
    else:
        ctx_block = (
            '【上一轮查询上下文】\n'
            f'- 文件：{context.filename}\n'
            f'- Sheet：{context.sheet_name}\n'
        )
    if analysis_context is not None:
        ctx_block += '\n' + _analysis_context_block(analysis_context)
    return [
        {'role': 'system', 'content': ANALYSIS_PLAN_PROMPT},
        {
            'role': 'user',
            'content': (
                f'【可用表格目录】\n{catalog_text}\n\n'
                f'{ctx_block}\n'
                f'【用户这句话】\n{user_message}\n\n'
                f'请输出 JSON。'
            ),
        },
    ]


async def llm_parse_analysis(
    llm,
    user_message: str,
    catalog: List[Dict[str, Any]],
    context: Optional['ExcelQueryContext'],
    analysis_context: Optional['AnalysisContext'] = None,
) -> Optional[AnalysisIntent]:
    """两步分析的兜底抽取（temperature=0）。任何失败都返回 None，由调用方回退。"""
    try:
        messages = build_analysis_messages(user_message, build_catalog_text(catalog), context,
                                          analysis_context)
        parts: List[str] = []
        async for chunk in llm.chat_completion(messages, stream=False, temperature=0.0):
            if chunk:
                parts.append(chunk)
        data = parse_llm_json(''.join(parts))
    except Exception as e:  # noqa: BLE001 - 兜底失败不影响主链路
        logger.warning('[nl] 两步分析兜底抽取失败（将回退）：%s', e)
        return None

    if (_as_text(data.get('action')) or '').lower() == ACTION_CLARIFY:
        logger.info('[nl] 两步分析兜底判定为 clarify：%s', data.get('clarification'))
        return None
    intent = analysis_intent_from_dict(data)
    logger.info('[nl] analysis_guard intent=%s raw=%s',
                json.dumps(intent.to_dict(), ensure_ascii=False)[:400],
                json.dumps(data, ensure_ascii=False)[:400])
    return intent


def _build_relaxed_payload(
    rep: WorkbookRepresentation, sheet_index: int, payload: Dict[str, Any]
) -> Optional[Tuple[Dict[str, Any], List[str]]]:
    """Phase 2 安全网：eq 命中 0 行时，把「唯一前缀」的等值条件放宽为包含。

    触发条件（全部满足才放宽，且必须能真正取到数据才生效）：
      * 该条件 operator 是 eq，值是**非空字符串**；
      * 该列**不存在**与值完全相等的单元格；
      * 该列中以该值为**前缀**的不同真实值**恰好只有 1 个**（唯一，无歧义）。

    这是一条确定性规则（不依赖 LLM、不做模糊猜测），并且放宽行为会在
    响应 message 中明确告知用户，绝不静默改变语义。
    """
    filters = payload.get('filters') or []
    if not filters:
        return None
    sheet = rep.sheets[sheet_index]
    names = sheet.column_names

    new_filters: List[Dict[str, Any]] = []
    notes: List[str] = []
    changed = False

    for f in filters:
        op = (f.get('operator') or '').lower()
        value = f.get('value')
        if op != excel_query.OPERATOR_EQ or not isinstance(value, str) or value.strip() == '':
            new_filters.append(f)
            continue
        if f.get('column') not in names:
            new_filters.append(f)
            continue

        ci = names.index(f['column'])
        cells = [str(r[ci]) for r in sheet.rows if ci < len(r) and r[ci] is not None]
        has_exact = any(c == value for c in cells)
        prefixes = {c for c in cells if len(c) > len(value) and c.lower().startswith(value.lower())}

        if not has_exact and len(prefixes) == 1:
            only = next(iter(prefixes))
            new_filters.append({**f, 'operator': excel_query.OPERATOR_CONTAINS, 'value': value})
            notes.append(
                f'「{f["column"]}」中没有与「{value}」完全相等的值；'
                f'已按前缀放宽为「包含 {value}」（该列唯一符合的值是「{only}」）'
            )
            changed = True
        else:
            new_filters.append(f)

    if not changed:
        return None
    return {**payload, 'filters': new_filters}, notes


def _execute_with_prefix_relaxation(
    rep: WorkbookRepresentation, payload: Dict[str, Any]
) -> Tuple[Dict[str, Any], 'excel_query.StructuredQueryResult', str, List[str]]:
    """先按原条件执行；若 0 行，再尝试「唯一前缀」放宽并重试。

    返回 (最终生效的 payload, 结果, 引擎名, 放宽说明列表)。
    """
    result, engine_used = query_engine.run_structured_query(rep, payload)
    if result.total_matches > 0:
        return payload, result, engine_used, []

    relaxed = _build_relaxed_payload(rep, result.sheet_index, payload)
    if relaxed is None:
        return payload, result, engine_used, []

    relaxed_payload, notes = relaxed
    result2, engine2 = query_engine.run_structured_query(rep, relaxed_payload)
    if result2.total_matches == 0:
        # 放宽也没取到数据 -> 保留原结果，不做任何"看起来对"的改动
        return payload, result, engine_used, []
    return relaxed_payload, result2, engine2, notes


def _run_pagination(turn: TurnIntent, ctx: 'ExcelQueryContext') -> Dict[str, Any]:
    """执行「继续分页」：完全复用上一轮的文件/Sheet/列/筛选，只改 offset/limit。

    - offset/limit 由 compute_page() 用 Python 计算（LLM 不参与）；
    - 数据仍来自 representation.json（Phase 1B Structured Query）；
    - 不重新猜文件/列，也不调用 LLM 取数。
    """
    from backend.excel import store as excel_store

    plan = compute_page(turn, ctx)
    if plan.error:
        return _clarify(plan.error, stage='pagination')

    rep = excel_store.load_representation(ctx.document_id)
    if rep is None:
        return {
            'status': STATUS_ERROR,
            'message': f'上一轮的表格文件「{ctx.filename}」已不存在（可能已被删除），请重新发起查询。',
        }

    sheet = None
    if 0 <= ctx.sheet_index < len(rep.sheets):
        sheet = rep.sheets[ctx.sheet_index]
    if sheet is None:
        sheet, _cands = resolve_sheet_nl(rep, ctx.sheet_name)
    if sheet is None:
        return {
            'status': STATUS_ERROR,
            'message': f'上一轮的 Sheet「{ctx.sheet_name}」已不存在，请重新发起查询。',
        }

    # 列防御性重校验（正常情况应全部命中）
    columns: List[str] = []
    for name in (ctx.columns or []):
        col, _c = resolve_column_nl(sheet, name)
        if col and col not in columns:
            columns.append(col)

    payload: Dict[str, Any] = {
        'sheet_index': sheet.sheet_index,
        'filters': list(ctx.filters or []),
        'match_mode': excel_query.MATCH_MODE_AND,
        'limit': plan.limit,
        'offset': plan.offset,
    }
    if columns:
        payload['columns'] = columns

    try:
        payload, result, engine_used, relax_notes = _execute_with_prefix_relaxation(rep, payload)
    except excel_query.ExcelQueryError as e:
        return {'status': STATUS_ERROR, 'message': e.message, 'details': e.details}

    new_ctx = ExcelQueryContext(
        user_id=ctx.user_id,
        session_key=ctx.session_key,
        document_id=rep.document_id,
        filename=rep.filename,
        sheet_index=result.sheet_index,
        sheet_name=result.sheet_name,
        columns=columns,
        filters=list(ctx.filters or []),
        limit=plan.limit,
        offset=plan.offset,
        total_matches=result.total_matches,
        total_rows_in_sheet=result.total_rows_in_sheet,
    )

    return {
        'status': STATUS_OK,
        'message': format_pagination_summary(result, rep.filename, turn.action)
                   + (''.join('\n- 提示：' + n for n in relax_notes) if relax_notes else ''),
        'turn': turn.to_dict(),
        'continued': True,
        'engine': engine_used,
        'relaxed_filters': relax_notes,
        'pagination': {
            **plan.to_dict(),
            'from_offset': ctx.offset,
            'returned_count': result.returned_count,
            'total_matches': result.total_matches,
            'has_prev': plan.offset > 0,
            'has_next': (plan.offset + result.returned_count) < result.total_matches,
        },
        'document': {'document_id': rep.document_id, 'filename': rep.filename, 'file_type': rep.file_type},
        'sheet': {'sheet_index': result.sheet_index, 'sheet_name': result.sheet_name},
        'query': payload,
        'result': result.to_dict(),
        'new_context': new_ctx.to_dict(),
    }


def resolve_filters_nl(
    sheet: SheetRepresentation, filters: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """把 NL 筛选草图按真实 schema 解析（与 build_validated_query 同一套规则，供统计层复用）。"""
    resolved: List[Dict[str, Any]] = []
    for f in filters:
        col, cands = resolve_column_nl(sheet, f.get('column'))
        if col is None:
            raise NlQueryError(
                'column_ambiguous_or_missing',
                f'筛选列「{f.get("column")}」无法确定（候选：{", ".join(cands[:10]) or "无"}）',
                {'column': f.get('column'), 'candidates': cands[:30]},
            )
        operator = (f.get('operator') or excel_query.OPERATOR_EQ)
        if operator not in excel_query.SUPPORTED_OPERATORS:
            raise NlQueryError(
                'filter_operator_invalid',
                f'不支持的筛选运算符：{operator!r}',
                {'column': col, 'operator': operator},
            )
        value = f.get('value')
        if operator == excel_query.OPERATOR_CONTAINS:
            if not isinstance(value, str) or value == '':
                raise NlQueryError(
                    'filter_value_invalid', f'筛选条件「{col} 包含 ...」缺少有效的文本值',
                    {'column': col, 'value': value},
                )
            guard_contains_unsafe(sheet, col, value)
        elif operator in excel_query.RANGE_OPERATORS:
            num = excel_query.to_number(value)
            if num is None:
                raise NlQueryError(
                    'filter_value_invalid',
                    f'筛选条件「{col} {operator} ?」需要一个数值，收到 {value!r}',
                    {'column': col, 'operator': operator, 'value': value},
                )
            value = num
        resolved.append({'column': col, 'operator': operator, 'value': value})
    return resolved


# ============================================================================
# 2A-P0-A) 时间语义（仅绝对日期）+ contains 危险模式拦截
# ============================================================================
#: 「纯数字短文本」判据：1~4 位数字（如 "8" / "2026"）。
#: 这类值 + 文本列做 contains 几乎必然全表命中（真实故障：Created Time contains "8" -> 19/19）。
_SHORT_NUMERIC_VALUE_RE = re.compile(r'^\d{1,4}$')


def _find_column(sheet: 'SheetRepresentation', col_name: str):
    return next((c for c in sheet.columns if c.name == col_name), None)


def _column_values(sheet: 'SheetRepresentation', col_name: str) -> Sequence[Any]:
    col = _find_column(sheet, col_name)
    return sheet.column_values(col.index) if col is not None else []


def _column_is_numeric(sheet: 'SheetRepresentation', col_name: str) -> bool:
    """与统计层一致的"可数值化"判定（Order Amount 存的是文本型数字）。"""
    non_empty = [v for v in _column_values(sheet, col_name)
                 if v is not None and str(v).strip() != '']
    numeric = [v for v in non_empty if excel_aggregate.aggregate_to_number(v) is not None]
    return bool(numeric) and len(numeric) * 2 >= len(non_empty)


def _metric_is_explicit(norm_text: str, column: Optional[str]) -> bool:
    """文本里是否真的出现了**映射到该列的列别名**（即用户确实说到了这个指标）。

    - 只认真实列别名（「金额」-> Order Amount、「数量」-> Quantity …）；
    - LLM 自行填出的列（用户从没说过）不算 —— 那是"猜口径"；
    - 「几个 / 多少 / 几笔」这类**数量词**不算指标（那是计数，不是指标列）。
    """
    if not column or not norm_text:
        return False
    for key, target in COLUMN_ALIASES.items():
        k = nl_norm.normalize_name(key)
        if k and k in norm_text and target == column:
            return True
    return False


def _dimension_column_hint(text: str, norm_text: str, intent: Any,
                           catalog: List[Dict[str, Any]]) -> Optional[str]:
    """从**维度别名**（物流商 / SKU / 商品 / 城市 …）确定性推导"按什么分组"的列；无则 None。

    只认维度别名，**不认度量别名**（金额 / 数量 / 单价）—— 后者是"排什么"，不是"按什么分组"。
    文档未唯一确定时**沿用维度别名**（这里只确定"按什么分组"，绝不猜文件）；
    真实 schema 可解析时以其中的真实列名为准，若该维度列在表中不存在则放弃（不猜）。
    """
    hit: Optional[str] = None
    for key in sorted(DIMENSION_ALIASES, key=len, reverse=True):
        k = nl_norm.normalize_name(key)
        if k and k in norm_text:
            hit = DIMENSION_ALIASES[key]
            break
    if hit is None:
        return None
    _rep, sheet = _resolve_sheet_for_message(intent, catalog)
    if sheet is None:
        return hit
    return nl_norm.longest_resolvable_column(text, sheet.column_names, DIMENSION_ALIASES)


def guard_contains_unsafe(sheet: 'SheetRepresentation', col_name: str, value: Any) -> None:
    """2A-P0：拦截**危险的 contains**（静默假命中的主要来源）。

    只在两类情况下拒绝，其余一律放行（保证「物流商 SF / SKU / 订单号 / 商品编码 /
    用户明确要求文本包含」不受影响）：
      A. 目标列是**日期列**（按真实值判定）—— 时间条件绝不允许退化为子串匹配；
      B. 目标列是**文本语义列且非业务标识列**，而 value 是**纯数字短文本**（1~4 位）
         —— 「Created Time contains "8"」这类必然全表命中的写法。
    """
    from backend.excel.representation import SEMANTIC_TEXT, is_date_like_values

    values = _column_values(sheet, col_name)

    # A. 日期列：任何 contains 一律禁止 -> 明确澄清（引导使用年/月/日或区间）
    if is_date_like_values(values):
        raise NlQueryError(
            'filter_contains_unsafe_date',
            f'「{col_name}」是日期列，不支持"包含"匹配。请给出明确的年 / 月 / 日或日期区间，'
            f'例如「8月19日的订单」「8月1日到8月15日的订单」。',
            {'column': col_name, 'value': value, 'candidates': [col_name]},
        )

    # B. 纯数字短文本 + 文本列（非标识列）
    if not isinstance(value, str) or not _SHORT_NUMERIC_VALUE_RE.match(value.strip()):
        return
    col = _find_column(sheet, col_name)
    if col is None or col.semantic_type != SEMANTIC_TEXT:
        return
    if is_identifier_column(col, values):
        return          # 订单号 / SKU / 商品编码等：用户明确要求"包含"时保持可用
    raise NlQueryError(
        'filter_contains_unsafe_numeric',
        f'「{col_name}」是文本列，用数字片段「{value}」做"包含"匹配会命中几乎所有行；'
        f'请给出明确取值或范围。',
        {'column': col_name, 'value': value},
    )


def _iter_date_columns(sheet: 'SheetRepresentation') -> List[str]:
    """按真实值找出该 Sheet 的日期列（白名单格式）。"""
    from backend.excel.representation import is_date_like_values

    return [c.name for c in sheet.columns
            if is_date_like_values(sheet.column_values(c.index))]


def _infer_year(sheet: 'SheetRepresentation', col_name: str) -> Tuple[Optional[int], set]:
    """从真实值推断年份：返回 (唯一年份或 None, 出现过的年份集合)。"""
    from backend.excel.representation import parse_date_value

    years = set()
    for v in _column_values(sheet, col_name):
        d = parse_date_value(v)
        if d is not None:
            years.add(d.year)
    if len(years) == 1:
        return next(iter(years)), years
    return None, years


def _expr_to_range(expr: Dict[str, Any], year: int):
    """把日期表达式 + 年份换算成 (start_date, end_date)；非法返回 None。"""
    import calendar
    from datetime import date as _date

    try:
        if expr['kind'] == 'year':
            start, end = _date(year, 1, 1), _date(year, 12, 31)
        elif expr['kind'] == 'month':
            m = expr['month']
            start, end = _date(year, m, 1), _date(year, m, calendar.monthrange(year, m)[1])
        elif expr['kind'] == 'day':
            start = end = _date(year, expr['month'], expr['day'])
        else:  # range
            (m1, d1), (m2, d2) = expr['start'], expr['end']
            start, end = _date(year, m1, d1), _date(year, m2, d2)
    except (ValueError, KeyError, TypeError):
        return None
    if start > end:
        start, end = end, start
    return start, end


def _count_in_range(sheet: 'SheetRepresentation', col_name: str, start, end) -> int:
    """按 Python 参考实现统计该列在 [start, end] 内的行数（用于多日期列一致性判定）。"""
    bounds = [start.isoformat(), end.isoformat()]
    return sum(1 for v in _column_values(sheet, col_name)
               if excel_query.date_between_match(v, bounds))


def _temporal_filter_for_message(
    message: str,
    sheet: 'SheetRepresentation',
    preferred: Optional[Sequence[str]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """2A-P0：把**绝对日期**表达式映射成 date_between 条件。

    ``preferred``：本轮意图里**已经指向**的列（通常来自 LLM）。命中其中的日期列时
    直接采用它 —— 这样「8月19日的订单」不会因为表里存在多个日期列
    （Created Time / Paid Time / RTS Time / Shipped Time）而被迫澄清。

    返回 (filter, clarify)：
      - (filter, None)   -> 成功生成日期区间条件
      - (None, clarify)  -> 必须澄清（相对时间 / 无日期列 / 多日期列无法确定 / 年份不明）
      - (None, None)     -> 文本里没有时间语义（保持原路径）
    """
    text = nl_norm.to_halfwidth(message)
    if not text:
        return None, None
    expr = nl_norm.parse_date_expression(text)
    if expr is None:
        # 相对时间（最近7天/上个月/本周…）：明确不支持 -> 澄清，绝不静默退化
        if nl_norm.RELATIVE_TIME_RE.search(text):
            return None, _clarify(
                '暂不支持相对时间（如"最近7天 / 上个月 / 本周"）。请给出明确的年 / 月 / 日'
                '或日期区间，例如「8月19日的订单」「2026年8月1日到8月15日的订单」。',
                stage='date',
            )
        return None, None

    date_cols = _iter_date_columns(sheet)
    if not date_cols:
        return None, _clarify(
            '没有找到可识别的日期列，无法按日期筛选。请指明具体列，或改用其他条件。',
            sheet.column_names[:30], stage='date',
        )
    pref = [c for c in (preferred or []) if c in date_cols]
    if len(pref) == 1:
        col_name = pref[0]                      # 本轮意图已指向唯一日期列 -> 直接采用
    elif len(date_cols) == 1:
        col_name = date_cols[0]
    else:
        # 多日期列、且本轮未指向任何一列：仅当**所有日期列的命中数完全一致**时
        # 才无歧义（例如 8月21日在任何时间列里都没有记录 -> 一致为 0 行）；
        # 只要有一列结果不同，就必须由用户指明按哪一列筛选（绝不猜）。
        counts: Dict[str, int] = {}
        for c in date_cols:
            y_c = expr.get('year') or _infer_year(sheet, c)[0]
            if y_c is None:
                return None, _clarify(
                    '表格中有多个日期列且年份不一致，请指明具体年份与日期列，'
                    '例如「2026年8月按 Created Time 统计」。',
                    date_cols[:30], stage='date',
                )
            rng_c = _expr_to_range(expr, y_c)
            if rng_c is None:
                return None, _clarify(
                    '无法解析该日期表达式，请给出有效的年 / 月 / 日或日期区间。', stage='date',
                )
            counts[c] = _count_in_range(sheet, c, rng_c[0], rng_c[1])
        if len(set(counts.values())) != 1:
            detail = '、'.join(f'{c}={counts[c]}' for c in date_cols[:4])
            return None, _clarify(
                f'该表格有多个日期列（{"、".join(date_cols[:5])}），各列结果不同'
                f'（{detail}），请指明按哪一列筛选。',
                date_cols[:30], stage='date',
            )
        col_name = date_cols[0]                 # 各日期列结果一致 -> 无歧义

    # 年份推断：未显式给出时，要求该列只有唯一年份，否则澄清
    year = expr.get('year')
    if year is None:
        year, years = _infer_year(sheet, col_name)
        if year is None:
            shown = '、'.join(str(y) for y in sorted(years)[:8]) or '未知'
            return None, _clarify(
                f'「{col_name}」包含多个年份（{shown}），请指明具体年份，'
                f'例如「2026年8月的订单」。',
                stage='date',
            )

    rng = _expr_to_range(expr, year)
    if rng is None:
        return None, _clarify(
            '无法解析该日期表达式，请给出有效的年 / 月 / 日或日期区间。', stage='date',
        )
    start, end = rng
    return {
        'column': col_name,
        'operator': excel_query.OPERATOR_DATE_BETWEEN,
        'value': [start.isoformat(), end.isoformat()],
    }, None


# ============================================================================
# 2A-P0-B) 中文名次语义守卫（排名前N / 排行前N / 第N名 / 前N名）
# ============================================================================
def _guard_rank_semantics(
    turn: Optional[TurnIntent],
    message: str,
    catalog: List[Dict[str, Any]],
    signals: Optional['nl_norm.IntentSignals'],
) -> Tuple[Optional[TurnIntent], List[str], Optional[Dict[str, Any]]]:
    """守卫中文名次语义，**绝不允许静默降级为全表 COUNT**。

    返回 (turn, notes, clarify)：

    1. **显式名次**（「排名 / 排行 / 第N名」）：
       - 分组维度可确定性解析 -> 分组排行（有度量列用度量，否则 COUNT）+ TOP-N；
       - 有度量列 -> 按聚合值排行 + TOP-N；
       - 都没有 -> **澄清**（并给出可排名的数值列候选）。
    2. **仅「前N名 / 前N位」**（无"排名/排行/第"）：
       - 视为"取前 N 个"的行级截取 -> 普通查询 limit=N（不要求排序依据）。
    """
    text = nl_norm.to_halfwidth(message)
    if not text or signals is None or turn is None:
        return turn, [], None
    explicit = bool(nl_norm.EXPLICIT_RANK_RE.search(text))
    first_n = bool(nl_norm.FIRST_N_RANK_RE.search(text))
    if not (explicit or first_n):
        return turn, [], None
    # O2 收口（证据：实测「哪几个订单排名最高」→ 被 LLM 猜出 Order Amount 后直接执行）：
    # **显式名次语义本身**就必须受本守卫约束，不要求文本一定带"前N/第N"这种显式 N。
    # 只有「前N名/前N位」这一支（行级截取）必须有可解析的 N，否则不构造。
    if first_n and not explicit and signals.top_n is None:
        return turn, [], None

    agg = turn.aggregate if turn.action == ACTION_AGGREGATE else None
    norm_text = nl_norm.normalize_name(text)
    # 2A-P0（统一边界，O2 收口）：analysis 轮次的"排名口径"在 step1。
    # 对 analysis **只做放行 / 澄清判定**，绝不改写分析计划。
    step1: Optional[Dict[str, Any]] = None
    if turn.action == ACTION_ANALYSIS and turn.analysis is not None:
        _steps = getattr(turn.analysis, 'steps', None) or []
        step1 = _steps[0] if _steps else None

    # ---------- 2) 仅「前N名 / 前N位」：行级截取前 N 行 ----------
    if first_n and not explicit:
        n = max(1, min(signals.top_n, MAX_NL_LIMIT))
        payload = {
            'query_type': INTENT_STRUCTURED,
            'document': getattr(agg, 'document', None),
            'sheet': getattr(agg, 'sheet', None),
            'filters': list(getattr(agg, 'filters', None) or []),
            'limit': n,
        }
        new_intent = intent_from_dict(payload)
        new_intent.limit_explicit = True
        ranked = TurnIntent(action=ACTION_NEW_QUERY, intent=new_intent,
                            source='deterministic_rank_first_n', raw=payload)
        return ranked, [f'名次语义(前N名) -> 行级截取 limit={n}'], None

    # ---------- 1b) analysis 轮次：step1 即"排名口径" ----------
    # 实测 bypass（修复前）：「排名前三的订单的总和是多少」-> llm_parse_turn 直接返回
    # action=analysis 且 step1.col=Order Amount（用户从未说过指标）-> analysis_guard
    # 补齐 step1 后直接 _run_analysis_turn 并 return，**完全绕过本守卫**（返回了真实数值）。
    # 这里统一拦住：无显式指标且无分组维度 -> 澄清（不改写计划；有依据则原样放行）。
    if step1 is not None:
        col1 = step1.get('column') or step1.get('calculation')
        grp1 = step1.get('group_by') or []
        if grp1 or _metric_is_explicit(norm_text, col1):
            return turn, [], None
        cands1: List[str] = []
        _r1, _s1 = _resolve_sheet_for_message(_EmptyIntent(), catalog)
        if _s1 is not None:
            cands1 = [c.name for c in _s1.columns if _column_is_numeric(_s1, c.name)][:30]
        return turn, [], _clarify(
            '需要明确按什么字段排名（例如「按订单金额排名前三的订单」「订单最多的前3个物流商」）。',
            cands1, stage='order',
        )

    # ---------- 1) 显式名次：必须有排序依据 ----------
    if agg is None:
        # LLM 直接要求澄清，但文本里是**显式名次**：给出可操作的"需要排序依据"澄清，
        # 而不是泛泛的"信息不足" —— 也避免后续路径把它当普通查询而静默 COUNT。
        # 文档/Sheet 无法唯一确定时保留原有澄清（不掩盖文件歧义）。
        _r0, _s0 = _resolve_sheet_for_message(_EmptyIntent(), catalog)
        if _s0 is None:
            return turn, [], None
        cands0 = [c.name for c in _s0.columns if _column_is_numeric(_s0, c.name)][:30]
        return turn, [], _clarify(
            '需要明确按什么字段排名（例如「按订单金额排名前三的订单」「订单最多的前3个物流商」）。',
            cands0, stage='order',
        )

    notes: List[str] = []
    # 分组维度只认**维度别名**（物流商/SKU/商品/城市…），**不认度量别名**（金额/数量/单价…）：
    # 后者回答的是"排什么"，不是"按什么分组"。实测依据：
    #   * 「排名前三的物流商」-> 维度别名命中 -> 分组排行（保持原能力）；
    #   * 「按订单金额排名前三」/「按数量排名前三」-> 只命中度量别名 -> **不得**当作分组维度
    #     （曾误入分组排行，执行了一次没有分组依据的聚合）；
    #   * 「查看排名前三订单」->「订单」不是维度别名 -> 澄清（O2 缺陷不会再回来）。
    # 注意：**不能**直接信任 agg.group_by —— LLM 会给「查看排名前三订单」填出
    # group_by=Order ID，那正是 O2 缺陷的来源。
    group_hint = _dimension_column_hint(text, norm_text, agg, catalog)

    if group_hint is not None:
        agg.group_by = agg.group_by or [group_hint]
        # 分组排行：有度量列用度量，否则用 COUNT（「排名前三的物流商」保持原能力）
        if not agg.column and not agg.calculation \
                and agg.operation in excel_aggregate.NUMERIC_OPERATIONS:
            agg.operation = excel_aggregate.OPERATION_COUNT
            agg.calculation = None
            notes.append('名次语义：无度量列 -> COUNT')
        agg.order_by = excel_aggregate.ORDER_BY_AGGREGATE
        agg.order_dir = agg.order_dir or 'desc'
        if signals.top_n is not None:
            agg.top_n = max(1, min(signals.top_n, MAX_NL_LIMIT))
            notes.append(f'名次语义 -> 分组排行 TOP-{agg.top_n}')
        else:
            notes.append(f'名次语义 -> 分组排行（沿用原 top_n={agg.top_n}）')
        return turn, notes, None

    # 指标排行：「度量列由文本显式给出」**且**「排行榜有分组维度」才成立。
    #   * 度量列必须命中真实列别名（LLM 自行填的 Order Amount 不算）；
    #   * 数量词（几个/多少/几笔）不算指标 —— 实测「哪几个订单排名最高」曾被误判；
    #   * 没有分组维度时 order_by=aggregate_value 无意义（单值聚合无"排行"可言），
    #     此时应澄清而不是执行一个无依据的 TOP-N。
    if ((agg.column or agg.calculation)
            and agg.group_by
            and _metric_is_explicit(norm_text, agg.column)):
        agg.order_by = excel_aggregate.ORDER_BY_AGGREGATE
        agg.order_dir = agg.order_dir or 'desc'
        if signals.top_n is not None:
            agg.top_n = max(1, min(signals.top_n, MAX_NL_LIMIT))
            notes.append(f'名次语义 -> 指标排行 TOP-{agg.top_n}')
        else:
            notes.append(f'名次语义 -> 指标排行（沿用原 top_n={agg.top_n}）')
        return turn, notes, None

    # 无分组维度、无度量列 -> **澄清**（禁止降级为无排序的全表 COUNT）
    cands: List[str] = []
    _rep2, sheet2 = _resolve_sheet_for_message(agg, catalog)
    if sheet2 is not None:
        cands = [c.name for c in sheet2.columns if _column_is_numeric(sheet2, c.name)][:30]
    return turn, notes, _clarify(
        '需要明确按什么字段排名（例如「按订单金额排名前三的订单」「订单最多的前3个物流商」）。',
        cands, stage='order',
    )


def _apply_temporal_repair(
    turn: Optional[TurnIntent], message: str, catalog: List[Dict[str, Any]]
) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """2A-P0：把**绝对日期**表达式落成 ``date_between`` 条件；必要时返回澄清。

    - 生成前会丢弃 LLM 可能已产出的、指向同一列的旧条件（尤其是 ``contains``），
      避免「time 条件 + 子串条件」叠加；
    - 文本没有时间语义时什么都不做（保持原路径）。
    """
    if turn is None:
        return [], None
    if turn.action == ACTION_AGGREGATE and turn.aggregate is not None:
        holder, hint = turn.aggregate, turn.aggregate
    elif turn.action == ACTION_NEW_QUERY and turn.intent is not None:
        holder, hint = turn.intent, turn.intent
    else:
        return [], None
    _rep, sheet = _resolve_sheet_for_message(hint, catalog)
    if sheet is None:
        return [], None

    # 本轮意图已指向的列（LLM 在 baseline 通常会给出 Created Time）
    preferred = [f.get('column') for f in (getattr(holder, 'filters', None) or [])
                 if isinstance(f, dict) and f.get('column')]
    filt, clarify = _temporal_filter_for_message(message, sheet, preferred)
    if clarify is not None:
        return [], clarify
    if filt is None:
        return [], None

    kept = [f for f in (getattr(holder, 'filters', None) or [])
            if (f or {}).get('column') != filt['column']]
    kept.append(filt)
    holder.filters = kept
    return [f"时间条件 -> {filt['column']} {filt['value'][0]} ~ {filt['value'][1]}"], None


def _execute_aggregate_with_prefix_relaxation(
    rep: WorkbookRepresentation, payload: Dict[str, Any]
) -> Tuple[Dict[str, Any], Any, str, List[str]]:
    """统计版「唯一前缀放宽」：与 Phase 2 同一规则、同一可见性。

    仅当统计条件命中 0 行、且放宽后确实命中数据时才生效（绝不静默改语义）。
    """
    result, engine_used = query_engine.run_aggregate(rep, payload)
    if result.matched_rows > 0:
        return payload, result, engine_used, []

    sheet_index = payload.get('sheet_index') or 0
    relaxed = _build_relaxed_payload(rep, sheet_index, payload)
    if relaxed is None:
        return payload, result, engine_used, []

    relaxed_payload, notes = relaxed
    result2, engine2 = query_engine.run_aggregate(rep, relaxed_payload)
    if result2.matched_rows == 0:
        return payload, result, engine_used, []
    return relaxed_payload, result2, engine2, notes


def _run_aggregate_turn(
    turn: TurnIntent,
    catalog: List[Dict[str, Any]],
    *,
    context: Optional['ExcelQueryContext'] = None,
    aggregate_context: Optional['AggregateContext'] = None,
    user_id: Optional[int] = None,
    session_key: Optional[str] = None,
    document_override: Optional[str] = None,
    user_message: str = '',
) -> Dict[str, Any]:
    """Phase 3A/3B：执行一次统计（COUNT / SUM / AVG / MIN / MAX，可选 GROUP BY）。

    上下文规则（显式、无猜测）：
    - refers_to_previous=true（"这些订单…"）时继承上一轮的 document / sheet / filters：
        优先继承**统计上下文**，其次继承**分页（列表）上下文**；
        两者都没有 -> clarify，绝不猜。
    - Phase 3B：若用户本轮**没有**给出新的分组字段，则一并继承上一轮统计的 group_by
        （支持「每个物流商分别有多少单」→「这些物流商的平均订单金额是多少」）。
    - 用户已明确给出文件/条件/分组时按用户的来（refers_to_previous=false）。
    - 继承来的 filters 与用户本轮额外给出的 filters 之间是 AND。
    """
    from backend.excel import store as excel_store

    agg = turn.aggregate or AggregateIntent()
    # Phase 4B：非法计算字段必须明确告知（绝不静默降级为普通统计）
    if agg.calc_error:
        return _clarify(agg.calc_error, stage='calculation')
    inherited_from = ''
    inherited_doc_id: Optional[str] = None
    inherited_sheet_index: Optional[int] = None
    inherited_sheet_name: Optional[str] = None
    inherited_filters: List[Dict[str, Any]] = []
    inherited_group_by: List[str] = []
    inherited_order_by: Optional[str] = None
    inherited_order_dir: Optional[str] = None
    inherited_top_n: Optional[int] = None
    inherited_filename = ''

    # 「再看前10个」这类"只有 TOP-N、没有分组/排序依据"的说法，语义上只能指代上一轮：
    # 即使 LLM 忘了标 refers_to_previous，也按继承处理（否则只会得到一句澄清）。
    should_inherit = agg.refers_to_previous
    if (not should_inherit and aggregate_context is not None
            and agg.top_n is not None and not agg.group_by
            and not agg.order_by and not agg.document):
        should_inherit = True

    if should_inherit:
        if aggregate_context is not None:
            inherited_from = 'aggregate'
            inherited_doc_id = aggregate_context.document_id
            inherited_filename = aggregate_context.filename
            inherited_sheet_index = aggregate_context.sheet_index
            inherited_sheet_name = aggregate_context.sheet_name
            inherited_filters = list(aggregate_context.filters or [])
            inherited_group_by = list(aggregate_context.group_by or [])
            inherited_order_by = aggregate_context.order_by
            inherited_order_dir = aggregate_context.order_dir
            inherited_top_n = aggregate_context.top_n
        elif context is not None:
            inherited_from = 'query'
            inherited_doc_id = context.document_id
            inherited_filename = context.filename
            inherited_sheet_index = context.sheet_index
            inherited_sheet_name = context.sheet_name
            inherited_filters = list(context.filters or [])
        else:
            return _clarify(
                '我还不知道「这些订单」指的是哪一批数据——最近一轮没有可继承的表格查询或统计。'
                '请先说明要查询哪个表格，或重新给出条件（例如「直邮5店 物流商为SF的有几单」）。',
                stage='aggregate_context',
            )

    # ---- 文件定位 ----
    doc_hint = document_override or agg.document
    rep: Optional[WorkbookRepresentation] = None
    if not doc_hint and inherited_doc_id:
        rep = excel_store.load_representation(inherited_doc_id)
        if rep is None:
            return {'status': STATUS_ERROR,
                    'message': f'上一轮的表格文件「{inherited_filename}」已不存在，请重新发起查询。'}
    else:
        doc, doc_candidates = resolve_document(catalog, doc_hint)
        if doc is None:
            if doc_candidates:
                return _clarify(
                    f'无法确定要对哪个表格做统计，请指定其中之一：{", ".join(doc_candidates)}',
                    doc_candidates, stage='document',
                )
            return _clarify('无法确定要对哪个表格做统计。', stage='document')
        rep = excel_store.load_representation(doc['document_id'])
        if rep is None:
            return {'status': STATUS_ERROR, 'message': f'表格文件「{doc["filename"]}」的表示数据缺失，请重新上传。'}

    # ---- Sheet 定位（继承时优先沿用上一轮 Sheet） ----
    sheet_hint = agg.sheet
    sheet: Optional[SheetRepresentation] = None
    if sheet_hint:
        sheet, sheet_candidates = resolve_sheet_nl(rep, sheet_hint)
        if sheet is None:
            return _clarify(
                f'无法确定统计哪个 Sheet（候选：{", ".join(sheet_candidates) or "无"}）',
                sheet_candidates, stage='schema',
            )
    elif inherited_from and inherited_sheet_index is not None and 0 <= inherited_sheet_index < len(rep.sheets):
        sheet = rep.sheets[inherited_sheet_index]
    if sheet is None:
        if len(rep.sheets) == 1:
            sheet = rep.sheets[0]
        else:
            sheet, cands = resolve_sheet_nl(rep, inherited_sheet_name)
            if sheet is None:
                return _clarify(
                    f'该文件有多个 Sheet，请指定统计哪一个：{", ".join(cands) or ", ".join(s.sheet_name for s in rep.sheets)}',
                    cands, stage='schema',
                )

    # ---- 目标列 / 筛选条件解析 ----
    try:
        resolved_filters = resolve_filters_nl(sheet, (inherited_filters + list(agg.filters)))
    except NlQueryError as e:
        if e.code in ('column_ambiguous_or_missing',):
            return _clarify(e.message, e.details.get('candidates'), stage='schema')
        return {'status': STATUS_ERROR, 'message': e.message, 'details': e.details}

    payload: Dict[str, Any] = {
        'operation': agg.operation,
        'sheet_index': sheet.sheet_index,
        'filters': resolved_filters,
    }
    # Phase 4B：受控计算字段（真实列唯一解析 + 口径歧义澄清）
    if agg.calculation:
        try:
            calc = excel_calculation.ensure_resolved(sheet, agg.calculation)
        except excel_query.ExcelQueryError as e:
            return _clarify(
                f'无法解析计算字段的列：{e.message}。'
                f'（候选：{", ".join(sheet.column_names[:10])}）',
                sheet.column_names[:30], stage='schema',
            )
        ambiguous = excel_calculation.needs_clarify(user_message, calc, level='aggregate')
        if ambiguous:
            return _clarify(ambiguous, stage='calculation')
        payload['calculation'] = calc.to_dict()
    elif agg.column:
        col, col_candidates = resolve_column_nl(sheet, agg.column)
        if col is None:
            return _clarify(
                f'无法确定统计的目标列「{agg.column}」'
                f'（候选：{", ".join(col_candidates[:10]) or "无"}）。'
                f'请明确要统计哪一列。',
                col_candidates[:30], stage='schema',
            )
        payload['column'] = col

    # ---- Phase 3B：分组字段解析（本轮显式给出优先；否则继承上一轮统计的分组） ----
    requested_group = list(agg.group_by) if agg.group_by else list(inherited_group_by)
    group_inherited = bool(inherited_group_by) and not agg.group_by
    resolved_group_by: List[str] = []
    for name in requested_group:
        gcol, g_candidates = resolve_column_nl(sheet, name)
        if gcol is None:
            return _clarify(
                f'无法确定分组字段「{name}」（候选：{", ".join(g_candidates[:10]) or "无"}）。'
                f'请明确按哪一列分组。',
                g_candidates[:30], stage='schema',
            )
        if gcol not in resolved_group_by:
            resolved_group_by.append(gcol)
    if len(resolved_group_by) > excel_aggregate.MAX_GROUP_COLUMNS:
        return _clarify(
            f'本阶段最多支持 {excel_aggregate.MAX_GROUP_COLUMNS} 个分组字段，'
            f'当前需要 {len(resolved_group_by)} 个（{", ".join(resolved_group_by)}）。',
            resolved_group_by, stage='schema',
        )
    if resolved_group_by:
        payload['group_by'] = resolved_group_by

    # ---- Phase 3C：排序 / TOP-N（本轮显式给出优先；否则继承上一轮统计的排序） ----
    raw_order_by = agg.order_by or inherited_order_by
    raw_order_dir = agg.order_dir or inherited_order_dir
    raw_top_n = agg.top_n if agg.top_n is not None else inherited_top_n
    # 只要有任一排序字段是"继承来的"（本轮未显式给出），就在结果里明确告知用户
    order_inherited = bool(
        (inherited_order_by and not agg.order_by)
        or (inherited_order_dir and not agg.order_dir)
        or (inherited_top_n is not None and agg.top_n is None)
    )
    order_by: Optional[str] = None
    order_dir: str = excel_aggregate.ORDER_ASC
    top_n: Optional[int] = None
    try:
        if raw_order_by:
            candidate = raw_order_by
            if not excel_aggregate.is_order_enum(candidate):
                # 可能是"分组列的中文简称"（如「物流商」）-> 先按真实 schema 解析列名
                ocol, _ocands = resolve_column_nl(sheet, candidate)
                if ocol:
                    candidate = ocol
            order_by = excel_aggregate.normalize_order_by(candidate, resolved_group_by)
        if order_by and not resolved_group_by:
            return _clarify(
                '排序只用于分组统计结果，请先说明按什么分组'
                '（例如「按物流商统计订单金额，从高到低排列」）。',
                stage='order',
            )
        if order_by:
            order_dir = excel_aggregate.normalize_order_dir(raw_order_dir, order_by)
        top_n = excel_aggregate.normalize_top_n(raw_top_n)
    except excel_query.ExcelQueryError as e:
        return _clarify(e.message, stage='order')
    if top_n is not None and order_by is None:
        return _clarify(
            '「前 N 个」需要明确的排序依据（例如「订单金额最高的5个物流商」），请补充按什么排序。',
            stage='order',
        )
    if order_by:
        payload['order_by'] = order_by
        payload['order_dir'] = order_dir
    if top_n is not None:
        payload['top_n'] = top_n

    # ---- 执行（DuckDB 优先；0 行时按唯一前缀放宽重试） ----
    try:
        payload, result, engine_used, relax_notes = _execute_aggregate_with_prefix_relaxation(rep, payload)
    except excel_query.ExcelQueryError as e:
        if e.code == excel_aggregate.ERR_IDENTIFIER_NOT_NUMERIC:
            # 稳定性补丁：业务标识列（Order ID / SKU ID…）不能做数值聚合。
            # 明确澄清，并给出真正可用于统计的数值列候选。
            identifiers = set(e.details.get('identifier_columns') or [])
            candidates = [c for c in (e.details.get('available_columns') or sheet.column_names)
                          if c not in identifiers]
            return _clarify(e.message, candidates[:30], stage='aggregate_target')
        if e.code in (excel_aggregate.ERR_NOT_NUMERIC_COLUMN,
                      excel_query.ERR_COLUMN_NOT_FOUND,
                      excel_query.ERR_COLUMN_AMBIGUOUS,
                      excel_aggregate.ERR_COLUMN_REQUIRED):
            return _clarify(e.message, e.details.get('available_columns') or [], stage='schema')
        return {'status': STATUS_ERROR, 'message': e.message, 'details': e.details}

    notes: List[str] = []
    if inherited_from:
        notes.append('已沿用上一轮的数据范围与筛选条件')
    if group_inherited:
        notes.append(f'已沿用上一轮的分组字段（{", ".join(resolved_group_by)}）')
    if order_inherited:
        notes.append('已沿用上一轮的排序与 TOP-N 设置')
    notes.extend(relax_notes)

    is_grouped = isinstance(result, excel_aggregate.GroupedAggregateResult)
    if is_grouped:
        message = excel_aggregate.format_group_aggregate_summary(result, rep.filename)
    else:
        message = excel_aggregate.format_aggregate_summary(result, rep.filename)
    if notes:
        message += ''.join('\n- 提示：' + n for n in notes)

    new_agg_ctx = AggregateContext(
        user_id=user_id if user_id is not None else 0,
        session_key=session_key or 'default',
        document_id=rep.document_id,
        filename=rep.filename,
        sheet_index=result.sheet_index,
        sheet_name=result.sheet_name,
        operation=result.operation,
        column=result.column,
        filters=list(payload.get('filters') or []),
        matched_rows=result.matched_rows,
        value=None if is_grouped else result.value,
        group_by=list(payload.get('group_by') or []),
        order_by=payload.get('order_by'),
        order_dir=payload.get('order_dir'),
        top_n=payload.get('top_n'),
    )

    return {
        'status': STATUS_OK,
        'message': message,
        'turn': turn.to_dict(),
        'aggregate': None if is_grouped else result.to_dict(),
        'group_aggregate': result.to_dict() if is_grouped else None,
        'engine': engine_used,
        'relaxed_filters': relax_notes,
        'inherited_from': inherited_from,
        'document': {'document_id': rep.document_id, 'filename': rep.filename, 'file_type': rep.file_type},
        'sheet': {'sheet_index': result.sheet_index, 'sheet_name': result.sheet_name},
        'query': payload,
        'new_aggregate_context': new_agg_ctx.to_dict(),
    }


# ============================================================================
# Phase 4A：受控两阶段分析
# ============================================================================
def _has_group_step(steps: Sequence[Dict[str, Any]]) -> bool:
    return any((s.get('type') or '').lower() in ('', 'group_aggregate') for s in steps)


def _analysis_steps_for_execution(
    intent: AnalysisIntent,
    inherited_plan: Optional[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """合并本轮步骤与上一轮第 1 步（支持「再算一下这 10 个的平均销售额」式追问）。

    返回 None 表示无法确定要执行什么（由调用方 clarify，绝不猜）。
    """
    steps = [dict(s) for s in (intent.steps or []) if isinstance(s, dict)]
    inherit_steps = []
    if isinstance(inherited_plan, dict):
        raw = inherited_plan.get('steps')
        inherit_steps = [dict(s) for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []

    if not inherit_steps:
        return steps or None

    need_inherit = intent.refers_to_previous or not _has_group_step(steps)
    if not need_inherit:
        return steps

    # 只替换第 2 步：第 1 步沿用上一轮
    agg_steps = [s for s in steps
                 if (s.get('type') or '').lower() == excel_multi_step.STEP_AGGREGATE
                 or s.get('source')]
    step2 = agg_steps[-1] if agg_steps else (inherit_steps[1] if len(inherit_steps) > 1 else None)
    if step2 is None:
        return None
    return [inherit_steps[0], step2]


#: 「逐行计算」的说法（这些词 + 没有任何统计词 -> 用户要的是每行的计算结果，不是统计）
_ROW_CALC_CUE_RE = re.compile(r'除以|相除|乘以|相乘|减去|相减|加起来|差额')


def _row_calc_only(message: str) -> bool:
    """判断这句话是否只是"逐行算一个值"，而不是要统计。

    例：「一店每笔订单的金额除以数量是多少？」-> True（要每行的计算值）
        「按SKU统计每笔订单的单价平均值」      -> False（含"平均" -> 统计）

    只用于**抑制统计兜底**，避免把"计算字段的行级查询"改写成统计查询。
    后续 2A：实现搬到 `nl_normalize.is_row_calc_only`（算术 + 无聚合动词；
    COUNT 不算聚合动词，因为「除以数量是多少」问的是每行计算值）。
    """
    return nl_norm.is_row_calc_only(message)


def _analysis_plan_missing_target(plan: Optional[AnalysisIntent]) -> bool:
    """多步计划的第 1 步是否「既没有列、也没有计算字段」（无法执行，必然只能澄清）。

    例：LLM 把「每笔订单单价平均值最高的5个SKU…」的第 1 步抽成
    `operation=avg, column=null`（漏掉了 Order Amount ÷ Quantity 这个计算字段）。
    这种情况应该用 temperature=0 的专用抽取器**再试一次**，而不是直接放弃。
    """
    if plan is None or not plan.steps:
        return False
    step1 = plan.steps[0]
    if not isinstance(step1, dict):
        return False
    step_type = (_as_text(step1.get('type')) or excel_multi_step.STEP_GROUP_AGGREGATE).lower()
    if step_type == excel_multi_step.STEP_AGGREGATE:
        return not step1.get('column') and not step1.get('calculation')
    operation = (_as_text(step1.get('operation')) or excel_aggregate.OPERATION_COUNT).lower()
    if operation == excel_aggregate.OPERATION_COUNT:
        return False                      # COUNT 不需要列
    return not step1.get('column') and not step1.get('calculation')


def _analysis_step1_to_aggregate_turn(turn: TurnIntent) -> Optional[TurnIntent]:
    """把「多步计划的第 1 步」降级成 Phase 3 的单步分组统计。

    用途：LLM 有时会把**只需要一次分组统计**的问题（如「按SKU统计每笔订单的单价平均值，
    从高到低排列，取前5个」）过度规划成两步。此时若用户文本里**没有**第二步汇总的语义、
    且第 1 步确实是「分组 + TOP-N 排行」形态，则降级为单步分组统计（Phase 3C），
    避免把 5 行明细压成一个数字。

    返回 None 表示无法降级（调用方继续走多步路径，绝不劣化）。
    """
    plan_intent = turn.analysis
    if plan_intent is None or not plan_intent.steps:
        return None
    # 超过 2 步属于"超出本阶段能力"，必须走原路径澄清，绝不能靠降级掩盖
    if len(plan_intent.steps) > excel_multi_step.MAX_STEPS:
        return None
    step1 = plan_intent.steps[0]
    if not isinstance(step1, dict):
        return None
    step_type = (_as_text(step1.get('type')) or excel_multi_step.STEP_GROUP_AGGREGATE).lower()
    if step_type != excel_multi_step.STEP_GROUP_AGGREGATE:
        return None
    if not _as_str_list(step1.get('group_by')):
        return None
    # 只有"排行 + TOP-N"形态才降级；没有 TOP-N 说明确实需要第二步汇总
    if _positive_int(step1.get('top_n')) is None:
        return None
    agg = AggregateIntent(
        operation=(_as_text(step1.get('operation')) or excel_aggregate.OPERATION_COUNT),
        document=plan_intent.document,
        sheet=plan_intent.sheet,
        column=_as_text(step1.get('column')),
        filters=normalize_filters(step1.get('filters')),
        group_by=_as_str_list(step1.get('group_by')),
        order_by=_as_text(step1.get('order_by')),
        order_dir=_as_text(step1.get('order_dir')),
        top_n=_positive_int(step1.get('top_n')),
        calculation=step1.get('calculation') if isinstance(step1.get('calculation'), dict) else None,
    )
    return TurnIntent(action=ACTION_AGGREGATE, aggregate=agg, source='analysis_downgrade')


#: 第 2 步 operation 的确定性映射（与计划 Prompt 里的规则一致）：
#: 「平均/均值」→ avg；「总/合计/加起来/一共/求和/汇总」→ sum；「最多/最大」→ max；「最少/最小」→ min。
#: 顺序很重要：「…销售额总和是多少？」里的「最高」只是**排名口径**，不能当成 max。
_STEP2_OP_RULES: Tuple[Tuple[str, 're.Pattern'], ...] = (
    (excel_aggregate.OPERATION_AVG, re.compile(r'平均|均值|平均数')),
    (excel_aggregate.OPERATION_SUM, re.compile(r'总|合计|加起来|一共|总共|共计|求和|汇总')),
    (excel_aggregate.OPERATION_MAX, re.compile(r'最多|最大|最高')),
    (excel_aggregate.OPERATION_MIN, re.compile(r'最少|最小|最低')),
)


def _step2_operation_from_text(message: str) -> str:
    """从用户原话里确定性推导第 2 步的汇总口径（缺省 sum：能触发多步判定的都是"要总量"的说法）。"""
    text = nl_norm.to_halfwidth(message)
    for op, regex in _STEP2_OP_RULES:
        if regex.search(text):
            return op
    return excel_aggregate.OPERATION_SUM


def _aggregate_to_analysis_turn(turn: TurnIntent,
                                message: str) -> Optional[TurnIntent]:
    """把「分组 + TOP-N」的单步统计**确定性升级**为两步分析（不调用 LLM）。

    用途：LLM 对「一店金额最高的10个SKU的销售额总和是多少？」只给出单步分组统计
    （返回 10 行明细），但用户问的是这 10 行的**总量**。此时第 2 步的口径完全可由
    用户措辞确定（总和→sum / 平均→avg…），直接在 Python 里补上第 2 步即可，
    既不需要再问 LLM，也不存在"同一句话两次给出不同 operator"的随机性。

    返回 None 表示不适合升级（保持原路径，绝不劣化）：
    - 不是单步分组统计，或缺少 group_by / top_n（无法确定"前 N 名"是什么）；
    - 文本里没有第二步汇总语义（由调用方用 `looks_like_multi_step_query` 判定）。
    """
    agg = turn.aggregate
    if agg is None or turn.action != ACTION_AGGREGATE:
        return None
    if not agg.group_by or _positive_int(agg.top_n) is None:
        return None
    step1: Dict[str, Any] = {
        'type': excel_multi_step.STEP_GROUP_AGGREGATE,
        'group_by': list(agg.group_by),
        'operation': agg.operation,
        'column': agg.column,
        'filters': agg.filters,
        'order_by': agg.order_by,
        'order_dir': agg.order_dir,
        'top_n': agg.top_n,
    }
    if isinstance(agg.calculation, dict):
        step1['calculation'] = agg.calculation
    step2 = {
        'type': excel_multi_step.STEP_AGGREGATE,
        'operation': _step2_operation_from_text(message),
        'source': excel_multi_step.SOURCE_STEP_1,
        'column': excel_multi_step.INTERMEDIATE_VALUE_COLUMN,
    }
    plan = AnalysisIntent(document=agg.document, sheet=agg.sheet, steps=[step1, step2])
    return TurnIntent(action=ACTION_ANALYSIS, analysis=plan, source='aggregate_upgrade')


def _deterministic_top_group_turn(message: str, catalog: List[Dict[str, Any]],
                                  signals: 'nl_norm.IntentSignals',
                                  hint_intent: Any = None) -> Optional[TurnIntent]:
    """「哪个物流商订单最多？」/「订单数量最多的物流商？」-> **分组排行 TOP-1** 的确定性意图。

    真实故障（浏览器验收 2026-09-17，7B 模型）：
    - 「哪个物流商订单最多？」   -> 被答成单值 count=19（应为 Yanwen Express 12）；
    - 「订单数量最多的物流商？」 -> 被答成 19 行普通表格。

    判定条件（全部满足才构造，否则 None）：
    1. 文本里有极值词（最多/最高/最大 或 最少/最低/最小），且**没有**显式 N（「前5个」不走这条）；
    2. 文本不是"列清单"请求（列出/显示/筛选…）；
    3. 能唯一解析出一个**非数值列**（分组维度，如"物流商"）；
    4. 统计口径：出现「订单数量/单量/笔数」-> count；否则若文本里有可数值化的列 -> sum 该列；
       都没有 -> count。
    输出固定为 order_by=aggregate_value + top_n=1（最少/最小 -> asc）。
    """
    text = nl_norm.to_halfwidth(message)
    if not text or signals is None or signals.top_n is not None:
        return None
    if nl_norm.LIST_REQUEST_RE.search(text):
        return None
    is_max = bool(nl_norm.TOP_GROUP_MAX_RE.search(text))
    is_min = bool(nl_norm.TOP_GROUP_MIN_RE.search(text))
    if is_max == is_min:                      # 既无方向，或方向冲突（"最多也最少"）
        return None
    _rep, sheet = _resolve_sheet_for_message(hint_intent or _EmptyIntent(), catalog)
    if sheet is None:
        return None

    def values_of(name: str) -> Sequence[Any]:
        col = next((c for c in sheet.columns if c.name == name), None)
        return sheet.column_values(col.index) if col is not None else []

    def numeric_of(name: str) -> bool:
        non_empty = [v for v in values_of(name) if v is not None and str(v).strip() != '']
        numeric = [v for v in non_empty if excel_aggregate.aggregate_to_number(v) is not None]
        return bool(numeric) and len(numeric) * 2 >= len(non_empty)

    group_hint = nl_norm.longest_resolvable_column(text, sheet.column_names, COLUMN_ALIASES)
    if group_hint is None or numeric_of(group_hint):
        return None                            # 分组维度必须是"非数值列"
    if is_identifier_column(next(c for c in sheet.columns if c.name == group_hint),
                            values_of(group_hint)) and not nl_norm.WHICH_RE.search(text) \
            and '最多' not in text and '最少' not in text:
        return None                            # 纯 ID 列只有在"哪个/最多"这类说法下才当分组

    operation, column = excel_aggregate.OPERATION_COUNT, None
    if not signals.order_count_word:
        # 度量列 = 唯一的"可数值化且非业务标识"列（与分组维度列不同）
        metric = _unique_numeric_column(text, sheet)
        if metric and metric != group_hint:
            operation, column = excel_aggregate.OPERATION_SUM, metric
    agg = AggregateIntent(
        operation=operation,
        document=getattr(hint_intent, 'document', None),
        sheet=getattr(hint_intent, 'sheet', None),
        column=column,
        filters=normalize_filters(getattr(hint_intent, 'filters', None) or []),
        group_by=[group_hint],
        order_by=excel_aggregate.ORDER_BY_AGGREGATE,
        order_dir='desc' if is_max else 'asc',
        top_n=1,
    )
    return TurnIntent(action=ACTION_AGGREGATE, aggregate=agg, source='deterministic_top_group')


class _EmptyIntent:
    """占位（只用于复用 `_resolve_sheet_for_message` 的"文档/Sheet"读取）。"""

    document = None
    sheet = None


def _repair_analysis_plan_from_text(turn: TurnIntent, message: str,
                                    catalog: List[Dict[str, Any]],
                                    signals: Optional['nl_norm.IntentSignals'] = None) -> List[str]:
    """纠正两步计划**第 1 步的统计口径**（只按用户措辞做确定性映射，不猜）。

    真实样本（Stage B，多模型混合抽样）：
    「找出一店中金额最高的10个SKU，并统计它们的总销售额。」
    LLM 把第 1 步写成 `operation=count`（去数每个 SKU 的订单条数）-> 第 2 步得到 14.0；
    正确口径是「**金额**最高的10个SKU」-> `SUM(Order Amount)`（GT 267.61）。

    规则（与单步统计完全一致）：
    - 出现「订单数量/单量/笔数」-> count（不带列）；
    - 否则，若排行度量能**唯一解析到可数值化的真实列** -> sum + 该列；
    - 其余（含计算字段作为度量）一律不动。
    """
    notes: List[str] = []
    plan = turn.analysis
    if turn.action != ACTION_ANALYSIS or plan is None:
        return notes
    if not isinstance(turn.raw, dict) or not turn.raw:
        return notes                                  # 只修复 LLM 产出的计划
    if len(plan.steps) != 2:
        return notes                                  # 只处理标准两步
    step1 = plan.steps[0]
    if not isinstance(step1, dict):
        return notes
    if (_as_text(step1.get('type')) or excel_multi_step.STEP_GROUP_AGGREGATE) \
            != excel_multi_step.STEP_GROUP_AGGREGATE:
        return notes
    if step1.get('calculation'):
        return notes                                  # 度量是计算字段 -> 不干预
    _rep, sheet = _resolve_sheet_for_message(plan, catalog)
    if sheet is None:
        return notes

    def numeric_of(name: str) -> bool:
        col = next((c for c in sheet.columns if c.name == name), None)
        if col is None:
            return False
        values = sheet.column_values(col.index)
        non_empty = [v for v in values if v is not None and str(v).strip() != '']
        numeric = [v for v in non_empty if excel_aggregate.aggregate_to_number(v) is not None]
        return bool(numeric) and len(numeric) * 2 >= len(non_empty)

    if signals is not None and signals.order_count_word:
        want_op, want_col = excel_aggregate.OPERATION_COUNT, None
    else:
        # 「金额最高的10个SKU」-> 度量 = 唯一的"可数值化且非业务标识"列（不是 SKU ID）
        metric = _unique_numeric_column(message, sheet)
        if metric is None:
            return notes
        want_op, want_col = excel_aggregate.OPERATION_SUM, metric

    cur_op = (_as_text(step1.get('operation')) or excel_aggregate.OPERATION_COUNT).lower()
    cur_col = _as_text(step1.get('column'))
    if cur_op == want_op and (want_col is None or cur_col == want_col):
        return notes
    step1['operation'] = want_op
    step1['column'] = want_col
    notes.append(f'两步计划第 1 步口径={want_op}'
                 f'{"/" + want_col if want_col else ""}（确定性纠正）')
    return notes


def _resolve_sheet_for_message(intent: Any,
                               catalog: List[Dict[str, Any]]) -> Tuple[Optional[WorkbookRepresentation],
                                                                     Optional[SheetRepresentation]]:
    """按「文档唯一 + Sheet 唯一」的严格规则定位数据（**绝不猜文件**）。

    文档：`intent.document` 为空时只有在 catalog 里只有 1 份表格才能确定；
    Sheet：给了名字就按名字解析，否则只有单 Sheet 才确定。
    任何一步不唯一都返回 (None, None)，由调用方保持原路径（澄清/报错）。
    """
    from backend.excel import store as excel_store

    doc, _candidates = resolve_document(catalog, getattr(intent, 'document', None))
    if doc is None:
        return None, None
    rep = excel_store.load_representation(doc['document_id'])
    if rep is None:
        return None, None
    sheet_hint = getattr(intent, 'sheet', None)
    if sheet_hint:
        sheet, _sc = resolve_sheet_nl(rep, sheet_hint)
    elif len(rep.sheets) == 1:
        sheet = rep.sheets[0]
    else:
        sheet, _sc = resolve_sheet_nl(rep, None)      # 多 Sheet -> 不猜
    if sheet is None:
        return None, None
    return rep, sheet


def _unique_numeric_column(message: str,
                           sheet: SheetRepresentation) -> Optional[str]:
    """文本里**唯一**"可数值化且不是业务标识"的列（作为被统计的目标列 / 排行度量）。

    为什么需要它：`longest_resolvable_column` 只看"最长命中"，而
    「金额最高的10个SKU」里 `SKU`（3 字）会命中 `SKU ID`、`金额`（2 字）才是真正的度量列；
    且 18 位 `SKU ID` 也能被 `TRY_CAST` 成数字，不能只看"能否数值化"。
    因此规则是：扫全部可解析列 -> 去掉业务标识列 -> 必须唯一剩下一个，否则 None（不猜）。
    """
    cands: List[str] = []
    for name in nl_norm.resolvable_columns(message, sheet.column_names, COLUMN_ALIASES):
        col = next((c for c in sheet.columns if c.name == name), None)
        if col is None:
            continue
        values = sheet.column_values(col.index)
        if is_identifier_column(col, values):
            continue
        non_empty = [v for v in values if v is not None and str(v).strip() != '']
        numeric = [v for v in non_empty if excel_aggregate.aggregate_to_number(v) is not None]
        if numeric and len(numeric) * 2 >= len(non_empty):
            cands.append(name)
    return cands[0] if len(cands) == 1 else None


def _repair_aggregate_from_text(turn: TurnIntent, message: str,
                                catalog: List[Dict[str, Any]],
                                signals: Optional['nl_norm.IntentSignals'] = None) -> List[str]:
    """确定性补全统计意图里 LLM 漏掉的「目标列 / 筛选 / 分组维度」（只填空，绝不覆盖）。

    真实故障（浏览器验收 + 真实 LLM 稳定性采样，2026-09-17）：
    - 「把订单金额加起来是多少？」   -> LLM 给 sum 但 `column=null` -> 只能澄清；
    - 「一店订单金额平均是多少？」   -> LLM 给 avg 但 `column=null` -> 只能澄清；
    - 「物流商为SF的订单有哪些？」   -> LLM 给 count 且 `filters=[]` -> 变成全表 19 行（应为 4 行）；
    - 「每个物流商分别有多少单？」   -> LLM 漏 `group_by` -> 变成单值 count=19（应为 3 组）；
    - 「一店订单金额最大是多少？」   -> LLM 多加 `group_by=物流商` -> 变成分组统计（应为 35.33）。

    硬约束：
    1. 只在原值**为空**时补（LLM 已给出的参数一律不动，只有"文本里完全没有分组语义"
       却出现 group_by 的明显矛盾才清空）；
    2. 目标列必须唯一解析且**可数值化**（业务标识列不参与统计）；
    3. 筛选值必须**能在真实单元格里找到**（完全相等 -> eq；只是子串 -> contains）；
    4. 分组维度必须在文本里有明确的分组标记（每个/各/按/分别…）且唯一可解析。
    """
    notes: List[str] = []
    agg = turn.aggregate
    if turn.action != ACTION_AGGREGATE or agg is None:
        return notes
    # 只修复 **LLM 产出的** 意图（判据：带原始 JSON）。override / 分页降级 / 两步升级
    # 等路径的意图是程序构造的（例如单元测试直接给出的 AggregateIntent，raw 为空），
    # 用文本去改写它们会误伤显式设置的 group_by（真实回归：清掉了显式构造的分组）。
    if not isinstance(turn.raw, dict) or not turn.raw:
        return notes
    _rep, sheet = _resolve_sheet_for_message(agg, catalog)
    if sheet is None:
        return notes

    def values_of(name: str) -> Sequence[Any]:
        col = next((c for c in sheet.columns if c.name == name), None)
        return sheet.column_values(col.index) if col is not None else []

    def numeric_of(name: str) -> bool:
        """与统计层一致的"可数值化"判定（真实表里 Order Amount 是文本型数字）。"""
        values = values_of(name)
        non_empty = [v for v in values if v is not None and str(v).strip() != '']
        numeric = [v for v in non_empty if excel_aggregate.aggregate_to_number(v) is not None]
        return bool(numeric) and len(numeric) * 2 >= len(non_empty)

    # ① 目标列缺失（sum / avg / min / max 必须有列）
    # 注意：**不能只看 dtype=='number'** —— Order Amount 存的是"文本型数字"
    # （dtype=string，统计层靠 TRY_CAST 处理，SUM 结果 306.02 正确）。
    if (not agg.column and not agg.calculation
            and agg.operation in excel_aggregate.NUMERIC_OPERATIONS):
        target = _unique_numeric_column(message, sheet)
        if target is not None:
            agg.column = target
            notes.append(f'统计目标列=「{target}」（确定性补全）')

    # ② 筛选条件缺失
    if not agg.filters:
        parsed = nl_norm.parse_simple_filter(message, sheet.column_names, COLUMN_ALIASES, values_of)
        if parsed is not None:
            agg.filters = [parsed]
            notes.append(f'筛选={parsed["column"]} {parsed["operator"]} {parsed["value"]!r}（确定性补全）')

    groupable = (excel_aggregate.OPERATION_COUNT,) + excel_aggregate.NUMERIC_OPERATIONS
    if not agg.group_by and signals is not None and signals.group \
            and agg.operation in groupable:
        # ③ 分组维度缺失：文本明确在分组（每个/各/按…），LLM 却给了单值统计
        group_hint = nl_norm.parse_group_hint(message, sheet.column_names, COLUMN_ALIASES,
                                              numeric_of)
        if group_hint is not None:
            agg.group_by = [group_hint]
            notes.append(f'group_by=「{group_hint}」（确定性补全）')
    elif (agg.group_by and signals is not None and not signals.group
            and signals.top_n is None and signals.order_dir is None
            and not nl_norm.WHICH_RE.search(nl_norm.to_halfwidth(message))):
        # ④ 多余的分组维度：文本里既没有分组词、也没有排行/TOP-N/"哪个"，
        #    却给了 group_by（例如「订单金额最大是多少？」被判成分组统计）
        notes.append(f'清空 group_by（文本无分组语义）：{agg.group_by}')
        agg.group_by = []
    return notes


def _deterministic_calculation(message: str, catalog: List[Dict[str, Any]],
                               intent: NlIntent) -> Optional[Dict[str, Any]]:
    """用确定性规则补出逐行计算字段（Phase 4B 的 `A op B`）。

    只在「两侧列名唯一解析成功」时返回，否则 None（调用方再走 LLM 兜底/澄清）。
    文档/Sheet 的解析同样严格：文档必须唯一定位、Sheet 必须唯一（或只有 1 个）；
    解析不到就返回 None，**绝不在这里猜文件**（真正的错误提示由后续链路给出）。
    """
    _rep, sheet = _resolve_sheet_for_message(intent, catalog)
    if sheet is None:
        return None
    parsed = nl_norm.parse_calculation_operands(message, sheet.column_names, COLUMN_ALIASES)
    if parsed is None:
        return None
    op, left_name, right_name = parsed
    # 业务标识列（Order ID / SKU ID…）不参与算术（即使它看起来是数字）
    for name in (left_name, right_name):
        col = next((c for c in sheet.columns if c.name == name), None)
        if col is not None and is_identifier_column(col, sheet.column_values(col.index)):
            return None
    try:
        calc = excel_calculation.normalize_calculation(
            {'operation': op, 'left_column': left_name, 'right_column': right_name})
    except excel_query.ExcelQueryError:
        return None
    return calc.to_dict() if calc is not None else None


def _run_analysis_turn(
    turn: TurnIntent,
    catalog: List[Dict[str, Any]],
    *,
    context: Optional['ExcelQueryContext'] = None,
    analysis_context: Optional['AnalysisContext'] = None,
    user_id: Optional[int] = None,
    session_key: Optional[str] = None,
    document_override: Optional[str] = None,
    user_message: str = '',
) -> Dict[str, Any]:
    """执行一次受控两阶段分析（Step 1 分组统计 + TOP-N → Step 2 再聚合）。

    上下文规则：
    - 本轮显式给出第 1 步 → 按用户说的做；
    - 本轮只给第 2 步 / 明确指代上一轮 → 沿用上一轮的第 1 步（分组、排序、TOP-N 全部不变）；
    - 两者都没有 → clarify，绝不猜。
    """
    from backend.excel import store as excel_store
    from backend.excel.query_context import AnalysisContext

    intent = turn.analysis or AnalysisIntent()
    inherited_from = ''
    inherited_doc_id: Optional[str] = None
    inherited_filename = ''
    inherited_sheet_index: Optional[int] = None
    inherited_sheet_name: Optional[str] = None
    inherited_plan: Optional[Dict[str, Any]] = None

    # 继承条件（显式，无猜测）：
    #   * 用户明确指代上一轮；或
    #   * 本轮没给出步骤；或
    #   * 本轮给出的步骤里**没有第 1 步（分组统计）** —— 属于"只改第 2 步"的追问
    if analysis_context is not None and (
            intent.refers_to_previous
            or not intent.steps
            or not _has_group_step(intent.steps)):
        inherited_from = 'analysis'
        inherited_doc_id = analysis_context.document_id
        inherited_filename = analysis_context.filename
        inherited_sheet_index = analysis_context.sheet_index
        inherited_sheet_name = analysis_context.sheet_name
        inherited_plan = analysis_context.plan

    raw_steps = _analysis_steps_for_execution(intent, inherited_plan)
    if raw_steps and not _has_group_step(raw_steps):
        # 只有"第二步"（或引用了原始列）→ 明确说出能力边界，绝不猜
        return _clarify(
            '本阶段的两步分析只支持「先分组排出前 N 名，再对这 N 名的汇总值做一次聚合」；'
            '暂不支持回到原始订单行重新统计（例如「前 10 个 SKU 对应的所有订单的平均金额」）。'
            '请换一种问法，例如「找出金额最高的10个SKU，并统计它们的总销售额」。',
            stage='analysis',
        )
    if not raw_steps:
        return _clarify(
            '我还不清楚要分析什么。两步分析需要先说清第 1 步（按什么分组、取前几名）'
            '和第 2 步（对前几名做什么汇总），例如「找出金额最高的10个SKU，并统计它们的总销售额」。',
            stage='analysis',
        )
    try:
        plan = excel_multi_step.normalize_analysis_plan({'steps': raw_steps})
    except excel_query.ExcelQueryError as e:
        return _clarify(e.message, stage='analysis')

    # ---- 文件定位（沿用逻辑与统计链路一致） ----
    doc_hint = document_override or intent.document
    rep: Optional[WorkbookRepresentation] = None
    if not doc_hint and inherited_doc_id:
        rep = excel_store.load_representation(inherited_doc_id)
        if rep is None:
            return {'status': STATUS_ERROR,
                    'message': f'上一轮的表格文件「{inherited_filename}」已不存在，请重新发起查询。'}
    else:
        doc, doc_candidates = resolve_document(catalog, doc_hint)
        if doc is None:
            if doc_candidates:
                return _clarify(
                    f'无法确定要对哪个表格做分析，请指定其中之一：{", ".join(doc_candidates)}',
                    doc_candidates, stage='document',
                )
            return _clarify('无法确定要对哪个表格做分析。', stage='document')
        rep = excel_store.load_representation(doc['document_id'])
        if rep is None:
            return {'status': STATUS_ERROR, 'message': f'表格文件「{doc["filename"]}」的表示数据缺失，请重新上传。'}

    # ---- Sheet 定位 ----
    sheet_hint = intent.sheet
    sheet: Optional[SheetRepresentation] = None
    if sheet_hint:
        sheet, sheet_candidates = resolve_sheet_nl(rep, sheet_hint)
        if sheet is None:
            return _clarify(
                f'无法确定分析哪个 Sheet（候选：{", ".join(sheet_candidates) or "无"}）',
                sheet_candidates, stage='schema',
            )
    elif inherited_from and inherited_sheet_index is not None and 0 <= inherited_sheet_index < len(rep.sheets):
        sheet = rep.sheets[inherited_sheet_index]
    if sheet is None:
        if len(rep.sheets) == 1:
            sheet = rep.sheets[0]
        else:
            sheet, cands = resolve_sheet_nl(rep, inherited_sheet_name)
            if sheet is None:
                return _clarify(
                    f'该文件有多个 Sheet，请指定分析哪一个：{", ".join(cands) or ", ".join(s.name for s in rep.sheets)}',
                    cands, stage='schema',
                )

    # ---- 第 1 步 schema 解析（分组列 / 数值列 / 排序键 / TOP-N / 筛选） ----
    s1, s2 = plan.step1, plan.step2
    resolved_group: List[str] = []
    for name in s1.group_by:
        gcol, g_candidates = resolve_column_nl(sheet, name)
        if gcol is None:
            return _clarify(
                f'无法确定分组字段「{name}」（候选：{", ".join(g_candidates[:10]) or "无"}）。',
                g_candidates[:30], stage='schema',
            )
        if gcol not in resolved_group:
            resolved_group.append(gcol)
    if not resolved_group:
        return _clarify('两步分析的第 1 步需要明确按哪一列分组（例如「按 SKU ID 汇总」）。', stage='schema')
    if len(resolved_group) > excel_aggregate.MAX_GROUP_COLUMNS:
        return _clarify(
            f'本阶段最多支持 {excel_aggregate.MAX_GROUP_COLUMNS} 个分组字段，'
            f'当前需要 {len(resolved_group)} 个。',
            resolved_group, stage='schema',
        )

    # Phase 4B：第 1 步的受控计算字段（真实列唯一解析 + 口径歧义澄清）
    resolved_calculation: Optional[Dict[str, Any]] = None
    resolved_column: Optional[str] = None
    if s1.calculation:
        try:
            calc = excel_calculation.ensure_resolved(sheet, s1.calculation)
        except excel_query.ExcelQueryError as e:
            return _clarify(
                f'第 1 步的计算字段无法解析：{e.message}。'
                f'（候选：{", ".join(sheet.column_names[:10])}）',
                sheet.column_names[:30], stage='schema',
            )
        ambiguous = excel_calculation.needs_clarify(user_message, calc, level='aggregate')
        if ambiguous:
            return _clarify(ambiguous, stage='calculation')
        resolved_calculation = calc.to_dict()
    elif s1.column:
        col, col_candidates = resolve_column_nl(sheet, s1.column)
        if col is None:
            return _clarify(
                f'无法确定第 1 步的目标列「{s1.column}」'
                f'（候选：{", ".join(col_candidates[:10]) or "无"}）。',
                col_candidates[:30], stage='schema',
            )
        resolved_column = col

    try:
        resolved_filters = resolve_filters_nl(sheet, s1.filters or [])
    except NlQueryError as e:
        if e.code in ('column_ambiguous_or_missing',):
            return _clarify(e.message, e.details.get('candidates'), stage='schema')
        return {'status': STATUS_ERROR, 'message': e.message, 'details': e.details}

    order_by: Optional[str] = None
    order_dir: Optional[str] = None
    top_n: Optional[int] = None
    try:
        if s1.order_by:
            candidate = s1.order_by
            if not excel_aggregate.is_order_enum(candidate):
                ocol, _ocands = resolve_column_nl(sheet, candidate)
                if ocol:
                    candidate = ocol
            order_by = excel_aggregate.normalize_order_by(candidate, resolved_group)
            order_dir = excel_aggregate.normalize_order_dir(s1.order_dir, order_by)
        top_n = excel_aggregate.normalize_top_n(s1.top_n)
    except excel_query.ExcelQueryError as e:
        return _clarify(e.message, stage='analysis')
    if top_n is not None and order_by is None:
        return _clarify(
            '「前 N 名」需要明确的排序依据（例如「金额最高的10个SKU」），请补充按什么排序。',
            stage='analysis',
        )

    resolved_plan_raw = {
        'steps': [
            {
                'type': excel_multi_step.STEP_GROUP_AGGREGATE,
                'operation': s1.operation,
                'column': resolved_column,
                'calculation': resolved_calculation,
                'group_by': resolved_group,
                'order_by': order_by,
                'order_dir': order_dir,
                'top_n': top_n,
                'filters': resolved_filters,
            },
            {
                'type': excel_multi_step.STEP_AGGREGATE,
                'operation': s2.operation,
                'source': excel_multi_step.SOURCE_STEP_1,
                'column': excel_multi_step.INTERMEDIATE_VALUE_COLUMN,
            },
        ],
    }
    try:
        resolved_plan = excel_multi_step.normalize_analysis_plan(resolved_plan_raw)
    except excel_query.ExcelQueryError as e:
        return _clarify(e.message, stage='analysis')

    # ---- 执行 ----
    try:
        result, engine_used = excel_multi_step.execute_analysis(
            rep, resolved_plan,
            sheet_index=sheet.sheet_index, sheet_name=sheet.sheet_name,
        )
    except excel_query.ExcelQueryError as e:
        if e.code == excel_aggregate.ERR_IDENTIFIER_NOT_NUMERIC:
            identifiers = set(e.details.get('identifier_columns') or [])
            candidates = [c for c in (e.details.get('available_columns') or sheet.column_names)
                          if c not in identifiers]
            return _clarify(e.message, candidates[:30], stage='analysis')
        if e.code in (excel_aggregate.ERR_NOT_NUMERIC_COLUMN,
                      excel_query.ERR_COLUMN_NOT_FOUND,
                      excel_query.ERR_COLUMN_AMBIGUOUS,
                      excel_aggregate.ERR_COLUMN_REQUIRED,
                      excel_aggregate.ERR_TOO_MANY_GROUP_COLUMNS):
            return _clarify(e.message, e.details.get('available_columns') or [], stage='schema')
        return _clarify(e.message, stage='analysis')

    notes: List[str] = []
    if inherited_from:
        notes.append('已沿用上一轮的第 1 步（分组、排序与 TOP-N 不变），只替换了第 2 步的汇总方式')
    message = excel_multi_step.format_analysis_summary(result)
    if notes:
        message += ''.join('\n- 提示：' + n for n in notes)

    new_ctx = AnalysisContext(
        user_id=user_id if user_id is not None else 0,
        session_key=session_key or 'default',
        document_id=rep.document_id,
        filename=rep.filename,
        sheet_index=result.sheet_index,
        sheet_name=result.sheet_name,
        plan=resolved_plan.to_dict(),
        step2_operation=result.step2_operation,
        step2_value=result.step2_value,
        step2_input_rows=result.step2_matched,
    )

    return {
        'status': STATUS_OK,
        'message': message,
        'turn': turn.to_dict(),
        'multi_step': result.to_dict(),
        'engine': engine_used,
        'inherited_from': inherited_from,
        'document': {'document_id': rep.document_id, 'filename': rep.filename, 'file_type': rep.file_type},
        'sheet': {'sheet_index': result.sheet_index, 'sheet_name': result.sheet_name},
        'query': resolved_plan.to_dict(),
        'new_analysis_context': new_ctx.to_dict(),
    }


async def run_nl_query(
    user_message: str,
    catalog: List[Dict[str, Any]],
    *,
    llm=None,
    context: Optional['ExcelQueryContext'] = None,
    aggregate_context: Optional['AggregateContext'] = None,
    analysis_context: Optional['AnalysisContext'] = None,
    intent_override: Optional[NlIntent] = None,
    pagination_override: Optional[TurnIntent] = None,
    aggregate_override: Optional[TurnIntent] = None,
    analysis_override: Optional[TurnIntent] = None,
    document_override: Optional[str] = None,
    user_id: Optional[int] = None,
    session_key: Optional[str] = None,
) -> Dict[str, Any]:
    """NL 查询主入口。

    在 `_run_nl_query_impl` 之外套一层**执行器日志**：
    每次请求都会留下「action -> executor -> status」的证据，便于排查
    "报告说统计通过、浏览器却看到普通查询" 这类路由问题。
    """
    outcome = await _run_nl_query_impl(
        user_message, catalog,
        llm=llm,
        context=context,
        aggregate_context=aggregate_context,
        analysis_context=analysis_context,
        intent_override=intent_override,
        pagination_override=pagination_override,
        aggregate_override=aggregate_override,
        analysis_override=analysis_override,
        document_override=document_override,
        user_id=user_id,
        session_key=session_key,
    )
    turn = outcome.get('turn') or {}
    logger.info(
        '[nl] route action=%s source=%s executor=%s status=%s | %s',
        turn.get('action') or (outcome.get('intent') or {}).get('query_type') or '',
        turn.get('source') or '',
        describe_executor(outcome) or 'none',
        outcome.get('status'),
        (user_message or '')[:80],
    )
    return outcome


async def _run_nl_query_impl(
    user_message: str,
    catalog: List[Dict[str, Any]],
    *,
    llm=None,
    context: Optional['ExcelQueryContext'] = None,
    aggregate_context: Optional['AggregateContext'] = None,
    analysis_context: Optional['AnalysisContext'] = None,
    intent_override: Optional[NlIntent] = None,
    pagination_override: Optional[TurnIntent] = None,
    aggregate_override: Optional[TurnIntent] = None,
    analysis_override: Optional[TurnIntent] = None,
    document_override: Optional[str] = None,
    user_id: Optional[int] = None,
    session_key: Optional[str] = None,
) -> Dict[str, Any]:
    """NL 查询主入口（Phase 1C 新查询 + Phase 1D 连续分页）。

    返回 dict（不抛业务异常）：
      status: ok | clarify | not_excel | error
    附加字段：
      continued    : bool                本轮是否复用了上一轮上下文做分页
      pagination   : {...} | None        分页模式与 offset/limit
      new_context  : {...} | None        供调用方保存的新「上一轮上下文」（仅 status=ok）
    """
    if not catalog:
        return _clarify('你还没有上传任何表格文件，请先在「知识库管理」上传 Excel / CSV 后再试。', stage='catalog')

    # 同名重复上传只保留最新一份（规则见 dedupe_catalog_by_filename）
    catalog = dedupe_catalog_by_filename(catalog)

    # ---------- 0.5) 确定性快路径（后续 2A）：纯分页短语**不调用 LLM** ----------
    # 「下一页 / 再来20条 / 再来二十条 / 第51到100条 / 从第101条开始给我20条」
    # 这些说法完全可由规则确定；只有存在上一轮上下文时才有意义。
    # 收益：省一次 LLM 调用（延迟与额度），并彻底消除这类说法的随机性。
    signals = nl_norm.detect_signals(user_message)
    if (context is not None
            and analysis_override is None and aggregate_override is None
            and pagination_override is None and intent_override is None):
        _fast = deterministic_pagination(user_message)
        if _fast is not None:
            logger.info('[nl] fast-path 分页（未调用 LLM）：%s -> %s',
                        (user_message or '')[:40], _fast.to_dict())
            outcome = _run_pagination(_fast, context)
            outcome['fast_path'] = True
            return outcome

    # ---------- 1) 决定本轮动作（LLM 只做这一步） ----------
    turn: Optional[TurnIntent] = None
    if analysis_override is not None:
        turn = analysis_override
    elif aggregate_override is not None:
        turn = aggregate_override
    elif pagination_override is not None:
        turn = pagination_override
    elif intent_override is not None:
        turn = TurnIntent(action=ACTION_NEW_QUERY, intent=intent_override, source='override')
    elif llm is None:
        # LLM 不可用：有上下文时走严格分页短语降级；否则明确报错（不编造）
        turn = deterministic_pagination(user_message) if context is not None else None
        if turn is None:
            return {'status': STATUS_ERROR, 'message': '自然语言解析不可用（未配置 LLM）'}
    else:
        try:
            turn = await llm_parse_turn(llm, user_message, catalog, context, analysis_context)
        except NlQueryError as e:
            if e.status >= 500:
                # 5xx 的 NlQueryError 都是"上游模型行为异常"（空响应 / 非 JSON 输出），
                # 同样统一成可读 message（技术细节留在 details 里，不进用户可见文案）。
                classified = classify_llm_error(e)
                log_classified('nl_llm_turn', classified)
                return {'status': STATUS_ERROR,
                        'error_code': classified.error_code,
                        'message': f'{classified.message}（{e.code}）',
                        'details': e.details}
            return {'status': STATUS_ERROR, 'message': e.message, 'details': e.details}
        except Exception as e:  # 网络/配额/鉴权等 provider 侧问题
            # 稳定性补丁：**绝不把 provider 原始报文**（英文 JSON、request_id）抛给用户，
            # 统一分类成 error_code + 中文可读 message；原始内容脱敏后只进日志。
            classified = classify_llm_error(e)
            log_classified('nl_llm_turn', classified)
            turn = deterministic_pagination(user_message) if context is not None else None
            if turn is None:
                return {'status': STATUS_ERROR,
                        'error_code': classified.error_code,
                        'message': classified.message,
                        'details': {'kind': classified.kind}}

    # ---------- 1.5) 确定性修补（后续 2A）：只补 LLM 漏掉的、且文本里明确可判定的参数 ----
    # 原则：**只填空**（绝不覆盖 LLM 已给出的值），且只在规则唯一确定时才补。
    repairs = _repair_turn_with_signals(turn, signals)
    # 统计意图的「目标列 / 筛选 / 分组维度」补全（需要真实 schema，因此单独一步）
    repairs.extend(_repair_aggregate_from_text(turn, user_message, catalog, signals))
    # 两步计划第 1 步的统计口径纠正（同一套"按措辞映射"规则）
    repairs.extend(_repair_analysis_plan_from_text(turn, user_message, catalog, signals))
    # ---------- 1.55) 分组排行 TOP-1（「哪个X最多」/「X最多的Y」）----------
    # LLM 的两种真实错答：单值 count=19（应为 Yanwen 12）或 19 行普通表格。
    # 只在下述两种情况下动手，其它一律保持原路径：
    #   ① LLM 给了统计但**没有分组** -> 纠正为分组排行 TOP-1；
    #   ② LLM 给的是普通查询 -> 升级为分组排行 TOP-1。
    _top_group = _deterministic_top_group_turn(
        user_message, catalog, signals, turn.aggregate or turn.intent)
    if _top_group is not None:
        if turn.action == ACTION_AGGREGATE and turn.aggregate is not None \
                and not turn.aggregate.group_by:
            turn = _top_group
            repairs.append('分组排行 TOP-1（确定性纠正单值统计）')
        elif turn.action == ACTION_NEW_QUERY:
            turn = _top_group
            repairs.append('分组排行 TOP-1（确定性升级普通查询）')
    if repairs:
        logger.info('[nl] turn 确定性修补：%s', '; '.join(repairs))
    turn, downgrade_note = _plain_list_downgrade(turn, signals)
    if downgrade_note:
        logger.info('[nl] %s（纯列表问题不进入统计链路）', downgrade_note)
        repairs.append(downgrade_note)

    # ---------- 1.6) 计算字段兜底（后续 2A）----------
    # 「一店每笔订单的金额/数量」这类**逐行计算**问法，LLM 偶尔只给普通查询而漏掉
    # calculation（用户就会看到原始列）。这里用同一个"参数抽取器"补一次，
    # 且**只取 calculation / document**；任何失败都原样返回（不劣化）。
    if (turn.action == ACTION_NEW_QUERY and turn.intent is not None
            and turn.intent.calculation is None and not turn.intent.calc_error
            and signals.calc_ops and signals.row_calc
            and not signals.any_stat):
        # ① 确定性优先（后续 2A）：直接从「A 除以 B」「A/B」里切出左右真实列，
        #    不需要 LLM，"同一句话"不会因为采样而给出不同结果。
        det_calc = _deterministic_calculation(user_message, catalog, turn.intent)
        if det_calc is not None:
            turn.intent.calculation = det_calc
            logger.info('[nl] 计算字段确定性解析：%s', det_calc.get('label'))
            repairs.append('calculation（确定性 A op B）')
        elif llm is not None:
            # ② 规则解析不出来时才问 LLM（只取 calculation / document；失败不劣化）
            guard_calc = await llm_parse_aggregate(llm, user_message, catalog, context)
            if guard_calc is not None and guard_calc.calculation:
                turn.intent.calculation = guard_calc.calculation
                if not turn.intent.document and guard_calc.document:
                    turn.intent.document = guard_calc.document
                logger.info('[nl] 计算字段兜底：%s',
                            (guard_calc.calculation or {}).get('label'))
                repairs.append('calculation（逐行计算兜底）')

    # ---------- 2) 分页动作：必须复用上一轮上下文 ----------
    if turn.action in PAGINATION_ACTIONS:
        if context is None:
            return _clarify(
                '当前没有可继续的表格查询（最近一轮不是表格查询）。'
                '请先说明要查询哪个表格，例如「列出5店前20条SKU」。',
                stage='pagination',
            )
        return _run_pagination(turn, context)

    # ---------- 2.25) 2A-P0 统一名次守卫（O2 收口：堵住三条 analysis 绕过路径）----------
    # 位置是关键：必须在 2.3（确定性升级 / analysis_guard）与 2.4（ACTION_ANALYSIS 分派）
    # **之前**。实测 bypass：这三条路径都是先 `_run_analysis_turn(...)` 再 `return`，
    # 因此 2.45 的名次守卫对它们**完全不可见**
    # （「排名前三的订单的总和是多少」曾被 LLM 猜出 Order Amount 后直接执行并返回真实数值）。
    # 只对 aggregate / analysis 且命中"显式名次"或 analysis 动作的轮次生效，其余动作不动。
    if (turn is not None
            and turn.action in (ACTION_AGGREGATE, ACTION_ANALYSIS)
            and (turn.action == ACTION_ANALYSIS
                 or nl_norm.EXPLICIT_RANK_RE.search(user_message))):
        _pre_turn, _pre_notes, _pre_clarify = _guard_rank_semantics(
            turn, user_message, catalog, signals)
        if _pre_notes:
            logger.info('[nl] 名次语义（2.25 统一守卫 2A-P0）：%s', '; '.join(_pre_notes))
            repairs.extend(_pre_notes)
        if _pre_clarify is not None:
            return _pre_clarify
        turn = _pre_turn

    # ---------- 2.3) 两步分析兜底（Phase 4A）----------
    # 必须在 analysis / aggregate / new_query 分发**之前**：
    # LLM 可能把「先排行、再对前 N 名汇总」判成 aggregate 或 new_query，
    # 这里先用确定性词元判定，再做一次 temperature=0 的「只输出两阶段计划」调用；
    # 失败则原样回退原路径（绝不劣化）。
    _analysis_followup = bool(analysis_context is not None
                              and looks_like_analysis_followup(user_message))
    # LLM 自己判成 analysis、但第 1 步缺少"聚合目标"（既无列也无计算字段）时，
    # 也用专用抽取器再试一次（否则只能澄清，属于白丢一次可回答的机会）。
    _analysis_broken = (turn.action == ACTION_ANALYSIS
                        and _analysis_plan_missing_target(turn.analysis))
    # 确定性升级（**不调用 LLM**）：LLM 已给出「分组 + TOP-N」的单步统计，
    # 但用户在话里明确要的是前 N 名的总量（总和/合计/平均…）-> 直接补上第 2 步。
    # 收益：省一次 LLM 调用，且第 2 步口径完全由用户措辞决定（不存在随机性）。
    if turn.action == ACTION_AGGREGATE and looks_like_multi_step_query(user_message):
        upgraded = _aggregate_to_analysis_turn(turn, user_message)
        if upgraded is not None:
            logger.info('[nl] aggregate -> 两步分析（确定性升级，未调用 LLM）：step2=%s',
                        (upgraded.analysis.steps[1] or {}).get('operation'))
            upgraded_outcome = _run_analysis_turn(
                upgraded, catalog,
                context=context,
                analysis_context=analysis_context,
                user_id=user_id,
                session_key=session_key,
                document_override=document_override,
                user_message=user_message,
            )
            if upgraded_outcome.get('status') == STATUS_OK:
                upgraded_outcome['analysis_upgraded'] = True
                return upgraded_outcome
            logger.info('[nl] 确定性升级未成功（status=%s），回退原路径：%s',
                        upgraded_outcome.get('status'), upgraded_outcome.get('message'))
    if (turn.action in (ACTION_NEW_QUERY, ACTION_AGGREGATE, ACTION_ANALYSIS)
            or (turn.action == ACTION_CLARIFY and _analysis_followup)) \
            and llm is not None \
            and (looks_like_multi_step_query(user_message) or _analysis_followup
                 or _analysis_broken):
        guard_analysis = await llm_parse_analysis(llm, user_message, catalog, context,
                                                 analysis_context)
        if guard_analysis is not None and guard_analysis.steps:
            guard_turn = TurnIntent(
                action=ACTION_ANALYSIS, analysis=guard_analysis, source='analysis_guard',
            )
            guard_outcome = _run_analysis_turn(
                guard_turn, catalog,
                context=context,
                analysis_context=analysis_context,
                user_id=user_id,
                session_key=session_key,
                document_override=document_override,
                user_message=user_message,
            )
            if guard_outcome.get('status') == STATUS_OK:
                guard_outcome['analysis_guard'] = True
                return guard_outcome
            logger.info('[nl] 两步分析兜底未成功（status=%s），回退原路径：%s',
                        guard_outcome.get('status'), guard_outcome.get('message'))

    # ---------- 2.35) 统计意图兜底（后续 2A：提前到 clarify 之前） ----------
    # 若 LLM 把明确的统计问题判成了「新查询」或「澄清」，这里用确定性词元判定拦住，
    # 并再做一次 temperature=0 的"只抽统计参数"调用；成功则改走统计链路，
    # 失败则**原样回退**原路径（绝不劣化）。
    if (turn.action in (ACTION_NEW_QUERY, ACTION_CLARIFY)
            and llm is not None
            and (turn.intent is None or turn.intent.query_type != INTENT_NOT_EXCEL)
            and not _row_calc_only(user_message)
            and looks_like_statistical_query(user_message)):
        guard_agg = await llm_parse_aggregate(llm, user_message, catalog, context)
        if guard_agg is not None:
            guard_turn = TurnIntent(
                action=ACTION_AGGREGATE, aggregate=guard_agg, source='statistical_guard',
            )
            # 2A-P0（O2 修复）：统计守卫的 LLM 结果**同样必须**过名次守卫。
            # 否则「查看排名前三订单」会在这里被补出指标（Order Amount）后直接执行成
            # TOP-3，绕过 2.45 的名次语义规则（实测根因：本路径会 return，不再往下走）。
            guard_turn, _gnotes, _gclarify = _guard_rank_semantics(
                guard_turn, user_message, catalog, signals)
            if _gnotes:
                logger.info('[nl] 名次语义（统计守卫路径 2A-P0）：%s', '; '.join(_gnotes))
            if _gclarify is not None:
                return _gclarify
            if guard_turn.action != ACTION_AGGREGATE:
                # 名次守卫把统计兜底改写成了别的动作（如「前N名」-> 行级截取）：
                # 放弃本条统计兜底，交给后续统一分派（2.45 会对原 turn 再判一次，幂等）。
                logger.info('[nl] 统计兜底被 2A-P0 名次守卫改写为 %s，交回统一分派',
                            guard_turn.action)
            else:
                guard_outcome = _run_aggregate_turn(
                    guard_turn, catalog,
                    context=context,
                    aggregate_context=aggregate_context,
                    user_id=user_id,
                    session_key=session_key,
                    document_override=document_override,
                )
                if guard_outcome.get('status') == STATUS_OK:
                    guard_outcome['statistical_guard'] = True
                    return guard_outcome
                logger.info('[nl] 统计兜底未成功（status=%s），回退原路径：%s',
                            guard_outcome.get('status'), guard_outcome.get('message'))

    # ---------- 2.4) 多步分析（Phase 4A）：两阶段，与 Phase 3 上下文隔离 ----------
    if turn.action == ACTION_ANALYSIS:
        # LLM 过度规划时降级为单步分组统计：用户文本里没有"第二步汇总"语义
        #（且本轮不是对上一轮多步结果的追问）-> 不应把 N 行明细压成一个数字。
        if not looks_like_multi_step_query(user_message) and not _analysis_followup:
            downgraded = _analysis_step1_to_aggregate_turn(turn)
            if downgraded is not None:
                logger.info('[nl] analysis 过度规划 -> 降级为单步分组统计（无第二步汇总语义）')
                downgraded_outcome = _run_aggregate_turn(
                    downgraded, catalog,
                    context=context,
                    aggregate_context=aggregate_context,
                    user_id=user_id,
                    session_key=session_key,
                    document_override=document_override,
                    user_message=user_message,
                )
                downgraded_outcome['analysis_downgraded'] = True
                return downgraded_outcome
        return _run_analysis_turn(
            turn, catalog,
            context=context,
            analysis_context=analysis_context,
            user_id=user_id,
            session_key=session_key,
            document_override=document_override,
            user_message=user_message,
        )

    # ---------- 2.45) 2A-P0 守卫：时间语义 + 中文名次语义 ----------
    # 必须放在【统计守卫 / 分析升级】之后：这些守卫会再次调用 LLM 并把 NEW_QUERY
    # 重新路由成 AGGREGATE（例如「查看排名前三订单」被判为统计）——若前置执行，
    # 2A-P0 的判定会被随后的重路由绕过（实测故障）。
    # 铁律：宁可澄清，绝不静默出错 ——
    #   ① 时间条件不得退化为 contains（否则「8月19日」会变成 0 行或全表假命中）；
    #   ② 名次语义不得静默降级为无排序的全表 COUNT。
    time_notes, time_clarify = _apply_temporal_repair(turn, user_message, catalog)
    if time_clarify is not None:
        return time_clarify
    if time_notes:
        logger.info('[nl] 时间条件（2A-P0）：%s', '; '.join(time_notes))
        repairs.extend(time_notes)

    turn, rank_notes, rank_clarify = _guard_rank_semantics(
        turn, user_message, catalog, signals)
    if rank_notes:
        logger.info('[nl] 名次语义（2A-P0）：%s', '; '.join(rank_notes))
        repairs.extend(rank_notes)
    if rank_clarify is not None:
        return rank_clarify

    # ---------- 2.5) 统计动作（Phase 3A）：与分页上下文完全隔离 ----------
    if turn.action == ACTION_AGGREGATE:
        return _run_aggregate_turn(
            turn, catalog,
            context=context,
            aggregate_context=aggregate_context,
            user_id=user_id,
            session_key=session_key,
            document_override=document_override,
            user_message=user_message,
        )

    # ---------- 3) 非表格 / 澄清 ----------
    if turn.action == ACTION_NOT_EXCEL:
        return {'status': STATUS_NOT_EXCEL, 'message': turn.clarification or '该问题不是表格查询'}
    if turn.action == ACTION_CLARIFY:
        # 没有可用上下文，且用户说的确实是纯分页指令 -> 给出明确、可操作的提示
        # （而不是让用户看到一句泛泛的"信息不足"）
        if context is None and deterministic_pagination(user_message) is not None:
            return _clarify(
                '当前没有可继续的表格查询（最近一轮不是表格查询）。'
                '请先说明要查询哪个表格，例如「列出5店前20条SKU」。',
                stage='pagination',
            )
        # Phase 3C：上一轮是分组统计，且用户说的是「再看前N个」-> 明确是改 TOP-N（确定性兜底）
        if aggregate_context is not None:
            follow_top_n = deterministic_topn_followup(user_message)
            if follow_top_n is not None:
                follow_turn = TurnIntent(
                    action=ACTION_AGGREGATE,
                    aggregate=AggregateIntent(top_n=follow_top_n, refers_to_previous=True),
                    source='deterministic',
                )
                return _run_aggregate_turn(
                    follow_turn, catalog,
                    context=context,
                    aggregate_context=aggregate_context,
                    user_id=user_id,
                    session_key=session_key,
                    document_override=document_override,
                )
        if turn.action == ACTION_CLARIFY:
            return _clarify(turn.clarification or '请补充要查询的文件、Sheet 或列。', stage='llm')

    # ---------- 4) 新查询（Phase 1C 原路径，保持不变） ----------
    intent = turn.intent or NlIntent()
    if intent.query_type == INTENT_NOT_EXCEL:
        return {'status': STATUS_NOT_EXCEL, 'message': intent.clarification or '该问题不是表格查询'}
    if intent.query_type == INTENT_CLARIFY:
        return _clarify(intent.clarification or '请补充要查询的文件、Sheet 或列。', stage='llm')
    # Phase 4B：非法计算字段必须明确告知（绝不静默降级成普通查询）
    if intent.calc_error:
        return _clarify(intent.calc_error, stage='calculation')

    # 文件定位（确定性匹配，不猜）
    doc_hint = document_override or intent.document
    doc, doc_candidates = resolve_document(catalog, doc_hint)
    if doc is None:
        if doc_candidates:
            return _clarify(
                f'无法确定要查询哪个表格文件，请指定其中之一：{", ".join(doc_candidates)}',
                doc_candidates, stage='document',
            )
        return _clarify('无法确定要查询哪个表格文件。', stage='document')

    # 载入 representation（Phase 1A 产物；不重解析原文件）
    from backend.excel import store as excel_store
    rep = excel_store.load_representation(doc['document_id'])
    if rep is None:
        return {'status': STATUS_ERROR, 'message': f'表格文件「{doc["filename"]}」的表示数据缺失，请重新上传。'}

    # schema 校验 -> Phase 1B payload
    try:
        payload = build_validated_query(intent, rep)
    except NlQueryError as e:
        if e.code in ('sheet_ambiguous_or_missing', 'column_ambiguous_or_missing'):
            return _clarify(e.message, e.details.get('candidates'), stage='schema')
        return {'status': STATUS_ERROR, 'message': e.message, 'details': e.details}

    # 取数：完全交给结构化查询引擎（DuckDB / Python，均不经过 LLM）
    try:
        payload, result, engine_used, relax_notes = _execute_with_prefix_relaxation(rep, payload)
    except excel_query.ExcelQueryError as e:
        return {'status': STATUS_ERROR, 'message': e.message, 'details': e.details}

    new_ctx = ExcelQueryContext(
        user_id=user_id if user_id is not None else (context.user_id if context else 0),
        session_key=session_key if session_key is not None else (context.session_key if context else 'default'),
        document_id=rep.document_id,
        filename=rep.filename,
        sheet_index=result.sheet_index,
        sheet_name=result.sheet_name,
        columns=list(payload.get('columns') or []),
        filters=list(payload.get('filters') or []),
        limit=payload['limit'],
        offset=payload['offset'],
        total_matches=result.total_matches,
        total_rows_in_sheet=result.total_rows_in_sheet,
    )

    return {
        'status': STATUS_OK,
        'message': format_summary(result, doc['filename'])
                   + (''.join('\n- 提示：' + n for n in relax_notes) if relax_notes else ''),
        'intent': intent.to_dict(),
        'turn': turn.to_dict(),
        'continued': False,
        'engine': engine_used,
        'relaxed_filters': relax_notes,
        'pagination': None,
        'document': {'document_id': rep.document_id, 'filename': rep.filename, 'file_type': rep.file_type},
        'sheet': {'sheet_index': result.sheet_index, 'sheet_name': result.sheet_name},
        'query': payload,
        'result': result.to_dict(),
        'new_context': new_ctx.to_dict(),
    }
