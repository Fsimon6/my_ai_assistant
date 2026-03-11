# -*- coding: utf-8 -*-
import os
from typing import Dict, List, Optional, AsyncGenerator
from services.types import ChatMessage, SearchResult
from abc import ABC, abstractmethod
from dataclasses import dataclass
from ..utils.retry import retry, llm_retry_config
import logging


logger = logging.getLogger(__name__)

@dataclass
class LLMConfig:
    """LLM配置类"""
    provider: str  # qianfan
    api_key: str
    api_secret: Optional[str] = None
    base_url: Optional[str] = None
    model: str = 'ERNIE-3.5-8k'
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
            stream: bool = True
    ) -> AsyncGenerator[str, None]:
        """补全聊天接口"""
        pass

    @abstractmethod
    async def generate_embeddings(
            self,
            text: List[str],
            model: str = 'text-embedding-v1'
    ) -> List[List[float]]:
        """生成文本向量"""
        pass


class QianfanLLM(BaseLLM):
    """百度千帆大模型(带重试)"""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        try:
            import qianfan
            self.client = qianfan.ChatCompletion()
        except ImportError:
            raise ImportError('请安装qianfan：pip install qianfan')

    @retry(llm_retry_config)
    async def chat_completion(
            self,
            messages: List[Dict[str, str]],
            stream: bool = False
    ) -> AsyncGenerator[str, None]:
        """千帆聊天补全(带重试)"""
        try:
            import qianfan

            # 转换消息格式
            qianfan_messages = []
            for msg in messages:
                if msg['role'] == 'system':
                    continue  # 千帆不支持system消息
                qianfan_messages.append(msg)

            if stream:
                resp = self.client.do(
                    model=self.config.model,
                    messages=qianfan_messages,
                    temperature=self.config.temperature,
                    stream=True
                )

                for chunk in resp:
                    if chunk.get('code') == 200:
                        content = chunk.get('result', '')
                        yield content
                    else:
                        logger.error(f'千帆流式响应错误：{chunk}')
            else:
                resp = self.client.do(
                    model=self.config.model,
                    messages=qianfan_messages,
                    temperature=self.config.temperature,
                )

                if resp.get('code') == 200:
                    yield resp.get('result', '')
                else:
                    error_msg = resp.get('message', '未知错误')
                    logger.error(f'千帆API错误：{error_msg}')
                    raise Exception(f'千帆API错误：{error_msg}')
        except Exception as e:
            logger.error(f'千帆调用失败：{e}')
            raise

    async def generate_embeddings(
        self,
        texts: List[str],
        model: str = 'embedding-v1'
    ) -> List[List[float]]:
        """生成千帆embedding"""
        try:
            import qianfan
            emb_client = qianfan.Embedding()

            embeddings = []
            for text in texts:
                resp = emb_client.do(
                    model=model,
                    input=[text],
                )

                if resp.get('code') == 200:
                    data = resp.get('data', [])
                    if data:
                        embeddings.append(data[0].get('embedding', []))
                    else:
                        embeddings.append([])
                else:
                    logger.warning(f'生成embedding失败：{resp.get("message")}')
                    embeddings.append([])

            return embeddings

        except Exception as e:
            logger.error(f'生成embedding失败：{e}')
            return [[] for _ in texts]


class OpenAILikeLLM(BaseLLM):
    """OpenAI兼容接口（包括阿里灵积等）"""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        try:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url or 'https://api.openai.com/v1'
            )
        except ImportError:
            raise ImportError('请安装openai：pip install openai')

    async def chat_completion(
        self,
        messages: List[SearchResult],
        stream: bool = False
    ) -> AsyncGenerator[str, None]:
        """OpenAI兼容聊天补全"""
        try:
            if stream:
                response = await self.client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    stream=True
                )

                async for chunk in response:
                    if chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
                    else:
                        response = await self.client.chat.completions.create(
                            model=self.config.model,
                            messages=messages,
                            temperature=self.config.temperature,
                            max_tokens=self.config.max_tokens,
                        )

                        yield response.choices[0].message.content

        except Exception as e:
            logger.error(f'OpenAI调用失败：{e}')
            raise

    async def generate_embeddings(
        self,
        texts: List[str],
        model: str = 'text-embedding-3-small'
    ) -> List[List[float]]:
        """生成Open AI embedding"""
        try:
            response = await self.client.completions.create(
                model=model,
                input=texts,
            )

            return [data.embedding for data in response.data]

        except Exception as e:
            logger.error(f'生成embedding失败：{e}')
            return [[] for _ in texts]

class LLMFactory:
    """LLM工厂类"""

    @staticmethod
    def create_llm(config: LLMConfig) -> BaseLLM:
        """创建LLM实例"""
        if config.provider == 'qianfan':
            return QianfanLLM(config)
        elif config.provider in ['openai', 'dashscope', 'zhipu']:
            return OpenAILikeLLM(config)
        else:
            raise ValueError(f'不支持的provider：{config.provider}')

    @staticmethod
    def from_env() -> BaseLLM:
        """从环境变量创建LLM"""
        provider = os.getenv('LLM_PROVIDER', 'qianfan')
        api_key = os.getenv(f'{provider.upper()}_API_KEY')

        if not api_key:
            raise ValueError(f'请设置{provider.upper()}_API_KEY环境变量')

        config = LLMConfig(
            provider=provider,
            api_key=api_key,
            api_secret=os.getenv(f'{provider.upper()}_API_SECRET'),
            model=os.getenv('LLM_MODEL', 'ERNIE-3.5-8k'),
            temperature=os.getenv('LLM_TEMPERATURE', '0.7'),
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