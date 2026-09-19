# -*- coding: utf-8 -*-
"""Phase 2：DuckDB 表格数据层（精确筛选执行引擎）

职责边界（严格）：
- **数据来源只有 representation.json**：本模块不重新解析原始 Excel、
  不依赖 Chroma、不调用 LLM。
- **DuckDB 只负责执行**：把已经过 schema 校验的 StructuredQueryRequest
  编译成受控 SQL 并执行，返回真实数据行。
- **SQL 由 Python 构造**：LLM 永远不产出 SQL 文本，也永远不参与取数。

架构：
    StructuredQueryRequest
        ↓  build_plan()       （schema 校验：列名/operator 白名单）
    QueryPlan                 （纯 Python 对象，不含任何 SQL 文本）
        ↓  render_sql()       （唯一生成 SQL 的地方；标识符全为 Python 生成，值全部参数绑定）
    DuckDB 执行
        ↓
    StructuredQueryResult     （与 Phase 1B 完全相同的结构，可直接替换）

安全限制（对应 Phase 2 安全要求）：
1. 只允许 SELECT；生成的 SQL 会被 _assert_safe_sql 二次校验（必须以 SELECT 开头、不含分号）。
2. 表名由 Python 依据 document_id 生成并消毒（只保留 [A-Za-z0-9_]），且始终双引号包裹。
3. 列名不进入 SQL 文本：每个逻辑列映射为物理名 ``c<N>``（Python 生成），
   因此任何用户/LLM 提供的字符串都不可能成为 SQL 标识符。
4. **查询阶段**所有筛选值一律使用 ``?`` 绑定参数，不做字符串拼接。
5. operator 只能来自白名单字典（eq/neq/gt/gte/lt/lte/contains）。
6. 连接使用 ``:memory:`` 且 ``enable_external_access=false``：
   ATTACH / COPY / 读外部文件一律被 DuckDB 拒绝。
7. 不提供任何写入接口（无 INSERT/UPDATE/DELETE/DROP/PRAGMA 入口）。
8. 唯一的例外是**物化阶段**的 INSERT：它把已解析落盘的 representation 单元格值
   批量写入内存表（为性能考虑使用字面量 + SQL 标准 ``''`` 转义，见 _render_ingest_statements），
   且语句形状被 _assert_ingest_sql 严格校验。该路径的数据**不来自 LLM 或用户查询文本**。
"""

import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.excel import calculation
from backend.excel import query as excel_query
from backend.excel.representation import SheetRepresentation, WorkbookRepresentation

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 依赖环境
    import duckdb

    DUCKDB_AVAILABLE = True
except Exception:  # pragma: no cover - 依赖环境
    duckdb = None
    DUCKDB_AVAILABLE = False

# 隐藏列：承载 Excel 原始行号（1-based），保证来源可追溯与顺序稳定
PHYS_ROW = '__excel_row__'
_SAFE_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
MAX_CACHED_DOCUMENTS = 8


class DuckEngineUnavailable(RuntimeError):
    """DuckDB 不可用（未安装 / 初始化失败）。调用方可据此回退到 Python 引擎。"""


def phys_col(index: int) -> str:
    """逻辑列索引 -> 物理列名（Python 生成，绝不来自用户输入）。"""
    return f'c{int(index)}'


def table_name(document_id: str, sheet_index: int) -> str:
    """物理表名（Python 生成 + 消毒）。"""
    safe = re.sub(r'[^A-Za-z0-9_]', '', str(document_id))[:32] or 'doc'
    return f't_{safe}_s{int(sheet_index)}'


# ----------------------------------------------------------------------------
# 物理类型推断与取值转换
# ----------------------------------------------------------------------------
def _infer_physical_type(values: Sequence[Any]) -> str:
    """按**真实单元格值**推断物理类型（不使用 ColumnMeta.dtype）。

    原因：真实数据里 ``Quantity`` 的 ColumnMeta.dtype 是 ``string``（单元格是文本数字），
    但筛选需要数值比较，因此统一以实际值判定，并在比较时使用 TRY_CAST。
    """
    if not values:
        return 'VARCHAR'
    if all(isinstance(v, bool) for v in values):
        return 'BOOLEAN'
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return 'BIGINT'
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return 'DOUBLE'
    return 'VARCHAR'


def infer_physical_type(values: Sequence[Any]) -> str:
    """对外暴露的物理类型推断（Phase 3C 的 Python 参考实现在复现 DuckDB 排序语义时复用）。"""
    return _infer_physical_type(values)


def _coerce_value(value: Any, phys_type: str) -> Any:
    """入库取值转换。VARCHAR 一律 str()，与 Phase 1B 的 str(cell) 语义保持一致。"""
    if value is None:
        return None
    if phys_type == 'VARCHAR':
        return value if isinstance(value, str) else str(value)
    if phys_type == 'BOOLEAN':
        return bool(value)
    if phys_type == 'BIGINT':
        return int(value)
    return float(value)


# ----------------------------------------------------------------------------
# 查询计划（Query Plan）—— 纯 Python 对象，不含 SQL 文本
# ----------------------------------------------------------------------------
@dataclass
class PlanFilter:
    column_index: int
    phys: str
    operator: str
    value: Any
    resolved_column: str
    column_letter: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'column': self.resolved_column,
            'column_index': self.column_index,
            'column_letter': self.column_letter,
            'operator': self.operator,
            'value': self.value,
        }


@dataclass
class QueryPlan:
    table: str
    select_phys: List[str]
    select_indexes: List[int]
    filters: List[PlanFilter] = field(default_factory=list)
    limit: int = excel_query.DEFAULT_LIMIT
    offset: int = 0
    excel_row_phys: str = PHYS_ROW
    # Phase 4B：受控计算字段（SQL 表达式由 Python 生成；别名固定）
    calc_expr: Optional[str] = None
    calc_alias: Optional[str] = None
    calc_filter: Optional[Tuple[str, float]] = None      # (operator, value)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'table': self.table,
            'select': [{'index': i, 'phys': p} for i, p in zip(self.select_indexes, self.select_phys)],
            'filters': [f.to_dict() for f in self.filters],
            'limit': self.limit,
            'offset': self.offset,
            'calc_expr': self.calc_expr,
            'calc_alias': self.calc_alias,
            'calc_filter': list(self.calc_filter) if self.calc_filter else None,
        }


def build_plan_filters(
    sheet: SheetRepresentation,
    filters: Sequence['excel_query.FilterCondition'],
) -> List[PlanFilter]:
    """把已校验的筛选条件编译成 PlanFilter 列表（Phase 3A 的统计层同样复用本函数）。

    - 筛选列名必须能在真实 schema 中唯一解析，否则抛 ExcelQueryError；
    - operator 必须在白名单内（parse_request 已保证，这里再校验一次）；
    - 筛选值按 operator 做类型约束（范围类必须是数值；contains 必须非空字符串）。
    """
    plan_filters: List[PlanFilter] = []
    for f in filters:
        col = excel_query.resolve_column(sheet, f.column)
        op = (f.operator or excel_query.OPERATOR_EQ).strip().lower()
        if op not in excel_query.SUPPORTED_OPERATORS:
            raise excel_query.ExcelQueryError(
                excel_query.ERR_INVALID_OPERATOR,
                f'不支持的 operator={op!r}',
                {'supported': list(excel_query.SUPPORTED_OPERATORS)},
            )
        value = f.value
        if op in excel_query.RANGE_OPERATORS:
            num = excel_query.to_number(value)
            if num is None:
                raise excel_query.ExcelQueryError(
                    excel_query.ERR_INVALID_PARAM,
                    f'筛选条件 {col.name!r} {op} 需要数值，收到 {value!r}',
                    {'column': col.name, 'operator': op, 'value': value},
                )
            value = num
        if op == excel_query.OPERATOR_CONTAINS:
            if not isinstance(value, str) or value == '':
                raise excel_query.ExcelQueryError(
                    excel_query.ERR_INVALID_PARAM,
                    f'筛选条件 {col.name!r} contains 需要非空字符串',
                    {'column': col.name, 'value': value},
                )
        plan_filters.append(PlanFilter(
            column_index=col.index,
            phys=phys_col(col.index),
            operator=op,
            value=value,
            resolved_column=col.name,
            column_letter=col.excel_column_letter,
        ))
    return plan_filters


def build_plan(
    table: str,
    sheet: SheetRepresentation,
    request: 'excel_query.StructuredQueryRequest',
) -> QueryPlan:
    """把已校验的 StructuredQueryRequest 编译成 QueryPlan（此处完成 schema 校验）。

    - 列名/筛选列名必须能在真实 schema 中唯一解析，否则抛 ExcelQueryError；
    - operator 必须在白名单内（parse_request 已保证，这里再校验一次）；
    - 筛选值按 operator 做类型约束（范围类必须是数值）。
    """
    # 0) Phase 4B：计算字段（真实列唯一解析 -> Python 生成 SQL 表达式 + 固定别名）
    calc_expr = None
    calc_alias = None
    calc = None
    if request.calculation is not None:
        calc = calculation.resolve_calculation(
            sheet, calculation.normalize_calculation(request.calculation))
        calc_expr = calculation.calc_sql_expr(calc)
        calc_alias = calc.alias

    # 1) 投影列（与 Phase 1B Python 引擎共用同一规则）
    selected = excel_query.select_projection(sheet, request, calc)

    # 2) 筛选条件
    plan_filters = build_plan_filters(sheet, request.filters)

    plan = QueryPlan(
        table=table,
        select_phys=[phys_col(c.index) for c in selected],
        select_indexes=[c.index for c in selected],
        filters=plan_filters,
        limit=request.limit,
        offset=request.offset,
        calc_expr=calc_expr,
        calc_alias=calc_alias,
        calc_filter=((request.calc_filter['operator'], float(request.calc_filter['value']))
                     if request.calc_filter else None),
    )
    _validate_plan(plan)
    return plan


def _validate_plan(plan: QueryPlan) -> None:
    """标识符安全断言（纵深防御：即使上游被改坏，也不会拼出危险 SQL）。"""
    if not _SAFE_IDENT.match(plan.table):
        raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, f'非法表名：{plan.table!r}')
    for name in list(plan.select_phys) + [plan.excel_row_phys] + [f.phys for f in plan.filters]:
        if not _SAFE_IDENT.match(name):
            raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, f'非法列标识：{name!r}')
    if plan.limit < 1 or plan.limit > excel_query.MAX_LIMIT:
        raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, f'limit 越界：{plan.limit}')
    if plan.offset < 0:
        raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, f'offset 非法：{plan.offset}')
    # Phase 4B：计算表达式必须符合白名单形态；别名必须是 Python 生成的安全标识符
    if plan.calc_expr is not None:
        try:
            calculation.assert_calc_expr_safe(plan.calc_expr)
        except excel_query.ExcelQueryError as e:
            raise excel_query.ExcelQueryError(
                excel_query.ERR_INVALID_PARAM, f'非法计算表达式：{e.message}') from e
        if not plan.calc_alias or not _SAFE_IDENT.match(plan.calc_alias):
            raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM,
                                              f'非法计算列别名：{plan.calc_alias!r}')
    if plan.calc_filter is not None and plan.calc_expr is None:
        raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM,
                                          '对计算字段筛选前必须先给出计算字段定义')


# ----------------------------------------------------------------------------
# SQL 渲染（唯一生成 SQL 的地方）
# ----------------------------------------------------------------------------
# 注意：整体用 COALESCE(..., FALSE) 包住，把 SQL 三值逻辑收敛为二值逻辑。
# 否则当 TRY_CAST(? AS DOUBLE) 为 NULL（值不是数字）时，`FALSE OR NULL` = NULL，
# 会让 neq 的 `NOT (FALSE OR NULL)` 变成 NULL 而不是 TRUE，从而漏掉本应命中的行。
_SQL_EQ = ('COALESCE(CAST({c} AS VARCHAR) = CAST(? AS VARCHAR) '
           'OR TRY_CAST({c} AS DOUBLE) = TRY_CAST(? AS DOUBLE), FALSE)')
#: 稳定性补丁：长数字串（业务 ID / 时间戳）只做**逐位字符串**比较。
#: 若仍叠加 DOUBLE 比较，double 的 ulp 在 1.7e18 附近约为 256，
#: 两个相邻的 18 位 ID 会被舍入成同一个 double，从而"误命中"另一行。
_SQL_EQ_EXACT = 'CAST({c} AS VARCHAR) = CAST(? AS VARCHAR)'
#: 判定"长数字串"的阈值（>= 12 位数字即不可能是有意义的金额/数量）
_LONG_DIGITS_RE = re.compile(r'^[+-]?\d{12,}$')


def _is_long_digit_value(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return len(str(abs(value))) >= 12
    if isinstance(value, str):
        return bool(_LONG_DIGITS_RE.match(value.strip()))
    return False
_SQL_CONTAINS = 'contains(lower(CAST({c} AS VARCHAR)), lower(?))'
_SQL_RANGE = {
    excel_query.OPERATOR_GT: 'TRY_CAST({c} AS DOUBLE) > TRY_CAST(? AS DOUBLE)',
    excel_query.OPERATOR_GTE: 'TRY_CAST({c} AS DOUBLE) >= TRY_CAST(? AS DOUBLE)',
    excel_query.OPERATOR_LT: 'TRY_CAST({c} AS DOUBLE) < TRY_CAST(? AS DOUBLE)',
    excel_query.OPERATOR_LTE: 'TRY_CAST({c} AS DOUBLE) <= TRY_CAST(? AS DOUBLE)',
}


def render_where(plan: QueryPlan) -> Tuple[str, List[Any]]:
    """渲染 WHERE 片段与绑定参数（QueryPlan 入口，Phase 3A 统计层复用下面的 filters 版本）。"""
    return render_where_filters(plan.filters)


def render_where_filters(filters: Sequence[PlanFilter]) -> Tuple[str, List[Any]]:
    """渲染 WHERE 片段与绑定参数（全部筛选值均使用参数绑定）。"""
    clauses: List[str] = []
    params: List[Any] = []

    for f in filters:
        col = f'"{f.phys}"'
        op, value = f.operator, f.value

        if op == excel_query.OPERATOR_EQ:
            if value is None:
                # 与 Phase 1B 语义一致：eq + None 匹配空单元格
                clauses.append(f'{col} IS NULL')
            elif _is_long_digit_value(value):
                # 长数字串（18/19 位业务 ID）：只逐位比较，绝不走 DOUBLE 舍入
                clauses.append(f'({_SQL_EQ_EXACT.format(c=col)})')
                params.append(value)
            else:
                clauses.append(f'({_SQL_EQ.format(c=col)})')
                params.extend([value, value])

        elif op == excel_query.OPERATOR_NEQ:
            if value is None:
                clauses.append(f'{col} IS NOT NULL')
            elif _is_long_digit_value(value):
                clauses.append(f'({col} IS NOT NULL AND NOT ({_SQL_EQ_EXACT.format(c=col)}))')
                params.append(value)
            else:
                # NULL 单元格不匹配 neq（与 SQL <> 直觉一致，且与 eq/contains 的 NULL 语义统一）
                clauses.append(f'({col} IS NOT NULL AND NOT ({_SQL_EQ.format(c=col)}))')
                params.extend([value, value])

        elif op == excel_query.OPERATOR_CONTAINS:
            clauses.append(f'({col} IS NOT NULL AND {_SQL_CONTAINS.format(c=col)})')
            params.append(str(value))

        elif op in _SQL_RANGE:
            clauses.append(_SQL_RANGE[op].format(c=col))
            params.append(float(value))

        else:  # pragma: no cover - build_plan 已拦截
            raise excel_query.ExcelQueryError(
                excel_query.ERR_INVALID_OPERATOR, f'不支持的 operator：{op!r}'
            )

    if not clauses:
        return '', params
    return ' AND '.join(clauses), params


def assert_safe_sql(sql: str) -> None:
    """对外暴露的 SQL 安全断言（Phase 3A 统计层复用）。"""
    _assert_safe_sql(sql)


def _assert_safe_sql(sql: str) -> None:
    """纵深防御：只允许单条 SELECT。"""
    head = sql.lstrip().lower()
    if not head.startswith('select'):
        raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, '只允许 SELECT 查询')
    if ';' in sql:
        raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, 'SQL 中不允许出现分号')


def render_sql(plan: QueryPlan) -> Dict[str, Any]:
    """生成 (count_sql, page_sql) 与各自参数。"""
    where, where_params = render_where(plan)
    # Phase 4B：计算字段参与 WHERE（表达式由 Python 生成；比较值仍然 ? 绑定）
    if plan.calc_filter is not None:
        fragment = calculation.calc_filter_sql_expr(plan.calc_expr, plan.calc_filter[0])
        where = f'{where} AND {fragment}' if where else fragment
        where_params = list(where_params) + [plan.calc_filter[1]]
    where_clause = f' WHERE {where}' if where else ''

    count_sql = f'SELECT COUNT(*) FROM "{plan.table}"{where_clause}'
    cols = ', '.join(f'"{p}"' for p in plan.select_phys)
    if plan.calc_expr:
        cols += f', {plan.calc_expr} AS "{plan.calc_alias}"'
    page_sql = (
        f'SELECT {cols}, "{plan.excel_row_phys}" FROM "{plan.table}"{where_clause} '
        f'ORDER BY "{plan.excel_row_phys}" LIMIT ? OFFSET ?'
    )
    _assert_safe_sql(count_sql)
    _assert_safe_sql(page_sql)
    return {
        'count_sql': count_sql,
        'count_params': list(where_params),
        'page_sql': page_sql,
        'page_params': list(where_params) + [int(plan.limit), int(plan.offset)],
    }


# ----------------------------------------------------------------------------
# 表物化（批量写入）
# ----------------------------------------------------------------------------
# 说明：DuckDB Python 客户端的**参数绑定**在本机实测非常慢
# （447 行 × 64 列 ≈ 2.9 万参数需要 ~25s，而字面量批量写入只需 ~0.15s）。
# 因此**物化阶段**改用「字面量 VALUES + SQL 标准单引号转义」批量写入：
#   * 物化数据来自已解析落盘的 representation（Excel/CSV 单元格值），
#     **绝不来自 LLM 输出或用户查询文本**；
#   * 字符串使用 '' 双写转义（SQL 标准；DuckDB 不解析反斜杠转义，已实测验证
#     往返一致，且 `'; DROP TABLE s; --` 之类的单元格不会破坏语句）；
#   * 数值/布尔/NULL 使用各自的字面量形式，不经过字符串拼接歧义。
# 查询阶段（WHERE 条件）**仍然全部使用 ? 绑定参数**，与物化路径无关。
INGEST_BATCH_ROWS = 200
# 字面量与结构允许的字符（括号外）：数字/指数/符号/逗号/括号/空白，
# 以及恰好作为独立单词出现的 TRUE / FALSE / NULL。
_INGEST_WORDS_RE = re.compile(r'\b(?:TRUE|FALSE|NULL)\b')
_INGEST_PLAIN_SAFE_RE = re.compile(r'^[\s,()0-9+\-.eE]*$')


def _sql_literal(value: Any, phys_type: str) -> str:
    """把单元格值渲染成 SQL 字面量（字符串走 '' 转义）。"""
    if value is None:
        return 'NULL'
    if phys_type == 'BOOLEAN':
        return 'TRUE' if value else 'FALSE'
    if phys_type == 'BIGINT':
        return str(int(value))
    if phys_type == 'DOUBLE':
        num = float(value)
        if num != num or num in (float('inf'), float('-inf')):  # NaN / Inf 不合法
            return 'NULL'
        return repr(num)
    return "'" + str(value).replace("'", "''") + "'"


def _assert_ingest_sql(sql: str, table: str) -> None:
    """纵深防御：物化语句必须严格是「INSERT INTO "python 生成的表" VALUES (...)」。

    逐字符扫描并**把字符串字面量单独隔离**，然后校验：
      * 字面量引用正确闭合（'' 双写转义）；
      * 括号平衡；
      * 括号外只允许数字/符号/逗号/括号/空白，与 TRUE|FALSE|NULL 三个独立单词。
    这样可证明语句中不可能出现分号、子查询或其它关键字 —— 即便单元格里写着
    ``'; DROP TABLE x; --`` 也只会是一段普通字符串。
    """
    prefix = f'INSERT INTO "{table}" VALUES '
    s = sql.strip()
    if not s.startswith(prefix):
        raise DuckEngineUnavailable('非法的物化语句（表名或形状不匹配）')

    body = s[len(prefix):]
    plain: List[str] = []
    depth = 0
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "'":
            i += 1
            closed = False
            while i < n:
                if body[i] == "'":
                    if i + 1 < n and body[i + 1] == "'":  # '' 转义
                        i += 2
                        continue
                    closed = True
                    i += 1
                    break
                i += 1
            if not closed:
                raise DuckEngineUnavailable('物化语句中的字符串未闭合')
            continue
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth < 0:
                raise DuckEngineUnavailable('物化语句括号不匹配')
        plain.append(ch)
        i += 1

    if depth != 0:
        raise DuckEngineUnavailable('物化语句括号不匹配')
    if not _INGEST_PLAIN_SAFE_RE.match(_INGEST_WORDS_RE.sub('', ''.join(plain))):
        raise DuckEngineUnavailable('物化语句包含非法字符或关键字，拒绝执行')


def _render_ingest_statements(
    table: str,
    phys_types: Sequence[str],
    rows: Sequence[Tuple[Any, ...]],
) -> List[str]:
    """把数据行渲染成若干条 INSERT ... VALUES 语句（按批切分）。"""
    statements: List[str] = []
    for start in range(0, len(rows), INGEST_BATCH_ROWS):
        chunk = rows[start:start + INGEST_BATCH_ROWS]
        tuples = []
        for row in chunk:
            cells = [_sql_literal(v, phys_types[i]) for i, v in enumerate(row)]
            tuples.append('(' + ', '.join(cells) + ')')
        stmt = f'INSERT INTO "{table}" VALUES {", ".join(tuples)}'
        _assert_ingest_sql(stmt, table)
        statements.append(stmt)
    return statements


# ----------------------------------------------------------------------------
# 表物化与缓存
# ----------------------------------------------------------------------------
class _DocDatabase:
    """一个 document 对应的 DuckDB 内存库（含该文档所有 Sheet 的表）。"""

    def __init__(self, document_id: str):
        if not DUCKDB_AVAILABLE:
            raise DuckEngineUnavailable('未安装 duckdb')
        self.document_id = document_id
        try:
            self.con = duckdb.connect(':memory:', config={'enable_external_access': 'false'})
        except Exception:
            # 极老版本可能不认该 config：退回默认连接（SQL 构造本身已足够安全）
            self.con = duckdb.connect(':memory:')
            try:
                self.con.execute("SET enable_external_access = false")
            except Exception:
                logger.warning('无法关闭 DuckDB 外部访问能力（版本不支持）')
        self.lock = threading.Lock()
        self.tables: Dict[int, Dict[str, Any]] = {}

    def ensure_table(self, rep: WorkbookRepresentation, sheet_index: int) -> Dict[str, Any]:
        sheet = rep.sheets[sheet_index]
        fingerprint = (sheet.row_count, sheet.column_count, len(sheet.columns),
                       rep.schema_version, sheet.sheet_name)
        cached = self.tables.get(sheet_index)
        if cached and cached['fingerprint'] == fingerprint:
            return cached

        name = table_name(self.document_id, sheet_index)
        phys_types = []
        for col in sheet.columns:
            values = [row[col.index] for row in sheet.rows
                      if col.index < len(row) and row[col.index] is not None]
            phys_types.append(_infer_physical_type(values))

        ddl_cols = ', '.join(
            f'"{phys_col(i)}" {t}' for i, t in enumerate(phys_types)
        )
        ddl = f'CREATE OR REPLACE TABLE "{name}" ({ddl_cols}, "{PHYS_ROW}" BIGINT)'

        payload_rows: List[Tuple[Any, ...]] = []
        width = len(sheet.columns)
        for i, row in enumerate(sheet.rows):
            values = []
            for ci in range(width):
                raw = row[ci] if ci < len(row) else None
                values.append(_coerce_value(raw, phys_types[ci]))
            excel_row = sheet.row_excel_numbers[i] if i < len(sheet.row_excel_numbers) else i + 1
            values.append(int(excel_row))
            payload_rows.append(tuple(values))

        # 末尾多一列 __excel_row__（BIGINT）用于来源行号
        statements = _render_ingest_statements(name, list(phys_types) + ['BIGINT'], payload_rows)

        with self.lock:
            self.con.execute(ddl)
            for stmt in statements:
                self.con.execute(stmt)
            # 物化完整性校验：行数必须与 representation 完全一致（绝不静默丢行）
            actual = int(self.con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            if actual != len(payload_rows):
                raise DuckEngineUnavailable(
                    f'DuckDB 表物化行数不一致：{actual} != {len(payload_rows)}'
                )

        info = {
            'fingerprint': fingerprint,
            'table': name,
            'phys_types': phys_types,
            'row_count': len(payload_rows),
            'columns': sheet.column_names,
        }
        self.tables[sheet_index] = info
        logger.info('DuckDB 表就绪：%s（%d 行 × %d 列）', name, len(payload_rows), width)
        return info

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:
            pass


class DuckTableRegistry:
    """document_id -> _DocDatabase 的有界缓存（LRU + 线程安全）。"""

    def __init__(self, max_documents: int = MAX_CACHED_DOCUMENTS):
        self._max = max_documents
        self._lock = threading.Lock()
        self._dbs: 'OrderedDict[str, _DocDatabase]' = OrderedDict()

    def get(self, rep: WorkbookRepresentation, sheet_index: int) -> Dict[str, Any]:
        """确保该文档/Sheet 的表已物化，返回表信息。"""
        db = self._db_for(rep.document_id)
        return db.ensure_table(rep, sheet_index)

    def acquire(self, rep: WorkbookRepresentation, sheet_index: int) -> Tuple[_DocDatabase, Dict[str, Any]]:
        """确保表已物化，并返回 (数据库句柄, 表信息) 供执行阶段复用连接。"""
        db = self._db_for(rep.document_id)
        return db, db.ensure_table(rep, sheet_index)

    def _db_for(self, document_id: str) -> _DocDatabase:
        key = f'{document_id}'
        with self._lock:
            db = self._dbs.get(key)
            if db is None:
                db = _DocDatabase(document_id)
                self._dbs[key] = db
                while len(self._dbs) > self._max:
                    _, old = self._dbs.popitem(last=False)
                    old.close()
            else:
                self._dbs.move_to_end(key)
        return db

    def clear(self) -> None:
        with self._lock:
            for db in self._dbs.values():
                db.close()
            self._dbs.clear()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {'documents': len(self._dbs),
                    'sheets': sum(len(db.tables) for db in self._dbs.values())}


_registry: Optional[DuckTableRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> DuckTableRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = DuckTableRegistry()
    return _registry


def reset_registry() -> None:
    """测试用：重置全局注册表。"""
    global _registry
    with _registry_lock:
        if _registry is not None:
            _registry.clear()
        _registry = DuckTableRegistry()


# ----------------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------------
def execute_plan(rep: WorkbookRepresentation, sheet_index: int, plan: QueryPlan) -> Tuple[List[List[Any]], List[int], int]:
    """执行计划，返回 (投影行, Excel 行号, 命中总数)。"""
    db, _info = get_registry().acquire(rep, sheet_index)
    sql = render_sql(plan)
    with db.lock:
        total = db.con.execute(sql['count_sql'], sql['count_params']).fetchone()[0]
        rows = db.con.execute(sql['page_sql'], sql['page_params']).fetchall()

    # Phase 4B：计算列排在投影列之后、Excel 行号列之前
    n_sel = len(plan.select_phys) + (1 if plan.calc_expr else 0)
    projected = [list(r[:n_sel]) for r in rows]
    excel_rows = [int(r[n_sel]) for r in rows]
    return projected, excel_rows, int(total)


def query_duckdb_representation(
    representation: WorkbookRepresentation,
    request: 'excel_query.StructuredQueryRequest',
) -> 'excel_query.StructuredQueryResult':
    """DuckDB 引擎入口：与 Phase 1B execute_query 返回完全相同的结构。"""
    if not DUCKDB_AVAILABLE:
        raise DuckEngineUnavailable('未安装 duckdb，无法使用 DuckDB 引擎')

    sheet = excel_query.resolve_sheet(representation, request.sheet_index, request.sheet_name)
    _db, info = get_registry().acquire(representation, sheet.sheet_index)
    plan = build_plan(info['table'], sheet, request)

    projected, excel_rows, total_matches = execute_plan(representation, sheet.sheet_index, plan)

    selected = [sheet.columns[i] for i in plan.select_indexes]
    span_start_letter = min((c.excel_column_letter for c in selected), key=_col_letter_num)
    span_end_letter = max((c.excel_column_letter for c in selected), key=_col_letter_num)
    row_ranges = [f'{span_start_letter}{n}:{span_end_letter}{n}' for n in excel_rows]

    returned_count = len(projected)
    has_more = (request.offset + returned_count) < total_matches

    # Phase 4B：计算字段的列元信息 + 语义信息（前端只展示）
    calc = (calculation.normalize_calculation(request.calculation)
            if request.calculation is not None else None)
    if calc is not None:
        calc = calculation.resolve_calculation(sheet, calc)
    columns_meta = [c.to_dict() for c in selected]
    if calc is not None:
        columns_meta.append(calculation.calc_column_meta(calc))
    applied = [f.to_dict() for f in plan.filters]
    if plan.calc_filter is not None and calc is not None:
        applied.append({
            'column': calculation.CALC_FILTER_COLUMN,
            'resolved_column': calc.label(),
            'column_index': -1,
            'column_letter': '',
            'operator': plan.calc_filter[0],
            'value': plan.calc_filter[1],
            'is_calculated': True,
        })

    return excel_query.StructuredQueryResult(
        document_id=representation.document_id,
        sheet_index=sheet.sheet_index,
        sheet_name=sheet.sheet_name,
        columns=columns_meta,
        rows=projected,
        row_excel_numbers=excel_rows,
        row_ranges=row_ranges,
        total_rows_in_sheet=sheet.row_count,
        total_matches=total_matches,
        returned_count=returned_count,
        limit=request.limit,
        offset=request.offset,
        has_more=has_more,
        next_offset=(request.offset + returned_count) if has_more else None,
        applied_filters=applied,
        calculation=calc.to_dict() if calc is not None else None,
        calc_filter=({'operator': plan.calc_filter[0], 'value': plan.calc_filter[1]}
                     if plan.calc_filter else None),
    )


def _col_letter_num(letter: str) -> int:
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch.upper()) - ord('A') + 1)
    return n
