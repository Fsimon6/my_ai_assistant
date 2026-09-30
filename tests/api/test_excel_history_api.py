# -*- coding: utf-8 -*-
"""Stage 5 API Regression — Excel 历史 / 快照（§七）。

链路：`POST /api/v1/rag/excel/nl-query`（带 character_id）
        -> conversation_service 落库（user + assistant，source='excel'，assistant 带 excel 快照）
      -> `GET /api/v1/characters/{character_id}/conversations` 重新读取
      -> 快照必须能恢复结构化结果，并且与**同一次 HTTP 响应**的结果一致。

覆盖四类结果：table / aggregate / group_aggregate / multi_step。
另验证：普通聊天（main.py:/speak/stream 使用的 `get_history(exclude_sources=[SOURCE_EXCEL])`）
不会把 Excel 轮次当成普通聊天上下文。
"""
import pytest

import backend.services.conversation_service as conv_mod
from tests.helpers import fixtures as F
from .conftest import GOLDEN_SMALL

CARRIER, AMOUNT, SKU = F.CARRIER, F.AMOUNT, F.SKU
ORDER_ID = 'Order ID'
MULTI_MSG = '订单金额最高的前3个SKU的销售额总和是多少'


def _turn_new(columns, sheet=F.SMALL_SHEET):
    return {'action': 'new_query', 'sheet': sheet, 'columns': list(columns), 'filters': []}


def _turn_agg(op, column=None, group_by=None, sheet=F.SMALL_SHEET):
    return {'action': 'aggregate', 'aggregate_operation': op, 'column': column,
            'group_by': list(group_by or []), 'filters': [], 'sheet': sheet}


def _multi_plan():
    from backend.excel import multi_step as ms
    return {'action': 'analysis', 'sheet': F.SMALL_SHEET, 'steps': [
        {'type': ms.STEP_GROUP_AGGREGATE, 'group_by': [SKU], 'operation': 'sum',
         'column': AMOUNT, 'filters': [], 'order_by': 'aggregate_value',
         'order_dir': 'desc', 'top_n': 3},
        {'type': ms.STEP_AGGREGATE, 'operation': 'sum', 'source': ms.SOURCE_STEP_1,
         'column': ms.INTERMEDIATE_VALUE_COLUMN}]}


def _snapshot_mirrors_live(snapshot_kind: str, live: dict, payload: dict) -> None:
    """快照 payload 必须镜像同一次 HTTP 响应（不丢字段、不截断、不双层包裹）。"""
    if snapshot_kind == 'result':                    # 结构化查询（表格）
        assert payload['total_matches'] == live['result']['total_matches'] == 19
        assert payload['returned_count'] == len(live['result']['rows']) == 19
    elif snapshot_kind == 'aggregate':
        assert round(float(payload['value']), 2) == round(
            float(live['aggregate']['value']), 2) == F.SMALL_TOTAL_SUM
        assert payload['matched_rows'] == live['aggregate']['matched_rows'] == 19
    elif snapshot_kind == 'group_aggregate':
        assert len(payload['rows']) == len(live['group_aggregate']['rows']) == 3
        assert payload['operation'] == live['group_aggregate']['operation'] == 'count'
    elif snapshot_kind == 'multi_step':
        assert round(float(payload['value']), 2) == round(
            float(live['multi_step']['value']), 2) == F.SMALL_MULTI_STEP_VALUE
    else:                                            # pragma: no cover - 防御
        raise AssertionError('未知 kind：%s' % snapshot_kind)


@pytest.fixture()
def env(api):
    headers, user_id = api.user_with_id('history_user')
    api.upload_excel(headers, GOLDEN_SMALL)
    return api, headers, user_id


# ==========================================================================
# 四类结果的落库 + 读回 + 快照一致性
# ==========================================================================
@pytest.mark.parametrize('case_id,snapshot_kind,message,turn_payload,analysis_payload', [
    ('table', 'result', '列出订单号和物流商', _turn_new([ORDER_ID, CARRIER]), None),
    ('aggregate', 'aggregate', '订单金额总和是多少', _turn_agg('sum', AMOUNT), None),
    ('group', 'group_aggregate', '各物流商分别有多少订单',
     _turn_agg('count', None, group_by=[CARRIER]), None),
    ('multi_step', 'multi_step', MULTI_MSG, _multi_plan(), _multi_plan()),
])
def test_excel_turn_is_persisted_with_snapshot_and_roundtrips(
        env, case_id, snapshot_kind, message, turn_payload, analysis_payload):
    api, headers, _user_id = env
    api.fake.turn_payload = turn_payload
    api.fake.analysis_payload = analysis_payload

    character_id = api.create_character(headers)
    live = api.nl_ok(headers, message, character_id=character_id, session_id='hist-%s' % case_id)

    resp = api.client.get('/api/v1/characters/%s/conversations' % character_id, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()['data']
    assert data['total'] == 2
    conversations = data['conversations']
    assert [c['role'] for c in conversations] == ['user', 'assistant']
    assert conversations[0]['content'] == message

    # 来源标记：整轮都是 excel（普通聊天据此整轮丢弃）
    assert conversations[0]['meta_info']['source'] == 'excel'
    assert conversations[1]['meta_info']['source'] == 'excel'

    # assistant 的 excel 快照：单层包裹（历史回归：曾多包一层导致卡片无法恢复）
    excel_meta = conversations[1]['meta_info']['excel']
    assert excel_meta['schema_version'] == 1
    assert excel_meta['kind'] == snapshot_kind
    assert 'payload' in excel_meta
    _snapshot_mirrors_live(snapshot_kind, live, excel_meta['payload'])


def test_history_window_returns_latest_messages(env):
    """P1 历史消息窗口：`limit` 取**最新** N 条，返回顺序仍是旧 -> 新。"""
    api, headers, _user_id = env
    api.fake.turn_payload = _turn_agg('sum', AMOUNT)
    character_id = api.create_character(headers)
    api.nl_ok(headers, '订单金额总和是多少', character_id=character_id, session_id='win')

    resp = api.client.get('/api/v1/characters/%s/conversations' % character_id,
                          headers=headers, params={'limit': 1})
    data = resp.json()['data']
    assert data['total'] == 2                       # 总数仍是完整历史
    assert len(data['conversations']) == 1
    assert data['conversations'][0]['role'] == 'assistant'   # 最新一条

    older = api.client.get('/api/v1/characters/%s/conversations' % character_id,
                           headers=headers, params={'limit': 1, 'offset': 1}).json()['data']
    assert older['conversations'][0]['role'] == 'user'


def test_other_user_cannot_read_history_snapshot(env):
    """User B 不能读取 A 的对话历史（拿不到 excel 快照）。"""
    api, headers, _user_id = env
    api.fake.turn_payload = _turn_agg('sum', AMOUNT)
    character_id = api.create_character(headers)
    api.nl_ok(headers, '订单金额总和是多少', character_id=character_id, session_id='iso')

    _b_headers, _b_id = api.user_with_id('history_other')
    resp = api.client.get('/api/v1/characters/%s/conversations' % character_id,
                          headers=_b_headers)
    assert resp.status_code == 404


def test_plain_chat_context_excludes_excel_turns(env):
    """普通聊天上下文隔离（生产消费点：main.py:/speak/stream 的 get_history(exclude_sources)）。"""
    api, headers, user_id = env
    api.fake.turn_payload = _turn_agg('sum', AMOUNT)
    character_id = api.create_character(headers)
    api.nl_ok(headers, '订单金额总和是多少', character_id=character_id, session_id='ctx')

    conv_id = conv_mod.conversation_service.get_existing_conversation_id(
        user_id, int(character_id))
    assert conv_id is not None

    assert len(conv_mod.conversation_service.get_history(conv_id)) == 2
    assert conv_mod.conversation_service.get_history(
        conv_id, exclude_sources=[conv_mod.SOURCE_EXCEL]) == []
