# -*- coding: utf-8 -*-
"""Phase 3A：Excel 基础统计（COUNT / SUM / AVG / MIN / MAX）

严格遵守的链路：

    用户自然语言
        ↓ （LLM：只识别统计意图 / 文件 / Sheet / 目标列 / 筛选条件）
    AggregateRequest
        ↓ Python schema 校验（列必须唯一解析；数值列必须真的可数值化）
    QueryPlan（本模块内的聚合计划，纯 Python，不含 SQL 文本）
        ↓ Python 渲染受控 SQL（标识符全为 Python 生成；筛选值全部 ? 绑定）
    DuckDB
        ↓
    真实统计值

边界（本阶段刻意不做）：
- 不实现 GROUP BY / ORDER BY / TOP-N / JOIN / 计算字段 / 同比环比；
- 不实现 COUNT(DISTINCT ...)：COUNT 语义固定为「符合筛选条件的数据行数」。

统计口径（显式定义，前端与测试均以此为准）：
1. matched_rows   = 命中筛选条件的行数（= COUNT）。
2. 数值操作（SUM/AVG/MIN/MAX）只对**可数值化**的单元格计算：
     numeric_rows      可数值化（含文本数字 "1" / "10.5"）
     empty_rows        命中但单元格为空（None）—— 不计入、也不当 0
     non_numeric_rows  命中但不可数值化（如 "abc"）—— 不计入、也不当 0
   且恒有 matched_rows = numeric_rows + empty_rows + non_numeric_rows。
3. 若 numeric_rows == 0：value = None（**不会**编造 0），并给出明确说明。
   —— 注意这与「真实求和结果恰好是 0」不同：后者会如实返回 0。
4. 若目标列在**整个 Sheet** 中没有任何可数值化单元格 -> 明确报错
   （ERR_NOT_NUMERIC_COLUMN），绝不返回一个看起来像数字的错值。
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.excel import calculation
from backend.excel import duck as duck_engine
from backend.excel import query as excel_query
from backend.excel import representation as representation_module
from backend.excel.representation import (
    SheetRepresentation,
    WorkbookRepresentation,
    is_identifier_column,
)

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
OPERATION_COUNT = 'count'
OPERATION_SUM = 'sum'
OPERATION_AVG = 'avg'
OPERATION_MIN = 'min'
OPERATION_MAX = 'max'

SUPPORTED_OPERATIONS: Tuple[str, ...] = (
    OPERATION_COUNT, OPERATION_SUM, OPERATION_AVG, OPERATION_MIN, OPERATION_MAX,
)
#: 需要目标数值列的操作
NUMERIC_OPERATIONS: Tuple[str, ...] = (
    OPERATION_SUM, OPERATION_AVG, OPERATION_MIN, OPERATION_MAX,
)

OPERATION_LABELS: Dict[str, str] = {
    OPERATION_COUNT: 'COUNT（行数）',
    OPERATION_SUM: 'SUM（求和）',
    OPERATION_AVG: 'AVG（平均）',
    OPERATION_MIN: 'MIN（最小值）',
    OPERATION_MAX: 'MAX（最大值）',
}

ERR_OPERATION_INVALID = 'operation_invalid'
ERR_COLUMN_REQUIRED = 'column_required'
ERR_NOT_NUMERIC_COLUMN = 'not_numeric_column'
#: 稳定性补丁：对"业务标识列"（18 位 Order ID / SKU ID 等）做数值聚合
ERR_IDENTIFIER_NOT_NUMERIC = 'identifier_not_numeric'
ERR_TOO_MANY_GROUP_COLUMNS = 'too_many_group_columns'
# Phase 3C
ERR_ORDER_INVALID = 'order_invalid'
ERR_ORDER_REQUIRES_GROUP = 'order_requires_group'
ERR_TOP_N_INVALID = 'top_n_invalid'
ERR_TOP_N_REQUIRES_ORDER = 'top_n_requires_order'

#: 单次统计最多回传多少条命中行号（避免超大表把响应撑爆；行数与区间始终准确）
MAX_ROW_NUMBERS = 20000

#: 空值分组在后端的统一展示文本（前端不得自行猜测）
GROUP_NULL_LABEL = '<空>'
#: 最多允许的分组列数（避免多级分组语义发散；本阶段仅做单/少量列分组）
MAX_GROUP_COLUMNS = 3

# ----------------------------------------------------------------------------
# Phase 3C：ORDER BY / TOP-N
# ----------------------------------------------------------------------------
#: 排序键（**固定枚举**，绝不接受用户/LLM 的自由文本进入 ORDER BY）
ORDER_BY_AGGREGATE = 'aggregate_value'
ORDER_BY_GROUP_PREFIX = 'group_column_'
#: 排序方向（严格白名单）
ORDER_ASC = 'asc'
ORDER_DESC = 'desc'
SUPPORTED_ORDER_DIRS: Tuple[str, ...] = (ORDER_ASC, ORDER_DESC)

#: TOP-N 允许范围（Python 强校验，超界直接拒绝；也用于自然语言层）
MIN_TOP_N = 1
MAX_TOP_N = 200

#: 排序方向的中文标签（Python 生成，前端不猜）
ORDER_DIR_LABELS: Dict[str, str] = {ORDER_ASC: '从低到高（升序）', ORDER_DESC: '从高到低（降序）'}
#: 排序方向短语（用于摘要）
ORDER_DIR_PHRASES: Dict[str, str] = {ORDER_ASC: '升序', ORDER_DESC: '降序'}


# ----------------------------------------------------------------------------
# 数值转换（与 DuckDB TRY_CAST(x AS DOUBLE) 语义对齐）
# ----------------------------------------------------------------------------
def aggregate_to_number(value: Any) -> Optional[float]:
    """可数值化判定：与 DuckDB ``TRY_CAST(col AS DOUBLE)`` 保持一致。

    - None / 空串 / 纯空白 / 非数值文本 -> None（不参与统计，且绝不当作 0）
    - bool -> 1.0 / 0.0（DuckDB 的 BOOLEAN->DOUBLE 转换）
    - 文本数字 "1" / "2" / "10.5" / " 3 " / "1e5" -> 数值
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def format_number(value: Optional[float], is_count: bool = False) -> str:
    """把统计值渲染成人类可读字符串（Python 生成，不经过 LLM）。"""
    if value is None:
        return '无有效数值'
    if is_count:
        return str(int(value))
    if float(value).is_integer():
        return str(int(value))
    text = f'{float(value):.6f}'.rstrip('0').rstrip('.')
    return text or '0'


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------
@dataclass
class AggregateRequest:
    """已校验的统计请求（列名/Sheet 均已解析为真实 schema 中的名字）。"""

    document_id: str
    user_id: Optional[int] = None
    sheet_index: Optional[int] = None
    sheet_name: Optional[str] = None
    operation: str = OPERATION_COUNT
    column: Optional[str] = None            # 真实列名；COUNT 为 None
    column_index: Optional[int] = None
    column_letter: Optional[str] = None
    filters: List[Dict[str, Any]] = field(default_factory=list)
    # 目标列在整表中的数值分布（用于追溯与"非数值列"判定）
    column_numeric_in_sheet: int = 0
    column_empty_in_sheet: int = 0
    column_non_numeric_in_sheet: int = 0
    # Phase 3B：分组列（已解析的真实列名；空列表 = Phase 3A 单值统计）
    group_by: List[str] = field(default_factory=list)
    group_by_indexes: List[int] = field(default_factory=list)
    group_by_letters: List[str] = field(default_factory=list)
    # Phase 3C：排序 + TOP-N（order_by 只能是固定枚举；None = 不排序）
    order_by: Optional[str] = None
    order_dir: str = ORDER_ASC
    top_n: Optional[int] = None
    # 每个分组列在整表中的物理类型（供 Python 参考实现复现 DuckDB 的排序语义）
    group_by_phys_types: List[str] = field(default_factory=list)
    # Phase 4B：受控计算字段（计算值取代 column 参与聚合）
    calculation: Optional[Dict[str, Any]] = None
    calc_expr: Optional[str] = None
    calc_numeric_in_sheet: int = 0

    @property
    def is_grouped(self) -> bool:
        return bool(self.group_by)

    @property
    def is_sorted(self) -> bool:
        return self.order_by is not None

    @property
    def order_group_index(self) -> Optional[int]:
        """order_by 指向的分组列下标（order_by=aggregate_value 时为 None）。"""
        if not self.order_by or not self.order_by.startswith(ORDER_BY_GROUP_PREFIX):
            return None
        try:
            return int(self.order_by[len(ORDER_BY_GROUP_PREFIX):])
        except ValueError:  # pragma: no cover - 解析阶段已保证
            return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'document_id': self.document_id,
            'operation': self.operation,
            'sheet_index': self.sheet_index,
            'sheet_name': self.sheet_name,
            'column': self.column,
            'column_index': self.column_index,
            'column_letter': self.column_letter,
            'filters': self.filters,
            'group_by': self.group_by,
            'order_by': self.order_by,
            'order_dir': self.order_dir,
            'top_n': self.top_n,
        }


@dataclass
class AggregateResult:
    """统计结果（含完整来源信息，保证可追溯）。"""

    document_id: str
    sheet_index: int
    sheet_name: str
    operation: str
    column: Optional[str]
    column_index: Optional[int]
    column_letter: Optional[str]
    filters: List[Dict[str, Any]]
    value: Optional[float]                  # COUNT 为整数行数
    matched_rows: int
    numeric_rows: int
    empty_rows: int
    non_numeric_rows: int
    total_rows_in_sheet: int
    row_excel_numbers: List[int]
    row_excel_span: Optional[Dict[str, int]]
    row_excel_truncated: bool
    column_numeric_in_sheet: int
    definition: str
    # Phase 4B：受控计算字段（无则为 None）
    calculation: Optional[Dict[str, Any]] = None

    @property
    def is_count(self) -> bool:
        return self.operation == OPERATION_COUNT

    def value_display(self) -> str:
        return format_number(self.value, is_count=self.is_count)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'document_id': self.document_id,
            'sheet_index': self.sheet_index,
            'sheet_name': self.sheet_name,
            'operation': self.operation,
            'operation_label': OPERATION_LABELS.get(self.operation, self.operation),
            'column': self.column,
            'column_index': self.column_index,
            'column_letter': self.column_letter,
            'filters': self.filters,
            'applied_filters': self.filters,       # 与 Phase 2 命名对齐，便于前端复用
            'value': self.value,
            'value_display': self.value_display(),
            'matched_rows': self.matched_rows,
            'numeric_rows': self.numeric_rows,
            'empty_rows': self.empty_rows,
            'non_numeric_rows': self.non_numeric_rows,
            'total_rows_in_sheet': self.total_rows_in_sheet,
            'row_excel_numbers': self.row_excel_numbers,
            'row_excel_spans': self.row_excel_span,
            'row_excel_truncated': self.row_excel_truncated,
            'column_numeric_in_sheet': self.column_numeric_in_sheet,
            'definition': self.definition,
            'calculation': self.calculation,
        }


# ----------------------------------------------------------------------------
# Phase 3B：分组统计的数据结构
# ----------------------------------------------------------------------------
@dataclass
class GroupCell:
    """分组字段的一个取值（展示文本由后端决定，前端不得自行猜测）。"""

    column: str
    value: Any                      # 原值（可能为 None）
    display: str                    # 展示文本：None -> '<空>'
    is_null: bool

    def to_dict(self) -> Dict[str, Any]:
        return {'column': self.column, 'value': self.value,
                'display': self.display, 'is_null': self.is_null}


@dataclass
class GroupRow:
    """一个分组的结果行。"""

    group: List[GroupCell]
    value: Optional[float]           # COUNT 为行数；数值操作为 float 或 None
    matched_rows: int
    numeric_rows: int
    empty_rows: int
    non_numeric_rows: int

    def key(self) -> Tuple[Any, ...]:
        """分组键（用于与独立 Ground Truth 做逐组比较；空值为 None）。"""
        return tuple(c.value for c in self.group)

    def to_dict(self, operation: str) -> Dict[str, Any]:
        is_count = operation == OPERATION_COUNT
        return {
            'group': [c.to_dict() for c in self.group],
            'group_key': [c.value for c in self.group],
            'group_display': [c.display for c in self.group],
            'value': self.value,
            'value_display': format_number(self.value, is_count=is_count),
            'matched_rows': self.matched_rows,
            'numeric_rows': self.numeric_rows,
            'empty_rows': self.empty_rows,
            'non_numeric_rows': self.non_numeric_rows,
        }


@dataclass
class GroupedAggregateResult:
    """分组统计结果（与 Phase 3A 单值结果结构分离，避免硬塞）。"""

    document_id: str
    sheet_index: int
    sheet_name: str
    operation: str
    column: Optional[str]
    column_index: Optional[int]
    column_letter: Optional[str]
    group_by: List[Dict[str, Any]]           # [{'name','index','letter'}]
    filters: List[Dict[str, Any]]
    rows: List[GroupRow]
    matched_rows: int                        # 所有分组匹配行数之和
    total_rows_in_sheet: int
    row_excel_span: Optional[Dict[str, int]]
    column_numeric_in_sheet: int
    definition: str
    # Phase 3C：排序 + TOP-N
    order_by: Optional[str] = None           # 'aggregate_value' | 'group_column_<i>' | None
    order_dir: str = ORDER_ASC
    top_n: Optional[int] = None
    total_groups_before_top: int = 0         # 截断前的分组总数
    # Phase 4B：受控计算字段（无则为 None）
    calculation: Optional[Dict[str, Any]] = None

    @property
    def returned_groups(self) -> int:
        return len(self.rows)

    @property
    def total_groups(self) -> int:
        """截断前的分组总数（无 TOP-N 时 == returned_groups，保持 Phase 3B 语义）。"""
        return self.total_groups_before_top or len(self.rows)

    @property
    def is_sorted(self) -> bool:
        return self.order_by is not None

    @property
    def is_truncated(self) -> bool:
        return self.top_n is not None and self.total_groups > self.returned_groups

    def order_by_label(self) -> str:
        """排序键的中文标签（Python 生成）。"""
        if not self.order_by:
            return ''
        if self.order_by == ORDER_BY_AGGREGATE:
            return _aggregate_value_label(self.operation, self.column, self.calculation)
        idx = self.order_group_index()
        if idx is not None and idx < len(self.group_by):
            return self.group_by[idx]['name']
        return self.order_by

    def order_group_index(self) -> Optional[int]:
        if not self.order_by or not self.order_by.startswith(ORDER_BY_GROUP_PREFIX):
            return None
        try:
            return int(self.order_by[len(ORDER_BY_GROUP_PREFIX):])
        except ValueError:  # pragma: no cover
            return None

    def sort_description(self) -> str:
        """一句话说清排序与 TOP-N（前端直接展示，不做推理）。"""
        if not self.is_sorted:
            return '未做排序（结果顺序为数据库返回顺序）'
        dir_phrase = ORDER_DIR_PHRASES.get(self.order_dir, self.order_dir)
        text = f'已按「{self.order_by_label()}」{dir_phrase}排列'
        if self.group_by:
            text += '（相同值时按分组字段升序稳定排序）'
        if self.top_n is not None:
            text += f'，并截取前 {self.top_n} 个分组'
        return text

    def to_dict(self) -> Dict[str, Any]:
        group_index = self.order_group_index()
        return {
            'kind': 'group_aggregate',
            'document_id': self.document_id,
            'sheet_index': self.sheet_index,
            'sheet_name': self.sheet_name,
            'operation': self.operation,
            'operation_label': OPERATION_LABELS.get(self.operation, self.operation),
            'column': self.column,
            'column_index': self.column_index,
            'column_letter': self.column_letter,
            'group_by': self.group_by,
            'filters': self.filters,
            'applied_filters': self.filters,
            'rows': [r.to_dict(self.operation) for r in self.rows],
            'total_groups': self.total_groups,
            'returned_groups': self.returned_groups,
            'matched_rows': self.matched_rows,
            'total_rows_in_sheet': self.total_rows_in_sheet,
            'row_excel_spans': self.row_excel_span,
            'column_numeric_in_sheet': self.column_numeric_in_sheet,
            'definition': self.definition,
            # ---- Phase 3C ----
            'sorted': self.is_sorted,
            'order_by': self.order_by,
            'order_by_label': self.order_by_label(),
            'order_by_column': (self.group_by[group_index]['name']
                                if group_index is not None and group_index < len(self.group_by)
                                else None),
            'order_dir': self.order_dir if self.is_sorted else None,
            'order_dir_label': ORDER_DIR_LABELS.get(self.order_dir, '') if self.is_sorted else '',
            'top_n': self.top_n,
            'truncated_by_top_n': self.is_truncated,
            'sort_description': self.sort_description(),
            'calculation': self.calculation,
        }


def _group_cells(sheet: SheetRepresentation, indexes: Sequence[int], row: Sequence[Any]) -> List[GroupCell]:
    cells: List[GroupCell] = []
    for i in indexes:
        col = sheet.columns[i]
        raw = row[i] if i < len(row) else None
        cells.append(GroupCell(
            column=col.name,
            value=raw,
            display=GROUP_NULL_LABEL if raw is None else str(raw),
            is_null=raw is None,
        ))
    return cells


# ----------------------------------------------------------------------------
# 解析与校验
# ----------------------------------------------------------------------------
def normalize_operation(value: Any) -> str:
    if not isinstance(value, str):
        raise excel_query.ExcelQueryError(ERR_OPERATION_INVALID, 'operation 必须是字符串')
    op = value.strip().lower()
    if op not in SUPPORTED_OPERATIONS:
        raise excel_query.ExcelQueryError(
            ERR_OPERATION_INVALID,
            f'不支持的统计操作 {value!r}，本阶段仅支持 {list(SUPPORTED_OPERATIONS)}',
            {'supported': list(SUPPORTED_OPERATIONS)},
        )
    return op


#: 排序键「聚合值」的等价写法（**仍然是有界枚举**，不是自由文本）
_ORDER_BY_AGG_ALIASES = frozenset({
    'aggregate_value', 'value', 'agg', 'aggregate', '聚合值', '统计值', '值',
})
#: 排序方向的等价写法（有界枚举）
_DIR_ALIASES: Dict[str, str] = {
    'asc': ORDER_ASC, 'ascending': ORDER_ASC, '升序': ORDER_ASC,
    '从低到高': ORDER_ASC, '低到高': ORDER_ASC, '由小到大': ORDER_ASC,
    'desc': ORDER_DESC, 'descending': ORDER_DESC, '降序': ORDER_DESC,
    '从高到低': ORDER_DESC, '高到低': ORDER_DESC, '由大到小': ORDER_DESC,
}


def normalize_order_by(value: Any, group_names: Sequence[str]) -> Optional[str]:
    """把排序键规约为**固定枚举**（Phase 3C 安全核心）。

    只接受三类输入，其它任何字符串（含 ``aggregate_value DESC; DROP TABLE t``、
    ``foo) OR 1=1``、``random()``）一律拒绝：

    1. 聚合值：``aggregate_value`` 及其有界别名；
    2. 分组列下标：``group_column_<i>``（i 必须在 group_by 范围内）；
    3. 分组列的**名字**（必须与某个 group_by 列一致，大小写不敏感）。

    用户/LLM 的文本永远不会进入 ORDER BY —— 这里输出的是 Python 枚举，
    真正的 SQL 标识符由 build_group_aggregate_sql() 依据枚举生成。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise excel_query.ExcelQueryError(
            ERR_ORDER_INVALID, f'order_by 必须是字符串（收到 {type(value).__name__}）')
    text = value.strip()
    if text == '':
        return None
    low = text.lower()
    if low in _ORDER_BY_AGG_ALIASES:
        return ORDER_BY_AGGREGATE
    if low.startswith(ORDER_BY_GROUP_PREFIX):
        tail = low[len(ORDER_BY_GROUP_PREFIX):]
        if tail.isdigit() and int(tail) < len(group_names):
            return f'{ORDER_BY_GROUP_PREFIX}{int(tail)}'
        raise excel_query.ExcelQueryError(
            ERR_ORDER_INVALID,
            f'order_by 非法：{value!r}（当前只有 {len(group_names)} 个分组列）',
            {'supported': [ORDER_BY_AGGREGATE] + [f'{ORDER_BY_GROUP_PREFIX}{i}'
                                                  for i in range(len(group_names))]},
        )
    for i, name in enumerate(group_names):
        if isinstance(name, str) and name.strip() == text:
            return f'{ORDER_BY_GROUP_PREFIX}{i}'
    for i, name in enumerate(group_names):
        if isinstance(name, str) and name.strip().lower() == low:
            return f'{ORDER_BY_GROUP_PREFIX}{i}'
    raise excel_query.ExcelQueryError(
        ERR_ORDER_INVALID,
        f'order_by 非法：{value!r}。排序键只能是聚合值（{ORDER_BY_AGGREGATE}）'
        f'或分组字段之一（{", ".join(str(n) for n in group_names) or "无"}）',
        {'group_by': list(group_names)},
    )


def normalize_order_dir(value: Any, order_by: Optional[str]) -> str:
    """把排序方向规约为 asc/desc（严格白名单 + 中文映射）。

    默认值（用户未指定方向时，Python 统一决定，代码/测试/UI 保持一致）：
    - 排序键是**聚合值** -> ``desc``（"最多的/最高的"是统计场景的默认直觉）；
    - 排序键是**分组字段** -> ``asc``（按名称排列的默认直觉）。
    """
    if value is None or (isinstance(value, str) and value.strip() == ''):
        return ORDER_DESC if order_by == ORDER_BY_AGGREGATE else ORDER_ASC
    if not isinstance(value, str):
        raise excel_query.ExcelQueryError(
            ERR_ORDER_INVALID, f'order_dir 必须是字符串（收到 {type(value).__name__}）')
    direction = _DIR_ALIASES.get(value.strip().lower())
    if direction is None:
        raise excel_query.ExcelQueryError(
            ERR_ORDER_INVALID,
            f'order_dir 非法：{value!r}（只允许 asc / desc）',
            {'supported': list(SUPPORTED_ORDER_DIRS)},
        )
    return direction


def normalize_top_n(value: Any) -> Optional[int]:
    """TOP-N 校验：必须是 1..MAX_TOP_N 的整数（超界/非法一律拒绝）。"""
    if value is None or (isinstance(value, str) and value.strip() == ''):
        return None
    if isinstance(value, bool):
        raise excel_query.ExcelQueryError(ERR_TOP_N_INVALID, f'top_n 非法：{value!r}')
    n: Optional[int] = None
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value.is_integer():
        n = int(value)
    elif isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            n = int(s)
    if n is None or not (MIN_TOP_N <= n <= MAX_TOP_N):
        raise excel_query.ExcelQueryError(
            ERR_TOP_N_INVALID,
            f'top_n 必须是 {MIN_TOP_N}~{MAX_TOP_N} 之间的整数，收到 {value!r}',
            {'min': MIN_TOP_N, 'max': MAX_TOP_N},
        )
    return n


def parse_aggregate_payload(
    payload: Dict[str, Any],
    document_id: str,
    user_id: Optional[int] = None,
) -> AggregateRequest:
    """把原始 payload 校验成 AggregateRequest（筛选条件复用 Phase 2 的 parse_request）。"""
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, '请求体必须是 JSON 对象')

    operation = normalize_operation(payload.get('operation'))

    raw_column = payload.get('column')
    column: Optional[str] = None
    if raw_column is not None:
        if not isinstance(raw_column, str) or raw_column.strip() == '':
            raise excel_query.ExcelQueryError(
                excel_query.ERR_INVALID_PARAM, f'column 必须是列名字符串，收到 {raw_column!r}'
            )
        column = raw_column.strip()

    # Phase 4B：受控计算字段（计算值取代 column 参与聚合）
    calc = calculation.normalize_calculation(payload.get('calculation'))
    if calc is not None:
        if operation == OPERATION_COUNT:
            raise excel_query.ExcelQueryError(
                calculation.ERR_CALC_INVALID,
                'COUNT 统计的是行数，不需要计算字段；请去掉 calculation',
            )
        if column is not None:
            raise excel_query.ExcelQueryError(
                calculation.ERR_CALC_INVALID,
                f'不能同时指定目标列「{column}」和计算字段；请只保留计算字段',
                {'column': column, 'calculation': calc.to_dict()},
            )
    elif operation in NUMERIC_OPERATIONS and column is None:
        raise excel_query.ExcelQueryError(
            ERR_COLUMN_REQUIRED,
            f'{operation.upper()} 需要指定一个数值列或一个计算字段',
        )
    # COUNT：语义固定为"行数"，忽略 column（不实现 COUNT(DISTINCT)）
    if operation == OPERATION_COUNT:
        column = None

    # Phase 3B：分组列（只看名字，真实列解析在 resolve_aggregate 中完成）
    raw_group = payload.get('group_by')
    group_by: List[str] = []
    if raw_group is not None:
        if isinstance(raw_group, str):
            raw_group = [raw_group]
        if not isinstance(raw_group, (list, tuple)):
            raise excel_query.ExcelQueryError(
                excel_query.ERR_INVALID_PARAM, 'group_by 必须是列名数组')
        for g in raw_group:
            if not isinstance(g, str) or g.strip() == '':
                raise excel_query.ExcelQueryError(
                    excel_query.ERR_INVALID_PARAM, f'group_by 中存在非法列名：{g!r}')
            name = g.strip()
            if name not in group_by:
                group_by.append(name)
    if len(group_by) > MAX_GROUP_COLUMNS:
        raise excel_query.ExcelQueryError(
            ERR_TOO_MANY_GROUP_COLUMNS,
            f'本阶段最多支持 {MAX_GROUP_COLUMNS} 个分组列，收到 {len(group_by)} 个',
            {'group_by': group_by},
        )

    # Phase 3C：排序 + TOP-N（排序只作用于分组统计结果）
    order_by = normalize_order_by(payload.get('order_by'), group_by)
    if order_by is not None and not group_by:
        raise excel_query.ExcelQueryError(
            ERR_ORDER_REQUIRES_GROUP,
            '排序只用于分组统计结果，请先说明按什么分组（例如「按物流商统计订单金额，从高到低排列」）。',
            {'order_by': order_by},
        )
    order_dir = normalize_order_dir(payload.get('order_dir'), order_by) if order_by else ORDER_ASC
    top_n = normalize_top_n(payload.get('top_n'))
    if top_n is not None and order_by is None:
        raise excel_query.ExcelQueryError(
            ERR_TOP_N_REQUIRES_ORDER,
            'TOP-N 必须有明确的排序依据（例如「订单金额最高的5个物流商」）。'
            '请说明按哪个统计值排序。',
            {'top_n': top_n},
        )

    # 复用 Phase 2 的筛选校验（列名/operator/取值类型/匹配模式）
    probe = {
        'sheet_index': payload.get('sheet_index'),
        'sheet_name': payload.get('sheet_name'),
        'filters': payload.get('filters') or [],
        'match_mode': payload.get('match_mode') or excel_query.MATCH_MODE_AND,
        'limit': 1,
        'offset': 0,
    }
    base = excel_query.parse_request(probe, document_id=document_id, user_id=user_id)

    return AggregateRequest(
        document_id=document_id,
        user_id=user_id,
        sheet_index=base.sheet_index,
        sheet_name=base.sheet_name,
        operation=operation,
        column=column,
        filters=[f.to_dict() for f in base.filters],
        group_by=group_by,
        order_by=order_by,
        order_dir=order_dir,
        top_n=top_n,
        calculation=calc.to_dict() if calc is not None else None,
    )


def resolve_aggregate(
    representation: WorkbookRepresentation,
    request: AggregateRequest,
) -> AggregateRequest:
    """按真实 schema 解析 Sheet 与目标列，并对数值列做可靠性校验。

    - 列名无法唯一解析 -> ExcelQueryError(ERR_COLUMN_NOT_FOUND/AMBIGUOUS)
    - 数值操作但该列整表没有任何可数值化单元格 -> ExcelQueryError(ERR_NOT_NUMERIC_COLUMN)
      （明确失败，绝不返回错误的数字）
    """
    sheet = excel_query.resolve_sheet(representation, request.sheet_index, request.sheet_name)
    request.sheet_index = sheet.sheet_index
    request.sheet_name = sheet.sheet_name

    # Phase 3B：分组列必须能在真实 schema 中唯一解析（严格，不猜）
    if request.group_by:
        resolved_group: List[str] = []
        indexes: List[int] = []
        letters: List[str] = []
        for name in request.group_by:
            col = excel_query.resolve_column(sheet, name)
            if col.index in indexes:
                continue
            resolved_group.append(col.name)
            indexes.append(col.index)
            letters.append(col.excel_column_letter)
        request.group_by = resolved_group
        request.group_by_indexes = indexes
        request.group_by_letters = letters
        # Phase 3C：记录分组列物理类型（Python 参考实现据此复现 DuckDB 的排序比较语义）
        phys_types: List[str] = []
        for idx in indexes:
            vals = [r[idx] for r in sheet.rows if idx < len(r) and r[idx] is not None]
            phys_types.append(duck_engine.infer_physical_type(vals))
        request.group_by_phys_types = phys_types
        # 排序键可能是"分组列的名字" —— 解析成真实列名后再归一化一次
        if request.order_by is not None:
            request.order_by = normalize_order_by(request.order_by, resolved_group)

    # Phase 4B：计算字段解析（真实列唯一解析；随后计算值取代 column）
    if request.calculation:
        calc = calculation.resolve_calculation(
            sheet, calculation.normalize_calculation(request.calculation))
        request.calculation = calc.to_dict()
        request.calc_expr = calculation.calc_sql_expr(calc)

    if request.operation in NUMERIC_OPERATIONS and request.calc_expr:
        # 计算值列的可数值化统计（用于"整表无任何可计算值"的明确报错）
        numeric = empty = non_numeric = 0
        for row in sheet.rows:
            value = calculation.calc_python_value(calc, row)
            if value is None:
                # 计算值为空（空单元格 / 非数值 / 除零统一为 SQL 的 NULL 语义）
                empty += 1
            else:
                numeric += 1
        request.calc_numeric_in_sheet = numeric
        request.column_numeric_in_sheet = numeric
        request.column_empty_in_sheet = empty
        request.column_non_numeric_in_sheet = non_numeric
        if numeric == 0:
            raise excel_query.ExcelQueryError(
                ERR_NOT_NUMERIC_COLUMN,
                f'计算字段「{calc.label()}」在该 Sheet 的 {sheet.row_count} 行中没有任何可计算值'
                f'（为空/不可数值化/除零共 {empty + non_numeric} 行），无法执行 {request.operation.upper()}',
                {
                    'calculation': calc.to_dict(),
                    'empty_rows': empty,
                    'non_numeric_rows': non_numeric,
                    'available_columns': sheet.column_names,
                },
            )
        return request

    if request.operation in NUMERIC_OPERATIONS:
        if not request.column:
            raise excel_query.ExcelQueryError(
                ERR_COLUMN_REQUIRED, f'{request.operation.upper()} 需要指定一个数值列'
            )
        col = excel_query.resolve_column(sheet, request.column)
        request.column = col.name
        request.column_index = col.index
        request.column_letter = col.excel_column_letter

        # 稳定性补丁：业务标识列（Order ID / SKU ID / 追踪号…）不做数值聚合。
        # 这类列即使全是数字串，SUM/AVG 在业务上无意义，且会被 TRY_CAST AS DOUBLE
        # 丢精度（18 位 ID 在 double 里间隔 256，相邻 ID 会撞成同一个值）。
        if is_identifier_column(col, sheet.column_values(col.index)):
            raise excel_query.ExcelQueryError(
                ERR_IDENTIFIER_NOT_NUMERIC,
                f'列「{col.name}」是业务标识列（编号），不支持 '
                f'{request.operation.upper()}；如需统计数量请用 COUNT'
                f'（例如「有多少个不同的{col.name}」）。',
                {
                    'column': col.name,
                    'operation': request.operation,
                    'semantic_type': representation_module.SEMANTIC_IDENTIFIER,
                    'identifier_columns': sheet.identifier_columns(),
                    'available_columns': sheet.column_names,
                },
            )

        numeric = empty = non_numeric = 0
        for row in sheet.rows:
            raw = row[col.index] if col.index < len(row) else None
            if raw is None:
                empty += 1
            elif aggregate_to_number(raw) is None:
                non_numeric += 1
            else:
                numeric += 1
        request.column_numeric_in_sheet = numeric
        request.column_empty_in_sheet = empty
        request.column_non_numeric_in_sheet = non_numeric

        if numeric == 0:
            raise excel_query.ExcelQueryError(
                ERR_NOT_NUMERIC_COLUMN,
                f'列「{col.name}」不是数值列：该 Sheet 的 {sheet.row_count} 行中没有任何可数值化的单元格'
                f'（空值 {empty} 个、非数值 {non_numeric} 个），无法执行 {request.operation.upper()}',
                {
                    'column': col.name,
                    'empty_rows': empty,
                    'non_numeric_rows': non_numeric,
                    'available_columns': sheet.column_names,
                },
            )
    return request


# ----------------------------------------------------------------------------
# SQL 构造（唯一生成统计 SQL 的地方）
# ----------------------------------------------------------------------------
def build_aggregate_sql(
    table: str,
    plan_filters: Sequence[duck_engine.PlanFilter],
    operation: str,
    column_index: Optional[int],
    calc_expr: Optional[str] = None,
) -> Dict[str, Any]:
    """生成统计 SQL（标识符全部 Python 生成；筛选值全部 ? 绑定）。

    - 表名/列名先做标识符安全断言；
    - 只允许 SELECT；
    - COUNT 使用 COUNT(*)；数值操作使用 TRY_CAST(col AS DOUBLE)，
      空值与非数值自动被 SQL 聚合函数忽略（与 Python 参考实现一致）；
    - Phase 4B：给出 ``calc_expr``（Python 生成的受控计算表达式）时，
      用计算值取代目标列参与聚合（除零已在表达式内处理为 NULL）。
    """
    if not duck_engine._SAFE_IDENT.match(table):  # noqa: SLF001 - 复用同一套标识符白名单
        raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, f'非法表名：{table!r}')

    where_sql, where_params = duck_engine.render_where_filters(plan_filters)
    where_clause = f' WHERE {where_sql}' if where_sql else ''

    row_phys = f'"{duck_engine.PHYS_ROW}"'
    selects = ['COUNT(*) AS matched_rows']

    if operation == OPERATION_COUNT:
        selects.append(f'MIN({row_phys}) AS first_excel_row')
        selects.append(f'MAX({row_phys}) AS last_excel_row')
    else:
        if calc_expr:
            calculation.assert_calc_expr_safe(calc_expr)
            cast = calc_expr
            selects.append(f'COUNT({cast}) AS non_null_rows')
        else:
            if column_index is None:
                raise excel_query.ExcelQueryError(ERR_COLUMN_REQUIRED, '数值统计必须指定目标列')
            col = f'"{duck_engine.phys_col(column_index)}"'
            if not duck_engine._SAFE_IDENT.match(duck_engine.phys_col(column_index)):  # noqa: SLF001
                raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, '非法列标识')
            cast = f'TRY_CAST({col} AS DOUBLE)'
            selects.append(f'COUNT({col}) AS non_null_rows')
        selects.append(f'COUNT({cast}) AS numeric_rows')
        selects.append(f'SUM({cast}) AS agg_value')
        selects.append(f'MIN({cast}) AS agg_min')
        selects.append(f'MAX({cast}) AS agg_max')
        selects.append(f'AVG({cast}) AS agg_avg')
        selects.append(f'MIN({row_phys}) AS first_excel_row')
        selects.append(f'MAX({row_phys}) AS last_excel_row')

    main_sql = f'SELECT {", ".join(selects)} FROM "{table}"{where_clause}'
    rows_sql = (
        f'SELECT {row_phys} FROM "{table}"{where_clause} '
        f'ORDER BY {row_phys} LIMIT ?'
    )
    duck_engine.assert_safe_sql(main_sql)
    duck_engine.assert_safe_sql(rows_sql)

    return {
        'main_sql': main_sql,
        'main_params': list(where_params),
        'rows_sql': rows_sql,
        'rows_params': list(where_params) + [MAX_ROW_NUMBERS + 1],
    }


def _operation_aggregate_expr(operation: str, cast_expr: str) -> str:
    """返回与 operation **严格对应**的聚合表达式（用于排序值别名）。"""
    if operation == OPERATION_SUM:
        return f'SUM({cast_expr})'
    if operation == OPERATION_AVG:
        return f'AVG({cast_expr})'
    if operation == OPERATION_MIN:
        return f'MIN({cast_expr})'
    if operation == OPERATION_MAX:
        return f'MAX({cast_expr})'
    return f'COUNT({cast_expr})'  # pragma: no cover


def is_order_enum(value: Any) -> bool:
    """判断是否为「排序键枚举」写法（用于 NL 层决定是否先做列名解析）。"""
    if not isinstance(value, str):
        return False
    low = value.strip().lower()
    return low in _ORDER_BY_AGG_ALIASES or low.startswith(ORDER_BY_GROUP_PREFIX)


def build_group_sort_sql(
    operation: str,
    order_by: Optional[str],
    order_dir: str,
    n_group: int,
) -> List[str]:
    """生成 ORDER BY 的键列表（Phase 3C）。

    安全关键：输入是 **Python 枚举**（``aggregate_value`` / ``group_column_<i>``），
    输出的 SQL 标识符只可能是 Python 生成的别名 ``agg_value`` / ``matched_rows`` / ``g<i>``。
    用户/LLM 文本永远不进入 ORDER BY。

    确定性（Tie-breaker）：主排序键之后，**自动追加全部剩余分组列 g<i> ASC**，
    因此"聚合值相同"的多个分组顺序也是稳定可复现的；用户无法注入 tie-breaker。

    空值语义：每个排序键都显式带 ``NULLS LAST`` —— 无有效数值的分组（value=NULL）
    恒定排在最后，不参与"最大/最小"的排名语义（升序降序一致）。
    """
    if order_by == ORDER_BY_AGGREGATE:
        # COUNT 的聚合值就是行数；数值操作必须用「与 operation 对应」的排序别名，
        # 不能用 agg_value（恒为 SUM），否则 AVG/MIN/MAX 的排序结果会错。
        primary = 'matched_rows' if operation == OPERATION_COUNT else 'agg_sort_value'
        primary_idx: Optional[int] = None
    elif order_by and order_by.startswith(ORDER_BY_GROUP_PREFIX):
        primary_idx = int(order_by[len(ORDER_BY_GROUP_PREFIX):])
        if primary_idx >= n_group:
            raise excel_query.ExcelQueryError(
                ERR_ORDER_INVALID, f'order_by 指向的分组列不存在：{order_by!r}')
        primary = f'g{primary_idx}'
    else:  # pragma: no cover - 调用方已保证
        raise excel_query.ExcelQueryError(ERR_ORDER_INVALID, f'order_by 非法：{order_by!r}')

    direction = (order_dir or ORDER_ASC).upper()
    if direction not in ('ASC', 'DESC'):
        raise excel_query.ExcelQueryError(ERR_ORDER_INVALID, f'order_dir 非法：{order_dir!r}')

    keys = [f'{primary} {direction} NULLS LAST']
    for i in range(n_group):
        if i == primary_idx:
            continue
        keys.append(f'g{i} ASC NULLS LAST')
    return keys


def build_group_aggregate_sql(
    table: str,
    plan_filters: Sequence[duck_engine.PlanFilter],
    operation: str,
    column_index: Optional[int],
    group_indexes: Sequence[int],
    order_by: Optional[str] = None,
    order_dir: str = ORDER_ASC,
    top_n: Optional[int] = None,
    calc_expr: Optional[str] = None,
) -> Dict[str, Any]:
    """生成**分组统计** SQL（Phase 3B）。

        SELECT "c5"[,"c41"], COUNT(*) [, COUNT(col), COUNT(TRY_CAST..), SUM/MIN/MAX/AVG]
        FROM "t_x" [WHERE ...] GROUP BY "c5"[,"c41"]

    - **不追加 ORDER BY**：本阶段不定义任何业务排序语义（分组顺序即数据库自然顺序）；
    - 分组列与聚合列均为 Python 生成的物理名（c<N>），用户/LLM 字符串不进入标识符位；
    - 筛选值全部 ? 绑定；
    - WHERE 先于 GROUP BY 执行（过滤后再分组）。
    """
    if not table or not duck_engine._SAFE_IDENT.match(table):  # noqa: SLF001
        raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, f'非法表名：{table!r}')
    if not group_indexes:
        raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, '分组统计必须提供 group_by')

    where_sql, where_params = duck_engine.render_where_filters(plan_filters)
    where_clause = f' WHERE {where_sql}' if where_sql else ''
    row_phys = f'"{duck_engine.PHYS_ROW}"'

    selects: List[str] = []
    group_phys: List[str] = []
    for pos, gi in enumerate(group_indexes):
        phys = duck_engine.phys_col(gi)
        if not duck_engine._SAFE_IDENT.match(phys):  # noqa: SLF001
            raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, f'非法分组列标识：{phys!r}')
        group_phys.append(f'"{phys}"')
        selects.append(f'"{phys}" AS g{pos}')

    selects.append('COUNT(*) AS matched_rows')
    if operation != OPERATION_COUNT:
        if calc_expr:
            # Phase 4B：受控计算字段（逐行计算后再聚合；除零在表达式内为 NULL）
            calculation.assert_calc_expr_safe(calc_expr)
            cast = calc_expr
            selects.append(f'COUNT({cast}) AS non_null_rows')
        else:
            if column_index is None:
                raise excel_query.ExcelQueryError(ERR_COLUMN_REQUIRED, '数值统计必须指定目标列')
            phys = duck_engine.phys_col(column_index)
            if not duck_engine._SAFE_IDENT.match(phys):  # noqa: SLF001
                raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, '非法列标识')
            col = f'"{phys}"'
            cast = f'TRY_CAST({col} AS DOUBLE)'
            selects.append(f'COUNT({col}) AS non_null_rows')
        selects.append(f'COUNT({cast}) AS numeric_rows')
        selects.append(f'SUM({cast}) AS agg_value')
        selects.append(f'MIN({cast}) AS agg_min')
        selects.append(f'MAX({cast}) AS agg_max')
        selects.append(f'AVG({cast}) AS agg_avg')
        # Phase 3C：与 operation 严格对应的排序值别名（放最后，不打乱既有列序）
        selects.append(f'{_operation_aggregate_expr(operation, cast)} AS agg_sort_value')

    main_sql = (
        f'SELECT {", ".join(selects)} FROM "{table}"{where_clause} '
        f'GROUP BY {", ".join(group_phys)}'
    )
    main_params: List[Any] = list(where_params)
    sort_keys: List[str] = []
    if order_by:
        sort_keys = build_group_sort_sql(operation, order_by, order_dir, len(group_phys))
        main_sql += ' ORDER BY ' + ', '.join(sort_keys)
        if top_n is not None:
            # TOP-N：对**排序后的分组结果**截取前 N 组（LIMIT 参数绑定，N 已由 Python 校验）
            main_sql += ' LIMIT ?'
            main_params.append(int(top_n))

    totals_sql = (
        f'SELECT COUNT(*), MIN({row_phys}), MAX({row_phys}) FROM "{table}"{where_clause}'
    )
    groups_sql = (
        f'SELECT COUNT(*) FROM (SELECT 1 AS one FROM "{table}"{where_clause} '
        f'GROUP BY {", ".join(group_phys)}) AS g'
    )
    duck_engine.assert_safe_sql(main_sql)
    duck_engine.assert_safe_sql(totals_sql)
    duck_engine.assert_safe_sql(groups_sql)

    return {
        'main_sql': main_sql,
        'main_params': main_params,
        'totals_sql': totals_sql,
        'totals_params': list(where_params),
        'groups_sql': groups_sql,
        'groups_params': list(where_params),
        'sort_keys': sort_keys,
        'need_groups_count': top_n is not None,
    }


def _aggregate_value_label(operation: str, column: Optional[str],
                           calc: Optional[Dict[str, Any]] = None) -> str:
    """聚合值的中文标签（用于排序描述，Python 生成）。"""
    if operation == OPERATION_COUNT:
        return '分组行数'
    base = {
        OPERATION_SUM: '合计',
        OPERATION_AVG: '平均值',
        OPERATION_MIN: '最小值',
        OPERATION_MAX: '最大值',
    }.get(operation, operation)
    if calc:
        label = calc.get('label') or '计算值'
        return f'{label} {base}'
    return f'{column} {base}' if column else base


def _definition(operation: str, column: Optional[str],
                calc: Optional[Dict[str, Any]] = None) -> str:
    if operation == OPERATION_COUNT:
        return '统计符合筛选条件的数据行数（COUNT(*)，不做去重）'
    labels = {
        OPERATION_SUM: '求和',
        OPERATION_AVG: '求平均',
        OPERATION_MIN: '取最小值',
        OPERATION_MAX: '取最大值',
    }
    if calc:
        from backend.excel.calculation import Calculation

        obj = Calculation(
            operation=calc.get('operation', ''),
            left_column=calc.get('left_column', ''),
            right_column=calc.get('right_column', ''),
        )
        return calculation.definition_text(obj, labels.get(operation, operation))
    return (
        f'对「{column}」列中**可数值化**的单元格{labels.get(operation, operation)}；'
        f'空值与非数值单元格不计入（也不当作 0）'
    )


def _group_definition(
    operation: str,
    column: Optional[str],
    group_names: Sequence[str],
    order_by: Optional[str] = None,
    order_dir: str = ORDER_ASC,
    top_n: Optional[int] = None,
    calc: Optional[Dict[str, Any]] = None,
) -> str:
    text = f'先按「{"、".join(group_names)}」分组，再对每个分组{_definition(operation, column, calc)}'
    if order_by:
        if order_by == ORDER_BY_AGGREGATE:
            label = _aggregate_value_label(operation, column, calc)
        elif order_by.startswith(ORDER_BY_GROUP_PREFIX):
            # 不把内部枚举暴露给用户，直接翻译成分组列名
            idx = int(order_by[len(ORDER_BY_GROUP_PREFIX):])
            label = group_names[idx] if idx < len(group_names) else order_by
        else:  # pragma: no cover
            label = str(order_by)
        text += (f'；结果按「{label}」{ORDER_DIR_PHRASES.get(order_dir, order_dir)}排序'
                 f'（相同值时按分组字段升序稳定排序）')
        if top_n is not None:
            text += f'；并截取前 {top_n} 个分组'
    else:
        text += '；不排序'
    return text


def _group_cells_from_values(
    sheet: SheetRepresentation, indexes: Sequence[int], values: Sequence[Any]
) -> List[GroupCell]:
    cells: List[GroupCell] = []
    for pos, i in enumerate(indexes):
        col = sheet.columns[i]
        raw = values[pos]
        cells.append(GroupCell(
            column=col.name,
            value=raw,
            display=GROUP_NULL_LABEL if raw is None else str(raw),
            is_null=raw is None,
        ))
    return cells


def _build_result(
    representation: WorkbookRepresentation,
    request: AggregateRequest,
    sheet_row_count: int,
    value: Optional[float],
    matched_rows: int,
    numeric_rows: int,
    empty_rows: int,
    non_numeric_rows: int,
    row_numbers: List[int],
    truncated: bool,
    span: Optional[Dict[str, int]],
) -> AggregateResult:
    return AggregateResult(
        document_id=representation.document_id,
        sheet_index=request.sheet_index or 0,
        sheet_name=request.sheet_name or '',
        operation=request.operation,
        column=request.column,
        column_index=request.column_index,
        column_letter=request.column_letter,
        filters=list(request.filters),
        value=value,
        matched_rows=matched_rows,
        numeric_rows=numeric_rows,
        empty_rows=empty_rows,
        non_numeric_rows=non_numeric_rows,
        total_rows_in_sheet=sheet_row_count,
        row_excel_numbers=row_numbers,
        row_excel_span=span,
        row_excel_truncated=truncated,
        column_numeric_in_sheet=request.column_numeric_in_sheet,
        definition=_definition(request.operation, request.column, request.calculation),
        calculation=request.calculation,
    )


# ----------------------------------------------------------------------------
# 执行：DuckDB
# ----------------------------------------------------------------------------
def execute_aggregate_duckdb(
    representation: WorkbookRepresentation,
    request: AggregateRequest,
) -> AggregateResult:
    """DuckDB 执行统计（主路径）。"""
    if not duck_engine.DUCKDB_AVAILABLE:
        raise duck_engine.DuckEngineUnavailable('未安装 duckdb，无法使用 DuckDB 引擎')

    request = resolve_aggregate(representation, request)
    sheet = excel_query.resolve_sheet(representation, request.sheet_index, request.sheet_name)
    db, info = duck_engine.get_registry().acquire(representation, sheet.sheet_index)

    plan_filters = duck_engine.build_plan_filters(
        sheet, [excel_query.FilterCondition(**f) for f in request.filters]
    )
    sql = build_aggregate_sql(info['table'], plan_filters, request.operation, request.column_index,
                              calc_expr=request.calc_expr)

    with db.lock:
        row = db.con.execute(sql['main_sql'], sql['main_params']).fetchone()
        rows = db.con.execute(sql['rows_sql'], sql['rows_params']).fetchall()

    matched_rows = int(row[0] or 0)
    if request.operation == OPERATION_COUNT:
        numeric_rows = matched_rows
        empty_rows = 0
        non_numeric_rows = 0
        value: Optional[float] = float(matched_rows)
        first_row, last_row = row[1], row[2]
    else:
        # 列顺序与 build_aggregate_sql 保持一致：
        # 0 matched_rows / 1 non_null_rows / 2 numeric_rows / 3 sum / 4 min / 5 max / 6 avg
        # 7 first_excel_row / 8 last_excel_row
        non_null_rows = int(row[1] or 0)
        numeric_rows = int(row[2] or 0)
        agg_sum, agg_min, agg_max, agg_avg = row[3], row[4], row[5], row[6]
        op = request.operation
        if op == OPERATION_SUM:
            value = float(agg_sum) if agg_sum is not None else None
        elif op == OPERATION_MIN:
            value = float(agg_min) if agg_min is not None else None
        elif op == OPERATION_MAX:
            value = float(agg_max) if agg_max is not None else None
        else:  # AVG
            value = float(agg_avg) if agg_avg is not None else None
        first_row, last_row = row[7], row[8]
        empty_rows = max(0, matched_rows - non_null_rows)
        non_numeric_rows = max(0, non_null_rows - numeric_rows)

    fetched = [int(r[0]) for r in rows]
    truncated = len(fetched) > MAX_ROW_NUMBERS
    row_numbers = fetched[:MAX_ROW_NUMBERS]
    span = None
    if first_row is not None and last_row is not None:
        span = {'first': int(first_row), 'last': int(last_row)}

    return _build_result(
        representation, request, sheet.row_count, value, matched_rows, numeric_rows,
        empty_rows, non_numeric_rows, row_numbers, truncated, span,
    )


# ----------------------------------------------------------------------------
# 执行：Python 参考实现（降级 + 差分校验基准）
# ----------------------------------------------------------------------------
def execute_aggregate_python(
    representation: WorkbookRepresentation,
    request: AggregateRequest,
) -> AggregateResult:
    """纯 Python 参考实现（不依赖 DuckDB）。

    与 DuckDB 路径必须逐值一致（测试中做差分对比）：
    - 匹配语义复用 excel_query.match_cell（Phase 2 的同一个实现）；
    - 数值判定复用 aggregate_to_number（对齐 TRY_CAST AS DOUBLE）。
    """
    request = resolve_aggregate(representation, request)
    sheet = excel_query.resolve_sheet(representation, request.sheet_index, request.sheet_name)
    plan_filters = duck_engine.build_plan_filters(
        sheet, [excel_query.FilterCondition(**f) for f in request.filters]
    )

    matched: List[Tuple[Any, int]] = []
    for i, row in enumerate(sheet.rows):
        ok = True
        for f in plan_filters:
            cell = row[f.column_index] if f.column_index < len(row) else None
            if not excel_query.match_cell(cell, f.operator, f.value):
                ok = False
                break
        if ok:
            excel_row = sheet.row_excel_numbers[i] if i < len(sheet.row_excel_numbers) else i + 1
            matched.append((row, excel_row))

    matched_rows = len(matched)
    if request.operation == OPERATION_COUNT:
        numeric_rows, empty_rows, non_numeric_rows = matched_rows, 0, 0
        value: Optional[float] = float(matched_rows)
    else:
        ci = request.column_index or 0
        numeric_rows = empty_rows = non_numeric_rows = 0
        numbers: List[float] = []
        calc_obj = calculation.ensure_resolved(sheet, request.calculation)
        for row, _ in matched:
            if calc_obj is not None:
                # Phase 4B：计算值；为空/非数值/除零 -> None（与 SQL NULL 语义一致）
                num = calculation.calc_python_value(calc_obj, row)
                if num is None:
                    empty_rows += 1
                else:
                    numeric_rows += 1
                    numbers.append(num)
                continue
            raw = row[ci] if ci < len(row) else None
            if raw is None:
                empty_rows += 1
                continue
            num = aggregate_to_number(raw)
            if num is None:
                non_numeric_rows += 1
            else:
                numeric_rows += 1
                numbers.append(num)
        if not numbers:
            value = None
        elif request.operation == OPERATION_SUM:
            value = float(sum(numbers))
        elif request.operation == OPERATION_MIN:
            value = float(min(numbers))
        elif request.operation == OPERATION_MAX:
            value = float(max(numbers))
        else:
            value = float(sum(numbers) / len(numbers))

    excel_rows = [r for _, r in matched]
    truncated = len(excel_rows) > MAX_ROW_NUMBERS
    span = {'first': excel_rows[0], 'last': excel_rows[-1]} if excel_rows else None

    return _build_result(
        representation, request, sheet.row_count, value, matched_rows, numeric_rows,
        empty_rows, non_numeric_rows, excel_rows[:MAX_ROW_NUMBERS], truncated, span,
    )


# ----------------------------------------------------------------------------
# 摘要（Python 生成，不经过 LLM）
# ----------------------------------------------------------------------------
def format_aggregate_summary(result: AggregateResult, filename: str) -> str:
    """生成统计结果摘要（含来源与口径，保证可追溯）。"""
    op_label = OPERATION_LABELS.get(result.operation, result.operation)
    lines = [
        f'已对「{filename}」的 Sheet「{result.sheet_name}」执行统计。',
        f'- 操作：{op_label}',
    ]
    if result.column:
        lines.append(f'- 目标列：{result.column}（{result.column_letter} 列）')
    if result.calculation:
        lines.append(f'- 计算字段：{result.calculation["label"]}'
                     f'（逐行计算后再 {OPERATION_LABELS.get(result.operation, result.operation)}）')
    lines.append(f'- 统计结果：{result.value_display()}')

    if result.operation == OPERATION_COUNT:
        lines.append(f'- 匹配行数：{result.matched_rows} 行（该 Sheet 共 {result.total_rows_in_sheet} 行）')
    else:
        lines.append(
            f'- 匹配行数：{result.matched_rows} 行，其中可数值化 {result.numeric_rows} 行'
            f'（空值 {result.empty_rows} 行、非数值 {result.non_numeric_rows} 行不计入）'
        )
        if result.value is None:
            lines.append('- 说明：命中行中没有可数值化的单元格，因此没有可计算的统计值（未按 0 处理）')
    if result.row_excel_span:
        lines.append(
            f'- 匹配 Excel 行：{result.row_excel_span["first"]} ~ {result.row_excel_span["last"]}'
            f'（共 {result.matched_rows} 行）'
        )
    lines.append(f'- 统计口径：{result.definition}')
    if result.filters:
        lines.append('- 筛选条件：' + '；'.join(_filter_text(f) for f in result.filters))
    else:
        lines.append('- 筛选条件：无（全表）')
    return '\n'.join(lines)


def _filter_text(f: Dict[str, Any]) -> str:
    op = f.get('operator')
    label = {
        excel_query.OPERATOR_EQ: '=',
        excel_query.OPERATOR_NEQ: '≠',
        excel_query.OPERATOR_GT: '>',
        excel_query.OPERATOR_GTE: '≥',
        excel_query.OPERATOR_LT: '<',
        excel_query.OPERATOR_LTE: '≤',
        excel_query.OPERATOR_CONTAINS: '包含',
    }.get(op, str(op))
    value = f.get('value')
    return f'{f.get("column")} {label} {"" if value is None else value}'.strip()


# ============================================================================
# Phase 3B：分组统计执行（GROUP BY）
# ============================================================================
def execute_group_aggregate_duckdb(
    representation: WorkbookRepresentation,
    request: AggregateRequest,
) -> GroupedAggregateResult:
    """DuckDB 分组统计（主路径）。

    空值语义：DuckDB 的 GROUP BY 把 NULL 视为**独立分组**；空字符串 '' 是另一个独立取值
    （与 NULL 不同），两者都会作为独立分组返回。
    """
    if not duck_engine.DUCKDB_AVAILABLE:
        raise duck_engine.DuckEngineUnavailable('未安装 duckdb，无法使用 DuckDB 引擎')

    request = resolve_aggregate(representation, request)
    if not request.group_by:
        raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, '分组统计必须提供 group_by')

    sheet = excel_query.resolve_sheet(representation, request.sheet_index, request.sheet_name)
    db, info = duck_engine.get_registry().acquire(representation, sheet.sheet_index)
    plan_filters = duck_engine.build_plan_filters(
        sheet, [excel_query.FilterCondition(**f) for f in request.filters])
    sql = build_group_aggregate_sql(
        info['table'], plan_filters, request.operation, request.column_index,
        request.group_by_indexes, order_by=request.order_by,
        order_dir=request.order_dir, top_n=request.top_n,
        calc_expr=request.calc_expr,
    )

    with db.lock:
        db_rows = db.con.execute(sql['main_sql'], sql['main_params']).fetchall()
        totals = db.con.execute(sql['totals_sql'], sql['totals_params']).fetchone()
        total_groups_before = len(db_rows)
        if sql['need_groups_count']:
            # 有 TOP-N 时才需要"截断前"的分组总数
            total_groups_before = int(
                db.con.execute(sql['groups_sql'], sql['groups_params']).fetchone()[0] or 0)

    n_group = len(request.group_by_indexes)
    rows: List[GroupRow] = []
    for r in db_rows:
        group_values = [r[i] for i in range(n_group)]
        matched = int(r[n_group] or 0)
        if request.operation == OPERATION_COUNT:
            value: Optional[float] = float(matched)
            numeric_rows, empty_rows, non_numeric_rows = matched, 0, 0
        else:
            non_null = int(r[n_group + 1] or 0)
            numeric_rows = int(r[n_group + 2] or 0)
            agg_sum, agg_min, agg_max, agg_avg = (r[n_group + 3], r[n_group + 4],
                                                  r[n_group + 5], r[n_group + 6])
            if request.operation == OPERATION_SUM:
                value = float(agg_sum) if agg_sum is not None else None
            elif request.operation == OPERATION_MIN:
                value = float(agg_min) if agg_min is not None else None
            elif request.operation == OPERATION_MAX:
                value = float(agg_max) if agg_max is not None else None
            else:
                value = float(agg_avg) if agg_avg is not None else None
            empty_rows = max(0, matched - non_null)
            non_numeric_rows = max(0, non_null - numeric_rows)

        rows.append(GroupRow(
            group=_group_cells_from_values(sheet, request.group_by_indexes, group_values),
            value=value,
            matched_rows=matched,
            numeric_rows=numeric_rows,
            empty_rows=empty_rows,
            non_numeric_rows=non_numeric_rows,
        ))

    total_matched = int(totals[0] or 0)
    span = None
    if totals[1] is not None and totals[2] is not None:
        span = {'first': int(totals[1]), 'last': int(totals[2])}

    return _build_group_result(representation, request, sheet, rows, total_matched, span,
                               total_groups_before)


def _group_value_sort_key(value: Any, phys_type: str) -> Tuple[int, Any]:
    """分组值的可比较键（与 DuckDB 对该列物理类型的排序语义保持一致）。

    第一分量是"是否 NULL"，保证 NULL 恒排最后；第二分量按物理类型比较，
    因此 VARCHAR 用字符串、BIGINT/DOUBLE 用数值 —— 与 DuckDB 的行为一致。
    """
    if value is None:
        return (1, 0.0)
    if phys_type == 'BOOLEAN':
        return (0, 1.0 if value else 0.0)
    if phys_type in ('BIGINT', 'DOUBLE'):
        num = aggregate_to_number(value)
        return (0, num if num is not None else 0.0)
    return (0, str(value))


def sort_group_rows(rows: Sequence[GroupRow], request: AggregateRequest) -> List[GroupRow]:
    """Python 参考实现的排序（必须与 DuckDB ``ORDER BY ... NULLS LAST`` 逐位一致）。

    规则（与 SQL 侧完全对应）：
      1. 主键：聚合值 或 指定的分组列，方向由 order_dir 决定；
      2. NULL（无有效数值 / 该分组列取值为空）**恒排最后**（升序降序一致）；
      3. tie-breaker：其余分组列按索引顺序 ASC（NULLS LAST）—— 由 Python 自动生成。
    """
    if not request.order_by:
        return list(rows)
    desc = request.order_dir == ORDER_DESC
    primary_idx = request.order_group_index
    n_group = len(request.group_by_indexes)
    phys = list(request.group_by_phys_types) or ['VARCHAR'] * n_group
    tie_idx = [i for i in range(n_group) if i != primary_idx]

    def tie_key(r: GroupRow) -> Tuple[Any, ...]:
        return tuple(
            _group_value_sort_key(r.group[i].value, phys[i] if i < len(phys) else 'VARCHAR')
            for i in tie_idx
        )

    if primary_idx is None:
        non_null = [r for r in rows if r.value is not None]
        nulls = [r for r in rows if r.value is None]

        def primary_scalar(r: GroupRow) -> float:
            return float(r.value)  # type: ignore[arg-type]
    else:
        non_null = [r for r in rows if r.group[primary_idx].value is not None]
        nulls = [r for r in rows if r.group[primary_idx].value is None]

        def primary_scalar(r: GroupRow) -> Any:
            key = _group_value_sort_key(
                r.group[primary_idx].value,
                phys[primary_idx] if primary_idx < len(phys) else 'VARCHAR',
            )
            return key[1]

    non_null.sort(key=tie_key)                      # 先按 tie-breaker 升序
    non_null.sort(key=primary_scalar, reverse=desc)  # 再稳定排序主键
    nulls.sort(key=tie_key)
    return non_null + nulls


def execute_group_aggregate_python(
    representation: WorkbookRepresentation,
    request: AggregateRequest,
) -> GroupedAggregateResult:
    """纯 Python 参考实现（降级 + 差分基准）。

    分组顺序 = 分组键**首次出现**的顺序（不排序，不定义业务顺序）；
    与 DuckDB 路径比较时按 group_key 映射逐组比对（不依赖顺序）。
    """
    request = resolve_aggregate(representation, request)
    if not request.group_by:
        raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, '分组统计必须提供 group_by')

    sheet = excel_query.resolve_sheet(representation, request.sheet_index, request.sheet_name)
    plan_filters = duck_engine.build_plan_filters(
        sheet, [excel_query.FilterCondition(**f) for f in request.filters])

    order: List[Tuple[Any, ...]] = []
    buckets: Dict[Tuple[Any, ...], List[List[Any]]] = {}
    for row in sheet.rows:
        ok = True
        for f in plan_filters:
            cell = row[f.column_index] if f.column_index < len(row) else None
            if not excel_query.match_cell(cell, f.operator, f.value):
                ok = False
                break
        if not ok:
            continue
        key = tuple(row[i] if i < len(row) else None for i in request.group_by_indexes)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)

    rows: List[GroupRow] = []
    for key in order:
        bucket = buckets[key]
        matched = len(bucket)
        if request.operation == OPERATION_COUNT:
            value: Optional[float] = float(matched)
            numeric_rows, empty_rows, non_numeric_rows = matched, 0, 0
        else:
            ci = request.column_index or 0
            numbers: List[float] = []
            empty_rows = non_numeric_rows = 0
            calc_obj = calculation.ensure_resolved(sheet, request.calculation)
            for row in bucket:
                if calc_obj is not None:
                    num = calculation.calc_python_value(calc_obj, row)
                    if num is None:
                        empty_rows += 1
                    else:
                        numbers.append(num)
                    continue
                raw = row[ci] if ci < len(row) else None
                if raw is None:
                    empty_rows += 1
                    continue
                num = aggregate_to_number(raw)
                if num is None:
                    non_numeric_rows += 1
                else:
                    numbers.append(num)
            numeric_rows = len(numbers)
            if not numbers:
                value = None
            elif request.operation == OPERATION_SUM:
                value = float(sum(numbers))
            elif request.operation == OPERATION_MIN:
                value = float(min(numbers))
            elif request.operation == OPERATION_MAX:
                value = float(max(numbers))
            else:
                value = float(sum(numbers) / len(numbers))

        rows.append(GroupRow(
            group=_group_cells_from_values(sheet, request.group_by_indexes, list(key)),
            value=value,
            matched_rows=matched,
            numeric_rows=numeric_rows,
            empty_rows=empty_rows,
            non_numeric_rows=non_numeric_rows,
        ))

    total_matched = sum(r.matched_rows for r in rows)
    span = None
    if rows:
        # 整体匹配范围：从 Sheet 定位首个/末个命中行的 Excel 行号
        matched_excel_rows = _matched_excel_rows(sheet, plan_filters)
        if matched_excel_rows:
            span = {'first': matched_excel_rows[0], 'last': matched_excel_rows[-1]}

    # Phase 3C：排序（与 DuckDB 的 ORDER BY ... NULLS LAST 完全一致）后截取 TOP-N
    total_groups_before = len(rows)
    rows = sort_group_rows(rows, request)
    if request.top_n is not None:
        rows = rows[:request.top_n]

    return _build_group_result(representation, request, sheet, rows, total_matched, span,
                               total_groups_before)


def _matched_excel_rows(
    sheet: SheetRepresentation, plan_filters: Sequence[duck_engine.PlanFilter]
) -> List[int]:
    out: List[int] = []
    for i, row in enumerate(sheet.rows):
        ok = True
        for f in plan_filters:
            cell = row[f.column_index] if f.column_index < len(row) else None
            if not excel_query.match_cell(cell, f.operator, f.value):
                ok = False
                break
        if ok:
            out.append(sheet.row_excel_numbers[i] if i < len(sheet.row_excel_numbers) else i + 1)
    return out


def _build_group_result(
    representation: WorkbookRepresentation,
    request: AggregateRequest,
    sheet: SheetRepresentation,
    rows: List[GroupRow],
    total_matched: int,
    span: Optional[Dict[str, int]],
    total_groups_before: int,
) -> GroupedAggregateResult:
    group_meta = [
        {'name': sheet.columns[i].name, 'index': i,
         'letter': sheet.columns[i].excel_column_letter}
        for i in request.group_by_indexes
    ]
    return GroupedAggregateResult(
        document_id=representation.document_id,
        sheet_index=sheet.sheet_index,
        sheet_name=sheet.sheet_name,
        operation=request.operation,
        column=request.column,
        column_index=request.column_index,
        column_letter=request.column_letter,
        group_by=group_meta,
        filters=list(request.filters),
        rows=rows,
        matched_rows=total_matched,
        total_rows_in_sheet=sheet.row_count,
        row_excel_span=span,
        column_numeric_in_sheet=request.column_numeric_in_sheet,
        definition=_group_definition(request.operation, request.column, request.group_by,
                                     request.order_by, request.order_dir, request.top_n,
                                     request.calculation),
        order_by=request.order_by,
        order_dir=request.order_dir,
        top_n=request.top_n,
        total_groups_before_top=total_groups_before,
        calculation=request.calculation,
    )


def format_group_aggregate_summary(result: GroupedAggregateResult, filename: str) -> str:
    """分组统计摘要（Python 生成，含来源与口径，不经过 LLM）。"""
    group_names = [g['name'] for g in result.group_by]
    op_label = OPERATION_LABELS.get(result.operation, result.operation)
    is_count = result.operation == OPERATION_COUNT

    lines = [
        f'已对「{filename}」的 Sheet「{result.sheet_name}」执行分组统计。',
        f'- 分组字段：{" + ".join(group_names)}',
        f'- 操作：{op_label}',
    ]
    if result.column:
        lines.append(f'- 目标列：{result.column}（{result.column_letter} 列）')
    if result.calculation:
        lines.append(f'- 计算字段：{result.calculation["label"]}'
                     f'（逐行计算后再 {OPERATION_LABELS.get(result.operation, result.operation)}）')
    if result.is_truncated:
        lines.append(
            f'- 分组数：{result.total_groups} 个 → **TOP-{result.top_n} 截取前 {result.returned_groups} 个**'
            f'｜匹配总行数：{result.matched_rows} 行（该 Sheet 共 {result.total_rows_in_sheet} 行）'
        )
    else:
        lines.append(
            f'- 分组数：{result.total_groups} 个｜匹配总行数：{result.matched_rows} 行'
            f'（该 Sheet 共 {result.total_rows_in_sheet} 行）'
        )
    if result.is_sorted:
        lines.append(f'- 排序：按「{result.order_by_label()}」'
                     f'{ORDER_DIR_LABELS.get(result.order_dir, result.order_dir)}'
                     + (f'｜TOP-{result.top_n}' if result.top_n is not None else ''))
        lines.append('- 排序说明：' + result.sort_description())
    else:
        lines.append('- 排序：无（结果顺序为数据库返回顺序）')
    if result.row_excel_span:
        lines.append(
            f'- 匹配 Excel 行区间：{result.row_excel_span["first"]} ~ {result.row_excel_span["last"]}'
        )
    lines.append('- 筛选条件：' + ('；'.join(_filter_text(f) for f in result.filters) if result.filters else '无（全表）'))

    lines.append('- 结果（顺序即数据库返回顺序，前端不再排序）：' if result.is_sorted
                 else '- 结果（未做排序，顺序为数据库返回顺序）：')
    if not result.rows:
        lines.append('    （没有匹配的数据行，因此没有任何分组）')
    else:
        for r in result.rows:
            key = ' / '.join(c.display for c in r.group)
            if is_count:
                detail = f'{r.matched_rows} 行'
            else:
                detail = (f'{r.matched_rows} 行，可数值化 {r.numeric_rows} 行'
                          f'（空值 {r.empty_rows}、非数值 {r.non_numeric_rows} 不计入）')
            lines.append(f'    {key} → {format_number(r.value, is_count=is_count)}（{detail}）')
    lines.append(f'- 统计口径：{result.definition}')
    return '\n'.join(lines)
