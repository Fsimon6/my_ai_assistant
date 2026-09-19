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
      const res = await ragApi.getDocuments()
      documents.value = (res.documents || []).map((d: any) => ({
        id: d.document_id,
        document_id: d.document_id,
        filename: d.filename,
        type: d.type,
        size: d.size || 0,
        chunks: d.chunks || 0,
        uploadTime: d.created_at
      }))
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

  const deleteDocument = async (documentId: string) => {
    try {
      const res = await ragApi.deleteDocument([documentId])
      return res
    } catch (error) {
      console.error('删除文档失败：', error)
      throw error
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
    deleteDocument,
    toggleRAG
  }
})
