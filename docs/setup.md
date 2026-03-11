# 安装指南

## 环境需求

- Python 3.10+
- Node.ts 18+
- Docker

## 后端安装

1. 创建虚拟环境
'''bash
python -m venv venv
source venv/bin/activate    # Linux/Mac
venv\Script\activate        # Windows

2. 安装依赖
cd backend
pip install -r requirements.txt

3. 配置环境变量
复制 .env.example 为 .env 并修改配置

4. 初始化数据库
python init_db.py

5. 运行后端
python main.py

## 前端安装

1. 安装依赖
cd frontend
npm install

2. 运行前端
npm run dev

## Docker部署

1. 构建镜像
docker-compose build

2. 启动服务
docker-compose up -d