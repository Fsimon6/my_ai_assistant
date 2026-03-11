"""
配置模块
根据环境加载不同配置
"""
import os
from typing import Optional
from pathlib import Path

# 环境类型
ENV_DEVELOPMENT = 'development'
ENV_PRODUCTION = 'production'
ENV_TESTING = 'testing'

# 默认环境
DEFAULT_ENV = ENV_DEVELOPMENT


def get_env() -> str:
    """获取当前环境"""
    env = os.getenv('ENVIRONMENT', DEFAULT_ENV).lower()

    if env not in [ENV_DEVELOPMENT, ENV_PRODUCTION, ENV_TESTING]:
        env = DEFAULT_ENV

    return env

# 根据环境导入配置
env = get_env()

if env == ENV_PRODUCTION:
    from .production import ProductionConfig as Config
elif env == ENV_TESTING:
    from .testing import TestConfig as Config
else:   # development
    from .development import DevelopmentConfig as Config

# 导出配置实例
config = Config()

# 导出配置类
__all__ = ['config', 'Config', 'get_env', 'ENV_DEVELOPMENT', 'ENV_PRODUCTION', 'ENV_TESTING']