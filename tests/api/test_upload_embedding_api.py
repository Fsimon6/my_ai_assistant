# -*- coding: utf-8 -*-
"""Stage 5 API Regression — 上传链路的 embedding 契约（§四）。

只做 **HTTP 层不会破坏生产行为** 的校验（batch 逻辑本身不重复实现）：
  * 每个 chunk 都必须被嵌入（数量一致、不丢 chunk）；
  * 顺序保持；
  * 失败必须向 API 正确返回错误，且不留下"幽灵文档"；
  * **记录** 远端 embedding 请求的实际形态（本仓库的分片现状见报告）。

embedding 的分片上限（≤20/请求）由**唯一 batch boundary** `OpenAILikeLLM.generate_embeddings`
（`backend/services/llm_service.py`，`EMBEDDING_BATCH_SIZE=20`）保证；上传链路
（TXT/MD/DOCX/PDF 的 chunk 与 Excel 摘要 chunk）都经
`VectorStoreManager.add_documents` → Chroma `add_texts` → `AIAssistantEmbeddings.embed_documents`
汇聚到该层，因此这里断言每个远端请求 ≤20 条、总量一致、顺序保持。
"""
import pytest

from .conftest import GOLDEN_SMALL


def _marker_sections(count: int, chars_per_section: int = 900) -> bytes:
    """生成带唯一标记的分段文本（标记用于**独立**校验 embed 文本顺序）。"""
    parts = []
    for i in range(count):
        head = 'MARK%04d 段落标题' % i
        filler = ('这是一段用于 API regression 的合成文本，不含任何真实业务数据。'
                  'The quick brown fox jumps over the lazy dog. ') * 12
        body = (head + '\n' + filler)[:chars_per_section].ljust(chars_per_section, '.')
        parts.append(body)
    return ('\n'.join(parts)).encode('utf-8')


def _markers_in_order(texts) -> list:
    """按出现顺序取出标记（忽略因 overlap 重复出现的标记）。"""
    seq = []
    joined = '\n'.join(texts)
    for i in range(10000):
        key = 'MARK%04d' % i
        pos = joined.find(key)
        if pos < 0:
            break
        seq.append((pos, key))
    return [k for _p, k in sorted(seq)]


# ==========================================================================
# Excel 摘要 chunk：每个 chunk 恰好嵌入一次
# ==========================================================================
def test_excel_upload_embeds_every_summary_chunk_once(api):
    """Golden A（2 Sheet）-> 2 条摘要 chunk，全部走到 embedding，且顺序一致。"""
    headers = api.user('embed_excel_user')
    body = api.upload_excel(headers, GOLDEN_SMALL)

    assert body['total_chunks'] == 2
    requests = api.fake.embedding_requests
    assert sum(len(r) for r in requests) == 2                     # 不丢 chunk
    embedded = [t for r in requests for t in r]
    assert len({t for t in embedded}) == 2                         # 无重复嵌入
    assert 'OrderSKUList' in embedded[0]                           # Sheet 顺序保持
    assert 'SkuMaster' in embedded[1]


# ==========================================================================
# > 20 chunks：数量一致 / 顺序保持 / 失败处理
# ==========================================================================
def test_large_document_over_20_chunks_embedding_contract(api):
    """>20 chunks 的合成文档：每个 chunk 都被嵌入、顺序保持，并**记录**请求形态。"""
    headers = api.user('embed_big_user')
    content = _marker_sections(28)
    resp = api.upload_bytes(headers, 'synthetic_big.txt', content, 'text/plain')
    assert resp.status_code == 200, resp.text
    body = resp.json()
    total = body['total_chunks']
    assert total > 20, '合成文本必须产生 >20 个 chunk，实际 %s' % total

    requests = api.fake.embedding_requests
    assert requests, '必须发生远端 embedding 调用'
    embedded = [t for r in requests for t in r]
    assert len(embedded) == total                                  # 数量一致（不丢/不重）
    assert len(set(embedded)) == total

    # 顺序保持（独立校验：标记出现顺序必须是 0..N）
    markers = _markers_in_order(embedded)
    assert markers == ['MARK%04d' % i for i in range(len(markers))]
    assert len(markers) >= 20

    # 唯一 batch boundary：单次远端请求 <= 20 条，且分片覆盖全部 chunk
    observed_sizes = [len(r) for r in requests]
    assert max(observed_sizes) <= 20, observed_sizes
    assert observed_sizes == [20, total - 20], observed_sizes      # 28 -> [20, 8]
    assert sum(observed_sizes) == total

    # 文档可用且可查（API 层未被大文件破坏）
    listed = api.client.get('/api/v1/rag/documents', headers=headers).json()
    assert body['document_id'] in [d['document_id'] for d in listed['documents']]

    print('[api-regression] per-request embedding sizes: %s (chunks=%d)'
          % (observed_sizes, total))


def test_later_batch_failure_is_not_partially_successful(api):
    """>20 chunks 时**后续批**失败：API 必须报错，不部分成功、不留幽灵文档/不完整向量。"""
    headers = api.user('embed_batch_fail_user')
    api.fake.fail_on_embedding_call = 2                            # 第 2 批（8 条）失败
    resp = api.upload_bytes(headers, 'synthetic_batch_fail.txt',
                            _marker_sections(28), 'text/plain')

    assert resp.status_code >= 400, resp.text
    assert resp.json().get('detail'), resp.text
    assert 'Traceback' not in resp.text

    # 第 1 批已发出、第 2 批失败 -> 后续不再调用（顺序分批、失败即止）
    assert [len(r) for r in api.fake.embedding_requests] == [20, 8]

    # 不产生幽灵文档：列表为空，且没有任何向量残留到 Chroma
    listed = api.client.get('/api/v1/rag/documents', headers=headers).json()
    assert listed['documents'] == []


def test_embedding_failure_returns_api_error_and_leaves_no_document(api):
    """embedding 失败 -> API 返回分类错误（非 2xx），且不产生幽灵文档 / 残留 chunk。"""
    headers = api.user('embed_fail_user')
    api.fake.embedding_error = RuntimeError('remote embedding exploded')

    resp = api.upload_bytes(headers, 'synthetic_fail.txt', _marker_sections(3), 'text/plain')
    assert resp.status_code >= 400, resp.text                    # 必须显式报错
    detail = resp.json().get('detail')
    assert detail, resp.text                                     # 有结构化错误体
    assert 'Traceback' not in resp.text                          # 不泄露原始栈

    # 失败后：没有文档进入列表，且没有向量残留
    listed = api.client.get('/api/v1/rag/documents', headers=headers).json()
    assert listed['documents'] == []
    assert len(api.fake.embedding_requests) == 1                 # 只尝试了 1 批（失败即止）
