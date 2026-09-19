@echo off
chcp 65001 > nul
echo ========================================
echo    My AI Assistant - API测试脚本
echo ========================================
echo.

:: 激活虚拟环境
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

echo 🧪 运行API测试...
python backend\test_api.py

echo.
echo ✅ 测试完成
pause