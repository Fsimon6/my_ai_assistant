# -*- coding: utf-8 -*-
"""Stage 5：生成**共享的历史快照 fixture**（后端契约测试与前端 restore 契约共用同一份结构）。

做法：在 Golden A（纯合成就绪数据）上跑**真实** NL 管线（LLM 用确定性注入，零 provider 调用）
      -> `build_excel_history_snapshot` -> 只保留稳定字段 -> 落 JSON。

为什么要"从真实管线生成"：避免手写出与生产 schema 不一致的假快照；
    fixtures 一旦生成即**固定入库**，CI / 前端 vitest 只读它，不再依赖运行时。

明确剔除的字段（不稳定，绝不固定进 fixture）：
    message_id / conversation_id / created_at / token usage / provider 原始响应 / narrative 全文

运行：`python tests/fixtures/excel_history/_generate_history_fixtures.py`
"""
import asyncio
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
os.environ['PYTHONPATH'] = ROOT

from backend.api.v1.rag import build_excel_history_snapshot      # noqa: E402
from backend.excel import aggregate as ag                        # noqa: E402
from backend.excel import multi_step as ms                       # noqa: E402
from backend.excel import nl_query as nq                         # noqa: E402
from backend.excel import store as excel_store                   # noqa: E402
from tests.helpers import fixtures as F                          # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CARRIER, AMOUNT, SKU = F.CARRIER, F.AMOUNT, F.SKU


class _FakeLLM:
    """占位对象：NL 解析被下面替换为确定性注入，绝不触发真实 provider。"""


def _run(rep, message, turn, key):
    orig = nq.llm_parse_turn

    async def fake(llm, msg, catalog, context=None, analysis_context=None):
        return turn
    nq.llm_parse_turn = fake
    orig_load = excel_store.load_representation
    excel_store.load_representation = lambda document_id, user_id=None: rep
    try:
        return asyncio.run(nq.run_nl_query(message, F.catalog_of(rep), llm=_FakeLLM(),
                                           user_id=0, session_key=key))
    finally:
        nq.llm_parse_turn = orig
        excel_store.load_representation = orig_load


def _trim(snap):
    """只保留稳定字段（当前 schema：schema_version/kind/document/sheet/engine/
    relaxed_filters/continued/history_truncated/payload）。"""
    keep = ('schema_version', 'kind', 'document', 'sheet', 'engine',
            'inherited_from', 'relaxed_filters', 'continued', 'history_truncated', 'payload')
    return {k: snap[k] for k in keep if k in snap}


def _drop_runs(payload):
    """旧快照：移除 Stage 3 新增的可选增量字段（用于兼容性 fixture）。"""
    out = {k: v for k, v in payload.items() if not k.startswith('matched_row_run')}
    if isinstance(out.get('step1'), dict):
        out['step1'] = {k: v for k, v in out['step1'].items()
                        if not k.startswith('matched_row_run')}
    return out


def main():
    rep = F.parse_golden()
    out = {}

    # ---- table（无筛选：连续 3~21）----
    t_table = nq.TurnIntent(action=nq.ACTION_NEW_QUERY, intent=nq.intent_from_dict(
        {'query_type': nq.INTENT_STRUCTURED, 'sheet': F.SMALL_SHEET,
         'columns': ['Order ID', CARRIER], 'limit': 50}))
    out['table'] = _trim(build_excel_history_snapshot(
        _run(rep, '列出订单号和物流商', t_table, 'fx-table')))

    # ---- aggregate（eq SF -> 唯一前缀放宽为 contains：覆盖 relaxed_filters；
    #      用中性提问避免确定性 filter authority 把 eq 提前改写成 contains）----
    t_agg = nq.TurnIntent(action=nq.ACTION_AGGREGATE, aggregate=nq.AggregateIntent(
        operation=ag.OPERATION_SUM, column=AMOUNT, sheet=F.SMALL_SHEET,
        filters=[{'column': CARRIER, 'operator': 'eq', 'value': 'SF'}]))
    out['aggregate'] = _trim(build_excel_history_snapshot(
        _run(rep, '帮我看看', t_agg, 'fx-agg')))

    # ---- group_aggregate（按物流商分组 SUM）----
    t_group = nq.TurnIntent(action=nq.ACTION_AGGREGATE, aggregate=nq.AggregateIntent(
        operation=ag.OPERATION_SUM, column=AMOUNT, group_by=[CARRIER], sheet=F.SMALL_SHEET,
        order_by=ag.ORDER_BY_AGGREGATE, order_dir='desc'))
    out['group_aggregate'] = _trim(build_excel_history_snapshot(
        _run(rep, '各物流商的订单金额汇总', t_group, 'fx-group')))

    # ---- multi_step（SKU 分组 SUM 取前 3 再求和 = 1400.00）----
    t_multi = nq.TurnIntent(action=nq.ACTION_ANALYSIS, analysis=nq.AnalysisIntent(
        sheet=F.SMALL_SHEET, steps=[
            {'type': ms.STEP_GROUP_AGGREGATE, 'group_by': [SKU],
             'operation': ag.OPERATION_SUM, 'column': AMOUNT,
             'order_by': ag.ORDER_BY_AGGREGATE, 'order_dir': 'desc', 'top_n': 3},
            {'type': ms.STEP_AGGREGATE, 'operation': ag.OPERATION_SUM,
             'source': ms.SOURCE_STEP_1, 'column': ms.INTERMEDIATE_VALUE_COLUMN}]))
    out['multi_step'] = _trim(build_excel_history_snapshot(
        _run(rep, '订单金额最高的前3个SKU的销售额总和', t_multi, 'fx-multi')))

    # ---- 旧快照（缺 Stage 3 新增字段）----
    legacy = json.loads(json.dumps(out['table'], ensure_ascii=False))
    legacy['payload'] = _drop_runs(legacy['payload'])
    out['legacy_table'] = legacy
    legacy_multi = json.loads(json.dumps(out['multi_step'], ensure_ascii=False))
    legacy_multi['payload'] = _drop_runs(legacy_multi['payload'])
    out['legacy_multi_step'] = legacy_multi

    for name, snap in out.items():
        path = os.path.join(HERE, '%s.json' % name)
        with io.open(path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(snap, f, ensure_ascii=False, indent=2, sort_keys=False)
            f.write('\n')
        print('已写出 %-22s kind=%-16s schema_version=%s payload_keys=%d'
              % (name + '.json', snap.get('kind'), snap.get('schema_version'),
                 len(snap.get('payload') or {})))


if __name__ == '__main__':
    main()
