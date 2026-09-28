/**
 * 第 3 项 P2：卡片文案纯函数单测（G1 行区间 / G2 可数值化个数 / G5 条件放宽说明）。
 * 离线、无网络；只验证文案规则与"旧快照缺字段时优雅降级"。
 */
import { describe, expect, it } from 'vitest'
import { groupRelaxedLabel, multiStepSpanLabel, multiStepStep2InputLabel } from './excelCardLabels'

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
