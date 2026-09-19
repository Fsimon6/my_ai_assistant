/**
 * 文档处理相关类型定义
 */

// 文档类型枚举
export enum DocumentType {
  PDF = 'pdf',
  TXT = 'txt',
  DOCX = 'docx',
  XLSX = 'xlsx',
  CSV = 'csv',
  IMAGE = 'image',
  OTHER = 'other'
}

// 文档状态
export type DocumentStatus = 'pending' | 'uploading' | 'processing' | 'processed' | 'failed'

// 文档元数据
export interface DocumentMetadata {
  author?: string
  createdAt?: string
  modifiedAt?: string
  pageCount?: number
  wordCount?: number
  language?: string
  encoding?: string
  mimeType?: string
  [key: string]: any
}

// 上传任务
export interface UploadTask {
  id: string
  file: File
  name: string
  size: number
  type: DocumentType
  status: DocumentStatus
  progress: number
  error?: string
  documentId?: string
  metadata?: DocumentMetadata
  createdAt: string
  updateAt: string
}

// 上传配置
export interface UploadConfig {
  maxSize: number
  allowedTypes: string[]
  maxConcurrent: number
  chunkSize?: number
  useChunking?: boolean
}

// 上传事件
export interface UploadEvent {
  type: 'start' | 'progress' | 'complete' | 'error' | 'cancel'
  taskId: string
  data?: any
}

// 文档处理配置
export interface ProgressingConfig {
  chunkSize?: number
  chunkOverlap: number
  extractTables: boolean
  extractImages: boolean
  preserveFormatting: boolean
  language: string
}

// 文档导出格式
export enum ExportFormat {
  JSON = 'json',
  CSV = 'csv',
  TXT = 'txt',
  MARKDOWN = 'md'
}

// 文档导出选项
export interface ExportOptions {
  format: ExportFormat
  includeMetadata: boolean
  includeChunks: boolean
  includeEmbeddings?: boolean
}

