import { request } from '../utils/requests'

export interface Document {
  id: string
  filename: string
  status: 'processing' | 'processed' | 'failed'
}

export interface GetDocumentsParams {
  page?: number
  perPage?: number
}

export interface SearchParams {
  query: string
  documentIds?: string[]
}

// API 基础路径
const API_BASE =
import.meta.env.VITE_API_BASE_URL || ''

export const ragService = {
  // 上传文档
  uploadDocument: (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    return request.post<{ id: string; filename: string; status: string }>(
      `${API_BASE}/documents/upload`,
      formData,
      {
        headers: { 'Content-Type': 'multipart/form-data' },
      }
    )
  },

  // 获取文档目录
  getDocument: (params?: GetDocumentsParams) => {
    return request.get<{ items: Document[]; total: number }>(
      `${API_BASE}/documents`,
      { params }
    )
  },

  // 语义搜索
  search: (data: SearchParams) => {
    return request.post<{ query: string; results: any[] }>(
      `${API_BASE}/knowledge/search`,
      data
    )
  }
}
