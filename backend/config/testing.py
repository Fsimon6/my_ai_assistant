"""
测试环境配置

运行配置的唯一来源是 backend.config.settings.settings；本类不再维护
独立的 JWT / 数据库 / 向量库 / 上传 / LLM 值，只负责：
* 提供 FastAPI 初始化需要的元信息与 HOST/PORT；
* 在进入 lifespan 前校验测试路径确实位于隔离根目录；
* 保留历史辅助方法（无调用方）以维持兼容。
"""
import os
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

from backend.config.settings import settings


# 项目根目录
BASE_DIR = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = BASE_DIR
_DEFAULT_TEST_ROOT = Path(tempfile.gettempdir()) / 'my_ai_assistant_test'


def _reject_home_shorthand(value: str) -> str:
    """拒绝会被 Shell/用户误解为家目录的路径表达。

    validation 层不能把 ``~`` 展开成家目录：实际业务代码（main、
    document_service、logger、excel.store、database.base）都不做
    expanduser，否则会出现“校验通过、实际写到项目下 ``~`` 目录”的绕过。
    """
    raw = str(value)
    if raw.startswith('~'):
        raise ValueError(
            f'测试环境路径不允许使用 ~ 家目录简写：{raw!r}'
        )
    return raw


def _absolute(value: str) -> Path:
    return Path(_reject_home_shorthand(value)).resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _isolation_root() -> Path:
    configured = (os.getenv('TEST_ISOLATION_ROOT') or '').strip()
    root = _absolute(configured) if configured else _DEFAULT_TEST_ROOT.resolve()
    if root == _PROJECT_ROOT or _is_within(root, _PROJECT_ROOT):
        raise ValueError(
            '测试隔离根目录必须位于项目目录之外，'
            f'当前为：{root}'
        )
    return root


def _require_path(label: str, value: str, root: Path) -> Path:
    if not (value or '').strip():
        raise ValueError(f'测试环境必须设置 {label}')
    path = _absolute(value)
    _require_under(label, path, root)
    return path


def _require_under(label: str, path: Path, root: Path) -> Path:
    path = Path(_reject_home_shorthand(str(path))).resolve()
    if not _is_within(path, root):
        raise ValueError(
            f'测试环境 {label} 必须位于隔离根目录 {root} 内，实际为：{path}'
        )
    return path


def _sqlite_path(url: str) -> Path:
    try:
        parsed = make_url(url)
    except Exception as exc:  # noqa: BLE001 - 非法 URL 统一转为配置错误
        raise ValueError(f'测试环境 DATABASE_URL 无法解析：{exc}') from exc
    if parsed.get_backend_name() != 'sqlite':
        raise ValueError('测试环境数据库必须使用本地 SQLite，禁止连接正式数据库')
    database = parsed.database or ''
    if not database or database == ':memory:':
        raise ValueError('测试环境 DATABASE_URL 必须指向隔离目录内的 SQLite 文件')
    _reject_home_shorthand(database)
    path = Path(database)
    if not path.is_absolute():
        # 与 backend.database.base 对相对 SQLite 路径的解析规则保持一致。
        path = _PROJECT_ROOT / path
    return path.resolve()


class TestingConfig:
    """测试环境配置类"""

    # ========== 应用元信息（供 main.py 使用） ==========
    APP_NAME: str = 'My AI Assistant API'
    APP_VERSION: str = '1.0.0'
    APP_DESCRIPTION: str = '基于大模型的本地知识库智能问答助手API'
    API_V1_PREFIX: str = '/api/v1'
    # 优先使用统一 Settings（HOST/PORT）；未配置时保留 TEST_HOST/TEST_PORT
    # 兼容变量及测试默认值，避免与开发 8021 / 生产 8000 冲突。
    HOST: str = settings.HOST or os.getenv('TEST_HOST', '127.0.0.1')
    PORT: int = settings.PORT or int(os.getenv('TEST_PORT', '8022'))

    # ========== 基础配置 ==========
    DEBUG: bool = True
    TESTING: bool = True
    # 以下运行值全部以 Settings 为唯一来源；TEST_* 兼容变量已在
    # Settings.apply_testing_aliases 中先行映射，不再在此处二次解析。
    JWT_SECRET: str = settings.JWT_SECRET
    ENVIRONMENT: str = str(settings.ENVIRONMENT or '').strip().lower()

    # ========== 数据库配置 ==========
    DATABASE_URL: str = settings.DATABASE_URL

    # ========== 向量数据库配置 ==========
    VECTOR_DB_PATH: str = settings.VECTOR_DB_PATH

    # ========== 大模型API配置 ==========
    LLM_PROVIDER: str = settings.LLM_PROVIDER

    # ========== 文件上传配置 ==========
    # 与 document_service.save_uploaded_file 的运行时解析规则保持一致。
    UPLOAD_FOLDER: str = (
        (settings.UPLOAD_TEMP_DIR or '').strip()
        or str(Path(tempfile.gettempdir()) / 'ai_assistant_uploads')
    )
    # 上传大小上限已统一到 settings.MAX_UPLOAD_SIZE（B2-B）

    # ========== 安全配置 ==========
    SECURITY_PASSWORD_SALT: str = 'test-password-salt'
    BCRYPT_LOG_ROUNDS: int = 4   # 测试环境使用较少的轮数以加速

    # ========== 测试专用配置 ==========
    # 测试用户
    TEST_USER_EMAIL: str = 'test@axample.com'

    # 覆盖率配置
    COVERAGE_REPORT_DIR: str = str(BASE_DIR / 'coverage')
    COVERAGE_HTML_DIR: str = str(BASE_DIR / 'coverage' / 'html')
    COVERAGE_XML_DIR: str = str(BASE_DIR / 'coverage' / 'xml')

    # 测试数据路径
    TEST_DATA_DIR: str = str(BASE_DIR / 'test' / 'data')

    # 测试数据库清理
    CLEANUP_DATABASE: bool = True
    CLEANUP_UPLOADS: bool = True

    # ========== 测试报告配置 ==========
    TEST_REPORT_OUTPUT_DIR: str = str(BASE_DIR / 'test-reports')

    @classmethod
    def validate_config(cls):
        """测试环境配置校验（启动期；不要求生产凭据/ALLOWED_HOSTS）。

        校验对象就是 Settings 中实际运行的值；路径必须全部落在
        TEST_ISOLATION_ROOT（默认系统临时目录下）内，避免测试写入
        data/chroma_db、data/uploads、正式日志或正式 Excel 目录。
        """
        if not cls.TESTING:
            raise ValueError('测试环境必须启用 TESTING')
        if not (cls.JWT_SECRET or '').strip():
            raise ValueError('测试环境必须设置 JWT_SECRET')
        if not (cls.DATABASE_URL or '').strip():
            raise ValueError('测试环境必须设置 DATABASE_URL')
        if not (cls.UPLOAD_FOLDER or '').strip():
            raise ValueError('测试环境必须设置 UPLOAD_TEMP_DIR')
        if not (cls.VECTOR_DB_PATH or '').strip():
            raise ValueError('测试环境必须设置 VECTOR_DB_PATH')
        if not (cls.HOST or '').strip():
            raise ValueError('测试环境必须设置 HOST')
        if not (1 <= int(cls.PORT) <= 65535):
            raise ValueError('测试环境 PORT 必须在 1..65535 之间')
        if not (cls.API_V1_PREFIX or '').startswith('/'):
            raise ValueError('测试环境 API_V1_PREFIX 必须以 "/" 开头')

        root = _isolation_root()
        db_path = _sqlite_path(cls.DATABASE_URL)
        _require_under('DATABASE_URL', db_path, root)
        _require_path('VECTOR_DB_PATH', cls.VECTOR_DB_PATH, root)
        _require_path('UPLOAD_TEMP_DIR', cls.UPLOAD_FOLDER, root)

        log_file = (settings.LOG_FILE or '').strip()
        if log_file:
            _require_path('LOG_FILE', log_file, root)
        excel_root = (settings.EXCEL_DATA_ROOT or '').strip()
        if not excel_root:
            raise ValueError(
                '测试环境必须设置 EXCEL_DATA_ROOT，禁止回退到项目 data/excel'
            )
        _require_path('EXCEL_DATA_ROOT', excel_root, root)

        print('  测试环境配置验证通过')

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
            'mock_mode': cls.is_mock_mode(),
            'test_user': cls.TEST_USER_EMAIL,
            'upload_dir': cls.UPLOAD_FOLDER,
        }

# 导出配置实例（不执行目录创建；运行期目录由 lifespan/上传逻辑创建）
config = TestingConfig()
