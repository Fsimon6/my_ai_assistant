# 我的AI知识库助手
# 常用命令集合

.PHONY: help install dev test build deploy clean lint

# 项目信息
PROJECT_NAME = my_ai_assistant
VERSION = 1.0.0

# 颜色定义
GREEN = \033[0;32m
RED = \033[0;31m
YELLOW = \033[0;33m
BLUE = \033[0;34m
NC = \033[0m	# 无色

# 帮助
help:
	@echo "$(GREEN)=== $(PROJECT_NAME) 项目命令 ===$(NC)"
	@echo ""
	@echo "$(BLUE)开发环境:$(NC)"
	@echo "  $(YELLOW)make install$(NC)     安装所有依赖"
	@echo "  $(YELLOW)make dev$(NC)         启动开发环境"
	@echo "  $(YELLOW)make dev-frontend$(NC) 仅启动前端开发服务器"
	@echo "  $(YELLOW)make dev-backend$(NC)  仅启动后端开发服务器"
	@echo ""
	@echo "$(BLUE)代码质量:$(NC)"
	@echo "  $(YELLOW)make lint$(NC)        运行代码检查"
	@echo "  $(YELLOW)make format$(NC)      格式化代码"
	@echo "  $(YELLOW)make type-check$(NC)  类型检查"
	@echo ""
	@echo "$(BLUE)测试:$(NC)"
	@echo "  $(YELLOW)make test$(NC)        运行所有测试"
	@echo "  $(YELLOW)make test-backend$(NC) 运行后端测试"
	@echo "  $(YELLOW)make test-frontend$(NC) 运行前端测试"
	@echo "  $(YELLOW)make coverage$(NC)    生成测试覆盖率报告"
	@echo ""
	@echo "$(BLUE)构建与部署:$(NC)"
	@echo "  $(YELLOW)make build$(NC)       构建项目"
	@echo "  $(YELLOW)make docker-build$(NC) 构建Docker镜像"
	@echo "  $(YELLOW)make docker-up$(NC)   启动Docker容器"
	@echo "  $(YELLOW)make docker-down$(NC) 停止Docker容器"
	@echo "  $(YELLOW)make deploy$(NC)      部署到生产环境"
	@echo ""
	@echo "$(BLUE)数据库:$(NC)"
	@echo "  $(YELLOW)make db-init$(NC)     初始化数据库"
	@echo "  $(YELLOW)make db-migrate$(NC)  运行数据库迁移"
	@echo "  $(YELLOW)make db-reset$(NC)    重置数据库"
	@echo ""
	@echo "$(BLUE)清理:$(NC)"
	@echo "  $(YELLOW)make clean$(NC)       清理构建产物"
	@echo "  $(YELLOW)make clean-all$(NC)   清理所有生成文件"
	@echo ""
	@echo "$(BLUE)其他:$(NC)"
	@echo "  $(YELLOW)make open-api$(NC)    打开API文档"
	@echo "  $(YELLOW)make version$(NC)     显示版本信息"

# 版本信息
version:
	@echo "$(GREEN)$(PROJECT_NAME) v$(VERSION)$(NC)"

# 安装依赖
install: install-backend install-frontend

install-backend:
	@echo "$(BLUE)安装后端依赖...$(NC)"
	cd backend && pip install -r requirements.txt
	@echo "$(GREEN)后端依赖安装完成$(NC)"

install-frontend:
	@echo "$(BLUE)安装前端依赖...$(NC)"
	cd frontend && pip npm install
	@echo "$(GREEN)前端依赖安装完成$(NC)"

# 开发环境
dev: dev-backend dev-frontend

dev-backend:
	@echo "$(BLUE)启动后端开发服务器...$(NC)"
	cd backend && python main.py

dev-frontend:
	@echo "$(BLUE)启动前端开发服务器...$(NC)"
	cd frontend && python npm run dev

# 代码质量
lint: lint-backend lint-frontend

lint-backend:
	@echo "$(BLUE)检查后端代码...$(NC)"
	cd backend && python -m flake8 . --count --select=E9,F63,F7,F82 --show-source --ststistics
	cd backend && python -m flake8 . --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics

lint-frontend:
	@echo "$(BLUE)检查前端代码...$(NC)"
	cd frontend && npm run lint

format: format-backend format-frontend

format-backend:
	@echo "$(BLUE)格式化后端代码...$(NC)"
	cd backend && python -m black .
	cd backend && python -m isort .

format-frontend:
	@echo "$(BLUE)格式化前端代码...$(NC)"
	cd frontend && npm run format

type-check:
	@echo "$(BLUE)运行类型检查...$(NC)"
	cd backend && python -m mypy

# 测试
test: test-backend test-frontend

test-backend:
	@echo "$(BLUE)运行后端测试...$(NC)"
	cd backend && python -m pytest tests/ -v --cov=. --cov-report=html

test-frontend:
	@echo "$(BLUE)运行前端测试...$(NC)"
	cd frontend && npm run test:unit

coverage:
	@echo "$(BLUE)生成测试覆盖率报告...$(NC)"
	cd backend && python -m pytest tests/ --cov=. --cov-report=html --cov-report=xml
	@echo "$(GREEN)覆盖率报告已生成：backend/htmlcov/index.html$(NC)"

# 构建
build: build-backend build-frontend

build-backend:
	@echo "$(BLUE)构建后端...$(NC)"
	cd backend && python -m py_compile**/*.py

build-frontend:
	@echo "$(BLUE)构建前端...$(NC)"
	cd frontend && npm run build

# Docker
docker-build:
	@echo "$(BLUE)构建Docker镜像...$(NC)"
	docker-compose build

docker-up:
	@echo "$(BLUE)启动Docker容器...$(NC)"
	docker-compose up -d

docker-down:
	@echo "$(BLUE)停止Docker容器...$(NC)"
	docker-compose down

docker-logs:
	@echo "$(BLUE)查看Docker日志...$(NC)"
	docker-compose logs -f

# 部署
deploy:
	@echo "$(BLUE)部署到生产环境...$(NC)"
	chmod +x deploy.sh
	./deploy.sh

# 数据库
db-init:
	@echo "$(BLUE)初始化数据库...$(NC)"
	cd backend && python init_db.py

db-migrate:
	@echo "$(BLUE)运行数据库迁移...$(NC)"
	cd backend && alembic upgrade head

db-reset:
	@echo "$(RED)警告：即将重置数据库，所有数据将丢失！$(NC)"
	@read -p "确认继续？ (y/n): " confirm;

	if [ $$confirm = "y" ]; then \
		cd backend && rm -f my_ai_assistent.db; \
		python init_db.py; \
		echo "$(GREEN)数据库已重置$(NC)";

	else \
		echo "$(YELLOW)操作已取消$(NC)"; \
	fi

# 清理
clean:
	@echo "$(BLUE)清理构建产物...$(NC)"
	rm -rf backend/__pycache__
	rm -rf backend/*/__pycache__
	rm -rf frontend/dist
	rm -rf frontend/node_modules/.cache
	rm -rf backend/.coverage
	rm -rf backend/htmlcov
	rm -rf backend/.pytest_cache

clean-all: clean
	@echo "$(BLUE)清理所有生成文件...$(NC)"
	rm -rf frontend/node_modules
	rm -rf backend/venv
	rm -f backend/my_ai_assistant.db
	rm -rf backend/data/chroma.db
	rm -rf backend/data/uploads
	rm -rf logs
	rm -rf coverage
	rm -rf test-reports

# 其他
open-api:
	@echo "$(BLUE)打开API文档...$(NC)"
	open http://localhost:8000/docs || xdg-open http://;localhost:8000/docs || echo "请手动访问：http://localhost:8000/docs"

# 默认目标
.DEFAULT_GOAL := help
