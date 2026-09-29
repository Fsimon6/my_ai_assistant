/**
 * 第 3 项「结果与可解释性」：历史消息结构化结果恢复单测（vitest，离线，无网络）。
 *
 * 覆盖：四类 result 恢复、旧历史回退、chat/RAG 不受影响、版本兼容、
 *      builder 参数契约（前端依赖字段齐全）、传参透传（document/engine/relaxed/continued）、
 *      裁剪提示、以及"历史消息不获得实时能力"（无 excelSessionId / 分页上下文）。
 */
import { describe, expect, it, vi } from 'vitest'
import {
  groupRelaxedLabel,
  matchedRowsLabel,
  matchedRowsLine,
  multiStepSpanLabel,
  multiStepStep2InputLabel,
  multiStepStep2SourceLabel,
  tableRowRangeLabel
} from './excelCardLabels'
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

  it('R1：表格卡片「本页 Excel 行号」在历史恢复后同样正确（含分页窗口）', () => {
    const { builders, calls } = makeBuilders()
    const out = restoreExcelHistory({ source: 'excel', excel: snapshot('result', PAYLOADS.result) },
                                    builders)
    expect(out.excelTable).toBeTruthy()
    // 该快照 has_more=true（还有更多命中行）-> 追加"超过单次显示上限"提示（P1）
    expect(tableRowRangeLabel(calls.result![0]))
      .toBe('本页 Excel 行号 3 ~ 3｜共命中 19 行，本次返回 1 行（第 1~1 条，offset=0, limit=50）'
            + '｜结果超过单次显示上限（50 条），可继续说「下一页」继续获取')
  })

  it('R1：旧快照缺 offset/returned_count 时仍可恢复且不产生 NaN', () => {
    const { builders, calls } = makeBuilders()
    const legacy = { ...PAYLOADS.result } as Record<string, any>
    delete legacy.offset
    delete legacy.returned_count
    const out = restoreExcelHistory({ source: 'excel', excel: snapshot('result', legacy) }, builders)
    expect(out.excelTable).toBeTruthy()
    const label = tableRowRangeLabel(calls.result![0])
    expect(label.startsWith('本页 Excel 行号')).toBe(true)     // 不回退到歧义写法
    expect(label.includes('NaN')).toBe(false)
  })

  it('R4：历史恢复后第 2 步来源文案取**动态**分组数（不再展示旧 source_text）', () => {
    const { builders, calls } = makeBuilders()
    const out = restoreExcelHistory(
      { source: 'excel', excel: snapshot('multi_step', PAYLOADS.multi_step) }, builders)
    expect(out.excelMultiStep).toBeTruthy()
    const step2 = calls.multi![0].step2
    const label = multiStepStep2SourceLabel(step2)
    expect(label).toContain(`（${step2.input_rows} 个分组的聚合值）`)
    expect(label).toContain('不会重新回到原始 Excel 数据行')
    expect(label).not.toContain('TOP')
  })

  it('相邻 P2：全部命中行的连续段随历史恢复透传（实时==历史）', () => {
    const { builders, calls } = makeBuilders()
    const snap = { ...snapshot('result', PAYLOADS.result),
                   payload: { ...PAYLOADS.result, matched_row_runs: [[3, 3], [12, 12], [18, 19]],
                              matched_row_run_count: 3, matched_row_runs_truncated: false } }
    const out = restoreExcelHistory({ source: 'excel', excel: snap }, builders)
    expect(out.excelTable).toBeTruthy()
    const payload = calls.result![0]
    expect(payload.matched_row_runs).toEqual([[3, 3], [12, 12], [18, 19]])
    expect(matchedRowsLabel(payload)).toBe('全部命中 Excel 行：3、12、18~19')
  })

  it('相邻 P2：旧快照缺新字段 -> 正常恢复、不显示"全部命中行"（不猜）', () => {
    const { builders, calls } = makeBuilders()
    const legacy = { ...PAYLOADS.result } as Record<string, any>
    delete legacy.matched_row_runs
    delete legacy.matched_row_run_count
    delete legacy.matched_row_runs_truncated
    const out = restoreExcelHistory({ source: 'excel', excel: snapshot('result', legacy) },
                                    builders)
    expect(out.excelTable).toBeTruthy()
    expect(matchedRowsLabel(calls.result![0])).toBe('')
    expect(tableRowRangeLabel(calls.result![0])).toContain('本页 Excel 行号')   // 既有行仍在
  })

  it('相邻 P2：aggregate / group 的真实连续段随恢复透传（实时==历史）', () => {
    const { builders, calls } = makeBuilders()
    const runs = { matched_row_runs: [[3, 3], [12, 12], [18, 19]],
                   matched_row_run_count: 3, matched_row_runs_truncated: false }

    const outA = restoreExcelHistory(
      { source: 'excel', excel: snapshot('aggregate', { ...PAYLOADS.aggregate, ...runs,
                                                        matched_rows: 4 }) }, builders)
    expect(outA.excelAgg).toBeTruthy()
    expect(matchedRowsLine(calls.aggregate![0], { withCount: true, spanPrefix: '匹配 Excel 行' }))
      .toBe('匹配 Excel 行：3、12、18~19（共 4 行）')

    const outG = restoreExcelHistory(
      { source: 'excel', excel: snapshot('group_aggregate', { ...PAYLOADS.group_aggregate, ...runs,
                                                              matched_rows: 4 }) }, builders)
    expect(outG.excelGroupAgg).toBeTruthy()
    expect(matchedRowsLine(calls.group![0])).toBe('匹配 Excel 行：3、12、18~19')
  })

  it('相邻 P2：multi_step 第 1 步真实连续段随恢复透传（不写成 3 ~ 19）', () => {
    const { builders, calls } = makeBuilders()
    const snap = snapshot('multi_step', {
      ...PAYLOADS.multi_step,
      step1: { ...PAYLOADS.multi_step.step1,
               matched_row_runs: [[3, 3], [12, 12], [18, 19]],
               matched_row_run_count: 3, matched_row_runs_truncated: false }
    })
    const out = restoreExcelHistory({ source: 'excel', excel: snap }, builders)
    expect(out.excelMultiStep).toBeTruthy()
    const step1 = calls.multi![0].step1
    expect(step1.matched_row_runs).toEqual([[3, 3], [12, 12], [18, 19]])
    expect(multiStepSpanLabel(step1)).toBe('匹配 Excel 行：3、12、18~19')
  })

  it('相邻 P2：旧快照（无 runs）-> 正常恢复并逐字沿用既有包络文案', () => {
    const { builders, calls } = makeBuilders()
    const aggPayload = { ...PAYLOADS.aggregate } as Record<string, any>
    delete aggPayload.matched_row_runs
    delete aggPayload.matched_row_run_count
    delete aggPayload.matched_row_runs_truncated
    const out = restoreExcelHistory(
      { source: 'excel', excel: snapshot('aggregate', aggPayload) }, builders)
    expect(out.excelAgg).toBeTruthy()
    expect(matchedRowsLine(calls.aggregate![0], { withCount: true, spanPrefix: '匹配 Excel 行' }))
      .toBe('匹配 Excel 行：3 ~ 21（共 19 行）')          // 回退：与旧实现逐字一致
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
