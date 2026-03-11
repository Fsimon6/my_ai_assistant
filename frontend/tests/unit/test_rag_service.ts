// tests/unit/test_rag_service.ts
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { ragService } from 'frontend/src/services/rag1'

// 注意：mock 的路径要与 rag.ts 中导入的路径一致
vi.mock('@/utils/requests', () => ({
  request: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn()
  }
}))

import { request } from 'frontend/src/utils/requests'

describe('RAG Service', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('should upload document', async () => {
    const mockResponse = { data: { id: '1', filename: 'test.pdf', status: 'processing' } }
    vi.mocked(request.post).mockResolvedValue(mockResponse)

    const file = new File(['test'], 'test.pdf', { type: 'application/pdf' })
    const result = await ragService.uploadDocument(file)

    expect(result).toEqual(mockResponse.data)
    expect(request.post).toHaveBeenCalledWith(
      expect.stringContaining('/documents/upload'),
      expect.any(FormData),
      expect.any(Object)
    )
  })

  it('should get documents list', async () => {
    const mockResponse = {
      data: {
        items: [
          { id: '1', filename: 'doc1.pdf', status: 'processed' },
          { id: '2', filename: 'doc2.pdf', status: 'processing' }
        ],
        total: 2
      }
    }
    vi.mocked(request.get).mockResolvedValue(mockResponse)

    const result = await ragService.getDocuments({ page: 1, perPage: 10 })

    expect(result).toEqual(mockResponse.data)
    expect(request.get).toHaveBeenCalledWith(
      expect.stringContaining('/documents'),
      { params: { page: 1, perPage: 10 } }
    )
  })

  it('should search documents', async () => {
    const mockResponse = {
      data: {
        query: 'test',
        results: [{ documentId: '1', content: 'test content', similarity: 0.9 }]
      }
    }
    vi.mocked(request.post).mockResolvedValue(mockResponse)

    const result = await ragService.search({ query: 'test', documentIds: ['1'] })

    expect(result).toEqual(mockResponse.data)
    expect(request.post).toHaveBeenCalledWith(
      expect.stringContaining('/knowledge/search'),
      { query: 'test', documentIds: ['1'] }
    )
  })
})