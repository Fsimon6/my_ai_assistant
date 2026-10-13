#!/usr/bin/env node
/**
 * 前端集成测试编排器
 *
 * 一条命令完成：临时隔离环境 → 启动隔离后端 → 真实 HTTP 取 Token → 运行 vitest → 可靠清理。
 *
 * 隔离手段（每一条都对应后端真实代码里的一个写入点）
 *   - SQLite ...... 环境变量 DATABASE_URL → 临时文件
 *                    （backend/config/development.py:28 → backend/database/base.py:16-47）
 *   - Chroma ...... 以临时目录为工作目录，命中 backend/services/vector_service.py:84 的相对默认值
 *                    ./data/chroma_db（该目录不读 settings.VECTOR_DB_PATH）
 *   - 运行期目录 .. backend/main.py:120-121 的 ./data/* 随工作目录落到临时目录
 *   - 应用日志 .... 环境变量 LOG_FILE → 临时文件（backend/utils/logger.py:20-23）
 *   - 模块导入 ... PYTHONPATH 指回项目根，保证临时工作目录下仍导入本项目后端代码
 *   - ENVIRONMENT . 显式固定为 development（设为 testing 会命中 backend/config/__init__.py:33
 *                    对不存在类名 TestConfig 的导入，属已知缺陷，本轮不修）
 *
 * 额外约束
 *   - 不使用 8021 / 5181 / 8000 / 5173；隔离后端只监听 127.0.0.1 的临时空闲端口
 *   - 不启动 --reload（避免多余监控子进程）
 *   - 不写、不改根 .env；不打印 Token / 密码 / API Key / JWT Secret
 *   - 默认不产生任何远程 LLM / Embedding 调用（除非显式 E2E_ALLOW_LLM=1）
 *   - 无论健康检查超时、注册失败、vitest 失败还是被中断，都尽力终止后端并删除临时目录
 *   - 清理只针对本轮创建的 PID 与临时目录；清理失败会明确报告残留，不谎称已清理
 */
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:net'
import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND_DIR = path.resolve(SCRIPT_DIR, '..')
const PROJECT_ROOT = path.resolve(FRONTEND_DIR, '..')

/** 现有服务端口：绝不使用（§三 / §九） */
const RESERVED_PORTS = new Set([8000, 5173, 8021, 5181])
const TEST_FILTER = 'tests/integration'
const HEALTH_TIMEOUT_MS = Number(process.env.E2E_HEALTH_TIMEOUT_MS || 90_000)
const PORT_ATTEMPTS = 10
const SHUTDOWN_TIMEOUT_MS = 15_000
const FORCE_KILL_TIMEOUT_MS = 5_000

const state = {
  tmpRoot: null,
  child: null,
  cleaned: false,
  cleanupFailed: false,
  backendLog: null, // 路径（展示用）
  appLog: null, // 路径（展示用）
  capture: null, // 捕获对象（含 close）
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const info = (msg) => process.stdout.write(`[integration] ${msg}\n`)
const warn = (msg) => process.stderr.write(`[integration] ${msg}\n`)

/** 对可能进入终端/日志的文本做脱敏（JWT / Bearer / password） */
function sanitize(text) {
  return String(text)
    .replace(/eyJ[A-Za-z0-9_\-.]{10,}/g, '<jwt-masked>')
    .replace(/(Bearer\s+)[A-Za-z0-9_\-.]{10,}/gi, '$1<token-masked>')
    .replace(/("password"\s*:\s*")[^"]*(")/gi, '$1<masked>$2')
}

function resolvePython() {
  const isWin = process.platform === 'win32'
  const candidate = path.join(
    PROJECT_ROOT,
    '.venv',
    isWin ? 'Scripts' : 'bin',
    isWin ? 'python.exe' : 'python',
  )
  if (!fs.existsSync(candidate)) {
    throw new Error(`未找到本项目虚拟环境 Python：${candidate}（请确认项目根存在 .venv）`)
  }
  return candidate
}

function resolveVitestEntry() {
  const entry = path.join(FRONTEND_DIR, 'node_modules', 'vitest', 'vitest.mjs')
  if (!fs.existsSync(entry)) {
    throw new Error(`未找到 vitest 入口：${entry}（请先在 frontend/ 执行 npm install）`)
  }
  return entry
}

function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = createServer()
    srv.unref()
    srv.on('error', reject)
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address()
      srv.close(() => resolve(port))
    })
  })
}

async function pickPort() {
  for (let i = 0; i < PORT_ATTEMPTS; i += 1) {
    const port = await getFreePort()
    if (!RESERVED_PORTS.has(port)) return port
  }
  throw new Error('连续多次取到保留端口，已放弃（避免误用现有服务端口）')
}

/** 把子进程输出写入临时日志文件，并保留尾部片段用于失败诊断 */
function createCapture(filePath) {
  const stream = fs.createWriteStream(filePath, { flags: 'a' })
  const chunks = []
  const MAX = 8_000
  const push = (text) => {
    chunks.push(text)
    while (chunks.join('').length > MAX) chunks.shift()
  }
  return {
    stream,
    write(text) {
      push(text)
      try {
        stream.write(text)
      } catch {
        /* 写日志失败不影响测试结果 */
      }
    },
    tail() {
      return sanitize(chunks.join('').slice(-MAX))
    },
    /** 返回 Promise：必须等句柄真正关闭再删除临时目录（Windows 下 EPERM 的根因） */
    close() {
      return new Promise((resolve) => {
        try {
          stream.once('close', resolve)
          stream.end()
        } catch {
          resolve()
        }
        setTimeout(resolve, 2_000) // 兜底：避免极端情况下永久等待
      })
    },
  }
}

async function waitForHealth(base, capture) {
  const deadline = Date.now() + HEALTH_TIMEOUT_MS
  while (Date.now() < deadline) {
    if (state.child && state.child.exitCode !== null) {
      throw new Error(
        `隔离后端在健康检查前退出（exit=${state.child.exitCode}）。后端日志尾部：\n${capture.tail()}`,
      )
    }
    try {
      const res = await fetch(`${base}/health`, { signal: AbortSignal.timeout(3_000) })
      if (res.status === 200) return
    } catch {
      /* 未就绪，继续轮询 */
    }
    await delay(400)
  }
  throw new Error(`健康检查超时（${HEALTH_TIMEOUT_MS}ms）：${base}/health\n后端日志尾部：\n${capture.tail()}`)
}

async function postJson(base, pathname, body) {
  const res = await fetch(`${base}${pathname}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(20_000),
  })
  const text = await res.text()
  let json = null
  try {
    json = JSON.parse(text)
  } catch {
    /* 非 JSON 响应由调用方按 text 报错 */
  }
  return { status: res.status, json, text }
}

function randomCredentials() {
  const suffix = crypto.randomBytes(6).toString('hex')
  return {
    username: `e2e_${suffix}`,
    email: `e2e_${suffix}@example.com`,
    password: `E2e-${crypto.randomBytes(12).toString('base64url')}`,
  }
}

async function stopBackend() {
  const child = state.child
  if (!child || child.exitCode !== null || child.signalCode !== null) return true

  const exited = new Promise((resolve) => child.once('exit', () => resolve(true)))
  try {
    child.kill('SIGTERM')
  } catch {
    /* 可能已退出 */
  }

  let done = await Promise.race([exited, delay(SHUTDOWN_TIMEOUT_MS).then(() => false)])
  if (done) return true

  warn(`后端进程未在 ${SHUTDOWN_TIMEOUT_MS}ms 内退出，改用强制终止（仅本轮 PID ${child.pid}）`)
  if (process.platform === 'win32') {
    // 仅针对本轮 PID（含其子进程树），不触碰其它 python / uvicorn 进程
    spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' })
  } else {
    try {
      child.kill('SIGKILL')
    } catch {
      /* ignore */
    }
  }
  done = await Promise.race([exited, delay(FORCE_KILL_TIMEOUT_MS).then(() => false)])
  return done
}

async function removeTempRoot() {
  if (!state.tmpRoot) return true
  // Windows 下刚退出的后端/刚关闭的日志句柄可能仍短暂占用文件 → 带重试删除
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      fs.rmSync(state.tmpRoot, { recursive: true, force: true, maxRetries: 10, retryDelay: 250 })
    } catch (err) {
      warn(`删除临时目录失败（第 ${attempt + 1} 次）：${err.message}`)
    }
    if (!fs.existsSync(state.tmpRoot)) return true
    await delay(500)
  }
  return !fs.existsSync(state.tmpRoot)
}

async function cleanup() {
  if (state.cleaned) return
  state.cleaned = true
  info('开始清理（终止隔离后端 + 删除临时目录）…')

  // 每一步独立兜底：任一步失败都不得跳过后续步骤
  let stopped = false
  try {
    stopped = await stopBackend()
  } catch (err) {
    warn(`终止后端时出错：${sanitize(err?.message ?? err)}`)
  }
  try {
    await state.capture?.close()
  } catch (err) {
    warn(`关闭日志流时出错：${sanitize(err?.message ?? err)}`)
  }
  let removed = false
  try {
    removed = await removeTempRoot()
  } catch (err) {
    warn(`删除临时目录时出错：${sanitize(err?.message ?? err)}`)
  }

  if (stopped && removed) {
    info('清理完成：后端已退出，临时目录已删除')
    return
  }
  state.cleanupFailed = true
  if (!stopped) warn(`清理未完成：后端进程可能仍存活（PID ${state.child?.pid}），请人工确认`)
  if (!removed) warn(`清理未完成：临时目录仍存在，请人工删除：${state.tmpRoot}`)
}

function runVitest(env) {
  return new Promise((resolve) => {
    const entry = resolveVitestEntry()
    // 直接以 node 运行 vitest 入口：避免 Windows 下 spawn .cmd 需要 shell 的限制
    const child = spawn(process.execPath, [entry, 'run', TEST_FILTER], {
      cwd: FRONTEND_DIR,
      stdio: 'inherit',
      windowsHide: true,
      env: { ...process.env, ...env },
    })
    child.on('error', (err) => {
      warn(`无法启动 vitest：${err.message}`)
      resolve(1)
    })
    child.on('exit', (code, signal) => {
      if (signal) warn(`vitest 被信号终止：${signal}`)
      resolve(code === null ? 1 : code)
    })
  })
}

async function main() {
  const python = resolvePython()
  resolveVitestEntry()

  state.tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'myai-e2e-'))
  const dbPath = path.join(state.tmpRoot, 'e2e-isolated.db')
  const databaseUrl = `sqlite:///${dbPath.split(path.sep).join('/')}`
  state.backendLog = path.join(state.tmpRoot, 'backend.stdout.log')
  state.appLog = path.join(state.tmpRoot, 'backend.app.log')
  const port = await pickPort()
  const base = `http://127.0.0.1:${port}`

  info(`项目根        ：${PROJECT_ROOT}`)
  info(`临时隔离目录  ：${state.tmpRoot}`)
  info(`隔离 SQLite   ：${databaseUrl}`)
  info(`隔离 Chroma   ：${path.join(state.tmpRoot, 'data', 'chroma_db')}（随工作目录解析）`)
  info(`隔离后端地址  ：${base}（仅 127.0.0.1；未使用 8021/5181/8000/5173）`)
  info(`后端 stdout   ：${state.backendLog}`)
  info(`应用日志文件  ：${state.appLog}`)

  const capture = createCapture(state.backendLog)
  state.capture = capture
  state.child = spawn(
    python,
    ['-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', String(port)],
    {
      cwd: state.tmpRoot, // ← Chroma / ./data 等相对路径隔离的关键
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        PYTHONPATH: [PROJECT_ROOT, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
        PYTHONDONTWRITEBYTECODE: '1',
        ENVIRONMENT: 'development',
        DATABASE_URL: databaseUrl,
        LOG_FILE: state.appLog,
      },
    },
  )
  state.child.stdout.on('data', (d) => capture.write(d.toString('utf8')))
  state.child.stderr.on('data', (d) => capture.write(d.toString('utf8')))
  state.child.on('error', (err) => warn(`隔离后端启动失败：${err.message}`))

  try {
    await waitForHealth(base, capture)
    info('健康检查通过：GET /health → 200')

    // ── 真实 HTTP：注册 + 登录，取得 Token（凭据随机、仅存在于本轮内存与临时库）──
    const creds = randomCredentials()
    const reg = await postJson(base, '/api/v1/auth/register', {
      username: creds.username,
      email: creds.email,
      password: creds.password,
      full_name: 'E2E Isolated User',
    })
    if (reg.status !== 200) {
      throw new Error(`隔离后端注册失败：HTTP ${reg.status} ${sanitize(reg.text).slice(0, 300)}`)
    }
    const login = await postJson(base, '/api/v1/auth/login', {
      username: creds.username,
      password: creds.password,
    })
    if (login.status !== 200) {
      throw new Error(`隔离后端登录失败：HTTP ${login.status} ${sanitize(login.text).slice(0, 300)}`)
    }
    const token = login.json?.data?.access_token ?? login.json?.access_token
    if (!token || typeof token !== 'string') {
      throw new Error(`登录响应中未找到 access_token（响应字段：${Object.keys(login.json ?? {}).join(',')}）`)
    }
    info('已通过真实 HTTP 注册并登录隔离用户（用户名/密码/Token 均不打印、不落盘）')

    const code = await runVitest({
      E2E_API_BASE: base,
      E2E_AUTH_TOKEN: token,
      E2E_EXPECT_EMPTY_KB: '1', // 隔离库对全新用户必然为空 → 用于证明隔离生效
      ...(process.env.E2E_ALLOW_LLM ? { E2E_ALLOW_LLM: process.env.E2E_ALLOW_LLM } : {}),
    })
    info(`vitest 退出码 = ${code}`)
    process.exitCode = code
  } catch (err) {
    warn(`基础设施错误：${sanitize(err?.message ?? err)}`)
    process.exitCode = 2
  } finally {
    await cleanup()
    if (state.cleanupFailed && !process.exitCode) process.exitCode = 3
  }
}

// 中断也要尽力清理
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(sig, () => {
    warn(`收到 ${sig}，正在清理…`)
    cleanup().then(() => process.exit(130))
  })
}
process.on('uncaughtException', (err) => {
  warn(`未捕获异常：${sanitize(err?.stack ?? err)}`)
  cleanup().then(() => process.exit(2))
})

main().catch(async (err) => {
  // 修复 R1：临时目录可能在进入 try/finally 之前就已创建（例如 pickPort / createCapture /
  // spawn 抛错），此处必须补一次清理。cleanup() 对 child / capture / tmpRoot 为空均安全，
  // 且由 state.cleaned 保证幂等（不会与 finally 的正常清理重复执行）。
  warn(`编排失败：${sanitize(err?.stack ?? err)}`)
  try {
    await cleanup()
  } catch (cleanupErr) {
    // 清理自身的异常不得覆盖上面的原始错误，也不得阻止非零退出码
    warn(`清理过程出错：${sanitize(cleanupErr?.message ?? cleanupErr)}`)
  }
  process.exitCode = 2
})
