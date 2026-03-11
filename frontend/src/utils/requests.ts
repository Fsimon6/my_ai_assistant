import axios from "axios"
import type {AxiosResponse, AxiosInstance, InternalAxiosRequestConfig} from "axios"
import {ElMessage} from "element-plus";
import router from "@/router";

// 创建axios实例
const request: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
request.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    // 从localStorage获取token
    const token = localStorage.getItem('access_token')
    if (token) {
      config.headers = config.headers || {}
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => {
    console.error('Request Error:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器
request.interceptors.response.use(
  (response: AxiosResponse) => {
    const res = response.data

    // 如果响应数据有success字段
    if (res.hasOwnProperty('success')) {
      if (res.success) {
        return res.data || res
      } else {
        // 显示错误信息
        ElMessage.error(res.message || '请求失败')
        return Promise.reject(new Error(res.message || '请求失败'))
      }
    }

    // 如果响应的时直接的数据
    return res
  },
  (error) => {
    console.error('Request Error:', error)

    if (error.response) {
      const {status, data} = error.response

      switch (status) {
        case 401:
          ElMessage.error('登录已过期， 请重新登录')

          localStorage.removeItem('access_token')
          localStorage.removeItem('user_info')
          router.push('/login')
          break
        case 403:
          ElMessage.error('没有权限访问')
          break
        case 404:
          ElMessage.error('请求的资源不存在')
          break
        case 500:
          ElMessage.error('服务器内部错误')
          break
        default:
          ElMessage.error(data?.message || `请求失败${status}`)
      }
    } else if (error.request) {
      ElMessage.error('网络错误，请检查网络连接')
    } else {
      ElMessage.error('请求配置错误')
    }

    return Promise.reject(error)
  }
)

// 改为命名导出
export { request }
