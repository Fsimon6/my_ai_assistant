"""
AI角色服务层

持久化架构（第十九阶段）：
- 单一真相源 = SQLite 数据库（backend.models.character.AICharacter / ai_characters 表）
- CharacterService 的所有 CRUD 直接读写数据库，不再维护内存 dict
- core.character.AICharacter 仅用于 legacy 规则型 mock 端点（speak / stats / batch_speak），
  不作为持久化存储对象
"""
from typing import List, Dict, Optional
from datetime import datetime

from backend.database.base import SessionLocal
from backend.models.character import AICharacter as DBAICharacter, Conversation, Text
from core.character import AICharacter as MemoryAICharacter
from core.advanced_features import (
    AIConversationAnalyzer,
    AIConversationBatchProcessor,
    AICharacterManager,
)


class CharacterService:
    """AI角色服务（数据库持久化）"""

    def __init__(self):
        # 保留管理器实例，仅用于 legacy mock 端点构造内存对象
        self.character_manager = AICharacterManager()

    def _session(self):
        return SessionLocal()

    def _to_int_id(self, character_id):
        try:
            return int(character_id)
        except (TypeError, ValueError):
            return None

    def create_character(
            self,
            name: str,
            system_prompt: str,
            model: str = 'gpt-3.5-turbo',
            api_key: Optional[str] = None,
            advanced: bool = False,
            role: str = 'assistant',
            user_id: Optional[int] = None,
    ) -> Dict:
        """创建角色并写入数据库"""
        db = self._session()
        try:
            character = DBAICharacter(
                name=name,
                system_prompt=system_prompt,
                model=model,
                role_type=role,
                api_key=api_key,
                user_id=user_id,
            )
            db.add(character)
            db.commit()
            db.refresh(character)
            return {
                'success': True,
                'character_id': str(character.id),
                'character': character,
                'message': '角色创建成功',
            }
        except Exception as e:
            db.rollback()
            return {
                'success': False,
                'error': str(e),
                'character_id': None,
            }
        finally:
            db.close()

    def get_character(
            self, character_id: str, user_id: Optional[int] = None
    ) -> Optional[DBAICharacter]:
        """获取角色（按 id；可选按 owner 隔离）"""
        cid = self._to_int_id(character_id)
        if cid is None:
            return None
        db = self._session()
        try:
            query = db.query(DBAICharacter).filter(DBAICharacter.id == cid)
            if user_id is not None:
                query = query.filter(DBAICharacter.user_id == user_id)
            return query.first()
        finally:
            db.close()

    def update_character(
            self,
            character_id: str,
            name: Optional[str] = None,
            system_prompt: Optional[str] = None,
            model: Optional[str] = None,
            api_key: Optional[str] = None,
            user_id: Optional[int] = None,
    ) -> Dict:
        """更新角色（数据库）"""
        cid = self._to_int_id(character_id)
        if cid is None:
            return {'success': False, 'error': '角色不存在'}
        db = self._session()
        try:
            query = db.query(DBAICharacter).filter(DBAICharacter.id == cid)
            if user_id is not None:
                query = query.filter(DBAICharacter.user_id == user_id)
            character = query.first()
            if character is None:
                return {'success': False, 'error': '角色不存在'}

            if name is not None:
                character.name = name
            if system_prompt is not None:
                character.system_prompt = system_prompt
            if model is not None:
                character.model = model
            if api_key is not None:
                character.api_key = api_key

            # 最终态校验：模型与 API Key 必须同填或同空（方案 A，编辑同样适用）
            if bool(character.model) != bool(character.api_key):
                db.rollback()
                return {'success': False, 'error': '模型和 API Key 必须同时填写或同时留空'}

            db.commit()
            db.refresh(character)
            return {
                'success': True,
                'character': character,
                'message': '角色更新成功',
            }
        except Exception as e:
            db.rollback()
            return {'success': False, 'error': str(e)}
        finally:
            db.close()

    def delete_character(
            self, character_id: str, user_id: Optional[int] = None
    ) -> Dict:
        """删除角色（真实从数据库删除）"""
        cid = self._to_int_id(character_id)
        if cid is None:
            return {'success': False, 'error': '角色不存在'}
        db = self._session()
        try:
            query = db.query(DBAICharacter).filter(DBAICharacter.id == cid)
            if user_id is not None:
                query = query.filter(DBAICharacter.user_id == user_id)
            character = query.first()
            if character is None:
                return {'success': False, 'error': '角色不存在'}

            # 级联删除该角色的全部对话与消息（按 user_id + character_id，防误删他人数据）
            conv_query = db.query(Conversation).filter(Conversation.character_id == cid)
            if user_id is not None:
                conv_query = conv_query.filter(Conversation.user_id == user_id)
            conv_ids = [c.id for c in conv_query.all()]
            if conv_ids:
                db.query(Text).filter(Text.conversation_id.in_(conv_ids)).delete(
                    synchronize_session=False
                )
                db.query(Conversation).filter(Conversation.id.in_(conv_ids)).delete(
                    synchronize_session=False
                )

            db.delete(character)
            db.commit()
            return {'success': True, 'message': '角色删除成功'}
        except Exception as e:
            db.rollback()
            return {'success': False, 'error': str(e)}
        finally:
            db.close()

    def list_characters(self, user_id: Optional[int] = None) -> List[Dict]:
        """列出角色（可选按 owner 隔离）"""
        db = self._session()
        try:
            query = db.query(DBAICharacter)
            if user_id is not None:
                query = query.filter(DBAICharacter.user_id == user_id)
            characters = query.order_by(DBAICharacter.id.desc()).all()
            result = []
            for character in characters:
                sp = character.system_prompt or ''
                result.append({
                    'id': str(character.id),
                    'name': character.name,
                    'system_prompt': sp[:100] + '...' if len(sp) > 100 else sp,
                    'model': character.model,
                    'conversation_count': character.total_conversations or 0,
                    'type': 'AICharacter',
                    'created_at': character.created_at.isoformat() if character.created_at else None,
                })
            return result
        finally:
            db.close()

    def _memory_character(self, db_character: DBAICharacter) -> MemoryAICharacter:
        """由数据库行构造内存 AICharacter，供 legacy 规则型 mock 端点使用"""
        return MemoryAICharacter(
            name=db_character.name,
            system_prompt=db_character.system_prompt or '',
            model=db_character.model or 'gpt-3.5-turbo',
        )

    def speak(self, character_id: str, text: str, user_id: Optional[int] = None) -> Dict:
        """与AI角色对话（legacy 规则型 mock）"""
        db_character = self.get_character(character_id, user_id)
        if db_character is None:
            return {'success': False, 'error': '角色不存在'}
        try:
            character = self._memory_character(db_character)
            response = character.speak(text)
            return {
                'success': True,
                'character_id': character_id,
                'response': response,
                'character_name': character.name,
                'timestamp': datetime.now().isoformat(),
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'character_id': character_id,
            }

    def get_character_stats(self, character_id: str, user_id: Optional[int] = None) -> Dict:
        """获取角色统计信息（legacy）"""
        db_character = self.get_character(character_id, user_id)
        if db_character is None:
            return {'success': False, 'error': '角色不存在'}
        try:
            character = self._memory_character(db_character)
            analyzer = AIConversationAnalyzer(character)
            stats = analyzer.get_conversation_stats()
            return {
                'success': True,
                'character_id': character_id,
                'character_name': character.name,
                **stats,
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def batch_speak(self, character_id: str, texts: List[str], user_id: Optional[int] = None) -> Dict:
        """批量对话（legacy）"""
        db_character = self.get_character(character_id, user_id)
        if db_character is None:
            return {'success': False, 'error': '角色不存在'}
        try:
            character = self._memory_character(db_character)
            characters = [character] * len(texts)
            result = AIConversationBatchProcessor.batch_converse(
                characters,
                texts[0],
            )
            responses = []
            success_count = 0
            for i, (char_name, item) in enumerate(result.items()):
                response_data = {
                    'text': texts[i] if i < len(texts) else texts[0],
                    'response': item['response'],
                    'success': item['success'],
                    'timestamp': item['timestamp'],
                    'index': i,
                }
                if item['success']:
                    success_count += 1
                    responses.append(response_data)
            return {
                'success': True,
                'responses': responses,
                'total': len(responses),
                'success_count': success_count,
                'character_id': character_id,
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}


# 创建全局服务实例
character_service = CharacterService()
