# -*- coding: utf-8 -*-
"""Phase 1D：对话内连续分页所需的「上一次结构化查询上下文」

设计要点
--------
1. 只保存**查询参数**，绝不保存数据行；真实行永远来自 representation.json。
2. 上下文严格绑定 (user_id, session_key)：
   - 不同用户即使 session_key 相同也不会串数据（key 里含 user_id）；
   - 同一用户的不同会话（不同 character / 不同 session_id）互不影响。
3. 记录「最近一轮对话的类型」：只有当最近一轮确实是 Excel 结构化查询时，
   才允许后续「下一页 / 再来20条」这类**纯分页指令**复用该上下文；
   否则必须明确提示"当前没有可继续的表格查询"，不猜、不串。
4. 存储采用**进程内字典 + 锁 + TTL + 容量上限**，与项目现有
   cache_service / 登录限流保持一致的实现风格，不新增数据库表。
   （注意：与项目既有能力一致，不支持多 worker。）
"""

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 最近一轮对话的类型
TURN_EXCEL = 'excel'
TURN_OTHER = 'other'

# 上下文有效期（秒）与最大保留数量
DEFAULT_TTL_SECONDS = 2 * 60 * 60
MAX_CONTEXTS = 500


@dataclass
class ExcelQueryContext:
    """上一次成功的结构化查询参数（不含任何数据行）。"""

    user_id: int
    session_key: str
    document_id: str
    filename: str
    sheet_index: int
    sheet_name: str
    columns: List[str] = field(default_factory=list)      # 已解析的真实列名；[] 表示全部列
    filters: List[Dict[str, Any]] = field(default_factory=list)
    limit: int = 20
    offset: int = 0
    total_matches: int = 0
    total_rows_in_sheet: int = 0
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ExcelQueryContextStore:
    """进程内上下文存储（线程安全 + TTL + 容量上限）。"""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS, max_contexts: int = MAX_CONTEXTS):
        self._ttl = ttl_seconds
        self._max = max_contexts
        self._lock = threading.Lock()
        self._contexts: Dict[str, ExcelQueryContext] = {}
        self._last_turn: Dict[str, str] = {}

    # ---- 内部 ----
    @staticmethod
    def _key(user_id: Optional[int], session_key: Optional[str]) -> str:
        return f'{user_id if user_id is not None else "anon"}::{session_key or "default"}'

    def _purge_locked(self) -> None:
        now = time.time()
        expired = [k for k, c in self._contexts.items() if now - c.updated_at > self._ttl]
        for k in expired:
            self._contexts.pop(k, None)
            self._last_turn.pop(k, None)
        # 容量上限：淘汰最旧的
        while len(self._contexts) > self._max:
            oldest = min(self._contexts.items(), key=lambda kv: kv[1].updated_at)[0]
            self._contexts.pop(oldest, None)
            self._last_turn.pop(oldest, None)

    def _get_locked(self, key: str) -> Optional[ExcelQueryContext]:
        ctx = self._contexts.get(key)
        if ctx is None:
            return None
        if time.time() - ctx.updated_at > self._ttl:
            self._contexts.pop(key, None)
            self._last_turn.pop(key, None)
            return None
        return ctx

    # ---- 对外 API ----
    def save_excel(self, ctx: ExcelQueryContext) -> None:
        """保存/更新一次成功的 Excel 结构化查询上下文，并把最近一轮标记为 excel。"""
        ctx.updated_at = time.time()
        key = self._key(ctx.user_id, ctx.session_key)
        with self._lock:
            self._contexts[key] = ctx
            self._last_turn[key] = TURN_EXCEL
            self._purge_locked()

    def mark_other_turn(self, user_id: Optional[int], session_key: Optional[str]) -> None:
        """标记最近一轮不是 Excel 结构化查询（用于阻断错误的上下文复用）。

        注意：只更新「最近一轮类型」，**不删除**上下文本身，
        这样用户重新发起一次表格查询后仍能继续以新查询为基准翻页。
        """
        key = self._key(user_id, session_key)
        with self._lock:
            self._last_turn[key] = TURN_OTHER

    def get_for_pagination(self, user_id: Optional[int], session_key: Optional[str]) -> Optional[ExcelQueryContext]:
        """取出可用于「纯分页指令」的上下文。

        仅当最近一轮确实是 Excel 结构化查询时才返回；否则返回 None。
        """
        key = self._key(user_id, session_key)
        with self._lock:
            if self._last_turn.get(key) != TURN_EXCEL:
                return None
            return self._get_locked(key)

    def get_last_turn(self, user_id: Optional[int], session_key: Optional[str]) -> Optional[str]:
        key = self._key(user_id, session_key)
        with self._lock:
            return self._last_turn.get(key)

    def clear(self, user_id: Optional[int], session_key: Optional[str]) -> None:
        """清除某会话的上下文（清空对话时调用）。"""
        key = self._key(user_id, session_key)
        with self._lock:
            self._contexts.pop(key, None)
            self._last_turn.pop(key, None)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            self._purge_locked()
            return {'contexts': len(self._contexts)}


_store: Optional[ExcelQueryContextStore] = None
_store_lock = threading.Lock()


def get_context_store() -> ExcelQueryContextStore:
    """获取全局上下文存储单例。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ExcelQueryContextStore()
    return _store


def reset_context_store() -> None:
    """测试用：重置全局单例。"""
    global _store
    with _store_lock:
        _store = ExcelQueryContextStore()


# ============================================================================
# Phase 3A：统计上下文（与分页上下文**完全隔离**，避免相互污染）
# ============================================================================
TURN_AGGREGATE = 'aggregate'


@dataclass
class AggregateContext:
    """上一次成功的统计请求参数（不含数据行），用于"这些订单..."式追问。"""

    user_id: int
    session_key: str
    document_id: str
    filename: str
    sheet_index: int
    sheet_name: str
    operation: str
    column: Optional[str] = None
    filters: List[Dict[str, Any]] = field(default_factory=list)
    matched_rows: int = 0
    value: Optional[float] = None
    # Phase 3B：上一轮的分组字段（空 = 单值统计）
    group_by: List[str] = field(default_factory=list)
    # Phase 3C：上一轮的排序与 TOP-N（用于「再看前10个」式追问；空 = 未排序）
    order_by: Optional[str] = None
    order_dir: Optional[str] = None
    top_n: Optional[int] = None
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AggregateContextStore:
    """统计上下文存储（与 ExcelQueryContextStore 同款策略：TTL + 上限 + 线程安全）。"""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS, max_contexts: int = MAX_CONTEXTS):
        self._ttl = ttl_seconds
        self._max = max_contexts
        self._lock = threading.Lock()
        self._contexts: Dict[str, AggregateContext] = {}
        self._last_turn: Dict[str, str] = {}

    @staticmethod
    def _key(user_id: Optional[int], session_key: Optional[str]) -> str:
        return f'{user_id if user_id is not None else "anon"}::{session_key or "default"}'

    def save_aggregate(self, ctx: AggregateContext) -> None:
        """保存一次成功的统计上下文，并把最近一轮标记为统计。"""
        ctx.updated_at = time.time()
        key = self._key(ctx.user_id, ctx.session_key)
        with self._lock:
            self._contexts[key] = ctx
            self._last_turn[key] = TURN_AGGREGATE
            now = time.time()
            expired = [k for k, c in self._contexts.items() if now - c.updated_at > self._ttl]
            for k in expired:
                self._contexts.pop(k, None)
                self._last_turn.pop(k, None)
            while len(self._contexts) > self._max:
                oldest = min(self._contexts.items(), key=lambda kv: kv[1].updated_at)[0]
                self._contexts.pop(oldest, None)
                self._last_turn.pop(oldest, None)

    def mark_other_turn(self, user_id: Optional[int], session_key: Optional[str]) -> None:
        """标记最近一轮不是统计（阻断"这些订单"错误继承）。"""
        key = self._key(user_id, session_key)
        with self._lock:
            self._last_turn[key] = TURN_OTHER

    def get_for_inheritance(
        self, user_id: Optional[int], session_key: Optional[str]
    ) -> Optional[AggregateContext]:
        """取出可用于「这些订单…」继承的统计上下文；仅当最近一轮确实是统计时返回。"""
        key = self._key(user_id, session_key)
        with self._lock:
            if self._last_turn.get(key) != TURN_AGGREGATE:
                return None
            ctx = self._contexts.get(key)
            if ctx is None:
                return None
            if time.time() - ctx.updated_at > self._ttl:
                self._contexts.pop(key, None)
                self._last_turn.pop(key, None)
                return None
            return ctx

    def clear(self, user_id: Optional[int], session_key: Optional[str]) -> None:
        key = self._key(user_id, session_key)
        with self._lock:
            self._contexts.pop(key, None)
            self._last_turn.pop(key, None)


_agg_store: Optional[AggregateContextStore] = None
_agg_store_lock = threading.Lock()


def get_aggregate_store() -> AggregateContextStore:
    global _agg_store
    if _agg_store is None:
        with _agg_store_lock:
            if _agg_store is None:
                _agg_store = AggregateContextStore()
    return _agg_store


def reset_aggregate_store() -> None:
    """测试用：重置统计上下文单例。"""
    global _agg_store
    with _agg_store_lock:
        _agg_store = AggregateContextStore()


# ============================================================================
# Phase 4A：多步分析上下文（与分页 / 单值统计上下文**三方隔离**）
# ============================================================================
TURN_ANALYSIS = 'analysis'


@dataclass
class AnalysisContext:
    """上一次成功的两阶段分析（只存**计划参数**，不存数据行）。

    用途：支持「再算一下这 10 个 SKU 的平均销售额」这类追问 ——
    复用上一轮的第 1 步（分组 + 排序 + TOP-N），只替换第 2 步的聚合方式；
    执行时会**重新跑一次第 1 步**（同一 SQL，结果确定），因此不缓存任何数据。
    """

    user_id: int
    session_key: str
    document_id: str
    filename: str
    sheet_index: int
    sheet_name: str
    plan: Dict[str, Any] = field(default_factory=dict)       # {'steps': [...已解析真实列名...]}
    step2_operation: str = ''
    step2_value: Optional[float] = None
    step2_input_rows: int = 0
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AnalysisContextStore:
    """多步分析上下文存储（同款策略：TTL + 上限 + 线程安全）。"""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS, max_contexts: int = MAX_CONTEXTS):
        self._ttl = ttl_seconds
        self._max = max_contexts
        self._lock = threading.Lock()
        self._contexts: Dict[str, AnalysisContext] = {}
        self._last_turn: Dict[str, str] = {}

    @staticmethod
    def _key(user_id: Optional[int], session_key: Optional[str]) -> str:
        return f'{user_id if user_id is not None else "anon"}::{session_key or "default"}'

    def save_analysis(self, ctx: AnalysisContext) -> None:
        ctx.updated_at = time.time()
        key = self._key(ctx.user_id, ctx.session_key)
        with self._lock:
            self._contexts[key] = ctx
            self._last_turn[key] = TURN_ANALYSIS
            now = time.time()
            for k in [k for k, c in self._contexts.items() if now - c.updated_at > self._ttl]:
                self._contexts.pop(k, None)
                self._last_turn.pop(k, None)
            while len(self._contexts) > self._max:
                oldest = min(self._contexts.items(), key=lambda kv: kv[1].updated_at)[0]
                self._contexts.pop(oldest, None)
                self._last_turn.pop(oldest, None)

    def mark_other_turn(self, user_id: Optional[int], session_key: Optional[str]) -> None:
        """标记最近一轮不是多步分析（阻断错误的第 1 步继承）。"""
        key = self._key(user_id, session_key)
        with self._lock:
            self._last_turn[key] = TURN_OTHER

    def get_for_inheritance(
        self, user_id: Optional[int], session_key: Optional[str]
    ) -> Optional[AnalysisContext]:
        """取出可用于追问继承的分析上下文；仅当最近一轮确实是多步分析时返回。"""
        key = self._key(user_id, session_key)
        with self._lock:
            if self._last_turn.get(key) != TURN_ANALYSIS:
                return None
            ctx = self._contexts.get(key)
            if ctx is None:
                return None
            if time.time() - ctx.updated_at > self._ttl:
                self._contexts.pop(key, None)
                self._last_turn.pop(key, None)
                return None
            return ctx

    def clear(self, user_id: Optional[int], session_key: Optional[str]) -> None:
        key = self._key(user_id, session_key)
        with self._lock:
            self._contexts.pop(key, None)
            self._last_turn.pop(key, None)


_analysis_store: Optional[AnalysisContextStore] = None
_analysis_store_lock = threading.Lock()


def get_analysis_store() -> AnalysisContextStore:
    global _analysis_store
    if _analysis_store is None:
        with _analysis_store_lock:
            if _analysis_store is None:
                _analysis_store = AnalysisContextStore()
    return _analysis_store


def reset_analysis_store() -> None:
    """测试用：重置多步分析上下文单例。"""
    global _analysis_store
    with _analysis_store_lock:
        _analysis_store = AnalysisContextStore()
