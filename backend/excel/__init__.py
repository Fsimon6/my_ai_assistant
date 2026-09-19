# -*- coding: utf-8 -*-
"""Excel / 表格统一表示与解析包（Phase 1A）

对外暴露：
- ExcelParser: 解析 xlsx / xls / csv / tsv -> WorkbookRepresentation
- WorkbookRepresentation / SheetRepresentation / ColumnMeta: 统一中间表示
- store: 原始文件 + representation.json 的本地持久化（含用户隔离）

本包是后续 Phase 1B（精确查询）/ Phase 1C（统计）/ Structured Table Access 的
唯一数据入口，Preview 与后续能力必须复用该表示，禁止二次解析原文件。
"""

from backend.excel.representation import (
    SCHEMA_VERSION,
    ColumnMeta,
    SheetRepresentation,
    WorkbookRepresentation,
)
from backend.excel.parser import ExcelParser

__all__ = [
    'SCHEMA_VERSION',
    'ColumnMeta',
    'SheetRepresentation',
    'WorkbookRepresentation',
    'ExcelParser',
]
