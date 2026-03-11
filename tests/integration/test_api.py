"""
集成测试：API端点测试
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
import json

from backend.main import app
from backend.database.base import get_db
from backend.services.rag_service import RagService


@pytest.fixture
def client(db_session):
    """测试客户端"""
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


class TestAuthAPI:
    """认证API测试"""

    def test_register(self, client):
        """测试用户注册"""
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": "test@example.com",
                "password": "password123",
                "username": "testuser",
                "full_name": "Test User"
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert data["email"] == "test@example.com"
        assert "access_token" in data

    def test_login(self, client, test_user):
        """测试用户登录"""
        # 先注册
        client.post("/api/v1/auth/register", json=test_user)

        # 登录
        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": test_user["email"],
                "password": test_user["password"]
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_invalid_password(self, client, test_user):
        """测试密码错误"""
        client.post("/api/v1/auth/register", json=test_user)

        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": test_user["email"],
                "password": "wrongpassword"
            }
        )
        assert response.status_code == 401
        assert "error" in response.json()


class TestDocumentAPI:
    """文档API测试"""

    @pytest.fixture
    def auth_client(self, client, test_user):
        """已认证的客户端"""
        # 注册并登录
        client.post("/api/v1/auth/register", json=test_user)
        login_resp = client.post(
            "/api/v1/auth/login",
            json={
                "email": test_user["email"],
                "password": test_user["password"]
            }
        )
        token = login_resp.json()["access_token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        return client

    def test_upload_document(self, auth_client, temp_test_file):
        """测试上传文档"""
        with open(temp_test_file, "rb") as f:
            response = auth_client.post(
                "/api/v1/documents/upload",
                files={"file": ("test.txt", f, "text/plain")}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["filename"] == "test.txt"
        assert data["status"] == "processing"
        assert "id" in data

    def test_get_documents(self, auth_client):
        """测试获取文档列表"""
        response = auth_client.get("/api/v1/documents")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "total" in data

    def test_get_document_detail(self, auth_client, temp_test_file):
        """测试获取文档详情"""
        # 先上传
        with open(temp_test_file, "rb") as f:
            upload_resp = auth_client.post(
                "/api/v1/documents/upload",
                files={"file": ("test.txt", f, "text/plain")}
            )
        doc_id = upload_resp.json()["id"]

        # 获取详情
        response = auth_client.get(f"/api/v1/documents/{doc_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == doc_id
        assert data["filename"] == "test.txt"

    def test_delete_document(self, auth_client, temp_test_file):
        """测试删除文档"""
        # 上传
        with open(temp_test_file, "rb") as f:
            upload_resp = auth_client.post(
                "/api/v1/documents/upload",
                files={"file": ("test.txt", f, "text/plain")}
            )
        doc_id = upload_resp.json()["id"]

        # 删除
        response = auth_client.delete(f"/api/v1/documents/{doc_id}")
        assert response.status_code == 200
        assert response.json()["success"] is True


class TestChatAPI:
    """聊天API测试"""

    @pytest.fixture
    def auth_client(self, client, test_user):
        """已认证的客户端"""
        client.post("/api/v1/auth/register", json=test_user)
        login_resp = client.post(
            "/api/v1/auth/login",
            json={
                "email": test_user["email"],
                "password": test_user["password"]
            }
        )
        token = login_resp.json()["access_token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        return client

    @patch("backend.services.rag_service.RAGService.query")  # 根据实际服务调整
    def test_chat_message(self, mock_query, auth_client):
        """测试发送聊天消息"""
        mock_query.return_value = {
            "answer": "这是模拟的回答",
            "sources": [{"content": "来源内容", "metadata": {}}]
        }

        response = auth_client.post(
            "/api/v1/chat/messages",
            json={
                "conversation_id": None,
                "content": "你好",
                "document_ids": []
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert "reply" in data
        assert data["reply"]["content"] == "这是模拟的回答"

    def test_get_conversations(self, auth_client):
        """测试获取对话列表"""
        response = auth_client.get("/api/v1/chat/conversations")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


class TestHealthAPI:
    """健康检查API测试"""

    def test_health_check(self, client):
        """测试健康检查"""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data
        assert "timestamp" in data