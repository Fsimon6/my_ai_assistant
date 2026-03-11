"""
集成测试：向量数据库测试（适配现有 VectorStoreManager）
"""
import pytest
import tempfile
import shutil
import asyncio

from backend.services.vector_service import VectorStoreManager


@pytest.fixture
def temp_vector_db():
    """临时向量数据库目录"""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
def vector_manager(temp_vector_db):
    """VectorStoreManager 实例"""
    # 注意：VectorStoreManager 的 __init__ 会创建 Chroma 实例，需要异步初始化？但当前代码是同步的。
    # 为了测试，我们直接实例化，但需要确保 embed_documents 方法不会实际调用外部 API。
    # 可以打补丁替换 embeddings 为 mock。
    manager = VectorStoreManager(persist_directory=temp_vector_db)
    return manager


@pytest.fixture
def mock_embeddings(monkeypatch):
    """模拟 embeddings，避免真实调用"""
    class MockEmbeddings:
        def embed_documents(self, texts):
            # 返回固定维度的模拟向量（384维）
            import numpy as np
            return [np.random.randn(384).tolist() for _ in texts]

        def embed_query(self):
            import numpy as np
            return np.random.randn(384).tolist()

    monkeypatch.setattr("backend.services.vector_service.AIAssistantEmbeddings", MockEmbeddings)
    # 注意：VectorStoreManager 初始化时创建了 self.embeddings = AIAssistantEmbeddings()
    # 因此需要在使用 vector_manager fixture 之前打补丁，或者直接在 fixture 内替换。


class TestVectorDB:
    """向量数据库测试"""

    def test_add_documents(self, vector_manager):
        """测试添加文档"""
        documents = [
            {
                "id": "doc1",
                "content": "这是第一个测试文档",
                "metadata": {"source": "test1", "page": 1}
            },
            {
                "id": "doc2",
                "content": "这是第二个测试文档",
                "metadata": {"source": "test2", "page": 2}
            }
        ]

        # 由于 add_documents 是异步方法，需要运行事件循环
        async def add():
            ids = await vector_manager.add_documents(documents)
            return ids

        ids = asyncio.run(add())
        assert len(ids) == 2
        assert ids == ["doc1", "doc2"]

        # 验证集合信息
        info = vector_manager.get_collection_info()
        assert info["total_documents"] == 2

    def test_search(self, vector_manager):
        """测试相似度搜索"""
        # 先添加文档
        documents = [
            {
                "id": "doc1",
                "content": "机器学习是人工智能的一个分支",
                "metadata": {"source": "test1"}
            },
            {
                "id": "doc2",
                "content": "深度学习是机器学习的一个子集",
                "metadata": {"source": "test2"}
            },
            {
                "id": "doc3",
                "content": "自然语言处理涉及文本理解",
                "metadata": {"source": "test3"}
            }
        ]

        async def add():
            await vector_manager.add_documents(documents)

        asyncio.run(add())

        # 搜索
        async def search():
            results = await vector_manager.search(query="人工智能", k=2)
            return results

        results = asyncio.run(search())
        assert len(results) == 2
        for res in results:
            assert "content" in res
            assert "metadata" in res
            assert "score" in res
            assert "id" in res

    def test_delete_documents(self, vector_manager):
        """测试删除文档"""
        documents = [
            {
                "id": "doc_to_delete",
                "content": "要删除的内容",
                "metadata": {"source": "test"}
            }
        ]

        async def add_and_delete():
            await vector_manager.add_documents(documents)
            # 验证存在
            info_before = vector_manager.get_collection_info()
            assert info_before["total_documents"] == 1

            # 删除
            success = await vector_manager.delete_documents(ids=["doc_to_delete"])
            assert success is True

            # 验证删除后
            info_after = vector_manager.get_collection_info()
            assert info_after["total_documents"] == 0

        asyncio.run(add_and_delete())

    def test_collection_info(self, vector_manager):
        """测试获取集合信息"""
        info = vector_manager.get_collection_info()
        assert "total_documents" in info
        assert "collection_name" in info
        assert "persist_directory" in info
        assert info["total_documents"] == 0  # 初始应为0

    def test_persistence(self, temp_vector_db):
        """测试数据持久化"""
        # 创建第一个管理器并添加数据
        manager1 = VectorStoreManager(persist_directory=temp_vector_db)

        async def add():
            docs = [{"id": "persist_doc", "content": "持久化测试", "metadata": {}}]
            await manager1.add_documents(docs)

        asyncio.run(add())

        # 创建第二个管理器（应该加载已有数据）
        manager2 = VectorStoreManager(persist_directory=temp_vector_db)
        info = manager2.get_collection_info()
        assert info["total_documents"] == 1

        # 验证能搜索到
        async def search():
            results = await manager2.search(query="持久化", k=1)
            return results

        results = asyncio.run(search())
        assert len(results) == 1
        assert results[0]["content"] == "持久化测试"
