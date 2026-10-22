@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo ========================================
echo    My AI Assistant - 后端启动（8021）
echo ========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] 未找到 .venv\Scripts\python.exe
    echo         请先创建虚拟环境并安装 backend\requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] 未找到根目录 .env
    echo         请先复制 .env.example 为 .env，并填写 JWT_SECRET / API_KEY
    pause
    exit /b 1
)

echo 后端地址 : http://127.0.0.1:8021
echo API 文档 : http://127.0.0.1:8021/docs
echo 按 Ctrl+C 停止
echo.

".venv\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 8021 --reload
pause
