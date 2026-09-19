FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先装依赖（利用镜像层缓存）
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# 复制应用代码（不复制 .env：密钥通过 compose environment 注入，绝不进镜像）
COPY backend/ /app/backend/

EXPOSE 8000

# 单 worker：SQLite + 进程内锁/登录限流仅单进程有效；
# 多 worker 部署需改 PostgreSQL + Redis 并替换进程内锁/限流。
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
