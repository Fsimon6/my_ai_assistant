# -*- coding: utf-8 -*-
"""极简内存级登录频率限制（生产加固，第二阶段）。

设计边界（务必清楚）：
- 仅单进程（单 uvicorn worker）内有效；多 worker / 多机部署需改用 Redis 或反向代理层限流。
- 仅统计“登录失败”次数；正确登录永远放行并清零，不会误锁正常用户。
"""
import time
from collections import defaultdict


# key: "ip:username" -> 最近失败时间戳列表
_FAILED = defaultdict(list)

WINDOW_SECONDS = 5 * 60  # 5 分钟滑动窗口
MAX_FAILURES = 5         # 窗口内失败达 5 次后，后续失败请求返回 429


def _now() -> float:
    return time.time()


def prune(key: str) -> None:
    now = _now()
    _FAILED[key] = [t for t in _FAILED[key] if now - t < WINDOW_SECONDS]


def register_login_failure(key: str) -> None:
    _FAILED[key].append(_now())


def register_login_success(key: str) -> None:
    # 成功后清零该键的失败记录
    if key in _FAILED:
        _FAILED[key].clear()


def is_blocked(key: str) -> bool:
    """失败次数已达上限返回 True（用于失败路径返回 429）。"""
    prune(key)
    return len(_FAILED[key]) >= MAX_FAILURES
