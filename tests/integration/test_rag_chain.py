"""
RAG链条集成测试
"""
# tests/integration/test_rag_chain.py
import pytest
from backend.services.rag_service import RagService
from backend.services.vector_service import VectorStoreManager

@pytest.mark.asyncio
class TestRagChain:
    async def test_rag_chain_creation(self, tmp_path):
        # 使用临时目录初始化向量存储
        vector_manager = VectorStoreManager(persist_directory=str(tmp_path / "chroma"))
        service = RagService(vector_manager=vector_manager)
        assert service is not None

    async def test_rag_query(self, tmp_path):
        # 准备测试数据：先添加一些文档到向量库
        vector_manager = VectorStoreManager(persist_directory=str(tmp_path / "chroma"))
        # ... 添加文档逻辑 ...
        service = RagService(vector_manager=vector_manager)
        result = await service.query("测试问题")
        assert result is not None
        assert "answer" in result