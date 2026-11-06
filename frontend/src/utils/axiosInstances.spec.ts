import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/router', () => ({ default: { push: vi.fn() } }))
vi.mock('element-plus', () => ({
  ElMessage: { error: vi.fn(), success: vi.fn(), info: vi.fn() },
}))

describe('Axios instance routing', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.resetModules()
  })

  it('uses the explicit L1 origin for both instances', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'http://127.0.0.1:5191')
    const { request } = await import('@/utils/requests')
    const { default: api } = await import('@/services/api')
    expect(request.defaults.baseURL).toBe('http://127.0.0.1:5191')
    expect(api.defaults.baseURL).toBe('http://127.0.0.1:5191/api/v1')
    expect(request.getUri({ url: '/api/v1/auth/me' }))
      .toBe('http://127.0.0.1:5191/api/v1/auth/me')
    expect(api.getUri({ url: '/rag/upload' }))
      .toBe('http://127.0.0.1:5191/api/v1/rag/upload')
  })

  it('uses the explicit formal origin for both instances', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'http://127.0.0.1:8021')
    const { request } = await import('@/utils/requests')
    const { default: api } = await import('@/services/api')
    expect(request.defaults.baseURL).toBe('http://127.0.0.1:8021')
    expect(api.defaults.baseURL).toBe('http://127.0.0.1:8021/api/v1')
  })

  it('falls back to same-origin instead of 8021', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '')
    const { request } = await import('@/utils/requests')
    const { default: api } = await import('@/services/api')
    expect(request.defaults.baseURL).toBe('')
    expect(api.defaults.baseURL).toBe('/api/v1')
  })
})
