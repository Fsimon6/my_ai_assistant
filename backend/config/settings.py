import os
from pathlib import Path
from typing import Literal
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: .env 固定指向**项目根目录**下的 .env。
#: 原来 env_file=".env" 是相对路径，取决于进程启动时的工作目录；
#: 一旦从 backend/ 等子目录启动就读不到 .env（JWT_SECRET 缺失 → 启动直接失败）。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # 基础配置
    ENVIRONMENT: str = "development"
    # JWT 签名密钥：必填，无默认值，缺失时应用启动必须失败
    JWT_SECRET: str
    DEBUG: bool = False

    # JWT 访问令牌有效期（分钟）
    # 注：原代码引用 settings.ACCESS_TOKEN_EXPIRE_MINUTES，此处补齐以免启动即 AttributeError
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    # 注册控制（P-4）：false 时关闭自助注册（register 返回 403）。
    # 默认 true 保持既有行为；面向公司/内网部署请按 .env.example 说明改为 false。
    ALLOW_REGISTRATION: bool = True

    # 表格解析上限（P-8 大表保护）：超限直接明确报错（413 FILE_TOO_LARGE），不做静默截断。
    # 大内存机器可放宽；改 .env 后需重启后端生效。
    EXCEL_MAX_SHEET_ROWS: int = 200_000
    EXCEL_MAX_SHEET_CELLS: int = 2_000_000
    EXCEL_MAX_TOTAL_CELLS: int = 5_000_000

    # 单文件上传上限（**字节**）：文档与 Excel 上传共用的唯一来源（B2-B）。
    # 默认 10MiB（10485760）；合法范围 1 字节 .. 99MiB（103809024）。
    # 上限取 99MiB（< nginx `client_max_body_size 100M`），避免"后端允许、代理先拒绝"。
    MAX_UPLOAD_SIZE: int = Field(default=10 * 1024 * 1024,
                                 ge=1, le=99 * 1024 * 1024)

    # 运行时数据目录（F2 统一配置，供测试/特殊部署隔离；**留空即保持原有默认路径**）
    #   UPLOAD_TEMP_DIR 空 -> <系统临时目录>/ai_assistant_uploads（原行为）
    #                     非空 -> 直接作为上传临时文件目录
    #   EXCEL_DATA_ROOT 空 -> <项目根>/data/excel（原行为）
    #                     非空 -> 直接作为 Excel 原件/解析产物的根目录
    UPLOAD_TEMP_DIR: str = ""
    EXCEL_DATA_ROOT: str = ""

    # 数据库
    DATABASE_URL: str = "sqlite:///./my_ai_assistant.db"

    # 向量数据库
    VECTOR_DB_PATH: str = "./data/chroma_db"

    # 大模型供应商（通用 API_KEY 驱动 OpenAI 兼容接口）
    LLM_PROVIDER: Literal["openai", "dashscope", "zhipu", "ollama", "local"] = "openai"
    # 通用大模型调用密钥（OpenAI / DashScope / 智谱 使用；Ollama / local 可为空）
    API_KEY: str | None = None
    # OpenAI 兼容接口地址（DeepSeek / 本地 Ollama 等填此处）
    LLM_BASE_URL: str | None = None
    LLM_MODEL: str = "gpt-3.5-turbo"

    # LLM 生成参数（F1）：原先业务代码用 os.getenv 读取 → 写在 .env 里**不生效**；
    # 现统一到配置入口。默认值与原 os.getenv 兜底值一致（0.7 / 2000）。
    LLM_TEMPERATURE: float = 0.7
    LLM_MAX_TOKENS: int = 2000

    # LLM 请求超时（**秒**）：唯一来源（B2-A）。优先级 进程环境变量 > `.env` > 默认 30。
    # 合法范围 1..600；非法值在进程启动（Settings 构造）时即报错，不静默回退。
    # 说明：同一 AsyncOpenAI client 同时服务 chat 与 embedding（既有实现），
    # 因此该超时是 client 级；本轮不拆分 embedding 独立超时。
    LLM_TIMEOUT: int = Field(default=30, ge=1, le=600)

    # API Key 哈希盐（F1）：原 security.hash_api_key 用 os.getenv 读取（.env 不生效）
    API_KEY_SALT: str = "ai_assistant_salt"

    # 管理员初始化（F1）：原 init_db.py 用 os.getenv 读取（.env 不生效）
    INIT_ADMIN_PASSWORD: str | None = None
    INIT_ADMIN_EMAIL: str = "admin@example.com"

    # 嵌入模型（EMBEDDING_MODEL 仅用于 Embedding，与 LLM_MODEL（Chat）解耦）
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    # CORS
    BACKEND_CORS_ORIGINS: list[str] | str = []

    # 生产环境允许的 Host（逗号分隔）。放在 Settings 中，确保项目根 .env
    # 与进程环境变量共用同一加载/优先级机制，而不是依赖 os.getenv。
    ALLOWED_HOSTS: str = ""

    # 服务监听地址与端口（可选，用于保留开发/生产各自的默认端口）。
    # 优先级：显式启动参数 > 进程环境变量 > 项目根 .env > 环境默认值。
    HOST: str | None = None
    PORT: int | None = Field(default=None, ge=1, le=65535)

    @field_validator("HOST", mode="before")
    @classmethod
    def normalize_host(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("HOST 不能为空")
        return value

    @field_validator("PORT", mode="before")
    @classmethod
    def normalize_port(cls, value):
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            raise ValueError("PORT 不能为空")
        return value

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v):
        if isinstance(v, str):
            return [i.strip() for i in v.split(",")]
        return v

    @model_validator(mode="after")
    def apply_testing_aliases(self):
        """testing 环境保留 TEST_* 兼容变量，并映射到实际运行字段。

        优先级：显式 ``TEST_*`` 进程环境变量 > 标准 Settings 字段
        （进程环境变量 / 根 .env / 默认值）。只在 ``ENVIRONMENT=testing``
        时应用，开发/生产不会读取测试路径。映射后的值会直接参与后续
        业务运行与 TestingConfig 隔离校验，避免“校验一套、运行另一套”。
        """
        if str(self.ENVIRONMENT or "").strip().lower() != "testing":
            return self

        aliases = {
            "DATABASE_URL": "TEST_DATABASE_URL",
            "VECTOR_DB_PATH": "TEST_VECTOR_DB_PATH",
            "UPLOAD_TEMP_DIR": "TEST_UPLOAD_FOLDER",
            "LOG_FILE": "TEST_LOG_FILE",
            "EXCEL_DATA_ROOT": "TEST_EXCEL_DATA_ROOT",
            "LLM_PROVIDER": "TEST_LLM_PROVIDER",
            "LLM_MODEL": "TEST_LLM_MODEL",
            "LLM_BASE_URL": "TEST_LLM_BASE_URL",
        }
        for field_name, env_name in aliases.items():
            value = os.getenv(env_name)
            if value is None or not value.strip():
                continue
            if field_name == "LLM_PROVIDER":
                allowed = {"openai", "dashscope", "zhipu", "ollama", "local"}
                if value not in allowed:
                    raise ValueError(
                        f'{env_name}={value!r} 不受支持（可选：{sorted(allowed)}）'
                    )
            setattr(self, field_name, value)

        # testing 默认关闭文件日志：否则 main 导入时的 setup_logging 会在
        # lifespan 校验之前写入项目 logs/app.log。显式 LOG_FILE/TEST_LOG_FILE
        # 仍按上面的规则使用，并由 TestingConfig 校验其位于隔离根目录内。
        #
        # 空白值（空字符串、纯空格）统一规范为空：logger.py 使用
        # `if settings.LOG_FILE` 而不是 strip 后判断；若保留 ' '，校验会
        # 视为未配置而通过，但 logger 仍会尝试创建空白路径。
        if 'LOG_FILE' not in self.model_fields_set and not (os.getenv('TEST_LOG_FILE') or '').strip():
            self.LOG_FILE = ''
        elif not (self.LOG_FILE or '').strip():
            self.LOG_FILE = ''
        return self

    # 日志
    # P-3：默认开启文件日志（可运维/可回溯）。同时写控制台与文件（轮转）。
    # 说明：
    #   * 默认路径为**项目根**下的 logs/app.log（绝对路径，与 CWD 无关）；
    #   * 置空（.env 中 `LOG_FILE=`）即关闭文件日志，仅保留控制台输出；
    #   * 轮转上限 5MB × 5 个历史文件，避免长期运行无限增长。
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = str(_PROJECT_ROOT / "logs" / "app.log")
    LOG_MAX_BYTES: int = 5 * 1024 * 1024
    LOG_BACKUP_COUNT: int = 5

    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / '.env'),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


def parse_allowed_hosts(value: str) -> list[str]:
    """解析逗号分隔的 ALLOWED_HOSTS，丢弃空白项。"""
    return [host.strip() for host in (value or '').split(',') if host.strip()]


settings = Settings()
