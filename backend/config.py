"""
FastAPI应用配置
"""
import os


class Config:
    """应用配置类"""

    # 应用信息
    APP_NAME: str = 'My AI Assistant API'
    APP_VERSION: str = '1.0.0'
    APP_DESCRIPTION: str = '基于大模型的本地知识库智能问答助手API'

    # 服务器配置（Windows通常使用127.0.0.1而不是0.0.0.0）
    HOST: str = os.getenv('HOST', '0.0.0.0')
    PORT: int = int(os.getenv('PORT', 8080))
    DEBUG: bool = os.getenv('DEBUG', 'false').lower() == 'true'

    # 大模型配置（通用 API_KEY 驱动）
    LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'openai')
    LLM_MODEL = os.getenv('LLM_MODEL', 'gpt-3.5-turbo')
    LLM_BASE_URL = os.getenv('LLM_BASE_URL')
    API_KEY = os.getenv('API_KEY')
    LLM_TEMPERATURE = float(os.getenv('LLM_TEMPERATURE', '0.7'))
    LLM_MAX_TOKENS = int(os.getenv('LLM_MAX_TOKENS', '2000'))

    # 向量数据库
    VECTOR_DB_PATH = os.getenv('VECTOR_DB_PATH', './data/chroma_db')
    VECTOR_DB_COLLECTION = os.getenv('VECTOR_DB_COLLECTION', 'ai_assistant_docs')

    # 文档处理
    CHUNK_SIZE = int(os.getenv('CHUNK_SIZE', '800'))
    CHUNK_OVERLAP = int(os.getenv('CHUNK_OVERLAP', '150'))

    # CORS配置（与 settings.BACKEND_CORS_ORIGINS 对齐）
    BACKEND_CORS_ORIGINS = os.getenv('BACKEND_CORS_ORIGINS', 'http://localhost:5173').split(',')

    # 安全配置（JWT 签名密钥，无默认值，必须从环境变量读取）
    SECRET_KEY: str = os.getenv('JWT_SECRET')
    TOKEN_EXPIRE_HOURS = int(os.getenv('TOKEN_EXPIRE_HOURS', '24'))

    @classmethod
    def get_llm_api_key(cls) -> str:
        """获取通用 API_KEY"""
        api_key = cls.API_KEY
        if not api_key:
            raise ValueError('请设置 API_KEY 环境变量')
        return api_key

    @classmethod
    def validate_config(cls):
        """配置校验（详细校验由 settings.py 完成，这里仅做兼容性提示）"""
        from backend.config.settings import settings

        # 按 provider 校验 API_KEY 是否必填
        require_key = {'openai', 'dashscope', 'zhipu'}
        if settings.LLM_PROVIDER in require_key and not settings.API_KEY:
            raise ValueError(
                f'provider={settings.LLM_PROVIDER} 需要设置 API_KEY 环境变量'
            )

        print('√ 配置验证通过')
        print(f' LLM Provider：{settings.LLM_PROVIDER}')
        print(f' Vector DB: {cls.VECTOR_DB_PATH}')

