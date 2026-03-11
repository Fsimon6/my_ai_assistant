# 部署指南

## 部署选项

本项目支持多种部署方式：

1. **本地开发** - Windows/Linux/Mac 开发环境
2. **Docker部署** - 容器化部署（推荐生产）
3. **云服务器部署** - 阿里云/腾讯云/AWS
4. **Windows专用** - 使用批处理脚本

## 环境要求

### 开发环境
- Python 3.9+
- Node.js 18+
- Git
- 4GB RAM(推荐8GB)

### 生产环境
- CPU: 2核+
- 内存：4GB+
- 存储：20GB+
- 操作系统：Ubuntu 20.04+ / Windows Server 2019+

## 方式一: Windows本地部署

### 1. 克隆项目
'''bash
git clone https:github.com/yourusername/my_ai_assistant.git
cd my_ai_assistant

### 2. 后端设置
cd backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
copy .env.example .env  # 编辑 .env 文件，填入必要的API密钥
python init_db.py
python main.py

### 3. 前端设置
cd frontend
npm install
npm run dev

### 4. 使用一键启动脚本
start_backend.bat   # 启动后端
start_frontend.bat  # 启动前端（新窗口）

## 方式二：Docker部署（推荐）

### 1. 安装Docker Desktop for Windows
- 下载地址：https://www.docker.com/products/docker-desktop/
- 安装后确保Docker服务运行

### 2. 使用docker-compose
docker-compose up -d    # 构建并启动
docker-compose logs -f  # 查看日志
docker-compose down     # 停止服务

### 3. 自定义配置
编辑docker-compose.yml和.env文件调整配置。

## 方式三： 云服务器部署（Linux）

### 1. 准备服务器
ssh root@your-server-ip                 # 连接服务器
apt update && apt upgrade -y            # 更新系统
curl -fsSL https://get.docker.com | sh  # 安装Docker
systemctl start docker
systemctl enable docker

### 2. 部署项目
git clone https://github.com/yourusername/my_ai_assistant.git
cd my_ai_assistant
cp .env.example .env    # 编辑 .env 文件，设置生产环境变量
docker-compose -f docker-compose.prod.yml up -d     # 使用docker-compose部署

### 3. 配置Nginx反向代理（可选）
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

## 方式四：使用部署脚本

### 1. Windows（PowerShell）
.\scripts\deploy.ps1    # 运行部署脚本


### 2. Linux/Mac/Git Bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh

### 3. 环境变量配置
 - 应用配置
ENVIRONMENT=production
SECRET_KEY=your-secret-key

- 数据库
DATABASE_URL=sqlite:///./data/app.db

- 大模型API
OPENAI_API_KEY=sk-xxx

- 或使用百度千帆
QIANFAN_API_KEY=xxx
QIANFAN_SECRET_KEY=xxx

- 向量数据库
VECTOR_DB_PATH=./data/chroma_db

### 4. 验证部署
访问以下地址确认服务正常运行
- 前端：http://localhost:5173
- 后端API：http://localhost:8000
- API文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 5. 常见问题
- Windows上Docker运行缓慢
确保Docker Desktop使用WSL2后端
增加资源限制（CPU/内存）

- 端口冲突
netstat -ano | findstr :8000    # 查找占用端口的进程
taskkill /PID <PID> /F      
 
- 数据库迁移失败
cd backend
alembic downgrade base
alembic upgrade head

### 6. 备份与恢复
- 自动备份脚本
./scripts/backup_db.sh      # Linux/Mac
scripts\backup_db.bat       # Windows

- 手动备份
cp backend/my_ai_assistant.db backups/    # 备份数据库
cp -r data/chroma_db backups/             # 备份到向量数据库
