// tests/unit/test_rag_store.ts
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useRagStore } from '../src/stores/rag'
import { ragService } from '../src/services/rag1'

// mock ragService
vi.mock('@/services/rag', () => ({
  ragService: {
    getDocuments: vi.fn()
  }
}))

describe('RAG Store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('应该正确初始化RAG状态', () => {
    const store = useRagStore()
    expect(store.isRAGEnabled).toBe(false)
    expect(store.documents).toHaveLength(0)
  })

  it('应该能够获取文档列表', async () => {
    const mockDocs = [{ id: '1', filename: 'test.pdf', status: 'processed' }]
    vi.mocked(ragService.getDocuments).mockResolvedValue({ items: mockDocs, total: 1 })

    const store = useRagStore()
    await store.fetchDocuments()

    expect(store.documents).toEqual(mockDocs)
    expect(ragService.getDocuments).toHaveBeenCalled()
  })
})