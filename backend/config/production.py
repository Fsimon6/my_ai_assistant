# -*- coding: utf-8 -*-
import os
from backend.database.base import settings


class ProductionConfig(settings):
    """生产环境配置"""

    # 服务器配置
    DEBUG = False
    HOST = os.getenv('HOST', '0.0.0.0')
    PORT = int(os.getenv('PORT', '8000'))

    # 安全配置
    SECRET_KEY = os.getenv('SECRET_KEY')
    ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS').split(',')

    # CORS配置
    BACKEND_CORS_ORIGINS = os.getenv('BACKEND_CORS_ORIGINS', '').split(',')

    # 数据库配置
    DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./data/ai_assistant.db')

    # 缓存配置
    REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379/0')

    # 日志配置
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE = '/app/logs/ai_assistant.log'

    # 大模型配置
    LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'qianfan')
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
        """生产环境配置验证"""
        super().validate_config()

        required_vars = [
            'SECRET_KEY',
            'LLM_PROVIDER',
            f'{cls.LLM_PROVIDER.upper()}_API_KEY'
        ]

        for var in required_vars:
            if not os.getenv(var):
                raise ValueError(f'生产环境必须设置环境变量：{var}')

        # 检查安全配置
        if cls.SECRET_KEY == 'dev-secret-key-change-this':
            print(' 警告：使用默认的SECRET_KEY，生产环境请修改！')

        print('  生产环境配置验证通过')