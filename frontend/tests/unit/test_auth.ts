import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia} from 'pinia'
import { useAuthStore } from 'frontend/src/stores/auth'
import { authApi } from 'frontend/src/api/auth'

// Mock API
vi.mock('../../src/api/auth', () => ({
  authApi: {
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    getCurrentUser: vi.fn(),
  }
}))

describe('Auth Store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('should login successfully', async () => {
    const store = useAuthStore()
    const mockUser = {id: 1, username: 'test', email: 'test@example.com' }
    const mockResponse = { accessToken: 'token', user: mockUser }
    vi.mocked(authApi.login).mockResolveValue(mockResponse)
    await store.login({ email: 'test@example.com', password: 'password' })
    expect(store.isAuthenticated).toBe(true)
    expect(store.user).toEqual(mockUser)
    expect(store.token).toBe('token')
  })

  it('should handle login failure', async () => {
    const store = useAuthStore()
    vi.mocked(authApi.login).mockRejectedValue(new Error('Invalid credentials'))
    await expect(store.login({ email: 'test@example.com', password: 'wrong' })).rejects.toThrow()
    expect(store.isAuthenticated).toBe(false)
    expect(store.user).toBeNull()
  })

  it('should logout', () => {
    const store = useAuthStore()
    store.$patch({ user: { id: 1 }, token: 'token' })
    store.logout()
    expect(store.isAuthenticated).toBe(false)
    expect(store.user).toBeNull()
    expect(store.token).toBeNull()
    expect(authApi.logout).toHaveBeenCalled()
  })
})