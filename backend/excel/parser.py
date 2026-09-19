# -*- coding: utf-8 -*-
"""Excel / 表格统一解析器（Phase 1A）

支持：.xlsx/.xlsm(openpyxl) / .xls(xlrd) / .csv / .tsv(stdlib csv)

设计原则（严格对应 Phase 1A 验收要求）：
1. 不假设“第一行就是表头”。使用可解释的行特征启发式识别表头，
   并支持“描述行 + 表头”以及“双层表头”；无法识别时使用明确的 fallback
   （列名回退为 Excel 列标），绝不静默制造错误列名。
2. 多 Sheet 独立表示，绝不把多个 Sheet 拼接成一个字符串。
3. 完整保留 Excel 原生行列坐标（1-based），用于后续 Range / 来源引用。
4. 空单元格、空列、中文、换行、公式单元格均不丢失；行数/列数与原始使用区域一致。
5. 超长数字（Order ID / SKU ID 等）一律字符串化，避免精度损坏。
6. 不做复杂数据类型推理：仅区分 string/number/date/boolean/empty/mixed。
"""

import csv
import io
import logging
import math
import os
import time
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.excel.representation import (
    HEADER_MULTI,
    HEADER_NONE,
    HEADER_SINGLE,
    SCHEMA_VERSION,
    ColumnMeta,
    SheetRepresentation,
    WorkbookRepresentation,
    classify_semantic_type,
)

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {'.xlsx', '.xls', '.csv', '.tsv'}

# 超过该绝对值的整数一律字符串化（订单号 / SKU / 时间戳等业务主键）
LONG_INT_THRESHOLD = 10 ** 15

# 单元格类型
K_EMPTY = 'empty'
K_STRING = 'string'
K_NUMBER = 'number'
K_DATE = 'date'
K_BOOLEAN = 'boolean'

# 表头 / 描述行判定阈值（可解释、集中管理，避免散落魔法数字）
HEADER_MIN_FILL_RATIO = 0.5      # 表头行非空占比下限
HEADER_MIN_NON_EMPTY = 2         # 表头行最少非空单元格数（避免“单格标题行”被当成表头）
HEADER_MAX_NUMERIC_RATIO = 0.3   # 表头行中数值/日期占比上限
HEADER_MAX_AVG_TEXT_LEN = 40     # 表头单元格平均文本长度上限（超过视为描述句）
DESC_MIN_AVG_TEXT_LEN = 30       # 描述行平均文本长度下限
DESC_MAX_NUMERIC_RATIO = 0.1     # 描述行数值占比上限
SHORT_LABEL_MAX_LEN = 25         # 第二层表头的“短标签”长度上限
SHORT_LABEL_MAX_DOT_RATIO = 0.2  # 第二层表头的句号结尾占比上限


def excel_col_letter(col_1based: int) -> str:
    """1-based 列号 -> Excel 列标（1->A, 27->AA）。"""
    letters = ''
    n = int(col_1based)
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _normalize_cell(value: Any) -> Tuple[Any, str]:
    """将原生单元格值规约为 JSON 安全基元 + 类型。

    关键：超长整数（>=1e15）字符串化，避免 Excel/浮点导致的精度损坏。
    """
    if value is None:
        return None, K_EMPTY
    if isinstance(value, bool):
        return value, K_BOOLEAN
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat(sep=' ') if isinstance(value, datetime) else value.isoformat(), K_DATE
    if isinstance(value, int):
        if abs(value) >= LONG_INT_THRESHOLD:
            return str(value), K_STRING
        return value, K_NUMBER
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None, K_EMPTY
        if value.is_integer():
            iv = int(value)
            if abs(iv) >= LONG_INT_THRESHOLD:
                return str(iv), K_STRING
            return iv, K_NUMBER
        return value, K_NUMBER
    if isinstance(value, str):
        if value == '':
            return None, K_EMPTY
        return value, K_STRING
    # 兜底：其他类型转字符串，绝不丢弃
    text = str(value)
    if text == '':
        return None, K_EMPTY
    return text, K_STRING


def _coerce_csv_scalar(text: str) -> Tuple[Any, str]:
    """CSV/TSV 无类型信息，做保守的标量识别（长数字仍保持字符串）。"""
    if text is None:
        return None, K_EMPTY
    if text == '':
        return None, K_EMPTY
    stripped = text.strip()
    if stripped == '':
        return text, K_STRING
    # 整数
    try:
        if stripped.lstrip('+-').isdigit():
            iv = int(stripped)
            if abs(iv) >= LONG_INT_THRESHOLD:
                return text, K_STRING     # 长数字保持原样（字符串）
            return iv, K_NUMBER
    except (ValueError, TypeError):
        pass
    # 浮点（保留原字符串的语义：仅在能无损解析时转为数值）
    try:
        fv = float(stripped)
        if math.isnan(fv) or math.isinf(fv):
            return text, K_STRING
        return fv, K_NUMBER
    except (ValueError, TypeError):
        pass
    return text, K_STRING


def _row_stats(values: List[Any], kinds: List[str]) -> Dict[str, float]:
    """行特征统计（用于表头/描述行识别）。"""
    total = len(values)
    non_empty_idx = [i for i, k in enumerate(kinds) if k != K_EMPTY]
    non_empty = len(non_empty_idx)
    if non_empty == 0:
        return {
            'non_empty': 0, 'fill_ratio': 0.0, 'numeric_ratio': 0.0,
            'avg_text_len': 0.0, 'dot_end_ratio': 0.0, 'total': total,
        }
    numeric = sum(1 for i in non_empty_idx if kinds[i] in (K_NUMBER, K_DATE))
    texts = [str(values[i]) for i in non_empty_idx if kinds[i] == K_STRING]
    avg_text_len = (sum(len(t) for t in texts) / len(texts)) if texts else 0.0
    dot_end = sum(1 for t in texts if t.rstrip().endswith('.'))
    dot_ratio = (dot_end / len(texts)) if texts else 0.0
    return {
        'non_empty': non_empty,
        'fill_ratio': non_empty / total if total else 0.0,
        'numeric_ratio': numeric / non_empty,
        'avg_text_len': avg_text_len,
        'dot_end_ratio': dot_ratio,
        'total': total,
    }


class ExcelParser:
    """将表格文件解析为统一表示（WorkbookRepresentation）。"""

    def parse(
        self,
        file_path: str,
        document_id: str,
        user_id: Optional[int] = None,
        filename: Optional[str] = None,
        file_type: Optional[str] = None,
    ) -> WorkbookRepresentation:
        started = time.perf_counter()
        path = Path(file_path)
        ext = ('.' + (file_type or '').lower().lstrip('.')) if file_type else path.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ValueError(f'不支持的表格类型：{ext or path.suffix}')

        display_name = filename or path.name
        root_warnings: List[str] = []

        if ext in ('.xlsx', '.xlsm'):
            grids = self._read_xlsx(path, root_warnings)
            parser_name = 'openpyxl'
        elif ext == '.xls':
            grids = self._read_xls(path, root_warnings)
            parser_name = 'xlrd'
        else:
            grids = self._read_csv_tsv(path, ext, root_warnings)
            parser_name = 'csv'

        sheets: List[SheetRepresentation] = []
        for idx, grid in enumerate(grids):
            sheets.append(self._build_sheet(grid, idx))

        parse_ms = int((time.perf_counter() - started) * 1000)
        rep = WorkbookRepresentation(
            schema_version=SCHEMA_VERSION,
            document_id=document_id,
            user_id=user_id,
            filename=display_name,
            file_type=ext.lstrip('.'),
            parser=parser_name,
            sheet_count=len(sheets),
            sheets=sheets,
            created_at=datetime.now().isoformat(),
            parse_ms=parse_ms,
            warnings=root_warnings,
        )
        logger.info(
            '[excel] 解析完成 file=%s type=%s sheets=%d rows=%d ms=%d',
            display_name, rep.file_type, rep.sheet_count, rep.total_rows, parse_ms,
        )
        return rep

    # ------------------------------------------------------------------
    # 各格式读取：统一输出 Grid
    # ------------------------------------------------------------------
    def _read_xlsx(self, path: Path, warnings: List[str]) -> List[Dict[str, Any]]:
        try:
            from openpyxl import load_workbook
        except ImportError as e:  # pragma: no cover
            raise RuntimeError('缺少 openpyxl 依赖，无法解析 xlsx：pip install openpyxl') from e

        wb_formula = load_workbook(filename=str(path), data_only=False, read_only=False)
        wb_value = load_workbook(filename=str(path), data_only=True, read_only=False)
        grids: List[Dict[str, Any]] = []

        for idx, ws in enumerate(wb_formula.worksheets):
            ws_val = wb_value.worksheets[idx] if idx < len(wb_value.worksheets) else ws

            # 合并区域：记录 + 建立 “(row,col) -> 左上角值” 映射（仅用于表头填充）
            merged_ranges: List[str] = [str(rng) for rng in ws.merged_cells.ranges]
            fill_map: Dict[Tuple[int, int], Any] = {}
            for rng in ws.merged_cells.ranges:
                top_left = ws.cell(row=rng.min_row, column=rng.min_col).value
                for rr in range(rng.min_row, rng.max_row + 1):
                    for cc in range(rng.min_col, rng.max_col + 1):
                        fill_map[(rr, cc)] = top_left

            formulas: Dict[str, str] = {}
            raw_rows: List[List[Any]] = []
            kinds_rows: List[List[str]] = []
            max_col = max(ws.max_column or 0, 1)
            max_row = max(ws.max_row or 0, 1)

            for r in range(1, max_row + 1):
                row_vals: List[Any] = []
                row_kinds: List[str] = []
                for c in range(1, max_col + 1):
                    cell = ws.cell(row=r, column=c)
                    raw = cell.value
                    if isinstance(raw, str) and raw.startswith('='):
                        formulas[f'R{r}C{c}'] = raw
                        cached = ws_val.cell(row=r, column=c).value
                        raw = cached if cached is not None else raw
                    val, kind = _normalize_cell(raw)
                    row_vals.append(val)
                    row_kinds.append(kind)
                raw_rows.append(row_vals)
                kinds_rows.append(row_kinds)

            grids.append({
                'sheet_name': ws.title,
                'values': raw_rows,
                'kinds': kinds_rows,
                'fill_map': fill_map,
                'merged_ranges': merged_ranges,
                'formulas': formulas,
            })

        return grids

    def _read_xls(self, path: Path, warnings: List[str]) -> List[Dict[str, Any]]:
        try:
            import xlrd
        except ImportError as e:  # pragma: no cover
            raise RuntimeError('缺少 xlrd 依赖，无法解析 xls：pip install xlrd') from e

        book = xlrd.open_workbook(str(path))
        grids: List[Dict[str, Any]] = []
        warnings.append('.xls 由 xlrd 解析：不提供公式文本（仅保留计算后的值）。')

        for sh in book.sheets():
            merged_ranges: List[str] = []
            fill_map: Dict[Tuple[int, int], Any] = {}
            for (r_lo, r_hi, c_lo, c_hi) in getattr(sh, 'merged_cells', []):
                merged_ranges.append(
                    f'{excel_col_letter(c_lo + 1)}{r_lo + 1}:{excel_col_letter(c_hi)}{r_hi}'
                )
                top_left = sh.cell_value(r_lo, c_lo)
                for rr in range(r_lo, r_hi):
                    for cc in range(c_lo, c_hi):
                        fill_map[(rr + 1, cc + 1)] = top_left

            values: List[List[Any]] = []
            kinds: List[List[str]] = []
            for r in range(sh.nrows):
                row_vals: List[Any] = []
                row_kinds: List[str] = []
                for c in range(sh.ncols):
                    cell = sh.cell(r, c)
                    raw: Any = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            raw = xlrd.xldate.xldate_as_datetime(cell.value, book.datemode)
                        except Exception:
                            raw = cell.value
                    elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        raw = bool(cell.value)
                    elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                        raw = None
                    elif cell.ctype == xlrd.XL_CELL_ERROR:
                        raw = None
                    val, kind = _normalize_cell(raw)
                    row_vals.append(val)
                    row_kinds.append(kind)
                values.append(row_vals)
                kinds.append(row_kinds)

            grids.append({
                'sheet_name': sh.name,
                'values': values,
                'kinds': kinds,
                'fill_map': fill_map,
                'merged_ranges': merged_ranges,
                'formulas': {},
            })

        return grids

    def _read_csv_tsv(self, path: Path, ext: str, warnings: List[str]) -> List[Dict[str, Any]]:
        content, encoding = self._decode_text(path)
        warnings.append(f'CSV/TSV 解析使用编码：{encoding}')

        if ext == '.tsv':
            delimiter = '\t'
        else:
            sample = content[:8192]
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=',;\t|').delimiter
            except Exception:
                delimiter = ','
        warnings.append(f'CSV/TSV 解析使用分隔符：{delimiter!r}')

        reader = csv.reader(io.StringIO(content), delimiter=delimiter)
        values: List[List[Any]] = []
        kinds: List[List[str]] = []
        for row in reader:
            row_vals: List[Any] = []
            row_kinds: List[str] = []
            for cell_text in row:
                val, kind = _coerce_csv_scalar(cell_text)
                row_vals.append(val)
                row_kinds.append(kind)
            values.append(row_vals)
            kinds.append(row_kinds)

        sheet_name = 'CSV' if ext == '.csv' else 'TSV'
        return [{
            'sheet_name': sheet_name,
            'values': values,
            'kinds': kinds,
            'fill_map': {},
            'merged_ranges': [],
            'formulas': {},
        }]

    @staticmethod
    def _decode_text(path: Path) -> Tuple[str, str]:
        raw = path.read_bytes()
        for enc in ('utf-8-sig', 'utf-8', 'gbk', 'gb18030', 'big5', 'latin-1'):
            try:
                return raw.decode(enc), enc
            except UnicodeDecodeError:
                continue
        raise UnicodeDecodeError('utf-8', raw, 0, 1, '无法识别文本编码')

    # ------------------------------------------------------------------
    # Grid -> SheetRepresentation
    # ------------------------------------------------------------------
    def _build_sheet(self, grid: Dict[str, Any], sheet_index: int) -> SheetRepresentation:
        sheet_name = grid['sheet_name']
        values: List[List[Any]] = grid['values']
        kinds: List[List[str]] = grid['kinds']
        fill_map: Dict[Tuple[int, int], Any] = grid.get('fill_map') or {}
        merged_ranges: List[str] = grid.get('merged_ranges') or []
        formulas: Dict[str, str] = grid.get('formulas') or {}

        warnings: List[str] = []
        n_rows = len(values)
        n_cols = max((len(r) for r in values), default=0)

        # 补齐每行到相同宽度（保证 N×M 矩形，不因尾部空单元格丢失列）
        for r in range(n_rows):
            if len(values[r]) < n_cols:
                values[r].extend([None] * (n_cols - len(values[r])))
                kinds[r].extend([K_EMPTY] * (n_cols - len(kinds[r])))

        if n_rows == 0 or n_cols == 0:
            return SheetRepresentation(
                sheet_name=sheet_name,
                sheet_index=sheet_index,
                header_mode=HEADER_NONE,
                row_count=0,
                column_count=0,
                warnings=['空 Sheet'],
            )

        # 1) 裁剪：仅裁掉四周全空的行列；内部空行/空列完整保留
        row_has_data = [any(k != K_EMPTY for k in kinds[r]) for r in range(n_rows)]
        col_has_data = [
            any(kinds[r][c] != K_EMPTY for r in range(n_rows))
            for c in range(n_cols)
        ]

        if not any(row_has_data):
            return SheetRepresentation(
                sheet_name=sheet_name,
                sheet_index=sheet_index,
                header_mode=HEADER_NONE,
                row_count=0,
                column_count=0,
                warnings=['Sheet 内无有效数据'],
            )

        row_start = row_has_data.index(True)
        row_end = n_rows - 1 - row_has_data[::-1].index(True)      # inclusive
        col_start = col_has_data.index(True)
        col_end = n_cols - 1 - col_has_data[::-1].index(True)      # inclusive

        trimmed_values = [values[r][col_start:col_end + 1] for r in range(row_start, row_end + 1)]
        trimmed_kinds = [kinds[r][col_start:col_end + 1] for r in range(row_start, row_end + 1)]
        width = col_end - col_start + 1
        excel_row_numbers = [row_start + 1 + i for i in range(len(trimmed_values))]  # 1-based
        excel_col_numbers = [col_start + 1 + i for i in range(width)]                # 1-based

        # 2) 逐行特征
        row_stats = [_row_stats(trimmed_values[i], trimmed_kinds[i]) for i in range(len(trimmed_values))]

        def header_like(i: int) -> bool:
            st = row_stats[i]
            if st['non_empty'] == 0:
                return False
            # 多列场景下，单格标题行（如 A1="月度报表"）不算表头
            if width > 1 and st['non_empty'] < HEADER_MIN_NON_EMPTY:
                return False
            if st['fill_ratio'] < HEADER_MIN_FILL_RATIO:
                return False
            if st['numeric_ratio'] > HEADER_MAX_NUMERIC_RATIO:
                return False
            if st['avg_text_len'] > HEADER_MAX_AVG_TEXT_LEN:
                return False
            return True

        def description_like(i: int) -> bool:
            st = row_stats[i]
            if st['non_empty'] == 0:
                return False
            if st['numeric_ratio'] > DESC_MAX_NUMERIC_RATIO:
                return False
            return st['avg_text_len'] > DESC_MIN_AVG_TEXT_LEN or st['dot_end_ratio'] > 0.3

        def short_labels(i: int) -> bool:
            st = row_stats[i]
            return st['avg_text_len'] <= SHORT_LABEL_MAX_LEN and st['dot_end_ratio'] <= SHORT_LABEL_MAX_DOT_RATIO

        # 3) 定位表头行
        header_idx: Optional[int] = None
        for i in range(len(trimmed_values)):
            if header_like(i):
                header_idx = i
                break

        preamble_idx: List[int] = []
        header_idx_list: List[int] = []
        header_mode = HEADER_NONE

        if header_idx is None:
            warnings.append('未能识别表头行：已回退为“无表头”模式，列名使用 Excel 列标（col_A...）。')
            data_start = next((i for i, st in enumerate(row_stats) if st['non_empty'] > 0), 0)
            preamble_idx = list(range(0, data_start))
        else:
            header_idx_list = [header_idx]
            # 表头之前的行（标题/说明）完整保留为 preamble
            preamble_idx = list(range(0, header_idx))

            # 仅在“首行存在重复列名（分组表头）”或“存在合并单元格”时才允许双层表头，
            # 避免把描述行或数据行误判为第二层表头。
            primary_raw = [trimmed_values[header_idx][c] for c in range(width)]
            primary_non_empty = [str(v) for v in primary_raw if v is not None]
            has_dup = len(primary_non_empty) != len(set(primary_non_empty))
            merged_in_header = bool(merged_ranges)
            multi_level_allowed = (has_dup or merged_in_header) and len(primary_non_empty) >= 2

            j = header_idx + 1
            while j < len(trimmed_values):
                if description_like(j):
                    preamble_idx.append(j)
                    j += 1
                    continue
                if multi_level_allowed and header_like(j) and short_labels(j):
                    header_idx_list.append(j)
                    multi_level_allowed = False
                    j += 1
                    continue
                break

            header_mode = HEADER_MULTI if len(header_idx_list) > 1 else HEADER_SINGLE
            data_start = j
            # 表头/描述之后若还有描述行，继续纳入 preamble
            while data_start < len(trimmed_values) and description_like(data_start):
                preamble_idx.append(data_start)
                data_start += 1

        preamble_idx = sorted(set(preamble_idx))

        # 4) 列名（含合并单元格横向填充）
        def header_cell_value(row_i: int, col_i: int) -> Any:
            val = trimmed_values[row_i][col_i]
            if val is None:
                # 合并区域填充（Excel 1-based 坐标）
                excel_r = excel_row_numbers[row_i]
                excel_c = excel_col_numbers[col_i]
                fill_val = fill_map.get((excel_r, excel_c))
                if fill_val is not None:
                    norm, _ = _normalize_cell(fill_val)
                    return norm
            return val

        columns: List[ColumnMeta] = []
        used_names: Dict[str, int] = {}
        for c in range(width):
            parts: List[str] = []
            for r in header_idx_list:
                v = header_cell_value(r, c)
                if v is None:
                    continue
                text = str(v).strip()
                if text == '':
                    continue
                if not parts or parts[-1] != text:
                    parts.append(text)

            if parts:
                base_name = ' / '.join(parts)
            else:
                letter = excel_col_letter(excel_col_numbers[c])
                base_name = f'col_{letter}'
                warnings.append(
                    f'第 {excel_col_numbers[c]} 列（{letter}）在表头中无名称，已回退为 {base_name}。'
                )

            name = base_name
            if name in used_names:
                used_names[name] += 1
                name = f'{base_name}_{used_names[name]}'
                warnings.append(f'列名重复："{base_name}" 已重命名为 "{name}"。')
            else:
                used_names[name] = 1

            columns.append(ColumnMeta(
                name=name,
                index=c,
                excel_column=excel_col_numbers[c],
                excel_column_letter=excel_col_letter(excel_col_numbers[c]),
                dtype=K_EMPTY,
                header_row_excel=excel_row_numbers[header_idx_list[-1]] if header_idx_list else None,
            ))

        # 5) 数据行
        data_values = trimmed_values[data_start:]
        data_excel_rows = excel_row_numbers[data_start:]
        data_kinds = trimmed_kinds[data_start:]

        if merged_ranges:
            warnings.append(
                f'检测到 {len(merged_ranges)} 个合并单元格区域；数据行中合并单元格仅保留左上角值。'
            )

        empty_data_rows = sum(1 for k in data_kinds if all(x == K_EMPTY for x in k))
        if empty_data_rows:
            warnings.append(f'数据区包含 {empty_data_rows} 个全空行（已完整保留以维持行数一致）。')

        # 6) 列类型统计
        for c in range(width):
            col_kinds = [data_kinds[r][c] for r in range(len(data_kinds))]
            non_empty = [k for k in col_kinds if k != K_EMPTY]
            null_count = len(col_kinds) - len(non_empty)
            if not non_empty:
                dtype = K_EMPTY
            elif all(k == K_NUMBER for k in non_empty):
                dtype = K_NUMBER
            elif all(k == K_DATE for k in non_empty):
                dtype = K_DATE
            elif all(k == K_BOOLEAN for k in non_empty):
                dtype = K_BOOLEAN
            elif len(set(non_empty)) == 1:
                dtype = non_empty[0]
            else:
                dtype = 'mixed'
            columns[c].dtype = dtype
            columns[c].non_empty = len(non_empty)
            columns[c].null_count = null_count
            # 业务语义类型（稳定性补丁）：区分"业务主键（长数字 ID）"与"真实数值列"，
            # 供统计层拒绝 SUM/AVG(ID) 等无意义且会丢精度的操作。
            columns[c].semantic_type = classify_semantic_type(
                columns[c].name,
                [data_values[r][c] for r in range(len(data_values))],
                dtype,
            )

        # 7) 可验证性：行数/列数必须与裁剪后的矩阵一致
        sheet = SheetRepresentation(
            sheet_name=sheet_name,
            sheet_index=sheet_index,
            header_mode=header_mode,
            header_rows_excel=[excel_row_numbers[i] for i in header_idx_list],
            header_depth=len(header_idx_list),
            columns=columns,
            rows=data_values,
            row_excel_numbers=data_excel_rows,
            preamble_rows=[
                {
                    'excel_row': excel_row_numbers[i],
                    'values': trimmed_values[i],
                }
                for i in preamble_idx
            ],
            formulas=formulas,
            merged_ranges=merged_ranges,
            excel_range={
                'row_start': excel_row_numbers[0],
                'row_end': excel_row_numbers[-1],
                'column_start': excel_col_numbers[0],
                'column_end': excel_col_numbers[-1],
            },
            row_count=len(data_values),
            column_count=len(columns),
            warnings=warnings,
        )

        if sheet.row_count != len(data_values) or sheet.column_count != width:
            raise AssertionError('Representation 行列数自检失败：内部一致性错误')

        return sheet
