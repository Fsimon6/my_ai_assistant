# -*- coding: utf-8 -*-
"""Phase 4A：复杂多步分析（**受控两阶段**：分组统计 → TOP-N → 再聚合）

本模块只解决一件事：

    「一个分析步骤的结果，作为下一步分析的输入。」

典型问题：
    找出一店中金额最高的 10 个 SKU，并统计它们的总销售额。

    原始订单数据
        ↓
    Step 1：GROUP BY SKU ID → SUM(Order Amount) → ORDER BY DESC → TOP 10
        ↓  （**Step 1 的 TOP-N 结果**，不是整表）
    Step 2：SUM(上述 10 个分组的聚合值)
        ↓
    最终单值

设计边界（本阶段刻意不做）
--------------------------
- 最多 **2 步**（``MAX_STEPS``），不做任意步数递归、不做 DAG、不做 Agent；
- 不实现 JOIN / 多文件 / 同比环比 / 计算字段 / 窗口函数；
- **不允许**「中间结果回连原始数据」（例如 "这 10 个 SKU 对应订单的平均订单金额"）
  —— 这属于后续阶段，本阶段明确拒绝并说明；
- Step 2 **只能**读取 Step 1 产出的聚合值列（``aggregate_value``），
  不能读原始列、不能自带筛选/分组/排序。

安全模型（与 Phase 2/3 完全一致）
--------------------------------
- LLM 只输出 **计划 JSON**，绝不生成 SQL；
- Step 1 复用 Phase 3B/3C 的 ``AggregateRequest`` + ``build_group_aggregate_sql``
  （标识符全部 Python 生成，筛选值全部 ``?`` 绑定）；
- Step 2 的 SQL 由本模块**唯一生成**：中间值作为 ``VALUES (?)`` 参数绑定，
  标识符只有 Python 常量 ``t(v)``；用户/LLM 文本永远不进入 SQL；
- 只允许 SELECT；``step type`` / ``source`` / ``operation`` / ``order_by`` /
  ``order_dir`` / ``top_n`` 全部走白名单与范围校验。
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.excel import aggregate as excel_aggregate
from backend.excel import calculation
from backend.excel import duck as duck_engine
from backend.excel import query as excel_query
from backend.excel.representation import WorkbookRepresentation

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# 常量（白名单）
# ----------------------------------------------------------------------------
#: 步骤类型白名单
STEP_GROUP_AGGREGATE = 'group_aggregate'
STEP_AGGREGATE = 'aggregate'
SUPPORTED_STEP_TYPES: Tuple[str, ...] = (STEP_GROUP_AGGREGATE, STEP_AGGREGATE)

#: 本阶段最多支持的步骤数（硬限制；超出即拒绝，绝不递归执行）
MAX_STEPS = 2

#: Step 2 唯一允许的数据来源（必须是 Step 1 的结果）
SOURCE_STEP_1 = 'step_1'
SUPPORTED_SOURCES: Tuple[str, ...] = (SOURCE_STEP_1,)

#: Step 2 唯一允许读取的列 —— Step 1 产出的「聚合值」列（固定别名，非用户输入）
INTERMEDIATE_VALUE_COLUMN = 'aggregate_value'
#: 兼容字段名
INTERMEDIATE_VALUE_ALIASES: Tuple[str, ...] = (
    INTERMEDIATE_VALUE_COLUMN, 'value', 'aggregate', 'agg_value',
)

#: 中间结果最多允许多少行参与 Step 2（防止 VALUES 参数爆炸）
MAX_INTERMEDIATE_ROWS = 2000

#: 错误码
ERR_STEPS_UNSUPPORTED = 'steps_unsupported'
ERR_STEP_INVALID = 'step_invalid'
ERR_STEP_SOURCE_INVALID = 'step_source_invalid'
ERR_STEP_COLUMN_INVALID = 'step_column_invalid'
ERR_STEP_TYPE_INVALID = 'step_type_invalid'
ERR_INTERMEDIATE_TOO_LARGE = 'intermediate_too_large'

#: 来源说明（可追溯，前端直接展示）
SOURCE_STEP1_TEXT = '原始 Excel 数据（representation，先筛选后分组）'
SOURCE_STEP2_TEXT = 'Step 1 的 TOP-N 结果（本阶段不回到原始行）'


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------
@dataclass
class AnalysisStep:
    """一个分析步骤（**纯参数**，不含 SQL 文本）。

    - ``group_aggregate``：对原始数据分组统计（Phase 3B/3C 能力，可选排序 + TOP-N）；
    - ``aggregate``：对**上一步结果**再做一次聚合（仅本阶段使用）。
    """

    type: str = STEP_GROUP_AGGREGATE
    operation: str = excel_aggregate.OPERATION_SUM
    column: Optional[str] = None            # group_aggregate：原始数值列；aggregate：中间值列别名
    group_by: List[str] = field(default_factory=list)
    order_by: Optional[str] = None
    order_dir: Optional[str] = None
    top_n: Optional[int] = None
    source: Optional[str] = None            # aggregate 步骤必填：'step_1'
    filters: List[Dict[str, Any]] = field(default_factory=list)
    # Phase 4B：受控计算字段（仅第 1 步允许；与 column 互斥）
    calculation: Optional[Dict[str, Any]] = None

    @property
    def is_group_step(self) -> bool:
        return self.type == STEP_GROUP_AGGREGATE

    @property
    def is_aggregate_step(self) -> bool:
        return self.type == STEP_AGGREGATE

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            'type': self.type,
            'operation': self.operation,
        }
        if self.group_by:
            out['group_by'] = list(self.group_by)
        if self.is_group_step:
            if self.column:
                out['column'] = self.column
            if self.calculation:
                out['calculation'] = self.calculation
            if self.order_by:
                out['order_by'] = self.order_by
                out['order_dir'] = self.order_dir
            if self.top_n is not None:
                out['top_n'] = self.top_n
            if self.filters:
                out['filters'] = list(self.filters)
        else:
            out['column'] = self.column
            out['source'] = self.source
        return out


@dataclass
class AnalysisPlan:
    """受控两阶段分析计划（Step 1 → Step 2）。"""

    steps: List[AnalysisStep] = field(default_factory=list)

    @property
    def step1(self) -> AnalysisStep:
        return self.steps[0]

    @property
    def step2(self) -> AnalysisStep:
        return self.steps[1]

    @property
    def step_count(self) -> int:
        return len(self.steps)

    def to_dict(self) -> Dict[str, Any]:
        return {'steps': [s.to_dict() for s in self.steps], 'max_steps': MAX_STEPS}


# ----------------------------------------------------------------------------
# 计划校验（严格：任何非法/不支持一律抛 ExcelQueryError，绝不猜）
# ----------------------------------------------------------------------------
def _as_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value.strip() or None
    return None


def _as_name_list(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    out: List[str] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            name = _as_text(item)
            if name and name not in out:
                out.append(name)
    return out


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip('+-').isdigit():
        return int(value.strip())
    return None


def normalize_analysis_plan(raw: Any) -> AnalysisPlan:
    """把 LLM 给出的计划 JSON 规约为 :class:`AnalysisPlan`（**不做 schema 解析**）。

    只做「结构 + 白名单 + 范围」校验；真实列名/schema 由执行阶段用 representation 校验。
    """
    if isinstance(raw, AnalysisPlan):
        return raw

    raw_steps = None
    if isinstance(raw, dict):
        raw_steps = raw.get('steps')
        if raw_steps is None and raw.get('step_1') is not None:
            raw_steps = [raw.get('step_1'), raw.get('step_2')]
    if not isinstance(raw_steps, (list, tuple)):
        raise excel_query.ExcelQueryError(ERR_STEP_INVALID, '分析计划缺少 steps 数组')

    steps: List[AnalysisStep] = []
    for idx, item in enumerate(raw_steps, 1):
        if item is None:
            continue
        if not isinstance(item, dict):
            raise excel_query.ExcelQueryError(ERR_STEP_INVALID, f'第 {idx} 步必须是对象')
        steps.append(_normalize_step(item, idx))

    if len(steps) > MAX_STEPS:
        raise excel_query.ExcelQueryError(
            ERR_STEPS_UNSUPPORTED,
            f'本阶段最多支持 {MAX_STEPS} 步分析（收到 {len(steps)} 步）。'
            f'请把问题拆成「先排行、再对前 N 名做一次汇总」这样的两步。',
            {'steps': len(steps), 'max_steps': MAX_STEPS},
        )
    if len(steps) < 2:
        raise excel_query.ExcelQueryError(
            ERR_STEP_INVALID, f'多步分析需要恰好 {MAX_STEPS} 步（收到 {len(steps)} 步）'
        )

    step1, step2 = steps[0], steps[1]
    if not step1.is_group_step:
        raise excel_query.ExcelQueryError(
            ERR_STEP_TYPE_INVALID, '第 1 步必须是分组统计（group_aggregate）'
        )
    if not step2.is_aggregate_step:
        raise excel_query.ExcelQueryError(
            ERR_STEP_TYPE_INVALID, '第 2 步必须是对上一步结果的聚合（aggregate）'
        )
    # Step 2 只能读 Step 1 的结果，且只能读聚合值列
    if step2.source not in SUPPORTED_SOURCES:
        raise excel_query.ExcelQueryError(
            ERR_STEP_SOURCE_INVALID,
            f'第 2 步的来源只能是 {SOURCE_STEP_1!r}（当前：{step2.source!r}）',
        )
    if step2.column not in INTERMEDIATE_VALUE_ALIASES:
        raise excel_query.ExcelQueryError(
            ERR_STEP_COLUMN_INVALID,
            f'第 2 步只能读取第 1 步产出的「聚合值」列（{INTERMEDIATE_VALUE_COLUMN}）',
            {'received': step2.column},
        )
    # Step 2 不允许自带分组/排序/TOP-N/筛选（防止语义被偷换成新查询）
    for attr in ('group_by', 'order_by', 'order_dir', 'top_n'):
        value = getattr(step2, attr)
        if value:
            raise excel_query.ExcelQueryError(
                ERR_STEP_INVALID, f'第 2 步不允许携带 {attr}（只做一次聚合）'
            )
    if step2.filters:
        raise excel_query.ExcelQueryError(ERR_STEP_INVALID, '第 2 步不允许携带筛选条件')

    return AnalysisPlan(steps=[step1, step2])


def _normalize_step(item: Dict[str, Any], idx: int) -> AnalysisStep:
    step_type = (_as_text(item.get('type')) or '').lower()
    if step_type not in SUPPORTED_STEP_TYPES:
        # 仅第 2 步允许省略 type（默认按 aggregate 处理）
        if idx == MAX_STEPS and not step_type:
            step_type = STEP_AGGREGATE
        else:
            raise excel_query.ExcelQueryError(
                ERR_STEP_TYPE_INVALID,
                f'第 {idx} 步的 type 非法：{step_type!r}（可选 {list(SUPPORTED_STEP_TYPES)}）',
            )

    raw_op = (_as_text(item.get('operation'))
              or _as_text(item.get('aggregate_operation'))
              or excel_aggregate.OPERATION_COUNT)
    try:
        operation = excel_aggregate.normalize_operation(raw_op)
    except excel_query.ExcelQueryError as e:
        raise excel_query.ExcelQueryError(
            ERR_STEP_INVALID, f'第 {idx} 步 operation 非法：{raw_op!r}',
            {'supported': list(excel_aggregate.SUPPORTED_OPERATIONS), 'detail': e.message},
        ) from e

    column = _as_text(item.get('column'))
    if step_type == STEP_AGGREGATE and column is None:
        # 允许用 source_column / value_column 表达
        column = _as_text(item.get('source_column') or item.get('value_column'))
    if operation == excel_aggregate.OPERATION_COUNT:
        # COUNT 不读值列：group 步 column=None；aggregate 步同样不需要值列
        column = None if step_type == STEP_GROUP_AGGREGATE else (column or INTERMEDIATE_VALUE_COLUMN)
    elif step_type == STEP_AGGREGATE and column is None:
        column = INTERMEDIATE_VALUE_COLUMN

    # Phase 4B：受控计算字段（只允许出现在第 1 步的分组统计里）
    calc = calculation.normalize_calculation(item.get('calculation'))
    if calc is not None:
        if step_type != STEP_GROUP_AGGREGATE:
            raise excel_query.ExcelQueryError(
                calculation.ERR_CALC_INVALID,
                '计算字段只能出现在第 1 步（group_aggregate）中；'
                '第 2 步只能对第 1 步的聚合值再聚合',
            )
        if column is not None:
            raise excel_query.ExcelQueryError(
                calculation.ERR_CALC_INVALID,
                f'第 {idx} 步不能同时给出目标列「{column}」和计算字段',
            )
        if operation == excel_aggregate.OPERATION_COUNT:
            raise excel_query.ExcelQueryError(
                calculation.ERR_CALC_INVALID, 'COUNT 不需要计算字段（统计的是行数）')

    return AnalysisStep(
        type=step_type,
        operation=operation,
        column=column,
        group_by=_as_name_list(item.get('group_by')),
        order_by=_as_text(item.get('order_by')),
        order_dir=(_as_text(item.get('order_dir')) or '').lower() or None,
        top_n=_normalize_step_top_n(item.get('top_n'), idx),
        source=_as_text(item.get('source')),
        filters=item.get('filters') if isinstance(item.get('filters'), list) else [],
        calculation=calc.to_dict() if calc is not None else None,
    )


def _normalize_step_top_n(value: Any, idx: int) -> Optional[int]:
    """步骤级 TOP-N：**非法值必须拒绝**，绝不静默降级为"不看前 N 个"。

    复用 Phase 3C 的 ``normalize_top_n``（1~200 白名单），把错误码统一成步骤级错误。
    """
    if value is None or (isinstance(value, str) and value.strip() == ''):
        return None
    try:
        return excel_aggregate.normalize_top_n(value)
    except excel_query.ExcelQueryError as e:
        raise excel_query.ExcelQueryError(
            ERR_STEP_INVALID, f'第 {idx} 步 top_n 非法：{value!r}', {'detail': e.message},
        ) from e


def looks_like_analysis_plan(raw: Any) -> bool:
    """宽松判断 LLM 输出是否像多步计划（供 NL 层决定走哪条链路）。"""
    try:
        normalize_analysis_plan(raw)
        return True
    except excel_query.ExcelQueryError:
        return False


# ----------------------------------------------------------------------------
# Step 2：SQL 生成（本模块唯一生成 SQL 的地方）
# ----------------------------------------------------------------------------
def build_step2_sql(operation: str, value_count: int) -> Dict[str, Any]:
    """生成「对中间结果再聚合」的 SQL（标识符固定；中间值全部 ``?`` 绑定）。

        SELECT COUNT(*) AS intermediate_rows, COUNT(v) AS numeric_rows,
               SUM(v) AS agg_sum, MIN(v) AS agg_min, MAX(v) AS agg_max, AVG(v) AS agg_avg
        FROM (VALUES (?), (?), ...) AS t(v)

    - 表别名 ``t(v)``/列名 ``v`` 均为 Python 常量，用户输入不可能进入；
    - 中间值来自 Step 1 的 DuckDB 结果（数值），仍以参数绑定传递；
    - 一次性取出 5 种聚合值，便于不同 Step 2 operation 复用同一条 SQL。
    """
    if operation not in excel_aggregate.SUPPORTED_OPERATIONS:
        raise excel_query.ExcelQueryError(ERR_STEP_INVALID, f'operation 非法：{operation!r}')
    if not isinstance(value_count, int) or value_count < 1:
        raise excel_query.ExcelQueryError(ERR_STEP_INVALID, '中间结果为空，无法执行第 2 步')
    if value_count > MAX_INTERMEDIATE_ROWS:
        raise excel_query.ExcelQueryError(
            ERR_INTERMEDIATE_TOO_LARGE,
            f'中间结果有 {value_count} 行，超过本阶段上限 {MAX_INTERMEDIATE_ROWS}，'
            f'请先用「前 N 个」缩小范围。',
            {'rows': value_count, 'max_rows': MAX_INTERMEDIATE_ROWS},
        )

    tuples = ', '.join(['(?)'] * value_count)
    sql = (
        'SELECT COUNT(*) AS intermediate_rows, COUNT(v) AS numeric_rows, '
        'SUM(v) AS agg_sum, MIN(v) AS agg_min, MAX(v) AS agg_max, AVG(v) AS agg_avg '
        f'FROM (VALUES {tuples}) AS t(v)'
    )
    duck_engine.assert_safe_sql(sql)
    return {'sql': sql, 'params': [None] * value_count, 'value_count': value_count}


def execute_step2_python(operation: str, values: Sequence[Optional[float]]) -> Dict[str, Any]:
    """Python 参考实现（DuckDB 不可用时的降级路径；语义与 SQL 完全一致）。"""
    nums = [float(v) for v in values if v is not None]
    if operation == excel_aggregate.OPERATION_COUNT:
        value: Optional[float] = float(len(values))
    elif not nums:
        value = None
    elif operation == excel_aggregate.OPERATION_SUM:
        value = float(sum(nums))
    elif operation == excel_aggregate.OPERATION_MIN:
        value = float(min(nums))
    elif operation == excel_aggregate.OPERATION_MAX:
        value = float(max(nums))
    else:
        value = float(sum(nums) / len(nums))
    return {
        'intermediate_rows': len(values),
        'numeric_rows': len(nums),
        'value': value,
    }


# ----------------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------------
@dataclass
class AnalysisResult:
    """两阶段分析结果（保留中间结果，保证最终数字可追溯）。"""

    document_id: str
    filename: str
    sheet_index: int
    sheet_name: str
    plan: AnalysisPlan
    step1: Any                      # excel_aggregate.GroupedAggregateResult
    step2_operation: str
    step2_value: Optional[float]
    step2_matched: int              # 参与第 2 步的中间结果行数（= TOP-N 的返回组数）
    step2_numeric_rows: int
    engine: str
    step1_source: str = SOURCE_STEP1_TEXT
    step2_source: str = SOURCE_STEP2_TEXT

    @property
    def step2_is_count(self) -> bool:
        return self.step2_operation == excel_aggregate.OPERATION_COUNT

    @property
    def step2_value_display(self) -> str:
        return excel_aggregate.format_number(self.step2_value, is_count=self.step2_is_count)

    @property
    def intermediate_values(self) -> List[Optional[float]]:
        return [r.value for r in self.step1.rows]

    def definition(self) -> str:
        s1, s2 = self.plan.step1, self.plan.step2
        group_names = '、'.join(g['name'] for g in self.step1.group_by)
        op1 = excel_aggregate.OPERATION_LABELS.get(s1.operation, s1.operation)
        op2 = excel_aggregate.OPERATION_LABELS.get(s2.operation, s2.operation)
        calc_label = (self.step1.calculation or {}).get('label') if self.step1.calculation else None
        first = (
            f'第 1 步：按「{group_names}」分组，对每个分组 {op1}'
            + (f'（计算字段 {calc_label}，先逐行计算再聚合）' if calc_label
               else (f'（目标列 {s1.column}）' if s1.column else ''))
            + ('（先筛选后分组）' if self.step1.filters else '')
        )
        if self.step1.is_sorted:
            first += f'，按「{self.step1.order_by_label()}」{excel_aggregate.ORDER_DIR_PHRASES.get(self.step1.order_dir, "")}排列'
        if self.step1.top_n is not None:
            first += f'，截取前 {self.step1.top_n} 个分组'
        second = (
            f'第 2 步：对上述 {self.step2_matched} 个分组的**聚合值**再执行 {op2}'
            f'（数据来源：Step 1 结果，不回原始行）'
        )
        return f'{first}；{second}'

    def to_dict(self) -> Dict[str, Any]:
        s1, s2 = self.plan.step1, self.plan.step2
        return {
            'kind': 'multi_step',
            'document_id': self.document_id,
            'filename': self.filename,
            'sheet_index': self.sheet_index,
            'sheet_name': self.sheet_name,
            'engine': self.engine,
            'max_steps': MAX_STEPS,
            'step_count': self.plan.step_count,
            'plan': self.plan.to_dict(),
            'step1': {
                'type': STEP_GROUP_AGGREGATE,
                'group_by': self.step1.group_by,
                'operation': self.step1.operation,
                'operation_label': excel_aggregate.OPERATION_LABELS.get(
                    self.step1.operation, self.step1.operation),
                'column': self.step1.column,
                # Phase 4B：受控计算字段（无则为 None）
                'calculation': self.step1.calculation,
                'filters': self.step1.filters,
                'applied_filters': self.step1.filters,
                'sorted': self.step1.is_sorted,
                'order_by': self.step1.order_by,
                'order_by_label': self.step1.order_by_label(),
                'order_dir': self.step1.order_dir if self.step1.is_sorted else None,
                'top_n': self.step1.top_n,
                'total_groups': self.step1.total_groups,
                'returned_groups': self.step1.returned_groups,
                'truncated_by_top_n': self.step1.is_truncated,
                'matched_rows': self.step1.matched_rows,
                'total_rows_in_sheet': self.step1.total_rows_in_sheet,
                'rows': [r.to_dict(self.step1.operation) for r in self.step1.rows],
                'sort_description': self.step1.sort_description(),
                'source': self.step1_source,
            },
            'step2': {
                'type': STEP_AGGREGATE,
                'operation': self.step2_operation,
                'operation_label': excel_aggregate.OPERATION_LABELS.get(
                    self.step2_operation, self.step2_operation),
                'source': SOURCE_STEP_1,
                'source_text': self.step2_source,
                'input_rows': self.step2_matched,
                'numeric_rows': self.step2_numeric_rows,
                'value': self.step2_value,
                'value_display': self.step2_value_display,
            },
            'value': self.step2_value,
            'value_display': self.step2_value_display,
            'step2_input_values': self.intermediate_values,
            'definition': self.definition(),
        }


def execute_analysis(
    representation: WorkbookRepresentation,
    plan: AnalysisPlan,
    *,
    sheet_index: int = 0,
    sheet_name: Optional[str] = None,
    engine: Optional[str] = None,
) -> Tuple[AnalysisResult, str]:
    """执行受控两阶段分析，返回 (结果, 实际引擎名)。

    Step 1 复用 Phase 3B/3C 的统计引擎（含 ORDER BY / TOP-N）；
    Step 2 只接收 **Step 1 已截断后的结果行**，用参数绑定的 VALUES 再聚合。
    """
    from backend.excel import engine as excel_engine

    s1, s2 = plan.step1, plan.step2

    # ---- Step 1：分组统计（Phase 3B/3C，含排序与 TOP-N） ----
    payload: Dict[str, Any] = {
        'operation': s1.operation,
        'sheet_index': int(sheet_index or 0),
        'filters': s1.filters or [],
    }
    if sheet_name:
        payload['sheet_name'] = sheet_name
    if s1.column:
        payload['column'] = s1.column
    if s1.calculation:
        # Phase 4B：第 1 步使用受控计算字段（逐行计算后再聚合）
        payload['calculation'] = s1.calculation
    if s1.group_by:
        payload['group_by'] = list(s1.group_by)
    if s1.order_by:
        payload['order_by'] = s1.order_by
        payload['order_dir'] = s1.order_dir or excel_aggregate.ORDER_DESC
    if s1.top_n is not None:
        payload['top_n'] = int(s1.top_n)

    step1_result, engine_name = excel_engine.run_aggregate(
        representation, payload, engine=engine,
    )

    # ---- Step 2：对 Step 1 的结果（已排序、已截断）再聚合 ----
    values = [r.value for r in step1_result.rows]
    step2_value: Optional[float]
    numeric_rows: int
    if s2.operation == excel_aggregate.OPERATION_COUNT:
        # COUNT 语义：中间结果的**行数**（= 参与第 2 步的分组个数）
        step2_value = float(len(values))
        numeric_rows = len([v for v in values if v is not None])
    elif not values:
        step2_value, numeric_rows = None, 0
    else:
        numeric_values = [float(v) for v in values if v is not None]
        numeric_rows = len(numeric_values)
        if not numeric_values:
            step2_value = None
        else:
            sql = build_step2_sql(s2.operation, len(numeric_values))
            step2_value = _run_step2_duckdb(
                representation, step1_result.sheet_index, sql, numeric_values, s2.operation,
            )

    result = AnalysisResult(
        document_id=representation.document_id,
        filename=representation.filename,
        sheet_index=step1_result.sheet_index,
        sheet_name=step1_result.sheet_name,
        plan=plan,
        step1=step1_result,
        step2_operation=s2.operation,
        step2_value=step2_value,
        step2_matched=len(values),
        step2_numeric_rows=numeric_rows,
        engine=engine_name,
    )
    return result, engine_name


def _run_step2_duckdb(
    representation: WorkbookRepresentation,
    sheet_index: int,
    sql: Dict[str, Any],
    numeric_values: Sequence[float],
    operation: str,
) -> Optional[float]:
    """在与 Step 1 相同的 DuckDB 连接上执行 Step 2（同一 document 的库）。"""
    if not duck_engine.DUCKDB_AVAILABLE:
        return execute_step2_python(operation, list(numeric_values))['value']
    db, _info = duck_engine.get_registry().acquire(representation, sheet_index)
    with db.lock:
        row = db.con.execute(sql['sql'], list(numeric_values)).fetchone()
    if row is None:
        return None
    intermediate_rows, _numeric, agg_sum, agg_min, agg_max, agg_avg = row
    if operation == excel_aggregate.OPERATION_SUM:
        return float(agg_sum) if agg_sum is not None else None
    if operation == excel_aggregate.OPERATION_MIN:
        return float(agg_min) if agg_min is not None else None
    if operation == excel_aggregate.OPERATION_MAX:
        return float(agg_max) if agg_max is not None else None
    if operation == excel_aggregate.OPERATION_AVG:
        return float(agg_avg) if agg_avg is not None else None
    return float(intermediate_rows or 0)  # pragma: no cover - COUNT 已在调用方处理


# ----------------------------------------------------------------------------
# 摘要（Python 生成，前端只展示）
# ----------------------------------------------------------------------------
def format_analysis_summary(result: AnalysisResult) -> str:
    """两阶段分析摘要（含两步来源追溯，全部由 Python 生成）。"""
    s1 = result.step1
    group_names = '、'.join(g['name'] for g in s1.group_by)
    op1_label = excel_aggregate.OPERATION_LABELS.get(s1.operation, s1.operation)
    op2_label = excel_aggregate.OPERATION_LABELS.get(result.step2_operation, result.step2_operation)
    is_count1 = s1.operation == excel_aggregate.OPERATION_COUNT

    lines = [
        f'已对「{result.filename}」的 Sheet「{result.sheet_name}」执行**两步分析**。',
        f'- 第 1 步：按「{group_names}」分组，操作 {op1_label}'
        + (f'，目标列 {s1.column}' if s1.column else '')
        + f'｜分组数 {s1.total_groups} 个'
        + (f' → TOP-{s1.top_n} 截取前 {s1.returned_groups} 个' if s1.is_truncated else ''),
        f'- 第 1 步排序：' + (s1.sort_description() if s1.is_sorted else '无（数据库返回顺序）'),
        f'- 第 1 步来源：{result.step1_source}',
    ]
    if s1.row_excel_span:
        lines.append(f'- 第 1 步匹配 Excel 行区间：{s1.row_excel_span["first"]} ~ {s1.row_excel_span["last"]}')
    lines.append('- 筛选条件：' + (
        '；'.join(excel_aggregate._filter_text(f) for f in s1.filters) if s1.filters else '无（全表）'))
    lines.append('- 中间结果（第 1 步返回的每个分组的聚合值，顺序即数据库返回顺序）：')
    if not s1.rows:
        lines.append('    （没有匹配的数据行，因此没有任何分组）')
    else:
        for r in s1.rows:
            key = ' / '.join(c.display for c in r.group)
            lines.append(f'    {key} → {excel_aggregate.format_number(r.value, is_count=is_count1)}')
    lines.append(f'- 第 2 步：操作 {op2_label}｜输入 {result.step2_matched} 个分组'
                 f'（其中可数值化 {result.step2_numeric_rows} 个）')
    lines.append(f'- 第 2 步来源：{result.step2_source}')
    lines.append(f'- **最终结果：{result.step2_value_display}**')
    lines.append(f'- 分析口径：{result.definition()}')
    return '\n'.join(lines)
