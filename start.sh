# 一键启动脚本（适用于Git Bash/WSL）

echo "================================"
echo " 我的AI知识库助手 - 启动脚本"
echo "================================"
echo ""

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

# 检查Python
check_python() {
  if ! command -v python &> /dev/null; then
    echo -e "${RED} Python未安装${NC}"
    exit 1
  fi

  python_version=$(python --version 2>&1 | cut -d' ' -f2)
  echo -e "${GREEN} Python$python_version${NC}"
}

# 检查Node.js
check_node() {
  if ! command -v node &> /dev/null; then
    echo -e "${RED} Node.js未安装${NC}"
    exit 1
  fi

  node_version=$(node --version)
  echo -e "${GREEN} Node.js $node_version${NC}"
}

# 检查环境文件
check_env() {
  if [ ! -f "backend/.env" ] && [ -f "backend/.env.example" ]; then
    echo -e "${YELLOW} 未找到.env文件，从示例创建${NC}"
    cp backend/.env.example backend/.env
    echo -e "${YELLOW} 请编辑 backend/.env 文件配置API密钥${NC}"
  fi
}

# 启动后端
start_backend() {
  echo -e "\n${BLUE} 启动后端服务...${NC}"
  cd backend

  # 检查虚拟环境
  if [ -d "venv" ]; then
    source venv/Scripts/activate 2>/dev/null || source venv/bin/activate
  else
    echo -e "${YELLOW} 未找到虚拟环境，使用系统Python${NC}"
  fi

  # 检查数据库
  if [ ! -f "my_ai_assistant.db" ]; then
    echo -e "${YELLOW} 初始化数据库...${NC}"
    python init_db.py
  fi

  # 启动后端(后台运行)
  python main.py &
  BACKEND_PID=$!
  cd ..

  echo -e "${GREEN} 后端服务启动 （PID: $BACKEND_PID)${NC}"
  echo " http://localhost:8000"
  echo " http://localhost:8000/docs"
}

# 启动前端
start_frontend() {
  echo -e "\n${BLUE} 启动前端服务...${NC}"
  cd frontend

  # 检查node_modules
  if [ ! -d "node_modules" ]; then
    echo -e "${YELLOW} 安装前端依赖...${NC}"
    npm install
  fi

  # 启动前端（后台运行）
  npm run dev &
  FRONTEND_PID=$!
  cd ..

  echo -e "${GREEN} 启动前端服务 (PID: $FRONTEND_PID)${NC}"
  echo " http://localhost:5173"
}

# 显示状态
show_status() {
  echo -e "\n${BLUE} 服务状态${NC}"
  echo "=================================="
  echo -e "后端: http://localhost:8000"
  echo -e "API文档: http://localhost:8000/docs"
  echo -e "前端: http://localhost:5173"
  echo "=================================="
  echo -e "${YELLOW}按 Ctrl+C 停止所有服务${NC}"
}

# 清理进程
cleanup() {
  echo -e "\n${YELLOW} 停止服务...${NC}"
  if [ ! -z "$BACKEND_PID" ]; then
    kill $BACKEND_PID 2>/dev/null
    echo "后端已停止"
  fi

  if [ ! -z "FRONTEND_PID" ]; then
    kill $FRONTEND_PID 2>/dev/null
    echo "前端已停止"
  fi

  echo -e "${GREEN} 服务已停止${NC}"
  exit 0
}

# 设置清理钩子
trap cleanup SIGINT SIGTERM

# 主函数
main() {
  echo -e "${BLUE} 检查环境...${NC}"
  check_python
  check_node
  check_env

  start_backend

  # 等待后端启动
  echo -e "\n${YELLOW} 等待后端启动...${NC}"
  sleep 3

  start_frontend
  show_status

  # 等待用户中断
  wait
}

# 执行主函数
main