# -*- coding: utf-8 -*-
"""
生产环境配置

设计说明（避免导入期崩溃 / 循环依赖）：
- 不再继承 backend.database.base.settings 实例（否则会“对实例子类化”且触发
  backend.config <-> backend.database.base 的循环导入）；所有值直接读取。
- JWT_SECRET / API_KEY / LLM_* / DATABASE_URL / BACKEND_CORS_ORIGINS 优先取自
  pydantic Settings（它已通过 .env / 环境变量加载），避免“pydantic 读 .env 但
  os.getenv 读不到”的不一致。
- ALLOWED_HOSTS / HOST / PORT / LOG_* 等仅生产相关的项取自环境变量；ALLOWED_HOSTS
  缺失或含通配符 '*' 时，在 validate_config（启动期）安全失败，而非导入期崩溃。
"""
import os

from backend.config.settings import settings


class ProductionConfig:
    """生产环境配置"""

    ENVIRONMENT = 'production'

    # 服务器配置
    DEBUG = False
    HOST = os.getenv('HOST', '0.0.0.0')
    PORT = int(os.getenv('PORT', '8000'))

    # 应用元信息（供 main.py 使用）
    APP_NAME = 'My AI Assistant API'
    APP_VERSION = '1.0.0'
    APP_DESCRIPTION = '基于大模型的本地知识库智能问答助手API'
    API_V1_PREFIX = '/api/v1'

    # 安全配置：JWT_SECRET 必填（取自已加载的 settings，缺失即启动失败）
    SECRET_KEY = settings.JWT_SECRET

    # ALLOWED_HOSTS：导入期不崩溃；空列表在 validate_config 中安全失败（不允许通配符 '*'）
    ALLOWED_HOSTS = [
        h.strip() for h in os.getenv('ALLOWED_HOSTS', '').split(',') if h.strip()
    ]

    # CORS 配置（生产可配置，默认来自 environment，不允许 '*'）
    BACKEND_CORS_ORIGINS = settings.BACKEND_CORS_ORIGINS

    # 数据库配置
    DATABASE_URL = settings.DATABASE_URL
    VECTOR_DB_PATH = os.getenv('VECTOR_DB_PATH', './data/chroma_db')

    # 缓存配置
    REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379/0')

    # 日志配置
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE = os.getenv('LOG_FILE', '/app/logs/ai_assistant.log')

    # 大模型配置（通用 API_KEY 驱动）
    LLM_PROVIDER = settings.LLM_PROVIDER
    LLM_MODEL = settings.LLM_MODEL
    LLM_BASE_URL = settings.LLM_BASE_URL
    API_KEY = settings.API_KEY
    LLM_TIMEOUT = 30    # 生产环境增加超时

    # 文件上传限制
    MAX_UPLOAD_SIZE = 50 * 1024 * 1024      # 50MB
    ALLOWED_FILE_TYPES = ['.pdf', '.txt', '.docx', '.md']

    # 频率限制
    RATE_LIMIT_ENABLED = True
    RATE_LIMIT_REQUESTS = 100   # 每分钟请求数
    RATE_LIMIT_PERIOD = 60      # 秒

    @classmethod
    def validate_config(cls):
        """生产环境配置校验（启动期；缺失 / 不安全即安全失败，绝不静默使用默认值）"""
        # JWT_SECRET 必填：缺失即启动失败（不允许空或可预测默认）
        if not cls.SECRET_KEY:
            raise ValueError(
                '生产环境必须设置 JWT_SECRET 环境变量（不允许为空或使用可预测默认值）。'
            )

        # ALLOWED_HOSTS 必填且不允许通配符
        if not cls.ALLOWED_HOSTS:
            raise ValueError(
                '生产环境必须设置 ALLOWED_HOSTS 环境变量（逗号分隔的主机名），不允许为空。'
            )
        if '*' in cls.ALLOWED_HOSTS:
            raise ValueError('生产环境 ALLOWED_HOSTS 不允许包含通配符 "*"。')

        # 需要 API_KEY 的 provider 必须配置
        require_key = {'openai', 'dashscope', 'zhipu'}
        if cls.LLM_PROVIDER in require_key and not cls.API_KEY:
            raise ValueError(f'provider={cls.LLM_PROVIDER} 需要设置 API_KEY 环境变量')

        print('  生产环境配置验证通过')
