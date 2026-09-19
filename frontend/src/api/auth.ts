import { request } from '@/utils/requests.ts'
import type {
  LoginRequest,
  RegisterRequest,
  LoginResponse,
  UserInfo,
  ApiResponse
} from "@/types/auth";

// 注意：响应拦截器（utils/requests.ts）已经把 response.data 解包并返回内层 data，
// 因此 request.post/get 的运行时返回值即为内层 data（如 LoginResponse / UserInfo），
// 这里直接把返回值断言为对应的业务类型。

// 用户登录
export const login = async (data: LoginRequest): Promise<LoginResponse> => {
  const response = await
    request.post<ApiResponse<LoginResponse>>('/api/v1/auth/login', data)
  return response as unknown as LoginResponse
}

// 用户注册
export const register = async (data: RegisterRequest): Promise<UserInfo> => {
  const response = await
    request.post<ApiResponse<UserInfo>>('/api/v1/auth/register', data)
  return response as unknown as UserInfo
}

// 用户登出
export const logout = async ():Promise<void> => {
  await request.post('/api/v1/auth/logout')
}

// 获取当前用户信息
export const getCurrentUser = async (): Promise<UserInfo> => {
  const response = await
    request.get<ApiResponse<UserInfo>>('/api/v1/auth/me')
  return response as unknown as UserInfo
}

// 刷新令牌
export const refreshToken = async (): Promise<LoginResponse> => {
  const response = await
    request.post<ApiResponse<LoginResponse>>('/api/v1/auth/refresh')
  return response as unknown as LoginResponse
}
