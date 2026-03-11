"""
嵌入模型数据模型
"""
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class EmbeddingType(str, Enum):
    """嵌入类型"""
    QIANFAN = 'qianfan'
    LOCAL = 'local'
    AZURE = 'azure'
    COHERENT = 'cohere'


class EmbeddingConfig(BaseModel):
    """嵌入配置"""
    embedding_type: EmbeddingType = Field(default=EmbeddingType.LOCAL, description='嵌入类型')
    model_name: str = Field(default='ERNIE-3.5-8k', description='模型名称')
    dimensions: int = Field(default=512, description='向量维度')
    batch_size: int = Field(default=32, description='批处理大小')
    max_length: int = Field(default=8192, description='最大长度')

    # Qianfan特定配置
    qianfan_api_key: Optional[str] = Field(default=None, description='QianfanAPI密钥')
    qianfan_base_url: Optional[str] = Field(default=None, description='Qianfan基础URL')

    # 本地模型配置
    device: str = Field(default='auto', description='设备(auto/cpu/cuda)')
    model_cache_dir: Optional[str] = Field(default=None, description='模型缓存目录')

    # 缓存配置
    use_cache: bool = Field(default=True, description='是否使用缓存')
    cache_size: int = Field(default=1024, description='缓存大小')
    cache_ttl: int = Field(default=3600, description='缓存生存时间（秒）')

    class Config:
        json_schema_extra = {
            'example': {
                'embedding_type': 'local',
                'model_name': 'ERNIE-3.5-8k',
                'dimensions': 512,
                'batch_size': 32,
                'max_length': 8192,
                'device': 'auto',
                'use_cache': True,
                'cache_size': 1024,
                'cache_ttl': 3600,
            }
        }