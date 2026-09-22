"""
对话历史持久化服务（第二十阶段）

存储结构（复用现有表，不新增表、不新增字段）：
- conversations: 一次对话容器，按 (user_id, character_id) 唯一关联一个“当前对话”
    - character_id: Integer（无 FK 约束，仅语义关联 ai_characters.id）
    - user_id:       Integer（无 FK 约束，仅语义关联 users.id）
    - texts:         JSON（NOT NULL，遗留冗余字段；本阶段不维护，置 []）
- texts:            单条消息
    - conversation_id: Integer（无 FK 约束，仅语义关联 conversations.id）
    - role:          'user' | 'assistant'
    - content:       完整文本
    - model / token_used / created_at: 可选
    - meta_info:     JSON（复用已有列，不新增字段）
                     本阶段约定写入 {'source': 'chat' | 'rag' | 'excel'}

设计要点：
- Character(普通 Chat) 的对话 = 每个 (用户, 角色) 一个当前 Conversation
- 用户消息在流式开始前写入；AI 回复在完整流式结束后写入一次（不按 chunk 写库）
- 多轮：每次发送先加载该 Conversation 的历史 Text，注入 LLM messages
- 消息来源隔离：三类能力统一打 source 标记（chat / rag / excel）。
  Excel 表格查询属独立工具能力，其整轮（user + assistant）不参与普通聊天上下文，
  由调用方通过 get_history(exclude_sources=[SOURCE_EXCEL]) 丢弃。
  历史遗留消息 meta_info 为空 → 视为「无来源标记」，永不被排除（向后兼容）。
"""
from typing import List, Dict, Optional

from backend.database.base import SessionLocal
from backend.models.character import Conversation, Text

# 消息来源标记：写入 texts.meta_info['source']，用于按能力隔离 LLM 上下文
SOURCE_CHAT = 'chat'      # 普通角色聊天
SOURCE_RAG = 'rag'        # 知识库检索问答
SOURCE_EXCEL = 'excel'    # Excel 表格查询（独立工具能力）


class ConversationService:
    """对话历史服务（数据库持久化，conversations/texts 表）"""

    def get_or_create_conversation_id(self, user_id: int, character_id: int) -> int:
        """获取或创建 (user_id, character_id) 对应的当前对话，返回 conversation.id"""
        db = SessionLocal()
        try:
            conv = db.query(Conversation).filter(
                Conversation.user_id == user_id,
                Conversation.character_id == character_id,
            ).first()
            if conv is None:
                conv = Conversation(
                    user_id=user_id,
                    character_id=character_id,
                    title='',
                    texts=[],
                )
                db.add(conv)
                db.commit()
                db.refresh(conv)
            return conv.id
        finally:
            db.close()

    def get_existing_conversation_id(self, user_id: int, character_id: int) -> Optional[int]:
        """仅查询已存在的对话 id；不存在返回 None（用于读历史，不自动创建）"""
        db = SessionLocal()
        try:
            conv = db.query(Conversation).filter(
                Conversation.user_id == user_id,
                Conversation.character_id == character_id,
            ).first()
            return conv.id if conv else None
        finally:
            db.close()

    def get_history(
            self,
            conversation_id: int,
            exclude_sources: Optional[List[str]] = None,
    ) -> List[Dict]:
        """按时间顺序返回对话消息 [{'role','content','created_at','meta_info'}]

        exclude_sources: 需要整体丢弃的消息来源列表（按 meta_info['source'] 判定）。
            用于隔离独立工具能力——例如普通聊天传入 [SOURCE_EXCEL]，则 Excel 表格
            查询的整轮（user + assistant）都不会进入 LLM 上下文。
            历史遗留消息 meta_info 为空 → 视为「无来源标记」，永不排除（向后兼容）。
        """
        db = SessionLocal()
        try:
            texts = db.query(Text).filter(
                Text.conversation_id == conversation_id
            ).order_by(Text.id.asc()).all()
            excluded = set(exclude_sources or [])
            messages: List[Dict] = []
            for t in texts:
                meta = t.meta_info if isinstance(t.meta_info, dict) else None
                if excluded and (meta or {}).get('source') in excluded:
                    continue
                messages.append({
                    'role': t.role,
                    'content': t.content,
                    'created_at': t.created_at.isoformat() if t.created_at else None,
                    'meta_info': meta,
                })
            return messages
        finally:
            db.close()

    def append_message(
            self,
            conversation_id: int,
            role: str,
            content: str,
            model: Optional[str] = None,
            meta_info: Optional[Dict] = None,
    ) -> None:
        """写入一条消息（user 在流式前；assistant 在流式完整结束后写一次）

        meta_info: 消息元数据（本阶段为 {'source': SOURCE_CHAT|SOURCE_RAG|SOURCE_EXCEL}）。
            默认 None → 与旧行为完全一致（列写 NULL，不参与任何来源过滤）。
        """
        db = SessionLocal()
        try:
            text = Text(
                conversation_id=conversation_id,
                role=role,
                content=content,
                model=model,
                meta_info=meta_info,
            )
            db.add(text)
            db.commit()
        finally:
            db.close()


    def clear_conversation(self, user_id: int, character_id: int) -> bool:
        """清空指定 (user, character) 的当前对话：删除其全部 Text 与 Conversation 本身。
        幂等：若对话不存在，返回 True。不删除 Character / User / 其他 Conversation。"""
        db = SessionLocal()
        try:
            conv = db.query(Conversation).filter(
                Conversation.user_id == user_id,
                Conversation.character_id == character_id,
            ).first()
            if conv is None:
                return True
            db.query(Text).filter(Text.conversation_id == conv.id).delete()
            db.delete(conv)
            db.commit()
            return True
        finally:
            db.close()


# 创建全局实例
conversation_service = ConversationService()
