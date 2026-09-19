# 测试指南

## 测试结构

```
tests/
├── unit/              # 单元测试
├── integration/       # 集成测试
├── e2e/               # 端到端测试
├── fixtures/          # 测试数据
└── conftest.py        # pytest 配置
```

## 后端测试

### 运行测试

```bash
cd backend

# 运行所有测试
pytest tests/ -v

# 仅单元测试
pytest tests/unit/ -v

# 仅集成测试
pytest tests/integration/ -v

# 运行指定测试文件
pytest tests/unit/test_embeddings.py -v

# 运行指定测试函数
pytest tests/unit/test_embeddings.py::test_embed_text -v
```

### 覆盖率报告

```bash
cd backend

# 生成 HTML 覆盖率报告
pytest tests/ --cov=. --cov-report=html

# 生成 XML 覆盖率报告
pytest tests/ --cov=. --cov-report=xml

# 查看报告
# 打开 backend/htmlcov/index.html
```

### 测试配置

测试配置在 `pyproject.toml` 和 `backend/conftest.py` 中定义：
- 测试发现路径：`tests/`
- 覆盖率最小覆盖率：可在配置中设置
- 测试标记：支持 `@pytest.mark.unit`、`@pytest.mark.integration`

## 前端测试

### 代码检查

```bash
cd frontend

# ESLint + oxlint 代码检查
npm run lint

# TypeScript 类型检查
npm run type-check
```

### 构建检查

```bash
cd frontend

# 生产构建（包含类型检查）
npm run build
```

## Makefile 一键测试

```bash
# 运行所有测试（前后端）
make test

# 仅后端测试
make test-backend

# 仅前端测试
make test-frontend

# 生成覆盖率报告
make coverage
```
