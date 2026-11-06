/**
 * Resolve the browser-facing API origin.
 *
 * An explicit VITE_API_BASE_URL wins (development / production). When it is
 * absent we deliberately return an empty string so Axios and fetch use the
 * current page origin; Vite/nginx then owns the /api routing. This prevents a
 * development fallback from silently targeting the formal backend.
 */
export function resolveApiBaseUrl(explicitBaseUrl?: string): string {
  const value = (explicitBaseUrl || '').trim().replace(/\/+$/, '')
  return value
}

export function buildApiUrl(baseUrl: string | undefined, relativePath: string): string {
  if (/^https?:\/\//i.test(relativePath)) {
    return relativePath
  }
  const base = resolveApiBaseUrl(baseUrl)
  const path = relativePath.replace(/^\/+/, '')
  return base ? `${base}/${path}` : `/${path}`
}
