# -*- coding: utf-8 -*-
"""
Stage 32 Docker 持久化验证器（API 驱动，同源 /api）。

用法（在 CentOS 容器内服务启动后，于能访问 Nginx 的机器上执行）：
  E2E_BASE_URL=http://<centos_ip>:80 python tests/verify_docker_persistence.py setup
  # 然后：docker compose restart   （或 docker compose down && docker compose up -d）
  E2E_BASE_URL=http://<centos_ip>:80 python tests/verify_docker_persistence.py verify

setup：注册 e2e32_ 用户、创建角色、发起一次流式对话（落库 Conversation）、
       上传含标记 VECTOR-RUNTIME-E2E32 的 RAG 文档、查询确认命中。
       状态（ids）写入 ./e2e32_state.json（仅本地，非项目文件）。

verify：重新登录，确认 角色 仍存在、RAG 查询仍能命中标记。
        用于证明 SQLite + Chroma 在容器重启后仍持久化。

数据前缀：e2e32_
"""
import os
import sys
import time
import json
import requests

BASE = (os.environ.get("E2E_BASE_URL") or "http://localhost:80").rstrip("/")
PREFIX = "e2e32_"
RUN_ID = time.strftime("%Y%m%d_%H%M%S")
USER = PREFIX + "persist_" + RUN_ID
EMAIL = USER + "@example.com"
PASS = "E2ePass123"
CHAR_NAME = PREFIX + "persist角色" + RUN_ID[-6:]
CHAR_PROMPT = "你是一个测试助手，请简洁回答。"
TEST_ID = "VECTOR-RUNTIME-E2E32"
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "e2e32_state.json")


def api(path, **kw):
    return requests.request(url=BASE + "/api/v1" + path, timeout=30, **kw)


def register_login():
    r = api("/auth/register", json={"username": USER, "email": EMAIL, "password": PASS})
    if r.status_code >= 400 and "already" not in r.text.lower():
        # 已存在则直接登录
        pass
    r = api("/auth/login", json={"username": USER, "password": PASS})
    assert r.status_code == 200, f"login fail {r.status_code} {r.text[:200]}"
    return r.json()["access_token"]


def setup():
    tok = register_login()
    h = {"Authorization": f"Bearer {tok}"}
    # 创建角色
    r = api("/characters", json={"name": CHAR_NAME, "system_prompt": CHAR_PROMPT},
            headers=h)
    char_id = r.json().get("id") or r.json().get("data", {}).get("id")
    # 流式对话（落库 conversation）
    resp = api(f"/characters/{char_id}/speak/stream",
               json={"message": f"记住测试编号 {TEST_ID}", "stream": True},
               headers=h, stream=True)
    full = ""
    for line in resp.iter_lines():
        if line:
            try:
                full += json.loads(line).get("content", "")
            except Exception:
                pass
    # 上传 RAG 文档
    files = {"file": (PREFIX + "doc.txt", "VECTOR-RUNTIME-E2E32 marker content")}
    r = api("/rag/upload", files=files, headers=h)
    doc_ok = r.status_code == 200
    # 查询确认命中
    r = api("/rag/query", json={"query": TEST_ID, "stream": False}, headers=h)
    hit = TEST_ID in r.text
    state = {"char_id": char_id, "user": USER, "marker": TEST_ID}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    print("SETUP char_id=", char_id, "doc_upload=", doc_ok, "rag_hit=", hit)
    print("下一步：docker compose restart（或 down+up），再运行 verify")


def verify():
    if not os.path.exists(STATE_FILE):
        print("NO_STATE: 请先运行 setup")
        sys.exit(2)
    state = json.load(open(STATE_FILE))
    tok = register_login()
    h = {"Authorization": f"Bearer {tok}"}
    # 角色仍存在
    r = api("/characters", headers=h)
    chars = r.json().get("data", r.json())
    char_ok = any(state["char_id"] and str(state["char_id"]) in str(c.get("id", ""))
                  for c in (chars if isinstance(chars, list) else []))
    # RAG 查询仍能命中
    r = api("/rag/query", json={"query": state["marker"], "stream": False}, headers=h)
    hit = state["marker"] in r.text
    print("VERIFY char_exists=", char_ok, "rag_hit=", hit)
    sys.exit(0 if (char_ok and hit) else 1)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "setup"
    if mode == "setup":
        setup()
    elif mode == "verify":
        verify()
    else:
        print("usage: verify_docker_persistence.py [setup|verify]")
        sys.exit(2)
