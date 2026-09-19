"""
嵌入模型单元测试
"""
import pytest
from backend.embeddings.embedding_service import EmbeddingService
from backend.embeddings.models import EmbeddingConfig, EmbeddingType


class TestEmbeddings:
    """嵌入模型测试类"""

    def test_local_embedding(self):
        """测试本地嵌入模型（依赖**可选**的 sentence-transformers / torch）"""
        # 二者不在 backend/requirements.txt 的运行依赖内（见该文件末尾说明），
        # 未安装时跳过，避免全新克隆后 `pytest tests/unit` 直接失败。
        pytest.importorskip('sentence_transformers',
                            reason='未安装 sentence-transformers：本地嵌入属可选能力')
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