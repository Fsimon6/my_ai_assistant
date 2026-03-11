import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { UserInfo } from '@/types/auth'
import { login as apiLogin, register as apiRegister, logout as apiLogout, getCurrentUser } from '@/api/auth'

export const useAuthStore = defineStore('auth', () => {
  // 状态
  const token = ref<string | null>(localStorage.getItem('access_token'))
  const userInfo = ref<UserInfo | null>(null)
  const isAuthenticated = computed(() => !!token.value)

  // 从localStorage加载用户信息
  const savedUserInfo = localStorage.getItem('user_info')
  if (savedUserInfo) {
    try {
      userInfo.value = JSON.parse(savedUserInfo)
    } catch (error) {
      console.error('Failed to parse user info:', error)
    }
  }

  // 登录
  const login = async (username: string, password: string) => {
    try {
      const response = await apiLogin({ username, password })

      if (response.access_token) {
        token.value = response.access_token
        userInfo.value = response.user

        // 保存到localStorage
        localStorage.setItem('access_token', response.access_token)
        localStorage.setItem('user_info', JSON.stringify(response.user))

        return { success: true, text: '登录成功' }
      }

      return { success: false, text: '登录失败' }
    } catch (error: any) {
      return { success: false, text: error.text || '登录失败' }
    }
  }

  // 注册
  const register = async (userData: any) => {
    try {
      const response = await apiRegister(userData)
      return { success: true, data: response }
    } catch (error: any) {
      return { success: false, text: error.text || '注册失败' }
    }
  }

  // 登出
  const logout = async () => {
    try {
      await apiLogout()
    } finally {
      // 无论如何都清除本地状态
      token.value = null
      userInfo.value = null
      localStorage.removeItem('access_token')
      localStorage.removeItem('user_info')
    }
  }

  // 获取当前用户信息
  const fetchUserInfo = async () => {
    if (!token.value) return

    try {
      const user = await getCurrentUser()
      userInfo.value = user
      localStorage.setItem('user_info', JSON.stringify(user))
    } catch (error) {
      console.error('Failed to fetch user info:', error)
    }
  }

  return {
    token,
    userInfo,
    isAuthenticated,
    login,
    register,
    logout,
    fetchUserInfo
  }
})
