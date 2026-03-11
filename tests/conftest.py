"""
测试配置和夹具
"""
import pytest
import asyncio
import tempfile
import os
from pathlib import Path
from typing import Generator, AsyncGenerator, Dict, Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 添加项目根目录到Python路径
import sys
sys.path.append(str(Path(__file__).parent.parent))


from backend.main import app
from backend.database.base import Base, get_db
from backend.config.testing import TestingConfig


# 测试数据库配置
TEST_DATABASE_URL = "sqlite:///:memory:"

# 创建测试引擎和会话
engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
TestingSessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


def override_get_db():
    """重写数据库依赖"""
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()


# 重写应用依赖
app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(scope="session")
def event_loop():
    """创建事件循环"""
    loop = asyncio.get_event_loop_policy().get_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def test_config():
    """提供测试配置"""
    return TestingConfig()


@pytest.fixture(scope="session")
def db_engine():
    """数据库引擎"""
    # 创建所有表
    Base.metadata.create_all(bind=engine)
    yield engine
    # 清理表
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session(db_engine):
    """数据库对话"""
    connection = db_engine.connect()
    transaction = connection.begin()
    session = TestingSessionLocal(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def client(db_session):
    """FastAPI测试客户端"""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def test_user() -> Dict[str, Any]:
    """测试用户数据"""
    return {
        "email": "test@example.com",
        "password": "testpassword123",
        "username": "testuser",
        "full_name": "Test User",
    }


@pytest.fixture
def authenticated_client(client, test_user):
    """已认证的测试客户端"""
    # 注册用户
    register_response = client.post("/auth/register", json=test_user)

    # 登录获取令牌
    login_response = client.post(
        "/api/v1/auth/login",
        json={
            "email": test_user["email"],
            "password": test_user["password"],
        }
    )

    token = login_response.json()["access_token"]

    # 设置认证头
    client.headers.update({"Authorization": f"Bearer {token}"})

    return client


@pytest.fixture
def mock_qianfan():
    """Mock Qianfan API"""
    with patch("backend.services.llm_service.Qianfan") as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance

        # 配置mock响应
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "This is a mock response"
        mock_response.usage = MagicMock()
        mock_response.usage.total_tokens = 10
        mock_instance.chat.completions.create.return_value = mock_response
        yield mock_instance


@pytest.fixture
def mock_embedding():
    """Mock Embedding API"""
    with patch("backend.embeddings.qianfan_embeddings.Qianfan") as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance

        # 配置mock响应
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.data[0].embedding = [0.1] * 1536
        mock_instance.embeddings.create.return_value = mock_response
        yield mock_instance


@pytest.fixture
def temp_test_file():
    """创建临时测试文件"""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8"
    ) as f:
        f.write("This is a test document for unit testing.\n")
        f.write("It contains multiple lines of text.\n")
        f.write("This test will be used for embedding and search tests.")
    yield f.name

    # 清理
    if os.path.exists(f.name):
        os.unlink(f.name)


@pytest.fixture
def sample_pdf_data():
    """PDF测试数据"""
    return {
        'filename': 'test(空).pdf',
        'content': 'Sample PDF content',
        'pages': 3,
        'metadata': {
            "author": "Test Author",
            "title": "Test Document",
        }
    }


@pytest.fixture
def test_document_data():
    """测试文档数据"""
    return {
        'title': '测试文档',
        'content': '这是一个测试文档的内容，包含一些技术术语和描述',
        'metadata': {
            'source': 'test',
            'language': 'zh',
            'category': 'test'
        }
    }


@pytest.fixture
def rag_service_mock():
    """Mock RAG服务"""
    with patch("backend.services.rag_service.RAGService") as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance
        mock_instance.query.return_value = {
            'answer': 'This is a mock answer',
            'sources': [
                {
                    'content': 'Source document content',
                    'metadata': {'source': 'test'}
                }
            ],
            'confidence': 0.85
        }

        yield mock_instance


@pytest.fixture(scope="session")
def test_vector_db():
    """测试向量数据库"""
    import chromadb
    from chromadb.config import Settings

    # 使用临时目录
    temp_dir = tempfile.mkdtemp()
    client = chromadb.Client(Settings(
        chroma_db_impl='duckdb+parquet',
        persist_directory=temp_dir,
        anonymize_telemetry=False
    ))

    # 创建测试集合
    collection = client.create_collection(name='test_documents')
    yield collection, client

    # 清理
    client.clear_system_cache()
    client = None


@pytest.fixture
def async_client():
    """异步测试客户端"""
    from httpx import AsyncClient

    async def get_client():
        async with AsyncClient(app=app, base_url='http://test') as ac:
            yield ac

    return get_client
