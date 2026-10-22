# -*- coding: utf-8 -*-
import os
from typing import Dict, List, Optional, AsyncGenerator
from backend.services.types import ChatMessage, SearchResult
from abc import ABC, abstractmethod
from dataclasses import dataclass
from ..utils.retry import retry, llm_retry_config
from backend.config.settings import settings
import logging


logger = logging.getLogger(__name__)

#: 远端 embedding 单次请求的**条数上限**（2026-10-16 按真实 Provider 校正）。
#: 当前生产 Provider 为 DashScope 兼容端点、模型 text-embedding-v3，实测单次 input 上限为
#: **10** 条（10 条成功；11 条与 20 条均返回 400 "batch size is invalid, it should not be
#: larger than 10."），此前按 20 分片仍会触发 400，故改为 10。
#: 唯一 batch boundary 位于 ``OpenAILikeLLM.generate_embeddings``：任何一次可能传入数百条
#: 文本的上传（TXT/MD/DOCX/PDF 的 chunk、Excel 摘要 chunk）都只在这里分片，其它层不要再分批。
EMBEDDING_BATCH_SIZE = 10


@dataclass
class LLMConfig:
    """LLM配置类"""
    provider: str  # openai / dashscope / zhipu / ollama / local
    api_key: str
    api_secret: Optional[str] = None
    base_url: Optional[str] = None
    model: str = 'ERNIE-3.5-8k'
    embedding_model: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 2000
    timeout: int = 30


def resolve_request_timeout(config: Optional['LLMConfig'] = None) -> float:
    """LLM 请求超时（秒）—— 唯一来源：``settings.LLM_TIMEOUT``。

    取值优先级由 pydantic-settings 决定：进程环境变量 > 项目根 ``.env`` > 默认 30。
    合法范围（1..600）在 ``Settings`` 中校验：非法值在进程启动（Settings 构造）时
    即报错，因此这里**不再**做"回退到另一个值"的兜底。

    说明：
      * ``config`` 参数保留仅为兼容既有调用/测试签名，不再作为超时来源
        （旧实现的 ``ProductionConfig.LLM_TIMEOUT`` / ``LLMConfig.timeout`` 不再参与）；
      * 同一 ``AsyncOpenAI`` client 同时服务 chat 与 embedding（既有实现），
        因此该超时是 client 级；本批不拆分 embedding 的独立超时。
    """
    from backend.config.settings import settings
    return float(settings.LLM_TIMEOUT)


class BaseLLM(ABC):
    """大模型基类"""

    def __init__(self, config: LLMConfig):
        self.config = config

    @abstractmethod
    async def chat_completion(
            self,
            messages: List[ChatMessage],
            stream: bool = True,
            temperature: Optional[float] = None,
    ) -> AsyncGenerator[str, None]:
        """补全聊天接口

        temperature：可选的**单次调用**温度覆盖。
        用途：意图分类 / 参数抽取这类"必须可复现"的任务需要 temperature=0，
        而同一个 LLM 实例又可能被聊天链路复用，因此不能改动实例级配置。
        """
        pass

    @abstractmethod
    async def generate_embeddings(
            self,
            text: List[str],
            model: str = 'text-embedding-v1'
    ) -> List[List[float]]:
        """生成文本向量"""
        pass


class OpenAILikeLLM(BaseLLM):
    """OpenAI 兼容接口（OpenAI / 通义千问 DashScope / 智谱 / 本地 Ollama 等）

    统一使用通用 API_KEY 作为配置入口；各 provider 的默认 base_url 在此处区分，
    允许通过 LLM_BASE_URL 覆盖。Ollama / local 可不传 API_KEY。
    """

    # 各 provider 的默认 OpenAI 兼容 base_url（可被 LLM_BASE_URL 覆盖）
    DEFAULT_BASE_URLS = {
        'openai': 'https://api.openai.com/v1',
        'dashscope': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'zhipu': 'https://open.bigmodel.cn/api/paas/v4',
        'ollama': 'http://localhost:11434/v1',
        'local': 'http://localhost:11434/v1',
    }

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        try:
            from openai import AsyncOpenAI
            base_url = self.config.base_url or self.DEFAULT_BASE_URLS.get(
                self.config.provider, 'https://api.openai.com/v1'
            )
            # Ollama / local 不需要 API Key
            api_key = self.config.api_key or 'not-needed'
            # 超时唯一来源：settings.LLM_TIMEOUT（.env / 环境变量 / 默认 30；范围 1..600）
            self.request_timeout = resolve_request_timeout(self.config)
            self.client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=self.request_timeout,
            )
            logger.info('LLM client 已就绪：provider=%s model=%s timeout=%ss',
                        self.config.provider, self.config.model, self.request_timeout)
        except ImportError:
            raise ImportError('请安装openai：pip install openai')

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        stream: bool = False,
        temperature: Optional[float] = None,
    ) -> AsyncGenerator[str, None]:
        """OpenAI兼容聊天补全（temperature 可单次覆盖，不影响实例配置）"""
        temp = self.config.temperature if temperature is None else temperature
        try:
            if stream:
                response = await self.client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    temperature=temp,
                    max_tokens=self.config.max_tokens,
                    stream=True
                )

                async for chunk in response:
                    # 部分 OpenAI 兼容接口（如 DashScope）会在流末尾追加一个
                    # 仅用于 usage 统计的结束帧，其 choices 为空列表；
                    # 若直接取 choices[0] 会抛 IndexError: list index out of range。
                    choices = getattr(chunk, 'choices', None)
                    if not choices:
                        continue
                    delta = getattr(choices[0], 'delta', None)
                    content = getattr(delta, 'content', None)
                    if content:
                        yield content
            else:
                response = await self.client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    temperature=temp,
                    max_tokens=self.config.max_tokens,
                )
                # 非流式：choices 缺失/为空时返回安全结果，不抛异常
                choices = getattr(response, 'choices', None)
                if not choices:
                    logger.warning('LLM 非流式响应缺少 choices，返回空内容')
                    yield ''
                    return
                message = getattr(choices[0], 'message', None)
                yield getattr(message, 'content', None) or ''

        except Exception as e:
            logger.error(f'OpenAI调用失败：{e}')
            raise

    async def generate_embeddings(
        self,
        texts: List[str],
        model: Optional[str] = None
    ) -> List[List[float]]:
        """生成 OpenAI 兼容 embedding（必须走 embeddings 端点，绝不能误用 chat completions）

        模型优先级：显式传入 > config.embedding_model(settings.EMBEDDING_MODEL) > 兜底默认。

        **分片（2026-10-16 按真实 Provider 校正，唯一 batch boundary）**：远端 embedding API
        对单次 ``input`` 条数有上限（当前模型 text-embedding-v3 实测为 10），而上传链路
        （TXT/MD/DOCX/PDF 的 chunk、Excel 摘要 chunk）可能一次传入数百条，
        因此这里统一按 ``EMBEDDING_BATCH_SIZE``（<=10）**顺序分批**调用，并按输入顺序合并；
        ``embeddings[i]`` 严格对应 ``texts[i]``。

        为什么放在这一层：本函数是**唯一**"接收完整 texts 列表 + 直接调用远端 embedding API"
        的入口（`VectorStoreManager.add_documents` -> Chroma `add_texts` ->
        `AIAssistantEmbeddings.embed_documents` -> 这里），因此 Excel / RAG / VectorStore
        **不得**各自再实现一层 batch，避免多层分片。

        失败处理：任何一批失败都记录 batch 序号 / 总批数 / 批大小 / 异常原因后**原样抛出**，
        绝不"部分成功 + 静默继续"（也绝不打印 API Key、被嵌入文本或向量内容）。
        """
        model = model or self.config.embedding_model or 'text-embedding-3-small'
        if not texts:
            return []
        batches = [texts[i:i + EMBEDDING_BATCH_SIZE]
                   for i in range(0, len(texts), EMBEDDING_BATCH_SIZE)]
        embeddings: List[List[float]] = []
        for index, batch in enumerate(batches, start=1):
            # 只记录批次规模，不记录被嵌入文本 / 向量 / 密钥
            if len(batches) > 1:
                logger.info('Embedding batch: %d/%d, size=%d', index, len(batches), len(batch))
            try:
                response = await self.client.embeddings.create(model=model, input=batch)
            except Exception as e:
                logger.error(
                    '生成embedding失败（batch=%d total=%d size=%d model=%s）：%s: %s',
                    index, len(batches), len(batch), model, type(e).__name__, e)
                raise
            data = list(getattr(response, 'data', None) or [])
            if len(data) != len(batch):
                raise ValueError(
                    'embedding 返回数量与输入不一致（batch=%d/%d size=%d 实得=%d）'
                    % (index, len(batches), len(batch), len(data)))
            embeddings.extend(item.embedding for item in data)
        return embeddings

class LLMFactory:
    """LLM工厂类（配置统一来自 settings）"""

    # provider 是否需要 API_KEY
    PROVIDERS_REQUIRE_KEY = {'openai', 'dashscope', 'zhipu'}
    SUPPORTED_PROVIDERS = {'openai', 'dashscope', 'zhipu', 'ollama', 'local'}

    @staticmethod
    def create_llm(config: LLMConfig) -> BaseLLM:
        """创建LLM实例"""
        if config.provider in LLMFactory.SUPPORTED_PROVIDERS:
            return OpenAILikeLLM(config)
        raise ValueError(f'不支持的provider：{config.provider}')

    @staticmethod
    def from_env() -> BaseLLM:
        """从 settings 创建 LLM（统一配置入口）"""
        provider = settings.LLM_PROVIDER
        if provider not in LLMFactory.SUPPORTED_PROVIDERS:
            raise ValueError(f'不支持的provider：{provider}')

        # 按 provider 判断是否必须提供 API_KEY
        api_key = settings.API_KEY
        if provider in LLMFactory.PROVIDERS_REQUIRE_KEY and not api_key:
            raise ValueError(f'provider={provider} 需要设置 API_KEY 环境变量')

        config = LLMConfig(
            provider=provider,
            api_key=api_key,
            base_url=settings.LLM_BASE_URL,
            model=settings.LLM_MODEL,
            embedding_model=settings.EMBEDDING_MODEL,
            temperature=settings.LLM_TEMPERATURE,
            max_tokens=settings.LLM_MAX_TOKENS,
        )

        return LLMFactory.create_llm(config)


# 单例实例
_llm_instance = None

def get_llm() -> BaseLLM:
    """获取LLM单例"""
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = LLMFactory.from_env()
    return _llm_instance