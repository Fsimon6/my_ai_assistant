import { describe, it, expect } from 'vitest'

const API_BASE = 'http://localhost:8000' // 应与后端实际地址一致

describe('后端API集成测试', () => {
  it('健康检查接口', async () => {
    const res = await fetch(`${API_BASE}/health`)
    expect(res.status).toBe(200)
    const data = await res.json()
    expect(data.status).toBe('healthy')
  })

  it('RAG查询接口（非流式）', async () => {
    const res = await fetch(`${API_BASE}/api/v1/rag/query`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: '测试', stream: false })
    })
    expect(res.status).toBe(200)
    const data = await res.json()
    // 根据实际响应结构调整断言
    expect(data.success).toBe(true)
  })
})