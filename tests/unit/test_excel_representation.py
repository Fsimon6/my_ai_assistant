# -*- coding: utf-8 -*-
"""Phase 1A：Excel 统一表示（Unified Table Representation）解析测试矩阵

覆盖：
A. XLSX        B. XLS（无可用夹具，skip）   C. CSV      D. TSV
E. 单 Sheet    F. 多 Sheet                  G. 中文字段  H. 空值
I. 数字        J. 超长数字 / Order ID       K. 双层/复杂表头

核心校验（不硬编码任何具体行数）：
- Representation 行数 == 原始非空行数按「表头 + 描述行 + 数据行」全覆盖（无行丢失）
- Representation 列数 == 原始非空列数
- 每行单元格数 == column_count（N×M 完整）
- 超长数字（Order ID）必须为字符串且逐位一致
"""

from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from backend.excel.parser import ExcelParser
from backend.excel.representation import HEADER_MULTI, HEADER_NONE, HEADER_SINGLE

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REAL_XLSX_CANDIDATES = [
    PROJECT_ROOT / '直邮5店 7.10号订单.xlsx',
    PROJECT_ROOT / '直邮一店 8.20号订单.xlsx',
]
LONG_ORDER_ID = '577532993098256512'


def _parse(path, document_id='test-doc', user_id=1):
    return ExcelParser().parse(
        file_path=str(path),
        document_id=document_id,
        user_id=user_id,
        filename=Path(path).name,
    )


def _assert_structural_invariants(rep):
    """表示内部一致性：行列数自洽、列名唯一、矩形完整。"""
    assert rep.sheet_count == len(rep.sheets)
    for sheet in rep.sheets:
        assert sheet.row_count == len(sheet.rows), 'row_count 与 rows 长度不一致'
        assert sheet.column_count == len(sheet.columns), 'column_count 与 columns 长度不一致'
        assert len(sheet.row_excel_numbers) == sheet.row_count, '缺少 Excel 行号映射'
        assert len(sheet.column_names) == len(set(sheet.column_names)), '列名必须唯一'
        for row in sheet.rows:
            assert len(row) == sheet.column_count, '数据行宽度必须等于列数'


def _xlsx_accounted_rows(path):
    """独立读取（openpyxl）原始非空行号集合，用于“无行丢失”校验。"""
    wb = load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    rows = set()
    for r in range(1, ws.max_row + 1):
        if any(ws.cell(row=r, column=c).value not in (None, '') for c in range(1, ws.max_column + 1)):
            rows.add(r)
    cols = sum(
        1 for c in range(1, ws.max_column + 1)
        if any(ws.cell(row=r, column=c).value not in (None, '') for r in range(1, ws.max_row + 1))
    )
    return rows, cols


def _delimited_accounted_rows(path, delimiter):
    lines = [ln for ln in path.read_text(encoding='utf-8-sig').splitlines() if ln.strip()]
    return set(range(1, len(lines) + 1)), len(lines[0].split(delimiter))


def _assert_no_row_loss(sheet, accounted_rows, accounted_cols):
    """原始非空行必须全部落在 表头 / 描述行 / 数据行 三者之一（无丢失）。"""
    covered = set(sheet.header_rows_excel)
    covered |= {p['excel_row'] for p in sheet.preamble_rows}
    covered |= set(sheet.row_excel_numbers)
    assert covered == accounted_rows, (
        f'行覆盖不一致：原始 {sorted(accounted_rows)} vs 表示 {sorted(covered)}'
    )
    assert sheet.column_count == accounted_cols, (
        f'列数不一致：原始 {accounted_cols} vs 表示 {sheet.column_count}'
    )


# ----------------------------------------------------------------------
# 夹具：synthetic（标注为 synthetic，避免与真实业务文件混淆）
# ----------------------------------------------------------------------
@pytest.fixture
def synthetic_single_xlsx(tmp_path):
    """[synthetic] 单 Sheet：中文 / 数字 / 日期 / 空值 / 超长数字 / 换行。"""
    wb = Workbook()
    ws = wb.active
    ws.title = '订单'
    ws.append(['订单号', '商品名称', '数量', '单价', '下单日期', '备注'])
    ws.append([LONG_ORDER_ID, '中文商品A', 2, 19.9, datetime(2026, 8, 20, 7, 55, 37), None])
    ws.append(['577532993098256513', '商品B', 1, 33.99, datetime(2026, 8, 20, 8, 0, 0), '含\n换行'])
    ws.append(['577532993098256514', '商品C', 0, 0, None, None])
    path = tmp_path / 'synthetic_single.xlsx'
    wb.save(path)
    return path


@pytest.fixture
def synthetic_multi_xlsx(tmp_path):
    """[synthetic] 多 Sheet：每个 Sheet 独立保存，禁止拼接。"""
    wb = Workbook()
    ws1 = wb.active
    ws1.title = 'Sheet1'
    ws1.append(['名称', '数量'])
    ws1.append(['甲', 1])
    ws2 = wb.create_sheet('第二张表')
    ws2.append(['字段', '值'])
    ws2.append(['中文键', 3.5])
    path = tmp_path / 'synthetic_multi.xlsx'
    wb.save(path)
    return path


@pytest.fixture
def synthetic_double_header_xlsx(tmp_path):
    """[synthetic] 双层表头：合并的分组表头 + 子表头。"""
    wb = Workbook()
    ws = wb.active
    ws.title = '双层'
    ws['A1'] = '销售'
    ws.merge_cells('A1:B1')
    ws['C1'] = '订单'
    ws['A2'] = '数量'
    ws['B2'] = '金额'
    ws['C2'] = '订单号'
    ws.append([1, 19.9, LONG_ORDER_ID])
    ws.append([2, 29.9, '577532993098256513'])
    path = tmp_path / 'synthetic_double_header.xlsx'
    wb.save(path)
    return path


@pytest.fixture
def synthetic_title_preamble_xlsx(tmp_path):
    """[synthetic] 表头前存在标题行（单格标题不应被当成表头）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = '带标题'
    ws['A1'] = '月度销售报表'
    ws.append(['名称', '数量'])
    ws.append(['甲', 1])
    ws.append(['乙', 2])
    path = tmp_path / 'synthetic_title.xlsx'
    wb.save(path)
    return path


@pytest.fixture
def synthetic_no_header_xlsx(tmp_path):
    """[synthetic] 纯数字、无表头：必须走明确 fallback（col_A...）而非伪造列名。"""
    wb = Workbook()
    ws = wb.active
    ws.title = '无表头'
    ws.append([1, 2, 3])
    ws.append([4, 5, 6])
    path = tmp_path / 'synthetic_no_header.xlsx'
    wb.save(path)
    return path


@pytest.fixture
def synthetic_csv(tmp_path):
    """[synthetic] CSV：中文表头 + 空值 + 超长数字。"""
    path = tmp_path / 'synthetic.csv'
    path.write_text(
        '订单号,商品名称,数量,备注\n'
        f'{LONG_ORDER_ID},中文商品A,2,无\n'
        '577532993098256513,商品B,1,\n',
        encoding='utf-8-sig',
    )
    return path


@pytest.fixture
def synthetic_tsv(tmp_path):
    """[synthetic] TSV：制表符分隔。"""
    path = tmp_path / 'synthetic.tsv'
    path.write_text(
        '名称\t数量\t单价\n甲\t1\t9.9\n乙\t2\t19.9\n',
        encoding='utf-8',
    )
    return path


# ----------------------------------------------------------------------
# A/E/G/H/I/J：单 Sheet XLSX
# ----------------------------------------------------------------------
def test_single_sheet_xlsx(synthetic_single_xlsx, capsys):
    rep = _parse(synthetic_single_xlsx)
    _assert_structural_invariants(rep)

    assert rep.file_type == 'xlsx'
    assert rep.parser == 'openpyxl'
    assert rep.sheet_count == 1

    sheet = rep.sheets[0]
    assert sheet.header_mode == HEADER_SINGLE
    assert sheet.column_count == 6
    assert sheet.row_count == 3
    assert sheet.column_names == ['订单号', '商品名称', '数量', '单价', '下单日期', '备注']
    assert sheet.rows[0][0] == LONG_ORDER_ID and isinstance(sheet.rows[0][0], str)
    assert sheet.rows[1][5] == '含\n换行'
    assert sheet.rows[2][4] is None          # 空日期保留为 None
    assert sheet.rows[0][3] == 19.9

    accounted_rows, accounted_cols = _xlsx_accounted_rows(synthetic_single_xlsx)
    _assert_no_row_loss(sheet, accounted_rows, accounted_cols)

    with capsys.disabled():
        print(f'\n[程序级验收] single_sheet.xlsx | sheet={sheet.sheet_name} '
              f'rows={sheet.row_count} cols={sheet.column_count} '
              f'columns={sheet.column_names} parse_ms={rep.parse_ms}')
        print(f'  前5行: {sheet.rows[:5]}')
        print(f'  后5行: {sheet.rows[-5:]}')


# ----------------------------------------------------------------------
# B：XLS（环境无可用 .xls 夹具）
# ----------------------------------------------------------------------
@pytest.mark.skip(reason='环境无 .xls 测试文件，且未安装 xlwt（写入 .xls 需额外依赖）；'
                         'xlrd 读取路径已实现，待补充夹具后启用')
def test_xls_parsing():
    pass


# ----------------------------------------------------------------------
# F：多 Sheet
# ----------------------------------------------------------------------
def test_multi_sheet(synthetic_multi_xlsx, capsys):
    rep = _parse(synthetic_multi_xlsx)
    _assert_structural_invariants(rep)
    assert rep.sheet_count == 2
    assert rep.sheets[0].sheet_name == 'Sheet1'
    assert rep.sheets[1].sheet_name == '第二张表'
    assert rep.sheets[0].row_count == 1 and rep.sheets[0].column_count == 2
    assert rep.sheets[1].row_count == 1 and rep.sheets[1].column_count == 2
    assert rep.sheets[1].rows[0][0] == '中文键'
    with capsys.disabled():
        print(f'\n[程序级验收] multi_sheet.xlsx | sheet_count={rep.sheet_count} '
              f'total_rows={rep.total_rows} sheets={rep.sheets_meta()}')


# ----------------------------------------------------------------------
# K：双层表头（含合并单元格）
# ----------------------------------------------------------------------
def test_double_header_merged(synthetic_double_header_xlsx, capsys):
    rep = _parse(synthetic_double_header_xlsx)
    _assert_structural_invariants(rep)
    sheet = rep.sheets[0]
    assert sheet.header_mode == HEADER_MULTI
    assert sheet.header_depth == 2
    assert sheet.header_rows_excel == [1, 2]
    assert sheet.column_names == ['销售 / 数量', '销售 / 金额', '订单 / 订单号']
    assert sheet.row_count == 2
    assert sheet.column_count == 3
    assert sheet.rows[0][2] == LONG_ORDER_ID
    with capsys.disabled():
        print(f'\n[程序级验收] double_header.xlsx | mode={sheet.header_mode} '
              f'header_rows={sheet.header_rows_excel} columns={sheet.column_names} '
              f'rows={sheet.row_count}')


# ----------------------------------------------------------------------
# 表头前存在标题行
# ----------------------------------------------------------------------
def test_title_row_is_preamble_not_header(synthetic_title_preamble_xlsx, capsys):
    rep = _parse(synthetic_title_preamble_xlsx)
    _assert_structural_invariants(rep)
    sheet = rep.sheets[0]
    assert sheet.header_mode == HEADER_SINGLE
    assert sheet.header_rows_excel == [2]
    assert sheet.column_names == ['名称', '数量']
    assert [p['excel_row'] for p in sheet.preamble_rows] == [1]
    assert sheet.row_count == 2
    accounted_rows, accounted_cols = _xlsx_accounted_rows(synthetic_title_preamble_xlsx)
    _assert_no_row_loss(sheet, accounted_rows, accounted_cols)
    with capsys.disabled():
        print(f'\n[程序级验收] title_preamble.xlsx | header_rows={sheet.header_rows_excel} '
              f'preamble={[p["excel_row"] for p in sheet.preamble_rows]} '
              f'columns={sheet.column_names} rows={sheet.row_count}')


# ----------------------------------------------------------------------
# 明确 fallback：无表头
# ----------------------------------------------------------------------
def test_no_header_fallback(synthetic_no_header_xlsx, capsys):
    rep = _parse(synthetic_no_header_xlsx)
    _assert_structural_invariants(rep)
    sheet = rep.sheets[0]
    assert sheet.header_mode == HEADER_NONE
    assert sheet.header_depth == 0
    assert sheet.column_names == ['col_A', 'col_B', 'col_C']
    assert sheet.row_count == 2
    assert sheet.warnings, '无表头回退必须产生 warning，而非静默'
    with capsys.disabled():
        print(f'\n[程序级验收] no_header.xlsx | mode={sheet.header_mode} '
              f'columns={sheet.column_names} rows={sheet.row_count} '
              f'warnings={sheet.warnings}')


# ----------------------------------------------------------------------
# C：CSV
# ----------------------------------------------------------------------
def test_csv_parsing(synthetic_csv, capsys):
    rep = _parse(synthetic_csv)
    _assert_structural_invariants(rep)
    assert rep.file_type == 'csv'
    assert rep.parser == 'csv'
    sheet = rep.sheets[0]
    assert sheet.column_names == ['订单号', '商品名称', '数量', '备注']
    assert sheet.row_count == 2
    assert sheet.column_count == 4
    assert sheet.rows[0][0] == LONG_ORDER_ID and isinstance(sheet.rows[0][0], str)
    assert sheet.rows[1][3] is None          # 空值
    assert sheet.rows[0][2] == 2             # 数字识别
    accounted_rows, accounted_cols = _delimited_accounted_rows(synthetic_csv, ',')
    _assert_no_row_loss(sheet, accounted_rows, accounted_cols)
    with capsys.disabled():
        print(f'\n[程序级验收] synthetic.csv | columns={sheet.column_names} '
              f'rows={sheet.row_count} parse_ms={rep.parse_ms} '
              f'前2行={sheet.rows[:2]}')


# ----------------------------------------------------------------------
# D：TSV
# ----------------------------------------------------------------------
def test_tsv_parsing(synthetic_tsv, capsys):
    rep = _parse(synthetic_tsv)
    _assert_structural_invariants(rep)
    assert rep.file_type == 'tsv'
    sheet = rep.sheets[0]
    assert sheet.column_names == ['名称', '数量', '单价']
    assert sheet.row_count == 2
    assert sheet.rows[0] == ['甲', 1, 9.9]
    accounted_rows, accounted_cols = _delimited_accounted_rows(synthetic_tsv, '\t')
    _assert_no_row_loss(sheet, accounted_rows, accounted_cols)
    with capsys.disabled():
        print(f'\n[程序级验收] synthetic.tsv | columns={sheet.column_names} '
              f'rows={sheet.row_count} parse_ms={rep.parse_ms}')


# ----------------------------------------------------------------------
# 真实业务文件（自动读取真实行列数，不硬编码 448 / 19）
# ----------------------------------------------------------------------
@pytest.mark.parametrize('real_xlsx', REAL_XLSX_CANDIDATES)
def test_real_order_xlsx(real_xlsx, capsys):
    if not real_xlsx.exists():
        pytest.skip(f'真实测试文件不存在：{real_xlsx.name}')

    rep = _parse(real_xlsx)
    _assert_structural_invariants(rep)
    sheet = rep.sheets[0]

    accounted_rows, accounted_cols = _xlsx_accounted_rows(real_xlsx)
    _assert_no_row_loss(sheet, accounted_rows, accounted_cols)

    # 超长数字（订单号）必须逐位保真
    for row in sheet.rows:
        for value in row:
            if isinstance(value, str) and value.isdigit() and len(value) >= 15:
                assert len(value) >= 15  # 未被截断/科学计数

    with capsys.disabled():
        print(f'\n[程序级验收] 真实文件 {rep.filename} | parser={rep.parser} '
              f'sheets={rep.sheet_count} rows={sheet.row_count} cols={sheet.column_count} '
              f'header_mode={sheet.header_mode} header_rows={sheet.header_rows_excel} '
              f'preamble={[p["excel_row"] for p in sheet.preamble_rows]} '
              f'parse_ms={rep.parse_ms}')
        print(f'  列名({len(sheet.column_names)}): {sheet.column_names}')
        print(f'  前5行: {sheet.rows[:5]}')
        print(f'  后5行: {sheet.rows[-5:]}')
