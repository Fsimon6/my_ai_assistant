"""
开发配置环境
"""
import os
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).parent.parent.parent


class DevelopmentConfig:
    """开发环境配置类"""

    # 基础配置
    DEBUG = True
    TESTING = False
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

    # 数据库配置
    DATABASE_URL = os.environ.get('DATABASE_URL', f'sqlite:///{BASE_DIR}/my_ai_assistant.db')

    # Redis配置（用于缓存和会话）
    REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

    # 向量数据库配置
    VECTOR_DB_PATH = os.getenv('VECTOR_DB_PATH', str(BASE_DIR / 'data' / 'chroma_db'))
    VECTOR_DB_COLLECTION = 'documents'

    # 大模型API配置
    LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'qianfan')

    # OpenAI配置
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
    OPENAI_API_BASE = os.getenv('OPENAI_API_BASE', 'https://api.openai.com/v1')
    OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-3.5-turbo')

    # 百度千帆配置
    QIANFAN_API_KEY = os.getenv('QIANFAN_API_KEY', '')
    QIANFAN_SECRET_KEY = os.getenv('QIANFAN_SECRET_KEY', '')
    QIANFAN_MODEL = os.getenv('QIANFAN_MODEL', 'ERNIE-3.5-8k')

    # 智谱AI配置
    ZHIPU_API_KEY = os.getenv('ZHIPU_API_KEY', '')
    ZHIPU_MODEL = os.getenv('ZHIPU_MODEL', 'glm-4')

    # 嵌入模型配置
    EMBEDDING_MODEL = os.getenv('EMBEDDING_MODEL', 'BAAI/bge-small-zh-v1.5')
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
    QIANFAN_VERSION = '3.5'
    QIANFAN_URL_PREFIX = '/docs'
    QIANFAN_SWAGGER_UI_PATH = '/swagger'
    QIANFAN_SWAGGER_UI_URL = 'http://127.0.0.1:8000'

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
