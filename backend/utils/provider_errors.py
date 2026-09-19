# -*- coding: utf-8 -*-
"""外部服务（LLM / Embedding）错误的**统一分类**（稳定性补丁）。

目标（不做成"复杂错误平台"，只做最小分类）：
- 把 provider 的 4xx/5xx、欠费、限流、超时、鉴权失败、模型不存在等
  映射成稳定的 ``error_code`` + 面向用户的中文 ``message`` + HTTP 状态码；
- **绝不**把原始 provider JSON、request_id、密钥写进响应体；
- 日志里保留截断并脱敏后的原始信息，便于排障。

约定：
- ``error_code`` 前缀表示出错的服务：``EMBEDDING_`` / ``LLM_`` / ``PROVIDER_``；
- 解析（本地文件）类错误使用 ``DOCUMENT_PARSE_FAILED``，与 provider 问题区分开，
  绝不把"向量化服务不可用"伪装成"Excel 解析失败"（反之亦然）。
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------
DOCUMENT_PARSE_FAILED = 'DOCUMENT_PARSE_FAILED'
UNSUPPORTED_FILE_TYPE = 'UNSUPPORTED_FILE_TYPE'

QUOTA_EXCEEDED = 'QUOTA_EXCEEDED'
AUTH_FAILED = 'AUTH_FAILED'
RATE_LIMITED = 'RATE_LIMITED'
TIMEOUT = 'TIMEOUT'
NETWORK_ERROR = 'NETWORK_ERROR'
MODEL_NOT_FOUND = 'MODEL_NOT_FOUND'
PROVIDER_ERROR = 'PROVIDER_ERROR'
INTERNAL_ERROR = 'INTERNAL_ERROR'

#: 分类关键字（小写匹配原文）。顺序即优先级。
_RULES = (
    (QUOTA_EXCEEDED, (
        'arrearage', 'overdue', 'insufficient_quota', 'quota', 'freetieronly',
        'allocationquota', 'exceeded your current quota', 'balance',
        '欠费', '额度不足', '余额不足', '配额',
    )),
    (AUTH_FAILED, (
        'invalid_api_key', 'incorrect api key', 'invalid api key', 'unauthorized',
        'authentication', 'auth failed', 'permission denied', '401',
        '鉴权失败', '密钥无效',
    )),
    (RATE_LIMITED, (
        'rate limit', 'rate_limit', 'too many requests', '429', '限流', '请求过于频繁',
    )),
    (MODEL_NOT_FOUND, (
        'model_not_found', 'model not found', 'does not exist', 'no such model',
        'unknown model', '模型不存在',
    )),
    (TIMEOUT, (
        'timeout', 'timed out', 'readtimeout', 'apitimeouterror', '超时',
    )),
    (NETWORK_ERROR, (
        'connection error', 'connection refused', 'apiconnectionerror', 'network',
        'dns', 'ssl', 'proxy', 'unreachable', '网络',
    )),
)

#: 密钥/令牌样式的片段（日志与细节里一律脱敏）
_SECRET_RE = re.compile(r'(sk-[A-Za-z0-9._\-]{6,}|Bearer\s+[A-Za-z0-9._\-]{6,})', re.IGNORECASE)
#: 原始细节保留长度上限
_DETAIL_MAX = 400


@dataclass(frozen=True)
class ClassifiedError:
    """分类结果：给用户看 message，给日志看 detail，两者严格分离。"""

    error_code: str
    message: str
    http_status: int
    kind: str          # provider | parser | internal
    detail: str = ''   # 已脱敏、已截断；**只进日志，不进响应**

    def to_response(self) -> dict:
        """响应体（不含任何 provider 原始内容）。"""
        return {'error_code': self.error_code, 'message': self.message}


def sanitize_secret_text(text: Any) -> str:
    """去掉疑似密钥的片段并截断（用于日志）。"""
    s = '' if text is None else str(text)
    s = _SECRET_RE.sub('***', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:_DETAIL_MAX]


def _contains(text: str, keys) -> bool:
    return any(k in text for k in keys)


def _service_prefix(exc: Any, text: str, service_hint: str = '') -> str:
    """判定出错的服务：Embedding / LLM / 通用 Provider。"""
    name = type(exc).__name__.lower()
    if 'embedding' in name or 'embedding' in text or 'embed' in text:
        return 'EMBEDDING_'
    if 'llm' in name or 'chat' in text or 'completion' in text:
        return 'LLM_'
    if service_hint:
        return service_hint
    return 'PROVIDER_'


def _quota_message(prefix: str) -> str:
    what = '向量化（Embedding）服务' if prefix == 'EMBEDDING_' else '大模型服务'
    return (f'{what}额度不足或账户欠费，本次操作未完成。'
            f'请在服务商控制台确认账户状态后重试。')


def _auth_message(prefix: str) -> str:
    what = '向量化（Embedding）服务' if prefix == 'EMBEDDING_' else '大模型服务'
    return (f'{what}鉴权失败（API Key 无效、过期或无该模型权限）。'
            f'请检查 .env 中的 API_KEY / LLM_BASE_URL 配置后重启后端。')


def classify_provider_error(exc: Any, service_hint: str = '') -> ClassifiedError:
    """把 provider/网络类异常分类成稳定的 error_code + 用户可读 message。

    service_hint：无法从异常文本/类型判断服务时的兜底前缀（如 'EMBEDDING_'）。
    """
    text = sanitize_secret_text(exc).lower()
    prefix = _service_prefix(exc, text, service_hint)

    if _contains(text, _RULES[0][1]):
        return ClassifiedError(f'{prefix}{QUOTA_EXCEEDED}', _quota_message(prefix),
                               503, 'provider', sanitize_secret_text(exc))
    if _contains(text, _RULES[1][1]):
        return ClassifiedError(f'{prefix}{AUTH_FAILED}', _auth_message(prefix),
                               502, 'provider', sanitize_secret_text(exc))
    if _contains(text, _RULES[2][1]):
        return ClassifiedError(
            f'{prefix}{RATE_LIMITED}',
            '外部服务触发限流（请求过于频繁），请稍后重试。',
            429, 'provider', sanitize_secret_text(exc))
    if _contains(text, _RULES[3][1]):
        return ClassifiedError(
            f'{prefix}{MODEL_NOT_FOUND}',
            '外部服务上不存在指定的模型（可能是模型名写错或未开通）。'
            '请检查 .env 中的 LLM_MODEL / EMBEDDING_MODEL。',
            502, 'provider', sanitize_secret_text(exc))
    if _contains(text, _RULES[4][1]):
        return ClassifiedError(
            f'{prefix}{TIMEOUT}',
            '外部服务响应超时，本次操作未完成，请稍后重试。',
            504, 'provider', sanitize_secret_text(exc))
    if _contains(text, _RULES[5][1]):
        return ClassifiedError(
            f'{prefix}{NETWORK_ERROR}',
            '无法连接外部服务（网络/DNS/代理异常），请检查网络后重试。',
            502, 'provider', sanitize_secret_text(exc))
    return ClassifiedError(
        f'{prefix}{PROVIDER_ERROR}',
        '外部服务返回错误，本次操作未完成，请稍后重试。',
        502, 'provider', sanitize_secret_text(exc))


def classify_parse_error(exc: Any) -> ClassifiedError:
    """本地文件解析失败（与 provider 问题严格区分）。"""
    detail = sanitize_secret_text(getattr(exc, 'message', None) or exc)
    return ClassifiedError(
        DOCUMENT_PARSE_FAILED,
        f'文档解析失败：{detail}。该文件未被保存，请检查文件是否损坏或格式是否符合要求。',
        400, 'parser', detail)


def classify_upload_error(exc: Any) -> ClassifiedError:
    """上传链路的统一分类入口：解析类 / provider 类 / 其它内部错误。"""
    from backend.excel import query as excel_query

    if isinstance(exc, excel_query.ExcelQueryError):
        return classify_parse_error(exc)

    name = type(exc).__name__
    if name in ('BadZipFile', 'InvalidFileException', 'XLRDError', 'UnicodeDecodeError',
                'EmptyDataError', 'ParserError', 'JSONDecodeError'):
        return classify_parse_error(exc)

    text = sanitize_secret_text(exc).lower()
    if 'embedding' in text or 'embed' in text or '向量' in text or 'chroma' in text:
        return classify_provider_error(exc)
    if any(k in text for k in ('解析', 'parse', '无法读取', '不是有效的', 'unsupported')):
        # "解析/格式"类问题优先按解析失败处理（但不吞掉 provider 报错）
        if not any(k in text for k in ('quota', 'arrearage', 'api key', '401', '429',
                                       'timeout', 'connection')):
            return classify_parse_error(exc)
    if _contains(text, tuple(k for _c, keys in _RULES for k in keys)):
        # 上传链路上唯一的外部调用就是 embedding（摘要 chunk 向量化），
        # 无法从文本判断服务时按 EMBEDDING_ 归类，便于用户定位。
        return classify_provider_error(exc, service_hint='EMBEDDING_')

    return ClassifiedError(
        INTERNAL_ERROR,
        '服务器内部错误，本次上传未完成，请稍后重试或联系管理员。',
        500, 'internal', sanitize_secret_text(exc))


def classify_llm_error(exc: Any) -> ClassifiedError:
    """NL / 对话链路的 LLM 错误分类（服务前缀固定 ``LLM_``）。

    为什么必须有这一层：此前 Excel 自然语言查询遇到 provider 4xx（如
    ``AllocationQuota.FreeTierOnly``）时，会把 **provider 原始报文**
    直接拼进 ``message`` 返回给前端（浏览器上会看到一大段英文 JSON）。
    这里统一成：稳定 error_code + 中文可读 message；原始内容只脱敏进日志。
    """
    return classify_provider_error(exc, service_hint='LLM_')


def log_classified(context: str, classified: ClassifiedError) -> None:
    """按级别记录分类结果（原始细节只进日志，且已脱敏）。"""
    payload = f'{context} error_code={classified.error_code} kind={classified.kind} detail={classified.detail}'
    if classified.kind == 'internal':
        logger.error(payload)
    else:
        logger.warning(payload)
