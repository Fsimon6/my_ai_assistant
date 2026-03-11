import { request } from '@/utils/requests.ts'
import type {
  LoginRequest,
  RegisterRequest,
  LoginResponse,
  UserInfo,
  ApiResponse
} from "@/types/auth";


// 用户登录
export const login = async (data: LoginRequest): Promise<LoginResponse> => {
  const response = await
    request.post<ApiResponse<LoginResponse>>('/api/v1/auth/login', data)
  return response.data.data || response
}

// 用户注册
export const register = async (data: RegisterRequest): Promise<UserInfo> => {
  const response = await
    request.post<ApiResponse<UserInfo>>('/api/v1/auth/register', data)
  return response.data.data || response
}

// 用户登出
export const logout = async ():Promise<void> => {
  await request.post('/api/v1/auth/logout')
}

// 获取当前用户信息
export const getCurrentUser = async (): Promise<UserInfo> => {
  const response = await
    request.get<ApiResponse<UserInfo>>('/api/v1/auth/me')
  return response.data.data || response
}

// 刷新令牌
export const refreshToken = async (): Promise<LoginResponse> => {
  const response = await
    request.post<ApiResponse<LoginResponse>>('/api/v1/auth/refresh')
  return response.data.data || response
}
