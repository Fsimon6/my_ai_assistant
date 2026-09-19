# -*- coding: utf-8 -*-
"""Phase 2：结构化查询引擎分派层

统一入口：

    payload(dict) -> 校验 -> [DuckDB 引擎 | Python 参考引擎] -> StructuredQueryResult

- 默认 ``auto``：优先 DuckDB；仅当 **DuckDB 不可用**（未安装/连接失败/建表失败）
  时回退到 Phase 1B 的 Python 引擎，并记录 warning。
- 显式 ``duckdb``：强制 DuckDB，不可用则抛错（便于验证与排障）。
- 显式 ``python``：强制 Python 参考引擎（用于差分对比与降级）。

注意：不会因为"SQL 执行报错"而静默回退 —— 那属于 bug，必须暴露出来。
"""

import logging
from typing import Any, Dict, Optional, Tuple

from backend.excel import query as excel_query
from backend.excel.representation import WorkbookRepresentation

logger = logging.getLogger(__name__)

ENGINE_AUTO = 'auto'
ENGINE_DUCKDB = 'duckdb'
ENGINE_PYTHON = 'python'
SUPPORTED_ENGINES = (ENGINE_AUTO, ENGINE_DUCKDB, ENGINE_PYTHON)


def normalize_engine(engine: Optional[str]) -> str:
    value = (engine or ENGINE_AUTO)
    if not isinstance(value, str):
        raise excel_query.ExcelQueryError(excel_query.ERR_INVALID_PARAM, 'engine 必须是字符串')
    value = value.strip().lower()
    if value not in SUPPORTED_ENGINES:
        raise excel_query.ExcelQueryError(
            excel_query.ERR_INVALID_PARAM,
            f'不支持的 engine={value!r}，可选 {list(SUPPORTED_ENGINES)}',
        )
    return value


def run_structured_query(
    representation: WorkbookRepresentation,
    payload: Dict[str, Any],
    engine: Optional[str] = ENGINE_AUTO,
) -> Tuple['excel_query.StructuredQueryResult', str]:
    """执行结构化查询，返回 (结果, 实际使用的引擎名)。"""
    engine = normalize_engine(engine)

    request = excel_query.parse_request(
        payload,
        document_id=representation.document_id,
        user_id=representation.user_id,
    )

    if engine in (ENGINE_AUTO, ENGINE_DUCKDB):
        try:
            from backend.excel import duck as duck_engine

            if not duck_engine.DUCKDB_AVAILABLE:
                raise duck_engine.DuckEngineUnavailable('未安装 duckdb')
            result = duck_engine.query_duckdb_representation(representation, request)
            return result, ENGINE_DUCKDB
        except Exception as e:
            is_unavailable = _is_engine_unavailable(e)
            if not is_unavailable:
                # 真实错误（SQL/schema 等问题）必须暴露，不静默回退
                raise
            if engine == ENGINE_DUCKDB:
                raise excel_query.ExcelQueryError(
                    'engine_unavailable', f'DuckDB 引擎不可用：{e}'
                )
            logger.warning('DuckDB 引擎不可用，回退 Python 引擎：%s', e)

    result = excel_query.execute_query(representation, request)
    return result, ENGINE_PYTHON


def run_aggregate(
    representation: WorkbookRepresentation,
    payload: Dict[str, Any],
    engine: Optional[str] = ENGINE_AUTO,
) -> Tuple['Any', str]:
    """执行统计（Phase 3A），返回 (AggregateResult, 实际使用的引擎名)。

    与 run_structured_query 相同的引擎策略：auto 优先 DuckDB，仅在引擎**不可用**时
    回退 Python 参考实现；SQL/校验类错误一律抛出，绝不静默掩盖。
    """
    from backend.excel import aggregate as excel_aggregate

    engine = normalize_engine(engine)
    request = excel_aggregate.parse_aggregate_payload(
        payload,
        document_id=representation.document_id,
        user_id=representation.user_id,
    )

    if engine in (ENGINE_AUTO, ENGINE_DUCKDB):
        try:
            from backend.excel import duck as duck_engine

            if not duck_engine.DUCKDB_AVAILABLE:
                raise duck_engine.DuckEngineUnavailable('未安装 duckdb')
            if request.group_by:
                result = excel_aggregate.execute_group_aggregate_duckdb(representation, request)
            else:
                result = excel_aggregate.execute_aggregate_duckdb(representation, request)
            return result, ENGINE_DUCKDB
        except Exception as e:
            if not _is_engine_unavailable(e):
                raise
            if engine == ENGINE_DUCKDB:
                raise excel_query.ExcelQueryError(
                    'engine_unavailable', f'DuckDB 引擎不可用：{e}'
                )
            logger.warning('DuckDB 引擎不可用，回退 Python 引擎（统计）：%s', e)

    if request.group_by:
        result = excel_aggregate.execute_group_aggregate_python(representation, request)
    else:
        result = excel_aggregate.execute_aggregate_python(representation, request)
    return result, ENGINE_PYTHON


def _is_engine_unavailable(exc: Exception) -> bool:
    """区分"引擎不可用"（可回退）与"查询错误"（不可回退）。"""
    try:
        from backend.excel.duck import DuckEngineUnavailable

        if isinstance(exc, DuckEngineUnavailable):
            return True
    except Exception:  # pragma: no cover
        pass
    if isinstance(exc, ImportError):
        return True
    name = type(exc).__name__
    if name in ('ModuleNotFoundError', 'DuckDBPyConnectionException'):
        return True
    # DuckDB 连接/建表阶段抛出的原生异常统一视为不可用
    return any(k in str(exc) for k in ('duckdb', 'DuckDB')) and 'Connection' in str(exc)


def engine_status() -> Dict[str, Any]:
    """供运维/调试查看引擎可用性。"""
    try:
        from backend.excel import duck as duck_engine

        available = bool(duck_engine.DUCKDB_AVAILABLE)
        version = getattr(duck_engine.duckdb, '__version__', None) if available else None
        stats = duck_engine.get_registry().stats() if available else {}
    except Exception as e:  # pragma: no cover
        return {'duckdb_available': False, 'error': str(e)}
    return {'duckdb_available': available, 'duckdb_version': version,
            'default_engine': ENGINE_DUCKDB if available else ENGINE_PYTHON,
            'registry': stats}
