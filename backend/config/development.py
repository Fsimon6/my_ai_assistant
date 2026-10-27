"""
开发配置环境
"""
import os
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).parent.parent.parent

# F1 统一配置：本类剩余的"仍被运行时读取"的字段改为委托给 pydantic settings
# （`.env` 才会真正生效）。容器导入失败时（例如缺少 JWT_SECRET 的极端场景）
# 回退到本文件原有的 os.getenv / 硬编码默认值，保证不改变既有默认行为。
try:  # pragma: no cover - 正常情况下一定可用
    from backend.config.settings import settings as _unified_settings
except Exception:  # noqa: BLE001
    _unified_settings = None


class DevelopmentConfig:
    """开发环境配置类"""

    # 应用元信息（供 main.py 使用）
    APP_NAME: str = 'My AI Assistant API'
    APP_VERSION: str = '1.0.0'
    APP_DESCRIPTION: str = '基于大模型的本地知识库智能问答助手API'
    API_V1_PREFIX: str = '/api/v1'
    # HOST / PORT 统一从 Settings 读取；未配置时保留开发环境原有默认值。
    HOST: str = (
        getattr(_unified_settings, 'HOST', None)
        if _unified_settings else os.getenv('HOST')
    ) or '0.0.0.0'
    # 开发默认端口：与 README / 启动脚本 / Vite 代理保持一致（8021）
    PORT: int = int(
        (getattr(_unified_settings, 'PORT', None)
         if _unified_settings else os.getenv('PORT'))
        or 8021
    )

    # 基础配置
    # F1：DEBUG 委托给统一配置（`.env` 的 DEBUG 生效）；统一配置不可用时保持原默认 True。
    # 该字段目前只被 database/base.py 用于 echo（SQL 打印），不影响其它行为。
    DEBUG = bool(getattr(_unified_settings, 'DEBUG', True)) if _unified_settings else True
    SECRET_KEY = os.environ.get('JWT_SECRET')

    # 数据库配置
    # F1：优先级 = 进程环境变量 > 统一配置（.env） > 原有绝对路径默认值。
    # 三者指向同一文件：pydantic 默认值 "./my_ai_assistant.db" 会被 database/base.py
    # 按**项目根**解析为 <root>/my_ai_assistant.db，与 BASE_DIR 版本等价。
    DATABASE_URL = (
        os.environ.get('DATABASE_URL')
        or (getattr(_unified_settings, 'DATABASE_URL', None) if _unified_settings else None)
        or f'sqlite:///{BASE_DIR}/my_ai_assistant.db'
    )

    # 安全配置
    SECURITY_PASSWORD_SALT = os.getenv('SECURITY_PASSWORD_SALT', 'dev-password-salt')
    BCRYPT_LOG_ROUNDS = 4   # 开发环境可以低一些

    @classmethod
    def validate_config(cls):
        """配置校验（详细校验由 settings.py 完成，这里仅做兼容性提示）"""
        from backend.config.settings import settings

        require_key = {'openai', 'dashscope', 'zhipu'}
        if settings.LLM_PROVIDER in require_key and not settings.API_KEY:
            raise ValueError(
                f'provider={settings.LLM_PROVIDER} 需要设置 API_KEY 环境变量'
            )

        print('√ 配置验证通过')
        print(f' LLM Provider：{settings.LLM_PROVIDER}')


# 创建必要的目录
def create_directories():
    """创建必要的目录"""
    directories = [
        BASE_DIR / 'logs',
        BASE_DIR / 'data' / 'uploads',
        BASE_DIR / 'data' / 'chroma_db',
        BASE_DIR / 'cache'
    ]

    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)


# 初始化时创建目录
create_directories()
