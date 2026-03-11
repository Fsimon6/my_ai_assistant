/**
 * RAG相关类型定义
 */

// 文档类型
export interface Document {
  id: string
  filename: string
  title?: string
  description?: string
  size: number
  pages?: number
  status: 'processing' | 'processed' | 'failed'
  uploadedAt: string
  processedAt?: string
  metadata?: Record<string, any>
  tags?: string[]
  chunkCount?: number
}

// 文档块
export interface DocumentChunk {
  id: string
  documentId: string
  content: string
  metadata: {
    page?: number
    section?: string
    index: number
    [key: string]: any
  }
  embedding?: number[]
}

// 上传响应
export interface UploadResponse {
  id: string
  filename: string
  title?: string
  size: number
  status: 'processing' | 'processed' | 'failed'
  uploadedAt?: string
}

// 文档列表响应
export interface DocumentListResponse {
  items: Document[]
  total: number
  page: number
  perPage: number
  totalPages: number
}

// 文档搜索参数
export interface SearchParams {
  query: string
  documentIds?: string[]
  topK?: number
  threshold?: number
}

// 搜索结果
export interface SearchResult {
  documentId: string
  documentTitle: string
  chunkId: string
  content: string
  similarity: number
  page?: number
  metadata?: Record<string, any>
}

// 搜索相应
export interface SearchResponse {
  query: string
  results: SearchResult[]
  total: number
  searchTime: number
}

// 知识库统计
export interface KnowledgeStats {
  totalDocuments: number
  totalChunks: number
  totalSize: number
  documentTypes: Record<string, number>
  recentUploads: Document[]
}
