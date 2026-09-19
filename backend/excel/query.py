# -*- coding: utf-8 -*-
"""Structured Query（Phase 1B）

职责（严格单一）：
    Unified Representation  ->  Structured Query Result

即：只做「读取 / 筛选 / 匹配 / 切片 / 限制数量 / 来源行号 / 列定位」。

明确不做（留待后续阶段）：
- 不依赖 Chroma（数据源只有 representation.json）
- 不调用 LLM（事实数据完全由 Python 判定，LLM 不参与筛选）
- 不做 SQL / DuckDB / NL2SQL / Text-to-SQL
- 不做 COUNT / SUM / AVG / GROUP BY / 排序 / TOP-N 等统计与计算
- 不重新解析原始 Excel（调用方通过 store.load_representation 传入表示）

筛选语义：
- operator: 'eq' | 'neq' | 'gt' | 'gte' | 'lt' | 'lte' | 'contains'
    * eq       : 精确比较。优先字符串比较（保证 Order ID / SKU ID 这类
                 超长业务 ID 逐位安全，绝不退化为科学计数法）；
                 当单元格本身是原生数值时，额外允许数值等值（容差 0）。
                 值支持 None（匹配空单元格）与 ""（匹配字面空字符串）。
    * neq      : eq 的取反。**空单元格(None)不匹配 neq**（与 SQL <> 直觉一致）。
    * gt/gte/lt/lte : **数值范围比较**。单元格必须可数值化（str(cell) 可转 float），
                 否则该行不匹配（不报错）。值必须是数值（否则报参数错误）。
    * contains : 子串匹配，大小写不敏感（SQL LIKE 的常见直觉）；
                 needle 必须是非空字符串。
- match_mode: 仅 'and'（多个 filter 全部满足）。本阶段不支持 'or'，传入即报错。
- 空值语义（显式定义，不与 "" 混淆）：
    * representation 中空单元格统一为 None；
    * eq + value=None  -> 匹配单元格为 None 的行；
    * eq + value=""    -> 匹配"字面空字符串"的单元格（本表示下通常为 0 行）；
    * contains + value="" -> 视为非法参数（空 needle 无意义）。

说明（Phase 2）：
本文件同时是 **DuckDB 引擎的语义基准**：`duck.py` 生成的 SQL 必须与这里
的逐行匹配结果完全一致（测试中用差分对比保证）。DuckDB 是执行引擎，
这里是参考实现与降级回退路径。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.excel import calculation
from backend.excel.representation import ColumnMeta, SheetRepresentation, WorkbookRepresentation

# ----------------------------------------------------------------------------
# 常量与错误
# ----------------------------------------------------------------------------
OPERATOR_EQ = 'eq'
OPERATOR_NEQ = 'neq'
OPERATOR_GT = 'gt'
OPERATOR_GTE = 'gte'
OPERATOR_LT = 'lt'
OPERATOR_LTE = 'lte'
OPERATOR_CONTAINS = 'contains'

#: 数值范围类操作符
RANGE_OPERATORS: Tuple[str, ...] = (OPERATOR_GT, OPERATOR_GTE, OPERATOR_LT, OPERATOR_LTE)

SUPPORTED_OPERATORS: Tuple[str, ...] = (
    OPERATOR_EQ, OPERATOR_NEQ, OPERATOR_GT, OPERATOR_GTE, OPERATOR_LT, OPERATOR_LTE,
    OPERATOR_CONTAINS,
)

MATCH_MODE_AND = 'and'
SUPPORTED_MATCH_MODES: Tuple[str, ...] = (MATCH_MODE_AND,)

DEFAULT_LIMIT = 50
MAX_LIMIT = 500

# 错误码 -> HTTP 状态码
ERR_DOCUMENT_NOT_FOUND = 'document_not_found'
ERR_SHEET_NOT_FOUND = 'sheet_not_found'
ERR_SHEET_CONFLICT = 'sheet_conflict'
ERR_COLUMN_NOT_FOUND = 'column_not_found'
ERR_COLUMN_AMBIGUOUS = 'column_ambiguous'
ERR_INVALID_OPERATOR = 'invalid_operator'
ERR_INVALID_PARAM = 'invalid_param'

_ERROR_STATUS: Dict[str, int] = {
    ERR_DOCUMENT_NOT_FOUND: 404,
    ERR_SHEET_NOT_FOUND: 404,
    ERR_SHEET_CONFLICT: 400,
    ERR_COLUMN_NOT_FOUND: 400,
    ERR_COLUMN_AMBIGUOUS: 400,
    ERR_INVALID_OPERATOR: 400,
    ERR_INVALID_PARAM: 400,
}


class ExcelQueryError(Exception):
    """结构化查询的领域错误（含 HTTP 状态码，供 API 层直接映射）。"""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = _ERROR_STATUS.get(code, 400)
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {'code': self.code, 'message': self.message, 'details': self.details}


# ----------------------------------------------------------------------------
# 请求 / 结果模型（纯 dataclass，便于脱离 HTTP 做单元测试）
# ----------------------------------------------------------------------------
@dataclass
class FilterCondition:
    column: str
    operator: str = OPERATOR_EQ
    value: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {'column': self.column, 'operator': self.operator, 'value': self.value}


@dataclass
class StructuredQueryRequest:
    document_id: str
    user_id: Optional[int] = None
    sheet_index: Optional[int] = None
    sheet_name: Optional[str] = None
    columns: Optional[List[str]] = None          # None 或 [] -> 返回全部列
    filters: List[FilterCondition] = field(default_factory=list)
    match_mode: str = MATCH_MODE_AND
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    # Phase 4B：受控计算字段（逐行计算；见 calculation.py）
    calculation: Optional[Dict[str, Any]] = None      # {'operation','left_column','right_column'}
    calc_filter: Optional[Dict[str, Any]] = None      # {'operator','value'} 对计算值比较


@dataclass
class StructuredQueryResult:
    document_id: str
    sheet_index: int
    sheet_name: str
    columns: List[Dict[str, Any]]                # 投影列元信息（含 excel_column_letter）
    rows: List[List[Any]]                        # 投影后的数据行（保持原始顺序）
    row_excel_numbers: List[int]                 # 每行对应的 Excel 原始行号（1-based）
    row_ranges: List[str]                        # 例如 "A226:J226"（按投影列跨度）
    total_rows_in_sheet: int
    total_matches: int
    returned_count: int
    limit: int
    offset: int
    has_more: bool
    next_offset: Optional[int]
    applied_filters: List[Dict[str, Any]]
    # Phase 4B：计算字段信息（已解析到真实列；无计算字段时为 None）
    calculation: Optional[Dict[str, Any]] = None
    calc_filter: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'document_id': self.document_id,
            'sheet_index': self.sheet_index,
            'sheet_name': self.sheet_name,
            'columns': self.columns,
            'rows': self.rows,
            'row_excel_numbers': self.row_excel_numbers,
            'row_ranges': self.row_ranges,
            'total_rows_in_sheet': self.total_rows_in_sheet,
            'total_matches': self.total_matches,
            'returned_count': self.returned_count,
            'limit': self.limit,
            'offset': self.offset,
            'has_more': self.has_more,
            'next_offset': self.next_offset,
            'applied_filters': self.applied_filters,
            'calculation': self.calculation,
            'calc_filter': self.calc_filter,
        }


# ----------------------------------------------------------------------------
# 参数解析与校验
# ----------------------------------------------------------------------------
def _as_int(value: Any, name: str, allow_none: bool = False) -> Optional[int]:
    if value is None:
        if allow_none:
            return None
        raise ExcelQueryError(ERR_INVALID_PARAM, f'参数 {name} 不能为空')
    if isinstance(value, bool):
        raise ExcelQueryError(ERR_INVALID_PARAM, f'参数 {name} 必须是整数，收到布尔值')
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip('+-').isdigit():
        return int(value.strip())
    raise ExcelQueryError(ERR_INVALID_PARAM, f'参数 {name} 必须是整数，收到 {value!r}')


def parse_request(payload: Dict[str, Any], document_id: str, user_id: Optional[int] = None) -> StructuredQueryRequest:
    """把原始 payload 校验并规约为 StructuredQueryRequest（所有校验集中在此，便于单测）。"""
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ExcelQueryError(ERR_INVALID_PARAM, '请求体必须是 JSON 对象')

    sheet_index = _as_int(payload.get('sheet_index'), 'sheet_index', allow_none=True)
    sheet_name = payload.get('sheet_name')
    if sheet_name is not None and not isinstance(sheet_name, str):
        raise ExcelQueryError(ERR_INVALID_PARAM, '参数 sheet_name 必须是字符串')
    if isinstance(sheet_name, str) and sheet_name.strip() == '':
        sheet_name = None

    columns = payload.get('columns')
    if columns is None:
        columns = None
    elif isinstance(columns, (list, tuple)):
        norm_cols: List[str] = []
        for c in columns:
            if not isinstance(c, str) or c.strip() == '':
                raise ExcelQueryError(ERR_INVALID_PARAM, f'columns 中存在非法列名：{c!r}')
            norm_cols.append(c)
        columns = norm_cols or None
    else:
        raise ExcelQueryError(ERR_INVALID_PARAM, '参数 columns 必须是字符串数组')

    match_mode = payload.get('match_mode') or MATCH_MODE_AND
    if not isinstance(match_mode, str):
        raise ExcelQueryError(ERR_INVALID_PARAM, '参数 match_mode 必须是字符串')
    match_mode = match_mode.strip().lower()
    if match_mode not in SUPPORTED_MATCH_MODES:
        raise ExcelQueryError(
            ERR_INVALID_PARAM,
            f'暂不支持 match_mode={match_mode!r}，本轮仅支持 {list(SUPPORTED_MATCH_MODES)}',
        )

    raw_filters = payload.get('filters')
    if raw_filters is None:
        raw_filters = []
    if not isinstance(raw_filters, (list, tuple)):
        raise ExcelQueryError(ERR_INVALID_PARAM, '参数 filters 必须是数组')

    filters: List[FilterCondition] = []
    calc_raw_filter: Optional[Dict[str, Any]] = None
    for i, item in enumerate(raw_filters):
        if not isinstance(item, dict):
            raise ExcelQueryError(ERR_INVALID_PARAM, f'filters[{i}] 必须是对象')
        column = item.get('column')
        if not isinstance(column, str) or column.strip() == '':
            raise ExcelQueryError(ERR_INVALID_PARAM, f'filters[{i}].column 不能为空')
        # Phase 4B：指向"计算值"的筛选单独承载（不进入真实列解析）
        if calculation.is_calc_filter_column(column):
            calc_raw_filter = {'operator': item.get('operator') or 'gt', 'value': item.get('value')}
            continue
        operator = item.get('operator') or OPERATOR_EQ
        if not isinstance(operator, str):
            raise ExcelQueryError(ERR_INVALID_PARAM, f'filters[{i}].operator 必须是字符串')
        operator = operator.strip().lower()
        if operator not in SUPPORTED_OPERATORS:
            raise ExcelQueryError(
                ERR_INVALID_OPERATOR,
                f'不支持的 operator={operator!r}，本轮仅支持 {list(SUPPORTED_OPERATORS)}',
                {'filter_index': i, 'supported': list(SUPPORTED_OPERATORS)},
            )
        value = item.get('value')
        if operator == OPERATOR_CONTAINS:
            if not isinstance(value, str):
                raise ExcelQueryError(
                    ERR_INVALID_PARAM,
                    f'filters[{i}]: contains 的 value 必须是字符串',
                )
            if value == '':
                raise ExcelQueryError(
                    ERR_INVALID_PARAM,
                    f'filters[{i}]: contains 的 value 不能为空字符串（空 needle 无意义）',
                )
        elif operator in RANGE_OPERATORS:
            # 只做"可数值化"校验；原始值原样保留（比对时才转换）
            if to_number(value) is None:
                raise ExcelQueryError(
                    ERR_INVALID_PARAM,
                    f'filters[{i}]: {operator} 的 value 必须是数值，收到 {value!r}',
                )
        filters.append(FilterCondition(column=column, operator=operator, value=value))

    limit = DEFAULT_LIMIT if payload.get('limit') is None else _as_int(payload.get('limit'), 'limit')
    offset = 0 if payload.get('offset') is None else _as_int(payload.get('offset'), 'offset')

    if limit <= 0:
        raise ExcelQueryError(ERR_INVALID_PARAM, f'limit 必须 >= 1，收到 {limit}')
    if limit > MAX_LIMIT:
        raise ExcelQueryError(ERR_INVALID_PARAM, f'limit 不能超过 {MAX_LIMIT}，收到 {limit}')
    if offset < 0:
        raise ExcelQueryError(ERR_INVALID_PARAM, f'offset 必须 >= 0，收到 {offset}')

    # Phase 4B：计算字段（形状 + 运算符白名单；真实列解析在 build_plan/execute_query 中）
    calculation_raw = calculation.normalize_calculation(payload.get('calculation'))
    if calculation_raw is not None:
        calculation_raw = calculation_raw.to_dict()
    explicit_calc_filter = calculation.normalize_calc_filter(payload.get('calc_filter'))
    if explicit_calc_filter is not None and calc_raw_filter is not None:
        raise ExcelQueryError(
            ERR_INVALID_PARAM, '对计算字段的筛选只能给一次（filters 里的计算列 与 calc_filter 不能同时出现）'
        )
    calc_filter = explicit_calc_filter or calculation.normalize_calc_filter(calc_raw_filter)
    if calc_filter is not None and calculation_raw is None:
        raise ExcelQueryError(
            ERR_INVALID_PARAM, '对计算字段筛选前必须先给出 calculation（计算字段定义）'
        )

    return StructuredQueryRequest(
        document_id=document_id,
        user_id=user_id,
        sheet_index=sheet_index,
        sheet_name=sheet_name,
        columns=columns,
        filters=filters,
        match_mode=match_mode,
        limit=limit,
        offset=offset,
        calculation=calculation_raw,
        calc_filter=calc_filter,
    )


# ----------------------------------------------------------------------------
# Sheet / 列定位
# ----------------------------------------------------------------------------
def resolve_sheet(
    representation: WorkbookRepresentation,
    sheet_index: Optional[int],
    sheet_name: Optional[str],
) -> SheetRepresentation:
    """定位目标 Sheet。sheet_index 与 sheet_name 同时提供且冲突时明确报错。"""
    if not representation.sheets:
        raise ExcelQueryError(ERR_SHEET_NOT_FOUND, '该表格文档没有任何 Sheet')

    by_index: Optional[SheetRepresentation] = None
    if sheet_index is not None:
        if sheet_index < 0 or sheet_index >= len(representation.sheets):
            raise ExcelQueryError(
                ERR_SHEET_NOT_FOUND,
                f'sheet_index={sheet_index} 越界（共 {len(representation.sheets)} 个 Sheet）',
                {'available': representation.sheets_meta()},
            )
        by_index = representation.sheets[sheet_index]

    by_name: Optional[SheetRepresentation] = None
    if sheet_name is not None:
        target = sheet_name.strip()
        exact = [s for s in representation.sheets if s.sheet_name == target]
        if len(exact) == 1:
            by_name = exact[0]
        elif len(exact) > 1:
            raise ExcelQueryError(ERR_SHEET_CONFLICT, f'sheet_name={sheet_name!r} 存在多个同名 Sheet')
        else:
            ci = [s for s in representation.sheets if s.sheet_name.strip().lower() == target.lower()]
            if len(ci) == 1:
                by_name = ci[0]
            elif len(ci) > 1:
                raise ExcelQueryError(ERR_SHEET_CONFLICT, f'sheet_name={sheet_name!r} 匹配到多个 Sheet')
            else:
                raise ExcelQueryError(
                    ERR_SHEET_NOT_FOUND,
                    f'不存在 Sheet：{sheet_name!r}',
                    {'available': [s.sheet_name for s in representation.sheets]},
                )

    if by_index is not None and by_name is not None:
        if by_index.sheet_index != by_name.sheet_index:
            raise ExcelQueryError(
                ERR_SHEET_CONFLICT,
                f'sheet_index={sheet_index} 与 sheet_name={sheet_name!r} 指向不同 Sheet',
                {'by_index': by_index.sheet_name, 'by_name': by_name.sheet_name},
            )
        return by_index

    if by_index is not None:
        return by_index
    if by_name is not None:
        return by_name
    return representation.sheets[0]


def resolve_column(sheet: SheetRepresentation, name: str) -> ColumnMeta:
    """把列名解析为 ColumnMeta。

    规则（严格、可解释、绝不模糊自动选择）：
    1. 先做「大小写敏感的精确匹配」；
    2. 再做「大小写不敏感的精确匹配」；
    3. 任一阶段出现多个候选 -> column_ambiguous（要求调用方消歧）；
    4. 都没有 -> column_not_found（附 available + 参考建议，但绝不自动选取）。
    """
    target = name.strip()

    exact = [c for c in sheet.columns if c.name == target]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ExcelQueryError(
            ERR_COLUMN_AMBIGUOUS,
            f'列名 {name!r} 存在歧义（{len(exact)} 个候选）',
            {'candidates': [c.name for c in exact]},
        )

    ci = [c for c in sheet.columns if c.name.strip().lower() == target.lower()]
    if len(ci) == 1:
        return ci[0]
    if len(ci) > 1:
        raise ExcelQueryError(
            ERR_COLUMN_AMBIGUOUS,
            f'列名 {name!r} 大小写不敏感匹配存在歧义（{len(ci)} 个候选）',
            {'candidates': [c.name for c in ci]},
        )

    # 仅给出建议，绝不自动采用（避免 "Order ID" 误匹配 "Order ID Description"）
    suggestions = [c.name for c in sheet.columns if target.lower() in c.name.lower()][:5]
    raise ExcelQueryError(
        ERR_COLUMN_NOT_FOUND,
        f'列不存在：{name!r}',
        {'available': sheet.column_names, 'suggestions': suggestions},
    )


# ----------------------------------------------------------------------------
# 匹配语义
# ----------------------------------------------------------------------------
def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def to_number(value: Any) -> Optional[float]:
    """把任意值安全转成 float。

    - bool 不是数值（避免 True 被当成 1）；
    - None / 空串 / 非数值文本 -> None（调用方据此判定"该行不匹配"或"参数非法"）；
    - 刻意使用 float() 而非 repr 解析，保证与 DuckDB TRY_CAST(... AS DOUBLE) 语义一致。
    """
    if value is None or isinstance(value, bool):
        return None
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


def eq_match(cell: Any, value: Any) -> bool:
    """等值匹配（字符串安全）。

    - 双方都为 None -> True；只有一方为 None -> False（None 与 "" 严格区分）。
    - 先做字符串比较：str(cell) == str(value)。这保证超长业务 ID 逐位精确，
      永不经过 float，也就不会出现 5.775...e17。
    - 若单元格是原生数值，再允许一次数值等值（容差 0），使 "19.90" == 19.9。
    """
    if cell is None and value is None:
        return True
    if cell is None or value is None:
        return False
    if str(cell) == str(value):
        return True
    if _is_number(cell) and not isinstance(value, bool):
        try:
            return abs(float(cell) - float(value)) <= 1e-9
        except (TypeError, ValueError):
            return False
    return False


def contains_match(cell: Any, value: Any) -> bool:
    """子串匹配（大小写不敏感）。cell 为 None 时恒为 False。"""
    if cell is None or value is None:
        return False
    return str(value).lower() in str(cell).lower()


def neq_match(cell: Any, value: Any) -> bool:
    """不等匹配。

    空单元格(None) **不匹配** neq（与 SQL ``<>`` 直觉一致，也与 eq/contains 的
    NULL 语义保持统一：NULL 不参与任何"相等/不等"判定）。
    """
    if cell is None:
        return False
    return not eq_match(cell, value)


def range_match(cell: Any, operator: str, value: Any) -> bool:
    """数值范围比较。单元格不可数值化（含 None / 空串 / 文本）时恒为 False。"""
    cell_num = to_number(cell)
    if cell_num is None:
        return False
    target = to_number(value)
    if target is None:
        return False
    if operator == OPERATOR_GT:
        return cell_num > target
    if operator == OPERATOR_GTE:
        return cell_num >= target
    if operator == OPERATOR_LT:
        return cell_num < target
    if operator == OPERATOR_LTE:
        return cell_num <= target
    return False  # pragma: no cover


def match_cell(cell: Any, operator: str, value: Any) -> bool:
    """单条件匹配分派（Python 参考实现）。"""
    if operator == OPERATOR_EQ:
        return eq_match(cell, value)
    if operator == OPERATOR_NEQ:
        return neq_match(cell, value)
    if operator == OPERATOR_CONTAINS:
        return contains_match(cell, value)
    if operator in RANGE_OPERATORS:
        return range_match(cell, operator, value)
    raise ExcelQueryError(ERR_INVALID_OPERATOR, f'不支持的 operator：{operator!r}')


def _row_matches(row: List[Any], resolved_filters: List[Tuple[int, str, Any]]) -> bool:
    """resolved_filters: [(col_index, operator, value)]；当前为 AND 语义。"""
    for col_index, operator, value in resolved_filters:
        cell = row[col_index] if col_index < len(row) else None
        if not match_cell(cell, operator, value):
            return False
    return True


# ----------------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------------
def select_projection(
    sheet: SheetRepresentation,
    request: 'StructuredQueryRequest',
    calc: Optional['calculation.Calculation'] = None,
) -> List[ColumnMeta]:
    """投影列选择（**两个引擎共用同一规则**，避免 DuckDB / Python 结果不一致）。

    规则：
    - 显式给了 columns -> 按给的去重后使用；若有计算字段，**保证左右列一定在结果里**；
    - 没给 columns 且有计算字段 -> 只返回计算依据的两列（避免 63 列噪音）；
    - 没给 columns 且无计算字段 -> 全部列（Phase 1B/2 原语义）。
    """
    if request.columns:
        seen = set()
        selected: List[ColumnMeta] = []
        for name in request.columns:
            col = resolve_column(sheet, name)
            if col.index not in seen:
                seen.add(col.index)
                selected.append(col)
        if calc is not None:
            for idx in (calc.left_index, calc.right_index):
                if idx is not None and idx not in seen:
                    selected.append(sheet.columns[idx])
                    seen.add(idx)
    elif calc is not None:
        selected = [sheet.columns[calc.left_index], sheet.columns[calc.right_index]]
    else:
        selected = list(sheet.columns)

    if not selected:
        raise ExcelQueryError(ERR_INVALID_PARAM, '投影列不能为空（该 Sheet 没有任何列）')
    return selected


def _calc_match(value: Optional[float], operator: str, target: float) -> bool:
    """计算值的比较（与 SQL 三值逻辑一致：NULL 不匹配任何比较，包括 neq）。"""
    if value is None:
        return False
    if operator == 'gt':
        return value > target
    if operator == 'gte':
        return value >= target
    if operator == 'lt':
        return value < target
    if operator == 'lte':
        return value <= target
    if operator == 'eq':
        return abs(value - target) <= 1e-9
    if operator == 'neq':
        return abs(value - target) > 1e-9
    raise ExcelQueryError(ERR_INVALID_OPERATOR, f'不支持的计算字段比较运算符：{operator!r}')


def execute_query(
    representation: WorkbookRepresentation,
    request: StructuredQueryRequest,
) -> StructuredQueryResult:
    """在 Representation 上执行结构化查询（纯函数，无 IO、无 LLM、无 Chroma）。

    Phase 4B：支持**受控计算字段**（逐行计算；可投影、可参与筛选）。
    """
    sheet = resolve_sheet(representation, request.sheet_index, request.sheet_name)

    # 0) Phase 4B：计算字段解析（真实列必须唯一解析）
    calc = calculation.normalize_calculation(request.calculation)
    if calc is not None:
        calc = calculation.resolve_calculation(sheet, calc)
        request.calculation = calc.to_dict()

    # 1) 投影列解析（与 DuckDB 引擎共用同一规则）
    selected = select_projection(sheet, request, calc)
    sel_indexes = [c.index for c in selected]

    # 2) 过滤条件解析（可引用未被投影的列）
    resolved_filters: List[Tuple[int, str, Any]] = []
    applied_filters: List[Dict[str, Any]] = []
    for f in request.filters:
        col = resolve_column(sheet, f.column)
        resolved_filters.append((col.index, f.operator, f.value))
        applied_filters.append({
            'column': f.column,
            'resolved_column': col.name,
            'column_index': col.index,
            'column_letter': col.excel_column_letter,
            'operator': f.operator,
            'value': f.value,
        })
    calc_filter = request.calc_filter
    if calc_filter is not None:
        applied_filters.append({
            'column': calculation.CALC_FILTER_COLUMN,
            'resolved_column': calc.label() if calc else calculation.CALC_FILTER_COLUMN,
            'column_index': -1,
            'column_letter': '',
            'operator': calc_filter['operator'],
            'value': calc_filter['value'],
            'is_calculated': True,
        })

    # 3) 逐行匹配（保持 Representation 的原始顺序；LLM 不参与任何判定）
    matched_rows: List[List[Any]] = []
    matched_excel_rows: List[int] = []
    matched_calc_values: List[Optional[float]] = []
    for i, row in enumerate(sheet.rows):
        if not _row_matches(row, resolved_filters):
            continue
        value = calculation.calc_python_value(calc, row) if calc is not None else None
        if calc_filter is not None:
            if not _calc_match(value, calc_filter['operator'], calc_filter['value']):
                continue
        matched_rows.append(row)
        matched_calc_values.append(value)
        matched_excel_rows.append(
            sheet.row_excel_numbers[i] if i < len(sheet.row_excel_numbers) else i + 1
        )

    total_matches = len(matched_rows)

    # 4) 分页切片（offset 超过总数 -> 空结果，不是错误）
    start = request.offset
    end = start + request.limit
    page_rows = matched_rows[start:end]
    page_excel_rows = matched_excel_rows[start:end]
    page_calc_values = matched_calc_values[start:end]

    # 5) 投影 + 来源范围
    span_start_letter = selected[0].excel_column_letter
    span_end_letter = selected[-1].excel_column_letter
    if selected[0].excel_column > selected[-1].excel_column:
        span_start_letter = selected[-1].excel_column_letter
        span_end_letter = selected[0].excel_column_letter

    projected_rows: List[List[Any]] = []
    row_ranges: List[str] = []
    for row, excel_row, calc_value in zip(page_rows, page_excel_rows, page_calc_values):
        projected = [row[idx] if idx < len(row) else None for idx in sel_indexes]
        if calc is not None:
            projected.append(calc_value)
        projected_rows.append(projected)
        row_ranges.append(f'{span_start_letter}{excel_row}:{span_end_letter}{excel_row}')

    returned_count = len(projected_rows)
    has_more = (start + returned_count) < total_matches

    columns_meta = [c.to_dict() for c in selected]
    if calc is not None:
        columns_meta.append(calculation.calc_column_meta(calc))

    return StructuredQueryResult(
        document_id=representation.document_id,
        sheet_index=sheet.sheet_index,
        sheet_name=sheet.sheet_name,
        columns=columns_meta,
        rows=projected_rows,
        row_excel_numbers=page_excel_rows,
        row_ranges=row_ranges,
        total_rows_in_sheet=sheet.row_count,
        total_matches=total_matches,
        returned_count=returned_count,
        limit=request.limit,
        offset=request.offset,
        has_more=has_more,
        next_offset=(start + returned_count) if has_more else None,
        applied_filters=applied_filters,
        calculation=calc.to_dict() if calc is not None else None,
        calc_filter=dict(calc_filter) if calc_filter is not None else None,
    )


def query_representation(
    representation: WorkbookRepresentation,
    payload: Dict[str, Any],
) -> StructuredQueryResult:
    """便捷入口：payload -> 校验 -> 执行（供 API 层一行调用）。"""
    request = parse_request(payload, document_id=representation.document_id, user_id=representation.user_id)
    return execute_query(representation, request)
