/**
 * 第 3 项 P2：卡片文案纯函数单测（G1 行区间 / G2 可数值化个数 / G5 条件放宽说明）。
 * 离线、无网络；只验证文案规则与"旧快照缺字段时优雅降级"。
 */
import { describe, expect, it } from 'vitest'
import {
  groupRelaxedLabel,
  matchedRowsLabel,
  multiStepSpanLabel,
  multiStepStep2InputLabel,
  multiStepStep2SourceLabel,
  tableRowRangeLabel
} from './excelCardLabels'

describe('G1 multiStepSpanLabel', () => {
  it('有真实行区间 -> 复用分组卡片的既有格式', () => {
    expect(multiStepSpanLabel({ row_excel_spans: { first: 3, last: 21 } }))
      .toBe('匹配 Excel 行区间：3 ~ 21')
  })

  it('行号被裁剪时显式标注（不假装完整）', () => {
    expect(multiStepSpanLabel({ row_excel_spans: { first: 3, last: 21 }, row_excel_truncated: true }))
      .toBe('匹配 Excel 行区间：3 ~ 21（行号已按上限裁剪）')
  })

  it('旧快照无该字段 / 无命中（None）-> 空串（不渲染、不报错）', () => {
    expect(multiStepSpanLabel({})).toBe('')
    expect(multiStepSpanLabel({ row_excel_spans: null })).toBe('')
    expect(multiStepSpanLabel(undefined)).toBe('')
    expect(multiStepSpanLabel({ row_excel_spans: { first: 3 } })).toBe('')
  })
})

describe('G2 multiStepStep2InputLabel', () => {
  it('可数值化个数正常显示', () => {
    expect(multiStepStep2InputLabel({ input_rows: 3, numeric_rows: 3 }))
      .toBe('（输入 3 个分组，其中可数值化 3 个）')
  })

  it('numeric_rows = 0 也必须显示为 0（不得被 if(numeric_rows) 隐藏）', () => {
    expect(multiStepStep2InputLabel({ input_rows: 0, numeric_rows: 0 }))
      .toBe('（输入 0 个分组，其中可数值化 0 个）')
    expect(multiStepStep2InputLabel({ input_rows: 3, numeric_rows: 0 }))
      .toBe('（输入 3 个分组，其中可数值化 0 个）')
  })

  it('旧快照缺 numeric_rows -> 省略该子句（优雅降级）', () => {
    expect(multiStepStep2InputLabel({ input_rows: 3 })).toBe('（输入 3 个分组）')
  })

  it('连 input_rows 都没有 -> 空串', () => {
    expect(multiStepStep2InputLabel({})).toBe('')
    expect(multiStepStep2InputLabel(undefined)).toBe('')
  })
})

describe('R1 tableRowRangeLabel（本页行号语义）', () => {
  const page = (offset: number, nums: number[], returned = nums.length, total = 19, limit = 5) => ({
    row_excel_numbers: nums, offset, returned_count: returned, total_matches: total, limit
  })

  it('第一页：明确写「本页 Excel 行号」', () => {
    expect(tableRowRangeLabel(page(0, [3, 4, 5, 6, 7])))
      .toBe('本页 Excel 行号 3 ~ 7｜共命中 19 行，本次返回 5 行（第 1~5 条，offset=0, limit=5）')
  })

  it('第二页：本页行号与分页窗口都正确（数值/分页逻辑不变）', () => {
    const label = tableRowRangeLabel(page(5, [8, 9, 10, 11, 12]))
    expect(label).toBe('本页 Excel 行号 8 ~ 12｜共命中 19 行，本次返回 5 行（第 6~10 条，offset=5, limit=5）')
    expect(label.includes('Excel 行号 8 ~ 12')).toBe(true)
    expect(label.startsWith('本页 Excel 行号')).toBe(true)   // 不得退回歧义写法
  })

  it('末页部分返回：本次返回行数与序号窗口按实际值显示', () => {
    expect(tableRowRangeLabel(page(15, [18, 19, 20, 21], 4, 19, 5)))
      .toBe('本页 Excel 行号 18 ~ 21｜共命中 19 行，本次返回 4 行（第 16~19 条，offset=15, limit=5）')
  })

  it('无匹配行：沿用既有文案（不含本页行号）', () => {
    expect(tableRowRangeLabel(page(0, [], 0, 0, 5)))
      .toBe('无匹配行（起点为第 1 条，共命中 0 行）')
  })

  it('旧快照缺 offset/returned_count/limit 时优雅降级（不产生 NaN）', () => {
    const label = tableRowRangeLabel({ row_excel_numbers: [3, 4], total_matches: 2 })
    expect(label).toBe('本页 Excel 行号 3 ~ 4｜共命中 2 行，本次返回 2 行（第 1~2 条，offset=0, limit=undefined）')
    expect(label.includes('NaN')).toBe(false)
  })

  it('P1：还有更多命中行（has_more=true）必须明说"超过单次显示上限"', () => {
    const label = tableRowRangeLabel({ ...page(0, [3, 4, 5]), has_more: true })
    expect(label).toContain('结果超过单次显示上限（5 条）')
    expect(label).toContain('可继续说「下一页」继续获取')
    expect(label).toContain('共命中 19 行')          // 总数仍然如实显示
  })

  it('P1 对照：has_more=false / 缺该字段时不追加提示', () => {
    expect(tableRowRangeLabel({ ...page(0, [3, 4, 5]), has_more: false }))
      .toBe('本页 Excel 行号 3 ~ 5｜共命中 19 行，本次返回 3 行（第 1~3 条，offset=0, limit=5）')
    expect(tableRowRangeLabel(page(0, [3, 4, 5])))
      .not.toContain('单次显示上限')
  })
})

describe('相邻 P2 matchedRowsLabel（全部命中 Excel 行，真实连续段）', () => {
  it('连续命中 -> 单段区间', () => {
    expect(matchedRowsLabel({ matched_row_runs: [[3, 449]], matched_row_run_count: 1,
                              matched_row_runs_truncated: false, total_matches: 447 }))
      .toBe('全部命中 Excel 行：3 ~ 449')
  })

  it('不连续命中 -> 保留真实不连续性（绝不合并成包络）', () => {
    const label = matchedRowsLabel({ matched_row_runs: [[3, 3], [12, 12], [18, 19]],
                                     matched_row_run_count: 3,
                                     matched_row_runs_truncated: false, total_matches: 4 })
    expect(label).toBe('全部命中 Excel 行：3、12、18~19')
    expect(label).not.toContain('3 ~ 19')      // 不得伪造连续范围
  })

  it('单行 -> 单值', () => {
    expect(matchedRowsLabel({ matched_row_runs: [[7, 7]], matched_row_run_count: 1,
                              matched_row_runs_truncated: false, total_matches: 1 }))
      .toBe('全部命中 Excel 行：7')
  })

  it('空结果 / 无字段（旧快照或本页非全量）-> 空串（不显示该行）', () => {
    expect(matchedRowsLabel({ matched_row_runs: [], matched_row_run_count: 0,
                              matched_row_runs_truncated: false })).toBe('')
    expect(matchedRowsLabel({})).toBe('')
    expect(matchedRowsLabel(undefined)).toBe('')
  })

  it('段数被截断 -> 明确"共 N 个命中行、分布在 M 段"（不假装列全）', () => {
    const runs = [[1, 1], [3, 3], [5, 5], [7, 7], [9, 9], [11, 11], [13, 13], [15, 15]]
    expect(matchedRowsLabel({ matched_row_runs: runs, matched_row_run_count: 87,
                              matched_row_runs_truncated: true, total_matches: 447 }))
      .toBe('全部命中 Excel 行：共 447 个命中行，分布在 87 段｜范围 1 ~ 15')
  })
})

describe('R4 multiStepStep2SourceLabel（自然中文 + 动态数量）', () => {
  it('动态带出本步实际输入的分组数', () => {
    expect(multiStepStep2SourceLabel({ input_rows: 3 }))
      .toBe('数据来源：第 1 步的结果（3 个分组的聚合值），不会重新回到原始 Excel 数据行')
    expect(multiStepStep2SourceLabel({ input_rows: 7 })).toContain('（7 个分组的聚合值）')
  })

  it('保留关键语义：不回原始 Excel 数据行；且不含固定 TOP 文案', () => {
    const label = multiStepStep2SourceLabel({ input_rows: 3 })
    expect(label).toContain('第 1 步的结果')
    expect(label).toContain('不会重新回到原始 Excel 数据行')
    expect(label).not.toContain('TOP')
    expect(label).not.toContain('本阶段')
  })

  it('数量不是写死的 3：不同输入得到不同文案', () => {
    expect(multiStepStep2SourceLabel({ input_rows: 5 }))
      .not.toBe(multiStepStep2SourceLabel({ input_rows: 3 }))
  })

  it('旧快照缺 input_rows 时不报错（退化为不含数量）', () => {
    expect(multiStepStep2SourceLabel({})).toBe('数据来源：第 1 步的结果，不会重新回到原始 Excel 数据行')
    expect(multiStepStep2SourceLabel(undefined)).toContain('不会重新回到原始 Excel 数据行')
  })
})

describe('G5 groupRelaxedLabel', () => {
  it('有放宽说明 -> 与表格/单聚合卡片同样的连接格式', () => {
    expect(groupRelaxedLabel({ relaxed_filters: ['物流商 eq SF → contains SF'] }))
      .toBe('物流商 eq SF → contains SF')
    expect(groupRelaxedLabel({ relaxed_filters: ['a', 'b'] })).toBe('a；b')
  })

  it('空 / 缺失 -> 空串（不渲染说明行）', () => {
    expect(groupRelaxedLabel({ relaxed_filters: [] })).toBe('')
    expect(groupRelaxedLabel({})).toBe('')
    expect(groupRelaxedLabel(undefined)).toBe('')
  })
})
