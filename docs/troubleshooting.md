# 故障排除指南

## 常见问题

### 1. 后端无法启动：ModuleNotFoundError

**问题**：`ModuleNotFoundError: No module named 'xxx'`

**原因**：依赖未安装或虚拟环境未激活。

**解决**：

```bash
cd backend
.venv\Scripts\activate           # Windows，确保激活虚拟环境
# source .venv/bin/activate      # Linux/Mac
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 2. 前端无法连接后端：CORS 错误

**问题**：浏览器控制台报 CORS 错误。

**原因**：后端 CORS 配置未包含前端地址。

**解决**：检查 `.env` 中 `BACKEND_CORS_ORIGINS` 是否包含 `http://localhost:5173`。

### 3. 端口被占用

**问题**：`Address already in use` 或 `Port 8000 is in use`。

**解决**：

```bash
# Windows：查找并终止占用端口的进程
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# Linux/Mac
lsof -i :8000
kill -9 <PID>
```

也可以在 `.env` 中修改 `PORT` 环境变量使用其他端口。

### 4. ChromaDB 初始化失败

**问题**：向量数据库初始化报错。

**解决**：

```bash
# 删除现有 ChromaDB 数据，重新初始化
rm -rf backend/data/chroma_db        # Linux/Mac
rmdir /s /q backend\data\chroma_db   # Windows

# 重启后端，会自动创建新的数据库
python main.py
```

### 5. LLM API 调用失败

**问题**：启动报 `JWT_SECRET` 校验失败，或 LLM 调用返回 401。

**解决**：
1. 确认 `.env` 中已设置 `JWT_SECRET`（必填，无默认值；缺失应用启动即失败）
2. 确认已设置通用 `API_KEY`（OpenAI / DashScope / 智谱 需要；Ollama / local 可留空）
3. 确认 `LLM_PROVIDER` 与 `LLM_BASE_URL` / `LLM_MODEL` 匹配
4. 检查 `API_KEY` 是否有效、是否有余额

### 6. 嵌入模型下载缓慢

**问题**：首次启动时 `sentence-transformers` 下载模型卡住。

**解决**：模型 `BAAI/bge-small-zh-v1.5` 约 100MB，首次启动需下载。如果网络较慢，可以：
- 设置 HuggingFace 镜像：`set HF_ENDPOINT=https://hf-mirror.com`（Windows）
- 或手动下载模型到本地缓存目录

### 7. 数据库迁移失败

**问题**：Alembic 迁移报错。

**解决**：

```bash
cd backend

# 回滚到基础版本，重新迁移
alembic downgrade base
alembic upgrade head

# 或直接重新初始化
python init_db.py
```

### 8. 文档上传失败：不支持的文件类型

**问题**：上传文档时返回 `不支持的文件类型`。

**原因**：后端仅支持 `.pdf`、`.docx`、`.txt`、`.md` 格式。

**解决**：将文档转换为支持的格式后重新上传。

### 9. Windows 编码问题

**问题**：中文显示为乱码。

**原因**：文件编码不是 UTF-8。

**解决**：确保所有文本文件使用 UTF-8 编码保存。VS Code 可在右下角状态栏切换编码。

### 10. Docker 容器启动失败

**问题**：`docker-compose up` 后容器立即退出。

**解决**：
1. 检查端口是否被占用（8000、5173、6379）
2. 检查 `.env` 文件配置是否正确
3. 查看容器日志：`docker-compose logs <service_name>`
4. 尝试重新构建：`docker-compose build --no-cache`
