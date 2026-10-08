"""
集成测试：API端点测试（认证 / 健康检查）

6A-6 说明（2026-10-08）：
  * 原 `TestDocumentAPI`（4 例，`/api/v1/documents*`）与 `TestChatAPI`（2 例，`/api/v1/chat/*`）
    已被产品正式认定为**废弃 API**（当前只有 `/api/v1/rag/*`、`/api/v1/characters/*`
    与知识库 UI），故一并移除；不在本文件重建旧路由的兼容层。
  * 保留的 `TestAuthAPI` / `TestHealthAPI` 走的是**当前仍在服务**的端点，
    其断言已按**当前真实响应形态**校正：
      - `/api/v1/auth/*` 走统一响应信封 `{success, message, code, data}`；
      - `/api/v1/auth/login` 的字段是 `username`（用户名或邮箱），不是 `email`；
      - `/api/v1/auth/register` 只返回用户信息（令牌由 `/login` 签发）；
      - `/health` 是**未封装**的平面 dict，键为 `versions`（非 `version`）。
"""
import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.database.base import get_db


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
    """认证API测试（当前 `/api/v1/auth/*`）"""

    def test_register(self, client):
        """测试用户注册：返回统一信封，data 为用户信息（令牌由 /login 签发）"""
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
        body = response.json()
        assert body.get("success") is True
        assert "data" in body                      # 统一响应信封
        data = body["data"]
        assert "id" in data
        assert data["email"] == "test@example.com"
        assert data["username"] == "testuser"

    def test_login(self, client, test_user):
        """测试用户登录：字段为 username（用户名或邮箱），令牌在信封的 data 内"""
        # 先注册
        client.post("/api/v1/auth/register", json=test_user)

        # 登录
        response = client.post(
            "/api/v1/auth/login",
            json={
                "username": test_user["username"],
                "password": test_user["password"]
            }
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_invalid_password(self, client, test_user):
        """测试密码错误：401"""
        client.post("/api/v1/auth/register", json=test_user)

        response = client.post(
            "/api/v1/auth/login",
            json={
                "username": test_user["username"],
                "password": "wrongpassword"
            }
        )
        assert response.status_code == 401


class TestHealthAPI:
    """健康检查API测试（当前 `/health`，平面 dict，不走信封）"""

    def test_health_check(self, client):
        """测试健康检查"""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["versions"]                    # 当前键名为 versions
        assert "timestamp" in data
