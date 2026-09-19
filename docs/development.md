# 开发指南

## 开发环境设置

### 1. 克隆项目

```bash
git clone https://github.com/FSimon/my_ai_assistant.git
cd my_ai_assistant
```

### 2. 安装依赖

```bash
# 后端
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # Linux/Mac
cd backend
pip install -r requirements.txt
pip install -r requirements-dev.txt

# 前端
cd ../frontend
npm install
```

### 3. 配置环境变量

```bash
cp .env.example .env            # 编辑 .env，填入 API 密钥
```

### 4. 启动开发服务器

```bash
# 后端（终端 1）
cd backend
python main.py

# 前端（终端 2）
cd frontend
npm run dev
```

### 5. 使用 Makefile（Linux/Mac）

```bash
make dev          # 同时启动前后端
make lint         # 代码检查
make format       # 格式化代码
make test         # 运行测试
```

## 代码规范

### 后端（Python）

- **格式化工具**：Black + isort
- **代码检查**：Flake8（最大行长度 127）
- **类型检查**：mypy
- **命名规范**：
  - 类名：PascalCase（如 `CharacterService`）
  - 函数/变量：snake_case（如 `get_character`）
  - 常量：UPPER_SNAKE_CASE（如 `MAX_TOKENS`）

```bash
# 格式化
cd backend
python -m black .
python -m isort .

# 检查
python -m flake8 . --max-line-length=127
```

### 前端（TypeScript/Vue）

- **格式化工具**：Prettier
- **代码检查**：ESLint + oxlint
- **类型检查**：vue-tsc
- **命名规范**：
  - 组件名：PascalCase（如 `ChatMessage.vue`）
  - 变量/函数：camelCase（如 `sendMessage`）
  - 类型/接口：PascalCase（如 `Character`）

```bash
cd frontend
npm run lint        # 代码检查并自动修复
npm run format      # 格式化
npm run type-check  # TypeScript 类型检查
```

## Git 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

```
<type>(<scope>): <subject>

types:
  feat     新功能
  fix      修复 bug
  docs     文档变更
  style    代码格式（不影响功能）
  refactor 重构
  test     测试
  chore    构建/工具变更
```

示例：
```
feat(rag): 支持流式查询响应
fix(auth): 修复令牌刷新过期问题
docs(readme): 重写 README 运行指令
```

## 分支策略

- `main`：稳定发布分支
- `develop`：开发集成分支
- `feature/*`：功能分支（如 `feature/stream-chat`）
- `fix/*`：修复分支（如 `fix/auth-token`）

## 预提交钩子

项目配置了 pre-commit（见 `.pre-commit-config.yaml`），提交前会自动运行代码检查和格式化。

```bash
# 安装 pre-commit
pip install pre-commit
pre-commit install
```
