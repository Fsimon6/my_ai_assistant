import { describe, it, expect } from 'vitest'

/**
 * 后端 API 集成测试（真实 HTTP，不 mock fetch）
 *
 * 默认运行方式（推荐：自动隔离，无需人工准备 Token）
 *     cd frontend && npm run test:integration
 *   该命令由 scripts/run-integration.mjs 编排：启动隔离后端（临时 SQLite + 临时 Chroma，
 *   仅监听 127.0.0.1 的临时端口）→ 真实 HTTP 注册/登录取得 Token → 运行本文件 → 清理资源。
 *
 * 低层调试（对接已运行的后端，不做隔离，需自备 Token）
 *     E2E_API_BASE=http://127.0.0.1:<port> E2E_AUTH_TOKEN=<token> npm run test:integration:external
 *
 * 可选档：真实 RAG 查询（真实消耗 1 次 Embedding + 1 次 Chat 调用，默认不执行）
 *     E2E_ALLOW_LLM=1 npm run test:integration
 *
 * 环境变量
 *   E2E_API_BASE         被测后端地址（必需；缺失即报基础设施错误）
 *   E2E_AUTH_TOKEN       有效 Token（认证相关用例必需）
 *   E2E_EXPECT_EMPTY_KB  =1 时断言知识库为空（仅隔离后端设置；对接真实后端时勿设置）
 *   E2E_ALLOW_LLM        =1 时才执行真实 RAG 查询用例
 */
const API_BASE = (process.env.E2E_API_BASE ?? '').replace(/\/+$/, '')
const AUTH_TOKEN = process.env.E2E_AUTH_TOKEN ?? ''
const EXPECT_EMPTY_KB = process.env.E2E_EXPECT_EMPTY_KB === '1'
const ALLOW_LLM = process.env.E2E_ALLOW_LLM === '1'

const INFRA_HINT = [
  '基础设施错误（不是被测接口的问题）：缺少运行集成测试所需的连接信息。',
  '推荐用法：cd frontend && npm run test:integration',
  '          （自动启动隔离后端并注入 E2E_API_BASE / E2E_AUTH_TOKEN，无需手工准备）',
  '手动对接已运行的后端：',
  '          E2E_API_BASE=http://127.0.0.1:<port> E2E_AUTH_TOKEN=<token> npm run test:integration:external',
].join('\n')

function requireBase(): string {
  if (!API_BASE) throw new Error(`未收到 E2E_API_BASE。\n${INFRA_HINT}`)
  return API_BASE
}

function requireToken(): string {
  if (!API_BASE) throw new Error(`未收到 E2E_API_BASE。\n${INFRA_HINT}`)
  if (!AUTH_TOKEN) throw new Error(`未收到 E2E_AUTH_TOKEN。\n${INFRA_HINT}`)
  return AUTH_TOKEN
}

function maskSecrets(text: string): string {
  return text.replace(/eyJ[A-Za-z0-9_\-.]{10,}/g, '<jwt-masked>')
}

/**
 * 失败诊断文本：明确 HTTP 状态、URL 与响应体，便于区分「接口错误」与「认证被拒」。
 * 注意：必须读 res.clone()，否则会消费原始 body，导致调用方后续 res.json() 报
 * "Body is unusable: Body has already been read"。
 */
async function diagnose(res: Response): Promise<string> {
  let body = '<响应体读取失败>'
  try {
    body = maskSecrets((await res.clone().text()).slice(0, 400))
  } catch {
    /* 保持占位文本 */
  }
  return `HTTP ${res.status} ${res.statusText} ← ${res.url}\n响应体：${body}`
}

function jsonHeaders(token: string): Record<string, string> {
  return { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }
}

describe('后端 API 集成测试（真实 HTTP）', () => {
  it('健康检查：GET /health → 200 且 status=healthy', async () => {
    const base = requireBase()
    const res = await fetch(`${base}/health`)
    expect(res.status, await diagnose(res)).toBe(200)
    const data = await res.json()
    expect(data.status).toBe('healthy')
  })

  it('认证边界：未带 Token 的 POST /api/v1/rag/query → 401（在认证中间件被拒绝）', async () => {
    const base = requireBase()
    const res = await fetch(`${base}/api/v1/rag/query`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: '集成测试-未认证请求', stream: false }),
    })
    const diag = await diagnose(res)
    expect(res.status, diag).toBe(401)

    const body = await res.json().catch(() => ({}))
    // 认证中间件（backend/middleware/auth.py:43-44）返回 "Missing or invalid token"；
    // 若改由路由依赖（backend/utils/auth.py:52-58）拒绝则是 "需要登录"。
    // 断言 detail 含 "token" ⇒ 证明请求在进入 RAG 路由之前就被拒绝，
    // 因此不会触发任何 Embedding / Chat 远程调用。
    expect(String(body.detail ?? '').toLowerCase(), diag).toContain('token')
  })

  it('认证边界：无效 Token 不得访问受保护资源（GET /api/v1/rag/documents → 401）', async () => {
    const base = requireBase()
    const res = await fetch(`${base}/api/v1/rag/documents`, {
      headers: { Authorization: 'Bearer e2e-invalid-token-not-a-jwt' },
    })
    expect(res.status, await diagnose(res)).toBe(401)
  })

  it('受保护只读接口：有效 Token → GET /api/v1/rag/documents → 200 且响应结构正确', async () => {
    const base = requireBase()
    const token = requireToken()
    const res = await fetch(`${base}/api/v1/rag/documents`, { headers: jsonHeaders(token) })
    const diag = await diagnose(res)
    expect(res.status, diag).toBe(200)

    const body = await res.json()
    // 响应结构以 backend/api/v1/rag.py:469-474 为准：success / documents / total / timestamp
    expect(body.success, diag).toBe(true)
    expect(Array.isArray(body.documents), diag).toBe(true)
    expect(typeof body.total, diag).toBe('number')
    expect(body.total, diag).toBe(body.documents.length)

    if (EXPECT_EMPTY_KB) {
      // 隔离后端 + 全新用户 ⇒ 向量库为空。用于证明本测试未连到真实开发数据。
      expect(body.total, `期望隔离知识库为空，实际 total=${body.total}`).toBe(0)
    }
  })

  // 可选档：真实 RAG 生成（消耗远程调用）。默认跳过 → 不计入默认模式的通过数。
  it.skipIf(!ALLOW_LLM)(
    '（可选）真实 RAG 查询：POST /api/v1/rag/query → 200（需 E2E_ALLOW_LLM=1，消耗真实 Embedding + Chat 各 1 次）',
    async () => {
      const base = requireBase()
      const token = requireToken()
      const res = await fetch(`${base}/api/v1/rag/query`, {
        method: 'POST',
        headers: jsonHeaders(token),
        body: JSON.stringify({ query: '集成测试-可选真实查询', stream: false }),
      })
      const diag = await diagnose(res)
      expect(res.status, diag).toBe(200)

      const body = await res.json()
      expect(body.success, diag).toBe(true)
      expect(typeof body.response, diag).toBe('string')
    },
  )
})
