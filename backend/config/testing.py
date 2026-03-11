"""
测试环境配置
"""
import os
import tempfile
from pathlib import Path
from typing import Optional


# 项目根目录
BASE_DIR = Path(__file__).parent.parent.parent


class TestingConfig:
    """测试环境配置类"""

    # ========== 基础配置 ==========
    DEBUG: bool = True
    TESTING: bool = True
    SECRET_KEY: str = 'testing'
    ENVIRONMENT: str = 'testing'

    # ========== 数据库配置 ==========
    # 使用内存数据库或临时文件数据库
    DATABASE_URL: str = os.getenv(
        'TEST_DATABASE_URL',
        f"sqlite:///{tempfile.gettempdir()}/test_ai_assistant.db"
        )

    # ========== Redis配置 ==========
    # 使用不同的数据库索引避免冲突
    REDIS_URL: str = os.getenv('TEST_REDIS_URL', 'redis://localhost:6379/15')

    # ========== 向量数据库配置 ==========
    # 使用临时目录
    VECTOR_DB_PATH: str = os.getenv('TEST_VECTOR_DB_PATH', str(tempfile.gettempdir() /  Path('text_chroma_db')))
    VECTOR_DB_COLLECTION: str = 'test_documents'

    # ========== 大模型API配置 ==========
    # 测试环境使用模拟或低成本模型
    LLM_PROVIDER: str = os.getenv('TEST_LLM_PROVIDER', 'mock')

    # OpenAI测试配置
    OPENAI_API_KEY: Optional[str] = os.getenv('TEST_OPENAI_API_KEY', 'test_key')
    OPENAI_API_BASE: Optional[str] = os.getenv('TEST_OPENAI_API_BASE', 'http://api.openai.con/v1')
    OPENAI_MODEL: str = os.getenv('TEST_OPENAI_MODEL', 'gpt-3.5-turbo')

    # 百度千帆测试配置
    QIANFAN_API_KEY: Optional[str] = os.getenv('TEST_QIANFAN_API_KEY', 'test_api_key')
    QIANFAN_SECRET_KEY: Optional[str] = os.getenv('TEST_QIANFAN_SECRET_KEY', 'test_secret_key')
    QIANFAN_MODEL: str = os.getenv('TEST_QIANFAN_MODEL', 'ERNIE-3.5-8K')

    # 智谱AI测试配置
    ZHIPU_API_KEY: Optional[str] = os.getenv('TEST_ZHIPU_API_KEY')
    ZHIPU_MODEL: str = os.getenv('TEST_ZHIPU_MODEL', 'glm-3-turbo')

    # ========== 嵌入模型配置 ==========
    EMBEDDING_PROVIDER: str = os.getenv('TEST_EMBEDDING_PROVIDER', 'mock')
    EMBEDDING_MODEL: str = os.getenv('TEST_EMBEDDING_MODEL', 'mock')
    EMBEDDING_DEVICE: str = os.getenv('TEST_EMBEDDING_DEVICE', 'cpu')
    EMBEDDING_DIMENSIONS: int = int(os.getenv('TEST_EMBEDDING_DIMENSIONS', 512))

    # ========== 文件上传配置 ==========
    # 使用临时上传目录
    UPLOAD_FOLDER: str = os.getenv('TEST_UPLOAD_FOLDER', str(tempfile.gettempdir() / Path('test_uploads')))
    MAX_CONTENT_LENGTH: int = 10 * 1024 * 1024  # 10MB
    ALLOWED_EXTENSIONS: set = {'.txt', '.pdf', '.docx', '.xlsx', '.csv', '.jpg', '.png'}

    # ========== 会话配置 ==========
    SESSION_TYPE: str = 'filesystem'    # 测试环境使用文件系统会话
    SESSION_PERMANENT: bool = False
    SESSION_USE_SIGNER: bool = False
    SESSION_KEY_PREFIX: str = 'test_session:'
    PERMANENT_SESSION_LIFETIME: int = 300   # 5分钟

    # ========== 跨域配置 ==========
    CORS_ORIGINS: list = [
        'http://localhost:5173',
        'http://127.0.0.1:5173',
        'http://localhost:3000',
    ]

    # ========== 日志配置 ==========
    LOG_LEVEL: str = 'INFO'     # 测试环境可以设为INFO或WARNING
    LOG_FILE: Optional[str] = os.getenv('TEST_LOG_FILE', str(BASE_DIR / 'logs' / 'test.log'))
    LOG_FORMAT: str = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

    # ========== 缓存配置 ==========
    CACHE_TYPE: str = 'simple'  # 使用简单内存缓存
    CACHE_DEFAULT_TIMEOUT: int = 60     # 1分钟
    CACHE_THRESHOLD: int = 1000

    # ========== 限流配置 ==========
    RATELIMIT_ENABLED: bool = False     # 测试环境禁用限流
    RATELIMIT_DEFAULT: str = '1000 per minute'
    RATELIMIT_STORAGE_URL: str = REDIS_URL

    # ========== API文档配置 ==========
    API_TITLE: str = 'AI知识库助手测试API'
    API_VERSION: str = '1.0.0'
    QIANFAN_VERSION: str = 3.5
    QIANFAN_URL_PREFIX: str = '/test/docs'
    QIANFAN_SWAGGER_UI_PATH: str = '/test/swagger'
    QIANFAN_SWAGGER_UI_URL:str = ''

    # ========== SQLAlchemy配置 ==========
    SQLALCHEMY_ECHO: bool = False   # 测试环境关闭SQL回显
    SQLALCHEMY_TRACK_MODIFICATIONS:bool = False
    SQLALCHEMY_POOL_SIZE: int = 5
    SQLALCHEMY_MAX_OVERFLOW: int = 10
    SQLALCHEMY_POOL_RECYCLE: int = 1800

    # ========== 安全配置 ==========
    SECURITY_PASSWORD_SALT: str = 'test-password-salt'
    BCRYPT_LOG_ROUNDS: int = 4   # 测试环境使用较少的轮数以加速
    JWT_SECRET_KEY: str = 'test-jwt-secret-key'
    JWT_ACCESS_TOKEN_EXPIRES: int = 900     # 15分钟
    JWT_REFRESH_TOKEN_EXPIRES: int = 604800     # 7天

    # ========== 邮件配置 ==========
    MAIL_SERVER: str = 'localhost'
    MAIL_PORT: int = 1025   # 测试邮件服务器端口
    MAIL_USE_TLS: bool = False
    MAIL_USE_SSL: bool = False
    MAIL_USERNAME: Optional[str] = None
    MAIL_PASSWORD: Optional[str] = None
    MAIL_DEFAULT_SENDER: str = 'test@axample.com'

    # ========== 测试专用配置 ==========
    # 测试用户
    TEST_USER_EMAIL: str = 'test@axample.com'
    TEST_USER_PASSWORD: str = 'test123456'
    TEST_USER_NAME: str = '测试用户'

    # 测试API密钥
    TEST_API_KEY: str = 'test-api-key-123'

    # 覆盖率配置
    COVERAGE_ENABLE: bool = True
    COVERAGE_REPORT_DIR: str = str(BASE_DIR / 'coverage')
    COVERAGE_HTML_DIR: str = str(BASE_DIR / 'coverage' / 'html')
    COVERAGE_XML_DIR: str = str(BASE_DIR / 'coverage' / 'xml')

    # 测试超时配置
    TEST_TIMEOUT: int = 30  # 秒
    TEST_ASYNC_TIMEOUT: int  = 60   # 异步测试超时

    # 测试数据路径
    TEST_DATA_DIR: str = str(BASE_DIR / 'test' / 'data')

    # 模拟响应配置
    MOCK_LLM_RESPONSES: bool = True
    MOCK_EMBEDDING_RESPONSES: bool = True

    # 测试数据库清理
    CLEANUP_DATABASE: bool = True
    CLEANUP_UPLOADS: bool = True

    # ========== 性能测试配置 ==========
    PERFORMANCE_TEST_ENABLE: bool = False
    PERFORMANCE_TEST_USERS: int = 10
    PERFORMANCE_TEST_DURATION: int = 30     # 秒

    # ========== 集成测试配置 ==========
    INTEGRATION_TEST_ENABLE: bool = False
    INTEGRATION_TEST_BASE_URL: str = 'http://localhost:8000'

    # ========== E2E测试配置 ==========
    E2E_TEST_ENABLED: bool = False
    E2E_TEST_BROWSER: str = 'chroma'
    E2E_TEST_HEADLESS: bool = True
    E2E_TEST_BASE_URL: str = 'http://localhost:5173'

    # ========== 测试报告配置 ==========
    TEST_REPORT_ENABLED: bool = True
    TEST_REPORT_FORMAT: str = 'html'
    TEST_REPORT_OUTPUT_DIR: str = str(BASE_DIR / 'test-reports')

    @classmethod
    def init_app(cls, app):
        """初始化应用配置"""
        # 创建必要的测试目录
        cls._create_test_directories()

        # 设置测试标记
        app.config['TESTING'] = True

        # 禁用CSRF保护（方便测试）
        app.config['WTF_CSRF_ENABLED'] = False

        # 设置测试数据库
        if cls.CLEANUP_DATABASE:
            cls._cleanup_test_database()

        # 设置测试上传目录
        if cls.CLEANUP_UPLOADS:
            cls._cleanup_test_uploads()

    @classmethod
    def _create_test_directories(cls):
        """创建测试目录"""
        directories = [
            Path(cls.UPLOAD_FOLDER),
            Path(cls.VECTOR_DB_PATH),
            Path(cls.COVERAGE_REPORT_DIR),
            Path(cls.COVERAGE_HTML_DIR),
            Path(cls.COVERAGE_XML_DIR),
            Path(cls.TEST_DATA_DIR),
            Path(cls.TEST_REPORT_OUTPUT_DIR),
            Path(BASE_DIR / 'logs'),
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _cleanup_test_database(cls):
        """清理测试数据库"""
        # 这里可以添加数据库清理逻辑
        # 例如：删除测试数据库文件
        db_path = cls.DATABASE_URL.replace("sqlite:///","")
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except:
                pass

    @classmethod
    def _cleanup_test_uploads(cls):
        """清理测试上传目录"""
        import shutil

        upload_dir = Path(cls.UPLOAD_FOLDER)
        if upload_dir.exists():
            try:
                shutil.rmtree(upload_dir)
                upload_dir.mkdir(parents=True, exist_ok=True)
            except:
                pass

    @classmethod
    def get_test_user_credentials(cls) -> dict:
        """获取测试用户凭证"""
        return {
            'environment': cls.ENVIRONMENT,
            'database': cls.DATABASE_URL,
            'vector_db': cls.VECTOR_DB_PATH,
            'llm_provider': cls.LLM_PROVIDER,
            'embedding_provider': cls.EMBEDDING_PROVIDER,
            'mock_mode': cls.is_mock_mode(),
            'test_user': cls.TEST_USER_EMAIL,
            'upload_dir': cls.UPLOAD_FOLDER,
        }

# 创建测试目录
TestingConfig._create_test_directories()

# 导出配置实例
config = TestingConfig()
