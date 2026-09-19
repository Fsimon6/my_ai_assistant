# -*- coding: utf-8 -*-
"""
Stage 32 Docker E2E 数据清理器（API 驱动，同源 /api）。

仅清理 e2e32_ 前缀的测试数据：
  - 删除 e2e32_ 角色（DELETE /api/v1/characters/{id}）
  - 删除 e2e32_ RAG 文档（DELETE /api/v1/rag/documents，按 document_id 精确删除）
  - 测试用户本身若后端提供删除接口则删除，否则保留（不删其他用户/数据）

用法：
  E2E_BASE_URL=http://<centos_ip>:80 python tests/cleanup_docker_e2e.py

严禁 docker compose down -v / 删 volume。本脚本不删除任何 volume 或真实数据。
数据前缀：e2e32_
"""
import os
import sys
import json
import requests

BASE = (os.environ.get("E2E_BASE_URL") or "http://localhost:80").rstrip("/")
PREFIX = "e2e32_"


def api(path, **kw):
    return requests.request(url=BASE + "/api/v1" + path, timeout=30, **kw)


def login(user, password):
    r = api("/auth/login", json={"username": user, "password": password})
    assert r.status_code == 200, f"login {user} fail {r.status_code}"
    return r.json()["access_token"]


def main():
    # 需要已知测试用户凭据；从 e2e32_state.json 读取（若存在）
    user = None
    pw = "E2ePass123"
    sf = os.path.join(os.path.dirname(__file__), "..", "e2e32_state.json")
    if os.path.exists(sf):
        user = json.load(open(sf)).get("user")
    if not user:
        print("未找到 e2e32_ 测试用户；请在运行 e2e/verify 后执行本清理，"
              "或手动指定用户名。")
        sys.exit(2)
    tok = login(user, pw)
    h = {"Authorization": f"Bearer {tok}"}

    # 删除 e2e32_ 角色
    r = api("/characters", headers=h)
    chars = r.json().get("data", r.json())
    deleted_chars = 0
    for c in (chars if isinstance(chars, list) else []):
        if str(c.get("name", "")).startswith(PREFIX):
            cid = c.get("id")
            dr = api(f"/characters/{cid}", method="DELETE", headers=h)
            if dr.status_code in (200, 204):
                deleted_chars += 1

    # 删除 e2e32_ RAG 文档（先取集合信息定位 document_id）
    del_docs = []
    try:
        r = api("/rag/collection-info", headers=h)
        info = r.json()
        docs = info.get("documents") or info.get("data", {}).get("documents") or []
        for d in docs:
            if str(d.get("filename", "")).startswith(PREFIX) or \
               str(d.get("document_id", "")).startswith(PREFIX):
                del_docs.append(d["document_id"])
    except Exception as e:
        print("collection-info 读取失败（可能无该接口）：", str(e)[:120])
    if del_docs:
        r = api("/rag/documents", method="DELETE",
                json={"document_ids": del_docs}, headers=h)
        print("RAG docs deleted:", r.status_code, del_docs)

    print(f"CLEANUP done: chars_deleted={deleted_chars}, rag_docs_deleted={len(del_docs)}")
    # 删除本地状态文件（仅测试态，不影响项目/volume）
    if os.path.exists(sf):
        try:
            os.remove(sf)
        except Exception:
            pass


if __name__ == "__main__":
    main()
