import { defineStore } from "pinia"
import { ref, computed } from "vue"
import { ragApi } from '@/services/api'

export const useRagStore = defineStore('rag', () => {
  // 状态
  const documents = ref<any[]>([])
  const isRAGEnabled = ref(false)
  const collectionInfo = ref<any>(null)
  const uploadHistory = ref<any[]>([])

  // 计算属性
  const documentCount = computed(() => documents.value.length)
  const totalChunks = computed(() => collectionInfo.value?.total_documents || 0)

  // 动作
  const fetchDocuments = async () => {
    try {
      // 这里应该调用API获取文档列表
      // 暂时使用模拟数据
      documents.value = [
        {
          id: '1',
          name: '示例文档.pdf',
          type: 'pdf',
          size: '2.3MB',
          uploadedAt: '2024-01-10'
        }
      ]
    } catch (error) {
      console.error('获取文档列表失败：', error)
    }
  }

  const uploadDocument = async (file: File, metadata?: any) => {
    try {
      const response = await ragApi.uploadDocument(file, metadata)

      // 添加到历史记录
      uploadHistory.value.unshift({
        filename: file.name,
        timestamp: new Date().toISOString(),
        success: true,
        ...response
      })

      // 刷新文档列表
      await fetchDocuments()

      return {success: true, data: response }
    } catch (error) {
      console.error('上传文档失败', error)
      return { success: false, error}
    }
  }

  const queryKnowledgeBase = async (query: string, stream: boolean = false) => {
    try {
      return await ragApi.queryDocument(query, stream)
    } catch (error) {
      console.error('查询知识库失败：', error)
      throw error
    }
  }

  const fetchCollectionInfo = async () => {
    try {
      const info = await ragApi.getCollectionInfo()
      collectionInfo.value = info.collection_info
      return info
    } catch (error) {
      console.error('获取集合信息失败：', error)
      return null
    }
  }

  const toggleRAG = (enabled: boolean) => {
    isRAGEnabled.value = enabled
    if (enabled) {
      fetchCollectionInfo()
    }
  }

  return {
    // 状态
    documents,
    isRAGEnabled,
    collectionInfo,
    uploadHistory,

    // 计算属性
    documentCount,
    totalChunks,

    // 动作
    fetchDocuments,
    uploadDocument,
    queryKnowledgeBase,
    fetchCollectionInfo,
    toggleRAG
  }
})
