<template>
  <div class="knowledge-base">
    <!-- 顶部标题 -->
    <div class="page-header">
      <h1>知识库管理</h1>
      <p>上传和管理您的文档，构建智能知识库</p>
    </div>

    <div class="content-container">
      <!-- 左侧：上传区域 -->
      <div class="upload-section">
        <el-card class="upload-card">
          <template #header>
            <div class="card-header">
              <span class="header-title">上传文档</span>
            </div>
          </template>

          <!-- 上传组件 -->
          <file-uploader @file-uploaded="handleFileUploaded" />

          <!-- 上传历史 -->
          <div class="upload-history" v-if="uploadHistory.length > 0">
            <h3>最近上传</h3>
            <el-timeline>
              <el-timeline-item
                v-for="item in uploadHistory.slice(0, 5)"
                :key="item"
                :timestamp="formatTime(item.timestamp)"
                placement="top"
              >
                <el-card>
                  <div class="history-item">
                    <div class="file-info">
                      <el-icon><Document /></el-icon>
                      <span class="filename">{{ item.filename }}</span>
                    </div>
                    <div class="file-stats">
                      <el-tag size="small" :type="item.status === 'success' ? 'success' : 'danger'">
                        {{ item.status === 'success' ? '成功' : '失败' }}
                      </el-tag>
                      <span class="chunks-count">{{ item.chunks }} chunk</span>
                    </div>
                  </div>
                </el-card>
              </el-timeline-item>
            </el-timeline>
          </div>
        </el-card>
      </div>

      <!-- 右侧：文档列表 -->
      <div class="document-section">
        <el-card class="document-card">
          <template #header>
            <div class="card-header">
              <span class="header-title">文档列表</span>
              <div class="header-actions">
                <el-button type="primary" size="small" @click="refreshDocuments">
                  <el-icon><Refresh /></el-icon>
                  刷新
                </el-button>
              </div>
            </div>
          </template>

          <!-- 操作栏 -->
          <div class="search-bar">
            <el-input
              v-model="searchQuery"
              placeholder="搜索文档..."
              clearable
              @clear="clearSearch"
            >
              <template #prefix>
                <el-icon><Search /></el-icon>
              </template>
            </el-input>
          </div>

          <!-- 文档列表 -->
          <div class="documents-list">
            <el-table
              :data="filteredDocuments"
              style="width: 100%"
              empty-text="暂无文档，请先上传"
            >
              <el-table-column
                prop="filename"
                label="文件名"
                width="250"
              >
                <template #default="scope">
                  <div class="filename-cell">
                    <el-icon class="file-icon">
                      <component :is="getFileIcon(scope.row.type)" />
                    </el-icon>
                    <span class="filename-text">{{ scope.row.filename }}</span>
                  </div>
                </template>
              </el-table-column>

              <el-table-column
                prop="type"
                label="类型"
                width="100"
              >
                <template #default="scope">
                  <el-tag size="small">{{ scope.row.type.toUpperCase() }}</el-tag>
                  <el-tag
                    v-if="scope.row.kind === 'excel'"
                    size="small"
                    type="success"
                    style="margin-left: 6px">
                    表格
                  </el-tag>
                </template>
              </el-table-column>

              <el-table-column
                prop="size"
                label="大小"
                width="120"
              >
                <template #default="scope">
                  {{ formatFileSize(scope.row.size) }}
                </template>
              </el-table-column>

              <el-table-column
                prop="chunks"
                label="分数段"
                width="100" />

              <el-table-column
                prop="uploadTime"
                label="上传时间"
                width="180" />

              <el-table-column
                label="操作"
                width="200"
                fixed="right">
                <template #default="scope">
                  <div class="action-buttons">
                    <el-button
                      type="primary"
                      size="small"
                      @click="queryDocument(scope.row)"
                    >
                      查询
                    </el-button>
                    <el-button
                      type="info"
                      size="small"
                      @click="previewDocument(scope.row)"
                    >
                      预览
                    </el-button>
                    <el-button
                      type="danger"
                      size="small"
                      @click="deleteDocument(scope.row)"
                    >
                      删除
                    </el-button>
                  </div>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <!-- 统计信息 -->
          <div class="stats-info">
            <el-row :gutter="20">
              <el-col :span="6">
                <el-statistic title="文档总数" :value="documents.length" />
              </el-col>
              <el-col :span="6">
                <el-statistic title="总分段数" :value="totalChunks" />
              </el-col>
              <el-col :span="6">
                <el-statistic title="知识库大小" :value="totalSize" />
              </el-col>
              <el-col :span="6">
                <el-statistic title="最后更新" :value="lastUpdate" />
              </el-col>
            </el-row>
          </div>
        </el-card>

        <!-- 快捷查询 -->
        <el-card class="quick-query-card">
          <template #header>
            <div class="card-header">
              <span class="header-title">快速查询</span>
            </div>
          </template>

          <div class="quick-query-form">
            <el-input
              v-model="quickQuery"
              type="textarea"
              :rows="2"
              placeholder="输入您的问题，快速查询知识库..."
            />
            <div class="query-actions">
              <el-button type="primary" @click="handleQuickQuery">
                查询
              </el-button>
              <el-button
                @click="clearQuickQuery">
                清空
              </el-button>
            </div>

            <!-- 查询结果 -->
            <div v-if="queryResult" class="query-result">
              <h4>查询结果：</h4>
              <div class="result-content">
                {{ queryResult }}
              </div>
            </div>
          </div>
        </el-card>
      </div>
    </div>

    <!-- 文档预览对话框 -->
    <el-dialog
      v-model="previewVisible"
      :title="`预览：${previewDoc?.filename || ''}`"
      width="60%">
      <div v-loading="previewLoading">
        <div v-if="!previewLoading && previewChunks.length === 0" class="empty-tip">
          该文档暂无可预览的分块内容
        </div>
        <div v-for="chunk in previewChunks" :key="chunk.index" class="chunk-block">
          <div class="chunk-index">第 {{ chunk.index + 1 }} 段</div>
          <pre class="chunk-content">{{ chunk.content }}</pre>
        </div>
      </div>
    </el-dialog>

    <!-- 表格预览对话框（Phase 1A：数据直接来自服务端 Unified Representation，不重新解析原文件） -->
    <el-dialog
      v-model="excelVisible"
      :title="`表格预览：${excelDoc?.filename || ''}`"
      width="82%"
      top="6vh">
      <div v-loading="excelLoading" class="excel-preview">
        <div v-if="excelPreview" class="excel-meta">
          <el-tag size="small" type="success">{{ (excelPreview.file_type || '').toUpperCase() }}</el-tag>
          <span>共 {{ excelPreview.sheet_count }} 个 Sheet</span>
          <span v-if="excelPreview.parser">解析器：{{ excelPreview.parser }}</span>
          <span v-if="excelPreview.parse_ms != null">解析耗时：{{ excelPreview.parse_ms }} ms</span>
        </div>

        <el-tabs
          v-if="excelPreview && (excelPreview.sheets?.length || 0) > 0"
          :model-value="excelActiveSheetIndex"
          @tab-change="handleExcelTabChange">
          <el-tab-pane
            v-for="s in excelPreview.sheets"
            :key="s.sheet_index"
            :name="String(s.sheet_index)"
            :label="`${s.sheet_name}（${s.row_count}行×${s.column_count}列）`" />
        </el-tabs>

        <div v-if="excelPreview?.active_sheet" class="excel-sheet-info">
          <div class="excel-sheet-line">
            Sheet：<strong>{{ excelPreview.active_sheet.sheet_name }}</strong>
            ｜ {{ excelPreview.active_sheet.row_count }} 行 × {{ excelPreview.active_sheet.column_count }} 列
            ｜ 表头模式：{{ headerModeLabel(excelPreview.active_sheet.header_mode) }}
            ｜ 表头行（Excel）：{{ (excelPreview.active_sheet.header_rows_excel || []).join(', ') || '无' }}
          </div>
          <div v-if="excelPreview.active_sheet.truncated" class="excel-tip">
            仅显示前 {{ excelPreview.active_sheet.preview_count }} 行（共 {{ excelPreview.active_sheet.row_count }} 行）
          </div>
          <div v-if="(excelPreview.active_sheet.warnings || []).length" class="excel-warn">
            <div v-for="(w, i) in excelPreview.active_sheet.warnings" :key="i">· {{ w }}</div>
          </div>
        </div>

        <!-- 结构化查询面板（Phase 1B）：服务端在 representation.json 上精确筛选，不经 LLM / Chroma -->
        <div v-if="excelPreview?.active_sheet" class="excel-query-panel">
          <div class="query-row">
            <span class="query-label">返回列</span>
            <el-select
              v-model="excelQueryColumns"
              multiple
              collapse-tags
              collapse-tags-tooltip
              filterable
              size="small"
              placeholder="留空 = 全部列"
              style="min-width: 260px">
              <el-option
                v-for="c in sheetColumnOptions"
                :key="c.index"
                :label="c.name"
                :value="c.name" />
            </el-select>

            <span class="query-label">limit</span>
            <el-input-number
              v-model="excelQueryLimit"
              :min="1"
              :max="500"
              size="small"
              controls-position="right"
              style="width: 110px" />
            <span class="query-label">offset</span>
            <el-input-number
              v-model="excelQueryOffset"
              :min="0"
              size="small"
              controls-position="right"
              style="width: 130px" />

            <el-button type="primary" size="small" :loading="excelQueryLoading" @click="runExcelQuery(false)">查询</el-button>
            <el-button size="small" @click="clearExcelQuery">清空</el-button>
          </div>

          <div v-for="(f, fi) in excelQueryFilters" :key="fi" class="query-row">
            <span class="query-label">条件{{ fi + 1 }}</span>
            <el-select v-model="f.column" filterable placeholder="选择列" size="small" style="min-width: 200px">
              <el-option v-for="c in sheetColumnOptions" :key="c.index" :label="c.name" :value="c.name" />
            </el-select>
            <el-select v-model="f.operator" size="small" style="width: 110px">
              <el-option label="等于" value="eq" />
              <el-option label="包含" value="contains" />
              <el-option label="为空" value="is_null" />
            </el-select>
            <el-input
              v-if="f.operator !== 'is_null'"
              v-model="f.value"
              size="small"
              placeholder="值"
              style="width: 220px"
              @keyup.enter="runExcelQuery(false)" />
            <el-button size="small" text @click="removeQueryFilter(fi)">删除</el-button>
          </div>

          <div class="query-row">
            <el-button size="small" text @click="addQueryFilter">+ 添加条件</el-button>
            <span class="query-hint">
              多条件为 AND；等于=精确比较（业务 ID 字符串安全），包含=子串（大小写不敏感）；
              查询按当前 offset 执行，翻页用首页/上一页/下一页
            </span>
          </div>

          <div v-if="excelQueryError" class="excel-warn">{{ excelQueryError }}</div>
        </div>

        <!-- 查询结果概览 + 分页 -->
        <div v-if="excelQueryActive && excelQueryResult" class="excel-query-meta">
          <span>共匹配 <strong>{{ excelQueryResult.total_matches }}</strong> 行</span>
          <span>本次返回 {{ excelQueryResult.returned_count }} 行</span>
          <span>offset {{ excelQueryResult.offset }} / limit {{ excelQueryResult.limit }}</span>
          <span v-if="(excelQueryResult.row_excel_numbers || []).length">
            Excel 行 {{ excelQueryResult.row_excel_numbers[0] }} ~
            {{ excelQueryResult.row_excel_numbers[excelQueryResult.row_excel_numbers.length - 1] }}
          </span>
          <el-button size="small" text :disabled="excelQueryResult.offset <= 0" @click="queryFirstPage">首页</el-button>
          <el-button size="small" text :disabled="excelQueryResult.offset <= 0" @click="queryPrevPage">上一页</el-button>
          <el-button size="small" text :disabled="!excelQueryResult.has_more" @click="queryNextPage">下一页</el-button>
        </div>

        <!-- 统一结果表：Preview 与 Query 复用同一渲染，避免两套重复数据逻辑 -->
        <el-table
          v-if="tableColumns.length"
          :data="excelTableData"
          size="small"
          border
          height="380"
          style="width: 100%">
          <el-table-column type="index" label="Excel 行" width="90" :index="excelRowIndex" />
          <el-table-column
            v-for="(col, ci) in tableColumns"
            :key="ci"
            :prop="String(ci)"
            :label="col.name"
            min-width="150"
            show-overflow-tooltip>
            <template #default="scope">
              <span>{{ formatCell(scope.row[String(ci)]) }}</span>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="!excelLoading && !excelPreview" class="empty-tip">
          暂无表格预览数据
        </div>
      </div>
    </el-dialog>

    <!-- 按文档查询对话框 -->
    <el-dialog
      v-model="queryVisible"
      :title="`在文档内查询：${queryTargetDoc?.filename || ''}`"
      width="60%">
      <el-input
        v-model="docQueryInput"
        type="textarea"
        :rows="3"
        placeholder="输入要在这篇文档中查询的问题（Ctrl+Enter 发送）"
        @keyup.ctrl.enter="submitDocQuery"
      />
      <div class="query-actions" style="margin-top: 12px;">
        <el-button type="primary" :loading="docQueryLoading" @click="submitDocQuery">
          查询
        </el-button>
      </div>
      <div v-if="docQueryResult" class="query-result" style="margin-top: 16px;">
        <h4>查询结果：</h4>
        <div class="result-content">{{ docQueryResult }}</div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted} from "vue"
import { ElMessage, ElMessageBox } from "element-plus"
import {
  Document,
  Refresh,
  Search,
  Folder,
  Ticket,
  Picture,
  VideoCamera,
  Files,
  Grid
} from "@element-plus/icons-vue"
import FileUploader from "@/components/chat/FileUploader.vue"
import { ragApi } from '@/services/api'
import type { ExcelPreviewResult, ExcelQueryResult } from '@/services/api'

// 数据
const documents = ref<any[]>([])
const uploadHistory = ref<any[]>([])
const searchQuery = ref('')
const quickQuery = ref('')
const queryResult = ref('')

// 文档预览（拉取该文档全部分块原文）
const previewVisible = ref(false)
const previewDoc = ref<any>(null)
const previewChunks = ref<any[]>([])
const previewLoading = ref(false)

// 按文档查询（限定只在该文档内检索）
const queryVisible = ref(false)
const queryTargetDoc = ref<any>(null)
const docQueryInput = ref('')
const docQueryResult = ref('')
const docQueryLoading = ref(false)

// 表格预览（Phase 1A：来自服务端 Unified Representation）
const excelVisible = ref(false)
const excelDoc = ref<any>(null)
const excelLoading = ref(false)
const excelPreview = ref<ExcelPreviewResult | null>(null)
const excelActiveSheetIndex = ref('0')

// 结构化查询（Phase 1B）：数据源同 Preview（服务端 representation.json），不经 LLM / Chroma
interface QueryFilterRow {
  column: string
  operator: 'eq' | 'contains' | 'is_null'
  value: string
}
const excelQueryActive = ref(false)
const excelQueryResult = ref<ExcelQueryResult | null>(null)
const excelQueryLoading = ref(false)
const excelQueryError = ref('')
const excelQueryColumns = ref<string[]>([])
const excelQueryLimit = ref(50)
const excelQueryOffset = ref(0)
const excelQueryFilters = ref<QueryFilterRow[]>([{ column: '', operator: 'eq', value: '' }])

// 计算属性
const filteredDocuments = computed(() => {
  if (!searchQuery.value) return documents.value
  return documents.value.filter(doc => doc.filename.toLowerCase().includes(searchQuery.value.toLowerCase()))
})

const totalChunks = computed(() => {
  return documents.value.reduce((sum, doc) => sum + (doc.chunks || 0), 0)
})

const totalSize = computed(() => {
  const total = documents.value.reduce((sum, doc) => sum + (doc.size || 0), 0)
  return formatFileSize(total)
})

const lastUpdate = computed(() => {
  if (documents.value.length === 0) return '无'
  const latest = Math.max(...documents.value.map(d => new Date(d.uploadTime).getTime()))
  return new Date(latest).toLocaleDateString()
})

// 文件图标映射
const fileIcons = {
  'pdf': Ticket,
  'txt': Files,
  'docx': Document,
  'doc': Document,
  'md': Files,
  'xlsx': Grid,
  'xls': Grid,
  'csv': Grid,
  'tsv': Grid,
  'default': Folder
}

// 生命周期
onMounted(() => {
  loadDocuments()
  loadCollectionInfo()
})

// 方法
const getFileIcon = (fileType: string) => {
  const t = (fileType || '').toLowerCase()
  if (t.includes('pdf')) return fileIcons.pdf
  if (['xlsx', 'xls', 'csv', 'tsv'].includes(t)) return fileIcons.xlsx
  if (t.includes('text')) return fileIcons.txt
  if (t.includes('document')) return fileIcons.docx
  if (t.includes('markdown')) return fileIcons.md
  return fileIcons.default
}

const formatFileSize = (bytes: number) => {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

const formatTime = (timestamp: string) => {
  return new Date(timestamp).toLocaleDateString()
}

const loadDocuments = async () => {
  try {
    // 真实调用后端：按当前用户隔离的文档列表（不再使用 mock）
    const res = await ragApi.getDocuments()
    documents.value = (res.documents || []).map((d: any) => ({
      id: d.document_id,
      document_id: d.document_id,
      filename: d.filename,
      type: d.type,
      kind: d.kind || 'document',
      sheet_count: d.sheet_count || 0,
      size: d.size || 0,
      chunks: d.chunks || 0,
      uploadTime: d.created_at ? new Date(d.created_at).toLocaleString() : ''
    }))
  } catch (error) {
    console.error('加载文档失败:', error)
    ElMessage.error('加载文档失败')
  }
}

const loadCollectionInfo = async () => {
  try {
    const info = await ragApi.getCollectionInfo()
    console.log('集合信息：', info)
  } catch (error) {
    console.warn('获取集合信息失败：', error)
  }
}

const handleFileUploaded = async (fileData: any) => {
  try {
    // 调用上传API
    const result = await ragApi.uploadDocument(fileData.file, {
      description: fileData.description
    })

    const docId = (result as any).document_id
    // 稳定性补丁：把"后端真实结果"回写给上传组件（否则失败时列表仍显示"上传成功"）
    fileData.done?.(true)

    // 重新拉取真实文档列表（含后端聚合的 created_at/chunks），保证与删除闭环一致
    await loadDocuments()

    // 添加到上传历史（含 document_id）
    uploadHistory.value.unshift({
      id: docId || Date.now(),
      document_id: docId,
      filename: fileData.name,
      status: 'success',
      chunks: (result as any).total_chunks || 0,
      timestamp: new Date().toISOString()
    })

    ElMessage.success(`文件"${fileData.name}" 上传成功`)
  } catch (error: any) {
    console.error('文件上传失败：', error)

    uploadHistory.value.unshift({
      id: Date.now(),
      filename: fileData.name,
      status: 'error',
      chunks: 0,
      timestamp: new Date().toISOString()
    })

    // 稳定性补丁：把后端分类后的可读原因展示给用户
    // （detail 可能是字符串，也可能是 {error_code, message}；绝不显示 provider 原始报文）
    const detail = error?.response?.data?.detail
    const rawMessage = typeof detail === 'string' ? detail : detail?.message
    const errorCode = typeof detail === 'object' && detail ? detail.error_code : ''
    const text = rawMessage || (error?.response ? '文件上传失败' : '网络错误，请检查后端服务是否启动')

    // 回写失败结果：上传列表不再显示"上传成功"
    fileData.done?.(false, errorCode ? `${text}（${errorCode}）` : text)
  }
}

const handleQuickQuery = async () => {
  if (!quickQuery.value.trim()) {
    ElMessage.warning('请输入查询内容')
    return
  }

  try {
    const response = await ragApi.queryDocument(quickQuery.value, false)
    queryResult.value = response.response || '无相关结果'
    ElMessage.success('查询完成')
  } catch (error) {
    console.error('查询失败：', error)
    queryResult.value = '查询失败，请重试'
    ElMessage.error('查询失败')
  }
}

// 预览文档：表格走 Excel 预览；普通文档拉取全部分块原文（服务端按 user_id 隔离）
const previewDocument = async (doc: any) => {
  if (doc.kind === 'excel') {
    await openExcelPreview(doc)
    return
  }

  previewDoc.value = doc
  previewChunks.value = []
  previewVisible.value = true
  previewLoading.value = true
  try {
    const res = await ragApi.getDocument(doc.document_id)
    previewChunks.value = res.chunks || []
  } catch (error: any) {
    const detail = error?.response?.data?.detail || error?.message || '获取失败'
    ElMessage.error('预览失败：' + detail)
  } finally {
    previewLoading.value = false
  }
}

// ===== 表格数据访问层：预览与结构化查询复用同一套列/行/行号（避免两套重复展示逻辑）=====
const sheetColumnOptions = computed(() => excelPreview.value?.active_sheet?.columns || [])

const tableColumns = computed(() => {
  if (excelQueryActive.value && excelQueryResult.value) return excelQueryResult.value.columns
  return excelPreview.value?.active_sheet?.columns || []
})

const tableRows = computed(() => {
  if (excelQueryActive.value && excelQueryResult.value) return excelQueryResult.value.rows
  return excelPreview.value?.active_sheet?.rows || []
})

const tableRowNumbers = computed(() => {
  if (excelQueryActive.value && excelQueryResult.value) return excelQueryResult.value.row_excel_numbers
  return excelPreview.value?.active_sheet?.row_excel_numbers || []
})

const excelTableData = computed(() => {
  return tableRows.value.map((row: any[]) => {
    const obj: Record<string, any> = {}
    row.forEach((v: any, ci: number) => { obj[String(ci)] = v })
    return obj
  })
})

const excelRowIndex = (index: number) => {
  const nums = tableRowNumbers.value || []
  return nums[index] ?? index + 1
}

// ===== 结构化查询（Phase 1B）=====
const extractDetail = (error: any): string => {
  const d = error?.response?.data?.detail
  if (d === undefined || d === null) return error?.message || '请求失败'
  if (typeof d === 'string') return d
  if (typeof d === 'object') {
    const base = d.message || '请求失败'
    const sug = d.details?.suggestions
    if (Array.isArray(sug) && sug.length) return `${base}（相近列：${sug.join(', ')}）`
    const avail = d.details?.available
    if (Array.isArray(avail) && avail.length && avail.length <= 10) {
      return `${base}（可用：${avail.join(', ')}）`
    }
    return base
  }
  return String(d)
}

const addQueryFilter = () => {
  excelQueryFilters.value.push({ column: '', operator: 'eq', value: '' })
}

const removeQueryFilter = (index: number) => {
  excelQueryFilters.value.splice(index, 1)
  if (excelQueryFilters.value.length === 0) addQueryFilter()
}

const resetQueryState = () => {
  excelQueryActive.value = false
  excelQueryResult.value = null
  excelQueryError.value = ''
  excelQueryColumns.value = []
  excelQueryLimit.value = 50
  excelQueryOffset.value = 0
  excelQueryFilters.value = [{ column: '', operator: 'eq', value: '' }]
}

const buildQueryPayload = () => {
  const sheet = excelPreview.value?.active_sheet
  const filters = excelQueryFilters.value
    .filter((f: any) => !!f.column)
    .map((f: any) => f.operator === 'is_null'
      ? { column: f.column, operator: 'eq' as const, value: null }
      : { column: f.column, operator: f.operator as 'eq' | 'contains', value: f.value })

  const payload: any = {
    sheet_index: sheet?.sheet_index ?? 0,
    filters,
    match_mode: 'and',
    limit: excelQueryLimit.value,
    offset: excelQueryOffset.value
  }
  if (excelQueryColumns.value.length > 0) payload.columns = [...excelQueryColumns.value]
  return payload
}

const runExcelQuery = async (resetOffset: boolean = true) => {
  const docId = excelDoc.value?.document_id
  if (!docId || !excelPreview.value?.active_sheet) return
  if (resetOffset) excelQueryOffset.value = 0

  excelQueryLoading.value = true
  excelQueryError.value = ''
  try {
    const res = await ragApi.queryExcel(docId, buildQueryPayload())
    excelQueryResult.value = res
    excelQueryActive.value = true
  } catch (error: any) {
    excelQueryResult.value = null
    excelQueryActive.value = false
    excelQueryError.value = extractDetail(error)
    ElMessage.error('结构化查询失败：' + excelQueryError.value)
  } finally {
    excelQueryLoading.value = false
  }
}

const clearExcelQuery = () => {
  // 回到预览视图（不重新请求后端，预览数据仍在）
  resetQueryState()
}

const queryFirstPage = async () => {
  if (!excelQueryResult.value) return
  excelQueryOffset.value = 0
  await runExcelQuery(false)
}

const queryPrevPage = async () => {
  if (!excelQueryResult.value) return
  excelQueryOffset.value = Math.max(0, excelQueryResult.value.offset - excelQueryResult.value.limit)
  await runExcelQuery(false)
}

const queryNextPage = async () => {
  if (!excelQueryResult.value?.has_more) return
  excelQueryOffset.value = excelQueryResult.value.next_offset
    ?? (excelQueryResult.value.offset + excelQueryResult.value.limit)
  await runExcelQuery(false)
}

const headerModeLabel = (mode?: string) => {
  const labels: Record<string, string> = {
    single: '单层表头',
    multi: '多层表头',
    none: '未识别表头（已回退为列标）'
  }
  return labels[mode || ''] || (mode || '未知')
}

const formatCell = (v: any) => (v === null || v === undefined ? '' : String(v))

const openExcelPreview = async (doc: any) => {
  excelDoc.value = doc
  excelPreview.value = null
  excelActiveSheetIndex.value = '0'
  resetQueryState()
  excelVisible.value = true
  await loadExcelSheet(0)
}

const loadExcelSheet = async (index: number) => {
  const docId = excelDoc.value?.document_id
  if (!docId) return
  excelLoading.value = true
  try {
    const res = await ragApi.getExcelPreview(docId, index, 20)
    excelPreview.value = res
    excelActiveSheetIndex.value = String(res.active_sheet?.sheet_index ?? index)
  } catch (error: any) {
    ElMessage.error('表格预览失败：' + extractDetail(error))
  } finally {
    excelLoading.value = false
  }
}

const handleExcelTabChange = (name: any) => {
  const idx = Number(name)
  if (Number.isNaN(idx)) return
  // 切换 Sheet 时清除上一次查询结果（查询与 Sheet 强绑定），并回到该 Sheet 的预览
  resetQueryState()
  loadExcelSheet(idx)
}

// 按文档查询：限定只在该文档内检索（打开对话框）
const queryDocument = (doc: any) => {
  queryTargetDoc.value = doc
  docQueryInput.value = ''
  docQueryResult.value = ''
  queryVisible.value = true
}

// 提交按文档查询
const submitDocQuery = async () => {
  if (!docQueryInput.value.trim()) {
    ElMessage.warning('请输入查询内容')
    return
  }
  docQueryLoading.value = true
  try {
    const res = await ragApi.queryDocument(
      docQueryInput.value,
      false,
      3,
      queryTargetDoc.value.document_id
    )
    docQueryResult.value = res.response || '无相关结果'
  } catch (error: any) {
    const detail = error?.response?.data?.detail || error?.message || '查询失败'
    ElMessage.error('查询失败：' + detail)
    docQueryResult.value = ''
  } finally {
    docQueryLoading.value = false
  }
}

const deleteDocument = async (doc: any) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除文档 "${doc.filename}" 吗?此操作不可撤销。`,
      '确认删除',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
  } catch {
    // 用户取消
    return
  }

  try {
    // 真实调用后端删除 API（自动携带 JWT）；无 document_id 的仅为本地占位数据
    if (doc.document_id) {
      await ragApi.deleteDocument([doc.document_id])
    }
    documents.value = documents.value.filter(d => d.id !== doc.id)
    uploadHistory.value = uploadHistory.value.filter(d => d.document_id !== doc.document_id)
    ElMessage.success('文档删除成功')
  } catch (error: any) {
    console.error('删除文档失败：', error)
    const detail = error?.response?.data?.detail || error?.message || '请重试'
    ElMessage.error('删除失败：' + detail)
  }
}

const refreshDocuments = () => {
  loadDocuments()
  ElMessage.info('文档列表已刷新')
}

const clearSearch = () => {
  searchQuery.value = ''
}

const clearQuickQuery = () => {
  quickQuery.value = ''
  queryResult.value = ''
}
</script>

<style scoped lang="scss">
.knowledge-base {
  padding: 20px;
}

.page-header {
  margin-bottom: 30px;

  h1 {
    font-size: 28px;
    color: var(--text-primary);
    margin-bottom: 8px;
  }

  p {
    color: var(--text-secondary);
    font-size: 16px;
  }
}

.content-container {
  display: grid;
  grid-template-columns: 1fr 2fr;
  gap: 20px;

  @media (max-width: 1200px) {
    grid-template-columns: 1fr;
  }
}

.upload-card,
.document-card,
.quick-query-card {
  margin-bottom: 20px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;

  .header-title {
    font-size: 18px;
    font-weight: 600;
  }
}

.upload-history {
  margin-top: 20px;
  padding-top: 20px;
  border-top: 1px solid var(--border-light);

  h3 {
    margin-bottom: 15px;
    font-size: 16px;
    color: var(--text-primary);
  }

  .history-item {
    display: flex;
    justify-content: space-between;
    align-items: center;

    .file-info {
      display: flex;
      align-items: center;
      gap: 8px;

      .filename {
        font-weight: 500;
      }
    }

    .file-stats {
      display: flex;
      align-items: center;
      gap: 12px
    }
  }
}

.search-bar {
  margin-bottom: 20px;
}

.documents-list {
  min-height: 300px;

  .filename-cell {
    display: flex;
    align-items: center;
    gap: 8px;

    .file-icon {
      color: var(--primary-color);
      font-size: 18px;
    }

    .filename-text {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  }

  .action-buttons {
    display: flex;
    gap: 8px;
  }
}

.stats-info {
  margin-top: 20px;
  padding-top: 20px;
  border-top: 1px solid var(--border-light);
}

.quick-query-form {
  .quick-actions {
    display: flex;
    gap: 10px;
    margin-top: 10px;
    margin-bottom: 20px;
  }

  .query-result {
    margin-top: 20px;
    padding: 15px;
    background: var(--bg-base);
    border-radius: 6px;
    border: 1px solid var(--border-light);

    h4 {
      margin-bottom: 10px;
      color: var(--text-primary);
    }

    .result-content {
      line-height: 1.6;
      color: var(--text-regular);
    }
  }
}


.empty-tip {
  padding: 20px;
  text-align: center;
  color: var(--text-secondary);
}

.chunk-block {
  margin-bottom: 16px;
  padding: 12px 14px;
  background: var(--bg-base);
  border: 1px solid var(--border-light);
  border-radius: 6px;

  .chunk-index {
    font-size: 13px;
    font-weight: 600;
    color: var(--primary-color);
    margin-bottom: 8px;
  }

  .chunk-content {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-word;
    line-height: 1.6;
    font-family: inherit;
    color: var(--text-regular);
  }
}

.excel-preview {
  .excel-meta {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 8px;
  }

  .excel-sheet-info {
    margin: 8px 0 12px;
    font-size: 13px;
    color: var(--text-secondary);

    .excel-sheet-line {
      margin-bottom: 4px;
    }

    .excel-tip {
      color: var(--primary-color);
    }

    .excel-warn {
      margin-top: 4px;
      color: #e6a23c;
    }
  }

  .excel-query-panel {
    margin: 8px 0 12px;
    padding: 10px 12px;
    background: var(--bg-base);
    border: 1px solid var(--border-light);
    border-radius: 6px;

    .query-row {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 8px;
    }

    .query-label {
      font-size: 13px;
      color: var(--text-secondary);
      white-space: nowrap;
    }

    .query-hint {
      font-size: 12px;
      color: var(--text-placeholder);
    }
  }

  .excel-query-meta {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 14px;
    margin: 4px 0 10px;
    font-size: 13px;
    color: var(--text-secondary);
  }
}</style>
