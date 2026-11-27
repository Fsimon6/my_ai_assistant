# -*- coding: utf-8 -*-
"""S17-C 方案 A：assistant 原地替换的归属/角色/来源/冲突测试。

只使用临时 SQLite，不接触正式数据库。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.base import Base
from backend.models import character as _models  # noqa: F401
from backend.models.character import Text
from backend.services import conversation_service as conv_mod
from backend.services.conversation_service import (
    SOURCE_CHAT,
    SOURCE_RAG,
    conversation_service as conv,
)

USER = 990101
OTHER_USER = 990102
CHARACTER = 71
OTHER_CHARACTER = 72
MODEL = 'regenerate-contract-model'


@pytest.fixture()
def temp_conv(tmp_path, monkeypatch):
    engine = create_engine(
        'sqlite:///%s' % (tmp_path / 'regenerate.db').as_posix(),
        connect_args={'check_same_thread': False},
    )
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(conv_mod, 'SessionLocal', session)
    Base.metadata.create_all(bind=engine)
    conv_id = conv.get_or_create_conversation_id(USER, CHARACTER)
    yield conv_id, session
    engine.dispose()


def _pair(conv_id, user_content='原问题', assistant_content='原回答', source=SOURCE_CHAT):
    user_id = conv.append_message(
        conv_id, 'user', user_content, MODEL, meta_info={'source': source})
    assistant_id = conv.append_message(
        conv_id, 'assistant', assistant_content, MODEL, meta_info={'source': source})
    return user_id, assistant_id


def _row(session, message_id):
    db = session()
    try:
        return db.query(Text).filter(Text.id == message_id).one()
    finally:
        db.close()


def test_get_history_exposes_id_conversation_and_before_boundary(temp_conv):
    conv_id, session = temp_conv
    first_user, first_assistant = _pair(conv_id)
    second_user, second_assistant = _pair(conv_id, '第二问', '第二答')

    history = conv.get_history(conv_id)
    assert [m['id'] for m in history] == [
        first_user, first_assistant, second_user, second_assistant]
    assert all(m['conversation_id'] == conv_id for m in history)
    assert [m['content'] for m in conv.get_history(
        conv_id, before_message_id=second_user)] == ['原问题', '原回答']


def test_get_regenerate_target_and_replace_keeps_ids_and_user(temp_conv):
    conv_id, session = temp_conv
    user_id, assistant_id = _pair(conv_id)

    target = conv.get_regenerate_target(
        USER, CHARACTER, user_id, assistant_id, SOURCE_CHAT)
    assert target is not None
    assert target['user_content'] == '原问题'
    assert target['assistant_content'] == '原回答'
    original_created_at = _row(session, assistant_id).created_at

    assert conv.replace_assistant_message(
        USER, CHARACTER, user_id, assistant_id, SOURCE_CHAT,
        '原回答', '新回答', MODEL,
    ) is True

    assistant = _row(session, assistant_id)
    assert assistant.id == assistant_id
    assert assistant.conversation_id == conv_id
    assert assistant.role == 'assistant'
    assert assistant.content == '新回答'
    assert assistant.meta_info == {'source': SOURCE_CHAT}
    assert assistant.created_at == original_created_at
    assert _row(session, user_id).content == '原问题'


def test_replace_rejects_wrong_expected_content_without_mutation(temp_conv):
    conv_id, session = temp_conv
    user_id, assistant_id = _pair(conv_id)

    assert conv.replace_assistant_message(
        USER, CHARACTER, user_id, assistant_id, SOURCE_CHAT,
        '不是旧回答', '新回答', MODEL,
    ) is False
    assert _row(session, assistant_id).content == '原回答'


def test_replace_rejects_role_source_conversation_user_and_character_mismatch(temp_conv):
    conv_id, session = temp_conv
    user_id, assistant_id = _pair(conv_id)
    other_conv = conv.get_or_create_conversation_id(USER, OTHER_CHARACTER)
    other_user, other_assistant = _pair(other_conv)

    assert conv.replace_assistant_message(
        USER, CHARACTER, assistant_id, user_id, SOURCE_CHAT,
        '原回答', 'x', MODEL,
    ) is False
    assert conv.replace_assistant_message(
        USER, CHARACTER, user_id, assistant_id, SOURCE_RAG,
        '原回答', 'x', MODEL,
    ) is False
    assert conv.replace_assistant_message(
        OTHER_USER, CHARACTER, user_id, assistant_id, SOURCE_CHAT,
        '原回答', 'x', MODEL,
    ) is False
    assert conv.replace_assistant_message(
        USER, OTHER_CHARACTER, user_id, assistant_id, SOURCE_CHAT,
        '原回答', 'x', MODEL,
    ) is False
    assert conv.replace_assistant_message(
        USER, CHARACTER, user_id, other_assistant, SOURCE_CHAT,
        '原回答', 'x', MODEL,
    ) is False
    assert _row(session, assistant_id).content == '原回答'
    assert _row(session, other_assistant).content == '原回答'


def test_replace_rejects_non_adjacent_target_pair(temp_conv):
    conv_id, session = temp_conv
    user_id = conv.append_message(
        conv_id, 'user', '原问题', MODEL, meta_info={'source': SOURCE_CHAT})
    middle_id = conv.append_message(
        conv_id, 'user', '夹层', MODEL, meta_info={'source': SOURCE_CHAT})
    assistant_id = conv.append_message(
        conv_id, 'assistant', '原回答', MODEL, meta_info={'source': SOURCE_CHAT})
    assert middle_id > user_id

    assert conv.replace_assistant_message(
        USER, CHARACTER, user_id, assistant_id, SOURCE_CHAT,
        '原回答', 'x', MODEL,
    ) is False
    assert _row(session, assistant_id).content == '原回答'


def test_replace_rejects_source_mismatch_before_update(temp_conv):
    conv_id, session = temp_conv
    user_id, assistant_id = _pair(conv_id, source=SOURCE_RAG)

    assert conv.get_regenerate_target(
        USER, CHARACTER, user_id, assistant_id, SOURCE_CHAT) is None
    assert conv.replace_assistant_message(
        USER, CHARACTER, user_id, assistant_id, SOURCE_CHAT,
        '原回答', '新回答', MODEL,
    ) is False
    assert _row(session, assistant_id).content == '原回答'
