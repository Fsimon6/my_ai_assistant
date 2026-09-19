# 系统架构

## 技术栈概览

| 层级 | 技术 | 说明 |
|------|------|------|
| 前端 | Vue 3 + Element Plus + Pinia | SPA 单页应用 |
| 后端 | FastAPI + Uvicorn | 异步 ASGI 框架 |
| ORM | SQLAlchemy + Alembic | 数据库映射与迁移 |
| 向量数据库 | ChromaDB | 本地向量存储与检索 |
| 嵌入模型 | sentence-transformers | 本地嵌入（BAAI/bge-small-zh-v1.5） |
| LLM | 百度千帆（默认）/ OpenAI / 智谱 | 大语言模型推理 |
| 缓存 | Redis | 会话缓存与速率限制 |
| 数据库 | SQLite（开发）/ PostgreSQL（生产） | 关系型数据存储 |

## 架构图

```
┌─────────────┐     HTTP/API      ┌─────────────┐
│   前端 Vue   │ ←──────────────→ │  FastAPI    │
│  (port 5173)│   Vite Proxy     │  (port 8000) │
└─────────────┘                  └──────┬──────┘
                                        │
                    ┌───────────────────┼───────────────────┐
                    │                   │                   │
              ┌─────┴─────┐     ┌──────┴──────┐    ┌──────┴──────┐
              │  SQLite /  │     │  ChromaDB   │    │   Redis     │
              │ PostgreSQL │     │ (向量数据库)  │    │   (缓存)    │
              └───────────┘     └──────┬──────┘    └─────────────┘
                                       │
                               ┌───────┴───────┐
                               │ 嵌入模型(本地)  │
                               │ bge-small-zh  │
                               └───────────────┘
                                        │
                               ┌────────┴────────┐
                               │  LLM Provider    │
                               │ 千帆/OpenAI/智谱  │
                               └─────────────────┘
```

## 目录结构

```
my_ai_assistant/
├── backend/                    # 后端代码
│   ├── api/v1/                 # API 路由
│   │   ├── auth.py             # 认证（注册/登录/登出/刷新）
│   │   ├── characters.py       # AI 角色管理（CRUD/对话）
│   │   └── rag.py              # RAG 知识库
│   ├── config/                 # 配置模块
│   │   └── settings.py         # pydantic-settings 配置
│   ├── database/               # 数据库连接
│   ├── document_loaders/       # 文档加载器
│   │   ├── pdf_loader.py       # PDF 解析
│   │   ├── docx_loader.py      # Word 解析
│   │   ├── excel_loader.py     # Excel 解析
│   │   ├── txt_loader.py       # 文本解析
│   │   └── image_loader.py     # 图片 OCR
│   ├── embeddings/             # 嵌入服务
│   │   ├── embedding_service.py # 嵌入主服务
│   │   ├── local_embeddings.py  # 本地嵌入模型
│   │   ├── qianfan_embeddings.py # 千帆嵌入
│   │   └── cache_manager.py     # 嵌入缓存
│   ├── middleware/              # 中间件
│   │   ├── auth.py             # 认证中间件
│   │   └── logging.py          # 日志中间件
│   ├── models/                 # SQLAlchemy 数据模型
│   │   ├── user.py             # 用户模型
│   │   └── character.py        # 角色模型
│   ├── prompts/                # 提示词模板
│   │   ├── template/           # Jinja2 模板文件
│   │   ├── chat_templates.py   # 聊天模板
│   │   └── system_prompts.py   # 系统提示词
│   ├── rag/                    # RAG 链路
│   │   ├── chain.py            # RAG 链
│   │   ├── pipeline.py         # RAG 管道
│   │   └── retriever.py        # 检索器
│   ├── schemas/                # Pydantic 请求/响应模型
│   ├── services/               # 业务服务层
│   │   ├── auth_service.py     # 认证服务
│   │   ├── character_service.py # 角色服务
│   │   ├── rag_service.py     # RAG 服务
│   │   ├── llm_service.py     # LLM 调用服务
│   │   ├── document_service.py # 文档处理服务
│   │   ├── vector_service.py  # 向量服务
│   │   └── cache_service.py   # 缓存服务
│   ├── utils/                  # 工具函数
│   │   ├── auth.py            # 认证工具
│   │   ├── exceptions.py      # 异常定义
│   │   ├── logger.py          # 日志配置
│   │   ├── response.py        # 统一响应格式
│   │   ├── retry.py           # 重试装饰器
│   │   └── security.py        # 安全工具
│   ├── main.py                # FastAPI 入口
│   ├── config.py              # 配置类
│   └── requirements.txt       # Python 依赖
├── frontend/                   # 前端代码
│   ├── src/
│   │   ├── api/                # API 调用层
│   │   ├── components/         # Vue 组件
│   │   ├── composables/        # 组合式函数
│   │   ├── layouts/            # 布局组件
│   │   ├── router/             # Vue Router
│   │   ├── stores/             # Pinia 状态管理
│   │   ├── views/              # 页面视图
│   │   └── main.ts             # 前端入口
│   └── package.json            # 前端依赖
├── docs/                       # 项目文档
├── scripts/                    # 部署/备份脚本
├── tests/                      # 测试代码
│   ├── unit/                   # 单元测试
│   ├── integration/            # 集成测试
│   └── e2e/                    # 端到端测试
├── nginx/                      # Nginx 配置
├── docker-compose.yml          # 生产 Docker 配置
├── docker-compose-dev.yml      # 开发 Docker 配置
├── Makefile                    # 构建命令集合
└── .env.example                # 环境变量模板
```

## 核心流程

### RAG 知识库流程

1. 用户上传文档（PDF/DOCX/TXT/MD）
2. 文档加载器解析并分块
3. 嵌入模型将文本块转为向量
4. 向量存入 ChromaDB
5. 用户提问时，检索相关文档块
6. 组合上下文 + 用户问题，发送给 LLM
7. LLM 生成回答（支持流式输出）

### 认证流程

1. 用户注册/登录，后端返回 JWT 令牌
2. 前端存储令牌（localStorage）
3. 后续请求携带 `Authorization: Bearer <token>`
4. 认证中间件验证令牌有效性
5. 令牌过期后通过 refresh 端点刷新
