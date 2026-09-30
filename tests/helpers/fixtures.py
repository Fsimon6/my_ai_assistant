# -*- coding: utf-8 -*-
"""Stage 5：Golden fixture 定位 + **独立**期望值（人工可推算，不由被测实现生成）。

职责边界：
  * 只定位 `tests/fixtures/` 下的**合成、去标识化**文件（相对于本文件解析路径，
    **不依赖当前工作目录**、**不读取用户 `data/`**、fresh clone 也能找到）；
  * 提供 `parse_golden()`（走真实 `ExcelParser`，直接从 fixture 文件构建 representation，
    因此测试不依赖 `data/excel/**` 的上传产物）；
  * 固化 Golden A/B 的期望值：全部来自 `_generate_golden.py` 的显式公式，
    **人工可推算**（见该文件头部注释）；
  * 提供**独立**的"最大连续段"扫描 `row_runs()` —— 刻意不复用生产 `collapse_row_runs()`，
    避免"被测实现验证自己"。
"""
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.excel import ExcelParser
from backend.excel.representation import WorkbookRepresentation

# --------------------------------------------------------------------------
# 路径（相对本文件，绝不依赖 CWD / data/）
# --------------------------------------------------------------------------
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / 'fixtures'
GOLDEN_EXCEL_DIR = FIXTURE_ROOT / 'excel'
HISTORY_DIR = FIXTURE_ROOT / 'excel_history'

GOLDEN_SMALL_NAME = 'golden_small.xlsx'
GOLDEN_MEDIUM_NAME = 'golden_medium.xlsx'
GOLDEN_SMALL_PATH = GOLDEN_EXCEL_DIR / GOLDEN_SMALL_NAME
GOLDEN_MEDIUM_PATH = GOLDEN_EXCEL_DIR / GOLDEN_MEDIUM_NAME

#: 历史快照 fixture（4 种稳定契约 + 1 组"旧快照"）
HISTORY_SNAPSHOTS = ('table', 'aggregate', 'group_aggregate', 'multi_step', 'legacy_table')


def golden_path(name: str = GOLDEN_SMALL_NAME) -> Path:
    """返回 Golden Excel 的绝对路径（不存在则明确报错，而不是静默 skip）。"""
    path = GOLDEN_EXCEL_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            'Golden fixture 缺失：%s（应随仓库入库；可用 '
            'python tests/fixtures/excel/_generate_golden.py 重新生成）' % path)
    return path


def parse_golden(name: str = GOLDEN_SMALL_NAME, document_id: str = 'golden-synth',
                 user_id: int = 0) -> WorkbookRepresentation:
    """用真实解析器从 Golden fixture 构建 representation（不触碰 data/）。"""
    path = golden_path(name)
    return ExcelParser().parse(file_path=str(path), document_id=document_id,
                               user_id=user_id, filename=path.name,
                               file_type=path.suffix.lstrip('.').lower())


def load_history_snapshot(kind: str) -> Dict[str, Any]:
    """读取历史快照 fixture（与后端 meta_info['excel'] 同构）。"""
    import json
    path = HISTORY_DIR / ('%s.json' % kind)
    if not path.exists():
        raise FileNotFoundError('历史快照 fixture 缺失：%s' % path)
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def history_meta(kind: str) -> Dict[str, Any]:
    """后端/前端共用的 meta_info 形状（包装规则与 `build_excel_turn_meta` 一致）。"""
    return {'source': 'excel', 'excel': load_history_snapshot(kind)}


# --------------------------------------------------------------------------
# 独立工具：最大连续段（**不复用**生产实现）
# --------------------------------------------------------------------------
def row_runs(rows: Sequence[int]) -> List[List[int]]:
    """把已排序/乱序的 Excel 行号折叠成最大连续段（本函数为**独立**实现）。

    例：[3, 12, 18, 19] -> [[3,3],[12,12],[18,19]]；[3,4,5] -> [[3,5]]。
    """
    out: List[List[int]] = []
    for n in sorted({int(x) for x in rows}):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


# --------------------------------------------------------------------------
# Golden A（19 行）—— 人工可推算期望
# --------------------------------------------------------------------------
SMALL_DOCUMENT = GOLDEN_SMALL_NAME
SMALL_SHEET = 'OrderSKUList'
SMALL_SHEET_COUNT = 2
SMALL_ROW_COUNT = 19
SMALL_FIRST_ROW, SMALL_LAST_ROW = 3, 21
SMALL_ALL_ROWS = list(range(SMALL_FIRST_ROW, SMALL_LAST_ROW + 1))

#: contains 'SF' 命中的 Excel 行（离散！）
SMALL_SF_ROWS = [3, 12, 18, 19]
SMALL_SF_MATCHED = 4
SMALL_SF_NUMERIC = 3          # Excel 行 18 的金额为空值
SMALL_SF_EMPTY = 1
SMALL_SF_NON_NUMERIC = 0
SMALL_SF_SUM = 280.00         # 10.00 + 100.00 + 170.00

#: 全表统计（无筛选）
SMALL_TOTAL_MATCHED = 19
SMALL_TOTAL_NUMERIC = 17
SMALL_TOTAL_EMPTY = 1
SMALL_TOTAL_NON_NUMERIC = 1
SMALL_TOTAL_SUM = 1550.00

#: 连续段（独立实现算出的期望；生产实现必须与之一致）
SMALL_ALL_RUNS = [[3, 21]]
SMALL_SF_RUNS = [[3, 3], [12, 12], [18, 19]]

#: 供应商分组（Golden A 设计：SF=4 / JS=7 / Yanwen=8）
SMALL_PROVIDER_GROUPS = {'SF International': 4, 'JS Express International': 7,
                         'Yanwen Express': 8}

#: SKU 分组 SUM（人工可推算：SKU5=1000 / SKU4=270 / SKU3=130 / SKU2=90 / SKU1=60）
SMALL_SKU_ID = lambda n: '1000000000000%05d' % n            # noqa: E731
SMALL_SKU_SUMS = {SMALL_SKU_ID(5): 1000.00, SMALL_SKU_ID(4): 270.00,
                  SMALL_SKU_ID(3): 130.00, SMALL_SKU_ID(2): 90.00,
                  SMALL_SKU_ID(1): 60.00}
SMALL_SKU_TOP3 = [SMALL_SKU_ID(5), SMALL_SKU_ID(4), SMALL_SKU_ID(3)]
SMALL_MULTI_STEP_VALUE = 1400.00        # 1000 + 270 + 130
SMALL_MULTI_STEP_INPUT_ROWS = 3
SMALL_MULTI_STEP_NUMERIC_ROWS = 3

CARRIER = 'Shipping Provider Name'
AMOUNT = 'Order Amount'
SKU = 'SKU ID'

# --------------------------------------------------------------------------
# Golden B（447 行）—— 确定性公式期望
# --------------------------------------------------------------------------
MEDIUM_DOCUMENT = GOLDEN_MEDIUM_NAME
MEDIUM_SHEET = 'OrderSKUList'
MEDIUM_ROW_COUNT = 447
MEDIUM_FIRST_ROW, MEDIUM_LAST_ROW = 3, 449
MEDIUM_ALL_ROWS = list(range(MEDIUM_FIRST_ROW, MEDIUM_LAST_ROW + 1))
MEDIUM_SF_COUNT = 149                   # i % 3 == 0 的行数
MEDIUM_PROVIDER_GROUPS = 3
MEDIUM_SKU_DISTINCT = 120               # SKU 重复（全部 SKU 返回 447 行且不去重）
MEDIUM_TOTAL_SUM = 31536.75


# --------------------------------------------------------------------------
# 供测试直接使用的 catalog（与 API 层 `list_excel_catalog` 同构）
# --------------------------------------------------------------------------
def catalog_of(rep: WorkbookRepresentation) -> List[Dict[str, Any]]:
    return [{
        'document_id': rep.document_id, 'filename': rep.filename,
        'file_type': rep.file_type, 'created_at': rep.created_at,
        'total_rows': rep.total_rows,
        'sheets': [{'sheet_name': s.sheet_name, 'sheet_index': s.sheet_index,
                    'row_count': s.row_count, 'column_count': s.column_count,
                    'columns': s.column_names} for s in rep.sheets],
    }]


def sheet_of(rep: WorkbookRepresentation, sheet_name: str = SMALL_SHEET):
    for s in rep.sheets:
        if s.sheet_name == sheet_name:
            return s
    raise AssertionError('Golden fixture 缺少 Sheet：%s' % sheet_name)


def column_index(sheet, name: str) -> int:
    return sheet.column_names.index(name)


def independent_filter_rows(sheet, column: str, operator: str, value: Any) -> List[int]:
    """**独立**筛选扫描（不复用生产 match 实现）：返回命中的 Excel 行号。"""
    import re as _re
    ci = column_index(sheet, column)
    out: List[int] = []
    for i, row in enumerate(sheet.rows):
        cell = row[ci] if ci < len(row) else None
        text = '' if cell is None else str(cell)
        if operator == 'contains':
            ok = str(value) in text
        elif operator == 'eq':
            ok = text == str(value)
        elif operator == 're':
            ok = bool(_re.search(str(value), text))
        else:
            raise AssertionError('未支持的 operator：%s' % operator)
        if ok:
            out.append(sheet.row_excel_numbers[i])
    return out


def independent_numeric(value: Any) -> Optional[float]:
    """**独立**数值判定（与生产 `aggregate_to_number` 无关，仅覆盖 fixture 用到的形态）。"""
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


def independent_amount_stats(sheet, rows: Sequence[int]) -> Tuple[int, int, int, float]:
    """给定 Excel 行号集合，独立算出 (numeric, empty, non_numeric, sum)。"""
    ci = column_index(sheet, AMOUNT)
    by_row = {sheet.row_excel_numbers[i]: sheet.rows[i] for i in range(len(sheet.rows))}
    numeric = empty = non_numeric = 0
    total = 0.0
    for r in rows:
        row = by_row[r]
        cell = row[ci] if ci < len(row) else None
        if cell is None or (isinstance(cell, str) and not cell.strip()):
            empty += 1
            continue
        num = independent_numeric(cell)
        if num is None:
            non_numeric += 1
        else:
            numeric += 1
            total += num
    return numeric, empty, non_numeric, round(total, 2)
