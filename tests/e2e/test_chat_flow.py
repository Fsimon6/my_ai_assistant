"""
端到端测试：完整聊天流程测试
"""
import pytest
import time
from fastapi.testclient import TestClient
from backend.main import app
from backend.database.base import get_db


@pytest.fixture
def client(db_session):
    """测试客户端"""
    def override_get_db():
        try:
            yield get_db()
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


@pytest.fixture
def authenticated_client(client, test_user):
    """已认证的客户端"""
    # 注册
    client.post("/api/v1/auth/register", json=test_user)

    # 登录
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


class TestChatE2E:
    """端到端聊天流程测试"""

    def test_complete_chat_flow(self, authenticated_client, temp_test_file):
        """测试完整聊天流程：上传文档 → 创建对话 → 发送消息 → 获取历史"""

        # 1. 上传文档
        with open(temp_test_file, "r", encoding="utf-8") as f:
            upload_resp = authenticated_client.post(
                "/api/v1/documents/upload",
                files={"file": ("test.txt", f, "test/plain")}
            )
        assert upload_resp.status_code == 200
        doc_id = upload_resp.json()["id"]

        # 等待文档处理完成（实际可能需要轮询）
        time.sleep(2)

        # 2. 获取文档列表
        list_resp = authenticated_client.get("/api/v1/documents")
        assert list_resp.status_code == 200
        docs = list_resp.json()["items"]
        assert len(docs) >= 1

        # 3. 创建对话（发送第一条消息）
        chat_resp = authenticated_client.post(
            "/api/v1/chat/messages",
            json={
                "conversation_id": None,  # 新对话
                "content": "文档中说了什么？",
                "document_ids": [doc_id]
            }
        )
        assert chat_resp.status_code == 200
        chat_data = chat_resp.json()
        assert "conversation_id" in chat_data
        conversation_id = chat_data["conversation_id"]
        assert chat_data["reply"]["content"] is not None

        # 4. 继续对话
        follow_resp = authenticated_client.post(
            "/api/v1/chat/messages",
            json={
                "conversation_id": conversation_id,
                "content": "能详细说说吗？",
                "document_ids": [doc_id]
            }
        )
        assert follow_resp.status_code == 200
        follow_data = follow_resp.json()
        assert follow_data["conversation_id"] == conversation_id

        # 5. 获取对话历史
        history_resp = authenticated_client.get(
            f"/api/v1/chat/conversations/{conversation_id}/messages"
        )
        assert history_resp.status_code == 200
        history = history_resp.json()
        assert "messages" in history
        assert len(history["messages"]) >= 2    # 至少两条消息（用户+助手）

        # 6. 获取所有对话列表
        conv_list_resp = authenticated_client.get("/api/v1/chat/conversations")
        assert conv_list_resp.status_code == 200
        conv_list = conv_list_resp.json()
        assert len(conv_list) >= 1
        assert any(c["id"] == conversation_id for c in conv_list)

        # 7. 删除文档
        del_resp = authenticated_client.delete(f"/api/v1/documents/{doc_id}")
        assert del_resp.status_code == 200

    def test_streaming_chat(self, authenticated_client):
        """测试流式聊天"""
        response = authenticated_client.post(
            "/api/v1/chat/messages",
            json={
                "conversation_id": None,
                "content": "讲个笑话",
                "document_ids": [],
                "stream": True,
            }
        )

        # 流式响应应该返回 text/event-stream
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream; charset=utf-8"

        # 解析流式数据
        content = ""
        for line in response.iter_lines():
            if line and line.startswith("data: "):
                data = line[6:]   # 去掉"data: "
                if data == "[DONE]":
                    break
                import json
                chunk = json.loads(data)
                if "content" in chunk:
                    content += chunk["content"]

        assert len(content) > 0