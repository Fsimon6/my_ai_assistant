export type AppEnvironment = 'development' | 'testing' | 'production' | 'unknown'

export function normalizeAppEnv(value?: string): AppEnvironment {
  const normalized = (value || 'development').trim().toLowerCase()
  if (normalized === 'testing' || normalized === 'production' || normalized === 'development') {
    return normalized
  }
  return 'unknown'
}

export function isL1TestingEnv(value?: string): boolean {
  return normalizeAppEnv(value) === 'testing'
}

export const appEnvironment = normalizeAppEnv(import.meta.env.VITE_APP_ENV)
export const isL1Testing = isL1TestingEnv(import.meta.env.VITE_APP_ENV)
export const l1BannerText = 'L1 TESTING · 当前为隔离测试环境，不是正式环境'
