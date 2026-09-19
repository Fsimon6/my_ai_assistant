#!/bin/bash
# 部署脚本

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 日志函数
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查命令是否存在
check_command() {
    if ! command -v $1 &> /dev/null; then
        log_error "未找到 $1，请先安装"
        exit 1
    fi
}

# 显示部署菜单
show_menu() {
    echo "=== 部署我的AI知识库助手 ==="
    echo "1. 开发环境部署"
    echo "2. 生产环境部署"
    echo "3. Docker部署"
    echo "4. 云服务器部署"
    echo "5. 退出"
    echo ""
    read -p "请选择部署方式 [1-5]: " choice
    echo ""
}

# 开发环境部署
deploy_development() {
    log_info "开始开发环境部署..."

    # 检查依赖
    check_command python3
    check_command npm

    # 安装后端依赖
    log_info "安装后端依赖..."
    cd backend
    python3 -m venv venv 2>/dev/null || true
    source venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
    pip install -r requirements-dev.txt 2>/dev/null || true

    # 初始化数据库
    log_info "初始化数据库..."
    if [ -f "init_db.py" ]; then
        python init_db.py
    fi

    cd ..

    # 安装前端依赖
    log_info "安装前端依赖..."
    cd frontend
    npm install

    cd ..

    log_success "开发环境部署完成！"
    echo ""
    echo "启动命令:"
    echo "  后端: cd backend && source venv/bin/activate && python main.py"
    echo "  前端: cd frontend && npm run dev"
    echo ""
}

# 生产环境部署
deploy_production() {
    log_info "开始生产环境部署..."

    # 创建生产目录
    DEPLOY_DIR="/opt/my_ai_assistant"
    log_info "部署目录: $DEPLOY_DIR"

    if [ ! -d "$DEPLOY_DIR" ]; then
        sudo mkdir -p $DEPLOY_DIR
    fi

    # 复制项目文件
    log_info "复制项目文件..."
    sudo rsync -av --exclude='node_modules' --exclude='venv' --exclude='.git' --exclude='__pycache__' ./ $DEPLOY_DIR/

    # 设置权限
    sudo chown -R $USER:$USER $DEPLOY_DIR
    sudo chmod -R 755 $DEPLOY_DIR

    # 安装系统依赖
    log_info "安装系统依赖..."
    if command -v apt &> /dev/null; then
        # Ubuntu/Debian
        sudo apt update
        sudo apt install -y python3 python3-pip python3-venv nodejs npm nginx
    elif command -v yum &> /dev/null; then
        # CentOS/RHEL
        sudo yum install -y python3 python3-pip nodejs npm nginx
    fi

    # 安装Python依赖
    log_info "安装Python依赖..."
    cd $DEPLOY_DIR/backend
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt

    # 安装前端依赖并构建
    log_info "构建前端..."
    cd ../frontend
    npm install
    npm run build

    cd ..

    # 配置Nginx
    log_info "配置Nginx..."
    NGINX_CONF="/etc/nginx/sites-available/my_ai_assistant"

    cat << EOF | sudo tee $NGINX_CONF > /dev/null
server {
    listen 80;
    server_name _;

    # 前端静态文件
    location / {
        root $DEPLOY_DIR/frontend/dist;
        try_files \$uri \$uri/ /index.html;
    }

    # 后端API代理
    location /api {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # WebSocket支持
    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
EOF

    # 启用站点
    sudo ln -sf $NGINX_CONF /etc/nginx/sites-enabled/
    sudo nginx -t
    sudo systemctl restart nginx

    # 创建systemd服务
    log_info "创建系统服务..."
    SERVICE_FILE="/etc/systemd/system/my_ai_assistant.service"

    cat << EOF | sudo tee $SERVICE_FILE > /dev/null
[Unit]
Description=My AI Assistant Backend
After=network.target

[Service]
Type=exec
User=$USER
Group=$USER
WorkingDirectory=$DEPLOY_DIR/backend
Environment=PATH=$DEPLOY_DIR/backend/venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONPATH=$DEPLOY_DIR/backend
ExecStart=$DEPLOY_DIR/backend/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    # 启动服务
    sudo systemctl daemon-reload
    sudo systemctl enable my_ai_assistant
    sudo systemctl start my_ai_assistant

    log_success "生产环境部署完成！"
    echo ""
    echo "服务状态: sudo systemctl status my_ai_assistant"
    echo "查看日志: sudo journalctl -u my_ai_assistant -f"
    echo "访问地址: http://服务器IP"
    echo ""
}

# Docker部署
deploy_docker() {
    log_info "开始Docker部署..."

    check_command docker

    # 检查docker-compose
    if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
        log_error "未找到docker-compose"
        exit 1
    fi

    # 创建生产环境配置文件
    if [ ! -f "docker-compose.prod.yml" ]; then
        log_warning "未找到docker-compose.prod.yml，使用开发配置"
        COMPOSE_FILE="docker-compose.yml"
    else
        COMPOSE_FILE="docker-compose.prod.yml"
    fi

    # 检查环境变量文件
    if [ ! -f ".env" ] && [ -f ".env.example" ]; then
        log_warning "未找到.env文件，从示例文件创建"
        cp .env.example .env
        log_info "请编辑 .env 文件配置生产环境变量"
        read -p "按回车键继续..."
    fi

    # 构建并启动
    log_info "构建Docker镜像..."
    docker-compose -f $COMPOSE_FILE build

    log_info "启动服务..."
    docker-compose -f $COMPOSE_FILE up -d

    log_info "等待服务启动..."
    sleep 10

    # 检查服务状态
    log_info "服务状态:"
    docker-compose -f $COMPOSE_FILE ps

    log_success "Docker部署完成！"
    echo ""
    echo "查看日志: docker-compose -f $COMPOSE_FILE logs -f"
    echo "停止服务: docker-compose -f $COMPOSE_FILE down"
    echo "访问地址: http://localhost:8080"
    echo ""
}

# 云服务器部署
deploy_cloud() {
    log_info "开始云服务器部署..."

    echo "请选择云服务商:"
    echo "1. 阿里云"
    echo "2. 腾讯云"
    echo "3. AWS"
    echo "4. 其他"
    echo ""
    read -p "请选择 [1-4]: " cloud_choice

    case $cloud_choice in
        1)
            deploy_aliyun
            ;;
        2)
            deploy_tencent
            ;;
        3)
            deploy_aws
            ;;
        4)
            log_info "请参考以下步骤手动部署:"
            echo "1. 创建云服务器实例"
            echo "2. 配置安全组开放80、443、22端口"
            echo "3. 通过SSH连接服务器"
            echo "4. 运行 ./scripts/deploy.sh 选择生产环境部署"
            ;;
        *)
            log_error "无效选择"
            ;;
    esac
}

# 阿里云部署
deploy_aliyun() {
    log_info "阿里云部署指南:"
    echo ""
    echo "1. 登录阿里云控制台: https://ecs.console.aliyun.com"
    echo "2. 创建ECS实例，推荐配置:"
    echo "   - 系统: Ubuntu 20.04 或 CentOS 8"
    echo "   - 配置: 2核4GB或更高"
    echo "   - 带宽: 最低3Mbps"
    echo "3. 安全组开放端口: 80(HTTP), 443(HTTPS), 22(SSH)"
    echo "4. 通过SSH连接服务器:"
    echo "   ssh root@你的服务器IP"
    echo "5. 在服务器上运行:"
    echo "   git clone 你的项目地址"
    echo "   cd my_ai_assistant"
    echo "   ./scripts/deploy.sh"
    echo "6. 选择生产环境部署"
    echo ""
}

# 腾讯云部署
deploy_tencent() {
    log_info "腾讯云部署指南:"
    echo ""
    echo "1. 登录腾讯云控制台: https://console.cloud.tencent.com/cvm"
    echo "2. 创建CVM实例，推荐配置:"
    echo "   - 系统: Ubuntu 20.04 或 CentOS 8"
    echo "   - 配置: 2核4GB或更高"
    echo "   - 带宽: 最低3Mbps"
    echo "3. 安全组开放端口: 80(HTTP), 443(HTTPS), 22(SSH)"
    echo "4. 通过SSH连接服务器:"
    echo "   ssh root@你的服务器IP"
    echo "5. 在服务器上运行:"
    echo "   git clone 你的项目地址"
    echo "   cd my_ai_assistant"
    echo "   ./scripts/deploy.sh"
    echo "6. 选择生产环境部署"
    echo ""
}

# AWS部署
deploy_aws() {
    log_info "AWS部署指南:"
    echo ""
    echo "1. 登录AWS控制台: https://console.aws.amazon.com/ec2"
    echo "2. 创建EC2实例，推荐配置:"
    echo "   - 系统: Ubuntu 20.04 或 Amazon Linux 2"
    echo "   - 实例类型: t3.medium或更高"
    echo "   - 存储: 最少20GB"
    echo "3. 安全组开放端口: 80(HTTP), 443(HTTPS), 22(SSH)"
    echo "4. 通过SSH连接服务器:"
    echo "   ssh -i your-key.pem ubuntu@你的服务器IP"
    echo "5. 在服务器上运行:"
    echo "   git clone 你的项目地址"
    echo "   cd my_ai_assistant"
    echo "   ./scripts/deploy.sh"
    echo "6. 选择生产环境部署"
    echo ""
}

# 主函数
main() {
    echo "=========================================="
    echo "    我的AI知识库助手部署脚本"
    echo "=========================================="
    echo ""

    while true; do
        show_menu

        case $choice in
            1)
                deploy_development
                break
                ;;
            2)
                deploy_production
                break
                ;;
            3)
                deploy_docker
                break
                ;;
            4)
                deploy_cloud
                break
                ;;
            5)
                log_info "退出部署脚本"
                exit 0
                ;;
            *)
                log_error "无效选择，请重新输入"
                ;;
        esac
    done
}

# 运行主函数
main