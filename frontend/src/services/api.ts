import type { AxiosResponse, AxiosInstance } from 'axios'
import axios from 'axios'
import type { UploadFile } from "element-plus"

// 创建axios实例
const api: AxiosInstance = axios.create({
  baseURL: 'http://localhost:8000/api/v1',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
api.interceptors.response.use(
  (config) => {
    // 可以在这里添加token等
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

// RAG API
export const ragApi = {
  // 上传文档
  uploadDocument: async (file: File, metadata?: any) => {
    const formData = new FormData()
    formData.append('file', file)
    if (metadata) {
      formData.append('metadata', JSON.stringify(metadata))
    }

    return api.post('/rag/upload', formData, {
      headers: {
        'Content-Type': 'multipart/form-data'
      }
    })
  },

  // 查询文档
  queryDocument: async (query: string, stream: boolean = false, contextCount: number = 3) => {
    return api.post('/rag/query', {
      query,
      stream,
      context_count: contextCount
    })
  },

  // 带历史查询
  queryWithHistory: async (query: string, history: any[], stream: boolean = false) => {
    return api.post('/rag/query-with-history', {
      query,
      history,
      stream
    })
  },

  // 获取集合信息
  getCollectionInfo: async () => {
    return api.get('/rag/collection-info')
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
