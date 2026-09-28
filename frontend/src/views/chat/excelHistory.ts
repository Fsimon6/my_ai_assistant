/**
 * 第 3 项「结果与可解释性」：Excel 结构化结果的**历史恢复**（纯函数，可在 vitest 中直接测试）。
 *
 * 背景：实时查询时后端返回 `result / aggregate / group_aggregate / multi_step`，
 * 前端用 `buildExcelTable / buildExcelAgg / buildGroupAgg / buildMultiStep` 渲染卡片；
 * 但这些结构化结果此前**没有持久化**，重新打开历史会话只剩文字摘要。
 *
 * 现在 assistant 消息的 `meta_info['excel']` 会带一份**历史快照**（见
 * backend/api/v1/rag.py::build_excel_history_snapshot）。本模块负责：
 *   * 按 `schema_version / kind` 决定能否恢复（未知版本 -> 回退纯文字，旧历史天然兼容）；
 *   * 通过**注入的**既有 builder 生成与实时完全一致的视图模型（绝不重写第二套 builder）；
 *   * 只读展示：**不**恢复任何实时执行/分页上下文（`excelSessionId` / `new_context` 等一律不产生）。
 */
import type {
  ExcelAggregateResult,
  ExcelGroupedAggregateResult,
  ExcelMultiStepResult,
  ExcelNlQueryResult,
  ExcelQueryResult
} from '@/services/api'

/** 与后端 `EXCEL_HISTORY_SCHEMA_VERSION` 对齐；版本不认识 -> 不恢复（保持纯文字）。 */
export const EXCEL_HISTORY_SCHEMA_VERSION = 1

/** 历史快照（后端 meta_info['excel']） */
export interface ExcelHistorySnapshot {
  schema_version?: number
  kind?: 'result' | 'aggregate' | 'group_aggregate' | 'multi_step' | string
  document?: { document_id?: string; filename?: string }
  sheet?: { sheet_index?: number; sheet_name?: string }
  engine?: string
  inherited_from?: string
  relaxed_filters?: string[]
  continued?: boolean
  history_truncated?: boolean
  payload?: any
}

/** 恢复出的消息字段（只含展示所需字段；绝不包含实时执行状态） */
export interface RestoredExcelMessage {
  isExcelQuery?: boolean
  excelTable?: any
  excelAgg?: any
  excelGroupAgg?: any
  excelMultiStep?: any
}

/** 与 Chat.vue 中既有 builder 的签名保持一致（依赖注入，避免第二套实现） */
export interface ExcelBuilders {
  buildExcelTable: (
    r: ExcelQueryResult, documentName: string, continued?: boolean,
    relaxed?: string[], engine?: string
  ) => any
  buildExcelAgg: (a: ExcelAggregateResult, res: ExcelNlQueryResult) => any
  buildGroupAgg: (g: ExcelGroupedAggregateResult, res: ExcelNlQueryResult) => any
  buildMultiStep: (m: ExcelMultiStepResult, res: ExcelNlQueryResult) => any
}

/** 快照被裁剪时的显式提示（绝不假装结果是完整的） */
export const EXCEL_HISTORY_TRUNCATED_NOTICE = '（历史快照：仅保留当时返回结果的前若干行）'

/**
 * 从历史消息的 `meta_info` 恢复 Excel 结构化卡片字段。
 *
 * @returns 命中且版本可识别时返回 `{ isExcelQuery, excelXxx }`；否则返回 `{}`
 *          （旧历史 / 普通 chat / RAG / 未知版本 -> 调用方保持原有纯文字渲染）。
 */
export function restoreExcelHistory(
  metaInfo: any,
  builders: ExcelBuilders
): RestoredExcelMessage {
  if (!metaInfo || typeof metaInfo !== 'object') return {}
  if (metaInfo.source !== 'excel') return {}
  const snap: ExcelHistorySnapshot | null =
    metaInfo.excel && typeof metaInfo.excel === 'object' ? metaInfo.excel : null
  if (!snap) return {}
  if (snap.schema_version !== EXCEL_HISTORY_SCHEMA_VERSION) return {}
  const payload = snap.payload && typeof snap.payload === 'object' ? snap.payload : null
  if (!payload) return {}

  const documentName = snap.document?.filename || ''
  const relaxed = Array.isArray(snap.relaxed_filters) ? snap.relaxed_filters : []
  const continued = !!snap.continued
  // 供 buildExcelAgg / buildGroupAgg / buildMultiStep 复用的"类响应对象"：
  // 只带展示所需的来源/解释性字段，**不含**任何实时上下文。
  const resLike = {
    engine: snap.engine || '',
    inherited_from: snap.inherited_from || '',
    document: snap.document || {},
    relaxed_filters: relaxed,
    continued
  } as ExcelNlQueryResult

  const out: RestoredExcelMessage = { isExcelQuery: true }
  switch (snap.kind) {
    case 'result':
      out.excelTable = builders.buildExcelTable(
        payload as ExcelQueryResult, documentName, continued, relaxed, snap.engine || '')
      break
    case 'aggregate':
      out.excelAgg = builders.buildExcelAgg(payload as ExcelAggregateResult, resLike)
      break
    case 'group_aggregate':
      out.excelGroupAgg = builders.buildGroupAgg(
        payload as ExcelGroupedAggregateResult, resLike)
      break
    case 'multi_step':
      out.excelMultiStep = builders.buildMultiStep(payload as ExcelMultiStepResult, resLike)
      break
    default:
      return {}
  }

  if (snap.history_truncated) {
    if (out.excelTable) {
      out.excelTable.historyTruncated = true
      out.excelTable.rowRangeLabel = `${out.excelTable.rowRangeLabel || ''}${EXCEL_HISTORY_TRUNCATED_NOTICE}`
    }
    if (out.excelGroupAgg) {
      out.excelGroupAgg.historyTruncated = true
      out.excelGroupAgg.summaryLabel = `${out.excelGroupAgg.summaryLabel || ''}${EXCEL_HISTORY_TRUNCATED_NOTICE}`
    }
    if (out.excelMultiStep) {
      out.excelMultiStep.historyTruncated = true
      out.excelMultiStep.step1Summary = `${out.excelMultiStep.step1Summary || ''}${EXCEL_HISTORY_TRUNCATED_NOTICE}`
    }
    if (out.excelAgg) out.excelAgg.historyTruncated = true
  }
  return out
}
