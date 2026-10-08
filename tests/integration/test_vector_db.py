"""
集成测试：向量数据库（适配当前 VectorStoreManager）

6A-6（2026-10-08）修复点（**只改测试，不改生产代码**）：
  1. embedding 全部替换为**本地确定性假实现**（`FakeEmbeddings`），
     测试不再调用远程 embedding API（原 `mock_embeddings` fixture 从未被
     `vector_manager` 依赖，等于没生效；且其 `embed_query(self)` 签名与
     LangChain 实际调用的 `embed_query(text)` 不符）；
  2. 临时目录改用 pytest 的 `tmp_path`，并在 teardown 里尽力释放 Chroma 客户端，
     不再手工 `shutil.rmtree`（原写法在 Windows 上因 Chroma 文件锁报
     `PermissionError: [WinError 32] ... data_level0.bin`）；
  3. 删除用例对齐**当前真实 API**：`delete_documents(document_ids=[...], user_id=None)`
     返回删除数量，且按 `metadata.document_id` 匹配（原测试传 `ids=` 并断言 `is True`，
     且 metadata 里没有 document_id，必然失败）。

测试目标不变：仍真实验证当前 `VectorStoreManager` 的
add / search / delete / collection_info / persistence 行为。
"""
import asyncio

import pytest

from backend.services.vector_service import VectorStoreManager

DIM = 384


class FakeEmbeddings:
    """确定性假 embedding（不联网、可复现）。"""

    def _vec(self, text: str):
        seed = sum(ord(c) for c in text) or 1
        return [((seed * (i + 1)) % 997) / 997.0 for i in range(DIM)]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


@pytest.fixture
def fake_embeddings(monkeypatch):
    """在 VectorStoreManager 构造前，把生产 embedding 类替换为本地假实现。

    `VectorStoreManager.__init__` 通过模块全局名 `AIAssistantEmbeddings()`
    实例化 embedding，因此 patch 模块属性即可生效。
    """
    monkeypatch.setattr(
        "backend.services.vector_service.AIAssistantEmbeddings", FakeEmbeddings)
    return FakeEmbeddings


def _release(manager) -> None:
    """尽力释放 Chroma 客户端（避免 Windows 下文件锁影响临时目录清理）。"""
    try:
        client = getattr(manager.vector_store, "_client", None)
        system = getattr(client, "_system", None)
        if system is not None:
            system.stop()
    except Exception:                                   # noqa: BLE001 - 清理失败不影响结论
        pass


@pytest.fixture
def vector_manager(tmp_path, fake_embeddings):
    """VectorStoreManager 实例（独立临时目录 + 假 embedding）"""
    manager = VectorStoreManager(persist_directory=str(tmp_path / "chroma"))
    yield manager
    _release(manager)


class TestVectorDB:
    """向量数据库测试"""

    def test_add_documents(self, vector_manager):
        """测试添加文档"""
        documents = [
            {"id": "doc1", "content": "这是第一个测试文档",
             "metadata": {"document_id": "doc1", "source": "test1", "page": 1}},
            {"id": "doc2", "content": "这是第二个测试文档",
             "metadata": {"document_id": "doc2", "source": "test2", "page": 2}},
        ]

        ids = asyncio.run(vector_manager.add_documents(documents))
        assert ids == ["doc1", "doc2"]

        info = vector_manager.get_collection_info()
        assert info["total_documents"] == 2

    def test_search(self, vector_manager):
        """测试相似度搜索"""
        documents = [
            {"id": "doc1", "content": "机器学习是人工智能的一个分支",
             "metadata": {"document_id": "doc1", "source": "test1"}},
            {"id": "doc2", "content": "深度学习是机器学习的一个子集",
             "metadata": {"document_id": "doc2", "source": "test2"}},
            {"id": "doc3", "content": "自然语言处理涉及文本理解",
             "metadata": {"document_id": "doc3", "source": "test3"}},
        ]
        asyncio.run(vector_manager.add_documents(documents))

        results = asyncio.run(vector_manager.search(query="人工智能", k=2))
        assert len(results) == 2
        for res in results:
            assert "content" in res
            assert "metadata" in res
            assert "score" in res
            assert "id" in res

    def test_delete_documents(self, vector_manager):
        """测试按 document_id 删除（当前 API：返回删除数量）"""
        documents = [
            {"id": "vec_1", "content": "要删除的内容",
             "metadata": {"document_id": "doc_to_delete", "source": "test"}},
        ]
        asyncio.run(vector_manager.add_documents(documents))
        assert vector_manager.get_collection_info()["total_documents"] == 1

        removed = asyncio.run(
            vector_manager.delete_documents(document_ids=["doc_to_delete"]))
        assert removed == 1
        assert vector_manager.get_collection_info()["total_documents"] == 0

    def test_delete_documents_respects_user_id(self, vector_manager):
        """删除必须按 user_id 隔离：他人 document_id 不可删除"""
        documents = [
            {"id": "vec_owner1", "content": "用户1的文档",
             "metadata": {"document_id": "d1", "user_id": 1}},
            {"id": "vec_owner2", "content": "用户2的文档",
             "metadata": {"document_id": "d2", "user_id": 2}},
        ]
        asyncio.run(vector_manager.add_documents(documents))

        removed = asyncio.run(
            vector_manager.delete_documents(document_ids=["d2"], user_id=1))
        assert removed == 0
        assert vector_manager.get_collection_info()["total_documents"] == 2

        removed = asyncio.run(
            vector_manager.delete_documents(document_ids=["d2"], user_id=2))
        assert removed == 1
        assert vector_manager.get_collection_info()["total_documents"] == 1

    def test_collection_info(self, vector_manager):
        """测试获取集合信息"""
        info = vector_manager.get_collection_info()
        assert info["total_documents"] == 0
        assert info["collection_name"] == "ai_assistant_docs"
        assert info["persist_directory"] == vector_manager.persist_directory

    def test_persistence(self, tmp_path, fake_embeddings):
        """测试数据持久化：第二个管理器应能读到第一个写入的数据"""
        persist_dir = str(tmp_path / "chroma")

        manager1 = VectorStoreManager(persist_directory=persist_dir)
        asyncio.run(manager1.add_documents(
            [{"id": "persist_doc", "content": "持久化测试",
              "metadata": {"document_id": "persist_doc"}}]))

        # 注意：不要在两个 manager 之间 stop() Chroma 全局 system ——
        # chromadb 的 system 是**进程级单例**，停掉后同进程再建客户端会
        # 报 "Could not connect to tenant default_tenant"。释放统一放到测试末尾。
        manager2 = VectorStoreManager(persist_directory=persist_dir)
        assert manager2.get_collection_info()["total_documents"] == 1

        results = asyncio.run(manager2.search(query="持久化", k=1))
        assert len(results) == 1
        assert results[0]["content"] == "持久化测试"
