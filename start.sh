#!/usr/bin/env bash
# 一键启动（Git Bash / WSL）：后端 8021 + 前端 5181
# 说明：
#   * 后端端口由 `uvicorn --port` 显式指定（开发环境的唯一实际生效来源）；
#   * `.env` 在进程启动时读取一次，改完需重跑本脚本；
#   * 前端端口来自 frontend/vite.config.ts 的 server.port（5181）。
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

BACKEND_PORT=8021
FRONTEND_PORT=5181

if [ ! -f "$ROOT/.env" ]; then
  echo -e "${YELLOW}未找到根目录 .env：请先复制 .env.example 为 .env 并填写 JWT_SECRET / API_KEY${NC}"
  exit 1
fi

PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo -e "${RED}未找到 .venv Python：请先创建虚拟环境并安装 backend/requirements.txt${NC}"
  exit 1
fi

# 首次初始化：仅当 SQLite 数据库文件尚不存在时执行（已存在的库不会重复初始化）。
# **存在性判断由 Python 完成**：实测 Git Bash 下 Python 输出的中文/反斜杠路径
# 经命令替换后，Bash 的 `-f` 会把实际存在的库误判为不存在（仓库路径含"副本"）。
# 因此这里只向 Bash 返回 ASCII 状态，Bash 不再对 Windows 路径做文件判断。
DB_STATE="$("$PY" - <<'PY'
import os
import sys

sys.path.insert(0, os.getcwd())
from backend.config.settings import settings  # noqa: E402

url = (settings.DATABASE_URL or '').strip()
prefix = 'sqlite:///'
if not url.startswith(prefix):
    print('NON_SQLITE')
else:
    path = url[len(prefix):]
    absolute = os.path.abspath(path) if not os.path.isabs(path) else path
    print('EXISTS' if os.path.exists(absolute) else 'MISSING')
PY
)"
if [ "$DB_STATE" = "MISSING" ]; then
  echo -e "${YELLOW}首次启动：初始化数据库与管理员账号（python -m backend.init_db）${NC}"
  "$PY" -m backend.init_db
elif [ "$DB_STATE" = "NON_SQLITE" ]; then
  echo -e "${YELLOW}非 SQLite 的 DATABASE_URL：请按部署文档手动执行一次 python -m backend.init_db${NC}"
fi

echo -e "${GREEN}backend  -> http://127.0.0.1:${BACKEND_PORT}${NC}"
"$PY" -m uvicorn backend.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --reload &
BACKEND_PID=$!

echo -e "${GREEN}frontend -> http://127.0.0.1:${FRONTEND_PORT}${NC}"
( cd "$ROOT/frontend" && npm run dev ) &
FRONTEND_PID=$!

cleanup() {
  echo -e "\n${YELLOW}停止服务...${NC}"
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null
  exit 0
}
trap cleanup INT TERM

echo -e "${YELLOW}Ctrl+C 停止；后端 :${BACKEND_PORT}，前端 :${FRONTEND_PORT}${NC}"
wait
