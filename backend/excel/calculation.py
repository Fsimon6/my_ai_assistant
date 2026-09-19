# -*- coding: utf-8 -*-
"""Phase 4B：受控计算字段（Calculation Expression）

本模块只做一件事：**把"两个真实列 + 一个基础算术运算符"变成受控的计算值**，
并让这个计算值可以参与 Phase 2 的筛选、Phase 3 的统计/分组/排序/TOP-N、
以及 Phase 4A 的两阶段分析。

严格边界（刻意不做，避免变成自由表达式引擎）
------------------------------------------------
- 只允许 **一个运算符 + 两个真实列**：``A op B``，**不支持嵌套**（``(A+B)*C`` / ``A/B+C`` 一律拒绝）；
- 运算符白名单固定为 ``add / sub / mul / div``（**没有** pow / mod / sqrt / abs / round / CASE / 任意函数）；
- **绝不接受表达式字符串**：LLM 只能给出 ``{"operation":..., "left_column":..., "right_column":...}``，
  任何形如 ``"Order Amount / Quantity"`` / ``"SUM(...)"`` / ``"a); DROP TABLE"`` 的输入都会在
  **列解析阶段**被拒绝，因为左右列必须能在真实 schema 中唯一解析；
- 不使用 ``eval`` / ``exec`` / 动态 SQL；SQL 标识符全部为 Python 生成的物理名 ``c<N>``。

数值语义（与 Phase 3A 完全一致）
--------------------------------
- 文本数字（``"1"`` / ``"10.5"``）参与计算；
- 空值 / 非数值 -> 该行计算结果为 **NULL**（**不当作 0**）；
- **除零 -> NULL**（用 ``NULLIF(分母, 0)``），既不报错也不伪造 0；
- 计算结果为 NULL 的行：不参与数值聚合，也不匹配 ``> / < / >= / <=`` 这类范围筛选
  （SQL 三值逻辑的 NULL 比较为 UNKNOWN，与 Phase 2 的 NULL 语义一致）。

层级（Level）
-------------
- ``row``      ：逐行计算 —— 用于**投影**（返回每行的计算值）与**筛选**（计算值 + 比较）；
- ``aggregate``：计算值进入**聚合**（如 ``AVG(Order Amount / Quantity)`` / ``SUM(A*B)`` 按分组统计）。
  语义固定为"**先逐行计算，再聚合**"（由 Prompt 明确规定）；若用户措辞可能指
  ``SUM(A)/SUM(B)``（如"按 SKU 统计平均单价"），本阶段**一律澄清**，绝不擅自选择口径。
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# 运算符白名单
# ----------------------------------------------------------------------------
OP_ADD = 'add'
OP_SUB = 'sub'
OP_MUL = 'mul'
OP_DIV = 'div'
SUPPORTED_CALC_OPERATIONS = (OP_ADD, OP_SUB, OP_MUL, OP_DIV)

#: 中文/符号展示（前端与摘要直接用，前端不猜）
OP_SYMBOLS: Dict[str, str] = {OP_ADD: '+', OP_SUB: '−', OP_MUL: '×', OP_DIV: '÷'}
OP_LABELS: Dict[str, str] = {
    OP_ADD: '加法', OP_SUB: '减法', OP_MUL: '乘法', OP_DIV: '除法',
}

#: Python 固定生成的计算列别名（用户/LLM 文本永不进入 SQL 标识符）
CALC_ALIAS = 'calc_0'
#: NL/LLM 引用"计算值"时使用的固定列名（不是真实列名；由 Python 识别）
CALC_FILTER_COLUMN = 'calculated_value'
CALC_FILTER_ALIASES = frozenset({
    CALC_FILTER_COLUMN, 'calc', 'calculation', 'calculated', '计算值', '计算字段', '计算结果',
})

#: 唯一允许的运算符写法（严格白名单 + 少量等价别名）
_OP_ALIASES: Dict[str, str] = {
    'add': OP_ADD, '+': OP_ADD, 'plus': OP_ADD, '加': OP_ADD, '加上': OP_ADD, '相加': OP_ADD,
    'sub': OP_SUB, '-': OP_SUB, 'minus': OP_SUB, '减': OP_SUB, '减去': OP_SUB, '相减': OP_SUB,
    'mul': OP_MUL, '*': OP_MUL, 'times': OP_MUL, 'multiply': OP_MUL, '乘': OP_MUL, '乘以': OP_MUL, '相乘': OP_MUL,
    'div': OP_DIV, '/': OP_DIV, 'divide': OP_DIV, '除以': OP_DIV, '相除': OP_DIV, '除': OP_DIV,
}

#: 错误码
ERR_CALC_INVALID = 'calculation_invalid'
ERR_CALC_COLUMN_INVALID = 'calculation_column_invalid'
ERR_CALC_FILTER_INVALID = 'calculation_filter_invalid'

#: 允许对**计算值**使用的比较运算符（不含 contains/eq 文本语义）
SUPPORTED_CALC_FILTER_OPERATORS = ('gt', 'gte', 'lt', 'lte', 'eq', 'neq')

#: 列名长度上限（防止超长垃圾串进入解析路径）
MAX_COLUMN_NAME_LEN = 120


def _err(code: str, message: str, details: Optional[Dict[str, Any]] = None):
    """延迟导入 query.py 的异常类型，避免模块循环导入。"""
    from backend.excel import query as excel_query

    return excel_query.ExcelQueryError(code, message, details)


def _resolve_column(sheet, name: str):
    from backend.excel import query as excel_query

    return excel_query.resolve_column(sheet, name)


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------
@dataclass
class Calculation:
    """受控计算字段（**一个运算符 + 两个真实列**，不可嵌套）。"""

    operation: str
    left_column: str
    right_column: str
    # 解析后填充（真实 schema）
    left_index: Optional[int] = None
    right_index: Optional[int] = None
    left_letter: str = ''
    right_letter: str = ''
    alias: str = CALC_ALIAS

    @property
    def is_div(self) -> bool:
        return self.operation == OP_DIV

    @property
    def symbol(self) -> str:
        return OP_SYMBOLS.get(self.operation, self.operation)

    @property
    def operation_label(self) -> str:
        return OP_LABELS.get(self.operation, self.operation)

    @property
    def is_resolved(self) -> bool:
        return self.left_index is not None and self.right_index is not None

    def label(self) -> str:
        """人类可读的计算字段（如 ``Order Amount ÷ Quantity``）。"""
        return f'{self.left_column} {self.symbol} {self.right_column}'

    def to_dict(self) -> Dict[str, Any]:
        return {
            'kind': 'calculation',
            'operation': self.operation,
            'operation_label': self.operation_label,
            'symbol': self.symbol,
            'left_column': self.left_column,
            'left_index': self.left_index,
            'left_letter': self.left_letter,
            'right_column': self.right_column,
            'right_index': self.right_index,
            'right_letter': self.right_letter,
            'alias': self.alias,
            'label': self.label(),
            'is_calculated': True,
        }


# ----------------------------------------------------------------------------
# 解析与校验
# ----------------------------------------------------------------------------
def normalize_calculation(raw: Any) -> Optional[Calculation]:
    """把 LLM 给出的计算草图规约为 :class:`Calculation`（**只做形状 + 白名单校验**）。

    真实列名的解析（schema validation）在 :func:`resolve_calculation` 中完成。
    任何"表达式字符串"都会在这里被拒绝：左右列必须是**单个列名**，不能包含运算符/分号/括号函数等。
    """
    if raw is None:
        return None
    if isinstance(raw, Calculation):
        return raw
    if not isinstance(raw, dict):
        raise _err(ERR_CALC_INVALID, '计算字段必须是对象 {"operation","left_column","right_column"}')

    raw_op = raw.get('operation') or raw.get('op')
    if not isinstance(raw_op, str):
        raise _err(ERR_CALC_INVALID, f'计算字段的 operation 必须是字符串，收到 {raw_op!r}')
    operation = _OP_ALIASES.get(raw_op.strip().lower())
    if operation is None:
        raise _err(
            ERR_CALC_INVALID,
            f'不支持的计算 operation={raw_op!r}，本阶段仅支持 {list(SUPPORTED_CALC_OPERATIONS)}'
            f'（对应 + - * /）',
            {'supported': list(SUPPORTED_CALC_OPERATIONS)},
        )

    left = raw.get('left_column')
    right = raw.get('right_column')
    for name, value in (('left_column', left), ('right_column', right)):
        if not isinstance(value, str) or value.strip() == '':
            raise _err(ERR_CALC_INVALID, f'计算字段的 {name} 必须是非空列名，收到 {value!r}')
        if len(value) > MAX_COLUMN_NAME_LEN:
            raise _err(ERR_CALC_INVALID, f'计算字段的 {name} 过长（>{MAX_COLUMN_NAME_LEN} 字符）')

    return Calculation(operation=operation, left_column=left.strip(), right_column=right.strip())


def resolve_calculation(sheet, calc: Calculation) -> Calculation:
    """按**真实 schema** 解析左右列（唯一解析；不唯一/不存在一律拒绝）。

    这是"防自由表达式"的关键一步：``"(SELECT ...)"`` / ``"a); DROP TABLE t"`` 这类字符串
    根本不可能匹配到任何真实列，因此在这里被拒绝；而一旦解析成功，进入 SQL 的
    永远是 Python 生成的物理列名 ``c<N>``，用户文本不可能成为 SQL 标识符。
    """
    left = _resolve_column(sheet, calc.left_column)
    right = _resolve_column(sheet, calc.right_column)
    calc.left_column = left.name
    calc.left_index = left.index
    calc.left_letter = left.excel_column_letter
    calc.right_column = right.name
    calc.right_index = right.index
    calc.right_letter = right.excel_column_letter
    return calc


def ensure_resolved(sheet, raw: Any) -> Optional[Calculation]:
    """从 dict / Calculation 得到**已解析**的计算字段（幂等）。

    安全要点：**永远以真实 schema 为准重新解析**，绝不信任调用方传来的
    ``left_index`` / ``right_index``（避免被伪造的索引绕过列解析）。
    """
    calc = normalize_calculation(raw)
    if calc is None:
        return None
    return resolve_calculation(sheet, calc)


def normalize_calc_filter(raw: Any) -> Optional[Dict[str, Any]]:
    """把「对计算值筛选」的条件规约为 ``{operator, value}``（范围/等值，不含 contains）。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _err(ERR_CALC_FILTER_INVALID, '对计算字段的筛选必须是对象 {"operator","value"}')
    raw_op = raw.get('operator') or 'gt'
    if not isinstance(raw_op, str):
        raise _err(ERR_CALC_FILTER_INVALID, f'计算字段筛选的 operator 必须是字符串，收到 {raw_op!r}')
    operator = raw_op.strip().lower()
    if operator not in SUPPORTED_CALC_FILTER_OPERATORS:
        raise _err(
            ERR_CALC_FILTER_INVALID,
            f'对计算字段不支持 operator={raw_op!r}，仅支持 {list(SUPPORTED_CALC_FILTER_OPERATORS)}'
            f'（数值比较，不支持 contains/文本匹配）',
            {'supported': list(SUPPORTED_CALC_FILTER_OPERATORS)},
        )
    value = raw.get('value')
    from backend.excel import query as excel_query

    num = excel_query.to_number(value)
    if num is None:
        raise _err(
            ERR_CALC_FILTER_INVALID,
            f'对计算字段的 {operator} 需要一个数值，收到 {value!r}',
            {'operator': operator, 'value': value},
        )
    return {'operator': operator, 'value': num}


def is_calc_filter_column(name: Any) -> bool:
    """判断筛选条件里的列名是否指向"计算值"（固定别名，不是真实列名）。"""
    if not isinstance(name, str):
        return False
    return name.strip().lower() in CALC_FILTER_ALIASES


# ----------------------------------------------------------------------------
# SQL 渲染（本模块唯一生成计算 SQL 的地方）
# ----------------------------------------------------------------------------
#: 允许出现的计算表达式形态（纵深防御：即使上游被改坏也不会拼出危险 SQL）
_SAFE_CALC_EXPR_RE = re.compile(
    r'^(?:TRY_CAST\("c\d+" AS DOUBLE\)|NULLIF\(TRY_CAST\("c\d+" AS DOUBLE\), 0\)|[-+*/() ,])+$'
)


def _cast_phys(index: int) -> str:
    from backend.excel import duck as duck_engine

    phys = duck_engine.phys_col(index)
    if not duck_engine._SAFE_IDENT.match(phys):  # noqa: SLF001 - 复用同一套标识符白名单
        raise _err(ERR_CALC_COLUMN_INVALID, f'非法物理列名：{phys!r}')
    return f'TRY_CAST("{phys}" AS DOUBLE)'


def calc_sql_expr(calc: Calculation) -> str:
    """生成计算值 SQL 表达式（**只用物理列名 + 白名单运算符**）。

    - 除法的分母用 ``NULLIF(..., 0)``：``A / 0 -> NULL``（不报错、不伪造 0）；
    - 空值/非数值经 ``TRY_CAST`` 得到 NULL，整个表达式的结果也是 NULL；
    - 生成后再用形态白名单正则做一次纵深校验。
    """
    if calc.left_index is None or calc.right_index is None:
        raise _err(ERR_CALC_COLUMN_INVALID, '计算字段尚未解析到真实列')
    left = _cast_phys(calc.left_index)
    right = _cast_phys(calc.right_index)
    if calc.operation == OP_ADD:
        expr = f'{left} + {right}'
    elif calc.operation == OP_SUB:
        expr = f'{left} - {right}'
    elif calc.operation == OP_MUL:
        expr = f'{left} * {right}'
    elif calc.operation == OP_DIV:
        expr = f'{left} / NULLIF({right}, 0)'
    else:  # pragma: no cover - normalize 已拦截
        raise _err(ERR_CALC_INVALID, f'不支持的计算 operation：{calc.operation!r}')
    assert_calc_expr_safe(expr)
    return expr


def assert_calc_expr_safe(expr: str) -> None:
    """计算表达式形态白名单（只允许 TRY_CAST 物理列、NULLIF(..,0) 与四个运算符）。"""
    if not isinstance(expr, str) or not _SAFE_CALC_EXPR_RE.match(expr):
        raise _err(ERR_CALC_INVALID, f'计算表达式形态非法：{expr!r}')


#: 计算字段比较运算符 -> SQL（固定白名单）
CALC_FILTER_SQL_OPERATORS: Dict[str, str] = {
    'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<=', 'eq': '=', 'neq': '<>',
}


def calc_filter_sql_expr(expr: str, operator: str) -> str:
    """用**已渲染的计算表达式**生成"计算值 + 比较"的 WHERE 片段（值由调用方 ``?`` 绑定）。"""
    op_sql = CALC_FILTER_SQL_OPERATORS.get(operator)
    if op_sql is None:
        raise _err(ERR_CALC_FILTER_INVALID, f'不支持的计算字段比较运算符：{operator!r}')
    assert_calc_expr_safe(expr)
    return f'({expr} {op_sql} ?)'


def calc_filter_sql(calc: Calculation, operator: str) -> str:
    """生成"计算值 + 比较"的 WHERE 片段（值由调用方 ``?`` 绑定）。"""
    return calc_filter_sql_expr(calc_sql_expr(calc), operator)


# ----------------------------------------------------------------------------
# Python 参考实现（与 SQL 语义逐值一致）
# ----------------------------------------------------------------------------
def calc_python_value(calc: Calculation, row: Sequence[Any]) -> Optional[float]:
    """逐行计算（Python 参考实现）。

    与 SQL 路径对齐：
    - 任一侧为空 / 非数值 -> None（TRY_CAST 失败 => NULL）；
    - 除法分母为 0 -> None（NULLIF）；
    - 其余按四则运算返回 float。
    """
    from backend.excel import aggregate as excel_aggregate

    if calc.left_index is None or calc.right_index is None:
        return None
    li, ri = calc.left_index, calc.right_index
    left = excel_aggregate.aggregate_to_number(row[li] if li < len(row) else None)
    right = excel_aggregate.aggregate_to_number(row[ri] if ri < len(row) else None)
    if left is None or right is None:
        return None
    if calc.operation == OP_ADD:
        return float(left + right)
    if calc.operation == OP_SUB:
        return float(left - right)
    if calc.operation == OP_MUL:
        return float(left * right)
    if right == 0:
        return None
    return float(left / right)


# ----------------------------------------------------------------------------
# 展示与口径（Python 生成，前端不猜）
# ----------------------------------------------------------------------------
def definition_text(calc: Calculation, operation_label: str = '') -> str:
    """计算字段的口径说明（不含"估算"等模糊措辞）。"""
    prefix = f'对每行的「{calc.left_column}」与「{calc.right_column}」做数值化后{calc.operation_label}'
    if operation_label:
        prefix = f'{prefix}，再对结果{operation_label}'
    tail = '；空值与非数值不参与运算（结果为空）；'
    if calc.is_div:
        tail += f'「{calc.right_column}」为 0 时结果为空（不当作 0）；'
    tail += '计算字段由程序生成，不是模型估算'
    return prefix + tail


def calc_column_meta(calc: Calculation) -> Dict[str, Any]:
    """行级计算列的列元信息（供 Phase 2 投影结果复用同一套列结构）。"""
    return {
        'name': f'{calc.label()}（计算值）',
        'index': -1,
        'excel_column': -1,
        'excel_column_letter': '',
        'dtype': 'number',
        'is_calculated': True,
        'calculation': calc.to_dict(),
    }


def needs_clarify(message: str, calc: Optional[Calculation], level: str = 'row') -> Optional[str]:
    """判断当前计算语义是否存在**业务口径歧义**（有则返回澄清文案）。

    本阶段固定的业务语义：聚合时对**每行**先算再聚合（``AVG(A/B)``、``SUM(A*B)``…）。
    但用户在说"平均单价 / 均价 / 单价最高"时，往往指的是 ``SUM(A)/SUM(B)``
    （金融口径的"均价"），两者在数学上一般不等 —— 因此本阶段**不擅自选择**：
    只有用户明确说了"每笔 / 每行 / 每一单 / 逐笔"这类**逐行**措辞才执行，否则澄清。
    """
    if calc is None or level != 'aggregate' or not calc.is_div:
        return None
    if _PER_ROW_CUE_RE.search(message or ''):
        return None
    return (
        f'「{calc.label()}」按分组统计时有两种口径，结果一般不同：\n'
        f'  ① 逐行计算再平均：AVG({calc.left_column} ÷ {calc.right_column})\n'
        f'  ② 分别求和后再相除：SUM({calc.left_column}) ÷ SUM({calc.right_column})\n'
        f'本阶段固定按 ① 执行，需要你明确确认。请改说「按每笔订单的单价求平均」'
        f'（走 ①）或改用「{calc.left_column} 总和 ÷ {calc.right_column} 总和」这样的两次求和。'
    )


#: 明确的"逐行"措辞（出现即认为口径 ① 无歧义）
_PER_ROW_CUE_RE = re.compile(r'每笔|每行|每一单|每一条|逐笔|逐行|每个订单|每单|每次|单笔|每一条记录|每笔订单')
