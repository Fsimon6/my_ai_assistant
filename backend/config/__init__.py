"""
配置模块
根据环境加载不同配置

环境值的**唯一来源**是统一 Settings（`backend/config/settings.py`）：
优先级：进程环境变量 > 项目根 `.env` > 代码默认值（"development"）。
不再直接读取未经过 Settings 的 `os.getenv('ENVIRONMENT')`，避免出现
"启动日志显示 production、实际却选用 DevelopmentConfig" 的双轨问题。
"""
import logging
from typing import Optional

# 环境类型
ENV_DEVELOPMENT = 'development'
ENV_PRODUCTION = 'production'
ENV_TESTING = 'testing'

# 默认环境
DEFAULT_ENV = ENV_DEVELOPMENT

_VALID_ENVS = (ENV_DEVELOPMENT, ENV_PRODUCTION, ENV_TESTING)

logger = logging.getLogger(__name__)


def normalize_env(value: Optional[str]) -> str:
    """把配置中的环境值规约为受支持的三种之一。

    - 大小写与首尾空白不敏感（与 `.env` 的 `case_sensitive=False` 语义一致）；
    - 空值 / 非法值沿用既有约定：回退 `DEFAULT_ENV`；
    - 非空非法值额外记录一条警告，便于运维发现拼写错误；
    - 不新增环境名称：只接受 development / production / testing。
    """
    raw = '' if value is None else str(value)
    env = raw.strip().lower()
    if env in _VALID_ENVS:
        return env
    if env:
        logger.warning(
            "ENVIRONMENT=%r 不受支持（可选：%s），按既有约定回退为 %s",
            raw, ' / '.join(_VALID_ENVS), DEFAULT_ENV,
        )
    return DEFAULT_ENV


def get_env() -> str:
    """返回当前生效环境（配置类选择与启动日志**共用**此函数）。

    取值来自统一 Settings：进程环境变量 > `.env` > 默认值。
    Settings 不可用时（极端场景，例如缺少必填 `JWT_SECRET`）回退默认环境，
    以保证仅导入 `backend.config` 的模块仍可工作；此时应用本身无法启动，
    由 `main.py` 的配置导入 / 启动校验负责报错。
    """
    try:
        from .settings import settings
        raw = getattr(settings, 'ENVIRONMENT', None)
    except Exception as exc:  # pragma: no cover - 仅在 Settings 构造失败的极端场景
        logger.warning('读取 Settings.ENVIRONMENT 失败（%s），回退默认环境 %s', exc, DEFAULT_ENV)
        raw = None
    return normalize_env(raw)


# 根据环境导入配置（唯一选择点）
env = get_env()

if env == ENV_PRODUCTION:
    from .production import ProductionConfig as Config
elif env == ENV_TESTING:
    # 说明：testing.py 中的类名是 TestingConfig（历史代码此处误写为 TestConfig，
    # 会导致 ENVIRONMENT=testing 时 ImportError）；本批一并按真实类名修正。
    from .testing import TestingConfig as Config
else:   # development
    from .development import DevelopmentConfig as Config

# 导出配置实例
config = Config()

# testing 必须在任何 database.base / logger 导入产生副作用之前完成路径校验。
# 该模块是 backend.config.settings 的父包：database/base.py 与 main.py 在导入
# settings 时都会先执行到这里。开发/生产环境保持原有“lifespan 校验”时序。
if env == ENV_TESTING:
    Config.validate_config()

# 导出配置类
__all__ = ['config', 'Config', 'get_env', 'normalize_env', 'env',
           'ENV_DEVELOPMENT', 'ENV_PRODUCTION', 'ENV_TESTING']
