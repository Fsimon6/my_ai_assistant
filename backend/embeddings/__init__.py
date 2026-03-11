"""
嵌入模型模块
提供文本向量化功能，支持多种嵌入模型
"""

from .base_embedding import BaseEmbedding, EmbeddingResult
from .qianfan_embeddings import QianfanEmbeddings
from .local_embeddings import LocalEmbeddings
from .embedding_service import EmbeddingService
from .models import EmbeddingConfig, EmbeddingType
from .cache_manager import EmbeddingCache

__all__ = [
    'BaseEmbedding',
    'EmbeddingResult',
    'QianfanEmbeddings',
    'LocalEmbeddings',
    'EmbeddingService',
    'EmbeddingConfig',
    'EmbeddingType',
    'EmbeddingCache'
]