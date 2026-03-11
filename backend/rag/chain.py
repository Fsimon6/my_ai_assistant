from typing import List, Dict, Any, Optional
from backend.services.llm_service import BaseLLM
from backend.prompts.template_loader import get_prompt_template


class RAGChain:
    """RAG链条：组合检索结果和大模型生成"""

    def __init__(self, llm: BaseLLM, prompt_template: str = "rag"):
        self.llm = llm
        self.prompt_template = prompt_template

    async def run(
        self,
        query: str,
        context_docs: List[Dict[str, Any]],
        history: Optional[List[Dict]] = None,
    ) -> str:
        """执行链条：根据查询和上下文生成回答"""
        # 构建上下文文本
        context = "\n\n".join([
            f"[{i+1}] {doc.get('content', '')}" for i, doc in enumerate(context_docs)
        ])

        # 获取提示词模板
        prompt = get_prompt_template(self.prompt_template, {
            "question": query,
            "context": context,
            "history": history or []
        })

        # 构造消息列表（BaseLLM 的 chat_completion 期望 List[Dict]）
        messages = [
            {"role": "user", "content": prompt}
        ]

        # 调用大模型，收集所有块
        full_answer = ""
        async for chunk in self.llm.chat_completion(messages, stream=False):
            full_answer += chunk

        return full_answer