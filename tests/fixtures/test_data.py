"""
测试数据 Fixtures
"""
import pytest
import tempfile
import json
from pathlib import Path


@pytest.fixture
def sample_text():
    """示例文本"""
    return """这是一个示例文档，用于测试，它包含多行内容
    第一行：测试文档
    第二行：包含一些技术术语，如Python、机器学习、自然语言处理等。
    第三行：结束。"""


@pytest.fixture
def sample_document_data(sample_text):
    """示例文档数据"""
    return {
        "id": "doc-001",
        "text": sample_text,
        "metadata": {
            "author": "测试作者",
            "created": "2026-3-3T18:06:00",
            "source": "test",
            "tags": ["test", "sample"]
        }
    }


@pytest.fixture
def sample_chunk_data():
    """示例文档块数据"""
    return [
        {
            "id": "chunk-001",
            "document_id": "doc-001",
            "content": "这是一个示例文档，用于测试",
            "metadata": {"index": 0, "page": 1}
        },
        {
            "id": "chunk-002",
            "document_id": "doc-002",
            "content": "它包含多行内容",
            "metadata": {"index": 1, "page": 1}
        }
    ]


@pytest.fixture
def sample_embedding():
    """示例嵌入向量"""
    return [0.1] * 384  # 384维向量


@pytest.fixture
def temp_json_file(sample_document_data):
    """临时JSON文件"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump(sample_document_data, f, ensure_ascii=False)
        temp_file = f.name

    yield temp_file

    # 清理
    Path(temp_file).unlink(missing_ok=True)


@pytest.fixture
def conversation_history():
    """对话历史示例"""
    return [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！有什么可以帮你的？"},
        {"role": "user", "content": "文档中说了什么？"},
    ]


@pytest.fixture
def rag_query_result():
    """RAG查询结果示例"""
    return {
        "answer": "根据文档，主要讨论了人工智能的应用。",
        "source": [
            {
                "document_id": "doc-001",
                "document_title": "AI简介",
                "content": "人工智能广泛应用于医疗、金融等领域。",
                "similarity": {"page": 3}
            }
        ],
        "confidence": 0.85
    }


@pytest.fixture
def mock_llm_response():
    """模拟LLM响应"""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "这是一个模拟的LLM相应"
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30
        }
    }


@pytest.fixture
def mock_embedding_response():
    """模拟嵌入响应"""
    import numpy as np
    return {
        "data": [
            {"embedding": np.random.randn(384).tolist()}
        ],
        "model": "test-model",
        "usage": {"total_tokens": 5}
    }