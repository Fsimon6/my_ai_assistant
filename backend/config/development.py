"""
开发配置环境
"""
import os
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).parent.parent.parent


class DevelopmentConfig:
    """开发环境配置类"""

    # 应用元信息（供 main.py 使用）
    APP_NAME: str = 'My AI Assistant API'
    APP_VERSION: str = '1.0.0'
    APP_DESCRIPTION: str = '基于大模型的本地知识库智能问答助手API'
    API_V1_PREFIX: str = '/api/v1'
    HOST: str = os.getenv('HOST', '0.0.0.0')
    PORT: int = int(os.getenv('PORT', '8080'))

    # 基础配置
    DEBUG = True
    TESTING = False
    SECRET_KEY = os.environ.get('JWT_SECRET')

    # 数据库配置
    DATABASE_URL = os.environ.get('DATABASE_URL', f'sqlite:///{BASE_DIR}/my_ai_assistant.db')

    # Redis配置（用于缓存和会话）
    REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

    # 向量数据库配置
    VECTOR_DB_PATH = os.getenv('VECTOR_DB_PATH', str(BASE_DIR / 'data' / 'chroma_db'))
    VECTOR_DB_COLLECTION = 'documents'

    # 大模型API配置（通用 API_KEY 驱动）
    LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'openai')
    LLM_MODEL = os.getenv('LLM_MODEL', 'gpt-3.5-turbo')
    LLM_BASE_URL = os.getenv('LLM_BASE_URL')
    API_KEY = os.getenv('API_KEY')

    # 嵌入模型配置
    # 注意：实际运行值由 .env → settings.EMBEDDING_MODEL 决定（当前为 DashScope 远程嵌入）；
    # 此处不再写死本地模型默认值，避免与运行时配置冲突（该字段未被 embedding 运行时代码读取）。
    EMBEDDING_MODEL = os.getenv('EMBEDDING_MODEL', 'text-embedding-3-small')
    EMBEDDING_DEVICE = os.getenv('EMBEDDING_DEVICE', 'cpu')
    EMBEDDING_DIMENSIONS = int(os.getenv('EMBEDDING_DIMENSIONS', '512'))

    # 文件上传配置
    UPLOAD_FOLDER = str(BASE_DIR / 'data' / 'uploads')
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024   # 50MB
    ALLOWED_EXTENSIONS = {'.txt', '.pdf', '.doc', '.docx',
                          '.xls', '.xlsx', '.ppt', '.pptx',
                          '.md', '.json', '.xml', '.csv'
                          '.png', '.jpg', '.jpeg', '.gif'}

    # 会话配置
    SESSION_TYPE = 'redis'
    SESSION_PERMANENT = False
    SESSION_USE_SIGNER = True
    SESSION_KEY_PREFIX = 'ai_assistant:'
    PERMANENT_SESSION_LIFETIME = 3600       # 1小时

    # 跨域配置
    CORS_ORIGINS = [
        'http://localhost:5173',    # Vite开发服务器
        'http://localhost:8000',
        'http://127.0.0.1:5173',
        'http://127.0.0.1:8000',
    ]

    # 日志配置
    LOG_LEVEL = 'DEBUG'
    LOG_FILE = str(BASE_DIR / 'logs' / 'development.log')

    # 缓存配置
    CACHE_TYPE = 'redis'
    CACHE_REDIS_URL = REDIS_URL
    CACHE_DEFAULT_TIMEOUT = 300

    # 限流配置
    RATELIMIT_ENABLED = True
    RATELIMIT_DEFAULT = '100 per minute'
    RATELIMIT_STORAGE_URL = REDIS_URL

    # API文档配置
    API_TITLE = '我的AI知识库助手 API'
    API_VERSION = '1.0.0'

    # 性能配置
    SQLALCHEMY_ECHO = False
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 安全配置
    SECURITY_PASSWORD_SALT = os.getenv('SECURITY_PASSWORD_SALT', 'dev-password-salt')
    BCRYPT_LOG_ROUNDS = 4   # 开发环境可以低一些

    # 邮件配置
    MAIL_SERVER = os.getenv('MAIL_SERVER', '2376709678@qq.com')
    MAIL_PORT = os.getenv('MAIL_PORT', '587')
    MAIL_USE_TLS = os.getenv('MAIL_USE_TLS', 'True') == 'True'
    MAIL_USERNAME = os.getenv('MAIL_USERNAME', '<EMAIL>')
    MAIL_PASSWORD = os.getenv('MAIL_PASSWORD', '<PASSWORD>')
    MAIL_DEFAULT_SENDER = os.getenv('MAIL_DEFAULT_SENDER', '<EMAIL>')

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
