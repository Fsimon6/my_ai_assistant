"""
嵌入模型单元测试
"""
import pytest
from backend.embeddings.embedding_service import EmbeddingService
from backend.embeddings.models import EmbeddingConfig, EmbeddingType


class TestEmbeddings:
    """嵌入模型测试类"""

    def test_local_embedding(self):
        """测试本地嵌入模型"""
        config = EmbeddingConfig(
            embedding_type=EmbeddingType.LOCAL,
            model_name="BAAI/bge-small-zh-v1.5",
            dimensions=512
        )
        service = EmbeddingService(config)
        service.initialize()

        texts = ["这是一个测试句子。", "这是另一个测试句子。"]
        result = service.embed(texts)

        assert len(result.embeddings) == 2
        assert len(result.embeddings[0]) == config.dimensions

    def test_qianfan_embedding(self):
        """测试Qianfan嵌入模型（需要API密钥）"""
        # 如果没有设置API密钥，跳过测试
        import os
        if not os.getenv("QIANFAN_API_KEY"):
            pytest.skip("未设置QIANFAN_API_KEY，跳过测试")
        config = EmbeddingConfig(
            embedding_type=EmbeddingType.QIANFAN,
            model_name="text-embedding-3-small",
            dimensions=1536
        )
        service = EmbeddingService(config)
        service.initialize()

        texts = ["This is a test sentence.", "This is another test sentence."]
        result = service.embed(texts)
        assert len(result.embeddings) == 2
        assert len(result.embeddings[0]) == config.dimensions