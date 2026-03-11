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

    # 大模型配置
    LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'qianfan')
    LLM_MODEL = os.getenv('LLM_MODEL', 'ERNIE-3.5-8k')
    LLM_TEMPERATURE = float(os.getenv('LLM_TEMPERATURE', '0.7'))
    LLM_MAX_TOKENS = int(os.getenv('LLM_MAX_TOKENS', '2000'))

    # API密钥
    QIANFAN_API_KEY = os.getenv('QIANFAN_API_KEY')
    QIANFAN_SECRET_KEY = os.getenv('QIANFAN_SECRET_KEY')
    DASHSCOPE_API_KEY = os.getenv('DASHSCOPE_API_KEY')
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    ZHIPU_API_KEY = os.getenv('ZHIPU_API_KEY')

    # 向量数据库
    VECTOR_DB_PATH = os.getenv('VECTOR_DB_PATH', './data/chroma_db')
    VECTOR_DB_COLLECTION = os.getenv('VECTOR_DB_COLLECTION', 'ai_assistant_docs')

    # 文档处理
    CHUNK_SIZE = int(os.getenv('CHUNK_SIZE', '800'))
    CHUNK_OVERLAP = int(os.getenv('CHUNK_OVERLAP', '150'))

    # CORS配置
    ALLOWED_ORIGINS = os.getenv('ALLOWED_ORIGINS', 'http://localhost:5173').split(',')

    # 安全配置
    SECRET_KEY: str = os.getenv('SECRET_KEY', 'iBaE6BgWJTzMBjm0yoHLhzWrzSe9dVWv')
    TOKEN_EXPIRE_HOURS = int(os.getenv('TOKEN_EXPIRE_HOURS', '24'))

    @classmethod
    def get_llm_api_key(cls) -> str:
        """获取当前provider的API密钥"""
        provider = cls.LLM_PROVIDER.upper()
        api_key = getattr(cls, f'{provider}_API_KEY')

        if not api_key:
            raise ValueError(f'请设置{provider}_API_KEY环境变量')

        return api_key

    @classmethod
    def get_llm_api_secret(cls) -> str:
        """获取当前provider的API密钥"""
        provider = cls.LLM_PROVIDER.upper()
        api_secret = getattr(cls, f'{provider}_API_SECRET', '')
        return api_secret

    @classmethod
    def validate_config(cls):
        """验证配置"""
        required = [
            ('LLM_PROVIDER', cls.LLM_PROVIDER),
            ('API_KEY', cls.get_llm_api_key())
        ]

        for name, value in required:
            if not value:
                raise ValueError(f'配置项{name}不能为空')

        print('√ 配置验证通过')
        print(f' LLM Provider：{cls.LLM_PROVIDER}')
        print(f' API Key: {cls.get_llm_api_key()[:10]}...')
        print(f' Vector DB: {cls.VECTOR_DB_PATH}')

