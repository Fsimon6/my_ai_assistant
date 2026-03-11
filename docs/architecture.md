# 系统架构

## 组件

- 前端: Vue 3 + Element Plus
- 后端: FastAPI + SQLAlchemy
- 向量数据库: ChromaDB
- 大模型: 百度千帆
- 缓存: Redis
- 数据库: SQLite(开发) / PostgreSQL(生产)

## 目录结构
my_ai_assistant/
├── backend/        # 后端代码
├── frontend/       # 前端代码
├── data/           # 数据储存
├── tests/          # 测试
├── docs/           # 文档
└── deploy/         # 部署配置