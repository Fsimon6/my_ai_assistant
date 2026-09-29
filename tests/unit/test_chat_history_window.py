# -*- coding: utf-8 -*-
"""P1 回归测试：对话历史接口必须返回**最新** N 条（而不是最早 N 条）。

真实故障（2026-09-29）：conv 38 共 108 条消息，前端请求 limit=100 时，
旧实现 `history[offset:offset+limit]` 返回**最早 100 条**（id 950..1049），
最新 8 条（含 id=1056/1057 两条 Excel 记录）被裁掉 → 用户重新进入对话以为"记录丢失"。

修复：`backend/api/v1/characters.py::_latest_window` —— offset=0 表示"最新 limit 条"，
返回顺序仍是**旧→新**（前端无需 reverse）；offset 表示"跳过最新 offset 条"。

本文件覆盖：
  * 纯函数 `_latest_window` 的边界矩阵（total < / == / > limit、total=0、offset 超界、limit=1）
  * **端点级**真实调用（临时 SQLite）：108 条 -> 返回最新 100 条，且**最新一条必须存在**、
    顺序为旧→新、total 保持 108（§八 A~G）
"""
import asyncio
import json
import os
import tempfile
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api.v1.characters import _latest_window, get_character_conversations
from backend.database.base import Base
from backend.models.character import AICharacter, Conversation          # noqa: F401
from backend.services import character_service as char_mod
from backend.services import conversation_service as conv_mod
from backend.services.conversation_service import conversation_service as conv

USER_ID = 61
MODEL = 'test-model'


# ==========================================================================
# 纯函数：窗口边界（§四 / §五）
# ==========================================================================
@pytest.mark.parametrize('total,offset,limit,want', [
    (0, 0, 100, (0, 0)),          # total=0 -> 空
    (20, 0, 100, (0, 20)),        # total < limit -> 全部
    (100, 0, 100, (0, 100)),      # total == limit -> 全部
    (101, 0, 100, (1, 101)),      # total=101 -> 丢掉最老 1 条，保留最新 100
    (108, 0, 100, (8, 108)),      # 真实案例：108 -> 第 9..108 条（含最新）
    (108, 100, 100, (0, 8)),      # 跳过最新 100 条 -> 只剩更早的 8 条
    (108, 108, 100, (0, 0)),      # offset == total -> 空（不是全部）
    (108, 500, 100, (0, 0)),      # offset 超界 -> 空（绝不负索引切片）
    (5, 0, 1, (4, 5)),            # limit=1 -> 只有最新一条
    (108, 8, 100, (0, 100)),      # 跳过最新 8 条 -> 前面 100 条
])
def test_latest_window_bounds(total, offset, limit, want):
    start, end = _latest_window(total, offset, limit)
    assert (start, end) == want
    assert 0 <= start <= end <= total          # 恒成立的不变量


def test_latest_window_keeps_chronological_order():
    history = list(range(950, 1058))           # 旧 -> 新
    start, end = _latest_window(len(history), 0, 100)
    win = history[start:end]
    assert len(win) == 100
    assert win[0] == 958 and win[-1] == 1057   # 最新一条必须在窗口内
    assert win == sorted(win)                  # 返回顺序仍为旧→新


# ==========================================================================
# 端点级：真实调用（临时 SQLite）
# ==========================================================================
def _seed(point):
    engine = create_engine('sqlite:///%s' % os.path.join(point, 'hist.db'))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return engine, factory


@pytest.fixture()
def temp_env(monkeypatch):
    """临时 DB：角色 + 会话（把两个 service 的会话工厂都指向它）。"""
    point = tempfile.mkdtemp(prefix='hist_window_')
    engine, factory = _seed(point)
    monkeypatch.setattr(conv_mod, 'SessionLocal', factory)
    monkeypatch.setattr(char_mod, 'SessionLocal', factory)
    session = factory()
    character = AICharacter(name='窗口测试角色', model=MODEL, user_id=USER_ID,
                            system_prompt='（测试角色）')
    session.add(character)
    session.commit()
    session.refresh(character)
    yield character, session
    session.close()
    engine.dispose()


async def _call_endpoint(character_id, limit, offset):
    resp = await get_character_conversations(
        character_id=str(character_id), limit=limit, offset=offset,
        current_user=SimpleNamespace(id=USER_ID), db=None)
    return json.loads(resp.body)['data']


def _add_messages(conv_id, count):
    for i in range(count):
        role = 'user' if i % 2 == 0 else 'assistant'
        conv.append_message(conv_id, role, 'msg-%03d' % i, model=MODEL, meta_info=None)


def test_endpoint_total_gt_limit_returns_latest(temp_env):
    """§八 A/E/F/G：total=108 > limit=100 -> 最新 100 条，且**最新一条必须存在**。"""
    character, _session = temp_env
    conv_id = conv.get_or_create_conversation_id(USER_ID, character.id)
    _add_messages(conv_id, 108)

    data = asyncio.run(_call_endpoint(character.id, 100, 0))
    convs = data['conversations']
    assert data['total'] == 108
    assert len(convs) == 100
    assert convs[-1]['content'] == 'msg-107'        # 最新一条在（修复前会丢）
    assert convs[0]['content'] == 'msg-008'         # 丢掉的只有最老的 8 条
    ids = [c['created_at'] for c in convs]
    assert ids == sorted(ids)                       # 顺序仍为旧→新


def test_endpoint_total_equal_limit_returns_all(temp_env):
    """§八 B：total == limit -> 全量返回。"""
    character, _session = temp_env
    conv_id = conv.get_or_create_conversation_id(USER_ID, character.id)
    _add_messages(conv_id, 100)

    data = asyncio.run(_call_endpoint(character.id, 100, 0))
    assert data['total'] == 100 and len(data['conversations']) == 100
    assert data['conversations'][0]['content'] == 'msg-000'
    assert data['conversations'][-1]['content'] == 'msg-099'


def test_endpoint_total_less_than_limit_returns_all(temp_env):
    """§八 C：total < limit -> 全部返回（顺序不变）。"""
    character, _session = temp_env
    conv_id = conv.get_or_create_conversation_id(USER_ID, character.id)
    _add_messages(conv_id, 20)

    data = asyncio.run(_call_endpoint(character.id, 100, 0))
    assert data['total'] == 20 and len(data['conversations']) == 20
    assert data['conversations'][-1]['content'] == 'msg-019'


def test_endpoint_empty_conversation(temp_env):
    """§四 5：total=0 -> 空列表（不是错误）。"""
    character, _session = temp_env
    conv.get_or_create_conversation_id(USER_ID, character.id)
    data = asyncio.run(_call_endpoint(character.id, 100, 0))
    assert data['total'] == 0 and data['conversations'] == []


def test_endpoint_offset_skips_latest(temp_env):
    """offset 语义：offset=100 -> 跳过最新 100 条（用于将来加载更早历史）。"""
    character, _session = temp_env
    conv_id = conv.get_or_create_conversation_id(USER_ID, character.id)
    _add_messages(conv_id, 108)

    data = asyncio.run(_call_endpoint(character.id, 100, 100))
    convs = data['conversations']
    assert data['total'] == 108 and len(convs) == 8
    assert convs[-1]['content'] == 'msg-007'        # 更早的那批
    assert convs[0]['content'] == 'msg-000'


def test_endpoint_real_conv38_like_case_includes_newest_excel_pair(temp_env):
    """与真实故障同形：最后两条是 Excel 记录 -> 必须出现在返回里（含 meta_info 透传）。"""
    character, _session = temp_env
    conv_id = conv.get_or_create_conversation_id(USER_ID, character.id)
    _add_messages(conv_id, 106)
    conv.append_message(conv_id, 'user', '列出一店物流商为SF的订单号和物流商',
                        model=MODEL, meta_info={'source': 'excel'})
    conv.append_message(conv_id, 'assistant', '已从「直邮一店 8.20号订单.xlsx」…',
                        model=MODEL, meta_info={'source': 'excel',
                                                'excel': {'schema_version': 1, 'kind': 'result',
                                                          'payload': {'rows': [1, 2, 3, 4]}}})

    data = asyncio.run(_call_endpoint(character.id, 100, 0))
    convs = data['conversations']
    assert data['total'] == 108
    assert convs[-2]['content'].startswith('列出一店物流商')
    assert convs[-1]['meta_info']['source'] == 'excel'
    assert convs[-1]['meta_info']['excel']['kind'] == 'result'      # 结构化卡片可恢复
    assert len(convs) == 100
