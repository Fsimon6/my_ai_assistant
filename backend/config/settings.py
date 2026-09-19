from typing import Literal
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 基础配置
    ENVIRONMENT: str = "development"
    # JWT 签名密钥：必填，无默认值，缺失时应用启动必须失败
    JWT_SECRET: str
    DEBUG: bool = False

    # JWT 访问令牌有效期（分钟）
    # 注：原代码引用 settings.ACCESS_TOKEN_EXPIRE_MINUTES，此处补齐以免启动即 AttributeError
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    # 数据库
    DATABASE_URL: str = "sqlite:///./my_ai_assistant.db"

    # Redis
    REDIS_URL: str | None = None

    # 向量数据库
    VECTOR_DB_PATH: str = "./data/chroma_db"
    VECTOR_DB_COLLECTION: str = "documents"

    # 大模型供应商（通用 API_KEY 驱动 OpenAI 兼容接口）
    LLM_PROVIDER: Literal["openai", "dashscope", "zhipu", "ollama", "local"] = "openai"
    # 通用大模型调用密钥（OpenAI / DashScope / 智谱 使用；Ollama / local 可为空）
    API_KEY: str | None = None
    # OpenAI 兼容接口地址（DeepSeek / 本地 Ollama 等填此处）
    LLM_BASE_URL: str | None = None
    LLM_MODEL: str = "gpt-3.5-turbo"

    # 嵌入模型（EMBEDDING_MODEL 仅用于 Embedding，与 LLM_MODEL（Chat）解耦）
    EMBEDDING_PROVIDER: str = "local"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSIONS: int = 512
    EMBEDDING_DEVICE: str = "cpu"

    # CORS
    BACKEND_CORS_ORIGINS: list[str] | str = []

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v):
        if isinstance(v, str):
            return [i.strip() for i in v.split(",")]
        return v

    # 日志
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
