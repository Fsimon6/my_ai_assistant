import type { AxiosResponse, AxiosInstance } from 'axios'
import axios from 'axios'
import type { UploadFile } from "element-plus"

// 创建axios实例
// 统一使用 VITE_API_BASE_URL：开发=http://localhost:8000，生产=空（同源 /api，由 nginx 反代）。
// 避免硬编码 localhost:8000 进入生产 bundle。
const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')
// 生产构建中 DEV 为 false，空 baseURL 时回退为同源相对 /api/v1；仅开发回退到 localhost:8000
const api: AxiosInstance = axios.create({
  baseURL: API_BASE_URL ? `${API_BASE_URL}/api/v1` : (import.meta.env.DEV ? 'http://localhost:8000/api/v1' : '/api/v1'),
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器：附加鉴权 token（与 utils/requests.ts 保持一致）
api.interceptors.request.use(
  (config: any) => {
    const token = localStorage.getItem('access_token')
    if (token) {
      config.headers = config.headers || {}
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// 响应拦截器
api.interceptors.response.use(
  (response: AxiosResponse) => {
    return response.data
  },
  (error) => {
    console.error('API请求错误：', error)
    return Promise.reject(error)
  }
)

// RAG 接口返回结构（响应拦截器已解包为 response.data）
export interface RagUploadResult {
  success?: boolean | string
  message?: string
  filename?: string
  total_chunks?: number
  [key: string]: any
}

export interface RagQueryResult {
  success?: boolean | string
  response?: string
  query?: string
  timestamp?: string
  sources?: any[]
  [key: string]: any
}

export interface RagCollectionInfoResult {
  success?: boolean | string
  collection_info?: any
  timestamp?: string
  [key: string]: any
}

// ===== Excel（Phase 1A）统一表示 / 预览结构 =====
export interface ExcelColumnMeta {
  name: string
  index: number
  excel_column: number
  excel_column_letter: string
  dtype: string
  header_row_excel?: number | null
  non_empty?: number
  null_count?: number
}

export interface ExcelSheetMeta {
  sheet_index: number
  sheet_name: string
  row_count: number
  column_count: number
  header_mode: string
  header_rows_excel: number[]
  header_depth: number
}

export interface ExcelActiveSheet {
  sheet_index: number
  sheet_name: string
  row_count: number
  column_count: number
  header_mode: string
  header_rows_excel: number[]
  header_depth: number
  excel_range?: Record<string, number>
  columns: ExcelColumnMeta[]
  column_names: string[]
  rows: any[][]
  row_excel_numbers: number[]
  preview_limit: number
  preview_count: number
  truncated: boolean
  warnings: string[]
}

export interface ExcelPreviewResult {
  success?: boolean | string
  document_id?: string
  filename?: string
  file_type?: string
  parser?: string
  sheet_count?: number
  total_rows?: number
  parse_ms?: number
  sheets?: ExcelSheetMeta[]
  active_sheet?: ExcelActiveSheet
  warnings?: string[]
  [key: string]: any
}

// ===== Excel 结构化查询（Phase 1B）=====
export type ExcelFilterOperator = 'eq' | 'contains'

export interface ExcelFilter {
  column: string
  operator: ExcelFilterOperator
  value: any
}

// Phase 4B：受控计算字段（一个运算符 + 两个真实列；由后端解析并渲染 SQL）
export interface ExcelCalculation {
  kind: 'calculation'
  operation: 'add' | 'sub' | 'mul' | 'div'
  operation_label: string
  symbol: string
  left_column: string
  left_index: number | null
  left_letter: string
  right_column: string
  right_index: number | null
  right_letter: string
  alias: string
  label: string
  is_calculated: boolean
}

export interface ExcelQueryParams {
  sheet_index?: number
  sheet_name?: string
  columns?: string[]
  filters?: ExcelFilter[]
  match_mode?: 'and'
  limit?: number
  offset?: number
  // 仅排障/差分验证使用：auto（默认，优先 DuckDB）| duckdb | python
  engine?: 'auto' | 'duckdb' | 'python'
}

export interface ExcelAppliedFilter {
  column: string
  resolved_column: string
  column_index: number
  column_letter: string
  operator: string
  value: any
}

// 自然语言表格查询（Phase 1C + 1D）返回结构
export interface ExcelPaginationInfo {
  mode: string                 // next | prev | range | start
  offset: number
  limit: number
  from_offset: number
  returned_count: number
  total_matches: number
  has_prev: boolean
  has_next: boolean
  error?: string
}

export interface ExcelNlQueryResult {
  success?: boolean | string
  status: 'ok' | 'clarify' | 'not_excel' | 'error'
  message?: string
  intent?: any
  turn?: any
  continued?: boolean
  // 实际执行引擎：duckdb（Phase 2 默认）| python
  engine?: 'duckdb' | 'python'
  // 条件放宽说明（eq 无精确匹配时按唯一前缀放宽为 contains；为空表示未放宽）
  relaxed_filters?: string[]
  // Phase 3A：单值统计结果（与 result 互斥）
  aggregate?: ExcelAggregateResult | null
  // Phase 3B：分组统计结果（与 aggregate / result 互斥）
  group_aggregate?: ExcelGroupedAggregateResult | null
  // Phase 4A：两步分析结果（与上面三者互斥）
  multi_step?: ExcelMultiStepResult | null
  // 统计是否继承了上一轮条件：'' | 'aggregate' | 'query' | 'analysis'
  inherited_from?: string
  pagination?: ExcelPaginationInfo | null
  document?: { document_id: string; filename: string; file_type?: string }
  sheet?: { sheet_index: number; sheet_name: string }
  query?: any
  result?: ExcelQueryResult
  candidates?: string[]
  stage?: string
  details?: any
}

// 统计结果（Phase 3A）：COUNT / SUM / AVG / MIN / MAX
// 分组统计（Phase 3B）：GROUP BY 结果
export interface ExcelGroupCell {
  column: string
  value: any
  display: string        // 展示文本由后端决定（NULL -> '<空>'）
  is_null: boolean
}

export interface ExcelGroupRow {
  group: ExcelGroupCell[]
  group_key: any[]
  group_display: string[]
  value: number | null
  value_display: string
  matched_rows: number
  numeric_rows: number
  empty_rows: number
  non_numeric_rows: number
}

export interface ExcelGroupedAggregateResult {
  kind: 'group_aggregate'
  document_id: string
  sheet_index: number
  sheet_name: string
  operation: 'count' | 'sum' | 'avg' | 'min' | 'max'
  operation_label?: string
  column: string | null
  column_index?: number | null
  column_letter: string | null
  group_by: { name: string; index: number; letter: string }[]
  filters: ExcelAppliedFilter[]
  applied_filters: ExcelAppliedFilter[]
  rows: ExcelGroupRow[]
  total_groups: number
  returned_groups: number
  matched_rows: number
  total_rows_in_sheet: number
  row_excel_spans: { first: number; last: number } | null
  column_numeric_in_sheet?: number
  definition: string
  // Phase 3C：排序 + TOP-N（顺序由 DuckDB 决定，前端不得重排）
  sorted: boolean
  order_by: string | null
  order_by_label: string
  order_by_column: string | null
  order_dir: 'asc' | 'desc' | null
  order_dir_label: string
  top_n: number | null
  truncated_by_top_n: boolean
  sort_description: string
  engine?: 'duckdb' | 'python'
  // Phase 4B：受控计算字段（无则为 null）
  calculation?: ExcelCalculation | null
}

// Phase 4A：两步分析（Step 1 分组统计 + TOP-N → Step 2 对前 N 名再聚合）
// 所有数值、顺序、来源说明均由后端给出；前端只展示，不计算、不排序、不重新聚合。
export interface ExcelMultiStepStep1 {
  type: 'group_aggregate'
  // 后端仅在能定位 Sheet 时返回（multi_step.py: if sheet_name: payload['sheet_name']=...）
  sheet_name?: string
  sheet_index?: number
  group_by: { name: string; index: number; letter: string }[]
  operation: 'count' | 'sum' | 'avg' | 'min' | 'max'
  operation_label?: string
  column: string | null
  calculation?: ExcelCalculation | null
  filters: ExcelAppliedFilter[]
  applied_filters: ExcelAppliedFilter[]
  sorted: boolean
  order_by: string | null
  order_by_label: string
  order_dir: 'asc' | 'desc' | null
  top_n: number | null
  total_groups: number
  returned_groups: number
  truncated_by_top_n: boolean
  matched_rows: number
  total_rows_in_sheet: number
  rows: ExcelGroupRow[]
  sort_description: string
  source: string
}

export interface ExcelMultiStepStep2 {
  type: 'aggregate'
  operation: 'count' | 'sum' | 'avg' | 'min' | 'max'
  operation_label?: string
  source: string
  source_text: string
  input_rows: number
  numeric_rows: number
  value: number | null
  value_display: string
}

export interface ExcelMultiStepResult {
  kind: 'multi_step'
  document_id: string
  filename: string
  sheet_index: number
  sheet_name: string
  engine: 'duckdb' | 'python'
  max_steps: number
  step_count: number
  plan: { steps: any[]; max_steps: number }
  step1: ExcelMultiStepStep1
  step2: ExcelMultiStepStep2
  value: number | null
  value_display: string
  step2_input_values: (number | null)[]
  definition: string
}

export interface ExcelAggregateResult {
  document_id: string
  sheet_index: number
  sheet_name: string
  operation: 'count' | 'sum' | 'avg' | 'min' | 'max'
  operation_label?: string
  column: string | null
  column_index: number | null
  column_letter: string | null
  filters: ExcelAppliedFilter[]
  applied_filters: ExcelAppliedFilter[]
  value: number | null
  value_display: string
  matched_rows: number
  numeric_rows: number
  empty_rows: number
  non_numeric_rows: number
  total_rows_in_sheet: number
  row_excel_numbers: number[]
  row_excel_spans: { first: number; last: number } | null
  row_excel_truncated: boolean
  column_numeric_in_sheet: number
  definition: string
  engine?: 'duckdb' | 'python'
  // Phase 4B：受控计算字段（无则为 null）
  calculation?: ExcelCalculation | null
}

export interface ExcelQueryResult {
  success?: boolean | string
  document_id: string
  filename?: string
  file_type?: string
  // 实际执行引擎：duckdb（Phase 2 默认）| python
  engine?: 'duckdb' | 'python'
  sheet_index: number
  sheet_name: string
  columns: ExcelColumnMeta[]
  rows: any[][]
  row_excel_numbers: number[]
  row_ranges: string[]
  total_rows_in_sheet: number
  total_matches: number
  returned_count: number
  limit: number
  offset: number
  has_more: boolean
  next_offset: number | null
  applied_filters: ExcelAppliedFilter[]
  // Phase 4B：计算字段与"对计算值筛选"（无则为 null）
  calculation?: ExcelCalculation | null
  calc_filter?: { operator: string; value: number } | null
}

// RAG API
export const ragApi = {
  // 上传文档
  uploadDocument: async (file: File, metadata?: any): Promise<RagUploadResult> => {
    const formData = new FormData()
    formData.append('file', file)
    if (metadata) {
      formData.append('metadata', JSON.stringify(metadata))
    }
    const res = await api.post('/rag/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
    return res as unknown as RagUploadResult
  },

  // 查询文档（可选 documentId 限定只在该文档内检索）
  queryDocument: async (query: string, stream: boolean = false, contextCount: number = 3, documentId?: string): Promise<RagQueryResult> => {
    const res = await api.post('/rag/query', { query, stream, context_count: contextCount, document_id: documentId })
    return res as unknown as RagQueryResult
  },

  // 获取单个文档全部分块内容（按 document_id，供预览；服务端 user_id 隔离）
  getDocument: async (documentId: string): Promise<{ success?: boolean; document_id?: string; chunks?: any[]; total?: number }> => {
    const res = await api.get(`/rag/documents/${documentId}`)
    return res as unknown as { success?: boolean; document_id?: string; chunks?: any[]; total?: number }
  },

  // Excel 基础预览（Phase 1A）：直接读取服务端 Unified Representation，不重新解析原文件
  getExcelPreview: async (documentId: string, sheetIndex: number = 0, limit: number = 20): Promise<ExcelPreviewResult> => {
    const res = await api.get(`/rag/excel/${documentId}/preview`, {
      params: { sheet_index: sheetIndex, limit }
    })
    return res as unknown as ExcelPreviewResult
  },

  // Excel 结构化统计（Phase 3A / 3B）：COUNT / SUM / AVG / MIN / MAX（可选 group_by），由 DuckDB 计算
  aggregateExcel: async (documentId: string, payload: {
    operation: 'count' | 'sum' | 'avg' | 'min' | 'max'
    column?: string | null
    group_by?: string[]
    order_by?: string | null
    order_dir?: 'asc' | 'desc' | null
    top_n?: number | null
    sheet_index?: number
    sheet_name?: string
    filters?: ExcelFilter[]
    engine?: 'auto' | 'duckdb' | 'python'
  }): Promise<ExcelAggregateResult | ExcelGroupedAggregateResult> => {
    const res = await api.post(`/rag/excel/${documentId}/aggregate`, payload)
    return res as unknown as ExcelAggregateResult | ExcelGroupedAggregateResult
  },

  // Excel 结构化查询（Phase 1B）：服务端在 representation.json 上做精确筛选/分页，不经 LLM、不经 Chroma
  queryExcel: async (documentId: string, payload: ExcelQueryParams): Promise<ExcelQueryResult> => {
    const res = await api.post(`/rag/excel/${documentId}/query`, payload)
    return res as unknown as ExcelQueryResult
  },

  // 自然语言表格查询（Phase 1C + 1D + 3 + 4A）：LLM 只解析意图/动作 → 后端 schema 校验 → 精确取数
  // session_id 用于把「上一轮结构化查询上下文」绑定到当前会话，支持「再来20条 / 下一页」
  // 注意：Phase 4A（两步分析）在一次请求内可能发生 1~2 次 LLM 调用，
  // 慢模型下会超过默认 30s；这里为该接口单独放宽超时（仅此接口，不影响其它请求）。
  nlQueryExcel: async (message: string, characterId?: string, sessionId?: string): Promise<ExcelNlQueryResult> => {
    const res = await api.post('/rag/excel/nl-query', {
      message,
      character_id: characterId,
      session_id: sessionId
    }, { timeout: 180000 })
    return res as unknown as ExcelNlQueryResult
  },

  // 带历史查询
  queryWithHistory: async (query: string, history: any[], stream: boolean = false): Promise<RagQueryResult> => {
    const res = await api.post('/rag/query-with-history', { query, history, stream })
    return res as unknown as RagQueryResult
  },

  // 获取集合信息
  getCollectionInfo: async (): Promise<RagCollectionInfoResult> => {
    const res = await api.get('/rag/collection-info')
    return res as unknown as RagCollectionInfoResult
  },

  // 删除文档（按 document_id 精确删除，自动携带 JWT）
  deleteDocument: async (documentIds: string[]): Promise<any> => {
    const res = await api.delete('/rag/documents', {
      data: { document_ids: documentIds }
    })
    return res as unknown as any
  },

  // 获取当前用户的真实文档列表（按 user_id 隔离，聚合自向量库 metadata）
  getDocuments: async (): Promise<{ success?: boolean; documents?: any[]; total?: number }> => {
    const res = await api.get('/rag/documents')
    return res as unknown as { success?: boolean; documents?: any[]; total?: number }
  }
}

// Characters API
export const charactersApi = {
  // 与角色对话
  speakToCharacter: async (characterId: string, message: string, stream: boolean = false) => {
    return api.post(`/characters/${characterId}/speak`, {
      message,
      stream
    })
  },

  // 流式对话
  speakToCharacterStream: async (characterId: string, message: string) => {
    return api.post(`/characters/${characterId}/speak/stream`, {
      message,
      stream: true
    })
  }
}

// 系统API
export const systemApi = {
  // 健康检查
  healthCheck: async () => {
    return api.get('/health')
  },

  // 系统信息
  getSystemInfo: async () => {
    return api.get('/')
  }
}

export default api
