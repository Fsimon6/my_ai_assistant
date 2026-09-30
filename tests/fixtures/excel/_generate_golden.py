# -*- coding: utf-8 -*-
"""Stage 5：生成 **完全合成、去标识化、确定性** 的 Golden Excel fixture。

设计原则（对应本轮要求）：
  * **绝不**复制真实业务 Excel（真实文件含买家昵称 / 收件人 / 电话 / 地址等 PII）；
  * **不使用** random / faker / 当前时间 / 机器环境 —— 全部为显式公式，任何人任何时间运行
    结果完全一致（可直接与下面的"人工可推算"注释对账）；
  * 结构对齐真实文件：**表头在第 1 行、第 2 行留空、数据从第 3 行开始**
    （因此 `row_excel_numbers` 从 3 开始，与线上一致）；
  * 生成物：`golden_small.xlsx`（19 行 × 2 Sheet）、`golden_medium.xlsx`（447 行 × 1 Sheet）。

运行：`python tests/fixtures/excel/_generate_golden.py`（幂等，可重复生成）

------------------------------------------------------------------------------
Golden A（golden_small.xlsx，Sheet「OrderSKUList」，19 个数据行 = Excel 行 3~21）
------------------------------------------------------------------------------
每行 i = 0..18（Excel 行 = i + 3）：

  Order Amount  = (i+1) * 10.00，**例外**：
                    i = 15 -> ''（空值）      => Excel 行 18 空值
                    i = 18 -> 'N/A'（非数值） => Excel 行 21 非数值
  Shipping Provider Name：
      SF International   -> i ∈ {0, 9, 15, 16}   => Excel 行 **3、12、18、19**（离散命中）
      JS Express International -> i ∈ {1..7}     （7 行）
      Yanwen Express     -> 其余 8 行（含 i=18）
  SKU ID（18 位数字，**重复**）：
      SKU 1 -> i ∈ {0,1,2} ; 2 -> {3,4} ; 3 -> {5,6} ; 4 -> {7,8,9} ; 5 -> {10..18}
  Quantity = (i % 5) + 1 ; Order ID（18 位，唯一）

人工可推算的期望值（由 tests/helpers/fixtures.py 固化，独立于被测实现）：
  * 全表：19 行；数值 17、空值 1、非数值 1；SUM(Order Amount) = 1550.00
  * contains 'SF'：matched_rows=4、numeric_rows=3、empty_rows=1、SUM = 10+100+170 = 280.00
  * 全表（无筛选）：matched_row_runs = [[3, 21]]（连续）
  * contains 'SF'：matched_row_runs = [[3,3], [12,12], [18,19]]（离散，**绝不**写成 3~19）
  * 按 SKU 分组 SUM：SKU5=1000、SKU4=270、SKU3=130、SKU2=90、SKU1=60
  * multi-step（SKU 分组 SUM 取前 3 再求和）= 1000 + 270 + 130 = **1400.00**
    （SKU5 = i 10..18 = 110+120+130+140+150+170+180，其中 i=15 空值、i=18 非数值不计入）

------------------------------------------------------------------------------
Golden B（golden_medium.xlsx，Sheet「OrderSKUList」，447 个数据行 = Excel 行 3~449）
------------------------------------------------------------------------------
每行 i = 0..446：

  Order ID   = '2000000000000' + '%05d' % (i+1)        （18 位、唯一）
  SKU ID     = '9000000000000' + '%05d' % (i % 120 + 1)（18 位、**120 个取值 ⇒ 大量重复**）
  Order Amount = round(10 + (i * 7 % 500) / 4.0, 2)     （全部可数值化）
  Quantity   = i % 5 + 1
  Shipping Provider Name = ['SF International','JS Express International','Yanwen Express'][i % 3]
        => SF 行数 = 149（i % 3 == 0）
  Created Time = '2026-07-%02d 08:%02d:00' % (i % 28 + 1, i % 60)
  Product Name = 'Synthetic Product %02d' % (i % 40)

  期望：total_matches = 447、全部 SKU 返回 447 行且**不去重**、Excel 行 3 ~ 449
"""
import os

from openpyxl import Workbook

HERE = os.path.dirname(os.path.abspath(__file__))

SMALL_COLUMNS = ['Order ID', 'SKU ID', 'Seller SKU', 'Order Amount', 'Quantity',
                 'Shipping Provider Name', 'Created Time', 'Order Status', 'Product Name']
MEDIUM_COLUMNS = ['Order ID', 'SKU ID', 'Order Amount', 'Quantity',
                  'Shipping Provider Name', 'Created Time', 'Product Name']

SF = 'SF International'
JS = 'JS Express International'
YW = 'Yanwen Express'

#: Golden A 的 SKU（18 位标识）序号（1..5）
SKU_BY_INDEX = {**{i: 1 for i in (0, 1, 2)}, **{i: 2 for i in (3, 4)},
                **{i: 3 for i in (5, 6)}, **{i: 4 for i in (7, 8, 9)},
                **{i: 5 for i in range(10, 19)}}
#: Golden A 的物流商
PROVIDER_BY_INDEX = {**{i: SF for i in (0, 9, 15, 16)},
                     **{i: JS for i in (1, 2, 3, 4, 5, 6, 7)},
                     **{i: YW for i in (8, 10, 11, 12, 13, 14, 17, 18)}}


def _num_id(prefix: str, n: int) -> str:
    """18 位数字标识（前 13 位固定 + 5 位序号）。"""
    return '%s%05d' % (prefix, n)


def _small_amount(i: int) -> str:
    if i == 15:
        return ''            # 空值
    if i == 18:
        return 'N/A'         # 非数值
    return '%.2f' % ((i + 1) * 10.0)


#: 第 2 行的合成"说明行"（对应真实文件里被解析器归入 preamble 的那一行）。
#: 该行为纯文本、长度 > DESC_MIN_AVG_TEXT_LEN(30) 且无数值（numeric_ratio=0），
#: 因此解析器会把它放进 preamble_rows 并从数据行中剔除 —— 数据因此从 Excel 第 3 行开始。
DESCRIPTION_ROW = ('本行是合成测试数据的说明行（Golden fixture，非业务数据，不含任何个人信息）；'
                   '数据从下一行开始。')


def _write_sheet(ws, columns, rows):
    """表头写第 1 行；第 2 行为说明行；数据从第 3 行开始（与线上真实文件结构一致）。"""
    for ci, name in enumerate(columns, start=1):
        ws.cell(row=1, column=ci, value=name)
    for ci in range(1, len(columns) + 1):
        ws.cell(row=2, column=ci, value=DESCRIPTION_ROW)
    for ri, row in enumerate(rows, start=3):
        for ci, value in enumerate(row, start=1):
            ws.cell(row=ri, column=ci, value=value)


def build_small() -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = 'OrderSKUList'
    rows = []
    for i in range(19):
        sku_no = SKU_BY_INDEX[i]
        rows.append([
            _num_id('2000000000000', i + 1),               # Order ID（18 位、唯一）
            _num_id('1000000000000', sku_no),              # SKU ID（18 位、重复）
            'SELLER-SKU-%02d' % sku_no,                    # Seller SKU
            _small_amount(i),                              # Order Amount（含空值/非数值）
            i % 5 + 1,                                     # Quantity
            PROVIDER_BY_INDEX[i],                          # Shipping Provider Name
            '2026-08-%02d 10:%02d:00' % (i % 28 + 1, i % 60),   # Created Time
            'Completed' if i % 2 == 0 else 'Shipped',      # Order Status
            'Synthetic Product %s' % chr(65 + sku_no - 1),  # Product Name
        ])
    _write_sheet(ws, SMALL_COLUMNS, rows)

    ws2 = wb.create_sheet('SkuMaster')
    master = [[_num_id('1000000000000', k), 'SELLER-SKU-%02d' % k,
               'Synthetic Product %s' % chr(65 + k - 1),
               'Category %s' % ('X' if k % 2 else 'Y')] for k in range(1, 6)]
    _write_sheet(ws2, ['SKU ID', 'Seller SKU', 'Product Name', 'Product Category'], master)
    return wb


def build_medium() -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = 'OrderSKUList'
    providers = [SF, JS, YW]
    rows = []
    for i in range(447):
        rows.append([
            _num_id('2000000000000', i + 1),
            _num_id('9000000000000', i % 120 + 1),
            round(10 + (i * 7 % 500) / 4.0, 2),
            i % 5 + 1,
            providers[i % 3],
            '2026-07-%02d 08:%02d:00' % (i % 28 + 1, i % 60),
            'Synthetic Product %02d' % (i % 40),
        ])
    _write_sheet(ws, MEDIUM_COLUMNS, rows)
    return wb


def main() -> None:
    build_small().save(os.path.join(HERE, 'golden_small.xlsx'))
    build_medium().save(os.path.join(HERE, 'golden_medium.xlsx'))
    print('已生成：golden_small.xlsx（19 行 / 2 Sheet）、golden_medium.xlsx（447 行）')


if __name__ == '__main__':
    main()
