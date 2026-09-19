# My AI Assistant（我的 AI 知识库助手）

基于大语言模型（LLM）与 RAG 技术构建的**本地知识库智能问答助手**，同时提供 AI 角色对话与
**Excel 表格自然语言查询**能力。

- 前端：Vue 3 + TypeScript + Element Plus + Pinia + Vite
- 后端：FastAPI + SQLAlchemy + ChromaDB + DuckDB
- 大模型：任意 OpenAI 兼容接口（OpenAI / DashScope / 智谱 / Ollama / 本地推理服务）

> 本项目默认在**本地运行**：`.env` 中的密钥只保存在本机，不会上传任何第三方（LLM 调用除外）。

---

## 功能特性

### 1. 文档知识库（RAG）
- 上传 `PDF / DOCX / TXT / MD / Excel`，自动解析、分块、向量化并持久化到本地 ChromaDB
- 检索增强问答，支持**流式输出**与带历史的连续对话
- 文档与向量检索结果**按用户隔离**（`user_id` 过滤）
- 文档列表 / 详情 / 删除、向量库信息查询

### 2. AI 角色对话
- 角色创建、编辑、删除、列表、统计
- 自定义系统提示词，塑造角色人设
- 流式对话、批量对话、历史会话查询

### 3. Excel 表格自然语言查询
把「用自然语言问表格」拆成**确定性规则层 + LLM 兜底**两级，保证同一句话结果可复现：

| 能力 | 说明 |
|------|------|
| 结构化查询 | 按列筛选（`=` `!=` `>` `>=` `<` `<=` `包含`）、分页、指定列投影 |
| 基础统计 | `COUNT` / `SUM` / `AVG` / `MIN` / `MAX` |
| 分组统计 | `GROUP BY` + 排序（`ORDER BY`）+ `TOP-N` |
| 逐行计算 | `A op B`（加、减、乘、除）派生列 |
| 多步分析 | 先取「金额最高的 N 个」，再对这批结果求总和 / 平均值 |

- SQL 由 Python 受控构造，运行在**内存 DuckDB**（`enable_external_access=false`，禁止 `ATTACH` / `COPY`）
- 业务标识列（订单号 / SKU 等）不会参与算术或统计，避免语义误判
- 确定性层解析不了时才调用 LLM，且对 provider 错误（欠费 / 限流 / 鉴权）统一分类并**脱敏**后再返回

### 4. 认证与安全
- JWT 鉴权（`Authorization: Bearer <token>`）+ bcrypt 口令哈希
- `JWT_SECRET` 无默认值：缺失时应用启动即失败（安全失败）
- 用户数据隔离、CORS 白名单、错误信息不回传 provider 原始报文与密钥

---

## 目录结构

```
my_ai_assistant/
├── backend/                   # 后端
│   ├── api/v1/                # 路由：auth / characters / rag
│   ├── excel/                 # Excel 查询引擎（表示层 / 解析 / DuckDB / 多步分析 / NL 解析）
│   ├── config/                # 配置（settings.py 读取根目录 .env）
│   ├── database/              # 数据库连接与 Session
│   ├── models/  schemas/      # ORM 模型与 Pydantic 模型
│   ├── services/              # 业务服务（RAG / 向量 / 文档 / LLM / 认证）
│   ├── embeddings/  rag/      # 嵌入与检索链路
│   ├── middleware/  utils/    # 中间件与工具（日志 / 认证 / 异常 / 错误分类）
│   ├── alembic/               # 数据库迁移
│   ├── main.py                # FastAPI 入口
│   └── requirements.txt       # 运行依赖（开发依赖见 requirements-dev.txt）
├── frontend/                  # 前端（Vue 3 + Vite）
├── core/                      # 角色能力示例模块
├── tests/                     # 测试（unit / integration / e2e / fixtures）
├── docs/                      # 项目文档（架构 / API / 部署 / 测试 …）
├── scripts/  nginx/           # 部署脚本与 Nginx 配置
├── docker-compose.yml         # 容器化部署（backend + frontend）
├── .env.example               # 环境变量模板（复制为 .env）
└── README.md
```

---

## 快速开始

### 0. 环境要求

- Python >= 3.9（推荐 3.11）
- Node.js `^20.19.0 || >=22.12.0`
- 可选：Docker / Docker Compose（容器化部署）

### 1. 安装依赖

```bash
# 后端（在仓库根目录创建虚拟环境）
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux / macOS
pip install -r backend/requirements.txt
pip install -r backend/requirements-dev.txt   # 可选：测试与代码检查

# 前端
cd frontend && npm install && cd ..
```

### 2. 配置环境变量

```bash
copy .env.example .env            # Windows
# cp .env.example .env            # Linux / macOS
```

编辑 `.env`，至少填写：

| 变量 | 必填 | 说明 |
|------|------|------|
| `JWT_SECRET` | ✅ | JWT 签名密钥，无默认值，缺失时后端启动失败。生成：`python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `LLM_PROVIDER` | ✅ | `openai` / `dashscope` / `zhipu` / `ollama` / `local` |
| `API_KEY` | 条件 | 使用 openai / dashscope / zhipu 时必填；ollama / local 可留空 |
| `LLM_BASE_URL` | 条件 | OpenAI 兼容接口地址（留空则用供应商默认地址） |
| `LLM_MODEL` | ✅ | Chat 模型名 |
| `EMBEDDING_MODEL` | ✅ | 向量化模型名（与 Chat 模型解耦） |
| `DATABASE_URL` | ❌ | 默认 `sqlite:///./my_ai_assistant.db` |
| `BACKEND_CORS_ORIGINS` | ❌ | 前端来源白名单，逗号分隔 |

> ⚠️ `.env` 已被 `.gitignore` 忽略，**不要提交真实密钥**。
> ⚠️ 配置在进程启动时读取一次，修改后需**重启后端**才生效（启动日志会打印当前生效的模型配置，不含密钥）。

### 3. 初始化数据库（可选）

```bash
python backend/init_db.py
```

会创建数据表，并生成初始账号：

- 管理员：`admin`（口令取自环境变量 `INIT_ADMIN_PASSWORD`；未设置则**随机生成并打印一次**）
- 测试用户：`test`（仅非生产环境；口令取自 `INIT_TEST_PASSWORD` 或随机生成）

### 4. 启动后端（默认 http://127.0.0.1:8000）

在**仓库根目录**执行（推荐，`backend.main:app` 需要以根目录为工作目录）：

```bash
# Windows
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload

# Linux / macOS
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

也可以直接运行 `python backend/main.py`（内部使用 uvicorn 启动，端口见配置）。

### 5. 启动前端（默认 http://localhost:5173）

```bash
cd frontend
npm run dev
```

Vite 已配置代理：`/api` → `http://127.0.0.1:8000`（见 `frontend/vite.config.ts`）。

### 6. 访问

| 服务 | 地址 |
|------|------|
| 前端页面 | http://localhost:5173 |
| 后端 API | http://localhost:8000 |
| Swagger 文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |

---

## API 概览

完整、始终最新的接口定义请以 **`http://localhost:8000/docs`（Swagger）** 为准。

| 分组 | 主要端点 |
|------|----------|
| 认证 | `POST /api/v1/auth/register`、`POST /api/v1/auth/login`、`GET /api/v1/auth/me`、`POST /api/v1/auth/refresh`、`POST /api/v1/auth/logout` |
| 角色 | `GET/POST /api/v1/characters`、`PUT/DELETE /api/v1/characters/{id}`、`POST /api/v1/characters/{id}/speak`、`POST /api/v1/characters/{id}/speak/stream` |
| 知识库 | `POST /api/v1/rag/upload`、`POST /api/v1/rag/query`、`POST /api/v1/rag/query-with-history`、`GET /api/v1/rag/documents`、`DELETE /api/v1/rag/documents`、`GET /api/v1/rag/collection-info` |
| Excel | `GET /api/v1/rag/excel/{document_id}/preview`、`POST /api/v1/rag/excel/{document_id}/query`、`POST /api/v1/rag/excel/{document_id}/aggregate`、`POST /api/v1/rag/excel/nl-query` |

除 `/health`、`/docs` 与认证接口外，其余接口均需在请求头携带 `Authorization: Bearer <token>`。

---

## 测试

```bash
# 后端（在仓库根目录运行；需要根目录存在 .env，其中包含 JWT_SECRET）
python -m pytest tests/unit -q
python -m pytest tests/unit/test_excel_nl_language.py -q     # 单文件

# 前端
cd frontend
npm run lint          # oxlint + eslint
npm run type-check    # vue-tsc
npm run build         # 生产构建
```

说明：

- Excel 相关用例以仓库根目录的样例表格（`直邮一店*.xlsx`、`直邮5店*.xlsx`）为夹具；
  仓库**不包含**这些业务数据文件，缺失相关用例会自动 `skip`（不会失败）。
- `backend/document_loaders/` 属可选能力（依赖 PyMuPDF / Pillow / python-docx 等，均未列入 `requirements.txt`），
  未安装这些库时对应用例会自动 `skip`，不会导致收集失败。
- 需要真实 LLM 的端到端用例不在 `tests/unit` 内，请参考 `docs/testing.md`。

---

## Docker 部署

```bash
# 1. 准备 .env（生产务必设置强随机 JWT_SECRET / ALLOWED_HOSTS）
cp .env.example .env

# 2. 构建并启动（frontend 暴露 80 端口，反向代理 /api 到 backend）
docker-compose up -d

# 查看日志 / 停止
docker-compose logs -f
docker-compose down
```

数据（SQLite + ChromaDB）通过 `backend_data` 卷持久化，不会写入镜像。

---

## 安全须知

1. **`.env` 不入库**：仓库仅保留 `.env.example`。若曾把真实密钥写入代码或提交历史，请立即在供应商侧**吊销并轮换**。
2. **`JWT_SECRET` 必须为强随机值**，生产环境缺失会导致服务拒绝启动（这是有意的安全失败）。
3. **用户数据不入库**：`data/`（上传的表格原件、解析产物、向量库）、`*.db`、`*.xlsx` 等已在 `.gitignore` 中排除，请勿强制 `git add -f` 提交。
4. 生产环境请设置 `ENVIRONMENT=production` 与 `ALLOWED_HOSTS`，并配置 HTTPS 反向代理。

---

## 文档索引

| 文档 | 说明 |
|------|------|
| [docs/setup.md](docs/setup.md) | 安装指南 |
| [docs/architecture.md](docs/architecture.md) | 系统架构与目录说明 |
| [docs/api.md](docs/api.md) | API 端点说明（以 `/docs` 为准） |
| [docs/deployment.md](docs/deployment.md) | 部署方式 |
| [docs/development.md](docs/development.md) | 开发流程与代码规范 |
| [docs/testing.md](docs/testing.md) | 测试策略与运行方法 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 常见问题排查 |

> 部分文档中的路径 / 端口可能滞后于实现，冲突时以本文档与 `http://localhost:8000/docs` 为准。

---

## 许可证

本项目暂未指定开源许可证。如需公开发布，请补充 `LICENSE` 文件（如 MIT / Apache-2.0），
否则默认保留全部权利（All rights reserved）。
