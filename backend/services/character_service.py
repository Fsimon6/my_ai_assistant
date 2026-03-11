"""
AI角色服务层
"""
import uuid
from typing import List, Dict, Optional
from datetime import datetime

# 导入我们已有的AICharacter类
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))

from core.character import AICharacter, AdvancedAICharacter
from core.advanced_features import AIConversationBatchProcessor, AIConversationAnalyzer, AICharacterManager

class CharacterService:
    """AI角色服务"""

    def __init__(self):
        self.characters: Dict[str, AICharacter] = {}
        self.character_manager = AICharacterManager()

    def creat_character(
            self,
            name: str,
            system_prompt: str,
            model: str = 'gpt-3.5-turbo',
            api_key: Optional[str] = None,
            advanced: bool = False,
            role: str = 'assistant'
    ) -> Dict:
        # 创建AI角色
        try:
            # 生成唯一ID
            character_id = str(uuid.uuid4())

            # 创建角色实例
            if advanced:
                character = AdvancedAICharacter(
                    name=name,
                    system_prompt=system_prompt,
                    model=model,
                    role=role,
                )
            else:
                character = AICharacter(
                    name=name,
                    system_prompt=system_prompt,
                    model=model,
                )

            # 设置API Key
            if api_key:
                try:
                    character.api_key = api_key
                except ValueError as e:
                    return {
                        'success': False,
                        'error': f'API Key验证失败：{str(e)}',
                        'character_id': None
                    }

            # 储存角色
            self.characters[character_id] = character

            # 添加到管理器
            self.character_manager.add_character(character)
            return {
                'success': True,
                'character_id': character_id,
                'character': character,
                'message': '角色创建成功'
            }

        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'character_id': None,
            }

    def get_character(self, character_id: str) -> Optional[AICharacter]:
        """获取角色"""
        return self.characters.get(character_id)

    def update_character(
            self,
            character_id: str,
            name: Optional[str] = None,
            system_prompt: Optional[str] = None,
            model: Optional[str] = None,
            api_key: Optional[str] = None,
    ) -> Dict:
        """更新角色"""
        character = self.get_character(character_id)
        if not character:
            return {
                'success': False,
                'error': '角色不存在'
            }

        try:
            # 更新属性
            if name is not None:
                character.name = name
            if system_prompt is not None:
                character.system_prompt = system_prompt
            if model is not None:
                character.model = model
            if api_key is not None:
                character.api_key = api_key

            return {
                'success': True,
                'character': character,
                'message': '角色更新成功'
            }

        except Exception as e:
            return {
                'success': False,
                'error': str(e),
            }

    def delete_character(self, character_id: str) -> Dict:
        """删除角色"""
        if character_id not in self.characters:
            return {
                'success': False,
                'error': '角色不存在'
            }
        try:
            # 从管理器中移除
            character = self.characters[character_id]

            self.character_manager.remove_character(character.name)

            # 从字典中删除
            del self.characters[character_id]

            return {
                'success': True,
                'message': '角色删除成功'
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
            }

    def list_characters(self) -> List[Dict]:
        """列出所有角色"""
        result = []
        for char_id, character in self.characters.items():
            result.append({
                'id': char_id,
                'name': character.name,
                'system_prompt': character.system_prompt[:100] + '...' if len(
                    character.system_prompt) > 100 else character.system_prompt,
                'model': character.model,
                'conversation_count': len(character),
                'type': character.__class__.__name__,
                'created_at': character.created_at.isoformat() if hasattr(character, 'created_at') else None,
            })
        return result

    def speak(self, character_id: str, text: str) -> Dict:
        """与AI角色对话"""
        character = self.get_character(character_id)
        if not character:
            return {
                'success': False,
                'error': '角色不存在'
            }

        try:
            response = character.speak(text)

            return {
                'success': True,
                'character_id': character_id,
                'response': response,
                'character_name': character.name,
                'timestamp': datetime.now().isoformat()
            }

        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'character_id': character_id,
            }

    def get_character_stats(self, character_id: str) -> Dict:
        """获取角色统计信息"""
        character = self.get_character(character_id)
        if not character:
            return {
                'success': False,
                'error': '角色不存在'
            }

        try:
            # 使用对话分析器
            analyzer = AIConversationAnalyzer(character)
            stats = analyzer.get_conversation_stats()

            # 添加额外信息
            result = {
                'success': True,
                'character_id': character_id,
                'character_name': character.name,
                **stats
            }

            # 如果是高级角色，添加token使用量
            if isinstance(character, AdvancedAICharacter):
                result['token_used'] = character.token_used
                return result

        except Exception as e:
            return {
                'success': False,
                'error': str(e),
            }

    def batch_speak(self, character_id: str, texts: List[str]) -> Dict:
        """批量对话"""
        character = self.get_character(character_id)
        if not character:
            return {
                'success': False,
                'error': '角色不存在'
            }

        try:
            # 创建角色列表（同一个角色重复用于批量对话）
            characters = [character] * len(texts)

            # 使用批量处理器
            result = AIConversationBatchProcessor.batch_converse(
                characters,
                texts[0]  # 当前实现只支持单个消息
            )

            responses = []
            success_count = 0

            for i, (char_name, result) in enumerate(result.items()):
                response_data = {
                    'text': texts[i] if i < len(texts) else texts[0],
                    'response': result['response'],
                    'success': result['success'],
                    'timestamp': result['timestamp'],
                    'index': i
                }

                if result['success']:
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
            return {
                'success': False,
                'error': str(e),
            }


# 创建全局服务实例
character_service = CharacterService()
