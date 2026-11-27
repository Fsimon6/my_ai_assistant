# -*- coding: utf-8 -*-
"""S17-C 方案 A：普通聊天 / RAG regenerate 后端 API 契约测试。

只使用 tests/api/conftest.py 的临时 SQLite 与 Fake Provider，不访问正式数据。
本文件代码已编写，但本轮不执行。
"""
import json

import pytest

import backend.api.v1.rag as rag_api
import backend.services.conversation_service as conv_mod
import backend.services.llm_service as llm_mod
from backend.models.character import Text
from backend.services.conversation_service import SOURCE_RAG


def _parse_ndjson(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _rows():
    db = conv_mod.SessionLocal()
    try:
        return db.query(Text).order_by(Text.id.asc()).all()
    finally:
        db.close()


class RecordingChatLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def chat_completion(self, messages, stream=False):
        self.calls.append(messages)
        yield self.response


class FailingChatLLM:
    async def chat_completion(self, messages, stream=False):
        raise RuntimeError('chat provider down')
        yield  # pragma: no cover


class FakeRagService:
    def __init__(self, answer='新RAG回答', fail=False):
        self.answer = answer
        self.fail = fail
        self.calls = []

    async def query_with_history(
        self, query, history, context_count=3, stream=False,
        user_id=None, model=None, api_key=None, document_id=None,
        sources_out=None, error_out=None,
    ):
        self.calls.append({'query': query, 'history': history})
        if self.fail:
            if error_out is not None:
                error_out['error'] = 'provider down'
            yield 'partial'
            return
        if sources_out is not None:
            sources_out.append({
                'document_id': 'doc-r', 'filename': 'r.pdf',
                'chunk_index': 0, 'score': 0.9, 'snippet': 'rag',
            })
        yield self.answer


def _rag_pair():
    return (
        '原 RAG 问题',
        '原 RAG 回答',
    )


def _append_rag_pair(user_id, character_id, question, answer, source=SOURCE_RAG):
    conv_id = conv_mod.conversation_service.get_or_create_conversation_id(
        user_id, int(character_id))
    user_message_id = conv_mod.conversation_service.append_message(
        conv_id, 'user', question, 'test-model', meta_info={'source': source})
    assistant_message_id = conv_mod.conversation_service.append_message(
        conv_id, 'assistant', answer, 'test-model', meta_info={'source': source})
    return conv_id, user_message_id, assistant_message_id


def test_normal_first_send_still_inserts_user_and_assistant(api, monkeypatch):
    headers = api.user('regen_normal_first_user')
    character_id = api.create_character(headers)
    llm = RecordingChatLLM('正常回答')
    monkeypatch.setattr(llm_mod, 'get_llm', lambda: llm)

    resp = api.client.post(
        '/api/v1/characters/%s/speak/stream' % character_id,
        headers=headers,
        json={'message': '普通问题'},
    )
    assert resp.status_code == 200, resp.text
    assert any(f['type'] == 'complete' for f in _parse_ndjson(resp.text))
    rows = _rows()
    assert [r.role for r in rows] == ['user', 'assistant']
    assert rows[0].meta_info == {'source': 'chat'}
    assert rows[1].meta_info == {'source': 'chat'}


def test_normal_regenerate_updates_existing_assistant_without_new_rows(api, monkeypatch):
    headers, user_id = api.user_with_id('regen_normal_replace_user')
    character_id = api.create_character(headers)
    first = RecordingChatLLM('旧回答')
    monkeypatch.setattr(llm_mod, 'get_llm', lambda: first)
    resp = api.client.post(
        '/api/v1/characters/%s/speak/stream' % character_id,
        headers=headers,
        json={'message': '原问题'},
    )
    assert resp.status_code == 200, resp.text
    rows = _rows()
    user_message_id, assistant_message_id = rows[0].id, rows[1].id

    second = RecordingChatLLM('新回答')
    monkeypatch.setattr(llm_mod, 'get_llm', lambda: second)
    resp = api.client.post(
        '/api/v1/characters/%s/speak/stream' % character_id,
        headers=headers,
        json={
            'message': '原问题',
            'target_user_message_id': user_message_id,
            'target_assistant_message_id': assistant_message_id,
        },
    )
    assert resp.status_code == 200, resp.text
    frames = _parse_ndjson(resp.text)
    assert any(f['type'] == 'complete' and f['content'] == '新回答' for f in frames)

    rows_after = _rows()
    assert len(rows_after) == 2
    assert rows_after[0].id == user_message_id
    assert rows_after[0].content == '原问题'
    assert rows_after[1].id == assistant_message_id
    assert rows_after[1].content == '新回答'
    assert rows_after[1].meta_info == {'source': 'chat'}

    contents = [m['content'] for m in second.calls[0]]
    assert contents.count('原问题') == 1
    assert '旧回答' not in contents


def test_normal_regenerate_provider_error_keeps_old_answer_and_user(api, monkeypatch):
    headers, user_id = api.user_with_id('regen_normal_error_user')
    character_id = api.create_character(headers)
    monkeypatch.setattr(llm_mod, 'get_llm', lambda: RecordingChatLLM('旧回答'))
    api.client.post(
        '/api/v1/characters/%s/speak/stream' % character_id,
        headers=headers,
        json={'message': '原问题'},
    )
    rows = _rows()
    user_message_id, assistant_message_id = rows[0].id, rows[1].id

    monkeypatch.setattr(llm_mod, 'get_llm', lambda: FailingChatLLM())
    resp = api.client.post(
        '/api/v1/characters/%s/speak/stream' % character_id,
        headers=headers,
        json={
            'message': '原问题',
            'target_user_message_id': user_message_id,
            'target_assistant_message_id': assistant_message_id,
        },
    )
    assert resp.status_code == 200, resp.text
    assert any(f['type'] == 'error' for f in _parse_ndjson(resp.text))
    rows_after = _rows()
    assert len(rows_after) == 2
    assert rows_after[1].content == '旧回答'
    assert rows_after[0].meta_info == {'source': 'chat'}


def test_normal_regenerate_rejects_incomplete_or_wrong_source_target(api, monkeypatch):
    headers, user_id = api.user_with_id('regen_normal_invalid_user')
    character_id = api.create_character(headers)
    monkeypatch.setattr(llm_mod, 'get_llm', lambda: RecordingChatLLM('旧回答'))
    api.client.post(
        '/api/v1/characters/%s/speak/stream' % character_id,
        headers=headers,
        json={'message': '原问题'},
    )
    rows = _rows()
    user_message_id, assistant_message_id = rows[0].id, rows[1].id

    # 只给一个目标字段：SSE error，不执行新请求。
    resp = api.client.post(
        '/api/v1/characters/%s/speak/stream' % character_id,
        headers=headers,
        json={
            'message': '原问题',
            'target_user_message_id': user_message_id,
        },
    )
    assert resp.status_code == 200, resp.text
    assert any(f['type'] == 'error' for f in _parse_ndjson(resp.text))

    # 使用 RAG 来源的目标对调用普通聊天：必须拒绝。
    conv_id, rag_user, rag_assistant = _append_rag_pair(
        user_id, character_id, '原 RAG 问题', '原 RAG 回答')
    resp = api.client.post(
        '/api/v1/characters/%s/speak/stream' % character_id,
        headers=headers,
        json={
            'message': '原 RAG 问题',
            'target_user_message_id': rag_user,
            'target_assistant_message_id': rag_assistant,
        },
    )
    assert resp.status_code == 200, resp.text
    assert any(f['type'] == 'error' for f in _parse_ndjson(resp.text))
    assert len(_rows()) == 4
    assert _rows()[1].content == '旧回答'


def test_rag_regenerate_updates_target_and_excludes_target_and_future_history(api, monkeypatch):
    headers, user_id = api.user_with_id('regen_rag_replace_user')
    character_id = api.create_character(headers)
    conv_id, user_message_id, assistant_message_id = _append_rag_pair(
        user_id, character_id, '原 RAG 问题', '原 RAG 回答')
    # 目标之后再加一轮，必须不进入本次历史。
    _, future_user, future_assistant = _append_rag_pair(
        user_id, character_id, '未来问题', '未来回答')

    fake = FakeRagService(answer='新RAG回答')
    monkeypatch.setattr(rag_api, 'get_rag_service', lambda: fake)

    resp = api.client.post(
        '/api/v1/rag/query-with-history',
        headers=headers,
        json={
            'query': '原 RAG 问题',
            'history': [{'role': 'user', 'content': '客户端历史应被服务端忽略'}],
            'stream': True,
            'character_id': character_id,
            'target_user_message_id': user_message_id,
            'target_assistant_message_id': assistant_message_id,
        },
    )
    assert resp.status_code == 200, resp.text
    frames = _parse_ndjson(resp.text)
    complete = [f for f in frames if f['type'] == 'complete']
    assert len(complete) == 1
    assert complete[0]['content'] == '新RAG回答'
    assert complete[0]['sources'][0]['document_id'] == 'doc-r'

    rows = _rows()
    assert len(rows) == 4  # 原一对 + 未来一对，没有新增
    target = [r for r in rows if r.id == assistant_message_id][0]
    assert target.content == '新RAG回答'
    assert target.meta_info == {'source': SOURCE_RAG}
    assert [r.id for r in rows] == [
        user_message_id, assistant_message_id, future_user, future_assistant]

    history_contents = [m['content'] for m in fake.calls[0]['history']]
    assert '原 RAG 问题' not in history_contents
    assert '原 RAG 回答' not in history_contents
    assert '未来问题' not in history_contents
    assert '客户端历史应被服务端忽略' not in history_contents
    assert fake.calls[0]['query'] == '原 RAG 问题'


def test_rag_regenerate_provider_error_keeps_old_answer(api, monkeypatch):
    headers, user_id = api.user_with_id('regen_rag_error_user')
    character_id = api.create_character(headers)
    _, user_message_id, assistant_message_id = _append_rag_pair(
        user_id, character_id, '原 RAG 问题', '原 RAG 回答')
    fake = FakeRagService(fail=True)
    monkeypatch.setattr(rag_api, 'get_rag_service', lambda: fake)

    resp = api.client.post(
        '/api/v1/rag/query-with-history',
        headers=headers,
        json={
            'query': '原 RAG 问题',
            'history': [],
            'stream': True,
            'character_id': character_id,
            'target_user_message_id': user_message_id,
            'target_assistant_message_id': assistant_message_id,
        },
    )
    assert resp.status_code == 200, resp.text
    assert any(f['type'] == 'error' for f in _parse_ndjson(resp.text))
    rows = _rows()
    assert len(rows) == 2
    assert rows[1].content == '原 RAG 回答'
    assert rows[0].meta_info == {'source': SOURCE_RAG}


def test_rag_regenerate_rejects_cross_source_target(api, monkeypatch):
    headers, user_id = api.user_with_id('regen_rag_cross_source_user')
    character_id = api.create_character(headers)
    conv_id, user_message_id, assistant_message_id = _append_rag_pair(
        user_id, character_id, '普通问题', '普通回答', source='chat')
    fake = FakeRagService()
    monkeypatch.setattr(rag_api, 'get_rag_service', lambda: fake)

    resp = api.client.post(
        '/api/v1/rag/query-with-history',
        headers=headers,
        json={
            'query': '普通问题',
            'history': [],
            'stream': True,
            'character_id': character_id,
            'target_user_message_id': user_message_id,
            'target_assistant_message_id': assistant_message_id,
        },
    )
    assert resp.status_code == 400, resp.text
    assert fake.calls == []
    rows = _rows()
    assert len(rows) == 2
    assert rows[1].content == '普通回答'


def test_history_api_returns_database_message_ids(api, monkeypatch):
    headers, user_id = api.user_with_id('regen_history_id_user')
    character_id = api.create_character(headers)
    conv_id, user_message_id, assistant_message_id = _append_rag_pair(
        user_id, character_id, '历史问题', '历史回答')

    data = api.client.get(
        '/api/v1/characters/%s/conversations' % character_id,
        headers=headers,
    ).json()['data']
    assert data['total'] == 2
    assert data['conversations'][0]['id'] == user_message_id
    assert data['conversations'][1]['id'] == assistant_message_id
    assert data['conversations'][0]['conversation_id'] == conv_id
    assert data['conversations'][1]['conversation_id'] == conv_id
    assert data['conversations'][0]['role'] == 'user'
    assert data['conversations'][1]['role'] == 'assistant'
