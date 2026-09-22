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
            self.client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
            )
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
        """
        model = model or self.config.embedding_model or 'text-embedding-3-small'
        try:
            response = await self.client.embeddings.create(
                model=model,
                input=texts,
            )
            return [data.embedding for data in response.data]
        except Exception as e:
            logger.error(f'生成embedding失败：{e}')
            raise

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
            temperature=float(os.getenv('LLM_TEMPERATURE', '0.7')),
            max_tokens=int(os.getenv('LLM_MAX_TOKENS', '2000')),
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