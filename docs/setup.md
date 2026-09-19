# 安装指南

## 环境要求

- Python >= 3.9
- Node.js ^20.19.0 || >=22.12.0
- Git
- 4GB RAM（推荐 8GB）
- Docker（可选，用于容器化部署）

## 后端安装

### 1. 创建虚拟环境

```bash
python -m venv .venv
source .venv/bin/activate       # Linux/Mac
.venv\Scripts\activate          # Windows
```

### 2. 安装依赖

```bash
cd backend
pip install -r requirements.txt
pip install -r requirements-dev.txt   # 开发依赖（可选）
```

### 3. 配置环境变量

```bash
cp .env.example .env            # Linux/Mac
copy .env.example .env          # Windows
```

编辑 `.env` 文件，至少配置一个 LLM 提供商的 API 密钥。详细配置项说明请参考根目录 [README.md](../README.md#环境变量配置)。

### 4. 初始化数据库

```bash
python init_db.py
```

### 5. 运行后端

```bash
python main.py
```

启动后访问 http://localhost:8000/docs 查看 API 文档。

> **Windows 快捷方式**：在项目根目录运行 `start_backend.bat`，会自动创建虚拟环境、安装依赖并启动服务。

## 前端安装

### 1. 安装依赖

```bash
cd frontend
npm install
```

> 也可以使用 pnpm：`pnpm install`

### 2. 运行前端

```bash
npm run dev
```

启动后访问 http://localhost:5173。

前端开发服务器已配置代理，`/api` 请求会自动转发到后端 `http://127.0.0.1:8000`。

## Docker 部署

```bash
# 构建并启动所有服务
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down
```

详细部署方式请参考 [部署指南](deployment.md)。
