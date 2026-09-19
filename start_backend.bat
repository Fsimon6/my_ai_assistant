@echo off
chcp 65001 > nul
echo ========================================
echo    My AI Assistant - FastAPI后端启动脚本
echo ========================================
echo.

:: 检查Python
python --version > nul 2>&1
if errorlevel 1 (
    echo ❌ 未找到Python，请先安装Python 3.8+
    pause
    exit /b 1
)

:: 检查虚拟环境
if not exist ".venv\Scripts\activate.bat" (
    echo ⚠️  未找到虚拟环境，正在创建...
    python -m venv .venv
    if errorlevel 1 (
        echo ❌ 创建虚拟环境失败
        pause
        exit /b 1
    )
    echo ✅ 虚拟环境创建成功
)

:: 激活虚拟环境
echo 📦 激活虚拟环境...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ❌ 激活虚拟环境失败
    pause
    exit /b 1
)

:: 检查依赖
if not exist "backend\requirements.txt" (
    echo ⚠️  未找到依赖文件，正在创建...
    echo fastapi==0.104.1 > backend\requirements.txt
    echo uvicorn[standard]==0.24.0 >> backend\requirements.txt
    echo python-dotenv==1.0.0 >> backend\requirements.txt
    echo pydantic==2.5.0 >> backend\requirements.txt
)

echo 📦 安装依赖...
pip install -r backend\requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo ❌ 安装依赖失败
    pause
    exit /b 1
)

:: 启动服务
echo 🚀 启动FastAPI服务...
echo 📍 服务地址: http://127.0.0.1:8000
echo 📚 API文档: http://127.0.0.1:8000/docs
echo 🛑 按 Ctrl+C 停止服务
echo.

cd backend
python main.py
pause