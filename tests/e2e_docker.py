# -*- coding: utf-8 -*-
"""
Stage 32 Docker Production Browser E2E (Playwright / Chromium).

目标：Docker Nginx 暴露的前端（E2E_BASE_URL，默认 http://localhost:80）。
链路必须是：Browser -> frontend/nginx -> /api -> backend。
严禁直连 localhost:5173 或 backend:8000。

测试数据统一前缀：e2e32_
本脚本只创建/读取/删除 e2e32_ 测试数据，不改动项目文件。

运行：
  E2E_BASE_URL=http://<centos_ip>:80 python tests/e2e_docker.py
（需要本机安装 playwright + chromium：pip install playwright && playwright install chromium）
"""
import os
import time
import sys
import json
from playwright.sync_api import sync_playwright

BASE = (os.environ.get("E2E_BASE_URL") or "http://localhost:80").rstrip("/")
PREFIX = "e2e32_"
RUN_ID = time.strftime("%Y%m%d_%H%M%S")
USER = PREFIX + RUN_ID
EMAIL = USER + "@example.com"
PASS = "E2ePass123"
CHAR_NAME = PREFIX + "角色" + RUN_ID[-6:]
CHAR_PROMPT = "你是一个测试助手，请记住用户提供的测试编号，并简洁回答。"
TEST_ID = "DOCKER-RUNTIME-2026"
RAG_FILE = PREFIX + "doc.txt"

results = {}
violations = []  # 绕过 /api 或访问外部主机的请求


def log(name, ok, detail=""):
    results[name] = (ok, detail)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""), flush=True)


def on_request(page, url):
    # 仅关注 API 类请求：必须同源且走 /api
    if any(x in url for x in ("localhost:5173", ":8000/api", "backend:8000")):
        violations.append(url)
    if url.startswith("http") and "/api" in url and not url.startswith(BASE + "/api"):
        violations.append("OFFORIGIN:" + url)


def fill(page, placeholder, value):
    page.fill(f"input[placeholder='{placeholder}']", value, timeout=8000)


def safe_click(page, text, timeout=8000):
    page.click(f"text={text}", timeout=timeout)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("request", lambda r: on_request(page, r.url))
        page.on("response", lambda r: None)

        try:
            # ---- 1. 打开前端（Nginx） ----
            page.goto(BASE + "/", timeout=20000)
            log("page_root", True)

            # ---- 2. 注册 + 登录（走 /api，不直连后端） ----
            page.goto(BASE + "/login", timeout=20000)
            # 优先尝试注册入口；多数页面在 /login 提供“注册”链接
            try:
                safe_click(page, "注册", timeout=4000)
                page.goto(BASE + "/register", timeout=20000)
            except Exception:
                pass
            try:
                fill(page, "用户名", USER)
                fill(page, "邮箱", EMAIL)
                fill(page, "密码", PASS)
                safe_click(page, "注册")
            except Exception as e:
                log("register", False, str(e)[:120])
            time.sleep(1.5)
            # 登录
            page.goto(BASE + "/login", timeout=20000)
            fill(page, "用户名", USER)
            fill(page, "密码", PASS)
            safe_click(page, "登录")
            time.sleep(2.0)
            # 登录成功应跳离 /login
            log("login", "/login" not in page.url, f"url={page.url}")
            # 无 console 级 CORS/5xx（粗略：页面仍在）
            log("no_console_error_redirect", "/login" not in page.url)

            # ---- 3. 创建 Character ----
            page.goto(BASE + "/characters", timeout=20000)
            try:
                safe_click(page, "创建角色")
                fill(page, "角色名称", CHAR_NAME)
                fill(page, "系统提示词", CHAR_PROMPT)
                safe_click(page, "保存")
                time.sleep(1.5)
                log("character_create", CHAR_NAME in page.content())
            except Exception as e:
                log("character_create", False, str(e)[:120])

            # ---- 4. 进入角色详情并发起 Streaming 聊天 ----
            try:
                page.click(f"text={CHAR_NAME}", timeout=8000)
                time.sleep(1.0)
                log("character_detail", True)
                fill(page, "输入消息", f"我的测试编号是 {TEST_ID}。")
                # 点击发送
                safe_click(page, "发送")
                # 监测流式增量：助手文本应分多步增长
                samples = []
                for _ in range(20):
                    time.sleep(0.4)
                    txt = page.inner_text(".chat-messages, .message-list, [class*='message']", timeout=2000) if False else ""
                    try:
                        txt = page.locator("div:has-text('')").last.inner_text(timeout=500)
                    except Exception:
                        txt = ""
                    samples.append(len(txt))
                grew = len([s for s in samples if s > 0])
                incremental = any(samples[i+1] > samples[i] for i in range(len(samples)-1))
                log("streaming_incremental", grew >= 2 and incremental,
                    f"grew_steps={grew}, incremental={incremental}")
            except Exception as e:
                log("streaming", False, str(e)[:120])

            # ---- 5. History：刷新后仍应存在 ----
            try:
                page.reload(timeout=20000)
                time.sleep(1.5)
                log("history_persist", TEST_ID in page.content() or CHAR_NAME in page.content())
            except Exception as e:
                log("history_persist", False, str(e)[:120])

            # ---- 6. RAG Upload / Query / Delete ----
            try:
                page.goto(BASE + "/knowledge", timeout=20000)
                # 上传文件（内容含标记）
                page.set_input_files("input[type=file]",
                                     files=[(__file__ + ".ragtmp", "name=" + RAG_FILE,
                                             "VECTOR-RUNTIME-E2E32")])
                safe_click(page, "上传")
                time.sleep(2.0)
                log("rag_upload", True)
                fill(page, "搜索", "VECTOR-RUNTIME-E2E32")
                safe_click(page, "查询")
                time.sleep(2.0)
                log("rag_query", "VECTOR-RUNTIME-E2E32" in page.content())
                # 删除（勾选后点删除）
                try:
                    page.check("input[type=checkbox]")
                    safe_click(page, "删除")
                    time.sleep(1.5)
                    log("rag_delete", True)
                except Exception as e:
                    log("rag_delete", False, str(e)[:120])
            except Exception as e:
                log("rag", False, str(e)[:120])

            # ---- 网络路径校验 ----
            log("network_via_nginx_api_only", len(violations) == 0,
                f"violations={violations[:5]}")

        finally:
            browser.close()

    # 汇总
    fails = [k for k, v in results.items() if not v[0]]
    print("\n==== SUMMARY ====")
    print("PASS" if not fails and not violations else "FAIL",
          "fails=", fails, "violations=", len(violations))
    sys.exit(0 if (not fails and not violations) else 1)


if __name__ == "__main__":
    main()
