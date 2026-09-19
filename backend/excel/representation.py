# -*- coding: utf-8 -*-
"""Unified Table Representation（Phase 1A）

设计目标（为后续 Phase 1B/1C 与 Structured Table Access 预留清晰入口）：

WorkbookRepresentation
 ├─ document_id / user_id / filename / file_type / parser
 └─ sheets: List[SheetRepresentation]
      ├─ sheet_name / sheet_index
      ├─ header_mode / header_rows_excel / header_depth
      ├─ columns: List[ColumnMeta]        (列名 / 类型 / 来源列号)
      ├─ rows: List[List[Any]]            (数据行，JSON 安全基元)
      ├─ row_excel_numbers: List[int]     (每行对应的 Excel 原始行号，1-based)
      ├─ preamble_rows                    (表头之前的描述行，完整保留不丢弃)
      ├─ formulas                         (公式单元格 "R{r}C{c}" -> 公式文本)
      ├─ merged_ranges / excel_range       (来源定位：row_start/row_end/column_start/column_end)
      └─ row_count / column_count / warnings

约定：
- 所有值均为 JSON 可序列化基元（None / bool / int / float / str），
  日期统一转为 ISO 字符串，超长数字统一字符串化（见 parser）。
- 行列坐标一律使用 Excel 原生 1-based 语义，便于后续做 Range 来源引用。
- 本文件不依赖任何第三方库，便于被 API / 服务层安全 import。
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

SCHEMA_VERSION = '1.0'

# 表头模式
HEADER_SINGLE = 'single'   # 单层表头
HEADER_MULTI = 'multi'     # 多层（双层）表头
HEADER_NONE = 'none'       # 未能识别表头（全表按数据行处理，列名回退为 Excel 列标）

# ---------------------------------------------------------------------------
# 业务语义类型（稳定性补丁：最小的语义判定层，不做 schema 重构）
# ---------------------------------------------------------------------------
# 目的：把"看起来像数字、但业务上是主键"的列（Order ID / SKU ID / 追踪号…）
# 与真正的数值列区分开，避免：
#   * SUM/AVG/MIN/MAX(业务 ID) 这种无意义且会丢精度的操作；
#   * 18 位数字串被当作 DOUBLE 比较/排序。
SEMANTIC_IDENTIFIER = 'identifier'
SEMANTIC_NUMBER = 'number'
SEMANTIC_TEXT = 'text'
SEMANTIC_DATE = 'date'
SEMANTIC_BOOLEAN = 'boolean'
SEMANTIC_EMPTY = 'empty'
SEMANTIC_MIXED = 'mixed'

#: 列名里像业务主键的关键词（**只作为辅助条件**，必须同时满足"值几乎全是数字串"）
_ID_NAME_RE = re.compile(
    r'(^|[^a-z0-9])id([^a-z0-9]|$)|^id[ _-]|[_ -]id$|编号|单号|订单号|流水号|货号|条码'
    r'|barcode|ean|upc|sku|serial|tracking|package[ _]?no',
    re.IGNORECASE,
)
#: 值里"纯数字串"的占比阈值（判断是否是编号型列）
_ID_PURE_DIGIT_RATIO = 0.6
#: 纯数字串长度达到该值时，即使列名不像 ID 也判为业务标识（超出常见金额范围）
_ID_LONG_DIGITS = 12
#: 列名像 ID 时，数字串长度门槛（避免把 "1/2/3" 这种短数字列误判）
_ID_NAMED_MIN_DIGITS = 6
#: 文本列被判为"数值列"的可数值化占比阈值
_NUMERIC_TEXT_RATIO = 0.8


def _is_pure_digits(value: Any) -> bool:
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(value, str):
        v = value.strip()
        return v.isdigit()
    return False


def classify_semantic_type(name: str, values: Sequence[Any], dtype: str) -> str:
    """按**值 + 列名**判定业务语义类型（纯函数，便于单测与复用）。

    仅使用最小规则：
    - dtype 为 number/date/boolean/empty 时直接映射（number 也可能被"编号"覆盖，
      见下方 identifier 规则）；
    - string/mixed：若"纯数字串"占比 >= 0.6 且
        * 最长数字串长度 >= 12（长数字，如 18/19 位 ID / 时间戳），或
        * 列名匹配 ID 关键词且最长数字串长度 >= 6
      则判为 identifier；
    - 其余为 text / mixed。
    """
    non_empty = [v for v in values if v is not None and v != '']
    if not non_empty:
        return SEMANTIC_EMPTY

    if dtype == 'boolean':
        return SEMANTIC_BOOLEAN
    if dtype == 'date':
        return SEMANTIC_DATE

    digits = [v for v in non_empty if _is_pure_digits(v)]
    ratio = len(digits) / len(non_empty)
    max_len = max((len(str(v).strip()) for v in digits), default=0)
    if digits and ratio >= _ID_PURE_DIGIT_RATIO and (
        max_len >= _ID_LONG_DIGITS
        or (_ID_NAME_RE.search(name or '') and max_len >= _ID_NAMED_MIN_DIGITS)
    ):
        return SEMANTIC_IDENTIFIER

    if dtype == 'number':
        return SEMANTIC_NUMBER
    if dtype == 'string':
        # 文本型数字列（单元格是 "18.12" / "1" 这类文本）仍属数值列：
        # 统计层按**值**判断可数值化，这里只是把语义标注与之对齐。
        numeric_like = 0
        for v in non_empty:
            s = str(v).strip()
            if not s:
                continue
            try:
                float(s)
            except (TypeError, ValueError):
                continue
            numeric_like += 1
        if non_empty and numeric_like / len(non_empty) >= _NUMERIC_TEXT_RATIO:
            return SEMANTIC_NUMBER
        return SEMANTIC_TEXT
    if dtype == 'empty':
        return SEMANTIC_EMPTY
    return SEMANTIC_MIXED


def is_identifier_column(col: 'ColumnMeta', sheet_values: Optional[Sequence[Any]] = None) -> bool:
    """该列是否业务标识列（标识列不允许 SUM/AVG/MIN/MAX，按字符串比较与排序）。"""
    if getattr(col, 'semantic_type', ''):
        return col.semantic_type == SEMANTIC_IDENTIFIER
    if sheet_values is None:
        return False
    return classify_semantic_type(col.name, sheet_values, col.dtype) == SEMANTIC_IDENTIFIER


@dataclass
class ColumnMeta:
    """单列元信息。"""

    name: str                              # 最终列名（已去重；保证唯一）
    index: int                             # 逻辑列索引（0-based，行列表中的位置）
    excel_column: int                      # Excel 原始列号（1-based）
    excel_column_letter: str               # Excel 原始列标（A / B / ... / BK）
    dtype: str                             # string | number | date | boolean | empty | mixed
    header_row_excel: Optional[int] = None  # 列名来源的 Excel 行号（1-based）
    non_empty: int = 0                      # 该列非空单元格数（数据行范围内）
    null_count: int = 0                     # 该列空单元格数（数据行范围内）
    # 业务语义类型（stability patch）：identifier/number/text/date/boolean/empty/mixed
    semantic_type: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SheetRepresentation:
    """单个 Sheet 的统一表示。"""

    sheet_name: str
    sheet_index: int
    header_mode: str
    header_rows_excel: List[int] = field(default_factory=list)   # 1-based
    header_depth: int = 0
    columns: List[ColumnMeta] = field(default_factory=list)
    rows: List[List[Any]] = field(default_factory=list)
    row_excel_numbers: List[int] = field(default_factory=list)   # 1-based
    preamble_rows: List[Dict[str, Any]] = field(default_factory=list)
    formulas: Dict[str, str] = field(default_factory=dict)
    merged_ranges: List[str] = field(default_factory=list)
    excel_range: Dict[str, int] = field(default_factory=dict)
    # excel_range keys: row_start / row_end / column_start / column_end （1-based，含表头与描述行）
    row_count: int = 0
    column_count: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def column_names(self) -> List[str]:
        return [c.name for c in self.columns]

    def column_values(self, index: int) -> List[Any]:
        """该列在数据行范围内的全部取值（含 None），用于语义判定/校验。"""
        return [row[index] if index < len(row) else None for row in self.rows]

    def ensure_semantic_types(self) -> None:
        """为缺失 semantic_type 的列补齐业务语义（兼容旧 representation.json）。

        幂等：已有 semantic_type 的列不重算，避免覆盖解析期结论。
        """
        for col in self.columns:
            if col.semantic_type:
                continue
            col.semantic_type = classify_semantic_type(
                col.name, self.column_values(col.index), col.dtype)

    def identifier_columns(self) -> List[str]:
        """业务标识列名（SUM/AVG 等数值聚合不应作用于这些列）。"""
        self.ensure_semantic_types()
        return [c.name for c in self.columns if c.semantic_type == SEMANTIC_IDENTIFIER]

    def preview_rows(self, limit: int = 20) -> List[List[Any]]:
        return self.rows[: max(0, limit)]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['row_count'] = self.row_count
        data['column_count'] = self.column_count
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SheetRepresentation':
        columns = [ColumnMeta(**c) for c in data.get('columns', [])]
        return cls(
            sheet_name=data.get('sheet_name', ''),
            sheet_index=int(data.get('sheet_index', 0)),
            header_mode=data.get('header_mode', HEADER_NONE),
            header_rows_excel=list(data.get('header_rows_excel', []) or []),
            header_depth=int(data.get('header_depth', 0)),
            columns=columns,
            rows=list(data.get('rows', []) or []),
            row_excel_numbers=list(data.get('row_excel_numbers', []) or []),
            preamble_rows=list(data.get('preamble_rows', []) or []),
            formulas=dict(data.get('formulas', {}) or {}),
            merged_ranges=list(data.get('merged_ranges', []) or []),
            excel_range=dict(data.get('excel_range', {}) or {}),
            row_count=int(data.get('row_count', 0)),
            column_count=int(data.get('column_count', 0)),
            warnings=list(data.get('warnings', []) or []),
        )


@dataclass
class WorkbookRepresentation:
    """一个上传文件的统一表示（可含多个 Sheet）。"""

    schema_version: str
    document_id: str
    user_id: Optional[int]
    filename: str
    file_type: str            # xlsx | xls | csv | tsv
    parser: str               # openpyxl | xlrd | csv
    sheet_count: int
    sheets: List[SheetRepresentation] = field(default_factory=list)
    created_at: str = ''
    parse_ms: int = 0
    warnings: List[str] = field(default_factory=list)
    # 预留：后续 Structured Access 可在此挂载表级索引 / 存储定位信息
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return sum(s.row_count for s in self.sheets)

    def sheet_by_index(self, sheet_index: int) -> Optional[SheetRepresentation]:
        if 0 <= sheet_index < len(self.sheets):
            return self.sheets[sheet_index]
        return None

    def summary(self) -> Dict[str, Any]:
        """轻量摘要（供文档列表 / 上传响应使用）。"""
        return {
            'document_id': self.document_id,
            'filename': self.filename,
            'file_type': self.file_type,
            'parser': self.parser,
            'sheet_count': self.sheet_count,
            'total_rows': self.total_rows,
            'sheet_names': [s.sheet_name for s in self.sheets],
            'created_at': self.created_at,
        }

    def sheets_meta(self) -> List[Dict[str, Any]]:
        """各 Sheet 的元信息列表（不含数据行，供 Preview 的 Sheet 切换）。"""
        return [
            {
                'sheet_index': s.sheet_index,
                'sheet_name': s.sheet_name,
                'row_count': s.row_count,
                'column_count': s.column_count,
                'header_mode': s.header_mode,
                'header_rows_excel': s.header_rows_excel,
                'header_depth': s.header_depth,
            }
            for s in self.sheets
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'document_id': self.document_id,
            'user_id': self.user_id,
            'filename': self.filename,
            'file_type': self.file_type,
            'parser': self.parser,
            'sheet_count': self.sheet_count,
            'created_at': self.created_at,
            'parse_ms': self.parse_ms,
            'warnings': self.warnings,
            'extra': self.extra,
            'sheets': [s.to_dict() for s in self.sheets],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkbookRepresentation':
        sheets = [SheetRepresentation.from_dict(s) for s in data.get('sheets', [])]
        return cls(
            schema_version=data.get('schema_version', SCHEMA_VERSION),
            document_id=data.get('document_id', ''),
            user_id=data.get('user_id'),
            filename=data.get('filename', ''),
            file_type=data.get('file_type', ''),
            parser=data.get('parser', ''),
            sheet_count=int(data.get('sheet_count', len(sheets))),
            sheets=sheets,
            created_at=data.get('created_at', ''),
            parse_ms=int(data.get('parse_ms', 0)),
            warnings=list(data.get('warnings', []) or []),
            extra=dict(data.get('extra', {}) or {}),
        )
