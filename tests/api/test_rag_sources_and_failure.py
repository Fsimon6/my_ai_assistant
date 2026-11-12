import asyncio
import json

import pytest

import backend.api.v1.rag as rag_api
import backend.services.conversation_service as conv_mod
import backend.services.llm_service as llm_mod
from backend.models.character import Text


def _sources():
    return [
        {
            'document_id': 'doc-a',
            'filename': 'a.pdf',
            'chunk_index': 1,
            'score': 0.75,
            'snippet': 'alpha',
        }
    ]


class FakeRagService:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def query_with_history(
        self, query, history, context_count=3, stream=False,
        user_id=None, model=None, api_key=None, document_id=None,
        sources_out=None, error_out=None,
    ):
        self.calls.append({
            'query': query,
            'history': history,
            'user_id': user_id,
            'sources_out': sources_out,
        })
        if self.fail:
            if error_out is not None:
                error_out['error'] = 'provider down'
            yield 'partial answer'
            return
        if sources_out is not None:
            sources_out.extend(_sources())
        yield 'answer from rag'

    async def rag_query(
        self, query, context_count=3, stream=False, user_id=None,
        model=None, api_key=None, document_id=None,
        sources_out=None, error_out=None,
    ):
        self.calls.append({'query': query, 'user_id': user_id})
        if self.fail:
            if error_out is not None:
                error_out['error'] = 'provider down'
            yield 'partial answer'
            return
        if sources_out is not None:
            sources_out.extend(_sources())
        yield 'answer from rag'


def _parse_ndjson(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _rows(api):
    db = conv_mod.SessionLocal()
    try:
        return db.query(Text).order_by(Text.id.asc()).all()
    finally:
        db.close()


def test_rag_stream_complete_includes_sources(api, monkeypatch):
    headers = api.user('rag_sources_user')
    character_id = api.create_character(headers)
    fake = FakeRagService()
    monkeypatch.setattr(rag_api, 'get_rag_service', lambda: fake)

    resp = api.client.post(
        '/api/v1/rag/query-with-history',
        headers=headers,
        json={
            'query': '问题',
            'history': [],
            'stream': True,
            'context_count': 3,
            'character_id': character_id,
        },
    )
    assert resp.status_code == 200, resp.text
    frames = _parse_ndjson(resp.text)
    complete = [f for f in frames if f['type'] == 'complete']
    assert len(complete) == 1
    assert complete[0]['sources'] == _sources()
    assert complete[0]['content'] == 'answer from rag'
    # 来源必须是当前请求传入的独立容器，而不是全局状态。
    assert fake.calls[0]['sources_out'] is not None


def test_rag_query_stream_complete_includes_sources(api, monkeypatch):
    headers = api.user('rag_query_stream_user')
    monkeypatch.setattr(rag_api, 'get_rag_service', lambda: FakeRagService())

    resp = api.client.post(
        '/api/v1/rag/query',
        headers=headers,
        json={'query': '问题', 'stream': True, 'context_count': 3},
    )
    assert resp.status_code == 200, resp.text
    complete = [f for f in _parse_ndjson(resp.text) if f['type'] == 'complete']
    assert len(complete) == 1
    assert complete[0]['sources'] == _sources()


def test_rag_json_includes_sources(api, monkeypatch):
    headers = api.user('rag_sources_json_user')
    monkeypatch.setattr(rag_api, 'get_rag_service', lambda: FakeRagService())

    resp = api.client.post(
        '/api/v1/rag/query',
        headers=headers,
        json={'query': '问题', 'stream': False, 'context_count': 3},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['sources'] == _sources()


def test_rag_failure_marks_user_message_failed_without_assistant_placeholder(
    api, monkeypatch,
):
    headers = api.user('rag_failure_user')
    character_id = api.create_character(headers)
    monkeypatch.setattr(rag_api, 'get_rag_service', lambda: FakeRagService(fail=True))

    resp = api.client.post(
        '/api/v1/rag/query-with-history',
        headers=headers,
        json={
            'query': '会失败的问题',
            'history': [],
            'stream': True,
            'character_id': character_id,
        },
    )
    assert resp.status_code == 200, resp.text
    frames = _parse_ndjson(resp.text)
    assert any(f['type'] == 'error' for f in frames)
    assert not any(f['type'] == 'complete' for f in frames)

    rows = _rows(api)
    assert len(rows) == 1
    assert rows[0].role == 'user'
    assert rows[0].meta_info == {'source': 'rag', 'status': 'failed'}

    history = api.client.get(
        '/api/v1/characters/%s/conversations' % character_id,
        headers=headers,
    ).json()['data']
    assert history['total'] == 0


def test_rag_uses_server_history_and_excludes_failed_turns(api, monkeypatch):
    headers, user_id = api.user_with_id('rag_history_user')
    character_id = api.create_character(headers)
    conv_id = conv_mod.conversation_service.get_or_create_conversation_id(
        user_id, int(character_id)
    )
    failed_id = conv_mod.conversation_service.append_message(
        conv_id, 'user', '失败问题', model='m',
        meta_info={'source': 'rag'},
    )
    conv_mod.conversation_service.mark_message_failed(failed_id, conv_id, user_id)
    conv_mod.conversation_service.append_message(
        conv_id, 'assistant', '（内容生成失败，请重试）', model='m',
        meta_info={'source': 'rag'},
    )
    conv_mod.conversation_service.append_message(
        conv_id, 'user', '正常历史问题', model='m',
        meta_info={'source': 'chat'},
    )
    fake = FakeRagService()
    monkeypatch.setattr(rag_api, 'get_rag_service', lambda: fake)

    resp = api.client.post(
        '/api/v1/rag/query-with-history',
        headers=headers,
        json={
            'query': '新问题',
            'history': [{'role': 'user', 'content': '客户端伪造历史'}],
            'stream': True,
            'character_id': character_id,
        },
    )
    assert resp.status_code == 200, resp.text
    history_sent = fake.calls[0]['history']
    assert [item['content'] for item in history_sent] == ['正常历史问题']


def test_rag_without_character_keeps_client_history(api, monkeypatch):
    headers = api.user('rag_client_history_user')
    fake = FakeRagService()
    monkeypatch.setattr(rag_api, 'get_rag_service', lambda: fake)
    client_history = [{'role': 'user', 'content': '客户端历史'}]

    resp = api.client.post(
        '/api/v1/rag/query-with-history',
        headers=headers,
        json={
            'query': '新问题',
            'history': client_history,
            'stream': True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert fake.calls[0]['history'] == client_history


def test_get_history_filters_legacy_placeholder_but_keeps_db_rows(api):
    headers, user_id = api.user_with_id('legacy_placeholder_user')
    character_id = api.create_character(headers)
    conv_id = conv_mod.conversation_service.get_or_create_conversation_id(
        user_id, int(character_id)
    )
    failed_id = conv_mod.conversation_service.append_message(
        conv_id, 'user', '失败问题', model='m',
        meta_info={'source': 'chat'},
    )
    conv_mod.conversation_service.mark_message_failed(failed_id, conv_id, user_id)
    conv_mod.conversation_service.append_message(
        conv_id, 'assistant', '（内容生成失败，请重试）', model='m',
        meta_info={'source': 'chat'},
    )
    conv_mod.conversation_service.append_message(
        conv_id, 'user', '正常问题', model='m',
        meta_info={'source': 'chat'},
    )
    conv_mod.conversation_service.append_message(
        conv_id, 'assistant', '正常回答', model='m',
        meta_info={'source': 'chat'},
    )
    conv_mod.conversation_service.append_message(
        conv_id, 'assistant', '讨论内容生成失败的常见原因', model='m',
        meta_info={'source': 'chat'},
    )

    history = conv_mod.conversation_service.get_history(conv_id)
    assert [m['content'] for m in history] == [
        '正常问题', '正常回答', '讨论内容生成失败的常见原因'
    ]

    api_history = api.client.get(
        '/api/v1/characters/%s/conversations' % character_id,
        headers=headers,
    ).json()['data']
    assert api_history['total'] == 3
    assert [m['content'] for m in api_history['conversations']] == [
        '正常问题', '正常回答', '讨论内容生成失败的常见原因'
    ]

    rows = _rows(api)
    assert len(rows) == 5
    assert any(r.content == '（内容生成失败，请重试）' for r in rows)
    assert any(r.meta_info and r.meta_info.get('status') == 'failed' for r in rows)


def test_mark_failed_preserves_metadata_and_only_filters_target_message(api):
    headers, user_id = api.user_with_id('failed_meta_user')
    character_id = api.create_character(headers)
    conv_id = conv_mod.conversation_service.get_or_create_conversation_id(
        user_id, int(character_id)
    )

    failed_id = conv_mod.conversation_service.append_message(
        conv_id, 'user', '失败问题', model='m',
        meta_info={'source': 'chat', 'keep': 'value'},
    )
    normal_id = conv_mod.conversation_service.append_message(
        conv_id, 'user', '正常问题', model='m', meta_info=None,
    )
    assert failed_id != normal_id
    assert conv_mod.conversation_service.mark_message_failed(
        failed_id, conv_id, user_id
    ) is True

    history = conv_mod.conversation_service.get_history(conv_id)
    assert [m['content'] for m in history] == ['正常问题']
    assert history[0]['meta_info'] is None

    db = conv_mod.SessionLocal()
    try:
        failed = db.query(Text).filter(Text.id == failed_id).one()
        assert failed.meta_info['source'] == 'chat'
        assert failed.meta_info['keep'] == 'value'
        assert failed.meta_info['status'] == 'failed'
    finally:
        db.close()


class RecordingChatLLM:
    def __init__(self):
        self.calls = []

    async def chat_completion(self, messages, stream=False):
        self.calls.append(messages)
        yield '正常回答'


class CancellingChatLLM:
    async def chat_completion(self, messages, stream=False):
        raise asyncio.CancelledError()
        yield  # pragma: no cover


class FailingChatLLM:
    async def chat_completion(self, messages, stream=False):
        raise RuntimeError('chat provider down')
        yield  # pragma: no cover


def test_chat_cancelled_error_marks_user_failed(api, monkeypatch):
    headers, user_id = api.user_with_id('chat_cancel_user')
    character_id = api.create_character(headers)
    monkeypatch.setattr(llm_mod, 'get_llm', lambda: CancellingChatLLM())

    resp = api.client.post(
        '/api/v1/characters/%s/speak/stream' % character_id,
        headers=headers,
        json={'message': '取消问题'},
    )
    # TestClient/Starlette 会消费流式异常，不能在这里可靠断言异常向外传播；
    # 取消信号传播由 RagService 单元测试单独验证。
    assert resp.status_code == 200

    rows = _rows(api)
    assert len(rows) == 1
    assert rows[0].role == 'user'
    assert rows[0].meta_info == {'source': 'chat', 'status': 'failed'}


def test_plain_chat_context_excludes_legacy_placeholder(api, monkeypatch):
    headers, user_id = api.user_with_id('legacy_chat_context_user')
    character_id = api.create_character(headers)
    conv_id = conv_mod.conversation_service.get_or_create_conversation_id(
        user_id, int(character_id)
    )
    conv_mod.conversation_service.append_message(
        conv_id, 'assistant', '（内容生成失败，请重试）', model='m',
        meta_info={'source': 'chat'},
    )
    conv_mod.conversation_service.append_message(
        conv_id, 'user', '正常问题', model='m',
        meta_info={'source': 'chat'},
    )
    llm = RecordingChatLLM()
    monkeypatch.setattr(llm_mod, 'get_llm', lambda: llm)

    resp = api.client.post(
        '/api/v1/characters/%s/speak/stream' % character_id,
        headers=headers,
        json={'message': '新问题'},
    )
    assert resp.status_code == 200, resp.text
    assert llm.calls
    contents = [m['content'] for m in llm.calls[0]]
    assert '（内容生成失败，请重试）' not in contents
    assert '正常问题' in contents


def test_chat_failure_marks_user_message_failed_without_assistant_placeholder(
    api, monkeypatch,
):
    headers = api.user('chat_failure_user')
    character_id = api.create_character(headers)
    monkeypatch.setattr(llm_mod, 'get_llm', lambda: FailingChatLLM())

    resp = api.client.post(
        '/api/v1/characters/%s/speak/stream' % character_id,
        headers=headers,
        json={'message': '会失败的问题'},
    )
    assert resp.status_code == 200, resp.text
    frames = _parse_ndjson(resp.text)
    assert any(f['type'] == 'error' for f in frames)

    rows = _rows(api)
    assert len(rows) == 1
    assert rows[0].role == 'user'
    assert rows[0].meta_info == {'source': 'chat', 'status': 'failed'}

    history = api.client.get(
        '/api/v1/characters/%s/conversations' % character_id,
        headers=headers,
    ).json()['data']
    assert history['total'] == 0
