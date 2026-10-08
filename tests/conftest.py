"""
测试配置和夹具
"""
import pytest
import asyncio
import tempfile
import os
from pathlib import Path
from typing import Dict, Any

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