#!/bin/bash

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'  # No Color

# 打印带颜色的消息
print_message() {
  echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
  echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
  echo -e "${RED}[ERROR]${NC} $1"
}

# 检查Docker和Docker Compose
check_dependencies() {
  print_message '检查依赖...'

  if ! command -v docker &> /dev/null; then
    print_error 'Docker未安装'
    exit 1
  fi

  if ! command -v docker-compose &> /dev/null; then
    print_error 'Docker Compose未安装'
    exit 1
  fi

  print_message '依赖检查通过'
}

# 加载环境变量
load_env() {
  if [ -f .env ]; then
    print_message '加载环境变量...'
    source .env
  else
    print_warning '.env文件不存在，创建示例...'
    cp .env.example .env 2>/dev/null || create_example_env
    print_warning '请编辑.env文件并重新运行脚本'
    exit 1
  fi
}

# 创建示例环境文件
create_example_env() {
  cat > .env.example << EOF
# 应用配置
ENV=production
HOST=0.0.0.0
PORT=8000
DEBUG=False

# 安全配置
SECRET_KEY='iBaE6BgWJTzMBjm0yoHLhzWrzSe9dVWv'

# 大模型配置
LLM_PROVIDER=qianfan
QIANFAN_API_KEY='k1S3A8bVjorhQdrs2dBXkgmY'
QIANFAN_SECRET_KEY'iBaE6BgWJTzMBjm0yoHLhzWrzSe9dVWv'

# 数据库配置
DATABASE_URL=sqlite:///./data/ai_assistant.db

# CORS配置
BACKEND_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173

# 日志配置
LOG_LEVEL=INFO
EOF
}

# 构建Docker镜像
build_images() {
  print_message '构建Docker镜像...'
  docker-compose build
}

# 启动服务
start_services() {
  print_message '启动服务...'
  docker-compose up -d

  # 等待服务启动
  print_message '等待服务启动...'
  sleep 10

  # 检查服务状态
  check_service_health
}

# 检查服务健康状态
check_service_health() {
  print_message '检查服务健康状态...'

  if curl -f http://localhost:8000/health >/dev/null 2>&1; then
    print_message ' 后端服务运行正常'
  else
    print_error ' 后端服务可能又问题'
  fi
}

# 停止服务
stop_services() {
  print_message '停止服务...'
  docker-compose down
}

# 查看日志
view_logs() {
  print_message '查看日志...'
  docker-compose logs -f
}

# 清理资源
cleanup() {
  print_message '清理未使用的Docker资源...'
  docker-compose prune -f
}

# 备份数据
backup_data() {
  print_message '备份数据...'
  timestamp=$(date +%Y%m%d_%H%M%S)
  backup_dir='backups/$timestamp'

  mkdir -p "$backup_dir"

  # 备份数据卷
  docker run --rm -v ai_assistant_data:/data -v $(pwd)/$backup_dir:/backup alpine \
  tar czf /backup/data.tar.gz -C /data .

  print_message '数据已备份到：$backup_dir/data.tar.gz'
}

# 主菜单
main_menu() {
  echo '============================'
  echo '    AI助手部署脚本'
  echo '============================'
  echo '1. 完整部署（构建+启动）'
  echo '2. 仅启动服务'
  echo '3. 停止服务'
  echo '4. 查看日志'
  echo '5. 备份数据'
  echo '6. 清除资源'
  echo '7. 退出'
  echo '============================'

  read -p '请选择操作 [1-7]: ' choice

  case $choice in
    1)
      check_dependencies
      load_env
      build_images
      start_services
      ;;
    2)
      check_dependencies
      load_env
      start_services
      ;;
    3)
      stop_services
      ;;
    4)
      view_logs
      ;;
    5)
      backup_data
      ;;
    6)
      cleanup
      ;;
    7)
      print_message '退出脚本'
      exit 0
      ;;
    *)
      print_error '无效选择'
      ;;
  esac

  # 返回主菜单
  main_menu
}

# 执行主菜单
main_menu