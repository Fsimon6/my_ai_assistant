# 部署指南

本项目支持多种部署方式：

1. **本地开发** - Windows/Linux/Mac 开发环境
2. **Docker 部署** - 容器化部署（推荐生产）
3. **云服务器部署** - 阿里云/腾讯云/AWS
4. **Windows 专用** - 使用批处理脚本

## 环境要求

### 开发环境

- Python >= 3.9
- Node.js ^20.19.0 || >=22.12.0
- Git
- 4GB RAM（推荐 8GB）

### 生产环境

- CPU: 2 核+
- 内存：4GB+
- 存储：20GB+
- 操作系统：Ubuntu 20.04+ / Windows Server 2019+

## 方式一：Windows 本地部署

### 1. 克隆项目

```bash
git clone https://github.com/FSimon/my_ai_assistant.git
cd my_ai_assistant
```

### 2. 后端设置

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
copy ..\.env.example ..\.env   # 编辑 .env 文件，填入必要的 API 密钥
python init_db.py
python main.py
```

### 3. 前端设置

```bash
cd frontend
npm install
npm run dev
```

### 4. 使用一键启动脚本

```bash
# 在项目根目录运行
start_backend.bat   # 启动后端（自动创建虚拟环境、安装依赖）
```

## 方式二：Docker 部署（推荐）

### 1. 安装 Docker

- Windows：安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)，确保使用 WSL2 后端
- Linux：`curl -fsSL https://get.docker.com | sh`

### 2. 使用 docker-compose

```bash
# 构建并启动所有服务
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down
```

### 3. 开发环境（含监控工具）

```bash
docker-compose -f docker-compose-dev.yml up -d
```

开发环境额外启动：ChromaDB、PostgreSQL、pgAdmin、Redis Commander、Prometheus、Grafana。

### 4. 自定义配置

编辑 `docker-compose.yml` 和 `.env` 文件调整配置。

## 方式三：云服务器部署（Linux）

### 1. 准备服务器

```bash
ssh root@your-server-ip                  # 连接服务器
apt update && apt upgrade -y             # 更新系统
curl -fsSL https://get.docker.com | sh   # 安装 Docker
systemctl start docker
systemctl enable docker
```

### 2. 部署项目

```bash
git clone https://github.com/FSimon/my_ai_assistant.git
cd my_ai_assistant
cp .env.example .env    # 编辑 .env 文件，设置生产环境变量
docker-compose up -d
```

### 3. 配置 Nginx 反向代理（可选）

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /api {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
    }
}
```

参考项目中的 `nginx/nginx.conf` 配置文件。

## 方式四：使用部署脚本

### Linux/Mac/Git Bash

```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh
```

### 数据库备份

```bash
# 自动备份脚本
./scripts/backup_db.sh

# 手动备份
cp backend/my_ai_assistant.db backups/       # 备份数据库
cp -r data/chroma_db backups/                # 备份向量数据库
```

## 验证部署

访问以下地址确认服务正常运行：

| 服务 | 地址 |
|------|------|
| 前端 | http://localhost:5173 |
| 后端 API | http://localhost:8000 |
| API 文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |

## 生产环境配置要点

1. 修改 `.env` 中 `ENVIRONMENT=production`
2. 设置 `JWT_SECRET` 为强随机字符串（必填，缺失应用无法启动）
3. 设置通用 `API_KEY`（OpenAI / DashScope / 智谱 需要；Ollama / local 可留空），并确认 `LLM_PROVIDER` 与 `LLM_BASE_URL` / `LLM_MODEL` 匹配
4. 生产环境建议使用 PostgreSQL，修改 `DATABASE_URL`
4. 配置 HTTPS（通过 Nginx + Let's Encrypt）
5. 设置定期数据库备份（`scripts/backup_db.sh`）

## 常见部署问题

### Windows 上 Docker 运行缓慢

- 确保 Docker Desktop 使用 WSL2 后端
- 增加资源限制（CPU/内存）

### 端口冲突

```bash
# Windows
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# Linux/Mac
lsof -i :8000
kill -9 <PID>
```

### 数据库迁移失败

```bash
cd backend
alembic downgrade base
alembic upgrade head
```
