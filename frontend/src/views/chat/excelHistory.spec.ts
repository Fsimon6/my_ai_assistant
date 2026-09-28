/**
 * 第 3 项「结果与可解释性」：历史消息结构化结果恢复单测（vitest，离线，无网络）。
 *
 * 覆盖：四类 result 恢复、旧历史回退、chat/RAG 不受影响、版本兼容、
 *      builder 参数契约（前端依赖字段齐全）、传参透传（document/engine/relaxed/continued）、
 *      裁剪提示、以及"历史消息不获得实时能力"（无 excelSessionId / 分页上下文）。
 */
import { describe, expect, it, vi } from 'vitest'
import { groupRelaxedLabel, multiStepSpanLabel, multiStepStep2InputLabel } from './excelCardLabels'
import {
  EXCEL_HISTORY_SCHEMA_VERSION,
  EXCEL_HISTORY_TRUNCATED_NOTICE,
  restoreExcelHistory,
  type ExcelBuilders
} from './excelHistory'

/** 记录调用参数的假 builder（只做参数捕获，不做任何业务判断） */
function makeBuilders() {
  const calls: Record<string, any[]> = {}
  const builders: ExcelBuilders = {
    buildExcelTable: vi.fn((...args: any[]) => {
      calls.result = args
      return { kind: 'table', rowRangeLabel: 'Excel 行号 3 ~ 21', data: [] }
    }),
    buildExcelAgg: vi.fn((...args: any[]) => {
      calls.aggregate = args
      return { kind: 'agg', valueDisplay: '306.02' }
    }),
    buildGroupAgg: vi.fn((...args: any[]) => {
      calls.group = args
      return { kind: 'group', summaryLabel: '共 3 个分组' }
    }),
    buildMultiStep: vi.fn((...args: any[]) => {
      calls.multi = args
      return { kind: 'multi', step1Summary: '共 15 个分组', value_display: '106.6' }
    })
  }
  return { builders, calls }
}

function snapshot(kind: string, extra: Record<string, any> = {}) {
  return {
    schema_version: EXCEL_HISTORY_SCHEMA_VERSION,
    kind,
    document: { document_id: 'DOC1', filename: '直邮一店 8.20号订单.xlsx' },
    sheet: { sheet_index: 0, sheet_name: 'OrderSKUList' },
    engine: 'duckdb',
    inherited_from: '',
    relaxed_filters: ['物流商 eq SF → contains SF'],
    continued: false,
    history_truncated: false,
    payload: { sheet_name: 'OrderSKUList', ...extra }
  }
}

const PAYLOADS: Record<string, any> = {
  result: {
    columns: [{ name: 'Order ID', index: 0, excel_column: 1, excel_column_letter: 'A' }],
    rows: [['577531902952575236', '59.32']],
    row_excel_numbers: [3], row_ranges: ['3~3'],
    offset: 0, limit: 50, returned_count: 1, total_matches: 19, total_rows_in_sheet: 19,
    has_more: true, next_offset: 1,
    applied_filters: [{ column: 'Shipping Provider Name', operator: 'contains', value: 'SF' }],
    calculation: null, calc_filter: null
  },
  aggregate: {
    operation: 'sum', operation_label: '求和', column: 'Order Amount', column_letter: 'AA',
    value: 306.02, value_display: '306.02', matched_rows: 19, numeric_rows: 19,
    empty_rows: 0, non_numeric_rows: 0, total_rows_in_sheet: 19,
    row_excel_numbers: [3, 4, 5], row_excel_spans: { first: 3, last: 21 },
    filters: [{ column: 'Shipping Provider Name', operator: 'contains', value: 'SF' }],
    definition: 'SUM(Order Amount)'
  },
  group_aggregate: {
    kind: 'group', operation: 'sum', operation_label: '求和', column: 'Order Amount',
    group_by: [{ name: 'Shipping Provider Name', index: 9, excel_column: 10 }],
    rows: [{ group_key: ['SF International'], group_display: ['SF International'],
             value: 66.3, value_display: '66.3', matched_rows: 4, numeric_rows: 4 }],
    total_groups: 3, returned_groups: 1, top_n: 1, order_by: 'aggregate_value',
    order_by_label: '聚合值', order_dir: 'desc', order_dir_label: '从高到低', sorted: true,
    sort_description: '按聚合值从高到低', truncated_by_top_n: true, matched_rows: 19,
    total_rows_in_sheet: 19, row_excel_spans: { first: 3, last: 21 },
    applied_filters: [{ column: 'Quantity', operator: 'gt', value: 3 }],
    definition: 'SUM(Order Amount) GROUP BY Shipping Provider Name'
  },
  multi_step: {
    kind: 'multi_step', engine: 'duckdb', filename: '直邮一店 8.20号订单.xlsx',
    step_count: 2, max_steps: 2, value: 106.6, value_display: '106.6',
    step1: {
      operation: 'sum', operation_label: '求和', column: 'Order Amount',
      group_by: [{ name: 'SKU ID', index: 1, excel_column: 2 }],
      rows: [{ group_key: ['S1'], group_display: ['S1'], value: 59.32,
               value_display: '59.32', matched_rows: 2, numeric_rows: 2 }],
      total_groups: 15, returned_groups: 3, top_n: 3, order_by_label: '聚合值',
      order_dir: 'desc', sorted: true, sort_description: '按聚合值从高到低',
      truncated_by_top_n: true, matched_rows: 19, total_rows_in_sheet: 19,
      row_excel_spans: { first: 3, last: 21 },
      filters: [{ column: 'Shipping Provider Name', operator: 'contains', value: 'SF' }]
    },
    step2: { operation: 'sum', operation_label: '求和', source: 'step_1', input_rows: 3,
             numeric_rows: 3, value: 106.6, value_display: '106.6' },
    step2_input_values: [59.32, 35.33, 33.2],
    plan: { max_steps: 2, steps: [] },
    definition: '两步分析'
  }
}

describe('restoreExcelHistory', () => {
  it('10. 新 result 历史 -> excelTable 恢复（含来源/行号/筛选/引擎透传）', () => {
    const { builders, calls } = makeBuilders()
    const out = restoreExcelHistory(
      { source: 'excel', excel: snapshot('result', PAYLOADS.result) }, builders)
    expect(out.excelTable).toBeTruthy()
    expect(out.isExcelQuery).toBe(true)
    const [payload, documentName, continued, relaxed, engine] = calls.result!
    expect(documentName).toBe('直邮一店 8.20号订单.xlsx')     // 来源文件
    expect(engine).toBe('duckdb')                             // 引擎
    expect(relaxed).toEqual(['物流商 eq SF → contains SF'])   // 条件放宽说明
    expect(continued).toBe(false)
    for (const key of ['columns', 'rows', 'row_excel_numbers', 'offset', 'limit',
                       'returned_count', 'total_matches', 'total_rows_in_sheet',
                       'applied_filters']) {
      expect(payload).toHaveProperty(key)                     // builder 依赖字段齐全
    }
  })

  it('11. 新 aggregate 历史 -> excelAgg 恢复', () => {
    const { builders, calls } = makeBuilders()
    const out = restoreExcelHistory(
      { source: 'excel', excel: snapshot('aggregate', PAYLOADS.aggregate) }, builders)
    expect(out.excelAgg).toBeTruthy()
    const [payload, resLike] = calls.aggregate!
    expect(payload.operation).toBe('sum')
    expect(resLike.document.filename).toBe('直邮一店 8.20号订单.xlsx')
    expect(resLike.engine).toBe('duckdb')
  })

  it('12. 新 group aggregate 历史 -> excelGroupAgg 恢复', () => {
    const { builders, calls } = makeBuilders()
    const out = restoreExcelHistory(
      { source: 'excel', excel: snapshot('group_aggregate', PAYLOADS.group_aggregate) }, builders)
    expect(out.excelGroupAgg).toBeTruthy()
    const [payload] = calls.group!
    expect(payload.group_by[0].name).toBe('Shipping Provider Name')
    expect(payload.rows[0].value_display).toBe('66.3')
    expect(payload.top_n).toBe(1)
  })

  it('13. 新 multi-step 历史 -> excelMultiStep 恢复', () => {
    const { builders, calls } = makeBuilders()
    const out = restoreExcelHistory(
      { source: 'excel', excel: snapshot('multi_step', PAYLOADS.multi_step) }, builders)
    expect(out.excelMultiStep).toBeTruthy()
    const [payload] = calls.multi!
    expect(payload.step1.group_by[0].name).toBe('SKU ID')
    expect(payload.step2.input_rows).toBe(3)
    expect(payload.value_display).toBe('106.6')
  })

  it('14. 旧历史（meta_info 只有 source）-> 不回恢复，保持纯文字', () => {
    const { builders } = makeBuilders()
    for (const info of [{ source: 'excel' }, {}, null, undefined, { source: 'excel', excel: null }]) {
      const out = restoreExcelHistory(info, builders)
      expect(out).toEqual({})
      expect(out.excelTable).toBeUndefined()
      expect(out.excelAgg).toBeUndefined()
      expect(out.excelGroupAgg).toBeUndefined()
      expect(out.excelMultiStep).toBeUndefined()
    }
    expect(builders.buildExcelTable).not.toHaveBeenCalled()
  })

  it('15/16. 普通 chat / RAG 历史（source 非 excel）-> 行为不变', () => {
    const { builders } = makeBuilders()
    expect(restoreExcelHistory({ source: 'chat' }, builders)).toEqual({})
    expect(restoreExcelHistory({ source: 'rag' }, builders)).toEqual({})
    // 即便携带了 excel 键，但 source 不是 excel -> 依然不动（严格隔离）
    expect(restoreExcelHistory(
      { source: 'chat', excel: snapshot('result', PAYLOADS.result) }, builders)).toEqual({})
  })

  it('17. 历史消息不获得实时能力（无会话/分页/查询上下文）', () => {
    const { builders, calls } = makeBuilders()
    const out = restoreExcelHistory(
      { source: 'excel', excel: snapshot('result', PAYLOADS.result) }, builders)
    expect(Object.keys(out).sort()).toEqual(['excelTable', 'isExcelQuery'])
    const serialized = JSON.stringify(out)
    for (const forbidden of ['excelSessionId', 'new_context', 'new_aggregate_context',
                             'new_analysis_context', 'session_key', 'pagination', 'next_offset_context']) {
      expect(serialized).not.toContain(forbidden)
    }
    // builder 收到的 payload 里也不含任何实时上下文
    expect(JSON.stringify(calls.result)).not.toContain('new_context')
  })

  it('未知 schema_version -> 不回恢复（向前兼容，保持纯文字）', () => {
    const { builders } = makeBuilders()
    const snap = snapshot('result', PAYLOADS.result)
    snap.schema_version = 99
    expect(restoreExcelHistory({ source: 'excel', excel: snap }, builders)).toEqual({})
  })

  it('历史快照被裁剪 -> 显式提示（不假装完整）', () => {
    const { builders, calls } = makeBuilders()
    const snap = snapshot('group_aggregate', PAYLOADS.group_aggregate)
    snap.history_truncated = true
    const out = restoreExcelHistory({ source: 'excel', excel: snap }, builders)
    expect(out.excelGroupAgg.historyTruncated).toBe(true)
    expect(out.excelGroupAgg.summaryLabel).toContain(EXCEL_HISTORY_TRUNCATED_NOTICE)
    expect(calls.group).toBeTruthy()

    const snap2 = snapshot('result', PAYLOADS.result)
    snap2.history_truncated = true
    const out2 = restoreExcelHistory({ source: 'excel', excel: snap2 }, builders)
    expect(out2.excelTable.rowRangeLabel).toContain(EXCEL_HISTORY_TRUNCATED_NOTICE)
  })

  it('G1/G2：新快照的 step1 行区间与 step2 可数值化个数随恢复透传给 builder', () => {
    const { builders, calls } = makeBuilders()
    const snap = snapshot('multi_step', {
      ...PAYLOADS.multi_step,
      step1: { ...PAYLOADS.multi_step.step1,
               row_excel_spans: { first: 3, last: 21 }, row_excel_truncated: false },
      step2: { ...PAYLOADS.multi_step.step2, numeric_rows: 3 }
    })
    const out = restoreExcelHistory({ source: 'excel', excel: snap }, builders)
    expect(out.excelMultiStep).toBeTruthy()
    const [payload] = calls.multi!
    expect(payload.step1.row_excel_spans).toEqual({ first: 3, last: 21 })
    expect(payload.step2.numeric_rows).toBe(3)
    expect(multiStepSpanLabel(payload.step1)).toBe('匹配 Excel 行区间：3 ~ 21')
    expect(multiStepStep2InputLabel(payload.step2)).toBe('（输入 3 个分组，其中可数值化 3 个）')
  })

  it('旧快照（v1，缺 G1/G2 新字段）-> 仍正常恢复且文案优雅降级', () => {
    const { builders, calls } = makeBuilders()
    // 显式构造"旧快照"：删除 G1/G2 引入的可选增量字段
    const legacyStep1 = { ...PAYLOADS.multi_step.step1 } as Record<string, any>
    const legacyStep2 = { ...PAYLOADS.multi_step.step2 } as Record<string, any>
    delete legacyStep1.row_excel_spans
    delete legacyStep1.row_excel_truncated
    delete legacyStep2.numeric_rows
    const out = restoreExcelHistory(
      { source: 'excel',
        excel: snapshot('multi_step', { ...PAYLOADS.multi_step,
                                       step1: legacyStep1, step2: legacyStep2 }) }, builders)
    expect(out.excelMultiStep).toBeTruthy()               // 不报错、不丢卡片
    const [payload] = calls.multi!
    expect(payload.step1.row_excel_spans).toBeUndefined()
    expect(multiStepSpanLabel(payload.step1)).toBe('')                     // 不渲染行区间行
    expect(multiStepStep2InputLabel(payload.step2)).toBe('（输入 3 个分组）')  // 省略子句
  })

  it('G5：group_aggregate 的 relaxed_filters 随恢复透传（历史卡片同样显示放宽说明）', () => {
    const { builders, calls } = makeBuilders()
    const snap = {
      ...snapshot('group_aggregate', PAYLOADS.group_aggregate),
      relaxed_filters: ['物流商 eq SF → contains SF']
    }
    const out = restoreExcelHistory({ source: 'excel', excel: snap }, builders)
    expect(out.excelGroupAgg).toBeTruthy()
    const [, resLike] = calls.group!
    expect(resLike.relaxed_filters).toEqual(['物流商 eq SF → contains SF'])
    expect(groupRelaxedLabel(resLike)).toBe('物流商 eq SF → contains SF')
  })

  it('实时与历史使用同一 builder（注入）—— 相同 payload 得到相同视图模型', () => {
    // 用一个"确定性假 builder"，模拟实时 builder 的输出仅由入参决定
    const real = (r: any, doc: string, cont: boolean, relaxed: string[], engine: string) =>
      ({ kind: 'table', doc, cont, relaxed, engine, columns: r.columns, rows: r.rows })
    const builders: ExcelBuilders = {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      buildExcelTable: real, buildExcelAgg: () => ({}), buildGroupAgg: () => ({}),
      buildMultiStep: () => ({})
    } as ExcelBuilders
    const payload = PAYLOADS.result
    const snap = snapshot('result', payload)
    // 实时调用与历史恢复使用**同一组入参**（快照的 document/engine/relaxed/continued）
    const live = real(payload, snap.document.filename, snap.continued,
                      snap.relaxed_filters, snap.engine)
    const restored = restoreExcelHistory({ source: 'excel', excel: snap }, builders).excelTable
    expect(restored).toEqual(live)      // 实时 == 历史展示（同 payload 同 builder）
  })
})
