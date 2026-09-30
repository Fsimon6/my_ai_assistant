/**
 * Stage 5：Excel 历史恢复**Golden 契约**（前端侧）。
 *
 * 与后端 `tests/unit/test_excel_golden_contract.py` 使用**同一份**快照 fixture
 * （`tests/fixtures/excel_history/*.json`，由后端真实管线生成后固定入库），
 * 因此前后端通过同一稳定结构对接：
 *
 *    后端：真实结果 -> build_excel_history_snapshot ->（fixture）-> persist -> get_history
 *    前端：fixture（meta_info['excel']）-> restoreExcelHistory() -> 卡片视图模型
 *
 * 覆盖：table / aggregate / group_aggregate / multi_step 四类恢复，
 *      以及"旧快照缺 Stage 3 新字段 -> 正常降级（绝不报错）"。
 * 说明：builder 仍由测试注入（复用真实纯函数标签），与 `excelHistory.spec.ts` 同约定；
 *      只断言稳定字段，不比较 message id / 时间戳 / narrative 全文。
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
import { EXCEL_HISTORY_SCHEMA_VERSION, restoreExcelHistory, type ExcelBuilders } from './excelHistory'

// 共享 fixture（单一事实来源；用 ?raw 避免 tsconfig 的 JSON 解析/路径限制）
import tableRaw from '../../../../tests/fixtures/excel_history/table.json?raw'
import aggregateRaw from '../../../../tests/fixtures/excel_history/aggregate.json?raw'
import groupRaw from '../../../../tests/fixtures/excel_history/group_aggregate.json?raw'
import multiRaw from '../../../../tests/fixtures/excel_history/multi_step.json?raw'
import legacyTableRaw from '../../../../tests/fixtures/excel_history/legacy_table.json?raw'
import legacyMultiRaw from '../../../../tests/fixtures/excel_history/legacy_multi_step.json?raw'

const FIXTURES: Record<string, any> = {
  table: JSON.parse(tableRaw),
  aggregate: JSON.parse(aggregateRaw),
  group_aggregate: JSON.parse(groupRaw),
  multi_step: JSON.parse(multiRaw),
  legacy_table: JSON.parse(legacyTableRaw),
  legacy_multi_step: JSON.parse(legacyMultiRaw)
}

/** 与 Chat.vue 的 builder 同构：用真实纯函数标签拼装视图模型（不做第二套业务逻辑） */
function makeBuilders() {
  const calls: Record<string, any[]> = {}
  const builders: ExcelBuilders = {
    buildExcelTable: vi.fn((r: any, doc: string, continued = false, relaxed: string[] = [],
                            engine = '') => {
      calls.table = [r, doc, continued, relaxed, engine]
      return {
        kind: 'table',
        document: doc,
        rowRangeLabel: tableRowRangeLabel(r),
        matchedRowsLabel: matchedRowsLabel(r),
        rows: r.rows?.length ?? 0,
        totalMatches: r.total_matches
      }
    }),
    buildExcelAgg: vi.fn((a: any, res: any) => {
      calls.aggregate = [a, res]
      return {
        kind: 'agg',
        valueDisplay: a.value_display,
        matchedLabel: `${a.matched_rows} 行，其中可数值化 ${a.numeric_rows} 行`,
        filtersLabel: (a.applied_filters || []).map((f: any) => `${f.column} 包含 ${f.value}`)
          .join('；'),
        relaxedLabel: groupRelaxedLabel(res),
        spanLabel: matchedRowsLine(a, { withCount: true, spanPrefix: '匹配 Excel 行' })
      }
    }),
    buildGroupAgg: vi.fn((g: any, res: any) => {
      calls.group = [g, res]
      return {
        kind: 'group',
        summaryLabel: `共 ${g.total_groups} 个分组`,
        spanLabel: matchedRowsLine(g),
        relaxedLabel: groupRelaxedLabel(res),
        groupCount: g.rows?.length ?? 0
      }
    }),
    buildMultiStep: vi.fn((m: any, res: any) => {
      calls.multi = [m, res]
      return {
        kind: 'multi',
        valueDisplay: m.value_display,
        spanLabel: multiStepSpanLabel(m.step1),
        step2SourceLabel: multiStepStep2SourceLabel(m.step2),
        step2InputLabel: multiStepStep2InputLabel(m.step2),
        definition: m.definition
      }
    })
  }
  return { builders, calls }
}

function restore(kind: string, snap: any = FIXTURES[kind]) {
  return restoreExcelHistory({ source: 'excel', excel: snap }, makeBuilders().builders)
}

describe('Stage 5 Golden 契约：table 恢复', () => {
  it('恢复表格卡片：行范围/全部命中行（真实连续段）与行数一致', () => {
    const out = restore('table')
    expect(out.isExcelQuery).toBe(true)
    expect(out.excelTable).toBeTruthy()
    expect(out.excelTable.rowRangeLabel).toContain('本页 Excel 行号 3 ~ 21')
    expect(out.excelTable.matchedRowsLabel).toBe('全部命中 Excel 行：3 ~ 21')
    expect(out.excelTable.rows).toBe(19)
    expect(out.excelTable.totalMatches).toBe(19)
  })

  it('实时与历史同一 payload -> 同一标签（恢复不改内容）', () => {
    const snap = FIXTURES.table
    const out = restore('table')
    expect(out.excelTable.rowRangeLabel).toBe(tableRowRangeLabel(snap.payload))
    expect(out.excelTable.matchedRowsLabel).toBe(matchedRowsLabel(snap.payload))
  })
})

describe('Stage 5 Golden 契约：aggregate 恢复（含放宽说明）', () => {
  it('恢复统计卡片：值/匹配行/最终条件/放宽说明/离散行区间', () => {
    const out = restore('aggregate')
    expect(out.excelAgg).toBeTruthy()
    expect(out.excelAgg.valueDisplay).toBe('280')
    expect(out.excelAgg.matchedLabel).toBe('4 行，其中可数值化 3 行')
    expect(out.excelAgg.filtersLabel).toBe('Shipping Provider Name 包含 SF')   // 最终执行条件
    expect(out.excelAgg.relaxedLabel).toContain('已按前缀放宽为「包含 SF」')      // 放宽过程单独说明
    expect(out.excelAgg.spanLabel).toBe('匹配 Excel 行：3、12、18~19（共 4 行）')
  })
})

describe('Stage 5 Golden 契约：group_aggregate 恢复', () => {
  it('恢复分组卡片：分组数/连续行区间/无放宽说明', () => {
    const out = restore('group_aggregate')
    expect(out.excelGroupAgg).toBeTruthy()
    expect(out.excelGroupAgg.summaryLabel).toBe('共 3 个分组')
    expect(out.excelGroupAgg.groupCount).toBe(3)
    expect(out.excelGroupAgg.spanLabel).toBe('匹配 Excel 行：3 ~ 21')
    expect(out.excelGroupAgg.relaxedLabel).toBe('')
  })
})

describe('Stage 5 Golden 契约：multi_step 恢复', () => {
  it('恢复多步卡片：最终值/step1 行区间/step2 输入与来源', () => {
    const out = restore('multi_step')
    expect(out.excelMultiStep).toBeTruthy()
    expect(out.excelMultiStep.valueDisplay).toBe('1400')
    expect(out.excelMultiStep.spanLabel).toBe('匹配 Excel 行：3 ~ 21')
    expect(out.excelMultiStep.step2InputLabel).toBe('（输入 3 个分组，其中可数值化 3 个）')
    expect(out.excelMultiStep.step2SourceLabel)
      .toBe('数据来源：第 1 步的结果（3 个分组的聚合值），不会重新回到原始 Excel 数据行')
    expect(out.excelMultiStep.definition).toBeTruthy()
  })
})

describe('Stage 5 Golden 契约：旧快照降级（绝不因新增字段而失败）', () => {
  it('旧 table 快照缺 matched_row_runs -> 卡片仍恢复，只是不显示行分布', () => {
    const out = restore('legacy_table')
    expect(out.excelTable).toBeTruthy()
    expect(out.excelTable.rowRangeLabel).toContain('本页 Excel 行号 3 ~ 21')
    expect(out.excelTable.matchedRowsLabel).toBe('')          // 无 runs -> 不显示（不猜）
  })

  it('旧 multi_step 快照缺 step1 的 runs -> 行区间回退既有包络文案', () => {
    const out = restore('legacy_multi_step')
    expect(out.excelMultiStep).toBeTruthy()
    expect(out.excelMultiStep.spanLabel).toBe('匹配 Excel 行区间：3 ~ 21')
    expect(out.excelMultiStep.step2InputLabel).toBe('（输入 3 个分组，其中可数值化 3 个）')
  })

  it('未知 schema_version -> 不恢复（保持纯文字），且不抛错', () => {
    const out = restore('table', { ...FIXTURES.table, schema_version: 999 })
    expect(out).toEqual({})
    expect(EXCEL_HISTORY_SCHEMA_VERSION).toBe(1)
  })

  it('缺少 payload / 非 excel 来源 -> 不恢复', () => {
    expect(restoreExcelHistory({ source: 'chat' }, makeBuilders().builders)).toEqual({})
    expect(restore('table', { ...FIXTURES.table, payload: null })).toEqual({})
  })
})
