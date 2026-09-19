"""
嵌入模型模块
提供文本向量化功能，支持多种嵌入模型
"""

from .base_embedding import BaseEmbedding, EmbeddingResult
from .embedding_service import EmbeddingService
from .models import EmbeddingConfig, EmbeddingType
from .cache_manager import EmbeddingCache

# LocalEmbeddings 依赖 sentence-transformers/torch，仅在显式使用时导入，
# 避免未安装 torch 时阻断整个 embeddings 包及测试导入。
def __getattr__(name):
    if name == 'LocalEmbeddings':
        from .local_embeddings import LocalEmbeddings
        return LocalEmbeddings
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')

__all__ = [
    'BaseEmbedding',
    'EmbeddingResult',
    'LocalEmbeddings',
    'EmbeddingService',
    'EmbeddingConfig',
    'EmbeddingType',
    'EmbeddingCache'
]