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

set "BACKEND_HOST="
for /f "delims=" %%H in ('call ".venv\Scripts\python.exe" -c "from backend.config.settings import settings; print(settings.HOST or '127.0.0.1')"') do set "BACKEND_HOST=%%H"
if not defined BACKEND_HOST (
    echo [ERROR] 无法解析后端 HOST，请检查 .env 中的 HOST 配置
    pause
    exit /b 1
)

set "BACKEND_PORT="
for /f "delims=" %%P in ('call ".venv\Scripts\python.exe" -c "from backend.config.settings import settings; print(settings.PORT or 8021)"') do set "BACKEND_PORT=%%P"
if not defined BACKEND_PORT (
    echo [ERROR] 无法解析后端 PORT，请检查 .env 中的 PORT 配置
    pause
    exit /b 1
)

echo 后端地址 : http://%BACKEND_HOST%:%BACKEND_PORT%
echo API 文档 : http://%BACKEND_HOST%:%BACKEND_PORT%/docs
echo 按 Ctrl+C 停止
echo.

".venv\Scripts\python.exe" -m uvicorn backend.main:app --host "%BACKEND_HOST%" --port "%BACKEND_PORT%" --reload
pause
