# API 文档

后端启动后，访问 http://localhost:8000/docs 可查看交互式 Swagger API 文档。

## 基础端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 项目基本信息 |
| GET | `/health` | 健康检查 |
| GET | `/docs` | Swagger API 文档 |

## 认证

认证路由定义在 `backend/api/v1/auth.py` 中。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/auth/register` | 用户注册 |
| POST | `/api/v1/auth/login` | 用户登录 |
| GET | `/api/v1/auth/me` | 获取当前用户信息 |
| POST | `/api/v1/auth/logout` | 登出 |
| POST | `/api/v1/auth/refresh` | 刷新令牌 |

## 角色管理

角色路由定义在 `backend/api/v1/characters.py` 中。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/characters` | 获取所有角色 |
| POST | `/api/v1/characters` | 创建角色 |
| GET | `/api/v1/characters/{id}` | 获取单个角色 |
| PUT | `/api/v1/characters/{id}` | 更新角色 |
| DELETE | `/api/v1/characters/{id}` | 删除角色 |
| POST | `/api/v1/characters/{id}/speak` | 角色对话（非流式） |
| POST | `/api/v1/characters/{id}/speak/stream` | 角色对话（流式） |
| POST | `/api/v1/characters/{id}/batch-speak` | 批量对话 |
| GET | `/api/v1/characters/{id}/stats` | 角色统计 |
| GET | `/api/v1/characters/{id}/conversations` | 对话历史 |

## RAG 知识库

RAG 路由直接定义在 `backend/main.py` 中，已启用。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/rag/upload` | 上传文档（PDF/DOCX/TXT/MD） |
| POST | `/api/v1/rag/query` | 知识库查询 |
| POST | `/api/v1/rag/query-with-history` | 带对话历史的查询 |
| GET | `/api/v1/rag/collection-info` | 获取向量数据库信息 |

### 上传文档示例

```bash
curl -X POST http://localhost:8000/api/v1/rag/upload \
  -F "file=@document.pdf" \
  -F "metadata={\"source\":\"manual\"}"
```

### 查询示例

```bash
curl -X POST http://localhost:8000/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -d '{"query": "你的问题", "context_count": 3}'
```

### 流式查询

```bash
curl -X POST http://localhost:8000/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -d '{"query": "你的问题", "stream": true}'
```

## 注意事项

- 认证和角色管理路由定义在 `backend/api/v1/` 目录下，需要在 `main.py` 中启用 `include_router` 来激活
- RAG 路由直接定义在 `main.py` 中，默认已启用
- 所有需要认证的端点需在请求头中携带 `Authorization: Bearer <token>`
