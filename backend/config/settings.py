from typing import Literal
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 基础配置
    ENVIRONMENT: str = "development"
    SECRET_KEY: str
    DEBUG: bool = False

    # 数据库
    DATABASE_URL: str = "sqlite:///./my_ai_assistant.db"

    # Redis
    REDIS_URL: str | None = None

    # 向量数据库
    VECTOR_DB_PATH: str = "./data/chroma_db"
    VECTOR_DB_COLLECTION: str = "documents"

    # 大模型供应商
    LLM_PROVIDER: Literal["qianfan", "openai", "zhipu", "local"] = "qianfan"

    # 百度千帆
    QIANFAN_API_KEY: str | None = None
    QIANFAN_SECRET_KEY: str | None = None
    QIANFAN_MODEL: str = "ERNIE-3.5-8K"

    # OpenAI
    OPENAI_API_KEY: str | None = None
    OPENAI_API_BASE: str | None = None
    OPENAI_MODEL: str = "gpt-3.5-turbo"

    # 嵌入模型
    EMBEDDING_PROVIDER: str = "local"
    EMBEDDING_MODEL: str = "BAAI/bge-small-zh-v1.5"
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
