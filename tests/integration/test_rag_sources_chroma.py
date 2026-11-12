"""RAG sources 与真实临时 Chroma metadata 的集成断言（不接触正式库）。"""
import pytest

from backend.services import rag_service as rag_mod
from backend.services import vector_service as vec_mod
from backend.services.rag_service import RagService
from backend.services.vector_service import VectorStoreManager

DIM = 384


class FakeEmbeddings:
    def _vec(self, text: str):
        seed = sum(ord(c) for c in text) or 1
        return [((seed * (i + 1)) % 997) / 997.0 for i in range(DIM)]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class FakeLLM:
    async def chat_completion(self, messages, stream=False):
        yield 'answer'


@pytest.mark.asyncio
async def test_extract_sources_reads_real_chroma_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(vec_mod, 'AIAssistantEmbeddings', FakeEmbeddings)
    manager = VectorStoreManager(persist_directory=str(tmp_path / 'chroma'))
    monkeypatch.setattr(vec_mod, '_vector_store_manager', manager)
    monkeypatch.setattr(rag_mod, '_rag_service', None)
    monkeypatch.setattr(rag_mod, 'get_llm', lambda: FakeLLM())

    await manager.add_documents([
        {
            'id': 'chunk-a',
            'content': 'alpha content',
            'metadata': {'document_id': 'doc-real', 'source': 'kb.md'},
        },
        {
            'id': 'chunk-b',
            'content': 'beta content',
            'metadata': {'document_id': 'doc-real', 'source': 'kb.md'},
        },
        {
            'id': 'chunk-c',
            'content': 'alpha content',
            'metadata': {'document_id': 'doc-real', 'source': 'kb.md'},
        },
    ])

    service = RagService()
    sources = []
    _ = [chunk async for chunk in service.rag_query(
        'content', context_count=5, sources_out=sources
    )]

    assert len(sources) == 2
    assert {s['snippet'] for s in sources} == {'alpha content', 'beta content'}
    for source in sources:
        assert source['document_id'] == 'doc-real'
        assert source['filename'] == 'kb.md'
        assert source['chunk_index'] is None
        assert isinstance(source['score'], float)
