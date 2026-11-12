import asyncio

import pytest

from backend.services.rag_service import RagService


class FakeVectorStore:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def search(self, query, k=5, filter_dict=None):
        self.calls.append((query, k, filter_dict))
        return self.results


class FakeLLM:
    async def chat_completion(self, messages, stream=False):
        yield '测试回答'


def _result(doc_id, chunk_index, content='片段内容', score=0.8):
    return {
        'content': content,
        'metadata': {
            'document_id': doc_id,
            'filename': '%s.pdf' % doc_id,
            'chunk_index': chunk_index,
            'user_id': 7,
        },
        'score': score,
    }


def test_extract_sources_deduplicates_and_keeps_real_fields():
    service = RagService()
    sources = service._extract_sources([
        _result('doc-a', 1, 'alpha', 0.9),
        _result('doc-a', 1, 'duplicate', 0.1),
        _result('doc-b', 2, 'beta', 0.5),
    ])

    assert [s['document_id'] for s in sources] == ['doc-a', 'doc-b']
    assert sources[0]['filename'] == 'doc-a.pdf'
    assert sources[0]['chunk_index'] == 1
    assert sources[0]['score'] == 0.9
    assert sources[0]['snippet'] == 'alpha'


def test_extract_sources_index_fallback_does_not_drop_distinct_content():
    service = RagService()
    sources = service._extract_sources([
        _result('doc-a', 1, 'chunk one'),
        _result('doc-a', 2, 'chunk two'),
        _result('doc-a', None, 'missing index one'),
        _result('doc-a', None, 'missing index two'),
        _result('doc-a', 'bad', 'invalid index one'),
        _result('doc-a', 'bad', 'invalid index two'),
        _result('doc-a', None, 'missing index one'),
        {'content': 'same', 'metadata': {'document_id': 'doc-b', 'chunk_index': 1}, 'score': 0.1},
    ])

    assert [s['snippet'] for s in sources] == [
        'chunk one',
        'chunk two',
        'missing index one',
        'missing index two',
        'invalid index one',
        'invalid index two',
        'same',
    ]
    assert sources[2]['chunk_index'] is None
    assert sources[4]['chunk_index'] is None


def test_extract_sources_skips_missing_document_id():
    service = RagService()
    assert service._extract_sources([
        {'content': 'x', 'metadata': {'chunk_index': 1}, 'score': 0.2}
    ]) == []


@pytest.mark.asyncio
async def test_rag_query_writes_request_sources_to_out_container():
    service = RagService()
    service.vector_store = FakeVectorStore([_result('doc-a', 1)])
    service.llm = FakeLLM()
    sources = []
    chunks = [
        chunk async for chunk in service.rag_query(
            '问题', user_id=7, sources_out=sources
        )
    ]

    assert ''.join(chunks) == '测试回答'
    assert len(sources) == 1
    assert sources[0]['document_id'] == 'doc-a'


@pytest.mark.asyncio
async def test_rag_query_empty_sources_returns_empty_list():
    service = RagService()
    service.vector_store = FakeVectorStore([])
    service.llm = FakeLLM()
    sources = []
    _ = [chunk async for chunk in service.rag_query(
        '问题', user_id=7, sources_out=sources
    )]

    assert sources == []


@pytest.mark.asyncio
async def test_rag_query_keeps_sources_isolated_across_concurrent_requests():
    class QueryVectorStore:
        async def search(self, query, k=5, filter_dict=None):
            return [_result('doc-%s' % query, 1, query)]

    class SlowLLM:
        async def chat_completion(self, messages, stream=False):
            yield 'answer'

    service = RagService()
    service.vector_store = QueryVectorStore()
    service.llm = SlowLLM()

    async def run(query, user_id):
        sources = []
        _ = [chunk async for chunk in service.rag_query(
            query, user_id=user_id, sources_out=sources
        )]
        return sources

    first, second = await asyncio.gather(
        run('one', 1), run('two', 2), return_exceptions=False
    )
    assert [s['document_id'] for s in first] == ['doc-one']
    assert [s['document_id'] for s in second] == ['doc-two']


@pytest.mark.asyncio
async def test_rag_query_keeps_user_filter_in_vector_search():
    class FilterVectorStore:
        def __init__(self):
            self.filters = []

        async def search(self, query, k=5, filter_dict=None):
            self.filters.append(filter_dict)
            return []

    service = RagService()
    vector = FilterVectorStore()
    service.vector_store = vector
    service.llm = FakeLLM()
    sources = []
    _ = [chunk async for chunk in service.rag_query(
        '问题', user_id=42, sources_out=sources
    )]
    assert vector.filters == [{'user_id': 42}]
    assert sources == []


@pytest.mark.asyncio
async def test_rag_query_does_not_swallow_cancelled_error():
    class CancellingLLM:
        async def chat_completion(self, messages, stream=False):
            raise asyncio.CancelledError()
            yield  # pragma: no cover

    service = RagService()
    service.vector_store = FakeVectorStore([_result('doc-a', 1)])
    service.llm = CancellingLLM()
    sources = []

    with pytest.raises(asyncio.CancelledError):
        _ = [chunk async for chunk in service.rag_query(
            '问题', user_id=7, sources_out=sources
        )]


@pytest.mark.asyncio
async def test_rag_query_clears_sources_on_failure():
    class ExplodingLLM:
        async def chat_completion(self, messages, stream=False):
            raise RuntimeError('provider down')
            yield  # pragma: no cover

    service = RagService()
    service.vector_store = FakeVectorStore([_result('doc-a', 1)])
    service.llm = ExplodingLLM()
    sources = [_result('stale', 0)]
    errors = {}
    chunks = [
        chunk async for chunk in service.rag_query(
            '问题', user_id=7, sources_out=sources, error_out=errors
        )
    ]

    assert sources == []
    assert errors['error'] == 'provider down'
    assert '查询过程中出现错误' in ''.join(chunks)
