"""
RAG 链路集成测试（适配当前 RagService）

6A-6（2026-10-08）修复点（**只改测试，不改生产代码**）：
  1. `RagService.__init__()` 当前**不接受任何参数**（内部取
     `get_vector_store_manager()` / `get_llm()` / `get_cache_service()` 单例），
     原测试 `RagService(vector_manager=...)` 必然 `TypeError`；
  2. 当前对外方法是 `rag_query(...)`（**异步生成器**，逐块 yield 文本），
     不存在原测试调用的 `query()`；
  3. 依赖注入沿用仓库既有约定（`tests/api/conftest.py:226-231`）：
     把 `vector_service._vector_store_manager` 指向**临时目录**的
     `VectorStoreManager`（并用本地假 embedding），再 patch
     `rag_service.get_llm` 为假 LLM —— 因此不会触碰真实 `data/chroma_db`，
     也不会调用真实模型 / embedding API。

测试目标不变：验证**当前** RagService + **当前** vector service + **当前** RAG 行为
（检索 → 构建上下文 → 组 prompt → 流式输出）。
"""
import pytest

from backend.services import rag_service as rag_mod
from backend.services import vector_service as vec_mod
from backend.services.rag_service import RagService
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


class FakeLLM:
    """假 LLM：记录收到的 messages，流式返回可预期文本。"""

    def __init__(self, reply: str = '这是基于检索上下文的模拟回答。'):
        self.reply = reply
        self.calls = []

    async def chat_completion(self, messages, stream: bool = False, temperature=None):
        self.calls.append(messages)
        yield self.reply


@pytest.fixture
def rag_env(tmp_path, monkeypatch):
    """临时向量库 + 假 embedding + 假 LLM 的 RagService 环境。"""
    monkeypatch.setattr(vec_mod, 'AIAssistantEmbeddings', FakeEmbeddings)
    manager = VectorStoreManager(persist_directory=str(tmp_path / 'chroma'))
    # 注入单例：RagService 内部 get_vector_store_manager() 会读该模块全局变量
    monkeypatch.setattr(vec_mod, '_vector_store_manager', manager)
    monkeypatch.setattr(rag_mod, '_rag_service', None)
    fake_llm = FakeLLM()
    monkeypatch.setattr(rag_mod, 'get_llm', lambda: fake_llm)
    return RagService(), manager, fake_llm


@pytest.mark.asyncio
class TestRagChain:
    async def test_rag_chain_creation(self, rag_env):
        """RagService 使用注入的当前向量存储与 LLM 构造成功"""
        service, manager, fake_llm = rag_env
        assert isinstance(service, RagService)
        assert service.vector_store is manager
        assert service.llm is fake_llm

    async def test_rag_query(self, rag_env):
        """检索 → 构建上下文 → 组 prompt → 流式返回（真实验证当前 RAG 行为）"""
        service, manager, fake_llm = rag_env
        await manager.add_documents([{
            'id': 'chunk_1',
            'content': '机器学习是人工智能的一个分支',
            'metadata': {'document_id': 'doc_1', 'source': 'kb.md'},
        }])

        chunks = [chunk async for chunk in service.rag_query('什么是机器学习？')]
        answer = ''.join(chunks)

        # 1) 流式输出即假 LLM 的回答
        assert answer == fake_llm.reply
        # 2) 检索结果确实进入了 system prompt（上下文被组装进消息）
        assert fake_llm.calls, 'rag_query 未调用 LLM'
        system_content = fake_llm.calls[0][0]['content']
        assert '机器学习是人工智能的一个分支' in system_content
        # 3) 用户问题原样进入 user message
        assert fake_llm.calls[0][1]['content'] == '什么是机器学习？'

    async def test_rag_query_without_documents_still_answers(self, rag_env):
        """向量库为空时仍应给出（基于"没有找到相关文档内容"的）回答，而非抛异常"""
        service, manager, fake_llm = rag_env
        answer = ''.join([c async for c in service.rag_query('空库提问')])
        assert answer == fake_llm.reply
        assert '没有找到相关文档内容' in fake_llm.calls[0][0]['content']
